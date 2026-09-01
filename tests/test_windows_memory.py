import unittest
from pathlib import Path
import tempfile

from chatlog_assistant.sources.windows_memory import (
    extract_candidate_keys,
    extract_hex_secrets,
    extract_matching_keys,
    zero_secret,
)
from chatlog_assistant.sources.wechat_nt import verify_sqlcipher4_raw_key


class WindowsMemoryScannerTests(unittest.TestCase):
    def test_extracts_only_requested_salt(self) -> None:
        wanted_salt = bytes.fromhex("11" * 16)
        other_salt = bytes.fromhex("22" * 16)
        wanted_key = "aa" * 32
        other_key = "bb" * 32
        data = (
            b"prefix x'" + wanted_key.encode() + wanted_salt.hex().encode() + b"' "
            + b"x'" + other_key.encode() + other_salt.hex().encode() + b"' suffix"
        )
        matches = extract_matching_keys(data, [wanted_salt])
        self.assertEqual(bytes(matches[wanted_salt]), bytes.fromhex(wanted_key))
        self.assertNotIn(other_salt, matches)
        zero_secret(matches[wanted_salt])
        self.assertEqual(bytes(matches[wanted_salt]), bytes(32))

    def test_extracts_relaxed_and_binary_adjacent_candidates(self) -> None:
        salt = bytes.fromhex("33" * 16)
        hex_key = bytes.fromhex("44" * 32)
        binary_key = bytes(range(32))
        data = b"prefix" + hex_key.hex().encode() + salt.hex().encode() + b"middle" + binary_key + salt + b"suffix"
        candidates = extract_candidate_keys(data, [salt])
        candidate_values = {bytes(item) for item in candidates[salt]}
        self.assertIn(hex_key, candidate_values)
        self.assertIn(binary_key, candidate_values)

    def test_extracts_ascii_and_utf16_hex_secrets(self) -> None:
        first = "5a" * 32
        second = "b7" * 32
        utf16 = second.encode("utf-16le")
        data = b"nothex:" + first.encode("ascii") + b";wide:" + utf16 + b"\x00"
        candidates = {bytes(item) for item in extract_hex_secrets(data)}
        self.assertEqual(candidates, {bytes.fromhex(first), bytes.fromhex(second)})

    def test_ignores_hex_substrings_and_zero_secret(self) -> None:
        candidate = "ab" * 32
        data = b"f" + candidate.encode("ascii") + b"e " + ("00" * 32).encode("ascii")
        self.assertEqual(extract_hex_secrets(data), [])

    def test_verifies_sqlcipher4_raw_key(self) -> None:
        from sqlcipher3 import dbapi2 as sqlcipher

        raw_key = bytes.fromhex("19" * 32)
        salt = bytes.fromhex("2a" * 16)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "encrypted.db"
            connection = sqlcipher.connect(path)
            connection.execute(f"PRAGMA key=\"x'{raw_key.hex() + salt.hex()}'\"")
            connection.execute("CREATE TABLE sample(value TEXT)")
            connection.commit()
            connection.close()
            first_page = path.read_bytes()[:4096]
        self.assertTrue(verify_sqlcipher4_raw_key(first_page, raw_key))
        self.assertFalse(verify_sqlcipher4_raw_key(first_page, bytes.fromhex("20" * 32)))


if __name__ == "__main__":
    unittest.main()
