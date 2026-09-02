from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import os
import struct
import time
from typing import Callable, Iterable

from .windows_memory import (
    list_threads,
    read_process_memory,
)


DBG_CONTINUE = 0x00010002
DBG_EXCEPTION_NOT_HANDLED = 0x80010001
DEBUG_EVENT_EXCEPTION = 1
DEBUG_EVENT_CREATE_THREAD = 2
DEBUG_EVENT_CREATE_PROCESS = 3
DEBUG_EVENT_EXIT_THREAD = 4
DEBUG_EVENT_EXIT_PROCESS = 5
DEBUG_EVENT_LOAD_DLL = 6
EXCEPTION_BREAKPOINT = 0x80000003
EXCEPTION_SINGLE_STEP = 0x80000004
THREAD_GET_CONTEXT = 0x0008
THREAD_SET_CONTEXT = 0x0010
THREAD_SUSPEND_RESUME = 0x0002
THREAD_QUERY_INFORMATION = 0x0040
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PAGE_EXECUTE_READWRITE = 0x40
CONTEXT_AMD64 = 0x00100000
CONTEXT_AMD64_FULL_DEBUG = CONTEXT_AMD64 | 0x00000001 | 0x00000002 | 0x00000010
WOW64_CONTEXT_i386 = 0x00010000
WOW64_CONTEXT_FULL_DEBUG = WOW64_CONTEXT_i386 | 0x00000001 | 0x00000002 | 0x00000010
X64_CONTEXT_SIZE = 1232
WOW64_CONTEXT_SIZE = 716
X64_OFF_FLAGS = 0x30
X64_OFF_DR0 = 0x48
X64_OFF_DR7 = 0x70
X64_OFF_RDX = 0x88
X64_OFF_RSP = 0x98
X64_OFF_RSI = 0xA8
X64_OFF_R8 = 0xB8
X64_OFF_R9 = 0xC0
X64_OFF_R14 = 0xE8
X64_OFF_RIP = 0xF8
WOW64_OFF_FLAGS = 0
WOW64_OFF_DR0 = 4
WOW64_OFF_DR7 = 24
WOW64_OFF_EDX = 168
WOW64_OFF_ECX = 172
WOW64_OFF_EAX = 176
WOW64_OFF_EBP = 180
WOW64_OFF_EIP = 184
WOW64_OFF_EFLAGS = 192
WOW64_OFF_ESP = 196


class EXCEPTION_RECORD(ctypes.Structure):
    _fields_ = [
        ("ExceptionCode", wintypes.DWORD),
        ("ExceptionFlags", wintypes.DWORD),
        ("ExceptionRecord", ctypes.c_void_p),
        ("ExceptionAddress", ctypes.c_void_p),
        ("NumberParameters", wintypes.DWORD),
        ("ExceptionInformation", ctypes.c_ulonglong * 15),
    ]


class EXCEPTION_DEBUG_INFO(ctypes.Structure):
    _fields_ = [("ExceptionRecord", EXCEPTION_RECORD), ("dwFirstChance", wintypes.DWORD)]


class CREATE_THREAD_DEBUG_INFO(ctypes.Structure):
    _fields_ = [
        ("hThread", wintypes.HANDLE),
        ("lpThreadLocalBase", ctypes.c_void_p),
        ("lpStartAddress", ctypes.c_void_p),
    ]


