'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

class Element {
  constructor() { this.listeners={}; this.children=[]; this.dataset={}; this.hidden=true; this.open=false; this.disabled=false; this.textContent=''; }
  addEventListener(name, fn) { this.listeners[name]=fn; }
  dispatchEvent(event) { this.listeners[event.type]?.(event); }
  append(value) { this.children.push(value); }
  replaceChildren() { this.children=[]; }
  setAttribute(key,value) { this[key]=value; }
  showModal() { this.open=true; }
  close() { this.open=false; }
}
class FormDataDouble {
  constructor(form) { this.values=new Map(Object.entries(form.values).map(([key,value])=>[key,[value]])); }
  getAll(key) { return this.values.get(key) || []; }
  append(key,value) { this.values.set(key,[...this.getAll(key),value]); }
  set(key,value) { this.values.set(key,[value]); }
}
function ui() {
  const ids = ['theme-toggle','command-dialog','page-alert','cancel-command','confirm-command',
    'confirmation-title','confirmation-description','confirmation-targets','result-list','command-results','dashboard'];
  const elements=new Map(ids.map(id=>[id,new Element()]));
  const form=new Element(); form.id='control-form'; form.action='/control';
  form.values={csrf_token:'test-csrf',selected_ids:'1'};
  const calls=[]; let fail=false; let delayed=false; let resolve;
  const document={
    documentElement:{dataset:{theme:'light'}},
    getElementById:id=>elements.get(id), createElement:()=>new Element(),
    querySelectorAll:()=>[form], dispatchEvent() {}
  };
  const preview={confirmation_required:true,confirmation_token:'signed-test-token',action:'delete',
    label:'Видалити',value:100,inverters:[{id:1,name:'<script>device</script>',ip_address:'192.0.2.1'}]};
  const results={results:[{id:1,name:'<script>device</script>',outcome:'deleted',message:'Інвертор видалено.'}]};
  const context=vm.createContext({
    document,window:{},Event,FormData:FormDataDouble,AbortController,TypeError,SyntaxError,
    localStorage:{setItem(){throw new Error('disabled');}},
    setTimeout:()=>1,clearTimeout(){},
    fetch:async(url, options)=>{
      assert.equal(url,'/control'); assert.equal(options.headers.Accept,'application/json');
      assert.equal(options.body.getAll('csrf_token')[0],'test-csrf');
      calls.push(options.body);
      if (fail) throw new TypeError('simulated timeout');
      if (calls.length>1 && delayed) await new Promise(done=>{resolve=done;});
      return {ok:true,status:200,json:async()=>calls.length===1 ? preview : results};
    }
  });
  vm.runInContext(fs.readFileSync('static/panel.js','utf8'),context);
  const submit=()=>form.listeners.submit({preventDefault(){},submitter:{name:'action',value:'delete'}});
  return {elements,form,calls,context,submit,fail:()=>{fail=true;},delay:()=>{delayed=true;},finish:()=>resolve()};
}

test('confirmation shows exact device text; cancelling sends no execution request', async()=>{
  const browser=ui(); await browser.submit();
  assert.equal(browser.elements.get('command-dialog').open,true);
  assert.equal(browser.elements.get('confirmation-targets').children[0].textContent,
    '<script>device</script> · 192.0.2.1');
  assert.equal(browser.calls.length,1);
  browser.elements.get('cancel-command').listeners.click();
  assert.equal(browser.context.window.IngeconPanel.busy,false);
  assert.equal(browser.calls.length,1);
});

test('confirmation cannot double-submit and displays per-device results', async()=>{
  const browser=ui(); await browser.submit(); browser.delay();
  const confirm=browser.elements.get('confirm-command').listeners.click;
  const completion=confirm(); await confirm();
  assert.equal(browser.calls.length,2);
  assert.equal(browser.calls[1].getAll('confirmation_token')[0],'signed-test-token');
  browser.finish(); await completion;
  assert.equal(browser.elements.get('command-dialog').open,false);
  assert.equal(browser.elements.get('command-results').hidden,false);
  assert.match(browser.elements.get('result-list').children[0].textContent,/Інвертор видалено/);
});

test('command transport failure reports unknown outcome and never retries', async()=>{
  const browser=ui(); browser.fail(); await browser.submit();
  assert.equal(browser.calls.length,1);
  assert.match(browser.elements.get('page-alert').textContent,/Результат невідомий/);
  assert.equal(browser.context.window.IngeconPanel.busy,false);
});

test('API connection loss cancels a pending confirmation', async()=>{
  const browser=ui(); await browser.submit();
  browser.context.window.IngeconPanel.connected=false;
  await browser.elements.get('confirm-command').listeners.click();
  assert.equal(browser.calls.length,1);
  assert.equal(browser.elements.get('command-dialog').open,false);
  assert.match(browser.elements.get('page-alert').textContent,/підтвердьте дію заново/);
});

test('missing selection cannot initiate a command', async()=>{
  const browser=ui(); delete browser.form.values.selected_ids;
  await browser.submit(); assert.equal(browser.calls.length,0);
  assert.match(browser.elements.get('page-alert').textContent,/Оберіть/);
});

test('theme toggle works even when persistent storage is disabled', ()=>{
  const browser=ui();
  browser.elements.get('theme-toggle').listeners.click();
  assert.equal(browser.context.document.documentElement.dataset.theme,'dark');
  assert.equal(browser.elements.get('theme-toggle').textContent,'Світла тема');
});
