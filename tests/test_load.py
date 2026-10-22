"""Database -> engine model loading tests (M5).

Seeded the real way: normalise the fixtures, link the courses, then add
categories and a scheme as M6 will.
"""

from __future__ import annotations

import json

import pytest

from gradetracker.engine.calculate import current_grade
from gradetracker.engine.models import DEFAULT_CUTOFFS
from gradetracker.storage import db, link as linking, load
from gradetracker.storage.normalize import normalize_blackboard, normalize_gradescope

from tests.test_normalize import bb_payload, gs_payload


@pytest.fixture
def seeded(tmp_path):
    """A database holding both sources, linked, with ECON 166 fully set up."""
    conn = db.connect(tmp_path / "grades.db")
    normalize_blackboard(conn, bb_payload())
    normalize_gradescope(conn, gs_payload())
    linking.link_auto(conn)

    econ = conn.execute("SELECT id FROM courses WHERE code = 'ECON 166'").fetchone()["id"]
    for name, weight, drop in (
        ("Problem Sets", 40.0, 1),
        ("Quizzes", 10.0, 0),
        ("Midterm", 25.0, 0),
        ("Participation", 25.0, 0),
    ):
        conn.execute(
            """
            INSERT INTO categories (course_id, name, weight, drop_lowest, origin)
            VALUES (?, ?, ?, ?, 'syllabus')
            """,
            (econ, name, weight, drop),
        )
    conn.execute(
        """
        INSERT INTO schemes (course_id, mode, cutoffs, notes, origin, confirmed_at)
        VALUES (?, 'weighted', ?, 'Final may be curved.', 'manual', '2026-10-20T12:00:00+00:00')
        """,
        (econ, json.dumps({"A": 93, "A-": 90, "B+": 87, "B": 83, "F": 0})),
    )
    conn.commit()
    # Re-run so the auto-categoriser sees the new categories.
    normalize_blackboard(conn, bb_payload())
    normalize_gradescope(conn, gs_payload())

    yield conn, econ
    conn.close()


# --- list and find --------------------------------------------------------


def test_list_courses_counts_what_is_attached(seeded):
    conn, _ = seeded

    rows = {row["code"]: row for row in load.list_courses(conn)}

    assert set(rows) == {"ECON 166", "CS 220"}
    # Each course is fed by both platforms.
    assert rows["ECON 166"]["source_course_count"] == 2
    assert rows["ECON 166"]["assignment_count"] > 0


def test_find_course_id_is_forgiving_about_formatting(seeded):
    conn, econ = seeded

    assert load.find_course_id(conn, "ECON 166") == econ
    assert load.find_course_id(conn, "econ-166") == econ
    assert load.find_course_id(conn, "ECON166") == econ
    assert load.find_course_id(conn, "PHYS 101") is None


# --- load_course ----------------------------------------------------------


def test_load_course_assembles_both_platforms(seeded):
    conn, econ = seeded

    model = load.load_course(conn, econ)

    assert model.code == "ECON 166"
    assert len(model.categories) == 4
    # Blackboard's 5 real columns plus Gradescope's 5 assignments.
    assert len(model.assignments) == 10
    names = {a.name for a in model.assignments}
    assert "Problem Set 1" in names  # from both, actually
    assert "Weighted Total" not in names  # calculated column was dropped


def test_load_course_reads_the_scheme(seeded):
    conn, econ = seeded

    model = load.load_course(conn, econ)

    assert model.scheme is not None
    assert model.scheme.mode == "weighted"
    assert model.scheme.notes == "Final may be curved."
    assert model.scheme.cutoffs[0] == ("A", 93.0)
    # Cutoffs come back sorted descending whatever order they were stored in.
    assert [minimum for _, minimum in model.scheme.cutoffs] == [93.0, 90.0, 87.0, 83.0, 0.0]


def test_load_course_reads_categories_with_weights_and_drops(seeded):
    conn, econ = seeded

    model = load.load_course(conn, econ)
    by_name = {category.name: category for category in model.categories}

    assert by_name["Problem Sets"].weight == 40.0
    assert by_name["Problem Sets"].drop_lowest == 1
    assert by_name["Quizzes"].drop_lowest == 0


def test_load_course_maps_statuses_and_points(seeded):
    conn, econ = seeded

    model = load.load_course(conn, econ)
    blackboard = {
        a.name: a
        for a in model.assignments
        if a.possible in (20.0, 10.0, 100.0, 50.0)
    }

    quiz = next(a for a in model.assignments if a.name == "Quiz 1")
    assert quiz.status == "excused"
    midterm = next(a for a in model.assignments if a.name == "Midterm Exam")
    assert midterm.status == "submitted"  # NeedsGrading in Blackboard
    assert midterm.possible == 100.0
    assert blackboard["Participation"].earned == 45.0


def test_load_course_without_a_scheme_returns_none(seeded):
    conn, _ = seeded
    cs = conn.execute("SELECT id FROM courses WHERE code = 'CS 220'").fetchone()["id"]

    model = load.load_course(conn, cs)

    assert model.scheme is None


def test_load_course_for_an_unknown_id(seeded):
    conn, _ = seeded

    assert load.load_course(conn, 9999) is None


def test_load_all_courses(seeded):
    conn, _ = seeded

    courses = load.load_all_courses(conn)

    assert {course.code for course in courses} == {"ECON 166", "CS 220"}


# --- the engine runs on it ------------------------------------------------


def test_the_engine_produces_a_grade_from_the_database(seeded):
    """The PDF's done-when: a real course from the database returns a number."""
    conn, econ = seeded

    model = load.load_course(conn, econ)
    result = current_grade(model)

    assert result.mode == "weighted"
    assert result.percent is not None
    assert 0.0 <= result.percent <= 110.0
    assert result.letter is not None


def test_a_course_with_no_scheme_still_grades_with_a_warning(seeded):
    conn, _ = seeded
    cs = conn.execute("SELECT id FROM courses WHERE code = 'CS 220'").fetchone()["id"]

    result = current_grade(load.load_course(conn, cs))

    assert result.mode == "points"
    assert "no confirmed grading rules" in result.warnings


def test_auto_categorisation_reached_the_loaded_model(seeded):
    conn, econ = seeded

    model = load.load_course(conn, econ)
    by_name = {a.name: a for a in model.assignments}

    problem_sets = next(c for c in model.categories if c.name == "Problem Sets")
    assert by_name["Problem Set 1"].category_id == problem_sets.id
    quizzes = next(c for c in model.categories if c.name == "Quizzes")
    assert by_name["Quiz 1"].category_id == quizzes.id


# --- cutoff parsing -------------------------------------------------------


def test_cutoffs_parse_from_either_json_shape():
    as_dict = load._parse_cutoffs(json.dumps({"A": 93, "B": 83}))
    as_list = load._parse_cutoffs(json.dumps([["A", 93], ["B", 83]]))

    assert as_dict == as_list == (("A", 93.0), ("B", 83.0))


@pytest.mark.parametrize("raw", [None, "", "not json", "[]", "{}", '"text"', "5"])
def test_unusable_cutoffs_fall_back_to_the_standard_scale(raw):
    """A grade with no scale at all cannot be shown, so default rather than fail."""
    assert load._parse_cutoffs(raw) == DEFAULT_CUTOFFS


def test_cutoffs_are_sorted_descending_regardless_of_storage_order():
    parsed = load._parse_cutoffs(json.dumps({"F": 0, "A": 93, "C": 73}))

    assert parsed == (("A", 93.0), ("C", 73.0), ("F", 0.0))
