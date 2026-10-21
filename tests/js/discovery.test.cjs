'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.dataset={}; this.children=[]; this.listeners={}; this.hidden=false; this.textContent=''; this.checked=false; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  addEventListener(key, fn) { this.listeners[key]=fn; }
}
function browser(overrides={}, status=200, fails=false) {
  const elements = new Map(['discovery-results','discovery-select-all','discovery-progress','discovery-progress-bar','discovery-rows','discovery-error','discovery-empty'].map(id=>[id,new Element()]));
  elements.get('discovery-results').dataset.url='/add/discovery/scans/1';
  const boxes=[new Element(),new Element()];
  const timers = new Map(); let counter=0; let requests=0;
  const events={};
  const data = {state:'running',processed:2,total:4,found:1,error:null,stale:false,
    results:[{ip:'10.12.0.2',mac:null,cidr:'10.12.0.0/24',modbus_ok:true,tcp_ok:true,confidence:'profile',reason:'<img src=x>',registered:false,add_url:'/add?candidate=1&mode=manual'}],...overrides};
  vm.runInNewContext(fs.readFileSync('static/discovery.js','utf8'), {
    document:{getElementById:id=>elements.get(id),querySelectorAll:()=>boxes,createElement:()=>new Element()},
    window:{addEventListener:(key,fn)=>{events[key]=fn;}},
    fetch:async(url,options)=>{requests++;assert.equal(url,'/add/discovery/scans/1');assert.equal(options.credentials,'same-origin');if(fails) throw new Error('offline');return {status,ok:status===200,json:async()=>data};},
    AbortController, setTimeout:(fn,ms)=>{const key=++counter;timers.set(key,{fn,ms});return key;},clearTimeout:key=>timers.delete(key)
  });
  return {elements,timers,boxes,events,requests:()=>requests};
}
const settle = () => new Promise(resolve=>setImmediate(resolve));
test('progress, candidate link, text escaping and responsive labels',async()=>{
  const b=browser();await settle();
  assert.match(b.elements.get('discovery-progress').textContent,/2 \/ 4/);
  const row=b.elements.get('discovery-rows').children[0];
  assert.equal(row.children.length,5);
  assert.equal(row.children[0].dataset.label,'IP / MAC');
  assert.match(row.children[3].textContent,/<img src=x>/);
  assert.equal(row.children[4].children[0].href,'/add?candidate=1&mode=manual');
  assert.ok([...b.timers.values()].some(timer=>timer.ms===2500));
});
test('registered inverter has no add button and finished scan stops polling',async()=>{
  const b=browser({state:'completed',results:[{ip:'10.12.0.2',registered:true}]});await settle();
  const cell=b.elements.get('discovery-rows').children[0].children[4];
  assert.equal(cell.textContent,'Вже доданий');assert.equal(cell.children.length,0);assert.equal(b.timers.size,0);
});
test('empty and stale scan indicates worker issue',async()=>{
  const b=browser({results:[],stale:true});await settle();
  assert.equal(b.elements.get('discovery-empty').hidden,false);
  assert.match(b.elements.get('discovery-error').textContent,/Worker/);
  assert.equal(b.elements.get('discovery-error').hidden,false);
});
test('authorization loss stops polling',async()=>{
  const b=browser({},403);await settle();
  assert.match(b.elements.get('discovery-error').textContent,/адміністратор/);assert.equal(b.timers.size,0);
});
test('network failure is visible and retried',async()=>{
  const b=browser({},200,true);await settle();
  assert.equal(b.elements.get('discovery-error').textContent,'offline');assert.ok(b.timers.size>0);
});
test('select all toggles and leaving page stops subsequent request',async()=>{
  const b=browser();await settle();
  b.elements.get('discovery-select-all').listeners.click();assert.ok(b.boxes.every(box=>box.checked));
  b.elements.get('discovery-select-all').listeners.click();assert.ok(b.boxes.every(box=>!box.checked));
  b.events.pagehide();[...b.timers.values()].find(timer=>timer.ms===2500).fn();await settle();assert.equal(b.requests(),1);
});
