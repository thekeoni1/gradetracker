"""Blackboard connector tests (M2), driven by the fixtures in tests/fixtures.

No network and no browser: a fake AuthProvider hands back a fake session that
routes URLs to fixture files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gradetracker.auth.base import AuthExpired
from gradetracker.connectors.base import ConnectorError
from gradetracker.connectors.blackboard import BlackboardConnector, BlackboardPaths

FIXTURES = Path(__file__).parent / "fixtures" / "blackboard"
BASE_URL = "https://bb.example.edu"
USER_ID = "_8675_1"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload=None,
        text: str | None = None,
        content_type: str = "application/json",
        url: str | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)
        self.headers = {"Content-Type": content_type} if content_type else {}
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON object could be decoded")
        return self._payload


class FakeSession:
    """Serves the fixtures by URL and records every request it saw."""

    def __init__(self, routes: dict[str, FakeResponse], default: FakeResponse | None = None) -> None:
        self.routes = routes
        self.default = default
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        for fragment, response in self.routes.items():
            if fragment in url:
                if response.url is None:
                    response.url = url
                return response
        if self.default is not None:
            return self.default
        raise AssertionError(f"unrouted URL: {url}")


class FakeAuth:
    def __init__(self, session) -> None:
        self._session = session
        self.session_calls = 0

    def session(self):
        self.session_calls += 1
        return self._session

    def is_valid(self) -> bool:
        return True


def default_routes() -> dict[str, FakeResponse]:
    """The happy path: paged courses, then columns and grades per course."""
    return {
        "/users/me": FakeResponse(200, load("me")),
        "courses?offset=2": FakeResponse(200, load("courses_page2")),
        f"/users/{USER_ID}/courses": FakeResponse(200, load("courses_page1")),
        "/courses/_2100_1/gradebook/columns": FakeResponse(200, load("columns_econ166")),
        f"/courses/_2100_1/gradebook/users/{USER_ID}": FakeResponse(200, load("grades_econ166")),
        "/courses/_2101_1/gradebook/columns": FakeResponse(200, load("columns_cs220")),
        f"/courses/_2101_1/gradebook/users/{USER_ID}": FakeResponse(200, load("grades_cs220")),
    }


def build(routes=None, **kwargs):
    """A connector wired to a fake session; returns (connector, session, sleeps)."""
    session = FakeSession(routes if routes is not None else default_routes())
    sleeps: list[float] = []
    connector = BlackboardConnector(
        auth=FakeAuth(session),
        base_url=BASE_URL,
        sleep=sleeps.append,
        **kwargs,
    )
    return connector, session, sleeps


# --- happy path -----------------------------------------------------------


def test_fetch_returns_user_courses_columns_and_grades():
    connector, _, _ = build()

    payload = connector.fetch()

    assert payload["source"] == "blackboard"
    assert payload["fetched_at"]
    assert payload["user"]["userName"] == "sstudent"
    assert len(payload["courses"]) == 2

    names = [course["course"]["name"] for course in payload["courses"]]
    assert names == [
        "ECON 166: Intermediate Microeconomics",
        "CS 220: Data Structures",
    ]

    for course in payload["courses"]:
        assert len(course["columns"]) == 6
        assert len(course["grades"]) == 6

    # The membership fields survive alongside the fetched data.
    econ = payload["courses"][0]
    assert econ["courseId"] == "_2100_1"
    assert econ["courseRoleId"] == "Student"


def test_fetch_follows_pagination():
    """Page 1 carries paging.nextPage, so page 2 must be requested too."""
    connector, session, _ = build()

    connector.fetch()

    urls = [url for url, _ in session.calls]
    assert any("courses?offset=2" in url for url in urls), urls
    # nextPage is relative, so it must be joined to the base URL exactly once.
    next_page_calls = [url for url in urls if "offset=2" in url]
    assert next_page_calls == [
        f"{BASE_URL}/learn/api/public/v1/users/{USER_ID}/courses?offset=2&expand=course"
    ]


def test_expand_course_requested_on_first_page_only():
    """The nextPage link already carries its own query string."""
    connector, session, _ = build()

    connector.fetch()

    first_courses_call = next(
        (url, params) for url, params in session.calls if url.endswith("/courses")
    )
    assert first_courses_call[1] == {"expand": "course"}
    offset_call = next(params for url, params in session.calls if "offset=2" in url)
    assert offset_call is None


def test_unavailable_course_is_dropped():
    """Page 2's MATH 113 is availability No, so it must not come back."""
    connector, _, _ = build()

    payload = connector.fetch()

    ids = [course["courseId"] for course in payload["courses"]]
    assert "_1900_1" not in ids
    assert ids == ["_2100_1", "_2101_1"]


