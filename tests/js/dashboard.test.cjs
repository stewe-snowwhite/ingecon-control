'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const model = require('../../static/dashboard.js');
// Read the exact catalog used by Jinja; there is no separate JS translation table.
const source = fs.readFileSync('templates/limit_reasons.html','utf8');
const catalog = JSON.parse(source.split('{% set LIMIT_REASONS = ')[1].split(' %}')[0]);

function sample() {
  return {
    generated_at: 1000,
    monitor: {alive: true, last_seen: 990, interval_seconds: 30, stale_after_seconds: 90},
    inverters: [{
      id: 1, name: 'Сонце <img src=x>', group: 'A', ip_address: '192.0.2.1',
      state: 'Online', recorded_state: 'Online', stale: false, checked_at: 980, last_success_at: 980,
      latency_ms: 4, power_limit_percent: 50, checks: {icmp: {ok: false}, tcp: {ok: true}, modbus: {ok: true}},
      operating_state_code: 2, operating_state_label: 'Підключений до мережі',
      active_power_raw: 6298, active_power_w: 62980, limit_reason_code: 1
    }]
  };
}

test('fresh samples retain independent failed ICMP and Online', () => {
  const result = model.freshSnapshot(sample(), 1000);
  assert.equal(result.inverters[0].state, 'Online');
  assert.equal(result.inverters[0].checks.icmp.ok, false);
  assert.deepEqual(model.counts(result.inverters), {total:1, Online:1, Offline:0, Degraded:0, Unknown:0});
});

test('staleness expires during an API outage, without mutating the cached snapshot', () => {
  const original = sample();
  const result = model.freshSnapshot(original, 1081);
  assert.equal(result.monitor.alive, false);
  assert.equal(result.inverters[0].state, 'Unknown');
  assert.equal(result.inverters[0].checks.modbus.ok, null);
  assert.equal(result.inverters[0].power_limit_percent, null);
  for (const key of ['operating_state_code','operating_state_label','active_power_raw','active_power_w','limit_reason_code']) {
    assert.equal(result.inverters[0][key], null);
  }
  assert.equal(original.inverters[0].state, 'Online');
});

test('fresh heartbeat does not make a stale inverter fresh', () => {
  const data = sample();
  data.monitor.last_seen = 1080;
  const result = model.freshSnapshot(data, 1080);
  assert.equal(result.monitor.alive, true);
  assert.equal(result.inverters[0].state, 'Unknown');
});

test('future heartbeat and explicitly stopped monitor are Unknown', () => {
  assert.equal(model.freshSnapshot(sample(), 900).monitor.alive, false);
  const data = sample(); data.monitor.alive = false;
  assert.equal(model.freshSnapshot(data, 1000).inverters[0].state, 'Unknown');
});

test('combined search, group, state, Ukrainian names and empty results', () => {
  const rows = sample().inverters;
  assert.equal(model.filterRows(rows,{q:'СОНЦЕ',group:'A',state:'Online'}).length,1);
  assert.equal(model.filterRows(rows,{q:'192.0.2.',group:'A',state:''}).length,1);
  assert.equal(model.filterRows(rows,{q:'',group:'B',state:'Online'}).length,0);
  assert.equal(model.filterRows(rows,{q:'',group:'A',state:'Offline'}).length,0);
  assert.equal(model.counts([]).total,0);
});

const sortSample = [
  {id:1, name:'Інвертор 10', group:'Бета', active_power_w:100000, power_limit_percent:100},
  {id:2, name:'Інвертор 2', group:'альфа', active_power_w:9000, power_limit_percent:9},
  {id:3, name:'Інвертор 1', group:'Альфа', active_power_w:1000, power_limit_percent:10},
  {id:4, name:'Інвертор 3', group:'Гамма', active_power_w:0, power_limit_percent:0},
  {id:5, name:'Інвертор 4', group:'Бета', active_power_w:null, power_limit_percent:null},
  {id:6, name:'Інвертор 5', group:'Бета', active_power_w:-20, power_limit_percent:50}
];
const sortCases = [
  ['name', [3,2,4,5,6,1], [1,6,5,4,2,3]],
  ['group', [2,3,1,5,6,4], [4,1,5,6,2,3]],
  ['active_power_w', [6,4,3,2,1,5], [1,2,3,4,6,5]],
  ['power_limit_percent', [4,2,3,6,1,5], [1,6,3,2,4,5]]
];
for (const [key, ascending, descending] of sortCases) {
  test(`${key}: natural Ukrainian text or numeric sorting in both directions without mutating API data`, () => {
    const before = JSON.stringify(sortSample);
    assert.deepEqual(model.sortRows(sortSample, key, 'asc').map(row=>row.id), ascending);
    assert.deepEqual(model.sortRows(sortSample, key, 'desc').map(row=>row.id), descending);
    assert.equal(JSON.stringify(sortSample), before);
  });
}

