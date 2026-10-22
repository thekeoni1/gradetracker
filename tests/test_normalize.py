"""Normalizer, schema and linking tests (M4), run against the M2/M3 fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gradetracker.storage import db, link as linking
from gradetracker.storage.normalize import (
    normalize_blackboard,
    normalize_gradescope,
    suggest_category,
)

BB_FIXTURES = Path(__file__).parent / "fixtures" / "blackboard"
GS_FIXTURES = Path(__file__).parent / "fixtures" / "gradescope"


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "grades.db")
    yield connection
    connection.close()


def bb_payload() -> dict:
    """A Blackboard dump shaped the way the connector emits one."""
    memberships = json.loads((BB_FIXTURES / "courses_page1.json").read_text())["results"]
    columns = {
        "_2100_1": json.loads((BB_FIXTURES / "columns_econ166.json").read_text())["results"],
        "_2101_1": json.loads((BB_FIXTURES / "columns_cs220.json").read_text())["results"],
    }
    grades = {
        "_2100_1": json.loads((BB_FIXTURES / "grades_econ166.json").read_text())["results"],
        "_2101_1": json.loads((BB_FIXTURES / "grades_cs220.json").read_text())["results"],
    }
    return {
        "fetched_at": "2026-10-20T18:00:00+00:00",
        "source": "blackboard",
        "user": json.loads((BB_FIXTURES / "me.json").read_text()),
        "courses": [
            {
                **membership,
                "columns": columns[membership["courseId"]],
                "grades": grades[membership["courseId"]],
            }
            for membership in memberships
        ],
        "skipped": [],
    }


def gs_payload() -> dict:
    """A Gradescope dump shaped the way the connector emits one."""
    courses = json.loads((GS_FIXTURES / "courses.json").read_text())["student"]
    current = {cid: c for cid, c in courses.items() if c["year"] == "2026" and c["semester"] == "Fall"}

    def assignments(course_id):
        rows = json.loads((GS_FIXTURES / f"assignments_{course_id}.json").read_text())
        out = []
        for row in rows:
            score = row["grade"]
            out.append(
                {
                    "id": row["assignment_id"],
                    "name": row["name"],
                    "score": score,
                    "max_points": row["max_grade"],
                    "status": "graded" if score is not None else "submitted",
                    "due_date": row["due_date"],
                    "late_due_date": row["late_due_date"],
                    "release_date": row["release_date"],
                    "submitted": True,
                }
            )
        return out

    return {
        "fetched_at": "2026-10-20T18:00:00+00:00",
        "source": "gradescope",
        "term": "Fall 2026",
        "courses": [
            {
                "id": cid,
                "name": course["name"],
                "full_name": course["full_name"],
                "semester": course["semester"],
                "year": course["year"],
                "assignments": assignments(cid),
            }
            for cid, course in current.items()
        ],
        "skipped": [],
    }


def snapshot(conn) -> dict:
    """Every row of the data tables, with last_seen_at removed.

    Idempotency means two runs of the same dump differ only in that column.
    """
    out = {}
    for table in ("source_courses", "assignments", "courses", "categories"):
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        out[table] = [
            {k: row[k] for k in row.keys() if k != "last_seen_at"} for row in rows
        ]
    return out


def assignments_by_name(conn, source="blackboard") -> dict:
    rows = conn.execute(
        """
        SELECT a.* FROM assignments AS a
        JOIN source_courses AS sc ON sc.id = a.source_course_id
        WHERE sc.source = ?
        """,
        (source,),
    ).fetchall()
    return {row["name"]: row for row in rows}


# --- schema ---------------------------------------------------------------


def test_schema_version_is_recorded(conn):
    assert db.schema_version(conn) == db.SCHEMA_VERSION


def test_foreign_keys_are_enforced(conn):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO assignments (source_course_id, platform_id, status, last_seen_at)
            VALUES (9999, 'x', 'graded', 'now')
            """
        )


