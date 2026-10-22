"""GradescopeAuth and gt login gradescope tests (M3).

conftest swaps the OS keychain for a dict and blocks a real login, so nothing
here touches the developer's credentials or the network.
"""

from __future__ import annotations

import keyring
import pytest
import requests
from typer.testing import CliRunner

from gradetracker import config
from gradetracker.auth import gradescope_auth
from gradetracker.auth.base import AuthExpired
from gradetracker.auth.gradescope_auth import (
    EMAIL_KEY,
    PASSWORD_KEY,
    SERVICE,
    GradescopeAuth,
    clear_credentials,
    load_credentials,
    store_credentials,
)
from gradetracker.cli import app

runner = CliRunner()

EMAIL = "sstudent@example.edu"
PASSWORD = "correct horse battery staple"


class FakeConnection:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.logged_in = True
        self.account = object()


def fake_login(monkeypatch, result=None, raises=None):
    """Replace the module's _login, recording the credentials it was given."""
    calls = []

    def _login(email, password):
        calls.append((email, password))
        if raises is not None:
            raise raises
        return result if result is not None else FakeConnection()

    monkeypatch.setattr(gradescope_auth, "_login", _login)
    return calls


# --- keyring round trip ---------------------------------------------------


def test_store_and_load_round_trip():
    store_credentials(EMAIL, PASSWORD)

    assert load_credentials() == (EMAIL, PASSWORD)


def test_credentials_are_stored_under_separate_keys(fake_keyring):
    """Neither the email nor the password lands in a file."""
    store_credentials(EMAIL, PASSWORD)

    assert fake_keyring[(SERVICE, EMAIL_KEY)] == EMAIL
    assert fake_keyring[(SERVICE, PASSWORD_KEY)] == PASSWORD


def test_load_is_none_when_nothing_is_stored():
    assert load_credentials() is None


def test_load_is_none_when_only_the_email_is_stored():
    keyring.set_password(SERVICE, EMAIL_KEY, EMAIL)

    assert load_credentials() is None


def test_load_is_none_when_only_the_password_is_stored():
    keyring.set_password(SERVICE, PASSWORD_KEY, PASSWORD)

    assert load_credentials() is None


def test_clear_removes_both_halves():
    store_credentials(EMAIL, PASSWORD)

    clear_credentials()

    assert load_credentials() is None


def test_clear_is_safe_when_nothing_is_stored():
    clear_credentials()  # must not raise

    assert load_credentials() is None


def test_keyring_failure_on_store_becomes_auth_expired(monkeypatch):
    def explode(service, username, password):
        raise keyring.errors.PasswordSetError("no backend")

    monkeypatch.setattr(keyring, "set_password", explode)

    with pytest.raises(AuthExpired, match="keychain"):
        store_credentials(EMAIL, PASSWORD)


def test_keyring_failure_on_load_reads_as_no_credentials(monkeypatch):
    def explode(service, username):
        raise keyring.errors.NoKeyringError("no backend")

    monkeypatch.setattr(keyring, "get_password", explode)

    assert load_credentials() is None


# --- session --------------------------------------------------------------


def test_session_logs_in_with_the_stored_credentials(monkeypatch):
    store_credentials(EMAIL, PASSWORD)
    calls = fake_login(monkeypatch)

    auth = GradescopeAuth()
    session = auth.session()

    assert calls == [(EMAIL, PASSWORD)]
    assert isinstance(session, requests.Session)
    assert auth.email == EMAIL
    assert auth.connection is not None


def test_session_logs_in_once_per_instance(monkeypatch):
    store_credentials(EMAIL, PASSWORD)
    calls = fake_login(monkeypatch)
    auth = GradescopeAuth()

    first = auth.session()
    second = auth.session()

    assert first is second
    assert len(calls) == 1


def test_session_without_credentials_raises_auth_expired():
    with pytest.raises(AuthExpired, match="gt login gradescope"):
        GradescopeAuth().session()


def test_session_with_a_rejected_password_raises_auth_expired(monkeypatch):
    store_credentials(EMAIL, "wrong")
    fake_login(monkeypatch, raises=AuthExpired("Gradescope rejected the saved credentials"))

    with pytest.raises(AuthExpired):
        GradescopeAuth().session()


def test_session_offline_raises_auth_expired_naming_the_reach_failure(monkeypatch):
    store_credentials(EMAIL, PASSWORD)
    fake_login(monkeypatch, raises=requests.ConnectionError("dns failure"))

    with pytest.raises(AuthExpired, match="Could not reach Gradescope"):
        GradescopeAuth().session()


# --- check ----------------------------------------------------------------


def test_check_is_expired_without_credentials():
    auth = GradescopeAuth()

    assert auth.check() == "expired"
    assert auth.is_valid() is False


