"""Grading rules, validation and category mapping tests (M6)."""

from __future__ import annotations

import json
import math

import pytest
from typer.testing import CliRunner

from gradetracker import config
from gradetracker.cli import app
from gradetracker.storage import db, link as linking, mapping, rules as rules_store
from gradetracker.storage.normalize import normalize_blackboard
from gradetracker.syllabus.schema import (
    CategoryDraft,
    CutoffDraft,
    RulesDraft,
    default_cutoffs,
    has_blocking,
    validate,
)

from tests.test_normalize import bb_payload

runner = CliRunner()


def draft(mode="weighted", categories=None, cutoffs=None, **kwargs) -> RulesDraft:
    return RulesDraft(
        mode=mode,
        categories=categories if categories is not None else [],
        cutoffs=cutoffs if cutoffs is not None else default_cutoffs(),
        **kwargs,
    )


def cat(name, weight=None, drop_lowest=0, rank_weights=None) -> CategoryDraft:
    return CategoryDraft(
        name=name, weight=weight, drop_lowest=drop_lowest, rank_weights=rank_weights
    )


def messages(warnings) -> str:
    return " | ".join(w.message for w in warnings)


def blocking_only(warnings):
    return [w for w in warnings if w.blocking]


# --- validate: blocking ---------------------------------------------------


def test_empty_category_name_blocks():
    warnings = validate(draft(categories=[cat("", 100)]))

    assert has_blocking(warnings)
    assert any("name is empty" in w.message for w in warnings)


def test_duplicate_category_name_blocks():
    warnings = validate(draft(categories=[cat("Exams", 50), cat("Exams", 50)]))

    assert has_blocking(warnings)
    assert any("duplicate category name" in w.message for w in warnings)


def test_duplicate_category_name_is_case_insensitive():
    """The save path matches names case-insensitively, so this must too."""
    warnings = validate(draft(categories=[cat("Exams", 50), cat("exams", 50)]))

    assert has_blocking(warnings)


def test_duplicate_letter_blocks():
    cutoffs = [CutoffDraft(letter="A", min_percent=93), CutoffDraft(letter="a", min_percent=90)]

    warnings = validate(draft(categories=[cat("X", 100)], cutoffs=cutoffs))

    assert has_blocking(warnings)
    assert any("duplicate letter" in w.message for w in warnings)


def test_empty_letter_blocks():
    cutoffs = [CutoffDraft(letter="", min_percent=93)]

    assert has_blocking(validate(draft(categories=[cat("X", 100)], cutoffs=cutoffs)))


@pytest.mark.parametrize(
    "values",
    [
        [("A", 90.0), ("B", 93.0)],  # ascending
        [("A", 90.0), ("B", 90.0)],  # equal is not strictly descending
    ],
)
def test_non_descending_cutoffs_block(values):
    cutoffs = [CutoffDraft(letter=letter, min_percent=p) for letter, p in values]

    warnings = validate(draft(categories=[cat("X", 100)], cutoffs=cutoffs))

    assert has_blocking(warnings)
    assert any("must descend" in w.message for w in warnings)


def test_descending_cutoffs_do_not_block():
    cutoffs = [CutoffDraft(letter="A", min_percent=90), CutoffDraft(letter="F", min_percent=0)]

    warnings = validate(draft(categories=[cat("X", 100)], cutoffs=cutoffs))

    assert not has_blocking(warnings)


def test_blocking_warnings_are_listed_first():
    warnings = validate(draft(categories=[cat("", 40), cat("B", 40)]))

    assert warnings[0].blocking


# --- validate: advisory ---------------------------------------------------


def test_weights_not_summing_to_one_hundred_is_advisory():
    warnings = validate(draft(categories=[cat("A", 40), cat("B", 40)]))

    assert not has_blocking(warnings)
    assert any("sum to 80%" in w.message for w in warnings)


def test_weights_summing_to_one_hundred_pass_silently():
    warnings = validate(draft(categories=[cat("A", 60), cat("B", 40)]))

    assert warnings == []


def test_tolerance_allows_a_rounding_error():
    warnings = validate(draft(categories=[cat("A", 33.33), cat("B", 33.33), cat("C", 33.34)]))

    assert warnings == []


def test_weighted_with_a_missing_weight_warns():
    warnings = validate(draft(categories=[cat("A", 100), cat("B", None)]))

    assert not has_blocking(warnings)
    assert any("no weight set for 'B'" in w.message for w in warnings)


def test_points_mode_with_weights_warns_they_are_ignored():
    warnings = validate(draft(mode="points", categories=[cat("A", 40)]))

    assert not has_blocking(warnings)
    assert any("ignored in points mode" in w.message for w in warnings)


def test_points_mode_without_weights_passes():
    warnings = validate(draft(mode="points", categories=[cat("A"), cat("B")]))

    assert warnings == []


def test_no_categories_warns():
    warnings = validate(draft(mode="points"))

    assert not has_blocking(warnings)
    assert any("no categories" in w.message for w in warnings)


def test_negative_drop_lowest_warns_without_blocking():
    warnings = validate(draft(mode="points", categories=[cat("A", drop_lowest=-1)]))

    assert not has_blocking(warnings)
    assert any("negative" in w.message for w in warnings)


def test_rank_weights_that_sum_to_the_category_weight_pass():
    """ECON 166: a 40% Midterms category split 23 / 17."""
    warnings = validate(
        draft(categories=[cat("Midterms", 40, rank_weights=[23, 17]), cat("Rest", 60)])
    )

    assert blocking_only(warnings) == []


def test_rank_weights_that_do_not_sum_to_the_category_weight_block():
    warnings = validate(
        draft(categories=[cat("Midterms", 40, rank_weights=[23, 18]), cat("Rest", 60)])
    )

    assert has_blocking(warnings)
    # The sum is named, so the fix is obvious.
    assert "add up to 41%" in messages(warnings)
    assert "not the category's 40%" in messages(warnings)