def test_status_check_constraint_rejects_junk(conn):
    import sqlite3

    source_course_id = _seed_source_course(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO assignments (source_course_id, platform_id, status, last_seen_at)
            VALUES (?, 'x', 'invented', 'now')
            """,
            (source_course_id,),
        )


def _seed_source_course(conn, source="blackboard", platform_id="_1_1") -> int:
    cursor = conn.execute(
        """
        INSERT INTO source_courses (source, platform_id, name, code, term, last_seen_at)
        VALUES (?, ?, 'X', 'ECON 166', 'Fall 2026', 'now')
        """,
        (source, platform_id),
    )
    return int(cursor.lastrowid)


# --- Blackboard normalization ---------------------------------------------


def test_blackboard_counts(conn):
    result = normalize_blackboard(conn, bb_payload())

    assert result.source_courses == 2
    # 6 columns each, minus one Calculated total each.
    assert result.assignments == 10
    assert result.dropped == 2


def test_calculated_columns_are_dropped(conn):
    normalize_blackboard(conn, bb_payload())

    names = set(assignments_by_name(conn))

    assert "Weighted Total" not in names
    assert "Total" not in names
    assert "Problem Set 1" in names


def test_blackboard_status_mapping(conn):
    normalize_blackboard(conn, bb_payload())
    rows = assignments_by_name(conn)

    # A score present.
    assert rows["Problem Set 1"]["status"] == "graded"
    assert rows["Problem Set 1"]["earned"] == 18.0
    assert rows["Problem Set 1"]["possible"] == 20.0
    # exempt = excused, and it keeps no score.
    assert rows["Quiz 1"]["status"] == "excused"
    assert rows["Quiz 1"]["earned"] is None
    # Handed in, not yet marked: Blackboard says NeedsGrading.
    assert rows["Midterm Exam"]["status"] == "submitted"
    assert rows["Midterm Exam"]["possible"] == 100.0


def test_blackboard_reads_score_nested_under_display_grade(conn):
    """Hamilton's Blackboard (seen 2026-10-21) has no top-level score field."""
    payload = bb_payload()
    for grade in payload["courses"][0]["grades"]:
        grade.pop("score", None)
        grade.pop("text", None)
    normalize_blackboard(conn, payload)
    rows = assignments_by_name(conn)

    assert rows["Problem Set 1"]["status"] == "graded"
    assert rows["Problem Set 1"]["earned"] == 18.0


def percent_scale_payload(score=90.0, possible=110.0, column_possible=110.0):
    """bb_payload with Problem Set 1 marked on a percentage scale.

    The real ECON 166 Midterm 1 row (seen 2026-10-21), which is a 110-point
    column scored 90 -> 99/110.
    """
    payload = bb_payload()
    grades = payload["courses"][0]["grades"]
    for grade in grades:
        if grade["columnId"] != "_14001_1":
            continue
        grade.pop("score", None)
        grade.pop("text", None)
        grade["status"] = "Graded"
        display = {"scaleType": "Percent"}
        if score is not None:
            display["score"] = score
        if possible is not None:
            display["possible"] = possible
        grade["displayGrade"] = display
    for column in payload["courses"][0]["columns"]:
        if column["id"] == "_14001_1":
            column["score"] = (
                {"possible": column_possible} if column_possible is not None else {}
            )
    return payload


def test_blackboard_percent_scale_becomes_points(conn):
    """90 on a 110-point column is 99/110, not 90/110."""
    normalize_blackboard(conn, percent_scale_payload())

    row = assignments_by_name(conn)["Problem Set 1"]

    assert row["earned"] == 99.0
    assert row["possible"] == 110.0
    assert row["status"] == "graded"


@pytest.mark.parametrize(
    "score, expected",
    [(90.0, 99.0), (100.0, 110.0), (0.0, 0.0), (81.8182, 90.0)],
)
def test_blackboard_percent_scale_conversions(conn, score, expected):
    normalize_blackboard(conn, percent_scale_payload(score=score))

    assert assignments_by_name(conn)["Problem Set 1"]["earned"] == expected


def test_blackboard_percent_scale_without_a_score_is_ungraded(conn):
    """No number to convert, and no crash either."""
    normalize_blackboard(
        conn, percent_scale_payload(score=None, possible=None, column_possible=None)
    )

    row = assignments_by_name(conn)["Problem Set 1"]

    assert row["earned"] is None
    assert row["possible"] is None
    assert row["status"] == "ungraded"


def test_blackboard_percent_scale_keeps_the_original_in_raw(conn):
    """The untouched grade row stays available for checking the arithmetic."""
    normalize_blackboard(conn, percent_scale_payload())

    raw = json.loads(assignments_by_name(conn)["Problem Set 1"]["raw"])

    assert raw["grade"]["displayGrade"] == {
        "scaleType": "Percent",
        "score": 90.0,
        "possible": 110.0,
    }


def test_blackboard_score_scale_is_left_alone(conn):
    """The ordinary case: scaleType Score is already points."""
    normalize_blackboard(conn, bb_payload())

    rows = assignments_by_name(conn)

    assert rows["Problem Set 1"]["earned"] == 18.0
    assert rows["Problem Set 1"]["possible"] == 20.0
    assert rows["Participation"]["earned"] == 45.0
    assert rows["Participation"]["possible"] == 50.0


def test_blackboard_column_without_grade_row_is_ungraded(conn):
    payload = bb_payload()
    payload["courses"][0]["grades"] = []
    normalize_blackboard(conn, payload)

    assert assignments_by_name(conn)["Problem Set 1"]["status"] == "ungraded"


def test_blackboard_course_metadata(conn):
    normalize_blackboard(conn, bb_payload())

    rows = {
        row["platform_id"]: row
        for row in conn.execute("SELECT * FROM source_courses").fetchall()
    }

    econ = rows["_2100_1"]
    assert econ["code"] == "ECON 166"
    assert econ["term"] == "Fall 2026"
    assert econ["name"] == "ECON 166: Intermediate Microeconomics"
    assert rows["_2101_1"]["code"] == "CS 220"


def test_blackboard_due_dates_are_kept(conn):
    normalize_blackboard(conn, bb_payload())

    assert assignments_by_name(conn)["Problem Set 1"]["due_at"].startswith("2026-09-15")


def test_blackboard_raw_is_stored_per_assignment(conn):
    normalize_blackboard(conn, bb_payload())

    raw = json.loads(assignments_by_name(conn)["Problem Set 1"]["raw"])

    assert raw["column"]["name"] == "Problem Set 1"
    assert raw["grade"]["score"] == 18.0


# --- Gradescope normalization ---------------------------------------------


def test_gradescope_counts(conn):
    result = normalize_gradescope(conn, gs_payload())

    assert result.source_courses == 2
    assert result.assignments == 8  # 5 ECON + 3 CS


def test_gradescope_course_metadata(conn):
    normalize_gradescope(conn, gs_payload())

    rows = {
        row["platform_id"]: row
        for row in conn.execute("SELECT * FROM source_courses").fetchall()
    }

    assert rows["812345"]["code"] == "ECON 166"
    assert rows["812345"]["term"] == "Fall 2026"
    assert rows["812345"]["name"] == "Intermediate Microeconomics"


def test_gradescope_status_passes_through(conn):
    payload = gs_payload()
    payload["courses"][0]["assignments"][0]["status"] = "missing"
    payload["courses"][0]["assignments"][1]["status"] = "submitted"

    normalize_gradescope(conn, payload)
    rows = assignments_by_name(conn, "gradescope")

    assert rows["Problem Set 1"]["status"] == "missing"
    assert rows["Problem Set 2"]["status"] == "submitted"


def test_unknown_status_falls_back_to_ungraded(conn):
    """A new platform status must not violate the CHECK constraint."""
    payload = gs_payload()
    payload["courses"][0]["assignments"][0]["status"] = "something_new"

    normalize_gradescope(conn, payload)

    assert assignments_by_name(conn, "gradescope")["Problem Set 1"]["status"] == "ungraded"


def test_assignment_without_an_id_is_dropped_not_duplicated(conn):
    """No stable key means an upsert would insert a new row every sync."""
    payload = gs_payload()
    payload["courses"][0]["assignments"].append(
        {"id": None, "name": "Unsubmittable", "score": None, "max_points": None, "status": "ungraded"}
    )

    result = normalize_gradescope(conn, payload)

    assert result.dropped == 1
    assert "Unsubmittable" not in assignments_by_name(conn, "gradescope")


def test_both_sources_coexist(conn):
    normalize_blackboard(conn, bb_payload())
    normalize_gradescope(conn, gs_payload())

    counts = dict(
        conn.execute("SELECT source, COUNT(*) FROM source_courses GROUP BY source").fetchall()
    )

    assert counts == {"blackboard": 2, "gradescope": 2}


# --- idempotency ----------------------------------------------------------


def test_blackboard_normalization_is_idempotent(conn):
    payload = bb_payload()
    normalize_blackboard(conn, payload)
    first = snapshot(conn)

    normalize_blackboard(conn, payload)
    second = snapshot(conn)

    assert first == second


def test_gradescope_normalization_is_idempotent(conn):
    payload = gs_payload()
    normalize_gradescope(conn, payload)
    first = snapshot(conn)

    normalize_gradescope(conn, payload)
    second = snapshot(conn)

    assert first == second


def test_resync_updates_a_changed_score_in_place(conn):
    payload = bb_payload()
    normalize_blackboard(conn, payload)
    before = assignments_by_name(conn)["Midterm Exam"]

    # The midterm comes back graded.
    for grade in payload["courses"][0]["grades"]:
        if grade["columnId"] == "_14004_1":
            grade["score"] = 91.0
            grade["status"] = "Graded"
    normalize_blackboard(conn, payload)
    after = assignments_by_name(conn)["Midterm Exam"]

    assert after["id"] == before["id"]  # same row, not a new one
    assert after["earned"] == 91.0
    assert after["status"] == "graded"
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 10


def test_only_last_seen_at_moves_on_a_no_op_resync(conn):
    payload = bb_payload()
    normalize_blackboard(conn, payload)
    first = conn.execute(
        "SELECT last_seen_at FROM assignments ORDER BY id LIMIT 1"
    ).fetchone()["last_seen_at"]

    normalize_blackboard(conn, payload)
    second = conn.execute(
        "SELECT last_seen_at FROM assignments ORDER BY id LIMIT 1"
    ).fetchone()["last_seen_at"]

    assert second >= first


# --- hand-set fields survive sync -----------------------------------------


def test_locked_category_survives_a_resync(conn):
    payload = bb_payload()
    normalize_blackboard(conn, payload)
    linking.link_auto(conn)
    course_id = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()["id"]
    category_id = conn.execute(
        "INSERT INTO categories (course_id, name, origin) VALUES (?, 'Exams', 'manual')",
        (course_id,),
    ).lastrowid
    target = assignments_by_name(conn)["Problem Set 1"]["id"]
    conn.execute(
        "UPDATE assignments SET category_id = ?, category_locked = 1 WHERE id = ?",
        (category_id, target),
    )
    conn.commit()

    normalize_blackboard(conn, payload)

    row = conn.execute("SELECT * FROM assignments WHERE id = ?", (target,)).fetchone()
    assert row["category_id"] == category_id
    assert row["category_locked"] == 1


def test_missing_as_zero_and_extra_credit_survive_a_resync(conn):
    payload = bb_payload()
    normalize_blackboard(conn, payload)
    target = assignments_by_name(conn)["Participation"]["id"]
    conn.execute(
        "UPDATE assignments SET missing_as_zero = 0, extra_credit = 1 WHERE id = ?",
        (target,),
    )
    conn.commit()

    normalize_blackboard(conn, payload)

    row = conn.execute("SELECT * FROM assignments WHERE id = ?", (target,)).fetchone()
    assert row["missing_as_zero"] == 0
    assert row["extra_credit"] == 1


def test_manual_rows_are_never_overwritten(conn):
    """A paper exam you added by hand must survive a platform row with its key."""
    payload = bb_payload()
    normalize_blackboard(conn, payload)
    source_course_id = conn.execute(
        "SELECT id FROM source_courses WHERE platform_id = '_2100_1'"
    ).fetchone()["id"]
    conn.execute(
        """
        UPDATE assignments SET manual = 1, name = 'Paper exam (by hand)', earned = 44
        WHERE source_course_id = ? AND platform_id = '_14001_1'
        """,
        (source_course_id,),
    )
    conn.commit()

    normalize_blackboard(conn, payload)

    row = conn.execute(
        "SELECT * FROM assignments WHERE source_course_id = ? AND platform_id = '_14001_1'",
        (source_course_id,),
    ).fetchone()
    assert row["name"] == "Paper exam (by hand)"
    assert row["earned"] == 44


def test_a_link_survives_a_resync(conn):
    payload = bb_payload()
    normalize_blackboard(conn, payload)
    linking.link_auto(conn)
    before = {
        row["platform_id"]: row["course_id"]
        for row in conn.execute("SELECT platform_id, course_id FROM source_courses")
    }

    normalize_blackboard(conn, payload)

    after = {
        row["platform_id"]: row["course_id"]
        for row in conn.execute("SELECT platform_id, course_id FROM source_courses")
    }
    assert after == before
    assert all(course_id is not None for course_id in after.values())


# --- suggest_category -----------------------------------------------------


CATEGORIES = [
    {"id": 1, "name": "Problem Sets"},
    {"id": 2, "name": "Quizzes"},
    {"id": 3, "name": "Midterm"},
    {"id": 4, "name": "Final Exam"},
    {"id": 5, "name": "Labs"},
    {"id": 6, "name": "Project"},
    {"id": 7, "name": "Participation"},
]


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Problem Set 4", 1),
        ("PS 7", 1),
        ("pset 2", 1),
        ("HW 3", 1),
        ("Homework 9", 1),
        ("Quiz 1", 2),
        ("Midterm Exam", 3),
        ("Final Exam", 4),
        ("Lab 3: Balanced Trees", 5),
        # The exact-category-name stage runs first, so a final project lands in
        # Project rather than Final Exam. That is the better answer.
        ("Final Project", 6),
        ("Participation", 7),
        ("Attendance", 7),
        ("Reading for Thursday", None),
        ("", None),
        (None, None),
    ],
)
def test_suggest_category(name, expected):
    assert suggest_category(name, CATEGORIES) == expected


