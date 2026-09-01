from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
import struct
from typing import Any

from .cipher_objects import recover_cipher_keys
from .discovery import default_documents
from .wechat_messages import parse_wechat_messages
from .windows_memory import (
    find_process_marker_regions,
    list_processes,
    scan_process_for_candidate_keys,
    scan_process_for_hex_secrets,
    scan_process_regions_for_key,
    zero_secret,
)


@dataclass(frozen=True, slots=True)
class EncryptedTarget:
    name: str
    path: Path
    salt: bytes


def locate_targets(documents: str | Path | None = None) -> list[EncryptedTarget]:
    root = Path(documents) if documents else default_documents()
    targets: list[EncryptedTarget] = []
    for account in sorted((root / "xwechat_files").glob("wxid_*")):
        paths = (
            ("message", account / "db_storage" / "message" / "message_0.db"),
            ("contact", account / "db_storage" / "contact" / "contact.db"),
        )
        for kind, path in paths:
            if not path.is_file():
                continue
            with path.open("rb") as handle:
                salt = handle.read(16)
            if len(salt) == 16:
                targets.append(EncryptedTarget(f"{account.name}:{kind}", path, salt))
    return targets


def _open_readonly(target: EncryptedTarget, key: bytearray):
    from sqlcipher3 import dbapi2 as sqlcipher

    uri = target.path.resolve().as_uri() + "?mode=ro"
    connection = sqlcipher.connect(uri, uri=True)
    connection.execute("PRAGMA cipher_log_level=NONE")
    raw_key = bytes(key).hex() + target.salt.hex()
    connection.execute(f"PRAGMA key = \"x'{raw_key}'\";")
    connection.execute("PRAGMA query_only=ON")
    return connection


def verify_sqlcipher4_raw_key(first_page: bytes, key: bytes) -> bool:
    """Verify a SQLCipher 4 raw page key using page 1's HMAC-SHA512."""
    page_size = 4096
    iv_size = 16
    hmac_size = 64
    reserve_size = 80
    if len(first_page) < page_size or len(key) != 32:
        return False
    salt = first_page[:16]
    hmac_salt = bytes(value ^ 0x3A for value in salt)
    hmac_key = hashlib.pbkdf2_hmac("sha512", key, hmac_salt, 2, dklen=32)
    authenticated_end = page_size - reserve_size + iv_size
    payload = first_page[16:authenticated_end] + struct.pack("<I", 1)
    expected = hmac.new(hmac_key, payload, hashlib.sha512).digest()
    stored = first_page[authenticated_end : authenticated_end + hmac_size]
    return hmac.compare_digest(expected, stored)


def probe_raw_keys(targets: list[EncryptedTarget], pid: int) -> tuple[dict[str, bytearray], list[dict[str, int | str]]]:
    """Find per-database raw keys only in regions containing SQLCipher salt state."""
    matched: dict[str, bytearray] = {}
    stats: list[dict[str, int | str]] = []
    markers: list[bytes] = []
    for target in targets:
        markers.extend((target.salt, bytes(value ^ 0x3A for value in target.salt)))
    regions_by_marker, marker_stats = find_process_marker_regions(pid, markers, writable_only=True)
    marker_stats.update({"pid": pid, "mode": "sqlcipher_marker_regions"})
    stats.append(marker_stats)

    for target in targets:
        hmac_salt = bytes(value ^ 0x3A for value in target.salt)
        salt_regions = {region.base_address: region for region in regions_by_marker[target.salt]}
        hmac_regions = {region.base_address: region for region in regions_by_marker[hmac_salt]}
        common_regions = [salt_regions[address] for address in salt_regions.keys() & hmac_regions.keys()]
        with target.path.open("rb") as handle:
            first_page = handle.read(4096)
        key, key_stats = scan_process_regions_for_key(
            pid,
            common_regions,
            lambda candidate, page=first_page: verify_sqlcipher4_raw_key(page, candidate),
        )
        key_stats.update({"pid": pid, "mode": f"sqlcipher_raw_key:{target.name}"})
        stats.append(key_stats)
        if key is not None:
            matched[target.name] = key
    return matched, stats


