/* Third-Party Modified Version of the webMUSHRA Software — PLC platform,
 * 2026-10-06: native accessible UI around the pinned webMUSHRA audio engine;
 * generated YAML, immutable invitation orders, acknowledged async saves,
 * per-tab IndexedDB recovery, bounded loading, and a same-origin theme bridge.
 * 2026-10-16: deployment-configured decoded-audio budgets and loading timeout,
 * size-specific errors, loading progress and retry for full-track trials;
 * compact shared rating scale and blurred primary-color fader focus.
 * See LICENSE.txt, THIRD-PARTY-NOTICES.txt and source.zip. No legacy sender/PHP.
 */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const MIB = 1024 * 1024, WAV_HEADER_ALLOWANCE = 4096;
  let maxDecodedBytes = 256 * MIB, audioTimeoutMs = 120000, loadFailed = false;
  function configureAudioLoading(limits) {
    if (limits == null) return; // Compatibility with older checkpoint payloads.
    if (!Number.isSafeInteger(limits.max_decoded_bytes) || limits.max_decoded_bytes < MIB || limits.max_decoded_bytes > 1024 * MIB ||
        !Number.isSafeInteger(limits.timeout_seconds) || limits.timeout_seconds < 5 || limits.timeout_seconds > 600) {
      throw new Error('Invalid audio loading limits. Contact the study owner.');
    }
    maxDecodedBytes = limits.max_decoded_bytes;
    audioTimeoutMs = limits.timeout_seconds * 1000;
  }
  function audioBudgetError(requiredBytes) {
    return new Error(`This trial needs approximately ${(requiredBytes / MIB).toFixed(2)} MiB of decoded audio; the allowed limit is ${(maxDecodedBytes / MIB).toFixed(0)} MiB. Contact the study owner to adjust the audio loading budget or study material.`);
  }
  let checkpoint, nonce, context, control, config, api, storageKey, database;
  let index = 0, pending = null, flight = null, loader = null, generation = 0;
  let volumeSetup = true;
  const trialInstructions = $('instructions').cloneNode(true);
  const volumeInstructions = 'Put on your headphones and play the reference excerpt. Starting at a low device volume, adjust the playback slider and your device volume until the audio is clear and comfortable, never uncomfortably loud. When ready, continue to the trials. Keep the same headphones, playback volume, and device volume for the rest of the study so your ratings are comparable.';
  let ready = false, locked = false, navigating = false, pollTimer, saveTimer;
  const rows = new Map(), changes = new Map();
  let tracks = [], focusedTrack = 0, playingTrack = -1, playbackIntent = false, playRequest = 0;
  function syncPlayback() {
    const playing = Boolean(playbackIntent && control?.audioPlaying && context?.state === 'running');
    tracks.forEach((track, i) => {
      const active = playing && i === playingTrack;
      track.element.classList.toggle('is-playing', active);
      if (track.button) {
        const pressed = String(active), label = active ? 'Pause' : track.idleLabel || 'Play';
        if (track.button.getAttribute('aria-pressed') !== pressed) {
          track.button.setAttribute('aria-pressed', pressed);
          track.button.setAttribute('aria-label', `${active ? 'Pause' : 'Play'} ${track.name}`);
        }
        if (track.button.textContent !== label) track.button.textContent = label;
      }
    });
    $('position').disabled = !ready || !writable();
    const text = playing ? `Playing · ${tracks[playingTrack]?.name || 'Reference'}` : 'Paused';
    if ($('playback-state').textContent !== text) $('playback-state').textContent = text;
    $('playback-state').classList.toggle('is-playing', playing);
  }
  function pausePlayback() {
    ++playRequest; playbackIntent = false;
    control?.pause(); syncPlayback();
  }
  function renderTimeline(position = control?.getPosition() || 0) {
    const duration = control?.getDuration() || 0;
    const rate = context?.sampleRate || 48000;
    const time = samples => {
      const seconds = Math.max(0, Math.floor(samples / rate));
      return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
    };
    $('position').value = duration ? Math.min(1, position / duration) : 0;
    $('playback-clock').textContent = `${time(position)} / ${time(duration)}`;
  }
  function focusTrack(position, moveKeyboardFocus = false) {
    focusedTrack = position;
    tracks.forEach((track, i) => {
      track.element.classList.toggle('track-focused', i === position);
      track.element.tabIndex = i === position ? 0 : -1;
    });
    if (moveKeyboardFocus) {
      const element = tracks[position]?.element;
      element?.focus({ preventScroll: true });
      // Keep a focused fader visible without scrolling the surrounding study.
      const scroller = element?.closest('.rating-scroll');
      if (scroller) {
        const column = element.getBoundingClientRect(), viewport = scroller.getBoundingClientRect();
        if (column.left < viewport.left) scroller.scrollLeft -= viewport.left - column.left;
        else if (column.right > viewport.right) scroller.scrollLeft += column.right - viewport.right;
      }
    }
  }
  function registerTrack(track) {
    const position = tracks.length;
    tracks.push(track);
    track.element.addEventListener('pointerdown', () => focusTrack(position, true));
    track.element.addEventListener('click', () => focusTrack(position));
    track.element.addEventListener('focusin', () => focusTrack(position));
  }
  async function playTrack(position, toggle = true) {
    const track = tracks[position], engine = control;
    if (!track || !ready || !writable() || navigating) return;
    focusTrack(position);
    if (toggle && playbackIntent && engine.audioPlaying && context.state === 'running' && playingTrack === position) {
      pausePlayback(); return;
    }
    const request = ++playRequest;
    try {
      await context.resume();
      if (request !== playRequest || engine !== control || !ready || !writable() || navigating) return;
      if (context.state !== 'running') throw new Error('Audio could not start. Use a supported desktop browser.');
      const cursor = engine.getPosition(), switching = playingTrack >= 0 && playingTrack !== position;
      const atEnd = cursor >= engine.getDuration();
      if (atEnd) engine.setPosition(0, false);
      playingTrack = position; playbackIntent = true;
      track.play();
      // Switching selects another buffer on the SAME timeline, including a
      // pending seek or a paused position. Only explicit rewind/end restarts.
      if (switching && !atEnd) engine.setPosition(cursor, false);
      if (track.slider) {
        track.slider.disabled = false;
        track.confirm.disabled = rows.get(key(track.row))?.answered === true;
      }
      syncPlayback();
    } catch (failure) { error(failure.message); }
  }
  function trialKeydown(event) {
    if (volumeSetup || !ready || !writable() || navigating || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.target.closest('a, textarea, select, input:not([type="range"]):not([type="checkbox"])')) return;
    const inTrack = tracks.some(track => track.element === event.target || track.element.contains(event.target));
    const key = event.key;
    if (!inTrack) return;
    if (![' ', 'ArrowLeft', 'ArrowUp', 'ArrowDown', 'Tab', 'Enter', 'r', 'R', 'Escape'].includes(key)) return;
    if (key === 'Escape') {
      event.preventDefault();
      const exit = $('next').disabled ? $('save') : $('next');
      exit.focus();
      return;
    }
    event.preventDefault();
    if (event.repeat && !['ArrowUp', 'ArrowDown'].includes(key)) return;
    const track = tracks[focusedTrack];
    if (key === 'Tab') focusTrack((focusedTrack + (event.shiftKey ? -1 : 1) + tracks.length) % tracks.length, true);
    else if (key === ' ') void playTrack(focusedTrack, true);
    else if (key === 'ArrowLeft') control.setPosition(0, false);
    else if (key === 'ArrowUp' || key === 'ArrowDown') {
      if (!track?.slider || track.slider.disabled) return;
      const step = event.shiftKey ? 20 : 5;
      track.slider.value = String(Math.max(0, Math.min(100, track.slider.valueAsNumber + (key === 'ArrowUp' ? step : -step))));
      changed(track.row, track.slider, track.label);
    } else if (key === 'Enter' && !$('next').disabled) void navigate(1);
    else if (key.toLowerCase() === 'r') {
      $('loop').checked = !$('loop').checked;
      control.setLoopingActive($('loop').checked);
    }
  }
  document.addEventListener('keydown', trialKeydown);
  const key = row => `${row.trial_id}:${row.condition_id}`;
  const status = text => { $('status').textContent = text; };
  const error = text => { $('error').textContent = text; $('error').hidden = !text; };
  const same = (a, b) => a && b && a.score === b.score && a.answered === b.answered;
  const notify = type => parent.postMessage({ type, nonce, session_id: checkpoint.session_id }, location.origin);
  const writable = () => !locked && checkpoint.status !== 'completed' && checkpoint.study_status === 'collecting';

  async function request(url, options = {}) {
    const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 30000);
    try {
      const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...options, signal: abort.signal });
      const result = await response.json();
      if (!response.ok) {
        const failure = new Error(typeof result.detail === 'string' ? result.detail : 'Request rejected. Reload the saved session before retrying.');
        failure.status = response.status; throw failure;
      }
      if (result.session_id !== checkpoint.session_id || !Number.isSafeInteger(result.revision)) throw new Error('Invalid session acknowledgement');
      return result;
    } finally { clearTimeout(timer); }
  }
  async function openDatabase() {
    return new Promise(resolve => {
      try {
        const request = indexedDB.open('plc-listening-drafts', 1);
        request.onupgradeneeded = () => request.result.createObjectStore('sessions');
        request.onsuccess = () => resolve(request.result);
        request.onerror = request.onblocked = () => resolve(null);
      } catch { resolve(null); }
    });
  }
  async function disk(mode, value) {
    if (!database) return null;
    return new Promise((resolve, reject) => {
      const transaction = database.transaction('sessions', mode === 'get' ? 'readonly' : 'readwrite');
      const store = transaction.objectStore('sessions');
      const request = mode === 'get' ? store.get(storageKey) : mode === 'delete' ? store.delete(storageKey) : store.put(value, storageKey);
      transaction.oncomplete = () => resolve(request.result ?? null);
      transaction.onerror = transaction.onabort = () => reject(new Error('Offline storage unavailable. Keep this page open until changes are saved.'));
    });
  }
  async function remember() {
    try { await disk('put', { revision: checkpoint.revision, current_trial_id: checkpoint.trial_order[index], changes: [...changes.values()].map(row => ({ ...row })), pending }); }
    catch (failure) { error(failure.message); }
    notify(changes.size || pending ? 'plc-listening-dirty' : 'plc-listening-saved');
  }
  function buttons() {
    const busy = Boolean(flight) || navigating;
    $('volume-continue').disabled = !ready || !writable() || busy;
    $('previous').disabled = !ready || !writable() || busy || index === 0;
    $('next').disabled = !ready || !writable() || busy || !answeredPage();
    $('save').disabled = !ready || !writable() || busy;
    $('retry').hidden = (!pending && !loadFailed) || busy || !writable();
    $('retry').textContent = pending ? 'Retry unchanged request' : 'Retry audio loading';
    $('next').textContent = index === checkpoint.trial_order.length - 1 ? 'Finish and submit' : 'Next trial';
  }
  function answeredPage() {
    const trial = checkpoint.trial_order[index];
    return checkpoint.condition_orders[trial].every(id => rows.get(`${trial}:${id}`)?.answered);
  }
  function completeCoverage() {
    return checkpoint.trial_order.every(trial => checkpoint.condition_orders[trial].every(id => rows.get(`${trial}:${id}`)?.answered));
  }
  function applyAcknowledgement(saved, sent) {
    checkpoint = saved;
    const acknowledged = new Map(saved.responses.map(row => [key(row), row]));
    for (const [id, row] of changes) if (same(row, acknowledged.get(id))) changes.delete(id);
    // Edits made during an in-flight draft save must not be overwritten by its ACK.
    for (const row of saved.responses) if (!changes.has(key(row))) rows.set(key(row), { ...row });
    if (sent.complete && saved.status !== 'completed') throw new Error('Completion has not been acknowledged');
  }
  async function sendPending() {
    const sent = pending;
    if (!sent) return;
    const saved = await request(`${api}/checkpoint`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(sent) });
    applyAcknowledgement(saved, sent);
    pending = null;
    await remember();
    if (saved.status === 'completed') { await disk('delete'); status('All responses have been saved. Thank you.'); notify('plc-listening-complete'); }
  }
  async function flush(complete = false, target = checkpoint.trial_order[index]) {
    if (!writable()) throw new Error('Collection/session is read-only');
    if (flight) await flight;
    const work = async () => {
      error('');
      if (pending) await sendPending();
      if (checkpoint.status === 'completed') return;
      if (!changes.size && !complete && target === checkpoint.current_trial_id) return;
      pending = { request_id: crypto.randomUUID(), expected_revision: checkpoint.revision, current_trial_id: target,
        responses: [...rows.values()].map(row => ({ ...row })), complete,
        audio_sample_rate: context.sampleRate, browser: navigator.userAgent.slice(0, 256) };
      // Persist the exact request before sending. A lost ACK is retried unchanged.
      await remember(); status('Saving…'); await sendPending();
      status(changes.size ? 'Earlier changes saved; newer edits are pending.' : 'Checkpoint saved.');
    };
    flight = work(); buttons();
    try { await flight; }
    catch (failure) {
      error(failure.message || 'Save failed. Your pending request is retained; reconnect and retry.');
      if ([403, 409].includes(failure.status)) {
        locked = true; $('discard').hidden = false; status('Session changed or collection closed. Do not overwrite another tab’s answers.');
      }
      throw failure;
    } finally { flight = null; buttons(); }
  }
  function changed(row, slider, label) {
    if (!writable() || navigating) return;
    const score = Math.round(slider.valueAsNumber);
    if (!Number.isFinite(score) || score < 0 || score > 100) return;
    const next = { ...row, answered: true, score };
    rows.set(key(next), next); changes.set(key(next), next); slider.value = String(score);
    label.textContent = String(score);
    label.classList.remove('is-unanswered'); label.setAttribute('aria-label', `Score ${score}`);
    const confirm = tracks.find(track => track.row && key(track.row) === key(row))?.confirm;
    if (confirm) { confirm.textContent = '✓ Rated'; confirm.disabled = true; }
    void remember(); buttons();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { if (!pending && !flight && writable()) void flush().catch(() => {}); }, 800);
  }
  function clearAudio() {
    ++playRequest; playbackIntent = false; syncPlayback();
    loader?.abort(); loader = null;
    if (control) { control.freeAudio(); control = null; }
    ready = false;
    tracks = []; focusedTrack = 0; playingTrack = -1;
  }
  async function loadPage() {
    const currentGeneration = ++generation;
    loadFailed = false; error('');
    clearAudio(); $('conditions').replaceChildren(); buttons();
    $('reference').disabled = true;
    const trial = checkpoint.trial_order[volumeSetup ? 0 : index], page = config.pages.find(page => page.id === trial);
    const volumePage = config.pages.find(page => page.type === 'volume');
    if (volumeSetup) $('instructions').textContent = volumePage?.content || volumeInstructions;
    else $('instructions').replaceChildren(...Array.from(trialInstructions.childNodes, node => node.cloneNode(true)));
    for (const id of ['quality-scale', 'trial-navigation', 'rating-help', 'rating-panel', 'conditions', 'keyboard-help']) $(id).hidden = volumeSetup;
    $('keyboard-help').open = !volumeSetup && index === 0;
    $('volume-continue').hidden = !volumeSetup;
    $('volume-label').hidden = !volumeSetup;
    if (!page || page.type !== 'mushra') throw new Error('Published MUSHRA configuration does not match this invitation');
    $('title').textContent = volumeSetup ? 'Adjust your listening volume' : `Trial ${index + 1} of ${checkpoint.trial_order.length}`;
    status('Loading audio…'); loader = new AbortController();
    const signal = loader.signal;
    const paths = new Map(volumeSetup ? [] : Object.entries(page.stimuli)); paths.set('reference', page.reference);
    const order = volumeSetup ? ['reference'] : checkpoint.condition_orders[trial];
    if (!order || new Set(order).size !== paths.size || order.some(id => !paths.has(id))) throw new Error('Condition order/configuration mismatch');
    const stimuli = new Map(); let cursor = 0, decodedBytes = 0, estimatedBytes = 0, loadedFiles = 0, timedOut = false;
    const currentLoader = loader;
    const entries = [...paths.entries()];
    async function worker() {
      while (cursor < entries.length) {
        const [id, path] = entries[cursor++];
        if (typeof path !== 'string' || !/^audio\/[a-z0-9-]+\.wav$/.test(path)) throw new Error('Invalid published audio path');
        const timer = setTimeout(() => { timedOut = true; currentLoader.abort(); }, audioTimeoutMs);
        try {
          const response = await fetch(`${api}/${path}`, { credentials: 'same-origin', signal });
          if (!response.ok) throw new Error('Audio is unavailable. Reload after checking your invitation and connection.');
          const length = Number(response.headers.get('content-length'));
          if (!Number.isSafeInteger(length) || length <= 0) throw new Error('Audio size information is unavailable. Contact the study owner.');
          // Exported WAVs contain float32 PCM plus a small header. Reserve
          // before decoding, but do not charge WAV headers as decoded samples.
          const ratio = context.sampleRate / 48000;
          estimatedBytes += Math.ceil(Math.max(0, length - WAV_HEADER_ALLOWANCE) * ratio);
          if (length > maxDecodedBytes + WAV_HEADER_ALLOWANCE || estimatedBytes > maxDecodedBytes) {
            throw audioBudgetError(Math.max(length - WAV_HEADER_ALLOWANCE, estimatedBytes));
          }
          const encoded = await response.arrayBuffer();
          const buffer = await context.decodeAudioData(encoded);
          if (signal.aborted || currentGeneration !== generation) return;
          decodedBytes += buffer.length * buffer.numberOfChannels * 4;
          if (![1, 2].includes(buffer.numberOfChannels)) throw new Error('Only mono/stereo audio is supported.');
          if (decodedBytes > maxDecodedBytes) throw audioBudgetError(decodedBytes);
          const stimulus = new Stimulus(id, path); stimulus.setAudioBuffer(buffer); stimuli.set(id, stimulus);
          status(`Loading audio: ${++loadedFiles} of ${entries.length} files ready…`);
        } finally { clearTimeout(timer); }
      }
    }
    const workers = Array.from({ length: Math.min(3, entries.length) }, worker);
    try {
      await Promise.all(workers);
      if (timedOut) throw new Error('Audio loading timed out');
    } catch (failure) {
      currentLoader.abort();
      // decodeAudioData cannot be cancelled. Let aborted workers settle before
      // allowing retry, rather than overlapping old and new decoding buffers.
      await Promise.allSettled(workers);
      stimuli.clear(); loadFailed = true;
      status('Audio loading failed. Your saved ratings are unchanged.'); buttons();
      if (timedOut) throw new Error(`Audio loading exceeded ${audioTimeoutMs / 1000} seconds for a file. Check your connection and retry, or contact the study owner.`);
      throw failure;
    }
    if (signal.aborted || currentGeneration !== generation) return;
    const reference = stimuli.get('reference'), ordered = order.map(id => stimuli.get(id));
    if (ordered.some(stimulus => stimulus.getAudioBuffer().length !== reference.getAudioBuffer().length)) throw new Error('Audio lengths do not match');
    control = new MushraAudioControl(context, 2048, reference, ordered, { sendError: error }, false, false, false, false, false);
    control.initAudio();
    control.setLoopingActive($('loop').checked);
    $('break').hidden = volumeSetup || index === 0 || index % 10 !== 0;
    control.addEventListener(event => {
      if (event.name === 'processUpdate') renderTimeline(event.currentSample);
      if (['pauseTriggered', 'stopTriggered', 'playbackEnded'].includes(event.name)) playbackIntent = false;
      syncPlayback();
    });
    $('conditions').style.gridTemplateColumns = `repeat(${order.length}, var(--condition-width))`;
    // Discard the preceding page's focus listeners, including calibration.
    $('reference').replaceWith($('reference').cloneNode(true));
    $('reference').classList.add('play-toggle');
    registerTrack({ element: $('reference'), button: $('reference'), name: 'Reference', idleLabel: 'Reference', stimulus: reference, play: () => control.playReference() });
    $('reference').onclick = () => void playTrack(0);
    (volumeSetup ? [] : order).forEach((id, position) => {
      const identifier = `${trial}:${id}`;
      const row = rows.get(identifier) ?? { trial_id: trial, condition_id: id, score: null, answered: false };
      rows.set(identifier, row);
      const column = document.createElement('section'); column.className = 'condition';
      column.setAttribute('role', 'group');
      column.setAttribute('aria-label', `Stimulus ${String.fromCharCode(65 + position)}`);
      const heading = document.createElement('h2'); heading.textContent = `Condition ${String.fromCharCode(65 + position)}`;
      const play = document.createElement('button'); play.className = 'play-toggle'; play.textContent = 'Play';
      const slider = document.createElement('input'); slider.type = 'range'; slider.min = '0'; slider.max = '100'; slider.step = 'any'; slider.className = 'rating';
      slider.value = String(row.score ?? 50); slider.disabled = true; slider.setAttribute('aria-label', `${heading.textContent} quality, 0 bad to 100 excellent`);
      const label = document.createElement('output'); label.className = row.answered ? 'value' : 'value is-unanswered'; label.textContent = row.answered ? String(row.score) : '—';
      label.setAttribute('aria-label', row.answered ? `Score ${row.score}` : 'Unanswered');
      slider.setAttribute('aria-describedby', 'quality-scale');
      const fader = document.createElement('div'); fader.className = 'fader-zone'; fader.append(slider);
      const confirm = document.createElement('button'); confirm.className = 'confirm-rating'; confirm.textContent = row.answered ? '✓ Rated' : 'Use 50'; confirm.disabled = true;
      confirm.setAttribute('aria-label', `Confirm current score for ${heading.textContent}`);
      const trackPosition = tracks.length;
      registerTrack({ element: column, button: play, name: heading.textContent, stimulus: stimuli.get(id), play: () => control.playCondition(position), row, slider, label, confirm });
      play.onclick = () => void playTrack(trackPosition);
      slider.addEventListener('input', () => changed(row, slider, label));
      slider.addEventListener('pointerup', () => { if (!slider.disabled) changed(row, slider, label); });
      confirm.onclick = () => changed(row, slider, label);
      column.append(heading, play, fader, label, confirm); $('conditions').append(column);
    });
    ready = true; $('reference').disabled = false; buttons(); syncPlayback(); renderTimeline();
    if (!volumeSetup) focusTrack(0, true);
    status(context.sampleRate === 48000 ? (volumeSetup ? 'Play the reference and choose a comfortable listening level.' : 'Ready. Play every stimulus before rating.') : `Warning: audio context is ${context.sampleRate} Hz, not 48000 Hz. This is recorded for owner review.`);
  }
  async function navigate(direction) {
    if (!ready || navigating || !writable()) return;
    const targetIndex = index + direction, complete = targetIndex === checkpoint.trial_order.length;
    if (direction > 0 && (!answeredPage() || (complete && !completeCoverage()))) { error('Every assigned rating must be answered before finishing.'); return; }
    navigating = true; buttons();
    try {
      await flush(complete, checkpoint.trial_order[complete ? index : targetIndex]);
      if (complete) { clearAudio(); $('conditions').replaceChildren(); $('rating-panel').hidden = true; status('All responses saved. Thank you.'); return; }
      index = targetIndex; await loadPage();
    } catch (failure) { error(failure.message); }
    finally { navigating = false; buttons(); }
  }
  async function boot(value) {
    checkpoint = value; api = `/api/listening-participant/${encodeURIComponent(checkpoint.session_id)}`;
    configureAudioLoading(checkpoint.audio_loading_limits);
    if (!checkpoint.consent_acknowledged || checkpoint.status === 'completed' || checkpoint.study_status !== 'collecting') throw new Error('Consent is required and the session must be collecting');
    let tab;
    try { tab = sessionStorage.getItem('plc-listening-tab') || crypto.randomUUID(); sessionStorage.setItem('plc-listening-tab', tab); }
    catch { tab = crypto.randomUUID(); }
    storageKey = `${checkpoint.session_id}:${tab}`; database = await openDatabase();
    if (!database) error('Offline storage is unavailable. Keep this page open until answers are saved.');
    for (const row of checkpoint.responses) rows.set(key(row), { ...row });
    const Audio = window.AudioContext || window.webkitAudioContext;
    if (!Audio) throw new Error('Web Audio is unavailable; use a supported desktop browser.');
    context = new Audio({ sampleRate: 48000 }); context.volume = 0.8;
    context.addEventListener('statechange', syncPlayback);
    if (!context.createScriptProcessor) throw new Error('This browser does not support the pinned playback engine. Try Chrome/Edge desktop.');
    const response = await fetch(`${api}/config`, { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new Error('Published configuration is unavailable');
    config = YAML.parse(await response.text());
    if (!Array.isArray(config.pages)) throw new Error('Invalid published configuration');
    const defaultVolume = config.pages.find(page => page.type === 'volume')?.defaultVolume ?? 0.8;
    context.volume = Number.isFinite(defaultVolume) && defaultVolume >= 0 && defaultVolume <= 1 ? defaultVolume : 0.8;
    $('volume').value = String(context.volume);
    const stored = await disk('get');
    if (stored) {
      if (stored.revision !== checkpoint.revision && !stored.pending && stored.changes.length) {
        locked = true; $('discard').hidden = false; throw new Error('Local draft conflicts with the saved revision. Discard it explicitly to reload server answers.');
      }
      for (const row of stored.changes) { rows.set(key(row), row); changes.set(key(row), row); }
      pending = stored.pending;
      if (pending) { await sendPending(); if (checkpoint.status === 'completed') return; }
    }
    index = checkpoint.trial_order.indexOf(checkpoint.current_trial_id);
    if (index < 0) throw new Error('Invalid saved trial');
    await loadPage();
    startPolling();
  }
  function startPolling() {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try {
        const saved = await request(`${api}/checkpoint`);
        if (saved.study_status !== 'collecting' || saved.status === 'completed') {
          locked = true; checkpoint.study_status = saved.study_status; pausePlayback(); buttons();
          for (const input of $('conditions').querySelectorAll('input,button')) input.disabled = true;
          status(saved.status === 'completed' ? 'This session is already completed.' : 'Collection closed. Unfinished drafts are frozen and not analyzed.');
        } else if (!flight && !pending && saved.revision !== checkpoint.revision) { locked = true; buttons(); $('discard').hidden = false; error('Another tab/device changed this session. Reload the saved version before continuing.'); }
      } catch (failure) { if (failure.status === 403) { locked = true; buttons(); error('Invitation revoked or unavailable.'); } }
    }, 8000);
  }
  $('volume-continue').onclick = async () => {
    if (!ready || !writable()) return;
    volumeSetup = false;
    try { await loadPage(); } catch (failure) { error(failure.message); }
  };
  $('reference').onclick = async () => { await context.resume(); control.playReference(); };
  $('position').oninput = () => {
    const fraction = $('position').valueAsNumber;
    if (ready && control && writable() && Number.isFinite(fraction)) control.setPosition(Math.round(fraction * control.getDuration()), false);
  };
  $('volume').oninput = () => { if (context) context.volume = $('volume').valueAsNumber; };
  $('loop').onchange = () => control?.setLoopingActive($('loop').checked);
  $('previous').onclick = () => void navigate(-1);
  $('next').onclick = () => void navigate(1);
  $('save').onclick = () => void flush().catch(() => {});
  $('retry').onclick = async () => {
    if (!context || !config) { notify('plc-listening-reload'); return; }
    try {
      await flush();
      if (!ready && checkpoint.status !== 'completed') { index = checkpoint.trial_order.indexOf(checkpoint.current_trial_id); await loadPage(); startPolling(); }
    } catch (failure) { error(failure.message); }
  };
  $('discard').onclick = async () => { if (confirm('Discard this tab’s unsaved local changes and reload the saved server version?')) { await disk('delete'); notify('plc-listening-reload'); } };
  window.addEventListener('beforeunload', event => { if (changes.size || pending) { event.preventDefault(); event.returnValue = ''; } });
  window.addEventListener('message', event => {
    if (event.origin !== location.origin || event.source !== parent) return;
    const message = event.data;
    if (message.type === 'plc-listening-init' && !checkpoint && typeof message.nonce === 'string') {
      nonce = message.nonce; void boot(message.checkpoint).catch(failure => {
        error(failure.message);
        if ([403, 409].includes(failure.status)) { locked = true; $('discard').hidden = false; }
        else $('retry').hidden = false;
      });
    } else if (message.nonce === nonce && message.type === 'plc-listening-shutdown') { clearInterval(pollTimer); clearAudio(); void context?.close(); }
    if (message.nonce === nonce && message.theme) {
      document.body.classList.toggle('dark', message.theme.dark === true);
      if (typeof message.theme.primary === 'string' && CSS.supports('color', message.theme.primary)) document.body.style.setProperty('--primary', message.theme.primary);
    }
  });
  parent.postMessage({ type: 'plc-listening-ready' }, location.origin);
})();
