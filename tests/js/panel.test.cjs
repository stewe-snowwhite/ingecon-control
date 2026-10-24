'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

class Element {
  constructor() { this.listeners={}; this.children=[]; this.dataset={}; this.attributes={}; this.hidden=true; this.open=false; this.disabled=false; this.textContent=''; }
  addEventListener(name, fn) { this.listeners[name]=fn; }
  dispatchEvent(event) { this.listeners[event.type]?.(event); }
  append(value) { this.children.push(value); }
  replaceChildren() { this.children=[]; }
  setAttribute(key,value) { this.attributes[key]=String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  showModal() { this.open=true; }
  close() { this.open=false; }
}
class FormDataDouble {
  constructor(form) { this.values=new Map(Object.entries(form.values).map(([key,value])=>[key,[value]])); }
  getAll(key) { return this.values.get(key) || []; }
  append(key,value) { this.values.set(key,[...this.getAll(key),value]); }
  set(key,value) { this.values.set(key,[value]); }
}
function ui(override={}, actionUrl='/control') {
  const ids = ['theme-toggle','command-dialog','page-alert','cancel-command','confirm-command',
    'confirmation-title','confirmation-description','confirmation-targets','confirmation-note','result-list','command-results','dashboard'];
  const elements=new Map(ids.map(id=>[id,new Element()]));
  elements.get('confirmation-note').hidden=false;
  const form=new Element(); form.id='control-form'; form.setAttribute('id','control-form'); form.setAttribute('action',actionUrl);
  // HTMLFormElement named access shadows .action when buttons share name=action.
  Object.defineProperty(form,'action',{configurable:true,get:()=>({[Symbol.toStringTag]:'RadioNodeList'})});
  form.values={csrf_token:'test-csrf',selected_ids:'1',limit_value:'37'};
  const calls=[]; let fail=false; let delayed=false; let resolve; let httpError=null;
  const otherForms=[];
  const document={
    documentElement:{dataset:{theme:'light'}},
    getElementById:id=>elements.get(id), createElement:()=>new Element(),
    querySelectorAll:selector=>selector==='form' ? [form,...otherForms] : [form], dispatchEvent() {}
  };
  const preview={confirmation_required:true,confirmation_token:'signed-test-token',action:'delete',
    label:'Видалити',value:100,inverters:[{id:1,name:'<script>device</script>',ip_address:'192.0.2.1'}]};
  Object.assign(preview,override);
  const results={results:[{id:1,name:'<script>device</script>',outcome:'deleted',message:'Інвертор видалено.'}]};
  const context=vm.createContext({
    document,window:{},Event,FormData:FormDataDouble,AbortController,TypeError,SyntaxError,
    localStorage:{setItem(){throw new Error('disabled');}},
    setTimeout:()=>1,clearTimeout(){},
    fetch:async(url, options)=>{
      assert.equal(url,actionUrl); assert.equal(options.headers.Accept,'application/json');
      assert.equal(options.body.getAll('csrf_token')[0],'test-csrf');
      calls.push({url,options,data:options.body});
      if (fail) throw new TypeError('simulated timeout');
      if (calls.length>1 && delayed) await new Promise(done=>{resolve=done;});
      return httpError ? {ok:false,status:httpError.status,json:async()=>({error:httpError.message})} :
        {ok:true,status:200,json:async()=>calls.length===1 ? preview : results};
    }
  });
  vm.runInContext(fs.readFileSync('static/panel.js','utf8'),context);
  const submit=(action='delete')=>form.listeners.submit({preventDefault(){},submitter:{name:'action',value:action}});
  return {elements,form,calls,context,submit,otherForms,fail:()=>{fail=true;},
    reject:(status,message)=>{httpError={status,message};},delay:()=>{delayed=true;},finish:()=>resolve()};
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
  assert.equal(browser.calls[1].data.getAll('confirmation_token')[0],'signed-test-token');
  browser.finish(); await completion;
  assert.equal(browser.elements.get('command-dialog').open,false);
  assert.equal(browser.elements.get('command-results').hidden,false);
  assert.match(browser.elements.get('result-list').children[0].textContent,/Інвертор видалено/);
});

