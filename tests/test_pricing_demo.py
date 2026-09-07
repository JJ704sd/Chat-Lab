"""Human review boundaries using isolated synthetic fixtures, never real prices."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from chatlog_assistant.sources.pricing_demo import PricingDemo, chat_candidates
from chatlog_assistant.sources.wecom_airfreight import AirfreightOperationError


class PricingReviewTests(unittest.TestCase):
    def test_explicit_quote_body_is_not_parsed_as_a_new_airline_price(self):
        reply = self.source['messages'][1]
        reply['body'] = '旧询价 TK +500 99；供应商本次 O3 +500 28'
        reply['reply_body'] = 'O3 +500 28'
        self.source['messages'] = self.source['messages'][:2]
        self.write_sources()
        candidates = self.prepare('chat')['candidates']
        self.assertEqual([(c['airline'], c['amount']) for c in candidates], [('O3', '28')])
        self.assertIn('TK +500 99', candidates[0]['evidence']['messages'][1]['body'])

    def test_ambiguous_density_typo_does_not_become_a_price(self):
        self.source['messages'][1]['body'] = 'TK +100 1;1000 31.5'
        self.source['messages'] = self.source['messages'][:2]
        self.write_sources()
        candidates = self.prepare('chat')['candidates']
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['amount'], '')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = PricingDemo(self.root)
        self.card = {"source_sha256": "a" * 64, "file_name": "fixture.pdf", "supplier": "Test Supplier",
                     "airline": "ET", "currency": "HKD", "valid_from": "2026-08-19", "validity": "有效至另行通知",
                     "fees": [], "warnings": [], "rows": [
                         {"destination": "BRU", "breaks": {"+45": "42", "+100": "41", "+300": None, "+500": "39"},
                          "notes": "", "page": 1, "row": 2, "bbox": [10, 20, 200, 40], "minimum": "1700",
                          "raw_cells": ["BRU", "Brussels", "1700", "65", "42", "41", "", "39", ""], "status": "listed"}]}
        inquiry = "WAW 560KG 1.07CBM 货在深圳"
        self.source = {"source_mode": "wecom_ui_observation", "anonymized": True, "message_date": "2026-09-04",
                       "group_alias": "Test Group", "messages": [
                           {"capture_order": 1, "sender": "Test Sales", "role": "sales", "body": inquiry},
                           {"capture_order": 2, "sender": "Test Supplier", "role": "supplier", "body": "深圳O3 +500 1:300 28", "quoted_text": inquiry},
                           {"capture_order": 3, "sender": "Test Supplier", "role": "supplier", "body": "深圳TK +500 30", "quoted_text": inquiry}]}
        self.write_sources()

    def write_sources(self):
        (self.root / "active-rate.json").write_text(json.dumps(self.card, ensure_ascii=False), encoding="utf-8")
        (self.root / "source-chat.json").write_text(json.dumps(self.source, ensure_ascii=False), encoding="utf-8")

    def prepare(self, lane):
        return self.service.prepare(lane, self.service.sources.snapshot()["revision"])

    def review(self, snapshot, ids, **overrides):
        value = {"run_id": snapshot["run_id"], "revision": snapshot["materials"]["revision"],
                 "candidate_ids": ids, "action": "approve", "corrections": {"origin": "Test warehouse"}}
        value.update(overrides)
        return self.service.review(value)

    def test_sample_selection_preserves_default_and_isolates_reviews(self):
        from chatlog_assistant.sources.presentation_samples import sample_root, sample_catalog
        sample = self.root / "samples" / "chat-example"
        sample.mkdir(parents=True)
        for name in ("source-chat.json", "active-rate.json"):
            (sample / name).write_bytes((self.root / name).read_bytes())
        (self.root / "samples.json").write_text(json.dumps({"samples": [
            {"id": "chat-example", "title": "示例会话"}]}), encoding="utf-8")
        original = self.prepare("chat")
        before = self.service.path.read_bytes()
        self.assertEqual(sample_root(self.root, "default"), self.root.resolve())
        self.assertEqual(sample_catalog(self.root)[1]["id"], "chat-example")
        other = PricingDemo(sample_root(self.root, "chat-example"))
        self.assertIsNone(other.snapshot()["run_id"])
        prepared = other.prepare("chat", other.sources.snapshot()["revision"])
        other.new_run()
        self.assertNotEqual(original["run_id"], prepared["run_id"])
        self.assertEqual(self.service.path.read_bytes(), before)
        for invalid in ("../", "unknown", "chat-example/.."):
            with self.assertRaises(AirfreightOperationError):
                sample_root(self.root, invalid)

    def test_curated_demo_hides_unselected_messages_and_rejects_hidden_review(self):
        import hashlib
        prepared = self.prepare('chat')
        keep, hidden = prepared['candidates']
        before = self.service.path.read_bytes()
        source_hash = hashlib.sha256((self.root / 'source-chat.json').read_bytes()).hexdigest()
        policy = {'source_sha256': source_hash, 'cases': [{'inquiry_id': keep['inquiry_id'],
            'title': '明确报价', 'explanation': '核对报价', 'message_orders': [1, 2],
            'candidate_ids': [keep['id']]}]}
        (self.root / 'demo-curation.json').write_text(json.dumps(policy), encoding='utf-8')
        result = self.service.snapshot()
        self.assertEqual([c['id'] for c in result['candidates']], [keep['id']])
        self.assertEqual([m['capture_order'] for m in result['materials']['source']['messages']], [1, 2])
        self.assertEqual([r['capture_order'] for r in result['materials']['inquiries'][0]['responses']], [2])
        with self.assertRaises(AirfreightOperationError) as caught:
            self.review(prepared, [hidden['id']], action='reject')
        self.assertEqual(caught.exception.error_code, 'case_not_selected')
        self.assertEqual(self.service.path.read_bytes(), before)
        self.source['messages'][0]['body'] += ' source changed'
        self.write_sources()
        self.assertEqual(self.service.snapshot()['candidates'], [])

    def test_bare_number_is_not_a_meaningful_validity(self):
        prepared = self.prepare('pdf')
        with self.assertRaises(AirfreightOperationError) as caught:
            self.review(prepared, [prepared['candidates'][0]['id']],
                        corrections={'origin': '测试交仓', 'validity': '1'})
        self.assertEqual(caught.exception.error_code, 'validity_unclear')
        self.assertEqual(self.service.snapshot()['prices'], [])

    def test_get_does_not_initialize_or_auto_adopt(self):
        self.assertFalse(self.service.path.exists())
        self.assertEqual(self.service.snapshot()["prices"], [])
        self.assertFalse(self.service.path.exists())
        s = self.prepare("pdf")
        self.assertEqual(len(s["candidates"]), 3)
        self.assertTrue(all(c["status"] == "pending" for c in s["candidates"]))
        self.assertEqual(s["prices"], [])
        self.assertEqual(len(self.prepare("pdf")["candidates"]), 3)

    def test_pdf_batch_approval_is_persistent_and_attributed(self):
        s = self.prepare("pdf")
        ids = [c["id"] for c in s["candidates"][:2]]
        after = self.review(s, ids)
        self.assertEqual(len(after["prices"]), 2)
        self.assertEqual(len(after["decisions"]), 2)
        self.assertTrue(all(p["evidence"]["sha256"] == "a" * 64 for p in after["prices"]))
        self.assertTrue(all(p["reviewed_by"].endswith("（演示）") for p in after["prices"]))
        self.assertEqual(PricingDemo(self.root).snapshot()["prices"], after["prices"])
        with self.assertRaises(AirfreightOperationError):
            self.review(s, ids)
        self.assertEqual(len(self.service.snapshot()["decisions"]), 2)

    def test_chat_requires_single_review_and_missing_fields(self):
        s = self.prepare("chat")
        ids = [c["id"] for c in s["candidates"]]
        self.assertEqual(len(ids), 2)
        with self.assertRaises(AirfreightOperationError) as error:
            self.review(s, ids)
        self.assertEqual(error.exception.error_code, "chat_review_one")
        with self.assertRaises(AirfreightOperationError) as error:
            self.review(s, ids[:1])
        self.assertEqual(error.exception.error_code, "review_incomplete")
        self.assertFalse(self.service.snapshot()["prices"])
        after = self.review(s, ids[:1], corrections={"currency": "CNY", "unit": "KG", "validity": "仅本票，经人工核验"})
        self.assertEqual(len(after["prices"]), 1)
        self.assertIn("仅本票", after["prices"][0]["scope"])
        self.assertEqual(after["prices"][0]["evidence"]["messages"][1]["body"], self.source["messages"][1]["body"])

    def test_approval_needs_no_note_or_extra_confirmation(self):
        s = self.prepare("pdf")
        ids = [s["candidates"][0]["id"]]
        after = self.review(s, ids)
        self.assertEqual(after["decisions"][0]["reason"], "人工点击审核通过")
        self.assertEqual(after["prices"][0]["evidence"], s["candidates"][0]["evidence"])
        self.assertTrue(after["prices"][0]["reviewed_at"])

    def test_cannot_replace_review_action_with_forged_status(self):
        s = self.prepare("pdf")
        with self.assertRaises(AirfreightOperationError):
            self.review(s, [s["candidates"][0]["id"]], corrections={"status": "approved"})
        self.assertFalse(self.service.snapshot()["prices"])

    def test_demo_prefill_is_attributed_without_changing_original_evidence(self):
        s = self.prepare("chat")
        candidate = s["candidates"][0]
        self.assertFalse(s["prices"])
        after = self.review(s, [candidate["id"]],
                            corrections={"currency": "CNY", "unit": "KG", "validity": "仅本票演示使用"},
                            demo_prefill_fields=["currency", "unit", "validity"])
        self.assertEqual(after["prices"][0]["demo_prefill_fields"], ["currency", "unit", "validity"])
        self.assertEqual(after["prices"][0]["evidence"], candidate["evidence"])
        self.assertEqual(after["prices"][0]["amount"], candidate["amount"])
        self.assertEqual(json.loads(after["decisions"][0]["after_json"])["demo_prefill_fields"],
                         ["currency", "unit", "validity"])

    def test_prefill_marker_does_not_grant_approval_or_bypass_validation(self):
        s = self.prepare("chat")
        with self.assertRaises(AirfreightOperationError):
            self.review(s, [s["candidates"][0]["id"]], corrections={}, demo_prefill_fields=["currency"])
        with self.assertRaises(AirfreightOperationError):
            self.review(s, [s["candidates"][0]["id"]], action="prefill", demo_prefill_fields=[])
        self.assertEqual(self.service.snapshot()["prices"], [])

    def test_batch_failure_rolls_back_all_selected_prices(self):
        s = self.prepare("pdf")
        ids = [c["id"] for c in s["candidates"]]
        # Corrupt the last fixture value to prove earlier row writes roll back.
        with closing(sqlite3.connect(self.service.path)) as conn, conn:
            payload = json.loads(conn.execute("SELECT payload FROM candidates WHERE id=?", (ids[-1],)).fetchone()[0])
            payload["amount"] = "NaN"
            conn.execute("UPDATE candidates SET payload=? WHERE id=?", (json.dumps(payload), ids[-1]))
        with self.assertRaises(AirfreightOperationError):
            self.review(s, ids)
        after = self.service.snapshot()
        self.assertEqual(after["prices"], [])
        self.assertEqual(after["decisions"], [])
        self.assertTrue(all(c["status"] == "pending" for c in after["candidates"]))

    def test_rejection_does_not_publish_and_reimport_does_not_undo_review(self):
        s = self.prepare("pdf")
        ids = [s["candidates"][0]["id"]]
        self.review(s, ids, action="reject", corrections={})
        after = self.prepare("pdf")
        self.assertEqual(after["prices"], [])
        self.assertEqual(next(c for c in after["candidates"] if c["id"] in ids)["status"], "rejected")
        self.assertEqual(after["decisions"][0]["reason"], "人工点击驳回")

    def test_new_source_stays_pending_until_approved_then_versions(self):
        s = self.prepare("pdf")
        old = self.review(s, [s["candidates"][0]["id"]])["prices"][0]
        self.card.update(source_sha256="b" * 64, valid_from="2026-08-20")
        self.card["rows"][0]["breaks"][old["weight_break"]] = "40"
        self.write_sources()
        s = self.prepare("pdf")
        self.assertEqual(s["prices"][0]["amount"], "42")
        updated = next(c for c in s["candidates"] if c["evidence"]["sha256"] == "b" * 64 and c["weight_break"] == old["weight_break"])
        after = self.review(s, [updated["id"]])
        self.assertEqual(len(after["prices"]), 1)
        self.assertEqual(after["prices"][0]["amount"], "40")
        self.assertEqual(after["prices"][0]["version"], 2)
        self.assertEqual(json.loads(after["decisions"][0]["before_json"])["amount"], "42")

    def test_old_pdf_cannot_roll_back_current(self):
        s = self.prepare("pdf")
        old = self.review(s, [s["candidates"][0]["id"]])["prices"][0]
        self.card.update(source_sha256="b" * 64, valid_from="2026-08-01")
        self.write_sources()
        s = self.prepare("pdf")
        candidate = next(c for c in s["candidates"] if c["evidence"]["sha256"] == "b" * 64 and c["weight_break"] == old["weight_break"])
        with self.assertRaises(AirfreightOperationError) as error:
            self.review(s, [candidate["id"]])
        self.assertEqual(error.exception.error_code, "older_quote")
        self.assertEqual(self.service.snapshot()["prices"][0]["id"], old["id"])

    def test_new_run_archives_old_reviews_and_stale_page_cannot_write(self):
        old = self.prepare("pdf")
        self.review(old, [old["candidates"][0]["id"]])
        new = self.service.new_run()
        self.assertNotEqual(old["run_id"], new["run_id"])
        self.assertEqual(new["prices"], [])
        with self.assertRaises(AirfreightOperationError):
            self.review(old, [old["candidates"][1]["id"]])
        with closing(sqlite3.connect(self.service.path)) as conn, conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)

    def test_source_changed_blocks_approval_before_reparse(self):
        s = self.prepare("pdf")
        self.card["validity"] = "new terms"
        self.write_sources()
        with self.assertRaises(AirfreightOperationError):
            self.review(s, [s["candidates"][0]["id"]])
        self.assertEqual(self.service.snapshot()["prices"], [])

    def test_complex_reply_is_not_flattened_to_one_rate(self):
        self.source["messages"][1]["body"] = "CZ +100 1:200 26,1:250 25"
        self.write_sources()
        item = chat_candidates(self.service.sources.snapshot())[0]
        self.assertEqual(item["amount"], "")
        self.assertEqual(item["currency"], "")
        self.assertIn("多个价格条件", item["issues"][-1])


if __name__ == "__main__":
    unittest.main()
