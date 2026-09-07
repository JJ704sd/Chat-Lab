"""AES-CBC using the host's native cryptographic library, without padding."""
from __future__ import annotations

import ctypes
from functools import lru_cache
import os
import sys


@lru_cache(maxsize=1)
def _common_crypto():
    library = ctypes.CDLL('/usr/lib/system/libcommonCrypto.dylib')
    library.CCCrypt.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    library.CCCrypt.restype = ctypes.c_int32
    return library


def aes_cbc(key: bytes, iv: bytes, data: bytes, *, encrypt: bool) -> bytes:
    if len(key) not in (16, 24, 32):
        raise ValueError('AES requires a 16, 24 or 32-byte key')
    if len(iv) != 16 or not data or len(data) % 16:
        raise ValueError('AES-CBC requires a 16-byte IV and block-aligned data')
    if os.name == 'nt':
        from .crypto_win import aes_cbc as windows_aes_cbc
        return windows_aes_cbc(key, iv, data, encrypt=encrypt)
    if sys.platform != 'darwin':
        raise OSError('Native AES backend is available on Windows and macOS only')
    library = _common_crypto()
    key_buffer = ctypes.create_string_buffer(key, len(key))
    iv_buffer = ctypes.create_string_buffer(iv, len(iv))
    input_buffer = ctypes.create_string_buffer(data, len(data))
    output_buffer = ctypes.create_string_buffer(len(data))
    moved = ctypes.c_size_t()
    try:
        # kCCEncrypt=0, kCCDecrypt=1, kCCAlgorithmAES=0, options=0 (CBC, no padding).
        status = library.CCCrypt(
            0 if encrypt else 1, 0, 0, key_buffer, len(key), iv_buffer,
            input_buffer, len(data), output_buffer, len(data), ctypes.byref(moved),
        )
        if status != 0:
            raise OSError(f'CommonCrypto AES failed with status {status}')
        if moved.value != len(data):
            raise OSError('CommonCrypto returned an unexpected block length')
        return output_buffer.raw[:moved.value]
    finally:
        for buffer in (key_buffer, iv_buffer, input_buffer, output_buffer):
            ctypes.memset(ctypes.addressof(buffer), 0, ctypes.sizeof(buffer))


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    return aes_cbc(key, iv, data, encrypt=False)


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    return aes_cbc(key, iv, data, encrypt=True)
