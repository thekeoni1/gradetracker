"""gt link and full-sync database tests (M4).

These drive the CLI and sync.run_sync end to end against a tmp_path database,
with only the connectors' fetch() faked.
"""

from __future__ import annotations

from typer.testing import CliRunner

from gradetracker import config, sync
from gradetracker.auth.base import AuthExpired
from gradetracker.cli import app
from gradetracker.storage import db
from gradetracker.storage import link as linking

from tests.test_normalize import bb_payload, gs_payload

runner = CliRunner()


def configure(tmp_path, monkeypatch):
    """Point every path at tmp_path so nothing touches the real data dir."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(config, "SYLLABI_DIR", tmp_path / "syllabi")
    monkeypatch.setattr(config, "BB_PROFILE_DIR", tmp_path / "profiles" / "blackboard")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "grades.db")
    monkeypatch.setattr(config, "BB_BASE_URL", "https://bb.example.edu")
    return tmp_path / "grades.db"


def fake_blackboard(monkeypatch, payload=None, raises=None):
    from gradetracker.auth.cookie_auth import CookieAuth
    from gradetracker.connectors.blackboard import BlackboardConnector

    monkeypatch.setattr(CookieAuth, "persist", lambda self: True)

    def fetch(self):
        if raises is not None:
            raise raises
        return payload if payload is not None else bb_payload()

    monkeypatch.setattr(BlackboardConnector, "fetch", fetch)


def fake_gradescope(monkeypatch, payload=None, raises=None):
    from gradetracker.connectors.gradescope import GradescopeConnector

    def fetch(self):
        if raises is not None:
            raise raises
        return payload if payload is not None else gs_payload()

    monkeypatch.setattr(GradescopeConnector, "fetch", fetch)


# --- full sync populates the database -------------------------------------


def test_full_sync_populates_the_database(tmp_path, monkeypatch, capsys):
    db_path = configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)

    code = sync.run_sync(["blackboard"], raw_only=False)
    out = capsys.readouterr().out

    assert code == 0
    assert "normalized 2 courses, 10 assignments, 2 dropped" in out
    with db.connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_courses").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 10
        assert db.last_sync_run(conn, "blackboard")["status"] == "ok"


def test_raw_only_does_not_touch_the_database(tmp_path, monkeypatch):
    db_path = configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)

    sync.run_sync(["blackboard"], raw_only=True)

    assert not db_path.exists()


def test_rerunning_a_full_sync_changes_nothing(tmp_path, monkeypatch, capsys):
    """The PDF's done-when: rerunning sync leaves the database alone."""
    db_path = configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)

    sync.run_sync(["blackboard"], raw_only=False)
    with db.connection(db_path) as conn:
        from tests.test_normalize import snapshot

        first = snapshot(conn)

    sync.run_sync(["blackboard"], raw_only=False)
    with db.connection(db_path) as conn:
        from tests.test_normalize import snapshot

        second = snapshot(conn)

    assert first == second


def test_sync_records_auth_expired(tmp_path, monkeypatch, capsys):
    db_path = configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch, raises=AuthExpired("no cookies"))

    code = sync.run_sync(["blackboard"], raw_only=False)

    assert code == 1
    with db.connection(db_path) as conn:
        row = db.last_sync_run(conn, "blackboard")
        assert row["status"] == "auth_expired"
        assert "no cookies" in row["message"]


def test_sync_records_a_connector_error(tmp_path, monkeypatch):
    from gradetracker.connectors.base import ConnectorError

    db_path = configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch, raises=ConnectorError("columns moved"))

    code = sync.run_sync(["blackboard"], raw_only=False)

    assert code == 1
    with db.connection(db_path) as conn:
        row = db.last_sync_run(conn, "blackboard")
        assert row["status"] == "error"
        assert "columns moved" in row["message"]


def test_sync_nudges_about_unlinked_courses(tmp_path, monkeypatch, capsys):
    configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)

    sync.run_sync(["blackboard"], raw_only=False)

    assert "not linked to a course yet" in capsys.readouterr().out


def test_both_sources_land_in_one_database(tmp_path, monkeypatch):
    db_path = configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)
    fake_gradescope(monkeypatch)

    code = sync.run_sync(["blackboard", "gradescope"], raw_only=False)

    assert code == 0
    with db.connection(db_path) as conn:
        counts = dict(
            conn.execute(
                "SELECT source, COUNT(*) FROM source_courses GROUP BY source"
            ).fetchall()
        )
        assert counts == {"blackboard": 2, "gradescope": 2}
        assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 18