def test_suggest_category_prefers_an_exact_category_name():
    categories = [{"id": 1, "name": "Exams"}, {"id": 2, "name": "Problem Sets"}]

    assert suggest_category("Exams review sheet", categories) == 1


def test_suggest_category_falls_through_to_a_later_rule():
    """"Final Exam" with no Finals category should still land in Exams."""
    categories = [{"id": 9, "name": "Exams"}]

    assert suggest_category("Final Exam", categories) == 9


def test_suggest_category_with_no_categories_is_none():
    assert suggest_category("Problem Set 1", []) is None


def test_short_triggers_do_not_match_inside_words():
    """"Maps 1" contains "ps " but is not a problem set."""
    assert suggest_category("Maps 1", CATEGORIES) is None


# --- plural category names ------------------------------------------------
#
# Real CPSCI 101 categories, which produced 29 "no suggestion" rows:
# a syllabus names the category in the plural and the assignments in the
# singular, and "codelets" is not a substring of "codelet 00".

CPSCI_CATEGORIES = [
    {"id": 1, "name": "Codelets"},
    {"id": 2, "name": "Programming Projects"},
    {"id": 3, "name": "Labs"},
    {"id": 4, "name": "Midterms"},
    {"id": 5, "name": "Final"},
    {"id": 6, "name": "Attendance and Participation"},
]


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Codelet 00", 1),
        ("Codelet 18", 1),
        ("Programming Project 3", 2),
        # Only one word of the category name, but the distinctive one.
        ("Project 00: Turtles", 2),
        ("Lab 3: Trees", 3),
        # The whole name beats a single word: a programming lab is a lab.
        ("Programming Lab 2", 3),
        ("Midterm 1", 4),
        ("Midterm", 4),
        ("Final", 5),
        ("Attendance 9/15", 6),
        ("Participation", 6),
        # "and" is too short and too common to match on.
        ("Rise and Shine", None),
        ("Syllabus Acknowledgement", None),
    ],
)
def test_suggest_category_handles_plural_category_names(name, expected):
    assert suggest_category(name, CPSCI_CATEGORIES) == expected


