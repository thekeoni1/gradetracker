"""The queries the views need, so the views stay thin (M7).

Nothing here renders; nothing in the views writes SQL.

Connection state is derived from facts already on disk -- the login stamp, the
stored credentials, the last sync_runs row -- and never by authenticating. A
page load must not open a browser window or hit the network.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from gradetracker.engine.calculate import (
    current_grade,
    projected_grade,
    rank_weight_labels,
)
from gradetracker.storage import connections as connections_store
from gradetracker.storage import duplicates as duplicates_store
from gradetracker.storage import link as linking
from gradetracker.storage import load, mapping
from gradetracker.storage import rules as rules_store
from gradetracker.redact import scrub
from gradetracker.storage.db import now_iso

# The PDF: anything not refreshed in more than a day gets a warning.
STALE_AFTER = timedelta(hours=24)

SOURCES = ("blackboard", "gradescope")

# Flags a row action may toggle. Whitelisted because the name reaches SQL.
TOGGLEABLE = {"missing_as_zero", "excluded", "extra_credit", "category_locked"}


def _parse(moment: str | None) -> datetime | None:
    if not moment:
        return None
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age(moment: str | None, now: datetime | None = None) -> timedelta | None:
    parsed = _parse(moment)
    if parsed is None:
        return None
    return (now or datetime.now(timezone.utc)) - parsed


def humanise(delta: timedelta | None) -> str:
    """"3 hours ago", for a dashboard that has to be skimmable."""
    if delta is None:
        return "never"
    seconds = int(delta.total_seconds())
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} minutes ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hours ago"
    return f"{hours // 24} days ago"


# --- sync history ---------------------------------------------------------


def last_sync_runs(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """The most recent attempt per source, whatever its outcome."""
    out: dict[str, sqlite3.Row] = {}
    for source in SOURCES:
        row = conn.execute(
            """
            SELECT * FROM sync_runs WHERE source = ?
            ORDER BY started_at DESC, id DESC LIMIT 1
            """,
            (source,),
        ).fetchone()
        if row is not None:
            out[source] = row
    return out


def last_successful_runs(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    out: dict[str, sqlite3.Row] = {}
    for source in SOURCES:
        row = conn.execute(
            """
            SELECT * FROM sync_runs WHERE source = ? AND status = 'ok'
            ORDER BY started_at DESC, id DESC LIMIT 1
            """,
            (source,),
        ).fetchone()
        if row is not None:
            out[source] = row
    return out


def course_sources(conn: sqlite3.Connection, course_id: int) -> list[dict]:
    """Which platforms feed a course, and when each was last seen."""
    rows = conn.execute(
        """
        SELECT source, platform_id, name, last_seen_at
        FROM source_courses WHERE course_id = ? ORDER BY source
        """,
        (course_id,),
    ).fetchall()
    return [
        {
            "source": row["source"],
            "name": row["name"],
            "last_seen_at": row["last_seen_at"],
            "ago": humanise(_age(row["last_seen_at"])),
        }
        for row in rows
    ]


def collapse_sources(sources: list[dict]) -> list[dict]:
    """One entry per platform, carrying its oldest sync time.

    A course can be fed by two courses on the same platform -- CPSCI 101 has a
    lecture and a lab section on Blackboard -- and two badges reading
    "blackboard 47 minutes ago" tell the reader nothing the first one did not.
    The oldest is the one worth showing: it is the half that has gone stale.
    """
    out: dict[str, dict] = {}
    for entry in sources:
        kept = out.get(entry["source"])
        if kept is None or _older(entry["last_seen_at"], kept["last_seen_at"]):
            out[entry["source"]] = entry
    return [out[source] for source in sorted(out)]


def _older(moment: str | None, than: str | None) -> bool:
    """True when `moment` is the older of the two. Never seen counts as oldest."""
    if moment is None:
        return True
    if than is None:
        return False
    return moment < than


# --- dashboard ------------------------------------------------------------


def _reconnected_since(
    conn: sqlite3.Connection, source: str, run: sqlite3.Row | None
) -> bool:
    """True when a login was recorded after the run that failed.

    A reconnect fixes the connection and writes connections.last_login_at, but
    the failed sync_runs row stays the latest attempt until the next sync. A
    display that reads only sync_runs keeps asking for a reconnect that has
    already happened, so both facts have to be read together.
    """
    if run is None:
        return False
    row = connections_store.get(conn, source)
    if row is None:
        return False
    login_at = _parse(row["last_login_at"])
    started_at = _parse(run["started_at"])
    if login_at is None or started_at is None:
        return False
    return login_at > started_at


def course_card(conn: sqlite3.Connection, course_id: int) -> dict | None:
    """Everything one dashboard card shows, grade included."""
    course = load.load_course(conn, course_id)
    if course is None:
        return None

    current = current_grade(course)
    projected = projected_grade(course)
    sources = course_sources(conn, course_id)

    freshest = max(
        (source["last_seen_at"] for source in sources if source["last_seen_at"]),
        default=None,
    )
    age = _age(freshest)
    stale = age is not None and age > STALE_AFTER

    runs = last_sync_runs(conn)
    needs_reconnect = sorted(
        source
        for source in {entry["source"] for entry in sources}
        if source in runs
        and runs[source]["status"] == "auth_expired"
        and not _reconnected_since(conn, source, runs[source])
    )

    return {
        "course": course,
        "current": current,
        "projected": projected,
        "rules_status": rules_store.rules_status(conn, course_id),
        "rules_summary": rules_store.rules_summary(conn, course_id),
        "unmapped_graded": mapping.count_unmapped_graded(conn, course_id),
        # Collapsed for display only: `freshest` above still reads every
        # platform course, so staleness is judged on all of them.
        "sources": collapse_sources(sources),
        "last_seen_at": freshest,
        "last_seen_ago": humanise(age),
        "stale": stale,
        "needs_reconnect": needs_reconnect,
    }


def dashboard(conn: sqlite3.Connection) -> list[dict]:
    """One card per course, newest term first."""
    cards = []
    for row in load.list_courses(conn):
        card = course_card(conn, int(row["id"]))
        if card is not None:
            cards.append(card)
    return cards


# --- course page ----------------------------------------------------------


def assignment_rows(conn: sqlite3.Connection, course_id: int) -> list[dict]:
    """Every assignment with the display facts the course page needs."""
    rows = conn.execute(
        """
        SELECT a.*, sc.source, sc.platform_id AS source_platform_id
        FROM assignments AS a
        JOIN source_courses AS sc ON sc.id = a.source_course_id
        WHERE sc.course_id = ?
        ORDER BY a.category_id IS NULL, a.category_id, a.due_at IS NULL, a.due_at, a.id
        """,
        (course_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _group_stats(items: list[dict], duplicate_ids: set[int], unmapped: bool = False) -> dict:
    """What one collapsed group says about itself, and whether to open it.

    A group opens on its own only when it holds something to act on, so a
    course with a hundred settled rows stays quiet.
    """
    graded = sum(1 for item in items if item["status"] == "graded")
    pending = sum(
        1 for item in items if item["status"] in ("ungraded", "submitted")
    )
    attention = sum(
        1
        for item in items
        if item["id"] in duplicate_ids
        or (item["status"] == "missing" and not item["missing_as_zero"])
        or (unmapped and item["status"] == "graded")
    )
    return {
        "total": len(items),
        "graded": graded,
        "pending": pending,
        "attention": attention,
        "open": attention > 0,
    }


def course_detail(conn: sqlite3.Connection, course_id: int) -> dict | None:
    """The course page's whole payload: grade, bars, and grouped assignments."""
    card = course_card(conn, course_id)
    if card is None:
        return None

    course = card["course"]
    categories = {category.id: category for category in course.categories}
    results = {result.category_id: result for result in card["current"].categories}
    dropped = {
        assignment_id
        for result in card["current"].categories
        for assignment_id in result.dropped_ids
    }
    duplicates = {
        assignment_id for pair in card["current"].duplicate_pairs for assignment_id in pair
    }

    rows = assignment_rows(conn, course_id)
    grouped: list[dict] = []
    # The weight a ranked item actually received, keyed by assignment id, so
    # the table can show "(23%)" next to the midterm that earned it.
    rank_labels: dict[int, str] = {}
    for category_id, category in categories.items():
        result = results.get(category_id)
        if result is not None:
            rank_labels.update(rank_weight_labels(category, result))
        # Excluded rows are collected in their own group, so a category shows
        # only what is actually counting towards it.
        items = [
            row
            for row in rows
            if row["category_id"] == category_id and not row["excluded"]
        ]
        grouped.append(
            {
                "category": category,
                "result": result,
                "assignments": items,
                "stats": _group_stats(items, duplicates),
            }
        )
    loose = [
        row
        for row in rows
        if row["category_id"] not in categories and not row["excluded"]
    ]
    excluded = [row for row in rows if row["excluded"]]

    # One row per detected pair, so the page can offer "keep this one" without
    # the user working out which two of a hundred rows are the twins.
    by_id = {int(row["id"]): row for row in rows}
    duplicate_rows = []
    for left_id, right_id in card["current"].duplicate_pairs:
        left, right = by_id.get(left_id), by_id.get(right_id)
        if left is None or right is None:
            continue
        duplicate_rows.append(
            {"pair": f"{left_id}-{right_id}", "left": left, "right": right}
        )

    return {
        **card,
        "groups": grouped,
        "uncategorised": loose,
        "uncategorised_stats": _group_stats(loose, duplicates, unmapped=True),
        "excluded_items": excluded,
        "excluded_stats": _group_stats(excluded, duplicates),
        "dropped_ids": dropped,
        "duplicate_ids": duplicates,
        "duplicate_rows": duplicate_rows,
        "preferred_source": duplicates_store.get_preference(conn, course_id),
        "categories": list(categories.values()),
        "rank_labels": rank_labels,
    }


