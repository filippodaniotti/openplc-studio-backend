// Real full-track WAV regression: the former 64 MiB failure and configured limits.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdtemp, rm } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { tmpdir } from 'node:os';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const assets = resolve(dirname(fileURLToPath(import.meta.url)), '../plc_platform_backend/static/listening-client');
const MIB = 1024 * 1024, frames = 2176863; // Same 45.3513125-second stereo case as the reported failure.
const wave = Buffer.alloc(44 + frames * 8);
wave.write('RIFF'); wave.writeUInt32LE(wave.length - 8, 4); wave.write('WAVEfmt ', 8); wave.writeUInt32LE(16, 16);
wave.writeUInt16LE(3, 20); wave.writeUInt16LE(2, 22); wave.writeUInt32LE(48000, 24); wave.writeUInt32LE(384000, 28);
wave.writeUInt16LE(8, 32); wave.writeUInt16LE(32, 34); wave.write('data', 36); wave.writeUInt32LE(wave.length - 44, 40);
for (let i = 0; i < frames; i++) {
  const sample = 0.1 * Math.sin(i * 2 * Math.PI * 440 / 48000);
  wave.writeFloatLE(sample, 44 + i * 8); wave.writeFloatLE(sample, 48 + i * 8);
}
assert.ok(4 * wave.length > 64 * MIB);
const state = {
  session_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', study_id: 'study', status: 'draft',
  study_status: 'collecting', revision: 1, consent_acknowledged: true, consent_version: 1,
  trial_order: ['trial'], condition_orders: { trial: ['plc-0', 'reference', 'plc-1', 'anchor35'] },
  current_trial_id: 'trial', responses: [],
};
const yaml = 'pages:\n- type: mushra\n  id: trial\n  reference: audio/test-reference.wav\n  stimuli:\n    plc-0: audio/test-plc-a.wav\n    plc-1: audio/test-plc-b.wav\n    anchor35: audio/test-anchor.wav\n';
let audioRequests = 0, slowAudio = false;
const server = createServer(async (req, res) => {
  try {
    let content, type = 'application/json';
    if (req.url.startsWith('/?')) {
      const params = new URL(req.url, 'http://localhost').searchParams;
      const budget = Number(params.get('budget'));
      slowAudio = params.get('slow') === '1';
      const checkpoint = { ...state, ...(budget ? { audio_loading_limits: { max_decoded_bytes: budget * MIB, timeout_seconds: slowAudio ? 5 : 120 } } : {}) };
      type = 'text/html';
      content = `<!doctype html><iframe id="player" src="/api/listening-client/index.html"></iframe><script>
        addEventListener('message', event => { if (event.data.type === 'plc-listening-ready')
          document.getElementById('player').contentWindow.postMessage({ type: 'plc-listening-init', nonce: 'test', checkpoint: ${JSON.stringify(checkpoint)} }, location.origin); });</script>`;
    } else if (req.url.startsWith('/api/listening-client/')) {
      const relative = new URL(req.url, 'http://localhost').pathname.slice('/api/listening-client/'.length);
      if (!/^[a-zA-Z0-9/.+-]+$/.test(relative) || relative.includes('..')) throw new Error('Invalid asset path');
      content = await readFile(resolve(assets, relative));
      type = relative.endsWith('.js') ? 'text/javascript' : relative.endsWith('.css') ? 'text/css' : 'text/html';
    } else if (req.url.endsWith('/config')) { content = yaml; type = 'application/yaml'; }
    else if (req.url.includes('/audio/')) {
      audioRequests++;
      if (slowAudio) await new Promise(resolve => setTimeout(resolve, 6000));
      content = wave; type = 'audio/wav';
    }
    else content = JSON.stringify(state);
    const data = Buffer.isBuffer(content) ? content : Buffer.from(content);
    res.writeHead(200, { 'content-type': type, 'content-length': data.length, 'cache-control': 'no-store' }); res.end(data);
  } catch (error) { res.writeHead(500); res.end(JSON.stringify({ detail: error.message })); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port, profile = await mkdtemp(resolve(tmpdir(), 'plc-audio-budget-'));
const chrome = spawn(process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
  '--headless=new', '--no-first-run', '--no-default-browser-check', '--autoplay-policy=no-user-gesture-required',
  '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank',
], { stdio: 'ignore' });
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let socket;
try {
  let debugPort;
  for (let i = 0; i < 100; i++) {
    try { debugPort = (await readFile(resolve(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; break; }
    catch { await delay(100); }
  }
  if (!debugPort) throw new Error('Chromium did not start. Set CHROME_BIN.');
  const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json`)).json();
  socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
  await new Promise(resolve => socket.addEventListener('open', resolve, { once: true }));
  let sequence = 0, loadWaiter; const waits = new Map(), exceptions = [];
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const pair = waits.get(message.id); waits.delete(message.id);
      if (message.error) pair.reject(new Error(JSON.stringify(message.error)));
      else pair.resolve(message.result);
    } else if (message.method === 'Runtime.exceptionThrown') exceptions.push(message.params.exceptionDetails);
    else if (message.method === 'Page.loadEventFired') loadWaiter?.();
  });
  const cdp = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence; waits.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params }));
  });
  const navigate = async url => {
    const loaded = new Promise(resolve => { loadWaiter = resolve; });
    await cdp('Page.navigate', { url });
    await loaded;
  };
  const evaluate = async expression => {
    const result = await cdp('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  const frame = 'document.getElementById("player").contentWindow';
  const wait = async predicate => {
    for (let i = 0; i < 200; i++) { if (await evaluate(predicate)) return; await delay(100); }
    throw new Error(`Timed out: ${predicate}; ${await evaluate(`${frame}.document.body.innerText`)}`);
  };
  await cdp('Runtime.enable'); await cdp('Page.enable');
  for (const budget of [0, 64, 128]) {
    await navigate(`http://127.0.0.1:${port}/?budget=${budget}`);
    await wait(`document.getElementById('player')?.contentWindow?.document.getElementById('volume-continue')?.disabled === false`);
    await evaluate(`${frame}.document.getElementById('volume-continue').click()`);
    if (budget === 64) {
      await wait(`${frame}.document.getElementById('error').textContent.includes('allowed limit is 64 MiB')`);
      assert.equal(await evaluate(`${frame}.document.getElementById('retry').hidden`), false);
      assert.equal(await evaluate(`${frame}.document.getElementById('reference').disabled`), true);
      const before = audioRequests;
      await evaluate(`${frame}.document.getElementById('retry').click()`);
      await wait(`${frame}.document.getElementById('error').textContent.includes('allowed limit is 64 MiB')`);
      assert.ok(audioRequests > before);
    } else {
      await wait(`${frame}.document.querySelectorAll('.condition').length === 4`);
      assert.equal(await evaluate(`${frame}.document.getElementById('error').textContent`), '');
      if (budget === 0) {
        const doc = `${frame}.document`, first = `${doc}.querySelectorAll('.condition .play-toggle')[0]`, second = `${doc}.querySelectorAll('.condition .play-toggle')[2]`;
        const dimensions = () => evaluate(`[${doc}.getElementById('reference'),${doc}.getElementById('position'),...${doc}.querySelectorAll('.condition h2')].map(e=>{const r=e.getBoundingClientRect();return [r.width,r.left];})`);
        const idleDimensions = await dimensions();
        await evaluate(`${first}.click()`);
        await wait(`Number(${doc}.getElementById('position').value) > .015`);
        assert.equal(await evaluate(`${first}.getAttribute('aria-pressed')`), 'true');
        assert.deepEqual(await dimensions(), idleDimensions, 'condition playback must not resize or shift reference/timeline');
        const before = await evaluate(`Number(${doc}.getElementById('position').value)`);
        await evaluate(`${second}.click()`);
        await wait(`${second}.getAttribute('aria-pressed') === 'true'`);
        assert.ok(await evaluate(`Number(${doc}.getElementById('position').value) >= ${before}`), 'switch must not rewind');
        assert.equal(await evaluate(`${doc}.querySelectorAll('.play-toggle[aria-pressed="true"]').length`), 1);
        await evaluate(`${second}.click()`);
        assert.equal(await evaluate(`${doc}.querySelectorAll('.play-toggle[aria-pressed="true"]').length`), 0);
        await delay(100);
        await evaluate(`{const e=${doc}.getElementById('position');e.value='.5';e.dispatchEvent(new Event('input'));}`);
        await evaluate(`${doc}.getElementById('reference').click()`);
        await wait(`Number(${doc}.getElementById('position').value) > .5`);
        assert.equal(await evaluate(`${doc}.getElementById('reference').getAttribute('aria-pressed')`), 'true');
        assert.deepEqual(await dimensions(), idleDimensions, 'reference playback must not resize or shift reference/timeline');
        await evaluate(`${doc}.getElementById('reference').click()`);
        assert.equal(await evaluate(`${doc}.getElementById('reference').getAttribute('aria-pressed')`), 'false');
        assert.deepEqual(await dimensions(), idleDimensions, 'paused reference must not resize or shift reference/timeline');
        await delay(100);
        await evaluate(`{const e=${doc}.getElementById('position');e.value='.998';e.dispatchEvent(new Event('input'));}`);
        await evaluate(`${second}.click()`);
        await wait(`Number(${doc}.getElementById('position').value) === 1 && ${doc}.querySelectorAll('.play-toggle[aria-pressed="true"]').length === 0`);
        await evaluate(`${first}.click()`);
        await wait(`Number(${doc}.getElementById('position').value) > 0 && Number(${doc}.getElementById('position').value) < .1`);
      }
      await evaluate(`${frame}.document.querySelector('.play-toggle[aria-pressed="true"]')?.click()`);
    }
  }
  // Retry must remain visible during calibration, outside hidden trial navigation.
  await navigate(`http://127.0.0.1:${port}/?budget=256&slow=1`);
  await wait(`document.getElementById('player')?.contentWindow?.document.getElementById('error')?.textContent.includes('exceeded 5 seconds')`);
  assert.equal(await evaluate(`${frame}.document.getElementById('retry').getClientRects().length > 0`), true);
  slowAudio = false;
  await evaluate(`${frame}.document.getElementById('retry').click()`);
  await wait(`${frame}.document.getElementById('volume-continue').disabled === false`);
  await evaluate(`${frame}.document.getElementById('volume-continue').click()`);
  await wait(`${frame}.document.querySelectorAll('.condition').length === 4`);
  assert.equal(exceptions.length, 0, JSON.stringify(exceptions));
  console.log('PASS: real 45.35-second stereo / four-buffer playback at default 256 and configured 128 MiB; 64 MiB rejection/retry, timeout recovery, pressed playback states, synchronized switching, paused seek, natural end and replay.');
} finally { socket?.close(); chrome.kill(); server.close(); await delay(500); await rm(profile, { recursive: true, force: true }); }
