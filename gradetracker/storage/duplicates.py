"""The preferred-platform rule: keep one copy of mirrored work (v5).

Every new MATH quiz arrives on both Blackboard and Gradescope, and the
duplicates card only flags a pair once both copies are already counted. A
course with a preference settles each pair as it arrives instead, by excluding
the copy from the other platform.

The rule is deliberately timid. It only ever excludes, never re-includes, and
only touches rows nobody has decided about: an "include" click is recorded as
exclusion_source = 'user' and is left alone from then on.
"""

from __future__ import annotations

import sqlite3

from gradetracker.engine.calculate import mirrored_pairs
from gradetracker.storage import changes as changes_store
from gradetracker.storage import load

PLATFORMS = ("blackboard", "gradescope")


def get_preference(conn: sqlite3.Connection, course_id: int) -> str | None:
    """Which platform this course keeps, or None for "ask me"."""
    row = conn.execute(
        "SELECT preferred_source FROM courses WHERE id = ?", (course_id,)
    ).fetchone()
    if row is None:
        return None
    return row["preferred_source"] or None


def set_preference(
    conn: sqlite3.Connection, course_id: int, source: str | None
) -> None:
    """Store a preference. Anything but a known platform clears it."""
    wanted = (source or "").strip().lower()
    value = wanted if wanted in PLATFORMS else None
    conn.execute(
        "UPDATE courses SET preferred_source = ? WHERE id = ?", (value, course_id)
    )
    conn.commit()


def courses_with_preference(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every course that has chosen a platform, for the end of a sync."""
    return conn.execute(
        """
        SELECT id, code, preferred_source FROM courses
        WHERE preferred_source IS NOT NULL
        ORDER BY id
        """
    ).fetchall()


def _rows(conn: sqlite3.Connection, course_id: int) -> dict[int, dict]:
    """Exclusion state and platform for every assignment in the course."""
    rows = conn.execute(
        """
        SELECT a.id, a.name, a.excluded, a.exclusion_source, sc.source
        FROM assignments AS a
        JOIN source_courses AS sc ON sc.id = a.source_course_id
        WHERE sc.course_id = ?
        """,
        (course_id,),
    ).fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def apply_preference(conn: sqlite3.Connection, course_id: int) -> list[int]:
    """Exclude the non-preferred half of every mirrored pair it may touch.

    Returns the assignment ids it excluded on this run, so a second call
    returns an empty list: the rows it wrote now carry exclusion_source =
    'auto' and no longer qualify.
    """
    preferred = get_preference(conn, course_id)
    if preferred is None:
        return []

    course = load.load_course(conn, course_id)
    if course is None:
        return []

    rows = _rows(conn, course_id)
    excluded: list[int] = []

    for left_id, right_id in mirrored_pairs(course):
        left, right = rows.get(left_id), rows.get(right_id)
        if left is None or right is None:
            continue
        # Both on the preferred platform, or neither: two linked Blackboard
        # sections, say. There is nothing here to choose between.
        if (left["source"] == preferred) == (right["source"] == preferred):
            continue

        keeper, victim = (
            (left, right) if left["source"] == preferred else (right, left)
        )
        # The copy the rule would keep has already been put aside by hand, so
        # excluding its twin as well would leave the work counted nowhere.
        if keeper["excluded"]:
            continue
        if victim["excluded"] or victim["exclusion_source"] is not None:
            continue

        conn.execute(
            "UPDATE assignments SET excluded = 1, exclusion_source = 'auto' WHERE id = ?",
            (victim["id"],),
        )
        # Written here rather than from sync, so setting the preference from
        # the course page or from gt prefer is logged the same way.
        changes_store.record_exclusion(
            conn, int(victim["id"]), keeper.get("name"), keeper.get("source")
        )
        # Kept in step with the database, so a row that is half of two pairs
        # is only counted once.
        victim["excluded"] = 1
        victim["exclusion_source"] = "auto"
        excluded.append(int(victim["id"]))

    if excluded:
        conn.commit()
    return excluded


def default_notice(code: str) -> str:
    """What to say at the moment a course first draws on both platforms.

    Said there rather than at the next sync because that is the moment it can
    be explained: the user has just attached the second platform themselves.
    """
    return (
        f"{code} now draws from Blackboard and Gradescope. When both carry the "
        "same assignment the Gradescope copy is kept and the Blackboard copy "
        "is excluded (marked 'auto' under Excluded). Change this with "
        f"`gt prefer {code} none` or on the course page."
    )


def summarise(code: str, preferred: str, excluded: list[int]) -> str:
    """One line for the sync log: what was excluded and on whose account."""
    other = PLATFORMS[0] if preferred == PLATFORMS[1] else PLATFORMS[1]
    return (
        f"{code}: excluded {len(excluded)} {other.title()} twin(s) "
        f"(prefers {preferred.title()})"
    )
