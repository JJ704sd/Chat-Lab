import tempfile
import unittest
from pathlib import Path

from chatlog_assistant.sources.wecom_parser import records_from_message_tree
from chatlog_assistant.sources.wecom_storage import WecomLocalStorage


def chat_fixture(storage):
    messages = [
        {"source_message_id": "ask", "sender_id": "z", "sender_display": "张调度", "sender_corp_name": "中技物流",
         "sent_at": "2026-09-03T09:00:00+08:00", "content": "上海到深圳，1000kg，2cbm，10箱，请报价"},
        {"source_message_id": "quote", "sender_id": "s", "sender_display": "李经理", "sender_corp_id": "fleet", "sender_corp_name": "合成车队",
         "sent_at": "2026-09-03T09:01:00+08:00", "content": "总价620元，隔日达", "reply_to_message_id": "ask"},
        {"source_message_id": "unknown", "sender_id": "x", "sender_display": "访客", "sender_corp_name": "其他企业",
         "sent_at": "2026-09-03T09:02:00+08:00", "content": "@中技物流 请查看 password=secret-value"},
        {"source_message_id": "forward", "sender_id": "x", "sender_display": "访客", "sender_corp_name": "其他企业",
         "sent_at": "2026-09-03T09:03:00+08:00", "forwarded_messages": [
             {"source_message_id": "original", "sender_id": "z", "sender_display": "原始调度", "sender_corp_name": "中技物流",
              "sent_at": "2026-09-02T08:00:00+08:00", "content": "按约定揽收"}]},
    ]
    records, gaps = records_from_message_tree(messages, account_id="fixture", source_database="synthetic",
        conversation_id="room", conversation_name="揽收车队合成群", source_reference="synthetic", strict=True)
    assert not gaps
    storage.upsert_messages(records)
    return records


class ChatWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = WecomLocalStorage(Path(self.temp.name) / "analysis.db")
        self.storage.initialize()
        self.records = chat_fixture(self.storage)
        self.scope = dict(account_id="fixture", source_database="synthetic", conversation_id="room")

    def test_evidence_contains_inquiry_context_and_safe_business_roles(self):
        evidence = self.storage.get_message_evidence("quote", **self.scope)
        self.assertIn("messages", evidence)
        rows = {row["message_id"]: row for row in evidence["messages"]}
        self.assertEqual(rows["ask"]["business_role"], "zhongji")
        self.assertEqual(rows["quote"]["business_role"], "unknown")
        self.assertEqual(rows["quote"]["reply_to_message_id"], rows["ask"]["id"])
        self.assertEqual(rows["unknown"]["business_role"], "unknown")
        self.assertNotIn("secret-value", str(evidence))
        self.assertNotIn("original", rows)
        self.assertEqual(rows["forward"]["forwarded_count"], 1)

    def test_confirmed_supplier_and_forward_preserve_original_author(self):
        self.storage.price_maintenance().configure_responsibility("reviewer", "审核员")
        self.storage.confirm_business_role(**self.scope, company_id="fleet", company_name="合成车队",
            business_role="supplier", basis="已核验车队合同", actor_id="reviewer", actor_name="审核员")
        evidence = self.storage.get_message_evidence("quote", **self.scope)
        self.assertEqual(evidence["message"]["business_role"], "supplier")
        forward = self.storage.get_forwarded_messages("forward", **self.scope)
        self.assertEqual(len(forward["messages"]), 1)
        self.assertEqual(forward["messages"][0]["sender_name"], "原始调度")
        self.assertEqual(forward["messages"][0]["business_role"], "zhongji")
        self.assertIsNone(self.storage.get_forwarded_messages("forward", account_id="another"))
        self.assertIsNone(self.storage.get_message_evidence("quote", account_id="another"))

    def test_context_expands_without_mixing_nested_or_other_sources(self):
        small = self.storage.get_message_evidence("ask", context_limit=1, **self.scope)
        self.assertTrue(small["has_more_after"])
        large = self.storage.get_message_evidence("ask", context_limit=20, **self.scope)
        self.assertGreater(len(large["messages"]), len(small["messages"]))
        self.assertFalse(large["has_more_after"])
        self.assertTrue(all(row["conversation_id"] == "room" for row in large["messages"]))

    def test_legacy_zhongji_identity_displays_without_rewriting_messages(self):
        # Old rows gained company_status='unknown' through an additive schema
        # update, while keeping their original company and reporting identity.
        with self.storage.connect() as conn:
            conn.execute("UPDATE messages SET company_status='unknown'")
            before = conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
        evidence = self.storage.get_message_evidence("quote", **self.scope)
        rows = {row["message_id"]: row for row in evidence["messages"]}
        self.assertEqual(rows["ask"]["business_role"], "zhongji")
        self.assertEqual(rows["ask"]["role_label"], "中技方")
        self.assertEqual(rows["quote"]["business_role"], "unknown")
        self.assertEqual(rows["unknown"]["business_role"], "unknown")
        forward = self.storage.get_forwarded_messages("forward", **self.scope)
        self.assertEqual(forward["messages"][0]["business_role"], "zhongji")
        with self.storage.connect() as conn:
            self.assertEqual(before, conn.execute("SELECT * FROM messages ORDER BY id").fetchall())

    def test_legacy_fallback_requires_consistent_author_company(self):
        cases = [
            ("conflict", "zhongji", "中技物流", "张调度"),
            ("unknown", "zhongji", None, "张调度"),
            ("unknown", "zhongji", "其他企业", "张调度"),
            ("unknown", "other", "中技物流", "张调度"),
            ("unknown", "zhongji", "中技物流", "张调度 @其他企业"),
        ]
        for status, bucket, company, sender in cases:
            with self.subTest(status=status, bucket=bucket, company=company, sender=sender):
                with self.storage.connect() as conn:
                    conn.execute("UPDATE messages SET company_status=?,subject_bucket=?,sender_corp_name=?,sender_name=? WHERE message_id='ask'",
                                 (status, bucket, company, sender))
                evidence = self.storage.get_message_evidence("ask", **self.scope)
                self.assertEqual(evidence["message"]["business_role"], "unknown")


if __name__ == "__main__":
    unittest.main()
