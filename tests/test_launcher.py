"""Launcher tests (M1).

The poll loop is tested with fakes, so none of this needs a browser.
"""

from __future__ import annotations

import stat
import sys

import pytest
from typer.testing import CliRunner

from gradetracker import config
from gradetracker.auth import launcher, secretbox
from gradetracker.auth.cookie_auth import CookieAuth
from gradetracker.cli import app

runner = CliRunner()


class FakeClock:
    """A monotonic clock that only moves when the fake sleep is called."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _responder(responses):
    """A check_fn walking a list of (status, body), repeating the last entry."""
    calls = {"n": 0}

    def check_fn():
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        value = responses[index]
        if isinstance(value, Exception):
            raise value
        return value

    check_fn.calls = calls
    return check_fn


# --- response classification ---------------------------------------------


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (200, '{"id": "_123_1", "userName": "sstudent"}', True),
        (200, '{"userName": "sstudent"}', True),
        (200, '{"id": "_123_1"}', True),
        (401, "", False),
        (403, '{"status": 403}', False),
        (200, "<html>Sign in to Blackboard</html>", False),
        (200, '{"results": []}', False),
        (200, "[1, 2, 3]", False),
        (302, '{"id": "_123_1"}', False),
    ],
)
def test_is_authenticated(status, body, expected):
    assert launcher._is_authenticated(status, body) is expected


# --- poll loop ------------------------------------------------------------


def test_poll_succeeds_on_third_call():
    """SSO answers 401 for a while, then the user object appears."""
    clock = FakeClock()
    check_fn = _responder([(401, ""), (401, ""), (200, '{"id": "_1_1"}')])

    result = launcher._poll_until_authenticated(
        check_fn, timeout_s=300, poll_interval_s=2.0, clock=clock.time, sleep=clock.sleep
    )

    assert result is True
    assert check_fn.calls["n"] == 3
    assert clock.sleeps == [2.0, 2.0]


def test_poll_times_out_and_stops():
    """A user who walks away ends the attempt cleanly, not in an endless loop."""
    clock = FakeClock()
    check_fn = _responder([(401, "")])

    result = launcher._poll_until_authenticated(
        check_fn, timeout_s=10, poll_interval_s=2.0, clock=clock.time, sleep=clock.sleep
    )

    assert result is False
    assert clock.now == pytest.approx(10.0)
    assert check_fn.calls["n"] == 6


def test_poll_treats_login_page_as_not_yet():
    """A 200 of HTML is the SSO page, not a finished login."""
    clock = FakeClock()
    check_fn = _responder(
        [(200, "<html>login</html>"), (200, '{"userName": "sstudent"}')]
    )

    result = launcher._poll_until_authenticated(
        check_fn, timeout_s=300, poll_interval_s=1.0, clock=clock.time, sleep=clock.sleep
    )

    assert result is True
    assert check_fn.calls["n"] == 2


def test_poll_aborts_when_window_closes():
    """Closing the window is an abort, not a crash."""
    clock = FakeClock()
    check_fn = _responder([(401, ""), launcher.PollingAborted("Target page closed")])

    result = launcher._poll_until_authenticated(
        check_fn, timeout_s=300, poll_interval_s=2.0, clock=clock.time, sleep=clock.sleep
    )

    assert result is False


def test_poll_checks_once_even_with_zero_timeout():
    """An already-valid session is reported without waiting."""
    clock = FakeClock()
    check_fn = _responder([(200, '{"id": "_1_1"}')])

    result = launcher._poll_until_authenticated(
        check_fn, timeout_s=0, poll_interval_s=2.0, clock=clock.time, sleep=clock.sleep
    )

    assert result is True
    assert clock.sleeps == []


def test_poll_propagates_unexpected_errors():
    """A real bug in the check must not look like a timeout."""
    clock = FakeClock()
    check_fn = _responder([RuntimeError("boom")])

    with pytest.raises(RuntimeError, match="boom"):
        launcher._poll_until_authenticated(
            check_fn, timeout_s=300, poll_interval_s=2.0, clock=clock.time, sleep=clock.sleep
        )


# --- login timestamp ------------------------------------------------------


def test_login_timestamp_round_trip(tmp_path):
    profile = tmp_path / "profiles" / "blackboard"
    profile.mkdir(parents=True)

    assert launcher.read_login_timestamp(profile) is None

    launcher.write_login_timestamp(profile)

    assert launcher.read_login_timestamp(profile) is not None
    assert (tmp_path / "profiles" / "blackboard.login.json").exists()


def test_read_login_timestamp_survives_corrupt_file(tmp_path):
    profile = tmp_path / "profiles" / "blackboard"
    profile.mkdir(parents=True)
    (tmp_path / "profiles" / "blackboard.login.json").write_text("not json", encoding="utf-8")

    assert launcher.read_login_timestamp(profile) is None


# --- CLI ------------------------------------------------------------------


def test_login_errors_when_base_url_unset(monkeypatch):
    monkeypatch.setattr(config, "BB_BASE_URL", "")

    result = runner.invoke(app, ["login", "blackboard"])

    assert result.exit_code == 1
    assert "BB_BASE_URL is not set" in result.output


def test_login_rejects_unknown_source(monkeypatch):
    monkeypatch.setattr(config, "BB_BASE_URL", "https://bb.example.edu")

    result = runner.invoke(app, ["login", "canvas"])

    assert result.exit_code == 1
    assert "Unknown source" in result.output


def test_status_reports_unset_base_url(monkeypatch):
    """gt status must not open a browser when there is nothing to check."""
    monkeypatch.setattr(config, "BB_BASE_URL", "")

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "blackboard: BB_BASE_URL not set" in result.output
    assert "gradescope: needs login (run gt login gradescope)" in result.output


def test_status_reports_needs_login(monkeypatch):
    monkeypatch.setattr(config, "BB_BASE_URL", "https://bb.example.edu")
    monkeypatch.setattr(CookieAuth, "check", lambda self: "expired")

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "blackboard: needs login (run gt login blackboard)" in result.output


def test_status_reports_valid_session_with_login_time(monkeypatch):
    monkeypatch.setattr(config, "BB_BASE_URL", "https://bb.example.edu")
    monkeypatch.setattr(CookieAuth, "check", lambda self: "valid")
    monkeypatch.setattr(
        launcher, "read_login_timestamp", lambda profile_dir: "2026-10-20T18:00:00+00:00"
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "blackboard: valid session (logged in 2026-10-20T18:00:00+00:00)" in result.output


def test_status_distinguishes_offline_from_logged_out(monkeypatch):
    """Being offline must not send the user through Duo for nothing."""
    monkeypatch.setattr(config, "BB_BASE_URL", "https://bb.example.edu")
    monkeypatch.setattr(CookieAuth, "check", lambda self: "offline")

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "blackboard: could not reach bb.example.edu (offline?)" in result.output
    assert "blackboard: needs login" not in result.output


def test_login_reports_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BB_BASE_URL", "https://bb.example.edu")
    monkeypatch.setattr(config, "BB_PROFILE_DIR", tmp_path / "blackboard")
    monkeypatch.setattr(launcher, "login_blackboard", lambda **kwargs: False)

    result = runner.invoke(app, ["login", "blackboard"])

    assert result.exit_code == 1
    assert "Timed out after 5 minutes waiting for login." in result.output


def test_login_saves_and_stamps_on_success(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BB_BASE_URL", "https://bb.example.edu")
    monkeypatch.setattr(config, "BB_PROFILE_DIR", tmp_path / "blackboard")
    monkeypatch.setattr(launcher, "login_blackboard", lambda **kwargs: True)

    result = runner.invoke(app, ["login", "blackboard"])

    assert result.exit_code == 0
    assert "Blackboard login saved." in result.output
    assert launcher.read_login_timestamp(tmp_path / "blackboard") is not None



# --- cookie snapshot ------------------------------------------------------


def test_cookie_snapshot_round_trip(tmp_path):
    """Captured at login because Chromium drops session cookies on close."""
    profile = tmp_path / "profiles" / "blackboard"
    cookies = [{"name": "BbRouter", "value": "abc", "domain": "bb.example.edu", "path": "/"}]

    path = launcher.save_cookie_snapshot(profile, cookies)

    assert path == tmp_path / "profiles" / "blackboard.cookies.bin"
    assert launcher.read_cookie_snapshot(profile) == cookies


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_cookie_snapshot_is_not_plaintext_on_disk(tmp_path):
    """These are live session tokens, so DPAPI encrypts them at rest."""
    profile = tmp_path / "profiles" / "blackboard"
    launcher.save_cookie_snapshot(
        profile, [{"name": "BbRouter", "value": "supersecret", "domain": "x", "path": "/"}]
    )

    blob = (tmp_path / "profiles" / "blackboard.cookies.bin").read_bytes()

    assert b"supersecret" not in blob
    assert b"BbRouter" not in blob


@pytest.mark.skipif(sys.platform == "win32", reason="file modes are POSIX-only")
def test_cookie_snapshot_is_owner_only_without_dpapi(tmp_path):
    """Off Windows the snapshot is plaintext, so the mode is all it has.

    Twice over, because a rewrite goes through a fresh temp file and the mode
    has to be set on that one too, not just on the first write.
    """
    profile = tmp_path / "profiles" / "blackboard"
    path = launcher.save_cookie_snapshot(profile, [{"name": "a", "value": "b"}])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    path = launcher.save_cookie_snapshot(profile, [{"name": "a", "value": "c"}])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_cookie_snapshot_write_leaves_no_temp_files(tmp_path):
    """The write is atomic via a temp file plus os.replace."""
    profile = tmp_path / "profiles" / "blackboard"
    launcher.save_cookie_snapshot(profile, [{"name": "a", "value": "b"}])
    launcher.save_cookie_snapshot(profile, [{"name": "a", "value": "c"}])

    names = sorted(f.name for f in (tmp_path / "profiles").iterdir())

    assert names == ["blackboard.cookies.bin"]


def test_cookie_snapshot_is_empty_when_absent(tmp_path):
    assert launcher.read_cookie_snapshot(tmp_path / "blackboard") == []


def test_cookie_snapshot_survives_a_corrupt_file(tmp_path):
    """Truncated, foreign-user or garbage ciphertext all mean "no snapshot"."""
    profile = tmp_path / "profiles" / "blackboard"
    profile.mkdir(parents=True)
    (tmp_path / "profiles" / "blackboard.cookies.bin").write_bytes(b"not a dpapi blob")

    assert launcher.read_cookie_snapshot(profile) == []


def test_cookie_snapshot_survives_a_truncated_file(tmp_path):
    profile = tmp_path / "profiles" / "blackboard"
    path = launcher.save_cookie_snapshot(profile, [{"name": "a", "value": "b"}])
    blob = path.read_bytes()
    path.write_bytes(blob[: len(blob) // 2])

    assert launcher.read_cookie_snapshot(profile) == []


def test_cookie_snapshot_rejects_a_non_list_payload(tmp_path):
    profile = tmp_path / "profiles" / "blackboard"
    profile.mkdir(parents=True)
    (tmp_path / "profiles" / "blackboard.cookies.bin").write_bytes(
        secretbox.protect(b'{"a": 1}')
    )

    assert launcher.read_cookie_snapshot(profile) == []


def test_snapshot_and_login_stamp_do_not_collide(tmp_path):
    profile = tmp_path / "profiles" / "blackboard"
    launcher.save_cookie_snapshot(profile, [{"name": "a", "value": "b"}])
    launcher.write_login_timestamp(profile)

    assert launcher.read_cookie_snapshot(profile) == [{"name": "a", "value": "b"}]
    assert launcher.read_login_timestamp(profile) is not None