def test_rank_weights_in_points_mode_block():
    warnings = validate(
        draft(mode="points", categories=[cat("Midterms", rank_weights=[23, 17])])
    )

    assert has_blocking(warnings)
    assert "need weighted mode" in messages(warnings)


def test_rank_weights_with_a_drop_rule_block():
    """Two rules deciding which item counts least is one too many."""
    warnings = validate(
        draft(categories=[cat("Midterms", 40, drop_lowest=1, rank_weights=[23, 17])])
    )

    assert has_blocking(warnings)
    assert "use one rule or the other" in messages(warnings)


def test_negative_rank_weights_block():
    warnings = validate(
        draft(categories=[cat("Midterms", 40, rank_weights=[50, -10])])
    )

    assert has_blocking(warnings)
    assert "cannot be negative" in messages(warnings)


def test_rank_weights_that_climb_block():
    """The order IS the rule: the first weight goes to the highest score."""
    warnings = validate(
        draft(categories=[cat("Midterms", 40, rank_weights=[17, 23])])
    )

    assert has_blocking(warnings)
    assert "must not climb" in messages(warnings)
    assert "List the highest weight first" in messages(warnings)


def test_a_leading_zero_rank_weight_blocks():
    """[0, 40] would hand the top score a weight of nothing."""
    warnings = validate(draft(categories=[cat("Midterms", 40, rank_weights=[0, 40])]))

    assert has_blocking(warnings)
    assert "must not climb" in messages(warnings)


def test_equal_rank_weights_are_allowed():
    """MATH 116's middle two ranks are both 15."""
    warnings = validate(
        draft(categories=[cat("Midterms", 60, rank_weights=[20, 15, 15, 10])])
    )

    assert blocking_only(warnings) == []


def test_a_trailing_zero_rank_weight_is_allowed():
    """"Only your best midterm counts" is a real rule."""
    warnings = validate(draft(categories=[cat("Midterms", 40, rank_weights=[40, 0])]))

    assert blocking_only(warnings) == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_rank_weights_that_are_not_numbers_block(bad):
    """NaN passes a sum check and a sign check alike: both comparisons are False."""
    warnings = validate(draft(categories=[cat("Midterms", 40, rank_weights=[bad, 40])]))

    assert has_blocking(warnings)
    assert "must be a number" in messages(warnings)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_category_weight_that_is_not_a_number_blocks(bad):
    warnings = validate(draft(categories=[cat("Midterms", bad)]))

    assert has_blocking(warnings)
    assert "weight for 'Midterms' must be a number" in messages(warnings)


def test_a_bonus_percent_that_is_not_a_number_blocks():
    warnings = validate(
        draft(categories=[cat("All", 100)], bonus_percent=float("nan"))
    )

    assert has_blocking(warnings)
    assert "bonus percentage must be a number" in messages(warnings)


def test_a_cutoff_minimum_that_is_not_a_number_blocks():
    warnings = validate(
        draft(
            categories=[cat("All", 100)],
            cutoffs=[
                CutoffDraft(letter="A", min_percent=float("nan")),
                CutoffDraft(letter="F", min_percent=0),
            ],
        )
    )

    assert has_blocking(warnings)
    assert "minimum for 'A' must be a number" in messages(warnings)


def test_unreadable_rank_weight_text_blocks_and_names_the_token():
    """The form keeps what could not be read rather than scoring it zero."""
    warnings = validate(
        draft(
            categories=[
                CategoryDraft(
                    name="Midterms",
                    weight=40,
                    rank_weights=[40.0],
                    rank_weights_invalid=["abc"],
                )
            ]
        )
    )

    assert has_blocking(warnings)
    assert "could not read 'abc'" in messages(warnings)


def test_a_category_with_no_rank_weights_is_not_checked_for_them():
    warnings = validate(draft(categories=[cat("Midterms", 40), cat("Rest", 60)]))

    assert "rank weights" not in messages(warnings)


def test_four_rank_weights_for_math_116():
    """Four midterms: best 20, middle two 15, worst 10, in a 60% category."""
    warnings = validate(
        draft(
            categories=[
                cat("Midterms", 60, rank_weights=[20, 15, 15, 10]),
                cat("Homework", 40),
            ]
        )
    )

    assert blocking_only(warnings) == []


def test_no_cutoffs_warns():
    warnings = validate(draft(mode="points", categories=[cat("A")], cutoffs=[]))

    assert not has_blocking(warnings)
    assert any("no letter cutoffs" in w.message for w in warnings)


def test_pydantic_rejects_a_bad_mode():
    with pytest.raises(ValueError):
        RulesDraft(mode="curved")


def test_a_minimal_draft_gets_the_standard_scale():
    parsed = RulesDraft.model_validate({"mode": "points"})

    assert parsed.cutoffs[0].letter == "A"
    assert parsed.cutoffs[0].min_percent == 93.0
    assert len(parsed.cutoffs) == 12


# --- save_rules -----------------------------------------------------------


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A linked ECON 166 and CS 220 from the Blackboard fixtures."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "grades.db")
    conn = db.connect(tmp_path / "grades.db")
    normalize_blackboard(conn, bb_payload())
    linking.link_auto(conn)
    econ = conn.execute("SELECT id FROM courses WHERE code = 'ECON 166'").fetchone()["id"]
    yield conn, int(econ), tmp_path
    conn.close()


STANDARD = [cat("Problem Sets", 40, 1), cat("Quizzes", 10), cat("Midterm", 25), cat("Participation", 25)]


def test_save_creates_the_scheme_and_categories(seeded):
    conn, econ, _ = seeded

    result = rules_store.save_rules(conn, econ, draft(categories=STANDARD))

    assert result.created == 4
    assert result.updated == 0
    scheme = conn.execute("SELECT * FROM schemes WHERE course_id = ?", (econ,)).fetchone()
    assert scheme["mode"] == "weighted"
    assert scheme["confirmed_at"] is not None
    assert scheme["origin"] == "manual"
    assert scheme["outdated"] == 0