# --- gt link --------------------------------------------------------------


def test_link_list_on_an_empty_database(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)

    result = runner.invoke(app, ["link", "--list"])

    assert result.exit_code == 0
    assert "No source courses yet. Run gt sync first." in result.output


def test_link_auto_on_an_empty_database(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)

    result = runner.invoke(app, ["link", "--auto"])

    assert result.exit_code == 0
    assert "Nothing to link." in result.output


def test_link_auto_links_everything(tmp_path, monkeypatch):
    db_path = configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)
    fake_gradescope(monkeypatch)
    sync.run_sync(["blackboard", "gradescope"], raw_only=False)

    result = runner.invoke(app, ["link", "--auto"])

    assert result.exit_code == 0
    assert "created and linked" in result.output
    with db.connection(db_path) as conn:
        assert linking.list_unlinked(conn) == []
        # Four source courses collapse onto two logical courses.
        assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 2


def test_link_list_shows_links_after_auto(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)
    runner.invoke(app, ["link", "--auto"])

    result = runner.invoke(app, ["link", "--list"])

    assert result.exit_code == 0
    assert "blackboard" in result.output
    assert "ECON 166" in result.output
    assert "(unlinked)" not in result.output


def test_link_list_marks_unlinked_courses(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)

    result = runner.invoke(app, ["link", "--list"])

    assert result.exit_code == 0
    assert "(unlinked)" in result.output


def test_link_interactive_accepts_a_suggestion(tmp_path, monkeypatch):
    db_path = configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)

    result = runner.invoke(app, ["link"], input="y\ny\n")

    assert result.exit_code == 0
    assert "linked" in result.output
    with db.connection(db_path) as conn:
        assert linking.list_unlinked(conn) == []


def test_link_interactive_can_decline(tmp_path, monkeypatch):
    db_path = configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)

    result = runner.invoke(app, ["link"], input="n\nn\n")

    assert result.exit_code == 0
    assert "skipped" in result.output
    with db.connection(db_path) as conn:
        assert len(linking.list_unlinked(conn)) == 2
        assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 0


def test_link_interactive_reuses_an_existing_course(tmp_path, monkeypatch):
    """The second platform's ECON 166 should be offered the existing course."""
    db_path = configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)
    fake_gradescope(monkeypatch)
    sync.run_sync(["blackboard", "gradescope"], raw_only=False)
    runner.invoke(app, ["link", "--auto"])

    with db.connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT sc.source, c.code FROM source_courses AS sc
            JOIN courses AS c ON c.id = sc.course_id
            WHERE c.code = 'ECON 166'
            """
        ).fetchall()

    assert sorted(row["source"] for row in rows) == ["blackboard", "gradescope"]


def test_link_interactive_when_everything_is_linked(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)
    runner.invoke(app, ["link", "--auto"])

    result = runner.invoke(app, ["link"])

    assert result.exit_code == 0
    assert "Everything is linked." in result.output


# --- attaching a course whose code differs across platforms ---------------
#
# Real data has Blackboard "ECON 166" and Gradescope "ECONOMICS 166" for the
# same course. --auto would make two courses out of that and split the grade,
# so the code has to be nameable by hand.


def gs_econ_only(code: str) -> dict:
    """A one-course Gradescope dump, under the code the caller names."""
    payload = gs_payload()
    payload["courses"] = [c for c in payload["courses"] if c["name"] == "ECON 166"]
    payload["courses"][0]["name"] = code
    return payload


def blackboard_then_divergent_gradescope(monkeypatch, code="ECONOMICS 166"):
    """ECON 166 and CS 220 linked, plus one unlinked row under `code`.

    Returns that row's source_courses.id, which is what --set takes.
    """
    fake_blackboard(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)
    runner.invoke(app, ["link", "--auto"])

    fake_gradescope(monkeypatch, payload=gs_econ_only(code))
    sync.run_sync(["gradescope"], raw_only=False)

    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT id FROM source_courses
            WHERE source = 'gradescope' AND code = ? AND course_id IS NULL
            """,
            (code,),
        ).fetchone()
    assert row is not None, f"expected an unlinked {code} source course"
    return int(row["id"])


