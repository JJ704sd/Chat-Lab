from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from chatlog_assistant.sources.crypto_native import aes_cbc_decrypt, aes_cbc_encrypt
from chatlog_assistant.sources.wecom_cli import main
from chatlog_assistant.sources.wecom_decrypter import decrypt_and_verify_snapshot
from chatlog_assistant.sources.wecom_pipeline import discover_wecom_accounts, run_single_capture
from chatlog_assistant.sources.wecom_snapshot import capture_consistent_snapshot
from chatlog_assistant.sources.wxsqlite3 import encrypt_page


@unittest.skipUnless(os.name == 'nt' or sys.platform == 'darwin', 'Native AES requires Windows or macOS')
class NativeAesTests(unittest.TestCase):
    def test_nist_cbc_known_answer(self):
        # NIST SP 800-38A, F.2.1: independent expected bytes, no padding.
        key = bytes.fromhex('2b7e151628aed2a6abf7158809cf4f3c')
        iv = bytes.fromhex('000102030405060708090a0b0c0d0e0f')
        plain = bytes.fromhex('6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51')
        encrypted = bytes.fromhex('7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2')
        self.assertEqual(aes_cbc_encrypt(key, iv, plain), encrypted)
        self.assertEqual(aes_cbc_decrypt(key, iv, encrypted), plain)

    def test_encrypted_multipage_sqlite_integrity_and_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'synthetic.db'
            connection = sqlite3.connect(path)
            connection.execute('PRAGMA page_size=4096')
            connection.execute('CREATE TABLE sample(body TEXT)')
            body = 'local synthetic verification ' * 700
            connection.execute('INSERT INTO sample VALUES (?)', (body,))
            connection.commit()
            connection.close()
            original = path.read_bytes()
            self.assertGreater(len(original), 4096)
            key = bytes(range(16))
            path.write_bytes(b''.join(encrypt_page(key, original[offset:offset + 4096], offset // 4096 + 1)
                                      for offset in range(0, len(original), 4096)))
            snapshot = capture_consistent_snapshot(path, 'synthetic-only')
            decoded, validation = decrypt_and_verify_snapshot(snapshot, key)
            self.assertTrue(validation.is_valid, validation.error)
            self.assertTrue(validation.integrity_ok)
            connection = sqlite3.connect(':memory:')
            connection.deserialize(decoded)
            self.assertEqual(connection.execute('SELECT body FROM sample').fetchone()[0], body)
            connection.close()
            decoded, validation = decrypt_and_verify_snapshot(snapshot, bytes(16))
            self.assertIsNone(decoded)
            self.assertFalse(validation.is_valid)


class MacDiscoveryTests(unittest.TestCase):
    def test_discovers_mac_and_windows_layouts_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / ('A' * 32)
            folder = profile / 'Messages1'
            folder.mkdir(parents=True)
            (folder / 'Info.db').write_bytes(bytes(4096))
            (folder / 'Info.db-wal').write_bytes(b'synthetic-wal')
            (folder / 'Session.db').write_bytes(bytes(4096))
            windows = root / '1688855117808518' / 'Data'
            windows.mkdir(parents=True)
            (windows / 'message.db').write_bytes(bytes(4096))
            (root / 'settings').mkdir()
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            accounts = discover_wecom_accounts(root)
            self.assertEqual(len(accounts), 2)
            mac = next(a for a in accounts if a.get('platform') == 'macos')
            self.assertFalse(mac['capture_ready'])
            self.assertTrue(mac['has_session_db'])
            self.assertEqual(mac['databases'][0]['wal_size'], len(b'synthetic-wal'))
            self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_capture_blocks_before_writing_or_accessing_keyring(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / ('B' * 32)
            (profile / 'Messages1').mkdir(parents=True)
            (profile / 'Messages1/Info.db').write_bytes(bytes(4096))
            output = root / 'analysis' / 'analysis.db'
            with patch('chatlog_assistant.sources.wecom_pipeline.KeyRing', side_effect=AssertionError('DPAPI called')):
                result = run_single_capture(profile.name, root, output)
            self.assertEqual(result['error_code'], 'macos_capture_not_ready')
            self.assertFalse(result['success'])
            self.assertFalse(result['retryable'])
            self.assertFalse(output.parent.exists())

    def test_watch_stops_on_permanent_capture_blocker(self):
        result = {'success': False, 'retryable': False, 'error_code': 'macos_capture_not_ready'}
        with patch('chatlog_assistant.sources.wecom_cli.run_single_capture', return_value=result) as capture:
            with patch('chatlog_assistant.sources.wecom_cli.time.sleep', side_effect=AssertionError('must not poll')):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = main(['watch', '--account-id', 'B' * 32])
        self.assertEqual(code, 1)
        self.assertEqual(capture.call_count, 1)
        self.assertIn('macos_capture_not_ready', output.getvalue())
