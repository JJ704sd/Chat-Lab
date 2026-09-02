from __future__ import annotations

import re
import struct
from typing import Any


CONTENT_TYPE_MAP = {
    0: "文本",
    2: "文本",
    14: "图片",
    15: "语音",
    16: "视频",
    17: "文件",
    19: "位置",
    29: "表情",
    31: "链接卡片",
    32: "小程序",
    132: "系统通知",
    1011: "系统消息",
}


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
        if shift > 63:
            break
    return val, pos


def parse_protobuf_fields(data: bytes) -> list[tuple[int, int, Any]]:
    """Parse protobuf payload into [(field_num, wire_type, payload), ...]."""
    fields: list[tuple[int, int, Any]] = []
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _read_varint(data, pos)
            field_num = tag >> 3
            wire_type = tag & 0x07
            if wire_type == 0:  # varint
                val, pos = _read_varint(data, pos)
                fields.append((field_num, 0, val))
            elif wire_type == 2:  # length-delimited
                length, pos = _read_varint(data, pos)
                if pos + length > len(data):
                    break
                payload = data[pos : pos + length]
                pos += length
                fields.append((field_num, 2, payload))
            elif wire_type == 5:  # fixed32
                if pos + 4 > len(data):
                    break
                val = struct.unpack("<I", data[pos : pos + 4])[0]
                pos += 4
                fields.append((field_num, 5, val))
            elif wire_type == 1:  # fixed64
                if pos + 8 > len(data):
                    break
                val = struct.unpack("<Q", data[pos : pos + 8])[0]
                pos += 8
                fields.append((field_num, 1, val))
            else:
                break
        except Exception:
            break
    return fields


def pb_get(fields: list[tuple[int, int, Any]], field_num: int, wire_type: int | None = None) -> Any | None:
    for fn, wt, val in fields:
        if fn == field_num:
            if wire_type is None or wt == wire_type:
                return val
    return None


