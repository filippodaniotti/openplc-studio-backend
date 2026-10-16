const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const MIB = 1024 * 1024;

function setup() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, { cloneNode: () => ({ childNodes: [] }), replaceChildren() {}, classList: { toggle() {} } });
    return elements.get(id);
  }
  const source = readFileSync(path.join(__dirname, '../../plc_platform_backend/static/listening-client/player.js'), 'utf8');
  const sandbox = {
    document: { getElementById: element, addEventListener() {} },
    window: { addEventListener() {} }, parent: { postMessage() {} }, location: { origin: 'http://localhost' },
    AbortController, setTimeout() {}, clearTimeout() {},
    fetch: async () => ({ ok: true, headers: { get: () => '60' }, arrayBuffer: async () => new ArrayBuffer(60) }),
  };
  vm.runInNewContext(source.replace("  parent.postMessage({ type: 'plc-listening-ready' }, location.origin);", `
    checkpoint = { status: 'draft', study_status: 'collecting', trial_order: ['t'], condition_orders: {t: []} };
    globalThis.api = { configureAudioLoading, audioBudgetError,
      limits: () => [maxDecodedBytes, audioTimeoutMs],
      failed(value) { loadFailed = value; buttons(); },
      locked() { locked = true; buttons(); },
      decodedOversize() {
        configureAudioLoading({ max_decoded_bytes: 1024 * 1024, timeout_seconds: 120 });
        config = { pages: [{ id: 't', type: 'mushra', reference: 'audio/reference.wav', stimuli: {} }] };
        context = { sampleRate: 48000, decodeAudioData: async () => ({ length: 1024 * 1024 / 4 + 1, numberOfChannels: 1 }) };
        return loadPage();
      } };
  `), sandbox);
  return { api: sandbox.api, element };
}

test('fallback supports old checkpoints and valid server limits override it', () => {
  const { api } = setup();
  api.configureAudioLoading(undefined);
  assert.equal(api.limits()[0], 256 * MIB);
  assert.equal(api.limits()[1], 120000);
  api.configureAudioLoading({ max_decoded_bytes: 64 * MIB, timeout_seconds: 180 });
  assert.equal(api.limits()[0], 64 * MIB);
  assert.equal(api.limits()[1], 180000);
});

test('malformed and unbounded client limits are rejected', () => {
  const { api } = setup();
  for (const max_decoded_bytes of [0, Infinity, 1025 * MIB, '256', MIB + 0.5]) {
    assert.throws(() => api.configureAudioLoading({ max_decoded_bytes, timeout_seconds: 120 }), /Invalid audio loading limits/);
  }
  for (const timeout_seconds of [0, 601, '120']) {
    assert.throws(() => api.configureAudioLoading({ max_decoded_bytes: 256 * MIB, timeout_seconds }), /Invalid audio loading limits/);
  }
});

test('budget failures report required and allowed memory without condition identities', () => {
  const { api } = setup();
  api.configureAudioLoading({ max_decoded_bytes: 64 * MIB, timeout_seconds: 120 });
  assert.match(api.audioBudgetError(66.43 * MIB).message, /66.43 MiB.*64 MiB.*study owner/);
});

test('actual decoded guard catches oversize even when encoded size appears small', async () => {
  const { api, element } = setup();
  await assert.rejects(api.decodedOversize(), /allowed limit is 1 MiB/);
  assert.equal(element('retry').hidden, false);
  assert.equal(element('reference').disabled, true);
});

test('loading failure exposes retry even without pending ratings; success and locking hide it', () => {
  const { api, element } = setup();
  api.failed(true); assert.equal(element('retry').hidden, false);
  api.failed(false); assert.equal(element('retry').hidden, true);
  api.failed(true); api.locked(); assert.equal(element('retry').hidden, true);
});
