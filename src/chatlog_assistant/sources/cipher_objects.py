from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Callable, Iterable

from .windows_memory import (
    find_pattern_addresses,
    iter_readable_chunks,
    read_process_memory,
    zero_secret,
)


CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
_HEX = frozenset(b"0123456789abcdefABCDEF")
_SQLCIPHER_BLOB_LENGTHS = frozenset({67, 99, 100, 101})


@dataclass(frozen=True, slots=True)
class CipherCandidate:
    raw_key: bytearray
    salt: bytes | None
    method: str


def xor_repeat(data: bytes, mask: bytes) -> bytes:
    if not mask:
        return data
    return bytes(value ^ mask[index % len(mask)] for index, value in enumerate(data))


def recover_repeating_mask(
    blob: bytes,
    period: int,
    hints: dict[int, int],
    *,
    hex_range: tuple[int, int] | None = None,
) -> bytes | None:
    if period <= 0 or len(blob) < period:
        return None
    mask: list[int | None] = [None] * period
    for index, plaintext in hints.items():
        if index >= len(blob):
            return None
        slot = index % period
        candidate = blob[index] ^ plaintext
        if mask[slot] is None:
            mask[slot] = candidate
        elif mask[slot] != candidate:
            return None

    start, stop = hex_range if hex_range is not None else (0, 0)
    lowercase = frozenset(b"0123456789abcdef")
    mixed = _HEX
    for slot in range(period):
        if mask[slot] is not None:
            continue
        possible: set[int] | None = None
        positions = [index for index in range(slot, len(blob), period) if start <= index < stop]
        if not positions:
            return None
        for charset in (lowercase, mixed):
            current: set[int] | None = None
            for index in positions:
                options = {blob[index] ^ char for char in charset}
                current = options if current is None else current & options
            if current:
                possible = current
                break
        if not possible:
            return None
        if len(possible) == 1:
            mask[slot] = next(iter(possible))
            continue
        best: tuple[int, int] | None = None
        for candidate in possible:
            score = sum(1 for index in positions if (blob[index] ^ candidate) in lowercase)
            if best is None or score > best[0]:
                best = (score, candidate)
        mask[slot] = best[1] if best is not None else None

    if any(item is None for item in mask):
        return None
    recovered = bytes(int(item) for item in mask)
    decoded = xor_repeat(blob, recovered)
    for index, plaintext in hints.items():
        if decoded[index] != plaintext:
            return None
    if hex_range is not None:
        for index in range(hex_range[0], min(hex_range[1], len(decoded))):
            if decoded[index] not in mixed:
                return None
    return recovered


def _cipher_name_masks() -> tuple[bytes, ...]:
    name = CIPHER_NAME
    return tuple(
        item
        for item in (
            name[:27],
            name,
            name[:16],
            name[4:],
            b"Tencent.WCDB.Config.Cipher",
        )
        if item
    )


def _parse_plain_sqlcipher(text: bytes) -> tuple[bytes, bytes | None] | None:
    if not (text.startswith(b"x'") and text.endswith(b"'") and 3 <= len(text) <= 200):
        return None
    inner = text[2:-1]
    if not inner or any(value not in _HEX for value in inner) or len(inner) % 2:
        return None
    decoded = bytes.fromhex(inner.decode("ascii"))
    if len(decoded) == 32:
        return decoded, None
    if len(decoded) >= 48:
        return decoded[:32], decoded[32:48]
    return None


def decode_sqlcipher_blob(blob: bytes) -> tuple[bytes, bytes | None] | None:
    """Return (raw_key, optional salt) from a SQLCipher x'...' configuration blob."""
    text = blob.strip(b"\x00")
    parsed = _parse_plain_sqlcipher(text)
    if parsed is not None:
        return parsed
    for mask in _cipher_name_masks():
        parsed = _parse_plain_sqlcipher(xor_repeat(text, mask))
        if parsed is not None:
            return parsed
    for period in (27, 16, 32, 13):
        if len(text) < max(period, 3):
            continue
        hints = {0: ord("x"), 1: ord("'"), len(text) - 1: ord("'")}
        mask = recover_repeating_mask(text, period, hints, hex_range=(2, len(text) - 1))
        if mask is None:
            continue
        parsed = _parse_plain_sqlcipher(xor_repeat(text, mask))
        if parsed is not None:
            return parsed
    return None


