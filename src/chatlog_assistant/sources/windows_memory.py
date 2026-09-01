from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import os
import re
from typing import Callable, Iterable


PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPTHREAD = 0x00000004
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
MAX_MODULE_NAME32 = 255
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_WRITABLE_PROTECTIONS = {0x04, 0x08, 0x40, 0x80}
_READABLE_PROTECTIONS = _WRITABLE_PROTECTIONS | {0x02, 0x20}
_ASCII_HEX_64 = re.compile(rb"(?<![0-9a-fA-F])([0-9a-fA-F]{64})(?![0-9a-fA-F])")
_UTF16_HEX_64 = re.compile(
    rb"(?<![0-9a-fA-F]\x00)((?:[0-9a-fA-F]\x00){64})(?![0-9a-fA-F]\x00)"
)


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    pid: int
    parent_pid: int
    executable_name: str


@dataclass(frozen=True, slots=True)
class MarkerRegion:
    base_address: int
    region_size: int
    protection: int
    marker_count: int


@dataclass(frozen=True, slots=True)
class ModuleInfo:
    pid: int
    name: str
    path: str
    base_address: int
    size: int


@dataclass(frozen=True, slots=True)
class ThreadInfo:
    pid: int
    tid: int


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * (MAX_MODULE_NAME32 + 1)),
        ("szExePath", wintypes.WCHAR * 260),
    ]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def read_process_memory(pid: int, address: int, size: int) -> bytes | None:
    if os.name != "nt" or size <= 0 or size > 16 * 1024 * 1024:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    read_memory = kernel32.ReadProcessMemory
    read_memory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    read_memory.restype = wintypes.BOOL
    handle = open_process(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_string_buffer(size)
        bytes_read = ctypes.c_size_t(0)
        ok = read_memory(handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(bytes_read))
        if ok and bytes_read.value:
            return buffer.raw[: bytes_read.value]
        return None
    finally:
        kernel32.CloseHandle(handle)


def iter_readable_chunks(
    pid: int,
    *,
    writable_only: bool = True,
    chunk_limit: int = 512 * 1024,
    overlap: int = 32,
):
    """Yield (address, bytes) chunks from committed readable process memory."""
    if os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    virtual_query = kernel32.VirtualQueryEx
    virtual_query.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    virtual_query.restype = ctypes.c_size_t
    read_memory = kernel32.ReadProcessMemory
    read_memory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    read_memory.restype = wintypes.BOOL
    handle = open_process(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return
    allowed = _WRITABLE_PROTECTIONS if writable_only else _READABLE_PROTECTIONS
    address = 0x10000
    try:
        while address < 0x7FFF_FFFF_FFFF:
            mbi = MEMORY_BASIC_INFORMATION()
            queried = virtual_query(handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not queried:
                break
            region_address = int(mbi.BaseAddress or 0)
            region_size = int(mbi.RegionSize or 0)
            protect = int(mbi.Protect)
            base_protect = protect & 0xFF
            readable = (
                mbi.State == MEM_COMMIT
                and base_protect in allowed
                and not (protect & PAGE_GUARD)
                and base_protect != PAGE_NOACCESS
                and 0 < region_size <= 512 * 1024 * 1024
            )
            if readable:
                position = region_address
                end = region_address + region_size
                tail = b""
                tail_address = position
                while position < end:
                    chunk_size = min(chunk_limit, end - position)
                    buffer = ctypes.create_string_buffer(chunk_size)
                    bytes_read = ctypes.c_size_t(0)
                    ok = read_memory(
                        handle,
                        ctypes.c_void_p(position),
                        buffer,
                        chunk_size,
                        ctypes.byref(bytes_read),
                    )
                    if ok and bytes_read.value:
                        data = tail + buffer.raw[: bytes_read.value]
                        yield tail_address, data
                        tail = data[-overlap:] if overlap else b""
                        tail_address = position + bytes_read.value - len(tail)
                    else:
                        tail = b""
                        tail_address = position + chunk_size
                    position += chunk_size
            next_address = region_address + max(region_size, 1)
            if next_address <= address:
                break
            address = next_address
    finally:
        kernel32.CloseHandle(handle)


def find_pattern_addresses(
    pid: int,
    needle: bytes,
    *,
    writable_only: bool = False,
    limit: int = 64,
) -> list[int]:
    if not needle:
        return []
    found: list[int] = []
    overlap = max(len(needle) - 1, 0)
    for address, data in iter_readable_chunks(pid, writable_only=writable_only, overlap=overlap):
        start = 0
        while len(found) < limit:
            index = data.find(needle, start)
            if index < 0:
                break
            found.append(address + index)
            start = index + 1
        if len(found) >= limit:
            break
    return found


def is_wow64_process(pid: int) -> bool:
    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    is_wow64 = kernel32.IsWow64Process
    is_wow64.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    is_wow64.restype = wintypes.BOOL
    handle = open_process(PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        wow = wintypes.BOOL(False)
        if not is_wow64(handle, ctypes.byref(wow)):
            return False
        return bool(wow.value)
    finally:
        kernel32.CloseHandle(handle)


def list_processes(executable_name: str) -> list[ProcessInfo]:
    if os.name != "nt":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    process_next.restype = wintypes.BOOL

    snapshot = create_snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        processes: list[ProcessInfo] = []
        ok = process_first(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.casefold() == executable_name.casefold():
                processes.append(
                    ProcessInfo(
                        pid=int(entry.th32ProcessID),
                        parent_pid=int(entry.th32ParentProcessID),
                        executable_name=entry.szExeFile,
                    )
                )
            ok = process_next(snapshot, ctypes.byref(entry))
        process_ids = {item.pid for item in processes}
        return sorted(processes, key=lambda item: (item.parent_pid in process_ids, item.pid))
    finally:
        kernel32.CloseHandle(snapshot)


def list_modules(pid: int) -> list[ModuleInfo]:
    if os.name != "nt":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    module_first = kernel32.Module32FirstW
    module_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    module_first.restype = wintypes.BOOL
    module_next = kernel32.Module32NextW
    module_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    module_next.restype = wintypes.BOOL

    snapshot = create_snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        modules: list[ModuleInfo] = []
        ok = module_first(snapshot, ctypes.byref(entry))
        while ok:
            modules.append(
                ModuleInfo(
                    pid=pid,
                    name=entry.szModule,
                    path=entry.szExePath,
                    base_address=int(entry.modBaseAddr or 0),
                    size=int(entry.modBaseSize),
                )
            )
            ok = module_next(snapshot, ctypes.byref(entry))
        return modules
    finally:
        kernel32.CloseHandle(snapshot)


def list_threads(pid: int) -> list[ThreadInfo]:
    if os.name != "nt":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    thread_first = kernel32.Thread32First
    thread_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    thread_first.restype = wintypes.BOOL
    thread_next = kernel32.Thread32Next
    thread_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    thread_next.restype = wintypes.BOOL

    snapshot = create_snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        threads: list[ThreadInfo] = []
        ok = thread_first(snapshot, ctypes.byref(entry))
        while ok:
            if int(entry.th32OwnerProcessID) == pid:
                threads.append(ThreadInfo(pid=pid, tid=int(entry.th32ThreadID)))
            ok = thread_next(snapshot, ctypes.byref(entry))
        return threads
    finally:
        kernel32.CloseHandle(snapshot)


def module_base(pid: int, module_name: str) -> int | None:
    wanted = module_name.casefold()
    for item in list_modules(pid):
        if item.name.casefold() == wanted:
            return item.base_address
    return None


def _compile_target_pattern(target_salts: Iterable[bytes]) -> re.Pattern[bytes]:
    salt_hexes = sorted({salt.hex().encode("ascii") for salt in target_salts})
    if not salt_hexes:
        raise ValueError("at least one target salt is required")
    alternatives = b"|".join(re.escape(item) for item in salt_hexes)
    return re.compile(rb"x'([0-9a-fA-F]{64})(" + alternatives + rb")'", re.I)


def extract_matching_keys(data: bytes, target_salts: Iterable[bytes]) -> dict[bytes, bytearray]:
    """Extract only key/salt pairs whose salt belongs to the caller's databases."""
    pattern = _compile_target_pattern(target_salts)
    matches: dict[bytes, bytearray] = {}
    for match in pattern.finditer(data):
        key = bytearray.fromhex(match.group(1).decode("ascii"))
        salt = bytes.fromhex(match.group(2).decode("ascii"))
        matches.setdefault(salt, key)
    return matches


def _append_candidate(
    result: dict[bytes, list[bytearray]],
    seen: dict[bytes, set[bytes]],
    salt: bytes,
    candidate: bytes,
    max_per_salt: int,
) -> None:
    if len(candidate) != 32 or candidate == bytes(32):
        return
    if candidate in seen[salt] or len(result[salt]) >= max_per_salt:
        return
    seen[salt].add(candidate)
    result[salt].append(bytearray(candidate))


def extract_candidate_keys(
    data: bytes,
    target_salts: Iterable[bytes],
    *,
    max_per_salt: int = 256,
) -> dict[bytes, list[bytearray]]:
    """Find plausible adjacent keys without returning unrelated memory content."""
    salts = tuple(target_salts)
    result = {salt: [] for salt in salts}
    seen = {salt: set() for salt in salts}
    lowered = data.lower()
    hex_chars = set(b"0123456789abcdef")

    exact = extract_matching_keys(data, salts)
    for salt, key in exact.items():
        _append_candidate(result, seen, salt, bytes(key), max_per_salt)
        zero_secret(key)

    for salt in salts:
        salt_hex = salt.hex().encode("ascii")
        position = 0
        while len(result[salt]) < max_per_salt:
            index = lowered.find(salt_hex, position)
            if index < 0:
                break
            if index >= 64:
                prefix = lowered[index - 64 : index]
                if all(value in hex_chars for value in prefix):
                    _append_candidate(result, seen, salt, bytes.fromhex(prefix.decode("ascii")), max_per_salt)
            suffix = lowered[index + len(salt_hex) : index + len(salt_hex) + 64]
            if len(suffix) == 64 and all(value in hex_chars for value in suffix):
                _append_candidate(result, seen, salt, bytes.fromhex(suffix.decode("ascii")), max_per_salt)
            position = index + 1

        salt_utf16 = b"".join(bytes((value, 0)) for value in salt_hex)
        position = 0
        while len(result[salt]) < max_per_salt:
            index = lowered.find(salt_utf16, position)
            if index < 0:
                break
            if index >= 128:
                prefix = lowered[index - 128 : index]
                chars = prefix[::2]
                zeros = prefix[1::2]
                if len(chars) == 64 and all(value in hex_chars for value in chars) and not any(zeros):
                    _append_candidate(result, seen, salt, bytes.fromhex(chars.decode("ascii")), max_per_salt)
            suffix = lowered[index + len(salt_utf16) : index + len(salt_utf16) + 128]
            chars = suffix[::2]
            zeros = suffix[1::2]
            if len(chars) == 64 and all(value in hex_chars for value in chars) and not any(zeros):
                _append_candidate(result, seen, salt, bytes.fromhex(chars.decode("ascii")), max_per_salt)
            position = index + 2

        position = 0
        while len(result[salt]) < max_per_salt:
            index = data.find(salt, position)
            if index < 0:
                break
            if index >= 32:
                _append_candidate(result, seen, salt, data[index - 32 : index], max_per_salt)
            following = data[index + len(salt) : index + len(salt) + 32]
            if len(following) == 32:
                _append_candidate(result, seen, salt, following, max_per_salt)
            position = index + 1
    return result


def extract_hex_secrets(data: bytes, *, max_candidates: int = 2048) -> list[bytearray]:
    """Return bounded 32-byte values represented as standalone 64-char hex strings."""
    result: list[bytearray] = []
    seen: set[bytes] = set()

    def append(value: bytes) -> None:
        if len(result) >= max_candidates:
            return
        decoded = bytes.fromhex(value.decode("ascii"))
        if decoded == bytes(32) or decoded in seen:
            return
        seen.add(decoded)
        result.append(bytearray(decoded))

    for match in _ASCII_HEX_64.finditer(data):
        append(match.group(1))
        if len(result) >= max_candidates:
            return result
    for match in _UTF16_HEX_64.finditer(data):
        append(match.group(1)[::2])
        if len(result) >= max_candidates:
            break
    return result


def _merge_candidates(
    destination: dict[bytes, list[bytearray]],
    source: dict[bytes, list[bytearray]],
    max_per_salt: int,
) -> None:
    for salt, candidates in source.items():
        existing = {bytes(item) for item in destination[salt]}
        for candidate in candidates:
            candidate_bytes = bytes(candidate)
            if candidate_bytes not in existing and len(destination[salt]) < max_per_salt:
                destination[salt].append(candidate)
                existing.add(candidate_bytes)
            else:
                zero_secret(candidate)


def scan_process_for_hex_secrets(
    pid: int,
    *,
    writable_only: bool = True,
    max_candidates: int = 2048,
) -> tuple[list[bytearray], dict[str, int]]:
    """Read process memory and collect only bounded 64-char hex candidates."""
    if os.name != "nt":
        raise OSError("Windows process scanning is only available on Windows")
    found: list[bytearray] = []
    seen: set[bytes] = set()
    stats = {"regions": 0, "bytes": 0}
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    virtual_query = kernel32.VirtualQueryEx
    virtual_query.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    virtual_query.restype = ctypes.c_size_t
    read_memory = kernel32.ReadProcessMemory
    read_memory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    read_memory.restype = wintypes.BOOL

    handle = open_process(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        stats["open_error"] = ctypes.get_last_error()
        return found, stats

    allowed = _WRITABLE_PROTECTIONS if writable_only else _READABLE_PROTECTIONS
    address = 0x10000
    overlap = 132
    try:
        while address < 0x7FFF_FFFF_FFFF and len(found) < max_candidates:
            mbi = MEMORY_BASIC_INFORMATION()
            queried = virtual_query(handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not queried:
                break
            region_address = int(mbi.BaseAddress or 0)
            region_size = int(mbi.RegionSize or 0)
            protect = int(mbi.Protect)
            base_protect = protect & 0xFF
            readable = (
                mbi.State == MEM_COMMIT
                and base_protect in allowed
                and not (protect & PAGE_GUARD)
                and base_protect != PAGE_NOACCESS
                and 0 < region_size <= 512 * 1024 * 1024
            )
            if readable:
                stats["regions"] += 1
                position = region_address
                end = region_address + region_size
                tail = b""
                while position < end and len(found) < max_candidates:
                    chunk_size = min(512 * 1024, end - position)
                    buffer = ctypes.create_string_buffer(chunk_size)
                    bytes_read = ctypes.c_size_t(0)
                    ok = read_memory(handle, ctypes.c_void_p(position), buffer, chunk_size, ctypes.byref(bytes_read))
                    if ok and bytes_read.value:
                        stats["bytes"] += int(bytes_read.value)
                        data = tail + buffer.raw[: bytes_read.value]
                        batch = extract_hex_secrets(data, max_candidates=max_candidates - len(found))
                        for candidate in batch:
                            raw = bytes(candidate)
                            if raw not in seen:
                                seen.add(raw)
                                found.append(candidate)
                            else:
                                zero_secret(candidate)
                        tail = data[-overlap:]
                    else:
                        tail = b""
                    position += chunk_size
            next_address = region_address + max(region_size, 1)
            if next_address <= address:
                break
            address = next_address
    finally:
        kernel32.CloseHandle(handle)
    return found, stats


def find_process_marker_regions(
    pid: int,
    markers: Iterable[bytes],
    *,
    writable_only: bool = True,
) -> tuple[dict[bytes, list[MarkerRegion]], dict[str, int]]:
    """Locate readable regions containing caller-provided markers without returning memory."""
    if os.name != "nt":
        raise OSError("Windows process scanning is only available on Windows")
    wanted = tuple(dict.fromkeys(markers))
    found = {marker: [] for marker in wanted}
    stats = {"regions": 0, "bytes": 0}
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    virtual_query = kernel32.VirtualQueryEx
    virtual_query.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    virtual_query.restype = ctypes.c_size_t
    read_memory = kernel32.ReadProcessMemory
    read_memory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    read_memory.restype = wintypes.BOOL

    handle = open_process(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        stats["open_error"] = ctypes.get_last_error()
        return found, stats
    allowed = _WRITABLE_PROTECTIONS if writable_only else _READABLE_PROTECTIONS
    address = 0x10000
    overlap = max((len(marker) for marker in wanted), default=1) - 1
    try:
        while address < 0x7FFF_FFFF_FFFF:
            mbi = MEMORY_BASIC_INFORMATION()
            queried = virtual_query(handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not queried:
                break
            region_address = int(mbi.BaseAddress or 0)
            region_size = int(mbi.RegionSize or 0)
            protect = int(mbi.Protect)
            base_protect = protect & 0xFF
            readable = (
                mbi.State == MEM_COMMIT
                and base_protect in allowed
                and not (protect & PAGE_GUARD)
                and base_protect != PAGE_NOACCESS
                and 0 < region_size <= 512 * 1024 * 1024
            )
            if readable:
                stats["regions"] += 1
                counts = {marker: 0 for marker in wanted}
                position = region_address
                end = region_address + region_size
                tail = b""
                while position < end:
                    chunk_size = min(512 * 1024, end - position)
                    buffer = ctypes.create_string_buffer(chunk_size)
                    bytes_read = ctypes.c_size_t(0)
                    ok = read_memory(handle, ctypes.c_void_p(position), buffer, chunk_size, ctypes.byref(bytes_read))
                    if ok and bytes_read.value:
                        stats["bytes"] += int(bytes_read.value)
                        data = tail + buffer.raw[: bytes_read.value]
                        for marker in wanted:
                            counts[marker] += data.count(marker)
                        tail = data[-overlap:] if overlap else b""
                    else:
                        tail = b""
                    position += chunk_size
                for marker, count in counts.items():
                    if count:
                        found[marker].append(
                            MarkerRegion(region_address, region_size, protect, count)
                        )
            next_address = region_address + max(region_size, 1)
            if next_address <= address:
                break
            address = next_address
    finally:
        kernel32.CloseHandle(handle)
    return found, stats


def scan_process_regions_for_key(
    pid: int,
    regions: Iterable[MarkerRegion],
    verifier: Callable[[bytes], bool],
    *,
    alignment: int = 8,
    max_total_bytes: int = 32 * 1024 * 1024,
) -> tuple[bytearray | None, dict[str, int]]:
    """Scan bounded, preselected memory regions and return only a verified key."""
    if os.name != "nt":
        raise OSError("Windows process scanning is only available on Windows")
    if alignment <= 0:
        raise ValueError("alignment must be positive")
    selected: list[MarkerRegion] = []
    selected_bases: set[int] = set()
    selected_bytes = 0
    for region in sorted(regions, key=lambda item: item.region_size):
        if region.base_address in selected_bases:
            continue
        if selected_bytes + region.region_size > max_total_bytes:
            continue
        selected.append(region)
        selected_bases.add(region.base_address)
        selected_bytes += region.region_size

    stats = {"regions": 0, "bytes": 0, "candidates": 0}
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    read_memory = kernel32.ReadProcessMemory
    read_memory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    read_memory.restype = wintypes.BOOL
    handle = open_process(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        stats["open_error"] = ctypes.get_last_error()
        return None, stats

    try:
        for region in selected:
            stats["regions"] += 1
            position = region.base_address
            end = region.base_address + region.region_size
            tail = b""
            while position < end:
                chunk_size = min(512 * 1024, end - position)
                buffer = ctypes.create_string_buffer(chunk_size)
                bytes_read = ctypes.c_size_t(0)
                ok = read_memory(handle, ctypes.c_void_p(position), buffer, chunk_size, ctypes.byref(bytes_read))
                if not ok or not bytes_read.value:
                    tail = b""
                    position += chunk_size
                    continue
                stats["bytes"] += int(bytes_read.value)
                data = tail + buffer.raw[: bytes_read.value]
                data_address = position - len(tail)
                start = (-data_address) % alignment
                stop = len(data) - 31
                for offset in range(start, stop, alignment):
                    candidate = data[offset : offset + 32]
                    if candidate == bytes(32):
                        continue
                    stats["candidates"] += 1
                    if verifier(candidate):
                        return bytearray(candidate), stats
                tail = data[-31:]
                position += chunk_size
    finally:
        kernel32.CloseHandle(handle)
    return None, stats


def scan_process_for_candidate_keys(
    pid: int,
    target_salts: Iterable[bytes],
    *,
    writable_only: bool = True,
    max_per_salt: int = 256,
) -> tuple[dict[bytes, list[bytearray]], dict[str, int]]:
    if os.name != "nt":
        raise OSError("Windows process scanning is only available on Windows")
    salts = tuple(target_salts)
    found = {salt: [] for salt in salts}
    stats = {"regions": 0, "bytes": 0}
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    virtual_query = kernel32.VirtualQueryEx
    virtual_query.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    virtual_query.restype = ctypes.c_size_t
    read_memory = kernel32.ReadProcessMemory
    read_memory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    read_memory.restype = wintypes.BOOL

    handle = open_process(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        stats["open_error"] = ctypes.get_last_error()
        return found, stats

    allowed = _WRITABLE_PROTECTIONS if writable_only else _READABLE_PROTECTIONS
    address = 0x10000
    overlap = 256
    try:
        while address < 0x7FFF_FFFF_FFFF:
            mbi = MEMORY_BASIC_INFORMATION()
            queried = virtual_query(handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not queried:
                break
            region_address = int(mbi.BaseAddress or 0)
            region_size = int(mbi.RegionSize or 0)
            protect = int(mbi.Protect)
            base_protect = protect & 0xFF
            readable = (
                mbi.State == MEM_COMMIT
                and base_protect in allowed
                and not (protect & PAGE_GUARD)
                and base_protect != PAGE_NOACCESS
                and 0 < region_size <= 512 * 1024 * 1024
            )
            if readable:
                stats["regions"] += 1
                position = region_address
                end = region_address + region_size
                tail = b""
                while position < end:
                    chunk_size = min(512 * 1024, end - position)
                    buffer = ctypes.create_string_buffer(chunk_size)
                    bytes_read = ctypes.c_size_t(0)
                    ok = read_memory(handle, ctypes.c_void_p(position), buffer, chunk_size, ctypes.byref(bytes_read))
                    if ok and bytes_read.value:
                        stats["bytes"] += int(bytes_read.value)
                        data = tail + buffer.raw[: bytes_read.value]
                        batch = extract_candidate_keys(data, salts, max_per_salt=max_per_salt)
                        _merge_candidates(found, batch, max_per_salt)
                        tail = data[-overlap:]
                    else:
                        tail = b""
                    position += chunk_size
            next_address = region_address + max(region_size, 1)
            if next_address <= address:
                break
            address = next_address
    finally:
        kernel32.CloseHandle(handle)
    return found, stats


def scan_process_for_keys(
    pid: int,
    target_salts: Iterable[bytes],
    *,
    writable_only: bool = True,
) -> dict[bytes, bytearray]:
    if os.name != "nt":
        raise OSError("Windows process scanning is only available on Windows")

    salts = tuple(target_salts)
    pattern = _compile_target_pattern(salts)
    wanted = {salt.hex().encode("ascii").lower(): salt for salt in salts}
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    virtual_query = kernel32.VirtualQueryEx
    virtual_query.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    virtual_query.restype = ctypes.c_size_t
    read_memory = kernel32.ReadProcessMemory
    read_memory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    read_memory.restype = wintypes.BOOL

    handle = open_process(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return {}

    found: dict[bytes, bytearray] = {}
    allowed = _WRITABLE_PROTECTIONS if writable_only else _READABLE_PROTECTIONS
    address = 0x10000
    maximum_address = 0x7FFF_FFFF_FFFF
    overlap = 128
    chunk_limit = 512 * 1024
    try:
        while address < maximum_address and len(found) < len(salts):
            mbi = MEMORY_BASIC_INFORMATION()
            queried = virtual_query(handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not queried:
                break
            region_address = int(mbi.BaseAddress or 0)
            region_size = int(mbi.RegionSize or 0)
            protect = int(mbi.Protect)
            base_protect = protect & 0xFF
            readable = (
                mbi.State == MEM_COMMIT
                and base_protect in allowed
                and not (protect & PAGE_GUARD)
                and base_protect != PAGE_NOACCESS
                and 0 < region_size <= 512 * 1024 * 1024
            )
            if readable:
                position = region_address
                end = region_address + region_size
                tail = b""
                while position < end and len(found) < len(salts):
                    chunk_size = min(chunk_limit, end - position)
                    buffer = ctypes.create_string_buffer(chunk_size)
                    bytes_read = ctypes.c_size_t(0)
                    ok = read_memory(
                        handle,
                        ctypes.c_void_p(position),
                        buffer,
                        chunk_size,
                        ctypes.byref(bytes_read),
                    )
                    if ok and bytes_read.value:
                        data = tail + buffer.raw[: bytes_read.value]
                        for match in pattern.finditer(data):
                            salt_hex = match.group(2).lower()
                            salt = wanted.get(salt_hex)
                            if salt is not None and salt not in found:
                                found[salt] = bytearray.fromhex(match.group(1).decode("ascii"))
                        tail = data[-overlap:]
                    else:
                        tail = b""
                    position += chunk_size
            next_address = region_address + max(region_size, 1)
            if next_address <= address:
                break
            address = next_address
    finally:
        kernel32.CloseHandle(handle)
    return found


def scan_weixin_for_keys(target_salts: Iterable[bytes], pid: int | None = None) -> tuple[int, dict[bytes, bytearray]]:
    salts = tuple(target_salts)
    processes = list_processes("Weixin.exe")
    if pid is not None:
        processes = [item for item in processes if item.pid == pid]
    for process in processes:
        found = scan_process_for_keys(process.pid, salts, writable_only=True)
        if len(found) < len(salts):
            broader = scan_process_for_keys(
                process.pid,
                [salt for salt in salts if salt not in found],
                writable_only=False,
            )
            found.update(broader)
        if found:
            return process.pid, found
    return 0, {}


def zero_secret(secret: bytearray) -> None:
    for index in range(len(secret)):
        secret[index] = 0
