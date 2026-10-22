"""The preferred-platform rule: storage, the sync hook, and gt prefer (v5).

Every MATH quiz arrives on both platforms, and the duplicates card only flags
a pair once both copies already count. These tests pin the rule that settles
them first, and above all the limits on it: it never un-excludes, and it never
touches a row the user has decided about.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from gradetracker import config, sync
from gradetracker.cli import app
from gradetracker.storage import db, duplicates
from gradetracker.storage import link as linking
from gradetracker.storage.normalize import normalize_blackboard, normalize_gradescope

from tests.test_normalize import bb_payload, gs_payload

runner = CliRunner()


def ask_me_again(conn) -> None:
    """Undo what linking now decides, so the rule can be tested from the start.

    Linking sets the preference and applies it, which is the subject of its
    own tests further down. Everything about the rule's mechanics wants the
    world before that, with both copies still counted.
    """
    conn.execute("UPDATE courses SET preferred_source = NULL")
    conn.execute(
        "UPDATE assignments SET excluded = 0, exclusion_source = NULL "
        "WHERE exclusion_source = 'auto'"
    )
    conn.execute("DELETE FROM changes WHERE kind = 'excluded'")
    conn.commit()


def seed(path) -> None:
    """Both platforms normalised and linked, exactly as a first run leaves it."""
    conn = db.connect(path)
    normalize_blackboard(conn, bb_payload())
    normalize_gradescope(conn, gs_payload())
    linking.link_auto(conn)
    conn.close()


@pytest.fixture
def linked(tmp_path, monkeypatch):
    """A first run as it really ends: linked, defaulted, twins already settled."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "grades.db")
    seed(tmp_path / "grades.db")
    return tmp_path / "grades.db"


@pytest.fixture
def seeded(linked):
    """The same, wound back to before the preference existed. See ask_me_again."""
    conn = db.connect(linked)
    try:
        ask_me_again(conn)
    finally:
        conn.close()
    return linked


def econ_id(path) -> int:
    conn = db.connect(path)
    try:
        return int(
            conn.execute("SELECT id FROM courses WHERE code = 'ECON 166'").fetchone()["id"]
        )
    finally:
        conn.close()


def rows_of(path, course_id) -> dict[tuple[str, str], dict]:
    """Every assignment keyed by (source, name), for readable assertions."""
    conn = db.connect(path)
    try:
        return {
            (row["source"], row["name"]): dict(row)
            for row in conn.execute(
                """
                SELECT a.*, sc.source FROM assignments AS a
                JOIN source_courses AS sc ON sc.id = a.source_course_id
                WHERE sc.course_id = ?
                """,
                (course_id,),
            )
        }
    finally:
        conn.close()


# --- apply_preference -----------------------------------------------------


def clear_preferences(path) -> None:
    """Back to "ask me", which is no longer what linking leaves behind."""
    conn = db.connect(path)
    try:
        conn.execute("UPDATE courses SET preferred_source = NULL")
        conn.commit()
    finally:
        conn.close()


def test_no_preference_excludes_nothing(seeded):
    course_id = econ_id(seeded)
    clear_preferences(seeded)
    conn = db.connect(seeded)
    try:
        assert duplicates.get_preference(conn, course_id) is None

        assert duplicates.apply_preference(conn, course_id) == []
    finally:
        conn.close()

    assert not any(row["excluded"] for row in rows_of(seeded, course_id).values())


def test_the_rule_excludes_only_the_copy_from_the_other_platform(seeded):
    course_id = econ_id(seeded)
    conn = db.connect(seeded)
    try:
        duplicates.set_preference(conn, course_id, "gradescope")

        excluded = duplicates.apply_preference(conn, course_id)
    finally:
        conn.close()

    rows = rows_of(seeded, course_id)
    assert excluded
    for (source, _name), row in rows.items():
        if row["id"] in excluded:
            assert source == "blackboard"
            assert row["exclusion_source"] == "auto"
    # Every Gradescope copy is still counted.
    assert all(
        not row["excluded"] for (source, _n), row in rows.items() if source == "gradescope"
    )


def test_the_rule_catches_a_twin_that_has_not_been_graded_yet(seeded):
    """Midterm Exam is on both platforms and graded on neither."""
    course_id = econ_id(seeded)
    conn = db.connect(seeded)
    try:
        duplicates.set_preference(conn, course_id, "gradescope")
        duplicates.apply_preference(conn, course_id)
    finally:
        conn.close()

    rows = rows_of(seeded, course_id)
    assert rows[("blackboard", "Midterm Exam")]["excluded"] == 1
    assert rows[("gradescope", "Midterm Exam")]["excluded"] == 0


