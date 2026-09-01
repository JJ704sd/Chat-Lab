from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import struct
from typing import Any

from ..models import Message
from .discovery import wecom_root
from .wechat_messages import extract_text
from .windows_memory import (
    is_wow64_process,
    iter_readable_chunks,
    list_processes,
    read_process_memory,
    zero_secret,
)
from .wxsqlite3 import SQLITE_HEADER, decrypt_database_bytes, has_wxsqlite3_header_shape, is_plain_sqlite, verify_key


CN_TZ = timezone(timedelta(hours=8))
PAGEKEY_TAG = b"sAlT"
CIPHER_KEY_OFFSETS = tuple(range(8, 64, 4))
IMPORT_KINDS = ("message", "user", "session")
WECOM_PROCESS_NAMES = ("WXWork.exe",)


@dataclass(frozen=True, slots=True)
class WecomTarget:
    name: str
    account: str
    kind: str
    path: Path
    page_one: bytes


def locate_wecom_targets(
    documents: str | Path | None = None,
    *,
    include_all: bool = False,
) -> list[WecomTarget]:
    work_root = wecom_root(documents)
    targets: list[WecomTarget] = []
    if not work_root.is_dir():
        return targets
    for account in sorted(path for path in work_root.iterdir() if path.is_dir() and path.name.isdigit()):
        paths: list[tuple[str, Path]] = []
        if include_all:
            seen: set[Path] = set()
            for folder in (account / "Data", account / "Index"):
                if not folder.is_dir():
                    continue
                for path in sorted(folder.glob("*.db")):
                    resolved = path.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    paths.append((path.stem, path))
        else:
            for kind in IMPORT_KINDS:
                paths.append((kind, account / "Data" / f"{kind}.db"))
        for kind, path in paths:
            if not path.is_file():
                continue
            with path.open("rb") as handle:
                page_one = handle.read(4096)
            if len(page_one) == 4096:
                targets.append(WecomTarget(f"wecom:{account.name}:{kind}", account.name, kind, path, page_one))
    return targets


def pagekey_buffer_keys(data: bytes) -> list[bytes]:
    """Extract 16-byte raw keys from wxSQLite3 page-key material: key + page_no + 'sAlT'."""
    found: list[bytes] = []
    start = 0
    while True:
        index = data.find(PAGEKEY_TAG, start)
        if index < 0:
            break
        if index >= 20:
            page_number = struct.unpack_from("<I", data, index - 4)[0]
            if 1 <= page_number <= 16_777_216:
                key = bytes(data[index - 20 : index - 4])
                if _plausible_key(key):
                    found.append(key)
        start = index + 1
    return found


def _plausible_key(raw: bytes) -> bool:
    return len(raw) == 16 and raw != bytes(16) and len(set(raw)) >= 6


def list_wecom_processes() -> list:
    seen: set[int] = set()
    processes = []
    for name in WECOM_PROCESS_NAMES:
        for item in list_processes(name):
            if item.pid in seen:
                continue
            seen.add(item.pid)
            processes.append(item)
    return processes


def _import_pending(remaining: dict[str, WecomTarget]) -> bool:
    return any(target.kind in IMPORT_KINDS for target in remaining.values())


def _collect_key_windows(blob: bytes, *, step: int = 4) -> list[bytes]:
    found: list[bytes] = []
    seen: set[bytes] = set()
    for offset in range(0, max(len(blob) - 15, 0), step):
        key = bytes(blob[offset : offset + 16])
        if key in seen or not _plausible_key(key):
            continue
        seen.add(key)
        found.append(key)
    return found


def _try_keys(
    keys: list[bytes],
    remaining: dict[str, WecomTarget],
    matched: dict[str, bytearray],
    tables: dict[str, list[str]],
    method: str,
    stats: dict[str, int],
) -> bool:
    for key in keys:
        stats["key_tests"] = int(stats.get("key_tests") or 0) + 1
        _record_key(remaining, key, matched, tables, method)
        if not _import_pending(remaining):
            return True
    return False


def _open_plain(plaintext: bytes) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.deserialize(plaintext)
    connection.row_factory = sqlite3.Row
    return connection


def _verify_sqlite_master(target: WecomTarget, key: bytes) -> list[str]:
    wal = Path(str(target.path) + "-wal")
    wal_bytes = wal.read_bytes() if wal.is_file() else None
    plaintext = decrypt_database_bytes(target.path.read_bytes(), key, wal_bytes=wal_bytes)
    connection = _open_plain(plaintext)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        connection.close()


def _record_key(
    remaining: dict[str, WecomTarget],
    key: bytes,
    matched: dict[str, bytearray],
    tables: dict[str, list[str]],
    method: str,
) -> str | None:
    hit = None
    for name, target in list(remaining.items()):
        if not verify_key(key, target.page_one):
            continue
        try:
            table_names = _verify_sqlite_master(target, key)
        except Exception:
            continue
        if not table_names:
            continue
        matched[name] = bytearray(key)
        tables[name] = table_names
        remaining.pop(name, None)
        hit = method
    return hit


