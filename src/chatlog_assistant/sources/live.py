from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ..models import Message
from ..pipeline import import_messages
from ..secrets import KeyRecord, KeyRing
from ..storage import Storage
from .discovery import SourceCandidate, discover_sources
from .encrypted_sources import AuthorizationRequired
from .wechat_nt import EncryptedTarget, extract_wechat_messages, locate_targets, probe_wechat
from .wecom import locate_wecom_targets, probe_wecom, parse_wecom_messages, WecomTarget
from .windows_memory import zero_secret
from .wxsqlite3 import decrypt_database_bytes


def public_probe(result: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(result)
    cleaned.pop("keys", None)
    return cleaned


def default_keyring(database: Path) -> KeyRing:
    return KeyRing(database.parent / "keyring.dpapi")


def _save_verified_keys(keyring: KeyRing, keys: dict[str, bytearray], *, platform: str) -> int:
    saved = 0
    try:
        for key_id, secret in keys.items():
            parts = key_id.split(":")
            account = parts[-2] if len(parts) >= 2 else key_id
            database = parts[-1]
            keyring.put(
                KeyRecord(key_id=key_id, kind=platform, account=account, database=database),
                bytes(secret),
            )
            saved += 1
    finally:
        for secret in keys.values():
            zero_secret(secret)
    return saved


def _merge_capture(
    result: dict[str, Any],
    keys: dict[str, bytearray],
    captured: Any,
) -> None:
    result["capture"] = captured.stats
    for name, secret in captured.verified.items():
        keys.setdefault(name, secret)
    for item in result.get("databases", []):
        name = item.get("database")
        if name in captured.verified:
            item["matched"] = True
            item["verified"] = True
            item["method"] = "debug_hwbp"
    result["success"] = any(item.get("verified") for item in result.get("databases", []))
    result["process_found"] = bool(result.get("process_found")) or int(captured.stats.get("attached") or 0) > 0


def probe_and_save_wechat(
    keyring: KeyRing,
    documents: str | Path | None = None,
    pid: int | None = None,
    *,
    capture: bool = False,
    restart: bool = False,
    timeout: float = 180,
) -> dict[str, Any]:
    from .capture import capture_wechat_keys

    result = {"success": False, "process_found": False, "databases": []}
    keys: dict[str, bytearray] = {}
    if not restart:
        result = probe_wechat(documents, pid, retain_keys=True)
        keys = result.pop("keys", {})
    if (capture or restart) and not result.get("success"):
        captured = capture_wechat_keys(documents, restart=restart, timeout=timeout)
        if not result.get("databases"):
            result = {
                "success": False,
                "process_found": True,
                "databases": [
                    {"database": target.name, "matched": False, "verified": False}
                    for target in locate_targets(documents)
                ],
            }
        _merge_capture(result, keys, captured)
    result["saved"] = _save_verified_keys(keyring, keys, platform="sqlcipher4-raw")
    return result


def probe_and_save_wecom(
    keyring: KeyRing,
    documents: str | Path | None = None,
    pid: int | None = None,
    *,
    capture: bool = False,
    restart: bool = False,
    timeout: float = 180,
) -> dict[str, Any]:
    from .capture import capture_wecom_keys

    result = {"success": False, "process_found": False, "databases": []}
    keys: dict[str, bytearray] = {}
    if not restart and not capture:
        result = probe_wecom(documents, pid, retain_keys=True)
        keys = result.pop("keys", {})
    if (capture or restart) and not result.get("success"):
        captured = capture_wecom_keys(documents, restart=restart, timeout=timeout)
        if not result.get("databases"):
            result = {
                "success": False,
                "process_found": True,
                "databases": [
                    {"database": target.name, "matched": False, "verified": False}
                    for target in locate_wecom_targets(documents)
                ],
            }
        _merge_capture(result, keys, captured)
    result["saved"] = _save_verified_keys(keyring, keys, platform="wxsqlite3-aes128")
    return result


def _wechat_targets(source: SourceCandidate, documents: str | Path | None) -> tuple[EncryptedTarget | None, EncryptedTarget | None]:
    targets = locate_targets(documents)
    message = next((item for item in targets if item.path.resolve() == source.database_path.resolve()), None)
    contact = next(
        (item for item in targets if item.name == f"{source.account_hint}:contact"),
        None,
    )
    return message, contact


def _load_secret(keyring: KeyRing, key_id: str) -> bytearray:
    secret = keyring.get(key_id)
    if secret is None:
        raise AuthorizationRequired(f"missing DPAPI key for {key_id}")
    return secret


def extract_wechat_source(
    source: SourceCandidate,
    keyring: KeyRing,
    since_cursor: str | None,
    documents: str | Path | None = None,
) -> tuple[Iterable[Message], str]:
    message_target, contact_target = _wechat_targets(source, documents)
    if message_target is None:
        raise FileNotFoundError(source.database_path)
    message_key = _load_secret(keyring, message_target.name)
    contact_key = keyring.get(contact_target.name) if contact_target is not None else None
    try:
        return extract_wechat_messages(
            message_target,
            message_key,
            source_key=source.key,
            account_hint=source.account_hint,
            contact_target=contact_target,
            contact_key=contact_key,
            since_cursor=since_cursor,
        )
    finally:
        zero_secret(message_key)
        if contact_key is not None:
            zero_secret(contact_key)


def extract_wecom_source(
    source: SourceCandidate,
    keyring: KeyRing,
    since_cursor: str | None,
    documents: str | Path | None = None,
) -> tuple[Iterable[Message], str]:
    targets = [item for item in locate_wecom_targets(documents) if item.account == source.account_hint]
    message_target = next((item for item in targets if item.kind == "message"), None)
    user_target = next((item for item in targets if item.kind == "user"), None)
    if message_target is None:
        raise FileNotFoundError(source.database_path)
    message_key = _load_secret(keyring, message_target.name)
    user_key = keyring.get(user_target.name) if user_target is not None else None
    try:
        message_plain = _decrypt_wecom(message_target, message_key)
        user_plain = _decrypt_wecom(user_target, user_key) if user_target is not None and user_key is not None else None
    finally:
        zero_secret(message_key)
        if user_key is not None:
            zero_secret(user_key)

    import sqlite3

    message_conn = sqlite3.connect(":memory:")
    message_conn.deserialize(message_plain)
    message_conn.row_factory = sqlite3.Row
    user_conn = None
    if user_plain is not None:
        user_conn = sqlite3.connect(":memory:")
        user_conn.deserialize(user_plain)
        user_conn.row_factory = sqlite3.Row
    try:
        return parse_wecom_messages(
            message_conn,
            user_conn=user_conn,
            source="wecom",
            since_cursor=since_cursor,
        )
    finally:
        message_conn.close()
        if user_conn is not None:
            user_conn.close()


def _decrypt_wecom(target: WecomTarget, key: bytearray) -> bytes:
    wal = Path(str(target.path) + "-wal")
    wal_bytes = wal.read_bytes() if wal.is_file() else None
    return decrypt_database_bytes(target.path.read_bytes(), bytes(key), wal_bytes=wal_bytes)


def import_live_sources(
    storage: Storage,
    keyring: KeyRing,
    documents: str | Path | None = None,
    sources: Iterable[SourceCandidate] | None = None,
    *,
    platform: str | None = None,
    semantic: bool | None = None,
) -> dict[str, Any]:
    storage.initialize()
    selected = list(sources) if sources is not None else discover_sources(documents)
    if platform:
        selected = [item for item in selected if item.platform == platform]
    imported = 0
    details: list[dict[str, Any]] = []
    for source in selected:
        cursor = storage.get_cursor(source.key)
        try:
            if source.platform == "wechat":
                messages, new_cursor = extract_wechat_source(source, keyring, cursor, documents)
            elif source.platform == "wecom":
                messages, new_cursor = extract_wecom_source(source, keyring, cursor, documents)
            else:
                continue
            message_list = list(messages)
            count = import_messages(storage, message_list, rebuild=False) if message_list else 0
            storage.set_cursor(source.key, new_cursor)
            imported += count
            details.append({"source": source.key, "imported": count, "status": "ok"})
        except AuthorizationRequired:
            details.append({"source": source.key, "imported": 0, "status": "need_key"})
        except Exception as exc:
            details.append({"source": source.key, "imported": 0, "status": "error", "error": type(exc).__name__})
    analysis = storage.rebuild_analysis(semantic=semantic) if imported else None
    payload = {"imported": imported, "sources": details, "database": str(storage.path)}
    if analysis is not None:
        payload["analysis"] = analysis
    return payload


def import_live_workspaces(
    documents: str | Path | None = None,
    sources: Iterable[SourceCandidate] | None = None,
    *,
    platform: str | None = None,
    semantic: bool | None = None,
) -> dict[str, Any]:
    from ..workspaces import PLATFORMS, database_path, migrate_legacy_wechat_keyring

    selected = list(sources) if sources is not None else discover_sources(documents)
    platforms = (platform,) if platform else PLATFORMS
    results: dict[str, Any] = {}
    total = 0
    if "wechat" in platforms:
        migrate_legacy_wechat_keyring()
    for name in platforms:
        subset = [item for item in selected if item.platform == name]
        storage = Storage(database_path(name))
        keyring = default_keyring(storage.path)
        payload = import_live_sources(
            storage, keyring, documents, subset, platform=name, semantic=semantic
        )
        results[name] = payload
        total += int(payload.get("imported") or 0)
    return {"imported": total, "workspaces": results}
