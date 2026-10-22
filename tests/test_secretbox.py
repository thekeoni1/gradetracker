"""DPAPI at-rest encryption tests (M2 fixes)."""

from __future__ import annotations

import os

import pytest

from gradetracker.auth.secretbox import (
    IS_WINDOWS,
    SecretBoxError,
    protect,
    unprotect,
)


def test_round_trip():
    data = b'[{"name": "BbRouter", "value": "token"}]'

    assert unprotect(protect(data)) == data


def test_ciphertext_hides_the_plaintext():
    if not IS_WINDOWS:
        pytest.skip("pass-through on non-Windows platforms")

    blob = protect(b"supersecret-session-token")

    assert b"supersecret-session-token" not in blob
    assert blob != b"supersecret-session-token"


def test_a_cookie_sized_payload_round_trips():
    """Far larger than Windows Credential Manager's ~2560 byte limit."""
    data = os.urandom(8000)

    assert unprotect(protect(data)) == data


def test_unprotect_rejects_garbage():
    if not IS_WINDOWS:
        pytest.skip("pass-through on non-Windows platforms")

    with pytest.raises(SecretBoxError):
        unprotect(b"this was never a dpapi blob")


def test_unprotect_rejects_a_truncated_blob():
    if not IS_WINDOWS:
        pytest.skip("pass-through on non-Windows platforms")

    blob = protect(b"some cookies")

    with pytest.raises(SecretBoxError):
        unprotect(blob[: len(blob) // 2])


def test_empty_input_is_rejected_cleanly():
    if not IS_WINDOWS:
        pytest.skip("pass-through on non-Windows platforms")

    with pytest.raises(SecretBoxError, match="empty"):
        protect(b"")
