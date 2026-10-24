"""Flask dashboard tests (M7).

The sync runner and the browser launcher are injected fakes, so nothing here
touches the network or opens a window. conftest already blocks a real Gradescope
login and swaps the OS keychain for a dict.
"""

from __future__ import annotations

import json

import pytest

from gradetracker import config
from gradetracker.storage import db, link as linking, load, mapping
from gradetracker.storage import rules as rules_store
from gradetracker.storage.normalize import normalize_blackboard, normalize_gradescope
from gradetracker.syllabus.schema import CategoryDraft, RulesDraft
from gradetracker.web import queries
from gradetracker.web.app import create_app

from tests.test_normalize import bb_payload, gs_payload

ECON_RULES = RulesDraft(
    mode="weighted",
    categories=[
        CategoryDraft(name="Problem Sets", weight=40, drop_lowest=1),
        CategoryDraft(name="Quizzes", weight=10),
        CategoryDraft(name="Midterm", weight=25),
        CategoryDraft(name="Participation", weight=25),
    ],
    notes="Final may be curved.",
)


class FakeJobTarget:
    """Records that it ran, and what it returned."""

    def __init__(self, lines=("done",), raises=None) -> None:
        self.calls = 0
        self.lines = list(lines)
        self.raises = raises

    def __call__(self):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.lines


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(config, "BB_PROFILE_DIR", tmp_path / "profiles" / "blackboard")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "grades.db")
    monkeypatch.setattr(config, "BB_BASE_URL", "https://bb.example.edu")
    path = tmp_path / "grades.db"
    db.connect(path).close()
    return path


def ask_me_again(conn) -> None:
    """Undo the platform preference linking now sets, and its exclusions.

    Most of this file predates that default and is about something else
    entirely -- what-if, the solver, the duplicates card -- so it wants the
    world where both mirrored copies are still counted. Leaving the default
    on here would quietly remove the fixture's only ungraded Blackboard item
    and leave a dozen tests asserting about an empty list. The default itself
    is covered by its own tests, below and in test_preference.py.
    """
    conn.execute("UPDATE courses SET preferred_source = NULL")
    conn.execute(
        "UPDATE assignments SET excluded = 0, exclusion_source = NULL "
        "WHERE exclusion_source = 'auto'"
    )
    conn.execute("DELETE FROM changes WHERE kind = 'excluded'")
    conn.commit()


@pytest.fixture
def seeded_db(empty_db):
    """Both platforms, linked, with ECON 166's rules confirmed.

    Linked as if the platform preference had never been offered; see
    ask_me_again.
    """
    conn = db.connect(empty_db)
    normalize_blackboard(conn, bb_payload())
    normalize_gradescope(conn, gs_payload())
    linking.link_auto(conn)
    ask_me_again(conn)
    econ = conn.execute("SELECT id FROM courses WHERE code = 'ECON 166'").fetchone()["id"]
    rules_store.save_rules(conn, int(econ), ECON_RULES, origin="manual")
    conn.commit()
    conn.close()
    return empty_db


@pytest.fixture
def fakes():
    return {"sync": FakeJobTarget(["blackboard: ok"]), "launcher": FakeJobTarget(["logged in"])}


@pytest.fixture
def client(seeded_db, fakes):
    app = create_app(
        db_path=seeded_db, sync_runner=fakes["sync"], launcher=fakes["launcher"]
    )
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture
def empty_client(empty_db, fakes):
    app = create_app(db_path=empty_db, sync_runner=fakes["sync"], launcher=fakes["launcher"])
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def course_ids(path):
    conn = db.connect(path)
    try:
        return {
            row["code"]: int(row["id"])
            for row in conn.execute("SELECT id, code FROM courses")
        }
    finally:
        conn.close()


def text(response) -> str:
    return response.get_data(as_text=True)


# --- every GET renders ----------------------------------------------------


def test_dashboard_with_data(client, seeded_db):
    response = client.get("/")

    assert response.status_code == 200
    body = text(response)
    assert "ECON 166" in body
    assert "CS 220" in body
    assert "Sync now" in body


def test_dashboard_empty_state_points_at_the_cli(empty_client):
    response = empty_client.get("/")

    assert response.status_code == 200
    body = text(response)
    assert "No courses yet" in body
    assert "gt sync" in body
    assert "gt link" in body


def test_every_course_route_renders(client, seeded_db):
    for course_id in course_ids(seeded_db).values():
        for suffix in ("", "/whatif", "/need", "/rules", "/map"):
            response = client.get(f"/courses/{course_id}{suffix}")
            assert response.status_code == 200, (course_id, suffix)


def test_connections_and_link_render(client):
    assert client.get("/connections").status_code == 200
    assert client.get("/link").status_code == 200


def test_link_page_empty_state(empty_client):
    body = text(empty_client.get("/link"))

    assert "Nothing synced yet" in body


def test_unknown_course_is_404(client):
    assert client.get("/courses/9999").status_code == 404
    assert client.get("/courses/9999/whatif").status_code == 404
    assert client.get("/courses/9999/need").status_code == 404
    assert client.get("/courses/9999/rules").status_code == 404
    assert client.get("/courses/9999/map").status_code == 404


def test_healthz_reports_the_schema_version(client):
    payload = client.get("/healthz").get_json()

    assert payload["ok"] is True
    assert payload["schema"] == db.SCHEMA_VERSION


def test_static_assets_are_served_locally(client):
    css = client.get("/static/app.css")
    htmx = client.get("/static/htmx.min.js")

    assert css.status_code == 200
    assert htmx.status_code == 200
    # Vendored, so the app works with no network.
    assert len(htmx.get_data()) > 10_000


def test_no_external_cdn_references(client, seeded_db):
    """Everything must load from /static, per the brief."""
    for path in ["/", "/connections", "/link"]:
        body = text(client.get(path))
        assert "https://unpkg" not in body
        assert "cdn." not in body


# --- course page ----------------------------------------------------------


def test_course_page_shows_categories_and_bars(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]

    body = text(client.get(f"/courses/{econ}"))

    assert "Problem Sets" in body
    assert "Quizzes" in body
    assert "bar-fill" in body
    assert "Problem Set 1" in body


def test_course_page_shows_the_duplicate_warning(client, seeded_db):
    """The fixtures mirror Problem Set 2 to both platforms."""
    econ = course_ids(seeded_db)["ECON 166"]

    body = text(client.get(f"/courses/{econ}"))

    assert "may be a duplicate" in body


def assignment_row(path, assignment_id):
    conn = db.connect(path)
    try:
        return conn.execute(
            """
            SELECT a.*, sc.source FROM assignments AS a
            JOIN source_courses AS sc ON sc.id = a.source_course_id
            WHERE a.id = ?
            """,
            (assignment_id,),
        ).fetchone()
    finally:
        conn.close()


def duplicate_pairs_of(path, course_id):
    from gradetracker.engine.calculate import current_grade

    conn = db.connect(path)
    try:
        return current_grade(load.load_course(conn, course_id)).duplicate_pairs
    finally:
        conn.close()


def test_course_page_offers_to_resolve_each_duplicate_pair(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]

    body = text(client.get(f"/courses/{econ}"))

    assert "Possible duplicates" in body
    assert "keep gradescope" in body
    assert "keep blackboard" in body