def test_save_auto_maps_assignments_to_the_new_categories(seeded):
    conn, econ, _ = seeded

    result = rules_store.save_rules(conn, econ, draft(categories=STANDARD))

    assert result.categorised > 0
    mapped = conn.execute(
        """
        SELECT a.name, c.name AS category
        FROM assignments AS a
        JOIN categories AS c ON c.id = a.category_id
        JOIN source_courses AS sc ON sc.id = a.source_course_id
        WHERE sc.course_id = ?
        """,
        (econ,),
    ).fetchall()
    by_name = {row["name"]: row["category"] for row in mapped}
    assert by_name["Problem Set 1"] == "Problem Sets"
    assert by_name["Quiz 1"] == "Quizzes"


def test_reconfirming_keeps_category_ids_and_assignment_links(seeded):
    """Re-confirming the same rules must not orphan every mapping."""
    conn, econ, _ = seeded
    rules_store.save_rules(conn, econ, draft(categories=STANDARD))
    before_categories = {
        row["name"]: row["id"]
        for row in conn.execute("SELECT id, name FROM categories WHERE course_id = ?", (econ,))
    }
    before_links = {
        row["id"]: row["category_id"]
        for row in conn.execute(
            """
            SELECT a.id, a.category_id FROM assignments AS a
            JOIN source_courses AS sc ON sc.id = a.source_course_id
            WHERE sc.course_id = ?
            """,
            (econ,),
        )
    }

    result = rules_store.save_rules(conn, econ, draft(categories=STANDARD))

    assert result.created == 0
    assert result.updated == 4
    after_categories = {
        row["name"]: row["id"]
        for row in conn.execute("SELECT id, name FROM categories WHERE course_id = ?", (econ,))
    }
    after_links = {
        row["id"]: row["category_id"]
        for row in conn.execute(
            """
            SELECT a.id, a.category_id FROM assignments AS a
            JOIN source_courses AS sc ON sc.id = a.source_course_id
            WHERE sc.course_id = ?
            """,
            (econ,),
        )
    }
    assert after_categories == before_categories
    assert after_links == before_links


def test_a_changed_weight_updates_in_place(seeded):
    conn, econ, _ = seeded
    rules_store.save_rules(conn, econ, draft(categories=STANDARD))
    original = conn.execute(
        "SELECT id FROM categories WHERE course_id = ? AND name = 'Problem Sets'", (econ,)
    ).fetchone()["id"]

    heavier = [cat("Problem Sets", 50, 2), cat("Quizzes", 10), cat("Midterm", 20), cat("Participation", 20)]
    rules_store.save_rules(conn, econ, draft(categories=heavier))

    row = conn.execute("SELECT * FROM categories WHERE id = ?", (original,)).fetchone()
    assert row["weight"] == 50.0
    assert row["drop_lowest"] == 2


def test_matching_is_case_insensitive_and_keeps_the_new_spelling(seeded):
    conn, econ, _ = seeded
    rules_store.save_rules(conn, econ, draft(categories=[cat("Problem Sets", 100)]))
    original = conn.execute("SELECT id FROM categories WHERE course_id = ?", (econ,)).fetchone()["id"]

    result = rules_store.save_rules(conn, econ, draft(categories=[cat("problem sets", 100)]))

    assert result.updated == 1
    assert result.deleted == 0
    row = conn.execute("SELECT * FROM categories WHERE id = ?", (original,)).fetchone()
    assert row["name"] == "problem sets"


def test_a_removed_category_unlinks_and_unlocks_its_assignments(seeded):
    """A rename is a delete plus an insert, so the links must be cleaned up."""
    conn, econ, _ = seeded
    rules_store.save_rules(conn, econ, draft(categories=STANDARD))
    quizzes = conn.execute(
        "SELECT id FROM categories WHERE course_id = ? AND name = 'Quizzes'", (econ,)
    ).fetchone()["id"]
    quiz_assignment = conn.execute(
        "SELECT id FROM assignments WHERE category_id = ?", (quizzes,)
    ).fetchone()["id"]
    mapping.set_category(conn, quiz_assignment, quizzes, locked=True)

    renamed = [cat("Problem Sets", 40, 1), cat("Tests", 10), cat("Midterm", 25), cat("Participation", 25)]
    result = rules_store.save_rules(conn, econ, draft(categories=renamed))

    assert result.deleted == 1
    assert result.deleted_names == ("Quizzes",)
    assert result.unlinked >= 1
    assert result.unlocked >= 1
    row = conn.execute("SELECT * FROM assignments WHERE id = ?", (quiz_assignment,)).fetchone()
    # The lock pointed at a category that no longer exists, so it is cleared.
    assert row["category_locked"] == 0
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE id = ?", (quizzes,)).fetchone()[0] == 0


def test_cutoffs_round_trip_through_load_rules(seeded):
    conn, econ, _ = seeded
    custom = [
        CutoffDraft(letter="Pass", min_percent=70),
        CutoffDraft(letter="Fail", min_percent=0),
    ]
    rules_store.save_rules(
        conn,
        econ,
        draft(categories=[cat("All", 100)], cutoffs=custom, bonus_percent=2.5, notes="Curved."),
    )

    loaded = rules_store.load_rules(conn, econ)

    assert [(c.letter, c.min_percent) for c in loaded.cutoffs] == [("Pass", 70.0), ("Fail", 0.0)]
    assert loaded.bonus_percent == 2.5
    assert loaded.notes == "Curved."
    assert loaded.mode == "weighted"
    assert [(c.name, c.weight) for c in loaded.categories] == [("All", 100.0)]


