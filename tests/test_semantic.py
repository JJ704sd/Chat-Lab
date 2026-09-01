import json
import unittest
from datetime import datetime
from pathlib import Path
import tempfile

from chatlog_assistant.classifier import classify_issue
from chatlog_assistant.models import Classification, Message
from chatlog_assistant.pipeline import import_messages
from chatlog_assistant.semantic import (
    SemanticAnalyzer,
    SemanticSettings,
    merge_classifications,
    parse_json_object,
)
from chatlog_assistant.storage import Storage


class FakeMiniMax:
    def complete(self, system: str, user: str) -> str:
        payload = json.loads(user.split("\n", 1)[1])
        if "请评估下列回复" in user:
            return json.dumps(
                {
                    "items": [
                        {"id": row["id"], "kind": "处理方案", "is_solution": True, "confidence": 0.92}
                        for row in payload
                    ]
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "items": [
                    {
                        "id": row["id"],
                        "is_issue": any(marker in row["text"] for marker in ("请", "麻烦", "怎么")),
                        "categories": ["角色与人设"],
                        "confidence": 0.91,
                        "summary": "调整人设",
                    }
                    for row in payload
                ]
            },
            ensure_ascii=False,
        )


class SemanticParserTests(unittest.TestCase):
    def test_parse_json_from_fenced_output(self) -> None:
        payload = parse_json_object('```json\n{"items":[]}\n```')
        self.assertEqual(payload["items"], [])

    def test_keyword_keeps_high_confidence_logistics(self) -> None:
        text = "麻烦请报价"
        hits = classify_issue(text)
        merged = merge_classifications(text, hits, [Classification("角色与人设", 0.9, ("minimax-m3",))])
        self.assertIn("询价报价", {item.category for item in merged})

    def test_llm_replaces_other_logistics(self) -> None:
        text = "请把人设改成更稳重"
        hits = classify_issue(text)
        semantic = [Classification("角色与人设", 0.91, ("minimax-m3",))]
        merged = merge_classifications(text, hits, semantic)
        self.assertEqual([item.category for item in merged], ["角色与人设"])

    def test_analyzer_classifies_batch(self) -> None:
        analyzer = SemanticAnalyzer(
            settings=SemanticSettings(api_key="test-key"),
            client=FakeMiniMax(),
        )
        result = analyzer.classify_many([("m1", "请把人设改得更稳重")])
        self.assertEqual(result["m1"][0].category, "角色与人设")
        self.assertIn("minimax-m3", result["m1"][0].evidence)


class SemanticPipelineTests(unittest.TestCase):
    def test_rebuild_uses_minimax_for_persona_issue(self) -> None:
        analyzer = SemanticAnalyzer(
            settings=SemanticSettings(api_key="test-key"),
            client=FakeMiniMax(),
        )
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.db")
            messages = [
                Message(
                    "sample",
                    "1",
                    "g",
                    datetime.fromisoformat("2026-08-31T09:00:00+08:00"),
                    "张某 @中技物流",
                    "请把中技AI人设改得更稳重一点",
                ),
                Message(
                    "sample",
                    "2",
                    "g",
                    datetime.fromisoformat("2026-08-31T09:01:00+08:00"),
                    "客服 @其他物流",
                    "已按你的要求改了系统提示，新角色会少说空话",
                ),
            ]
            import_messages(storage, messages, semantic=False)
            result = storage.rebuild_analysis(analyzer=analyzer)
            self.assertTrue(result["semantic_enabled"])
            issues = storage.list_issues("zhongji")
            self.assertTrue(any(item["category"] == "角色与人设" for item in issues))
            self.assertTrue(any(item["is_solution"] for item in issues))


if __name__ == "__main__":
    unittest.main()
