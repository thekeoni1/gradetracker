"""Gradescope connector (M3).

Same fetch() -> dict shape as Blackboard, so the normalizer and engine never
learn which platform a grade came from.

Notes on gradescopeapi==1.8.1, from reading its source:

- Course carries no id; the id is the key in get_courses()["student"], so
  courses must be read with .items().
- Course.semester and Course.year come from splitting the term heading on a
  space, e.g. "Fall 2026" -> ("Fall", "2026"). year is a str.
- Assignment.grade and Assignment.max_grade are annotated str but the student
  view sets floats or None, so both are coerced here.
- Assignment.submissions_status is the literal status cell text ("No
  Submission", "Submitted", ...) EXCEPT when a score parsed, where the library
  hardcodes "Submitted". So a score's presence, not the status string, is what
  decides "graded".
- The student-view parser drops the first and last <tr role="row">, assuming a
  header row and a dropzone tail row. A course whose table lacks the tail row
  loses its last assignment, which is one reason _scrape_scores exists.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from bs4 import BeautifulSoup

from gradetracker import config
from gradetracker.auth.base import AuthExpired
from gradetracker.connectors.base import ConnectorError

DEFAULT_BASE_URL = "https://www.gradescope.com"

# Within one calendar year, later terms sort higher.
_SEMESTER_ORDER = {"winter": 1, "spring": 2, "summer": 3, "fall": 4}

# Status cell texts that mean nothing was handed in.
_NOT_SUBMITTED = {"", "no submission", "missing"}


def _as_float(value: Any) -> float | None:
    """Points may arrive as float, str or None depending on the code path."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_aware(moment: datetime | None) -> datetime | None:
    """Gradescope dates are usually tz-aware; assume UTC when they are not."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _iso(moment: datetime | None) -> str | None:
    aware = _as_aware(moment)
    return aware.isoformat() if aware else None


def _term_key(semester: str | None, year: str | None) -> tuple[int, int]:
    """Sortable (year, semester) so the newest term can be picked."""
    try:
        year_number = int(str(year))
    except (TypeError, ValueError):
        year_number = 0
    return year_number, _SEMESTER_ORDER.get(str(semester or "").strip().lower(), 0)


def _term_label(semester: str | None, year: str | None) -> str:
    return f"{semester or ''} {year or ''}".strip()


def _is_submitted(status: Any) -> bool:
    """Anything but an explicit "no submission" counts as handed in."""
    return str(status or "").strip().lower() not in _NOT_SUBMITTED


def _row_due(row: dict[str, Any]) -> datetime | None:
    """The last moment a submission would count, read back off a built row."""
    moments = [
        datetime.fromisoformat(row[key])
        for key in ("due_date", "late_due_date")
        if row.get(key)
    ]
    return max(moments) if moments else None


class GradescopeConnector:
    """Fetches one dict per sync: the chosen term's student courses and grades."""

    source = "gradescope"

    def __init__(
        self,
        auth,
        term: str | None = None,
        delay_s: float = 0.75,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = DEFAULT_BASE_URL,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.auth = auth
        self.term = term
        self.delay_s = delay_s
        self.sleep = sleep
        self.base_url = base_url.rstrip("/")
        # Injectable so "is it past due" is testable without freezing the clock.
        self.now = now or (lambda: datetime.now(timezone.utc))

    # --- term selection --------------------------------------------------

    def _select_term(self, courses: dict[str, Any]) -> str | None:
        """The term to sync: the configured one, else the newest one present."""
        if self.term:
            return self.term.strip()
        if not courses:
            return None
        newest = max(
            courses.values(), key=lambda course: _term_key(course.semester, course.year)
        )
        return _term_label(newest.semester, newest.year)

    # --- status ----------------------------------------------------------

    def _effective_due(self, assignment) -> datetime | None:
        """The last moment a submission would still count."""
        due = _as_aware(getattr(assignment, "due_date", None))
        late = _as_aware(getattr(assignment, "late_due_date", None))
        if due and late:
            return max(due, late)
        return late or due

    def _status_for(self, assignment, score: float | None) -> str:
        """graded | submitted | missing | ungraded, per the PDF's rules."""
        if score is not None:
            return "graded"
        if _is_submitted(getattr(assignment, "submissions_status", None)):
            return "submitted"
        due = self._effective_due(assignment)
        if due and due < self.now():
            return "missing"
        return "ungraded"

    def _assignment_row(self, assignment) -> dict[str, Any]:
        score = _as_float(getattr(assignment, "grade", None))
        return {
            "id": getattr(assignment, "assignment_id", None),
            "name": (getattr(assignment, "name", "") or "").strip(),
            "score": score,
            "max_points": _as_float(getattr(assignment, "max_grade", None)),
            "status": self._status_for(assignment, score),
            "due_date": _iso(getattr(assignment, "due_date", None)),
            "late_due_date": _iso(getattr(assignment, "late_due_date", None)),
            "release_date": _iso(getattr(assignment, "release_date", None)),
            "submitted": _is_submitted(getattr(assignment, "submissions_status", None)),
        }

    # --- scrape fallback -------------------------------------------------

    def _needs_scrape(self, rows: list[dict[str, Any]]) -> bool:
        """Only when the library found no score at all yet something is overdue.

        A term that has genuinely not been graded yet looks the same as a
        library parsing failure, so the past-due test is what separates them.
        """
        if not rows:
            return False
        if any(row["score"] is not None for row in rows):
            return False
        now = self.now()
        # The same effective cutoff the status mapping uses: inside an open late
        # window nothing is overdue yet, so an absent score is simply normal.
        return any(_row_due(row) is not None and _row_due(row) < now for row in rows)

    def _scrape_scores(self, course_id: str, session) -> dict[str, tuple[float, float]]:
        """Parse the student assignments table for "earned / possible" cells.

        The fallback for when the library's own parser comes back empty-handed.
        Returns {assignment name: (score, max_points)}.
        """
        url = f"{self.base_url}/courses/{course_id}"
        try:
            response = session.get(url, timeout=30)
        except Exception as exc:
            raise ConnectorError(f"Could not load {url}: {exc}") from exc

        if getattr(response, "status_code", 200) != 200:
            raise ConnectorError(f"Gradescope returned {response.status_code} for {url}")

        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.select_one("#assignments-student-table")
        if table is None:
            return {}

        scraped: dict[str, tuple[float, float]] = {}
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            name = cells[0].get_text(strip=True)
            if not name:
                continue
            for cell in cells[1:]:
                points = self._parse_points(cell.get_text(strip=True))
                if points is not None:
                    scraped[name] = points
                    break
        return scraped

    @staticmethod
    def _parse_points(text: str) -> tuple[float, float] | None:
        """"18.0 / 20.0" -> (18.0, 20.0). Anything else -> None."""
        if "/" not in text:
            return None
        earned, _, possible = text.partition("/")
        score = _as_float(earned.strip())
        total = _as_float(possible.strip())
        if score is None or total is None:
            return None
        return score, total

    def _merge_scraped(
        self, rows: list[dict[str, Any]], scraped: dict[str, tuple[float, float]]
    ) -> list[dict[str, Any]]:
        """Fill in missing scores by assignment name, leaving the rest alone."""
        for row in rows:
            if row["score"] is not None:
                continue
            points = scraped.get(row["name"])
            if points is None:
                continue
            row["score"], row["max_points"] = points
            row["status"] = "graded"
            row["submitted"] = True
        return rows

    # --- fetch -----------------------------------------------------------

    def fetch(self) -> dict[str, Any]:
        """Log in, then one dict holding the chosen term's courses and grades."""
        session = self.auth.session()
        connection = getattr(self.auth, "connection", None)
        account = getattr(connection, "account", None)
        if account is None:
            raise ConnectorError("Gradescope login did not produce an account object")

        try:
            all_courses = account.get_courses()
        except RuntimeError as exc:
            # A non-200 on /account for a logged-in session means it is gone.
            raise AuthExpired(f"Gradescope would not list your courses: {exc}") from exc
        except Exception as exc:
            raise ConnectorError(f"Could not list Gradescope courses: {exc}") from exc

        # Instructor courses are someone else's gradebook, not the user's grades.
        student_courses = (all_courses or {}).get("student") or {}
        term = self._select_term(student_courses)

        courses: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        attempted = 0

        for course_id, course in student_courses.items():
            if term and _term_label(course.semester, course.year) != term:
                continue

            if attempted:
                self.sleep(self.delay_s)
            attempted += 1

            try:
                rows = [
                    self._assignment_row(assignment)
                    for assignment in account.get_assignments(course_id) or []
                ]
                if self._needs_scrape(rows):
                    rows = self._merge_scraped(rows, self._scrape_scores(course_id, session))
            except Exception as exc:
                # One unreadable course must not cost the whole sync.
                skipped.append(
                    {
                        "id": course_id,
                        "name": getattr(course, "name", None),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue

            courses.append(
                {
                    "id": course_id,
                    "name": getattr(course, "name", None),
                    "full_name": getattr(course, "full_name", None),
                    "semester": getattr(course, "semester", None),
                    "year": getattr(course, "year", None),
                    "assignments": rows,
                }
            )

        return {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": self.source,
            "term": term,
            "courses": courses,
            "skipped": skipped,
        }