def test_a_plural_category_claims_a_final_project_like_a_singular_one():
    """A "Projects" category reads "Final Project" the way "Project" does.

    The longer phrase wins, so the head noun beats the "Final" modifier.
    """
    plural = [{"id": 1, "name": "Final"}, {"id": 2, "name": "Projects"}]
    singular = [{"id": 1, "name": "Final Exam"}, {"id": 2, "name": "Project"}]

    assert suggest_category("Final Project", plural) == 2
    assert suggest_category("Final Project", singular) == 2


def test_a_category_named_final_claims_a_final_project():
    """Documented limit: with CPSCI 101's real names, "Final" wins.

    "Programming Projects" never appears in "Final Project" as a phrase, while
    "Final" does, and a phrase beats a single word. Whether a final project is
    the final or a project is a syllabus question, not a string question, so
    the Map page is where it gets settled.
    """
    assert suggest_category("Final Project", CPSCI_CATEGORIES) == 5


def test_auto_mapping_runs_during_normalization(conn):
    normalize_blackboard(conn, bb_payload())
    linking.link_auto(conn)
    econ = conn.execute(
        """
        SELECT c.id FROM courses AS c
        JOIN source_courses AS sc ON sc.course_id = c.id
        WHERE sc.platform_id = '_2100_1'
        """
    ).fetchone()["id"]
    for name in ("Problem Sets", "Quizzes", "Midterm"):
        conn.execute(
            "INSERT INTO categories (course_id, name, origin) VALUES (?, ?, 'manual')",
            (econ, name),
        )
    conn.commit()

    result = normalize_blackboard(conn, bb_payload())
    rows = assignments_by_name(conn)

    assert result.categorised >= 3
    assert rows["Problem Set 1"]["category_id"] is not None
    assert rows["Quiz 1"]["category_id"] is not None
    assert rows["Participation"]["category_id"] is None  # no matching category


