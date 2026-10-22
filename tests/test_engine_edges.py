"""Edge cases from the M5 review: letter floor, solver statuses, duplicates."""

from __future__ import annotations

import pytest

from gradetracker.engine.calculate import (
    current_grade,
    mirrored_pairs,
    projected_grade,
)
from gradetracker.engine.models import (
    DEFAULT_CUTOFFS,
    Assignment,
    Category,
    Course,
    Scheme,
    letter_for,
)
from gradetracker.engine.solve import required_for_letter, required_score

POINTS = Scheme(mode="points")
WEIGHTED = Scheme(mode="weighted")


def course(assignments, categories=(), scheme=POINTS):
    return Course(
        id=1,
        code="TEST 101",
        categories=tuple(categories),
        assignments=tuple(assignments),
        scheme=scheme,
    )


# --- B2: the scale has a floor --------------------------------------------


@pytest.mark.parametrize("percent", [-0.01, -5.0, -100.0])
def test_a_negative_percent_earns_the_bottom_of_the_scale(percent):
    """A negative curve should read as an F, not as "no letter"."""
    assert letter_for(percent) == "F"


def test_a_percent_below_a_custom_scale_floor_takes_the_last_letter():
    cutoffs = (("Pass", 70.0), ("Fail", 50.0))

    assert letter_for(49.0, cutoffs) == "Fail"
    assert letter_for(-1.0, cutoffs) == "Fail"
    assert letter_for(70.0, cutoffs) == "Pass"


def test_only_an_unknown_percent_has_no_letter():
    assert letter_for(None) is None
    assert letter_for(0.0) == "F"


def test_a_negative_bonus_still_produces_a_letter():
    result = current_grade(
        course(
            [Assignment(1, "PS1", earned=1, possible=100, status="graded")],
            scheme=Scheme(mode="points", bonus_percent=-10.0),
        )
    )

    assert result.percent == pytest.approx(-9.0)
    assert result.letter == "F"


# --- B3: an unknown that cannot move the grade ----------------------------


def test_an_excused_unknown_reports_not_counted():
    """"locked_in, score 0" would be true and thoroughly misleading."""
    model = course(
        [
            Assignment(1, "PS1", earned=18, possible=20, status="graded"),
            Assignment(2, "Quiz", possible=10, status="excused"),
        ]
    )

    required = required_score(model, 95.0, [2])

    assert required.status == "not_counted"
    assert "excused" in required.message
    assert required.score_percent is None


def test_an_excluded_unknown_reports_not_counted():
    model = course(
        [
            Assignment(1, "PS1", earned=18, possible=20, status="graded"),
            Assignment(2, "Copy", possible=20, status="ungraded", excluded=True),
        ]
    )

    required = required_score(model, 95.0, [2])

    assert required.status == "not_counted"
    assert "excluded" in required.message


def test_an_unknown_in_an_unweighted_category_reports_not_counted():
    """In weighted mode a category with no weight carries nothing."""
    categories = [
        Category(1, "Problem Sets", weight=100),
        Category(2, "Extras", weight=None),
    ]
    model = course(
        [
            Assignment(1, "PS1", category_id=1, earned=18, possible=20, status="graded"),
            Assignment(2, "Side quest", category_id=2, possible=50, status="ungraded"),
        ],
        categories,
        WEIGHTED,
    )

    required = required_score(model, 95.0, [2])

    assert required.status == "not_counted"
    assert "no weight" in required.message


def test_a_real_unknown_is_not_reported_as_not_counted():
    model = course(
        [
            Assignment(1, "PS1", earned=18, possible=20, status="graded"),
            Assignment(2, "Final", possible=80, status="ungraded"),
        ]
    )

    assert required_score(model, 85.0, [2]).status == "ok"


def test_a_course_with_no_points_at_all_reports_not_counted():
    model = course([Assignment(1, "Attendance", possible=0, status="ungraded")])

    required = required_score(model, 50.0, [1])

    assert required.status == "not_counted"


# --- B4: no unknown chosen ------------------------------------------------


