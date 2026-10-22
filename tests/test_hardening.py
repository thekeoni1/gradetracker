"""Pre-first-run hardening (step 8b).

The headline is the Gradescope password: gradescopeapi 1.8.1 sends the login
POST with params=, so the password lands in the URL and therefore in any
connection error's str(). That string used to reach sync_runs.message and the
Connections page.
"""

from __future__ import annotations

import sqlite3

import pytest
import requests
from typer.testing import CliRunner

from gradetracker import config, locking, redact, scheduler, sync
from gradetracker.auth import gradescope_auth
from gradetracker.auth.base import AuthExpired
from gradetracker.cli import app as cli_app
from gradetracker.connectors.blackboard import BlackboardConnector
from gradetracker.storage import db
from gradetracker.web.app import create_app

from tests.test_blackboard import FakeAuth, FakeResponse, FakeSession, load

runner = CliRunner()

PASSWORD = "hunter2-correct-horse"
LEAKY_MESSAGE = (
    "HTTPSConnectionPool(host='www.gradescope.com', port=443): Max retries "
    "exceeded with url: /login?session%5Bemail%5D=sam%40example.edu&"
    f"session%5Bpassword%5D={PASSWORD}&commit=Log+In (Caused by NameResolutionError)"
)


# --- the scrubber ---------------------------------------------------------


def test_scrub_redacts_a_url_encoded_password():
    scrubbed = redact.scrub(LEAKY_MESSAGE)

    assert PASSWORD not in scrubbed
    assert "session%5Bpassword%5D=[redacted]" in scrubbed
    # The rest of the message survives, so it is still diagnosable.
    assert "NameResolutionError" in scrubbed


@pytest.mark.parametrize(
    "text",
    [
        "session[password]=hunter2-correct-horse&x=1",
        "session%5Bpassword%5D=hunter2-correct-horse",
        "password=hunter2-correct-horse",
        "PASSWORD=hunter2-correct-horse&next=1",
        "api_key=hunter2-correct-horse",
    ],
)
def test_scrub_covers_the_common_spellings(text):
    assert PASSWORD not in redact.scrub(text)


def test_scrub_passes_harmless_text_through():
    assert redact.scrub("Blackboard returned 500 for /columns") == (
        "Blackboard returned 500 for /columns"
    )
    assert redact.scrub(None) is None
    assert redact.scrub("") == ""


def test_safe_error_is_only_the_class_name():
    exc = requests.ConnectionError(LEAKY_MESSAGE)

    assert redact.safe_error(exc) == "ConnectionError"
    assert PASSWORD not in redact.safe_error(exc)


# --- the password never reaches output, the database, or the page ---------


def _raise_connection_error(email, password):
    raise requests.ConnectionError(LEAKY_MESSAGE)


def test_the_password_never_reaches_the_cli_output(monkeypatch):
    monkeypatch.setattr(gradescope_auth, "_login", _raise_connection_error)

    result = runner.invoke(
        cli_app, ["login", "gradescope"], input=f"sam@example.edu\n{PASSWORD}\n"
    )

    assert result.exit_code == 1
    assert PASSWORD not in result.output
    assert "Could not reach Gradescope (ConnectionError)" in result.output


def test_the_password_never_reaches_sync_runs(monkeypatch, capsys):
    """One Wi-Fi blip mid-login must not persist the password in the database."""
    monkeypatch.setattr(config, "BB_BASE_URL", "")
    gradescope_auth.store_credentials("sam@example.edu", PASSWORD)
    monkeypatch.setattr(gradescope_auth, "_login", _raise_connection_error)

    code = sync.run_sync(["gradescope"], raw_only=False)
    captured = capsys.readouterr()

    assert code == 1
    assert PASSWORD not in captured.out
    assert PASSWORD not in captured.err

    conn = db.connect(config.DB_PATH)
    try:
        rows = conn.execute("SELECT message FROM sync_runs").fetchall()
        assert rows
        for row in rows:
            assert PASSWORD not in (row["message"] or "")
    finally:
        conn.close()