# --- course codes and linking ---------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("ECON-166-01-2026F", "ECON 166"),
        ("ECON 166: Intermediate Microeconomics", "ECON 166"),
        ("ECON 166", "ECON 166"),
        ("CS-220-02-2026F", "CS 220"),
        ("CS 220: Data Structures", "CS 220"),
        ("MATH-113-01-2026S", "MATH 113"),
        ("econ-166", "ECON 166"),
        ("ECON166", "ECON 166"),
        ("CHEM 101A", "CHEM 101A"),
        ("", None),
        (None, None),
        ("No code here", None),
        # Hamilton's real formats, seen 2026-10-21.
        ("CLASC_120_01_FA2026", "CLASC 120"),
        ("MATH_116_PARENT_FA2026", "MATH 116"),
        ("CS101 lab 3", "CS 101"),
        ("FA2026 CALCULUS II (Hamilton College)", None),
        ("Fall 2026 Micro", None),
        ("SP2027 Something", None),
    ],
)
def test_course_code(value, expected):
    assert linking.course_code(value) == expected


def test_course_code_skips_term_tokens_to_later_candidates():
    assert linking.course_code("FA2026 CALCULUS II", "MATH_116_PARENT_FA2026") == "MATH 116"


def test_course_code_tries_candidates_in_order():
    """A courseId is more structured than a free-text name, so it goes first."""
    assert linking.course_code(None, "ECON 166: Micro") == "ECON 166"
    assert linking.course_code("ECON-166-01-2026F", "Fall 2026 Micro") == "ECON 166"


