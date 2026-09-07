"""Read-only discovery of Mac WeCom profiles; discovery is not message capture."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .wxsqlite3 import has_wxsqlite3_header_shape, is_plain_sqlite


def discover_macos_profiles(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    profiles = []
    for profile in sorted(root.iterdir()):
        if not profile.is_dir() or profile.is_symlink() or not re.fullmatch(r'[0-9a-fA-F]{32}', profile.name):
            continue
        folder = profile / 'Messages1'
        if not (folder / 'Info.db').is_file():
            continue
        databases = []
        for path in sorted(folder.glob('*.db')):
            if not path.is_file() or path.is_symlink():
                continue
            with path.open('rb') as stream:
                header = stream.read(4096)
            wal = Path(str(path) + '-wal')
            databases.append({
                'name': path.name, 'size': path.stat().st_size,
                'has_wal': wal.is_file(), 'wal_size': wal.stat().st_size if wal.is_file() else 0,
                'has_shm': Path(str(path) + '-shm').is_file(),
                'format': 'plain_sqlite' if is_plain_sqlite(header) else (
                    'wxsqlite3_candidate' if has_wxsqlite3_header_shape(header) else 'unknown'),
            })
        profiles.append({
            'account_id': profile.name, 'account_id_kind': 'profile_directory',
            'platform': 'macos', 'path': str(profile), 'data_dir': str(folder),
            'has_message_db': True, 'has_session_db': (folder / 'Session.db').is_file(),
            'has_user_db': (profile / 'Contact/Contact.db').is_file(),
            'databases': databases, 'capture_ready': False,
            'capture_blockers': ['macos_key_provider_unverified', 'macos_message_schema_unverified'],
            'note': '仅发现本地数据库；尚未验证密钥与消息表结构，不代表已取得聊天正文。',
        })
    return profiles
