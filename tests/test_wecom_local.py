import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from chatlog_assistant.sources.wecom_snapshot import (
    analyze_wal,
    capture_consistent_snapshot,
)
from chatlog_assistant.sources.wecom_decrypter import (
    decrypt_and_verify_snapshot,
    sanitize_wal_header_for_deserialize,
)
from chatlog_assistant.sources.wecom_subject import (
    WecomSubjectClassifier,
    SubjectClassification,
)
from chatlog_assistant.sources.wecom_protobuf import (
    parse_wecom_content,
    extract_text_from_pb,
)
from chatlog_assistant.sources.wecom_classifier import (
    classify_logistics_issue,
    assess_logistics_response,
    extract_business_clues,
)
from chatlog_assistant.sources.wecom_storage import WecomLocalStorage
from chatlog_assistant.sources.wecom_parser import WecomUnifiedRecord, WecomLocalParser
from chatlog_assistant.sources.wecom_exporter import (
    export_issues_to_csv,
    export_issues_to_json,
    mask_sensitive_text,
)
from chatlog_assistant.sources.wecom_pipeline import (
    import_decrypted_directory,
    discover_wecom_accounts,
)
from chatlog_assistant.sources.wecom_semantic import (
    WecomSemanticAnalyzer,
    WecomSemanticSettings,
    WecomMiniMaxClient,
    parse_json_object,
)


class FakeWecomMiniMaxClient:
    def complete(self, system: str, user: str) -> str:
        payload = json.loads(user.split("\n", 1)[1])
        if "请评估下列回复" in user:
            items = []
            for row in payload:
                t = row["text"]
                if "28 CNY" in t or "已安排" in t or "报价" in t:
                    items.append({
                        "id": row["id"],
                        "kind": "处理方案",
                        "is_solution": True,
                        "status_contribution": "solved",
                        "solution_text": t[:40],
                        "confidence": 0.95,
                    })
                elif "收到" in t or "好的" in t:
                    items.append({
                        "id": row["id"],
                        "kind": "即时响应",
                        "is_solution": False,
                        "status_contribution": "acknowledged",
                        "solution_text": None,
                        "confidence": 0.98,
                    })
                else:
                    items.append({
                        "id": row["id"],
                        "kind": "一般回复",
                        "is_solution": False,
                        "status_contribution": "none",
                        "solution_text": None,
                        "confidence": 0.60,
                    })
            return json.dumps({"items": items}, ensure_ascii=False)

        # Issue classification
        items = []
        for row in payload:
            t = row["text"]
            if "铝锭" in t or "多少钱" in t:
                items.append({
                    "id": row["id"],
                    "is_issue": True,
                    "categories": ["询价报价"],
                    "confidence": 0.95,
                    "summary": "咨询铝锭空运运费",
                })
            elif "违禁品" in t:
                items.append({
                    "id": row["id"],
                    "is_issue": True,
                    "categories": ["单证报关"],
                    "confidence": 0.92,
                    "summary": "咨询违禁品寄递限制",
                })
            else:
                items.append({
                    "id": row["id"],
                    "is_issue": False,
                    "categories": [],
                    "confidence": 0.5,
                    "summary": "",
                })
        return json.dumps({"items": items}, ensure_ascii=False)


