"""Database rows -> engine models (M5).

This is the only place that knows both SQL and the engine's dataclasses, which
is what keeps gradetracker/engine free of any database import.
"""

from __future__ import annotations

import json
import math
import sqlite3

from gradetracker.engine.models import (
    DEFAULT_CUTOFFS,
    Assignment,
    Category,
    Course,
    Scheme,
)


def list_courses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every logical course, with how much is attached to it."""
    return conn.execute(
        """
        SELECT
            c.*,
            (SELECT COUNT(*) FROM source_courses sc WHERE sc.course_id = c.id)
                AS source_course_count,
            (SELECT COUNT(*) FROM assignments a
             JOIN source_courses sc2 ON sc2.id = a.source_course_id
             WHERE sc2.course_id = c.id) AS assignment_count
        FROM courses AS c
        ORDER BY c.term DESC, c.code
        """
    ).fetchall()


def find_course_id(conn: sqlite3.Connection, code: str) -> int | None:
    """A course id from a code the user typed, matched loosely."""
    from gradetracker.storage.link import code_key

    wanted = code_key(code)
    if not wanted:
        return None
    for row in conn.execute("SELECT id, code FROM courses ORDER BY term DESC").fetchall():
        if code_key(row["code"]) == wanted:
            return int(row["id"])
    return None


def _parse_cutoffs(raw: str | None) -> tuple[tuple[str, float], ...]:
    """Cutoffs come out of the database as JSON, in either shape.

    Accepts {"A": 93, ...} or [["A", 93], ...]. Falls back to the standard
    scale, because a grade with no scale at all cannot be shown.
    """
    if not raw:
        return DEFAULT_CUTOFFS
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return DEFAULT_CUTOFFS

    pairs: list[tuple[str, float]] = []
    if isinstance(parsed, dict):
        items = parsed.items()
    elif isinstance(parsed, list):
        # A stored [93, 90] of bare numbers has no len()-2 pairs to unpack, and
        # calling len() on the ints would raise rather than fall back.
        items = [
            (entry[0], entry[1])
            for entry in parsed
            if isinstance(entry, (list, tuple)) and len(entry) == 2
        ]
    else:
        return DEFAULT_CUTOFFS

    for letter, minimum in items:
        try:
            pairs.append((str(letter), float(minimum)))
        except (TypeError, ValueError):
            continue

    if not pairs:
        return DEFAULT_CUTOFFS
    return tuple(sorted(pairs, key=lambda pair: pair[1], reverse=True))


def load_scheme(conn: sqlite3.Connection, course_id: int) -> Scheme | None:
    """The confirmed rules, or None when the course has none yet."""
    row = conn.execute(
        "SELECT * FROM schemes WHERE course_id = ?", (course_id,)
    ).fetchone()
    if row is None:
        return None

    notes = row["notes"] or ""
    bonus = 0.0
    if "bonus_percent" in row.keys() and row["bonus_percent"] is not None:
        bonus = float(row["bonus_percent"])

    return Scheme(
        mode=row["mode"] or "points",
        cutoffs=_parse_cutoffs(row["cutoffs"]),
        bonus_percent=bonus,
        notes=notes,
    )


def parse_rank_weights(raw: str | None) -> tuple[float, ...] | None:
    """A category's rank weights from the stored JSON list, or None.

    Junk is treated as no ranking rather than as an error: the ordinary rule
    gives a number that is merely less specific, while refusing to load the
    course would take the whole grade away.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, (list, tuple)):
        return None
    weights = []
    for entry in parsed:
        try:
            weight = float(entry)
        except (TypeError, ValueError):
            return None
        # JSON has a NaN literal and json.dumps writes one, so a row saved
        # before validate() refused these, or edited by hand, can still hold
        # one. A NaN here would turn the whole course grade into "nan%".
        if not math.isfinite(weight):
            return None
        weights.append(weight)
    return tuple(weights) or None


def load_categories(conn: sqlite3.Connection, course_id: int) -> tuple[Category, ...]:
    rows = conn.execute(
        "SELECT * FROM categories WHERE course_id = ? ORDER BY id", (course_id,)
    ).fetchall()
    return tuple(
        Category(
            id=int(row["id"]),
            name=row["name"],
            weight=None if row["weight"] is None else float(row["weight"]),
            drop_lowest=int(row["drop_lowest"] or 0),
            rank_weights=parse_rank_weights(row["rank_weights"]),
        )
        for row in rows
    )


def load_assignments(conn: sqlite3.Connection, course_id: int) -> tuple[Assignment, ...]:
    """Every assignment from every platform course linked to this course."""
    rows = conn.execute(
        """
        SELECT a.*
        FROM assignments AS a
        JOIN source_courses AS sc ON sc.id = a.source_course_id
        WHERE sc.course_id = ?
        ORDER BY a.id
        """,
        (course_id,),
    ).fetchall()
    return tuple(
        Assignment(
            id=int(row["id"]),
            name=row["name"] or "",
            category_id=None if row["category_id"] is None else int(row["category_id"]),
            earned=None if row["earned"] is None else float(row["earned"]),
            possible=None if row["possible"] is None else float(row["possible"]),
            status=row["status"],
            extra_credit=bool(row["extra_credit"]),
            missing_as_zero=bool(row["missing_as_zero"]),
            source_course_id=int(row["source_course_id"]),
            excluded=bool(row["excluded"]),
        )
        for row in rows
    )


def load_course(conn: sqlite3.Connection, course_id: int) -> Course | None:
    """Assemble one course for the engine, or None if the id is unknown."""
    row = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if row is None:
        return None

    return Course(
        id=int(row["id"]),
        code=row["code"],
        name=row["name"],
        categories=load_categories(conn, course_id),
        assignments=load_assignments(conn, course_id),
        scheme=load_scheme(conn, course_id),
    )


def load_all_courses(conn: sqlite3.Connection) -> list[Course]:
    """Every course, assembled. Used by the dashboard and gt grade."""
    courses = []
    for row in list_courses(conn):
        course = load_course(conn, int(row["id"]))
        if course is not None:
            courses.append(course)
    return courses
