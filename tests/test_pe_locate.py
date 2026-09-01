import unittest
from pathlib import Path

from chatlog_assistant.sources.capture import material_from_blob
from chatlog_assistant.sources.pe_locate import pe_export_lookup, pe_export_rva


class PeExportTests(unittest.TestCase):
    def test_resolves_kernel32_export(self) -> None:
        path = Path(r"C:\Windows\System32\kernel32.dll")
        if not path.is_file():
            self.skipTest("kernel32.dll is not available")
        rva = pe_export_rva(path.read_bytes(), "GetCurrentProcess")
        self.assertIsNotNone(rva)
        self.assertGreater(rva, 0)

    def test_wow64_kernel32_export(self) -> None:
        path = Path(r"C:\Windows\SysWOW64\kernel32.dll")
        if not path.is_file():
            self.skipTest("SysWOW64 kernel32.dll is not available")
        rva = pe_export_rva(path.read_bytes(), "GetCurrentProcess")
        self.assertIsNotNone(rva)
        self.assertGreater(rva, 0)

    def test_bcrypt_hashdata_is_code_or_forwarder(self) -> None:
        path = Path(r"C:\Windows\SysWOW64\bcrypt.dll")
        if not path.is_file():
            path = Path(r"C:\Windows\System32\bcrypt.dll")
        if not path.is_file():
            self.skipTest("bcrypt.dll is not available")
        rva, forward = pe_export_lookup(path.read_bytes(), "BCryptHashData")
        self.assertTrue(rva or forward)
        if forward:
            self.assertIn("BCryptHashData", forward)


class CaptureMaterialTests(unittest.TestCase):
    def test_pagekey_buffer_ending_with_salt(self) -> None:
        raw = bytes(range(16))
        blob = raw + (1).to_bytes(4, "little") + b"sAlT"
        self.assertIn(raw, material_from_blob(blob))


if __name__ == "__main__":
    unittest.main()