test('equal values have stable ID ordering; absent numeric values remain last in both directions', () => {
  const rows = [{id:7,active_power_w:0},{id:2,active_power_w:0},
    {id:9,active_power_w:null},{id:8,active_power_w:NaN},{id:6}];
  for (const direction of ['asc','desc']) {
    assert.deepEqual(model.sortRows(rows,'active_power_w',direction).map(row=>row.id), [2,7,6,8,9]);
  }
  for (const key of ['', 'ip_address', '__proto__', 'constructor']) {
    assert.deepEqual(model.sortRows(sortSample,key), sortSample);
  }
  assert.deepEqual(model.sortRows(sortSample,'name','invalid'), sortSample);
});

test('UTC timestamps and malformed API responses', () => {
  assert.equal(model.formatUtc(0),'1970-01-01 00:00:00');
  assert.equal(model.formatUtc(null),'—');
  assert.equal(model.validSnapshot(sample()),true);
  for (const data of [null, {}, {...sample(),generated_at:NaN}, {...sample(),inverters:[{}]}]) {
    assert.ok(!model.validSnapshot(data));
  }
});

test('power uses watts with explicit kW display, including zero and reverse flow', () => {
  assert.equal(model.formatPower(62980), '62,98 кВт');
  assert.equal(model.formatPower(0), '0,00 кВт');
  assert.equal(model.formatPower(-20), '-0,02 кВт');
  assert.equal(model.formatPower(null), '—');
});

const reasonLabels = [
  'Без обмеження', 'Без обмеження',
  'Не визначено виробником', 'Не визначено виробником', 'Не визначено виробником',
  "Зовнішня команда через зв'язок", 'Частота електромережі', 'Напруга електромережі',
  'Обмеження вихідного струму / пріоритет реактивної потужності', 'Налаштування інвертора',
  'Початкове нарощування потужності', 'Власне споживання', 'Зарезервовано виробником',
  'Залежність потужності від напруги'
];

test('reason formatting preserves zero, numeric diagnostics and manufacturer meanings for 0–4', () => {
  for (const [code, label] of reasonLabels.entries()) {
    const result = model.formatLimitReason(code, catalog);
    assert.equal(result.label, `${label} (${code})`);
    assert.equal(result.hint, catalog[code].hint);
    assert.ok(result.hint.length > 0);
  }
  assert.equal(model.formatLimitReason(65534, catalog).label, 'Невідома причина (код 65534)');
  assert.ok(model.formatLimitReason(65534, catalog).hint);
  for (const value of [null, undefined, '10', '<img src=x>', false, NaN, 1.5]) {
    assert.deepEqual(model.formatLimitReason(value, catalog), {label:'—',hint:''});
  }
});

