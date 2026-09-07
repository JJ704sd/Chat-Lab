from __future__ import annotations

import os
from pathlib import Path
import sys


DEFAULT_WECOM_ROOT = (
    Path.home() / 'Library/Containers/com.tencent.WeWorkMac/Data/Documents/Profiles'
    if sys.platform == 'darwin' else Path(r"C:\Users\Administrator\Documents\WXWork")
)
DEFAULT_LOCAL_DATA_ROOT = Path('data/wecom-local') if sys.platform == 'darwin' else Path(r"D:\chatlab\data\wecom-local")
DEFAULT_ACCOUNTS_DIR = DEFAULT_LOCAL_DATA_ROOT / "accounts"
DEFAULT_ANALYSIS_DB = DEFAULT_LOCAL_DATA_ROOT / "analysis.db"
DEFAULT_EXPORTS_DIR = DEFAULT_LOCAL_DATA_ROOT / "exports"
DEFAULT_LOGS_DIR = DEFAULT_LOCAL_DATA_ROOT / "logs"


def account_snapshot_dir(account_id: str, base: Path | str | None = None) -> Path:
    root = Path(base) if base else DEFAULT_ACCOUNTS_DIR
    return root / account_id / "snapshots"


def account_keyring_path(account_id: str, base: Path | str | None = None) -> Path:
    root = Path(base) if base else DEFAULT_ACCOUNTS_DIR
    return root / account_id / "keyring.dpapi"


def account_cursor_path(account_id: str, base: Path | str | None = None) -> Path:
    root = Path(base) if base else DEFAULT_ACCOUNTS_DIR
    return root / account_id / "cursors.json"


def ensure_local_dirs(account_id: str | None = None, base: Path | str | None = None) -> None:
    data_root = Path(base) if base else DEFAULT_LOCAL_DATA_ROOT
    data_root.mkdir(parents=True, exist_ok=True)
    (data_root / "exports").mkdir(parents=True, exist_ok=True)
    (data_root / "logs").mkdir(parents=True, exist_ok=True)
    if account_id:
        account_snapshot_dir(account_id, data_root / "accounts").mkdir(parents=True, exist_ok=True)