def test_code_key_normalises_for_comparison():
    assert linking.code_key("ECON 166") == linking.code_key("econ-166") == "ECON166"
    assert linking.code_key(None) is None


@pytest.mark.parametrize(
    "course_id, expected",
    [
        ("ECON-166-01-2026F", "Fall 2026"),
        ("MATH-113-01-2026S", "Spring 2026"),
        ("X-1-2026SU", "Summer 2026"),
        ("X-1-2026W", "Winter 2026"),
        ("ECON-166-01", None),
        (None, None),
        ("CLASC_120_01_FA2026", "Fall 2026"),
        ("ECON_166_01_SP2027", "Spring 2027"),
        ("MATH_116_PARENT_FA2026", "Fall 2026"),
    ],
)
def test_term_from_course_id(course_id, expected):
    assert linking.term_from_course_id(course_id) == expected


def test_link_auto_creates_and_then_reuses_a_course(conn):
    """Blackboard and Gradescope ECON 166 must land on one logical course."""
    normalize_blackboard(conn, bb_payload())
    normalize_gradescope(conn, gs_payload())

    actions = linking.link_auto(conn)

    codes = conn.execute("SELECT code, term FROM courses ORDER BY code").fetchall()
    assert [(row["code"], row["term"]) for row in codes] == [
        ("CS 220", "Fall 2026"),
        ("ECON 166", "Fall 2026"),
    ]
    assert len(actions) == 4
    assert sum(1 for a in actions if a["action"] == "created") == 2
    assert sum(1 for a in actions if a["action"] == "linked") == 2

    # Both platforms' ECON 166 point at the same course row.
    linked = conn.execute(
        """
        SELECT sc.source, sc.course_id FROM source_courses AS sc
        JOIN courses AS c ON c.id = sc.course_id
        WHERE c.code = 'ECON 166'
        """
    ).fetchall()
    assert len({row["course_id"] for row in linked}) == 1
    assert {row["source"] for row in linked} == {"blackboard", "gradescope"}


