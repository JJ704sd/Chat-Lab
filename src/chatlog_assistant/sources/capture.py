from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Iterable

from .pe_locate import locate_wechat_breakpoints, locate_wecom_breakpoints, pe_export_lookup
from .wechat_nt import EncryptedTarget, locate_targets, verify_sqlcipher4_raw_key
from .wecom import WecomTarget, locate_wecom_targets
from .windows_debug import CaptureHit, attach_and_wait, enable_debug_privilege
from .windows_memory import (
    is_wow64_process,
    list_modules,
    list_processes,
    module_base,
    zero_secret,
)
from .wxsqlite3 import verify_key as verify_wxsqlite3_key


WECHAT_DLL_CANDIDATES = tuple(
    sorted(Path(r"C:\Program Files\Tencent\Weixin").glob("*/Weixin.dll"), reverse=True)
)
WECHAT_EXE_CANDIDATES = (
    Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe"),
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tencent" / "Weixin" / "Weixin.exe",
)
WECOM_EXE_CANDIDATES = (
    Path(r"C:\Program Files (x86)\WXWork\WXWork.exe"),
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "WXWork" / "WXWork.exe",
)


@dataclass(slots=True)
class CaptureResult:
    verified: dict[str, bytearray]
    stats: dict[str, Any]


def material_from_blob(blob: bytes) -> list[bytes]:
    """Turn a captured buffer into candidate key/passphrase material. Never logs contents."""
    candidates: list[bytes] = []
    if len(blob) in {16, 32}:
        candidates.append(bytes(blob))
    if blob.startswith(b"x'") and blob.endswith(b"'") and 16 <= len(blob) <= 101:
        try:
            raw = bytes.fromhex(blob[2:-1].decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            raw = b""
        if len(raw) in {16, 32}:
            candidates.append(raw)
        elif len(raw) == 48:
            candidates.append(raw[:32])
            candidates.append(raw)
    if len(blob) == 24 and blob.endswith(b"sAlT"):
        candidates.append(bytes(blob[:16]))
    unique: list[bytes] = []
    seen: set[bytes] = set()
    for item in candidates:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _image_for_module(pid: int, module_name: str) -> Path | None:
    wanted = module_name.casefold()
    for item in list_modules(pid):
        if item.name.casefold() == wanted and item.path:
            path = Path(item.path)
            if path.is_file():
                return path
    return None


def _wechat_breakpoints(pid: int, cache: dict[str, list[int]]) -> list[int]:
    key = f"wechat:{pid}"
    if key in cache:
        return cache[key]
    base = module_base(pid, "Weixin.dll")
    image = _image_for_module(pid, "Weixin.dll")
    if not base or image is None:
        cache[key] = []
        return []
    located = locate_wechat_breakpoints(image.read_bytes())
    cache[key] = [base + item.rva for item in located]
    return cache[key]


WECOM_CRYPTO_EXPORTS = (
    ("bcrypt.dll", "BCryptDecrypt"),
    ("bcrypt.dll", "BCryptHashData"),
    ("bcrypt.dll", "BCryptGenerateSymmetricKey"),
    ("bcryptprimitives.dll", "BCryptHashData"),
    ("bcryptprimitives.dll", "BCryptGenerateSymmetricKey"),
    ("advapi32.dll", "CryptHashData"),
    ("cryptsp.dll", "CryptHashData"),
    ("advapi32.dll", "CryptDecrypt"),
)


def resolve_process_export(pid: int, module_name: str, export_name: str, depth: int = 0) -> int | None:
    """Resolve a live export, following PE forwarders. Returns a process address, never a secret."""
    if depth > 6:
        return None
    base = module_base(pid, module_name)
    image = _image_for_module(pid, module_name)
    if not base or image is None:
        return None
    rva, forward = pe_export_lookup(image.read_bytes(), export_name)
    if forward:
        dll, separator, name = forward.partition(".")
        if not separator or not dll or name.startswith("#"):
            return None
        if not dll.lower().endswith(".dll"):
            dll += ".dll"
        return resolve_process_export(pid, dll, name, depth + 1)
    if rva is None:
        return None
    return base + rva


def _wecom_breakpoints(pid: int, cache: dict[str, list[int]]) -> list[int]:
    key = f"wecom:{pid}"
    if key in cache:
        return cache[key]
    found: list[int] = []
    for module_name, export_name in WECOM_CRYPTO_EXPORTS:
        address = resolve_process_export(pid, module_name, export_name)
        if address:
            found.append(address)
    unique = list(dict.fromkeys(found))
    if len(unique) < 2:
        base = module_base(pid, "WXWork.exe")
        image = _image_for_module(pid, "WXWork.exe")
        if base and image is not None:
            unique.extend(base + item.rva for item in locate_wecom_breakpoints(image.read_bytes()))
            unique = list(dict.fromkeys(unique))
    cache[key] = unique[:8]
    return cache[key]


def _wechat_sqlite_master(target: EncryptedTarget, key: bytearray) -> bool:
    from .wechat_nt import _open_readonly

    try:
        connection = _open_readonly(target, key)
        try:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        finally:
            connection.close()
    except Exception:
        return False
    return bool(tables)


def _match_wechat(hit: CaptureHit, targets: list[EncryptedTarget], matched: dict[str, bytearray]) -> bool:
    pages = {target.name: target.path.read_bytes()[:4096] for target in targets}
    for blob in hit.blobs:
        for material in material_from_blob(blob):
            for target in targets:
                if target.name in matched:
                    continue
                page = pages[target.name]
                raw = material
                chosen: bytearray | None = None
                if len(raw) == 32 and verify_sqlcipher4_raw_key(page, raw):
                    chosen = bytearray(raw)
                elif len(raw) == 32:
                    derived = hashlib.pbkdf2_hmac("sha512", raw, target.salt, 256_000, dklen=32)
                    if verify_sqlcipher4_raw_key(page, derived):
                        chosen = bytearray(derived)
                if chosen is None:
                    continue
                if _wechat_sqlite_master(target, chosen):
                    matched[target.name] = chosen
                else:
                    zero_secret(chosen)
    return all(target.name in matched for target in targets)


def _match_wecom(hit: CaptureHit, targets: list[WecomTarget], matched: dict[str, bytearray]) -> bool:
    from .wecom import _verify_sqlite_master

    for blob in hit.blobs:
        for material in material_from_blob(blob):
            if len(material) != 16:
                continue
            for target in targets:
                if target.name in matched:
                    continue
                if not verify_wxsqlite3_key(material, target.page_one):
                    continue
                try:
                    tables = _verify_sqlite_master(target, material)
                except Exception:
                    continue
                if tables:
                    matched[target.name] = bytearray(material)
    return all(target.name in matched for target in targets)


def _close_processes(executable_name: str) -> None:
    subprocess.run(
        ["taskkill", "/IM", executable_name, "/T"],
        capture_output=True,
        text=True,
        check=False,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and list_processes(executable_name):
        time.sleep(0.4)
    if list_processes(executable_name):
        subprocess.run(
            ["taskkill", "/IM", executable_name, "/F", "/T"],
            capture_output=True,
            text=True,
            check=False,
        )
        time.sleep(1)


def _launch(path: Path) -> None:
    subprocess.Popen([str(path)], close_fds=False)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if list_processes(path.name):
            time.sleep(1.5)
            return
        time.sleep(0.4)


def _first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def _status(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _locate_from_disk(paths: Iterable[Path], locator) -> list[dict[str, int | str]]:
    notes: list[dict[str, int | str]] = []
    image = _first_existing(paths)
    if image is None:
        return notes
    for item in locator(image.read_bytes()):
        notes.append({"name": item.name, "rva": item.rva})
    return notes


def _wait_for_processes(executable_name: str, timeout: float = 30) -> list:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        processes = list_processes(executable_name)
        if processes:
            return processes
        time.sleep(0.3)
    return list_processes(executable_name)


def capture_wechat_keys(
    documents: str | Path | None = None,
    *,
    restart: bool = False,
    timeout: float = 180,
) -> CaptureResult:
    targets = locate_targets(documents)
    matched: dict[str, bytearray] = {}
    stats: dict[str, Any] = {"mode": "debug_hwbp", "platform": "wechat", "restarted": False}
    if not targets:
        return CaptureResult(matched, {**stats, "error": "未找到个微 message_0.db/contact.db"})
    located_note = _locate_from_disk(WECHAT_DLL_CANDIDATES, locate_wechat_breakpoints)
    stats["breakpoints"] = located_note
    if not located_note:
        return CaptureResult(matched, {**stats, "error": "未能在 Weixin.dll 中定位 setCipherKey"})
    if restart:
        exe = _first_existing(WECHAT_EXE_CANDIDATES)
        if exe is None:
            return CaptureResult(matched, {**stats, "error": "未找到 Weixin.exe"})
        _status("正在结束 Weixin.exe 以便在登录阶段捕获密钥…")
        _close_processes("Weixin.exe")
        _launch(exe)
        stats["restarted"] = True
    processes = _wait_for_processes("Weixin.exe")
    if not processes:
        return CaptureResult(matched, {**stats, "error": "未找到 Weixin.exe 进程"})
    cache: dict[str, list[int]] = {}
    _status("已附加调试器，请在微信中完成登录（硬件执行断点，不写入微信文件）…")

    def addresses(pid: int) -> list[int]:
        return _wechat_breakpoints(pid, cache)

    def on_hit(hit: CaptureHit) -> bool:
        return _match_wechat(hit, targets, matched)

    debug_stats = attach_and_wait(
        [item.pid for item in processes],
        addresses,
        on_hit,
        timeout=timeout,
        wow64=False,
        extra_pids=lambda: [item.pid for item in list_processes("Weixin.exe")],
    )
    stats.update(debug_stats)
    stats["verified"] = len(matched)
    return CaptureResult(matched, stats)


def capture_wecom_keys(
    documents: str | Path | None = None,
    *,
    restart: bool = False,
    timeout: float = 180,
) -> CaptureResult:
    targets = locate_wecom_targets(documents, include_all=True)
    matched: dict[str, bytearray] = {}
    stats: dict[str, Any] = {"mode": "debug_hwbp", "platform": "wecom", "restarted": False}
    if not targets:
        return CaptureResult(matched, {**stats, "error": "未找到企微 Data/message.db"})
    stats["breakpoints"] = _locate_from_disk(WECOM_EXE_CANDIDATES, locate_wecom_breakpoints)
    if restart:
        exe = _first_existing(WECOM_EXE_CANDIDATES)
        if exe is None:
            return CaptureResult(matched, {**stats, "error": "未找到 WXWork.exe"})
        _status("正在结束 WXWork.exe 以便在登录阶段捕获密钥…")
        _close_processes("WXWork.exe")
        _launch(exe)
        stats["restarted"] = True
    enable_debug_privilege()
    processes = _wait_for_processes("WXWork.exe")
    if not processes:
        return CaptureResult(matched, {**stats, "error": "未找到 WXWork.exe 进程"})
    cache: dict[str, list[int]] = {}
    stats["crypto_breakpoint_count"] = len(_wecom_breakpoints(processes[0].pid, cache))
    if stats["crypto_breakpoint_count"] < 1:
        return CaptureResult(matched, {**stats, "error": "未能解析企微进程中的 BCrypt/Crypt 导出"})
    _status("已附加调试器。请立刻在企微中点开多个会话、翻聊天记录以触发解密（不写入企微文件）…")

    def addresses(pid: int) -> list[int]:
        return _wecom_breakpoints(pid, cache)

    def on_hit(hit: CaptureHit) -> bool:
        return _match_wecom(hit, targets, matched)

    wow64 = any(is_wow64_process(item.pid) for item in processes) or True
    debug_stats = attach_and_wait(
        [item.pid for item in processes],
        addresses,
        on_hit,
        timeout=timeout,
        wow64=wow64,
        extra_pids=lambda: [item.pid for item in list_processes("WXWork.exe")],
    )
    stats.update(debug_stats)
    stats["verified"] = len(matched)
    return CaptureResult(matched, stats)
