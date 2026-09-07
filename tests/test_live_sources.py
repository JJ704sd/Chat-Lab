import json
import os
import sys
from pathlib import Path
import sqlite3
import tempfile
import unittest

from chatlog_assistant.models import Message
from chatlog_assistant.pipeline import import_messages
from chatlog_assistant.sources.cipher_objects import CIPHER_NAME, decode_sqlcipher_blob, xor_repeat
from chatlog_assistant.sources.live import public_probe
from chatlog_assistant.sources.wechat_messages import parse_wechat_messages
from chatlog_assistant.storage import Storage
from chatlog_assistant.subject import SubjectResolver


class CipherObjectTests(unittest.TestCase):
    def test_recovers_27_byte_sqlcipher_mask(self) -> None:
        raw_key = bytes(range(32))
        salt = bytes(range(16, 32))
        plain = ("x'" + raw_key.hex() + salt.hex() + "'").encode("ascii")
        mask = CIPHER_NAME[:27]
        blob = xor_repeat(plain, mask)
        self.assertNotEqual(blob[:2], b"x'")
        parsed = decode_sqlcipher_blob(blob)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed[0], raw_key)
        self.assertEqual(parsed[1], salt)


class DpapiKeyRingTests(unittest.TestCase):
    def test_round_trip_does_not_write_plaintext_key(self) -> None:
        if os.name != "nt":
            self.skipTest("DPAPI is Windows-only")
        from chatlog_assistant.secrets import KeyRecord, KeyRing

        secret = bytes(range(32))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keyring.dpapi"
            ring = KeyRing(path)
            ring.put(KeyRecord("wechat:demo:message", "sqlcipher4-raw", "demo", "message"), secret)
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"CLA1"))
            self.assertNotIn(secret, raw)
            self.assertNotIn(secret.hex().encode("ascii"), raw)
            loaded = ring.get("wechat:demo:message")
            self.assertEqual(bytes(loaded or b""), secret)


class WecomPagekeyTests(unittest.TestCase):
    def test_extracts_raw_key_from_pagekey_buffer(self) -> None:
        import struct

        from chatlog_assistant.sources.wecom import pagekey_buffer_keys

        key = bytes(range(16))
        blob = b"pad" + key + struct.pack("<I", 7) + b"sAlT" + b"tail"
        self.assertIn(key, pagekey_buffer_keys(blob))

    def test_wecom_root_accepts_wxwork_directory(self) -> None:
        from chatlog_assistant.sources.discovery import wecom_root
        from chatlog_assistant.sources.wecom import locate_wecom_targets

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "WXWork"
            work.mkdir()
            self.assertEqual(wecom_root(work), work)
            self.assertEqual(wecom_root(Path(directory)), work)
            account = work / "1688855117808518" / "Data"
            account.mkdir(parents=True)
            db = account / "message.db"
            db.write_bytes(b"\x00" * 4096)
            found = locate_wecom_targets(work)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].account, "1688855117808518")


class Wxsqlite3Tests(unittest.TestCase):
    def test_page_round_trip_and_verify(self) -> None:
        if os.name != "nt" and sys.platform != 'darwin':
            self.skipTest("A Windows or macOS native AES backend is required")
        from chatlog_assistant.sources.wxsqlite3 import decrypt_page, encrypt_page, verify_key

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plain.db"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA page_size=4096")
            connection.execute("CREATE TABLE sample(value TEXT)")
            connection.execute("INSERT INTO sample(value) VALUES ('hello')")
            connection.commit()
            connection.close()
            page = path.read_bytes()[:4096]
            self.assertEqual(len(page), 4096)
            key = bytes(range(16))
            encrypted = encrypt_page(key, page, 1)
            self.assertFalse(encrypted.startswith(b"SQLite format 3"))
            self.assertTrue(verify_key(key, encrypted))
            self.assertFalse(verify_key(bytes(16), encrypted))
            self.assertTrue(decrypt_page(key, encrypted, 1).startswith(b"SQLite format 3"))


class WechatParserAndSubjectTests(unittest.TestCase):
    def test_group_sender_subject_ignores_body_mention(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE Msg_demo(local_id INTEGER, create_time INTEGER, message_content TEXT, real_sender_id INTEGER, local_type INTEGER)"
        )
        connection.execute(
            "INSERT INTO Msg_demo VALUES (1, 1756512000, '@张某 请安排提货并报价', 9, 1)"
        )
        contact = sqlite3.connect(":memory:")
        contact.execute("CREATE TABLE contact(username TEXT, remark TEXT, nick_name TEXT)")
        contact.execute("INSERT INTO contact VALUES ('user9', '王某 @中技', '王某')")
        resource = sqlite3.connect(":memory:")
        resource.execute("CREATE TABLE name2id(user_name TEXT)")
        resource.execute("INSERT INTO name2id(rowid, user_name) VALUES (9, 'user9')")
        messages, cursor = parse_wechat_messages(
            connection,
            contact_conn=contact,
            resource_conn=resource,
            source="wechat",
            account_hint="wxid_self",
        )
        self.assertEqual(len(messages), 1)
        match = SubjectResolver().resolve(messages[0].sender_display)
        self.assertEqual(match.bucket, "zhongji")
        self.assertEqual(match.raw_subject, "中技")
        again, _ = parse_wechat_messages(
            connection,
            contact_conn=contact,
            resource_conn=resource,
            source="wechat",
            account_hint="wxid_self",
            since_cursor=cursor,
        )
        self.assertEqual(again, [])


