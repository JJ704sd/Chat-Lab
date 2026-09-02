from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

from .wecom_paths import (
    DEFAULT_WECOM_ROOT,
    DEFAULT_LOCAL_DATA_ROOT,
    DEFAULT_ANALYSIS_DB,
    DEFAULT_EXPORTS_DIR,
    DEFAULT_ACCOUNTS_DIR,
    account_snapshot_dir,
    account_keyring_path,
    ensure_local_dirs,
)
from .wecom_snapshot import capture_consistent_snapshot
from .wecom_decrypter import decrypt_and_verify_snapshot
from .wecom_parser import WecomLocalParser, WecomUnifiedRecord, records_from_message_tree
from .wecom_storage import WecomLocalStorage
from .wecom_exporter import export_issues_to_csv, export_issues_to_json
from .wecom_subject import WecomSubjectClassifier
from .wecom_semantic import WecomSemanticAnalyzer, WecomSemanticSettings
from ..secrets import KeyRing, KeyRecord
from .windows_memory import zero_secret


def import_normalized_jsonl(
    path: Path | str, *, account_id: str,
    analysis_db_path: Path | str = DEFAULT_ANALYSIS_DB,
    conversation_name: str | None = None, use_semantic: bool = False,
) -> dict[str, Any]:
    """Import explicit normalized messages; never invent forwarded authors or timestamps.

    Forwarded collections have isolated conversation IDs and parent evidence references.
    All input is validated before any database write. Repeated imports are idempotent.
    """
    source = Path(path)
    records, gaps = [], []
    root_conversations = set()
    provenance = {"origin": "normalized_unverified", "file": str(source.resolve()),
                  "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    for line_number, line in enumerate(source.read_text(encoding="utf-8-sig").splitlines(), 1):
        if line.strip():
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError(f"{source.name}:{line_number}: 消息必须是对象")
            conversation = item.get("conversation_id") or item.get("room_id")
            name = item.get("conversation_name")
            if not conversation or not name:
                raise ValueError(f"{source.name}:{line_number}: 缺少会话 ID 或名称")
            if conversation_name and conversation_name.casefold() not in str(name).casefold():
                continue
            root_conversations.add(str(conversation))
            decoded, missing = records_from_message_tree([item], account_id=account_id,
                source_database="normalized-jsonl", conversation_id=str(conversation),
                conversation_name=str(name), source_reference=f"{source.name}:{line_number}",
                provenance=provenance)
            records.extend(decoded)
            gaps.extend(missing)
    # Conflicting original IDs must never silently replace a different message.
    seen = {}
    for record in records:
        comparable = record.as_dict()
        comparable.pop("source_reference")
        if record.id in seen and seen[record.id] != comparable:
            raise ValueError(f"重复消息 ID 内容冲突: {record.message_id}")
        seen[record.id] = comparable
    storage = WecomLocalStorage(analysis_db_path)
    storage.initialize()
    count = storage.upsert_messages(records)
    analyzer = WecomSemanticAnalyzer() if use_semantic else None
    reports = [storage.rebuild_analysis(analyzer, account_id=account_id, conversation_id=conversation)
               for conversation in sorted(root_conversations)]
    return {"imported_messages": count, "conversations": len(reports), "analysis": reports,
            "gaps": gaps, "source_verified": False,
            "unexpanded_forwards": sum(r.parse_status == "unexpanded_forward" for r in records)}


def discover_wecom_accounts(wecom_root: Path | str | None = None) -> list[dict[str, Any]]:
    root = Path(wecom_root) if wecom_root else DEFAULT_WECOM_ROOT
    accounts = []
    if not root.is_dir():
        return accounts

    for item in sorted(root.iterdir()):
        if not item.is_dir() or not item.name.isdigit():
            continue
        data_dir = item / "Data"
        has_msg = (data_dir / "message.db").is_file()
        has_user = (data_dir / "user.db").is_file()
        has_session = (data_dir / "session.db").is_file()

        dbs = []
        if data_dir.is_dir():
            for db in sorted(data_dir.glob("*.db")):
                wal = Path(str(db) + "-wal")
                shm = Path(str(db) + "-shm")
                dbs.append({
                    "name": db.name,
                    "size": db.stat().st_size,
                    "has_wal": wal.is_file(),
                    "wal_size": wal.stat().st_size if wal.is_file() else 0,
                    "has_shm": shm.is_file(),
                })

        accounts.append({
            "account_id": item.name,
            "path": str(item),
            "data_dir": str(data_dir),
            "has_message_db": has_msg,
            "has_user_db": has_user,
            "has_session_db": has_session,
            "databases": dbs,
        })
    return accounts


def import_decrypted_directory(
    db_dir: Path | str,
    account_id: str = "offline_reference",
    analysis_db_path: Path | str | None = None,
    use_semantic: bool = False,
    full_replay: bool = False,
    conversation_name: str | None = None,
) -> dict[str, Any]:
    """Offline import from already decrypted databases directory (e.g. reference package)."""
    db_dir = Path(db_dir)
    msg_db = db_dir / "message.db"
    user_db = db_dir / "user.db"
    session_db = db_dir / "session.db"
    company_db = db_dir / "company.db"

    if not msg_db.is_file():
        raise FileNotFoundError(f"Missing message.db in {db_dir}")

    storage_path = Path(analysis_db_path) if analysis_db_path else DEFAULT_ANALYSIS_DB
    ensure_local_dirs(account_id, storage_path.parent)

    storage = WecomLocalStorage(storage_path)
    storage.initialize()

    connections, snapshots = {}, {}
    try:
        for path in (msg_db, user_db, session_db, company_db):
            if not path.is_file():
                continue
            snap = capture_consistent_snapshot(path, account_id)
            plain, verification = decrypt_and_verify_snapshot(snap, None)
            if not verification.is_valid or plain is None:
                raise ValueError(f"{path.name}: {verification.error}")
            conn = sqlite3.connect(":memory:")
            conn.deserialize(plain)
            conn.row_factory = sqlite3.Row
            connections[path.name] = conn
            snapshots[path.name] = {"file": str(path.resolve()), "db_sha256": snap.db_hash,
                                    "wal_sha256": snap.wal_hash, "integrity_ok": verification.integrity_ok}
        parser = WecomLocalParser()
        since_seq, since_msg_id = (0, "") if full_replay or conversation_name else storage.get_cursor(account_id, "message.db")
        records, new_seq, new_msg_id = parser.parse_databases(
            account_id=account_id,
            message_conn=connections["message.db"],
            user_conn=connections.get("user.db"),
            session_conn=connections.get("session.db"),
            company_conn=connections.get("company.db"),
            since_sequence=since_seq,
            since_message_id=since_msg_id,
        )
        records = [replace(r, provenance={**r.provenance, "snapshots": snapshots}) for r in records
                   if not conversation_name or conversation_name.casefold() in r.conversation_name.casefold()]
        upserted = storage.upsert_messages(records)
        if not conversation_name:
            storage.set_cursor(account_id, "message.db", new_seq, new_msg_id)

        semantic_analyzer = None
        if use_semantic:
            semantic_analyzer = WecomSemanticAnalyzer()
        analysis_result = storage.rebuild_analysis(semantic_analyzer=semantic_analyzer, account_id=account_id,
                                                   conversation_name=conversation_name)

        return {
            "success": True,
            "account_id": account_id,
            "imported_messages": upserted,
            "analysis": analysis_result,
            "storage_path": str(storage_path),
            "snapshots": snapshots,
            "full_replay": full_replay or bool(conversation_name),
        }
    finally:
        for conn in connections.values():
            conn.close()


def run_single_capture(
    account_id: str,
    wecom_root: Path | str | None = None,
    analysis_db_path: Path | str | None = None,
    keyring_path: Path | str | None = None,
    use_semantic: bool = False,
    full_replay: bool = False,
    conversation_name: str | None = None,
) -> dict[str, Any]:
    """Performs consistent snapshot, decrypts with DPAPI saved key, parses, and updates storage."""
    root = Path(wecom_root) if wecom_root else DEFAULT_WECOM_ROOT
    account_dir = root / account_id
    data_dir = account_dir / "Data"

    if not data_dir.is_dir():
        raise FileNotFoundError(f"Account Data directory not found: {data_dir}")

    storage_path = Path(analysis_db_path) if analysis_db_path else DEFAULT_ANALYSIS_DB
    ensure_local_dirs(account_id, storage_path.parent)

    storage = WecomLocalStorage(storage_path)
    storage.initialize()

    # Retrieve key from DPAPI
    keyring_file = Path(keyring_path) if keyring_path else account_keyring_path(account_id)
    kr = KeyRing(keyring_file) if keyring_file.is_file() else None

    # Capture snapshots for message, user, session
    snapshots_dir = storage_path.parent / "accounts" / account_id / "snapshots" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    decrypted_conns: dict[str, sqlite3.Connection] = {}
    snapshots = {}
    try:
        for name in ("message.db", "user.db", "session.db", "company.db"):
            db_file = data_dir / name
            if not db_file.is_file():
                continue
            snap = capture_consistent_snapshot(db_file, account_id)
            raw_key = (kr.get(f"wecom:{account_id}:{Path(name).stem}") or
                       kr.get(f"wecom:{account_id}:message") or kr.get(account_id)) if kr else None
            try:
                plain_bytes, verif = decrypt_and_verify_snapshot(snap, bytes(raw_key) if raw_key else None)
            finally:
                if raw_key is not None:
                    zero_secret(raw_key)
            for suffix, data in (("", snap.db_bytes), ("-wal", snap.wal_bytes), ("-shm", snap.shm_bytes)):
                if data is not None:
                    (snapshots_dir / (name + suffix)).write_bytes(data)
            snapshots[name] = {"source_file": str(db_file), "snapshot_file": str(snapshots_dir / name),
                               "db_sha256": snap.db_hash, "wal_sha256": snap.wal_hash,
                               "consistent": snap.is_consistent, "integrity_ok": verif.integrity_ok,
                               "verified": verif.is_valid, "error": verif.error}
            (snapshots_dir / "manifest.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2), encoding="utf-8")
            if not verif.is_valid or plain_bytes is None:
                return {"success": False, "error": f"Decryption/verification failed for {name}: {verif.error}",
                        "account_id": account_id, "snapshots": snapshots}
            conn = sqlite3.connect(":memory:")
            conn.deserialize(plain_bytes)
            conn.row_factory = sqlite3.Row
            decrypted_conns[name] = conn
        if "message.db" not in decrypted_conns:
            return {"success": False, "error": "message.db could not be opened", "account_id": account_id}
        parser = WecomLocalParser()
        since_seq, since_msg_id = (0, "") if full_replay or conversation_name else storage.get_cursor(account_id, "message.db")
        records, new_seq, new_msg_id = parser.parse_databases(
            account_id=account_id,
            message_conn=decrypted_conns["message.db"],
            user_conn=decrypted_conns.get("user.db"),
            session_conn=decrypted_conns.get("session.db"),
            company_conn=decrypted_conns.get("company.db"),
            since_sequence=since_seq,
            since_message_id=since_msg_id,
        )
        records = [replace(r, provenance={**r.provenance, "snapshots": snapshots}) for r in records
                   if not conversation_name or conversation_name.casefold() in r.conversation_name.casefold()]
        upserted = storage.upsert_messages(records)
        if not conversation_name:
            storage.set_cursor(account_id, "message.db", new_seq, new_msg_id)

        semantic_analyzer = None
        if use_semantic:
            semantic_analyzer = WecomSemanticAnalyzer()
        analysis_result = storage.rebuild_analysis(semantic_analyzer=semantic_analyzer, account_id=account_id,
                                                   conversation_name=conversation_name)

        return {
            "success": True,
            "account_id": account_id,
            "new_messages": upserted,
            "analysis": analysis_result,
            "snapshots": snapshots,
            "full_replay": full_replay or bool(conversation_name),
        }
    finally:
        for c in decrypted_conns.values():
            c.close()
