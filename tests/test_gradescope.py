"""Gradescope connector tests (M3), driven by the fixtures.

Everything is faked: the library's Course/Assignment objects, the account, and
the HTTP session. conftest blocks a real login outright.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gradetracker.auth.base import AuthExpired
from gradetracker.connectors.base import ConnectorError
from gradetracker.connectors.gradescope import GradescopeConnector

FIXTURES = Path(__file__).parent / "fixtures" / "gradescope"

# Between PS4's due date (Oct 16) and the midterm's (Nov 5), so the fixtures
# cover past-due and upcoming at the same moment.
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

ECON = "812345"
CS220 = "812346"
MATH = "798001"


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeCourse:
    """Stands in for gradescopeapi's Course dataclass (which has no id)."""

    def __init__(self, data: dict) -> None:
        self.name = data["name"]
        self.full_name = data["full_name"]
        self.semester = data["semester"]
        self.year = data["year"]
        self.num_grades_published = data["num_grades_published"]
        self.num_assignments = data["num_assignments"]


class FakeAssignment:
    """Stands in for gradescopeapi's Assignment dataclass."""

    def __init__(self, data: dict) -> None:
        self.assignment_id = data["assignment_id"]
        self.name = data["name"]
        self.release_date = _parse(data["release_date"])
        self.due_date = _parse(data["due_date"])
        self.late_due_date = _parse(data["late_due_date"])
        self.submissions_status = data["submissions_status"]
        self.grade = data["grade"]
        self.max_grade = data["max_grade"]


def _parse(value):
    return datetime.fromisoformat(value) if value else None


class FakeAccount:
    """The library's Account, serving fixtures and recording calls."""

    def __init__(self, courses=None, assignments=None, errors=None) -> None:
        raw = courses if courses is not None else load("courses")
        self._courses = {
            kind: {cid: FakeCourse(data) for cid, data in group.items()}
            for kind, group in raw.items()
        }
        self._assignments = assignments or {}
        self._errors = errors or {}
        self.assignment_calls: list[str] = []

    def get_courses(self):
        return self._courses

    def get_assignments(self, course_id):
        self.assignment_calls.append(course_id)
        if course_id in self._errors:
            raise self._errors[course_id]
        data = self._assignments.get(course_id)
        if data is None:
            data = load(f"assignments_{course_id}")
        return [FakeAssignment(row) for row in data]


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class FakeSession:
    def __init__(self, pages=None, error=None) -> None:
        self.pages = pages or {}
        self.error = error
        self.calls: list[str] = []

    def get(self, url, timeout=None, **kwargs):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        for fragment, response in self.pages.items():
            if fragment in url:
                return response
        return FakeResponse("<html></html>", 404)


class FakeAuth:
    def __init__(self, account=None, session=None, raises=None) -> None:
        self.connection = type("Connection", (), {"account": account})()
        self._session = session or FakeSession()
        self._raises = raises
        self.email = "sstudent@example.edu"

    def session(self):
        if self._raises is not None:
            raise self._raises
        return self._session


def build(account=None, session=None, auth=None, **kwargs):
    """A connector with the clock pinned and sleeps recorded."""
    account = account if account is not None else FakeAccount()
    sleeps: list[float] = []
    connector = GradescopeConnector(
        # CS 220's fixtures are the scrape-fallback shape, so the default
        # session serves its page; the happy path is then skip-free.
        auth=auth or FakeAuth(account=account, session=session or scrape_session()),
        sleep=sleeps.append,
        now=lambda: NOW,
        **kwargs,
    )
    return connector, account, sleeps


def by_name(payload, course_id):
    course = next(c for c in payload["courses"] if c["id"] == course_id)
    return {row["name"]: row for row in course["assignments"]}


# --- term selection -------------------------------------------------------


def test_newest_term_is_chosen_when_gs_term_is_unset():
    connector, _, _ = build()

    payload = connector.fetch()

    assert payload["term"] == "Fall 2026"
    assert sorted(course["id"] for course in payload["courses"]) == [ECON, CS220]