def test_no_unknown_is_judged_against_the_projected_grade():
    """Every other branch uses the projection, so this one must too.

    PS1 18/20 graded (90%), PS2 20 points ungraded. The projected grade assumes
    PS2 at 90%, so projected = 90%. A target of 85% is therefore locked in even
    though the CURRENT grade is also 90% -- the point is that both branches now
    answer the same question.
    """
    model = course(
        [
            Assignment(1, "PS1", earned=18, possible=20, status="graded"),
            Assignment(2, "PS2", possible=20, status="ungraded"),
        ]
    )
    assert projected_grade(model).percent == pytest.approx(90.0)

    required = required_score(model, 85.0, [])

    assert required.status == "locked_in"
    assert "projected grade" in required.message


def test_no_unknown_against_an_unreachable_target():
    model = course(
        [
            Assignment(1, "PS1", earned=18, possible=20, status="graded"),
            Assignment(2, "PS2", possible=20, status="ungraded"),
        ]
    )

    required = required_score(model, 99.0, [])

    assert required.status == "impossible"
    assert "projected grade" in required.message


def test_unmatched_unknown_ids_behave_like_none_chosen():
    model = course([Assignment(1, "PS1", earned=18, possible=20, status="graded")])

    required = required_score(model, 50.0, [12345])

    assert required.status == "locked_in"
    assert "no unknown item was chosen" in required.message


def test_an_unknown_letter_explains_itself():
    model = course([Assignment(1, "PS1", earned=18, possible=20, status="graded")])

    required = required_for_letter(model, "A++", [1])

    assert required.status == "impossible"
    assert "not on this course's grading scale" in required.message


def test_required_for_letter_passes_the_message_through():
    model = course(
        [
            Assignment(1, "PS1", earned=18, possible=20, status="graded"),
            Assignment(2, "Quiz", possible=10, status="excused"),
        ]
    )

    required = required_for_letter(model, "A", [2])

    assert required.status == "not_counted"
    assert "excused" in required.message


# --- weights that do not sum to 100 ---------------------------------------


def test_weights_not_summing_to_one_hundred_warns():
    categories = [Category(1, "A", weight=40), Category(2, "B", weight=40)]
    result = current_grade(
        course(
            [
                Assignment(1, "a", category_id=1, earned=9, possible=10, status="graded"),
                Assignment(2, "b", category_id=2, earned=8, possible=10, status="graded"),
            ],
            categories,
            WEIGHTED,
        )
    )

    assert any("sum to 80%" in warning for warning in result.warnings)
    # Renormalisation still applies: (40*90 + 40*80)/80 = 85
    assert result.percent == pytest.approx(85.0)


def test_weights_summing_to_one_hundred_do_not_warn():
    categories = [Category(1, "A", weight=60), Category(2, "B", weight=40)]
    result = current_grade(
        course(
            [Assignment(1, "a", category_id=1, earned=9, possible=10, status="graded")],
            categories,
            WEIGHTED,
        )
    )

    assert not any("sum to" in warning for warning in result.warnings)


def test_points_mode_does_not_warn_about_weights():
    categories = [Category(1, "A", weight=40)]
    result = current_grade(
        course(
            [Assignment(1, "a", category_id=1, earned=9, possible=10, status="graded")],
            categories,
            POINTS,
        )
    )

    assert not any("sum to" in warning for warning in result.warnings)


# --- mirrored assignments -------------------------------------------------


def mirrored(possible_right=20.0, **kwargs):
    """The same problem set posted to both platforms."""
    return [
        Assignment(
            1, "Problem Set 1", category_id=1, earned=18, possible=20,
            status="graded", source_course_id=10,
        ),
        Assignment(
            2, "Problem set 1!", category_id=1, earned=18, possible=possible_right,
            status="graded", source_course_id=11, **kwargs,
        ),
    ]


def test_a_mirrored_assignment_is_flagged():
    categories = [Category(1, "Problem Sets", weight=100)]
    result = current_grade(course(mirrored(), categories, WEIGHTED))

    assert result.duplicate_pairs == ((1, 2),)
    assert any("may be a duplicate" in warning for warning in result.warnings)
    assert any("#1" in warning and "#2" in warning for warning in result.warnings)


def test_names_are_matched_loosely():
    """Casefold, and collapse punctuation and spacing."""
    categories = [Category(1, "Problem Sets", weight=100)]
    result = current_grade(course(mirrored(), categories, WEIGHTED))

    # "Problem Set 1" vs "Problem set 1!" still match.
    assert result.duplicate_pairs == ((1, 2),)


def test_different_point_values_are_still_flagged():
    """The "typed the Gradescope score into a Blackboard column" case."""
    categories = [Category(1, "Problem Sets", weight=100)]
    result = current_grade(course(mirrored(possible_right=100.0), categories, WEIGHTED))

    assert result.duplicate_pairs == ((1, 2),)


