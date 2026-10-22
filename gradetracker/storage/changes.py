"""What changed since the previous sync (M8).

Snapshot a source's assignments before normalising, diff afterwards, and record
each difference. The point is the notification: "New grade in ECON 166: Problem
Set 4, 18/20" is the feature that makes the app worth leaving running.

First-sync suppression: on the first ever successful sync of a source, every
assignment is new and already graded, so notifying would mean thirty toasts on
day one. Those rows are still recorded as "new" -- the history matters -- but
nothing is announced. "First" means no earlier sync_runs row with status ok for
that source, so a term that genuinely starts empty does not get a free pass
forever.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from gradetracker.storage.db import now_iso

NEW = "new"
SCORE = "score"
STATUS = "status"
# Not a change the platform made: the preferred-platform rule setting aside a
# mirrored copy. Recorded like any other so it lands in Recent changes, which
# is where someone who has never heard of the rule will meet it.
EXCLUDED = "excluded"

# More than this many new grades in one sync becomes a single summary.
SUMMARY_THRESHOLD = 5


@dataclass(frozen=True)
class Change:
    """One recorded difference, with enough context to announce it."""

    assignment_id: int
    kind: str
    old_value: str | None
    new_value: str | None
    name: str
    course_label: str
    is_new_grade: bool = False


def _points(earned, possible) -> str:
    def fmt(value):
        return "-" if value is None else f"{value:g}"

    return f"{fmt(earned)}/{fmt(possible)}"


def is_first_sync(conn: sqlite3.Connection, source: str) -> bool:
    """True when no sync for this source has ever succeeded.

    Called before the current run is recorded, so it describes the past.
    """
    row = conn.execute(
        "SELECT 1 FROM sync_runs WHERE source = ? AND status = 'ok' LIMIT 1",
        (source,),
    ).fetchone()
    return row is None


def snapshot(conn: sqlite3.Connection, source: str) -> dict[int, tuple]:
    """Every assignment of a source, as (earned, possible, status, name)."""
    rows = conn.execute(
        """
        SELECT a.id, a.earned, a.possible, a.status, a.name
        FROM assignments AS a
        JOIN source_courses AS sc ON sc.id = a.source_course_id
        WHERE sc.source = ?
        """,
        (source,),
    ).fetchall()
    return {
        int(row["id"]): (row["earned"], row["possible"], row["status"], row["name"])
        for row in rows
    }


def _labels(conn: sqlite3.Connection, source: str) -> dict[int, str]:
    """A display label per assignment: the course code, or the platform course."""
    rows = conn.execute(
        """
        SELECT a.id, c.code AS course_code, sc.name AS source_name
        FROM assignments AS a
        JOIN source_courses AS sc ON sc.id = a.source_course_id
        LEFT JOIN courses AS c ON c.id = sc.course_id
        WHERE sc.source = ?
        """,
        (source,),
    ).fetchall()
    return {
        int(row["id"]): (row["course_code"] or row["source_name"] or "unlinked course")
        for row in rows
    }


def diff_and_record(
    conn: sqlite3.Connection, source: str, before: dict[int, tuple]
) -> list[Change]:
    """Compare a snapshot with the current state and record every difference."""
    after = snapshot(conn, source)
    labels = _labels(conn, source)
    seen_at = now_iso()
    recorded: list[Change] = []

    for assignment_id, current in after.items():
        earned, possible, status, name = current
        label = labels.get(assignment_id, "unlinked course")
        previous = before.get(assignment_id)

        if previous is None:
            recorded.append(
                Change(
                    assignment_id=assignment_id,
                    kind=NEW,
                    old_value=None,
                    new_value=_points(earned, possible),
                    name=name or "",
                    course_label=label,
                    is_new_grade=status == "graded" and earned is not None,
                )
            )
            continue

        old_earned, old_possible, old_status, _ = previous

        if (old_earned, old_possible) != (earned, possible):
            recorded.append(
                Change(
                    assignment_id=assignment_id,
                    kind=SCORE,
                    old_value=_points(old_earned, old_possible),
                    new_value=_points(earned, possible),
                    name=name or "",
                    course_label=label,
                    # A score appearing, or changing while graded, is news.
                    is_new_grade=status == "graded" and earned is not None,
                )
            )

        if old_status != status:
            recorded.append(
                Change(
                    assignment_id=assignment_id,
                    kind=STATUS,
                    old_value=old_status,
                    new_value=status,
                    name=name or "",
                    course_label=label,
                    is_new_grade=False,
                )
            )

    for change in recorded:
        conn.execute(
            """
            INSERT INTO changes (assignment_id, kind, old_value, new_value, seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (change.assignment_id, change.kind, change.old_value, change.new_value, seen_at),
        )
    conn.commit()
    return recorded


def record_exclusion(
    conn: sqlite3.Connection,
    assignment_id: int,
    twin_name: str | None,
    twin_source: str | None,
) -> None:
    """Record that the rule set one mirrored copy aside, naming the kept one.

    Deliberately not suppressed on a first sync. The suppression above exists
    so a day-one wall of fetched grades stays quiet; this is a decision the
    app took on the user's behalf, and the first sync is exactly when they
    have least reason to expect it.
    """
    conn.execute(
        """
        INSERT INTO changes (assignment_id, kind, old_value, new_value, seen_at)
        VALUES (?, ?, 'counted', ?, ?)
        """,
        (
            assignment_id,
            EXCLUDED,
            f"auto: mirrored copy of {twin_name or 'the same work'}"
            f" ({twin_source or 'the other platform'})",
            now_iso(),
        ),
    )


def notifications_for(changes: list[Change]) -> list[tuple[str, str]]:
    """The (title, body) pairs to show for a set of changes.

    One per new grade, collapsing to a single summary past the threshold so a
    bulk release of grades does not bury the desktop.
    """
    grades = [change for change in changes if change.is_new_grade]
    if not grades:
        return []

    if len(grades) > SUMMARY_THRESHOLD:
        courses = sorted({change.course_label for change in grades})
        return [
            (
                f"{len(grades)} new grades",
                ", ".join(courses) if len(courses) <= 3 else f"{len(courses)} courses",
            )
        ]

    return [
        (
            f"New grade in {change.course_label}",
            f"{change.name}, {change.new_value}",
        )
        for change in grades
    ]


def recent_changes(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """The latest changes, newest first, with course and assignment names."""
    rows = conn.execute(
        """
        SELECT ch.*, a.name AS assignment_name, sc.source, sc.name AS source_name,
               c.code AS course_code, c.id AS course_id
        FROM changes AS ch
        JOIN assignments AS a ON a.id = ch.assignment_id
        JOIN source_courses AS sc ON sc.id = a.source_course_id
        LEFT JOIN courses AS c ON c.id = sc.course_id
        ORDER BY ch.seen_at DESC, ch.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    return [
        {
            "id": int(row["id"]),
            "kind": row["kind"],
            "old_value": row["old_value"],
            "new_value": row["new_value"],
            "seen_at": row["seen_at"],
            "assignment_name": row["assignment_name"],
            "source": row["source"],
            "course_code": row["course_code"] or row["source_name"],
            "course_id": row["course_id"],
        }
        for row in rows
    ]
