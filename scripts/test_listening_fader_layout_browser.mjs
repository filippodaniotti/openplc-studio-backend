// Real Chromium layout/input regression for the shared webMUSHRA-style scale.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, writeFile, mkdtemp, rm } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { tmpdir } from 'node:os';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const assets = resolve(dirname(fileURLToPath(import.meta.url)), '../plc_platform_backend/static/listening-client');
const sid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
let state, conditions, yaml;
function fixture(count) {
  conditions = [...Array.from({ length: count - 2 }, (_, i) => `plc-${i}`), 'reference', 'anchor35'];
  state = { session_id: sid, study_id: 'study', status: 'draft', study_status: 'collecting', revision: 1,
    consent_acknowledged: true, trial_order: ['trial'], condition_orders: { trial: conditions }, current_trial_id: 'trial', responses: [] };
  yaml = 'pages:\n- type: mushra\n  id: trial\n  reference: audio/reference.wav\n  stimuli:\n' +
    conditions.filter(id => id !== 'reference').map(id => `    ${id}: audio/${id}.wav\n`).join('');
}
const frames = 48000 * 8;
const wave = Buffer.alloc(44 + frames * 4);
wave.write('RIFF'); wave.writeUInt32LE(wave.length - 8, 4); wave.write('WAVEfmt ', 8); wave.writeUInt32LE(16, 16);
wave.writeUInt16LE(3, 20); wave.writeUInt16LE(1, 22); wave.writeUInt32LE(48000, 24); wave.writeUInt32LE(192000, 28);
wave.writeUInt16LE(4, 32); wave.writeUInt16LE(32, 34); wave.write('data', 36); wave.writeUInt32LE(wave.length - 44, 40);
for (let i = 0; i < frames; i++) wave.writeFloatLE(.1 * Math.sin(i * 2 * Math.PI * 440 / 48000), 44 + i * 4);
const server = createServer(async (req, res) => {
  try {
    let content, type = 'application/json';
    if (req.url.startsWith('/?')) {
      fixture(Number(new URL(req.url, 'http://localhost').searchParams.get('conditions')));
      type = 'text/html'; content = `<!doctype html><style>body{margin:0}iframe{width:100%;height:2200px;border:0;display:block}</style>
        <iframe id="player" src="/api/listening-client/index.html"></iframe><script>
        addEventListener('message',event=>{if(event.data.type==='plc-listening-ready')document.getElementById('player').contentWindow.postMessage({type:'plc-listening-init',nonce:'layout',checkpoint:${JSON.stringify(state)},theme:{dark:false,primary:'rgb(20, 184, 166)'}},location.origin);});</script>`;
    } else if (req.url.startsWith('/api/listening-client/')) {
      const relative = new URL(req.url, 'http://localhost').pathname.slice('/api/listening-client/'.length);
      if (!/^[a-zA-Z0-9/.+-]+$/.test(relative) || relative.includes('..')) throw new Error('Invalid asset path');
      content = await readFile(resolve(assets, relative));
      type = relative.endsWith('.js') ? 'text/javascript' : relative.endsWith('.css') ? 'text/css' : 'text/html';
    } else if (req.url.endsWith('/config')) { content = yaml; type = 'application/yaml'; }
    else if (req.url.includes('/audio/')) { content = wave; type = 'audio/wav'; }
    else if (req.method === 'PUT') {
      let body = ''; for await (const chunk of req) body += chunk;
      const request = JSON.parse(body);
      state = { ...state, revision: state.revision + 1, responses: request.responses, current_trial_id: request.current_trial_id };
      content = JSON.stringify(state);
    } else content = JSON.stringify(state);
    const data = Buffer.isBuffer(content) ? content : Buffer.from(content);
    res.writeHead(200, { 'content-type': type, 'content-length': data.length, 'cache-control': 'no-store' }); res.end(data);
  } catch (error) { res.writeHead(500); res.end(JSON.stringify({ detail: error.message })); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port, profile = await mkdtemp(resolve(tmpdir(), 'plc-fader-layout-'));
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
      if (message.error) pair.reject(new Error(JSON.stringify(message.error))); else pair.resolve(message.result);
    } else if (message.method === 'Runtime.exceptionThrown') exceptions.push(message.params.exceptionDetails);
    else if (message.method === 'Page.loadEventFired') loadWaiter?.();
  });
  const cdp = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence; waits.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async expression => {
    const result = await cdp('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  const frame = 'document.getElementById("player").contentWindow';
  const inside = code => evaluate(`(()=>{const w=${frame}, d=w.document;${code}})()`);
  const wait = async predicate => {
    for (let i = 0; i < 150; i++) { if (await evaluate(predicate)) return; await delay(100); }
    throw new Error(`Timed out: ${predicate}; ${await inside('return d.body.innerText;')}`);
  };
  const open = async count => {
    const loaded = new Promise(resolve => { loadWaiter = resolve; });
    await cdp('Page.navigate', { url: `http://127.0.0.1:${port}/?conditions=${count}` }); await loaded;
    await wait(`${frame}.document.getElementById('volume-continue').disabled === false`);
    assert.equal(await inside("return d.getElementById('rating-panel').hidden;"), true);
    await inside("d.getElementById('volume-continue').click();");
    await wait(`${frame}.document.querySelectorAll('.condition').length === ${count}`);
    await inside("d.getElementById('keyboard-help').open=false;");
  };
  const screenshot = async name => {
    const clip = await inside("const r=d.getElementById('rating-panel').getBoundingClientRect();return {x:0,y:Math.max(0,r.top-12),width:w.innerWidth,height:r.height+24,scale:1};");
    const image = await cdp('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true, clip });
    await writeFile(`/tmp/plc-mushra-faders-${name}.png`, Buffer.from(image.data, 'base64'));
  };
  await cdp('Runtime.enable'); await cdp('Page.enable');
  await cdp('Emulation.setDeviceMetricsOverride', { width: 1100, height: 1600, deviceScaleFactor: 1, mobile: false });
  await open(6);
  const geometry = await inside(`
    const sliders=[...d.querySelectorAll('.rating')].map(e=>e.getBoundingClientRect());
    const guides=[...d.querySelectorAll('.rating-guides span')].map(e=>e.getBoundingClientRect());
    return { widths:[...d.querySelectorAll('.condition')].map(e=>e.getBoundingClientRect().width),
      borders:[...d.querySelectorAll('.condition')].map(e=>w.getComputedStyle(e).borderWidth),
      ticks:[...d.querySelectorAll('.scale-ticks span')].map(e=>e.textContent),
      guideY:guides.map(r=>r.top), sliderTop:sliders[0].top, sliderBottom:sliders[0].bottom,
      firstX:sliders[0].x, sliderWidth:sliders[0].width, halfThumb:parseFloat(w.getComputedStyle(d.getElementById('rating-panel')).getPropertyValue('--thumb-height'))/2,
      lastX:sliders.at(-1).right, guideLeft:guides[0].left, guideRight:guides[0].right,
      tops:sliders.map(r=>r.top) };`);
  assert.deepEqual(geometry.ticks, ['100', '80', '60', '40', '20', '0']);
  assert.ok(geometry.widths.every(width => width === 104));
  assert.ok(geometry.borders.every(border => border === '0px'));
  assert.ok(geometry.tops.every(top => top === geometry.sliderTop));
  assert.ok(Math.abs(geometry.guideY[0] - geometry.sliderTop - geometry.halfThumb) < 1);
  assert.ok(Math.abs(geometry.sliderBottom - geometry.guideY.at(-1) - geometry.halfThumb) < 1);
  assert.ok(geometry.guideLeft < geometry.firstX && geometry.guideRight >= geometry.lastX - geometry.sliderWidth / 2);
  // Real pointer input at each shared guide must produce its numeric score.
  await inside("d.querySelector('.condition button').click();"); await delay(100);
  for (const [i, score] of [100, 80, 60, 40, 20, 0].entries()) {
    const x = geometry.firstX + geometry.sliderWidth / 2, y = geometry.guideY[i];
    await cdp('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
    await cdp('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
    const value = await inside("return Number(d.querySelector('.rating').value);");
    assert.ok(Math.abs(value - score) < 1, `${score} guide gives ${value}`);
  }
  await inside("const e=d.querySelector('.rating');e.value='50';e.dispatchEvent(new w.Event('input',{bubbles:true}));e.focus();");
  await delay(200);
  assert.equal(await inside("return w.getComputedStyle(d.querySelector('.condition .fader-zone'),'::before').opacity;"), '1');
  assert.equal(await inside("return w.getComputedStyle(d.querySelectorAll('.condition .fader-zone')[1],'::before').opacity;"), '0');
  assert.equal(await inside("return w.getComputedStyle(d.querySelector('.condition'),'').outlineStyle;"), 'none');
  assert.equal(await inside("return d.querySelector('.condition .play-toggle').getAttribute('aria-pressed');"), 'true');
  await screenshot('light');
  await evaluate(`${frame}.postMessage({type:'plc-listening-theme',nonce:'layout',theme:{dark:true,primary:'rgb(20, 184, 166)'}},location.origin)`);
  await delay(200); assert.equal(await inside("return d.body.classList.contains('dark');"), true);
  await screenshot('dark');
  await cdp('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
  assert.equal(await inside("return w.getComputedStyle(d.querySelector('.fader-zone'),'::before').transitionDuration;"), '0s');
  await delay(1000); // Finish the fixture's final debounced checkpoint before navigating.
  await cdp('Emulation.setDeviceMetricsOverride', { width: 480, height: 1600, deviceScaleFactor: 1, mobile: false });
  await open(12);
  await inside(`for(let i=0;i<12;i++)d.activeElement.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Tab',bubbles:true}));`);
  const narrow = await inside(`const e=d.querySelector('.condition.track-focused'),r=e.getBoundingClientRect(),s=d.querySelector('.rating-scroll');
    return { label:e.getAttribute('aria-label'),left:r.left,right:r.right,viewport:w.innerWidth,scroll:s.scrollLeft,bodyWidth:d.body.scrollWidth };`);
  assert.equal(narrow.label, 'Stimulus L');
  assert.ok(narrow.scroll > 0 && narrow.left >= 0 && narrow.right <= narrow.viewport);
  assert.ok(narrow.bodyWidth <= narrow.viewport);
  await screenshot('narrow');
  await cdp('Emulation.setEmulatedMedia', { features: [{ name: 'forced-colors', value: 'active' }] });
  assert.equal(await inside("return w.getComputedStyle(d.querySelector('.condition.track-focused .fader-zone'),'::before').display;"), 'none');
  assert.equal(await inside("return w.getComputedStyle(d.querySelector('.condition.track-focused .rating')).outlineStyle;"), 'solid');
  assert.equal(exceptions.length, 0, JSON.stringify(exceptions));
  console.log('PASS: shared guides align with real native scores at 0/20/40/60/80/100, compact card-free columns, exclusive glow, parent theme, reduced motion, forced colors and 12-condition narrow keyboard scrolling. Screenshots: /tmp/plc-mushra-faders-{light,dark,narrow}.png');
} finally { socket?.close(); chrome.kill(); server.close(); await delay(500); await rm(profile, { recursive: true, force: true }); }
