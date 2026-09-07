/* Private Sites demo adapter. No business backend; reviews stay in this browser. */
(function (root) {
  'use strict';
  const fields = ['origin','destination','airline','weight_break','amount','currency','unit','conditions','validity'];
  const copy = value => JSON.parse(JSON.stringify(value));
  function create(bundle, storage, newId = () => crypto.randomUUID()) {
    const prefix = 'zj-private-demo-v1:';
    function seed(sample) {
      const value = bundle.snapshots[sample];
      if (!value) throw new Error('演示样本不存在');
      return value;
    }
    function read(sample) {
      const initial = seed(sample);
      const saved = JSON.parse(storage.getItem(prefix + sample) || 'null');
      if (saved && saved.revision !== initial.materials.revision) throw new Error('精选资料已更新，请导出或清理旧浏览器演示数据后重试');
      return saved || {run_id:'initial-'+sample, revision:initial.materials.revision, prepared:{pdf:false,chat:false}, reviews:{}, decisions:[]};
    }
    function save(sample, state) {
      try {storage.setItem(prefix + sample, JSON.stringify(state));}
      catch {throw new Error('浏览器存储不可用或空间不足，本次操作未保存');}
    }
    function snapshot(sample, state = read(sample)) {
      const initial = seed(sample);
      const candidates = initial.candidates.filter(c => state.prepared[c.lane]).map(c => {
        const review = state.reviews[c.id];
        return {...copy(c), ...(review?.values || {}), status:review?.action === 'approve'?'approved':review?'rejected':'pending',
          reviewed_at:review?.occurred_at || null, version:review?1:0, demo_prefill_fields:review?.demo_prefill_fields || []};
      });
      return {...copy(initial), run_id:state.run_id, candidates, prices:candidates.filter(c=>c.status==='approved'), decisions:copy(state.decisions)};
    }
    async function request(path, body) {
      const sample = body?.sample || 'default';
      if (path === '/samples') return {samples:copy(bundle.samples)};
      if (path.startsWith('/page?')) {
        const params = new URLSearchParams(path.split('?')[1]);
        const sha = params.get('sha256'), page = params.get('page');
        if (!bundle.pages[sha]?.includes(Number(page))) throw new Error('PDF 页码不存在');
        const response = await fetch(`/pages/${sha}-${page}.json`);
        if (!response.ok) throw new Error('PDF 原件加载失败，请重试');
        return response.json();
      }
      const state = read(sample);
      if (path === '/pricing') return snapshot(sample,state);
      if (path === '/pricing/new-run') {
        try {storage.setItem(prefix+sample+':archive:'+state.run_id, JSON.stringify(state));}
        catch {throw new Error('无法保存上一轮记录，未开始新一轮');}
        const fresh = {...state, run_id:newId(), prepared:{pdf:false,chat:false},reviews:{},decisions:[]};
        save(sample,fresh); return snapshot(sample,fresh);
      }
      if (body?.revision !== state.revision) throw new Error('资料已更新，请刷新');
      if (path === '/pricing/prepare') {
        if (!['pdf','chat'].includes(body.lane)) throw new Error('请选择报价来源');
        state.prepared[body.lane] = true; save(sample,state); return snapshot(sample,state);
      }
      if (path !== '/pricing/review') throw new Error('在线演示使用预置资料；更换 PDF 请在本地项目操作');
      if (body.run_id !== state.run_id) throw new Error('演示轮次已变化，请刷新');
      const latest = read(sample);
      if (JSON.stringify(latest) !== JSON.stringify(state)) throw new Error('另一页面已更新记录，请刷新');
      const ids = body.candidate_ids;
      if (!Array.isArray(ids) || !ids.length || ids.length>250 || new Set(ids).size!==ids.length) throw new Error('请选择要审核的报价');
      if (!['approve','reject'].includes(body.action)) throw new Error('审核操作无效');
      const corrections = body.corrections || {};
      if (Object.entries(corrections).some(([k,v])=>!fields.includes(k)||typeof v!=='string'||v.length>2000)) throw new Error('审核字段无效');
      const marked = body.demo_prefill_fields || [];
      if (!Array.isArray(marked)||marked.some(k=>!(k in corrections))) throw new Error('演示预填标记无效');
      const candidates = snapshot(sample,state).candidates;
      const selected = ids.map(id=>candidates.find(c=>c.id===id));
      if (selected.some(c=>!c||c.status!=='pending')) throw new Error('报价不存在或已经审核，请刷新');
      if (selected.length>1 && (selected.some(c=>c.lane!=='pdf')||Object.keys(corrections).some(k=>!['origin','validity'].includes(k)))) throw new Error('群聊须逐条审核；PDF 批量仅支持统一交仓地和期限');
      const timestamp = new Date().toISOString();
      for (const candidate of selected) {
        const item = {...candidate,...Object.fromEntries(Object.entries(corrections).map(([k,v])=>[k,v.trim()]))};
        if (body.action==='approve') {
          if (fields.some(k=>!String(item[k]||'').trim())) throw new Error('请补齐交仓地、币种、单位、重量档、价格和适用期限');
          if (!Number.isFinite(Number(item.amount))||Number(item.amount)<=0) throw new Error('价格必须大于零');
          if (!/^[A-Z]{3}$/.test(item.destination)||!['CNY','HKD','USD','EUR'].includes(item.currency)||!['KG','票'].includes(item.unit)) throw new Error('请明确目的港、币种和计价单位');
          if (/^\d+$/.test(item.validity)) throw new Error('适用期限不能只填写数字，请填写日期或明确的适用说明');
        }
        const values = Object.fromEntries(fields.map(k=>[k,item[k]]));
        state.reviews[item.id] = {action:body.action,values,occurred_at:timestamp,demo_prefill_fields:marked};
        state.decisions.unshift({candidate_id:item.id,action:body.action,actor:'演示审核人（当前浏览器）',occurred_at:timestamp,
          reason:body.action==='approve'?'人工点击审核通过':'人工点击驳回',before_json:null,after_json:JSON.stringify(values)});
      }
      save(sample,state); return snapshot(sample,state);
    }
    return {request, snapshot};
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {create};
  else {
    let adapter;
    const ready = fetch('/demo.json').then(r=>{if(!r.ok)throw new Error('演示资料加载失败');return r.json();}).then(bundle=>adapter=create(bundle,localStorage));
    root.PricingDemoApi = {hosted:true,canUpload:false,csrf:async()=>'',request:async(path,body)=>{await ready;return adapter.request(path,body);}};
    document.addEventListener('DOMContentLoaded',()=>{
      document.querySelector('.top-actions>.tag').textContent='私有演示 · 浏览器保存';
    });
  }
})(typeof window !== 'undefined' ? window : undefined);