class TestWecomLocal(unittest.TestCase):
    def test_subject_classification_rules(self):
        classifier = WecomSubjectClassifier()

        # Rule 1: corp_name contains 中技
        res = classifier.classify(sender_display="张三", sender_corp_name="深圳市中技物流有限公司")
        self.assertEqual(res.subject_bucket, "zhongji")
        self.assertEqual(res.basis, "corp_name_match")

        # Rule 2: corp_name is another company
        res = classifier.classify(sender_display="李四", sender_corp_name="嘉航盛物流")
        self.assertEqual(res.subject_bucket, "other")
        self.assertEqual(res.basis, "corp_name_match")

        # Rule 3: Display suffix @中技物流
        res = classifier.classify(sender_display="王五 @中技物流")
        self.assertEqual(res.subject_bucket, "zhongji")
        self.assertEqual(res.basis, "sender_display_suffix")

        # Rule 4: Display suffix @顺丰速运
        res = classifier.classify(sender_display="赵六 @顺丰速运")
        self.assertEqual(res.subject_bucket, "other")
        self.assertEqual(res.basis, "sender_display_suffix")

        # Rule 5: Message text mentions 中技, but sender has no corp info -> unknown (NEVER classify from message text)
        res = classifier.classify(sender_display="小七")
        self.assertEqual(res.subject_bucket, "unknown")

    def test_classifier_acknowledgment_and_solution(self):
        # '收到' must NOT be classified as solved
        res_ack1 = assess_logistics_response("收到")
        self.assertEqual(res_ack1.kind, "即时响应")
        self.assertFalse(res_ack1.is_solution)
        self.assertEqual(res_ack1.status_contribution, "acknowledged")

        res_ack2 = assess_logistics_response("好的，马上处理")
        self.assertFalse(res_ack2.is_solution)

        # Price quote is a solution
        res_sol1 = assess_logistics_response("上海隔日达，预计报价620元")
        self.assertEqual(res_sol1.kind, "报价方案")
        self.assertTrue(res_sol1.is_solution)
        self.assertEqual(res_sol1.status_contribution, "solved")

        # Solution text
        res_sol2 = assess_logistics_response("已安排司机上门提货，车牌号粤B12345")
        self.assertEqual(res_sol2.kind, "处理方案")
        self.assertTrue(res_sol2.is_solution)
        self.assertEqual(res_sol2.status_contribution, "solved")

    def test_business_clues_extraction(self):
        text = "40ctns/320kgs/1.77cbm 从深圳到上海，单号 SF1234567890 麻烦请报价"
        clues = extract_business_clues(text)
        self.assertEqual(clues.packages, "40ctns")
        self.assertEqual(clues.weight, "320kgs")
        self.assertEqual(clues.volume, "1.77cbm")
        self.assertEqual(clues.origin, "深圳")
        self.assertEqual(clues.destination, "上海")
        self.assertEqual(clues.waybill_no, "SF1234567890")

    def test_mask_sensitive_text(self):
        raw = "联系电话13812345678，身份证440301199001011234，银行卡6222021234567890123"
        masked = mask_sensitive_text(raw)
        self.assertIn("138****5678", masked)
        self.assertIn("440301********1234", masked)
        self.assertIn("6222****0123", masked)

    def test_storage_multi_account_isolation_and_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "analysis.db"
            storage = WecomLocalStorage(db_path)
            storage.initialize()

            now = datetime.now(timezone.utc)
            # Account 1 msg 1
            r1 = WecomUnifiedRecord(
                source="wecom-local",
                account_id="acc_001",
                source_database="message.db",
                message_id="msg_100",
                server_id="srv_100",
                conversation_id="conv_1",
                conversation_name="测试群",
                sender_id="u1",
                sender_name="张三 @中技物流",
                sender_corp_id="corp_1",
                sender_corp_name="中技物流",
                subject_bucket="zhongji",
                subject_basis="corp_name_match",
                message_type="文本",
                sent_at=now,
                text="请问去上海的运费是多少？麻烦报价",
                reply_to_message_id=None,
                parse_status="parsed",
                source_reference="message_table:msg_100",
            )

            # Account 2 msg 1 (same message_id and db name, different account)
            r2 = WecomUnifiedRecord(
                source="wecom-local",
                account_id="acc_002",
                source_database="message.db",
                message_id="msg_100",
                server_id="srv_100",
                conversation_id="conv_2",
                conversation_name="不同账号群",
                sender_id="u2",
                sender_name="李四 @客户企业",
                sender_corp_id="corp_2",
                sender_corp_name="客户企业",
                subject_bucket="other",
                subject_basis="corp_name_match",
                message_type="文本",
                sent_at=now,
                text="请问报关资料好了吗？",
                reply_to_message_id=None,
                parse_status="parsed",
                source_reference="message_table:msg_100",
            )

            # Insert both
            storage.upsert_messages([r1, r2])

            with storage.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                self.assertEqual(count, 2, "Both messages from different accounts must exist without collision")

            # Re-insert same records -> deduplicated
            storage.upsert_messages([r1, r2])
            with storage.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                self.assertEqual(count, 2, "Re-inserting same records must not duplicate")

    def test_wecom_semantic_analysis_with_fake_minimax(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            settings = WecomSemanticSettings(api_key="fake-test-key")
            analyzer = WecomSemanticAnalyzer(settings=settings, client=FakeWecomMiniMaxClient())
            self.assertTrue(analyzer.enabled)

            # Test batch issue classification
            issues_res = analyzer.classify_issues_batch([
                ("msg_1", "我要寄一批一吨的铝锭，多少钱？"),
                ("msg_2", "违禁品有哪些"),
                ("msg_3", "今天天气不错"),
            ])
            self.assertIn("msg_1", issues_res)
            self.assertEqual(issues_res["msg_1"][0][0].category, "询价报价")
            self.assertIn("铝锭", issues_res["msg_1"][1])

            self.assertIn("msg_2", issues_res)
            self.assertEqual(issues_res["msg_2"][0][0].category, "单证报关")

            self.assertIn("msg_3", issues_res)
            self.assertEqual(len(issues_res["msg_3"][0]), 0)

            # Test batch response assessment
            resp_res = analyzer.assess_responses_batch([
                ("resp_1", "好的收到，马上处理"),
                ("resp_2", "服务类型：空卡运输，单价：28 CNY/kg，预估总价：28,000 CNY"),
            ])
            self.assertEqual(resp_res["resp_1"].kind, "即时响应")
            self.assertFalse(resp_res["resp_1"].is_solution)
            self.assertEqual(resp_res["resp_1"].status_contribution, "acknowledged")

            self.assertEqual(resp_res["resp_2"].kind, "处理方案")
            self.assertTrue(resp_res["resp_2"].is_solution)
            self.assertEqual(resp_res["resp_2"].status_contribution, "solved")

            # Test storage rebuild with semantic analyzer
            db_path = tmp_path / "analysis.db"
            storage = WecomLocalStorage(db_path)
            storage.initialize()

            now = datetime.now(timezone.utc)
            r1 = WecomUnifiedRecord(
                source="wecom-local",
                account_id="acc1",
                source_database="message.db",
                message_id="m10",
                server_id="s10",
                conversation_id="conv_ai",
                conversation_name="AI群",
                sender_id="u1",
                sender_name="客户A",
                sender_corp_id=None,
                sender_corp_name=None,
                subject_bucket="other",
                subject_basis="default",
                message_type="文本",
                sent_at=now,
                text="我要寄一批一吨的铝锭，多少钱？",
                reply_to_message_id=None,
                parse_status="parsed",
                source_reference="message_table:10",
            )
            r2 = WecomUnifiedRecord(
                source="wecom-local",
                account_id="acc1",
                source_database="message.db",
                message_id="m11",
                server_id="s11",
                conversation_id="conv_ai",
                conversation_name="AI群",
                sender_id="bot1",
                sender_name="BY-LinkBot",
                sender_corp_id="corp_1",
                sender_corp_name="中技物流",
                subject_bucket="zhongji",
                subject_basis="corp_name_match",
                message_type="文本",
                sent_at=now + timedelta(seconds=10),
                text="单价：28 CNY/kg，预估总价：28,000 CNY",
                reply_to_message_id=None,
                parse_status="parsed",
                source_reference="message_table:11",
            )
            storage.upsert_messages([r1, r2])
            res = storage.rebuild_analysis(semantic_analyzer=analyzer)
            self.assertEqual(res["issues"], 1)

            issue_items = storage.list_issues()
            self.assertEqual(len(issue_items), 1)
            self.assertEqual(issue_items[0]["category"], "询价报价")
            self.assertEqual(issue_items[0]["issue_status"], "solved")
            self.assertIn("28 CNY", issue_items[0]["responder_raw_text"] or "")


if __name__ == "__main__":
    unittest.main()