def _scan_hex_keys(pid: int, remaining: dict[str, WecomTarget], matched: dict[str, bytearray], tables: dict[str, list[str]]) -> dict[str, int]:
    stats = {"regions": 0, "candidates": 0}
    for _address, data in iter_readable_chunks(pid, writable_only=True, overlap=8):
        stats["regions"] += 1
        start = 0
        while _import_pending(remaining):
            index = data.find(b"x'", start)
            if index < 0:
                break
            end = data.find(b"'", index + 2)
            if 34 <= end - index <= 194:
                inner = data[index + 2 : end]
                if all(chr(value).isalnum() for value in inner) and len(inner) in {32, 64}:
                    stats["candidates"] += 1
                    try:
                        key = bytes.fromhex(inner[:32].decode("ascii"))
                    except ValueError:
                        start = index + 2
                        continue
                    if len(key) == 16:
                        _record_key(remaining, key, matched, tables, "wxwork_hex")
            start = index + 2
        if not _import_pending(remaining):
            break
    return stats


def _scan_decrypted_pages(
    pid: int,
    remaining: dict[str, WecomTarget],
    matched: dict[str, bytearray],
    tables: dict[str, list[str]],
) -> dict[str, int]:
    stats = {"regions": 0, "headers": 0, "pointers": 0, "key_tests": 0}
    headers: list[int] = []
    regions: list[tuple[int, bytes]] = []
    for address, data in iter_readable_chunks(pid, writable_only=True, overlap=16, chunk_limit=1024 * 1024):
        stats["regions"] += 1
        regions.append((address, data))
        start = 0
        while True:
            index = data.find(SQLITE_HEADER, start)
            if index < 0:
                break
            headers.append(address + index)
            start = index + 1
    stats["headers"] = len(headers)
    if not headers:
        return stats
    header_set = {item & 0xFFFFFFFF for item in headers}
    seen: set[bytes] = set()
    for address, data in regions:
        for header in headers:
            if address <= header < address + len(data):
                offset = header - address
                window = data[max(0, offset - 128) : offset + 64]
                for key in _collect_key_windows(window):
                    if key in seen:
                        continue
                    seen.add(key)
                    if _try_keys([key], remaining, matched, tables, "wxwork_sqlite_page", stats):
                        return stats
        for offset in range(0, len(data) - 3, 4):
            pointer = struct.unpack_from("<I", data, offset)[0]
            if pointer not in header_set:
                continue
            stats["pointers"] += 1
            window = data[max(0, offset - 96) : offset + 96]
            for key in _collect_key_windows(window):
                if key in seen:
                    continue
                seen.add(key)
                if _try_keys([key], remaining, matched, tables, "wxwork_sqlite_page", stats):
                    return stats
            if not _import_pending(remaining):
                return stats
    return stats


def _scan_cipher_structs(
    pid: int,
    remaining: dict[str, WecomTarget],
    matched: dict[str, bytearray],
    tables: dict[str, list[str]],
    *,
    max_seconds: float = 90,
) -> dict[str, int]:
    import time

    stats = {"checked": 0, "ptr_hits": 0, "chain_hits": 0, "key_tests": 0}
    if not is_wow64_process(pid):
        stats["skipped"] = 1
        return stats
    started = time.monotonic()
    regions: list[tuple[int, bytes]] = []
    total = 0
    for address, data in iter_readable_chunks(pid, writable_only=True, overlap=0, chunk_limit=1024 * 1024):
        if total + len(data) > 256 * 1024 * 1024:
            break
        regions.append((address, data))
        total += len(data)

    def valid_ptr(addr: int, length: int = 4) -> bool:
        for base, data in regions:
            if base <= addr and addr + length <= base + len(data):
                return True
        return False

    def read_u32(addr: int) -> int | None:
        for base, data in regions:
            if base <= addr and addr + 4 <= base + len(data):
                return struct.unpack_from("<I", data, addr - base)[0]
        raw = read_process_memory(pid, addr, 4)
        if raw and len(raw) == 4:
            return struct.unpack("<I", raw)[0]
        return None

    page_sizes = {512, 1024, 2048, 4096, 8192, 16384, 32768, 65536}
    for base, data in regions:
        max_off = len(data) - 0x40
        for off in range(0, max_off, 4):
            if time.monotonic() - started > max_seconds:
                stats["timeout"] = 1
                return stats
            stats["checked"] += 1
            flag0, flag4 = struct.unpack_from("<II", data, off)
            if flag0 not in (1, 2) or flag4 not in (1, 2, 4096, 8192, 16384):
                continue
            cipher_addr = base + off
            aes_ctx = struct.unpack_from("<I", data, off + 0x2C)[0]
            if not valid_ptr(aes_ctx, 0x40):
                continue
            stats["ptr_hits"] += 1
            holder = read_u32(cipher_addr + 0x30)
            if holder is None or not valid_ptr(holder, 8):
                continue
            page_obj = read_u32(holder + 4)
            if page_obj is None:
                continue
            page_size = read_u32(page_obj + 0x24)
            if page_size not in page_sizes:
                continue
            stats["chain_hits"] += 1
            candidates: list[bytes] = []
            for key_off in CIPHER_KEY_OFFSETS:
                if off + key_off + 16 <= len(data):
                    candidates.append(bytes(data[off + key_off : off + key_off + 16]))
            aes_blob = read_process_memory(pid, aes_ctx, 80) or b""
            for key_off in range(0, max(len(aes_blob) - 15, 0), 8):
                candidates.append(bytes(aes_blob[key_off : key_off + 16]))
            seen: set[bytes] = set()
            for enc_key in candidates:
                if enc_key in seen or not _plausible_key(enc_key):
                    continue
                seen.add(enc_key)
                stats["key_tests"] += 1
                _record_key(remaining, enc_key, matched, tables, "wxwork_cipher_struct")
                if not _import_pending(remaining):
                    return stats
    return stats