def _iter_ptr_len(data: bytes, region_address: int, ptr_size: int) -> Iterable[tuple[int, int, int]]:
    fmt = "<Q" if ptr_size == 8 else "<I"
    step = ptr_size
    for offset in range(0, len(data) - 2 * ptr_size, step):
        ptr = struct.unpack_from(fmt, data, offset)[0]
        length = struct.unpack_from(fmt, data, offset + ptr_size)[0]
        if 8 <= length <= 256 and ptr > 0x10000:
            yield region_address + offset, ptr, int(length)


def collect_cipher_blobs(pid: int, *, ptr_size: int = 8) -> tuple[list[bytes], dict[str, int]]:
    """Locate Config.Cipher objects and collect nearby configuration blobs."""
    stats = {"needles": 0, "name_nodes": 0, "blobs": 0, "regions": 0}
    length_counts: dict[int, int] = {}
    needles = find_pattern_addresses(pid, CIPHER_NAME, writable_only=False, limit=16)
    stats["needles"] = len(needles)
    if not needles:
        return [], stats

    needle_set = set(needles)
    nodes: list[int] = []
    blobs: list[bytes] = []
    seen_blob: set[bytes] = set()
    ptr_fmt = "<Q" if ptr_size == 8 else "<I"

    for address, data in iter_readable_chunks(pid, writable_only=True, overlap=ptr_size * 2):
        stats["regions"] += 1
        for object_addr, ptr, length in _iter_ptr_len(data, address, ptr_size):
            if ptr in needle_set and length == len(CIPHER_NAME):
                stats["name_nodes"] += 1
                nodes.append(object_addr)

    for node in nodes:
        window = read_process_memory(pid, max(node - 64, 0x10000), 256)
        if not window:
            continue
        base = max(node - 64, 0x10000)
        for offset in range(0, len(window) - 2 * ptr_size, ptr_size):
            ptr = struct.unpack_from(ptr_fmt, window, offset)[0]
            length = struct.unpack_from(ptr_fmt, window, offset + ptr_size)[0]
            if not (32 <= length <= 256) or ptr in needle_set:
                continue
            blob = read_process_memory(pid, ptr, int(length))
            if not blob or blob in seen_blob:
                continue
            seen_blob.add(blob)
            blobs.append(blob)
            stats["blobs"] += 1
            length_counts[int(length)] = length_counts.get(int(length), 0) + 1
        for offset in range(0, len(window) - 32, 8):
            inline = window[offset : offset + 32]
            if inline in seen_blob or inline == bytes(32) or len(set(inline)) < 8:
                continue
            seen_blob.add(inline)
            blobs.append(inline)
            stats["blobs"] += 1
            length_counts[32] = length_counts.get(32, 0) + 1
    stats["blob_lengths"] = dict(sorted(length_counts.items())[:20])
    return blobs, stats


def candidates_from_blobs(blobs: Iterable[bytes]) -> list[CipherCandidate]:
    found: list[CipherCandidate] = []
    seen: set[bytes] = set()

    def add(raw_key: bytes, salt: bytes | None, method: str) -> None:
        if len(raw_key) != 32 or raw_key == bytes(32) or raw_key in seen:
            return
        seen.add(raw_key)
        found.append(CipherCandidate(bytearray(raw_key), salt, method))

    for blob in blobs:
        parsed = decode_sqlcipher_blob(blob)
        if parsed is not None:
            add(parsed[0], parsed[1], "config_cipher_xor")
        if len(blob) >= 32:
            add(blob[:32], None, "config_cipher_raw32")
            add(blob[-32:], None, "config_cipher_raw32_tail")
            for mask in _cipher_name_masks():
                xored = xor_repeat(blob[: max(len(mask), 32)], mask)[:32]
                add(xored, None, "config_cipher_xor32")
    return found


def recover_cipher_keys(
    pid: int,
    verifier: Callable[[bytes, bytes | None], bool],
    *,
    ptr_size: int = 8,
) -> tuple[list[CipherCandidate], dict[str, int]]:
    blobs, stats = collect_cipher_blobs(pid, ptr_size=ptr_size)
    stats["decoded"] = 0
    stats["verified"] = 0
    matched: list[CipherCandidate] = []
    try:
        for candidate in candidates_from_blobs(blobs):
            stats["decoded"] += 1
            if verifier(bytes(candidate.raw_key), candidate.salt):
                stats["verified"] += 1
                matched.append(candidate)
            else:
                zero_secret(candidate.raw_key)
    finally:
        for blob in blobs:
            if isinstance(blob, bytearray):
                zero_secret(blob)
    return matched, stats
