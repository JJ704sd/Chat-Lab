"""Contract tests for the additive airfreight local demo."""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from chatlog_assistant.sources.wecom_airfreight import (
    AirfreightOperationError,
    AirfreightService,
    calculate_chargeable_weight,
)
from chatlog_assistant.sources.wecom_storage import WecomLocalStorage
from chatlog_assistant.sources.wecom_parser import WecomUnifiedRecord


class AirfreightDemoTests(unittest.TestCase):
    def make_service(self):
        directory = TemporaryDirectory()
        storage = WecomLocalStorage(Path(directory.name) / "analysis.db")
        storage.initialize()
        # Synthetic chat content is opt-in and only used by deterministic
        # tests.  The normal local page discovers configured real sources.
        return directory, AirfreightService(storage, include_demo_fixtures=True)

    def test_mixed_batch_isolated_rates_evidence_and_failure(self):
        directory, service = self.make_service()
        self.addCleanup(directory.cleanup)
        batch = service.parse_batch(service.ensure_demo_batch()["batch_id"])
        self.assertEqual(batch["total_files"], 8)
        self.assertEqual(batch["processed_files"], 8)
        self.assertEqual(batch["status"], "needs_review")
        self.assertEqual(batch["conflict_count"], 1)
        statuses = {item["file_name"]: item["extraction"]["status"] for item in batch["artifacts"]}
        self.assertEqual(statuses["ET-HK-2026.8.19.pdf"], "parsed")
        self.assertEqual(statuses["ET-HK-2026.8.19.xlsx"], "parsed")
        self.assertEqual(statuses["ET-conditions.docx"], "parsed")
        self.assertEqual(statuses["cargo-multi-ticket.png"], "parsed")
        self.assertEqual(statuses["damaged-optional.pdf"], "rejected")
        artifacts = {item["file_name"]: item for item in batch["artifacts"]}
        self.assertTrue(artifacts["ET-HK-2026.8.19.pdf"]["preview_url"])
        self.assertTrue(artifacts["ET-HK-2026.8.19.pdf"]["download_url"])
        self.assertEqual(artifacts["ET-HK-2026.8.19.pdf"]["source"]["reply_group"], "rates")
        self.assertIsNone(artifacts["damaged-optional.pdf"]["download_url"])
        pdf_bytes, pdf_mime, _ = service.get_artifact_bytes(artifacts["ET-HK-2026.8.19.pdf"]["artifact_id"])
        self.assertEqual(pdf_mime, "application/pdf")
        self.assertGreater(len(pdf_bytes), 4_000)
        self.assertIn(b"ETHIOPIAN AIRLINES RATE SHEET", pdf_bytes)
        xls_bytes, _, _ = service.get_artifact_bytes(artifacts["ET-HK-2026.8.19.xls"]["artifact_id"])
        self.assertIn(b"Excel.Sheet", xls_bytes)
        png = artifacts["cargo-multi-ticket.png"]["extraction"]["result"]
        self.assertEqual(png["pixels"], {"width": 1200, "height": 720})
        et = next(item for item in batch["rate_cards"] if item["airline_code"] == "ET-071")
        self.assertEqual(len(et["rates"]), 35)
        self.assertEqual(et["valid_to"], None)
        self.assertEqual(et["validity_mode"], "until_further_notice")
        self.assertTrue(et["internal_rules"])
        self.assertTrue(et["surcharges"])
        self.assertEqual(et["surcharges"][0]["status"], "inquiry_required")
        self.assertGreaterEqual(len(service.field_evidence(entity_type="route_rate", entity_id=et["rates"][0]["rate_id"])), 1)
        self.assertTrue(any(item["destination_airport"] == "BLR" for item in et["restrictions"]))
        repeated = service.parse_batch(batch["batch_id"])
        self.assertEqual(len(repeated["rate_cards"]), 2)
        self.assertEqual(repeated["conflict_count"], 1)
        self.assertEqual(len(next(item for item in repeated["rate_cards"] if item["airline_code"] == "ET-071")["rates"]), 35)

    def test_slash_is_inquiry_not_zero_and_legacy_doc_degrades(self):
        directory, service = self.make_service()
        self.addCleanup(directory.cleanup)
        slash = service.create_batch(files=[{"file_name": "slash.csv", "data": b"destination,weight_break_label,amount,currency,unit\nXYZ,+45,/,USD,KG\n"}], request_id="slash")
        slash = service.parse_batch(slash["batch_id"])
        rate = slash["rate_cards"][0]["rates"][0]
        self.assertEqual(rate["status"], "inquiry_required")
        self.assertIsNone(rate["amount_text"])
        doc = service.create_batch(files=[{"file_name": "legacy.doc", "data": b"\xd0\xcf\x11\xe0" + b"x" * 50}], request_id="legacy")
        doc = service.parse_batch(doc["batch_id"])
        self.assertEqual(doc["artifacts"][0]["extraction"]["status"], "needs_manual_review")
        self.assertIn("legacy_converter_unavailable", doc["artifacts"][0]["extraction"]["errors"])

    def test_unrecognized_pdf_and_generic_docx_do_not_invent_business_facts(self):
        directory, service = self.make_service()
        self.addCleanup(directory.cleanup)
        docx_buffer = io.BytesIO()
        with zipfile.ZipFile(docx_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body><w:p><w:r><w:t>内部流程说明</w:t></w:r></w:p></w:body></w:document>")
        batch = service.create_batch(files=[
            {"file_name": "unrecognized.pdf", "data": b"%PDF-1.4\n(plain text only) Tj\n"},
            {"file_name": "generic.docx", "data": docx_buffer.getvalue()},
        ], request_id="unrecognized-formats")
        parsed = service.parse_batch(batch["batch_id"])
        artifacts = {item["file_name"]: item for item in parsed["artifacts"]}
        pdf = artifacts["unrecognized.pdf"]["extraction"]
        self.assertEqual(pdf["status"], "needs_manual_review")
        self.assertEqual(pdf["result"]["rate_observations"], [])
        self.assertIn("pdf_table_parser_unavailable", pdf["errors"])
        docx = artifacts["generic.docx"]["extraction"]
        self.assertEqual(docx["status"], "parsed")
        self.assertEqual(docx["result"]["restrictions"], [])
        self.assertEqual(docx["result"]["surcharges"], [])
        self.assertEqual(parsed["rate_cards"], [])

    def test_chat_preview_is_read_only_and_import_is_scope_bound(self):
        directory, service = self.make_service()
        self.addCleanup(directory.cleanup)
        with service.storage.connect() as conn:
            before = conn.execute("SELECT COUNT(*) FROM local_chat_import").fetchone()[0]
        scope = {"account_id": "demo-account-1", "source_snapshot": "demo-snapshot-20260903", "conversation_id": "demo-conversation-cosplay-1", "conversation_name": "中技AI cosplay", "start_at": "2026-09-03T08:00:00+08:00", "end_at": "2026-09-03T18:00:00+08:00"}
        preview = service.preview_chat(scope)
        with service.storage.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM local_chat_import").fetchone()[0], before)
        self.assertEqual(preview["summary"]["referenced_but_unavailable_count"], 1)
        self.assertEqual(len(preview["messages"]), 4)
        self.assertEqual(preview["messages"][0]["text"], "请询 ET：HKG-ADD-BRU 与 TLV，附件是多票货物截图。")
        self.assertTrue(any(message["parent_path"] for message in preview["messages"]))
        self.assertTrue(any(message["attachment_refs"] for message in preview["messages"]))
        imported = service.confirm_chat_import({"preview_id": preview["preview_id"], "preview_digest": preview["preview_digest"], "scope": preview["scope"], "idempotency_key": "chat-once"})
        repeated = service.confirm_chat_import({"preview_id": preview["preview_id"], "preview_digest": preview["preview_digest"], "scope": preview["scope"], "idempotency_key": "chat-once"})
        self.assertEqual(imported["import_id"], repeated["import_id"])
        with self.assertRaises(AirfreightOperationError) as caught:
            service.confirm_chat_import({"preview_id": preview["preview_id"], "preview_digest": preview["preview_digest"], "scope": {**preview["scope"], "conversation_id": "other"}, "idempotency_key": "chat-once"})
        self.assertEqual(caught.exception.error_code, "preview_scope_mismatch")
        evidence = service.chat_evidence(imported["import_id"])
        self.assertTrue(evidence["evidence_policy"]["original_sender_preserved"])
        self.assertEqual(len(evidence["messages"]), 4)
        quotes = service.parse_quotes(imported["import_id"])
        self.assertEqual(len(quotes["items"]), 2)
        self.assertTrue(service.field_evidence(entity_type="package_group", entity_id=quotes["items"][0]["package_groups"][0]["package_group_id"]))

    def test_decimal_weight_formula_and_unknown_semantics(self):
        result = calculate_chargeable_weight([
            {"length_cm": "40", "width_cm": "30", "height_cm": "20", "package_count": "2", "gross_weight_kg": "40", "dimension_semantics": "single_piece"},
            {"length_cm": "100", "width_cm": "80", "height_cm": "60", "package_count": "3", "gross_weight_kg": "120", "dimension_semantics": "group_outer"},
        ])
        self.assertEqual(result["total_volume_weight_kg"], "88")
        self.assertEqual(result["chargeable_weight_kg"], "160")
        blocked = calculate_chargeable_weight([{**{"length_cm": "40", "width_cm": "30", "height_cm": "20", "package_count": "2", "gross_weight_kg": "40"}, "dimension_semantics": "unknown"}])
        self.assertEqual(blocked["status"], "needs_manual_review")

    def test_publish_requires_configured_reviewer_and_keeps_source(self):
        directory, service = self.make_service()
        self.addCleanup(directory.cleanup)
        batch = service.parse_batch(service.ensure_demo_batch()["batch_id"])
        version_id = next(item["version_id"] for item in batch["rate_cards"] if item["airline_code"] == "ET-071")
        with self.assertRaises(AirfreightOperationError) as caught:
            service.publish_rate_card(version_id, actor_id="spoof", actor_name="伪造", reason="", idempotency_key="no-reviewer")
        self.assertEqual(caught.exception.error_code, "reviewer_required")
        service.configure_demo_reviewer()
        for conflict in service.list_conflicts(batch["batch_id"]):
            service.resolve_conflict(conflict["conflict_group_id"], actor_id="demo-reviewer", actor_name="演示审核员", reason="选择 PDF", idempotency_key="resolve-" + conflict["conflict_group_id"])
        result = service.publish_rate_card(version_id, actor_id="demo-reviewer", actor_name="演示审核员", reason="人工审核", idempotency_key="publish-et")
        self.assertTrue(result["published"])
        current = service.list_rate_cards(view="current")["items"]
        self.assertTrue(any(item["version_id"] == version_id for item in current))
        self.assertTrue(service.field_evidence(entity_type="rate_card_version", entity_id=version_id))

    def test_published_sheets_and_per_ticket_customer_quote_pdfs(self):
        directory, service = self.make_service()
        self.addCleanup(directory.cleanup)
        batch = service.parse_batch(service.ensure_demo_batch()["batch_id"])
        service.configure_demo_reviewer()
        for conflict in service.list_conflicts(batch["batch_id"]):
            service.resolve_conflict(
                conflict["conflict_group_id"], actor_id="demo-reviewer", actor_name="演示审核员",
                reason="选择原始候选", idempotency_key="document-resolve-" + conflict["conflict_group_id"],
            )
        boundaries = {
            "boundary_policy": "lower_inclusive_upper_exclusive", "below_45": "manual_inquiry",
            "above_1000": "manual_inquiry", "rounding": "none", "hit_order": ["+45", "+100", "+300", "+500", "+1000"],
        }
        version_ids = []
        for version in service.list_rate_cards(batch_id=batch["batch_id"], view="candidates")["items"]:
            version_ids.append(version["version_id"])
            service.publish_rate_card(
                version["version_id"], actor_id="demo-reviewer", actor_name="演示审核员",
                reason="发布演示价卡", idempotency_key="document-publish-" + version["version_id"],
            )
            service.confirm_weight_rules(
                version["version_id"], boundaries=boundaries, actor_id="demo-reviewer", actor_name="演示审核员",
                reason="确认演示重量档", idempotency_key="document-weight-" + version["version_id"],
            )
        rate_sheet = service.published_rate_card_sheet(version_ids[0])
        self.assertEqual(rate_sheet["document_type"], "published_rate_card")
        self.assertTrue(rate_sheet["source_files"])
        rate_pdf, rate_filename = service.published_rate_card_pdf(version_ids[0])
        self.assertTrue(rate_pdf.startswith(b"%PDF-1.4"))
        self.assertIn(b"PUBLISHED AIRFREIGHT RATE CARD", rate_pdf)
        self.assertTrue(rate_filename.endswith(".pdf"))

        source = service.list_chat_sources()["items"][0]
        scope = {"source_key": source["source_key"], "start_at": "2026-09-03T08:00:00+08:00", "end_at": "2026-09-03T18:00:00+08:00"}
        preview = service.preview_chat(scope)
        imported = service.confirm_chat_import({"preview_id": preview["preview_id"], "preview_digest": preview["preview_digest"], "scope": preview["scope"], "idempotency_key": "document-chat"})
        parsed = service.parse_quotes(imported["import_id"])
        matched_source_names = set()
        for quote in parsed["items"]:
            if quote["missing_fields"]:
                service.correct_quote_field(
                    quote["quote_request_id"], entity_type="quote_request", entity_id=quote["quote_request_id"],
                    field_name="missing_fields_json", corrected_value=[], actor_id="demo-reviewer", actor_name="演示审核员",
                    reason="核验演示字段", idempotency_key="document-correct-" + quote["quote_request_id"],
                )
            matched = service.match_rates(quote["quote_request_id"])
            self.assertEqual(matched["status"], "matched")
            self.assertEqual(matched["quote_key"], quote["quote_key"])
            for option in matched["items"]:
                self.assertTrue(option["source_files"])
                self.assertTrue(option["source_files"][0]["preview_url"])
                self.assertTrue(option["source_files"][0]["download_url"])
                matched_source_names.add(option["source_files"][0]["file_name"])
            service.generate_internal_calculation(quote["quote_request_id"])
            service.confirm_quote(
                quote["quote_request_id"], actor_id="demo-reviewer", actor_name="演示审核员",
                reason="确认 + USD 0.20/KG 销售调整", idempotency_key="document-confirm-" + quote["quote_request_id"],
                sales_adjustment_per_kg="0.20",
            )
        self.assertEqual(matched_source_names, {"ET-HK-2026.8.19.pdf", "alternative-carrier.csv"})
        quote_preview = service.quote_preview(import_id=imported["import_id"])
        self.assertEqual(quote_preview["total_documents"], 2)
        self.assertEqual(quote_preview["total"], 4)
        self.assertNotIn("base_rate", json.dumps(quote_preview, ensure_ascii=False))
        for document in quote_preview["documents"]:
            self.assertEqual(document["customer"], "Anonymous Customer")
            self.assertEqual(document["salutation"], "Dear Customer:")
            self.assertEqual(document["incoterm"], "To be confirmed")
            self.assertEqual(len(document["options"]), 2)
            options_by_airline = {item["airline_code"]: item for item in document["options"]}
            self.assertIn("-ADD-", options_by_airline["ET-071"]["routing"])
            self.assertEqual(options_by_airline["ET-071"]["frequency"], "D1/2/4/5/6/7")
            self.assertIn("-DOH-", options_by_airline["QR"]["routing"])
            self.assertEqual(options_by_airline["QR"]["frequency"], "To be confirmed")
            self.assertEqual(options_by_airline["QR"]["t_t"], "To be confirmed")
            quote_pdf, quote_filename = service.quote_pdf(document["quote_request_id"])
            self.assertTrue(quote_pdf.startswith(b"%PDF-1.4"))
            self.assertIn(b"AIR FREIGHT QUOTATION", quote_pdf)
            self.assertIn(b"Dear Customer:", quote_pdf)
            self.assertTrue(quote_filename.endswith(".pdf"))

    def test_demo_state_machine_separates_execute_continue_versions_and_flows(self):
        directory, service = self.make_service()
        self.addCleanup(directory.cleanup)
        initial = service.demo_state("A")["flows"]["A"]
        self.assertEqual(initial["current_step"], 1)
        self.assertEqual(initial["status"], "ready")
        executed = service.transition_demo(
            "A", "execute", state_version=initial["state_version"], payload={"step": 1},
            idempotency_key="state-a1",
        )
        run = executed["state"]
        self.assertEqual(run["current_step"], 1)
        self.assertEqual(run["last_completed_step"], 1)
        self.assertEqual(run["status"], "succeeded")
        # A network retry returns the prior result rather than repeating the
        # simulated operation or rejecting only because the version advanced.
        repeated = service.transition_demo(
            "A", "execute", state_version=initial["state_version"], payload={"step": 1},
            idempotency_key="state-a1",
        )
        self.assertTrue(repeated["idempotent"])
        with self.assertRaises(AirfreightOperationError) as stale:
            service.transition_demo("A", "continue", state_version=initial["state_version"], idempotency_key="stale")
        self.assertEqual(stale.exception.error_code, "stale_state_version")
        continued = service.transition_demo("A", "continue", state_version=run["state_version"], idempotency_key="continue-a1")
        self.assertEqual(continued["state"]["current_step"], 2)
        self.assertEqual(continued["state"]["status"], "ready")
        with self.assertRaises(AirfreightOperationError) as future:
            service.transition_demo("A", "execute", state_version=continued["state"]["state_version"], payload={"step": 3}, idempotency_key="future-a3")
        self.assertEqual(future.exception.error_code, "out_of_order_step")
        self.assertEqual(service.demo_state("B")["flows"]["B"]["last_completed_step"], 0)

    def test_mode_switch_keeps_manual_decision_guidance(self):
        directory, service = self.make_service()
        self.addCleanup(directory.cleanup)

        def transition(action, payload=None):
            run = service.demo_state("A")["flows"]["A"]
            return service.transition_demo(
                "A", action, state_version=run["state_version"], payload=payload or {},
                idempotency_key=f"keep-guidance-{action}-{run['current_step']}-{run['state_version']}",
            )["state"]

        for step in range(1, 6):
            transition("execute", {"step": step})
            transition("continue")
        blocked = transition("execute", {"step": 6})
        self.assertEqual(blocked["status"], "needs_manual_decision")
        self.assertTrue(blocked["blocked_reason"]["reason"])
        self.assertTrue(blocked["blocked_reason"]["next_action"])

        switched = transition("set_mode", {"mode": "auto"})
        self.assertEqual(switched["status"], "needs_manual_decision")
        self.assertEqual(switched["blocked_reason"], blocked["blocked_reason"])

    def test_demo_outcome_uses_extraction_runs_not_artifact_columns(self):
        directory, service = self.make_service()
        self.addCleanup(directory.cleanup)
        batch = service.parse_batch(service.ensure_demo_batch()["batch_id"])

        def transition(action):
            run = service.demo_state("A")["flows"]["A"]
            return service.transition_demo(
                "A", action, state_version=run["state_version"],
                payload={"step": run["current_step"]} if action == "execute" else {},
                idempotency_key=f"outcome-{action}-{run['current_step']}-{run['state_version']}",
            )["state"]

        transition("execute")
        transition("continue")
        transition("execute")
        transition("continue")
        transition("execute")
        outcome = service.demo_state("A")["flows"]["A"]["outcome"]
        self.assertEqual(outcome["batch_id"], batch["batch_id"])
        self.assertEqual(outcome["processed_files"], 8)
        self.assertEqual(outcome["failed_files"], 1)

    def test_demo_import_event_keeps_chat_body_out_of_run_state(self):
        directory, service = self.make_service()
        self.addCleanup(directory.cleanup)

        def transition(action, payload=None):
            run = service.demo_state("B")["flows"]["B"]
            return service.transition_demo(
                "B", action, state_version=run["state_version"], payload=payload or {},
                idempotency_key=f"body-free-{action}-{run['current_step']}-{run['state_version']}",
            )["state"]

        transition("execute", {"step": 1})
        transition("continue")
        source = service.list_chat_sources()["items"][0]
        scope = {"source_key": source["source_key"], "start_at": "2026-09-03T08:00:00+08:00", "end_at": "2026-09-03T18:00:00+08:00"}
        transition("execute", {"step": 2, "scope": scope})
        transition("continue")
        transition("execute", {"step": 3})
        transition("continue")
        imported = transition("execute", {"step": 4, "confirm": True})
        serialized = json.dumps(imported["result"], ensure_ascii=False)
        self.assertIn("import_id", imported["result"])
        self.assertNotIn("demo-msg-001", serialized)
        self.assertNotIn("请询 ET", serialized)

    def test_flow_b_missing_published_card_has_actionable_guidance(self):
        """A presenter can reach B9 without having run Flow A first."""
        directory, service = self.make_service()
        self.addCleanup(directory.cleanup)
        service.configure_demo_reviewer()

        def transition(action, payload=None):
            run = service.demo_state("B")["flows"]["B"]
            return service.transition_demo(
                "B", action, state_version=run["state_version"], payload=payload or {},
                idempotency_key=f"b9-card-guidance-{action}-{run['current_step']}-{run['state_version']}",
            )["state"]

        transition("execute", {"step": 1})
        transition("continue")
        source = service.list_chat_sources()["items"][0]
        scope = {
            "source_key": source["source_key"],
            "start_at": "2026-09-03T08:00:00+08:00",
            "end_at": "2026-09-03T18:00:00+08:00",
        }
        transition("execute", {"step": 2, "scope": scope})
        for step, payload in ((3, {}), (4, {"confirm": True}), (5, {}), (6, {})):
            transition("continue")
            transition("execute", {"step": step, **payload})
        import_id = service.demo_state("B")["flows"]["B"]["result"]["import_id"]
        review = transition("continue")
        self.assertEqual(review["current_step"], 7)
        blocked = transition("execute", {"step": 7})
        self.assertEqual(blocked["status"], "needs_manual_decision")
        for quote in service.list_quotes(import_id=import_id)["items"]:
            if quote["status"] == "needs_manual_review" or quote["missing_fields"]:
                service.correct_quote_field(
                    quote["quote_request_id"], entity_type="quote_request", entity_id=quote["quote_request_id"],
                    field_name="missing_fields_json", corrected_value=[], actor_id="demo-reviewer",
                    actor_name="演示审核员", reason="演示人工核验", idempotency_key="b9-correct-" + quote["quote_request_id"],
                )
        transition("retry", {"step": 7})
        transition("continue")
        transition("execute", {"step": 8})
        transition("continue")
        matching = transition("execute", {"step": 9})

        self.assertEqual(matching["status"], "needs_manual_decision")
        self.assertEqual(matching["blocked_reason"]["code"], "no_published_rate_card")
        self.assertIn("流程 A", matching["blocked_reason"]["next_action"])

    def test_explicit_synthetic_demo_can_continue_after_real_completeness_block(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        demo_storage = WecomLocalStorage(root / "demo.db")
        source_storage = WecomLocalStorage(root / "source.db")
        demo_storage.initialize()
        source_storage.initialize()
        cn_tz = timezone(timedelta(hours=8))
        # This fake normalized source is deliberately incomplete: it lets the
        # test prove that B5 first keeps the real boundary, then switches only
        # after the presenter's explicit synthetic-demo action.
        source_storage.upsert_messages([WecomUnifiedRecord(
            source="test", account_id="real-account", source_database="snapshot.db",
            message_id="real-root", server_id="", conversation_id="real-conversation",
            conversation_name="中技AI cosplay", sender_id="operator", sender_name="操作员",
            sender_corp_id=None, sender_corp_name=None, subject_bucket="zhongji",
            subject_basis="explicit", message_type="text",
            sent_at=datetime(2026, 9, 3, 10, 0, tzinfo=cn_tz), text="群聊的聊天记录",
            reply_to_message_id=None, parse_status="partial_forward", source_reference="test",
            provenance={"gaps": [{"reason": "fixture partial forward"}]},
        )])
        service = AirfreightService(
            demo_storage, source_db_paths=[source_storage.path], include_demo_fixtures=True,
        )

        def transition(action, payload=None):
            run = service.demo_state("B")["flows"]["B"]
            return service.transition_demo(
                "B", action, state_version=run["state_version"], payload=payload or {},
                idempotency_key=f"synthetic-fallback-{action}-{run['current_step']}-{run['state_version']}",
            )["state"]

        transition("execute", {"step": 1})
        transition("continue")
        real_source = next(item for item in service.list_chat_sources()["items"] if not item["fixture"])
        scope = {
            "source_key": real_source["source_key"],
            "start_at": "2026-09-03T00:00:00+08:00",
            "end_at": "2026-09-03T23:59:00+08:00",
        }
        transition("execute", {"step": 2, "scope": scope})
        transition("continue")
        transition("execute", {"step": 3})
        transition("continue")
        real_import = transition("execute", {"step": 4, "confirm": True})
        transition("continue")
        blocked = transition("execute", {"step": 5})
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(blocked["blocked_reason"]["synthetic_demo_available"])
        self.assertEqual(blocked["result"]["import_id"], real_import["result"]["import_id"])

        switched = transition("retry", {"step": 5, "use_synthetic_demo": True})
        self.assertEqual(switched["status"], "succeeded")
        self.assertEqual(switched["result"]["demo_data_mode"], "synthetic")
        self.assertNotEqual(switched["result"]["import_id"], real_import["result"]["import_id"])
        self.assertTrue(switched["result"]["preview_summary"]["fixture"])
        self.assertEqual(switched["result"]["preview_summary"]["default_target_root_status"], "synthetic_demo")
        self.assertNotIn("群聊的聊天记录", json.dumps(switched["result"], ensure_ascii=False))
        outcome = service.demo_state("B")["flows"]["B"]["outcome"]
        self.assertEqual(outcome["source_data_mode"], "synthetic_demo")
        self.assertIn("合成演示数据", outcome["source_disclaimer"])

        transition("continue")
        parsed = transition("execute", {"step": 6})
        self.assertEqual(parsed["status"], "succeeded")
        self.assertEqual(len(parsed["result"]["quotes"]["items"]), 2)

    def test_real_source_discovery_does_not_expose_synthetic_fixture_by_default(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        storage = WecomLocalStorage(Path(directory.name) / "analysis.db")
        storage.initialize()
        service = AirfreightService(storage)
        sources = service.list_chat_sources()
        self.assertEqual(sources["items"], [])
        self.assertEqual(sources["default_target"]["status"], "not_found")


if __name__ == "__main__":
    unittest.main()