def test_link_set_attaches_a_source_course_whose_code_differs(tmp_path, monkeypatch):
    db_path = configure(tmp_path, monkeypatch)
    source_course_id = blackboard_then_divergent_gradescope(monkeypatch)

    result = runner.invoke(app, ["link", "--set", str(source_course_id), "econ 166"])

    assert result.exit_code == 0
    assert "-> ECON 166" in result.output
    with db.connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT sc.source FROM source_courses AS sc
            JOIN courses AS c ON c.id = sc.course_id
            WHERE c.code = 'ECON 166'
            """
        ).fetchall()
        assert sorted(row["source"] for row in rows) == ["blackboard", "gradescope"]
        # No second ECON course was invented alongside it.
        assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 2


def test_link_set_rejects_an_unknown_course_code(tmp_path, monkeypatch):
    db_path = configure(tmp_path, monkeypatch)
    source_course_id = blackboard_then_divergent_gradescope(monkeypatch)

    result = runner.invoke(app, ["link", "--set", str(source_course_id), "ECON 999"])

    assert result.exit_code == 1
    assert "No course matching 'ECON 999'" in result.output
    assert "CS 220" in result.output and "ECON 166" in result.output
    with db.connection(db_path) as conn:
        # Nothing linked, nothing created.
        assert [row["id"] for row in linking.list_unlinked(conn)] == [source_course_id]
        assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 2


def test_link_set_rejects_an_unknown_source_course_id(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)
    runner.invoke(app, ["link", "--auto"])

    result = runner.invoke(app, ["link", "--set", "9999", "ECON 166"])

    assert result.exit_code == 1
    assert "No platform course with id 9999" in result.output


def test_link_unlink_detaches_a_source_course(tmp_path, monkeypatch):
    db_path = configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)
    runner.invoke(app, ["link", "--auto"])
    with db.connection(db_path) as conn:
        source_course_id = int(
            conn.execute(
                "SELECT id FROM source_courses WHERE code = 'ECON 166'"
            ).fetchone()["id"]
        )

    result = runner.invoke(app, ["link", "--unlink", str(source_course_id)])

    assert result.exit_code == 0
    assert "unlinked blackboard" in result.output
    with db.connection(db_path) as conn:
        assert [row["id"] for row in linking.list_unlinked(conn)] == [source_course_id]


def test_link_interactive_takes_a_typed_code_after_a_decline(tmp_path, monkeypatch):
    db_path = configure(tmp_path, monkeypatch)
    source_course_id = blackboard_then_divergent_gradescope(monkeypatch)

    # Decline the "new course ECONOMICS 166" suggestion, then name ECON 166.
    result = runner.invoke(app, ["link"], input="n\necon 166\n")

    assert result.exit_code == 0
    assert "existing courses: CS 220, ECON 166" in result.output
    assert "-> ECON 166" in result.output
    with db.connection(db_path) as conn:
        assert linking.list_unlinked(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 2
        assert (
            int(
                conn.execute(
                    "SELECT course_id FROM source_courses WHERE id = ?",
                    (source_course_id,),
                ).fetchone()["course_id"]
            )
            == conn.execute("SELECT id FROM courses WHERE code = 'ECON 166'").fetchone()["id"]
        )


def test_link_interactive_leaves_the_row_alone_on_an_unknown_typed_code(
    tmp_path, monkeypatch
):
    """A dropped course has to stay unlinked, so a typo must not guess."""
    db_path = configure(tmp_path, monkeypatch)
    source_course_id = blackboard_then_divergent_gradescope(monkeypatch, "PHYS 190")

    result = runner.invoke(app, ["link"], input="n\nPHYSICS 190\n")

    assert result.exit_code == 0
    assert "No course matching 'PHYSICS 190'" in result.output
    assert "skipped" in result.output
    with db.connection(db_path) as conn:
        assert [row["id"] for row in linking.list_unlinked(conn)] == [source_course_id]
        assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 2


def test_link_list_prints_the_source_course_id(tmp_path, monkeypatch):
    db_path = configure(tmp_path, monkeypatch)
    fake_blackboard(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)
    with db.connection(db_path) as conn:
        ids = [int(row["id"]) for row in linking.list_source_courses(conn)]

    result = runner.invoke(app, ["link", "--list"])

    assert result.exit_code == 0
    assert ids
    first_columns = [
        line.split()[0] for line in result.output.splitlines() if line.strip()
    ]
    for source_course_id in ids:
        assert str(source_course_id) in first_columns