def test_the_password_never_reaches_the_connections_page(monkeypatch):
    conn = db.connect(config.DB_PATH)
    # Simulate the pre-fix state: a leaky message already stored.
    db.record_sync_run(
        conn, "gradescope", db.now_iso(), "error", message=LEAKY_MESSAGE
    )
    conn.close()

    web = create_app(db_path=config.DB_PATH, sync_runner=lambda: [], launcher=lambda: [])
    web.config.update(TESTING=True)
    with web.test_client() as client:
        body = client.get("/connections").get_data(as_text=True)

    assert PASSWORD not in body
    assert "[redacted]" in body


def test_the_password_never_reaches_the_gradescope_fragment(monkeypatch):
    monkeypatch.setattr(gradescope_auth, "_login", _raise_connection_error)
    web = create_app(db_path=config.DB_PATH, sync_runner=lambda: [], launcher=lambda: [])
    web.config.update(TESTING=True)

    with web.test_client() as client:
        body = client.post(
            "/connections/gradescope",
            data={"email": "sam@example.edu", "password": PASSWORD},
        ).get_data(as_text=True)

    assert PASSWORD not in body
    assert "ConnectionError" in body


# --- the login POST puts credentials in the body -------------------------


class RecordingSession:
    """Captures what the login helper actually sends."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.cookies = requests.cookies.RequestsCookieJar()
        self.headers: dict[str, str] = {}

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})

        class Response:
            history = [type("R", (), {"status_code": 302})()]
            text = '<meta name="csrf-token" content="abc">'
            cookies = requests.cookies.RequestsCookieJar()

        return Response()


def test_the_login_post_sends_credentials_in_the_body_not_the_url():
    """The actual fix: params= would put the password in the query string."""
    session = RecordingSession()

    ok = gradescope_auth._login_with_body(
        session, "sam@example.edu", PASSWORD, "token", "https://www.gradescope.com"
    )

    assert ok is True
    call = session.calls[0]
    assert "params" not in call, "credentials must not go in the query string"
    assert call["data"]["session[password]"] == PASSWORD
    assert PASSWORD not in call["url"]
    assert "?" not in call["url"]


def test_the_login_post_still_sets_the_csrf_header():
    session = RecordingSession()

    gradescope_auth._login_with_body(session, "e@h.edu", PASSWORD, "token")

    assert session.headers["X-CSRF-Token"] == "abc"


def test_a_login_without_a_redirect_is_a_failure():
    class NoRedirect(RecordingSession):
        def post(self, url, **kwargs):
            self.calls.append({"url": url, **kwargs})

            class Response:
                history = []
                text = ""
                cookies = requests.cookies.RequestsCookieJar()

            return Response()

    assert gradescope_auth._login_with_body(NoRedirect(), "e", "p", "t") is False


def test_the_library_helper_is_restored_after_a_login(monkeypatch):
    """The patch is scoped: the third-party module must not stay mutated."""
    from gradescopeapi.classes import connection as connection_module

    original = connection_module.login_set_session_cookies

    with gradescope_auth._body_login_patch():
        assert connection_module.login_set_session_cookies is not original

    assert connection_module.login_set_session_cookies is original


# --- Gradescope requests have a timeout ----------------------------------


def test_the_adapter_fills_in_a_default_timeout(monkeypatch):
    """A stalled Gradescope would otherwise hang the sync thread forever."""
    captured = {}

    def fake_send(self, request, **kwargs):
        captured.update(kwargs)
        return "sent"

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)

    gradescope_auth._TimeoutAdapter().send(None)

    assert captured["timeout"] == gradescope_auth.REQUEST_TIMEOUT_S


def test_an_explicit_timeout_is_not_overridden(monkeypatch):
    captured = {}

    def fake_send(self, request, **kwargs):
        captured.update(kwargs)
        return "sent"

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)

    gradescope_auth._TimeoutAdapter().send(None, timeout=5)

    assert captured["timeout"] == 5


def test_both_schemes_are_mounted():
    session = gradescope_auth._apply_timeouts(requests.Session())

    for url in ("https://www.gradescope.com/", "http://www.gradescope.com/"):
        assert isinstance(session.get_adapter(url), gradescope_auth._TimeoutAdapter)


# --- Blackboard Term availability ----------------------------------------


def bb_routes(availability="Term", term_id="_55_1"):
    """Course memberships whose availability is the given enum value."""
    page = load("courses_page1")
    for membership in page["results"]:
        membership["availability"]["available"] = availability
        membership["course"]["availability"]["available"] = availability
        membership["course"]["termId"] = term_id
    page.pop("paging", None)

    return {
        "/users/me": FakeResponse(200, load("me")),
        "/users/_8675_1/courses": FakeResponse(200, page),
        "/courses/_2100_1/gradebook/columns": FakeResponse(200, load("columns_econ166")),
        "/courses/_2100_1/gradebook/users/_8675_1": FakeResponse(200, load("grades_econ166")),
        "/courses/_2101_1/gradebook/columns": FakeResponse(200, load("columns_cs220")),
        "/courses/_2101_1/gradebook/users/_8675_1": FakeResponse(200, load("grades_cs220")),
    }


def fetch_with(availability="Term", term_id="_55_1", **kwargs):
    session = FakeSession(bb_routes(availability, term_id))
    connector = BlackboardConnector(
        auth=FakeAuth(session), base_url="https://bb.example.edu", sleep=lambda _: None, **kwargs
    )
    return connector.fetch()


@pytest.mark.parametrize("availability", ["Yes", "Term", "term", "YES"])
def test_available_and_term_courses_are_both_kept(availability):
    """Learn's enum is Yes | No | Disabled | Term, and SIS schools use Term."""
    payload = fetch_with(availability)

    assert len(payload["courses"]) == 2


