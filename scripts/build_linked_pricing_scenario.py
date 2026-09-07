"""Build a clearly labelled PDF-linked demo, preserving original chat and audit data."""
from pathlib import Path
import hashlib
import json
import shutil
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from chatlog_assistant.sources.wecom_presentation import PresentationService, _canonical
from chatlog_assistant.sources.pricing_demo import chat_candidates


def build(root: Path) -> dict:
    original = root / 'samples/verified-jixiangtong'
    card = json.loads((original / 'active-rate.json').read_text(encoding='utf8'))
    row = next(row for row in card['rows'] if row['destination'] == 'BRU')
    amount = row['breaks']['+300']
    key = 'scenario-bru-pdf'
    target = root / 'samples' / key
    target.mkdir(parents=True, exist_ok=True)
    binding = dict(destination='BRU', weight_break='+300', amount=amount,
        currency=card['currency'], airline=card['airline'], rate_source_sha256=card['source_sha256'],
        origin='香港交仓（演示）',
        original_chat_sha256=hashlib.sha256((original / 'source-chat.json').read_bytes()).hexdigest())
    request = 'BRU\n2plts/396.20kg/1.75cbm，ratio:1:226\n110*110*105cm、80*80*75cm\n请按供应商价表确认本票运价。'
    reply = f"香港交仓（演示） {card['airline']} +300 {card['currency']} {amount}/kg，基础运价，附加费另计。适用期限按原价表条款核对。"
    source = dict(source_mode='demo_scenario', anonymized=True, scenario=binding,
        message_date='2026-09-04', group_alias='吉翔通国际询价群', complete_day=False,
        range_label='PDF 联动情景模拟 · 非真实聊天回放',
        messages=[dict(capture_order=1, sender='中技物流', role='sales', body=request, displayed_time='2026-09-04T10:48:00+08:00'),
                  dict(capture_order=2, sender='吉翔通国际', role='supplier', body=reply, reply_body=reply,
                       quoted_text=request, unit='KG', displayed_time='2026-09-04T10:49:00+08:00')])
    source_path = target / 'source-chat.json'
    encoded = _canonical(source)
    if source_path.exists() and source_path.read_bytes() != encoded:
        raise ValueError('Existing scenario differs; review its history before replacing it')
    source_path.write_bytes(encoded)
    shutil.copyfile(original / 'active-rate.json', target / 'active-rate.json')
    pdf = original / ('rate-' + card['source_sha256'] + '.pdf')
    shutil.copyfile(pdf, target / pdf.name)
    snapshot = PresentationService(target).snapshot()
    quotes = chat_candidates(snapshot)
    assert len(quotes) == 1
    quote = quotes[0]
    assert (quote['destination'], quote['weight_break'], quote['amount'], quote['currency'], quote['airline']) == ('BRU', '+300', amount, card['currency'], card['airline'])
    policy = dict(title='吉翔通国际 · BRU 价表联动演示',
        description='对照同一份 PDF 的 BRU +300 档演示群聊确认；聊天为改编情景，非真实报价记录。',
        source_sha256=hashlib.sha256(encoded).hexdigest(), cases=[dict(inquiry_id=quote['inquiry_id'],
            candidate_ids=[quote['id']], message_orders=[1,2], title=f"BRU · +300 · {card['currency']} {amount}/kg",
            explanation=f"情景模拟：吉翔通国际作为演示报价方，复述 {card['supplier']} 价表中的 BRU · {card['airline']} · +300 · {card['currency']} {amount}/kg。货量 396.20 kg，使用 +300 档；+500 档仍为 {row['breaks']['+500']}，不混用。交仓及适用期限需人工确认。")])
    (target / 'demo-curation.json').write_bytes(_canonical(policy))
    # Hide the replaced entry, keeping all of its source files and previous reviews.
    previous = original / 'demo-curation.json'
    old = json.loads(previous.read_text(encoding='utf8'))
    old['hidden'] = True
    previous.write_bytes(_canonical(old))
    catalog_path = root / 'samples.json'
    catalog = json.loads(catalog_path.read_text(encoding='utf8'))
    catalog['samples'] = [dict(id=key,title=policy['title'],description=policy['description']),
                          *[entry for entry in catalog['samples'] if entry['id'] != key]]
    catalog_path.write_bytes(_canonical(catalog))
    return dict(sample=key, destination='BRU', tier='+300', amount=amount, currency=card['currency'])


if __name__ == '__main__':
    print(json.dumps(build(PROJECT / 'data/management-demo/presentation'), ensure_ascii=False))
