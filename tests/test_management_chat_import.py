from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.import_management_chat import import_chat
from chatlog_assistant.sources.wecom_presentation import PresentationService


class LocalChatImportTests(unittest.TestCase):
    def test_embedded_quote_and_native_reply_do_not_attach_to_adjacent_inquiry(self):
        from scripts.import_management_chat import restore_explicit_quotes
        from chatlog_assistant.sources.wecom_presentation import extract_inquiries
        first = {"capture_order": 1, "sender": "我方联系人01", "role": "sales", "body": "CDG 240KG 2.626CBM", "source_message_id": "one"}
        second = {"capture_order": 2, "sender": "我方联系人02", "role": "sales", "body": "FRA 275KG 4.66CBM", "source_message_id": "two"}
        reply = {"capture_order": 3, "sender": "外部联系人03", "role": "supplier", "body": '“我方联系人01：\nCDG 240KG 2.626CBM”\n-----\nSQ +100 33', "source_message_id": "three"}
        followup = {"capture_order": 4, "sender": "外部联系人03", "role": "supplier", "body": "TK +100 32", "reply_to_message_id": "one", "source_message_id": "four"}
        source = {"messages": [first, second, reply, followup]}
        original = reply["body"]
        restore_explicit_quotes(source)
        inquiries = extract_inquiries(source["messages"])
        self.assertEqual([r["capture_order"] for r in inquiries[0]["responses"]], [3, 4])
        self.assertEqual(inquiries[1]["responses"], [])
        self.assertEqual(reply["body"], original)
        self.assertEqual(reply["reply_body"], "SQ +100 33")
        followup["reply_to_message_id"] = "missing"
        followup.pop("quoted_text", None)
        restore_explicit_quotes(source)
        self.assertNotIn(4, [r["capture_order"] for q in extract_inquiries(source["messages"]) for r in q["responses"]])

    def test_verified_import_redacts_and_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / 'input.db'
            row = dict(id='row1', account_id='fixture', conversation_id='group1', sender_id='sender1',
                       sender_name='测试姓名', sender_corp_name='测试公司', subject_bucket='zhongji',
                       sent_at='2026-09-04T09:00:00+08:00', text='WAW 560KG 1.07CBM 测试姓名 13812345678 a@example.com',
                       parse_status='parsed', message_id='native1', reply_to_message_id=None,
                       provenance_json=json.dumps({'snapshots': {'message.db': {
                           'consistent': True, 'integrity_ok': True, 'verified': True, 'db_sha256': 'a' * 64}}}))
            with closing(sqlite3.connect(database)) as conn, conn:
                conn.execute('CREATE TABLE messages (' + ','.join(k + ' TEXT' for k in row) + ')')
                conn.execute('INSERT INTO messages VALUES (' + ','.join('?' for _ in row) + ')', list(row.values()))
            target = root / 'presentation/source-chat.json'
            import_chat(database, target, 'group1', '2026-09-04')
            result = PresentationService(target.parent).snapshot()
            message = result['source']['messages'][0]
            self.assertNotIn('测试姓名', message['body'])
            self.assertNotIn('13812345678', message['body'])
            self.assertNotIn('a@example.com', message['body'])
            self.assertIn('560KG 1.07CBM', message['body'])
            self.assertEqual(message['source_message_id'], 'native1')
            before = target.read_bytes()
            with closing(sqlite3.connect(database)) as conn, conn:
                conn.execute("UPDATE messages SET provenance_json='{}'")
            with self.assertRaises(ValueError):
                import_chat(database, target, 'group1', '2026-09-04')
            self.assertEqual(target.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
