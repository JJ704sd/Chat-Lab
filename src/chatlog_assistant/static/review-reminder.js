/* In-page WeCom-style review reminder. No external messages or approval writes. */
(function (root) {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const unique = values => [...new Set(values.filter(Boolean))];
  const labels = {origin:'交仓地',destination:'目的港',airline:'航司',weight_break:'重量档',amount:'价格',currency:'币种',unit:'计价单位',conditions:'适用条件',validity:'适用期限'};
  const chatIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M4 4h16v12H9l-5 4V4zM8 8h8M8 12h5"/></svg>';

  function build(context) {
    if (!context?.ready || !['pdf','chat'].includes(context.lane)) return null;
    const pdf = context.lane === 'pdf';
    if (!pdf && !context.inquiryId) return null;
    const rows = (context.candidates || []).filter(c => c.lane === context.lane && c.status === 'pending' && !c.stale &&
      (pdf || c.inquiry_id === context.inquiryId));
    if (!rows.length) return null;
    const target = rows.find(c => c.id === context.selectedId) || rows[0];
    const scope = JSON.stringify([context.sample,context.run,context.revision,context.curation,context.lane,pdf?'rate-card':context.inquiryId]);
    return {
      scope, fingerprint:JSON.stringify([scope, rows.map(c=>[c.id,c.version||0])]),
      lane:context.lane, targetId:target.id, count:rows.length,
      suppliers:unique(rows.map(c=>c.supplier)).join('、'),
      destinationCount:unique(rows.map(c=>c.destination)).length,
      title:pdf?'供应商价表待审核':'询价报价待审核',
      source:pdf?'供应商 PDF 价表':context.scenario?'PDF 联动情景模拟':'当前询价 · 供应商回复',
      summary:pdf?`${rows.length} 条价格 · ${unique(rows.map(c=>c.destination)).length} 个目的港`:
        `${target.destination} · ${target.airline} · ${target.weight_break || '重量档待确认'}`,
      price:pdf?'':`${target.currency || '币种待确认'} ${target.amount || '价格待确认'} / ${target.unit || '单位待确认'}`,
      details:pdf?'整份价表已整理，请核对原件、附加费与适用条款。':target.scope || '仅适用于当前这笔询价',
      missing:Object.entries(labels).filter(([key])=>rows.some(c=>!String(c[key]??'').trim())).map(([,label])=>label),
    };
  }

  function create({getContext, onReview, onError, document:doc=root.document, schedule=setTimeout, cancel=clearTimeout}) {
    const notice = doc.createElement('aside');
    notice.className = 'review-notification';
    notice.hidden = true;
    notice.setAttribute('aria-label', '审核消息提醒');
    const conversation = doc.createElement('dialog');
    conversation.className = 'review-messenger';
    conversation.setAttribute('aria-labelledby', 'reviewMessengerTitle');
    doc.body.append(notice, conversation);
    const seen = new Set();
    let active=null, notified=null, timer=null, generation=0;
    const now = () => new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date());
    const model = () => build(getContext());
    function reset() {
      generation++;
      if (timer !== null) cancel(timer);
      timer=null; active=null;
    }
    function close() {
      reset();
      if (conversation.open) conversation.close();
    }
    function sync() {
      const fresh = model();
      if (notified && fresh?.fingerprint !== notified.fingerprint) {
        notice.hidden=true; notified=null;
      }
      if (active && fresh?.fingerprint !== active.fingerprint) close();
    }
    function notify() {
      sync();
      const value=model();
      if (!value || seen.has(value.fingerprint)) return;
      seen.add(value.fingerprint);
      notified=value;
      notice.innerHTML=`<div class="review-notification-top"><span>${chatIcon} 企业微信 · 提醒演示</span><button type="button" data-reminder="dismiss" aria-label="收起审核提醒">×</button></div>
        <div role="status" aria-live="polite"><strong>审核提醒机器人</strong><p>${esc(value.suppliers)}：${esc(value.title)}</p><small>${value.lane==='pdf'?esc(value.summary):`本票 ${value.count} 条报价，等待人工确认`}</small></div>
        <button type="button" class="review-notification-open" data-reminder="open">查看消息 ${chatIcon}</button>`;
      notice.hidden=false;
    }
    function open() {
      const value=model();
      if (!value) { onError('当前没有可提醒的待审核报价，请重新整理资料。'); return false; }
      close();
      active=value;
      seen.add(value.fingerprint);
      notice.hidden=true;
      const stamp=now();
      conversation.innerHTML=`<div class="review-messenger-frame">
        <aside class="review-messenger-rail" aria-hidden="true"><div class="review-messenger-app">企</div>${chatIcon}<span>消息</span></aside>
        <section class="review-messenger-chat">
          <header class="review-messenger-header"><div><h3 id="reviewMessengerTitle">审核提醒机器人</h3><p>与审核人的会话 <span>企微交互模拟</span></p></div><button type="button" data-reminder="close" aria-label="关闭提醒会话">×</button></header>
          <div class="review-messenger-messages" tabindex="0" aria-label="审核提醒会话记录">
            <div class="review-messenger-time">${esc(stamp)}</div>
            <div class="review-messenger-message"><div class="review-messenger-avatar">${chatIcon}</div><div class="review-messenger-content"><span class="review-messenger-name">审核提醒机器人</span>
              <p class="review-messenger-bubble">${value.lane==='pdf'?'供应商价表已整理完成，请审核本次收到的价格。':'当前询价已收到供应商报价，请核对后确认。'}</p>
              <article class="review-message-card"><div class="review-message-card-kind">${chatIcon}<span>运价审核通知</span><b>待审核</b></div><h4>${esc(value.title)}</h4>
                <dl><div><dt>供应商</dt><dd>${esc(value.suppliers)}</dd></div><div><dt>来源</dt><dd>${esc(value.source)}</dd></div><div><dt>待审核</dt><dd>${value.count} 条价格</dd></div></dl>
                <div class="review-message-card-summary"><strong>${esc(value.summary)}</strong>${value.price?`<span>${esc(value.price)}</span>`:''}<small>${esc(value.details)}</small></div>
                <p class="review-message-card-warning">${value.missing.length?`需确认：${esc(value.missing.join('、'))}。`:'请核对原始依据与适用条件。'}审核通过前，价格不会生效。</p>
                <button type="button" data-reminder="review">前往审核 <span aria-hidden="true">→</span></button>
              </article><span class="review-messenger-receipt">已读</span></div></div>
            <div class="review-messenger-message reviewer-reply" hidden><div class="review-messenger-avatar">审</div><div class="review-messenger-content"><span class="review-messenger-name">审核人</span><p class="review-messenger-bubble" role="status" aria-live="polite"></p><small>正在打开人工审核页面…</small></div></div>
          </div>
          <div class="review-messenger-compose"><span aria-hidden="true">☺　▱</span><span>点击卡片“前往审核”接收任务</span></div>
          <div class="review-messenger-note">站内演示消息 · 不会发送到真实企业微信</div>
        </section></div>`;
      conversation.showModal();
      return true;
    }
    function accept() {
      if (timer !== null || !active) return;
      const value=active, fresh=model();
      if (fresh?.fingerprint !== value.fingerprint) { close(); onError('提醒对应的报价已变化，请重新打开最新提醒。'); return; }
      const ticket=++generation;
      conversation.querySelector('[data-reminder="review"]').disabled=true;
      const reply=conversation.querySelector('.reviewer-reply');
      reply.hidden=false;
      reply.querySelector('.review-messenger-bubble').textContent='收到，开始审核。';
      const scroll=conversation.querySelector('.review-messenger-messages');
      scroll.scrollTop=scroll.scrollHeight;
      timer=schedule(()=>{
        timer=null;
        if (generation!==ticket || !conversation.open) return;
        if (model()?.fingerprint !== value.fingerprint) { close(); onError('提醒对应的报价已变化，请重新打开最新提醒。'); return; }
        close();
        onReview(value);
      },850);
    }
    notice.addEventListener('click',event=>{
      const action=event.target.closest('[data-reminder]')?.dataset.reminder;
      if (action==='dismiss') notice.hidden=true;
      if (action==='open') open();
    });
    conversation.addEventListener('click',event=>{
      const action=event.target.closest('[data-reminder]')?.dataset.reminder;
      if (action==='close') close();
      if (action==='review') accept();
    });
    conversation.addEventListener('cancel',reset);
    conversation.addEventListener('close',()=>{
      if (conversation.open) return;
      reset();
      if (!doc.activeElement || doc.activeElement===doc.body) doc.querySelector('[data-action="review-reminder"]')?.focus();
    });
    return {notify,open,sync,available:()=>Boolean(model())};
  }
  const api={build,create};
  if (typeof module!=='undefined' && module.exports) module.exports=api;
  else root.PricingDemoReminder=api;
})(typeof window!=='undefined'?window:globalThis);
