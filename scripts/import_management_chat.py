"""Import one day of one verified local WeCom conversation into the demo."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from chatlog_assistant.sources.wecom_presentation import _canonical, _write


def restore_explicit_quotes(source: dict) -> None:
    """Preserve visible quote boundaries; missing explicit targets never use adjacency."""
    messages = source['messages']
    by_id = {}
    for message in messages:
        key = message.get('source_message_id')
        if key:
            by_id.setdefault(key, []).append(message)
    for message in messages:
        embedded = re.match(r'^[“"]([^：:\n]+)[：:]\s*(.*?)[”"]\s*[-—]{3,}\s*(.*)$',
                            message.get('body', ''), re.S)
        if embedded:
            message['quoted_text'] = embedded[2].strip()
            message['reply_body'] = embedded[3].strip()
            message['quote_basis'] = 'visible_quote_block'
        elif message.get('reply_to_message_id'):
            targets = by_id.get(message['reply_to_message_id'], [])
            target = targets[0] if len(targets) == 1 else None
            message['quoted_text'] = (target.get('body') if target and
                target['capture_order'] < message['capture_order'] else None) or '[引用原文未在当前样本中定位]'
            message['quote_basis'] = 'native_reply_id'


def import_chat(database: Path, destination: Path, conversation_id: str, day: str) -> dict:
    date.fromisoformat(day)
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? AND substr(sent_at,1,10)=? ORDER BY sent_at,id",
            (conversation_id, day))]
    if not rows:
        raise ValueError("No messages match the selected conversation and day")
    if len({r['account_id'] for r in rows}) != 1:
        raise ValueError("Conversation selection must identify exactly one account")
    manifests = {}
    for row in rows:
        provenance = json.loads(row['provenance_json'])
        snapshots = provenance.get('snapshots', {})
        if 'message.db' not in snapshots or not all(
            s.get('consistent') is True and s.get('integrity_ok') is True and s.get('verified') is True
            for s in snapshots.values()
        ):
            raise ValueError("Only verified local database snapshots may be imported")
        for name, snapshot in snapshots.items():
            manifests[name + ':' + snapshot['db_sha256']] = {
                'database': name, 'sha256': snapshot['db_sha256'],
                'wal_sha256': snapshot.get('wal_sha256'), 'verified': True}
    aliases = {}
    for row in rows:
        key = row['sender_id'] or row['sender_name']
        if key not in aliases:
            prefix = '我方联系人' if row['subject_bucket'] == 'zhongji' else '外部联系人'
            aliases[key] = f'{prefix}{len(aliases) + 1:02d}'
    replacements = {r['sender_name']: aliases[r['sender_id'] or r['sender_name']]
                    for r in rows if len(r['sender_name']) > 1}
    replacements.update({r['sender_corp_name']: '公司（已隐藏）' for r in rows if r['sender_corp_name']})

    def redact(text):
        for original in sorted(replacements, key=len, reverse=True):
            text = text.replace(original, replacements[original])
        text = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[手机号已隐藏]', text)
        text = re.sub(r'(?<!\d)\d{16,19}[\dXx]?(?!\d)', '[长号码已隐藏]', text)
        return re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[邮箱已隐藏]', text)

    messages = []
    for order, row in enumerate(rows, 1):
        provenance = json.loads(row['provenance_json'])
        messages.append({
            'capture_order': order, 'sender': aliases[row['sender_id'] or row['sender_name']],
            'role': 'sales' if row['subject_bucket'] == 'zhongji' else
                    'supplier' if row['subject_bucket'] == 'other' else 'unknown',
            'body': redact(row['text']) if row['parse_status'] == 'parsed' else '',
            'displayed_time': row['sent_at'], 'source_message_id': row['message_id'],
            'reply_to_message_id': row['reply_to_message_id'],
            'parse_status': row['parse_status'],
            'source_record_sha256': hashlib.sha256(row['id'].encode()).hexdigest(),
            'message_id_basis': provenance.get('message_id_basis', 'source_database'),
        })
    source = {
        'source_mode': 'wecom_local_snapshot', 'anonymized': True,
        'verification': {'verified': True, 'snapshots': list(manifests.values())},
        'message_date': day, 'complete_day': False, 'group_alias': '真实空运询价会话（脱敏）',
        'range_label': f"{rows[0]['sent_at'][11:16]}—{rows[-1]['sent_at'][11:16]} · 本机可读样本",
        'imported_at': datetime.now(timezone.utc).isoformat(),
        'source_conversation_sha256': hashlib.sha256(conversation_id.encode()).hexdigest(),
        'role_basis': '按本地身份元数据区分我方与外部；外部联系人的供应商身份及报价关联仍需人工核对',
        'redaction_scope': '发送人、已知公司名、手机号、邮箱及长号码；仅用于本机演示',
        'messages': messages,
    }
    if destination.is_file():
        old = destination.read_bytes()
        _write(destination.parent / 'source-history' / (hashlib.sha256(old).hexdigest() + '.json'), old)
    _write(destination, _canonical(source))
    return {'messages': len(messages), 'readable': sum(bool(m['body']) for m in messages),
            'date': day, 'range': source['range_label']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--conversation-id', required=True)
    parser.add_argument('--day', required=True)
    parser.add_argument('--destination', type=Path, default=PROJECT / 'data/management-demo/presentation/source-chat.json')
    args = parser.parse_args()
    print(json.dumps(import_chat(args.database, args.destination, args.conversation_id, args.day), ensure_ascii=True))
