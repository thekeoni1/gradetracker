"""Mapping assignments to categories (M6).

Two ways in, and the difference matters. A choice you made by hand is locked, so
no sync and no rules change will move it. An accepted suggestion is not locked,
so adding a better-named category later can re-suggest it.
"""

from __future__ import annotations

import sqlite3

from gradetracker.storage.normalize import apply_category_suggestions, suggest_category


def list_categories(conn: sqlite3.Connection, course_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM categories WHERE course_id = ? ORDER BY id", (course_id,)
    ).fetchall()


def find_category(
    conn: sqlite3.Connection, course_id: int, name: str
) -> sqlite3.Row | None:
    """A category by name, case-insensitively."""
    wanted = (name or "").strip().casefold()
    for row in list_categories(conn, course_id):
        if (row["name"] or "").strip().casefold() == wanted:
            return row
    return None


def list_unmapped(conn: sqlite3.Connection, course_id: int) -> list[dict]:
    """Unmapped assignments with the category each one probably belongs to."""
    categories = list_categories(conn, course_id)
    by_id = {int(row["id"]): row["name"] for row in categories}

    rows = conn.execute(
        """
        SELECT a.id, a.name, a.status, a.earned, a.possible, sc.source
        FROM assignments AS a
        JOIN source_courses AS sc ON sc.id = a.source_course_id
        WHERE sc.course_id = ? AND a.category_id IS NULL AND a.excluded = 0
        ORDER BY a.id
        """,
        (course_id,),
    ).fetchall()

    out = []
    for row in rows:
        suggestion = suggest_category(row["name"], categories)
        out.append(
            {
                "id": int(row["id"]),
                "name": row["name"],
                "source": row["source"],
                "status": row["status"],
                "earned": row["earned"],
                "possible": row["possible"],
                "suggested_category_id": suggestion,
                "suggested_category_name": by_id.get(suggestion) if suggestion else None,
            }
        )
    return out


def count_unmapped_graded(conn: sqlite3.Connection, course_id: int) -> int:
    """Graded assignments with no category.

    These are the ones that silently fail to reach the grade in weighted mode,
    which is the warning the PDF insists on.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM assignments AS a
        JOIN source_courses AS sc ON sc.id = a.source_course_id
        WHERE sc.course_id = ?
          AND a.category_id IS NULL
          AND a.excluded = 0
          AND a.status IN ('graded', 'missing')
        """,
        (course_id,),
    ).fetchone()
    return int(row["n"])


def set_category(
    conn: sqlite3.Connection,
    assignment_id: int,
    category_id: int | None,
    locked: bool = True,
) -> None:
    """Put one assignment in a category. Locked by default: this was a decision."""
    conn.execute(
        "UPDATE assignments SET category_id = ?, category_locked = ? WHERE id = ?",
        (category_id, 1 if locked else 0, assignment_id),
    )
    conn.commit()


def accept_all_suggestions(conn: sqlite3.Connection, course_id: int) -> int:
    """Apply every suggestion for a course without locking any of them.

    Deliberately unlocked: these are guesses, and a later rules change should be
    free to improve on them.
    """
    mapped = 0
    for row in conn.execute(
        "SELECT id FROM source_courses WHERE course_id = ?", (course_id,)
    ).fetchall():
        mapped += apply_category_suggestions(conn, int(row["id"]))
    return mapped
