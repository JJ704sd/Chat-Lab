const test=require('node:test');
const assert=require('node:assert/strict');
const {create}=require('../src/chatlog_assistant/static/management-hosted.js');
const fixture=()=>({samples:[{id:'default'},{id:'other'}],pages:{},snapshots:Object.fromEntries(['default','other'].map(id=>[id,{
  materials:{revision:'r'},prices:[],decisions:[],candidates:['chat','pdf','pdf'].map((lane,i)=>({id:String(i),lane,
    destination:'ABC',airline:'ET',weight_break:'+100',amount:'30',currency:'CNY',unit:'KG',origin:'测试交仓',
    validity:'仅本票测试',conditions:'测试条件',evidence:{messages:[]},status:'pending'}))}]))});
function setup(){const records=new Map();const storage={getItem:k=>records.get(k)||null,setItem:(k,v)=>records.set(k,v)};
return {records,storage,api:create(fixture(),storage,()=> 'next-run')};}
const review=(run,ids,extra={})=>({sample:'default',run_id:run,revision:'r',candidate_ids:ids,action:'approve',corrections:{},...extra});
test('explicit preparation and review persist without crossing samples',async()=>{
 const {api,storage}=setup();assert.equal(api.snapshot('default').candidates.length,0);
 const prepared=await api.request('/pricing/prepare',{sample:'default',revision:'r',lane:'chat'});
 assert.equal(prepared.prices.length,0);
 const done=await api.request('/pricing/review',review(prepared.run_id,['0']));
 assert.equal(done.prices.length,1);assert.equal(done.decisions.length,1);
 assert.equal(create(fixture(),storage).snapshot('default').prices.length,1);
 assert.equal(api.snapshot('other').prices.length,0);
 await assert.rejects(api.request('/pricing/review',review(prepared.run_id,['0'])),/已经审核/);
});
test('PDF batch validation is atomic and chat cannot be batched',async()=>{
 const {api}=setup();const p=await api.request('/pricing/prepare',{revision:'r',lane:'pdf'});
 await assert.rejects(api.request('/pricing/review',review(p.run_id,['1','2'],{corrections:{amount:'40'}})),/批量/);
 assert.equal(api.snapshot('default').prices.length,0);
 const done=await api.request('/pricing/review',review(p.run_id,['1','2'],{corrections:{origin:'统一交仓'}}));
 assert.equal(done.prices.length,2);
});
test('invalid validity, stale run and persistence failure do not approve',async()=>{
 const {api,storage}=setup();const p=await api.request('/pricing/prepare',{revision:'r',lane:'chat'});
 await assert.rejects(api.request('/pricing/review',review(p.run_id,['0'],{corrections:{validity:'1'}})),/期限/);
 await assert.rejects(api.request('/pricing/review',review('old',['0'])),/轮次/);
 storage.setItem=()=>{throw new Error('quota');};
 await assert.rejects(api.request('/pricing/review',review(p.run_id,['0'])),/未保存/);
 assert.equal(api.snapshot('default').prices.length,0);
});
test('new runs retain the previous browser record and do not mutate fixture',async()=>{
 const {api,records}=setup();const p=await api.request('/pricing/prepare',{revision:'r',lane:'chat'});
 await api.request('/pricing/review',review(p.run_id,['0']));
 const next=await api.request('/pricing/new-run',{});
 assert.equal(next.run_id,'next-run');assert.equal(next.prices.length,0);
 assert.ok([...records.keys()].some(k=>k.includes(':archive:')));
});
