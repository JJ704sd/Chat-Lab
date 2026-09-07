from __future__ import annotations

import json
import base64
import hashlib
from pathlib import Path
import tempfile
import unittest

from chatlog_assistant.sources.trimanson_rates import match_pdf_rate, parse_trimanson_pdf
from chatlog_assistant.sources.wecom_airfreight import AirfreightOperationError
from chatlog_assistant.sources.wecom_presentation import PresentationService, extract_inquiries


def message(order, body, role="sales", quote=None):
    return {"capture_order": order, "sender": "测试业务" if role == "sales" else "测试供应商",
            "role": role, "body": body, "quoted_text": quote, "displayed_time": None}


class InquiryTests(unittest.TestCase):
    def test_packaging_and_month_tokens_are_not_destinations(self):
        item = extract_inquiries([message(1, "RUH 1PLT 160KG 0.96CBM SEP 4 1PKG")])[0]
        self.assertEqual(item['destinations'], ['RUH'])

    def test_total_weight_wins_over_per_piece_and_volume(self):
        items = extract_inquiries([
            message(1, "AMM 20箱 每箱重量12kg 总240 1.5CBM"),
            message(2, "TO VIE 共3.2CBM，830kgs，5pallets"),
        ])
        self.assertEqual([q["gross_kg"] for q in items], ["240", "830"])
        self.assertEqual(items[0]["pieces"], "20")

    def test_identical_cargo_is_grouped_but_quote_remains_separate(self):
        inquiry = "TO CDG 60CTNS 900KGS 12CBM"
        items = extract_inquiries([message(1, inquiry), message(2, inquiry),
            message(3, "TK +500 33", "supplier", inquiry)])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["message_orders"], [1, 2])
        self.assertEqual(items[0]["responses"][0]["association"], "explicit_quote")
        self.assertIsNone(items[0]["responses"][0]["currency"])

    def test_interleaved_inquiries_do_not_get_guessed_reply(self):
        items = extract_inquiries([message(1, "WAW 10CTNS 160KG 1CBM"),
            message(2, "DXB 15CTNS 250KG 2CBM"), message(3, "O3 +100 26", "supplier")])
        self.assertTrue(all(not item["responses"] for item in items))

    def test_adjacent_reply_is_marked_unconfirmed(self):
        items = extract_inquiries([message(1, "WAW 460KG 1CBM 货在深圳"), message(2, "O3 +300 27", "supplier")])
        self.assertEqual(items[0]["responses"][0]["association"], "adjacent_context")

    def test_shorthand_units_and_battery_remain_pending(self):
        item = extract_inquiries([message(1, "CDG 10/120/1.5 鼠标有电池")])[0]
        self.assertEqual(item["gross_kg"], "120")
        self.assertEqual(item["volume_cbm"], "1.5")
        self.assertEqual(len(item["pending"]), 2)
        self.assertIsNone(item["displayed_time"])


