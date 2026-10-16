const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', disabled: false, checked: false, value: '50',
      cloneNode() { return { childNodes: [] }; },
      get valueAsNumber() { return Number(this.value); },
      classList: { toggle() {} }, addEventListener() {}, focus() {},
      contains() { return false; }, closest() { return null; },
    });
    return elements.get(id);
  }
  const sandbox = {
    document: { getElementById: element, addEventListener() {} },
    window: { addEventListener() {} }, parent: { postMessage() {} },
    location: { origin: 'http://localhost' }, setTimeout() {}, clearTimeout() {},
  };
  const source = readFileSync(path.join(__dirname, '../../plc_platform_backend/static/listening-client/player.js'), 'utf8');
  vm.runInNewContext(source.replace("  parent.postMessage({ type: 'plc-listening-ready' }, location.origin);", `
    checkpoint = { status: 'draft', study_status: 'collecting', trial_order: ['t'], condition_orders: {t: ['a']} };
    ready = true; volumeSetup = false;
    context = { resume: async () => {}, state: 'running' };
    control = { audioPlaying: false, pause() { this.audioPlaying = false; },
      getPosition: () => 0, getDuration: () => 100, setPosition(n) { this.position = n; },
      setLoopingActive(value) { this.loop = value; } };
    let navigations = 0;
    navigate = async () => { navigations++; };
    globalThis.api = { registerTrack, focusTrack, playTrack, trialKeydown, control,
      get navigations() { return navigations; },
      get focused() { return focusedTrack; }, get changes() { return changes; },
      lock() { locked = true; }, volume() { volumeSetup = true; } };
  `), sandbox);
  const api = sandbox.api;
  const reference = { element: element('reference'), play() { api.control.audioPlaying = true; } };
  const condition = { element: element('condition'), slider: element('slider'), confirm: element('confirm'),
    label: element('label'), row: { trial_id: 't', condition_id: 'a' },
    play() { api.control.audioPlaying = true; } };
  condition.slider.disabled = true;
  api.registerTrack(reference); api.registerTrack(condition); api.focusTrack(0);
  function press(key, shiftKey = false) {
    const event = { key, shiftKey, target: api.focused === 0 ? reference.element : condition.element,
      preventDefault() { this.prevented = true; } };
    api.trialKeydown(event);
    return event;
  }
  return { api, condition, element, press };
}

test('Tab wraps between reference and conditions in both directions', () => {
  const { api, press } = setup();
  press('Tab'); assert.equal(api.focused, 1);
  press('Tab'); assert.equal(api.focused, 0);
  press('Tab', true); assert.equal(api.focused, 1);
});
test('scores require playback; arrows confirm and clamp scores', async () => {
  const { api, press, condition } = setup();
  press('Tab'); press('ArrowUp'); assert.equal(api.changes.size, 0);
  await api.playTrack(1);
  press('ArrowUp'); assert.equal(condition.slider.value, '55');
  assert.equal(api.changes.get('t:a').answered, true);
  press('ArrowUp', true); assert.equal(condition.slider.value, '75');
  press('ArrowDown', true); assert.equal(condition.slider.value, '55');
  condition.slider.value = '95'; press('ArrowUp', true); assert.equal(condition.slider.value, '100');
  condition.slider.value = '5'; press('ArrowDown', true); assert.equal(condition.slider.value, '0');
  condition.slider.value = '100'; press('ArrowUp'); assert.equal(condition.slider.value, '100');
  condition.slider.value = '0'; press('ArrowDown'); assert.equal(condition.slider.value, '0');
});
test('Space toggles playback, Left rewinds, R toggles repeat', async () => {
  const { api, press, element } = setup();
  press(' '); await Promise.resolve(); assert.equal(api.control.audioPlaying, true);
  press(' '); assert.equal(api.control.audioPlaying, false);
  press('ArrowLeft'); assert.equal(api.control.position, 0);
  press('r'); assert.equal(element('loop').checked, true);
  press('R'); assert.equal(element('loop').checked, false);
});
test('Enter advances only when the Next button is enabled', () => {
  const { api, press, element } = setup();
  element('next').disabled = true;
  press('Enter'); assert.equal(api.navigations, 0);
  element('next').disabled = false;
  press('Enter'); assert.equal(api.navigations, 1);
});
test('reference cannot be scored and locked or volume pages ignore shortcuts', () => {
  const { api, press } = setup();
  press('ArrowUp'); assert.equal(api.changes.size, 0);
  api.lock(); assert.equal(press('Tab').prevented, undefined);
  const volume = setup(); volume.api.volume();
  assert.equal(volume.press('Tab').prevented, undefined);
});
