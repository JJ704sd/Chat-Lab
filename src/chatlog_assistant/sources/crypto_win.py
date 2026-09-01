from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


NTSTATUS = ctypes.c_long
BCRYPT_HANDLE = wintypes.HANDLE


def _check(status: int) -> None:
    if status != 0:
        raise OSError(f"Windows CNG status 0x{status & 0xFFFFFFFF:08X}")


def _bcrypt():
    bcrypt = ctypes.WinDLL("bcrypt")
    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(BCRYPT_HANDLE),
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.ULONG,
    ]
    bcrypt.BCryptOpenAlgorithmProvider.restype = NTSTATUS
    bcrypt.BCryptSetProperty.argtypes = [
        BCRYPT_HANDLE,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    bcrypt.BCryptSetProperty.restype = NTSTATUS
    bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        BCRYPT_HANDLE,
        ctypes.POINTER(BCRYPT_HANDLE),
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    bcrypt.BCryptGenerateSymmetricKey.restype = NTSTATUS
    bcrypt.BCryptEncrypt.argtypes = [
        BCRYPT_HANDLE,
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
        wintypes.ULONG,
    ]
    bcrypt.BCryptEncrypt.restype = NTSTATUS
    bcrypt.BCryptDecrypt.argtypes = bcrypt.BCryptEncrypt.argtypes
    bcrypt.BCryptDecrypt.restype = NTSTATUS
    bcrypt.BCryptDestroyKey.argtypes = [BCRYPT_HANDLE]
    bcrypt.BCryptDestroyKey.restype = NTSTATUS
    bcrypt.BCryptCloseAlgorithmProvider.argtypes = [BCRYPT_HANDLE, wintypes.ULONG]
    bcrypt.BCryptCloseAlgorithmProvider.restype = NTSTATUS
    return bcrypt


def aes_cbc(key: bytes, iv: bytes, data: bytes, *, encrypt: bool) -> bytes:
    if os.name != "nt":
        raise OSError("Windows CNG AES is only available on Windows")
    if len(iv) != 16 or not data or len(data) % 16:
        raise ValueError("AES-CBC requires a 16-byte IV and block-aligned data")
    bcrypt = _bcrypt()
    alg = BCRYPT_HANDLE()
    _check(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "AES", None, 0))
    key_handle = BCRYPT_HANDLE()
    try:
        mode = ctypes.create_unicode_buffer("ChainingModeCBC")
        _check(
            bcrypt.BCryptSetProperty(
                alg,
                "ChainingMode",
                ctypes.cast(mode, ctypes.c_void_p),
                ctypes.sizeof(mode),
                0,
            )
        )
        key_buf = ctypes.create_string_buffer(key, len(key))
        _check(
            bcrypt.BCryptGenerateSymmetricKey(
                alg,
                ctypes.byref(key_handle),
                None,
                0,
                key_buf,
                len(key),
                0,
            )
        )
        iv_buf = ctypes.create_string_buffer(iv, len(iv))
        in_buf = ctypes.create_string_buffer(data, len(data))
        out_buf = ctypes.create_string_buffer(len(data))
        written = wintypes.ULONG(0)
        fn = bcrypt.BCryptEncrypt if encrypt else bcrypt.BCryptDecrypt
        _check(
            fn(
                key_handle,
                in_buf,
                len(data),
                None,
                iv_buf,
                len(iv),
                out_buf,
                len(data),
                ctypes.byref(written),
                0,
            )
        )
        return out_buf.raw[: written.value]
    finally:
        if key_handle:
            bcrypt.BCryptDestroyKey(key_handle)
        bcrypt.BCryptCloseAlgorithmProvider(alg, 0)


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    return aes_cbc(key, iv, data, encrypt=False)


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    return aes_cbc(key, iv, data, encrypt=True)