def test_sleeps_between_courses_but_not_before_the_first():
    connector, _, sleeps = build(delay_s=0.75)

    connector.fetch()

    assert sleeps == [0.75]  # two courses, one gap


def test_auth_session_opened_once_per_sync():
    """One sync must not reopen the browser per course."""
    session = FakeSession(default_routes())
    auth = FakeAuth(session)
    connector = BlackboardConnector(auth=auth, base_url=BASE_URL, sleep=lambda _: None)

    connector.fetch()

    assert auth.session_calls == 1


# --- filtering ------------------------------------------------------------


def test_term_filter_keeps_only_the_matching_term():
    connector, _, _ = build(term_id="_55_1")
    assert len(connector.fetch()["courses"]) == 2

    connector, _, _ = build(term_id="_99_1")
    assert connector.fetch()["courses"] == []


# --- failure modes --------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_on_the_user_record_raises_auth_expired(status):
    """A 403 on your own user record really is an auth problem."""
    routes = default_routes()
    routes["/users/me"] = FakeResponse(status, {"status": status})

    connector, _, _ = build(routes)

    with pytest.raises(AuthExpired):
        connector.fetch()


def test_401_on_one_course_still_raises_auth_expired():
    routes = default_routes()
    routes["/courses/_2101_1/gradebook/columns"] = FakeResponse(401, {"status": 401})

    connector, _, _ = build(routes)

    with pytest.raises(AuthExpired):
        connector.fetch()


def test_403_on_one_course_skips_it_and_keeps_the_rest():
    """A hidden gradebook must not cost every other grade."""
    routes = default_routes()
    routes["/courses/_2101_1/gradebook/columns"] = FakeResponse(403, {"status": 403})

    connector, _, _ = build(routes)
    payload = connector.fetch()

    assert [course["courseId"] for course in payload["courses"]] == ["_2100_1"]
    assert payload["skipped"] == [
        {
            "courseId": "_2101_1",
            "status": 403,
            "url": "/learn/api/public/v2/courses/_2101_1/gradebook/columns",
        }
    ]


def test_403_on_a_grades_endpoint_also_skips_the_course():
    routes = default_routes()
    routes[f"/courses/_2100_1/gradebook/users/{USER_ID}"] = FakeResponse(403, {"status": 403})

    connector, _, _ = build(routes)
    payload = connector.fetch()

    assert [course["courseId"] for course in payload["courses"]] == ["_2101_1"]
    assert payload["skipped"][0]["courseId"] == "_2100_1"


def test_no_skips_on_a_clean_sync():
    connector, _, _ = build()

    assert connector.fetch()["skipped"] == []


def test_expired_session_mid_sync_still_raises_auth_expired():
    """A session that dies between courses must not look like a parse failure."""
    routes = default_routes()
    routes["/courses/_2101_1/gradebook/columns"] = FakeResponse(401, {"status": 401})

    connector, _, _ = build(routes)

    with pytest.raises(AuthExpired):
        connector.fetch()


def test_server_error_raises_connector_error_naming_status_and_url():
    routes = default_routes()
    routes["/courses/_2100_1/gradebook/columns"] = FakeResponse(500, {"status": 500})

    connector, _, _ = build(routes)

    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch()

    message = str(excinfo.value)
    assert "500" in message
    assert "gradebook/columns" in message


def test_html_login_page_is_an_expired_session_not_a_parse_error():
    """requests follows the SSO redirect, so a 200 of HTML means logged out."""
    routes = default_routes()
    routes["/users/me"] = FakeResponse(
        200, None, text="<html>Sign in</html>", content_type="text/html; charset=utf-8"
    )

    connector, _, _ = build(routes)

    with pytest.raises(AuthExpired, match="login page"):
        connector.fetch()


