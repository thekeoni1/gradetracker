"""SQLite connection and schema application (M4).

Plain stdlib sqlite3, no ORM. Only connection plumbing and a couple of
cross-cutting helpers live here; the SQL for a table stays in the module that
owns it.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from gradetracker import config

SCHEMA_VERSION = 6
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def now_iso() -> str:
    """One timestamp format everywhere: UTC, ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database, applying the schema if it is not there yet.

    Foreign keys are off by default in SQLite and must be enabled per
    connection, so every caller goes through here.
    """
    target = Path(path) if path is not None else Path(config.DB_PATH)
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    # WAL survives in the file; a reader never blocks the scheduler's writer.
    conn.execute("PRAGMA journal_mode = WAL")
    apply_schema(conn)
    # executescript commits, so set this after it rather than before.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Add assignments.excluded and schemes.bonus_percent.

    Checked column by column rather than blind, so this is a no-op on a
    database that schema.sql just created with them.
    """
    if "excluded" not in _columns(conn, "assignments"):
        conn.execute(
            "ALTER TABLE assignments ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0"
        )
    if "bonus_percent" not in _columns(conn, "schemes"):
        conn.execute(
            "ALTER TABLE schemes ADD COLUMN bonus_percent REAL NOT NULL DEFAULT 0"
        )


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Add session_spans, for tracking how long a login survives.

    schema.sql creates it too, so this only matters for a database that
    already existed; CREATE TABLE IF NOT EXISTS makes it a safe no-op.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_spans (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            source       TEXT NOT NULL,
            logged_in_at TEXT NOT NULL,
            expired_at   TEXT
        )
        """
    )


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """Add categories.rank_weights, for unequally weighted work of one kind.

    A nullable column, so every existing category keeps the ordinary
    points-over-points rule without being touched.
    """
    if "rank_weights" not in _columns(conn, "categories"):
        conn.execute("ALTER TABLE categories ADD COLUMN rank_weights TEXT")


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """Add courses.preferred_source and assignments.exclusion_source.

    Every exclusion that exists before this migration was made by hand, so it
    is backfilled as 'user': the preferred-platform rule only touches rows
    with no exclusion_source, and must not claim one of those as its own.
    """
    if "preferred_source" not in _columns(conn, "courses"):
        conn.execute(
            "ALTER TABLE courses ADD COLUMN preferred_source TEXT "
            "CHECK (preferred_source IN ('blackboard', 'gradescope'))"
        )
    if "exclusion_source" not in _columns(conn, "assignments"):
        conn.execute(
            "ALTER TABLE assignments ADD COLUMN exclusion_source TEXT "
            "CHECK (exclusion_source IN ('auto', 'user'))"
        )
    conn.execute(
        "UPDATE assignments SET exclusion_source = 'user' "
        "WHERE excluded = 1 AND exclusion_source IS NULL"
    )


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """Widen the changes.kind CHECK, and default an existing both-platform course.

    Two halves of one decision: the preferred-platform rule now defaults to
    Gradescope the first time a course draws on both platforms, and the
    exclusions it makes are written to the changes table so they appear where
    a user already looks. An install that linked both platforms before this
    existed never passes through that moment, so it is backfilled here.
    """
    # SQLite cannot alter a CHECK constraint, so the table is rebuilt. Foreign
    # keys are still off at this point (connect turns them on after the
    # schema), so dropping the old table cannot cascade into assignments.
    conn.executescript(
        """
        CREATE TABLE changes_v6 (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id INTEGER NOT NULL REFERENCES assignments (id) ON DELETE CASCADE,
            kind          TEXT NOT NULL
                          CHECK (kind IN ('new', 'score', 'status', 'excluded')),
            old_value     TEXT,
            new_value     TEXT,
            seen_at       TEXT NOT NULL
        );
        INSERT INTO changes_v6 (id, assignment_id, kind, old_value, new_value, seen_at)
            SELECT id, assignment_id, kind, old_value, new_value, seen_at FROM changes;
        DROP TABLE changes;
        ALTER TABLE changes_v6 RENAME TO changes;
        """
    )
    # schema.sql already ran this connection, so the index went with the old
    # table and has to be put back by hand.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_changes_assignment "
        "ON changes (assignment_id, seen_at)"
    )

    conn.execute(
        """
        UPDATE courses SET preferred_source = 'gradescope'
        WHERE preferred_source IS NULL
          AND EXISTS (SELECT 1 FROM source_courses sc
                      WHERE sc.course_id = courses.id AND sc.source = 'blackboard')
          AND EXISTS (SELECT 1 FROM source_courses sc
                      WHERE sc.course_id = courses.id AND sc.source = 'gradescope')
        """
    )


# Applied in order, each only when the database is older than its version.
MIGRATIONS: list[tuple[int, object]] = [
    (2, _migrate_v2),
    (3, _migrate_v3),
    (4, _migrate_v4),
    (5, _migrate_v5),
    (6, _migrate_v6),
]


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create anything missing, then bring an older database up to date.

    Each applied version is recorded, not just the latest, so schema_version
    reads as a history of what this database has actually been through.
    """
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    current = schema_version(conn) or 0
    if current == 0:
        # schema.sql just created the baseline shape.
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (1)")

    for version, migrate in MIGRATIONS:
        if version > current:
            migrate(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (version,)
            )

    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
    )
    conn.commit()


def schema_version(conn: sqlite3.Connection) -> int | None:
    """The highest applied schema version, or None on an empty database."""
    row = conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    return row["version"] if row else None


@contextmanager
def connection(path: Path | str | None = None):
    """A connection that commits on clean exit and always closes.

    sqlite3's own context manager commits but leaves the handle open, which
    leaks file locks on Windows.
    """
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_sync_run(
    conn: sqlite3.Connection,
    source: str,
    started_at: str,
    status: str,
    finished_at: str | None = None,
    raw_path: str | None = None,
    message: str | None = None,
) -> int:
    """One row per sync attempt, whatever its outcome. Returns the row id."""
    cursor = conn.execute(
        """
        INSERT INTO sync_runs (source, started_at, finished_at, status, raw_path, message)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (source, started_at, finished_at or now_iso(), status, raw_path, message),
    )
    conn.commit()
    return int(cursor.lastrowid)


def last_sync_run(conn: sqlite3.Connection, source: str) -> sqlite3.Row | None:
    """The most recent attempt for a source. M8 uses it to skip piled-up runs."""
    return conn.execute(
        """
        SELECT * FROM sync_runs
        WHERE source = ?
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        (source,),
    ).fetchone()
