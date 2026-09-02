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

    def test_zhongji_ai_cosplay_end_to_end_timeline(self):
        """Simulate and verify the exact message forms and interactions seen in the '中技AI cosplay' group chat."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "analysis.db"
            storage = WecomLocalStorage(db_path)
            storage.initialize()

            classifier = WecomSubjectClassifier()
            # 1. Verify subject classifications
            s_qiu = classifier.classify(sender_display="邱一平 @ 中技物流")
            self.assertEqual(s_qiu.subject_bucket, "zhongji")
            self.assertEqual(s_qiu.raw_subject, "中技物流")

            s_jia = classifier.classify(sender_display="嘉航盛商务-小曾 @ 嘉航盛物流")
            self.assertEqual(s_jia.subject_bucket, "other")
            self.assertEqual(s_jia.raw_subject, "嘉航盛物流")

            s_qiu_li = classifier.classify(sender_display="丘丽兰 @ 嘉航盛物流")
            self.assertEqual(s_qiu_li.subject_bucket, "other")

            s_guo = classifier.classify(sender_display="郭立城@百运网")
            self.assertEqual(s_guo.subject_bucket, "other")
            self.assertEqual(s_guo.raw_subject, "百运网")

            s_lin = classifier.classify(sender_display="林华玲☆2025年度销售精英☆")
            self.assertEqual(s_lin.subject_bucket, "unknown")

            # 2. Build the exact chat sequence
            t0 = datetime(2026, 8, 31, 10, 12, 0, tzinfo=timezone.utc)
            messages = [
                # 1. Lin Hualing inquiry
                WecomUnifiedRecord(
                    source="wecom-local",
                    account_id="cosplay_acc",
                    source_database="message.db",
                    message_id="msg_01",
                    server_id="s_01",
                    conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay",
                    sender_id="u_lin",
                    sender_name="林华玲☆2025年度销售精英☆",
                    sender_corp_id=None,
                    sender_corp_name=None,
                    subject_bucket="unknown",
                    subject_basis="no_corp",
                    message_type="文本",
                    sent_at=t0,
                    text="广州-浦东 541.55kg/4.22cbm/20pcs",
                    reply_to_message_id=None,
                    parse_status="parsed",
                    source_reference="message_table:01",
                ),
                # 2. Qiu Lilan acknowledgment
                WecomUnifiedRecord(
                    source="wecom-local",
                    account_id="cosplay_acc",
                    source_database="message.db",
                    message_id="msg_02",
                    server_id="s_02",
                    conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay",
                    sender_id="u_qiulilan",
                    sender_name="丘丽兰 @ 嘉航盛物流",
                    sender_corp_id="corp_jhs",
                    sender_corp_name="嘉航盛物流",
                    subject_bucket="other",
                    subject_basis="corp_name_match",
                    message_type="文本",
                    sent_at=t0 + timedelta(seconds=34),
                    text="马上",
                    reply_to_message_id=None,
                    parse_status="parsed",
                    source_reference="message_table:02",
                ),
                # 3. Jiahangsheng-XiaoZeng acknowledgment
                WecomUnifiedRecord(
                    source="wecom-local",
                    account_id="cosplay_acc",
                    source_database="message.db",
                    message_id="msg_03",
                    server_id="s_03",
                    conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay",
                    sender_id="u_zeng",
                    sender_name="嘉航盛商务-小曾 @ 嘉航盛物流",
                    sender_corp_id="corp_jhs",
                    sender_corp_name="嘉航盛物流",
                    subject_bucket="other",
                    subject_basis="corp_name_match",
                    message_type="文本",
                    sent_at=t0 + timedelta(seconds=37),
                    text="马上",
                    reply_to_message_id=None,
                    parse_status="parsed",
                    source_reference="message_table:03",
                ),
                # 4. Jiahangsheng-XiaoZeng quote solution
                WecomUnifiedRecord(
                    source="wecom-local",
                    account_id="cosplay_acc",
                    source_database="message.db",
                    message_id="msg_04",
                    server_id="s_04",
                    conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay",
                    sender_id="u_zeng",
                    sender_name="嘉航盛商务-小曾 @ 嘉航盛物流",
                    sender_corp_id="corp_jhs",
                    sender_corp_name="嘉航盛物流",
                    subject_bucket="other",
                    subject_basis="corp_name_match",
                    message_type="文本",
                    sent_at=t0 + timedelta(seconds=50),
                    text="*林华玲☆2025年度销售精英☆: 广州-浦东 541.55kg/4.22cbm/20pcs* @林华玲☆2025年度销售精英☆ 上海900",
                    reply_to_message_id=None,
                    parse_status="parsed",
                    source_reference="message_table:04",
                ),
                # 5. Qiu Yiping (Zhongji) inquiry
                WecomUnifiedRecord(
                    source="wecom-local",
                    account_id="cosplay_acc",
                    source_database="message.db",
                    message_id="msg_05",
                    server_id="s_05",
                    conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay",
                    sender_id="u_qiu",
                    sender_name="邱一平 @ 中技物流",
                    sender_corp_id="corp_zj",
                    sender_corp_name="中技物流",
                    subject_bucket="zhongji",
                    subject_basis="corp_name_match",
                    message_type="文本",
                    sent_at=t0 + timedelta(seconds=125),
                    text="39ctn/624.2kg/0.36cbm 货交上海送郑州",
                    reply_to_message_id=None,
                    parse_status="parsed",
                    source_reference="message_table:05",
                ),
                # 6. Qiu Lilan acknowledgment
                WecomUnifiedRecord(
                    source="wecom-local",
                    account_id="cosplay_acc",
                    source_database="message.db",
                    message_id="msg_06",
                    server_id="s_06",
                    conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay",
                    sender_id="u_qiulilan",
                    sender_name="丘丽兰 @ 嘉航盛物流",
                    sender_corp_id="corp_jhs",
                    sender_corp_name="嘉航盛物流",
                    subject_bucket="other",
                    subject_basis="corp_name_match",
                    message_type="文本",
                    sent_at=t0 + timedelta(seconds=137),
                    text="马上",
                    reply_to_message_id=None,
                    parse_status="parsed",
                    source_reference="message_table:06",
                ),
                # 7. Jiahangsheng-XiaoZeng acknowledgment
                WecomUnifiedRecord(
                    source="wecom-local",
                    account_id="cosplay_acc",
                    source_database="message.db",
                    message_id="msg_07",
                    server_id="s_07",
                    conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay",
                    sender_id="u_zeng",
                    sender_name="嘉航盛商务-小曾 @ 嘉航盛物流",
                    sender_corp_id="corp_jhs",
                    sender_corp_name="嘉航盛物流",
                    subject_bucket="other",
                    subject_basis="corp_name_match",
                    message_type="文本",
                    sent_at=t0 + timedelta(seconds=137),
                    text="马上",
                    reply_to_message_id=None,
                    parse_status="parsed",
                    source_reference="message_table:07",
                ),
                # 8. Jiahangsheng-XiaoZeng quote solution
                WecomUnifiedRecord(
                    source="wecom-local",
                    account_id="cosplay_acc",
                    source_database="message.db",
                    message_id="msg_08",
                    server_id="s_08",
                    conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay",
                    sender_id="u_zeng",
                    sender_name="嘉航盛商务-小曾 @ 嘉航盛物流",
                    sender_corp_id="corp_jhs",
                    sender_corp_name="嘉航盛物流",
                    subject_bucket="other",
                    subject_basis="corp_name_match",
                    message_type="文本",
                    sent_at=t0 + timedelta(seconds=150),
                    text="*邱一平: 39ctn/624.2kg/0.36cbm 货交上海送郑州* @邱一平 郑州480",
                    reply_to_message_id=None,
                    parse_status="parsed",
                    source_reference="message_table:08",
                ),
            ]

            storage.upsert_messages(messages)
            analysis_res = storage.rebuild_analysis()
            self.assertEqual(analysis_res["issues"], 2)

            issues = storage.list_issues()
            self.assertEqual(len(issues), 2)

            # Issue 1: Lin Hualing
            i_lin = next(i for i in issues if "广州-浦东" in i["question_raw_text"])
            self.assertEqual(i_lin["category"], "询价报价")
            self.assertEqual(i_lin["issue_status"], "solved")
            clues_lin = i_lin["clues"]
            self.assertEqual(clues_lin["origin"], "广州")
            self.assertEqual(clues_lin["destination"], "浦东")
            self.assertEqual(clues_lin["packages"], "20pcs")
            self.assertEqual(clues_lin["weight"], "541.55kg")
            self.assertEqual(clues_lin["volume"], "4.22cbm")
            self.assertIn("上海900", i_lin["responder_raw_text"])

            # Issue 2: Qiu Yiping (Zhongji)
            i_qiu = next(i for i in issues if "货交上海送郑州" in i["question_raw_text"])
            self.assertEqual(i_qiu["category"], "询价报价")
            self.assertEqual(i_qiu["question_subject_bucket"], "zhongji")
            self.assertEqual(i_qiu["issue_status"], "solved")
            clues_qiu = i_qiu["clues"]
            self.assertEqual(clues_qiu["origin"], "上海")
            self.assertEqual(clues_qiu["destination"], "郑州")
            self.assertEqual(clues_qiu["packages"], "39ctn")
            self.assertEqual(clues_qiu["weight"], "624.2kg")
            self.assertEqual(clues_qiu["volume"], "0.36cbm")
            self.assertIn("郑州480", i_qiu["responder_raw_text"])

            # Verify summary
            summary = storage.get_summary()
            self.assertEqual(summary["issue_count"], 2)
            self.assertEqual(summary["solved_count"], 2)
            self.assertEqual(summary["unreplied_count"], 0)

    def test_zhongji_ai_cosplay_full_eight_cycles_through_210203(self):
        """Verify full timeline through 8/31 21:02:03 including dangerous goods and multi-batch pricing."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "analysis.db"
            storage = WecomLocalStorage(db_path)
            storage.initialize()

            # Subject classifier test for Liu Jia @ 中技物流
            classifier = WecomSubjectClassifier()
            s_liu = classifier.classify(sender_display="刘佳 @ 中技物流")
            self.assertEqual(s_liu.subject_bucket, "zhongji")
            self.assertEqual(s_liu.raw_subject, "中技物流")

            t0 = datetime(2026, 8, 31, 20, 54, 15, tzinfo=timezone.utc)
            messages = [
                # 1. Dangerous goods inquiry (20:54:15)
                WecomUnifiedRecord(
                    source="wecom-local", account_id="acc_dg", source_database="message.db",
                    message_id="msg_dg_01", server_id="s_dg_01", conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay", sender_id="u_lin",
                    sender_name="林华玲☆2025年度销售精英☆", sender_corp_id=None, sender_corp_name=None,
                    subject_bucket="unknown", subject_basis="default", message_type="文本",
                    sent_at=t0,
                    text="上海-深圳, 氢氧化钾溶液8类危险品\n1406kg/1.4cbm/1case/100*100*140cm",
                    reply_to_message_id=None, parse_status="parsed", source_reference="message_table:18",
                ),
                # 2. Ack (20:54:34)
                WecomUnifiedRecord(
                    source="wecom-local", account_id="acc_dg", source_database="message.db",
                    message_id="msg_dg_02", server_id="s_dg_02", conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay", sender_id="u_zeng",
                    sender_name="嘉航盛商务小曾 @ 嘉航盛物流", sender_corp_id="corp_jhs", sender_corp_name="嘉航盛物流",
                    subject_bucket="other", subject_basis="corp_name_match", message_type="文本",
                    sent_at=t0 + timedelta(seconds=19), text="马上",
                    reply_to_message_id=None, parse_status="parsed", source_reference="message_table:19",
                ),
                # 3. Dangerous goods rejection solution (20:54:50)
                WecomUnifiedRecord(
                    source="wecom-local", account_id="acc_dg", source_database="message.db",
                    message_id="msg_dg_03", server_id="s_dg_03", conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay", sender_id="u_zeng",
                    sender_name="嘉航盛商务小曾 @ 嘉航盛物流", sender_corp_id="corp_jhs", sender_corp_name="嘉航盛物流",
                    subject_bucket="other", subject_basis="corp_name_match", message_type="文本",
                    sent_at=t0 + timedelta(seconds=35),
                    text="“林华玲☆2025年度销售精英☆:\n上海-深圳, 氢氧化钾溶液8类危险品\n1406kg/1.4cbm/1case/100*100*140cm”\n------\n@林华玲☆2025年度销售精英☆ 不好意思危险品做不了",
                    reply_to_message_id=None, parse_status="parsed", source_reference="message_table:20",
                ),
                # 4. Liu Jia route 1: 交上海送南京 (20:55:58)
                WecomUnifiedRecord(
                    source="wecom-local", account_id="acc_dg", source_database="message.db",
                    message_id="msg_liu_01", server_id="s_liu_01", conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay", sender_id="u_liu",
                    sender_name="刘佳 @ 中技物流", sender_corp_id="corp_zj", sender_corp_name="中技物流",
                    subject_bucket="zhongji", subject_basis="corp_name_match", message_type="文本",
                    sent_at=t0 + timedelta(seconds=103),
                    text="5650KG /12.097CBM/6PLT 145*103*135CM-6交上海送南京",
                    reply_to_message_id=None, parse_status="parsed", source_reference="message_table:21",
                ),
                # 5. Quote solution for Liu Jia route 1 (20:58:17)
                WecomUnifiedRecord(
                    source="wecom-local", account_id="acc_dg", source_database="message.db",
                    message_id="msg_liu_02", server_id="s_liu_02", conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay", sender_id="u_zeng",
                    sender_name="嘉航盛商务小曾 @ 嘉航盛物流", sender_corp_id="corp_jhs", sender_corp_name="嘉航盛物流",
                    subject_bucket="other", subject_basis="corp_name_match", message_type="文本",
                    sent_at=t0 + timedelta(seconds=242),
                    text="“刘佳:\n5650KG /12.097CBM/6PLT 145*103*135CM-6交上海送南京”\n------\n@刘佳 南京2000次日达",
                    reply_to_message_id=None, parse_status="parsed", source_reference="message_table:22",
                ),
                # 6. Liu Jia route 2: 交杭州送南京 (21:01:23)
                WecomUnifiedRecord(
                    source="wecom-local", account_id="acc_dg", source_database="message.db",
                    message_id="msg_liu_03", server_id="s_liu_03", conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay", sender_id="u_liu",
                    sender_name="刘佳 @ 中技物流", sender_corp_id="corp_zj", sender_corp_name="中技物流",
                    subject_bucket="zhongji", subject_basis="corp_name_match", message_type="文本",
                    sent_at=t0 + timedelta(seconds=428),
                    text="5650KG /12.097CBM/6PLT 145*103*135CM-6交杭州送南京 这个呢",
                    reply_to_message_id=None, parse_status="parsed", source_reference="message_table:23",
                ),
                # 7. Quote solution for Liu Jia route 2 (21:02:03)
                WecomUnifiedRecord(
                    source="wecom-local", account_id="acc_dg", source_database="message.db",
                    message_id="msg_liu_04", server_id="s_liu_04", conversation_id="R:cosplay",
                    conversation_name="中技AI cosplay", sender_id="u_zeng",
                    sender_name="嘉航盛商务小曾 @ 嘉航盛物流", sender_corp_id="corp_jhs", sender_corp_name="嘉航盛物流",
                    subject_bucket="other", subject_basis="corp_name_match", message_type="文本",
                    sent_at=t0 + timedelta(seconds=468),
                    text="“刘佳:\n5650KG /12.097CBM/6PLT 145*103*135CM-6交杭州送南京 这个呢”\n------\n@刘佳 南京2000次日达",
                    reply_to_message_id=None, parse_status="parsed", source_reference="message_table:24",
                ),
            ]

            storage.upsert_messages(messages)
            analysis_res = storage.rebuild_analysis()
            issues = storage.list_issues()

            # Verify dangerous goods issue
            i_dg = next(i for i in issues if "氢氧化钾" in i["question_raw_text"])
            self.assertEqual(i_dg["issue_status"], "solved")
            self.assertIn("危险品做不了", i_dg["solution_text"])
            clues_dg = i_dg["clues"]
            self.assertEqual(clues_dg["origin"], "上海")
            self.assertEqual(clues_dg["destination"], "深圳")
            self.assertEqual(clues_dg["packages"], "1case")
            self.assertEqual(clues_dg["weight"], "1406kg")
            self.assertEqual(clues_dg["volume"], "1.4cbm")

            # Verify Liu Jia Hangzhou -> Nanjing issue
            i_hz = next(i for i in issues if "交杭州送南京" in i["question_raw_text"])
            self.assertEqual(i_hz["issue_status"], "solved")
            self.assertEqual(i_hz["question_subject_bucket"], "zhongji")
            self.assertIn("南京2000次日达", i_hz["solution_text"])
            clues_hz = i_hz["clues"]
            self.assertEqual(clues_hz["origin"], "杭州")
            self.assertEqual(clues_hz["destination"], "南京")
            self.assertEqual(clues_hz["packages"], "6PLT")
            self.assertEqual(clues_hz["weight"], "5650KG")
            self.assertEqual(clues_hz["volume"], "12.097CBM")

            # Verify Liu Jia Shanghai -> Nanjing issue
            i_sh = next(i for i in issues if "交上海送南京" in i["question_raw_text"])
            self.assertEqual(i_sh["issue_status"], "solved")
            self.assertEqual(i_sh["question_subject_bucket"], "zhongji")
            self.assertIn("南京2000次日达", i_sh["solution_text"])