def test_redirect_to_another_host_is_an_expired_session():
    """The SSO hop lands on a different host entirely."""
    routes = default_routes()
    routes["/users/me"] = FakeResponse(
        200,
        None,
        text="<html>Hamilton SSO</html>",
        content_type="text/html",
        url="https://sso.hamilton.edu/idp/profile/SAML2/Redirect/SSO",
    )

    connector, _, _ = build(routes)

    with pytest.raises(AuthExpired, match="sso.hamilton.edu"):
        connector.fetch()


def test_malformed_json_with_a_json_content_type_is_a_connector_error():
    """A genuine parse failure must stay a connector bug, not a login prompt."""
    routes = default_routes()
    routes["/users/me"] = FakeResponse(200, None, text="{broken", content_type="application/json")

    connector, _, _ = build(routes)

    with pytest.raises(ConnectorError, match="non-JSON"):
        connector.fetch()


def test_missing_user_id_raises_connector_error():
    routes = default_routes()
    routes["/users/me"] = FakeResponse(200, {"userName": "sstudent"})

    connector, _, _ = build(routes)

    with pytest.raises(ConnectorError, match="No user id"):
        connector.fetch()


def test_pagination_cycle_raises_rather_than_hanging():
    """A nextPage that points at itself is a server bug, so surface it."""
    self_referential = {
        "results": [],
        "paging": {"nextPage": f"/learn/api/public/v1/users/{USER_ID}/courses?offset=2"},
    }
    routes = default_routes()
    routes[f"/users/{USER_ID}/courses"] = FakeResponse(200, self_referential)
    routes["courses?offset=2"] = FakeResponse(200, self_referential)

    connector, _, _ = build(routes)

    with pytest.raises(ConnectorError, match="Pagination cycle"):
        connector.fetch()


def test_next_page_pointing_back_at_page_one_is_caught():
    """The start URL counts as seen, so rows cannot be yielded twice."""
    routes = default_routes()
    routes[f"/users/{USER_ID}/courses"] = FakeResponse(
        200,
        {
            "results": load("courses_page1")["results"],
            "paging": {"nextPage": f"/learn/api/public/v1/users/{USER_ID}/courses"},
        },
    )

    connector, _, _ = build(routes)

    with pytest.raises(ConnectorError, match="Pagination cycle"):
        connector.fetch()


# --- configurability ------------------------------------------------------


def test_endpoint_paths_are_swappable():
    """Recon may find Hamilton serves grades somewhere else entirely."""
    paths = BlackboardPaths(
        me="/internal/whoami",
        courses="/internal/{userId}/enrollments",
        columns="/internal/{courseId}/cols",
        grades="/internal/{courseId}/grades/{userId}",
    )
    routes = {
        "/internal/whoami": FakeResponse(200, load("me")),
        f"/internal/{USER_ID}/enrollments": FakeResponse(200, {"results": load("courses_page1")["results"]}),
        "/internal/_2100_1/cols": FakeResponse(200, load("columns_econ166")),
        f"/internal/_2100_1/grades/{USER_ID}": FakeResponse(200, load("grades_econ166")),
        "/internal/_2101_1/cols": FakeResponse(200, load("columns_cs220")),
        f"/internal/_2101_1/grades/{USER_ID}": FakeResponse(200, load("grades_cs220")),
    }

    connector, session, _ = build(routes, paths=paths)
    payload = connector.fetch()

    assert len(payload["courses"]) == 2
    assert all("/learn/api/" not in url for url, _ in session.calls)


def test_default_paths_are_the_public_rest_endpoints():
    paths = BlackboardPaths()

    assert paths.me == "/learn/api/public/v1/users/me"
    assert paths.courses == "/learn/api/public/v1/users/{userId}/courses"
    assert paths.columns == "/learn/api/public/v2/courses/{courseId}/gradebook/columns"
    assert paths.grades == "/learn/api/public/v2/courses/{courseId}/gradebook/users/{userId}"
