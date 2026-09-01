from __future__ import annotations

import shutil
from pathlib import Path


DATA_ROOT = Path("data")
PLATFORMS = ("wechat", "wecom")
PLATFORM_LABELS = {"wechat": "个微", "wecom": "企微"}
DEFAULT_PORTS = {"wechat": 8765, "wecom": 8766}
LEGACY_KEYRING = DATA_ROOT / "keyring.dpapi"


def archive_inbox_dir(platform: str = "wecom") -> Path:
    return workspace_dir(platform) / "inbox"


def userid_display_map_path(platform: str = "wecom") -> Path:
    return workspace_dir(platform) / "userid_display.json"


def workspace_dir(platform: str) -> Path:
    if platform not in PLATFORMS:
        raise ValueError(f"unsupported platform: {platform}")
    return DATA_ROOT / platform


def database_path(platform: str) -> Path:
    return workspace_dir(platform) / "chatlog.db"


def keyring_path(platform: str) -> Path:
    return workspace_dir(platform) / "keyring.dpapi"


def migrate_legacy_wechat_keyring() -> Path | None:
    """Copy the old shared DPAPI keyring into the 个微 workspace once."""
    dest = keyring_path("wechat")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() or not LEGACY_KEYRING.is_file():
        return dest if dest.is_file() else None
    shutil.copy2(LEGACY_KEYRING, dest)
    return dest
