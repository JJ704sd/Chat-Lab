from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import ctypes
from ctypes import wintypes

from .sources.windows_memory import zero_secret


_CRYPTPROTECT_UI_FORBIDDEN = 0x01
_APP_ENTROPY = b"chatlog-assistant-keyring-v1"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


@dataclass(frozen=True, slots=True)
class KeyRecord:
    key_id: str
    kind: str
    account: str
    database: str


def _crypt32():
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    return crypt32


def _blob_from_bytes(data: bytes) -> DATA_BLOB:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    blob._buffer = buffer  # keep alive
    return blob


def _bytes_from_blob(blob: DATA_BLOB) -> bytes:
    if not blob.cbData or not blob.pbData:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free_blob(blob: DATA_BLOB) -> None:
    if blob.pbData:
        ctypes.windll.kernel32.LocalFree(blob.pbData)
        blob.pbData = None
        blob.cbData = 0


def dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    crypt32 = _crypt32()
    entropy = _blob_from_bytes(_APP_ENTROPY)
    source = _blob_from_bytes(data)
    output = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(source),
        "chatlog-assistant",
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    try:
        return _bytes_from_blob(output)
    finally:
        _free_blob(output)


def dpapi_unprotect(data: bytes) -> bytearray:
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    crypt32 = _crypt32()
    entropy = _blob_from_bytes(_APP_ENTROPY)
    source = _blob_from_bytes(data)
    output = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return bytearray(_bytes_from_blob(output))
    finally:
        _free_blob(output)


class KeyRing:
    """DPAPI-protected local key store. Never writes raw keys as plaintext."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, object]:
        if not self.path.is_file():
            return {"records": {}, "blob": None}
        raw = self.path.read_bytes()
        if not raw.startswith(b"CLA1"):
            raise ValueError("unrecognized keyring format")
        header_len = int.from_bytes(raw[4:8], "little")
        meta = json.loads(raw[8 : 8 + header_len].decode("utf-8"))
        blob = raw[8 + header_len :]
        return {"records": meta.get("records", {}), "blob": blob}

    def _save(self, records: dict[str, dict[str, str]], payload: bytes) -> None:
        protected = dpapi_protect(payload)
        meta = json.dumps({"records": records}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = b"CLA1" + len(meta).to_bytes(4, "little") + meta + protected
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.path)

    def list_records(self) -> list[KeyRecord]:
        loaded = self._load()
        records = loaded["records"]
        return [
            KeyRecord(
                key_id=key_id,
                kind=str(item.get("kind", "")),
                account=str(item.get("account", "")),
                database=str(item.get("database", "")),
            )
            for key_id, item in records.items()
        ]

    def put(self, record: KeyRecord, secret: bytes) -> None:
        loaded = self._load()
        mapping: dict[str, str] = {}
        blob = loaded["blob"]
        if blob:
            unlocked = dpapi_unprotect(blob)
            try:
                mapping = json.loads(bytes(unlocked).decode("utf-8"))
            finally:
                zero_secret(unlocked)
        mapping[record.key_id] = secret.hex()
        payload = json.dumps(mapping, separators=(",", ":")).encode("utf-8")
        records = dict(loaded["records"])
        records[record.key_id] = {
            "kind": record.kind,
            "account": record.account,
            "database": record.database,
        }
        try:
            self._save(records, payload)
        finally:
            payload = b"\x00" * len(payload)

    def get(self, key_id: str) -> bytearray | None:
        loaded = self._load()
        blob = loaded["blob"]
        if not blob or key_id not in loaded["records"]:
            return None
        unlocked = dpapi_unprotect(blob)
        try:
            mapping = json.loads(bytes(unlocked).decode("utf-8"))
            value = mapping.get(key_id)
            if not value:
                return None
            return bytearray.fromhex(value)
        finally:
            zero_secret(unlocked)
