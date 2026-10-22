"""Raw dumps -> source_courses and assignments rows (M4).

One function per source. Normalisation is idempotent: running the same dump
twice leaves every row identical except last_seen_at.

What sync must never overwrite, because you set it by hand:
category_id and category_locked, extra_credit, missing_as_zero, and any row
with manual = 1. Only name, earned, possible, status, due_at, raw and
last_seen_at are refreshed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from gradetracker.storage.db import now_iso
from gradetracker.storage.link import course_code, term_from_course_id

# Blackboard columns that are not real assignments. The instructor's own
# running total would double count every grade underneath it.
_TOTAL_NAMES = {
    "total",
    "weighted total",
    "running total",
    "final grade",
    "overall grade",
    "course grade",
}

STATUSES = ("graded", "submitted", "ungraded", "missing", "excused")

# Assignment-name trigger -> the category names it should look for. Ordered:
# the first rule that matches AND finds a category wins, so "Final Exam" lands
# in Finals when there is one and in Exams when there is not.
_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        r"\bproblem\s+sets?\b|\bpsets?\b|\bps\b|\bhw\b|\bhomeworks?\b",
        ("problem", "homework", "assignment"),
    ),
    (r"\bquiz(?:zes)?\b", ("quiz",)),
    (r"\bfinals?\b", ("final",)),
    (r"\bmidterms?\b", ("midterm", "exam", "test")),
    (r"\bexams?\b|\btests?\b", ("exam", "midterm", "test")),
    (r"\blabs?\b", ("lab",)),
    (r"\bprojects?\b", ("project",)),
    (r"\bparticipation\b|\battendance\b", ("participation", "attendance")),
    (r"\bessays?\b|\bpapers?\b", ("essay", "paper", "writing")),
    (r"\breading\s+responses?\b|\bresponses?\b", ("reading", "response")),
)


@dataclass
class NormalizeResult:
    """What one normalisation run did."""

    source_courses: int = 0
    assignments: int = 0
    dropped: int = 0
    categorised: int = 0


# --- category suggestion --------------------------------------------------

# How a category's own name can match an assignment name, strongest first:
# the whole name as a phrase (in either number), then one word of it.
_MATCH_PHRASE, _MATCH_WORD = 0, 1


def _stem(word: str) -> str:
    """A crude singular: "codelets" -> "codelet", "quizzes" -> "quizz".

    Syllabi name a category in the plural and the assignments in the singular
    ("Codelets" holds "Codelet 00"), which is the whole reason this exists. A
    wrong stem is harmless: it simply fails to match, and the keyword table
    below still gets its turn.
    """
    for suffix in ("es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _has_whole_word(name: str, phrase: str) -> bool:
    """Whether `phrase` appears in `name` and is not part of a longer word.

    Whole words only, so "Attendance and Participation" cannot match on "and",
    and a "Labs" category cannot claim "Syllabus quiz".
    """
    if not phrase:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", name) is not None


def _name_match(name: str, category_name: str) -> tuple[int, int] | None:
    """How strongly a category's own name matches an assignment name.

    (strength, length of the matched text), or None. A lower strength wins, and
    a longer match breaks a tie, so "Programming Projects" beats a bare
    "Projects" category on "Programming Project 2".

    The plural and the singular of the whole name count the same, so a
    "Projects" category reads "Final Project" the way a "Project" category
    already did.
    """
    if not category_name:
        return None

    words = re.findall(r"[a-z0-9]+", category_name)
    singular = " ".join(_stem(word) for word in words)
    for phrase in (category_name, singular):
        if _has_whole_word(name, phrase):
            return (_MATCH_PHRASE, len(phrase))

    # One word of a multi-word name: "Programming Projects" on "Project 00".
    # Four letters minimum, so "and", "of" and "the" cannot match.
    matched = [
        stem
        for stem in (_stem(word) for word in words)
        if len(stem) >= 4 and _has_whole_word(name, stem)
    ]
    if matched:
        return (_MATCH_WORD, max(len(stem) for stem in matched))

    return None


def suggest_category(
    assignment_name: str | None, categories: Iterable[sqlite3.Row | dict]
) -> int | None:
    """The category an assignment name implies, or None to leave it unmapped.

    Pure, so M6's mapping screen can reuse it for one-click suggestions.
    """
    name = (assignment_name or "").strip().lower()
    if not name:
        return None

    options = [
        (int(category["id"]), str(category["name"] or "").strip().lower())
        for category in categories
    ]
    if not options:
        return None

    # A category whose own name appears in the assignment is the strongest
    # signal, in the plural as well: "Codelets" owns "Codelet 00". The best
    # match across every category wins, not the first one found, so a weak
    # single-word hit cannot beat a category named outright.
    best: tuple[tuple[int, int, int], int] | None = None
    for index, (category_id, category_name) in enumerate(options):
        match = _name_match(name, category_name)
        if match is None:
            continue
        strength, length = match
        key = (strength, -length, index)
        if best is None or key < best[0]:
            best = (key, category_id)
    if best is not None:
        return best[1]

    # Then the keyword table. A rule that matches but finds no category falls
    # through to the next rule rather than giving up.
    for pattern, targets in _CATEGORY_RULES:
        if not re.search(pattern, name):
            continue
        for target in targets:
            for category_id, category_name in options:
                if target in category_name:
                    return category_id

    return None


def apply_category_suggestions(conn: sqlite3.Connection, source_course_id: int) -> int:
    """Auto-map this course's unmapped assignments. Returns how many were mapped.

    Only touches rows with category_locked = 0 and no category yet, so a choice
    you made by hand survives every future sync.
    """
    course = conn.execute(
        "SELECT course_id FROM source_courses WHERE id = ?", (source_course_id,)
    ).fetchone()
    if course is None or course["course_id"] is None:
        return 0

    categories = conn.execute(
        "SELECT id, name FROM categories WHERE course_id = ?", (course["course_id"],)
    ).fetchall()
    if not categories:
        return 0

    rows = conn.execute(
        """
        SELECT id, name FROM assignments
        WHERE source_course_id = ? AND category_id IS NULL AND category_locked = 0
        """,
        (source_course_id,),
    ).fetchall()

    mapped = 0
    for row in rows:
        category_id = suggest_category(row["name"], categories)
        if category_id is None:
            continue
        conn.execute(
            "UPDATE assignments SET category_id = ? WHERE id = ?",
            (category_id, row["id"]),
        )
        mapped += 1

    conn.commit()
    return mapped


# --- shared upserts -------------------------------------------------------


def _upsert_source_course(
    conn: sqlite3.Connection,
    source: str,
    platform_id: str,
    name: str | None,
    code: str | None,
    term: str | None,
    raw: Any,
) -> int:
    """Insert or refresh a platform course. Never touches course_id (the link)."""
    seen = now_iso()
    conn.execute(
        """
        INSERT INTO source_courses (source, platform_id, name, code, term, raw, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, platform_id) DO UPDATE SET
            name = excluded.name,
            code = excluded.code,
            term = excluded.term,
            raw = excluded.raw,
            last_seen_at = excluded.last_seen_at
        """,
        (source, str(platform_id), name, code, term, json.dumps(raw, sort_keys=True), seen),
    )
    row = conn.execute(
        "SELECT id FROM source_courses WHERE source = ? AND platform_id = ?",
        (source, str(platform_id)),
    ).fetchone()
    return int(row["id"])


def _upsert_assignment(
    conn: sqlite3.Connection,
    source_course_id: int,
    platform_id: str,
    name: str | None,
    earned: float | None,
    possible: float | None,
    status: str,
    due_at: str | None,
    raw: Any,
) -> None:
    """Insert or refresh one assignment.

    The DO UPDATE lists only platform-owned fields, and skips rows you created
    yourself, so a manual row can never be clobbered by a platform row that
    happens to share its key.
    """
    if status not in STATUSES:
        status = "ungraded"

    conn.execute(
        """
        INSERT INTO assignments (
            source_course_id, platform_id, name, earned, possible, status,
            due_at, raw, last_seen_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source_course_id, platform_id) DO UPDATE SET
            name = excluded.name,
            earned = excluded.earned,
            possible = excluded.possible,
            status = excluded.status,
            due_at = excluded.due_at,
            raw = excluded.raw,
            last_seen_at = excluded.last_seen_at
        WHERE assignments.manual = 0
        """,
        (
            source_course_id,
            str(platform_id),
            name,
            earned,
            possible,
            status,
            due_at,
            json.dumps(raw, sort_keys=True),
            now_iso(),
        ),
    )


# --- Blackboard -----------------------------------------------------------


def _is_calculated_column(column: dict) -> bool:
    """True for the instructor's own totals, which would double count."""
    grading_type = str(((column.get("grading") or {}).get("type") or "")).strip().lower()
    if grading_type == "calculated":
        return True
    if str(column.get("name") or "").strip().lower() in _TOTAL_NAMES:
        return True
    return False