def test_a_blackboard_only_item_is_left_alone(seeded):
    course_id = econ_id(seeded)
    conn = db.connect(seeded)
    try:
        duplicates.set_preference(conn, course_id, "gradescope")
        duplicates.apply_preference(conn, course_id)
    finally:
        conn.close()

    # Participation exists on Blackboard alone, so there is no copy to prefer.
    assert rows_of(seeded, course_id)[("blackboard", "Participation")]["excluded"] == 0


def test_the_rule_is_idempotent(seeded):
    course_id = econ_id(seeded)
    conn = db.connect(seeded)
    try:
        duplicates.set_preference(conn, course_id, "blackboard")
        first = duplicates.apply_preference(conn, course_id)

        second = duplicates.apply_preference(conn, course_id)
    finally:
        conn.close()

    assert first
    assert second == []


def test_the_rule_never_re_includes_what_the_user_excluded(seeded):
    """The user kept Blackboard's copy by hand; preferring Gradescope must not undo that."""
    course_id = econ_id(seeded)
    rows = rows_of(seeded, course_id)
    kept = rows[("blackboard", "Problem Set 2")]["id"]
    dropped = rows[("gradescope", "Problem Set 2")]["id"]

    conn = db.connect(seeded)
    try:
        conn.execute(
            "UPDATE assignments SET excluded = 1, exclusion_source = 'user' WHERE id = ?",
            (dropped,),
        )
        conn.commit()
        duplicates.set_preference(conn, course_id, "gradescope")

        excluded = duplicates.apply_preference(conn, course_id)
    finally:
        conn.close()

    after = rows_of(seeded, course_id)
    assert dropped not in excluded
    assert after[("gradescope", "Problem Set 2")]["excluded"] == 1
    # And the copy the user kept is still counted: the rule would otherwise
    # have excluded both halves and lost the work entirely.
    assert kept not in excluded
    assert after[("blackboard", "Problem Set 2")]["excluded"] == 0


def test_the_rule_leaves_a_row_the_user_chose_to_count(seeded):
    """An 'include' click is remembered, so the next sync does not undo it."""
    course_id = econ_id(seeded)
    twin = rows_of(seeded, course_id)[("blackboard", "Problem Set 1")]["id"]

    conn = db.connect(seeded)
    try:
        duplicates.set_preference(conn, course_id, "gradescope")
        duplicates.apply_preference(conn, course_id)
        # The user puts it back by hand, which stamps exclusion_source.
        conn.execute(
            "UPDATE assignments SET excluded = 0, exclusion_source = 'user' WHERE id = ?",
            (twin,),
        )
        conn.commit()

        again = duplicates.apply_preference(conn, course_id)
    finally:
        conn.close()

    assert twin not in again
    assert rows_of(seeded, course_id)[("blackboard", "Problem Set 1")]["excluded"] == 0


def test_setting_the_preference_to_none_clears_it(seeded):
    course_id = econ_id(seeded)
    conn = db.connect(seeded)
    try:
        duplicates.set_preference(conn, course_id, "gradescope")
        duplicates.set_preference(conn, course_id, None)

        assert duplicates.get_preference(conn, course_id) is None
        assert duplicates.apply_preference(conn, course_id) == []
    finally:
        conn.close()


def test_courses_with_preference_lists_only_the_chosen(seeded):
    course_id = econ_id(seeded)
    clear_preferences(seeded)
    conn = db.connect(seeded)
    try:
        duplicates.set_preference(conn, course_id, "gradescope")

        chosen = duplicates.courses_with_preference(conn)
    finally:
        conn.close()

    assert [int(row["id"]) for row in chosen] == [course_id]


# --- the sync hook --------------------------------------------------------


def test_a_sync_applies_every_courses_preference(seeded, monkeypatch, capsys):
    course_id = econ_id(seeded)
    conn = db.connect(seeded)
    try:
        duplicates.set_preference(conn, course_id, "gradescope")
    finally:
        conn.close()

    # The fetch is not what is under test; only the step at the end of the run.
    monkeypatch.setattr(sync, "_handlers", lambda: {"blackboard": lambda *a, **k: 0})

    code = sync.run_sync(["blackboard"])

    assert code == 0
    assert "ECON 166: excluded" in capsys.readouterr().out
    assert rows_of(seeded, course_id)[("blackboard", "Problem Set 1")]["excluded"] == 1


