from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct


CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher\x00"
PRIORITY_HIGHEST = b"\x41\xb9\x00\x00\x00\x80"  # mov r9d, 0x80000000
CIPHER_HANDLE_QUOTE = b"\x41\xc6\x46\x42\x27"  # mov byte ptr [r14+0x42], '\''
CIPHER_HANDLE_LEN67 = b"\x41\xb8\x43\x00\x00\x00"  # mov r8d, 67
WECOM_SALT_IMM = b"\xc7\x45\xf4sAlT"  # mov dword [ebp-0xC], 'sAlT'
WECOM_PROLOGUE = b"\x55\x8b\xec\x81\xec"


@dataclass(frozen=True, slots=True)
class PeSection:
    name: bytes
    va: int
    vsize: int
    raw: int
    rsize: int


@dataclass(frozen=True, slots=True)
class LocatedBreakpoint:
    rva: int
    name: str
    module: str
    bitness: int


def parse_pe_sections(data: bytes) -> tuple[int, list[PeSection]]:
    if data[:2] != b"MZ":
        raise ValueError("not a PE image")
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
        raise ValueError("invalid PE signature")
    machine, nsec = struct.unpack_from("<HH", data, e_lfanew + 4)
    opt_size = struct.unpack_from("<H", data, e_lfanew + 20)[0]
    opt = e_lfanew + 24
    sec_off = opt + opt_size
    bitness = 32 if machine == 0x14C else 64
    sections: list[PeSection] = []
    for index in range(nsec):
        offset = sec_off + index * 40
        name = data[offset : offset + 8].split(b"\x00", 1)[0]
        vsize, va, rsize, raw = struct.unpack_from("<IIII", data, offset + 8)
        sections.append(PeSection(name, va, vsize, raw, rsize))
    return bitness, sections


def rva_to_offset(sections: list[PeSection], rva: int) -> int | None:
    for section in sections:
        if section.va <= rva < section.va + max(section.vsize, section.rsize):
            return section.raw + (rva - section.va)
    return None


def offset_to_rva(sections: list[PeSection], file_offset: int) -> int | None:
    for section in sections:
        if section.raw <= file_offset < section.raw + section.rsize:
            return section.va + (file_offset - section.raw)
    return None


def _text_slice(data: bytes, sections: list[PeSection]) -> tuple[int, bytes]:
    text = next((item for item in sections if item.name == b".text"), sections[0])
    return text.va, data[text.raw : text.raw + text.rsize]


def rip_relative_xrefs(text_va: int, text: bytes, target_rva: int) -> list[int]:
    hits: list[int] = []
    for offset in range(0, len(text) - 6):
        rex = text[offset]
        if rex not in (0x48, 0x4C, 0x49, 0x4D):
            continue
        if text[offset + 1] not in (0x8D, 0x8B):
            continue
        if (text[offset + 2] & 0xC7) != 0x05:
            continue
        rel = struct.unpack_from("<i", text, offset + 3)[0]
        insn_rva = text_va + offset
        if insn_rva + 7 + rel == target_rva:
            hits.append(insn_rva)
    return hits


def runtime_function_containing(data: bytes, sections: list[PeSection], rva: int) -> tuple[int, int] | None:
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    opt = e_lfanew + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic != 0x20B:
        return None
    exc_rva, exc_size = struct.unpack_from("<II", data, opt + 112 + 3 * 8)
    exc_off = rva_to_offset(sections, exc_rva)
    if exc_off is None or exc_size < 12:
        return None
    count = exc_size // 12
    lo, hi = 0, count
    while lo < hi:
        mid = (lo + hi) // 2
        begin, end, _unwind = struct.unpack_from("<III", data, exc_off + mid * 12)
        if rva < begin:
            hi = mid
        elif rva >= end:
            lo = mid + 1
        else:
            return begin, end
    return None


def _in_data_section(sections: list[PeSection], rva: int) -> bool:
    return any(item.name == b".data" and item.va <= rva < item.va + max(item.vsize, item.rsize) for item in sections)


def locate_wechat_breakpoints(image: bytes, *, module: str = "Weixin.dll") -> list[LocatedBreakpoint]:
    bitness, sections = parse_pe_sections(image)
    if bitness != 64:
        return []
    name_off = image.find(CIPHER_NAME)
    if name_off < 0:
        return []
    name_rva = offset_to_rva(sections, name_off)
    if name_rva is None:
        return []
    text_va, text = _text_slice(image, sections)
    string_xrefs = rip_relative_xrefs(text_va, text, name_rva)
    globals_found: set[int] = set()
    for xref in string_xrefs:
        off = rva_to_offset(sections, xref)
        if off is None or off < 7:
            continue
        prev = image[off - 7 : off]
        if prev[0] in (0x48, 0x4C) and prev[1] == 0x8D and (prev[2] & 0xC7) == 0x05:
            disp = struct.unpack_from("<i", prev, 3)[0]
            target = (xref - 7) + 7 + disp
            if _in_data_section(sections, target):
                globals_found.add(target)
    found: dict[str, LocatedBreakpoint] = {}
    for global_rva in globals_found:
        for xref in rip_relative_xrefs(text_va, text, global_rva):
            bounds = runtime_function_containing(image, sections, xref)
            if bounds is None:
                continue
            start, end = bounds
            body_off = rva_to_offset(sections, start)
            if body_off is None:
                continue
            body = image[body_off : body_off + (end - start)]
            if PRIORITY_HIGHEST in body:
                found["set_cipher_key"] = LocatedBreakpoint(start, "set_cipher_key", module, 64)
    handle_at = text.find(CIPHER_HANDLE_QUOTE)
    while handle_at >= 0:
        if text.find(CIPHER_HANDLE_LEN67, handle_at, handle_at + 24) >= 0:
            site = text_va + handle_at
            bounds = runtime_function_containing(image, sections, site)
            if bounds is not None:
                found["cipher_handle"] = LocatedBreakpoint(bounds[0], "cipher_handle", module, 64)
                break
        handle_at = text.find(CIPHER_HANDLE_QUOTE, handle_at + 1)
    order = ["set_cipher_key", "cipher_handle"]
    return [found[name] for name in order if name in found]


