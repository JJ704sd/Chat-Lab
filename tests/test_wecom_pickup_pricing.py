import tempfile
import unittest
from decimal import Decimal
import io
import json
from pathlib import Path
from zipfile import ZipFile

from chatlog_assistant.sources.wecom_pricing import (
    PriceMaintenance,
    build_single_price_workbook,
    build_price_workbook,
)
from chatlog_assistant.sources.wecom_storage import WecomLocalStorage
from chatlog_assistant.sources.wecom_parser import records_from_message_tree


def payload(amount="620", **overrides):
    result = {
        "origin": "上海", "destination": "深圳", "weight": "1000kg", "volume": "1cbm",
        "package_count": "1", "package_type": "箱", "service_time": "隔日达", "price": amount,
        "quote_company_id": overrides.pop("quote_company_id", "supplier-a"),
        "quote_company_name": overrides.pop("quote_company_name", "嘉航盛物流"),
        "currency": "CNY", "pricing_method": "总价", "quote_time": "2026-09-01T10:00:00+08:00",
        "source_message_id": "quote-1",
        "field_sources": {field: {"message_id": "quote-1", "excerpt": field} for field in
                          ("origin", "destination", "weight", "volume", "package_count", "service_time", "price", "quote_company")},
    }
    result.update(overrides)
    return result


