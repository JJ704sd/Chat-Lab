from __future__ import annotations

import json
import hashlib
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from typing import Any


CONTENT_TYPE_MAP = {
    0: "文本",
    2: "文本",
    4: "合并转发记录",
    14: "图片",
    15: "语音",
    16: "视频",
    17: "文件",
    19: "位置",
    29: "表情",
    31: "链接卡片",
    32: "小程序",
    40: "通话/转发候选",
    49: "应用卡片/转发记录",
    132: "系统通知",
    145: "业务卡片",
    565: "微盘文件通知",
    573: "OA协同卡片",
    1002: "用户状态同步",
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
            return val, pos
        if shift > 63:
            raise ValueError("Protobuf varint overflow")
    raise ValueError("Truncated Protobuf varint")


def parse_protobuf_fields(data: bytes, *, strict: bool = False) -> list[tuple[int, int, Any]]:
    """Parse protobuf payload into [(field_num, wire_type, payload), ...]."""
    fields: list[tuple[int, int, Any]] = []
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _read_varint(data, pos)
            field_num = tag >> 3
            wire_type = tag & 0x07
            if field_num == 0:
                raise ValueError("Invalid Protobuf field zero")
            if wire_type == 0:  # varint
                val, pos = _read_varint(data, pos)
                fields.append((field_num, 0, val))
            elif wire_type == 2:  # length-delimited
                length, pos = _read_varint(data, pos)
                if pos + length > len(data):
                    raise ValueError("Truncated Protobuf length field")
                payload = data[pos : pos + length]
                pos += length
                fields.append((field_num, 2, payload))
            elif wire_type == 5:  # fixed32
                if pos + 4 > len(data):
                    raise ValueError("Truncated Protobuf fixed32")
                val = struct.unpack("<I", data[pos : pos + 4])[0]
                pos += 4
                fields.append((field_num, 5, val))
            elif wire_type == 1:  # fixed64
                if pos + 8 > len(data):
                    raise ValueError("Truncated Protobuf fixed64")
                val = struct.unpack("<Q", data[pos : pos + 8])[0]
                pos += 8
                fields.append((field_num, 1, val))
            else:
                raise ValueError("Unsupported Protobuf wire type")
        except Exception:
            if strict:
                raise
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


def extract_rich_content(raw: bytes) -> str | None:
    """Extracts human-readable text from cards, forward records, and JSON payloads."""
    plain = try_decode_utf8(raw)
    if plain and len(plain) > 1:
        if plain.startswith("{") and plain.endswith("}"):
            try:
                data = json.loads(plain)
                if isinstance(data, dict):
                    parts: list[str] = []
                    if "main_title" in data and isinstance(data["main_title"], dict):
                        mt = data["main_title"].get("title")
                        if mt:
                            parts.append(str(mt))
                    if "card_desc" in data and data["card_desc"]:
                        parts.append(str(data["card_desc"]))
                    if "horizon_sub_title_list" in data and isinstance(data["horizon_sub_title_list"], list):
                        for item in data["horizon_sub_title_list"]:
                            tit = item.get("title")
                            sub = item.get("sub_title")
                            if tit or sub:
                                parts.append(f"{tit}: {sub}" if tit and sub else str(tit or sub))
                    if parts:
                        return "\n".join(parts)
            except Exception:
                pass
        return plain

    outer = parse_protobuf_fields(raw)
    texts: list[str] = []
    for _fn, wt, val in outer:
        if wt == 2 and isinstance(val, bytes):
            t = try_decode_utf8(val)
            if t:
                if t.startswith("{") and t.endswith("}"):
                    try:
                        d = json.loads(t)
                        if isinstance(d, dict):
                            parts = []
                            if "main_title" in d and isinstance(d["main_title"], dict):
                                mt = d["main_title"].get("title")
                                if mt:
                                    parts.append(str(mt))
                            if "card_desc" in d and d["card_desc"]:
                                parts.append(str(d["card_desc"]))
                            if "horizon_sub_title_list" in d and isinstance(d["horizon_sub_title_list"], list):
                                for item in d["horizon_sub_title_list"]:
                                    tit = item.get("title")
                                    sub = item.get("sub_title")
                                    if tit or sub:
                                        parts.append(f"{tit}: {sub}" if tit and sub else str(tit or sub))
                            if parts:
                                texts.append("\n".join(parts))
                                continue
                    except Exception:
                        pass
                if len(t) > 2 and not t.startswith("http") and not t.startswith("wedrive://") and not t.startswith("i."):
                    texts.append(t)

    return "\n".join(texts) if texts else None


@dataclass
class ForwardedContent:
    messages: list[dict[str, Any]] = field(default_factory=list)
    payload_paths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


def _native_envelope(raw: bytes):
    """Recognize the observed native forward envelope, not quoted metadata.

    The verified 2026 Windows payload uses repeated field 1 message records,
    field 2 title, and fields 1/2/11/101 inside each text message. In particular,
    message field 10 is the original conversation ID, NOT a message ID.
    """
    outer = parse_protobuf_fields(raw)
    title = pb_get(outer, 2, 2)
    nodes = [value for number, wire, value in outer if number == 1 and wire == 2]
    if not title or not try_decode_utf8(title) or not nodes:
        return None
    first = parse_protobuf_fields(nodes[0])
    if not (pb_get(first, 1, 0) and pb_get(first, 2, 0) and pb_get(first, 11, 2)):
        return None
    if not any(number >= 100 and wire == 2 for number, wire, _ in first):
        return None
    return nodes


def _decode_native_forward(raw: bytes, path: str) -> ForwardedContent | None:
    nodes = _native_envelope(raw)
    if nodes is None:
        return None
    result = ForwardedContent(payload_paths=[path])
    def check_complete(blob, location):
        try:
            parse_protobuf_fields(blob, strict=True)
        except (ValueError, struct.error):
            result.gaps.append(f"{location}: 原生 Protobuf 载荷截断或字段损坏")
    check_complete(raw, path)
    pending = [(nodes, result.messages, path)]
    root_hash = hashlib.sha256(raw).hexdigest()
    zone = timezone(timedelta(hours=8))
    while pending:
        children, destination, location = pending.pop()
        for index, blob in enumerate(children):
            fields = parse_protobuf_fields(blob)
            node_path = f"{location}.1[{index}]"
            check_complete(blob, node_path)
            sender, stamp = pb_get(fields, 1, 0), pb_get(fields, 2, 0)
            name = try_decode_utf8(pb_get(fields, 11, 2) or b"")
            kind = pb_get(fields, 4, 0) or 0
            info = {"native_payload_path": node_path, "native_node_sha256": hashlib.sha256(blob).hexdigest(),
                    "original_message_id": None, "message_id_basis": "native_payload_position",
                    "native_sender_id": str(sender) if sender else None, "native_send_time": stamp,
                    "native_conversation_id": str(pb_get(fields, 10, 0) or ""), "raw_content_type": kind}
            item = {"message_id": f"native:{root_hash}:{node_path}", "sender_id": str(sender) if sender else None,
                    "sender_display": name, "sender_corp_id": str(pb_get(fields, 13, 0) or "") or None,
                    "sender_corp_name": try_decode_utf8(pb_get(fields, 14, 2) or b"") or None,
                    "conversation_id": info["native_conversation_id"], "content_type": kind,
                    "_native_provenance": info}
            try:
                item["sent_at"] = datetime.fromtimestamp(stamp, zone).isoformat() if stamp else None
            except (ValueError, TypeError, OverflowError, OSError):
                item["sent_at"] = None
            payload = pb_get(fields, 101, 2)
            if kind in (0, 2) and payload is not None:
                item["content"] = extract_text_from_pb(payload)[0]
            else:
                item["content"] = "[未解析媒体]"
            if kind in (0, 2) and (payload is None or not item["content"]):
                result.gaps.append(f"{node_path}: 原生文本体缺失或无法解码")
                info["body_parse_status"] = "unknown_format"
            body_payload = pb_get(fields, 21, 2)
            if body_payload is not None:
                info["unquoted_text"] = extract_text_from_pb(body_payload)[0]
                mentions = []
                for number, wire, value in parse_protobuf_fields(body_payload):
                    if number == 1 and wire == 2:
                        part = parse_protobuf_fields(value)
                        if pb_get(part, 1, 0) == 5:
                            user = parse_protobuf_fields(pb_get(part, 2, 2) or b"")
                            if pb_get(user, 1, 0):
                                mentions.append(str(pb_get(user, 1, 0)))
                info["mentioned_sender_ids"] = mentions
            # Quote records are evidence for association, not extra chat events.
            extra = parse_protobuf_fields(pb_get(fields, 17, 2) or b"")
            quote_wrapper = parse_protobuf_fields(pb_get(extra, 1002, 2) or b"")
            quoted = parse_protobuf_fields(pb_get(quote_wrapper, 1, 2) or b"")
            if quoted:
                quote_name = try_decode_utf8(pb_get(quoted, 11, 2) or b"") or ""
                quote_text = extract_text_from_pb(pb_get(quoted, 101, 2) or b"")[0]
                item["quoted_text"] = f"{quote_name}:\n{quote_text}" if quote_text else None
                info["quoted_identity"] = {"sender_id": str(pb_get(quoted, 1, 0) or ""),
                                            "send_time": pb_get(quoted, 2, 0),
                                            "conversation_id": str(pb_get(quoted, 10, 0) or ""),
                                            "text": quote_text}
            if kind in (4, 40, 49):
                item["content_type"] = "合并转发记录"
                item["content"] = "[群聊的聊天记录]"
                for number, wire, value in fields:
                    if number >= 100 and wire == 2:
                        nested = _native_envelope(value)
                        if nested is not None:
                            check_complete(value, f"{node_path}.{number}")
                            item.setdefault("forwarded_messages", [])
                            pending.append((nested, item["forwarded_messages"], f"{node_path}.{number}"))
            destination.append(item)
    return result


def _xml_forward_items(root: ET.Element, path: str, result: ForwardedContent) -> list[dict[str, Any]]:
    """Decode self-describing recordinfo XML. Missing source fields stay missing."""
    output: list[dict[str, Any]] = []
    pending = [(root, output, path)]
    while pending:
        container, destination, location = pending.pop()
        listing = container if container.tag == "datalist" else container.find("datalist")
        if listing is None:
            info = container.find(".//recordinfo")
            listing = info.find("datalist") if info is not None else None
        if listing is None:
            continue
        elements = listing.findall("dataitem")
        declared = listing.get("count")
        if declared and (not declared.isdigit() or int(declared) != len(elements)):
            result.gaps.append(f"{location}: XML 声明条数与实际条数不符")
        for index, element in enumerate(elements):
            def value(*names):
                for name in names:
                    found = element.find(name)
                    if found is not None and found.text:
                        return "".join(found.itertext())
                return None
            stamp = value("srcMsgCreateTime", "sourcecreatetime")
            if stamp and stamp.isdigit():
                number = int(stamp)
                stamp = datetime.fromtimestamp(number / 1000 if number > 10**12 else number,
                                                timezone(timedelta(hours=8))).isoformat()
            item = {"source_message_id": element.get("dataid") or value("srcMsgId"),
                    "sender_id": value("dataitemsource/fromusr", "fromusr"),
                    "sender_display": value("sourcename"), "sent_at": stamp,
                    "content": value("datadesc"),
                    "content_type": "text" if element.get("datatype") == "1" else element.get("datatype", "unknown")}
            nested = element.find("recordinfo")
            record_text = value("recorditem")
            if record_text:
                try:
                    nested = ET.fromstring(record_text)
                except ET.ParseError:
                    result.gaps.append(f"{location}/{index}: 嵌套 XML 损坏")
            if nested is not None:
                item["forwarded_messages"] = []
                item["content"] = item["content"] or "[群聊的聊天记录]"
                pending.append((nested, item["forwarded_messages"], f"{location}/{index}"))
            elif item["content"] is None and item["content_type"] != "text":
                item["content"] = "[未解析媒体]"
            destination.append(item)
    return output


def decode_forwarded_content(raw_content: Any, extra_content: Any = None) -> ForwardedContent:
    """Walk Protobuf containers to explicit JSON/XML message collections.

    Wire field numbers alone do not establish message identity or timestamps.
    Unknown native layouts stay unresolved, including preview-only cards.
    No URLs, media references or attachment keys are fetched or interpreted.
    """
    result = ForwardedContent()
    stack = [("extra_content", to_raw_bytes(extra_content)), ("content", to_raw_bytes(raw_content))]
    seen_payloads: set[bytes] = set()
    while stack:
        path, raw = stack.pop()
        if not raw:
            continue
        native = _decode_native_forward(raw, path)
        if native is not None:
            result.messages.extend(native.messages)
            result.payload_paths.extend(native.payload_paths)
            result.gaps.extend(native.gaps)
            continue
        plain = try_decode_utf8(raw)
        if plain:
            if plain.startswith("{"):
                try:
                    parsed = json.loads(plain)
                except ValueError:
                    result.gaps.append(f"{path}: JSON 解析失败")
                    continue
                if isinstance(parsed, dict) and isinstance(parsed.get("forwarded_messages"), list):
                    if raw not in seen_payloads:
                        result.messages.extend(parsed["forwarded_messages"])
                        result.payload_paths.append(path)
                        seen_payloads.add(raw)
            elif plain.startswith("<") and ("recordinfo" in plain or "datalist" in plain):
                if "<!DOCTYPE" in plain.upper() or "<!ENTITY" in plain.upper():
                    result.gaps.append(f"{path}: 不支持带实体声明的 XML")
                    continue
                try:
                    root = ET.fromstring(plain)
                    if raw not in seen_payloads:
                        result.messages.extend(_xml_forward_items(root, path, result))
                        result.payload_paths.append(path)
                        seen_payloads.add(raw)
                except (ET.ParseError, ValueError, OverflowError, OSError):
                    result.gaps.append(f"{path}: XML 或原时间字段无效")
            if plain.startswith("{") or (plain.startswith("<") and ("recordinfo" in plain or "datalist" in plain)):
                continue
        # Iterative traversal imposes no nesting-depth cutoff.
        for index, (number, wire, value) in reversed(list(enumerate(parse_protobuf_fields(raw)))):
            if number > 0 and wire == 2 and isinstance(value, bytes) and len(value) < len(raw):
                stack.append((f"{path}/pb:{number}[{index}]", value))
    return result


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

    # Forwarded chat history records
    if content_type in (4, 40, 49):
        # Observed local type 40 also encodes calls. Never label these forwards.
        if content_type == 40:
            fields = parse_protobuf_fields(raw)
            call_text = pb_get(fields, 3, 2)
            decoded = try_decode_utf8(call_text) if isinstance(call_text, bytes) else None
            if decoded and re.fullmatch(r"(?:通话时长\s*\d+:\d+(?::\d+)?|对方未接听|对方已取消|已取消|未接听)", decoded):
                return decoded, "通话记录", "parsed"
        rich = extract_rich_content(raw)
        if rich:
            return f"[群聊的聊天记录]\n{rich}", "合并转发记录", "unexpanded_forward"
        return "[合并转发记录：正文尚未展开]", "合并转发记录", "unexpanded_forward"

    # Business / OA / WeDrive cards
    if content_type in (145, 565, 573, 561, 579, 580):
        rich = extract_rich_content(raw)
        label = CONTENT_TYPE_MAP.get(content_type, "业务卡片")
        if rich:
            return f"[{label}]\n{rich}", label, "parsed"
        return f"[{label}]", label, "parsed"

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
    if content_type in (16, 22, 23):
        return "[未解析视频]", "视频", "unparsed_media"
    if content_type in (17, 20):
        return "[未解析文件]", "文件", "unparsed_media"
    if content_type in (19, 32):
        label = CONTENT_TYPE_MAP.get(content_type, f"类型{content_type}")
        return f"[{label}]", label, "unparsed_media"

    # Fallback attempt
    rich = extract_rich_content(raw)
    if rich:
        return rich, CONTENT_TYPE_MAP.get(content_type, f"类型{content_type}"), "parsed"

    label = CONTENT_TYPE_MAP.get(content_type, f"类型{content_type}")
    return f"[{label}]", label, "unknown_format"