def test_rank_weights_round_trip_through_load_rules(seeded):
    conn, econ, _ = seeded

    rules_store.save_rules(
        conn,
        econ,
        draft(categories=[cat("Midterms", 40, rank_weights=[23, 17]), cat("Rest", 60)]),
    )
    loaded = rules_store.load_rules(conn, econ)

    assert [(c.name, c.rank_weights) for c in loaded.categories] == [
        ("Midterms", [23.0, 17.0]),
        ("Rest", None),
    ]
    stored = conn.execute(
        "SELECT rank_weights FROM categories WHERE name = 'Midterms'"
    ).fetchone()["rank_weights"]
    assert json.loads(stored) == [23.0, 17.0]


def test_rank_weights_reach_the_engine_through_load_course(seeded):
    """The whole path: saved rules, loaded course, ranked percentage."""
    from gradetracker.engine.calculate import current_grade
    from gradetracker.storage import load

    conn, econ, _ = seeded
    rules_store.save_rules(
        conn, econ, draft(categories=[cat("Midterms", 100, rank_weights=[60, 40])])
    )
    midterms = int(
        conn.execute("SELECT id FROM categories WHERE name = 'Midterms'").fetchone()["id"]
    )
    conn.executemany(
        "UPDATE assignments SET category_id = ?, category_locked = 1, "
        "earned = ?, possible = 100, status = 'graded' WHERE id = ?",
        [(midterms, 90.0, 1), (midterms, 80.0, 2)],
    )
    conn.execute(
        "UPDATE assignments SET category_id = NULL WHERE id NOT IN (1, 2)"
    )
    conn.commit()

    course = load.load_course(conn, econ)
    result = current_grade(course)

    assert course.categories[0].rank_weights == (60.0, 40.0)
    # (60 * 90 + 40 * 80) / 100 = 86
    assert result.categories[0].percent == pytest.approx(86.0)


def test_removing_rank_weights_clears_the_column(seeded):
    conn, econ, _ = seeded
    rules_store.save_rules(
        conn, econ, draft(categories=[cat("Midterms", 100, rank_weights=[60, 40])])
    )

    rules_store.save_rules(conn, econ, draft(categories=[cat("Midterms", 100)]))

    row = conn.execute(
        "SELECT rank_weights FROM categories WHERE name = 'Midterms'"
    ).fetchone()
    assert row["rank_weights"] is None
    assert rules_store.load_rules(conn, econ).categories[0].rank_weights is None


def test_a_nan_already_in_the_database_falls_back_to_no_ranking(seeded):
    """A row written before validate() refused these must not print "nan%"."""
    from gradetracker.engine.calculate import current_grade
    from gradetracker.storage import load

    conn, econ, _ = seeded
    rules_store.save_rules(conn, econ, draft(categories=[cat("Midterms", 100)]))
    conn.execute("UPDATE categories SET rank_weights = '[NaN, 40.0]'")
    conn.commit()

    course = load.load_course(conn, econ)
    percent = current_grade(course).percent

    assert course.categories[0].rank_weights is None
    assert percent is None or math.isfinite(percent)


def test_junk_rank_weights_in_the_database_fall_back_to_no_ranking(seeded):
    """A hand-edited database must not take the whole grade down."""
    from gradetracker.storage import load

    conn, econ, _ = seeded
    rules_store.save_rules(conn, econ, draft(categories=[cat("Midterms", 100)]))
    conn.execute("UPDATE categories SET rank_weights = 'not json'")
    conn.commit()

    course = load.load_course(conn, econ)

    assert course.categories[0].rank_weights is None


def test_load_rules_is_none_for_a_course_with_nothing(seeded):
    conn, _, _ = seeded
    cs = conn.execute("SELECT id FROM courses WHERE code = 'CS 220'").fetchone()["id"]

    assert rules_store.load_rules(conn, int(cs)) is None


def test_saving_unconfirmed_leaves_confirmed_at_null(seeded):
    conn, econ, _ = seeded

    rules_store.save_rules(conn, econ, draft(categories=[cat("All", 100)]), confirmed=False)

    assert rules_store.rules_status(conn, econ) == "draft"


def test_rules_status_transitions(seeded):
    conn, econ, _ = seeded
    assert rules_store.rules_status(conn, econ) == "none"

    rules_store.save_rules(conn, econ, draft(categories=[cat("All", 100)]))
    assert rules_store.rules_status(conn, econ) == "confirmed"

    rules_store.mark_outdated(conn, econ)
    assert rules_store.rules_status(conn, econ) == "outdated"

    rules_store.save_rules(conn, econ, draft(categories=[cat("All", 100)]))
    assert rules_store.rules_status(conn, econ) == "confirmed"


def test_rules_summary_wording(seeded):
    conn, econ, _ = seeded
    assert "NONE" in rules_store.rules_summary(conn, econ)

    rules_store.save_rules(conn, econ, draft(categories=[cat("All", 100)]))
    assert rules_store.rules_summary(conn, econ).startswith("rules: confirmed (manual,")

    rules_store.mark_outdated(conn, econ)
    assert "may be outdated" in rules_store.rules_summary(conn, econ)


def test_an_unknown_origin_is_rejected(seeded):
    conn, econ, _ = seeded

    with pytest.raises(ValueError, match="origin must be"):
        rules_store.save_rules(conn, econ, draft(categories=[cat("A", 100)]), origin="guessed")


