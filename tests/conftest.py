"""Shared test safety net.

Three things must never happen in this suite: a real Gradescope login, a write
to the developer's actual OS keychain, and a write to the real data directory.
All three are blocked for every test by default; a test that wants one must say
so explicitly.
"""

from __future__ import annotations

import keyring
import keyring.errors
import pytest

from gradetracker import config


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    """Swap the OS keychain for a dict, so tests cannot touch real credentials."""
    store: dict[tuple[str, str], str] = {}

    def set_password(service, username, password):
        store[(service, username)] = password

    def get_password(service, username):
        return store.get((service, username))

    def delete_password(service, username):
        if (service, username) not in store:
            raise keyring.errors.PasswordDeleteError(username)
        del store[(service, username)]

    monkeypatch.setattr(keyring, "set_password", set_password)
    monkeypatch.setattr(keyring, "get_password", get_password)
    monkeypatch.setattr(keyring, "delete_password", delete_password)
    return store


@pytest.fixture(autouse=True)
def no_gradescope_login(monkeypatch):
    """Fail loudly if a test would reach gradescope.com.

    Tests that need a login replace this with their own fake, which wins because
    it is applied after this fixture.
    """

    def refuse(email, password):
        raise AssertionError(
            "a test tried to log in to Gradescope for real; monkeypatch "
            "gradetracker.auth.gradescope_auth._login instead"
        )

    monkeypatch.setattr("gradetracker.auth.gradescope_auth._login", refuse)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point every configured path at tmp_path.

    Without this, any test that drives the CLI reaches the real
    %LOCALAPPDATA%\GradeTracker: gt login writes session_spans, a full sync
    writes grades.db. That already happened once, so the guard is per-test and
    autouse rather than something each test has to remember.
    """
    root = tmp_path / "gtdata"
    monkeypatch.setattr(config, "DATA_DIR", root)
    monkeypatch.setattr(config, "RAW_DIR", root / "raw")
    monkeypatch.setattr(config, "PROFILES_DIR", root / "profiles")
    monkeypatch.setattr(config, "SYLLABI_DIR", root / "syllabi")
    monkeypatch.setattr(config, "BB_PROFILE_DIR", root / "profiles" / "blackboard")
    monkeypatch.setattr(config, "DB_PATH", root / "grades.db")
    return root
