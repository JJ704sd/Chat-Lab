from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import Message
from .storage import Storage
from .subject import SubjectResolver
from .sources.wecom_archive import (
    load_display_map,
    looks_unified,
    message_from_archive,
    message_from_unified,
)


REQUIRED_FIELDS = (
    "source",
    "source_message_id",
    "conversation_id",
    "sent_at",
    "sender_display",
    "content",
)


def message_from_mapping(item: dict[str, Any]) -> Message:
    missing = [field for field in REQUIRED_FIELDS if not item.get(field)]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")
    sent_at = datetime.fromisoformat(str(item["sent_at"]).replace("Z", "+00:00"))
    return Message(
        source=str(item["source"]),
        source_message_id=str(item["source_message_id"]),
        conversation_id=str(item["conversation_id"]),
        sent_at=sent_at,
        sender_display=str(item["sender_display"]),
        content=str(item["content"]),
        direction=str(item.get("direction", "unknown")),
        content_type=str(item.get("content_type", "text")),
        raw_ref=str(item["raw_ref"]) if item.get("raw_ref") else None,
        sender_corp_name=str(item["sender_corp_name"]) if item.get("sender_corp_name") else None,
    )


def import_messages(
    storage: Storage,
    messages: Iterable[Message],
    *,
    semantic: bool | None = None,
    rebuild: bool = True,
) -> int:
    resolver = SubjectResolver()
    count = 0
    storage.initialize()
    for message in messages:
        storage.upsert_message(
            message,
            resolver.resolve(message.sender_display, message.sender_corp_name),
        )
        count += 1
    if rebuild:
        storage.rebuild_analysis(semantic=semantic)
    return count


def import_jsonl(storage: Storage, path: str | Path, *, semantic: bool | None = None) -> int:
    messages: list[Message] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                messages.append(message_from_mapping(item))
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return import_messages(storage, messages, semantic=semantic)


def _looks_normalized(item: dict[str, Any]) -> bool:
    return all(item.get(field) for field in REQUIRED_FIELDS)


def import_archive_jsonl(
    storage: Storage,
    path: str | Path,
    *,
    display_map: Mapping[str, str] | str | Path | None = None,
    semantic: bool | None = None,
    rebuild: bool = True,
) -> int:
    names = display_map if isinstance(display_map, Mapping) else load_display_map(display_map)
    messages: list[Message] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if _looks_normalized(item):
                    parsed = message_from_mapping(item)
                elif looks_unified(item):
                    parsed = message_from_unified(item)
                else:
                    parsed = message_from_archive(item, names)
                if parsed is not None:
                    messages.append(parsed)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return import_messages(storage, messages, semantic=semantic, rebuild=rebuild)


def import_archive_inbox(
    storage: Storage,
    inbox: str | Path,
    *,
    display_map: Mapping[str, str] | str | Path | None = None,
    semantic: bool | None = None,
) -> dict[str, Any]:
    inbox_dir = Path(inbox)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    done_dir = inbox_dir / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    storage.initialize()
    imported = 0
    files: list[str] = []
    for path in sorted(inbox_dir.glob("*.jsonl")):
        marker = json.dumps(
            {"size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns},
            sort_keys=True,
        )
        key = f"archive-inbox:{path.name}"
        if storage.get_cursor(key) == marker:
            continue
        imported += import_archive_jsonl(
            storage,
            path,
            display_map=display_map,
            semantic=semantic,
            rebuild=False,
        )
        storage.set_cursor(key, marker)
        path.replace(done_dir / path.name)
        files.append(path.name)
    if imported:
        storage.rebuild_analysis(semantic=semantic)
    return {"imported": imported, "files": files, "inbox": str(inbox_dir)}

