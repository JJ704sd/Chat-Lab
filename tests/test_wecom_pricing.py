import json
import unittest
from pathlib import Path
import tempfile

from chatlog_assistant.sources.wecom_subject import WecomSubjectClassifier
from chatlog_assistant.sources.wecom_pricing import PriceMaintenance, PriceReviewSettings, build_price_workbook, parse_price_workbook
from chatlog_assistant.sources.wecom_parser import records_from_message_tree
from chatlog_assistant.sources.wecom_storage import WecomLocalStorage


def price_payload(amount="620", company="嘉航盛物流", *, when="2026-09-01T10:00:00+08:00", currency="CNY"):
    evidence = {field: {"message_id": "quote-1", "excerpt": str(value)} for field, value in {
        "origin": "上海", "destination": "深圳", "weight": "1000kg", "volume": "1cbm",
        "package_count": "1", "service_time": "隔日达", "price": amount, "quote_company": company,
    }.items()}
    return {
        "origin": "上海", "destination": "深圳", "weight": "1000kg", "volume": "1cbm",
        "package_count": "1", "package_type": "箱", "service_time": "隔日达", "price": amount,
        "quote_company_id": company.lower().replace(" ", "-"), "quote_company_name": company,
        "currency": currency, "pricing_method": "总价", "quote_time": when,
        "source_message_id": "quote-1", "field_sources": evidence,
    }


def seed_quote(storage, scope):
    records, gaps = records_from_message_tree([
        {"source_message_id": "quote-1", "sender_id": "quote-user", "sender_display": "报价人",
         "sender_corp_id": "quote-corp", "sender_corp_name": "嘉航盛物流",
         "sent_at": "2026-09-01T10:00:00+08:00", "content": "上海到深圳 620 CNY 总价，隔日达"}
    ], account_id=scope["account_id"], source_database=scope["source_database"],
        conversation_id=scope["conversation_id"], conversation_name=scope.get("conversation_name") or "测试群",
        source_reference="test-quote", strict=True)
    assert not gaps
    storage.upsert_messages(records)


class WecomPricingIdentityTests(unittest.TestCase):
    def test_conflicting_identity_metadata_is_pending_confirmation(self):
        result = WecomSubjectClassifier().classify(
            sender_id="u1",
            sender_display="张三 @甲公司",
            sender_corp_id="corp-b",
            sender_corp_name="乙公司",
        )

        self.assertEqual(result.company_status, "conflict")
        self.assertEqual(result.subject_bucket, "unknown")
        self.assertEqual(result.corp_name, None)