// A small DOM double exercises the actual polling script. No browser, server or network is used.
class Element {
  constructor(tag='div') { this.tag=tag; this.dataset={}; this.children=[]; this.listeners={}; this.value=''; this.hidden=false; this.textContent=''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes.flatMap(node=>node.tag==='fragment' ? node.children : [node]); }
  addEventListener(name,fn) { this.listeners[name]=fn; }
  setAttribute(key,value) { this[key]=value; }
  querySelectorAll() { return []; }
  querySelector() { return null; }
}
function browserDouble(initiallyFail=false, reasonCode=1, includeCatalog=true, options={}) {
  const elements = new Map();
  const ids = ['dashboard','search','group-filter','state-filter','device-rows','filters','heartbeat','heartbeat-time',
    'monitor-detail','connection-warning','list-count','empty-list','api-warning','refresh-label','page-alert'];
  ids.forEach(id=>elements.set(id,new Element()));
  const sortButtons = new Map();
  if (options.sortControls !== false) {
    for (const id of ['sort-key','sort-direction','sort-status']) elements.set(id,new Element());
    elements.get('sort-direction').value='asc'; elements.get('sort-direction').disabled=true;
    for (const [key] of sortCases) {
      const button = new Element('button'), header=new Element('th'), indicator=new Element('span');
      button.dataset={sortKey:key,sortLabel:key}; header['aria-sort']='none';
      button.closest=()=>header; button.querySelector=()=>indicator;
      button.header=header; button.indicator=indicator; sortButtons.set(key,button);
    }
  }
  elements.get('dashboard').querySelectorAll=()=>[...sortButtons.values()];
  elements.get('dashboard').dataset = {api:'/api/statuses',role:options.role || 'Viewer',writes:'false'};
  for (const id of ['selection-count','select-all']) elements.set(id,new Element());
  elements.get('device-rows').querySelectorAll=selector=>selector==='input[name="selected_ids"]' ?
    elements.get('device-rows').children.flatMap(row=>row.children).flatMap(cell=>cell.children)
      .filter(element=>element.tag==='input' && element.name==='selected_ids') : [];
  if (includeCatalog) elements.get('dashboard').dataset.limitReasons = JSON.stringify(catalog);
  const countElements = ['total','Online','Offline','Degraded','Unknown'].map(key=>{
    const item = new Element(); item.dataset.count=key; item.textContent=(key==='total' || key==='Online') ? '1' : '0'; return item;
  });
  const listeners = {};
  const document = {
    hidden:false, activeElement:null, getElementById:id=>elements.get(id),
    createElement:tag=>new Element(tag), createDocumentFragment:()=>new Element('fragment'),
    querySelectorAll:selector=>selector==='[data-count]' ? countElements : [],
    addEventListener:(key,fn)=>{listeners[key]=fn;}
  };
  let fail = initiallyFail; let status = 200; let requests = 0; let elapsed = 0; let rowData=options.rows;
  const timers = new Map(); let nextId = 0;
  const context = vm.createContext({
    document, window:{IngeconPanel:{connected:true},history:{replaceState(){}},location:{pathname:'/'}},
    performance:{now:()=>elapsed}, Date, Set, Map, JSON, Number, Object, String, URLSearchParams,
    AbortController, TypeError,
    fetch:async(url,options)=>{
      assert.equal(url,'/api/statuses'); assert.equal(options.credentials,'same-origin');
      assert.equal(options.method,undefined); // Every request is a status GET, never a control POST.
      requests++; if (fail) throw new TypeError('simulated connection loss');
      return {status,ok:status===200,json:async()=>{
        const data=sample(); data.inverters[0].limit_reason_code=reasonCode;
        if (rowData) data.inverters=rowData.map(row=>({...data.inverters[0],...row}));
        return data;
      }};
    },
    setTimeout:(fn,ms)=>{const id=++nextId;timers.set(id,{fn,ms});return id;},
    clearTimeout:id=>timers.delete(id), setInterval:fn=>{context.tick=fn;}
  });
  vm.runInContext(fs.readFileSync('static/dashboard.js','utf8'),context);
  return {elements,document,listeners,timers,countElements,context,sortButtons,setRows:rows=>{rowData=rows;},
    fail:()=>{fail=true;}, recover:()=>{fail=false;}, expire:()=>{elapsed=100000;},
    deny:()=>{status=401;}, requests:()=>requests};
}
const settle = () => new Promise(resolve=>setImmediate(resolve));
async function nextPoll(browser) {
  const entry = [...browser.timers.entries()].find(([,value])=>value.ms===10000);
  assert.ok(entry,'next poll scheduled');
  browser.timers.delete(entry[0]); entry[1].fn(); await settle();
}

function renderedIds(browser) {
  return browser.elements.get('device-rows').children.map(row=>Number(row.dataset.id));
}

