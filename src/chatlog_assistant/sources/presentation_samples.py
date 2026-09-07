"""Optional local demo samples; omitted sample keeps the original price book."""
from pathlib import Path
import json
import re
import hashlib

from .wecom_airfreight import AirfreightOperationError


def sample_catalog(root: Path, *, include_hidden: bool = False) -> list[dict]:
    catalog = root / "samples.json"
    extra = json.loads(catalog.read_text(encoding="utf-8"))["samples"] if catalog.is_file() else []
    entries = [{"id": "default", "title": "会话 A · 9月4日 · 原演示", "description": "保留原有 PDF、群聊和审核记录"}, *extra]
    result = []
    for entry in entries:
        if not re.fullmatch(r"[a-z0-9-]{1,64}", entry['id']):
            continue
        folder = root if entry['id'] == 'default' else root / 'samples' / entry['id']
        policy = read_curation(folder)
        if policy and policy.get('hidden') and not include_hidden:
            continue
        result.append({**entry, **{k: policy[k] for k in ('title', 'description') if policy and k in policy}})
    return result


def read_curation(root: Path) -> dict | None:
    path = root / 'demo-curation.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None


def curate_materials(root: Path, materials: dict) -> dict:
    policy = read_curation(root)
    if policy is None:
        return materials
    valid = hashlib.sha256((root / 'source-chat.json').read_bytes()).hexdigest() == policy.get('source_sha256')
    cases = policy.get('cases', []) if valid else []
    selected = {case['inquiry_id']: case for case in cases}
    inquiries = []
    for inquiry in materials['inquiries']:
        case = selected.get(inquiry['id'])
        if case is None:
            continue
        orders = set(case['message_orders'])
        inquiries.append({**inquiry, 'demo_case': case,
            'message_orders': [n for n in inquiry['message_orders'] if n in orders],
            **{field: [r for r in inquiry[field] if r['capture_order'] in orders]
               for field in ('responses', 'price_responses')}})
    orders = {n for case in cases for n in case['message_orders']}
    messages = [m for m in (materials.get('source') or {}).get('messages', []) if m['capture_order'] in orders]
    # Projection only: do not rewrite original source, revisions, or past audits.
    return {**materials, 'source': {**(materials.get('source') or {}), 'messages': messages},
        'inquiries': inquiries, 'default_inquiry_id': inquiries[0]['id'] if inquiries else None,
        'curation': {'key': hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
                     'cases': cases, 'valid': valid, 'description': policy.get('description', ''),
                     'candidate_ids': [cid for case in cases for cid in case['candidate_ids']]},
        'metrics': {**materials['metrics'], 'visible_rows': len(messages),
                    'readable_messages': sum(bool(m.get('body')) for m in messages),
                    'inquiries': len(inquiries), 'destinations': len({d for q in inquiries for d in q['destinations']}),
                    'chat_price_candidates': sum(bool(q['price_responses']) for q in inquiries)}}


def sample_root(root: Path, sample: str = "default") -> Path:
    root = root.resolve()
    if sample == "default":
        return root
    if not re.fullmatch(r"[a-z0-9-]{1,64}", sample) or not any(
        item["id"] == sample for item in sample_catalog(root, include_hidden=True)[1:]
    ):
        raise AirfreightOperationError("sample_missing", "演示样本不存在，请刷新后重新选择", http_status=404)
    target = (root / "samples" / sample).resolve()
    if not target.is_relative_to(root) or not (target / "source-chat.json").is_file():
        raise AirfreightOperationError("sample_missing", "演示样本未就绪", http_status=404)
    return target
