import unittest

from chatlog_assistant.classifier import assess_response, classify_issue, needs_semantic


class ClassifierTests(unittest.TestCase):
    def test_transport_request_can_have_multiple_categories(self) -> None:
        text = "40ctns/320kgs/1.77cbm 提货地址：北京 送到上海机场，麻烦请报价"
        categories = {item.category for item in classify_issue(text)}
        self.assertIn("询价报价", categories)
        self.assertIn("提送货安排", categories)
        self.assertIn("时效与航线", categories)
        self.assertIn("货物规格", categories)

    def test_acknowledgement_is_not_a_solution(self) -> None:
        result = assess_response("马上")
        self.assertEqual(result.kind, "即时响应")
        self.assertFalse(result.is_solution)

    def test_quote_is_a_solution(self) -> None:
        result = assess_response("预计报价620元")
        self.assertEqual(result.kind, "报价方案")
        self.assertTrue(result.is_solution)

    def test_solution_statement_is_not_a_new_issue(self) -> None:
        self.assertEqual(classify_issue("@张某 预计报价620元，已安排提货"), [])

    def test_other_logistics_needs_semantic(self) -> None:
        text = "请看下这个怎么处理"
        hits = classify_issue(text)
        self.assertTrue(any(item.category == "其他物流问题" for item in hits))
        self.assertTrue(needs_semantic(text, hits))

    def test_persona_hint_needs_semantic(self) -> None:
        self.assertTrue(needs_semantic("把人设改成更稳重的客服", []))


if __name__ == "__main__":
    unittest.main()
