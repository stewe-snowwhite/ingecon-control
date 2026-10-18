const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../static/monitor_demo.js'), 'utf8');

function node() {
  return {dataset: {}, children: [], textContent: '', hidden: false,
    appendChild(child) { this.children.push(child); }, replaceChildren() { this.children = []; }};
}
async function run(fetch) {
  const cells = new Map(), bodies = new Map([['demo-events',node()], ['demo-deliveries',node()]]);
  const alert = node(), timers = new Map(); let nextTimer = 0;
  const root = {dataset: {api: '/api/demo/status'}, querySelector(selector) {
    const key = selector.match(/"([^"]+)"/)[1];
    if (!cells.has(key)) cells.set(key,node()); return cells.get(key);
  }};
  vm.runInNewContext(source, {
    document: {getElementById: id => id === 'monitor-demo' ? root : id === 'page-alert' ? alert : bodies.get(id), createElement: node},
    fetch, AbortController, Date, Error,
    setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id,{fn,delay}); return id; },
    clearTimeout: id => timers.delete(id)
  });
  await new Promise(setImmediate);
  return {cells,bodies,alert,timers};
}
function sample() {
  return {inverters: [{state: 'Online', checked_at: 1700000000, checks: {tcp: {ok: true}, modbus: {ok: true}}}],
    monitor: {alive: true}, equipment: 'Running', notifications: false,
    measurements: {power_w: 2500, voltage_v: 230, current_a: 10.9, frequency_hz: 50, temperature_c: 42.5},
    events: [{time: 1700000000, kind: 'equipment', state: 'Alarm', detail: '<img src=x onerror=alert(1)>'}], deliveries: []};
}
test('live DEMO measurements, safe event text, local API and one polling timer', async () => {
  let options;
  const result = await run(async (url, config) => {
    assert.equal(url, '/api/demo/status'); options = config;
    return {ok: true, status: 200, json: async () => sample()};
  });
  assert.equal(options.credentials, 'same-origin'); assert.equal(options.cache, 'no-store');
  assert.equal(result.cells.get('power_w').textContent, 2500);
  assert.equal(result.cells.get('state').textContent, 'Online');
  assert.equal(result.bodies.get('demo-events').children[0].children[3].textContent, '<img src=x onerror=alert(1)>');
  assert.equal(result.bodies.get('demo-deliveries').children[0].children[0].textContent, 'Записів немає.');
  assert.deepEqual([...result.timers.values()].map(x => x.delay), [5000]);
});
test('web outage invalidates displayed live status and hides raw transport details', async () => {
  const result = await run(async () => { throw new Error('SECRET-INTERNAL-URL'); });
  assert.equal(result.cells.get('state').textContent, 'Unknown');
  assert.equal(result.cells.get('power_w').textContent, '—');
  assert.equal(result.cells.get('modbus').textContent, 'невідомо');
  assert.equal(result.alert.hidden, false); assert.ok(!result.alert.textContent.includes('SECRET'));
});
for (const status of [401,403]) test(`revoked access ${status} stops polling`, async () => {
  const result = await run(async () => ({ok: false,status}));
  assert.equal(result.timers.size, 0); assert.match(result.alert.textContent, /Увійдіть/);
});
test('stale monitor hides values and shows unavailable heartbeat', async () => {
  const data = sample(); data.monitor.alive = false; data.measurements = null;
  data.equipment = 'Unknown'; data.inverters[0].state = 'Unknown';
  const result = await run(async () => ({ok: true,status: 200,json: async () => data}));
  assert.equal(result.cells.get('power_w').textContent, '—');
  assert.match(result.cells.get('heartbeat').textContent, /застарілі/);
});
test('a stalled request is aborted after eight seconds', async () => {
  let signal;
  const result = await run((url, options) => {
    signal = options.signal;
    return new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('aborted'))));
  });
  const timer = [...result.timers.values()].find(x => x.delay === 8000);
  assert.ok(timer); timer.fn(); await new Promise(setImmediate);
  assert.equal(signal.aborted, true); assert.equal(result.cells.get('state').textContent, 'Unknown');
});