for (const action of ['limit','limit_max']) for (const oldMarkup of [false,true]) test(action+(oldMarkup ? ' cached markup' : '')+': compact preview shows only percentage and target list', async()=>{
  const browser=ui({action,label:'Обмежити потужність',value:37,
    inverters:[{id:1,name:'100TL',ip_address:'192.0.2.1',command:{port:1502,device_id:7,function:16,address:1000,values:[3,12123]}}]});
  const note=browser.elements.get('confirmation-note');
  if (oldMarkup) {
    browser.elements.delete('confirmation-note');
    browser.elements.get('command-dialog').querySelector=selector=>selector==='.notice.warning' ? note : null;
  }
  await browser.submit();
  assert.equal(browser.elements.get('confirmation-description').textContent,action==='limit' ? '37%' : '100%');
  assert.equal(note.hidden,true);
  assert.equal(browser.elements.get('confirmation-targets').children[0].textContent,
    '100TL · 192.0.2.1');
  assert.equal(browser.calls.length,1);
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

for (const action of ['on','off','limit','limit_max']) {
  test(action+': both POST stages use the action attribute despite RadioNodeList shadowing', async()=>{
    const browser=ui({action,value:37});
    assert.equal(String(browser.form.action),'[object RadioNodeList]');
    await browser.submit(action);
    assert.equal(browser.calls.length,1);
    assert.equal(browser.calls[0].url,'/control');
    assert.equal(browser.elements.get('command-dialog').open,true);
    assert.deepEqual(browser.calls[0].data.getAll('action'),[action]);
    assert.deepEqual(browser.calls[0].data.getAll('selected_ids'),['1']);
    assert.deepEqual(browser.calls[0].data.getAll('limit_value'),['37']);
    assert.equal(browser.calls[0].options.credentials,'same-origin');
    // Capture the URL at preview time; later DOM edits must not retarget a confirmation.
    browser.form.setAttribute('action','/unexpected');
    await browser.elements.get('confirm-command').listeners.click();
    assert.equal(browser.calls.length,2);
    assert.equal(browser.calls[1].url,'/control');
    assert.deepEqual(browser.calls[1].data.getAll('action'),[action]);
    assert.deepEqual(browser.calls[1].data.getAll('csrf_token'),['test-csrf']);
    assert.deepEqual(browser.calls[1].data.getAll('confirmation_token'),['signed-test-token']);
  });
}

test('a single name/id=action control cannot replace the POST URL', async()=>{
  const browser=ui({action:'off'});
  Object.defineProperty(browser.form,'action',{get:()=>({[Symbol.toStringTag]:'HTMLButtonElement'})});
  await browser.submit('off');
  await browser.elements.get('confirm-command').listeners.click();
  assert.deepEqual(browser.calls.map(call=>call.url),['/control','/control']);
});

test('delete form keeps its own backend route for preview and confirmation', async()=>{
  const browser=ui({},'/delete/7');
  await browser.submit('delete');
  await browser.elements.get('confirm-command').listeners.click();
  assert.deepEqual(browser.calls.map(call=>call.url),['/delete/7','/delete/7']);
});

for (const stage of ['preview','confirmation']) {
  for (const [status,message] of [[400,'Недійсний CSRF.'],[401,'Сесію завершено.'],[403,'Недостатньо прав.'],[500,'Помилка сервера.']]) {
    test(stage+': HTTP '+status+' is displayed without automatic retry', async()=>{
      const browser=ui({action:'off'});
      if (stage==='confirmation') await browser.submit('off');
      browser.reject(status,message);
      if (stage==='preview') await browser.submit('off');
      else await browser.elements.get('confirm-command').listeners.click();
      assert.equal(browser.calls.length,stage==='preview' ? 1 : 2);
      assert.equal(browser.elements.get('page-alert').textContent,message);
      assert.equal(browser.context.window.IngeconPanel.busy,false);
      if (status===401) assert.equal(browser.context.window.IngeconPanel.connected,false);
    });
  }
}

test('missing action attribute does not submit to the current page', async()=>{
  const browser=ui(); delete browser.form.attributes.action;
  await browser.submit('off');
  assert.equal(browser.calls.length,0);
  assert.match(browser.elements.get('page-alert').textContent,/адрес/);
});

test('named id cannot bypass the dashboard selection requirement', async()=>{
  const browser=ui(); delete browser.form.values.selected_ids;
  Object.defineProperty(browser.form,'id',{get:()=>({[Symbol.toStringTag]:'HTMLInputElement'})});
  await browser.submit('on');
  assert.equal(browser.calls.length,0);
  assert.match(browser.elements.get('page-alert').textContent,/Оберіть/);
});

test('post-delete cleanup reads action attributes even when form properties are shadowed', async()=>{
  const browser=ui(); browser.elements.delete('dashboard');
  const edit=new Element(); edit.setAttribute('action','/edit/1');
  const deletion=new Element(); deletion.setAttribute('action','/delete/1');
  const unrelated=new Element(); unrelated.hidden=false;
  for (const form of [edit,deletion,unrelated]) Object.defineProperty(form,'action',{get:()=>({[Symbol.toStringTag]:'RadioNodeList'})});
  edit.hidden=false; deletion.hidden=false;
  browser.otherForms.push(edit,deletion,unrelated);
  await browser.submit('delete');
  await browser.elements.get('confirm-command').listeners.click();
  assert.equal(edit.hidden,true);
  assert.equal(deletion.hidden,true);
  assert.equal(unrelated.hidden,false);
  assert.equal(browser.elements.get('page-alert').hidden,true);
});