def test_a_sync_says_nothing_when_there_is_no_preference(seeded, monkeypatch, capsys):
    clear_preferences(seeded)
    monkeypatch.setattr(sync, "_handlers", lambda: {"blackboard": lambda *a, **k: 0})

    sync.run_sync(["blackboard"])

    assert "excluded" not in capsys.readouterr().out


def test_a_raw_only_sync_touches_nothing(seeded, monkeypatch):
    course_id = econ_id(seeded)
    conn = db.connect(seeded)
    try:
        duplicates.set_preference(conn, course_id, "gradescope")
    finally:
        conn.close()
    monkeypatch.setattr(sync, "_handlers", lambda: {"blackboard": lambda *a, **k: 0})

    sync.run_sync(["blackboard"], raw_only=True)

    assert rows_of(seeded, course_id)[("blackboard", "Problem Set 1")]["excluded"] == 0


# --- gt prefer ------------------------------------------------------------


def test_gt_prefer_sets_the_platform_and_says_what_it_excluded(seeded):
    result = runner.invoke(app, ["prefer", "econ166", "gradescope"])

    assert result.exit_code == 0
    assert "prefers Gradescope" in result.output
    assert "Blackboard twin(s)" in result.output
    assert rows_of(seeded, econ_id(seeded))[("blackboard", "Problem Set 1")]["excluded"] == 1


def test_gt_prefer_none_goes_back_to_asking(seeded):
    runner.invoke(app, ["prefer", "ECON 166", "gradescope"])

    result = runner.invoke(app, ["prefer", "ECON 166", "none"])

    assert result.exit_code == 0
    assert "no preferred platform" in result.output
    conn = db.connect(seeded)
    try:
        assert duplicates.get_preference(conn, econ_id(seeded)) is None
    finally:
        conn.close()
    # Clearing the preference does not count the old twins again.
    assert rows_of(seeded, econ_id(seeded))[("blackboard", "Problem Set 1")]["excluded"] == 1


def test_gt_prefer_rejects_an_unknown_platform(seeded):
    result = runner.invoke(app, ["prefer", "ECON 166", "moodle"])

    assert result.exit_code == 1
    assert "Unknown platform" in result.output


def test_gt_prefer_rejects_an_unknown_course(seeded):
    result = runner.invoke(app, ["prefer", "BASKETWEAVING 101", "gradescope"])

    assert result.exit_code == 1
    assert "No course matching" in result.output


# --- the default, set where it can be explained ---------------------------


def test_linking_a_second_platform_sets_the_default(linked):
    conn = db.connect(linked)
    try:
        assert duplicates.get_preference(conn, econ_id(linked)) == "gradescope"
    finally:
        conn.close()


def test_linking_settles_the_twins_there_and_then(linked):
    """Not at the next sync: an already-synced install would double count till then."""
    rows = rows_of(linked, econ_id(linked))

    # The Blackboard halves of the mirrored pairs are already set aside...
    assert rows[("blackboard", "Problem Set 1")]["excluded"] == 1
    assert rows[("blackboard", "Problem Set 1")]["exclusion_source"] == "auto"
    assert rows[("blackboard", "Midterm Exam")]["excluded"] == 1
    # ...the Gradescope copies still count, and so does Blackboard-only work.
    assert rows[("gradescope", "Problem Set 1")]["excluded"] == 0
    assert rows[("blackboard", "Participation")]["excluded"] == 0

    # ...and each one is already on the dashboard, before any sync has run.
    logged = [row for row in recent(linked) if row["kind"] == "excluded"]
    assert {row["assignment_name"] for row in logged} >= {
        "Problem Set 1",
        "Problem Set 2",
        "Midterm Exam",
    }