def _walk_msvc32_prologue(data: bytes, site: int) -> int | None:
    start = max(0, site - 0x200)
    window = data[start:site]
    marker = WECOM_PROLOGUE
    last = window.rfind(marker)
    if last < 0:
        return None
    prefix = window[max(0, last - 16) : last]
    if prefix and not all(byte in (0xCC, 0x90) for byte in prefix[-min(8, len(prefix)) :]):
        # still accept a nearby INT3-padded prologue
        if 0xCC not in prefix[-16:]:
            return start + last
    return start + last


def locate_wecom_breakpoints(image: bytes, *, module: str = "WXWork.exe") -> list[LocatedBreakpoint]:
    bitness, sections = parse_pe_sections(image)
    if bitness != 32:
        return []
    site = image.find(WECOM_SALT_IMM)
    found: list[LocatedBreakpoint] = []
    while site >= 0:
        prologue = _walk_msvc32_prologue(image, site)
        if prologue is not None:
            rva = offset_to_rva(sections, prologue)
            if rva is not None:
                found.append(LocatedBreakpoint(rva, "wxsqlite3_salt_derive", module, 32))
                load = image.find(b"\x0f\x10\x00", site, site + 64)
                if load >= 0:
                    load_rva = offset_to_rva(sections, load)
                    if load_rva is not None:
                        found.append(LocatedBreakpoint(load_rva, "wxsqlite3_key_load", module, 32))
                break
        site = image.find(WECOM_SALT_IMM, site + 1)
    return found


def pe_export_lookup(data: bytes, symbol: str) -> tuple[int | None, str | None]:
    """Return (rva, None) for a code export, or (None, 'dll.Name') for a forwarded export."""
    if data[:2] != b"MZ":
        return None, None
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
        return None, None
    opt = e_lfanew + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic == 0x10B:
        export_rva, export_size = struct.unpack_from("<II", data, opt + 96)
    elif magic == 0x20B:
        export_rva, export_size = struct.unpack_from("<II", data, opt + 112)
    else:
        return None, None
    if not export_rva:
        return None, None
    _bitness, sections = parse_pe_sections(data)
    directory = rva_to_offset(sections, export_rva)
    if directory is None:
        return None, None
    number_of_names = min(struct.unpack_from("<I", data, directory + 24)[0], 65_536)
    functions_rva = struct.unpack_from("<I", data, directory + 28)[0]
    names_rva = struct.unpack_from("<I", data, directory + 32)[0]
    ordinals_rva = struct.unpack_from("<I", data, directory + 36)[0]
    functions_off = rva_to_offset(sections, functions_rva)
    names_off = rva_to_offset(sections, names_rva)
    ordinals_off = rva_to_offset(sections, ordinals_rva)
    if None in {functions_off, names_off, ordinals_off}:
        return None, None
    wanted = symbol.encode("ascii")
    for index in range(number_of_names):
        name_rva = struct.unpack_from("<I", data, names_off + index * 4)[0]
        name_off = rva_to_offset(sections, name_rva)
        if name_off is None:
            continue
        end = data.find(b"\x00", name_off, name_off + 96)
        if end < 0:
            continue
        if data[name_off:end] != wanted:
            continue
        ordinal = struct.unpack_from("<H", data, ordinals_off + index * 2)[0]
        function_rva = struct.unpack_from("<I", data, functions_off + ordinal * 4)[0]
        if export_size and export_rva <= function_rva < export_rva + export_size:
            forward_off = rva_to_offset(sections, function_rva)
            if forward_off is None:
                return None, None
            stop = data.find(b"\x00", forward_off, forward_off + 256)
            if stop < 0:
                return None, None
            try:
                return None, data[forward_off:stop].decode("ascii")
            except UnicodeDecodeError:
                return None, None
        return function_rva, None
    return None, None


def pe_export_rva(data: bytes, symbol: str) -> int | None:
    rva, _forward = pe_export_lookup(data, symbol)
    return rva


def locate_image_file(path: str | Path) -> list[LocatedBreakpoint]:
    data = Path(path).read_bytes()
    name = Path(path).name
    if name.casefold() == "weixin.dll":
        return locate_wechat_breakpoints(data, module=name)
    return locate_wecom_breakpoints(data, module=name)