def _mark_verified(target: EncryptedTarget, results_by_name: dict[str, dict[str, Any]], method: str, tables: list[Any]) -> None:
    results_by_name[target.name].update(
        {
            "matched": True,
            "verified": True,
            "method": method,
            "table_count": len(tables),
            "tables": [row[0] for row in tables[:20]],
        }
    )


def recover_wechat_keys(
    documents: str | Path | None = None,
    pid: int | None = None,
) -> tuple[dict[str, bytearray], dict[str, Any]]:
    """Recover verified SQLCipher keys. Caller must zero returned secrets."""
    probe = probe_wechat(documents, pid, retain_keys=True)
    keys: dict[str, bytearray] = probe.pop("keys", {})
    return keys, probe


def probe_wechat(
    documents: str | Path | None = None,
    pid: int | None = None,
    *,
    retain_keys: bool = False,
) -> dict[str, Any]:
    targets = locate_targets(documents)
    if not targets:
        return {"success": False, "error": "未找到个微 message_0.db/contact.db"}

    processes = list_processes("Weixin.exe")
    if pid is not None:
        processes = [process for process in processes if process.pid == pid]
    results_by_name = {
        target.name: {"database": target.name, "matched": False, "verified": False}
        for target in targets
    }
    scan_stats: list[dict[str, int]] = []
    all_candidates: list[bytearray] = []
    verified_keys: dict[str, bytearray] = {}
    pages = {}
    for target in targets:
        with target.path.open("rb") as handle:
            pages[target.salt] = handle.read(4096)
    try:
        unresolved = {target.salt for target in targets}
        for process in processes:
            if not unresolved:
                break

            def _cipher_ok(raw_key: bytes, salt: bytes | None, pages=pages, unresolved=unresolved) -> bool:
                if salt is not None:
                    page = pages.get(salt)
                    return bool(page) and salt in unresolved and verify_sqlcipher4_raw_key(page, raw_key)
                return any(
                    verify_sqlcipher4_raw_key(pages[item], raw_key) for item in unresolved if item in pages
                )

            cipher_keys, cipher_stats = recover_cipher_keys(process.pid, _cipher_ok)
            cipher_stats.update({"pid": process.pid, "mode": "config_cipher_xor"})
            scan_stats.append(cipher_stats)
            for candidate in cipher_keys:
                all_candidates.append(candidate.raw_key)
                for target in targets:
                    if target.salt not in unresolved:
                        continue
                    if candidate.salt not in (None, target.salt):
                        continue
                    if not verify_sqlcipher4_raw_key(pages[target.salt], bytes(candidate.raw_key)):
                        continue
                    try:
                        connection = _open_readonly(target, candidate.raw_key)
                        try:
                            tables = connection.execute(
                                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                            ).fetchall()
                        finally:
                            connection.close()
                    except Exception:
                        continue
                    if tables:
                        _mark_verified(target, results_by_name, candidate.method, tables)
                        verified_keys[target.name] = bytearray(candidate.raw_key)
                        unresolved.discard(target.salt)

            if not unresolved:
                break
            raw_keys, raw_stats = probe_raw_keys(
                [target for target in targets if target.salt in unresolved], process.pid
            )
            scan_stats.extend(raw_stats)
            for target in targets:
                raw_key = raw_keys.get(target.name)
                if raw_key is None:
                    continue
                all_candidates.append(raw_key)
                try:
                    connection = _open_readonly(target, raw_key)
                    try:
                        tables = connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                        ).fetchall()
                    finally:
                        connection.close()
                except Exception:
                    continue
                if tables:
                    _mark_verified(target, results_by_name, "sqlcipher_memory_context", tables)
                    verified_keys[target.name] = bytearray(raw_key)
                    unresolved.remove(target.salt)

            if not unresolved:
                break
            passphrases, passphrase_stats = scan_process_for_hex_secrets(
                process.pid,
                writable_only=True,
                max_candidates=32,
            )
            passphrase_stats.update({"pid": process.pid, "mode": "hex_passphrase"})
            scan_stats.append(passphrase_stats)
            all_candidates.extend(passphrases)
            anchor = next(
                (target for target in targets if target.name.endswith(":message") and target.salt in unresolved),
                next((target for target in targets if target.salt in unresolved), None),
            )
            for passphrase in passphrases[:8]:
                if anchor is None:
                    break
                anchor_key = bytearray(
                    hashlib.pbkdf2_hmac(
                        "sha512", bytes(passphrase), anchor.salt, 256_000, dklen=32
                    )
                )
                all_candidates.append(anchor_key)
                try:
                    connection = _open_readonly(anchor, anchor_key)
                    try:
                        anchor_tables = connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                        ).fetchall()
                    finally:
                        connection.close()
                except Exception:
                    continue
                if not anchor_tables:
                    continue

                for target in targets:
                    if target.salt not in unresolved:
                        continue
                    if target is anchor:
                        derived = anchor_key
                        tables = anchor_tables
                    else:
                        derived = bytearray(
                            hashlib.pbkdf2_hmac(
                                "sha512", bytes(passphrase), target.salt, 256_000, dklen=32
                            )
                        )
                        all_candidates.append(derived)
                        try:
                            connection = _open_readonly(target, derived)
                            try:
                                tables = connection.execute(
                                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                                ).fetchall()
                            finally:
                                connection.close()
                        except Exception:
                            continue
                    if tables:
                        _mark_verified(target, results_by_name, "derived_passphrase", tables)
                        verified_keys[target.name] = bytearray(derived)
                        unresolved.remove(target.salt)
                break

            if not unresolved:
                break
            candidates, stats = scan_process_for_candidate_keys(process.pid, unresolved, writable_only=True)
            stats["mode"] = "adjacent_raw_key"
            stats["pid"] = process.pid
            scan_stats.append(stats)
            for target in targets:
                if target.salt not in unresolved:
                    continue
                target_candidates = candidates.get(target.salt, [])
                all_candidates.extend(target_candidates)
                result = results_by_name[target.name]
                result["candidate_count"] = len(target_candidates)
                for key in target_candidates:
                    try:
                        connection = _open_readonly(target, key)
                        try:
                            tables = connection.execute(
                                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                            ).fetchall()
                        finally:
                            connection.close()
                        if tables:
                            _mark_verified(target, results_by_name, "adjacent_raw_key", tables)
                            verified_keys[target.name] = bytearray(key)
                            unresolved.remove(target.salt)
                            break
                    except Exception:
                        continue
    finally:
        for key in all_candidates:
            if not any(secret is key for secret in verified_keys.values()):
                zero_secret(key)
        if not retain_keys:
            for secret in verified_keys.values():
                zero_secret(secret)
            verified_keys = {}
    results = list(results_by_name.values())
    payload = {
        "success": any(item.get("verified") for item in results),
        "process_found": bool(processes),
        "scan_stats": scan_stats,
        "databases": results,
    }
    if retain_keys:
        payload["keys"] = verified_keys
    return payload


def extract_wechat_messages(
    message_target: EncryptedTarget,
    message_key: bytearray,
    *,
    source_key: str,
    account_hint: str,
    contact_target: EncryptedTarget | None = None,
    contact_key: bytearray | None = None,
    since_cursor: str | None = None,
) -> tuple[list[Any], str]:
    message_conn = _open_readonly(message_target, message_key)
    contact_conn = _open_readonly(contact_target, contact_key) if contact_target is not None and contact_key is not None else None
    try:
        wxid = account_hint.rsplit("_", 1)[0] if account_hint.count("_") >= 2 else account_hint
        return parse_wechat_messages(
            message_conn,
            contact_conn=contact_conn,
            source="wechat",
            account_hint=wxid,
            since_cursor=since_cursor,
        )
    finally:
        message_conn.close()
        if contact_conn is not None:
            contact_conn.close()
