from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    key: str
    platform: str
    account_hint: str
    database_path: Path
    watch_paths: tuple[Path, ...]
    encrypted: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "platform": self.platform,
            "account_hint": self.account_hint,
            "database_path": str(self.database_path),
            "watch_paths": [str(path) for path in self.watch_paths],
            "encrypted": self.encrypted,
        }


def default_documents() -> Path:
    profile = Path(os.environ.get("USERPROFILE", Path.home()))
    return profile / "Documents"


def wecom_root(documents: str | Path | None = None) -> Path:
    root = Path(documents) if documents else default_documents()
    if root.is_dir() and root.name.casefold() == "wxwork":
        return root
    nested = root / "WXWork"
    if nested.is_dir():
        return nested
    if root.is_dir() and any(path.is_dir() and path.name.isdigit() for path in root.iterdir()):
        return root
    return nested


def discover_sources(documents: str | Path | None = None) -> list[SourceCandidate]:
    root = Path(documents) if documents else default_documents()
    candidates: list[SourceCandidate] = []

    work_root = wecom_root(root)
    if work_root.is_dir():
        for account in sorted(path for path in work_root.iterdir() if path.is_dir() and path.name.isdigit()):
            message_db = account / "Data" / "message.db"
            if not message_db.is_file():
                continue
            index_db = account / "Index" / "message_index_v1_1.db"
            watch_paths = tuple(path for path in (message_db, index_db) if path.exists())
            candidates.append(
                SourceCandidate(
                    key=f"wecom:{account.name}",
                    platform="wecom",
                    account_hint=account.name,
                    database_path=message_db,
                    watch_paths=watch_paths,
                )
            )

    xwechat_root = root / "xwechat_files"
    if xwechat_root.is_dir():
        for account in sorted(xwechat_root.glob("wxid_*")):
            message_dir = account / "db_storage" / "message"
            message_databases = (
                sorted(
                    path
                    for path in message_dir.glob("message_*.db")
                    if re.fullmatch(r"message_\d+\.db", path.name)
                )
                if message_dir.is_dir()
                else ()
            )
            for message_db in message_databases:
                wal = Path(str(message_db) + "-wal")
                session = account / "db_storage" / "session" / "session.db"
                session_wal = Path(str(session) + "-wal")
                watch_paths = tuple(
                    path for path in (message_db, wal, session, session_wal) if path.exists()
                )
                candidates.append(
                    SourceCandidate(
                        key=f"wechat:{account.name}:{message_db.stem}",
                        platform="wechat",
                        account_hint=account.name,
                        database_path=message_db,
                        watch_paths=watch_paths,
                    )
                )
    return candidates
