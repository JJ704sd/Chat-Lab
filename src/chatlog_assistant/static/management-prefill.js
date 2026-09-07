/* Prepare editable presentation values; this module cannot approve or persist prices. */
(function (root) {
  'use strict';
  const blank = value => value == null || String(value).trim() === '';
  const usable = item => /^[A-Z]{3}$/.test(item.destination || '') &&
    !blank(item.airline) && !blank(item.weight_break) &&
    Number.isFinite(Number(item.amount)) && Number(item.amount) > 0;

  function prepare({lane, candidates, selectedId, selectedIds = [], tier = '+500', values = {}, prefilledFields = []}) {
    const pending = candidates.filter(c => c.lane === lane && c.status === 'pending' && !c.stale);
    const selected = pending.find(c => c.id === selectedId);
    let item, chosen, weightTier = tier;
    if (lane === 'pdf') {
      const eligible = pending.filter(usable);
      chosen = eligible.filter(c => selectedIds.includes(c.id));
      if (!chosen.length) {
        if (!eligible.some(c => c.weight_break === weightTier))
          weightTier = eligible.find(c => c.weight_break === '+500')?.weight_break || eligible[0]?.weight_break;
        chosen = eligible.filter(c => c.weight_break === weightTier)
          .sort((a, b) => Number(b.destination === 'BRU') - Number(a.destination === 'BRU') || a.destination.localeCompare(b.destination))
          .slice(0, 3);
      }
      item = chosen.find(c => c.id === selectedId) || chosen[0];
    } else if (lane === 'chat') {
      // Prefer the selected usable quote, otherwise a complete real example.
      // Multi-destination or ambiguous prices are never supplied with made-up rates.
      item = selected && usable({...selected, ...values}) ? selected :
        pending.find(c => c.destination === 'WAW' && usable(c)) || pending.find(usable);
      chosen = item ? [item] : [];
    }
    if (!item) throw new Error('暂无可直接演示的待审核价格，请先整理报价或开始新一轮演示。');

    const sameSelection = lane === 'pdf' || item.id === selectedId;
    const fields = sameSelection ? {...values} : {};
    const marked = new Set(sameSelection ? prefilledFields : []);
    if (lane === 'pdf') {
      // Shared edits apply to every selected PDF row; never copy an amount
      // from the focused row onto another row or an entire batch.
      for (const key of Object.keys(fields)) {
        if (!['origin', 'validity'].includes(key) && (chosen.length > 1 || item.id !== selectedId)) {
          delete fields[key]; marked.delete(key);
        }
      }
    }
    const presets = lane === 'pdf'
      ? {origin: '香港交仓（演示）', validity: '按原价表条款，仅供演示'}
      : {currency: 'CNY', unit: 'KG', validity: '仅本票演示使用', origin: '深圳交仓（演示）',
         conditions: '演示用途，实际费用与适用条件待确认'};
    for (const [key, preset] of Object.entries(presets)) {
      if (blank(fields[key] ?? item[key])) { fields[key] = preset; marked.add(key); }
    }
    return {selectedId: item.id, selectedIds: chosen.map(c => c.id), tier: item.weight_break,
      values: fields, prefilledFields: [...marked].filter(key => key in fields)};
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = {prepare};
  else root.PricingDemoPrefill = {prepare};
})(typeof window !== 'undefined' ? window : undefined);