class TestRecursiveEvidence(unittest.TestCase):
    def test_snapshot_capture_full_replay_preserves_sources_and_nested_evidence(self):
        import hashlib
        from chatlog_assistant.sources.wecom_pipeline import run_single_capture
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "live" / "fixture" / "Data"
            data.mkdir(parents=True)
            path = data / "message.db"
            source = sqlite3.connect(path)
            source.execute("CREATE TABLE message_table(message_id INTEGER,sender_id INTEGER,conversation_id TEXT,content_type INTEGER,send_time INTEGER,content BLOB)")
            children = [{"message_id": "q", "sender_id": "u1", "sender_display": "测试客户 @中技物流",
                         "sent_at": "2026-08-31T09:02:23+08:00", "content": "交上海送南京，请报价"},
                        {"message_id": "r", "sender_id": "u2", "sender_display": "测试客服 @其他物流",
                         "sent_at": "2026-08-31T09:03:03+08:00", "content": "南京620次日达", "reply_to_message_id": "q"}]
            source.execute("INSERT INTO message_table VALUES(1,1,'g',49,1788220000,?)",
                           (json.dumps({"forwarded_messages": children}, ensure_ascii=False).encode(),))
            source.commit()
            source.close()
            session = sqlite3.connect(data / "session.db")
            session.execute("CREATE TABLE conversation_table(id TEXT,name TEXT)")
            session.execute("INSERT INTO conversation_table VALUES('g','测试目标群')")
            session.commit()
            session.close()
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            db = root / "analysis.db"
            for _ in range(2):
                result = run_single_capture("fixture", wecom_root=root / "live", analysis_db_path=db,
                                            conversation_name="目标", full_replay=True)
                self.assertTrue(result["success"])
                self.assertEqual(result["new_messages"], 3)
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
            report = WecomLocalStorage(db).get_report(conversation_name="目标")
            self.assertEqual(report["coverage"]["database_backed_count"], 3)
            self.assertEqual(report["events"][0]["solution_seconds"], 40)
            self.assertEqual(report["messages"][0]["nesting_depth"], 1)
            self.assertTrue(Path(result["snapshots"]["message.db"]["snapshot_file"]).is_file())

    def test_http_report_export_keeps_unverified_provenance_and_full_rows(self):
        from http.server import ThreadingHTTPServer
        from threading import Thread
        from urllib.request import urlopen
        from urllib.parse import urlencode
        from chatlog_assistant.sources.wecom_web import WecomDashboardHandler
        from chatlog_assistant.sources.wecom_pipeline import import_normalized_jsonl
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.jsonl"
            source.write_text(json.dumps({"message_id": "q", "conversation_id": "g", "conversation_name": "测试目标群",
                "sender_display": "测试客户 @中技物流", "sent_at": "2026-08-31T09:02:23+08:00",
                "content": "交上海送南京，请报价"}, ensure_ascii=False), encoding="utf-8")
            db = Path(directory) / "analysis.db"
            import_normalized_jsonl(source, account_id="fixture", analysis_db_path=db)
            handler = type("FixtureHandler", (WecomDashboardHandler,), {"storage": WecomLocalStorage(db)})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                query = urlencode({"conversation_name": "目标", "format": "json"})
                with urlopen(f"http://127.0.0.1:{server.server_port}/api/wecom/report-export?{query}", timeout=5) as response:
                    self.assertIn("attachment", response.headers["Content-Disposition"])
                    report = json.load(response)
                self.assertEqual(len(report["messages"]), 1)
                self.assertEqual(report["metrics"]["unreplied_count"], 1)
                self.assertFalse(report["coverage"]["full_history_verified"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_damaged_nested_sibling_does_not_discard_valid_messages(self):
        from chatlog_assistant.sources.wecom_parser import records_from_message_tree
        nodes = [{"message_id": "card", "sender_display": "转发者 @其他", "sent_at": "2026-09-01T10:00:00+08:00",
                  "forwarded_messages": [
                      {"message_id": "broken", "sender_display": "测试客户", "content": "缺少原时间"},
                      {"message_id": "valid", "sender_display": "测试客户 @中技物流", "sent_at": "2026-08-31T09:02:23+08:00", "content": "请报价"}]}]
        records, gaps = records_from_message_tree(nodes, account_id="test", source_database="fixture",
            conversation_id="g", conversation_name="测试目标群", source_reference="fixture", strict=False)
        self.assertEqual({r.message_id for r in records}, {"card", "valid"})
        self.assertEqual(len(gaps), 1)
        self.assertEqual(next(r for r in records if r.message_id == "card").parse_status, "partial_forward")

    def test_delivery_time_alone_is_a_solution_not_a_new_question(self):
        assessment = assess_logistics_response("@测试客户 上海隔日达")
        self.assertTrue(assessment.is_solution)
        self.assertEqual(classify_logistics_issue("上海隔日达"), [])

    def test_missing_raw_timestamp_is_not_replaced_with_current_time(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE message_table(message_id INTEGER,conversation_id TEXT,send_time INTEGER,content_type INTEGER,content TEXT)")
        conn.execute("INSERT INTO message_table VALUES(1,'g',0,0,'测试正文')")
        rows, _, _ = WecomLocalParser().parse_databases("test", conn)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].sent_at)
        self.assertEqual(rows[0].parse_status, "invalid_timestamp")

    def test_unstable_snapshot_cannot_be_decrypted_or_imported(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "message.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE message_table(id INTEGER)")
            conn.commit()
            conn.close()
            snapshot = capture_consistent_snapshot(path, "test")
            plaintext, result = decrypt_and_verify_snapshot(replace(snapshot, is_consistent=False), None)
            self.assertIsNone(plaintext)
            self.assertFalse(result.is_valid)

    def test_xml_forward_preserves_nested_times_and_reports_missing_count(self):
        from chatlog_assistant.sources.wecom_protobuf import decode_forwarded_content
        from chatlog_assistant.sources.wecom_parser import records_from_message_tree
        nested = ('<recordinfo><datalist count="2"><dataitem datatype="1" dataid="q">'
                  '<sourcename>测试客户 @中技物流</sourcename><dataitemsource><fromusr>u1</fromusr></dataitemsource>'
                  '<srcMsgCreateTime>1788138143</srcMsgCreateTime><datadesc>交上海送南京，请报价</datadesc>'
                  '</dataitem></datalist></recordinfo>')
        xml = ('<recordinfo><datalist count="1"><dataitem datatype="17" dataid="inner">'
               '<sourcename>测试转发者 @其他</sourcename><srcMsgCreateTime>1788220000</srcMsgCreateTime>'
               '<recorditem><![CDATA[' + nested + ']]></recorditem></dataitem></datalist></recordinfo>')
        decoded = decode_forwarded_content(xml)
        self.assertEqual(len(decoded.gaps), 1)
        records, gaps = records_from_message_tree(decoded.messages, account_id="test", source_database="fixture",
            conversation_id="g", conversation_name="测试目标群", source_reference="fixture")
        child = next(r for r in records if r.message_id == "q")
        self.assertEqual(child.sent_at.isoformat(), "2026-08-31T09:02:23+08:00")
        self.assertEqual(child.sender_id, "u1")
        self.assertEqual(child.subject_bucket, "zhongji")
        self.assertEqual(child.nesting_depth, 1)
        self.assertFalse(gaps)

    def test_report_counts_questions_not_categories_and_exposes_all_evidence(self):
        from chatlog_assistant.sources.wecom_pipeline import import_normalized_jsonl
        def row(key, second, name, body, **extra):
            return dict(message_id=key, conversation_id="g", conversation_name="测试目标群",
                        sender_display=name, sent_at=f"2026-08-31T09:02:{second:02d}+08:00", content=body, **extra)
        rows = [row("q", 0, "客户 @中技物流", "提货地址：北京市某仓\n送到上海浦东机场，40ctns/320kgs/1.77cbm，请报价并告知时效"),
                row("a", 7, "客服 @其他", "马上"),
                row("r", 38, "客服 @其他", "上海620隔日达", reply_to_message_id="q"),
                row("f", 40, "转发者 @其他", "[群聊的聊天记录] 客户...客服...")]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "chat.jsonl"
            source.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
            db = Path(directory) / "analysis.db"
            import_normalized_jsonl(source, account_id="test", analysis_db_path=db)
            report = WecomLocalStorage(db).get_report(conversation_name="目标群")
            self.assertEqual(report["metrics"]["question_count"], 1)
            self.assertEqual(report["metrics"]["solved_count"], 1)
            self.assertEqual(report["metrics"]["ack_latency"]["mean_seconds"], 7)
            self.assertEqual(report["metrics"]["solution_latency"]["mean_seconds"], 38)
            self.assertEqual(report["coverage"]["unexpanded_forward_count"], 1)
            self.assertFalse(report["coverage"]["full_history_verified"])
            self.assertEqual(len(report["messages"]), 4)
            self.assertEqual(len(report["events"][0]["responses"]), 2)
            self.assertEqual(report["routes"][0]["region"], "华北→华东")

    def test_exact_mentions_and_followup_branch_have_distinct_latencies(self):
        from chatlog_assistant.sources.wecom_pipeline import import_normalized_jsonl
        def item(key, second, sender, body):
            return {"message_id": key, "sent_at": f"2026-08-31T09:02:{second:02d}+08:00",
                    "sender_id": sender, "sender_display": sender, "content": body,
                    "conversation_id": "test", "conversation_name": "测试目标群"}
        rows = [item("q1", 0, "客户甲 @中技物流", "交上海送南京，请报价"),
                item("q2", 1, "客户甲乙 @中技物流", "交上海送南京，请报价"),
                item("a", 7, "客服 @其他", "@客户甲 马上"),
                item("r", 20, "客服 @其他", "客户甲: 交上海送南京，请报价\n@客户甲 南京620次日达"),
                item("q3", 30, "客户甲 @中技物流", "交杭州送南京 这个呢"),
                item("r3", 40, "客服 @其他", "@客户甲 南京700次日达")]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mentions.jsonl"
            source.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
            db = Path(directory) / "analysis.db"
            import_normalized_jsonl(source, account_id="test", analysis_db_path=db)
            issues = WecomLocalStorage(db).list_issues(category="询价报价")
            by_sender = [i for i in issues if i["question_sender_name"] == "客户甲乙 @中技物流"]
            self.assertEqual(by_sender[0]["issue_status"], "unreplied")
            first = next(i for i in issues if i["question_sent_at"].endswith("00+08:00"))
            followup = next(i for i in issues if "这个呢" in i["question_raw_text"])
            self.assertEqual(first["first_ack_seconds"], 7)
            self.assertEqual(first["solution_seconds"], 20)
            self.assertEqual(followup["solution_seconds"], 10)
            self.assertEqual(followup["clues"]["followup_to_message_id"], first["message_pk"])
            self.assertIsNone(followup["clues"]["weight"])

    def test_all_specs_special_goods_and_quote_amount_are_grounded(self):
        text = ("提货地址：江苏省无锡市新吴区纺城大道某仓\n送到北京首都机场，麻烦报价\n"
                "1PLT/772KGS/0.72CBM/800*1200*800mm；2case/1406kg/1.4cbm/100*100*140cm\n"
                "氢氧化钾溶液，8类危险品，需要报关/清关")
        clues = extract_business_clues(text).as_dict()
        self.assertEqual(clues["origin"], "无锡")
        self.assertEqual(clues["destination"], "北京首都机场")
        self.assertEqual(clues["package_items"], ["1PLT", "2case"])
        self.assertEqual(clues["weight_items"], ["772KGS", "1406kg"])
        self.assertEqual(clues["dimension_items"], ["800*1200*800mm", "100*100*140cm"])
        self.assertIn("氢氧化钾溶液", clues["special_goods"])
        self.assertEqual(clues["document_requirements"], ["报关", "清关"])
        self.assertIsNone(extract_business_clues("交杭州送南京 这个呢").as_dict()["weight"])
        quote = extract_business_clues("@测试客户 郑州480").as_dict()
        self.assertEqual(quote["price_quote"], "480")
        self.assertIsNone(quote["currency"])

    def test_raw_forward_envelope_expands_and_call_type40_is_not_forward(self):
        # Synthetic envelope with explicit field names; not a captured customer payload.
        def varint(number):
            out = bytearray()
            while number > 127:
                out.append((number & 127) | 128)
                number >>= 7
            return bytes(out) + bytes([number])
        def field(number, value):
            return varint(number * 8 + 2) + varint(len(value)) + value
        child = {"source_message_id": "original", "sender_id": "test-customer",
                 "sender_display": "测试客户 @中技物流", "sent_at": "2026-08-31T09:02:23+08:00",
                 "content": "交上海送南京，2PLT/500kg/1cbm，请报价"}
        envelope = field(7, field(9, json.dumps({"forwarded_messages": [child]}, ensure_ascii=False).encode()))
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE message_table(message_id INTEGER, sender_id INTEGER, conversation_id TEXT, content_type INTEGER, send_time INTEGER, content BLOB, extra_content BLOB)")
        conn.execute("INSERT INTO message_table VALUES(1,1,'g',49,1788220000,?,NULL)", (envelope,))
        conn.execute("INSERT INTO message_table VALUES(2,1,'g',40,1788220001,?,NULL)",
                     (b'\x08\x02\x10\x05' + field(3, '通话时长10:15'.encode()) + b'\x32\x00',))
        rows, _, _ = WecomLocalParser().parse_databases("test", conn)
        self.assertEqual(len(rows), 3)
        original = next(r for r in rows if r.message_id == "original")
        root = next(r for r in rows if r.message_id == "1")
        call = next(r for r in rows if r.message_id == "2")
        self.assertEqual(original.parent_id, root.id)
        self.assertEqual(original.subject_bucket, "zhongji")
        self.assertEqual(root.parse_status, "expanded_forward")
        self.assertEqual(call.message_type, "通话记录")
        self.assertEqual(call.text, "通话时长10:15")
        self.assertEqual(parse_wecom_content(49, "群聊的聊天记录：客户...客服...")[2], "unexpanded_forward")

    def test_recursive_jsonl_retains_containers_identity_and_parent_chain(self):
        from chatlog_assistant.sources.wecom_pipeline import import_normalized_jsonl
        def item(key, seconds, sender, content, **extra):
            return dict(source_message_id=key, sender_id=sender, sender_display=sender,
                        sent_at=f"2026-08-31T09:02:{seconds:02d}+08:00", content=content, **extra)
        question = item("q", 0, "测试客户 @中技物流", "交上海送南京，2PLT/500kg/1cbm，麻烦报价")
        ack = item("a", 7, "测试客服 @其他物流", "马上")
        quote = item("r", 38, "测试客服 @其他物流", "南京620次日达", reply_to_message_id="q")
        inner = item("f2", 45, "第二转发者 @其他", "[群聊的聊天记录]",
                     forwarded_messages=[question, ack, quote], conversation_name="原始子群")
        root = item("f1", 50, "第一转发者 @其他", "[群聊的聊天记录]",
                    conversation_id="root", conversation_name="测试目标群", forwarded_messages=[inner])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "tree.jsonl"
            source.write_text(json.dumps(root, ensure_ascii=False), encoding="utf-8")
            db = Path(directory) / "analysis.db"
            for _ in range(2):
                result = import_normalized_jsonl(source, account_id="test", analysis_db_path=db,
                                                conversation_name="测试目标群")
            self.assertEqual(result["imported_messages"], 5)
            storage = WecomLocalStorage(db)
            with storage.connect() as conn:
                rows = {r["message_id"]: dict(r) for r in conn.execute("SELECT * FROM messages")}
            self.assertEqual(len(rows), 5)
            self.assertEqual(rows["q"]["parent_id"], rows["f2"]["id"])
            self.assertEqual(rows["f2"]["parent_id"], rows["f1"]["id"])
            self.assertEqual(rows["q"]["root_message_id"], rows["f1"]["id"])
            self.assertEqual(rows["q"]["nesting_depth"], 2)
            self.assertEqual(rows["q"]["subject_bucket"], "zhongji")
            self.assertIn("f1", rows["q"]["source_reference"])
            self.assertIn("f2", rows["q"]["source_reference"])
            self.assertEqual(rows["q"]["sent_at"], "2026-08-31T09:02:00+08:00")
            issues = storage.list_issues(conversation_name="测试目标群", category="询价报价")
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["first_response_seconds"], 7)
            self.assertEqual(issues[0]["solution_seconds"], 38)
            self.assertEqual(storage.get_summary(conversation_id="root")["question_count"], 1)
            self.assertEqual(storage.get_summary(conversation_id="root")["coverage"]["message_count"], 5)
            self.assertEqual(len(storage.list_issues(conversation_id="root", category="询价报价")), 1)


if __name__ == "__main__":
    unittest.main()
