import json
from pathlib import Path
import tempfile
import unittest

from chatlog_assistant.pipeline import import_archive_inbox, import_archive_jsonl
from chatlog_assistant.sources.wecom_archive import (
    conversation_id_for,
    message_from_archive,
    message_from_unified,
    load_display_map,
)
from chatlog_assistant.storage import Storage
from chatlog_assistant.subject import SubjectResolver


OFFICIAL_TEXT = {
    "seq": 196,
    "msgid": "CAQQluDa4QUY0On2rYSAgAMgzPrShAE=",
    "action": "send",
    "from": "XuJinSheng",
    "tolist": ["icefog"],
    "roomid": "",
    "msgtime": 1547087894783,
    "msgtype": "text",
    "text": {"content": "请安排提货并报价"},
}

OFFICIAL_ROOM = {
    "msgid": "room-1",
    "from": "XuJinSheng",
    "tolist": ["icefog", "LiSi"],
    "roomid": "wrjc7bDwYAOAhf9quEwRRxyyoMm0QAAA",
    "msgtime": 1576034482344,
    "msgtype": "text",
    "text": {"content": "货物延误还没有到，请帮忙查询轨迹"},
}

OFFICIAL_IMAGE = {
    "msgid": "img-1_external",
    "from": "wmErxtDgAA9AW32YyyuYRimKr7D1KWlw",
    "tolist": ["kenshin"],
    "roomid": "",
    "msgtime": 1603875615723,
    "msgtype": "image",
    "image": {"sdkfileid": "CtYBMzA2", "filesize": 70961},
}

OFFICIAL_MIXED = {
    "msgid": "mixed-1",
    "from": "HeMiao",
    "tolist": ["HeChangTian"],
    "roomid": "wr_tZ2BwAAUwHpYMwy9cIWqnlU3Hzqfg",
    "msgtime": 1577414359072,
    "msgtype": "mixed",
    "mixed": {
        "item": [
            {"type": "text", "content": "{\"content\":\"你好\\n\"}"},
            {"type": "image", "content": "{\"sdkfileid\":\"abc\"}"},
        ]
    },
}


