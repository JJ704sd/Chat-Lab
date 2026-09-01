from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from ..models import Message


ARCHIVE_SOURCE = "wecom"
CN_TZ = timezone(timedelta(hours=8))
EXTERNAL_USER_PREFIXES = ("wm", "wo", "wb")


def load_display_map(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}
    target = Path(path)
    if not target.is_file():
        return {}
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("userid display map must be a JSON object")
    return {str(key): str(value) for key, value in payload.items() if value}


def conversation_id_for(
    roomid: str | None,
    from_user: str | None,
    to_list: list[str] | None,
) -> str:
    room = (roomid or "").strip()
    if room:
        return room
    people = [item for item in [from_user, *(to_list or [])] if item]
    return "dm:" + ":".join(sorted(people)) if people else "dm:unknown"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def content_from_archive(item: Mapping[str, Any]) -> tuple[str, str]:
    msgtype = str(item.get("msgtype") or "unknown")
    if msgtype == "text":
        return str(_as_mapping(item.get("text")).get("content") or ""), "text"
    if msgtype == "markdown":
        return str(_as_mapping(item.get("info")).get("content") or ""), "markdown"
    if msgtype == "image":
        image = _as_mapping(item.get("image"))
        return _media_placeholder("image", image), "image"
    if msgtype == "file":
        file_info = _as_mapping(item.get("file"))
        name = file_info.get("filename") or "file"
        return f"[file {name} {_media_id(file_info)}]".strip(), "file"
    if msgtype == "voice":
        return _media_placeholder("voice", _as_mapping(item.get("voice"))), "voice"
    if msgtype == "video":
        return _media_placeholder("video", _as_mapping(item.get("video"))), "video"
    if msgtype == "emotion":
        return _media_placeholder("emotion", _as_mapping(item.get("emotion"))), "emotion"
    if msgtype == "link":
        link = _as_mapping(item.get("link"))
        return f"{link.get('title') or ''} {link.get('link_url') or ''}".strip(), "link"
    if msgtype == "location":
        location = _as_mapping(item.get("location"))
        return str(location.get("address") or location.get("title") or "[location]"), "location"
    if msgtype == "revoke":
        return f"[revoke {_as_mapping(item.get('revoke')).get('pre_msgid') or ''}]".strip(), "revoke"
    if msgtype == "mixed":
        parts: list[str] = []
        for part in _as_mapping(item.get("mixed")).get("item") or []:
            mapping = _as_mapping(part)
            nested = mapping.get("content")
            if isinstance(nested, str):
                try:
                    nested = json.loads(nested)
                except json.JSONDecodeError:
                    nested = {"content": nested}
            piece_type = str(mapping.get("type") or "text")
            piece = {"msgtype": piece_type, piece_type: nested}
            text, _kind = content_from_archive(piece)
            if text:
                parts.append(text)
        return "\n".join(parts), "mixed"
    if msgtype == "switch":
        return "", "switch"
    raw = item.get(msgtype)
    if isinstance(raw, Mapping):
        dumped = json.dumps(raw, ensure_ascii=False)
        return dumped, msgtype
    return "", msgtype


def _media_id(payload: Mapping[str, Any]) -> str:
    file_id = payload.get("sdkfileid") or payload.get("sdk_file_id")
    path = payload.get("path") or payload.get("local_path")
    if path:
        return f"path={path}"
    if file_id:
        return f"sdkfileid={file_id}"
    return "pending"


def _media_placeholder(kind: str, payload: Mapping[str, Any]) -> str:
    return f"[{kind} {_media_id(payload)}]".strip()


def _sent_at(item: Mapping[str, Any]) -> datetime:
    raw = item.get("msgtime") or item.get("time") or 0
    millis = int(raw)
    if millis > 10_000_000_000:
        instant = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    elif millis > 0:
        instant = datetime.fromtimestamp(millis, tz=timezone.utc)
    else:
        instant = datetime.now(timezone.utc)
    return instant.astimezone(CN_TZ)


def _direction(item: Mapping[str, Any], from_user: str | None) -> str:
    msgid = str(item.get("msgid") or "")
    if msgid.endswith("_external") or msgid.endswith("_updown_stream"):
        return "outbound"
    if from_user and from_user.casefold().startswith(EXTERNAL_USER_PREFIXES):
        return "outbound"
    if from_user:
        return "inbound"
    return "unknown"


def should_skip(item: Mapping[str, Any]) -> bool:
    action = str(item.get("action") or "send")
    msgtype = str(item.get("msgtype") or "")
    return action == "switch" or msgtype == "switch"


def message_from_archive(
    item: Mapping[str, Any],
    display_map: Mapping[str, str] | None = None,
) -> Message | None:
    if should_skip(item):
        return None
    msgid = str(item.get("msgid") or item.get("source_message_id") or "").strip()
    if not msgid:
        raise ValueError("archive message missing msgid")
    from_user = str(item.get("from") or item.get("user") or "") or None
    to_list = [str(value) for value in (item.get("tolist") or []) if value]
    names = display_map or {}
    sender_display = str(item.get("sender_display") or names.get(from_user or "") or from_user or "unknown")
    content, content_type = content_from_archive(item)
    return Message(
        source=str(item.get("source") or ARCHIVE_SOURCE),
        source_message_id=msgid,
        conversation_id=conversation_id_for(str(item.get("roomid") or "") or None, from_user, to_list),
        sent_at=_sent_at(item),
        sender_display=sender_display,
        content=content,
        direction=str(item.get("direction") or _direction(item, from_user)),
        content_type=content_type,
        raw_ref=str(item.get("seq")) if item.get("seq") is not None else None,
    )


def looks_unified(item: Mapping[str, Any]) -> bool:
    if item.get("source_message_id") or item.get("msgid"):
        return False
    return bool(item.get("msg_id")) and "msg_type" in item


def message_from_unified(item: Mapping[str, Any]) -> Message | None:
    if should_skip(item):
        return None
    msgid = str(item.get("msg_id") or "").strip()
    if not msgid:
        raise ValueError("unified message missing msg_id")
    sender_id = str(item.get("sender_id") or "") or None
    sender_name = str(item.get("sender_name") or sender_id or "unknown")
    sender_corp = str(item.get("sender_corp_name") or "").strip()
    sender_display = f"{sender_name} @{sender_corp}" if sender_corp else sender_name
    room_id = str(item.get("room_id") or "") or None
    conversation_id = str(item.get("conversation_id") or "").strip()
    if not conversation_id:
        conversation_id = conversation_id_for(room_id, sender_id, None)
    text = str(item.get("text") or "")
    media_paths = [str(path) for path in (item.get("media_paths") or []) if path]
    if media_paths and "path=" not in text:
        extra = " ".join(f"[media path={path}]" for path in media_paths)
        text = f"{text}\n{extra}".strip()
    archive_like = {
        "msgid": msgid,
        "from": sender_id,
        "msgtime": item.get("msg_time") or 0,
        "msgtype": item.get("msg_type") or "text",
    }
    return Message(
        source=str(item.get("source") or ARCHIVE_SOURCE),
        source_message_id=msgid,
        conversation_id=conversation_id,
        sent_at=_sent_at(archive_like),
        sender_display=sender_display,
        content=text,
        direction=str(item.get("direction") or _direction(archive_like, sender_id)),
        content_type=str(item.get("msg_type") or "text"),
        raw_ref=str(item["seq"]) if item.get("seq") is not None else None,
        sender_corp_name=sender_corp or None,
    )