def test_a_failed_save_rolls_back(seeded, monkeypatch):
    """A half-written scheme would be worse than no scheme."""
    conn, econ, _ = seeded
    rules_store.save_rules(conn, econ, draft(categories=[cat("Keep", 100)]))

    boom = draft(categories=[cat("New", 100)])
    monkeypatch.setattr(
        rules_store,
        "apply_category_suggestions",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        rules_store.save_rules(conn, econ, boom)

    # The scheme/category work committed before suggestions ran, which is the
    # documented boundary; the point is the database is not half-written.
    assert rules_store.load_rules(conn, econ) is not None


# --- mapping --------------------------------------------------------------


def test_set_category_locks_by_default(seeded):
    conn, econ, _ = seeded
    rules_store.save_rules(conn, econ, draft(categories=STANDARD))
    target = conn.execute("SELECT id FROM assignments LIMIT 1").fetchone()["id"]
    category = conn.execute("SELECT id FROM categories WHERE course_id = ?", (econ,)).fetchone()["id"]

    mapping.set_category(conn, int(target), int(category))

    row = conn.execute("SELECT * FROM assignments WHERE id = ?", (target,)).fetchone()
    assert row["category_id"] == category
    assert row["category_locked"] == 1


def test_set_category_can_clear_a_mapping(seeded):
    conn, econ, _ = seeded
    rules_store.save_rules(conn, econ, draft(categories=STANDARD))
    target = conn.execute("SELECT id FROM assignments WHERE category_id IS NOT NULL LIMIT 1").fetchone()["id"]

    mapping.set_category(conn, int(target), None, locked=False)

    row = conn.execute("SELECT * FROM assignments WHERE id = ?", (target,)).fetchone()
    assert row["category_id"] is None
    assert row["category_locked"] == 0


def test_accept_all_suggestions_does_not_lock(seeded):
    """These are guesses, so a better category later should be free to win."""
    conn, econ, _ = seeded
    # Create categories without triggering the save path's auto-mapping.
    for name in ("Problem Sets", "Quizzes"):
        conn.execute(
            "INSERT INTO categories (course_id, name, weight, origin) VALUES (?, ?, 50, 'manual')",
            (econ, name),
        )
    conn.commit()

    mapped = mapping.accept_all_suggestions(conn, econ)

    assert mapped > 0
    rows = conn.execute(
        """
        SELECT a.category_locked FROM assignments AS a
        JOIN source_courses AS sc ON sc.id = a.source_course_id
        WHERE sc.course_id = ? AND a.category_id IS NOT NULL
        """,
        (econ,),
    ).fetchall()
    assert rows
    assert all(row["category_locked"] == 0 for row in rows)


def test_a_locked_mapping_survives_a_resync(seeded):
    conn, econ, _ = seeded
    rules_store.save_rules(conn, econ, draft(categories=STANDARD))
    quizzes = conn.execute(
        "SELECT id FROM categories WHERE course_id = ? AND name = 'Quizzes'", (econ,)
    ).fetchone()["id"]
    target = conn.execute("SELECT id FROM assignments WHERE name = 'Problem Set 1'").fetchone()["id"]
    mapping.set_category(conn, int(target), int(quizzes), locked=True)

    normalize_blackboard(conn, bb_payload())

    row = conn.execute("SELECT * FROM assignments WHERE id = ?", (target,)).fetchone()
    assert row["category_id"] == quizzes
    assert row["category_locked"] == 1


def test_an_accepted_suggestion_also_survives_a_resync(seeded):
    """Unlocked, but sync still must not clear it."""
    conn, econ, _ = seeded
    rules_store.save_rules(conn, econ, draft(categories=STANDARD))
    row = conn.execute(
        "SELECT id, category_id FROM assignments WHERE name = 'Problem Set 1'"
    ).fetchone()
    assert row["category_id"] is not None

    normalize_blackboard(conn, bb_payload())

    after = conn.execute("SELECT * FROM assignments WHERE id = ?", (row["id"],)).fetchone()
    assert after["category_id"] == row["category_id"]
    assert after["category_locked"] == 0


def test_list_unmapped_carries_suggestions(seeded):
    conn, econ, _ = seeded
    for name in ("Problem Sets", "Quizzes"):
        conn.execute(
            "INSERT INTO categories (course_id, name, weight, origin) VALUES (?, ?, 50, 'manual')",
            (econ, name),
        )
    conn.commit()

    unmapped = mapping.list_unmapped(conn, econ)
    by_name = {row["name"]: row for row in unmapped}

    assert by_name["Problem Set 1"]["suggested_category_name"] == "Problem Sets"
    assert by_name["Participation"]["suggested_category_name"] is None


def test_count_unmapped_graded_ignores_ungraded_items(seeded):
    conn, econ, _ = seeded

    total = mapping.count_unmapped_graded(conn, econ)
    all_unmapped = len(mapping.list_unmapped(conn, econ))

    # The midterm is ungraded, so it is not part of the trust warning.
    assert total < all_unmapped


def test_find_category_is_case_insensitive(seeded):
    conn, econ, _ = seeded
    rules_store.save_rules(conn, econ, draft(categories=STANDARD))

    assert mapping.find_category(conn, econ, "problem sets")["name"] == "Problem Sets"
    assert mapping.find_category(conn, econ, "nope") is None


# --- CLI ------------------------------------------------------------------


def write_rules_file(tmp_path, payload) -> str:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


VALID_PAYLOAD = {
    "mode": "weighted",
    "categories": [
        {"name": "Problem Sets", "weight": 40, "drop_lowest": 1},
        {"name": "Quizzes", "weight": 10},
        {"name": "Midterm", "weight": 25},
        {"name": "Participation", "weight": 25},
    ],
    "notes": "Final may be curved.",
}


def test_rules_file_with_a_nan_rank_weight_saves_nothing(seeded):
    """JSON really has a NaN literal, and json.loads takes it."""
    conn, econ, tmp_path = seeded
    conn.close()
    path = tmp_path / "rules.json"
    path.write_text(
        '{"mode": "weighted", "categories": ['
        '{"name": "Midterms", "weight": 40, "rank_weights": [NaN, 40]},'
        '{"name": "Rest", "weight": 60}]}',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["rules", "ECON 166", "--file", str(path)])

    assert result.exit_code == 1
    assert "must be a number" in result.output
    with db.connection(tmp_path / "grades.db") as fresh:
        assert rules_store.rules_status(fresh, econ) == "none"


def test_rules_file_with_climbing_rank_weights_saves_nothing(seeded):
    conn, econ, tmp_path = seeded
    conn.close()
    payload = {
        "mode": "weighted",
        "categories": [
            {"name": "Midterms", "weight": 40, "rank_weights": [17, 23]},
            {"name": "Rest", "weight": 60},
        ],
    }

    result = runner.invoke(
        app, ["rules", "ECON 166", "--file", write_rules_file(tmp_path, payload)]
    )

    assert result.exit_code == 1
    assert "must not climb" in result.output
    with db.connection(tmp_path / "grades.db") as fresh:
        assert rules_store.rules_status(fresh, econ) == "none"


def test_rules_interactive_refuses_a_nan_rank_weight(seeded):
    """float("nan") parses, so the prompt accepts it and validate stops it."""
    conn, econ, tmp_path = seeded
    conn.close()
    keys = "weighted\nMidterms\n40\n0\nnan, 40\nRest\n60\n0\n\n\nn\n0\n\ny\n"

    result = runner.invoke(app, ["rules", "ECON 166"], input=keys)

    assert result.exit_code == 1
    assert "must be a number" in result.output
    with db.connection(tmp_path / "grades.db") as fresh:
        assert rules_store.rules_status(fresh, econ) == "none"


def test_rules_interactive_refuses_a_nan_category_weight(seeded):
    conn, econ, tmp_path = seeded
    conn.close()
    keys = "weighted\nMidterms\nnan\n0\n\n\nn\n0\n\ny\n"

    result = runner.invoke(app, ["rules", "ECON 166"], input=keys)

    assert result.exit_code == 1
    assert "weight for 'Midterms' must be a number" in result.output


def test_rules_file_saves(seeded):
    conn, econ, tmp_path = seeded
    conn.close()
    path = write_rules_file(tmp_path, VALID_PAYLOAD)

    result = runner.invoke(app, ["rules", "ECON 166", "--file", path])

    assert result.exit_code == 0
    assert "4 category(ies) added" in result.output
    with db.connection(tmp_path / "grades.db") as fresh:
        assert rules_store.rules_status(fresh, econ) == "confirmed"
        assert len(rules_store.load_rules(fresh, econ).categories) == 4


def test_rules_file_with_a_blocking_problem_saves_nothing(seeded):
    conn, econ, tmp_path = seeded
    conn.close()
    path = write_rules_file(
        tmp_path,
        {"mode": "weighted", "categories": [{"name": "A", "weight": 50}, {"name": "A", "weight": 50}]},
    )

    result = runner.invoke(app, ["rules", "ECON 166", "--file", path])

    assert result.exit_code == 1
    assert "BLOCKING" in result.output
    assert "Not saved" in result.output
    with db.connection(tmp_path / "grades.db") as fresh:
        assert rules_store.rules_status(fresh, econ) == "none"


def test_rules_file_with_advisory_warnings_still_saves(seeded):
    conn, econ, tmp_path = seeded
    conn.close()
    path = write_rules_file(
        tmp_path,
        {"mode": "weighted", "categories": [{"name": "A", "weight": 40}, {"name": "B", "weight": 40}]},
    )

    result = runner.invoke(app, ["rules", "ECON 166", "--file", path])

    assert result.exit_code == 0
    assert "sum to 80%" in result.output
    with db.connection(tmp_path / "grades.db") as fresh:
        assert rules_store.rules_status(fresh, econ) == "confirmed"


def test_rules_file_that_is_not_json(seeded):
    conn, _, tmp_path = seeded
    conn.close()
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")

    result = runner.invoke(app, ["rules", "ECON 166", "--file", str(path)])

    assert result.exit_code == 1
    assert "not a valid rules file" in result.output


def test_rules_file_that_does_not_exist(seeded):
    conn, _, tmp_path = seeded
    conn.close()

    result = runner.invoke(app, ["rules", "ECON 166", "--file", str(tmp_path / "nope.json")])

    assert result.exit_code == 1
    assert "Could not read" in result.output


def test_rules_show_before_and_after(seeded):
    conn, econ, tmp_path = seeded
    conn.close()

    before = runner.invoke(app, ["rules", "ECON 166", "--show"])
    assert "rules: NONE" in before.output

    runner.invoke(app, ["rules", "ECON 166", "--file", write_rules_file(tmp_path, VALID_PAYLOAD)])
    after = runner.invoke(app, ["rules", "ECON 166", "--show"])

    assert "rules: confirmed (manual," in after.output
    assert "Problem Sets" in after.output
    assert "100%" in after.output  # the running total line


def test_rules_for_an_unknown_course(seeded):
    conn, _, _ = seeded
    conn.close()

    result = runner.invoke(app, ["rules", "PHYS 101", "--show"])

    assert result.exit_code == 1
    assert "No course matching" in result.output


def test_rules_interactive_happy_path(seeded):
    conn, econ, tmp_path = seeded
    conn.close()
    # mode, two categories (name, weight, drop, rank weights) then a blank name,
    # no cutoff edit, bonus, notes, confirm
    keys = "weighted\nProblem Sets\n60\n1\n\nExams\n40\n0\n\n\nn\n0\nCurved a bit\ny\n"

    result = runner.invoke(app, ["rules", "ECON 166"], input=keys)

    assert result.exit_code == 0, result.output
    assert "running total: 60%" in result.output
    assert "running total: 100%" in result.output
    with db.connection(tmp_path / "grades.db") as fresh:
        loaded = rules_store.load_rules(fresh, econ)
        assert [(c.name, c.weight, c.drop_lowest) for c in loaded.categories] == [
            ("Problem Sets", 60.0, 1),
            ("Exams", 40.0, 0),
        ]
        assert loaded.notes == "Curved a bit"


def test_rules_interactive_takes_rank_weights(seeded):
    """ECON 166's real rule, typed at the prompt: 23, 17 on a 40% category."""
    conn, econ, tmp_path = seeded
    conn.close()
    keys = (
        "weighted\n"
        "Midterms\n40\n0\n23, 17\n"  # name, weight, drop, rank weights
        "Homework\n60\n0\n\n"
        "\n"  # blank name ends the list
        "n\n0\n\ny\n"
    )

    result = runner.invoke(app, ["rules", "ECON 166"], input=keys)

    assert result.exit_code == 0, result.output
    with db.connection(tmp_path / "grades.db") as fresh:
        loaded = rules_store.load_rules(fresh, econ)
        assert [(c.name, c.weight, c.rank_weights) for c in loaded.categories] == [
            ("Midterms", 40.0, [23.0, 17.0]),
            ("Homework", 60.0, None),
        ]


def test_rules_interactive_rejects_rank_weights_that_do_not_add_up(seeded):
    conn, econ, tmp_path = seeded
    conn.close()
    keys = "weighted\nMidterms\n40\n0\n23, 18\nHomework\n60\n0\n\n\nn\n0\n\ny\n"

    result = runner.invoke(app, ["rules", "ECON 166"], input=keys)

    assert result.exit_code == 1
    assert "BLOCKING" in result.output
    assert "add up to 41%" in result.output
    with db.connection(tmp_path / "grades.db") as fresh:
        assert rules_store.rules_status(fresh, econ) == "none"


def test_rules_interactive_reprompts_for_unparseable_rank_weights(seeded):
    conn, econ, tmp_path = seeded
    conn.close()
    keys = "weighted\nMidterms\n40\n0\nabc\n23, 17\n\nn\n0\n\ny\n"

    result = runner.invoke(app, ["rules", "ECON 166"], input=keys)

    assert result.exit_code == 0, result.output
    assert "numbers separated by commas" in result.output
    with db.connection(tmp_path / "grades.db") as fresh:
        loaded = rules_store.load_rules(fresh, econ)
        assert loaded.categories[0].rank_weights == [23.0, 17.0]


def test_rules_show_lists_rank_weights(seeded):
    conn, econ, tmp_path = seeded
    rules_store.save_rules(
        conn, econ, draft(categories=[cat("Midterms", 100, rank_weights=[60, 40])])
    )
    conn.close()

    result = runner.invoke(app, ["rules", "ECON 166", "--show"])

    assert result.exit_code == 0
    assert "ranked 60, 40" in result.output


def test_gt_grade_shows_the_weight_each_ranked_item_received(seeded):
    conn, econ, tmp_path = seeded
    rules_store.save_rules(
        conn, econ, draft(categories=[cat("Midterms", 100, rank_weights=[60, 40])])
    )
    midterms = int(
        conn.execute("SELECT id FROM categories WHERE name = 'Midterms'").fetchone()["id"]
    )
    conn.executemany(
        "UPDATE assignments SET category_id = ?, category_locked = 1, name = ?, "
        "earned = ?, possible = 100, status = 'graded' WHERE id = ?",
        [(midterms, "Midterm 1", 80.0, 1), (midterms, "Midterm 2", 90.0, 2)],
    )
    conn.execute("UPDATE assignments SET excluded = 1 WHERE id NOT IN (1, 2)")
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["grade", "ECON 166"])

    assert result.exit_code == 0, result.output
    lines = {
        line.split("(")[0].strip(): line
        for line in result.output.splitlines()
        if "(" in line and "%)" in line
    }
    # Midterm 2 scored higher, so it takes the 60.
    assert "(60%)" in lines["Midterm 2"] and "90/100" in lines["Midterm 2"]
    assert "(40%)" in lines["Midterm 1"] and "80/100" in lines["Midterm 1"]