def test_check_is_valid_with_a_working_password(monkeypatch):
    store_credentials(EMAIL, PASSWORD)
    fake_login(monkeypatch)
    auth = GradescopeAuth()

    assert auth.check() == "valid"
    assert auth.is_valid() is True
    assert auth.email == EMAIL


def test_check_is_expired_with_a_rejected_password(monkeypatch):
    store_credentials(EMAIL, "wrong")
    fake_login(monkeypatch, raises=AuthExpired("rejected"))
    auth = GradescopeAuth()

    assert auth.check() == "expired"


def test_check_is_offline_on_a_network_error(monkeypatch):
    """A dead network must not read as a wrong password."""
    store_credentials(EMAIL, PASSWORD)
    fake_login(monkeypatch, raises=requests.ConnectionError("dns failure"))
    auth = GradescopeAuth()

    assert auth.check() == "offline"
    assert auth.is_valid() is False


def test_check_is_offline_on_a_timeout(monkeypatch):
    store_credentials(EMAIL, PASSWORD)
    fake_login(monkeypatch, raises=requests.Timeout("too slow"))

    assert GradescopeAuth().check() == "offline"


def test_check_reuses_a_successful_login(monkeypatch):
    store_credentials(EMAIL, PASSWORD)
    calls = fake_login(monkeypatch)
    auth = GradescopeAuth()

    auth.check()
    auth.session()
    auth.check()

    assert len(calls) == 1


# --- gt login gradescope --------------------------------------------------


def test_login_stores_credentials_after_verifying_them(monkeypatch):
    calls = fake_login(monkeypatch)

    result = runner.invoke(app, ["login", "gradescope"], input=f"{EMAIL}\n{PASSWORD}\n")

    assert result.exit_code == 0
    assert "Gradescope login saved." in result.output
    assert calls == [(EMAIL, PASSWORD)]
    assert load_credentials() == (EMAIL, PASSWORD)


def test_login_stores_nothing_when_the_password_is_rejected(monkeypatch):
    """A typo must never end up in the keychain."""
    fake_login(monkeypatch, raises=AuthExpired("Gradescope rejected the saved credentials"))

    result = runner.invoke(app, ["login", "gradescope"], input=f"{EMAIL}\nwrong\n")

    assert result.exit_code == 1
    assert "Gradescope login failed" in result.output
    assert "Nothing was saved" in result.output
    assert load_credentials() is None


def test_login_does_not_overwrite_good_credentials_on_a_failed_retry(monkeypatch):
    store_credentials(EMAIL, PASSWORD)
    fake_login(monkeypatch, raises=AuthExpired("rejected"))

    result = runner.invoke(app, ["login", "gradescope"], input=f"{EMAIL}\ntypo\n")

    assert result.exit_code == 1
    assert load_credentials() == (EMAIL, PASSWORD)


def test_login_reports_an_unreachable_gradescope(monkeypatch):
    fake_login(monkeypatch, raises=requests.ConnectionError("dns failure"))

    result = runner.invoke(app, ["login", "gradescope"], input=f"{EMAIL}\n{PASSWORD}\n")

    assert result.exit_code == 1
    assert "Could not reach Gradescope" in result.output
    assert load_credentials() is None


def test_login_offers_gs_email_as_the_default(monkeypatch):
    monkeypatch.setattr(config, "GS_EMAIL", EMAIL)
    calls = fake_login(monkeypatch)

    # An empty first line accepts the prompt's default.
    result = runner.invoke(app, ["login", "gradescope"], input=f"\n{PASSWORD}\n")

    assert result.exit_code == 0
    assert calls == [(EMAIL, PASSWORD)]


def test_login_password_prompt_is_hidden(monkeypatch):
    fake_login(monkeypatch)

    result = runner.invoke(app, ["login", "gradescope"], input=f"{EMAIL}\n{PASSWORD}\n")

    assert PASSWORD not in result.output


# --- gt status ------------------------------------------------------------


def test_status_reports_the_logged_in_email(monkeypatch):
    monkeypatch.setattr(config, "BB_BASE_URL", "")
    store_credentials(EMAIL, PASSWORD)
    fake_login(monkeypatch)

    result = runner.invoke(app, ["status"])

    assert f"gradescope: valid (as {EMAIL})" in result.output


def test_status_reports_needs_login_without_credentials(monkeypatch):
    monkeypatch.setattr(config, "BB_BASE_URL", "")

    result = runner.invoke(app, ["status"])

    assert "gradescope: needs login (run gt login gradescope)" in result.output


def test_status_reports_offline(monkeypatch):
    monkeypatch.setattr(config, "BB_BASE_URL", "")
    store_credentials(EMAIL, PASSWORD)
    fake_login(monkeypatch, raises=requests.ConnectionError("dns failure"))

    result = runner.invoke(app, ["status"])

    assert "gradescope: could not reach gradescope.com (offline?)" in result.output
