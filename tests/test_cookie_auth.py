"""CookieAuth tests (M2). No browser: the cookie plumbing and probe are faked."""

from __future__ import annotations

import pytest
import requests

from gradetracker.auth import launcher
from gradetracker.auth.base import AuthExpired
from gradetracker.auth.cookie_auth import (
    CookieAuth,
    _cookies_to_session,
    _session_to_cookie_dicts,
)

BASE_URL = "https://bb.example.edu"
CHECK_PATH = "/learn/api/public/v1/users/me"


def playwright_cookie(name="BbRouter", value="expires:1761000000", **overrides) -> dict:
    """A cookie shaped the way context.cookies() returns them."""
    cookie = {
        "name": name,
        "value": value,
        "domain": "bb.example.edu",
        "path": "/",
        "expires": 1761000000.0,
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
    }
    cookie.update(overrides)
    return cookie


@pytest.fixture
def profile(tmp_path):
    """An existing profile directory with no snapshot yet."""
    path = tmp_path / "profiles" / "blackboard"
    path.mkdir(parents=True)
    return path


def auth_for(profile_dir, **kwargs) -> CookieAuth:
    return CookieAuth(BASE_URL, CHECK_PATH, profile_dir, **kwargs)


def stub_probe(monkeypatch, *states):
    """Make _probe return the given states in order, recording each call."""
    calls = []
    sequence = list(states)

    def fake_probe(self, session):
        calls.append(session)
        return sequence[min(len(calls) - 1, len(sequence) - 1)]

    monkeypatch.setattr(CookieAuth, "_probe", fake_probe)
    return calls


def no_browser(monkeypatch):
    def explode(self):
        raise AssertionError("must not open a browser")

    monkeypatch.setattr(CookieAuth, "_read_cookies_from_profile", explode)


# --- cookie copy ----------------------------------------------------------


def test_cookies_are_copied_with_name_value_domain_and_path():
    session = _cookies_to_session(
        [
            playwright_cookie(),
            playwright_cookie(name="JSESSIONID", value="abc123", path="/learn"),
        ],
        requests.Session(),
    )

    jar = {cookie.name: cookie for cookie in session.cookies}
    assert set(jar) == {"BbRouter", "JSESSIONID"}
    assert jar["BbRouter"].value == "expires:1761000000"
    assert jar["BbRouter"].domain == "bb.example.edu"
    assert jar["JSESSIONID"].path == "/learn"


def test_parent_domain_cookies_are_kept():
    """Hamilton SSO may set cookies on the parent domain."""
    session = _cookies_to_session(
        [playwright_cookie(name="shib_session", domain=".example.edu")],
        requests.Session(),
    )

    assert "shib_session" in {cookie.name for cookie in session.cookies}


def test_browser_only_flags_are_dropped():
    """httpOnly, secure, sameSite and expires are the browser's business."""
    session = _cookies_to_session([playwright_cookie()], requests.Session())

    cookie = next(iter(session.cookies))
    assert cookie.expires is None


def test_nameless_or_valueless_cookies_are_skipped():
    session = _cookies_to_session(
        [
            {"value": "orphan", "domain": "bb.example.edu", "path": "/"},
            {"name": "", "value": "empty", "domain": "bb.example.edu", "path": "/"},
            {"name": "novalue", "domain": "bb.example.edu", "path": "/"},
            playwright_cookie(name="good", value="keep"),
        ],
        requests.Session(),
    )

    assert {cookie.name for cookie in session.cookies} == {"good"}


def test_empty_string_value_is_preserved():
    """An empty value is a real cookie; only a missing value is skipped."""
    session = _cookies_to_session(
        [playwright_cookie(name="flag", value="")], requests.Session()
    )

    assert "flag" in {cookie.name for cookie in session.cookies}


def test_copy_returns_the_same_session():
    session = requests.Session()
    assert _cookies_to_session([], session) is session


def test_cookie_round_trip_through_a_session():
    """_session_to_cookie_dicts must feed back into _cookies_to_session."""
    original = [
        playwright_cookie(),
        playwright_cookie(name="JSESSIONID", value="abc", path="/learn"),
    ]
    session = _cookies_to_session(original, requests.Session())

    dicts = _session_to_cookie_dicts(session)
    rebuilt = _cookies_to_session(dicts, requests.Session())

    assert {c.name: c.value for c in rebuilt.cookies} == {
        "BbRouter": "expires:1761000000",
        "JSESSIONID": "abc",
    }
    assert {c["name"] for c in dicts} == {"BbRouter", "JSESSIONID"}


