"""At-rest encryption for the cookie snapshot, via Windows DPAPI.

CryptProtectData ties the ciphertext to the current Windows user account, so a
copy of the file is useless on another machine or under another login. It needs
no key management and no extra dependency: ctypes reaches crypt32 directly.

The project folder may sit in OneDrive, which would otherwise sync a plaintext
session-token file to Microsoft's cloud. The data directory now lives outside
the synced tree (see config.DATA_DIR) and the snapshot is encrypted as well.

On non-Windows platforms protect/unprotect pass the bytes through unchanged, so
the calling code stays the same. That is a real reduction in protection: the
snapshot is plaintext there, and launcher.save_cookie_snapshot falls back to
mode 0600, which keeps out other accounts on the machine but not the owner's own
processes or a backup that copies it elsewhere. Closing the gap properly means a
second backend -- the macOS Keychain holds a key the same way the Credential
Manager holds the Gradescope password -- and that waits until someone runs this
on a Mac. Windows is the supported platform today.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

# Never prompt: this runs in a CLI and in a background scheduler.
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class SecretBoxError(Exception):
    """Encryption or decryption failed, e.g. a corrupt or foreign ciphertext."""


if IS_WINDOWS:

    class _Blob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]


def _call(func, data: bytes, what: str) -> bytes:
    """Run one DPAPI call, copy the result out, and free the native buffer."""
    if not data:
        raise SecretBoxError(f"Cannot {what} empty data")

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()

    ok = func(
        ctypes.byref(blob_in),
        None,  # description
        None,  # optional entropy
        None,  # reserved
        None,  # prompt struct
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise SecretBoxError(
            f"DPAPI could not {what} the data (error {ctypes.get_last_error()})"
        )

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(blob_out.pbData)


def protect(data: bytes) -> bytes:
    """Encrypt bytes for the current Windows user. Pass-through elsewhere."""
    if not IS_WINDOWS:
        return data
    return _call(_crypt32.CryptProtectData, data, "encrypt")


def unprotect(data: bytes) -> bytes:
    """Decrypt bytes written by protect(). Raises SecretBoxError on garbage."""
    if not IS_WINDOWS:
        return data
    return _call(_crypt32.CryptUnprotectData, data, "decrypt")