@pytest.mark.parametrize("availability", ["No", "Disabled"])
def test_unavailable_courses_are_still_dropped(availability):
    payload = fetch_with(availability)

    assert payload["courses"] == []


def test_the_payload_carries_diagnostics_when_nothing_is_kept():
    payload = fetch_with("Disabled")

    assert payload["memberships_seen"] == 2
    assert payload["availability_seen"] == ["Disabled"]
    assert payload["terms_seen"] == ["_55_1"]


def test_the_sync_explains_an_empty_result(capsys):
    payload = {
        "courses": [],
        "memberships_seen": 3,
        "availability_seen": ["Disabled"],
        "terms_seen": ["_55_1", "_56_1"],
        "term_filter": None,
    }

    sync._explain_empty_blackboard(payload)
    captured = capsys.readouterr().err

    assert "saw 3 enrolment(s) but kept none" in captured
    assert "Disabled" in captured
    assert "_55_1, _56_1" in captured
    assert "set BB_TERM_ID" in captured


def test_the_sync_names_a_wrong_term_filter(capsys):
    payload = {
        "courses": [],
        "memberships_seen": 2,
        "availability_seen": ["Yes"],
        "terms_seen": ["_55_1"],
        "term_filter": "_99_1",
    }

    sync._explain_empty_blackboard(payload)

    assert "BB_TERM_ID is set to '_99_1'" in capsys.readouterr().err


def test_nothing_is_explained_when_courses_were_found(capsys):
    sync._explain_empty_blackboard({"courses": [{"x": 1}], "memberships_seen": 2})

    assert capsys.readouterr().err == ""


# --- the cross-process sync lock -----------------------------------------


def test_the_lock_is_exclusive(tmp_path):
    with locking.sync_lock(tmp_path) as first:
        assert first is True
        with locking.sync_lock(tmp_path) as second:
            assert second is False


def test_the_lock_is_released_afterwards(tmp_path):
    with locking.sync_lock(tmp_path) as acquired:
        assert acquired is True
    assert not locking.lock_path(tmp_path).exists()

    with locking.sync_lock(tmp_path) as again:
        assert again is True


def test_a_stale_lock_is_taken_over(tmp_path):
    """A crashed sync must not block syncing forever."""
    import os
    import time

    path = locking.lock_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("99999\n", encoding="utf-8")
    old = time.time() - (3 * 60 * 60)
    os.utime(path, (old, old))

    with locking.sync_lock(tmp_path) as acquired:
        assert acquired is True


def test_a_fresh_lock_is_respected(tmp_path):
    path = locking.lock_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("99999\n", encoding="utf-8")

    with locking.sync_lock(tmp_path) as acquired:
        assert acquired is False


def test_run_sync_skips_when_another_sync_holds_the_lock(capsys):
    path = locking.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("99999\n", encoding="utf-8")

    code = sync.run_sync(["blackboard"], raw_only=False)

    assert code == 0
    assert "another sync is running" in capsys.readouterr().out