for (const [key, ascending, descending] of sortCases) {
  test(`${key}: actual header clicks toggle order and preserve it through polling`, async () => {
    const browser=browserDouble(false,1,true,{rows:sortSample}); await settle();
    const button=browser.sortButtons.get(key);
    button.listeners.click();
    assert.deepEqual(renderedIds(browser),ascending);
    assert.equal(button.header['aria-sort'],'ascending');
    assert.equal(button.indicator.textContent,'↑');
    assert.equal(browser.elements.get('sort-key').value,key);
    assert.equal(browser.elements.get('sort-direction').value,'asc');
    button.listeners.click();
    assert.deepEqual(renderedIds(browser),descending);
    assert.equal(button.header['aria-sort'],'descending');
    assert.equal(button.indicator.textContent,'↓');
    assert.equal(browser.elements.get('sort-direction').value,'desc');
    assert.equal(browser.requests(),1); // Changing order is local and never submits the control form.
    await nextPoll(browser);
    assert.deepEqual(renderedIds(browser),descending);
    assert.equal(browser.requests(),2);
  });
}

test('mobile sorting, filtering, live values, expiry and reset use the same ordering', async () => {
  const browser=browserDouble(false,1,true,{rows:sortSample}); await settle();
  const key=browser.elements.get('sort-key'), direction=browser.elements.get('sort-direction');
  key.value='active_power_w'; key.listeners.change();
  assert.deepEqual(renderedIds(browser),[6,4,3,2,1,5]);
  assert.equal(direction.disabled,false);
  browser.elements.get('group-filter').value='Бета';
  browser.elements.get('group-filter').listeners.change();
  assert.deepEqual(renderedIds(browser),[6,1,5]);
  direction.value='desc'; direction.listeners.change();
  assert.deepEqual(renderedIds(browser),[1,6,5]);
  assert.equal(browser.sortButtons.get('active_power_w').header['aria-sort'],'descending');
  browser.setRows(sortSample.map(row=>row.id===6 ? {...row,active_power_w:200000} : row));
  await nextPoll(browser);
  assert.deepEqual(renderedIds(browser),[6,1,5]);
  browser.expire(); browser.context.tick();
  assert.deepEqual(renderedIds(browser),[1,5,6]); // Stale numeric values are now all absent.
  browser.recover(); await nextPoll(browser);
  assert.deepEqual(renderedIds(browser),[6,1,5]);
  browser.elements.get('group-filter').value='';
  browser.elements.get('group-filter').listeners.change();
  key.value=''; key.listeners.change();
  assert.deepEqual(renderedIds(browser),[1,2,3,4,5,6]);
  assert.equal(direction.disabled,true);
  for (const button of browser.sortButtons.values()) assert.equal(button.header['aria-sort'],'none');
});

test('sorting preserves selection by inverter ID, not its visible row position', async () => {
  const browser=browserDouble(false,1,true,{rows:sortSample,role:'Operator'}); await settle();
  const body=browser.elements.get('device-rows');
  const checkbox=body.querySelectorAll('input[name="selected_ids"]').find(node=>node.value==='2');
  checkbox.checked=true; body.listeners.change({target:checkbox});
  browser.sortButtons.get('name').listeners.click();
  browser.sortButtons.get('active_power_w').listeners.click();
  await nextPoll(browser);
  const selected=body.querySelectorAll('input[name="selected_ids"]').filter(node=>node.checked);
  assert.deepEqual(selected.map(node=>node.value),['2']);
  assert.equal(browser.elements.get('selection-count').textContent,'Обрано: 1');
});

test('polling remains compatible with cached HTML without sorting controls', async () => {
  const browser=browserDouble(false,1,true,{sortControls:false}); await settle();
  assert.equal(browser.requests(),1);
  assert.equal(browser.context.window.IngeconPanel.connected,true);
  await nextPoll(browser);
  assert.equal(browser.requests(),2);
});

for (const [code, label] of reasonLabels.entries()) {
  test(`polling renders reason ${code} with the shared hover hint and numeric code`, async () => {
    const browser = browserDouble(false, code); await settle();
    const span = browser.elements.get('device-rows').children[0].children[8].children[0];
    assert.equal(span.textContent, `${label} (${code})`);
    assert.equal(span.title, catalog[code].hint);
    assert.equal(span.dataset.limitReasonCode, String(code));
  });
}