class RateTests(unittest.TestCase):
    def test_pdf_viewer_reports_actual_page_count_and_rejects_out_of_range(self):
        objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
                   b'<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
                   b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] /Resources << >> >>',
                   b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] /Resources << >> >>']
        data = b'%PDF-1.4\n'
        offsets = [0]
        for i, obj in enumerate(objects, 1):
            offsets.append(len(data))
            data += str(i).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
        xref = len(data)
        data += b'xref\n0 5\n0000000000 65535 f \n'
        data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
        data += f'trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            digest = hashlib.sha256(data).hexdigest()
            (root / f'rate-{digest}.pdf').write_bytes(data)
            service = PresentationService(root)
            page = service.pdf_page(digest, 2)
            self.assertEqual(page['page_count'], 2)
            self.assertEqual((page['width'], page['height']), (200, 300))
            self.assertTrue(base64.b64decode(page['image'].split(',')[1]).startswith(b'\x89PNG\r\n\x1a\n'))
            with self.assertRaises(AirfreightOperationError) as error:
                service.pdf_page(digest, 3)
            self.assertEqual(error.exception.error_code, 'page_missing')

    def setUp(self):
        self.card = {"valid_from": "2026-08-01", "currency": "HKD", "rows": [
            {"destination": "BRU", "breaks": {"+45": "9", "+100": "8", "+300": None, "+500": "6", "+1000": "5"}}
        ]}

    def test_effective_date_and_absent_destination(self):
        self.assertEqual(match_pdf_rate(self.card, "BRU", "500", "2026-07-31")["status"], "not_effective")
        self.assertEqual(match_pdf_rate(self.card, "WAW", "500", "2026-09-04")["status"], "not_covered")

    def test_weight_boundaries_blank_rates_and_minimum(self):
        self.assertEqual(match_pdf_rate(self.card, "BRU", "99.99", "2026-09-04")["amount"], "9")
        self.assertEqual(match_pdf_rate(self.card, "BRU", "100", "2026-09-04")["amount"], "8")
        self.assertEqual(match_pdf_rate(self.card, "BRU", "300", "2026-09-04")["status"], "inquiry_required")
        self.assertEqual(match_pdf_rate(self.card, "BRU", "4", "2026-09-04")["status"], "needs_confirmation")

    def test_corrupt_and_unrelated_file_do_not_get_fabricated_rates(self):
        self.assertIsNone(parse_trimanson_pdf(b"%PDF-1.4 corrupt data"))
        self.assertIsNone(parse_trimanson_pdf(b"unrelated attachment"))


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = {"source_mode": "wecom_ui_observation", "anonymized": True,
                       "message_date": "2026-09-04", "complete_day": False,
                       "messages": [message(1, "WAW 460KG 1CBM 货在深圳"), message(2, "O3 +300 27", "supplier")]}
        (self.root / "source-chat.json").write_text(json.dumps(self.source, ensure_ascii=False), encoding="utf-8")
        self.service = PresentationService(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_verified_local_snapshot_is_distinct_from_ui_observation(self):
        self.source.update(source_mode="wecom_local_snapshot", verification={
            "verified": True, "snapshots": [{"database": "message.db", "sha256": "a" * 64, "verified": True}]})
        (self.root / "source-chat.json").write_text(json.dumps(self.source), encoding="utf-8")
        self.assertEqual(self.service.snapshot()["source"]["source_mode"], "wecom_local_snapshot")
        self.source["verification"]["verified"] = False
        (self.root / "source-chat.json").write_text(json.dumps(self.source), encoding="utf-8")
        with self.assertRaises(AirfreightOperationError):
            self.service.snapshot()

    def test_draft_never_invents_currency_total_or_send_state(self):
        data = self.service.snapshot()
        draft = self.service.create_draft(data["default_inquiry_id"], data["revision"])
        self.assertIsNone(draft["currency"])
        self.assertIsNone(draft["total"])
        self.assertFalse(draft["sent"])
        self.assertIn("O3 +300 27", draft["body"])
        self.assertIn("待确认", draft["body"])
        self.assertEqual(draft, self.service.create_draft(data["default_inquiry_id"], data["revision"]))
        self.assertEqual(draft["evidence"]["reply_orders"], [2])

    def test_changed_source_invalidates_previous_revision(self):
        data = self.service.snapshot()
        self.source["messages"][1]["body"] = "O3 +300 29"
        (self.root / "source-chat.json").write_text(json.dumps(self.source, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(AirfreightOperationError) as result:
            self.service.create_draft(data["default_inquiry_id"], data["revision"])
        self.assertEqual(result.exception.error_code, "source_changed")
        self.assertFalse((self.root / "drafts").exists())

    def test_snapshot_is_read_only_and_empty_source_is_not_synthetic(self):
        with tempfile.TemporaryDirectory() as other:
            service = PresentationService(Path(other) / "not_created")
            self.assertFalse(service.snapshot()["ready"])
            self.assertFalse(service.root.exists())


if __name__ == "__main__":
    unittest.main()
