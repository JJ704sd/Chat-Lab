from __future__ import annotations

import argparse
from datetime import datetime
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
from .wecom_parser import WecomLocalParser
from .wecom_storage import WecomLocalStorage
from .wecom_exporter import export_issues_to_csv, export_issues_to_json
from .wecom_subject import WecomSubjectClassifier
from .wecom_semantic import WecomSemanticAnalyzer, WecomSemanticSettings
from ..secrets import KeyRing, KeyRecord


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

    msg_conn = sqlite3.connect(str(msg_db))
    msg_conn.row_factory = sqlite3.Row
    user_conn = sqlite3.connect(str(user_db)) if user_db.is_file() else None
    if user_conn:
        user_conn.row_factory = sqlite3.Row
    sess_conn = sqlite3.connect(str(session_db)) if session_db.is_file() else None
    if sess_conn:
        sess_conn.row_factory = sqlite3.Row
    comp_conn = sqlite3.connect(str(company_db)) if company_db.is_file() else None
    if comp_conn:
        comp_conn.row_factory = sqlite3.Row

    try:
        parser = WecomLocalParser()
        since_seq, since_msg_id = storage.get_cursor(account_id, "message.db")
        records, new_seq, new_msg_id = parser.parse_databases(
            account_id=account_id,
            message_conn=msg_conn,
            user_conn=user_conn,
            session_conn=sess_conn,
            company_conn=comp_conn,
            since_sequence=since_seq,
            since_message_id=since_msg_id,
        )

        upserted = storage.upsert_messages(records)
        storage.set_cursor(account_id, "message.db", new_seq, new_msg_id)

        semantic_analyzer = None
        if use_semantic:
            semantic_analyzer = WecomSemanticAnalyzer()
        analysis_result = storage.rebuild_analysis(semantic_analyzer=semantic_analyzer)

        return {
            "success": True,
            "account_id": account_id,
            "imported_messages": upserted,
            "analysis": analysis_result,
            "storage_path": str(storage_path),
        }
    finally:
        msg_conn.close()
        if user_conn:
            user_conn.close()
        if sess_conn:
            sess_conn.close()
        if comp_conn:
            comp_conn.close()


def run_single_capture(
    account_id: str,
    wecom_root: Path | str | None = None,
    analysis_db_path: Path | str | None = None,
    keyring_path: Path | str | None = None,
    use_semantic: bool = False,
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
    raw_key = None
    if keyring_file.is_file():
        kr = KeyRing(keyring_file)
        raw_key = kr.get(f"wecom:{account_id}:message") or kr.get(account_id)

    # Capture snapshots for message, user, session
    snapshots_dir = account_snapshot_dir(account_id)
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    decrypted_conns: dict[str, sqlite3.Connection] = {}
    db_names = ["message.db", "user.db", "session.db"]

    for name in db_names:
        db_file = data_dir / name
        if not db_file.is_file():
            continue

        snap = capture_consistent_snapshot(db_file, account_id)
        plain_bytes, verif = decrypt_and_verify_snapshot(snap, bytes(raw_key) if raw_key else None)

        if not verif.is_valid or plain_bytes is None:
            return {
                "success": False,
                "error": f"Decryption/verification failed for {name}: {verif.error}",
                "account_id": account_id,
            }

        conn = sqlite3.connect(":memory:")
        conn.deserialize(plain_bytes)
        conn.row_factory = sqlite3.Row
        decrypted_conns[name] = conn

    if "message.db" not in decrypted_conns:
        return {"success": False, "error": "message.db could not be opened", "account_id": account_id}

    try:
        parser = WecomLocalParser()
        since_seq, since_msg_id = storage.get_cursor(account_id, "message.db")
        records, new_seq, new_msg_id = parser.parse_databases(
            account_id=account_id,
            message_conn=decrypted_conns["message.db"],
            user_conn=decrypted_conns.get("user.db"),
            session_conn=decrypted_conns.get("session.db"),
            since_sequence=since_seq,
            since_message_id=since_msg_id,
        )

        upserted = storage.upsert_messages(records)
        storage.set_cursor(account_id, "message.db", new_seq, new_msg_id)

        semantic_analyzer = None
        if use_semantic:
            semantic_analyzer = WecomSemanticAnalyzer()
        analysis_result = storage.rebuild_analysis(semantic_analyzer=semantic_analyzer)

        return {
            "success": True,
            "account_id": account_id,
            "new_messages": upserted,
            "analysis": analysis_result,
        }
    finally:
        for c in decrypted_conns.values():
            c.close()
