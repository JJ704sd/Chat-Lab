from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
import json

from chatlog_assistant.sources.wecom_classifier import extract_business_clues
from chatlog_assistant.sources.wecom_parser import WecomUnifiedRecord
from chatlog_assistant.sources.wecom_storage import WecomLocalStorage
from chatlog_assistant.sources.wecom_semantic import WecomSemanticAnalyzer, WecomSemanticSettings
from chatlog_assistant.sources.wecom_exporter import export_issues_to_json, export_issues_to_csv


GROUP = "中技AI cosplay"
QUESTION = "40ctns/320kgs/1.77cbm\n提货地址：北京市通州区某工业园18栋\n送到上海浦东机场，麻烦请报价，并告知时效"


def record(key, seconds, text, sender="customer", **changes):
    item = WecomUnifiedRecord(
        source="wecom-local", account_id="test", source_database="fixture",
        message_id=key, server_id="server-" + key, conversation_id="target",
        conversation_name=GROUP, sender_id=sender, sender_name=sender,
        sender_corp_id=None, sender_corp_name=None, subject_bucket="unknown",
        subject_basis="unknown", message_type="文本",
        sent_at=datetime.fromisoformat("2026-08-31T09:02:23+08:00") + timedelta(seconds=seconds),
        text=text, reply_to_message_id=None, parse_status="parsed", source_reference="fixture:" + key,
    )
    return replace(item, **changes)


