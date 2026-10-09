// Real Chromium smoke/regression test against a deterministic API stub.
// Backend API authorization/CAS/consent/finalization are tested separately in pytest.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdtemp, rm } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { tmpdir } from 'node:os';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const assets = resolve(dirname(fileURLToPath(import.meta.url)), '../plc_platform_backend/static/listening-client');
const sid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
let state = { session_id: sid, study_id: 'study', status: 'draft', study_status: 'collecting', revision: 1,
  consent_acknowledged: true, consent_version: 1, trial_order: ['trial-1', 'trial-2'],
  condition_orders: { 'trial-1': ['plc-0', 'reference', 'anchor35'], 'trial-2': ['anchor35', 'plc-0', 'reference'] }, current_trial_id: 'trial-1', responses: [] };
const requests = []; let loseAck = true, lastRequest;
const wave = Buffer.alloc(44 + 48000 * 4);
wave.write('RIFF'); wave.writeUInt32LE(wave.length - 8, 4); wave.write('WAVEfmt ', 8); wave.writeUInt32LE(16, 16);
wave.writeUInt16LE(3, 20); wave.writeUInt16LE(1, 22); wave.writeUInt32LE(48000, 24); wave.writeUInt32LE(192000, 28);
wave.writeUInt16LE(4, 32); wave.writeUInt16LE(32, 34); wave.write('data', 36); wave.writeUInt32LE(wave.length - 44, 40);
for (let i = 0; i < 48000; i++) wave.writeFloatLE(0.1 * Math.sin(i * 2 * Math.PI * 440 / 48000), 44 + i * 4);
const yaml = 'pages:\n' + ['trial-1', 'trial-2'].map(id => `  - type: mushra\n    id: ${id}\n    reference: audio/test-reference.wav\n    stimuli:\n      plc-0: audio/test-plc.wav\n      anchor35: audio/test-anchor.wav\n`).join('');
const server = createServer(async (req, res) => {
  try {
    let content, type = 'application/json';
    if (req.url === '/') {
      type = 'text/html'; content = `<!doctype html><iframe id="player" src="/api/listening-client/index.html"></iframe><script>
      window.events=[];window.addEventListener('message',event=>{events.push(event.data.type);if(event.data.type==='plc-listening-ready')document.getElementById('player').contentWindow.postMessage({type:'plc-listening-init',nonce:'test-nonce',checkpoint:${JSON.stringify(state)},theme:{dark:true,primary:'rgb(9, 100, 220)'}},location.origin);if(event.data.type==='plc-listening-reload')location.reload();});</script>`;
    } else if (req.url.startsWith('/api/listening-client/')) {
      const relative = req.url.slice('/api/listening-client/'.length);
      if (!/^[a-zA-Z0-9/.+-]+$/.test(relative) || relative.includes('..')) throw new Error('Invalid asset path');
      content = await readFile(resolve(assets, relative));
      type = relative.endsWith('.js') ? 'text/javascript' : relative.endsWith('.css') ? 'text/css' : 'text/html';
    } else if (req.url.endsWith('/config')) { content = yaml; type = 'application/yaml'; }
    else if (req.url.includes('/audio/')) { content = wave; type = 'audio/wav'; }
    else if (req.method === 'PUT') {
      let body = ''; for await (const chunk of req) body += chunk;
      const parsed = JSON.parse(body); requests.push(parsed);
      if (parsed.request_id !== lastRequest) {
        assert.equal(parsed.expected_revision, state.revision);
        state = { ...state, revision: state.revision + 1, responses: parsed.responses, current_trial_id: parsed.current_trial_id, status: parsed.complete ? 'completed' : 'draft' };
        lastRequest = parsed.request_id;
      } else assert.deepEqual(parsed, requests[requests.length - 2]);
      if (loseAck) { loseAck = false; res.writeHead(503, { 'content-type': 'application/json' }); res.end(JSON.stringify({ detail: 'Acknowledgement unavailable after publication' })); return; }
      content = JSON.stringify(state);
    } else content = JSON.stringify(state);
    const data = Buffer.isBuffer(content) ? content : Buffer.from(content);
    res.writeHead(200, { 'content-type': type, 'content-length': data.length }); res.end(data);
  } catch (error) { res.writeHead(500); res.end(JSON.stringify({ detail: error.message })); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port, profile = await mkdtemp(resolve(tmpdir(), 'plc-listening-browser-'));
const chrome = spawn(process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', ['--headless=new', '--no-first-run', '--no-default-browser-check', '--autoplay-policy=no-user-gesture-required', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let socket;
try {
  let debugPort;
  for (let i = 0; i < 100; i++) { try { debugPort = (await readFile(resolve(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; break; } catch { await delay(100); } }
  if (!debugPort) throw new Error('Chromium did not start. Set CHROME_BIN.');
  const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json`)).json();
  socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
  await new Promise(resolve => socket.addEventListener('open', resolve, { once: true }));
  let sequence = 0; const waits = new Map(); const exceptions = [];
  socket.addEventListener('message', event => { const message = JSON.parse(event.data); if (message.id) { const pair = waits.get(message.id); waits.delete(message.id); message.error ? pair.reject(new Error(JSON.stringify(message.error))) : pair.resolve(message.result); } else if (message.method === 'Runtime.exceptionThrown') exceptions.push(message.params.exceptionDetails); });
  const cdp = (method, params = {}) => new Promise((resolve, reject) => { const id = ++sequence; waits.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params })); });
  const evaluate = async expression => {
    const result = await cdp('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails)); return result.result.value;
  };
  const frame = 'document.getElementById("player").contentWindow';
  const wait = async predicate => { for (let i = 0; i < 150; i++) { if (await evaluate(predicate)) return; await delay(100); } throw new Error(`Timed out: ${predicate}; ${await evaluate(`${frame}.document.body.innerText`)}`); };
  await cdp('Runtime.enable'); await cdp('Page.enable'); await cdp('Page.navigate', { url: `http://127.0.0.1:${port}/` });
  await wait(`${frame}.document.querySelectorAll('.condition').length===3`);
  assert.equal(await evaluate(`${frame}.document.body.classList.contains('dark')`), true);
  assert.equal(await evaluate(`${frame}.document.querySelectorAll('.value')[0].textContent`), 'Unanswered');
  // First save is committed by the server but its acknowledgement is lost.
  await evaluate(`(async()=>{const w=${frame};w.document.querySelector('.condition button').click();await new Promise(r=>setTimeout(r,100));const slider=w.document.querySelector('.rating');slider.value='0';slider.dispatchEvent(new w.Event('input'));w.document.getElementById('save').click();})()`);
  await wait(`!${frame}.document.getElementById('retry').hidden`);
  assert.equal(state.responses.find(row => row.condition_id === 'plc-0').score, 0);
  await cdp('Page.reload');
  await wait(`${frame}.document.querySelectorAll('.condition').length===3`);
  assert.equal(state.revision, 2); assert.equal(requests.length, 2);
  assert.deepEqual(requests[0], requests[1]);
  assert.equal(await evaluate(`${frame}.document.querySelectorAll('.value')[0].textContent`), '0');
  const answer = async () => evaluate(`(async()=>{const w=${frame};for(const card of w.document.querySelectorAll('.condition')){card.querySelector('button').click();await new Promise(r=>setTimeout(r,50));const slider=card.querySelector('input');slider.value='70';slider.dispatchEvent(new w.Event('input'));}})()`);
  await answer(); await evaluate(`${frame}.document.getElementById('next').click()`);
  await wait(`${frame}.document.getElementById('title').textContent==='Trial 2 of 2' && ${frame}.document.querySelectorAll('.condition').length===3`);
  assert.equal(state.current_trial_id, 'trial-2');
  await answer(); await evaluate(`${frame}.document.getElementById('next').click()`);
  await wait(`events.includes('plc-listening-complete')`);
  assert.equal(state.status, 'completed'); assert.equal(state.responses.length, 6);
  assert.equal(exceptions.length, 0, JSON.stringify(exceptions));
  console.log('PASS: real WAV decoding/playback, theme, unanswered/zero semantics, IndexedDB lost-ACK resume, immutable retry, exact navigation, completion acknowledgement.');
} finally { socket?.close(); chrome.kill(); server.close(); await delay(500); await rm(profile, { recursive: true, force: true }); }