def _scan_pagekey_buffers(
    pid: int,
    remaining: dict[str, WecomTarget],
    matched: dict[str, bytearray],
    tables: dict[str, list[str]],
) -> dict[str, int]:
    stats = {"regions": 0, "tags": 0, "candidates": 0}
    seen: set[bytes] = set()
    for _address, data in iter_readable_chunks(pid, writable_only=True, overlap=24):
        stats["regions"] += 1
        for key in pagekey_buffer_keys(data):
            stats["tags"] += 1
            if key in seen:
                continue
            seen.add(key)
            stats["candidates"] += 1
            _record_key(remaining, key, matched, tables, "wxwork_pagekey_salt")
            if not _import_pending(remaining):
                return stats
    return stats


def probe_wecom(
    documents: str | Path | None = None,
    pid: int | None = None,
    *,
    retain_keys: bool = False,
) -> dict[str, Any]:
    targets = locate_wecom_targets(documents, include_all=True)
    if not targets:
        return {"success": False, "error": "未找到企微 Data/message.db"}

    formats = {}
    for target in targets:
        if is_plain_sqlite(target.page_one):
            formats[target.name] = "sqlite"
        elif has_wxsqlite3_header_shape(target.page_one):
            formats[target.name] = "wxsqlite3-aes128"
        else:
            formats[target.name] = "unknown"

    remaining = {target.name: target for target in targets if formats[target.name] == "wxsqlite3-aes128"}
    matched: dict[str, bytearray] = {}
    tables: dict[str, list[str]] = {}
    scan_stats: list[dict[str, int | str]] = []
    processes = list_wecom_processes()
    if pid is not None:
        processes = [item for item in processes if item.pid == pid]
    try:
        for process in processes:
            if not _import_pending(remaining):
                break
            hex_stats = _scan_hex_keys(process.pid, remaining, matched, tables)
            hex_stats.update({"pid": process.pid, "mode": "wxwork_hex", "wow64": int(is_wow64_process(process.pid))})
            scan_stats.append(hex_stats)
            if _import_pending(remaining):
                salt_stats = _scan_pagekey_buffers(process.pid, remaining, matched, tables)
                salt_stats.update({"pid": process.pid, "mode": "wxwork_pagekey_salt"})
                scan_stats.append(salt_stats)
            if _import_pending(remaining):
                page_stats = _scan_decrypted_pages(process.pid, remaining, matched, tables)
                page_stats.update({"pid": process.pid, "mode": "wxwork_sqlite_page"})
                scan_stats.append(page_stats)
            if _import_pending(remaining):
                struct_stats = _scan_cipher_structs(process.pid, remaining, matched, tables)
                struct_stats.update({"pid": process.pid, "mode": "wxwork_cipher_struct"})
                scan_stats.append(struct_stats)
    finally:
        pass

    databases = []
    for target in targets:
        item = {
            "database": target.name,
            "account": target.account,
            "format": formats[target.name],
            "matched": target.name in matched,
            "verified": target.name in tables,
        }
        if target.name in tables:
            item["table_count"] = len(tables[target.name])
            item["tables"] = tables[target.name][:20]
            item["method"] = "wxsqlite3_memory_context"
        databases.append(item)
    payload = {
        "success": any(
            item.get("verified") and item.get("database", "").endswith(":message")
            for item in databases
        ),
        "process_found": bool(processes),
        "scan_stats": scan_stats,
        "databases": databases,
    }
    if retain_keys:
        payload["keys"] = matched
    else:
        zero_keys(matched)
    return payload