def _blackboard_status(grade: dict | None, earned: float | None) -> str:
    """Excused, graded or ungraded, per the PDF's mapping.

    An exempted column is excused and leaves both earned and possible out of
    the engine's arithmetic. A column with no grade yet is simply ungraded, not
    missing: Blackboard does not tell us whether anything was handed in.
    """
    if grade and grade.get("exempt"):
        return "excused"
    if earned is not None:
        return "graded"
    # Blackboard says NeedsGrading when something was handed in but not marked.
    if grade and grade.get("status") == "NeedsGrading":
        return "submitted"
    return "ungraded"


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_blackboard(conn: sqlite3.Connection, payload: dict) -> NormalizeResult:
    """Turn one Blackboard raw dump into source_courses and assignments rows."""
    result = NormalizeResult()

    for entry in payload.get("courses") or []:
        course = entry.get("course") or {}
        platform_id = entry.get("courseId") or course.get("id")
        if not platform_id:
            continue

        name = course.get("name")
        code = course_code(course.get("courseId"), name)
        term = term_from_course_id(course.get("courseId")) or course.get("termId")

        # Keep the membership and course metadata, not the grade payload: the
        # untouched version of that already lives in the raw dump on disk.
        raw_course = {key: value for key, value in entry.items() if key not in ("columns", "grades")}

        source_course_id = _upsert_source_course(
            conn, "blackboard", platform_id, name, code, term, raw_course
        )
        result.source_courses += 1

        grades_by_column = {
            str(grade.get("columnId")): grade
            for grade in entry.get("grades") or []
            if grade.get("columnId")
        }

        for column in entry.get("columns") or []:
            column_id = column.get("id")
            if not column_id:
                continue
            if _is_calculated_column(column):
                result.dropped += 1
                continue

            grade = grades_by_column.get(str(column_id))
            # Blackboard's v2 grade rows put the number under displayGrade;
            # some builds also mirror it at the top level. Take either.
            display = (grade or {}).get("displayGrade") or {}
            earned = _as_float((grade or {}).get("score"))
            if earned is None:
                earned = _as_float(display.get("score"))
            possible = _as_float((column.get("score") or {}).get("possible"))
            if possible is None:
                possible = _as_float(display.get("possible"))

            # scaleType "Percent" means that score is a percentage of the
            # column, not points: 90 on a 110-point midterm is 99/110, which
            # is the score Hamilton shows. Every other scaleType is points,
            # and "Score" is 48 of the 49 real graded rows.
            if (
                str(display.get("scaleType") or "").strip().lower() == "percent"
                and earned is not None
                and possible is not None
            ):
                earned = round(earned / 100.0 * possible, 4)

            _upsert_assignment(
                conn,
                source_course_id,
                column_id,
                column.get("name"),
                earned,
                possible,
                _blackboard_status(grade, earned),
                (column.get("grading") or {}).get("due"),
                {"column": column, "grade": grade},
            )
            result.assignments += 1

        result.categorised += apply_category_suggestions(conn, source_course_id)

    conn.commit()
    return result


