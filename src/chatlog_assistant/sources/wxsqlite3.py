from __future__ import annotations

import hashlib
import struct

from .crypto_native import aes_cbc_decrypt, aes_cbc_encrypt


PAGE_SIZE = 4096
SQLITE_HEADER = b"SQLite format 3\x00"
KEY_MATERIAL_TAG = b"sAlT"


def is_plain_sqlite(page: bytes) -> bool:
    return page.startswith(SQLITE_HEADER)


def has_wxsqlite3_header_shape(page: bytes) -> bool:
    if len(page) < 24 or is_plain_sqlite(page):
        return False
    fragment = page[16:24]
    page_size = int.from_bytes(fragment[:2], "big")
    if page_size == 1:
        page_size = 65536
    return (
        512 <= page_size <= 65536
        and page_size & (page_size - 1) == 0
        and fragment[5:8] == b"\x40\x20\x20"
    )


def _lcg_step(value: int) -> int:
    quotient = value // 52774
    value = 40692 * (value - 52774 * quotient) - 3791 * quotient
    return value if value >= 0 else value + 2147483399


def page_iv(page_number: int) -> bytes:
    if page_number < 1:
        raise ValueError("page_number must be >= 1")
    value = page_number + 1
    output = bytearray()
    for _ in range(4):
        value = _lcg_step(value)
        output.extend(struct.pack("<I", value & 0xFFFFFFFF))
    return hashlib.md5(output).digest()


def page_key(raw_key: bytes, page_number: int) -> bytes:
    if len(raw_key) != 16:
        raise ValueError("wxSQLite3 AES-128 raw key must be 16 bytes")
    material = raw_key + struct.pack("<I", page_number) + KEY_MATERIAL_TAG
    return hashlib.md5(material).digest()


def decrypt_page(raw_key: bytes, encrypted_page: bytes, page_number: int, *, derive_page_key: bool = True) -> bytes:
    if len(encrypted_page) != PAGE_SIZE:
        raise ValueError(f"page must be {PAGE_SIZE} bytes")
    aes_key = page_key(raw_key, page_number) if derive_page_key else raw_key
    iv = page_iv(page_number)
    if page_number == 1 and has_wxsqlite3_header_shape(encrypted_page):
        header_fragment = encrypted_page[16:24]
        ciphertext = encrypted_page[8:16] + encrypted_page[24:]
        plaintext_tail = aes_cbc_decrypt(aes_key, iv, ciphertext)
        if plaintext_tail[:8] != header_fragment:
            raise ValueError("wxSQLite3 AES-128 key validation failed")
        return SQLITE_HEADER + plaintext_tail
    return aes_cbc_decrypt(aes_key, iv, encrypted_page)


def encrypt_page(raw_key: bytes, plain_page: bytes, page_number: int, *, derive_page_key: bool = True) -> bytes:
    if len(plain_page) != PAGE_SIZE:
        raise ValueError(f"page must be {PAGE_SIZE} bytes")
    aes_key = page_key(raw_key, page_number) if derive_page_key else raw_key
    iv = page_iv(page_number)
    if page_number != 1:
        return aes_cbc_encrypt(aes_key, iv, plain_page)
    if not is_plain_sqlite(plain_page):
        raise ValueError("page 1 must contain a SQLite header")
    ciphertext = aes_cbc_encrypt(aes_key, iv, plain_page[16:])
    return bytes(8) + ciphertext[:8] + plain_page[16:24] + ciphertext[8:]


def verify_key(raw_key: bytes, page_one: bytes) -> bool:
    if len(raw_key) != 16 or len(page_one) < PAGE_SIZE:
        return False
    for derive in (True, False):
        try:
            plain = decrypt_page(raw_key, page_one[:PAGE_SIZE], 1, derive_page_key=derive)
        except (OSError, ValueError):
            continue
        if is_plain_sqlite(plain) and len(plain) >= 108 and plain[100] in (0x02, 0x05, 0x0A, 0x0D):
            return True
    return False


def _wal_frames(wal_bytes: bytes, page_size: int) -> list[tuple[int, int, bytes]]:
    if len(wal_bytes) < 32:
        return []
    magic = int.from_bytes(wal_bytes[:4], "big")
    if magic not in (0x377F0682, 0x377F0683):
        return []
    declared_size = int.from_bytes(wal_bytes[8:12], "big") or 65536
    if declared_size != page_size:
        return []
    salt = wal_bytes[16:24]
    frame_size = 24 + page_size
    frames: list[tuple[int, int, bytes]] = []
    offset = 32
    while offset + frame_size <= len(wal_bytes):
        header = wal_bytes[offset : offset + 24]
        page_number = int.from_bytes(header[:4], "big")
        commit_pages = int.from_bytes(header[4:8], "big")
        if page_number < 1:
            break
        if header[8:16] != salt:
            offset += frame_size
            continue
        frames.append((page_number, commit_pages, wal_bytes[offset + 24 : offset + frame_size]))
        offset += frame_size
    last_commit = -1
    for index, (_, commit_pages, _) in enumerate(frames):
        if commit_pages:
            last_commit = index
    return frames[: last_commit + 1] if last_commit >= 0 else []


def decrypt_database_bytes(source_bytes: bytes, raw_key: bytes, *, wal_bytes: bytes | None = None) -> bytes:
    if len(source_bytes) < PAGE_SIZE or len(source_bytes) % PAGE_SIZE:
        raise ValueError(f"database size is not a whole number of {PAGE_SIZE}-byte pages")
    if is_plain_sqlite(source_bytes[:PAGE_SIZE]):
        result = bytearray(source_bytes)
        source_encrypted = False
        derive = True
    elif has_wxsqlite3_header_shape(source_bytes[:PAGE_SIZE]):
        derive = True
        try:
            decrypt_page(raw_key, source_bytes[:PAGE_SIZE], 1, derive_page_key=True)
        except (OSError, ValueError):
            derive = False
        result = bytearray()
        for page_index in range(len(source_bytes) // PAGE_SIZE):
            page = source_bytes[page_index * PAGE_SIZE : (page_index + 1) * PAGE_SIZE]
            result.extend(decrypt_page(raw_key, page, page_index + 1, derive_page_key=derive))
        source_encrypted = True
    else:
        raise ValueError("unsupported database format")

    final_size = None
    for page_number, commit_pages, page in _wal_frames(wal_bytes or b"", PAGE_SIZE):
        plain_page = page if not source_encrypted else decrypt_page(raw_key, page, page_number, derive_page_key=derive)
        offset = (page_number - 1) * PAGE_SIZE
        if len(result) < offset + PAGE_SIZE:
            result.extend(bytes(offset + PAGE_SIZE - len(result)))
        result[offset : offset + PAGE_SIZE] = plain_page
        if commit_pages:
            final_size = commit_pages * PAGE_SIZE
    if final_size is not None:
        del result[final_size:]
    return bytes(result)
