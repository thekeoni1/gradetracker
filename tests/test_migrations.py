"""Schema v2 migration, the excluded flag, and gt exclude (M5 fixes)."""

from __future__ import annotations

import sqlite3
import subprocess

import pytest
from typer.testing import CliRunner

from gradetracker import config
from gradetracker.cli import app
from gradetracker.engine.calculate import current_grade
from gradetracker.storage import db, link as linking, load
from gradetracker.storage.normalize import normalize_blackboard

from tests.test_normalize import bb_payload

runner = CliRunner()


@pytest.fixture
def v1_database(tmp_path):
    """A database built from the schema as it stood at the M4 commit."""
    schema = subprocess.run(
        ["git", "show", "75fd9c4:gradetracker/storage/schema.sql"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    conn.execute(
        """
        INSERT INTO source_courses (source, platform_id, name, code, term, last_seen_at)
        VALUES ('blackboard', '_1_1', 'ECON 166', 'ECON 166', 'Fall 2026', 'now')
        """
    )
    conn.execute(
        """
        INSERT INTO assignments
            (source_course_id, platform_id, name, status, earned, possible, last_seen_at)
        VALUES (1, 'c1', 'Problem Set 1', 'graded', 18, 20, 'now')
        """
    )
    conn.commit()
    conn.close()
    return path


def test_a_v1_database_reports_version_one(v1_database):
    conn = sqlite3.connect(str(v1_database))
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"] == 1
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(assignments)")}
        assert "excluded" not in columns
    finally:
        conn.close()


def test_connecting_migrates_to_the_current_version(v1_database):
    conn = db.connect(v1_database)
    try:
        assert db.schema_version(conn) == db.SCHEMA_VERSION
        assert "excluded" in db._columns(conn, "assignments")
        assert "bonus_percent" in db._columns(conn, "schemes")
    finally:
        conn.close()


def test_migration_preserves_existing_rows(v1_database):
    conn = db.connect(v1_database)
    try:
        row = conn.execute("SELECT * FROM assignments").fetchone()
        assert row["name"] == "Problem Set 1"
        assert row["earned"] == 18.0
        assert row["excluded"] == 0  # the new column defaults sensibly
    finally:
        conn.close()


def test_migration_is_idempotent(v1_database):
    db.connect(v1_database).close()
    conn = db.connect(v1_database)
    try:
        assert db.schema_version(conn) == db.SCHEMA_VERSION
        versions = [row["version"] for row in conn.execute("SELECT version FROM schema_version")]
        # Every applied version is recorded, and none is applied twice.
        assert sorted(versions) == list(range(1, db.SCHEMA_VERSION + 1))
    finally:
        conn.close()


def test_a_fresh_database_is_at_the_current_version(tmp_path):
    conn = db.connect(tmp_path / "new.db")
    try:
        assert db.schema_version(conn) == db.SCHEMA_VERSION
        assert "excluded" in db._columns(conn, "assignments")
        assert "session_spans" in {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def test_migration_adds_rank_weights_and_leaves_categories_unranked(v1_database):
    """v4: an existing category keeps the ordinary points-over-points rule."""
    conn = db.connect(v1_database)
    try:
        assert "rank_weights" in db._columns(conn, "categories")
        conn.execute(
            "INSERT INTO courses (code, created_at) VALUES ('ECON 166', 'now')"
        )
        course_id = int(conn.execute("SELECT id FROM courses").fetchone()["id"])
        conn.execute(
            "INSERT INTO categories (course_id, name, weight) VALUES (?, 'Midterms', 40)",
            (course_id,),
        )
        conn.commit()

        row = conn.execute("SELECT rank_weights FROM categories").fetchone()
        categories = load.load_categories(conn, course_id)

        assert row["rank_weights"] is None
        assert categories[0].rank_weights is None
    finally:
        conn.close()


def test_rank_weights_round_trip_through_the_loader(tmp_path):
    conn = db.connect(tmp_path / "g.db")
    try:
        normalize_blackboard(conn, bb_payload())
        linking.link_auto(conn)
        course_id = int(conn.execute("SELECT id FROM courses LIMIT 1").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO categories (course_id, name, weight, rank_weights)
            VALUES (?, 'Midterms', 40, '[23, 17]')
            """,
            (course_id,),
        )
        conn.commit()

        categories = load.load_categories(conn, course_id)

        assert categories[0].rank_weights == (23.0, 17.0)
    finally:
        conn.close()


def test_bonus_percent_round_trips_through_the_loader(tmp_path):
    conn = db.connect(tmp_path / "g.db")
    try:
        normalize_blackboard(conn, bb_payload())
        linking.link_auto(conn)
        course_id = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()["id"]
        conn.execute(
            """
            INSERT INTO schemes (course_id, mode, cutoffs, origin, bonus_percent)
            VALUES (?, 'points', NULL, 'manual', 2.5)
            """,
            (course_id,),
        )
        conn.commit()

        scheme = load.load_scheme(conn, course_id)

        assert scheme.bonus_percent == 2.5
    finally:
        conn.close()


# --- excluded through the stack -------------------------------------------


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "grades.db")
    conn = db.connect(tmp_path / "grades.db")
    normalize_blackboard(conn, bb_payload())
    linking.link_auto(conn)
    conn.close()
    return tmp_path / "grades.db"


def test_the_normalizer_never_touches_excluded(seeded):
    """A hand-set exclusion must survive a re-sync, like category_locked."""
    conn = db.connect(seeded)
    try:
        target = conn.execute(
            "SELECT id FROM assignments WHERE name = 'Problem Set 1'"
        ).fetchone()["id"]
        conn.execute("UPDATE assignments SET excluded = 1 WHERE id = ?", (target,))
        conn.commit()

        normalize_blackboard(conn, bb_payload())

        row = conn.execute("SELECT excluded FROM assignments WHERE id = ?", (target,)).fetchone()
        assert row["excluded"] == 1
    finally:
        conn.close()


def test_the_loader_passes_excluded_and_source_course_id_through(seeded):
    conn = db.connect(seeded)
    try:
        target = conn.execute(
            "SELECT id FROM assignments WHERE name = 'Problem Set 1'"
        ).fetchone()["id"]
        conn.execute("UPDATE assignments SET excluded = 1 WHERE id = ?", (target,))
        conn.commit()
        course_id = conn.execute("SELECT id FROM courses WHERE code = 'ECON 166'").fetchone()["id"]

        model = load.load_course(conn, course_id)
        by_id = {a.id: a for a in model.assignments}

        assert by_id[target].excluded is True
        assert by_id[target].source_course_id is not None
        assert all(a.source_course_id is not None for a in model.assignments)
    finally:
        conn.close()


def test_an_excluded_assignment_drops_out_of_the_grade(seeded):
    conn = db.connect(seeded)
    try:
        course_id = conn.execute("SELECT id FROM courses WHERE code = 'ECON 166'").fetchone()["id"]
        before = current_grade(load.load_course(conn, course_id)).percent

        target = conn.execute(
            "SELECT id FROM assignments WHERE name = 'Problem Set 1'"
        ).fetchone()["id"]
        conn.execute("UPDATE assignments SET excluded = 1 WHERE id = ?", (target,))
        conn.commit()

        after = current_grade(load.load_course(conn, course_id)).percent

        assert before != after
    finally:
        conn.close()


# --- gt exclude -----------------------------------------------------------


def test_gt_exclude_hides_an_assignment(seeded):
    conn = db.connect(seeded)
    target = conn.execute(
        "SELECT id FROM assignments WHERE name = 'Problem Set 1'"
    ).fetchone()["id"]
    conn.close()

    result = runner.invoke(app, ["exclude", str(target)])

    assert result.exit_code == 0
    assert "excluded" in result.output
    conn = db.connect(seeded)
    try:
        assert conn.execute(
            "SELECT excluded FROM assignments WHERE id = ?", (target,)
        ).fetchone()["excluded"] == 1
    finally:
        conn.close()


def test_gt_exclude_undo_restores_it(seeded):
    conn = db.connect(seeded)
    target = conn.execute(
        "SELECT id FROM assignments WHERE name = 'Problem Set 1'"
    ).fetchone()["id"]
    conn.close()

    runner.invoke(app, ["exclude", str(target)])
    result = runner.invoke(app, ["exclude", str(target), "--undo"])

    assert result.exit_code == 0
    assert "counted again" in result.output
    conn = db.connect(seeded)
    try:
        assert conn.execute(
            "SELECT excluded FROM assignments WHERE id = ?", (target,)
        ).fetchone()["excluded"] == 0
    finally:
        conn.close()


def test_gt_exclude_rejects_an_unknown_id(seeded):
    result = runner.invoke(app, ["exclude", "99999"])

    assert result.exit_code == 1
    assert "No assignment with id 99999" in result.output


# --- v5: preferred platform and who excluded a row ------------------------


@pytest.fixture
def v4_database(tmp_path):
    """A database built from the schema as it stood at the v4 commit.

    It holds one assignment excluded by hand, which is the only way an
    exclusion could have been made before v5 existed.
    """
    schema = subprocess.run(
        ["git", "show", "373aa29:gradetracker/storage/schema.sql"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    path = tmp_path / "v4.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    for version in range(1, 5):
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.execute(
        """
        INSERT INTO source_courses (source, platform_id, name, code, term, last_seen_at)
        VALUES ('blackboard', '_1_1', 'MATH 116', 'MATH 116', 'Fall 2026', 'now')
        """
    )
    conn.execute(
        """
        INSERT INTO assignments
            (source_course_id, platform_id, name, status, earned, possible,
             excluded, last_seen_at)
        VALUES (1, 'q1', 'Quiz 1', 'graded', 9, 10, 1, 'now')
        """
    )
    conn.execute(
        """
        INSERT INTO assignments
            (source_course_id, platform_id, name, status, earned, possible,
             excluded, last_seen_at)
        VALUES (1, 'q2', 'Quiz 2', 'graded', 8, 10, 0, 'now')
        """
    )
    conn.commit()
    conn.close()
    return path


def test_migration_adds_the_preference_and_exclusion_source_columns(v4_database):
    conn = db.connect(v4_database)
    try:
        assert db.schema_version(conn) == db.SCHEMA_VERSION
        assert "preferred_source" in db._columns(conn, "courses")
        assert "exclusion_source" in db._columns(conn, "assignments")
    finally:
        conn.close()


def test_migration_calls_every_existing_exclusion_the_users_own(v4_database):
    """They were all set by hand, so the auto rule must not touch them."""
    conn = db.connect(v4_database)
    try:
        rows = {
            row["name"]: row["exclusion_source"]
            for row in conn.execute("SELECT name, exclusion_source FROM assignments")
        }

        assert rows == {"Quiz 1": "user", "Quiz 2": None}
    finally:
        conn.close()


def test_a_fresh_database_has_no_preference_and_no_exclusion_source(tmp_path):
    conn = db.connect(tmp_path / "new.db")
    try:
        assert "preferred_source" in db._columns(conn, "courses")
        conn.execute(
            "INSERT INTO courses (code, created_at) VALUES ('MATH 116', 'now')"
        )
        conn.commit()

        row = conn.execute("SELECT preferred_source FROM courses").fetchone()

        assert row["preferred_source"] is None
    finally:
        conn.close()


def test_gt_exclude_records_the_decision_as_the_users_own(seeded):
    conn = db.connect(seeded)
    target = conn.execute(
        "SELECT id FROM assignments WHERE name = 'Problem Set 1'"
    ).fetchone()["id"]
    conn.close()

    runner.invoke(app, ["exclude", str(target)])
    excluded = db.connect(seeded)
    try:
        assert excluded.execute(
            "SELECT exclusion_source FROM assignments WHERE id = ?", (target,)
        ).fetchone()["exclusion_source"] == "user"
    finally:
        excluded.close()

    # And the undo too: counting it again is just as deliberate a decision.
    runner.invoke(app, ["exclude", str(target), "--undo"])
    restored = db.connect(seeded)
    try:
        row = restored.execute(
            "SELECT excluded, exclusion_source FROM assignments WHERE id = ?", (target,)
        ).fetchone()
        assert (row["excluded"], row["exclusion_source"]) == (0, "user")
    finally:
        restored.close()


# --- v6: the default preference, and exclusions in the change log ---------


@pytest.fixture
def v5_database(tmp_path):
    """A v5 database: both platforms linked, no preference, one change row."""
    schema = subprocess.run(
        ["git", "show", "4449dbd:gradetracker/storage/schema.sql"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    path = tmp_path / "v5.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    for version in range(1, 6):
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))

    # MATH 116 on both platforms, CLASC 120 on Blackboard alone, ECON 166
    # already told to keep Blackboard.
    conn.executescript(
        """
        INSERT INTO courses (id, code, created_at) VALUES
            (1, 'MATH 116', 'now'), (2, 'CLASC 120', 'now'), (3, 'ECON 166', 'now');
        UPDATE courses SET preferred_source = 'blackboard' WHERE id = 3;
        INSERT INTO source_courses (id, source, platform_id, course_id, last_seen_at) VALUES
            (1, 'blackboard', 'm1', 1, 'now'),
            (2, 'gradescope', 'm2', 1, 'now'),
            (3, 'blackboard', 'c1', 2, 'now'),
            (4, 'blackboard', 'e1', 3, 'now'),
            (5, 'gradescope', 'e2', 3, 'now');
        INSERT INTO assignments
            (id, source_course_id, platform_id, name, status, last_seen_at)
            VALUES (7, 1, 'q1', 'Quiz 1', 'graded', 'now');
        INSERT INTO changes (id, assignment_id, kind, old_value, new_value, seen_at)
            VALUES (3, 7, 'score', '4/5', '5/5', '2026-10-20T10:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()
    return path


def test_v6_defaults_a_both_platform_course_to_gradescope(v5_database):
    conn = db.connect(v5_database)
    try:
        assert db.schema_version(conn) == db.SCHEMA_VERSION
        rows = {
            row["code"]: row["preferred_source"]
            for row in conn.execute("SELECT code, preferred_source FROM courses")
        }

        assert rows["MATH 116"] == "gradescope"
        # One platform: there is no mirrored copy to choose between.
        assert rows["CLASC 120"] is None
        # Already decided: the migration is a default, not an override.
        assert rows["ECON 166"] == "blackboard"
    finally:
        conn.close()


def test_v6_widens_the_change_kinds_and_keeps_the_existing_rows(v5_database):
    conn = db.connect(v5_database)
    try:
        row = conn.execute("SELECT * FROM changes WHERE id = 3").fetchone()
        assert (row["kind"], row["old_value"], row["new_value"]) == ("score", "4/5", "5/5")

        conn.execute(
            """
            INSERT INTO changes (assignment_id, kind, old_value, new_value, seen_at)
            VALUES (7, 'excluded', 'counted', 'auto: mirrored copy of Quiz 1 (gradescope)', 'now')
            """
        )
        conn.commit()

        kinds = {r["kind"] for r in conn.execute("SELECT kind FROM changes")}
        assert kinds == {"score", "excluded"}
    finally:
        conn.close()


def test_v6_still_refuses_a_kind_that_means_nothing(v5_database):
    conn = db.connect(v5_database)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO changes (assignment_id, kind, new_value, seen_at)
                VALUES (7, 'nonsense', 'x', 'now')
                """
            )
    finally:
        conn.close()


def test_v6_keeps_the_change_log_attached_to_its_assignment(v5_database):
    """The rebuilt table must keep its foreign key, or rows outlive their row."""
    conn = db.connect(v5_database)
    try:
        conn.execute("DELETE FROM assignments WHERE id = 7")
        conn.commit()

        assert conn.execute("SELECT COUNT(*) AS c FROM changes").fetchone()["c"] == 0
    finally:
        conn.close()