# --- Gradescope -----------------------------------------------------------


def normalize_gradescope(conn: sqlite3.Connection, payload: dict) -> NormalizeResult:
    """Turn one Gradescope raw dump into source_courses and assignments rows.

    The connector already mapped statuses into this vocabulary, so they pass
    straight through.
    """
    result = NormalizeResult()
    payload_term = payload.get("term")

    for course in payload.get("courses") or []:
        platform_id = course.get("id")
        if not platform_id:
            continue

        short_name = course.get("name")
        code = course_code(short_name, course.get("full_name"))
        semester, year = course.get("semester"), course.get("year")
        term = f"{semester} {year}".strip() if semester and year else payload_term

        raw_course = {
            key: value for key, value in course.items() if key != "assignments"
        }

        source_course_id = _upsert_source_course(
            conn,
            "gradescope",
            platform_id,
            course.get("full_name") or short_name,
            code,
            term,
            raw_course,
        )
        result.source_courses += 1

        for assignment in course.get("assignments") or []:
            assignment_id = assignment.get("id")
            if not assignment_id:
                # Gradescope omits the id for an unsubmittable row; without a
                # stable key an upsert would duplicate it on every sync.
                result.dropped += 1
                continue

            _upsert_assignment(
                conn,
                source_course_id,
                assignment_id,
                assignment.get("name"),
                _as_float(assignment.get("score")),
                _as_float(assignment.get("max_points")),
                str(assignment.get("status") or "ungraded"),
                assignment.get("due_date"),
                assignment,
            )
            result.assignments += 1

        result.categorised += apply_category_suggestions(conn, source_course_id)

    conn.commit()
    return result


NORMALIZERS = {
    "blackboard": normalize_blackboard,
    "gradescope": normalize_gradescope,
}
