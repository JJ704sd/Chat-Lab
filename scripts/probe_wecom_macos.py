"""Read-only macOS WeCom feasibility probe. Never exports chat bodies or keys."""
from __future__ import annotations
import argparse
import ctypes
import json
from pathlib import Path
import platform
import plistlib
import sqlite3
import sys
from datetime import datetime, timezone

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from chatlog_assistant.sources.wxsqlite3 import has_wxsqlite3_header_shape
from chatlog_assistant.sources.wecom_pipeline import discover_wecom_accounts
from chatlog_assistant.sources.wecom_snapshot import capture_consistent_snapshot, analyze_wal
from chatlog_assistant.sources.wecom_decrypter import decrypt_and_verify_snapshot
from chatlog_assistant.sources.crypto_native import aes_cbc_encrypt
from chatlog_assistant.secrets import dpapi_protect


def probe(profile: Path, group: str, pid: int | None) -> dict:
    info_path = Path('/Applications/企业微信.app/Contents/Info.plist')
    info = plistlib.loads(info_path.read_bytes())
    report = {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'platform': {'system': platform.system(), 'macos': platform.mac_ver()[0], 'machine': platform.machine()},
        'app': {k: info.get(k) for k in ('CFBundleIdentifier', 'CFBundleShortVersionString', 'CFBundleVersion')},
        'scope': {'group_name': group, 'source_mode': 'local_files_only', 'ui_used': False},
        'discovery_count': len(discover_wecom_accounts(profile.parent)),
        'databases': [], 'platform_backends': {},
    }
    for path in sorted((profile / 'Messages1').glob('*.db')):
        with path.open('rb') as stream:
            page = stream.read(4096)
        row = {
            'relative_path': str(path.relative_to(profile)), 'size': path.stat().st_size,
            'plain_sqlite': page.startswith(b'SQLite format 3\x00'),
            'wxsqlite3_header_shape': has_wxsqlite3_header_shape(page),
        }
        wal_path = Path(str(path) + '-wal')
        if wal_path.exists():
            wal = analyze_wal(wal_path.read_bytes())
            row['wal'] = {'valid_header': wal.valid_header, 'page_size': wal.page_size,
                          'committed_frames': wal.committed_frames}
        connection = None
        try:
            connection = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
            connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()
            row['plain_schema_read'] = True
        except sqlite3.DatabaseError as error:
            row['plain_schema_read'] = False
            row['plain_schema_error'] = str(error)
        finally:
            if connection:
                connection.close()
        report['databases'].append(row)
    for name, action in (
        ('aes', lambda: aes_cbc_encrypt(bytes(16), bytes(16), bytes(16))),
        ('protected_key_store', lambda: dpapi_protect(b'public-test-vector-no-secrets')),
    ):
        try:
            action()
            report['platform_backends'][name] = {'available': True}
        except OSError as error:
            report['platform_backends'][name] = {'available': False, 'reason': str(error)}
    report['snapshot_checks'] = []
    for name in ('Session.db', 'Info.db'):
        snapshot = capture_consistent_snapshot(profile / 'Messages1' / name, 'mac-validation')
        plain, result = decrypt_and_verify_snapshot(snapshot, None)
        report['snapshot_checks'].append({
            'database': name, 'consistent': snapshot.is_consistent, 'retries': snapshot.retries,
            'database_bytes': len(snapshot.db_bytes), 'wal_bytes': len(snapshot.wal_bytes or b''),
            'db_sha256': snapshot.db_hash, 'wal_sha256': snapshot.wal_hash,
            'decode_valid': result.is_valid, 'decode_error': result.error,
            'note': 'No actual database key was supplied or recovered; this is not a test that any key was rejected.',
        })
        del snapshot, plain
    cache = (profile / 'conv_snapshot').read_bytes()
    report['conversation_cache'] = {'exact_name_occurrences': cache.count(group.encode('utf-8')),
                                    'history_completeness_claim': False}
    connection = sqlite3.connect((profile / 'ai_chunks_embedding.db').as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        count = connection.execute('SELECT count(*) FROM chunks_with_embedding_metadatatext02 WHERE data=?', (group,)).fetchone()[0]
        schema = connection.execute("SELECT sql FROM sqlite_master WHERE name='chunks_with_embedding'").fetchone()[0]
        report['index'] = {'matching_chunk_name_entries': count, 'main_table_schema': schema,
                           'count_is_not_message_count': True,
                           'historical_body_extraction': False,
                           'immutable_snapshot_note': 'Metadata corroboration only; not a proof of current WAL or full history.'}
    finally:
        connection.close()
    if pid is not None:
        lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        buf = ctypes.create_string_buffer(4096)
        length = lib.proc_pidpath(pid, buf, len(buf))
        expected = '/Applications/企业微信.app/Contents/MacOS/企业微信'
        if length <= 0 or buf.value.decode() != expected:
            raise RuntimeError('PID no longer belongs to the verified WeCom executable')
        flags = ctypes.c_uint32()
        lib.csops.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]
        cs_result = lib.csops(pid, 0, ctypes.byref(flags), 4)
        lib.mach_task_self.restype = ctypes.c_uint
        own_port = lib.mach_task_self()
        lib.task_for_pid.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        lib.task_for_pid.restype = ctypes.c_int
        task_port = ctypes.c_uint()
        result = lib.task_for_pid(own_port, pid, ctypes.byref(task_port))
        lib.mach_error_string.argtypes = [ctypes.c_int]
        lib.mach_error_string.restype = ctypes.c_char_p
        report['process_access'] = {
            'code_signing_query_result': cs_result,
            'hardened_runtime': bool(flags.value & 0x10000) if cs_result == 0 else None,
            'get_task_allow': bool(flags.value & 4) if cs_result == 0 else None,
            'task_for_pid_result': result, 'task_for_pid_message': lib.mach_error_string(result).decode(),
            'task_port_acquired': bool(task_port.value), 'memory_read': False,
            'process_suspended_or_modified': False,
        }
        if result == 0 and task_port.value:
            lib.mach_port_deallocate(own_port, task_port.value)
    report['outcome'] = {
        'group_present_in_local_metadata': report['conversation_cache']['exact_name_occurrences'] > 0,
        'native_database_decrypted': False, 'real_messages_exported': 0,
        'latest_day_full_capture_verified': False,
        'reason': 'Mac discovery and native AES are available; database key acquisition and the real message schema remain unverified.',
        'implementation_stage': 'mac_discovery_and_crypto_only',
    }
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--group', required=True)
    parser.add_argument('--pid', type=int, help='Optional task-port availability probe; never reads process memory')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = probe(args.profile.resolve(), args.group, args.pid)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    args.output.chmod(0o600)
    print(json.dumps({'report': str(args.output), 'outcome': report['outcome']}, ensure_ascii=False))