def test_previous_term_is_excluded_by_default():
    connector, _, _ = build()

    assert MATH not in {course["id"] for course in connector.fetch()["courses"]}


def test_gs_term_overrides_the_newest_term():
    connector, _, _ = build(term="Spring 2026")

    payload = connector.fetch()

    assert payload["term"] == "Spring 2026"
    assert [course["id"] for course in payload["courses"]] == [MATH]


def test_gs_term_is_whitespace_tolerant():
    connector, _, _ = build(term="  Fall 2026  ")

    assert connector.fetch()["term"] == "Fall 2026"


def test_a_term_with_no_courses_yields_nothing():
    connector, _, _ = build(term="Fall 2099")

    payload = connector.fetch()

    assert payload["courses"] == []
    assert payload["term"] == "Fall 2099"


def test_fall_sorts_above_spring_in_the_same_year():
    """Spring < Summer < Fall, so an unset term must not pick Spring 2026."""
    courses = {
        "1": {
            "name": "A",
            "full_name": "A",
            "semester": "Spring",
            "year": "2026",
            "num_grades_published": None,
            "num_assignments": "1",
        },
        "2": {
            "name": "B",
            "full_name": "B",
            "semester": "Fall",
            "year": "2026",
            "num_grades_published": None,
            "num_assignments": "1",
        },
    }
    account = FakeAccount(courses={"student": courses, "instructor": {}}, assignments={"2": []})
    connector, _, _ = build(account=account)

    assert connector.fetch()["term"] == "Fall 2026"


def test_a_later_year_beats_a_later_semester():
    courses = {
        "1": {
            "name": "A",
            "full_name": "A",
            "semester": "Fall",
            "year": "2025",
            "num_grades_published": None,
            "num_assignments": "1",
        },
        "2": {
            "name": "B",
            "full_name": "B",
            "semester": "Spring",
            "year": "2026",
            "num_grades_published": None,
            "num_assignments": "1",
        },
    }
    account = FakeAccount(courses={"student": courses, "instructor": {}}, assignments={"2": []})
    connector, _, _ = build(account=account)

    assert connector.fetch()["term"] == "Spring 2026"


def test_no_student_courses_gives_a_null_term():
    account = FakeAccount(courses={"student": {}, "instructor": {}})
    connector, _, _ = build(account=account)

    payload = connector.fetch()

    assert payload["term"] is None
    assert payload["courses"] == []


# --- instructor courses ---------------------------------------------------


def test_instructor_courses_are_never_synced():
    """Those are someone else's gradebook, not the user's grades."""
    connector, account, _ = build()

    payload = connector.fetch()

    assert "655000" not in {course["id"] for course in payload["courses"]}
    assert "655000" not in account.assignment_calls


# --- status mapping -------------------------------------------------------


def test_graded_assignment_carries_its_score():
    connector, _, _ = build()

    rows = by_name(connector.fetch(), ECON)

    assert rows["Problem Set 1"]["status"] == "graded"
    assert rows["Problem Set 1"]["score"] == 18.0
    assert rows["Problem Set 1"]["max_points"] == 20.0
    assert rows["Problem Set 2"]["score"] == 19.5


def test_submitted_but_unscored_is_submitted():
    connector, _, _ = build()

    row = by_name(connector.fetch(), ECON)["Problem Set 3"]

    assert row["status"] == "submitted"
    assert row["score"] is None
    assert row["submitted"] is True


def test_unsubmitted_and_past_due_is_missing():
    connector, _, _ = build()

    row = by_name(connector.fetch(), ECON)["Problem Set 4"]

    assert row["status"] == "missing"
    assert row["submitted"] is False


def test_unsubmitted_and_not_yet_due_is_ungraded():
    """An upcoming exam is not a zero; the engine treats these differently."""
    connector, _, _ = build()

    row = by_name(connector.fetch(), ECON)["Midterm Exam"]

    assert row["status"] == "ungraded"
    assert row["submitted"] is False