# --- URLs -----------------------------------------------------------------


def test_check_url_joins_base_and_path():
    assert auth_for("/tmp/nope").check_url == f"{BASE_URL}{CHECK_PATH}"


def test_trailing_slash_on_base_url_does_not_double_up():
    auth = CookieAuth(f"{BASE_URL}/", CHECK_PATH, "/tmp/nope")
    assert auth.check_url == f"{BASE_URL}{CHECK_PATH}"


# --- session construction -------------------------------------------------


def test_session_raises_auth_expired_without_a_profile(tmp_path):
    with pytest.raises(AuthExpired, match="gt login blackboard"):
        auth_for(tmp_path / "missing").session()


def test_session_raises_auth_expired_when_nothing_has_cookies(profile, monkeypatch):
    monkeypatch.setattr(CookieAuth, "_read_cookies_from_profile", lambda self: [])

    with pytest.raises(AuthExpired, match="no longer valid"):
        auth_for(profile).session()


def test_session_is_cached_so_the_probe_runs_once(profile, monkeypatch):
    launcher.save_cookie_snapshot(profile, [playwright_cookie()])
    calls = stub_probe(monkeypatch, "valid")
    no_browser(monkeypatch)
    auth = auth_for(profile)

    first = auth.session()
    second = auth.session()

    assert first is second
    assert len(calls) == 1


def test_session_sets_json_and_user_agent_headers(profile, monkeypatch):
    launcher.save_cookie_snapshot(profile, [playwright_cookie()])
    stub_probe(monkeypatch, "valid")
    no_browser(monkeypatch)

    session = auth_for(profile).session()

    assert session.headers["Accept"] == "application/json"
    assert "HeadlessChrome" not in session.headers["User-Agent"]


# --- cookie source preference and staleness -------------------------------


def test_valid_snapshot_is_used_without_opening_a_browser(profile, monkeypatch):
    """The snapshot holds the session cookies the profile cannot keep."""
    launcher.save_cookie_snapshot(profile, [playwright_cookie(name="JSESSIONID")])
    stub_probe(monkeypatch, "valid")
    no_browser(monkeypatch)

    session = auth_for(profile).session()

    assert {cookie.name for cookie in session.cookies} == {"JSESSIONID"}


def test_profile_is_read_when_there_is_no_snapshot(profile, monkeypatch):
    stub_probe(monkeypatch, "valid")
    monkeypatch.setattr(
        CookieAuth,
        "_read_cookies_from_profile",
        lambda self: [playwright_cookie(name="from_profile")],
    )

    session = auth_for(profile).session()

    assert {cookie.name for cookie in session.cookies} == {"from_profile"}


def test_stale_snapshot_falls_back_to_the_profile(profile, monkeypatch):
    """A snapshot that no longer authenticates must not be the final word."""
    launcher.save_cookie_snapshot(profile, [playwright_cookie(name="stale")])
    stub_probe(monkeypatch, "expired", "valid")
    monkeypatch.setattr(
        CookieAuth,
        "_read_cookies_from_profile",
        lambda self: [playwright_cookie(name="from_profile")],
    )

    session = auth_for(profile).session()

    assert {cookie.name for cookie in session.cookies} == {"from_profile"}


def test_both_sources_stale_raises_auth_expired(profile, monkeypatch):
    launcher.save_cookie_snapshot(profile, [playwright_cookie(name="stale")])
    stub_probe(monkeypatch, "expired", "expired")
    monkeypatch.setattr(
        CookieAuth,
        "_read_cookies_from_profile",
        lambda self: [playwright_cookie(name="also_stale")],
    )

    with pytest.raises(AuthExpired, match="no longer valid"):
        auth_for(profile).session()


def test_offline_keeps_the_snapshot_rather_than_opening_a_browser(profile, monkeypatch):
    """Being offline says nothing about staleness, so do not re-read cookies."""
    launcher.save_cookie_snapshot(profile, [playwright_cookie(name="JSESSIONID")])
    stub_probe(monkeypatch, "offline")
    no_browser(monkeypatch)

    session = auth_for(profile).session()

    assert {cookie.name for cookie in session.cookies} == {"JSESSIONID"}


