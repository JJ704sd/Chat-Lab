const test = require('node:test');
const assert = require('node:assert/strict');
const {prepare} = require('../src/chatlog_assistant/static/management-prefill.js');
const quote = (id, extra={}) => ({id, lane:'pdf', status:'pending', destination:id, airline:'ET',
  weight_break:'+500', amount:'39', currency:'HKD', unit:'KG', origin:'', validity:'按原表', ...extra});

test('PDF chooses up to three real prices, skips reviewed rows and an empty tier', () => {
  const candidates=['BRU','ABJ','ADD','ACC'].map(id=>quote(id,{weight_break:'+45'}));
  candidates.push(quote('OLD',{status:'approved'}),quote('BAD',{amount:''}));
  const before=JSON.stringify(candidates);
  const plan=prepare({lane:'pdf',candidates,tier:'+500'});
  assert.equal(plan.tier,'+45');
  assert.deepEqual(plan.selectedIds,['BRU','ABJ','ACC']);
  assert.equal(plan.values.origin,'香港交仓（演示）');
  assert.deepEqual(plan.prefilledFields,['origin']);
  assert.equal('amount' in plan.values,false);
  assert.equal('currency' in plan.values,false);
  assert.equal(JSON.stringify(candidates),before);
});

test('Repeated preparation preserves explicit choices and entered values', () => {
  const candidates=[quote('BRU'),quote('ABJ')];
  const args={lane:'pdf',candidates,selectedId:'ABJ',selectedIds:['ABJ'],values:{origin:'用户指定交仓地',validity:'用户指定期限'}};
  const plan=prepare(args);
  assert.deepEqual(plan.selectedIds,['ABJ']);
  assert.deepEqual(plan.values,args.values);
  assert.deepEqual(plan.prefilledFields,[]);
});

test('Chat fills missing demo fields while preserving its original price and manual currency', () => {
  const candidates=[quote('WAW',{lane:'chat',currency:'',unit:'',validity:'',origin:'深圳',conditions:'O3 +500 28',amount:'28'})];
  const plan=prepare({lane:'chat',candidates,selectedId:'WAW',values:{currency:'USD'}});
  assert.deepEqual(plan.selectedIds,['WAW']);
  assert.equal(plan.values.currency,'USD');
  assert.equal(plan.values.unit,'KG');
  assert.equal(plan.values.validity,'仅本票演示使用');
  assert.equal('amount' in plan.values,false);
  assert.equal('currency' in Object.fromEntries(plan.prefilledFields.map(k=>[k,true])),false);
  assert.equal(candidates[0].status,'pending');
});

test('Ambiguous or stale chat prices are skipped, never invented', () => {
  const candidates=[quote('UNCLEAR',{lane:'chat',destination:'VIE/BUD',amount:''}),quote('STALE',{lane:'chat',stale:true}),quote('WAW',{lane:'chat',origin:'深圳'})];
  const plan=prepare({lane:'chat',candidates,selectedId:'UNCLEAR'});
  assert.deepEqual(plan.selectedIds,['WAW']);
  assert.throws(()=>prepare({lane:'chat',candidates:candidates.slice(0,2)}),/暂无/);
});

test('Switching or batching PDF selections cannot copy a focused-row amount to other prices', () => {
  const candidates=[quote('BRU'),quote('ABJ')];
  const plan=prepare({lane:'pdf',candidates,selectedId:'BRU',selectedIds:['BRU','ABJ'],values:{amount:'100',_edit:true,origin:'香港'}});
  assert.equal(plan.values.amount,undefined);
  assert.equal(plan.values._edit,undefined);
  assert.equal(plan.values.origin,'香港');
});