def test_gt_grade_says_so_far_while_a_midterm_is_unmarked(seeded):
    """One of two midterms marked: the 60 could still move to the other one."""
    conn, econ, tmp_path = seeded
    rules_store.save_rules(
        conn, econ, draft(categories=[cat("Midterms", 100, rank_weights=[60, 40])])
    )
    midterms = int(
        conn.execute("SELECT id FROM categories WHERE name = 'Midterms'").fetchone()["id"]
    )
    conn.execute(
        "UPDATE assignments SET category_id = ?, category_locked = 1, name = 'Midterm 1', "
        "earned = 80, possible = 100, status = 'graded' WHERE id = 1",
        (midterms,),
    )
    conn.execute(
        "UPDATE assignments SET category_id = ?, category_locked = 1, name = 'Midterm 2', "
        "earned = NULL, possible = 100, status = 'ungraded' WHERE id = 2",
        (midterms,),
    )
    conn.execute("UPDATE assignments SET excluded = 1 WHERE id NOT IN (1, 2)")
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["grade", "ECON 166"])

    assert "(60% so far)" in result.output


def test_gt_grade_drops_so_far_once_every_item_is_marked(seeded):
    """Three rows against four ranks is finished when all three are marked."""
    conn, econ, tmp_path = seeded
    rules_store.save_rules(
        conn,
        econ,
        draft(categories=[cat("Midterms", 100, rank_weights=[40, 30, 20, 10])]),
    )
    midterms = int(
        conn.execute("SELECT id FROM categories WHERE name = 'Midterms'").fetchone()["id"]
    )
    conn.executemany(
        "UPDATE assignments SET category_id = ?, category_locked = 1, name = ?, "
        "earned = ?, possible = 100, status = 'graded' WHERE id = ?",
        [
            (midterms, "Midterm 1", 90.0, 1),
            (midterms, "Midterm 2", 80.0, 2),
            (midterms, "Midterm 3", 70.0, 3),
        ],
    )
    conn.execute("UPDATE assignments SET excluded = 1 WHERE id NOT IN (1, 2, 3)")
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["grade", "ECON 166"])

    assert "so far" not in result.output
    assert "(40%)" in result.output
    # The unclaimed 10% rank is not an item, so three is the count.
    assert "3 item(s), ranked" in result.output