def unscored_assignments(conn: sqlite3.Connection, course_id: int) -> list[dict]:
    """The items a what-if can move: no real score yet.

    A missing item counted as a zero is a real score, so it only appears when
    missing_as_zero is off.
    """
    rows = conn.execute(
        """
        SELECT a.*, sc.source
        FROM assignments AS a
        JOIN source_courses AS sc ON sc.id = a.source_course_id
        WHERE sc.course_id = ?
          AND a.excluded = 0
          AND a.status != 'excused'
          AND a.possible > 0
          AND (
            a.status IN ('ungraded', 'submitted')
            OR (a.status = 'missing' AND a.missing_as_zero = 0)
          )
        ORDER BY a.due_at IS NULL, a.due_at, a.id
        """,
        (course_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def graded_assignments(conn: sqlite3.Connection, course_id: int) -> list[dict]:
    """The items a what-if can re-score: a real mark already in.

    For the course that lets you rewrite a paper, or a regrade request worth
    arguing: the question is not what an unknown might be, it is what the
    grade becomes if a known score changes. The engine applies an override to
    any item whatever its status, so this only had to be a list of rows.

    No excused clause, unlike unscored_assignments: an excused item is not
    graded, so status already rules it out.
    """
    rows = conn.execute(
        """
        SELECT a.*, sc.source
        FROM assignments AS a
        JOIN source_courses AS sc ON sc.id = a.source_course_id
        WHERE sc.course_id = ?
          AND a.excluded = 0
          AND a.possible > 0
          AND a.status = 'graded'
          AND a.earned IS NOT NULL
        ORDER BY a.due_at IS NULL, a.due_at, a.id
        """,
        (course_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# --- row actions ----------------------------------------------------------


def set_flag(conn: sqlite3.Connection, assignment_id: int, field: str, value: bool) -> None:
    """Toggle one whitelisted boolean column on an assignment."""
    if field not in TOGGLEABLE:
        raise ValueError(f"{field!r} is not a toggleable column")
    if field == "excluded":
        # Both directions: once a person has decided about this row, the
        # preferred-platform rule leaves it alone for good.
        conn.execute(
            "UPDATE assignments SET excluded = ?, exclusion_source = 'user' WHERE id = ?",
            (1 if value else 0, assignment_id),
        )
        conn.commit()
        return
    conn.execute(
        f"UPDATE assignments SET {field} = ? WHERE id = ?",
        (1 if value else 0, assignment_id),
    )
    conn.commit()


def assignment_course_id(conn: sqlite3.Connection, assignment_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT sc.course_id FROM assignments AS a
        JOIN source_courses AS sc ON sc.id = a.source_course_id
        WHERE a.id = ?
        """,
        (assignment_id,),
    ).fetchone()
    return None if row is None or row["course_id"] is None else int(row["course_id"])


# --- manual assignments ---------------------------------------------------


def manual_source_course(conn: sqlite3.Connection, course_id: int) -> int:
    """The synthetic source course that holds hand-added items.

    Created lazily, one per course, so manual rows hang off the same tables as
    platform rows and every query keeps working unchanged.
    """
    platform_id = str(course_id)
    row = conn.execute(
        "SELECT id FROM source_courses WHERE source = 'manual' AND platform_id = ?",
        (platform_id,),
    ).fetchone()
    if row is not None:
        return int(row["id"])

    course = conn.execute(
        "SELECT code, name, term FROM courses WHERE id = ?", (course_id,)
    ).fetchone()
    cursor = conn.execute(
        """
        INSERT INTO source_courses
            (source, platform_id, name, code, term, course_id, raw, last_seen_at)
        VALUES ('manual', ?, ?, ?, ?, ?, '{}', ?)
        """,
        (
            platform_id,
            "Added by hand",
            course["code"] if course else None,
            course["term"] if course else None,
            course_id,
            now_iso(),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def add_manual_assignment(
    conn: sqlite3.Connection,
    course_id: int,
    name: str,
    earned: float | None,
    possible: float | None,
    status: str = "graded",
    category_id: int | None = None,
    extra_credit: bool = False,
) -> int:
    """Add an item that will never appear on either platform."""
    source_course_id = manual_source_course(conn, course_id)
    cursor = conn.execute(
        """
        INSERT INTO assignments (
            source_course_id, platform_id, name, category_id, category_locked,
            earned, possible, status, extra_credit, manual, raw, last_seen_at
        )
        VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, 1, '{}', ?)
        """,
        (
            source_course_id,
            f"manual-{uuid.uuid4()}",
            name,
            category_id,
            earned,
            possible,
            status,
            1 if extra_credit else 0,
            now_iso(),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def delete_manual_assignment(conn: sqlite3.Connection, assignment_id: int) -> bool:
    """Delete a hand-added row. Platform rows are left alone."""
    cursor = conn.execute(
        "DELETE FROM assignments WHERE id = ? AND manual = 1", (assignment_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


# --- scenarios ------------------------------------------------------------


def list_scenarios(conn: sqlite3.Connection, course_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM scenarios WHERE course_id = ? ORDER BY name", (course_id,)
    ).fetchall()
    out = []
    for row in rows:
        try:
            overrides = json.loads(row["overrides"] or "{}")
        except (TypeError, ValueError):
            overrides = {}
        out.append(
            {
                "id": int(row["id"]),
                "name": row["name"],
                "overrides": {int(k): float(v) for k, v in overrides.items()},
                "created_at": row["created_at"],
            }
        )
    return out


def save_scenario(
    conn: sqlite3.Connection, course_id: int, name: str, overrides: dict[int, float]
) -> int:
    """Store a named set of hypothetical scores, replacing one of the same name."""
    payload = json.dumps({str(k): float(v) for k, v in overrides.items()})
    conn.execute(
        """
        INSERT INTO scenarios (course_id, name, overrides, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (course_id, name) DO UPDATE SET
            overrides = excluded.overrides,
            created_at = excluded.created_at
        """,
        (course_id, name, payload, now_iso()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM scenarios WHERE course_id = ? AND name = ?", (course_id, name)
    ).fetchone()
    return int(row["id"])


def delete_scenario(conn: sqlite3.Connection, scenario_id: int) -> bool:
    cursor = conn.execute("DELETE FROM scenarios WHERE id = ?", (scenario_id,))
    conn.commit()
    return cursor.rowcount > 0


# --- connections ----------------------------------------------------------


def connection_states(conn: sqlite3.Connection) -> list[dict]:
    """Per-source state, from stored facts only. Never authenticates."""
    from gradetracker import config
    from gradetracker.auth import launcher as bb_launcher
    from gradetracker.auth import gradescope_auth

    runs = last_sync_runs(conn)
    successes = last_successful_runs(conn)
    states = []

    for source in SOURCES:
        run = runs.get(source)
        success = successes.get(source)

        if source == "blackboard":
            configured = bool(config.BB_BASE_URL)
            login_at = bb_launcher.read_login_timestamp(config.BB_PROFILE_DIR)
            identity = config.BB_BASE_URL or None
        else:
            credentials = gradescope_auth.load_credentials()
            configured = credentials is not None
            login_at = None
            identity = credentials[0] if credentials else None

        expired = run is not None and run["status"] == "auth_expired"
        reconnected = expired and _reconnected_since(conn, source, run)

        if not configured:
            state = "not configured"
        elif expired and not reconnected:
            state = "needs reconnect"
        elif success is not None or reconnected:
            # A login after the failing run counts as connected even with no
            # successful sync behind it yet: the next sync will have one.
            state = "connected"
        else:
            state = "never synced"

        message = scrub(run["message"]) if run is not None else None
        if reconnected:
            # The old failure text is no longer what happened last, and leaving
            # it under a "connected" badge reads as a contradiction.
            login_row = connections_store.get(conn, source)
            when = humanise(_age(login_row["last_login_at"] if login_row else None))
            message = f"Logged in again {when}; grades refresh on the next sync."

        states.append(
            {
                "source": source,
                "state": state,
                "identity": identity,
                "configured": configured,
                "last_success_at": success["started_at"] if success else None,
                "last_success_ago": humanise(_age(success["started_at"] if success else None)),
                "last_run_status": run["status"] if run is not None else None,
                "last_run_message": message,
                # Kept so a parsing failure can be chased in the raw dump.
                "last_run_raw_path": run["raw_path"] if run is not None else None,
                "last_error": (
                    scrub(run["message"])
                    if run is not None and run["status"] == "error"
                    else None
                ),
                "last_login_at": login_at,
                "session_age": humanise(_age(login_at)) if login_at else None,
            }
        )
    return states


# --- links ----------------------------------------------------------------


def link_rows(conn: sqlite3.Connection) -> list[dict]:
    """Source courses with their logical course and a suggestion if unlinked."""
    out = []
    for row in linking.list_source_courses(conn):
        suggestion = None
        if row["course_id"] is None:
            match = linking.suggest_link(conn, row)
            suggestion = match["code"] if match is not None else row["code"]
        out.append(
            {
                "id": int(row["id"]),
                "source": row["source"],
                "name": row["name"],
                "code": row["code"],
                "term": row["term"],
                "course_id": row["course_id"],
                "course_code": row["course_code"],
                "suggestion": suggestion,
            }
        )
    return out


def courses_for_select(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, code, term FROM courses ORDER BY code").fetchall()
