const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup(channels = 1, length = 10000) {
  const sandbox = { console };
  vm.runInNewContext(readFileSync(path.join(__dirname, '../../plc_platform_backend/static/listening-client/vendor/MushraAudioControl.js'), 'utf8'), sandbox);
  const stimulus = value => {
    const data = Array.from({ length: channels }, () => new Float32Array(length).fill(value));
    const buffer = { length, numberOfChannels: channels, sampleRate: 1000, getChannelData: i => data[i] };
    return { getAudioBuffer: () => buffer };
  };
  const reference = stimulus(.8), first = stimulus(.4), second = stimulus(.2);
  const engine = new sandbox.MushraAudioControl({ sampleRate: 1000, volume: 1 }, 128, reference, [first, second], {}, false, false, false, false, false);
  engine.setLoopingActive(false);
  const events = []; engine.addEventListener(event => events.push(event));
  const process = (frames = 128) => {
    const output = Array.from({ length: channels }, () => new Float32Array(frames));
    engine.process({ outputBuffer: { length: frames, numberOfChannels: channels, getChannelData: i => output[i] } });
    return output;
  };
  return { engine, process, events, first, second, reference };
}

for (const channels of [1, 2]) {
  test(`${channels} channel: condition/reference switching preserves the advancing playhead`, () => {
    const { engine, process, second, reference } = setup(channels);
    engine.playCondition(0);
    for (let i = 0; i < 6; i++) process();
    assert.equal(engine.getPosition(), 768);
    engine.playCondition(1); process();
    assert.equal(engine.getPosition(), 896);
    assert.equal(engine.getActiveStimulus(), second);
    engine.playReference(); process();
    assert.equal(engine.getPosition(), 1024);
    assert.equal(engine.getActiveStimulus(), reference);
  });

  test(`${channels} channel: pause, seek and switch resumes at the seek, never zero`, () => {
    const { engine, process } = setup(channels);
    engine.playCondition(0); process(); engine.pause(); process();
    assert.equal(engine.audioPlaying, false);
    engine.setPosition(4000, false);
    assert.equal(engine.getPosition(), 4000);
    engine.playCondition(1); process();
    assert.equal(engine.getPosition(), 4128);
  });

  test(`${channels} channel: natural end keeps the cursor at the end and zeros the tail`, () => {
    const { engine, process, events } = setup(channels, 300);
    engine.playCondition(0); process(); process();
    const output = process();
    assert.equal(engine.audioPlaying, false);
    assert.equal(engine.getPosition(), 300);
    assert.equal(events.filter(event => event.name === 'playbackEnded').length, 1);
    for (const channel of output) assert.ok(channel.slice(44).every(sample => sample === 0));
    process(); assert.equal(events.filter(event => event.name === 'playbackEnded').length, 1);
  });
}

test('rapid pause then resume cancels the pending pause fade', () => {
  const { engine, process } = setup();
  engine.playCondition(0); process();
  engine.pause(); engine.playCondition(0); process();
  assert.equal(engine.audioPlaying, true);
  assert.equal(engine.getPosition(), 256);
});

test('looping still wraps normally without emitting an ended event', () => {
  const { engine, process, events } = setup(1, 300);
  engine.setLoopingActive(true); engine.playCondition(0);
  process(); process(); process();
  assert.equal(engine.audioPlaying, true);
  assert.equal(engine.getPosition(), 84);
  assert.equal(events.some(event => event.name === 'playbackEnded'), false);
});