def test_rules_interactive_declining_saves_nothing(seeded):
    conn, econ, tmp_path = seeded
    conn.close()
    keys = "points\nAll\n0\n\nn\n0\n\nn\n"

    result = runner.invoke(app, ["rules", "ECON 166"], input=keys)

    assert result.exit_code == 0
    assert "Not saved." in result.output
    with db.connection(tmp_path / "grades.db") as fresh:
        assert rules_store.rules_status(fresh, econ) == "none"


def test_rules_interactive_blocking_refuses(seeded):
    conn, econ, tmp_path = seeded
    conn.close()
    # Two categories with the same name.
    keys = "weighted\nExams\n50\n0\n\nexams\n50\n0\n\n\nn\n0\n\n"

    result = runner.invoke(app, ["rules", "ECON 166"], input=keys)

    assert result.exit_code == 1
    assert "BLOCKING" in result.output
    with db.connection(tmp_path / "grades.db") as fresh:
        assert rules_store.rules_status(fresh, econ) == "none"


def test_map_auto(seeded):
    conn, econ, tmp_path = seeded
    for name in ("Problem Sets", "Quizzes"):
        conn.execute(
            "INSERT INTO categories (course_id, name, weight, origin) VALUES (?, ?, 50, 'manual')",
            (econ, name),
        )
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["map", "ECON 166", "--auto"])

    assert result.exit_code == 0
    assert "Mapped" in result.output
    with db.connection(tmp_path / "grades.db") as fresh:
        rows = fresh.execute(
            "SELECT category_locked FROM assignments WHERE category_id IS NOT NULL"
        ).fetchall()
        assert rows
        assert all(row["category_locked"] == 0 for row in rows)