class CREATE_PROCESS_DEBUG_INFO(ctypes.Structure):
    _fields_ = [
        ("hFile", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("lpBaseOfImage", ctypes.c_void_p),
        ("dwDebugInfoFileOffset", wintypes.DWORD),
        ("nDebugInfoSize", wintypes.DWORD),
        ("lpThreadLocalBase", ctypes.c_void_p),
        ("lpStartAddress", ctypes.c_void_p),
        ("lpImageName", ctypes.c_void_p),
        ("fUnicode", wintypes.WORD),
    ]


class LOAD_DLL_DEBUG_INFO(ctypes.Structure):
    _fields_ = [
        ("hFile", wintypes.HANDLE),
        ("lpBaseOfDll", ctypes.c_void_p),
        ("dwDebugInfoFileOffset", wintypes.DWORD),
        ("nDebugInfoSize", wintypes.DWORD),
        ("lpImageName", ctypes.c_void_p),
        ("fUnicode", wintypes.WORD),
    ]


class DEBUG_EVENT_UNION(ctypes.Union):
    _fields_ = [
        ("Exception", EXCEPTION_DEBUG_INFO),
        ("CreateThread", CREATE_THREAD_DEBUG_INFO),
        ("CreateProcessInfo", CREATE_PROCESS_DEBUG_INFO),
        ("LoadDll", LOAD_DLL_DEBUG_INFO),
    ]


class DEBUG_EVENT(ctypes.Structure):
    _fields_ = [
        ("dwDebugEventCode", wintypes.DWORD),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
        ("u", DEBUG_EVENT_UNION),
    ]


@dataclass(frozen=True, slots=True)
class CaptureHit:
    pid: int
    address: int
    blobs: tuple[bytes, ...]
    page_size: int | None
    cipher_version: int | None


def enable_debug_privilege() -> bool:
    if os.name != "nt":
        return False
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    token = wintypes.HANDLE()
    if not kernel32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x28, ctypes.byref(token)):
        return False
    try:

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

        luid = LUID()
        if not advapi.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
            return False
        privileges = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES(luid, 0x2),))
        return bool(
            advapi.AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None)
        )
    finally:
        kernel32.CloseHandle(token)


def dr7_execute(slots: int) -> int:
    value = 0
    for index in range(slots):
        value |= 1 << (index * 2)
    return value


def looks_like_user_pointer(address: int, bitness: int) -> bool:
    if bitness == 64:
        return 0x10000 <= address <= 0x00007FFFFFFFFFFF
    return 0x10000 <= address <= 0x7FFEFFFF


