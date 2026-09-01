from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from typing import Any, Iterable

from ..models import Message


CN_TZ = timezone(timedelta(hours=8))
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
TEXT_LOCAL_TYPES = {1, 244813135921, 244813135929}


def _zstd_decompress(data: bytes) -> bytes:
    try:
        import zstandard
    except ImportError:
        try:
            import zstd
        except ImportError:
            return data
        return zstd.decompress(data)
    return zstandard.ZstdDecompressor().decompress(data)


def _read_varint(data: bytes, index: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while index < len(data):
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, index
        shift += 7
        if shift > 63:
            break
    return result, index


def iter_protobuf_strings(data: bytes) -> list[str]:
    strings: list[str] = []
    index = 0
    while index < len(data):
        tag, index = _read_varint(data, index)
        if index >= len(data) and tag == 0:
            break
        wire = tag & 7
        if wire == 0:
            _, index = _read_varint(data, index)
        elif wire == 1:
            index += 8
        elif wire == 5:
            index += 4
        elif wire == 2:
            length, index = _read_varint(data, index)
            chunk = data[index : index + length]
            index += length
            if not chunk:
                continue
            if chunk.startswith(ZSTD_MAGIC):
                chunk = _zstd_decompress(chunk)
            try:
                text = chunk.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if text.isprintable() or any("\u4e00" <= ch <= "\u9fff" for ch in text):
                strings.append(text.strip())
        else:
            break
    return strings


def extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    data = bytes(value)
    if not data:
        return ""
    if data.startswith(ZSTD_MAGIC):
        data = _zstd_decompress(data)
    try:
        text = data.decode("utf-8")
        if "\x00" not in text and (text.isprintable() or any("\u4e00" <= ch <= "\u9fff" for ch in text)):
            return text.strip()
    except UnicodeDecodeError:
        pass
    strings = [item for item in iter_protobuf_strings(data) if item]
    if not strings:
        return ""
    return max(strings, key=len)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _table_names(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [row[0] for row in rows]


def load_contact_names(connection: sqlite3.Connection | None) -> dict[str, str]:
    names: dict[str, str] = {}
    if connection is None:
        return names
    for table in _table_names(connection):
        columns = _columns(connection, table)
        user_col = next((item for item in ("username", "user_name", "userName", "id") if item in columns), None)
        if user_col is None:
            continue
        display_cols = [item for item in ("remark", "nick_name", "nickname", "nickName", "alias") if item in columns]
        if not display_cols:
            continue
        select = ", ".join([user_col, *display_cols])
        try:
            rows = connection.execute(f"SELECT {select} FROM {table}")
        except sqlite3.DatabaseError:
            continue
        for row in rows:
            username = str(row[0] or "")
            display = next((str(value).strip() for value in row[1:] if value), "")
            if username and display:
                names.setdefault(username, display)
    return names


def load_id_names(connection: sqlite3.Connection | None) -> dict[int, str]:
    mapping: dict[int, str] = {}
    if connection is None:
        return mapping
    for table in _table_names(connection):
        lowered = table.casefold()
        if "name2id" not in lowered and "sendername2id" not in lowered and "chatname2id" not in lowered:
            continue
        columns = _columns(connection, table)
        name_col = next((item for item in ("user_name", "username", "name", "userName") if item in columns), None)
        if name_col is None:
            continue
        try:
            rows = connection.execute(f"SELECT rowid, {name_col} FROM {table}")
        except sqlite3.DatabaseError:
            continue
        for rowid, name in rows:
            if name:
                mapping[int(rowid)] = str(name)
    return mapping


def _conversation_id_from_table(table: str, id_names: dict[int, str]) -> str:
    prefix = "Msg_"
    if not table.startswith(prefix):
        return table
    digest = table[len(prefix) :].casefold()
    for username in id_names.values():
        if hashlib.md5(username.encode("utf-8")).hexdigest() == digest:
            return username
    return table


def _sender_display(username: str | None, names: dict[str, str], fallback: str) -> str:
    if username and username in names:
        return names[username]
    if username:
        return username
    return fallback


def parse_wechat_messages(
    message_conn: sqlite3.Connection,
    *,
    contact_conn: sqlite3.Connection | None = None,
    resource_conn: sqlite3.Connection | None = None,
    source: str,
    account_hint: str,
    since_cursor: str | None = None,
) -> tuple[list[Message], str]:
    names = load_contact_names(contact_conn)
    names.update(load_contact_names(message_conn))
    id_names = load_id_names(resource_conn)
    id_names.update(load_id_names(message_conn))
    self_name = names.get(account_hint, account_hint)

    cursor = json.loads(since_cursor) if since_cursor else {}
    min_time = int(cursor.get("create_time", 0))
    min_id = str(cursor.get("message_id", ""))
    messages: list[Message] = []
    max_time = min_time
    max_id = min_id

    for table in _table_names(message_conn):
        if not table.startswith("Msg_") or table.endswith(("_data", "_idx", "_docsize", "_config", "_content")):
            continue
        columns = _columns(message_conn, table)
        time_col = next((item for item in ("create_time", "createTime", "timestamp") if item in columns), None)
        id_col = next((item for item in ("local_id", "localId", "server_id", "serverId") if item in columns), None)
        content_col = next((item for item in ("message_content", "messageContent", "content") if item in columns), None)
        if not time_col or not id_col or not content_col:
            continue
        sender_col = next((item for item in ("real_sender_id", "realSenderId", "sender_id") if item in columns), None)
        type_col = next((item for item in ("local_type", "localType", "type") if item in columns), None)
        conversation = _conversation_id_from_table(table, id_names)
        sql = f"SELECT {id_col}, {time_col}, {content_col}"
        if sender_col:
            sql += f", {sender_col}"
        if type_col:
            sql += f", {type_col}"
        sql += f" FROM {table} WHERE {time_col} >= ? ORDER BY {time_col}, {id_col}"
        try:
            rows = message_conn.execute(sql, (min_time,))
        except sqlite3.DatabaseError:
            continue
        for row in rows:
            source_message_id = f"{table}:{row[0]}"
            create_time = int(row[1] or 0)
            if create_time < min_time or (create_time == min_time and source_message_id <= min_id):
                continue
            local_type = int(row[-1]) if type_col else 1
            content = extract_text(row[2])
            if not content:
                continue
            if type_col and local_type not in TEXT_LOCAL_TYPES and not any("\u4e00" <= ch <= "\u9fff" for ch in content):
                continue
            sender_username = None
            if sender_col:
                sender_id = int(row[3] or 0)
                if sender_id == 2:
                    sender_username = account_hint
                    sender_display = self_name
                else:
                    sender_username = id_names.get(sender_id)
                    sender_display = _sender_display(sender_username, names, f"成员{sender_id}")
            else:
                sender_display = self_name
            messages.append(
                Message(
                    source=source,
                    source_message_id=source_message_id,
                    conversation_id=conversation,
                    sent_at=datetime.fromtimestamp(create_time, CN_TZ),
                    sender_display=sender_display,
                    content=content,
                    direction="outbound" if sender_username == account_hint else "inbound",
                    content_type="text",
                    raw_ref=table,
                )
            )
            if create_time > max_time or (create_time == max_time and source_message_id > max_id):
                max_time = create_time
                max_id = source_message_id
    new_cursor = json.dumps({"create_time": max_time, "message_id": max_id}, separators=(",", ":"))
    return messages, new_cursor