class WecomPriceMaintenanceTests(unittest.TestCase):
    def test_xlsx_round_trip_has_required_sheets_and_safe_text(self):
        workbook = build_price_workbook([{
            "payload": {**price_payload("=1+1"), "source_kind": "manual"},
            "record_id": "price_1", "version": 1, "state": "current",
        }])
        parsed = parse_price_workbook(workbook)
        self.assertEqual(parsed["headers"][:7], ["起点", "终点", "重量", "体积", "包装数量", "时效", "价格"])
        self.assertEqual(parsed["rows"][0]["价格"], "'=1+1")
        self.assertIn("填写说明", parsed["notes_present"] and "填写说明")

    def test_company_and_service_conditions_are_isolated_and_old_quote_cannot_roll_back(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = WecomLocalStorage(Path(directory) / "analysis.db")
            storage.initialize()
            prices = PriceMaintenance(storage)
            scope = {"account_id": "acc", "source_database": "fixture", "conversation_id": "room"}
            seed_quote(storage, scope)

            first = prices.submit_candidate(price_payload("620"), source_kind="chat", source_scope=scope,
                                            idempotency_key="q-620", actor_type="system")
            self.assertTrue(first["applied"])
            newer = prices.submit_candidate(price_payload("680", when="2026-09-02T10:00:00+08:00"), source_kind="chat", source_scope=scope,
                                            idempotency_key="q-680", actor_type="system")
            self.assertTrue(newer["applied"])
            older = prices.submit_candidate(price_payload("500", when="2026-08-31T10:00:00+08:00"), source_kind="chat", source_scope=scope,
                                            idempotency_key="q-500", actor_type="system")
            self.assertFalse(older["applied"])
            self.assertEqual(older["adoption_status"], "not_applied_old")

            other_company = prices.submit_candidate(price_payload("700", company="另一家物流", when="2026-09-03T10:00:00+08:00"), source_kind="chat", source_scope=scope,
                                                    idempotency_key="q-other", actor_type="system")
            self.assertTrue(other_company["applied"])
            rows = prices.list_prices(account_id="acc", source_database="fixture", conversation_id="room")["items"]
            self.assertEqual(len(rows), 2)
            by_company = {row["payload"]["quote_company_name"]: row["payload"]["price_raw"] for row in rows}
            self.assertEqual(by_company["嘉航盛物流"], "680")
            self.assertEqual(by_company["另一家物流"], "700")

            duplicate = prices.submit_candidate(price_payload("680", when="2026-09-02T10:00:00+08:00"), source_kind="chat", source_scope=scope,
                                                idempotency_key="q-680", actor_type="system")
            self.assertTrue(duplicate["idempotent"])

    def test_missing_currency_stays_pending_and_manual_review_requires_configured_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = WecomLocalStorage(Path(directory) / "analysis.db")
            storage.initialize()
            prices = PriceMaintenance(storage)
            scope = {"account_id": "acc", "source_database": "fixture", "conversation_id": "room"}
            seed_quote(storage, scope)
            pending = prices.submit_candidate(price_payload(currency=""), source_kind="chat", source_scope=scope,
                                              idempotency_key="missing-currency", actor_type="system")
            self.assertFalse(pending["applied"])
            self.assertEqual(pending["review_status"], "needs_review")
            self.assertEqual(prices.list_prices(account_id="acc")["total"], 0)

            manual = prices.submit_candidate(price_payload("621"), source_kind="manual", source_scope=scope,
                                             idempotency_key="manual-621", actor_type="human", actor_id="operator")
            self.assertEqual(manual["review_status"], "pending")
            prices.configure_responsibility("operator", "运营审核人")
            approved = prices.review_candidate(manual["candidate_id"], action="approve", actor_id="operator",
                                               actor_name="运营审核人", reason="已核验原始报价")
            self.assertTrue(approved["reviewed"])
            self.assertTrue(approved["applied"])
            self.assertEqual(approved["current"]["payload"]["origin_normalized"], "上海")
            with storage.connect() as conn:
                audit_actions = {row["action"] for row in conn.execute("SELECT action FROM price_audit")}
            self.assertIn("review", audit_actions)

    def test_unknown_unit_and_unresolved_chat_evidence_stay_reviewable(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = WecomLocalStorage(Path(directory) / "analysis.db")
            storage.initialize()
            prices = PriceMaintenance(storage)
            scope = {"account_id": "acc", "source_database": "fixture", "conversation_id": "room"}
            seed_quote(storage, scope)
            unknown_unit = price_payload()
            unknown_unit["weight"] = "100lb"
            result = prices.submit_candidate(unknown_unit, source_kind="chat", source_scope=scope,
                                             idempotency_key="unknown-unit", actor_type="system")
            self.assertFalse(result["applied"])
            self.assertIn("unknown_unit", {item["error_code"] for item in result["validation"]["errors"]})

            fake_evidence = price_payload("630")
            fake_evidence["source_message_id"] = "not-imported"
            fake_evidence["field_sources"] = {
                field: {"message_id": "not-imported", "excerpt": "伪造证据"}
                for field in ("origin", "destination", "weight", "volume", "package_count", "service_time", "price", "quote_company")
            }
            unresolved = prices.submit_candidate(fake_evidence, source_kind="chat", source_scope=scope,
                                                 idempotency_key="fake-evidence", actor_type="system")
            self.assertFalse(unresolved["applied"])
            self.assertIn("evidence_not_found", {item["error_code"] for item in unresolved["validation"]["errors"]})

    def test_excel_preview_and_confirm_use_the_same_candidate_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = WecomLocalStorage(Path(directory) / "analysis.db")
            storage.initialize()
            prices = PriceMaintenance(storage)
            scope = {"account_id": "acc", "source_database": "fixture", "conversation_id": "room"}
            seed_quote(storage, scope)
            created = prices.submit_candidate(price_payload(), source_kind="chat", source_scope=scope,
                                              idempotency_key="chat-1", actor_type="system")
            workbook = build_price_workbook(prices.list_prices(account_id="acc")["items"])
            preview = prices.preview_import(workbook, source_scope=scope, request_id="import-1")
            self.assertEqual(preview["summary"]["errors"], 0)
            prices.configure_responsibility("operator", "运营审核人")
            confirmed = prices.confirm_import(preview["preview_id"], selected_rows=[2], actor_id="operator", actor_name="运营审核人")
            self.assertTrue(confirmed["confirmed"])
            self.assertEqual(len(confirmed["results"]), 1)

    def test_excel_preview_does_not_expose_another_account_record(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = WecomLocalStorage(Path(directory) / "analysis.db")
            storage.initialize()
            prices = PriceMaintenance(storage)
            other_scope = {"account_id": "other", "source_database": "fixture", "conversation_id": "room"}
            own_scope = {"account_id": "acc", "source_database": "fixture", "conversation_id": "room"}
            seed_quote(storage, other_scope)
            prices.submit_candidate(price_payload(), source_kind="chat", source_scope=other_scope,
                                    idempotency_key="other-price", actor_type="system")
            workbook = build_price_workbook(prices.list_prices(account_id="other")["items"])
            preview = prices.preview_import(workbook, source_scope=own_scope, request_id="cross-account-preview")
            row = preview["rows"][0]
            self.assertIsNone(row["old"])
            self.assertTrue(any(error["field"] == "record_id" for error in row["errors"]))

    def test_http_writes_require_same_origin_and_csrf_and_messages_are_independent(self):
        from http.server import ThreadingHTTPServer
        from threading import Thread
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        from chatlog_assistant.sources.wecom_web import WecomDashboardHandler
        with tempfile.TemporaryDirectory() as directory:
            storage = WecomLocalStorage(Path(directory) / "analysis.db")
            storage.initialize()
            scope = {"account_id": "acc", "source_database": "fixture", "conversation_id": "room"}
            seed_quote(storage, scope)
            handler = type("PriceFixtureHandler", (WecomDashboardHandler,), {"storage": storage})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urlopen(base + "/api/wecom/csrf") as response:
                    csrf = json.load(response)["csrf_token"]
                payload = price_payload()
                payload.update({"source_scope": {"account_id": "acc", "source_database": "fixture", "conversation_id": "room"},
                                "source_kind": "chat", "idempotency_key": "http-1"})
                request = Request(base + "/api/wecom/price-candidates", data=json.dumps(payload).encode(),
                                   headers={"Content-Type": "application/json", "X-CSRF-Token": csrf, "Origin": base}, method="POST")
                with urlopen(request) as response:
                    self.assertEqual(response.status, 201)
                    result = json.load(response)
                self.assertFalse(result["applied"])
                self.assertEqual(result["review_status"], "pending")
                bad = Request(base + "/api/wecom/price-candidates", data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json", "X-CSRF-Token": csrf, "Origin": "http://evil.example"}, method="POST")
                with self.assertRaises(HTTPError) as caught:
                    try:
                        urlopen(bad)
                    except HTTPError as error:
                        error.close()
                        raise
                self.assertEqual(caught.exception.code, 403)
                with urlopen(base + "/api/wecom/prices?account_id=acc&view=pending") as response:
                    self.assertEqual(json.load(response)["total"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)


class WecomEvidenceAndModelTests(unittest.TestCase):
    def test_full_message_scope_is_independent_and_evidence_keeps_parent_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = WecomLocalStorage(Path(directory) / "analysis.db")
            storage.initialize()
            records, gaps = records_from_message_tree([
                {"source_message_id": "forward-root", "sender_id": "u-root", "sender_display": "转发者", "sender_corp_name": "转发公司",
                 "sent_at": "2026-09-01T09:00:00+08:00", "content_type": "合并转发记录", "content": "[群聊的聊天记录]",
                 "forwarded_messages": [{"source_message_id": "nested-quote", "sender_id": "u-quote", "sender_display": "报价人",
                                          "sender_corp_name": "报价公司", "sent_at": "2026-09-01T09:00:00+08:00", "content": "上海到深圳 620"}]}
            ], account_id="acc", source_database="fixture", conversation_id="room", conversation_name="测试群", source_reference="fixture", strict=True)
            self.assertFalse(gaps)
            storage.upsert_messages(records)
            page = storage.list_messages(account_id="acc", conversation_id="room", limit=1)
            self.assertEqual(page["range_total"], 2)
            self.assertEqual(page["matched_total"], 2)
            self.assertEqual(page["loaded_count"], 1)
            self.assertNotIn("text", page["items"][0])
            next_page = storage.list_messages(account_id="acc", conversation_id="room", cursor=page["next_cursor"], limit=1)
            self.assertEqual(next_page["loaded_count"], 1)
            self.assertNotEqual(page["items"][0]["id"], next_page["items"][0]["id"])
            child = next(item for item in records if item.message_id == "nested-quote")
            evidence = storage.get_message_evidence(child.id, account_id="acc", source_database="fixture", conversation_id="room")
            self.assertEqual(evidence["ancestors_count"], 1)
            self.assertEqual(evidence["message"]["sender_corp_name"], "报价公司")
            self.assertEqual(evidence["ancestors"][0]["sender_corp_name"], "转发公司")
            self.assertIsNone(storage.get_message_evidence(child.id, account_id="other", conversation_id="room"))

    def test_company_correction_is_display_overlay_and_price_key_conflict_is_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = WecomLocalStorage(Path(directory) / "analysis.db")
            storage.initialize()
            records, _ = records_from_message_tree([
                {"source_message_id": "m1", "sender_id": "u1", "sender_display": "甲", "sender_corp_id": "old-id", "sender_corp_name": "旧公司",
                 "sent_at": "2026-09-01T09:00:00+08:00", "content": "普通消息"},
                {"source_message_id": "m2", "sender_id": "u2", "sender_display": "乙", "sender_corp_id": "old-id", "sender_corp_name": "旧公司",
                 "sent_at": "2026-09-01T09:01:00+08:00", "content": "另一条消息"},
            ], account_id="acc", source_database="fixture", conversation_id="room", conversation_name="测试群", source_reference="fixture")
            storage.upsert_messages(records)
            prices = PriceMaintenance(storage)
            seed_quote(storage, {"account_id": "acc", "source_database": "fixture", "conversation_id": "room"})
            nested_records, _ = records_from_message_tree([
                {"source_message_id": "forward-root", "sender_id": "forwarder", "sender_display": "转发者", "sender_corp_name": "转发公司",
                 "sent_at": "2026-09-01T09:02:00+08:00", "content_type": "合并转发记录", "content": "[群聊的聊天记录]",
                 "forwarded_messages": [{"source_message_id": "nested-author", "sender_id": "nested-user", "sender_display": "嵌套作者",
                                          "sender_corp_name": "嵌套旧公司", "sent_at": "2026-09-01T09:02:01+08:00", "content": "嵌套内容"}]}
            ], account_id="acc", source_database="fixture", conversation_id="room", conversation_name="测试群", source_reference="nested")
            storage.upsert_messages(nested_records)
            nested_child = next(item for item in nested_records if item.message_id == "nested-author")
            nested_overlay = prices.apply_company_correction(account_id="acc", source_database="fixture", conversation_id="room",
                                                              message_ids=[nested_child.id], sender_ids=[], original_corp_id=None, original_corp_name="嵌套旧公司",
                                                              normalized_company_id="nested-new", normalized_company_name="嵌套规范企业", basis="人工核对嵌套作者",
                                                              actor_id="operator", actor_name="运营审核人")
            self.assertEqual(nested_overlay["status"], "applied")
            nested_visible = storage.list_messages(account_id="acc", source_database="fixture", conversation_id="room", company="嵌套规范企业")
            self.assertEqual(nested_visible["matched_total"], 1)
            prices.submit_candidate({**price_payload(), "source_message_id": "m2", "quote_company_id": "old-id", "quote_company_name": "旧公司"},
                                    source_kind="chat", source_scope={"account_id": "acc", "source_database": "fixture", "conversation_id": "room"},
                                    idempotency_key="old-price", actor_type="system")
            overlay = prices.apply_company_correction(account_id="acc", source_database="fixture", conversation_id="room",
                                                      message_ids=[records[0].id], sender_ids=[], original_corp_id="old-id", original_corp_name="旧公司",
                                                      normalized_company_id="new-id", normalized_company_name="新公司", basis="人工核对企业通讯录",
                                                      actor_id="operator", actor_name="运营审核人")
            self.assertEqual(overlay["status"], "applied")
            visible = storage.list_messages(account_id="acc", source_database="fixture", conversation_id="room", include_text=True)
            corrected = next(item for item in visible["items"] if item["message_id"] == "m1")
            self.assertEqual(corrected["sender_corp_name"], "新公司")
            corrected_search = storage.list_messages(account_id="acc", source_database="fixture", conversation_id="room", company="新公司")
            self.assertEqual(corrected_search["matched_total"], 1)
            with storage.connect() as conn:
                raw = conn.execute("SELECT sender_corp_name FROM messages WHERE message_id='m1'").fetchone()[0]
            self.assertEqual(raw, "旧公司")
            conflict = prices.apply_company_correction(account_id="acc", source_database="fixture", conversation_id="room",
                                                       message_ids=[records[1].id], sender_ids=[], original_corp_id="old-id", original_corp_name="旧公司",
                                                       normalized_company_id="newer-id", normalized_company_name="另一规范公司", basis="待核实",
                                                       actor_id="operator", actor_name="运营审核人")
            self.assertEqual(conflict["status"], "needs_review")

    def test_llm_is_independent_and_citations_cannot_bypass_rule_or_scope(self):
        class FakeAdapter:
            def __init__(self, result):
                self.result = result
                self.calls = 0
            def review(self, payload, evidence):
                self.calls += 1
                return self.result

        with tempfile.TemporaryDirectory() as directory:
            storage = WecomLocalStorage(Path(directory) / "analysis.db")
            storage.initialize()
            scope = {"account_id": "acc", "source_database": "fixture", "conversation_id": "room"}
            seed_quote(storage, scope)
            adapter = FakeAdapter({"status": "approve", "evidence_ids": ["quote-1"], "疑点": []})
            prices = PriceMaintenance(storage, settings=PriceReviewSettings(enabled=True, model="test", model_version="fake-1", adapter=adapter))
            applied = prices.submit_candidate(price_payload(), source_kind="chat", source_scope=scope, idempotency_key="llm-ok", actor_type="system")
            self.assertTrue(applied["applied"])
            self.assertEqual(adapter.calls, 1)
            bad_adapter = FakeAdapter({"status": "approve", "evidence_ids": ["outside"]})
            blocked = PriceMaintenance(storage, settings=PriceReviewSettings(enabled=True, adapter=bad_adapter)).submit_candidate(
                price_payload("630", when="2026-09-02T10:00:00+08:00"), source_kind="chat", source_scope=scope,
                idempotency_key="llm-outside", actor_type="system")
            self.assertFalse(blocked["applied"])
            self.assertEqual(blocked["review_status"], "needs_review")
            no_currency_adapter = FakeAdapter({"status": "approve", "evidence_ids": ["quote-1"]})
            missing = PriceMaintenance(storage, settings=PriceReviewSettings(enabled=True, adapter=no_currency_adapter)).submit_candidate(
                price_payload("640", currency=""), source_kind="chat", source_scope=scope,
                idempotency_key="llm-hard-block", actor_type="system")
            self.assertFalse(missing["applied"])
            self.assertEqual(no_currency_adapter.calls, 0)


if __name__ == "__main__":
    unittest.main()