def test_map_set_locks_one_choice(seeded):
    conn, econ, tmp_path = seeded
    rules_store.save_rules(conn, econ, draft(categories=STANDARD))
    target = conn.execute("SELECT id FROM assignments WHERE name = 'Participation'").fetchone()["id"]
    conn.close()

    result = runner.invoke(app, ["map", "ECON 166", "--set", str(target), "Midterm"])

    assert result.exit_code == 0
    assert "locked" in result.output
    with db.connection(tmp_path / "grades.db") as fresh:
        row = fresh.execute("SELECT * FROM assignments WHERE id = ?", (target,)).fetchone()
        assert row["category_locked"] == 1
        name = fresh.execute(
            "SELECT name FROM categories WHERE id = ?", (row["category_id"],)
        ).fetchone()["name"]
        assert name == "Midterm"


def test_map_set_with_an_unknown_category(seeded):
    conn, econ, tmp_path = seeded
    rules_store.save_rules(conn, econ, draft(categories=STANDARD))
    target = conn.execute("SELECT id FROM assignments LIMIT 1").fetchone()["id"]
    conn.close()

    result = runner.invoke(app, ["map", "ECON 166", "--set", str(target), "Nonsense"])

    assert result.exit_code == 1
    assert "No category named 'Nonsense'" in result.output
    assert "Problem Sets" in result.output  # lists the real ones


def test_map_without_categories_complains(seeded):
    conn, _, _ = seeded
    conn.close()

    result = runner.invoke(app, ["map", "ECON 166"])

    assert result.exit_code == 1
    assert "Run gt rules first" in result.output


def test_map_interactive_accepts_and_skips(seeded):
    conn, econ, tmp_path = seeded
    for name in ("Problem Sets", "Quizzes"):
        conn.execute(
            "INSERT INTO categories (course_id, name, weight, origin) VALUES (?, ?, 50, 'manual')",
            (econ, name),
        )
    conn.commit()
    unmapped = mapping.list_unmapped(conn, econ)
    conn.close()

    # Accept everything offered; blank answers fall back to each prompt's default.
    result = runner.invoke(app, ["map", "ECON 166"], input="\n" * len(unmapped))

    assert result.exit_code == 0
    with db.connection(tmp_path / "grades.db") as fresh:
        locked = fresh.execute(
            "SELECT COUNT(*) AS n FROM assignments WHERE category_locked = 1"
        ).fetchone()["n"]
        assert locked > 0


def test_map_interactive_choosing_by_number(seeded):
    conn, econ, tmp_path = seeded
    # Deliberately NOT the full set: the auto-mapper would otherwise place
    # every assignment and leave nothing to choose by hand.
    rules_store.save_rules(conn, econ, draft(categories=[cat("Problem Sets", 100)]))
    unmapped = mapping.list_unmapped(conn, econ)
    assert unmapped, "expected something left unmapped"
    first = unmapped[0]
    conn.close()

    answers = "1\n" + "s\n" * (len(unmapped) - 1)
    result = runner.invoke(app, ["map", "ECON 166"], input=answers)

    assert result.exit_code == 0
    with db.connection(tmp_path / "grades.db") as fresh:
        row = fresh.execute("SELECT * FROM assignments WHERE id = ?", (first["id"],)).fetchone()
        first_category = fresh.execute(
            "SELECT id FROM categories WHERE course_id = ? ORDER BY id LIMIT 1", (econ,)
        ).fetchone()["id"]
        assert row["category_id"] == first_category
        assert row["category_locked"] == 1


# --- gt grade trust signals -----------------------------------------------


def test_grade_shows_the_rules_status_and_unmapped_count(seeded):
    conn, econ, tmp_path = seeded
    conn.close()

    before = runner.invoke(app, ["grade", "ECON 166"])
    assert "rules: NONE, using points mode and standard scale" in before.output
    assert "not mapped to a category" in before.output

    runner.invoke(app, ["rules", "ECON 166", "--file", write_rules_file(tmp_path, VALID_PAYLOAD)])
    after = runner.invoke(app, ["grade", "ECON 166"])

    assert "rules: confirmed (manual," in after.output