def test_corrupt_snapshot_falls_back_to_the_profile(profile, monkeypatch):
    (profile.parent / "blackboard.cookies.bin").write_bytes(b"not a dpapi blob")
    stub_probe(monkeypatch, "valid")
    monkeypatch.setattr(
        CookieAuth,
        "_read_cookies_from_profile",
        lambda self: [playwright_cookie(name="from_profile")],
    )

    session = auth_for(profile).session()

    assert {cookie.name for cookie in session.cookies} == {"from_profile"}


# --- probe and check ------------------------------------------------------


class _Response:
    def __init__(self, status_code, text) -> None:
        self.status_code = status_code
        self.text = text


def probing_auth(profile, monkeypatch, response=None, raises=None) -> CookieAuth:
    """An auth whose cached session returns one canned response."""
    auth = auth_for(profile)

    def fake_get(url, timeout=None, **kwargs):
        if raises is not None:
            raise raises
        return response

    session = requests.Session()
    monkeypatch.setattr(session, "get", fake_get)
    auth._session = session
    return auth


def test_probe_reports_valid_on_a_user_object(profile, monkeypatch):
    auth = probing_auth(profile, monkeypatch, _Response(200, '{"id": "_8675_1"}'))
    assert auth.check() == "valid"
    assert auth.is_valid() is True


def test_probe_reports_expired_on_401(profile, monkeypatch):
    auth = probing_auth(profile, monkeypatch, _Response(401, ""))
    assert auth.check() == "expired"
    assert auth.is_valid() is False


def test_probe_reports_expired_on_an_html_login_page(profile, monkeypatch):
    auth = probing_auth(profile, monkeypatch, _Response(200, "<html>Sign in</html>"))
    assert auth.check() == "expired"


def test_probe_reports_offline_on_a_network_error(profile, monkeypatch):
    """A dead network must not be reported as a logged-out session."""
    auth = probing_auth(
        profile, monkeypatch, raises=requests.ConnectionError("no route to host")
    )

    assert auth.check() == "offline"
    assert auth.is_valid() is False


def test_check_reports_expired_without_a_profile(tmp_path):
    """gt status must report needs-login, not crash, before the first login."""
    auth = auth_for(tmp_path / "missing")

    assert auth.check() == "expired"
    assert auth.is_valid() is False


def test_check_does_not_probe_twice_for_a_fresh_session(profile, monkeypatch):
    """session() already classified the state; check() must reuse it."""
    launcher.save_cookie_snapshot(profile, [playwright_cookie()])
    calls = stub_probe(monkeypatch, "valid")
    no_browser(monkeypatch)
    auth = auth_for(profile)

    assert auth.check() == "valid"
    assert auth.check() == "valid"
    assert len(calls) == 1


def test_check_surfaces_offline_after_building_from_the_snapshot(profile, monkeypatch):
    launcher.save_cookie_snapshot(profile, [playwright_cookie()])
    stub_probe(monkeypatch, "offline")
    no_browser(monkeypatch)

    assert auth_for(profile).check() == "offline"


# --- persist --------------------------------------------------------------


def test_persist_writes_the_current_jar_back_to_the_snapshot(profile, monkeypatch):
    """Blackboard slides the cookie expiry, so the sync must save it back."""
    launcher.save_cookie_snapshot(profile, [playwright_cookie(value="old")])
    stub_probe(monkeypatch, "valid")
    no_browser(monkeypatch)
    auth = auth_for(profile)

    session = auth.session()
    # Blackboard re-issued the cookie during the sync.
    session.cookies.set("BbRouter", "refreshed", domain="bb.example.edu", path="/")

    assert auth.persist() is True
    saved = {c["name"]: c["value"] for c in launcher.read_cookie_snapshot(profile)}
    assert saved["BbRouter"] == "refreshed"


def test_persist_is_a_no_op_without_a_session(profile):
    assert auth_for(profile).persist() is False


def test_persist_failure_does_not_raise(profile, monkeypatch):
    """A failed save must never cost an otherwise successful sync."""
    launcher.save_cookie_snapshot(profile, [playwright_cookie()])
    stub_probe(monkeypatch, "valid")
    no_browser(monkeypatch)
    auth = auth_for(profile)
    auth.session()

    monkeypatch.setattr(
        "gradetracker.auth.cookie_auth.save_cookie_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert auth.persist() is False
