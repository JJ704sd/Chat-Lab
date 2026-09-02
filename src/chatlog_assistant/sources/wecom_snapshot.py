from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import time
from typing import Any


PAGE_SIZE = 4096
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24


@dataclass(frozen=True, slots=True)
class WalFrameInfo:
    page_number: int
    commit_pages: int
    salt: bytes


@dataclass(frozen=True, slots=True)
class WalAnalysis:
    valid_header: bool
    page_size: int
    salt: bytes
    seq: int
    valid_frames: int
    last_commit_frame: int
    committed_frames: int


def analyze_wal(wal_bytes: bytes, expected_page_size: int = PAGE_SIZE) -> WalAnalysis:
    if len(wal_bytes) < WAL_HEADER_SIZE:
        return WalAnalysis(
            valid_header=False,
            page_size=0,
            salt=b"",
            seq=0,
            valid_frames=0,
            last_commit_frame=-1,
            committed_frames=0,
        )

    magic = int.from_bytes(wal_bytes[:4], "big")
    if magic not in (0x377F0682, 0x377F0683):
        return WalAnalysis(
            valid_header=False,
            page_size=0,
            salt=b"",
            seq=0,
            valid_frames=0,
            last_commit_frame=-1,
            committed_frames=0,
        )

    declared_size = int.from_bytes(wal_bytes[8:12], "big") or 65536
    seq = int.from_bytes(wal_bytes[12:16], "big")
    salt = wal_bytes[16:24]
    frame_size = WAL_FRAME_HEADER_SIZE + expected_page_size

    offset = WAL_HEADER_SIZE
    valid_frames = 0
    last_commit_frame = -1
    frame_idx = 0

    while offset + frame_size <= len(wal_bytes):
        header = wal_bytes[offset : offset + WAL_FRAME_HEADER_SIZE]
        page_number = int.from_bytes(header[:4], "big")
        commit_pages = int.from_bytes(header[4:8], "big")
        frame_salt = header[8:16]

        if page_number < 1:
            break
        if frame_salt != salt:
            # Salt mismatch means frame belongs to an older/reset WAL transaction
            offset += frame_size
            frame_idx += 1
            continue

        valid_frames += 1
        if commit_pages > 0:
            last_commit_frame = frame_idx
        offset += frame_size
        frame_idx += 1

    committed_count = last_commit_frame + 1 if last_commit_frame >= 0 else 0
    return WalAnalysis(
        valid_header=True,
        page_size=declared_size,
        salt=salt,
        seq=seq,
        valid_frames=valid_frames,
        last_commit_frame=last_commit_frame,
        committed_frames=committed_count,
    )


@dataclass(frozen=True, slots=True)
class ConsistencySnapshot:
    account_id: str
    database_name: str
    db_bytes: bytes
    wal_bytes: bytes | None
    shm_bytes: bytes | None
    db_hash: str
    wal_hash: str | None
    is_consistent: bool
    retries: int
    meta: dict[str, Any]


def capture_consistent_snapshot(
    db_path: Path,
    account_id: str,
    *,
    max_retries: int = 5,
    retry_delay: float = 0.05,
) -> ConsistencySnapshot:
    """Read .db, .db-wal, and .db-shm files with double-read consistency verification."""
    wal_path = Path(str(db_path) + "-wal")
    shm_path = Path(str(db_path) + "-shm")

    last_snapshot: tuple[bytes, bytes | None, bytes | None] | None = None

    for attempt in range(max_retries):
        # Sample 1: stat and read
        if not db_path.is_file():
            raise FileNotFoundError(f"Database file not found: {db_path}")

        db_stat_1 = db_path.stat()
        wal_stat_1 = wal_path.stat() if wal_path.is_file() else None

        db_data = db_path.read_bytes()
        wal_data = wal_path.read_bytes() if wal_path.is_file() else None
        shm_data = shm_path.read_bytes() if shm_path.is_file() else None

        # Sample 2: stat check after read
        db_stat_2 = db_path.stat()
        wal_stat_2 = wal_path.stat() if wal_path.is_file() else None

        # Check if modified during read
        db_stable = (
            db_stat_1.st_size == db_stat_2.st_size
            and db_stat_1.st_mtime_ns == db_stat_2.st_mtime_ns
            and len(db_data) == db_stat_1.st_size
        )
        wal_stable = (wal_stat_1 is None) == (wal_stat_2 is None)
        if wal_stat_1 is not None or wal_stat_2 is not None:
            if wal_stat_1 is None or wal_stat_2 is None:
                wal_stable = False
            else:
                wal_stable = (
                    wal_stat_1.st_size == wal_stat_2.st_size
                    and wal_stat_1.st_mtime_ns == wal_stat_2.st_mtime_ns
                    and (wal_data is not None and len(wal_data) == wal_stat_1.st_size)
                )

        # A second complete read also detects same-size rewrites and WAL/SHM
        # appearance/disappearance; stat-only stability is insufficient.
        same_bytes = (
            db_data == db_path.read_bytes()
            and wal_data == (wal_path.read_bytes() if wal_path.is_file() else None)
            and shm_data == (shm_path.read_bytes() if shm_path.is_file() else None)
        )
        if db_stable and wal_stable and same_bytes:
            # Check WAL committed structure if WAL exists
            is_wal_ok = True
            wal_analysis_meta = {}
            if wal_data:
                analysis = analyze_wal(wal_data)
                wal_analysis_meta = {
                    "valid_header": analysis.valid_header,
                    "valid_frames": analysis.valid_frames,
                    "committed_frames": analysis.committed_frames,
                    "last_commit_frame": analysis.last_commit_frame,
                }
                if not analysis.valid_header and len(wal_data) > 0:
                    is_wal_ok = False

            db_hash = hashlib.sha256(db_data).hexdigest()
            wal_hash = hashlib.sha256(wal_data).hexdigest() if wal_data else None

            return ConsistencySnapshot(
                account_id=account_id,
                database_name=db_path.name,
                db_bytes=db_data,
                wal_bytes=wal_data,
                shm_bytes=shm_data,
                db_hash=db_hash,
                wal_hash=wal_hash,
                is_consistent=is_wal_ok,
                retries=attempt,
                meta={
                    "db_size": len(db_data),
                    "wal_size": len(wal_data) if wal_data else 0,
                    "wal_analysis": wal_analysis_meta,
                },
            )

        time.sleep(retry_delay)

    # If maximum retries exceeded, return inconsistent snapshot with best effort
    db_data = db_path.read_bytes() if db_path.is_file() else b""
    wal_data = wal_path.read_bytes() if wal_path.is_file() else None
    shm_data = shm_path.read_bytes() if shm_path.is_file() else None
    db_hash = hashlib.sha256(db_data).hexdigest()
    wal_hash = hashlib.sha256(wal_data).hexdigest() if wal_data else None

    return ConsistencySnapshot(
        account_id=account_id,
        database_name=db_path.name,
        db_bytes=db_data,
        wal_bytes=wal_data,
        shm_bytes=shm_data,
        db_hash=db_hash,
        wal_hash=wal_hash,
        is_consistent=False,
        retries=max_retries,
        meta={"error": "max retries exceeded during concurrent writes"},
    )