class TargetAnalysisTests(unittest.TestCase):
    def test_verified_report_excludes_manual_namesake_and_masks_nested_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            storage=WecomLocalStorage(Path(directory)/'test.db')
            storage.initialize()
            raw='请报价，电话13812345678；密码：SyntheticOnly123'
            storage.upsert_messages([record('real',0,raw,provenance={'origin':'database','quoted_identity':{'text':raw}}),
                                     record('manual',0,'请报价',account_id='manual')])
            storage.rebuild_analysis()
            report=storage.get_report(conversation_name='cosplay')
            self.assertEqual(report['coverage']['message_count'],1)
            self.assertEqual(report['coverage']['excluded_unverified_source_count'],1)
            serialized=json.dumps(report,ensure_ascii=False)
            self.assertNotIn('SyntheticOnly123',serialized)
            self.assertNotIn('13812345678',serialized)
            self.assertEqual(storage.get_report(account_id='manual')['coverage']['message_count'],1)
            with storage.connect() as conn:
                self.assertEqual(conn.execute("SELECT text FROM messages WHERE message_id='real'").fetchone()[0],raw)

    def test_extract_screenshot_specs_without_inventing_currency(self):
        clues = extract_business_clues(QUESTION).as_dict()
        self.assertEqual(clues["pickup_address"], "北京市通州区某工业园18栋")
        self.assertEqual(clues["destination"], "上海浦东机场")
        pallet = extract_business_clues("1PLT/772KGS/0.72CBM,800*1200*800mm").as_dict()
        self.assertEqual(pallet["packages"], "1PLT")
        self.assertEqual(pallet["dimensions"], "800*1200*800mm")
        quote = extract_business_clues("@客户 上海620隔日达").as_dict()
        self.assertEqual(quote["price_quote"], "620")
        self.assertEqual(quote["delivery_time"], "隔日达")
        self.assertIsNone(quote["currency"])

    def test_scoped_rebuild_links_quotes_and_preserves_other_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = WecomLocalStorage(Path(directory) / "analysis.db")
            storage.initialize()
            storage.upsert_messages([
                record("q1", 0, QUESTION), record("ack", 7, "马上", "vendor"),
                record("q2", 15, "1PLT/772KGS/0.72CBM,800*1200*800mm\n送到北京首都机场，麻烦报价", "customer2"),
                record("r1", 38, '“客户：\n' + QUESTION + '”\n@客户 上海620隔日达', "vendor"),
                record("other", 0, "请报价", conversation_id="other", conversation_name="其他群"),
                record("cross", 40, "报价100元", "outsider", account_id="other-account"),
            ])
            storage.rebuild_analysis()
            before = storage.list_issues(conversation_id="other")
            storage.rebuild_analysis(account_id="test", conversation_name=GROUP)
            self.assertEqual(before, storage.list_issues(conversation_id="other"))
            items = storage.list_issues(account_id="test", conversation_name=GROUP, category="询价报价")
            self.assertEqual(len(items), 2)
            by_text = {item["question_raw_text"]: item for item in items}
            first = by_text[QUESTION]
            self.assertEqual(first["issue_status"], "solved")
            self.assertEqual(first["association_basis"], "quoted_text_match")
            self.assertEqual(first["first_response_seconds"], 7)
            self.assertEqual(first["solution_seconds"], 38)
            self.assertEqual(first["response_clues"]["price_quote"], "620")
            second = next(item for item in items if item["question_raw_text"] != QUESTION)
            self.assertEqual(second["issue_status"], "unreplied")

    def test_context_llm_grounds_entities_and_rejects_foreign_ids(self):
        class Client:
            def complete(self, system, user):
                self.system, self.payload = system, json.loads(user)
                return json.dumps({"items": [
                    {"id": "invented", "is_issue": True, "categories": ["询价报价"], "confidence": .9},
                    {"id": target.id, "is_issue": True, "categories": ["其他待分类问题"], "confidence": .9,
                     "summary": "催促处理", "entities": {"weight": "900kg", "delivery_time": "三天"},
                     "reply_to_id": "outside", "urgency_level": 3, "urgency_evidence": "等三天",
                     "risk_evaluation": {"has_complaint_risk": True, "risk_reason": "我要投诉"}},
                ]})
        target = record("implicit", 30, "这票等三天了，一直没动静")
        client = Client()
        analyzer = WecomSemanticAnalyzer(WecomSemanticSettings(api_key="test"), client)
        result = analyzer.analyze_context([record("q", 0, QUESTION).as_dict(), target.as_dict()], [target.id])
        self.assertEqual(set(result), {target.id})
        self.assertNotIn("weight", result[target.id]["clues"])
        self.assertEqual(result[target.id]["clues"]["delivery_time"], "三天")
        self.assertIsNone(result[target.id]["semantic_reply_to"])
        self.assertFalse(result[target.id]["risk_evaluation"]["has_complaint_risk"])
        self.assertEqual(len(client.payload["context"]), 2)
        self.assertIn("不可信", client.system)

    def test_forwarded_import_keeps_original_identity_time_and_provenance(self):
        from chatlog_assistant.sources.wecom_pipeline import import_normalized_jsonl
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source = path / "chat.jsonl"
            source.write_text(json.dumps({
                "source_message_id": "forward-1", "conversation_id": "target", "conversation_name": GROUP,
                "sender_display": "转发者 @其他", "sent_at": "2026-09-02T10:00:00+08:00",
                "forwarded_messages": [{"source_message_id": "original-1", "sender_id": "customer",
                    "sender_display": "客户 @中技物流", "sent_at": "2026-08-31T09:02:23+08:00", "content": QUESTION}],
            }, ensure_ascii=False), encoding="utf-8")
            db = path / "test.db"
            for _ in range(2):
                import_normalized_jsonl(source, account_id="test", analysis_db_path=db, conversation_name=GROUP)
            storage = WecomLocalStorage(db)
            issues = storage.list_issues(category="询价报价")
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["question_subject_bucket"], "zhongji")
            self.assertEqual(issues[0]["question_sent_at"], "2026-08-31T09:02:23+08:00")
            self.assertIn("forward-1", issues[0]["source_reference"])
            self.assertIn("forwarded", issues[0]["conversation_id"])
            child_report = storage.get_report(conversation_id=issues[0]['conversation_id'])
            self.assertEqual(child_report['coverage']['message_count'],1)
            self.assertEqual(len(child_report['ancestors']),1)
            self.assertEqual(child_report['messages'][0]['parent_id'],child_report['ancestors'][0]['id'])

    def test_explicit_reply_after_a_day_and_ambiguous_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = WecomLocalStorage(Path(directory) / "test.db")
            storage.initialize()
            storage.upsert_messages([
                record("q1", 0, "去上海，麻烦报价"),
                record("q2", 10, "去北京，麻烦报价", "customer2"),
                record("ambiguous", 20, "报价620元", "vendor"),
                record("explicit", 86400, "报价800元", "vendor", reply_to_message_id="server-q2"),
            ])
            storage.rebuild_analysis()
            items = storage.list_issues(category="询价报价")
            first = next(item for item in items if "上海" in item["question_raw_text"])
            second = next(item for item in items if "北京" in item["question_raw_text"])
            self.assertEqual(first["issue_status"], "unreplied")
            self.assertIn("人工核对", first["status_reason"])
            self.assertEqual(second["issue_status"], "solved")
            self.assertEqual(second["solution_seconds"], 86390)

    def test_invalid_llm_falls_back_and_exports_mask_nested_fields(self):
        class Client:
            def complete(self, system, user):
                return '{"items":[{"id":"wrong","confidence":"high","is_issue":"yes"}]}'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            storage = WecomLocalStorage(path / "test.db")
            storage.initialize()
            storage.upsert_messages([record("q", 0, "请帮忙处理一下，电话13812345678")])
            analyzer = WecomSemanticAnalyzer(WecomSemanticSettings(api_key="test"), Client())
            result = storage.rebuild_analysis(analyzer)
            self.assertEqual(result["semantic_errors"], 1)
            issues = storage.list_issues()
            self.assertEqual(len(issues), 1)
            for exporter, suffix in ((export_issues_to_csv, "csv"), (export_issues_to_json, "json")):
                exported = exporter(issues, path / ("export." + suffix), anonymize=True).read_text(encoding="utf-8-sig")
                self.assertNotIn("13812345678", exported)
                self.assertIn("138****5678", exported)

    def test_llm_response_entities_reach_query_results(self):
        question = record("q", 0, "去上海，麻烦报价")
        reply = record("r", 20, "这票一千整", "vendor")
        class Client:
            def complete(self, system, user):
                return json.dumps({"items": [{"id": reply.id, "is_issue": False, "categories": [], "confidence": .92,
                    "entities": {"price_quote": "一千整"}, "reply_to_id": question.id,
                    "response_assessment": {"kind": "报价方案", "is_solution": True, "evidence": "这票一千整"}}]})
        with tempfile.TemporaryDirectory() as directory:
            storage = WecomLocalStorage(Path(directory) / "test.db")
            storage.initialize()
            storage.upsert_messages([question, reply])
            analyzer = WecomSemanticAnalyzer(WecomSemanticSettings(api_key="test"), Client())
            storage.rebuild_analysis(analyzer)
            item = storage.list_issues(category="询价报价")[0]
            self.assertEqual(item["response_clues"]["price_quote"], "一千整")
            self.assertEqual(item["association_basis"], "semantic_context")
            self.assertEqual(item["issue_status"], "solved")

    def test_unrelated_action_is_not_a_logistics_solution(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = WecomLocalStorage(Path(directory) / "test.db")
            storage.initialize()
            storage.upsert_messages([record("q", 0, "麻烦安排提货"), record("r", 10, "已安排明天的会议", "other")])
            storage.rebuild_analysis()
            self.assertNotEqual(storage.list_issues()[0]["issue_status"], "solved")


if __name__ == "__main__":
    unittest.main()