def test_a_second_section_of_one_platform_sets_nothing(tmp_path, monkeypatch):
    """Two Blackboard sections carry no mirrored copy to choose between."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "one.db")
    conn = db.connect(tmp_path / "one.db")
    try:
        course_id = linking.create_course(conn, "MATH 116")
        for platform_id in ("m1", "m2"):
            cursor = conn.execute(
                """
                INSERT INTO source_courses (source, platform_id, last_seen_at)
                VALUES ('blackboard', ?, 'now')
                """,
                (platform_id,),
            )
            noticed = linking.link(conn, int(cursor.lastrowid), course_id)
            assert noticed is False

        assert duplicates.get_preference(conn, course_id) is None
    finally:
        conn.close()


def test_the_default_does_not_overwrite_a_choice_already_made(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "two.db")
    conn = db.connect(tmp_path / "two.db")
    try:
        course_id = linking.create_course(conn, "MATH 116")
        first = conn.execute(
            """
            INSERT INTO source_courses (source, platform_id, last_seen_at)
            VALUES ('blackboard', 'm1', 'now')
            """
        )
        linking.link(conn, int(first.lastrowid), course_id)
        duplicates.set_preference(conn, course_id, "blackboard")

        second = conn.execute(
            """
            INSERT INTO source_courses (source, platform_id, last_seen_at)
            VALUES ('gradescope', 'm2', 'now')
            """
        )
        noticed = linking.link(conn, int(second.lastrowid), course_id)

        assert noticed is False
        assert duplicates.get_preference(conn, course_id) == "blackboard"
    finally:
        conn.close()


def test_gt_link_auto_says_what_the_default_means(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "fresh.db")
    conn = db.connect(tmp_path / "fresh.db")
    normalize_blackboard(conn, bb_payload())
    normalize_gradescope(conn, gs_payload())
    conn.close()

    result = runner.invoke(app, ["link", "--auto"])

    assert result.exit_code == 0
    assert "now draws from Blackboard and Gradescope" in result.output
    assert "the Gradescope copy is kept" in result.output
    assert "gt prefer ECON 166 none" in result.output


# --- the exclusions show up where a first-run user looks ------------------


def recent(path):
    from gradetracker.storage import changes as changes_store

    conn = db.connect(path)
    try:
        return changes_store.recent_changes(conn)
    finally:
        conn.close()


def test_an_automatic_exclusion_is_written_to_the_change_log(seeded):
    course_id = econ_id(seeded)
    conn = db.connect(seeded)
    try:
        duplicates.set_preference(conn, course_id, "gradescope")
        excluded = duplicates.apply_preference(conn, course_id)
    finally:
        conn.close()

    logged = [row for row in recent(seeded) if row["kind"] == "excluded"]
    assert len(logged) == len(excluded)
    one = logged[0]
    assert one["old_value"] == "counted"
    assert one["new_value"].startswith("auto: mirrored copy of ")
    assert "(gradescope)" in one["new_value"]
    assert one["course_code"] == "ECON 166"


def test_the_change_log_names_the_copy_that_was_kept(linked):
    by_name = {
        row["assignment_name"]: row["new_value"]
        for row in recent(linked)
        if row["kind"] == "excluded"
    }

    assert by_name["Problem Set 1"] == "auto: mirrored copy of Problem Set 1 (gradescope)"


def test_a_first_sync_hides_the_grades_but_not_the_exclusions(linked, monkeypatch, capsys):
    """is_first_sync keeps day one quiet; a decision the app took is not quiet."""
    # A mirrored copy arriving in this run, after the preference was set.
    twin = rows_of(linked, econ_id(linked))[("blackboard", "Problem Set 1")]["id"]
    conn = db.connect(linked)
    try:
        conn.execute(
            "UPDATE assignments SET excluded = 0, exclusion_source = NULL WHERE id = ?",
            (twin,),
        )
        conn.execute("DELETE FROM changes")
        conn.commit()
    finally:
        conn.close()

    toasts = []
    monkeypatch.setattr(
        "gradetracker.notify.notify", lambda title, body: toasts.append((title, body))
    )
    monkeypatch.setattr(sync, "_handlers", lambda: {"blackboard": lambda *a, **k: 0})

    sync.run_sync(["blackboard"])

    logged = [row for row in recent(linked) if row["kind"] == "excluded"]
    assert [row["assignment_name"] for row in logged] == ["Problem Set 1"]
    # One toast for the whole sync, not one per excluded copy.
    assert len(toasts) == 1
    assert toasts[0][0] == "ECON 166: 1 Blackboard copies excluded"


def test_the_toast_collapses_several_courses_into_one():
    title, body = sync._exclusion_toast(
        [("MATH 116", "gradescope", 12), ("ECON 166", "gradescope", 3)]
    )

    assert title == "15 mirrored copies excluded"
    assert body == "MATH 116, ECON 166"


def test_one_course_gets_a_toast_naming_it():
    title, body = sync._exclusion_toast([("MATH 116", "gradescope", 12)])

    assert title == "MATH 116: 12 Blackboard copies excluded"
    assert "Gradescope" in body