def try_decode_utf8(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8")
        if "\x00" not in text and all(c >= " " or c in "\n\r\t" for c in text):
            return text.strip()
    except Exception:
        pass
    return None


def extract_text_from_pb(raw: bytes) -> tuple[str, str | None]:
    """Extract message text and potential reply-to reference from Protobuf.

    Returns: (text, reply_to_hint)
    """
    text = try_decode_utf8(raw)
    if text:
        return text, None

    outer = parse_protobuf_fields(raw)
    reply_ref = None
    extracted_texts = []

    # Iterate ALL field 1 occurrences (repeated field 1)
    for fn, wt, f_data in outer:
        if fn == 1 and wt == 2 and isinstance(f_data, bytes):
            inner = parse_protobuf_fields(f_data)
            # Check reply
            f3_reply = pb_get(inner, 3, 2) or pb_get(inner, 4, 2)
            if f3_reply and isinstance(f3_reply, bytes):
                reply_inner = parse_protobuf_fields(f3_reply)
                for _rfn, rwt, rval in reply_inner:
                    if rwt == 2 and isinstance(rval, bytes):
                        r_txt = try_decode_utf8(rval)
                        if r_txt:
                            reply_ref = r_txt
                            break

            f2_data = pb_get(inner, 2, 2)
            if f2_data and isinstance(f2_data, bytes):
                inner2 = parse_protobuf_fields(f2_data)
                text_data = pb_get(inner2, 1, 2)
                if text_data and isinstance(text_data, bytes):
                    text_dec = try_decode_utf8(text_data)
                    if text_dec:
                        extracted_texts.append(text_dec)
                else:
                    text_dec = try_decode_utf8(f2_data)
                    if text_dec:
                        extracted_texts.append(text_dec)

    if extracted_texts:
        return "\n".join(extracted_texts), reply_ref

    # Structure 2: outer.6.2 (system notification)
    f6_data = pb_get(outer, 6, 2)
    if f6_data and isinstance(f6_data, bytes):
        inner6 = parse_protobuf_fields(f6_data)
        for _fn, wt, val in inner6:
            if wt == 2 and isinstance(val, bytes):
                text_dec = try_decode_utf8(val)
                if text_dec and len(text_dec) > 3:
                    return text_dec, reply_ref
        text_dec = try_decode_utf8(f6_data)
        if text_dec:
            return text_dec, reply_ref

    return "", reply_ref


def extract_link_from_pb(raw: bytes) -> tuple[str, str]:
    outer = parse_protobuf_fields(raw)
    f1_data = pb_get(outer, 1, 2)
    title, url = "", ""
    if f1_data and isinstance(f1_data, bytes):
        inner = parse_protobuf_fields(f1_data)
        f1_title = pb_get(inner, 1, 2)
        if f1_title and isinstance(f1_title, bytes):
            title = try_decode_utf8(f1_title) or ""
        f5_url = pb_get(inner, 5, 2)
        if f5_url and isinstance(f5_url, bytes):
            url = try_decode_utf8(f5_url) or ""
    return title, url


def extract_sticker_text(raw: bytes) -> str:
    outer = parse_protobuf_fields(raw)
    f9 = pb_get(outer, 9, 2)
    if f9 and isinstance(f9, bytes):
        desc = try_decode_utf8(f9)
        if desc:
            return desc
    f1 = pb_get(outer, 1, 2)
    if f1 and isinstance(f1, bytes):
        desc = try_decode_utf8(f1)
        if desc:
            return desc
    return ""


def to_raw_bytes(val: Any) -> bytes:
    if val is None:
        return b""
    if isinstance(val, bytes):
        return val
    if isinstance(val, str):
        try:
            return val.encode("utf-8")
        except Exception:
            return val.encode("latin-1", errors="replace")
    return bytes(val)


def parse_wecom_content(content_type: int, raw_content: Any) -> tuple[str, str, str]:
    """Parses WeCom raw message content into (display_text, message_type, parse_status).

    parse_status: 'parsed', 'unparsed_media', 'unknown_format'
    """
    raw = to_raw_bytes(raw_content)
    if not raw:
        label = CONTENT_TYPE_MAP.get(content_type, f"类型{content_type}")
        return f"[{label}]", label, "parsed"

    # Text messages
    if content_type in (0, 2):
        text, _reply = extract_text_from_pb(raw)
        if text:
            return text, "文本", "parsed"
        plain = try_decode_utf8(raw)
        if plain:
            return plain, "文本", "parsed"
        return "[文本内容解析失败]", "文本", "unknown_format"

    # System messages
    if content_type == 1011:
        text = try_decode_utf8(raw)
        return text or "[系统消息]", "系统消息", "parsed" if text else "unknown_format"

    # System notifications
    if content_type == 132:
        text, _ = extract_text_from_pb(raw)
        return text or "[系统通知]", "系统通知", "parsed" if text else "unknown_format"

    # Link card
    if content_type == 31:
        title, url = extract_link_from_pb(raw)
        display = f"[链接] {title}" + (f" {url}" if url else "") if title else "[链接卡片]"
        return display, "链接卡片", "parsed"

    # Sticker
    if content_type == 29:
        desc = extract_sticker_text(raw)
        return f"[表情] {desc}" if desc else "[表情]", "表情", "parsed"

    # Unparsed media types: image, voice, video, file
    if content_type == 14:
        return "[未解析图片]", "图片", "unparsed_media"
    if content_type == 15:
        return "[未解析语音]", "语音", "unparsed_media"
    if content_type == 16:
        return "[未解析视频]", "视频", "unparsed_media"
    if content_type == 17:
        return "[未解析文件]", "文件", "unparsed_media"
    if content_type in (19, 32):
        label = CONTENT_TYPE_MAP.get(content_type, f"类型{content_type}")
        return f"[{label}]", label, "unparsed_media"

    # Fallback attempt
    text, _ = extract_text_from_pb(raw)
    if text:
        return text, CONTENT_TYPE_MAP.get(content_type, f"类型{content_type}"), "parsed"

    label = CONTENT_TYPE_MAP.get(content_type, f"类型{content_type}")
    return f"[{label}]", label, "unknown_format"