class PickupPricingTests(unittest.TestCase):
    def make_prices(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        storage = WecomLocalStorage(Path(directory.name) / "analysis.db")
        storage.initialize()
        records, gaps = records_from_message_tree([{
            "source_message_id": "quote-1", "sender_id": "supplier-user", "sender_display": "报价人",
            "sender_corp_id": "supplier-a", "sender_corp_name": "嘉航盛物流",
            "sent_at": "2026-09-01T10:00:00+08:00", "content": "上海到深圳 620 CNY 总价，隔日达",
        }], account_id="a", source_database="db", conversation_id="room", conversation_name="测试群",
            source_reference="pickup-fixture", strict=True)
        assert not gaps
        storage.upsert_messages(records)
        return PriceMaintenance(storage)

    def rebuild_chat(self, messages):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        storage = WecomLocalStorage(Path(directory.name) / "analysis.db")
        storage.initialize()
        records, gaps = records_from_message_tree(
            messages, account_id="a", source_database="db", conversation_id="room",
            conversation_name="揽收车队群", source_reference="pickup-fixture", strict=True,
        )
        self.assertFalse(gaps)
        storage.upsert_messages(records)
        rebuilt = storage.rebuild_analysis(account_id="a", conversation_id="room")
        return storage, PriceMaintenance(storage), rebuilt

    def test_details_view_contains_current_and_unadopted_candidates_with_display_status(self):
        prices = self.make_prices()
        scope = {"account_id": "a", "source_database": "db", "conversation_id": "room"}
        current = prices.submit_candidate(payload(), source_kind="chat", source_scope=scope,
                                           idempotency_key="current", actor_type="system")
        incomplete = prices.submit_candidate(payload(currency=""), source_kind="chat", source_scope=scope,
                                             idempotency_key="incomplete", actor_type="system")
        rows = prices.list_prices(account_id="a", view="details")["items"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["display_status"] for row in rows}, {"有效价格", "待完善"})
        self.assertIn("currency", next(row for row in rows if row["candidate_id"] == incomplete["candidate_id"])["missing_fields"])
        self.assertEqual(sum(row.get("adoption_status") == "applied" for row in rows), 1)

    def test_single_workbook_has_core_header_and_exactly_one_typed_data_row(self):
        prices = self.make_prices()
        scope = {"account_id": "a", "source_database": "db", "conversation_id": "room"}
        result = prices.submit_candidate(payload(), source_kind="chat", source_scope=scope,
                                         idempotency_key="single", actor_type="system")
        item = prices.list_prices(account_id="a")["items"][0]
        data = build_single_price_workbook(item)
        with ZipFile(__import__("io").BytesIO(data)) as archive:
            workbook = archive.read("xl/workbook.xml").decode()
            rels = archive.read("xl/_rels/workbook.xml.rels").decode()
            sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("揽收车队", workbook)
        self.assertIn("relationships/styles", rels)
        self.assertEqual(sheet.count('<row r="'), 2)
        self.assertIn('t="n"><v>1000</v>', sheet)
        self.assertIn('t="n"><v>620</v>', sheet)
        self.assertEqual(sheet.count("<c r="), 14)

    def test_full_workbook_styles_part_is_linked(self):
        data = build_price_workbook([])
        with ZipFile(io.BytesIO(data)) as archive:
            rels = archive.read("xl/_rels/workbook.xml.rels").decode()
            styles = archive.read("xl/styles.xml").decode()
        self.assertIn("relationships/styles", rels)
        self.assertIn("cellXfs", styles)

    def test_rebuild_links_complete_single_route_and_keeps_two_explicit_plans(self):
        storage, prices, rebuilt = self.rebuild_chat([
            {"source_message_id": "q", "sender_id": "buyer", "sender_display": "业务",
             "sender_corp_id": "zhongji", "sender_corp_name": "中技",
             "sent_at": "2026-09-01T09:00:00+08:00",
             "content": "请报价 上海到深圳 1000kg 1cbm 1箱"},
            {"source_message_id": "r", "sender_id": "sup", "sender_display": "供应商",
             "sender_corp_id": "supplier", "sender_corp_name": "供应商甲",
             "sent_at": "2026-09-01T10:00:00+08:00", "reply_to_message_id": "q",
             "content": "方案一总价620 CNY隔日达；方案二总价800 CNY当日达"},
        ])
        self.assertEqual(rebuilt["price_maintenance"]["candidates"], 2)
        rows = prices.list_prices(account_id="a", view="details")["items"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["payload"]["price_raw"] for row in rows}, {"620", "800"})
        self.assertEqual({row["payload"]["service_time_raw"] for row in rows}, {"隔日达", "当日达"})
        self.assertEqual({row["payload"]["weight_value"] for row in rows}, {"1000"})
        self.assertEqual({row["payload"]["package_type"] for row in rows}, {"箱"})

    def test_same_service_different_amounts_are_selectable_and_not_lowest_selected(self):
        storage, prices, _ = self.rebuild_chat([
            {"source_message_id": "q", "sender_id": "buyer", "sender_display": "业务",
             "sender_corp_id": "zhongji", "sender_corp_name": "中技",
             "sent_at": "2026-09-01T09:00:00+08:00",
             "content": "请报价 上海到深圳 1000kg 1cbm 1箱"},
            {"source_message_id": "r", "sender_id": "sup", "sender_display": "供应商",
             "sender_corp_id": "supplier", "sender_corp_name": "供应商甲",
             "sent_at": "2026-09-01T10:00:00+08:00", "reply_to_message_id": "q",
             "content": "方案一总价620 CNY隔日达；方案二总价800 CNY隔日达"},
        ])
        rows = prices.list_prices(account_id="a", view="current")["items"]
        self.assertEqual({row["payload"]["price_raw"] for row in rows}, {"620", "800"})

    def test_multi_option_fields_do_not_leak_and_bare_unit_price_stays_unresolved(self):
        storage, prices, _ = self.rebuild_chat([
            {"source_message_id": "q", "sender_id": "buyer", "sender_display": "业务",
             "sender_corp_id": "zhongji", "sender_corp_name": "中技",
             "sent_at": "2026-09-01T09:00:00+08:00",
             "content": "请报价 上海到深圳 1000kg 1cbm 1箱"},
            {"source_message_id": "r", "sender_id": "sup", "sender_display": "供应商",
             "sender_corp_id": "supplier", "sender_corp_name": "供应商甲",
             "sent_at": "2026-09-01T10:00:00+08:00", "reply_to_message_id": "q",
             "content": "方案一总价620 CNY隔日达；方案二单价2 USD/kg，时效待确认"},
        ])
        candidates = prices.list_candidates(account_id="a")
        self.assertEqual(len(candidates), 2)
        by_price = {item["payload"]["price_raw"]: item for item in candidates}
        self.assertEqual(by_price["620"]["payload"]["pricing_method"], "total")
        self.assertEqual(by_price["2"]["payload"]["pricing_method"], "per_kg")
        self.assertEqual(by_price["2"]["payload"]["currency"], "USD")
        self.assertIsNone(by_price["2"]["payload"]["service_time_raw"])

        # A bare “单价” is not enough to invent a kg unit.
        storage2, prices2, _ = self.rebuild_chat([
            {"source_message_id": "q", "sender_id": "buyer", "sender_display": "业务",
             "sender_corp_id": "zhongji", "sender_corp_name": "中技",
             "sent_at": "2026-09-01T09:00:00+08:00",
             "content": "请报价 上海到深圳 1000kg 1cbm 1箱"},
            {"source_message_id": "r", "sender_id": "sup", "sender_display": "供应商",
             "sender_corp_id": "supplier", "sender_corp_name": "供应商甲",
             "sent_at": "2026-09-01T10:00:00+08:00", "reply_to_message_id": "q",
             "content": "单价2 USD"},
        ])
        self.assertIsNone(prices2.list_candidates(account_id="a")[0]["payload"]["pricing_method"])

    def test_multi_cargo_without_option_link_is_blank_and_incomplete(self):
        storage, prices, _ = self.rebuild_chat([
            {"source_message_id": "q", "sender_id": "buyer", "sender_display": "业务",
             "sender_corp_id": "zhongji", "sender_corp_name": "中技",
             "sent_at": "2026-09-01T09:00:00+08:00",
             "content": "请报价 上海到深圳 1000kg 1cbm 1箱，2000kg 2cbm 2箱"},
            {"source_message_id": "r", "sender_id": "sup", "sender_display": "供应商",
             "sender_corp_id": "supplier", "sender_corp_name": "供应商甲",
             "sent_at": "2026-09-01T10:00:00+08:00", "reply_to_message_id": "q",
             "content": "总价620 CNY隔日达"},
        ])
        rows = prices.list_prices(account_id="a", view="incomplete")["items"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["payload"]["weight_value"])
        self.assertIsNone(rows[0]["payload"]["volume_value"])
        self.assertIn("weight", rows[0]["missing_fields"])
        self.assertIn("cargo_association", rows[0]["payload"]["conditions"])

    def test_incomplete_candidate_can_be_corrected_and_survives_rebuild(self):
        storage, prices, _ = self.rebuild_chat([
            {"source_message_id": "q", "sender_id": "buyer", "sender_display": "业务",
             "sender_corp_id": "zhongji", "sender_corp_name": "中技",
             "sent_at": "2026-09-01T09:00:00+08:00",
             "content": "请报价 上海到深圳 1000kg 1cbm 1箱"},
            {"source_message_id": "r", "sender_id": "sup", "sender_display": "供应商",
             "sender_corp_id": "supplier", "sender_corp_name": "供应商甲",
             "sent_at": "2026-09-01T10:00:00+08:00", "reply_to_message_id": "q",
             "content": "总价620 隔日达"},
        ])
        candidate = prices.list_prices(account_id="a", view="incomplete")["items"][0]
        prices.configure_responsibility("operator", "运营审核人")
        corrected = prices.review_candidate(candidate["candidate_id"], action="correct_and_approve",
                                             correction={"currency": "CNY", "pricing_method": "total", "weight": "2000kg"}, actor_id="operator",
                                             actor_name="运营审核人", reason="人工确认总价")
        self.assertTrue(corrected["applied"])
        self.assertEqual(corrected["payload"]["weight_value"], "2000")
        self.assertEqual(len(prices.list_prices(account_id="a", view="details")["items"]), 1)
        storage.rebuild_analysis(account_id="a", conversation_id="room")
        rows = prices.list_prices(account_id="a", view="details")["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_status"], "有效价格")
        self.assertEqual(rows[0]["payload"]["price_raw"], "620")
        self.assertEqual(rows[0]["payload"]["weight_value"], "2000")

    def test_incomplete_single_workbook_blanks_numbers_and_scope_isolated(self):
        prices = self.make_prices()
        scope = {"account_id": "a", "source_database": "db", "conversation_id": "room"}
        candidate = prices.submit_candidate(payload(weight=None, volume=None, package_count=None, currency=""),
                                             source_kind="manual", source_scope=scope,
                                             idempotency_key="single-incomplete", actor_type="human", actor_id="u")
        item = prices.get_price_item(candidate_id=candidate["candidate_id"], account_id="a", source_database="db")
        self.assertIsNotNone(item)
        data = build_single_price_workbook(item)
        with ZipFile(io.BytesIO(data)) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode()
            styles = archive.read("xl/styles.xml").decode()
        self.assertEqual(sheet.count('<row r="'), 2)
        self.assertNotIn('r="C2" t="n"', sheet)
        self.assertIn("FFFFF2CC", styles)
        self.assertIsNone(prices.get_price_item(candidate_id=candidate["candidate_id"], account_id="a", source_database="other"))

    def test_single_workbook_redacts_phone_and_credentials(self):
        item = {"payload": {**payload(),
                            "quote_company_name": "供应商 13812345678 password=secret-pass"},
                "record_id": "price-safe", "display_status": "有效价格"}
        data = build_single_price_workbook(item)
        with ZipFile(io.BytesIO(data)) as archive:
            blob = b"".join(archive.read(name) for name in archive.namelist())
        self.assertNotIn(b"13812345678", blob)
        self.assertNotIn(b"secret-pass", blob)
        self.assertIn("138****5678".encode(), blob)


if __name__ == "__main__":
    unittest.main()