def test_run_sync_releases_the_lock_on_the_way_out(monkeypatch, capsys):
    monkeypatch.setattr(config, "BB_BASE_URL", "")

    sync.run_sync(["blackboard"], raw_only=False)

    assert not locking.lock_path().exists()


# --- the scheduler's grace window ----------------------------------------


def test_the_misfire_grace_time_is_an_hour():
    background = scheduler.SyncScheduler(lambda: None, startup_delay_s=3600)
    background.start()
    try:
        job = next(j for j in background._scheduler.get_jobs() if j.id == "gt-sync-interval")
        assert job.misfire_grace_time == 3600
    finally:
        background.shutdown()


# --- the origin guard -----------------------------------------------------


@pytest.fixture
def guarded_client():
    """The app with a database, for exercising the before_request guard."""
    db.connect(config.DB_PATH).close()
    web = create_app(db_path=config.DB_PATH, sync_runner=lambda: ["ok"], launcher=lambda: ["ok"])
    web.config.update(TESTING=True)
    with web.test_client() as client:
        yield client


def test_a_non_local_host_header_is_refused(guarded_client):
    """DNS rebinding: a hostile domain resolving to 127.0.0.1 must not pass."""
    response = guarded_client.get("/", headers={"Host": "evil.example.com"})

    assert response.status_code == 403
    assert b"localhost" in response.data


def test_localhost_and_loopback_are_both_accepted(guarded_client):
    for host in ("127.0.0.1:5000", "localhost:5000", "127.0.0.1"):
        assert guarded_client.get("/", headers={"Host": host}).status_code == 200


