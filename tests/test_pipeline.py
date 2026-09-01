from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from chatlog_assistant.models import Message
from chatlog_assistant.pipeline import import_messages
from chatlog_assistant.storage import Storage


class PipelineTests(unittest.TestCase):
    def test_ingest_is_idempotent_and_links_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.db")
            messages = [
                Message("sample", "1", "g", datetime.fromisoformat("2026-08-31T09:00:00+08:00"), "张某 @中技物流", "请安排提货并报价"),
                Message("sample", "2", "g", datetime.fromisoformat("2026-08-31T09:00:20+08:00"), "客服 @其他物流", "马上"),
                Message("sample", "3", "g", datetime.fromisoformat("2026-08-31T09:01:20+08:00"), "客服 @其他物流", "预计报价620元，已安排提货"),
            ]
            import_messages(storage, messages)
            import_messages(storage, messages)
            summary = storage.summary("zhongji")
            self.assertEqual(summary["issue_count"], 2)
            self.assertEqual(summary["replied_count"], 2)
            self.assertEqual(summary["solved_count"], 2)
            issues = storage.list_issues("zhongji")
            self.assertTrue(all(item["response_text"] == "预计报价620元，已安排提货" for item in issues))


if __name__ == "__main__":
    unittest.main()