def test_two_rows_from_the_same_platform_are_not_flagged():
    """Two real assignments that happen to share a name are not a mirror."""
    categories = [Category(1, "Problem Sets", weight=100)]
    assignments = [
        Assignment(1, "Problem Set 1", category_id=1, earned=18, possible=20,
                   status="graded", source_course_id=10),
        Assignment(2, "Problem Set 1", category_id=1, earned=18, possible=20,
                   status="graded", source_course_id=10),
    ]

    result = current_grade(course(assignments, categories, WEIGHTED))

    assert result.duplicate_pairs == ()


def test_different_categories_are_not_flagged():
    categories = [Category(1, "A", weight=50), Category(2, "B", weight=50)]
    assignments = [
        Assignment(1, "Essay", category_id=1, earned=9, possible=10,
                   status="graded", source_course_id=10),
        Assignment(2, "Essay", category_id=2, earned=9, possible=10,
                   status="graded", source_course_id=11),
    ]

    result = current_grade(course(assignments, categories, WEIGHTED))

    assert result.duplicate_pairs == ()


def test_an_uncounted_copy_is_not_flagged():
    """An ungraded twin is not being double counted yet."""
    categories = [Category(1, "Problem Sets", weight=100)]
    assignments = [
        Assignment(1, "Problem Set 1", category_id=1, earned=18, possible=20,
                   status="graded", source_course_id=10),
        Assignment(2, "Problem Set 1", category_id=1, possible=20,
                   status="ungraded", source_course_id=11),
    ]

    result = current_grade(course(assignments, categories, WEIGHTED))

    assert result.duplicate_pairs == ()


def test_uncategorised_duplicates_are_flagged_in_points_mode():
    assignments = [
        Assignment(1, "Lab 1", earned=9, possible=10, status="graded", source_course_id=10),
        Assignment(2, "Lab 1", earned=9, possible=10, status="graded", source_course_id=11),
    ]

    result = current_grade(course(assignments, scheme=POINTS))

    assert result.duplicate_pairs == ((1, 2),)


def test_a_dropped_copy_is_not_flagged():
    """If the drop rule already removed one, it is not being counted twice."""
    categories = [Category(1, "Quizzes", weight=100, drop_lowest=1)]
    assignments = [
        Assignment(1, "Quiz 1", category_id=1, earned=5, possible=10,
                   status="graded", source_course_id=10),
        Assignment(2, "Quiz 1", category_id=1, earned=5, possible=10,
                   status="graded", source_course_id=11),
        Assignment(3, "Quiz 2", category_id=1, earned=10, possible=10,
                   status="graded", source_course_id=10),
    ]

    result = current_grade(course(assignments, categories, WEIGHTED))

    # One of the two copies is dropped, so only one is counted.
    assert result.duplicate_pairs == ()


def test_one_extra_trailing_word_is_still_the_same_work():
    """Gradescope's "Quiz 1 ALL" is Blackboard's "Quiz 1", really MATH 116."""
    categories = [Category(1, "Quizzes", weight=100)]
    assignments = [
        Assignment(1, "Quiz 1", category_id=1, earned=4, possible=5,
                   status="graded", source_course_id=10),
        Assignment(2, "Quiz 1 ALL", category_id=1, earned=4, possible=5,
                   status="graded", source_course_id=11),
    ]

    result = current_grade(course(assignments, categories, WEIGHTED))

    assert result.duplicate_pairs == ((1, 2),)


def test_a_different_last_token_is_not_the_same_work():
    """"Homework 1" and "Homework 12" differ in a word, not by one."""
    categories = [Category(1, "Homework", weight=100)]
    assignments = [
        Assignment(1, "Homework 1", category_id=1, earned=9, possible=10,
                   status="graded", source_course_id=10),
        Assignment(2, "Homework 12", category_id=1, earned=9, possible=10,
                   status="graded", source_course_id=11),
    ]

    result = current_grade(course(assignments, categories, WEIGHTED))

    assert result.duplicate_pairs == ()


def test_two_numbered_items_are_not_the_same_work():
    categories = [Category(1, "Midterms", weight=100)]
    assignments = [
        Assignment(1, "Midterm 1", category_id=1, earned=90, possible=100,
                   status="graded", source_course_id=10),
        Assignment(2, "Midterm 2", category_id=1, earned=80, possible=100,
                   status="graded", source_course_id=11),
    ]

    result = current_grade(course(assignments, categories, WEIGHTED))

    assert result.duplicate_pairs == ()