test('polling renders unknown and absent reasons and removes expired labels and hints', async () => {
  for (const code of [65534, null]) {
    const browser = browserDouble(false, code); await settle();
    let span = browser.elements.get('device-rows').children[0].children[8].children[0];
    assert.equal(span.textContent, code === null ? '—' : 'Невідома причина (код 65534)');
    assert.equal(span.dataset.limitReasonCode, code === null ? undefined : '65534');
    if (code === null) assert.equal(span.title, undefined);
    else assert.ok(span.title);
  }
  const browser = browserDouble(false, 10); await settle();
  browser.expire(); browser.context.tick();
  const span = browser.elements.get('device-rows').children[0].children[8].children[0];
  assert.equal(span.textContent, '—');
  assert.equal(span.title, undefined);
  assert.equal(span.dataset.limitReasonCode, undefined);
});

test('new JS remains compatible with cached pre-upgrade HTML until web restart', async () => {
  const browser = browserDouble(false, 10, false); await settle();
  const span = browser.elements.get('device-rows').children[0].children[8].children[0];
  assert.equal(span.textContent, 'Код: 10');
  assert.equal(browser.requests(), 1);
  assert.equal(browser.context.window.IngeconPanel.connected, true);
});

test('actual polling updates HTML safely, reports outage, expires old samples and recovers', async () => {
  const browser = browserDouble(); await settle();
  assert.equal(browser.requests(),1);
  assert.equal(browser.countElements[1].textContent,1);
  const row = browser.elements.get('device-rows').children[0];
  assert.equal(row.children[0].children[0].textContent,'Сонце <img src=x>'); // text, not parsed HTML.
  assert.equal(row.children[5].children[0].textContent,'Підключений до мережі');
  assert.equal(row.children[6].textContent,'62,98 кВт');
  assert.equal(row.children[8].children[0].textContent,'Без обмеження (1)');
  browser.fail(); await nextPoll(browser);
  assert.equal(browser.context.window.IngeconPanel.connected,false);
  assert.equal(browser.elements.get('api-warning').hidden,false);
  browser.expire(); browser.context.tick();
  assert.equal(browser.countElements[4].textContent,1);
  browser.recover(); await nextPoll(browser);
  assert.equal(browser.elements.get('api-warning').hidden,true);
  assert.equal(browser.countElements[1].textContent,1);
});

test('polling stops on revoked session and pauses while hidden', async () => {
  const browser = browserDouble(); await settle();
  browser.document.hidden=true;
  browser.listeners.visibilitychange(); assert.equal(browser.timers.size,0);
  browser.deny(); browser.document.hidden=false;
  browser.listeners.visibilitychange(); await settle();
  assert.equal(browser.context.window.IngeconPanel.connected,false);
  assert.equal(browser.timers.size,0);
  assert.match(browser.elements.get('api-warning').textContent,/Сесію завершено/);
});

test('failure of the first API request invalidates the initial server-rendered counts', async () => {
  const browser = browserDouble(true); await settle();
  assert.equal(browser.countElements[1].textContent,0);
  assert.equal(browser.countElements[4].textContent,1);
  assert.equal(browser.elements.get('heartbeat').textContent,'Heartbeat не підтверджено');
  browser.recover(); await nextPoll(browser);
  assert.equal(browser.countElements[1].textContent,1);
});

test('theme applies system preference with unavailable localStorage', () => {
  const document = {documentElement:{dataset:{}}};
  const context = vm.createContext({document,localStorage:{getItem(){throw new Error('disabled');}},
    matchMedia:()=>({matches:true})});
  vm.runInContext(fs.readFileSync('static/theme.js','utf8'),context);
  assert.equal(document.documentElement.dataset.theme,'dark');
});

test('theme accepts saved choices and ignores unexpected values', () => {
  for (const [saved,expected] of [['light','light'],['dark','dark'],['unexpected','light']]) {
    const document={documentElement:{dataset:{}}};
    vm.runInNewContext(fs.readFileSync('static/theme.js','utf8'),{
      document,localStorage:{getItem:()=>saved},matchMedia:()=>({matches:false})});
    assert.equal(document.documentElement.dataset.theme,expected);
  }
});
