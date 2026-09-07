const test=require('node:test');
const assert=require('node:assert/strict');
const {build,create}=require('../src/chatlog_assistant/static/review-reminder.js');

function fixture(){
  const row={status:'pending',stale:false,supplier:'测试供应商',destination:'IST',airline:'3U',weight_break:'+100',amount:'28',currency:'',unit:'KG',conditions:'测试条件'};
  return {ready:true,sample:'one',run:'run-1',revision:'rev-1',curation:'curation-1',lane:'chat',inquiryId:'inquiry-a',selectedId:'chat-a',candidates:[
    {...row,id:'chat-a',lane:'chat',inquiry_id:'inquiry-a'},
    {...row,id:'chat-b',lane:'chat',inquiry_id:'inquiry-b'},
    {...row,id:'pdf-a',lane:'pdf',destination:'BRU',airline:'ET'},
    {...row,id:'pdf-b',lane:'pdf',destination:'LHR',airline:'ET'},
  ]};
}

test('PDF aggregates the full pending table; chat only includes the current inquiry',()=>{
  const state=fixture(), original=JSON.stringify(state);
  assert.equal(build(state).count,1);
  assert.equal(build({...state,lane:'pdf',selectedId:'pdf-b'}).count,2);
  assert.equal(build({...state,lane:'pdf'}).destinationCount,2);
  assert.equal(build({...state,lane:'pdf',selectedId:'pdf-b'}).targetId,'pdf-b');
  state.candidates.push({...state.candidates[0],id:'same-inquiry-alternative'});
  assert.equal(build(state).count,2);
  state.candidates.pop();
  assert.equal(JSON.stringify(state),original);
});

test('missing, stale, approved or unrelated quotes never trigger a review reminder',()=>{
  const state=fixture();
  assert.equal(build({...state,ready:false}),null);
  assert.equal(build({...state,inquiryId:undefined}),null);
  assert.equal(build({...state,inquiryId:'unknown'}),null);
  assert.equal(build({...state,candidates:[{...state.candidates[0],status:'approved'}]}),null);
  assert.equal(build({...state,candidates:[{...state.candidates[0],stale:true}]}),null);
  assert(build(state).missing.includes('币种'));
  assert(build(state).missing.includes('适用期限'));
  assert.match(build(state).price,/币种待确认/);
});

// A small DOM double exercises notification lifecycle without adding a browser dependency.
class Element {
  constructor(){this.hidden=false;this.open=false;this.children=[];this.listeners={};this.nodes=new Map();}
  setAttribute(){}
  append(...nodes){this.children.push(...nodes);}
  addEventListener(type,handler){(this.listeners[type]??=[]).push(handler);}
  emit(type,event={}){for(const fn of this.listeners[type]||[])fn(event);}
  action(action){this.emit('click',{target:{closest:()=>({dataset:{reminder:action}})}});}
  querySelector(selector){if(!this.nodes.has(selector))this.nodes.set(selector,new Element());return this.nodes.get(selector);}
  showModal(){this.open=true;}
  close(){this.open=false;this.emit('close');}
}
function setup(){
  let state=fixture(),tasks=new Map(),seq=0;
  const doc={body:new Element(),createElement:()=>new Element(),querySelector:()=>null};
  const reviewed=[],errors=[];
  const api=create({getContext:()=>state,onReview:m=>reviewed.push(m),onError:e=>errors.push(e),document:doc,
    schedule:fn=>{tasks.set(++seq,fn);return seq;},cancel:id=>tasks.delete(id)});
  const [notice,dialog]=doc.body.children;
  return {api,notice,dialog,reviewed,errors,state,replace:s=>state=s,
    tick:()=>{const pending=[...tasks.values()];tasks.clear();pending.forEach(fn=>fn());}};
}

test('notification opens only on click; acknowledgement navigates once and never approves',()=>{
  const f=setup(), original=JSON.stringify(f.state);
  f.api.notify();assert.equal(f.notice.hidden,false);assert.equal(f.dialog.open,false);
  f.notice.action('open');assert.equal(f.dialog.open,true);assert.equal(f.notice.hidden,true);
  assert.match(f.dialog.innerHTML,/审核提醒机器人/);assert.match(f.dialog.innerHTML,/审核人/);
  assert.equal(f.reviewed.length,0);
  f.dialog.action('review');f.dialog.action('review');
  assert.equal(f.dialog.querySelector('.reviewer-reply').hidden,false);
  assert.equal(f.reviewed.length,0);
  f.tick();assert.equal(f.reviewed.length,1);assert.equal(f.reviewed[0].targetId,'chat-a');
  assert.equal(f.dialog.open,false);assert.equal(JSON.stringify(f.state),original);
});

test('dismissed reminders can reopen manually without repeat popups',()=>{
  const f=setup();f.api.notify();f.notice.action('dismiss');
  f.api.notify();assert.equal(f.notice.hidden,true);
  assert.equal(f.api.open(),true);f.dialog.action('close');
  assert.equal(f.api.open(),true);
  f.dialog.action('review');f.dialog.action('close');f.tick();
  assert.equal(f.reviewed.length,0);
});

test('sample, inquiry, run, revision and candidate changes invalidate stale reminders',()=>{
  for(const change of [{sample:'other'},{run:'run-2'},{revision:'rev-2'},{curation:'new-curation'},{inquiryId:'inquiry-b'},{lane:'pdf'},{ready:false}]){
    const f=setup();f.api.notify();f.api.open();f.dialog.action('review');
    f.replace({...f.state,...change});f.api.sync();f.tick();
    assert.equal(f.dialog.open,false);assert.equal(f.notice.hidden,true);assert.equal(f.reviewed.length,0);
  }
  const f=setup();f.api.open();f.dialog.action('review');f.state.candidates[0].status='approved';f.tick();
  assert.equal(f.reviewed.length,0);assert.equal(f.errors.length,1);
});

test('untrusted supplier and quote text is escaped in notification and card markup',()=>{
  const f=setup();f.state.candidates[0].supplier='<img src=x onerror=alert(1)>';
  f.api.notify();f.api.open();
  assert(!f.notice.innerHTML.includes('<img'));
  assert(!f.dialog.innerHTML.includes('<img'));
  assert(f.dialog.innerHTML.includes('&lt;img'));
});