# --- excluded -------------------------------------------------------------


def test_an_excluded_assignment_is_not_counted():
    result = current_grade(
        course(
            [
                Assignment(1, "PS1", earned=18, possible=20, status="graded"),
                Assignment(2, "PS1 copy", earned=0, possible=20, status="graded", excluded=True),
            ]
        )
    )

    # Only the kept copy counts: 18/20 = 90%, not 18/40 = 45%.
    assert result.percent == pytest.approx(90.0)


def test_excluding_one_copy_clears_the_duplicate_warning():
    categories = [Category(1, "Problem Sets", weight=100)]
    before = current_grade(course(mirrored(), categories, WEIGHTED))
    assert before.duplicate_pairs == ((1, 2),)

    after = current_grade(course(mirrored(excluded=True), categories, WEIGHTED))

    assert after.duplicate_pairs == ()
    assert not any("may be a duplicate" in warning for warning in after.warnings)
    assert after.percent == pytest.approx(90.0)


def test_an_excluded_item_stays_in_the_assignment_list():
    """The UI needs it to show greyed out, so it must not be filtered away."""
    model = course(mirrored(excluded=True))

    assert len(model.assignments) == 2


def test_an_excluded_item_is_never_assumed_in_a_projection():
    model = course(
        [
            Assignment(1, "PS1", earned=18, possible=20, status="graded"),
            Assignment(2, "Copy", possible=20, status="ungraded", excluded=True),
        ]
    )

    projected = projected_grade(model)

    assert projected.assumed_ids == ()
    assert projected.percent == pytest.approx(90.0)


# --- mirrored_pairs -------------------------------------------------------


def test_mirrored_pairs_catches_a_twin_that_is_not_counted_yet():
    """The whole point: catch the copy before it has doubled anything."""
    assignments = [
        Assignment(1, "Quiz 4", category_id=1, possible=10, status="ungraded",
                   source_course_id=10),
        Assignment(2, "Quiz 4", possible=10, status="ungraded", source_course_id=11),
    ]
    model = course(assignments, [Category(1, "Quizzes", weight=100)], WEIGHTED)

    # Neither is graded, so the duplicate warning says nothing about them.
    assert current_grade(model).duplicate_pairs == ()
    assert mirrored_pairs(model) == ((1, 2),)


def test_mirrored_pairs_ignores_the_category():
    """A twin that arrived this morning has no category yet, and still counts."""
    assignments = [
        Assignment(1, "Homework 7", category_id=1, earned=9, possible=10,
                   status="graded", source_course_id=10),
        Assignment(2, "Homework 7", earned=9, possible=10, status="graded",
                   source_course_id=11),
    ]

    assert mirrored_pairs(course(assignments)) == ((1, 2),)


def test_mirrored_pairs_ignores_two_rows_from_one_platform_course():
    assignments = [
        Assignment(1, "Lab 3", earned=9, possible=10, status="graded",
                   source_course_id=10),
        Assignment(2, "Lab 3", earned=9, possible=10, status="graded",
                   source_course_id=10),
    ]

    assert mirrored_pairs(course(assignments)) == ()


def test_mirrored_pairs_still_keeps_separate_work_apart():
    assignments = [
        Assignment(1, "Homework 1", possible=10, status="ungraded", source_course_id=10),
        Assignment(2, "Homework 12", possible=10, status="ungraded", source_course_id=11),
    ]

    assert mirrored_pairs(course(assignments)) == ()


def test_mirrored_pairs_matches_one_extra_trailing_word():
    assignments = [
        Assignment(1, "Quiz 1", possible=10, status="ungraded", source_course_id=10),
        Assignment(2, "Quiz 1 ALL", possible=10, status="ungraded", source_course_id=11),
    ]

    assert mirrored_pairs(course(assignments)) == ((1, 2),)


def test_mirrored_pairs_sees_a_pair_that_is_already_excluded():
    """apply_preference relies on this: the pair stays visible, the row is skipped."""
    model = course(mirrored(excluded=True), [Category(1, "Problem Sets", weight=100)])

    assert current_grade(model).duplicate_pairs == ()
    assert mirrored_pairs(model) == ((1, 2),)