def recover_wecom_keys(
    documents: str | Path | None = None,
    pid: int | None = None,
) -> tuple[dict[str, bytearray], dict[str, Any]]:
    probe = probe_wecom(documents, pid, retain_keys=True)
    keys: dict[str, bytearray] = probe.pop("keys", {})
    return keys, probe


def _lookup_wecom_user(connection: sqlite3.Connection, user_id: str) -> str:
    for table in ("user", "User", "user_table", "contact"):
        try:
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        except sqlite3.DatabaseError:
            continue
        id_col = next((item for item in ("user_id", "id", "uin", "username") if item in columns), None)
        if id_col is None:
            continue
        name_cols = [item for item in ("name", "nick_name", "nickname", "realname", "remark") if item in columns]
        corp_cols = [item for item in ("corp_name", "corpname", "english_name", "party_name", "company") if item in columns]
        if not name_cols:
            continue
        select = ", ".join([id_col, *name_cols, *corp_cols])
        try:
            row = connection.execute(f"SELECT {select} FROM {table} WHERE {id_col}=?", (user_id,)).fetchone()
        except sqlite3.DatabaseError:
            continue
        if not row:
            continue
        name = next((str(value).strip() for value in row[1 : 1 + len(name_cols)] if value), "")
        corp = next((str(value).strip() for value in row[1 + len(name_cols) :] if value), "")
        if name and corp and not name.endswith("@" + corp):
            return f"{name} @{corp}"
        if name:
            return name
    return user_id


def parse_wecom_messages(
    message_conn: sqlite3.Connection,
    *,
    user_conn: sqlite3.Connection | None = None,
    source: str,
    since_cursor: str | None = None,
) -> tuple[list[Message], str]:
    cursor = json.loads(since_cursor) if since_cursor else {}
    min_seq = int(cursor.get("sequence", 0))
    min_id = str(cursor.get("message_id", ""))
    messages: list[Message] = []
    max_seq = min_seq
    max_id = min_id
    table = next(
        (
            name
            for name in ("message_table", "message", "kf_message_tableV1")
            if message_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
        ),
        None,
    )
    if table is None:
        return [], json.dumps({"sequence": max_seq, "message_id": max_id}, separators=(",", ":"))
    columns = {row[1] for row in message_conn.execute(f"PRAGMA table_info({table})")}
    id_col = next((item for item in ("message_id", "id", "server_id") if item in columns), None)
    seq_col = next((item for item in ("sequence", "seq", "send_time") if item in columns), None)
    time_col = next((item for item in ("send_time", "create_time", "time") if item in columns), None)
    sender_col = next((item for item in ("sender_id", "sender", "from_id") if item in columns), None)
    conv_col = next((item for item in ("conversation_id", "conversation", "room_id") if item in columns), None)
    content_col = next((item for item in ("content", "message", "extra_content") if item in columns), None)
    if not all([id_col, time_col, content_col, conv_col]):
        return [], json.dumps({"sequence": max_seq, "message_id": max_id}, separators=(",", ":"))
    order_col = seq_col or time_col
    sql = f"SELECT {id_col}, {time_col}, {content_col}, {conv_col}"
    if sender_col:
        sql += f", {sender_col}"
    if seq_col and seq_col != time_col:
        sql += f", {seq_col}"
    sql += f" FROM {table} WHERE {order_col} >= ? ORDER BY {order_col}, {id_col}"
    for row in message_conn.execute(sql, (min_seq,)):
        message_id = str(row[0])
        sent_raw = int(row[1] or 0)
        sequence = int(row[-1] or sent_raw) if seq_col else sent_raw
        if sequence < min_seq or (sequence == min_seq and message_id <= min_id):
            continue
        content = extract_text(row[2])
        if not content:
            continue
        sender_id = str(row[4]) if sender_col else ""
        sender_display = _lookup_wecom_user(user_conn, sender_id) if user_conn and sender_id else (sender_id or "未知")
        if sent_raw > 10**12:
            sent_at = datetime.fromtimestamp(sent_raw / 1000, CN_TZ)
        else:
            sent_at = datetime.fromtimestamp(sent_raw, CN_TZ)
        messages.append(
            Message(
                source=source,
                source_message_id=message_id,
                conversation_id=str(row[3]),
                sent_at=sent_at,
                sender_display=sender_display,
                content=content,
                direction="unknown",
                content_type="text",
                raw_ref=table,
            )
        )
        if sequence > max_seq or (sequence == max_seq and message_id > max_id):
            max_seq = sequence
            max_id = message_id
    return messages, json.dumps({"sequence": max_seq, "message_id": max_id}, separators=(",", ":"))


def zero_keys(keys: dict[str, bytearray]) -> None:
    for secret in keys.values():
        zero_secret(secret)