class WecomArchiveMapperTests(unittest.TestCase):
    def test_text_uses_display_map_for_zhongji_subject(self) -> None:
        message = message_from_archive(
            OFFICIAL_TEXT,
            {"XuJinSheng": "张某 @中技物流"},
        )
        assert message is not None
        self.assertEqual(message.source, "wecom")
        self.assertEqual(message.source_message_id, OFFICIAL_TEXT["msgid"])
        self.assertEqual(message.conversation_id, "dm:XuJinSheng:icefog")
        self.assertEqual(message.sender_display, "张某 @中技物流")
        self.assertEqual(message.content, "请安排提货并报价")
        self.assertEqual(message.direction, "inbound")
        subject = SubjectResolver().resolve(message.sender_display)
        self.assertEqual(subject.bucket, "zhongji")

    def test_body_at_does_not_change_subject(self) -> None:
        item = dict(OFFICIAL_TEXT)
        item["text"] = {"content": "@中技物流 请报价"}
        item["from"] = "icefog"
        message = message_from_archive(item, {"icefog": "客服 @嘉航盛物流"})
        assert message is not None
        self.assertEqual(SubjectResolver().resolve(message.sender_display).bucket, "other")
        self.assertIn("@中技物流", message.content)

    def test_room_id_wins_over_direct_chat(self) -> None:
        message = message_from_archive(OFFICIAL_ROOM, {})
        assert message is not None
        self.assertEqual(message.conversation_id, OFFICIAL_ROOM["roomid"])

    def test_external_image_is_outbound_placeholder(self) -> None:
        message = message_from_archive(OFFICIAL_IMAGE, {})
        assert message is not None
        self.assertEqual(message.direction, "outbound")
        self.assertEqual(message.content_type, "image")
        self.assertIn("sdkfileid=CtYBMzA2", message.content)

    def test_mixed_joins_text_and_media(self) -> None:
        message = message_from_archive(OFFICIAL_MIXED, {})
        assert message is not None
        self.assertEqual(message.content_type, "mixed")
        self.assertIn("你好", message.content)
        self.assertIn("sdkfileid=abc", message.content)

    def test_switch_is_skipped(self) -> None:
        self.assertIsNone(
            message_from_archive({"msgid": "s1", "action": "switch", "user": "XuJinSheng", "time": 1})
        )

    def test_conversation_id_sorts_direct_chat(self) -> None:
        self.assertEqual(conversation_id_for("", "b", ["a"]), "dm:a:b")

    def test_import_archive_jsonl_dual_format(self) -> None:
        display = {"XuJinSheng": "张某 @中技物流"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inbox.jsonl"
            normalized = {
                "source": "wecom",
                "source_message_id": "already-1",
                "conversation_id": "group-1",
                "sent_at": "2026-08-31T09:02:23+08:00",
                "sender_display": "李某 @中技",
                "content": "补一票",
            }
            path.write_text(
                json.dumps(OFFICIAL_TEXT, ensure_ascii=False)
                + "\n"
                + json.dumps(normalized, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            storage = Storage(Path(directory) / "chatlog.db")
            count = import_archive_jsonl(storage, path, display_map=display)
            self.assertEqual(count, 2)
            summary = storage.summary("zhongji")
            self.assertGreaterEqual(summary["issue_count"], 1)

    def test_unified_json_uses_corp_name_for_subject(self) -> None:
        item = {
            "source": "wecom",
            "corp_id": "fixture",
            "seq": 10001,
            "msg_id": "unified-1",
            "msg_time": 1547087894783,
            "room_id": "",
            "sender_id": "XuJinSheng",
            "sender_name": "张浩",
            "sender_corp_name": "中技物流",
            "subject": "zhongji",
            "msg_type": "text",
            "text": "请安排提货并报价",
            "media_paths": [],
        }
        message = message_from_unified(item)
        assert message is not None
        self.assertEqual(message.sender_display, "张浩 @中技物流")
        self.assertEqual(message.sender_corp_name, "中技物流")
        self.assertEqual(SubjectResolver().resolve(message.sender_display, message.sender_corp_name).bucket, "zhongji")
        self.assertNotIn("@中技物流", "请安排提货并报价")

    def test_unified_body_mention_is_not_subject(self) -> None:
        item = {
            "msg_id": "unified-2",
            "msg_time": 1,
            "sender_id": "icefog",
            "sender_name": "客服",
            "sender_corp_name": "嘉航盛物流",
            "msg_type": "text",
            "text": "@中技物流 请报价",
            "media_paths": [],
        }
        message = message_from_unified(item)
        assert message is not None
        self.assertEqual(SubjectResolver().resolve(message.sender_display, message.sender_corp_name).bucket, "other")
        self.assertIn("@中技物流", message.content)

    def test_import_inbox_moves_unified_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / "inbox"
            inbox.mkdir()
            (inbox / "batch.jsonl").write_text(
                json.dumps(
                    {
                        "source": "wecom",
                        "msg_id": "inbox-1",
                        "seq": 10,
                        "msg_time": 1547087894783,
                        "sender_id": "XuJinSheng",
                        "sender_name": "张浩",
                        "sender_corp_name": "中技物流",
                        "msg_type": "text",
                        "text": "请安排提货并报价",
                        "media_paths": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            storage = Storage(Path(directory) / "chatlog.db")
            payload = import_archive_inbox(storage, inbox)
            self.assertEqual(payload["imported"], 1)
            self.assertFalse((inbox / "batch.jsonl").exists())
            self.assertTrue((inbox / "done" / "batch.jsonl").exists())
            self.assertGreaterEqual(storage.summary("zhongji")["issue_count"], 1)


class DisplayMapTests(unittest.TestCase):
    def test_loads_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            path.write_text('{"XuJinSheng": "张某 @中技物流"}', encoding="utf-8")
            self.assertEqual(load_display_map(path)["XuJinSheng"], "张某 @中技物流")


if __name__ == "__main__":
    unittest.main()