def test_late_due_date_extends_the_missing_cutoff():
    """Past the due date but inside the late window is not yet missing."""
    assignments = {
        ECON: [
            {
                "assignment_id": "1",
                "name": "Late window open",
                "release_date": None,
                "due_date": "2026-10-18T03:59:00-04:00",
                "late_due_date": "2026-10-25T03:59:00-04:00",
                "submissions_status": "No Submission",
                "grade": None,
                "max_grade": None,
            }
        ]
    }
    account = FakeAccount(assignments={**assignments, CS220: []})
    connector, _, _ = build(account=account)

    assert by_name(connector.fetch(), ECON)["Late window open"]["status"] == "ungraded"


def test_dates_are_serialised_as_iso_strings():
    connector, _, _ = build()

    row = by_name(connector.fetch(), ECON)["Problem Set 2"]

    assert row["due_date"].startswith("2026-09-29T03:59:00")
    assert row["late_due_date"].startswith("2026-10-01T03:59:00")
    assert row["release_date"].startswith("2026-09-15T12:00:00")


def test_string_points_are_coerced_to_floats():
    """The dataclass annotates grade as str; the student parser sets floats."""
    account = FakeAccount(
        assignments={
            ECON: [
                {
                    "assignment_id": "1",
                    "name": "Stringy",
                    "release_date": None,
                    "due_date": None,
                    "late_due_date": None,
                    "submissions_status": "Submitted",
                    "grade": "17.5",
                    "max_grade": "20",
                }
            ],
            CS220: [],
        }
    )
    connector, _, _ = build(account=account)

    row = by_name(connector.fetch(), ECON)["Stringy"]

    assert row["score"] == 17.5
    assert row["max_points"] == 20.0


# --- shape and politeness -------------------------------------------------


def test_payload_shape():
    connector, _, _ = build()

    payload = connector.fetch()

    assert payload["source"] == "gradescope"
    assert payload["fetched_at"]
    assert payload["skipped"] == []
    course = next(c for c in payload["courses"] if c["id"] == ECON)
    assert course["name"] == "ECON 166"
    assert course["full_name"] == "Intermediate Microeconomics"
    assert course["semester"] == "Fall"
    assert course["year"] == "2026"
    assert set(course["assignments"][0]) == {
        "id",
        "name",
        "score",
        "max_points",
        "status",
        "due_date",
        "late_due_date",
        "release_date",
        "submitted",
    }


def test_sleeps_between_courses_but_not_before_the_first():
    connector, _, sleeps = build(delay_s=0.75)

    connector.fetch()

    assert sleeps == [0.75]


# --- scrape fallback ------------------------------------------------------


def scrape_session():
    page = (FIXTURES / "course_page.html").read_text(encoding="utf-8")
    return FakeSession(pages={f"/courses/{CS220}": FakeResponse(page)})


def test_scrape_fallback_fills_missing_scores_by_name():
    """CS 220 comes back all-unscored with work past due, so scrape it."""
    session = scrape_session()
    connector, _, _ = build(session=session)

    rows = by_name(connector.fetch(), CS220)

    assert rows["Lab 1: Linked Lists"]["score"] == 24.0
    assert rows["Lab 1: Linked Lists"]["max_points"] == 25.0
    assert rows["Lab 1: Linked Lists"]["status"] == "graded"
    assert rows["Lab 2: Hash Tables"]["score"] == 22.5
    assert any(f"/courses/{CS220}" in url for url in session.calls)


def test_scrape_leaves_genuinely_ungraded_rows_alone():
    """Lab 3 is not due yet and has no score on the page either."""
    connector, _, _ = build(session=scrape_session())

    row = by_name(connector.fetch(), CS220)["Lab 3: Balanced Trees"]

    assert row["score"] is None
    assert row["status"] == "ungraded"