def test_resolving_one_pair_excludes_only_the_other_copy(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    pairs = duplicate_pairs_of(seeded_db, econ)
    assert pairs
    left_id, right_id = pairs[0]
    left = assignment_row(seeded_db, left_id)
    right = assignment_row(seeded_db, right_id)
    assert left["source"] != right["source"]

    response = client.post(
        f"/courses/{econ}/duplicates/resolve",
        data={"keep": left["source"], "pair": f"{left_id}-{right_id}"},
    )

    assert response.status_code == 200
    assert assignment_row(seeded_db, right_id)["excluded"] == 1
    assert assignment_row(seeded_db, left_id)["excluded"] == 0


def test_resolving_every_pair_keeps_one_platform(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    pairs = duplicate_pairs_of(seeded_db, econ)
    assert pairs
    twins = {
        assignment_id
        for pair in pairs
        for assignment_id in pair
        if assignment_row(seeded_db, assignment_id)["source"] == "blackboard"
    }
    assert twins

    client.post(f"/courses/{econ}/duplicates/resolve", data={"keep": "gradescope"})

    for assignment_id in twins:
        assert assignment_row(seeded_db, assignment_id)["excluded"] == 1
    # A Blackboard row that was never half of a pair is left alone.
    untouched = assignment_named(seeded_db, "Participation")
    assert assignment_row(seeded_db, untouched)["excluded"] == 0
    assert not duplicate_pairs_of(seeded_db, econ)


def test_resolving_duplicates_twice_changes_nothing_more(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    client.post(f"/courses/{econ}/duplicates/resolve", data={"keep": "gradescope"})
    excluded = {
        int(row["id"])
        for row in db.connect(seeded_db).execute(
            "SELECT id FROM assignments WHERE excluded = 1"
        )
    }

    client.post(f"/courses/{econ}/duplicates/resolve", data={"keep": "blackboard"})

    after = {
        int(row["id"])
        for row in db.connect(seeded_db).execute(
            "SELECT id FROM assignments WHERE excluded = 1"
        )
    }
    assert after == excluded


def test_the_dashboard_card_counts_possible_duplicates(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    pairs = duplicate_pairs_of(seeded_db, econ)

    body = text(client.get("/"))

    assert f"{len(pairs)} possible duplicates" in body


def _details_blocks(body: str) -> dict[str, dict]:
    """{data-key: {"open": bool, "summary": text}} for each collapsible list."""
    import re

    out = {}
    for attrs, summary in re.findall(
        r'<details([^>]*)>\s*<summary>(.*?)</summary>', body, re.S
    ):
        key = re.search(r'data-key="([^"]+)"', attrs)
        if key is None:
            continue
        out[key.group(1)] = {
            "open": " open" in attrs or attrs.rstrip().endswith("open"),
            "summary": " ".join(re.sub(r"<[^>]+>", " ", summary).split()),
        }
    return out


def test_course_page_collapses_each_category_into_its_own_block(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    conn = db.connect(seeded_db)
    course = load.load_course(conn, econ)
    conn.close()

    blocks = _details_blocks(text(client.get(f"/courses/{econ}")))

    for category in course.categories:
        key = f"course-{econ}-category-{category.id}"
        assert key in blocks
        assert category.name in blocks[key]["summary"]
    assert "Expand all" in text(client.get(f"/courses/{econ}"))


def test_a_category_with_nothing_pending_starts_closed(client, seeded_db):
    """Only a list with something to act on opens itself."""
    econ = course_ids(seeded_db)["ECON 166"]
    conn = db.connect(seeded_db)
    quizzes = conn.execute(
        "SELECT id FROM categories WHERE course_id = ? AND name = 'Quizzes'", (econ,)
    ).fetchone()["id"]
    settled = conn.execute(
        "SELECT id FROM categories WHERE course_id = ? AND name = 'Participation'",
        (econ,),
    ).fetchone()["id"]
    # A missing item nobody has decided about is something to act on.
    target = assignment_named(seeded_db, "Quiz 1")
    conn.execute(
        """
        UPDATE assignments
        SET status = 'missing', missing_as_zero = 0, category_id = ?, category_locked = 1
        WHERE id = ?
        """,
        (quizzes, target),
    )
    conn.commit()
    conn.close()

    blocks = _details_blocks(text(client.get(f"/courses/{econ}")))

    assert blocks[f"course-{econ}-category-{quizzes}"]["open"] is True
    assert blocks[f"course-{econ}-category-{settled}"]["open"] is False


def test_an_unmapped_graded_item_opens_the_unmapped_block(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    conn = db.connect(seeded_db)
    loose = queries.add_manual_assignment(
        conn, econ, "Paper nobody mapped", 9.0, 10.0, status="graded"
    )
    conn.execute("UPDATE assignments SET category_id = NULL WHERE id = ?", (loose,))
    conn.commit()
    conn.close()

    blocks = _details_blocks(text(client.get(f"/courses/{econ}")))

    assert blocks[f"course-{econ}-unmapped"]["open"] is True
    assert "1 to act on" in blocks[f"course-{econ}-unmapped"]["summary"]


def test_excluded_rows_get_their_own_block(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    target = assignment_named(seeded_db, "Problem Set 1")
    client.post(f"/assignments/{target}/excluded")

    blocks = _details_blocks(text(client.get(f"/courses/{econ}")))

    assert f"course-{econ}-excluded" in blocks
    assert blocks[f"course-{econ}-excluded"]["open"] is False


def _badges(body: str) -> str:
    import re

    return " ".join(re.findall(r'<div class="badges">(.*?)</div>', body, re.S))


def test_two_sections_on_one_platform_make_one_badge(client, seeded_db):
    """CPSCI 101 is two Blackboard courses; two identical badges said nothing."""
    econ = course_ids(seeded_db)["ECON 166"]
    conn = db.connect(seeded_db)
    conn.execute(
        """
        INSERT INTO source_courses
            (source, platform_id, name, code, term, course_id, raw, last_seen_at)
        VALUES ('blackboard', 'bb-lab', 'ECON 166 lab', 'ECON 166', '2026F', ?, '{}', ?)
        """,
        (econ, "2020-01-01T00:00:00+00:00"),
    )
    conn.commit()
    card = queries.course_card(conn, econ)
    conn.close()

    assert [source["source"] for source in card["sources"]] == [
        "blackboard",
        "gradescope",
    ]
    # The older of the two is the one worth showing.
    assert card["sources"][0]["last_seen_at"] == "2020-01-01T00:00:00+00:00"
    # Staleness still reads every platform course, so the course is not stale.
    assert card["stale"] is False
    assert _badges(text(client.get(f"/courses/{econ}"))).count("blackboard") == 1


def test_the_course_page_counts_unmapped_items_once(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    conn = db.connect(seeded_db)
    conn.execute(
        "UPDATE assignments SET category_id = NULL, category_locked = 1 WHERE id = ?",
        (assignment_named(seeded_db, "Participation"),),
    )
    conn.commit()
    conn.close()

    body = text(client.get(f"/courses/{econ}"))

    assert body.count("have no category and are not counted") == 1
    assert "not mapped to a category" not in body
    assert "Map them" in body


def _unmapped_block(body: str) -> str:
    import re

    found = re.search(r'<details class="unmapped-group"(.*?)</details>', body, re.S)
    assert found is not None, "no unmapped block rendered"
    return found.group(1)


def test_the_unmapped_group_starts_closed_in_weighted_mode(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    conn = db.connect(seeded_db)
    loose = queries.add_manual_assignment(
        conn, econ, "Webtest nobody mapped", None, 100.0, status="ungraded"
    )
    conn.execute("UPDATE assignments SET category_id = NULL WHERE id = ?", (loose,))
    conn.commit()
    conn.close()

    for path in (f"/courses/{econ}/whatif", f"/courses/{econ}/need"):
        block = _unmapped_block(text(client.get(path)))
        assert not block.startswith(" open")
        assert "not counted in weighted mode" in block
    # The solver ignores them, so they are not ticked into "on N items" either.
    assert "checked" not in _unmapped_block(text(client.get(f"/courses/{econ}/need")))


def test_the_unmapped_group_starts_open_in_points_mode(client, seeded_db):
    """Without rules every item is unmapped, and in points mode they count."""
    cs = course_ids(seeded_db)["CS 220"]

    for path in (f"/courses/{cs}/whatif", f"/courses/{cs}/need"):
        block = _unmapped_block(text(client.get(path)))
        assert block.startswith(" open")
        assert "counted in points mode" in block
    assert "checked" in _unmapped_block(text(client.get(f"/courses/{cs}/need")))


def test_course_page_shows_trust_signals(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]

    body = text(client.get(f"/courses/{econ}"))

    assert "rules: confirmed (manual," in body
    assert "weighted mode" in body
    assert "synced" in body


def test_a_course_without_rules_shows_an_unconfirmed_badge(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]

    body = text(client.get(f"/courses/{cs}"))

    assert "unconfirmed rules" in body


# --- row actions ----------------------------------------------------------


def grade_of(path, course_id):
    conn = db.connect(path)
    try:
        from gradetracker.engine.calculate import current_grade

        return current_grade(load.load_course(conn, course_id)).percent
    finally:
        conn.close()


def assignment_named(path, name):
    conn = db.connect(path)
    try:
        row = conn.execute(
            "SELECT id FROM assignments WHERE name = ? ORDER BY id LIMIT 1", (name,)
        ).fetchone()
        return int(row["id"]) if row else None
    finally:
        conn.close()


def test_changing_a_category_locks_the_assignment(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    target = assignment_named(seeded_db, "Participation")
    conn = db.connect(seeded_db)
    quizzes = conn.execute(
        "SELECT id FROM categories WHERE course_id = ? AND name = 'Quizzes'", (econ,)
    ).fetchone()["id"]
    conn.close()

    response = client.post(f"/assignments/{target}/category", data={"category_id": str(quizzes)})

    assert response.status_code == 200
    conn = db.connect(seeded_db)
    try:
        row = conn.execute("SELECT * FROM assignments WHERE id = ?", (target,)).fetchone()
        assert row["category_id"] == quizzes
        assert row["category_locked"] == 1
    finally:
        conn.close()


def test_clearing_a_category_is_allowed(client, seeded_db):
    target = assignment_named(seeded_db, "Problem Set 1")

    client.post(f"/assignments/{target}/category", data={"category_id": ""})

    conn = db.connect(seeded_db)
    try:
        assert conn.execute(
            "SELECT category_id FROM assignments WHERE id = ?", (target,)
        ).fetchone()["category_id"] is None
    finally:
        conn.close()


def test_toggling_missing_as_zero_changes_the_grade(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    # Make one item missing so the toggle has something to act on.
    conn = db.connect(seeded_db)
    target = conn.execute(
        "SELECT id FROM assignments WHERE name = 'Midterm Exam' ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    conn.execute(
        "UPDATE assignments SET status = 'missing', missing_as_zero = 1 WHERE id = ?",
        (target,),
    )
    conn.commit()
    conn.close()
    before = grade_of(seeded_db, econ)

    response = client.post(f"/assignments/{target}/missing-as-zero")

    assert response.status_code == 200
    after = grade_of(seeded_db, econ)
    assert before != after


def test_excluding_a_copy_removes_the_duplicate_warning(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    body = text(client.get(f"/courses/{econ}"))
    assert "may be a duplicate" in body

    conn = db.connect(seeded_db)
    from gradetracker.engine.calculate import current_grade

    pairs = current_grade(load.load_course(conn, econ)).duplicate_pairs
    conn.close()
    assert pairs
    copy_id = pairs[0][1]

    response = client.post(f"/assignments/{copy_id}/excluded")

    assert "may be a duplicate" not in text(response)


def test_toggling_extra_credit(client, seeded_db):
    target = assignment_named(seeded_db, "Problem Set 1")

    client.post(f"/assignments/{target}/extra-credit")

    conn = db.connect(seeded_db)
    try:
        assert conn.execute(
            "SELECT extra_credit FROM assignments WHERE id = ?", (target,)
        ).fetchone()["extra_credit"] == 1
    finally:
        conn.close()


def test_an_unknown_flag_is_404(client, seeded_db):
    target = assignment_named(seeded_db, "Problem Set 1")

    assert client.post(f"/assignments/{target}/drop-table").status_code == 404


def test_a_flag_on_an_unknown_assignment_is_404(client):
    assert client.post("/assignments/999999/excluded").status_code == 404


# --- manual assignments ---------------------------------------------------


def test_adding_a_manual_item_then_deleting_it(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]

    response = client.post(
        f"/courses/{econ}/assignments",
        data={"name": "Oral presentation", "earned": "18", "possible": "20", "status": "graded"},
    )

    assert response.status_code == 200
    assert "Oral presentation" in text(response)
    added = assignment_named(seeded_db, "Oral presentation")
    assert added is not None

    conn = db.connect(seeded_db)
    try:
        row = conn.execute("SELECT * FROM assignments WHERE id = ?", (added,)).fetchone()
        assert row["manual"] == 1
        assert row["platform_id"].startswith("manual-")
        source = conn.execute(
            "SELECT source FROM source_courses WHERE id = ?", (row["source_course_id"],)
        ).fetchone()["source"]
        assert source == "manual"
    finally:
        conn.close()

    deleted = client.post(f"/assignments/{added}/delete")
    assert "Oral presentation" not in text(deleted)
    assert assignment_named(seeded_db, "Oral presentation") is None


def test_a_manual_item_survives_a_resync(client, seeded_db):
    """The whole point of adding by hand: sync must leave it alone."""
    econ = course_ids(seeded_db)["ECON 166"]
    client.post(
        f"/courses/{econ}/assignments",
        data={"name": "Paper exam", "earned": "44", "possible": "50", "status": "graded"},
    )

    conn = db.connect(seeded_db)
    try:
        normalize_blackboard(conn, bb_payload())
        normalize_gradescope(conn, gs_payload())
        row = conn.execute(
            "SELECT * FROM assignments WHERE name = 'Paper exam'"
        ).fetchone()
        assert row is not None
        assert row["earned"] == 44.0
        assert row["manual"] == 1
    finally:
        conn.close()


def test_the_manual_source_course_is_created_once(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    for index in range(3):
        client.post(f"/courses/{econ}/assignments", data={"name": f"Item {index}", "status": "graded"})

    conn = db.connect(seeded_db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM source_courses WHERE source = 'manual'"
        ).fetchone()["n"] == 1
    finally:
        conn.close()


def test_a_nameless_manual_item_is_ignored(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    before = len(queries.assignment_rows(db.connect(seeded_db), econ))

    client.post(f"/courses/{econ}/assignments", data={"name": "  ", "status": "graded"})

    conn = db.connect(seeded_db)
    try:
        assert len(queries.assignment_rows(conn, econ)) == before
    finally:
        conn.close()


def test_a_platform_row_cannot_be_deleted(client, seeded_db):
    target = assignment_named(seeded_db, "Problem Set 1")

    client.post(f"/assignments/{target}/delete")

    assert assignment_named(seeded_db, "Problem Set 1") is not None


# --- what-if --------------------------------------------------------------


def test_whatif_calc_changes_the_grade(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    conn = db.connect(seeded_db)
    items = queries.unscored_assignments(conn, econ)
    conn.close()
    assert items
    target = items[0]

    response = client.post(
        f"/courses/{econ}/whatif/calc",
        data={f"score_{target['id']}": str(target["possible"])},
    )

    assert response.status_code == 200
    body = text(response)
    assert "In this scenario" in body
    assert "1 item(s) filled in" in body


def test_whatif_calc_with_nothing_filled_in_matches_current(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]

    body = text(client.post(f"/courses/{econ}/whatif/calc", data={}))

    assert "nothing filled in yet" in body


def test_whatif_calc_ignores_blank_boxes(client, seeded_db):
    """A blank box means "still unknown", not zero."""
    econ = course_ids(seeded_db)["ECON 166"]
    # The fixtures leave only one scorable unknown, so add a second by hand.
    client.post(
        f"/courses/{econ}/assignments",
        data={"name": "Final essay", "possible": "40", "status": "ungraded"},
    )
    conn = db.connect(seeded_db)
    items = queries.unscored_assignments(conn, econ)
    conn.close()

    assert len(items) >= 2
    body = text(
        client.post(
            f"/courses/{econ}/whatif/calc",
            data={f"score_{items[0]['id']}": "", f"score_{items[1]['id']}": "10"},
        )
    )

    assert "1 item(s) filled in" in body


def test_whatif_calc_counts_what_is_filled_in_per_category(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    conn = db.connect(seeded_db)
    course = load.load_course(conn, econ)
    items = queries.unscored_assignments(conn, econ)
    conn.close()
    target = items[0]
    names = {category.id: category.name for category in course.categories}
    siblings = [i for i in items if i["category_id"] == target["category_id"]]

    body = text(
        client.post(
            f"/courses/{econ}/whatif/calc",
            data={f"score_{target['id']}": str(target["possible"])},
        )
    )
    rows = _by_category_rows(body)

    assert rows[names[target["category_id"]]][4] == f"1 of {len(siblings)} filled"
    # Every category is listed, even the ones with nothing left to guess at.
    for name in names.values():
        assert name in rows


def test_whatif_page_groups_the_sliders_by_category(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    course, items = _two_unknown_categories(seeded_db, econ)
    with_unknowns = {
        category.name
        for category in course.categories
        if any(item["category_id"] == category.id for item in items)
    }

    body = text(client.get(f"/courses/{econ}/whatif"))

    assert body.count('class="category-group"') == len(with_unknowns)
    for name in with_unknowns:
        assert name in body
    # One "Set all" box per group, and nothing extra.
    assert body.count('class="small set-all-button"') == len(with_unknowns)
    assert body.count('class="set-all"') == len(with_unknowns)


def test_whatif_page_has_a_reset_link(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]

    body = text(client.get(f"/courses/{econ}/whatif"))

    assert "Reset" in body
    assert 'type="range"' in body


def test_scenario_round_trip(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    conn = db.connect(seeded_db)
    items = queries.unscored_assignments(conn, econ)
    conn.close()
    target = items[0]

    saved = client.post(
        f"/courses/{econ}/scenarios",
        data={"name": "ace the final", f"score_{target['id']}": "17.5"},
    )
    assert saved.status_code == 302

    page = text(client.get(f"/courses/{econ}/whatif"))
    assert "ace the final" in page

    conn = db.connect(seeded_db)
    scenarios = queries.list_scenarios(conn, econ)
    conn.close()
    assert len(scenarios) == 1
    assert scenarios[0]["overrides"] == {target["id"]: 17.5}

    loaded = text(client.get(f"/courses/{econ}/whatif/scenario/{scenarios[0]['id']}"))
    assert 'value="17.5"' in loaded

    client.post(f"/courses/{econ}/scenarios/{scenarios[0]['id']}/delete")
    conn = db.connect(seeded_db)
    try:
        assert queries.list_scenarios(conn, econ) == []
    finally:
        conn.close()


def test_saving_a_scenario_twice_replaces_it(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    conn = db.connect(seeded_db)
    items = queries.unscored_assignments(conn, econ)
    conn.close()

    client.post(f"/courses/{econ}/scenarios", data={"name": "plan", f"score_{items[0]['id']}": "1"})
    client.post(f"/courses/{econ}/scenarios", data={"name": "plan", f"score_{items[0]['id']}": "2"})

    conn = db.connect(seeded_db)
    try:
        scenarios = queries.list_scenarios(conn, econ)
        assert len(scenarios) == 1
        assert scenarios[0]["overrides"][items[0]["id"]] == 2.0
    finally:
        conn.close()


# --- what-if placeholders -------------------------------------------------

ECON_RULES_WITH_A_FINAL = RulesDraft(
    mode="weighted",
    categories=[
        CategoryDraft(name="Problem Sets", weight=40, drop_lowest=1),
        CategoryDraft(name="Quizzes", weight=10),
        CategoryDraft(name="Midterm", weight=25),
        CategoryDraft(name="Participation", weight=10),
        CategoryDraft(name="Final", weight=15),
    ],
)


def _add_a_final_category(seeded_db, course_id) -> int:
    """The real case: a syllabus category neither platform has posted.

    save_rules matches categories by name, so the four that already hold
    assignments keep their ids and only Final is new -- and empty.
    """
    conn = db.connect(seeded_db)
    try:
        rules_store.save_rules(
            conn, course_id, ECON_RULES_WITH_A_FINAL, origin="manual"
        )
        row = conn.execute(
            "SELECT id FROM categories WHERE course_id = ? AND name = 'Final'",
            (course_id,),
        ).fetchone()
        return int(row["id"])
    finally:
        conn.close()


def _whatif_details(body: str, css_class: str) -> list[str]:
    """Every <details class="<css_class>"> chunk, opening tag included.

    Only for blocks with no nested <details>, which is both of the ones this
    section is about.
    """
    import re

    return re.findall(rf'<details class="{css_class}"[^>]*>.*?</details>', body, re.S)


def _group_containing(body: str, needle: str) -> str:
    """The category-group div holding needle, up to the next group."""
    for chunk in body.split('<div class="category-group">')[1:]:
        if needle in chunk:
            return chunk
    return ""


def _post_placeholder(client, course_id, category_id, name="Final", possible="100", **kw):
    """What the Add placeholder button sends: category in the URL, fields
    named after it, because it posts from inside the slider form."""
    return client.post(
        f"/courses/{course_id}/whatif/placeholder/{category_id}",
        data={
            f"ph_name_{category_id}": name,
            f"ph_possible_{category_id}": possible,
        },
        **kw,
    )


def _assignments_named(path, name: str) -> list[dict]:
    conn = db.connect(path)
    try:
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM assignments WHERE name = ?", (name,))
        ]
    finally:
        conn.close()


def test_a_category_with_nothing_in_it_shows_up_closed(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    final = _add_a_final_category(seeded_db, econ)

    body = text(client.get(f"/courses/{econ}/whatif"))

    blocks = [b for b in _whatif_details(body, "empty-category") if "Final" in b]
    assert len(blocks) == 1
    block = blocks[0]
    # Closed: there is nothing in it to read.
    assert "<details class=\"empty-category\">" in block
    assert "nothing posted yet" in block
    # And an add control pointing at this category, with fields named after
    # it so the slider form it sits in cannot confuse them for another's.
    assert f"whatif/placeholder/{final}" in block
    assert f'name="ph_name_{final}"' in block
    assert f'name="ph_possible_{final}"' in block
    assert f'hx-params="ph_name_{final},ph_possible_{final}"' in block


def test_an_empty_category_is_not_a_slider_group(client, seeded_db):
    """The sliders, the Set all box and the by-category table stay as they were."""
    econ = course_ids(seeded_db)["ECON 166"]
    before = text(client.get(f"/courses/{econ}/whatif"))
    rows_before = _by_category_rows(before)

    _add_a_final_category(seeded_db, econ)
    after = text(client.get(f"/courses/{econ}/whatif"))

    assert after.count('class="category-group"') == before.count(
        'class="category-group"'
    )
    assert after.count('class="set-all"') == before.count('class="set-all"')
    rows_after = _by_category_rows(after)
    assert set(rows_after) == set(rows_before) | {"Final"}
    # Cell 1 is the weight, which the draft above rebalances to make room for
    # the final. Everything the row says about the solve is untouched.
    for name, cells in rows_before.items():
        assert rows_after[name][2:] == cells[2:]


def test_the_name_box_starts_as_the_category_name(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    final = _add_a_final_category(seeded_db, econ)

    block = [
        b for b in _whatif_details(text(client.get(f"/courses/{econ}/whatif")), "empty-category")
        if "Final" in b
    ][0]

    assert f'name="ph_name_{final}" value="Final"' in " ".join(block.split())


def test_a_placeholder_becomes_a_slider_in_its_own_category(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    final = _add_a_final_category(seeded_db, econ)

    posted = _post_placeholder(client, econ, final, possible="150")
    assert posted.status_code == 302

    rows = _assignments_named(seeded_db, "Final")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ungraded"
    assert row["earned"] is None
    assert row["possible"] == 150.0
    assert row["category_id"] == final
    assert row["manual"] == 1
    # Locked, or the next mapping pass could move it out of the category it
    # was created to stand in for.
    assert row["category_locked"] == 1

    body = text(client.get(f"/courses/{econ}/whatif"))
    group = _group_containing(body, f'name="score_{row["id"]}"')
    assert "Final" in group.split("</h4>")[0]
    assert 'max="150"' in group
    # It is a real group now, not the closed block.
    assert not [b for b in _whatif_details(body, "empty-category") if "Final" in b]


def test_the_right_category_gets_it_when_every_box_is_posted(client, seeded_db):
    """The button sits inside the slider form, so htmx posts the lot.

    Every category on the page has a name and an out-of box, and they all
    arrive together. Reading a plain "possible" would take whichever came
    first, which is how clicking under Final used to file the row under
    Midterm.
    """
    econ = course_ids(seeded_db)["ECON 166"]
    final = _add_a_final_category(seeded_db, econ)
    conn = db.connect(seeded_db)
    categories = {
        row["name"]: int(row["id"])
        for row in conn.execute(
            "SELECT id, name FROM categories WHERE course_id = ?", (econ,)
        )
    }
    conn.close()
    assert len(categories) > 1

    every_box = {}
    for name, category_id in categories.items():
        every_box[f"ph_name_{category_id}"] = name
        every_box[f"ph_possible_{category_id}"] = "7"
    every_box[f"ph_name_{final}"] = "Final"
    every_box[f"ph_possible_{final}"] = "250"

    client.post(f"/courses/{econ}/whatif/placeholder/{final}", data=every_box)

    rows = [r for r in _assignments_named(seeded_db, "Final") if r["manual"]]
    assert len(rows) == 1
    assert rows[0]["category_id"] == final
    assert rows[0]["possible"] == 250.0
    # And nothing was created for any of the other categories.
    conn = db.connect(seeded_db)
    try:
        made = conn.execute("SELECT COUNT(*) AS n FROM assignments WHERE manual = 1")
        assert made.fetchone()["n"] == 1
    finally:
        conn.close()


def test_a_blank_name_falls_back_to_the_category(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    final = _add_a_final_category(seeded_db, econ)

    _post_placeholder(client, econ, final, name="   ")

    assert len(_assignments_named(seeded_db, "Final")) == 1


@pytest.mark.parametrize("possible", ["", "0", "-5", "not a number", "  "])
def test_a_placeholder_worth_nothing_is_refused_out_loud(client, seeded_db, possible):
    """A row with no point value is accepted by the database and then invisible."""
    econ = course_ids(seeded_db)["ECON 166"]
    final = _add_a_final_category(seeded_db, econ)

    response = _post_placeholder(
        client, econ, final, possible=possible, follow_redirects=True
    )

    assert response.status_code == 200
    assert "point value above zero" in text(response)
    assert _assignments_named(seeded_db, "Final") == []


def test_a_placeholder_needs_a_category_of_this_course(client, seeded_db):
    ids = course_ids(seeded_db)
    econ, cs = ids["ECON 166"], ids["CS 220"]
    final = _add_a_final_category(seeded_db, econ)

    # A category of no course at all, and ECON's own Final posted at CS 220.
    for category_id in (0, final + 9999, final):
        response = _post_placeholder(
            client, cs, category_id, follow_redirects=True
        )
        assert response.status_code == 200
        assert "does not belong to this course" in text(response)

    assert _assignments_named(seeded_db, "Final") == []


def test_a_category_with_sliders_offers_a_placeholder_too(client, seeded_db):
    """A second midterm that has not been posted yet is worth planning for."""
    econ = course_ids(seeded_db)["ECON 166"]

    body = text(client.get(f"/courses/{econ}/whatif"))

    toggles = _whatif_details(body, "placeholder-toggle")
    # One per group that has sliders; the fixture leaves only Midterm unknown.
    assert len(toggles) == body.count('class="category-group"')
    assert "Something else coming in Midterm?" in body
    assert "whatif/placeholder" in toggles[0]


def test_the_unmapped_group_offers_no_placeholder(client, seeded_db):
    """There is no category to add it to, so there is nothing to offer."""
    cs = course_ids(seeded_db)["CS 220"]

    body = text(client.get(f"/courses/{cs}/whatif"))

    assert 'class="unmapped-group"' in body
    assert "whatif/placeholder" not in body
    assert 'class="empty-category"' not in body


def test_remove_only_appears_on_by_hand_rows(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    final = _add_a_final_category(seeded_db, econ)
    conn = db.connect(seeded_db)
    platform_row = queries.unscored_assignments(conn, econ)[0]
    conn.close()
    assert platform_row["manual"] == 0

    _post_placeholder(client, econ, final)
    manual_id = _assignments_named(seeded_db, "Final")[0]["id"]

    body = text(client.get(f"/courses/{econ}/whatif"))

    assert f'/assignments/{manual_id}/delete' in body
    assert f'/assignments/{platform_row["id"]}/delete' not in body
    assert body.count('class="remove-placeholder"') == 1


def test_removing_a_placeholder_returns_to_whatif(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    final = _add_a_final_category(seeded_db, econ)
    _post_placeholder(client, econ, final)
    manual_id = _assignments_named(seeded_db, "Final")[0]["id"]

    removed = client.post(f"/assignments/{manual_id}/delete?return_to=whatif")

    assert removed.status_code == 302
    assert f"/courses/{econ}/whatif" in removed.headers["Location"]
    assert _assignments_named(seeded_db, "Final") == []


def test_remove_leaves_platform_rows_alone(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    conn = db.connect(seeded_db)
    platform_row = queries.unscored_assignments(conn, econ)[0]
    conn.close()

    client.post(f"/assignments/{platform_row['id']}/delete?return_to=whatif")

    assert _assignments_named(seeded_db, platform_row["name"])


def test_a_course_with_nothing_unscored_still_offers_placeholders(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    _add_a_final_category(seeded_db, econ)
    conn = db.connect(seeded_db)
    try:
        for item in queries.unscored_assignments(conn, econ):
            conn.execute(
                "UPDATE assignments SET status = 'graded', earned = 1 WHERE id = ?",
                (item["id"],),
            )
        conn.commit()
        assert queries.unscored_assignments(conn, econ) == []
    finally:
        conn.close()

    body = text(client.get(f"/courses/{econ}/whatif"))

    assert "Nothing left to guess at" in body
    # Nothing is unknown, so there is no group of unknowns.
    assert 'class="category-group"' not in body
    # Every category is still here to plan with, Final included.
    assert len(_whatif_details(body, "empty-category")) == 5
    assert "whatif/placeholder" in body
    # The only sliders left are regrades, and those are worth saving.
    assert "Regrade something in Problem Sets?" in body
    assert "Save scenario" in body


def test_a_placeholder_reaches_the_need_page(client, seeded_db):
    """Need lists categories with unknown items, so the new row arrives free."""
    econ = course_ids(seeded_db)["ECON 166"]
    final = _add_a_final_category(seeded_db, econ)

    def need_rows():
        return _by_category_rows(
            text(client.post(f"/courses/{econ}/need/calc", data={"letter": "A-"}))
        )

    assert "Final" not in need_rows()

    _post_placeholder(client, econ, final)

    assert "Final" in need_rows()


# --- what do I need -------------------------------------------------------


def test_need_calc_ok(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    conn = db.connect(seeded_db)
    items = queries.unscored_assignments(conn, econ)
    conn.close()

    response = client.post(
        f"/courses/{econ}/need/calc",
        data={"letter": "A", "unknown": [str(item["id"]) for item in items]},
    )

    assert response.status_code == 200
    assert "You need" in text(response)


def test_need_calc_impossible(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    conn = db.connect(seeded_db)
    items = queries.unscored_assignments(conn, econ)
    conn.close()

    body = text(
        client.post(
            f"/courses/{econ}/need/calc",
            data={"percent": "400", "unknown": [str(items[0]["id"])]},
        )
    )

    assert "Not reachable" in body


def test_need_calc_locked_in(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    conn = db.connect(seeded_db)
    items = queries.unscored_assignments(conn, econ)
    conn.close()

    body = text(
        client.post(
            f"/courses/{econ}/need/calc",
            data={"percent": "1", "unknown": [str(items[0]["id"])]},
        )
    )

    assert "Already locked in" in body


def test_need_calc_with_no_boxes_ticked_uses_every_unknown(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]

    body = text(client.post(f"/courses/{econ}/need/calc", data={"letter": "A-"}))

    assert "You need" in body or "locked in" in body


# --- what-if regrades -----------------------------------------------------
#
# The course that lets you rewrite a paper: an 88/100 that could become a 95,
# and the question is what that does to the course grade. The engine already
# applied an override to any item whatever its status, so all of this is the
# rows to drag.


def _regrade_blocks(body: str) -> list[str]:
    """Every <details class="regrade-toggle"> chunk, opening tag included."""
    import re

    return re.findall(r'<details class="regrade-toggle"[^>]*>.*?</details>', body, re.S)


def _regrade_row(body: str, assignment_id: int) -> str:
    """One slider-row's markup, found by its own box name."""
    marker = f'name="score_{assignment_id}"'
    for chunk in body.split('<div class="slider-row">')[1:]:
        if marker in chunk:
            return chunk.split("</details>", 1)[0]
    return ""


def _graded(path, course_id):
    conn = db.connect(path)
    try:
        return queries.graded_assignments(conn, course_id)
    finally:
        conn.close()


def _empty_category_tag(body: str, assignment_id: int) -> str:
    """What follows the class on the empty-category fold holding one box.

    "" for a closed fold, " open" for an open one. Each split chunk runs to
    the next empty-category block, so the regrade fold nested inside this one
    is part of this chunk.
    """
    marker = f'name="score_{assignment_id}"'
    for chunk in body.split('<details class="empty-category"')[1:]:
        if marker in chunk:
            return chunk.split(">", 1)[0]
    return ""


def test_a_graded_item_gets_a_blank_box_with_its_real_score_behind_it(
    client, seeded_db
):
    """Blank is how the row says "leave this alone", so blank it starts."""
    econ = course_ids(seeded_db)["ECON 166"]
    item = [i for i in _graded(seeded_db, econ) if i["name"] == "Problem Set 1"][0]
    assert item["earned"] == 18.0

    body = text(client.get(f"/courses/{econ}/whatif"))
    row = _regrade_row(body, item["id"])

    assert row
    assert f'name="score_{item["id"]}" min="0" step="0.5"' in " ".join(row.split())
    assert 'value=""' in row
    assert 'placeholder="18"' in row
    # The score it has now, said once, where the row says what it is.
    assert "18 / 20 now" in " ".join(row.split())
    assert "gradescope" in row


def test_the_regrade_toggle_only_appears_where_there_is_a_mark_to_change(
    client, seeded_db
):
    econ = course_ids(seeded_db)["ECON 166"]
    # Problem Sets then holds graded items and an unknown; Quizzes holds only
    # the unknown.
    _two_unknown_categories(seeded_db, econ)

    body = text(client.get(f"/courses/{econ}/whatif"))

    assert "Regrade something in Problem Sets?" in body
    assert "Regrade something in Quizzes?" not in body
    # Closed until it is wanted, like the placeholder offer beside it.
    assert '<details class="regrade-toggle">' in body
    assert "open" not in _regrade_blocks(body)[0].split(">", 1)[0]


def test_the_regrade_sliders_sit_inside_the_recalculating_form(client, seeded_db):
    """Outside #whatif-form the boxes would post nothing and move nothing."""
    econ = course_ids(seeded_db)["ECON 166"]

    body = text(client.get(f"/courses/{econ}/whatif"))
    form = body.split('<form id="whatif-form"', 1)[1].split("</form>", 1)[0]

    assert _regrade_blocks(form)
    assert len(_regrade_blocks(form)) == len(_regrade_blocks(body))


def test_a_regrade_moves_the_course_grade_by_what_the_engine_says(client, seeded_db):
    """The real case: an 88/100 taken to full marks."""
    from gradetracker.engine.calculate import current_grade

    cs = course_ids(seeded_db)["CS 220"]
    exam = [i for i in _graded(seeded_db, cs) if i["name"] == "Exam 1"][0]
    assert (exam["earned"], exam["possible"]) == (88.0, 100.0)

    conn = db.connect(seeded_db)
    course = load.load_course(conn, cs)
    conn.close()
    before = current_grade(course).percent
    after = current_grade(course, overrides={exam["id"]: 100.0}).percent
    assert after > before

    body = text(
        client.post(f"/courses/{cs}/whatif/calc", data={f"score_{exam['id']}": "100"})
    )

    assert f"{after:.2f}%" in body
    assert "1 item(s) filled in" in body
    assert f"+{after - before:.2f}" in body


def test_a_blank_regrade_box_changes_nothing(client, seeded_db):
    """An untouched row sends nothing, so the engine keeps the real mark."""
    cs = course_ids(seeded_db)["CS 220"]
    exam = [i for i in _graded(seeded_db, cs) if i["name"] == "Exam 1"][0]

    body = text(
        client.post(f"/courses/{cs}/whatif/calc", data={f"score_{exam['id']}": ""})
    )
    rows = _by_category_rows(body)

    assert "nothing filled in yet" in body
    # Current and scenario are the same number, whatever it is.
    percents = body.split('class="big">')
    assert percents[1][:7] == percents[2][:7]
    assert all("regraded" not in cells[4] for cells in rows.values())


def test_a_regrade_is_not_counted_as_a_filled_unknown(client, seeded_db):
    """A category of finished work has made no progress on its unknowns."""
    econ = course_ids(seeded_db)["ECON 166"]
    item = [i for i in _graded(seeded_db, econ) if i["name"] == "Problem Set 1"][0]

    body = text(
        client.post(f"/courses/{econ}/whatif/calc", data={f"score_{item['id']}": "20"})
    )
    cells = _by_category_rows(body)["Problem Sets"]

    assert "nothing unknown" in cells[4]
    assert "1 regraded" in cells[4]
    # The category it is in moved; the one with the real unknown did not.
    assert "regraded" not in _by_category_rows(body)["Midterm"][4]


def test_a_regrade_saves_and_loads_open(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    item = [i for i in _graded(seeded_db, econ) if i["name"] == "Problem Set 1"][0]

    saved = client.post(
        f"/courses/{econ}/scenarios",
        data={"name": "rewrite the first one", f"score_{item['id']}": "19.5"},
    )
    assert saved.status_code == 302

    conn = db.connect(seeded_db)
    scenarios = queries.list_scenarios(conn, econ)
    conn.close()
    assert scenarios[0]["overrides"] == {item["id"]: 19.5}

    loaded = text(client.get(f"/courses/{econ}/whatif/scenario/{scenarios[0]['id']}"))
    row = _regrade_row(loaded, item["id"])

    assert 'value="19.5"' in row
    # Folded open, or a loaded scenario would hide the change it is about.
    blocks = [b for b in _regrade_blocks(loaded) if f'score_{item["id"]}' in b]
    assert len(blocks) == 1
    assert blocks[0].startswith('<details class="regrade-toggle" open>')
    # And so is the fold it sits inside. Everything in Problem Sets is marked,
    # which puts the whole category behind one more fold, and an open block
    # inside a closed one is still a change nothing on screen shows.
    assert _empty_category_tag(loaded, item["id"]) == " open"
    # And the other categories' toggles stay closed.
    assert '<details class="regrade-toggle">' in loaded
    assert '<details class="empty-category">' in loaded


def test_excluded_and_excused_items_get_no_regrade_row(client, seeded_db):
    """Neither counts towards the grade, so neither is worth a slider."""
    econ = course_ids(seeded_db)["ECON 166"]
    sets = [i for i in _graded(seeded_db, econ) if i["name"].startswith("Problem Set")]
    excluded, excused = sets[0], sets[1]

    conn = db.connect(seeded_db)
    conn.execute("UPDATE assignments SET excluded = 1 WHERE id = ?", (excluded["id"],))
    conn.execute(
        "UPDATE assignments SET status = 'excused' WHERE id = ?", (excused["id"],)
    )
    conn.commit()
    conn.close()

    assert {i["id"] for i in _graded(seeded_db, econ)}.isdisjoint(
        {excluded["id"], excused["id"]}
    )

    body = text(client.get(f"/courses/{econ}/whatif"))

    assert f'name="score_{excluded["id"]}"' not in body
    assert f'name="score_{excused["id"]}"' not in body


def test_a_regrade_for_another_course_is_ignored(client, seeded_db):
    """The engine only ever sees this course's own assignments."""
    ids = course_ids(seeded_db)
    econ, cs = ids["ECON 166"], ids["CS 220"]
    exam = [i for i in _graded(seeded_db, cs) if i["name"] == "Exam 1"][0]

    mine = text(client.post(f"/courses/{econ}/whatif/calc", data={}))
    theirs = text(
        client.post(f"/courses/{econ}/whatif/calc", data={f"score_{exam['id']}": "0"})
    )

    # The scenario grade is untouched. The count is honest about the box that
    # was filled in, which is all the page can say about an id it never saw.
    assert mine.split('class="big">')[2][:7] == theirs.split('class="big">')[2][:7]
    assert f'name="score_{exam["id"]}"' not in text(
        client.get(f"/courses/{econ}/whatif")
    )


def _all_marked_points_course(seeded_db, course_id):
    """CS 220 with every item marked: points mode, and nothing mapped.

    Returns (unknown, graded). Nothing is unknown and nothing belongs to a
    category, which is the shape that rendered a card with no rows in it.
    """
    conn = db.connect(seeded_db)
    try:
        mine = "source_course_id IN (SELECT id FROM source_courses WHERE course_id = ?)"
        # The mirrored stubs carry no points of their own, so they are not work
        # and get no slider either way.
        conn.execute(
            f"UPDATE assignments SET excluded = 1 WHERE {mine} "
            "AND (possible IS NULL OR possible <= 0)",
            (course_id,),
        )
        conn.execute(
            f"UPDATE assignments SET status = 'graded', earned = 18.0 WHERE {mine} "
            "AND excluded = 0 AND status != 'graded'",
            (course_id,),
        )
        conn.commit()
        return (
            queries.unscored_assignments(conn, course_id),
            queries.graded_assignments(conn, course_id),
        )
    finally:
        conn.close()


def test_unmapped_marked_items_get_regrade_rows_in_points_mode(client, seeded_db):
    """Points mode counts every item, so a regrade on one of them is real."""
    cs = course_ids(seeded_db)["CS 220"]
    unknown, graded = _all_marked_points_course(seeded_db, cs)
    assert not unknown
    assert graded

    body = text(client.get(f"/courses/{cs}/whatif"))

    # The page promises this, two lines up, so it has to be there.
    assert "put a different score on something already marked" in body
    assert "Regrade something in Unmapped?" in body
    for item in graded:
        assert f'name="score_{item["id"]}"' in body
    # Nothing unknown, so the count says what is actually in the block, and
    # there is nothing for a percent to fill.
    assert f"{len(graded)} marked" in " ".join(body.split())
    assert "counted in points mode" in body
    # The script always mentions these; what matters is that no row renders one.
    assert 'class="set-all"' not in body
    assert 'class="small set-all-button"' not in body


def test_unmapped_marked_items_get_no_regrade_rows_in_weighted_mode(
    client, seeded_db
):
    """An unmapped item counts towards nothing, so its slider would do nothing."""
    econ = course_ids(seeded_db)["ECON 166"]
    sets = [i for i in _graded(seeded_db, econ) if i["name"].startswith("Problem Set")]
    loose, kept = sets[0], sets[1]

    conn = db.connect(seeded_db)
    conn.execute(
        "UPDATE assignments SET category_id = NULL, category_locked = 1 WHERE id = ?",
        (loose["id"],),
    )
    conn.commit()
    conn.close()

    body = text(client.get(f"/courses/{econ}/whatif"))

    assert f'name="score_{loose["id"]}"' not in body
    # The ones that do count keep theirs.
    assert f'name="score_{kept["id"]}"' in body
    assert "Regrade something in Unmapped?" not in body


def test_the_by_category_table_leaves_out_an_unmapped_row_with_nothing_in_it(
    client, seeded_db
):
    """A row of dashes the whole way across says nothing worth a row."""
    cs = course_ids(seeded_db)["CS 220"]
    unknown, graded = _all_marked_points_course(seeded_db, cs)
    assert not unknown

    body = text(client.get(f"/courses/{cs}/whatif"))

    # Nothing unknown and nothing regraded: no row, and so no table at all.
    assert "By category" not in body

    # Fill a regrade in and the row is worth having, because now it says
    # something the rest of the page does not.
    first = graded[0]
    calc = text(
        client.post(f"/courses/{cs}/whatif/calc", data={f"score_{first['id']}": "1"})
    )
    cells = _by_category_rows(calc)["Unmapped"]

    assert "nothing unknown" in cells[4]
    assert "1 regraded" in cells[4]


def _missed_homework(seeded_db, course_id, counted=True):
    """A missing item in Problem Sets, counted as a zero or not."""
    conn = db.connect(seeded_db)
    try:
        category = int(
            conn.execute(
                "SELECT id FROM categories WHERE course_id = ? AND name = 'Problem Sets'",
                (course_id,),
            ).fetchone()["id"]
        )
        missed = queries.add_manual_assignment(
            conn, course_id, "Problem Set 4", None, 20.0,
            status="missing", category_id=category,
        )
        if not counted:
            conn.execute(
                "UPDATE assignments SET missing_as_zero = 0 WHERE id = ?", (missed,)
            )
        conn.commit()
        return (
            missed,
            {item["id"]: item for item in queries.graded_assignments(conn, course_id)},
            {item["id"] for item in queries.unscored_assignments(conn, course_id)},
        )
    finally:
        conn.close()


def test_a_missing_item_counted_as_a_zero_can_be_regraded(client, seeded_db):
    """A missed homework accepted late is the regrade worth asking for."""
    econ = course_ids(seeded_db)["ECON 166"]
    missed, graded, unknown = _missed_homework(seeded_db, econ)

    # The engine already scores it a real zero, so it is a mark, not a blank.
    assert missed in graded
    assert graded[missed]["earned"] == 0.0
    assert missed not in unknown

    body = text(client.get(f"/courses/{econ}/whatif"))
    row = " ".join(_regrade_row(body, missed).split())

    assert row
    # It has no score of its own, so "0 / 20" would read as a mark it was given.
    assert "missing, counted as 0 now" in row
    assert "0 / 20 now" not in row
    assert 'placeholder="0"' in row


def test_a_missing_item_not_counted_as_a_zero_stays_an_unknown(client, seeded_db):
    """missing_as_zero = 0 means nobody has decided yet, which is a blank."""
    econ = course_ids(seeded_db)["ECON 166"]
    missed, graded, unknown = _missed_homework(seeded_db, econ, counted=False)

    assert missed in unknown
    assert missed not in graded

    body = text(client.get(f"/courses/{econ}/whatif"))

    assert f'name="score_{missed}"' in body
    assert all(f'name="score_{missed}"' not in block for block in _regrade_blocks(body))


def _by_category_rows(body: str) -> dict[str, list[str]]:
    """{category name: [cell text, ...]} from a rendered "By category" table."""
    import re

    chunk = body.split("By category", 1)[1].split("</table>", 1)[0]
    rows = {}
    for row in re.findall(r"<tr>(.*?)</tr>", chunk, re.S)[1:]:  # [1:] drops the header
        cells = [
            " ".join(re.sub(r"<[^>]+>", " ", cell).split())
            for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        ]
        if cells:
            rows[cells[0]] = cells
    return rows


def _two_unknown_categories(seeded_db, course_id):
    """Give ECON a second unknown item, in a different category."""
    conn = db.connect(seeded_db)
    try:
        categories = {
            row["name"]: int(row["id"])
            for row in conn.execute(
                "SELECT id, name FROM categories WHERE course_id = ?", (course_id,)
            )
        }
        queries.add_manual_assignment(
            conn, course_id, "Quiz 9", None, 20.0,
            status="ungraded", category_id=categories["Quizzes"],
        )
        queries.add_manual_assignment(
            conn, course_id, "Problem Set 9", None, 50.0,
            status="ungraded", category_id=categories["Problem Sets"],
        )
        return load.load_course(conn, course_id), queries.unscored_assignments(
            conn, course_id
        )
    finally:
        conn.close()


def test_need_calc_breaks_the_answer_down_by_category(client, seeded_db):
    """Each row solves for one category alone, so it must match that solve."""
    from gradetracker.engine.solve import required_score

    econ = course_ids(seeded_db)["ECON 166"]
    course, items = _two_unknown_categories(seeded_db, econ)
    names = {category.id: category.name for category in course.categories}

    body = text(
        client.post(
            f"/courses/{econ}/need/calc",
            data={"percent": "90", "unknown": [str(item["id"]) for item in items]},
        )
    )
    rows = _by_category_rows(body)

    grouped: dict[int, list[int]] = {}
    for item in items:
        grouped.setdefault(item["category_id"], []).append(item["id"])
    assert len(grouped) >= 2

    for category_id, ids in grouped.items():
        expected = required_score(course, 90.0, ids)
        cells = rows[names[category_id]]
        assert cells[4] == f"{sum(expected.per_item.values()):.2f}"
        assert cells[5] == f"{expected.score_percent:.2f}%"
    # Categories with nothing unknown stay out of it.
    assert "Participation" not in rows


def test_need_calc_by_category_says_which_rows_are_locked_in(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    _two_unknown_categories(seeded_db, econ)

    body = text(client.post(f"/courses/{econ}/need/calc", data={"percent": "1"}))

    assert "locked in, even zeros reach it" in body


def test_need_calc_by_category_ignores_unmapped_items_but_says_so(client, seeded_db):
    """In weighted mode an unmapped item carries no weight, so it gets a note."""
    econ = course_ids(seeded_db)["ECON 166"]
    _two_unknown_categories(seeded_db, econ)
    conn = db.connect(seeded_db)
    unmapped = queries.add_manual_assignment(
        conn, econ, "Mystery item", None, 30.0, status="ungraded"
    )
    conn.execute("UPDATE assignments SET category_id = NULL WHERE id = ?", (unmapped,))
    conn.commit()
    conn.close()

    body = text(client.post(f"/courses/{econ}/need/calc", data={"percent": "90"}))

    assert "1 unknown item(s) are not mapped to a category" in body
    assert "Unmapped" not in _by_category_rows(body)


def test_need_page_lists_the_courses_cutoffs(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]

    body = text(client.get(f"/courses/{econ}/need"))

    assert "A-" in body
    assert "90" in body


# --- rules form -----------------------------------------------------------


def rules_form_data(**overrides):
    data = {
        "mode": "weighted",
        "category_name": ["Problem Sets", "Exams"],
        "category_weight": ["60", "40"],
        "category_drop": ["1", "0"],
        "cutoff_letter": ["A", "F"],
        "cutoff_min": ["90", "0"],
        "bonus_percent": "0",
        "notes": "",
    }
    data.update(overrides)
    return data


def test_rules_post_with_blocking_warnings_does_not_save(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]
    data = rules_form_data(category_name=["Exams", "exams"], category_weight=["50", "50"])

    response = client.post(f"/courses/{cs}/rules", data=data)

    assert response.status_code == 200
    body = text(response)
    assert "duplicate category name" in body
    assert "cannot be saved" in body
    conn = db.connect(seeded_db)
    try:
        assert rules_store.rules_status(conn, cs) == "none"
    finally:
        conn.close()


def test_rules_post_with_advisory_warnings_asks_for_confirmation(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]
    data = rules_form_data(category_weight=["40", "40"])

    response = client.post(f"/courses/{cs}/rules", data=data)

    assert response.status_code == 200
    body = text(response)
    assert "sum to 80%" in body
    assert "Confirm anyway" in body
    conn = db.connect(seeded_db)
    try:
        assert rules_store.rules_status(conn, cs) == "none"
    finally:
        conn.close()


def test_rules_post_confirmed_saves_and_redirects_to_map(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]
    data = rules_form_data(category_weight=["40", "40"], confirm_anyway="1")

    response = client.post(f"/courses/{cs}/rules", data=data)

    assert response.status_code == 302
    assert f"/courses/{cs}/map" in response.headers["Location"]
    conn = db.connect(seeded_db)
    try:
        assert rules_store.rules_status(conn, cs) == "confirmed"
        draft = rules_store.load_rules(conn, cs)
        assert [c.name for c in draft.categories] == ["Problem Sets", "Exams"]
        assert draft.categories[0].drop_lowest == 1
    finally:
        conn.close()


def test_rules_form_saves_rank_weights(client, seeded_db):
    """ECON 166's real rule through the form: a 40% category split 23 / 17."""
    cs = course_ids(seeded_db)["CS 220"]
    data = rules_form_data(
        category_name=["Midterms", "Homework"],
        category_weight=["40", "60"],
        category_drop=["0", "0"],
        category_ranks=["23, 17", ""],
    )

    response = client.post(f"/courses/{cs}/rules", data=data)

    assert response.status_code == 302
    conn = db.connect(seeded_db)
    try:
        draft = rules_store.load_rules(conn, cs)
        assert [(c.name, c.rank_weights) for c in draft.categories] == [
            ("Midterms", [23.0, 17.0]),
            ("Homework", None),
        ]
    finally:
        conn.close()


def test_rules_form_reports_rank_weights_that_do_not_add_up(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]
    data = rules_form_data(
        category_name=["Midterms", "Homework"],
        category_weight=["40", "60"],
        category_drop=["0", "0"],
        category_ranks=["23, 18", ""],
        confirm_anyway="1",
    )

    response = client.post(f"/courses/{cs}/rules", data=data)

    assert response.status_code == 200
    body = text(response)
    assert "add up to 41%" in body
    # Blocking, so confirming through it is not offered and nothing is saved.
    assert "cannot be saved" in body
    conn = db.connect(seeded_db)
    try:
        assert rules_store.rules_status(conn, cs) == "none"
    finally:
        conn.close()


def test_the_rules_form_refuses_an_unreadable_rank_weight(client, seeded_db):
    """"40, abc" must not quietly become [40, 0] and save."""
    cs = course_ids(seeded_db)["CS 220"]
    data = rules_form_data(
        category_name=["Midterms", "Homework"],
        category_weight=["40", "60"],
        category_drop=["0", "0"],
        category_ranks=["40, abc", ""],
        confirm_anyway="1",
    )

    response = client.post(f"/courses/{cs}/rules", data=data)

    assert response.status_code == 200
    # The quotes around the token come back HTML-escaped.
    body = text(response).replace("&#39;", "'")
    assert "could not read 'abc'" in body
    assert "cannot be saved" in body
    conn = db.connect(seeded_db)
    try:
        assert rules_store.rules_status(conn, cs) == "none"
    finally:
        conn.close()


def test_the_rules_form_refuses_a_nan_rank_weight(client, seeded_db):
    """float("nan") parses happily and then passes every numeric check."""
    cs = course_ids(seeded_db)["CS 220"]
    data = rules_form_data(
        category_name=["Midterms", "Homework"],
        category_weight=["40", "60"],
        category_drop=["0", "0"],
        category_ranks=["nan, 40", ""],
        confirm_anyway="1",
    )

    response = client.post(f"/courses/{cs}/rules", data=data)

    assert response.status_code == 200
    assert "must be a number" in text(response)
    conn = db.connect(seeded_db)
    try:
        assert rules_store.rules_status(conn, cs) == "none"
    finally:
        conn.close()


def test_the_rules_form_refuses_a_nan_category_weight(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]
    data = rules_form_data(
        category_name=["Midterms", "Homework"],
        category_weight=["nan", "60"],
        category_drop=["0", "0"],
        confirm_anyway="1",
    )

    response = client.post(f"/courses/{cs}/rules", data=data)

    assert response.status_code == 200
    assert "must be a number" in text(response)
    conn = db.connect(seeded_db)
    try:
        assert rules_store.rules_status(conn, cs) == "none"
    finally:
        conn.close()


def test_the_rules_form_refuses_climbing_rank_weights(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]
    data = rules_form_data(
        category_name=["Midterms", "Homework"],
        category_weight=["40", "60"],
        category_drop=["0", "0"],
        category_ranks=["17, 23", ""],
        confirm_anyway="1",
    )

    response = client.post(f"/courses/{cs}/rules", data=data)

    assert response.status_code == 200
    assert "must not climb" in text(response)


def test_the_live_validator_reports_a_rank_weight_sum(client, seeded_db):
    """The warnings box updates as the rank weights are typed."""
    econ = course_ids(seeded_db)["ECON 166"]
    data = rules_form_data(
        category_name=["Midterms", "Homework"],
        category_weight=["40", "60"],
        category_drop=["0", "0"],
        category_ranks=["23, 18", ""],
    )

    body = text(client.post(f"/courses/{econ}/rules/validate", data=data))

    assert "add up to 41%" in body


def test_the_rules_page_shows_the_rank_weights_box(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]

    body = text(client.get(f"/courses/{cs}/rules"))

    assert 'name="category_ranks"' in body
    assert "e.g. 23, 17" in body


def test_the_rules_page_prefills_saved_rank_weights(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]
    client.post(
        f"/courses/{cs}/rules",
        data=rules_form_data(
            category_name=["Midterms", "Homework"],
            category_weight=["40", "60"],
            category_drop=["0", "0"],
            category_ranks=["23, 17", ""],
        ),
    )

    body = text(client.get(f"/courses/{cs}/rules"))

    assert 'value="23.0, 17.0"' in body or 'value="23, 17"' in body


def test_the_course_page_shows_the_weight_each_ranked_item_received(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    client.post(
        f"/courses/{econ}/rules",
        data=rules_form_data(
            category_name=["Midterms"],
            category_weight=["100"],
            category_drop=["0"],
            category_ranks=["60, 40"],
        ),
    )
    conn = db.connect(seeded_db)
    try:
        midterms = int(
            conn.execute(
                "SELECT id FROM categories WHERE course_id = ? AND name = 'Midterms'",
                (econ,),
            ).fetchone()["id"]
        )
        ids = [
            int(row["id"])
            for row in conn.execute(
                """
                SELECT a.id FROM assignments AS a
                JOIN source_courses AS sc ON sc.id = a.source_course_id
                WHERE sc.course_id = ? ORDER BY a.id LIMIT 2
                """,
                (econ,),
            ).fetchall()
        ]
        conn.executemany(
            "UPDATE assignments SET category_id = ?, category_locked = 1, name = ?, "
            "earned = ?, possible = 100, status = 'graded' WHERE id = ?",
            [(midterms, "Midterm 1", 80.0, ids[0]), (midterms, "Midterm 2", 90.0, ids[1])],
        )
        conn.execute(
            "UPDATE assignments SET excluded = 1 WHERE id NOT IN (?, ?)", (ids[0], ids[1])
        )
        conn.commit()
    finally:
        conn.close()

    body = text(client.get(f"/courses/{econ}"))

    # Midterm 2 scored higher, so it carries the 60.
    assert '<span class="badge">60%</span>' in body
    assert '<span class="badge">40%</span>' in body
    assert "ranked 60% / 40%" in body


def test_clean_rules_save_without_confirmation(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]

    response = client.post(f"/courses/{cs}/rules", data=rules_form_data())

    assert response.status_code == 302
    conn = db.connect(seeded_db)
    try:
        assert rules_store.rules_status(conn, cs) == "confirmed"
    finally:
        conn.close()


def test_rules_validate_fragment_reports_the_total(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]

    body = text(client.post(f"/courses/{cs}/rules/validate", data=rules_form_data()))

    assert "100%" in body
    assert "No problems found" in body


def test_rules_validate_fragment_flags_blocking(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]
    data = rules_form_data(cutoff_letter=["A", "B"], cutoff_min=["80", "90"])

    body = text(client.post(f"/courses/{cs}/rules/validate", data=data))

    assert "must descend" in body


def test_rules_form_prefills_from_saved_rules(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]

    body = text(client.get(f"/courses/{econ}/rules"))

    assert 'value="Problem Sets"' in body
    assert "Final may be curved." in body
    assert "rules: confirmed (manual," in body


def test_blank_category_rows_are_dropped(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]
    data = rules_form_data(
        category_name=["Problem Sets", "", "Exams"],
        category_weight=["60", "", "40"],
        category_drop=["1", "0", "0"],
    )

    client.post(f"/courses/{cs}/rules", data=data)

    conn = db.connect(seeded_db)
    try:
        assert len(rules_store.load_rules(conn, cs).categories) == 2
    finally:
        conn.close()


# --- mapping --------------------------------------------------------------


def test_map_accept_all(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]
    client.post(f"/courses/{cs}/rules", data=rules_form_data(category_name=["Labs", "Exams"], category_weight=["50", "50"]))

    response = client.post(f"/courses/{cs}/map/accept-all")

    assert response.status_code == 302
    conn = db.connect(seeded_db)
    try:
        rows = conn.execute(
            """
            SELECT a.category_locked FROM assignments AS a
            JOIN source_courses AS sc ON sc.id = a.source_course_id
            WHERE sc.course_id = ? AND a.category_id IS NOT NULL
            """,
            (cs,),
        ).fetchall()
        assert rows
        # Accepted suggestions stay unlocked.
        assert all(row["category_locked"] == 0 for row in rows)
    finally:
        conn.close()


def test_map_page_without_categories_explains(client, seeded_db):
    cs = course_ids(seeded_db)["CS 220"]

    body = text(client.get(f"/courses/{cs}/map"))

    assert "No categories to map into yet" in body


def test_map_page_lists_unmapped_with_suggestions(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    target = assignment_named(seeded_db, "Participation")
    client.post(f"/assignments/{target}/category", data={"category_id": ""})
    conn = db.connect(seeded_db)
    conn.execute("UPDATE assignments SET category_locked = 0 WHERE id = ?", (target,))
    conn.commit()
    conn.close()

    body = text(client.get(f"/courses/{econ}/map"))

    assert "Participation" in body


# --- the Map page saves every row in one submit ---------------------------
#
# Each select used to hx-post on change and the reply was the course detail,
# so every one of 29 codelets cost a round trip and bounced off the Map page.


def cs_with_categories(client, seeded_db):
    """CS 220 with two categories and every row unmapped.

    Saving rules accepts the suggestions it can, so the rows are cleared again
    here: this is about the Map page's own form, not about suggestions.
    """
    cs = course_ids(seeded_db)["CS 220"]
    client.post(
        f"/courses/{cs}/rules",
        data=rules_form_data(
            category_name=["Labs", "Exams"], category_weight=["50", "50"]
        ),
    )
    conn = db.connect(seeded_db)
    try:
        conn.execute(
            """
            UPDATE assignments SET category_id = NULL, category_locked = 0
            WHERE source_course_id IN (
                SELECT id FROM source_courses WHERE course_id = ?
            )
            """,
            (cs,),
        )
        conn.commit()
        categories = {
            row["name"]: int(row["id"]) for row in mapping.list_categories(conn, cs)
        }
        unmapped = [int(row["id"]) for row in mapping.list_unmapped(conn, cs)]
    finally:
        conn.close()
    return cs, categories, unmapped


def assignment_rows(path, assignment_ids):
    conn = db.connect(path)
    try:
        placeholders = ",".join("?" for _ in assignment_ids)
        rows = conn.execute(
            f"SELECT id, category_id, category_locked FROM assignments "
            f"WHERE id IN ({placeholders})",
            tuple(assignment_ids),
        ).fetchall()
        return {int(row["id"]): row for row in rows}
    finally:
        conn.close()


def test_map_save_sets_every_chosen_row_and_leaves_the_rest(client, seeded_db):
    cs, categories, unmapped = cs_with_categories(client, seeded_db)
    assert len(unmapped) >= 3
    first, second, untouched = unmapped[0], unmapped[1], unmapped[2]

    response = client.post(
        f"/courses/{cs}/map/save",
        data={
            f"category_{first}": str(categories["Labs"]),
            f"category_{second}": str(categories["Exams"]),
            f"category_{untouched}": "",
        },
    )

    assert response.status_code == 302
    # Back to the Map page, not the course page.
    assert f"/courses/{cs}/map" in response.headers["Location"]
    rows = assignment_rows(seeded_db, [first, second, untouched])
    assert rows[first]["category_id"] == categories["Labs"]
    assert rows[second]["category_id"] == categories["Exams"]
    # Chosen by hand, so locked, exactly as the single-row route does.
    assert rows[first]["category_locked"] == 1
    assert rows[second]["category_locked"] == 1
    assert rows[untouched]["category_id"] is None
    assert rows[untouched]["category_locked"] == 0


def test_map_page_posts_one_form_instead_of_one_request_per_choice(client, seeded_db):
    cs, _, unmapped = cs_with_categories(client, seeded_db)

    body = text(client.get(f"/courses/{cs}/map"))

    assert f"/courses/{cs}/map/save" in body
    assert "Save selections" in body
    assert "Apply to all rows" in body
    assert f'name="category_{unmapped[0]}"' in body
    # No per-choice round trip, so no bounce to the course page.
    assert "hx-post" not in body


def test_map_save_ignores_a_category_from_another_course(client, seeded_db):
    """A crafted form must not file CS 220's work under an ECON category."""
    econ = course_ids(seeded_db)["ECON 166"]
    cs, _, unmapped = cs_with_categories(client, seeded_db)
    conn = db.connect(seeded_db)
    try:
        foreign = int(mapping.list_categories(conn, econ)[0]["id"])
    finally:
        conn.close()

    response = client.post(
        f"/courses/{cs}/map/save", data={f"category_{unmapped[0]}": str(foreign)}
    )

    assert response.status_code == 302
    assert assignment_rows(seeded_db, [unmapped[0]])[unmapped[0]]["category_id"] is None


def test_map_save_ignores_an_assignment_from_another_course(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    cs, categories, _ = cs_with_categories(client, seeded_db)
    conn = db.connect(seeded_db)
    try:
        foreign_assignment = int(
            conn.execute(
                """
                SELECT a.id FROM assignments AS a
                JOIN source_courses AS sc ON sc.id = a.source_course_id
                WHERE sc.course_id = ? LIMIT 1
                """,
                (econ,),
            ).fetchone()["id"]
        )
        before = int(
            conn.execute(
                "SELECT category_id FROM assignments WHERE id = ?", (foreign_assignment,)
            ).fetchone()["category_id"]
            or 0
        )
    finally:
        conn.close()

    client.post(
        f"/courses/{cs}/map/save",
        data={f"category_{foreign_assignment}": str(categories["Labs"])},
    )

    row = assignment_rows(seeded_db, [foreign_assignment])[foreign_assignment]
    assert (row["category_id"] or 0) == before


def test_map_save_tolerates_junk_field_names(client, seeded_db):
    cs, categories, unmapped = cs_with_categories(client, seeded_db)

    response = client.post(
        f"/courses/{cs}/map/save",
        data={
            "category_": str(categories["Labs"]),
            "category_abc": str(categories["Labs"]),
            "something_else": "1",
            f"category_{unmapped[0]}": "not-a-number",
        },
    )

    assert response.status_code == 302
    assert assignment_rows(seeded_db, [unmapped[0]])[unmapped[0]]["category_id"] is None


# --- sync -----------------------------------------------------------------


def test_post_sync_calls_the_injected_runner_once(client, fakes):
    response = client.post("/sync")

    assert response.status_code == 200
    # The job runs in a thread; wait for it rather than sleeping blindly.
    _wait_for(lambda: fakes["sync"].calls == 1)
    assert fakes["sync"].calls == 1


def test_sync_status_reports_done(client, fakes):
    client.post("/sync")
    _wait_for(lambda: fakes["sync"].calls == 1)

    body = text(client.get("/sync/status"))

    assert "Sync finished" in body
    assert "blackboard: ok" in body
    # The finished fragment also refreshes the cards out of band.
    assert "hx-swap-oob" in body


def test_a_finished_sync_does_not_duplicate_the_cards_on_the_full_page(client, seeded_db, fakes):
    """The out-of-band swap belongs to htmx replies, never to a page load.

    The dashboard includes the sync-status partial with its own context, so a
    finished job used to render the cards inside #sync-status as well as in
    #cards: every course appeared twice.
    """
    before = text(client.get("/"))
    client.post("/sync")
    _wait_for(lambda: fakes["sync"].calls == 1)

    after = text(client.get("/"))

    assert "Sync finished" in after
    assert "hx-swap-oob" not in after
    for course_id in course_ids(seeded_db).values():
        link = f'href="/courses/{course_id}"'
        # The heading, the Open button, and a duplicates badge where the
        # course has one. The bug doubled whatever that count was.
        assert after.count(link) == before.count(link) >= 2


def test_a_failing_sync_is_reported(empty_db, fakes):
    broken = FakeJobTarget(raises=RuntimeError("endpoint moved"))
    app = create_app(db_path=empty_db, sync_runner=broken, launcher=fakes["launcher"])
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        test_client.post("/sync")
        _wait_for(lambda: broken.calls == 1)

        body = text(test_client.get("/sync/status"))

    assert "Sync failed" in body
    assert "endpoint moved" in body


def test_a_second_sync_while_one_runs_is_refused(empty_db, fakes):
    import threading

    gate = threading.Event()

    class Blocking(FakeJobTarget):
        def __call__(self):
            self.calls += 1
            gate.wait(5)
            return ["done"]

    blocking = Blocking()
    app = create_app(db_path=empty_db, sync_runner=blocking, launcher=fakes["launcher"])
    app.config.update(TESTING=True)
    try:
        with app.test_client() as test_client:
            test_client.post("/sync")
            _wait_for(lambda: blocking.calls == 1)

            second = text(test_client.post("/sync"))

        assert "already running" in second
        assert blocking.calls == 1
    finally:
        gate.set()


def _wait_for(condition, timeout=5.0):
    """Wait for a background thread to get somewhere, without a blind sleep."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    raise AssertionError("condition never became true")


# --- connections ----------------------------------------------------------


def test_reconnect_calls_the_injected_launcher(client, fakes):
    response = client.post("/connections/blackboard/reconnect")

    assert response.status_code == 200
    _wait_for(lambda: fakes["launcher"].calls == 1)
    assert fakes["launcher"].calls == 1
    assert "Waiting for the login window" in text(response) or "finished" in text(response)


def test_reconnect_status_renders(client, fakes):
    client.post("/connections/blackboard/reconnect")
    _wait_for(lambda: fakes["launcher"].calls == 1)

    assert client.get("/connections/blackboard/status").status_code == 200


def test_connections_page_does_not_authenticate(client, monkeypatch):
    """Rendering must never log in: that would open a browser or hit the net."""
    from gradetracker.auth.cookie_auth import CookieAuth
    from gradetracker.auth.gradescope_auth import GradescopeAuth

    def explode(*args, **kwargs):
        raise AssertionError("the connections page tried to authenticate")

    monkeypatch.setattr(CookieAuth, "check", explode)
    monkeypatch.setattr(CookieAuth, "session", explode)
    monkeypatch.setattr(GradescopeAuth, "check", explode)
    monkeypatch.setattr(GradescopeAuth, "session", explode)

    assert client.get("/connections").status_code == 200
    assert client.get("/").status_code == 200


def test_gradescope_credentials_are_stored_and_never_echoed(client, fakes, monkeypatch):
    from gradetracker.auth import gradescope_auth

    monkeypatch.setattr(gradescope_auth, "_login", lambda email, password: object())

    response = client.post(
        "/connections/gradescope",
        data={"email": "sstudent@example.edu", "password": "correct horse"},
    )

    body = text(response)
    assert "Gradescope login saved" in body
    assert "correct horse" not in body
    assert gradescope_auth.load_credentials() == ("sstudent@example.edu", "correct horse")


def test_a_rejected_gradescope_password_is_not_stored(client, monkeypatch):
    from gradetracker.auth import gradescope_auth
    from gradetracker.auth.base import AuthExpired

    def refuse(email, password):
        raise AuthExpired("Invalid credentials.")

    monkeypatch.setattr(gradescope_auth, "_login", refuse)

    body = text(
        client.post(
            "/connections/gradescope",
            data={"email": "sstudent@example.edu", "password": "wrong"},
        )
    )

    assert "rejected that login" in body
    assert "wrong" not in body.replace("something went wrong", "")
    assert gradescope_auth.load_credentials() is None


def test_gradescope_form_needs_both_fields(client):
    body = text(client.post("/connections/gradescope", data={"email": "x@y.z"}))

    assert "Both an email and a password" in body


def test_needs_reconnect_shows_a_banner(client, seeded_db):
    conn = db.connect(seeded_db)
    db.record_sync_run(conn, "blackboard", db.now_iso(), "auth_expired", message="expired")
    conn.close()

    body = text(client.get("/"))

    assert "needs reconnecting" in body


def test_a_login_after_the_failure_clears_the_badge_and_the_banner(client, seeded_db):
    """The reconnect worked; only the stale sync_runs row says otherwise."""
    from gradetracker.storage import connections as connections_store

    conn = db.connect(seeded_db)
    db.record_sync_run(conn, "blackboard", db.now_iso(), "auth_expired", message="expired")
    connections_store.record_login(conn, "blackboard")
    conn.close()

    connections_body = text(client.get("/connections"))
    dashboard_body = text(client.get("/"))

    assert "needs reconnect" not in connections_body
    assert "needs reconnecting" not in dashboard_body
    # The detail line says what happened last, not the failure before it. The
    # "Last attempt: auth_expired" row stays: it is still what the last sync did.
    assert "grades refresh on the next sync" in connections_body
    assert ">expired<" not in connections_body


def test_a_login_before_the_failure_still_asks_for_a_reconnect(client, seeded_db):
    """A login from last week must not clear a prompt raised this morning."""
    from gradetracker.storage import connections as connections_store

    conn = db.connect(seeded_db)
    connections_store.record_login(conn, "blackboard")
    db.record_sync_run(conn, "blackboard", db.now_iso(), "auth_expired", message="expired")
    conn.close()

    assert "needs reconnect" in text(client.get("/connections"))
    assert "needs reconnecting" in text(client.get("/"))


def test_a_successful_reconnect_starts_a_sync(client, fakes):
    client.post("/connections/blackboard/reconnect")
    _wait_for(lambda: fakes["launcher"].calls == 1)
    _wait_for(lambda: fakes["sync"].calls == 1)

    assert fakes["sync"].calls == 1
    # The job is only "done" once _reconnect_target returns, a moment after it
    # starts the sync, so the card is polled rather than read once.
    _wait_for(
        lambda: "A sync has started."
        in text(client.get("/connections/blackboard/status"))
    )


# --- links ----------------------------------------------------------------


def test_unlink_then_relink(client, seeded_db):
    conn = db.connect(seeded_db)
    row = conn.execute(
        "SELECT id, course_id FROM source_courses WHERE course_id IS NOT NULL LIMIT 1"
    ).fetchone()
    source_course_id, course_id = int(row["id"]), int(row["course_id"])
    conn.close()

    client.post(f"/link/{source_course_id}", data={"action": "unlink"})
    conn = db.connect(seeded_db)
    assert conn.execute(
        "SELECT course_id FROM source_courses WHERE id = ?", (source_course_id,)
    ).fetchone()["course_id"] is None
    conn.close()

    client.post(f"/link/{source_course_id}", data={"action": "link", "course_id": str(course_id)})
    conn = db.connect(seeded_db)
    try:
        assert conn.execute(
            "SELECT course_id FROM source_courses WHERE id = ?", (source_course_id,)
        ).fetchone()["course_id"] == course_id
    finally:
        conn.close()


def test_create_a_course_from_an_unlinked_source(client, seeded_db):
    conn = db.connect(seeded_db)
    conn.execute(
        """
        INSERT INTO source_courses (source, platform_id, name, code, term, last_seen_at)
        VALUES ('blackboard', '_7777_1', 'PHYS 101: Mechanics', 'PHYS 101', 'Fall 2026', 'now')
        """
    )
    conn.commit()
    new_id = conn.execute(
        "SELECT id FROM source_courses WHERE platform_id = '_7777_1'"
    ).fetchone()["id"]
    conn.close()

    client.post(f"/link/{new_id}", data={"action": "create"})

    conn = db.connect(seeded_db)
    try:
        row = conn.execute("SELECT * FROM courses WHERE code = 'PHYS 101'").fetchone()
        assert row is not None
        assert conn.execute(
            "SELECT course_id FROM source_courses WHERE id = ?", (new_id,)
        ).fetchone()["course_id"] == row["id"]
    finally:
        conn.close()


# --- template smoke -------------------------------------------------------


def test_every_template_renders_with_real_data(client, seeded_db, fakes):
    """A Jinja typo in any template fails here rather than in the browser."""
    paths = ["/", "/connections", "/link", "/healthz"]
    for course_id in course_ids(seeded_db).values():
        paths += [
            f"/courses/{course_id}",
            f"/courses/{course_id}/whatif",
            f"/courses/{course_id}/need",
            f"/courses/{course_id}/rules",
            f"/courses/{course_id}/map",
        ]

    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path

    econ = course_ids(seeded_db)["ECON 166"]
    fragments = [
        ("POST", "/sync", {}),
        ("GET", "/sync/status", None),
        ("POST", f"/courses/{econ}/whatif/calc", {}),
        ("POST", f"/courses/{econ}/need/calc", {"letter": "A-"}),
        ("POST", f"/courses/{econ}/rules/validate", rules_form_data()),
        ("POST", "/connections/blackboard/reconnect", {}),
        ("GET", "/connections/blackboard/status", None),
        ("POST", "/connections/gradescope", {"email": "", "password": ""}),
    ]
    for method, path, data in fragments:
        response = client.post(path, data=data) if method == "POST" else client.get(path)
        assert response.status_code == 200, path

    # not_found.html too.
    assert client.get("/courses/123456").status_code == 404


def test_the_cli_binds_to_localhost_only(monkeypatch):
    """This serves grades with no authentication, so it must not listen widely."""
    from typer.testing import CliRunner

    from gradetracker.cli import app as cli_app

    captured = {}

    class FakeFlask:
        def run(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("gradetracker.web.app.create_app", lambda *a, **k: FakeFlask())
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    result = CliRunner().invoke(cli_app, ["serve", "--no-browser", "--port", "5123"])

    assert result.exit_code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 5123
    assert captured["debug"] is False
    assert opened == []


# --- M8: staleness, changes panel, reconnect records a login --------------


def test_a_course_not_synced_for_over_a_day_warns(client, seeded_db):
    """The PDF: anything not refreshed in more than 24 hours gets a warning."""
    from datetime import datetime, timedelta, timezone

    fresh = text(client.get("/"))
    assert "stale" not in fresh

    conn = db.connect(seeded_db)
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    conn.execute("UPDATE source_courses SET last_seen_at = ?", (old,))
    conn.commit()
    conn.close()

    body = text(client.get("/"))

    assert "stale: synced 25 hours ago" in body


def test_the_dashboard_shows_recent_changes(client, seeded_db):
    from gradetracker.storage import changes as changes_store

    conn = db.connect(seeded_db)
    target = conn.execute(
        "SELECT id FROM assignments WHERE name = 'Problem Set 1' LIMIT 1"
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO changes (assignment_id, kind, old_value, new_value, seen_at)
        VALUES (?, 'score', '18/20', '20/20', ?)
        """,
        (target, db.now_iso()),
    )
    conn.commit()
    conn.close()

    body = text(client.get("/"))

    assert "Recent changes" in body
    assert "18/20" in body
    assert "20/20" in body


def test_the_changes_panel_has_an_empty_state(client):
    body = text(client.get("/"))

    assert "Changes appear here after the second sync" in body


def test_the_reconnect_banner_posts_directly(client, seeded_db):
    conn = db.connect(seeded_db)
    db.record_sync_run(conn, "blackboard", db.now_iso(), "auth_expired", message="expired")
    conn.close()

    body = client.get("/").get_data(as_text=True)

    assert "Reconnect now" in body
    assert "/connections/blackboard/reconnect" in body


# --- reading the page from another device ---------------------------------

TAILNET_IP = "100.89.52.71"
REMOTE_HINT = "Opens the login window on the laptop running gt serve"
LOCAL_HINT = "Opens a real browser window"


@pytest.fixture
def tailnet_client(seeded_db, fakes):
    """The app as `gt serve --tailscale` builds it, with the banner showing.

    Reachable at the tailnet address as well as at loopback, which is what
    lets one app answer both kinds of request.
    """
    conn = db.connect(seeded_db)
    db.record_sync_run(conn, "blackboard", db.now_iso(), "auth_expired", message="expired")
    conn.close()
    app = create_app(
        db_path=seeded_db,
        sync_runner=fakes["sync"],
        launcher=fakes["launcher"],
        extra_hosts=[TAILNET_IP],
    )
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


@pytest.mark.parametrize("path", ["/", "/connections"])
def test_a_phone_is_told_the_login_opens_on_the_laptop(tailnet_client, path):
    """Tapping Reconnect here opens Chromium somewhere else entirely."""
    body = text(tailnet_client.get(path, base_url=f"http://{TAILNET_IP}:5000"))

    assert REMOTE_HINT in body
    assert LOCAL_HINT not in body
    # Still offered: the laptop is usually within reach, and Duo comes to the
    # phone either way.
    assert "/connections/blackboard/reconnect" in body
    assert "disabled" not in body


@pytest.mark.parametrize("path", ["/", "/connections"])
def test_the_laptops_own_browser_keeps_the_plain_wording(tailnet_client, path):
    body = text(tailnet_client.get(path, base_url="http://127.0.0.1:5000"))

    assert LOCAL_HINT in body
    assert REMOTE_HINT not in body


def test_the_port_and_an_ipv6_bracket_do_not_read_as_remote(seeded_db, fakes):
    app = create_app(
        db_path=seeded_db, sync_runner=fakes["sync"], launcher=fakes["launcher"]
    )
    app.config.update(TESTING=True)
    with app.test_client() as client:
        for base in ("http://127.0.0.1:5000", "http://localhost:5000", "http://[::1]:5000"):
            body = text(client.get("/connections", base_url=base))
            assert LOCAL_HINT in body, base
            assert REMOTE_HINT not in body, base


def test_a_successful_reconnect_records_a_login(client, seeded_db, fakes):
    from gradetracker.storage import connections as connections_store

    client.post("/connections/blackboard/reconnect")
    _wait_for(lambda: fakes["launcher"].calls == 1)

    conn = db.connect(seeded_db)
    try:
        _wait_for(
            lambda: connections_store.get(conn, "blackboard") is not None
            and connections_store.get(conn, "blackboard")["last_login_at"] is not None
        )
        row = connections_store.get(conn, "blackboard")
        assert row["state"] == connections_store.CONNECTED
        spans = connections_store.session_spans(conn, "blackboard")
        assert len(spans) == 1
        assert spans[0]["open"] is True
    finally:
        conn.close()


def test_a_failed_reconnect_records_no_login(empty_db, fakes):
    from gradetracker.storage import connections as connections_store

    broken = FakeJobTarget(raises=RuntimeError("Timed out after 5 minutes"))
    app = create_app(db_path=empty_db, sync_runner=fakes["sync"], launcher=broken)
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        test_client.post("/connections/blackboard/reconnect")
        _wait_for(lambda: broken.calls == 1)
        body = text(test_client.get("/connections/blackboard/status"))

    assert "Reconnect failed" in body
    conn = db.connect(empty_db)
    try:
        row = connections_store.get(conn, "blackboard")
        assert row is None or row["last_login_at"] is None
    finally:
        conn.close()


def test_the_connections_page_shows_session_spans(client, seeded_db):
    from gradetracker.storage import connections as connections_store
    from datetime import datetime, timedelta, timezone

    conn = db.connect(seeded_db)
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO session_spans (source, logged_in_at, expired_at) VALUES (?, ?, ?)",
        ("blackboard", (now - timedelta(hours=12)).isoformat(), now.isoformat()),
    )
    conn.commit()
    conn.close()

    body = text(client.get("/connections"))

    assert "How long logins last" in body
    assert "12.0 hours" in body
    assert "OAuth" in body


def test_the_connections_page_shows_the_last_error_and_dump_path(client, seeded_db):
    conn = db.connect(seeded_db)
    db.record_sync_run(
        conn,
        "blackboard",
        db.now_iso(),
        "error",
        raw_path=r"C:\dumps\blackboard\x.json",
        message="Blackboard returned 500 for /columns",
    )
    conn.close()

    body = text(client.get("/connections"))

    assert "Blackboard returned 500 for /columns" in body
    assert "x.json" in body


# --- preferred platform ---------------------------------------------------


def source_of(path, assignment_id):
    conn = db.connect(path)
    try:
        return conn.execute(
            """
            SELECT a.excluded, a.exclusion_source, sc.source
            FROM assignments AS a
            JOIN source_courses AS sc ON sc.id = a.source_course_id
            WHERE a.id = ?
            """,
            (assignment_id,),
        ).fetchone()
    finally:
        conn.close()


def test_the_course_page_offers_a_platform_preference(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]

    body = text(client.get(f"/courses/{econ}"))

    assert "When both platforms carry the same assignment keep:" in body
    assert 'name="preferred_source"' in body


def test_setting_the_preference_excludes_the_twins_at_once(client, seeded_db):
    """Not at the next sync: the choice has to show its effect immediately."""
    econ = course_ids(seeded_db)["ECON 166"]
    twin = assignment_named(seeded_db, "Problem Set 1")

    response = client.post(
        f"/courses/{econ}/preference", data={"preferred_source": "gradescope"}
    )

    assert response.status_code == 200
    from gradetracker.storage import duplicates

    conn = db.connect(seeded_db)
    try:
        assert duplicates.get_preference(conn, econ) == "gradescope"
    finally:
        conn.close()
    row = source_of(seeded_db, twin)
    assert (row["source"], row["excluded"], row["exclusion_source"]) == (
        "blackboard",
        1,
        "auto",
    )


def test_the_chosen_platform_comes_back_selected(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    client.post(f"/courses/{econ}/preference", data={"preferred_source": "blackboard"})

    body = text(client.get(f"/courses/{econ}"))

    assert '<option value="blackboard" selected>' in body


def mirror_within_blackboard(path, course_id):
    """A second Blackboard section carrying a copy of Participation.

    A pair the rule cannot settle: preferring either platform leaves both
    halves on the preferred one, so the card still has to ask.
    """
    conn = db.connect(path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO source_courses (source, platform_id, name, code, term,
                                        course_id, last_seen_at)
            VALUES ('blackboard', '_2199_1', 'ECON 166 lab', 'ECON 166', 'Fall 2026',
                    ?, '2026-10-20T18:00:00+00:00')
            """,
            (course_id,),
        )
        # Same category as the original, or the engine files them apart and
        # never compares them.
        category_id = conn.execute(
            "SELECT category_id FROM assignments WHERE name = 'Participation'"
        ).fetchone()["category_id"]
        conn.execute(
            """
            INSERT INTO assignments (source_course_id, platform_id, name, category_id,
                                     status, earned, possible, last_seen_at)
            VALUES (?, 'lab-participation', 'Participation', ?, 'graded', 45, 50,
                    '2026-10-20T18:00:00+00:00')
            """,
            (int(cursor.lastrowid), category_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_pair_the_rule_cannot_settle_still_asks(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    mirror_within_blackboard(seeded_db, econ)

    body = text(
        client.post(
            f"/courses/{econ}/preference", data={"preferred_source": "gradescope"}
        )
    )

    assert "This course prefers Gradescope" in body
    assert "Possible duplicates" in body
    # Both copies are on Blackboard, so neither was excluded.
    conn = db.connect(seeded_db)
    try:
        kept = conn.execute(
            "SELECT excluded FROM assignments WHERE name = 'Participation'"
        ).fetchall()
    finally:
        conn.close()
    assert [row["excluded"] for row in kept] == [0, 0]


def test_clearing_the_preference_leaves_the_exclusions_alone(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    twin = assignment_named(seeded_db, "Problem Set 1")
    client.post(f"/courses/{econ}/preference", data={"preferred_source": "gradescope"})

    client.post(f"/courses/{econ}/preference", data={"preferred_source": ""})

    from gradetracker.storage import duplicates

    conn = db.connect(seeded_db)
    try:
        assert duplicates.get_preference(conn, econ) is None
    finally:
        conn.close()
    assert source_of(seeded_db, twin)["excluded"] == 1


def test_an_automatic_exclusion_says_so_on_the_row(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]

    body = text(
        client.post(
            f"/courses/{econ}/preference", data={"preferred_source": "gradescope"}
        )
    )

    assert "<span class=\"badge\">auto</span>" in body


def test_including_an_auto_excluded_row_makes_it_the_users_own(client, seeded_db):
    """And so immune to the rule: the next sync must not exclude it again."""
    econ = course_ids(seeded_db)["ECON 166"]
    twin = assignment_named(seeded_db, "Problem Set 1")
    client.post(f"/courses/{econ}/preference", data={"preferred_source": "gradescope"})

    client.post(f"/assignments/{twin}/excluded")

    row = source_of(seeded_db, twin)
    assert (row["excluded"], row["exclusion_source"]) == (0, "user")

    from gradetracker.storage import duplicates

    conn = db.connect(seeded_db)
    try:
        assert duplicates.apply_preference(conn, econ) == []
    finally:
        conn.close()
    assert source_of(seeded_db, twin)["excluded"] == 0


def test_resolving_a_duplicate_by_hand_is_recorded_as_the_users_own(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    pairs = duplicate_pairs_of(seeded_db, econ)
    left_id, right_id = pairs[0]
    left = assignment_row(seeded_db, left_id)

    client.post(
        f"/courses/{econ}/duplicates/resolve",
        data={"keep": left["source"], "pair": f"{left_id}-{right_id}"},
    )

    assert source_of(seeded_db, right_id)["exclusion_source"] == "user"


def test_an_unknown_course_cannot_be_given_a_preference(client, seeded_db):
    response = client.post(
        "/courses/9999/preference", data={"preferred_source": "gradescope"}
    )

    assert response.status_code == 404


# --- the default preference, and seeing what it did -----------------------


def test_the_links_page_says_what_linking_both_platforms_means(client, seeded_db):
    """The notice lands on the page the user is redirected back to."""
    conn = db.connect(seeded_db)
    try:
        conn.execute("UPDATE courses SET preferred_source = NULL")
        source_course = conn.execute(
            """
            SELECT sc.id, sc.course_id FROM source_courses sc
            JOIN courses c ON c.id = sc.course_id
            WHERE c.code = 'ECON 166' AND sc.source = 'gradescope'
            """
        ).fetchone()
        course_id = int(source_course["course_id"])
        conn.execute(
            "UPDATE source_courses SET course_id = NULL WHERE id = ?",
            (source_course["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.post(
        f"/link/{source_course['id']}",
        data={"course_id": str(course_id)},
        follow_redirects=True,
    )

    body = text(response)
    assert "now draws from Blackboard and Gradescope" in body
    assert "marked &#39;auto&#39; under Excluded" in body or "marked 'auto' under Excluded" in body


def test_the_dashboard_shows_an_automatic_exclusion(client, seeded_db):
    econ = course_ids(seeded_db)["ECON 166"]
    client.post(f"/courses/{econ}/preference", data={"preferred_source": "gradescope"})

    body = text(client.get("/"))

    assert "auto: mirrored copy of" in body
    assert "<span class=\"badge warn\">excluded</span>" in body


def test_an_unknown_change_kind_cannot_break_the_panel(client, seeded_db):
    """The CHECK should stop this, but the template must not depend on that."""
    conn = db.connect(seeded_db)
    try:
        assignment = conn.execute("SELECT id FROM assignments LIMIT 1").fetchone()["id"]
        # Straight past the constraint, the way a future migration might.
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute(
            """
            INSERT INTO changes (assignment_id, kind, old_value, new_value, seen_at)
            VALUES (?, 'status', 'ungraded', 'graded', '2026-10-21T10:00:00+00:00')
            """,
            (assignment,),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get("/")

    assert response.status_code == 200
    assert "ungraded" in text(response)
