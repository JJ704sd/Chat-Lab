from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any

from .wecom_snapshot import ConsistencySnapshot
from .wxsqlite3 import decrypt_database_bytes, is_plain_sqlite, verify_key


@dataclass(frozen=True, slots=True)
class DecryptVerificationResult:
    is_valid: bool
    is_plain: bool
    table_count: int
    tables: list[str]
    integrity_ok: bool
    integrity_message: str
    error: str | None = None


def sanitize_wal_header_for_deserialize(db_bytes: bytes) -> bytes:
    """When a database is in WAL mode, bytes 18..19 are [0x02, 0x02].

    sqlite3.deserialize() might fail with SQLITE_CANTOPEN (14) if journal_mode is WAL
    because an in-memory deserialized db has no WAL file attached.
    Setting version bytes 18 and 19 to 0x01 (rollback journal mode) in memory
    allows SQLite to open the in-memory image cleanly after WAL frames have been
    already merged into the page image.
    """
    if len(db_bytes) >= 20 and db_bytes.startswith(b"SQLite format 3\x00"):
        buf = bytearray(db_bytes)
        if buf[18] == 2:
            buf[18] = 1
        if buf[19] == 2:
            buf[19] = 1
        return bytes(buf)
    return db_bytes


def decrypt_and_verify_snapshot(
    snapshot: ConsistencySnapshot,
    raw_key: bytes | None,
) -> tuple[bytes | None, DecryptVerificationResult]:
    """Decrypts a snapshot (incorporating WAL) and performs thorough SQLite verification:

    - Key verification on page 1
    - SQLite header & page size check
    - sqlite_master readability
    - Core tables extraction
    - PRAGMA integrity_check
    """
    if not snapshot.db_bytes or len(snapshot.db_bytes) < 4096:
        return None, DecryptVerificationResult(
            is_valid=False,
            is_plain=False,
            table_count=0,
            tables=[],
            integrity_ok=False,
            integrity_message="",
            error="Database bytes too small or empty",
        )

    # Check if already plain
    if is_plain_sqlite(snapshot.db_bytes[:4096]):
        plain_bytes = snapshot.db_bytes
        if snapshot.wal_bytes:
            try:
                plain_bytes = decrypt_database_bytes(
                    snapshot.db_bytes, b"\x00" * 16, wal_bytes=snapshot.wal_bytes
                )
            except Exception:
                plain_bytes = snapshot.db_bytes
        sanitized = sanitize_wal_header_for_deserialize(plain_bytes)
        return sanitized, _verify_plain_sqlite_bytes(sanitized, is_plain=True)

    if raw_key is None or len(raw_key) != 16:
        return None, DecryptVerificationResult(
            is_valid=False,
            is_plain=False,
            table_count=0,
            tables=[],
            integrity_ok=False,
            integrity_message="",
            error="Missing or invalid 16-byte raw key",
        )

    if not verify_key(raw_key, snapshot.db_bytes[:4096]):
        return None, DecryptVerificationResult(
            is_valid=False,
            is_plain=False,
            table_count=0,
            tables=[],
            integrity_ok=False,
            integrity_message="",
            error="Key rejected on page 1 signature",
        )

    try:
        decrypted = decrypt_database_bytes(
            snapshot.db_bytes, raw_key, wal_bytes=snapshot.wal_bytes
        )
    except Exception as exc:
        return None, DecryptVerificationResult(
            is_valid=False,
            is_plain=False,
            table_count=0,
            tables=[],
            integrity_ok=False,
            integrity_message="",
            error=f"Decryption failed: {exc}",
        )

    sanitized = sanitize_wal_header_for_deserialize(decrypted)
    verif = _verify_plain_sqlite_bytes(sanitized, is_plain=False)
    return sanitized if verif.is_valid else None, verif


def _verify_plain_sqlite_bytes(plain_bytes: bytes, is_plain: bool) -> DecryptVerificationResult:
    if not is_plain_sqlite(plain_bytes[:4096]):
        return DecryptVerificationResult(
            is_valid=False,
            is_plain=is_plain,
            table_count=0,
            tables=[],
            integrity_ok=False,
            integrity_message="",
            error="Decrypted output does not start with SQLite format 3",
        )

    try:
        conn = sqlite3.connect(":memory:")
        conn.deserialize(plain_bytes)
        conn.row_factory = sqlite3.Row

        tables_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        table_names = [r[0] for r in tables_rows]

        # Check integrity
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
        conn.close()

        integrity_msg = "; ".join(str(r[0]) for r in integrity_rows)
        integrity_ok = integrity_msg.strip().lower() == "ok"

        return DecryptVerificationResult(
            is_valid=integrity_ok and len(table_names) > 0,
            is_plain=is_plain,
            table_count=len(table_names),
            tables=table_names,
            integrity_ok=integrity_ok,
            integrity_message=integrity_msg,
            error=None if integrity_ok else f"Integrity check failed: {integrity_msg}",
        )
    except Exception as exc:
        return DecryptVerificationResult(
            is_valid=False,
            is_plain=is_plain,
            table_count=0,
            tables=[],
            integrity_ok=False,
            integrity_message="",
            error=f"SQLite verification error: {exc}",
        )