def test_link_auto_is_idempotent(conn):
    normalize_blackboard(conn, bb_payload())
    linking.link_auto(conn)

    second = linking.link_auto(conn)

    assert second == []
    assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 2


def test_link_auto_skips_a_course_with_no_parsable_code(conn):
    conn.execute(
        """
        INSERT INTO source_courses (source, platform_id, name, code, term, last_seen_at)
        VALUES ('blackboard', '_999_1', 'Student Resources Hub', NULL, 'Fall 2026', 'now')
        """
    )
    conn.commit()

    actions = linking.link_auto(conn)

    assert actions == [
        {
            "source_course_id": 1,
            "source": "blackboard",
            "name": "Student Resources Hub",
            "action": "skipped",
            "reason": "no course code could be parsed",
        }
    ]
    assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 0


def test_a_same_code_different_term_course_is_not_reused(conn):
    """Last year's ECON 166 must not absorb this year's assignments."""
    linking.create_course(conn, "ECON 166", "Micro", "Spring 2026")
    normalize_blackboard(conn, bb_payload())

    linking.link_auto(conn)

    terms = [
        row["term"]
        for row in conn.execute("SELECT term FROM courses WHERE code = 'ECON 166' ORDER BY term")
    ]
    assert terms == ["Fall 2026", "Spring 2026"]


def test_list_unlinked_shrinks_as_links_are_made(conn):
    normalize_blackboard(conn, bb_payload())
    assert len(linking.list_unlinked(conn)) == 2

    linking.link_auto(conn)

    assert linking.list_unlinked(conn) == []


def test_create_course_returns_the_existing_row_for_the_same_code_and_term(conn):
    first = linking.create_course(conn, "ECON 166", "Micro", "Fall 2026")
    second = linking.create_course(conn, "econ-166", "Micro again", "Fall 2026")

    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 1


def test_unlink_detaches_without_deleting(conn):
    normalize_blackboard(conn, bb_payload())
    linking.link_auto(conn)
    source_course_id = conn.execute("SELECT id FROM source_courses LIMIT 1").fetchone()["id"]

    linking.unlink(conn, source_course_id)

    row = conn.execute(
        "SELECT course_id FROM source_courses WHERE id = ?", (source_course_id,)
    ).fetchone()
    assert row["course_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 2


# --- sync_runs ------------------------------------------------------------


def test_record_sync_run_round_trip(conn):
    started = db.now_iso()

    db.record_sync_run(conn, "blackboard", started, "ok", raw_path="x.json")

    row = db.last_sync_run(conn, "blackboard")
    assert row["status"] == "ok"
    assert row["raw_path"] == "x.json"
    assert row["started_at"] == started
    assert row["finished_at"] is not None


def test_last_sync_run_is_the_most_recent(conn):
    db.record_sync_run(conn, "blackboard", "2026-10-01T00:00:00+00:00", "error")
    db.record_sync_run(conn, "blackboard", "2026-10-02T00:00:00+00:00", "ok")

    assert db.last_sync_run(conn, "blackboard")["status"] == "ok"


def test_sync_run_status_is_constrained(conn):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        db.record_sync_run(conn, "blackboard", db.now_iso(), "weird")