def test_scrape_is_not_attempted_when_a_score_already_exists():
    """ECON 166 has scores, so its page must never be fetched."""
    session = scrape_session()
    connector, _, _ = build(session=session)

    connector.fetch()

    assert not any(f"/courses/{ECON}" in url for url in session.calls)


def test_scrape_is_not_attempted_when_nothing_is_past_due():
    """All-ungraded early in term is normal, not a parsing failure."""
    account = FakeAccount(
        assignments={
            ECON: [],
            CS220: [
                {
                    "assignment_id": "1",
                    "name": "Upcoming lab",
                    "release_date": None,
                    "due_date": "2026-12-01T03:59:00-05:00",
                    "late_due_date": None,
                    "submissions_status": "No Submission",
                    "grade": None,
                    "max_grade": None,
                }
            ],
        }
    )
    session = scrape_session()
    connector, _, _ = build(account=account, session=session)

    connector.fetch()

    assert session.calls == []


def test_scrape_with_no_table_changes_nothing():
    session = FakeSession(pages={f"/courses/{CS220}": FakeResponse("<html><body/></html>")})
    connector, _, _ = build(session=session)

    rows = by_name(connector.fetch(), CS220)

    assert rows["Lab 1: Linked Lists"]["score"] is None


def test_scrape_http_failure_skips_only_that_course():
    session = FakeSession(pages={f"/courses/{CS220}": FakeResponse("nope", 500)})
    connector, _, _ = build(session=session)

    payload = connector.fetch()

    assert [course["id"] for course in payload["courses"]] == [ECON]
    assert payload["skipped"][0]["id"] == CS220
    assert "500" in payload["skipped"][0]["error"]


def test_points_parser():
    parse = GradescopeConnector._parse_points

    assert parse("18.0 / 20.0") == (18.0, 20.0)
    assert parse("18/20") == (18.0, 20.0)
    assert parse("0 / 10") == (0.0, 10.0)
    assert parse("No Submission") is None
    assert parse("Ungraded") is None
    assert parse("") is None
    assert parse("- / 20") is None


# --- failure modes --------------------------------------------------------


def test_missing_credentials_surface_as_auth_expired():
    auth = FakeAuth(account=FakeAccount(), raises=AuthExpired("no saved credentials"))
    connector, _, _ = build(auth=auth)

    with pytest.raises(AuthExpired):
        connector.fetch()


def test_a_dead_session_listing_courses_is_auth_expired():
    """get_courses raises RuntimeError on a non-200, which means logged out."""

    class DeadAccount(FakeAccount):
        def get_courses(self):
            raise RuntimeError("Failed to access account page. Status code: 302")

    connector, _, _ = build(account=DeadAccount())

    with pytest.raises(AuthExpired, match="would not list your courses"):
        connector.fetch()


def test_an_unexpected_course_listing_error_is_a_connector_error():
    class BrokenAccount(FakeAccount):
        def get_courses(self):
            raise TypeError("NoneType is not subscriptable")

    connector, _, _ = build(account=BrokenAccount())

    with pytest.raises(ConnectorError, match="Could not list"):
        connector.fetch()


def test_a_missing_account_object_is_a_connector_error():
    auth = FakeAuth(account=None)
    connector, _, _ = build(auth=auth)

    with pytest.raises(ConnectorError, match="account object"):
        connector.fetch()


def test_one_failing_course_is_skipped_and_the_rest_survive():
    account = FakeAccount(errors={CS220: ValueError("table moved")})
    connector, _, _ = build(account=account)

    payload = connector.fetch()

    assert [course["id"] for course in payload["courses"]] == [ECON]
    assert payload["skipped"] == [
        {"id": CS220, "name": "CS 220", "error": "ValueError: table moved"}
    ]


def test_a_course_with_no_assignments_is_kept_not_skipped():
    account = FakeAccount(assignments={ECON: [], CS220: []})
    connector, _, _ = build(account=account)

    payload = connector.fetch()

    assert len(payload["courses"]) == 2
    assert payload["courses"][0]["assignments"] == []
    assert payload["skipped"] == []
