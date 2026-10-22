"""Per-source connection state and session-duration tracking (M8).

Two jobs. First, remember whether a source is usable, so the dashboard can show
a reconnect prompt instead of quietly serving stale grades. Second, record how
long each login survives: the PDF wants those numbers as evidence for the
OAuth pitch, and they are only obtainable by measuring.

record_auth_expired returns True only the first time per expiry. A sync that
fails every four hours must prompt once, not six times a day.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from gradetracker.storage.db import now_iso

CONNECTED = "connected"
NEEDS_RECONNECT = "needs_reconnect"
UNKNOWN = "unknown"


def _ensure(conn: sqlite3.Connection, source: str) -> sqlite3.Row:
    conn.execute(
        "INSERT OR IGNORE INTO connections (source, state) VALUES (?, ?)",
        (source, UNKNOWN),
    )
    return conn.execute(
        "SELECT * FROM connections WHERE source = ?", (source,)
    ).fetchone()


def get(conn: sqlite3.Connection, source: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM connections WHERE source = ?", (source,)
    ).fetchone()


def all_states(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {
        row["source"]: row
        for row in conn.execute("SELECT * FROM connections").fetchall()
    }


def record_success(conn: sqlite3.Connection, source: str) -> None:
    """A sync worked. Clears the notified flag so a later expiry can prompt again."""
    _ensure(conn, source)
    conn.execute(
        """
        UPDATE connections
        SET state = ?, last_success_at = ?, notified_at = NULL
        WHERE source = ?
        """,
        (CONNECTED, now_iso(), source),
    )
    conn.commit()


def record_auth_expired(conn: sqlite3.Connection, source: str) -> bool:
    """Flip a source to needs-reconnect. True only the first time per expiry.

    The return value is what gates the desktop notification, so a scheduled
    sync failing every four hours does not nag.
    """
    row = _ensure(conn, source)
    already_prompted = row is not None and row["state"] == NEEDS_RECONNECT and row["notified_at"]

    moment = now_iso()
    conn.execute(
        """
        UPDATE connections
        SET state = ?, last_expired_at = ?, notified_at = COALESCE(notified_at, ?)
        WHERE source = ?
        """,
        (NEEDS_RECONNECT, moment, moment, source),
    )
    _close_open_span(conn, source, moment)
    conn.commit()
    return not already_prompted


def record_login(conn: sqlite3.Connection, source: str) -> None:
    """A fresh login. Opens a session span and clears the reconnect prompt."""
    _ensure(conn, source)
    moment = now_iso()
    conn.execute(
        """
        UPDATE connections
        SET state = ?, last_login_at = ?, notified_at = NULL
        WHERE source = ?
        """,
        (CONNECTED, moment, source),
    )
    # At most one span is ever open; a re-login ends whatever came before.
    _close_open_span(conn, source, moment)
    conn.execute(
        "INSERT INTO session_spans (source, logged_in_at) VALUES (?, ?)",
        (source, moment),
    )
    conn.commit()


def _close_open_span(conn: sqlite3.Connection, source: str, moment: str) -> None:
    conn.execute(
        """
        UPDATE session_spans SET expired_at = ?
        WHERE source = ? AND expired_at IS NULL
        """,
        (moment, source),
    )


def _parse(moment: str | None) -> datetime | None:
    if not moment:
        return None
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def session_spans(
    conn: sqlite3.Connection, source: str, limit: int = 10
) -> list[dict]:
    """The most recent logins and how long each lasted, newest first."""
    rows = conn.execute(
        """
        SELECT * FROM session_spans WHERE source = ?
        ORDER BY logged_in_at DESC, id DESC LIMIT ?
        """,
        (source, limit),
    ).fetchall()

    out = []
    for row in rows:
        started, ended = _parse(row["logged_in_at"]), _parse(row["expired_at"])
        duration = (ended - started) if (started and ended) else None
        out.append(
            {
                "id": int(row["id"]),
                "logged_in_at": row["logged_in_at"],
                "expired_at": row["expired_at"],
                "open": row["expired_at"] is None,
                "duration": duration,
                "hours": None if duration is None else duration.total_seconds() / 3600.0,
            }
        )
    return out


def average_session_duration(conn: sqlite3.Connection, source: str) -> timedelta | None:
    """The mean length of a completed session, or None with nothing to average.

    Open spans are excluded: a session that has not expired yet would drag the
    average down and understate how long logins actually last.
    """
    rows = conn.execute(
        """
        SELECT logged_in_at, expired_at FROM session_spans
        WHERE source = ? AND expired_at IS NOT NULL
        """,
        (source,),
    ).fetchall()

    durations = []
    for row in rows:
        started, ended = _parse(row["logged_in_at"]), _parse(row["expired_at"])
        if started and ended and ended >= started:
            durations.append((ended - started).total_seconds())

    if not durations:
        return None
    return timedelta(seconds=sum(durations) / len(durations))