def _u64(buffer: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", buffer, offset)[0]


def _u32(buffer: bytes, offset: int) -> int:
    return struct.unpack_from("<I", buffer, offset)[0]


def _set_u64(buffer: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<Q", buffer, offset, value)


def _set_u32(buffer: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", buffer, offset, value)


def _open_thread(tid: int) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    access = THREAD_GET_CONTEXT | THREAD_SET_CONTEXT | THREAD_QUERY_INFORMATION | THREAD_SUSPEND_RESUME
    return int(kernel32.OpenThread(access, False, tid) or 0)


def _with_suspended_thread(tid: int, callback):
    handle = _open_thread(tid)
    if not handle:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SuspendThread.argtypes = [wintypes.HANDLE]
    kernel32.SuspendThread.restype = wintypes.DWORD
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    previous = kernel32.SuspendThread(handle)
    try:
        if previous == 0xFFFFFFFF:
            return False
        return callback()
    finally:
        if previous != 0xFFFFFFFF:
            kernel32.ResumeThread(handle)
        kernel32.CloseHandle(handle)


def _thread_context(tid: int, wow64: bool) -> bytearray | None:
    handle = _open_thread(tid)
    if not handle:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        size = WOW64_CONTEXT_SIZE if wow64 else X64_CONTEXT_SIZE
        raw = bytearray(size)
        flags = WOW64_CONTEXT_FULL_DEBUG if wow64 else CONTEXT_AMD64_FULL_DEBUG
        flag_off = WOW64_OFF_FLAGS if wow64 else X64_OFF_FLAGS
        _set_u32(raw, flag_off, flags)
        ctx = (ctypes.c_byte * size).from_buffer(raw)
        getter = kernel32.Wow64GetThreadContext if wow64 else kernel32.GetThreadContext
        getter.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        getter.restype = wintypes.BOOL
        if not getter(handle, ctypes.byref(ctx)):
            return None
        return bytearray(raw)
    finally:
        kernel32.CloseHandle(handle)


def _apply_thread_context(tid: int, raw: bytearray, wow64: bool) -> bool:
    handle = _open_thread(tid)
    if not handle:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        size = WOW64_CONTEXT_SIZE if wow64 else X64_CONTEXT_SIZE
        ctx = (ctypes.c_byte * size).from_buffer(raw)
        setter = kernel32.Wow64SetThreadContext if wow64 else kernel32.SetThreadContext
        setter.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        setter.restype = wintypes.BOOL
        return bool(setter(handle, ctypes.byref(ctx)))
    finally:
        kernel32.CloseHandle(handle)


def set_execute_breakpoints(tid: int, addresses: list[int], wow64: bool) -> bool:
    addresses = list(addresses)[:4]
    if not addresses:
        return False

    def _apply() -> bool:
        raw = _thread_context(tid, wow64)
        if raw is None:
            return False
        setter, offset, stride = (_set_u32, WOW64_OFF_DR0, 4) if wow64 else (_set_u64, X64_OFF_DR0, 8)
        for index in range(4):
            setter(raw, offset + index * stride, addresses[index] if index < len(addresses) else 0)
        setter(raw, WOW64_OFF_DR7 if wow64 else X64_OFF_DR7, dr7_execute(len(addresses)))
        # Update debug registers only; never replay stale general registers.
        _set_u32(raw, WOW64_OFF_FLAGS if wow64 else X64_OFF_FLAGS,
                 (WOW64_CONTEXT_i386 if wow64 else CONTEXT_AMD64) | 0x10)
        return _apply_thread_context(tid, raw, wow64)

    return bool(_with_suspended_thread(tid, _apply))


def clear_execute_breakpoints(tid: int, wow64: bool) -> bool:
    def _apply() -> bool:
        raw = _thread_context(tid, wow64)
        if raw is None:
            return False
        if wow64:
            for index in range(4):
                _set_u32(raw, WOW64_OFF_DR0 + index * 4, 0)
            _set_u32(raw, WOW64_OFF_DR7, 0)
            _set_u32(raw, WOW64_OFF_FLAGS, WOW64_CONTEXT_i386 | 0x10)
        else:
            for index in range(4):
                _set_u64(raw, X64_OFF_DR0 + index * 8, 0)
            _set_u64(raw, X64_OFF_DR7, 0)
            _set_u32(raw, X64_OFF_FLAGS, CONTEXT_AMD64 | 0x10)
        return _apply_thread_context(tid, raw, wow64)

    return bool(_with_suspended_thread(tid, _apply))


def read_unsafe_data_blobs(pid: int, struct_addr: int) -> list[bytes]:
    header = read_process_memory(pid, struct_addr, 32)
    if not header or len(header) < 24:
        return []
    blobs: list[bytes] = []
    layouts = ((_u64(header, 0), _u64(header, 8)), (_u64(header, 8), _u64(header, 16)))
    seen: set[tuple[int, int]] = set()
    for pointer, size in layouts:
        if (pointer, size) in seen:
            continue
        seen.add((pointer, size))
        if size not in {16, 32, 64, 67, 99} or not looks_like_user_pointer(pointer, 64):
            continue
        blob = read_process_memory(pid, pointer, int(size))
        if blob and len(blob) == size:
            blobs.append(blob)
    return blobs


def read_x64_hit(pid: int, tid: int) -> CaptureHit | None:
    raw = _thread_context(tid, False)
    if raw is None:
        return None
    rdx = _u64(raw, X64_OFF_RDX)
    r8 = _u64(raw, X64_OFF_R8)
    r9 = _u64(raw, X64_OFF_R9)
    r14 = _u64(raw, X64_OFF_R14)
    rsi = _u64(raw, X64_OFF_RSI)
    rip = _u64(raw, X64_OFF_RIP)
    blobs: list[bytes] = []
    blobs.extend(read_unsafe_data_blobs(pid, rdx))
    for pointer, size in ((r14, 67), (rsi, 67), (rdx, 32), (rdx, 16)):
        if not looks_like_user_pointer(pointer, 64):
            continue
        blob = read_process_memory(pid, pointer, size)
        if blob and len(blob) == size:
            blobs.append(blob)
    unique = tuple(dict.fromkeys(blobs))
    if not unique:
        return None
    page_size = int(r8) if r8 in {1024, 4096, 8192} else None
    version = int(r9) if r9 in {0, 1, 2, 3, 4} else None
    return CaptureHit(pid, rip, unique, page_size, version)


def read_x86_hit(pid: int, tid: int) -> CaptureHit | None:
    raw = _thread_context(tid, True)
    if raw is None:
        return None
    eip = _u32(raw, WOW64_OFF_EIP)
    ecx = _u32(raw, WOW64_OFF_ECX)
    edx = _u32(raw, WOW64_OFF_EDX)
    eax = _u32(raw, WOW64_OFF_EAX)
    esp = _u32(raw, WOW64_OFF_ESP)
    stack = read_process_memory(pid, esp, 48) or b""
    pointers = [eax, ecx, edx]
    sized: list[tuple[int, int]] = []
    if len(stack) >= 8:
        slots = struct.unpack_from("<" + "I" * (len(stack) // 4), stack)
        pointers.extend(slots[1:])
        for index in range(1, len(slots) - 1):
            length = slots[index + 1]
            if length in {16, 20, 24, 32}:
                sized.append((slots[index], length))
    blobs: list[bytes] = []
    for pointer, length in sized:
        if not looks_like_user_pointer(pointer, 32):
            continue
        blob = read_process_memory(pid, pointer, length)
        if not blob:
            continue
        if len(blob) == 16:
            blobs.append(blob)
        elif len(blob) >= 24 and blob[16:20] == b"sAlT":
            blobs.append(blob[:16])
        elif len(blob) >= 24 and blob.endswith(b"sAlT"):
            blobs.append(blob[:16])
        elif len(blob) >= 16:
            blobs.append(blob[:16])
    for pointer in pointers:
        if not looks_like_user_pointer(pointer, 32):
            continue
        blob = read_process_memory(pid, pointer, 16)
        if blob and len(blob) == 16:
            blobs.append(blob)
        codec = read_process_memory(pid, pointer + 8, 16)
        if codec and len(codec) == 16:
            blobs.append(codec)
        tagged = read_process_memory(pid, pointer, 24)
        if tagged and len(tagged) == 24 and tagged.endswith(b"sAlT"):
            blobs.append(tagged[:16])
    ebp = _u32(raw, WOW64_OFF_EBP)
    if looks_like_user_pointer(ebp, 32):
        slot = read_process_memory(pid, ebp + 12, 4)
        if slot and len(slot) == 4:
            key_ptr = struct.unpack("<I", slot)[0]
            if looks_like_user_pointer(key_ptr, 32):
                blob = read_process_memory(pid, key_ptr, 16)
                if blob and len(blob) == 16:
                    blobs.append(blob)
    unique = tuple(dict.fromkeys(blobs))
    if not unique:
        return None
    return CaptureHit(pid, eip, unique, None, None)


def write_process_memory(pid: int, address: int, data: bytes) -> bool:
    if os.name != "nt" or not data:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION,
        False,
        pid,
    )
    if not handle:
        return False
    try:
        old = wintypes.DWORD(0)
        kernel32.VirtualProtectEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_size_t,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.VirtualProtectEx.restype = wintypes.BOOL
        kernel32.VirtualProtectEx(
            handle, ctypes.c_void_p(address), len(data), PAGE_EXECUTE_READWRITE, ctypes.byref(old)
        )
        written = ctypes.c_size_t(0)
        kernel32.WriteProcessMemory.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.WriteProcessMemory.restype = wintypes.BOOL
        buffer = ctypes.create_string_buffer(data, len(data))
        ok = bool(
            kernel32.WriteProcessMemory(
                handle, ctypes.c_void_p(address), buffer, len(data), ctypes.byref(written)
            )
        )
        kernel32.FlushInstructionCache.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
        kernel32.FlushInstructionCache.restype = wintypes.BOOL
        kernel32.FlushInstructionCache(handle, ctypes.c_void_p(address), len(data))
        return ok and written.value == len(data)
    finally:
        kernel32.CloseHandle(handle)


def install_software_breakpoints(pid: int, addresses: list[int], originals: dict[tuple[int, int], bytes]) -> int:
    armed = 0
    for address in addresses:
        key = (pid, address)
        if key in originals:
            armed += 1
            continue
        current = read_process_memory(pid, address, 1)
        if not current or current == b"\xcc":
            continue
        if write_process_memory(pid, address, b"\xcc"):
            originals[key] = current
            armed += 1
    return armed


def restore_software_breakpoints(originals: dict[tuple[int, int], bytes], pid: int | None = None) -> None:
    for key, original in list(originals.items()):
        process_id, address = key
        if pid is not None and process_id != pid:
            continue
        write_process_memory(process_id, address, original)
        originals.pop(key, None)


def _set_wow64_eip_and_trap(tid: int, eip: int | None, trap: bool | None) -> bool:
    raw = _thread_context(tid, True)
    if raw is None:
        return False
    if eip is not None:
        _set_u32(raw, WOW64_OFF_EIP, eip & 0xFFFFFFFF)
    if trap is not None:
        eflags = _u32(raw, WOW64_OFF_EFLAGS)
        eflags = (eflags | 0x100) if trap else (eflags & ~0x100)
        _set_u32(raw, WOW64_OFF_EFLAGS, eflags)
    _set_u32(raw, WOW64_OFF_FLAGS, WOW64_CONTEXT_FULL_DEBUG)
    return _apply_thread_context(tid, raw, True)


def _resume_hardware_breakpoint(tid: int, wow64: bool) -> bool:
    raw = _thread_context(tid, wow64)
    if raw is None:
        return False
    offset = WOW64_OFF_EFLAGS if wow64 else 0x44
    # RF allows the interrupted instruction to execute without trapping again.
    _set_u32(raw, offset, _u32(raw, offset) | 0x10000)
    _set_u32(raw, WOW64_OFF_FLAGS if wow64 else X64_OFF_FLAGS,
             (WOW64_CONTEXT_i386 if wow64 else CONTEXT_AMD64) | 0x1)
    return _apply_thread_context(tid, raw, wow64)


def attach_and_wait(
    pids: Iterable[int],
    addresses_for_pid: Callable[[int], list[int]],
    on_hit: Callable[[CaptureHit], bool],
    *,
    timeout: float,
    wow64: bool,
    extra_pids: Callable[[], Iterable[int]] | None = None,
    on_progress: Callable[[dict[str, int]], None] | None = None,
) -> dict[str, int]:
    """Hardware-only capture; always continue pending events and detach normally."""
    if os.name != "nt":
        return {"attached": 0, "hits": 0}
    enable_debug_privilege()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for name in ("DebugActiveProcess", "DebugActiveProcessStop"):
        function = getattr(kernel32, name)
        function.argtypes, function.restype = [wintypes.DWORD], wintypes.BOOL
    kernel32.DebugSetProcessKillOnExit.argtypes = [wintypes.BOOL]
    kernel32.DebugSetProcessKillOnExit.restype = wintypes.BOOL
    kernel32.WaitForDebugEvent.argtypes = [ctypes.POINTER(DEBUG_EVENT), wintypes.DWORD]
    kernel32.WaitForDebugEvent.restype = wintypes.BOOL
    kernel32.ContinueDebugEvent.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    kernel32.ContinueDebugEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    attached: set[int] = set()
    attempted: set[int] = set()
    bindings: dict[tuple[int, int], tuple[int, ...]] = {}
    stats = {"attached": 0, "hits": 0, "breakpoint_events": 0, "armed_threads": 0,
             "software_breakpoints": 0, "detached": 0, "detach_failed": 0,
             "clear_failed": 0, "attach_failed": 0, "continue_failed": 0}
    pending: tuple[int, int, int] | None = None
    deadline = time.monotonic() + timeout

    def try_attach(pid: int) -> None:
        if pid in attempted:
            return
        attempted.add(pid)
        if not kernel32.DebugActiveProcess(pid):
            stats["attach_failed"] += 1
            return
        attached.add(pid)
        stats["attached"] += 1
        # Must be called after a successful attachment. The debugger must not
        # terminate the client when the capture process exits.
        if not kernel32.DebugSetProcessKillOnExit(False):
            raise OSError("Could not disable debugger kill-on-exit")

    def arm_pid(pid: int) -> None:
        addresses = tuple(addresses_for_pid(pid)[:4])
        if not addresses:
            return
        changed = False
        for thread in list_threads(pid):
            key = (pid, thread.tid)
            if bindings.get(key) == addresses:
                continue
            if set_execute_breakpoints(thread.tid, list(addresses), wow64):
                bindings[key] = addresses
                changed = True
        stats["armed_threads"] = len(bindings)
        if changed and on_progress is not None:
            on_progress(dict(stats))

    try:
        for pid in pids:
            try_attach(pid)
        while time.monotonic() < deadline:
            if extra_pids is not None:
                for pid in extra_pids():
                    try_attach(pid)
            if not attached:
                break
            event = DEBUG_EVENT()
            if not kernel32.WaitForDebugEvent(ctypes.byref(event), 250):
                for pid in list(attached):
                    arm_pid(pid)
                continue
            pid, tid = int(event.dwProcessId), int(event.dwThreadId)
            code = int(event.dwDebugEventCode)
            status, done = DBG_CONTINUE, False
            pending = (pid, tid, status)
            if code in (DEBUG_EVENT_CREATE_PROCESS, DEBUG_EVENT_CREATE_THREAD, DEBUG_EVENT_LOAD_DLL):
                # File handles from debug events belong to the debugger.
                handle = (event.u.CreateProcessInfo.hFile if code == DEBUG_EVENT_CREATE_PROCESS
                          else event.u.LoadDll.hFile if code == DEBUG_EVENT_LOAD_DLL else None)
                if handle:
                    kernel32.CloseHandle(handle)
                if pid in attached:
                    arm_pid(pid)
            elif code == DEBUG_EVENT_EXCEPTION:
                record = event.u.Exception.ExceptionRecord
                exc, address = int(record.ExceptionCode), int(record.ExceptionAddress or 0)
                if exc in (EXCEPTION_BREAKPOINT, 0x4000001F):
                    # Native and WOW64 initial attach breakpoints.
                    status = DBG_CONTINUE
                elif exc in (EXCEPTION_SINGLE_STEP, 0x4000001E) and address in bindings.get((pid, tid), ()):
                    stats["breakpoint_events"] += 1
                    if not _resume_hardware_breakpoint(tid, wow64):
                        raise OSError("Could not resume hardware breakpoint")
                    hit = (read_x86_hit if wow64 else read_x64_hit)(pid, tid)
                    if hit is not None:
                        stats["hits"] += 1
                        done = on_hit(hit)
                else:
                    status = DBG_EXCEPTION_NOT_HANDLED
            elif code == DEBUG_EVENT_EXIT_THREAD:
                bindings.pop((pid, tid), None)
            elif code == DEBUG_EVENT_EXIT_PROCESS:
                attached.discard(pid)
                for key in [key for key in bindings if key[0] == pid]:
                    bindings.pop(key, None)
            pending = (pid, tid, status)
            if done:
                break  # cleanup clears the breakpoint before continuing this event
            if not kernel32.ContinueDebugEvent(*pending):
                stats["continue_failed"] += 1
                raise OSError("Could not continue debug event")
            pending = None
        return stats
    finally:
        try:
            for pid in list(attached):
                for thread in list_threads(pid):
                    if (pid, thread.tid) in bindings and not clear_execute_breakpoints(thread.tid, wow64):
                        stats["clear_failed"] += 1
        finally:
            if pending is not None and not kernel32.ContinueDebugEvent(*pending):
                stats["continue_failed"] += 1
            for pid in list(attached):
                if kernel32.DebugActiveProcessStop(pid):
                    stats["detached"] += 1
                else:
                    stats["detach_failed"] += 1
