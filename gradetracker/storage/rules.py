"""Saving and loading confirmed grading rules (M6).

One save path, shared by the CLI now and M7's review form later. Nothing
reaches the engine until it has been confirmed here, because a wrong weight
corrupts every number that follows.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from gradetracker.storage.db import now_iso
from gradetracker.storage.normalize import apply_category_suggestions
from gradetracker.syllabus.schema import (
    CategoryDraft,
    CutoffDraft,
    RulesDraft,
    default_cutoffs,
)

ORIGINS = ("manual", "gradebook", "syllabus")


@dataclass
class SaveResult:
    """What a save changed, so the caller can report it honestly."""

    created: int = 0
    updated: int = 0
    deleted: int = 0
    unlinked: int = 0
    unlocked: int = 0
    categorised: int = 0
    deleted_names: tuple[str, ...] = field(default_factory=tuple)


def _normalise(name: str | None) -> str:
    return (name or "").strip().casefold()


def _rank_weights_json(weights) -> str | None:
    """Rank weights as the stored JSON list, or NULL when there are none."""
    if not weights:
        return None
    return json.dumps([float(weight) for weight in weights])


def _rank_weights_list(raw: str | None) -> list[float] | None:
    """The stored JSON list back as a draft field. One parser, in load.py."""
    from gradetracker.storage.load import parse_rank_weights

    weights = parse_rank_weights(raw)
    return list(weights) if weights else None


def save_rules(
    conn: sqlite3.Connection,
    course_id: int,
    draft: RulesDraft,
    origin: str = "manual",
    confirmed: bool = True,
) -> SaveResult:
    """Write a draft to schemes and categories, in one transaction.

    Categories are matched to existing rows by name, case-insensitively, and
    updated in place. That matters: the row id is what assignments point at, so
    re-confirming the same rules must not orphan every mapping.

    A category missing from the draft is deleted. The foreign key sets
    assignments.category_id to NULL, and category_locked is cleared too, since
    a lock onto a category that no longer exists would silently stop those
    assignments from ever being re-suggested.
    """
    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS}, not {origin!r}")

    result = SaveResult()
    cutoffs = [[cutoff.letter, float(cutoff.min_percent)] for cutoff in draft.cutoffs]

    try:
        conn.execute(
            """
            INSERT INTO schemes
                (course_id, mode, cutoffs, notes, origin, confirmed_at, outdated, bonus_percent)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT (course_id) DO UPDATE SET
                mode = excluded.mode,
                cutoffs = excluded.cutoffs,
                notes = excluded.notes,
                origin = excluded.origin,
                confirmed_at = excluded.confirmed_at,
                outdated = 0,
                bonus_percent = excluded.bonus_percent
            """,
            (
                course_id,
                draft.mode,
                json.dumps(cutoffs),
                draft.notes or "",
                origin,
                now_iso() if confirmed else None,
                float(draft.bonus_percent or 0.0),
            ),
        )

        existing = {
            _normalise(row["name"]): row
            for row in conn.execute(
                "SELECT * FROM categories WHERE course_id = ?", (course_id,)
            ).fetchall()
        }

        kept: set[str] = set()
        for category in draft.categories:
            name = (category.name or "").strip()
            if not name:
                # validate() flags this as blocking; skip rather than write junk.
                continue
            key = _normalise(name)
            kept.add(key)

            ranks = _rank_weights_json(category.rank_weights)

            row = existing.get(key)
            if row is None:
                conn.execute(
                    """
                    INSERT INTO categories
                        (course_id, name, weight, drop_lowest, rank_weights, origin)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        course_id,
                        name,
                        category.weight,
                        max(0, category.drop_lowest),
                        ranks,
                        origin,
                    ),
                )
                result.created += 1
            else:
                conn.execute(
                    """
                    UPDATE categories
                    SET name = ?, weight = ?, drop_lowest = ?, rank_weights = ?, origin = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        category.weight,
                        max(0, category.drop_lowest),
                        ranks,
                        origin,
                        row["id"],
                    ),
                )
                result.updated += 1

        for key, row in existing.items():
            if key in kept:
                continue
            locked = conn.execute(
                "SELECT COUNT(*) AS n FROM assignments WHERE category_id = ? AND category_locked = 1",
                (row["id"],),
            ).fetchone()["n"]
            linked = conn.execute(
                "SELECT COUNT(*) AS n FROM assignments WHERE category_id = ?",
                (row["id"],),
            ).fetchone()["n"]

            # Clear the lock BEFORE the delete: afterwards category_id is NULL
            # and these rows can no longer be found by category.
            conn.execute(
                "UPDATE assignments SET category_locked = 0 WHERE category_id = ?",
                (row["id"],),
            )
            conn.execute("DELETE FROM categories WHERE id = ?", (row["id"],))

            result.deleted += 1
            result.unlinked += int(linked)
            result.unlocked += int(locked)
            result.deleted_names += (row["name"],)

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # Outside the transaction: a new category should pick up its unlocked
    # assignments straight away, and this commits on its own.
    for row in conn.execute(
        "SELECT id FROM source_courses WHERE course_id = ?", (course_id,)
    ).fetchall():
        result.categorised += apply_category_suggestions(conn, int(row["id"]))

    return result


def load_rules(conn: sqlite3.Connection, course_id: int) -> RulesDraft | None:
    """The saved rules as a draft, for pre-filling a form or the CLI.

    Returns a draft when there are categories but no scheme yet, since those
    categories are still worth showing; None only when there is nothing at all.
    """
    scheme = conn.execute(
        "SELECT * FROM schemes WHERE course_id = ?", (course_id,)
    ).fetchone()
    categories = conn.execute(
        "SELECT * FROM categories WHERE course_id = ? ORDER BY id", (course_id,)
    ).fetchall()

    if scheme is None and not categories:
        return None

    drafts = [
        CategoryDraft(
            name=row["name"],
            weight=None if row["weight"] is None else float(row["weight"]),
            drop_lowest=int(row["drop_lowest"] or 0),
            rank_weights=_rank_weights_list(row["rank_weights"]),
        )
        for row in categories
    ]

    if scheme is None:
        return RulesDraft(mode="points", categories=drafts)

    cutoffs = default_cutoffs()
    if scheme["cutoffs"]:
        try:
            parsed = json.loads(scheme["cutoffs"])
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            rows = [
                CutoffDraft(letter=str(entry[0]), min_percent=float(entry[1]))
                for entry in parsed
                if isinstance(entry, (list, tuple)) and len(entry) == 2
            ]
            if rows:
                cutoffs = rows

    bonus = 0.0
    if "bonus_percent" in scheme.keys() and scheme["bonus_percent"] is not None:
        bonus = float(scheme["bonus_percent"])

    return RulesDraft(
        mode=scheme["mode"] or "points",
        categories=drafts,
        cutoffs=cutoffs,
        bonus_percent=bonus,
        notes=scheme["notes"] or "",
    )


def rules_status(conn: sqlite3.Connection, course_id: int) -> str:
    """confirmed | draft | outdated | none.

    outdated wins over confirmed: a newly fetched syllabus marks the rules
    stale until they are confirmed again, and that is the thing worth saying.
    """
    row = conn.execute(
        "SELECT confirmed_at, outdated FROM schemes WHERE course_id = ?", (course_id,)
    ).fetchone()
    if row is None:
        return "none"
    if row["outdated"]:
        return "outdated"
    return "confirmed" if row["confirmed_at"] else "draft"


def rules_summary(conn: sqlite3.Connection, course_id: int) -> str:
    """One line describing the rules, for gt grade and the dashboard."""
    row = conn.execute(
        "SELECT origin, confirmed_at, mode FROM schemes WHERE course_id = ?",
        (course_id,),
    ).fetchone()
    status = rules_status(conn, course_id)

    if row is None:
        return "rules: NONE, using points mode and standard scale"

    origin = row["origin"] or "unknown"
    when = (row["confirmed_at"] or "")[:10]
    if status == "confirmed":
        return f"rules: confirmed ({origin}, {when})"
    if status == "outdated":
        return f"rules: may be outdated ({origin}, confirmed {when}) - re-confirm with gt rules"
    return f"rules: draft, not confirmed ({origin})"


def mark_outdated(conn: sqlite3.Connection, course_id: int) -> None:
    """Flag the rules stale, e.g. when a revised syllabus appears (M6 path 1)."""
    conn.execute("UPDATE schemes SET outdated = 1 WHERE course_id = ?", (course_id,))
    conn.commit()