class IncrementalIdempotencyTests(unittest.TestCase):
    def test_cursor_skips_already_imported_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.db")
            first = [
                Message("wechat", "Msg_demo:1", "g", __import__("datetime").datetime.fromisoformat("2026-08-31T09:00:00+08:00"), "张某 @中技", "请报价"),
            ]
            second = first + [
                Message("wechat", "Msg_demo:2", "g", __import__("datetime").datetime.fromisoformat("2026-08-31T09:05:00+08:00"), "客服 @其他", "预计报价620元"),
            ]
            import_messages(storage, first)
            storage.set_cursor("wechat:demo", json.dumps({"create_time": 1, "message_id": "Msg_demo:1"}))
            import_messages(storage, second)
            import_messages(storage, second)
            summary = storage.summary("zhongji")
            self.assertEqual(summary["issue_count"], 1)
            self.assertEqual(summary["solved_count"], 1)
            self.assertEqual(storage.get_cursor("wechat:demo"), json.dumps({"create_time": 1, "message_id": "Msg_demo:1"}))


class LeakTests(unittest.TestCase):
    def test_public_probe_strips_keys(self) -> None:
        payload = public_probe({"success": True, "keys": {"demo": bytearray(b"secret")}, "databases": []})
        encoded = json.dumps(payload)
        self.assertNotIn("keys", payload)
        self.assertNotIn("secret", encoded)

    def test_source_does_not_print_key_hex(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "chatlog_assistant"
        forbidden = ("print(enc_key", "print(raw_key", "print(key.hex", "print(secret.hex")
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                self.assertNotIn(needle, text, f"{path} contains {needle}")


class CaptureMaterialTests(unittest.TestCase):
    def test_parses_raw_and_sqlcipher_hex_blobs(self) -> None:
        from chatlog_assistant.sources.capture import material_from_blob
        from chatlog_assistant.sources.windows_debug import dr7_execute

        raw = bytes(range(32))
        self.assertEqual(material_from_blob(raw), [raw])
        hexed = ("x'" + raw.hex() + "'").encode("ascii")
        self.assertEqual(len(hexed), 67)
        self.assertIn(raw, material_from_blob(hexed))
        with_salt = ("x'" + raw.hex() + bytes(range(16, 32)).hex() + "'").encode("ascii")
        self.assertEqual(len(with_salt), 99)
        parsed = material_from_blob(with_salt)
        self.assertIn(raw, parsed)
        self.assertEqual(dr7_execute(1), 0x1)
        self.assertEqual(dr7_execute(2), 0x5)

    def test_locates_wechat_set_cipher_key(self) -> None:
        dll = Path(r"C:\Program Files\Tencent\Weixin\4.1.12.55\Weixin.dll")
        if not dll.is_file():
            self.skipTest("Weixin.dll 4.1.12.55 is not installed")
        from chatlog_assistant.sources.pe_locate import locate_wechat_breakpoints

        found = locate_wechat_breakpoints(dll.read_bytes())
        names = {item.name for item in found}
        self.assertIn("set_cipher_key", names)
        self.assertIn("cipher_handle", names)
        by_name = {item.name: item.rva for item in found}
        self.assertEqual(by_name["set_cipher_key"], 0x5DEFB0)
        self.assertEqual(by_name["cipher_handle"], 0x3576230)

    def test_locates_wecom_salt_derive(self) -> None:
        exe = Path(r"C:\Program Files (x86)\WXWork\WXWork.exe")
        if not exe.is_file():
            self.skipTest("WXWork.exe is not installed")
        from chatlog_assistant.sources.pe_locate import locate_wecom_breakpoints

        found = locate_wecom_breakpoints(exe.read_bytes())
        self.assertTrue(found)
        names = {item.name for item in found}
        self.assertIn("wxsqlite3_salt_derive", names)
        self.assertEqual(found[0].bitness, 32)


class WorkspaceSeparationTests(unittest.TestCase):
    def test_wechat_and_wecom_use_different_paths(self) -> None:
        from chatlog_assistant.workspaces import database_path, keyring_path

        self.assertEqual(database_path("wechat"), Path("data/wechat/chatlog.db"))
        self.assertEqual(database_path("wecom"), Path("data/wecom/chatlog.db"))
        self.assertNotEqual(database_path("wechat"), database_path("wecom"))
        self.assertNotEqual(keyring_path("wechat"), keyring_path("wecom"))

    def test_cli_source_selects_workspace(self) -> None:
        from chatlog_assistant.cli import build_parser, _bind_workspace

        args = build_parser().parse_args(["--source", "wecom", "init"])
        self.assertEqual(args.source, "wecom")
        storage, _keyring, source = _bind_workspace(args, "wecom")
        self.assertEqual(source, "wecom")
        self.assertEqual(storage.path, Path("data/wecom/chatlog.db"))

    def test_import_live_reports_workspace_database(self) -> None:
        from chatlog_assistant.sources.live import import_live_sources
        from chatlog_assistant.secrets import KeyRing

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "chatlog.db"
            storage = Storage(db)
            keyring = KeyRing(Path(directory) / "keyring.dpapi")
            result = import_live_sources(storage, keyring, sources=[], platform="wecom")
            self.assertEqual(result["imported"], 0)
            self.assertEqual(result["database"], str(db))
            self.assertEqual(result["sources"], [])


if __name__ == "__main__":
    unittest.main()
