'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

for (const [utc, expected] of [
  ['2026-01-15T12:00:00Z', '15.01.2026, 14:00:00'],
  ['2026-07-15T12:00:00Z', '15.07.2026, 15:00:00'],
  [null, null]
]) test('controller time uses Kyiv, independent of browser timezone: '+utc, async () => {
  const elements=new Map();
  for (const id of ['power-budget','budget-enabled','budget-target','budget-actual','budget-coverage',
    'budget-reason','budget-updated','budget-error']) elements.set(id,{dataset:{statusUrl:'/power/status'}});
  let refresh;
  const context=vm.createContext({
    document:{getElementById:id=>elements.get(id)},
    window:{setTimeout:fn=>{refresh=fn;}}, Date, Number, Error, AbortSignal,
    fetch:async (url, options)=>{
      assert.equal(url,'/power/status'); assert.equal(options.credentials,'same-origin');
      assert.equal(options.method, undefined);
      return {ok:true,json:async()=>({enabled:false,last_cycle_at:utc===null ? null : Date.parse(utc)/1000})};
    }
  });
  vm.runInContext(fs.readFileSync('static/power_budget.js','utf8'), context);
  await refresh();
  assert.equal(elements.get('budget-error').hidden,true);
  assert.equal(elements.get('budget-updated').textContent, expected===null ? 'Очікування контролера' :
    'Контролер: '+expected+' (Київ)');
});