def test_a_cross_site_post_is_refused(guarded_client):
    """A page on the open web form-POSTing to the local app."""
    response = guarded_client.post(
        "/connections/gradescope",
        data={"email": "attacker@evil.com", "password": "x"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403


def test_a_post_with_a_foreign_origin_is_refused(guarded_client):
    response = guarded_client.post(
        "/sync", headers={"Origin": "https://evil.example.com"}
    )

    assert response.status_code == 403
    assert b"Origin" in response.data


def test_a_post_with_a_foreign_referer_is_refused(guarded_client):
    response = guarded_client.post(
        "/sync", headers={"Referer": "https://evil.example.com/page"}
    )

    assert response.status_code == 403


def test_a_post_with_a_mismatched_local_port_is_refused(guarded_client):
    """Another local app on a different port is still a different origin."""
    response = guarded_client.post(
        "/sync",
        headers={"Host": "127.0.0.1:5000", "Origin": "http://127.0.0.1:9999"},
    )

    assert response.status_code == 403


def test_an_htmx_style_same_origin_post_passes(guarded_client):
    """HTMX sends matching same-origin headers, so nothing legitimate breaks."""
    response = guarded_client.post(
        "/sync",
        headers={
            "Host": "127.0.0.1:5000",
            "Origin": "http://127.0.0.1:5000",
            "Referer": "http://127.0.0.1:5000/",
            "Sec-Fetch-Site": "same-origin",
            "HX-Request": "true",
        },
    )

    assert response.status_code == 200


def test_a_post_with_no_origin_headers_passes(guarded_client):
    """A same-origin XHR, or curl from the user's own shell."""
    assert guarded_client.post("/sync").status_code == 200


def test_the_reconnect_endpoint_is_guarded(guarded_client):
    """This one pops a real SSO window, so it must not be cross-site reachable."""
    response = guarded_client.post(
        "/connections/blackboard/reconnect", headers={"Sec-Fetch-Site": "cross-site"}
    )

    assert response.status_code == 403


def test_the_map_save_endpoint_is_guarded(guarded_client):
    """The bulk mapping form writes to the database, so it is origin-checked."""
    response = guarded_client.post(
        "/courses/1/map/save",
        data={"category_1": "1"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403


def test_the_duplicate_resolver_is_guarded(guarded_client):
    """It excludes assignments, so it must not be cross-site reachable."""
    response = guarded_client.post(
        "/courses/1/duplicates/resolve",
        data={"keep": "gradescope"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403


def test_a_cross_site_get_is_still_allowed(guarded_client):
    """Reading is harmless, and blocking it would break ordinary navigation."""
    response = guarded_client.get("/", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 200


# --- browser launch failure ----------------------------------------------


def test_a_locked_profile_is_not_reported_as_a_timeout(monkeypatch, capsys, tmp_path):
    """The profile is locked when a headless sync is reading it right now."""
    from playwright.sync_api import Error as PlaywrightError

    from gradetracker.auth import launcher

    class FakeChromium:
        def launch_persistent_context(self, *args, **kwargs):
            raise PlaywrightError("ProcessSingleton: profile is already in use")

    class FakePlaywright:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(launcher, "sync_playwright", lambda: FakePlaywright())

    ok = launcher.login_blackboard(
        base_url="https://bb.example.edu",
        check_path="/learn/api/public/v1/users/me",
        profile_dir=tmp_path / "profile",
        timeout_s=1,
    )

    assert ok is False
    error = capsys.readouterr().err
    assert "Could not open the browser profile" in error
    assert "already in use" in error
    # The misleading message must NOT appear.
    assert "Timed out" not in error


def test_the_platform_preference_endpoint_is_guarded(guarded_client):
    """It excludes assignments as a side effect, so it is origin-checked too."""
    response = guarded_client.post(
        "/courses/1/preference",
        data={"preferred_source": "gradescope"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403


# --- serving to your own phone over Tailscale -----------------------------


def bound_client(*extra_hosts):
    """The app as `gt serve --tailscale` builds it, bound to those addresses."""
    db.connect(config.DB_PATH).close()
    web = create_app(
        db_path=config.DB_PATH,
        sync_runner=lambda: ["ok"],
        launcher=lambda: ["ok"],
        extra_hosts=extra_hosts,
    )
    web.config.update(TESTING=True)
    return web.test_client()


def test_a_request_to_an_unbound_address_is_still_refused(guarded_client):
    """The loopback check is what stops DNS rebinding, so it stays on."""
    response = guarded_client.get("/", headers={"Host": "100.101.102.103:5000"})

    assert response.status_code == 403


def test_the_tailnet_address_is_accepted_when_the_server_was_bound_to_it():
    client = bound_client("100.101.102.103")

    allowed = client.get("/", headers={"Host": "100.101.102.103:5000"})
    # Loopback keeps working: the laptop's own browser is unaffected.
    local = client.get("/", headers={"Host": "127.0.0.1:5000"})
    # Any OTHER address is still refused, and a name still cannot be rebound.
    other = client.get("/", headers={"Host": "192.168.1.9:5000"})
    named = client.get("/", headers={"Host": "evil.example.com"})

    assert allowed.status_code == 200
    assert local.status_code == 200
    assert other.status_code == 403
    assert named.status_code == 403


def test_a_post_from_the_phone_is_accepted_with_matching_headers():
    """HTMX on the phone sends Origin; it must match the address it loaded from."""
    client = bound_client("100.101.102.103")

    good = client.post(
        "/sync",
        headers={
            "Host": "100.101.102.103:5000",
            "Origin": "http://100.101.102.103:5000",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    # A page on the open web, posting to the same tailnet address.
    bad = client.post(
        "/sync",
        headers={
            "Host": "100.101.102.103:5000",
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert good.status_code == 200
    assert bad.status_code == 403


def test_an_ipv6_extra_host_is_matched_in_its_bracketed_form():
    """A Host header brackets an IPv6 literal, so a bare one would never match."""
    client = bound_client("fd7a:115c:a1e0::1")

    response = client.get("/", headers={"Host": "[fd7a:115c:a1e0::1]:5000"})

    assert response.status_code == 200


def test_create_app_refuses_a_name_as_an_extra_host():
    """The Host check exists to stop a name being pointed here; it must not take one."""
    import pytest as _pytest

    with _pytest.raises(ValueError, match="literal IP addresses"):
        create_app(
            db_path=config.DB_PATH,
            sync_runner=lambda: [],
            launcher=lambda: [],
            extra_hosts=("mypc.local",),
        )


def test_create_app_refuses_an_empty_extra_host():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        create_app(
            db_path=config.DB_PATH,
            sync_runner=lambda: [],
            launcher=lambda: [],
            extra_hosts=("",),
        )
