"""Grade engine tests (M5). Every expected number is hand-computed in a comment.

No I/O: the engine is pure, so these are plain function calls.
"""

from __future__ import annotations

import random

import pytest

from gradetracker.engine.calculate import (
    current_grade,
    drop_lowest,
    effective_items,
    projected_grade,
)
from gradetracker.engine.models import (
    DEFAULT_CUTOFFS,
    Assignment,
    Category,
    Course,
    Scheme,
    cutoff_for,
    letter_for,
)
from gradetracker.engine.solve import required_for_letter, required_score

POINTS = Scheme(mode="points")
WEIGHTED = Scheme(mode="weighted")


def assignment(id, name="Item", category_id=None, earned=None, possible=None, status="graded", **kw):
    return Assignment(
        id=id,
        name=name,
        category_id=category_id,
        earned=earned,
        possible=possible,
        status=status,
        **kw,
    )


def course(assignments, categories=(), scheme=POINTS, code="TEST 101"):
    return Course(
        id=1,
        code=code,
        name="Test Course",
        categories=tuple(categories),
        assignments=tuple(assignments),
        scheme=scheme,
    )


def approx(value):
    return pytest.approx(value, abs=1e-6)


# --- (a) points course ----------------------------------------------------


def test_points_course():
    # 18/20 + 9/10 + 45/50 = 72/80 = 90.0%
    result = current_grade(
        course(
            [
                assignment(1, "PS1", earned=18, possible=20),
                assignment(2, "Quiz", earned=9, possible=10),
                assignment(3, "Exam", earned=45, possible=50),
            ]
        )
    )

    assert result.percent == approx(90.0)
    assert result.letter == "A-"
    assert result.mode == "points"
    assert result.warnings == ()


def test_nothing_graded_gives_no_percent():
    """Reporting 0% for an untouched course would be a lie."""
    result = current_grade(
        course([assignment(1, "PS1", possible=20, status="ungraded")])
    )

    assert result.percent is None
    assert result.letter is None


# --- (b) weighted with an empty category ----------------------------------


def test_weighted_renormalises_over_categories_with_graded_work():
    # Problem Sets w40: 18/20 = 90%. Participation w20: 8/10 = 80%.
    # Exams w40 has nothing graded, so weights renormalise over 40 + 20 = 60:
    # (40*90 + 20*80) / 60 = (3600 + 1600) / 60 = 5200/60 = 86.666...%
    categories = [
        Category(1, "Problem Sets", weight=40),
        Category(2, "Exams", weight=40),
        Category(3, "Participation", weight=20),
    ]
    result = current_grade(
        course(
            [
                assignment(1, "PS1", category_id=1, earned=18, possible=20),
                assignment(2, "Midterm", category_id=2, possible=100, status="ungraded"),
                assignment(3, "Attendance", category_id=3, earned=8, possible=10),
            ],
            categories,
            WEIGHTED,
        )
    )

    assert result.percent == approx(5200 / 60)
    assert result.mode == "weighted"

    empty = next(c for c in result.categories if c.category_id == 2)
    assert empty.percent is None
    assert empty.graded_count == 0


def test_weighted_with_no_graded_categories_has_no_percent():
    result = current_grade(
        course(
            [assignment(1, "PS1", category_id=1, possible=20, status="ungraded")],
            [Category(1, "Problem Sets", weight=100)],
            WEIGHTED,
        )
    )

    assert result.percent is None


# --- (c) drop lowest with unequal point values ----------------------------


def test_drop_lowest_maximises_rather_than_dropping_the_lowest_percentage():
    """The case where the two rules disagree.

    Items: A = 0/1 (0%), B = 50/100 (50%), C = 10/10 (100%), drop 1.

        no drop        60/111 = 54.054%
        drop A (0%)    60/110 = 54.545%   <- what "drop the lowest percent" does
        drop B (50%)   10/11  = 90.909%   <- the actual maximum
        drop C (100%)  50/101 = 49.505%

    So the engine must drop B, the item with the lowest percentage but by far
    the most points, not A.
    """
    categories = [Category(1, "Problem Sets", weight=100, drop_lowest=1)]
    assignments = [
        assignment(1, "A", category_id=1, earned=0, possible=1),
        assignment(2, "B", category_id=1, earned=50, possible=100),
        assignment(3, "C", category_id=1, earned=10, possible=10),
    ]

    result = current_grade(course(assignments, categories, WEIGHTED))
    bucket = result.categories[0]

    assert bucket.percent == approx(10 / 11 * 100)  # 90.909090...
    assert bucket.dropped_ids == (2,)  # B, not A
    assert bucket.earned == approx(10.0)
    assert bucket.possible == approx(11.0)


def test_drop_lowest_of_equal_weight_items_drops_the_worst():
    # 5/10, 8/10, 10/10, drop 1 -> drop 5/10 -> 18/20 = 90%
    categories = [Category(1, "Quizzes", weight=100, drop_lowest=1)]
    assignments = [
        assignment(1, "Q1", category_id=1, earned=5, possible=10),
        assignment(2, "Q2", category_id=1, earned=8, possible=10),
        assignment(3, "Q3", category_id=1, earned=10, possible=10),
    ]

    bucket = current_grade(course(assignments, categories, WEIGHTED)).categories[0]

    assert bucket.percent == approx(90.0)
    assert bucket.dropped_ids == (1,)


def test_drop_two_lowest():
    # 1/10, 2/10, 9/10, 10/10, drop 2 -> keep 19/20 = 95%
    categories = [Category(1, "Quizzes", weight=100, drop_lowest=2)]
    assignments = [
        assignment(1, "Q1", category_id=1, earned=1, possible=10),
        assignment(2, "Q2", category_id=1, earned=2, possible=10),
        assignment(3, "Q3", category_id=1, earned=9, possible=10),
        assignment(4, "Q4", category_id=1, earned=10, possible=10),
    ]

    bucket = current_grade(course(assignments, categories, WEIGHTED)).categories[0]

    assert bucket.percent == approx(95.0)
    assert sorted(bucket.dropped_ids) == [1, 2]


def test_drop_rule_does_nothing_with_too_few_items():
    """You cannot drop work that has not happened yet."""
    categories = [Category(1, "Quizzes", weight=100, drop_lowest=2)]
    assignments = [
        assignment(1, "Q1", category_id=1, earned=5, possible=10),
        assignment(2, "Q2", category_id=1, earned=10, possible=10),
    ]

    bucket = current_grade(course(assignments, categories, WEIGHTED)).categories[0]

    assert bucket.dropped_ids == ()
    assert bucket.percent == approx(75.0)  # 15/20


def test_extra_credit_is_never_dropped():
    items = effective_items(
        [
            assignment(1, "Q1", earned=5, possible=10),
            assignment(2, "Q2", earned=10, possible=10),
            assignment(3, "Bonus", earned=3, possible=5, extra_credit=True),
        ]
    )

    kept, dropped = drop_lowest(items, 1)

    assert dropped == (1,)
    assert 3 in {item.id for item in kept}


# --- (d) excused ----------------------------------------------------------


def test_excused_item_leaves_both_earned_and_possible():
    # Only 18/20 counts; the excused quiz's 10 points vanish. 18/20 = 90%
    result = current_grade(
        course(
            [
                assignment(1, "PS1", earned=18, possible=20),
                assignment(2, "Quiz", earned=None, possible=10, status="excused"),
            ]
        )
    )

    assert result.percent == approx(90.0)


def test_excused_item_with_a_score_is_still_removed():
    result = current_grade(
        course(
            [
                assignment(1, "PS1", earned=18, possible=20),
                assignment(2, "Quiz", earned=2, possible=10, status="excused"),
            ]
        )
    )

    assert result.percent == approx(90.0)


# --- (e) extra credit -----------------------------------------------------


def test_extra_credit_adds_to_earned_but_not_possible():
    # (18 + 3) / 20 = 105%
    result = current_grade(
        course(
            [
                assignment(1, "PS1", earned=18, possible=20),
                assignment(2, "Bonus", earned=3, possible=5, extra_credit=True),
            ]
        )
    )

    assert result.percent == approx(105.0)


def test_extra_credit_inside_a_weighted_category():
    # Problem Sets w100: without the bonus 18/20 = 90%; the 2 point bonus adds
    # to earned but not possible, so (18 + 2)/20 = 20/20 = 100%.
    categories = [Category(1, "Problem Sets", weight=100)]
    result = current_grade(
        course(
            [
                assignment(1, "PS1", category_id=1, earned=18, possible=20),
                assignment(2, "Bonus", category_id=1, earned=2, possible=0, extra_credit=True),
            ],
            categories,
            WEIGHTED,
        )
    )

    assert result.percent == approx(100.0)
    assert result.categories[0].earned == approx(20.0)
    assert result.categories[0].possible == approx(20.0)
    assert result.categories[0].extra_credit_earned == approx(2.0)


# --- (f) flat bonus -------------------------------------------------------


def test_flat_bonus_is_added_to_the_final_percentage():
    # 18/20 = 90%, plus a 2 point course-wide curve = 92%
    result = current_grade(
        course(
            [assignment(1, "PS1", earned=18, possible=20)],
            scheme=Scheme(mode="points", bonus_percent=2.0),
        )
    )

    assert result.percent == approx(92.0)
    assert result.letter == "A-"


def test_bonus_is_not_applied_when_nothing_is_graded():
    """A bonus on no data would invent a grade out of nothing."""
    result = current_grade(
        course(
            [assignment(1, "PS1", possible=20, status="ungraded")],
            scheme=Scheme(mode="points", bonus_percent=5.0),
        )
    )

    assert result.percent is None


# --- (g) missing ----------------------------------------------------------


def test_missing_counts_as_zero_by_default():
    # (18 + 0) / (20 + 20) = 18/40 = 45%
    result = current_grade(
        course(
            [
                assignment(1, "PS1", earned=18, possible=20),
                assignment(2, "PS2", possible=20, status="missing"),
            ]
        )
    )

    assert result.percent == approx(45.0)


def test_missing_can_be_excluded_instead():
    # With missing_as_zero off the item is simply not counted: 18/20 = 90%
    result = current_grade(
        course(
            [
                assignment(1, "PS1", earned=18, possible=20),
                assignment(2, "PS2", possible=20, status="missing", missing_as_zero=False),
            ]
        )
    )

    assert result.percent == approx(90.0)


def test_submitted_but_ungraded_is_excluded():
    result = current_grade(
        course(
            [
                assignment(1, "PS1", earned=18, possible=20),
                assignment(2, "PS2", possible=20, status="submitted"),
            ]
        )
    )

    assert result.percent == approx(90.0)


# --- (h) uncategorised in weighted mode -----------------------------------


def test_uncategorised_graded_items_are_excluded_in_weighted_mode():
    """They have no weight to carry, so counting them would be a guess."""
    categories = [Category(1, "Problem Sets", weight=100)]
    result = current_grade(
        course(
            [
                assignment(1, "PS1", category_id=1, earned=18, possible=20),
                assignment(2, "Mystery", earned=0, possible=10),
            ],
            categories,
            WEIGHTED,
        )
    )

    assert result.percent == approx(90.0)
    assert any("no category" in warning for warning in result.warnings)
    assert "1 graded assignments have no category and are not counted" in result.warnings


def test_uncategorised_items_do_count_in_points_mode():
    # (18 + 0)/(20 + 10) = 60%
    categories = [Category(1, "Problem Sets", weight=None)]
    result = current_grade(
        course(
            [
                assignment(1, "PS1", category_id=1, earned=18, possible=20),
                assignment(2, "Mystery", earned=0, possible=10),
            ],
            categories,
            POINTS,
        )
    )

    assert result.percent == approx(60.0)
    assert not any("no category" in warning for warning in result.warnings)


def test_weighted_category_without_a_weight_warns():
    categories = [Category(1, "Problem Sets", weight=None)]
    result = current_grade(
        course(
            [assignment(1, "PS1", category_id=1, earned=18, possible=20)],
            categories,
            WEIGHTED,
        )
    )

    assert result.percent is None
    assert any("no weight set for Problem Sets" in w for w in result.warnings)


# --- (i) projection -------------------------------------------------------


def test_projection_uses_the_category_average_not_the_course_average():
    """The distinguishing case.

    Problem Sets w50: PS1 18/20 (90%), PS2 ungraded 20 points.
    Exams w50:        Midterm 60/100 (60%).

    current  = (50*90 + 50*60) / 100 = 75%
    PS2 assumed at its CATEGORY average, 90% of 20 = 18:
        Problem Sets = (18+18)/(20+20) = 90%
        projected    = (50*90 + 50*60) / 100 = 75%

    Had it used the COURSE average of 75%, PS2 would be 15 points:
        Problem Sets = (18+15)/40 = 82.5%
        projected    = (50*82.5 + 50*60)/100 = 71.25%

    So 75.0 proves the category average was used.
    """
    categories = [Category(1, "Problem Sets", weight=50), Category(2, "Exams", weight=50)]
    assignments = [
        assignment(1, "PS1", category_id=1, earned=18, possible=20),
        assignment(2, "PS2", category_id=1, possible=20, status="ungraded"),
        assignment(3, "Midterm", category_id=2, earned=60, possible=100),
    ]
    model = course(assignments, categories, WEIGHTED)

    assert current_grade(model).percent == approx(75.0)

    projected = projected_grade(model)

    assert projected.percent == approx(75.0)
    assert projected.assumed_ids == (2,)
    assert projected.categories[0].percent == approx(90.0)


def test_projection_falls_back_to_the_course_percent_for_an_empty_category():
    # Problem Sets w50: 18/20 = 90%. Exams w50 has nothing graded, so the
    # final is assumed at the course percent, 90% of 100 = 90.
    # projected = (50*90 + 50*90)/100 = 90%
    categories = [Category(1, "Problem Sets", weight=50), Category(2, "Exams", weight=50)]
    assignments = [
        assignment(1, "PS1", category_id=1, earned=18, possible=20),
        assignment(2, "Final", category_id=2, possible=100, status="ungraded"),
    ]

    projected = projected_grade(course(assignments, categories, WEIGHTED))

    assert projected.percent == approx(90.0)
    assert projected.categories[1].percent == approx(90.0)
    assert projected.assumed_ids == (2,)


def test_projection_honours_an_explicit_assumption():
    # Final assumed at 50/100 -> Exams 50% -> (50*90 + 50*50)/100 = 70%
    categories = [Category(1, "Problem Sets", weight=50), Category(2, "Exams", weight=50)]
    assignments = [
        assignment(1, "PS1", category_id=1, earned=18, possible=20),
        assignment(2, "Final", category_id=2, possible=100, status="ungraded"),
    ]

    projected = projected_grade(
        course(assignments, categories, WEIGHTED), assumptions={2: 50.0}
    )

    assert projected.percent == approx(70.0)


def test_projection_leaves_a_missing_zero_alone():
    """A missing item counted as zero is a real score, not an unknown."""
    model = course(
        [
            assignment(1, "PS1", earned=18, possible=20),
            assignment(2, "PS2", possible=20, status="missing"),
        ]
    )

    projected = projected_grade(model)

    assert projected.assumed_ids == ()
    assert projected.percent == approx(45.0)


def test_projection_fills_a_missing_item_that_is_not_a_zero():
    # PS2 missing but not counted as zero, so it is an unknown: assumed at the
    # course percent 90% of 20 = 18 -> (18+18)/40 = 90%
    model = course(
        [
            assignment(1, "PS1", earned=18, possible=20),
            assignment(2, "PS2", possible=20, status="missing", missing_as_zero=False),
        ]
    )

    projected = projected_grade(model)

    assert projected.assumed_ids == (2,)
    assert projected.percent == approx(90.0)


def test_projection_of_an_ungraded_course_has_no_percent():
    projected = projected_grade(
        course([assignment(1, "PS1", possible=20, status="ungraded")])
    )

    assert projected.percent is None
    assert projected.assumed_ids == ()


# --- (j) required score, one unknown --------------------------------------


def test_required_score_single_unknown():
    """Hand check.

    PS1 is 18/20 graded, the final is 80 points ungraded, target 85%.
    Total possible = 20 + 80 = 100, so

        grade(p) = (18 + p/100 * 80) / 100 * 100 = 18 + 0.8p

    Setting 18 + 0.8p = 85 gives 0.8p = 67, so p = 83.75%.
    The final must earn 83.75% of 80 = 67 points, for 18 + 67 = 85/100.
    """
    model = course(
        [
            assignment(1, "PS1", earned=18, possible=20),
            assignment(2, "Final", possible=80, status="ungraded"),
        ]
    )

    required = required_score(model, 85.0, [2])

    assert required.status == "ok"
    assert required.score_percent == pytest.approx(83.75, abs=1e-3)
    assert required.per_item[2] == pytest.approx(67.0, abs=1e-2)


def test_required_score_is_verified_by_feeding_it_back():
    """Whatever the solver says, plugging it in must actually hit the target."""
    model = course(
        [
            assignment(1, "PS1", earned=18, possible=20),
            assignment(2, "Final", possible=80, status="ungraded"),
        ]
    )

    required = required_score(model, 85.0, [2])
    check = current_grade(model, overrides=required.per_item)

    assert check.percent >= 85.0 - 1e-3


def test_required_score_in_a_weighted_course():
    # Problem Sets w40 = 18/20 = 90%. Exams w60, final 100 points ungraded.
    # grade(p) = (40*90 + 60*p) / 100 = 36 + 0.6p
    # target 85 -> 0.6p = 49 -> p = 81.666...
    categories = [Category(1, "Problem Sets", weight=40), Category(2, "Exams", weight=60)]
    model = course(
        [
            assignment(1, "PS1", category_id=1, earned=18, possible=20),
            assignment(2, "Final", category_id=2, possible=100, status="ungraded"),
        ],
        categories,
        WEIGHTED,
    )

    required = required_score(model, 85.0, [2])

    assert required.score_percent == pytest.approx(49 / 0.6, abs=1e-3)


# --- (k) required score, several unknowns ---------------------------------


def test_required_score_several_unknowns_share_one_percentage():
    """Hand check.

    PS1 18/20 graded; midterm 30 points and final 50 points both unknown.
    Total possible = 20 + 30 + 50 = 100, and both unknowns score p:

        grade(p) = (18 + p/100 * 80) / 100 * 100 = 18 + 0.8p

    Target 80% gives 0.8p = 62, so p = 77.5%.
    Midterm earns 0.775 * 30 = 23.25, final 0.775 * 50 = 38.75,
    totalling 18 + 62 = 80/100.
    """
    model = course(
        [
            assignment(1, "PS1", earned=18, possible=20),
            assignment(2, "Midterm", possible=30, status="ungraded"),
            assignment(3, "Final", possible=50, status="ungraded"),
        ]
    )

    required = required_score(model, 80.0, [2, 3])

    assert required.status == "ok"
    assert required.score_percent == pytest.approx(77.5, abs=1e-3)
    assert required.per_item[2] == pytest.approx(23.25, abs=1e-2)
    assert required.per_item[3] == pytest.approx(38.75, abs=1e-2)


def test_other_unknowns_use_projection_assumptions():
    """An unknown you did not nominate is assumed, not solved for.

    PS1 18/20 (90%). PS2 20 points ungraded, assumed at 90% = 18.
    Final 60 points is the only unknown.
    grade(p) = (18 + 18 + 0.6p) / 100 * 100 = 36 + 0.6p
    target 90 -> 0.6p = 54 -> p = 90%.
    """
    model = course(
        [
            assignment(1, "PS1", earned=18, possible=20),
            assignment(2, "PS2", possible=20, status="ungraded"),
            assignment(3, "Final", possible=60, status="ungraded"),
        ]
    )

    required = required_score(model, 90.0, [3])

    assert required.score_percent == pytest.approx(90.0, abs=1e-3)


# --- (l) impossible and locked in -----------------------------------------


def test_impossible_when_a_perfect_score_is_not_enough():
    # 0/20 plus a perfect 20/20 final = 20/40 = 50%, short of 90%.
    model = course(
        [
            assignment(1, "PS1", earned=0, possible=20),
            assignment(2, "Final", possible=20, status="ungraded"),
        ]
    )

    required = required_score(model, 90.0, [2])

    assert required.status == "impossible"
    assert required.score_percent is None


def test_locked_in_when_a_zero_still_clears_the_target():
    # 20/20 plus a zero on the 20 point final = 20/40 = 50%, which meets 50%.
    model = course(
        [
            assignment(1, "PS1", earned=20, possible=20),
            assignment(2, "Final", possible=20, status="ungraded"),
        ]
    )

    required = required_score(model, 50.0, [2])

    assert required.status == "locked_in"
    assert required.per_item == {2: 0.0}


def test_required_score_with_no_real_unknowns():
    model = course([assignment(1, "PS1", earned=18, possible=20)])

    assert required_score(model, 50.0, [99]).status == "locked_in"
    assert required_score(model, 95.0, [99]).status == "impossible"


def test_required_for_letter_uses_the_scheme_cutoffs():
    # Target A- = 90%. grade(p) = 18 + 0.8p -> 0.8p = 72 -> p = 90%.
    model = course(
        [
            assignment(1, "PS1", earned=18, possible=20),
            assignment(2, "Final", possible=80, status="ungraded"),
        ]
    )

    required = required_for_letter(model, "A-", [2])

    assert required.status == "ok"
    assert required.letter == "A-"
    assert required.target_percent == approx(90.0)
    assert required.score_percent == pytest.approx(90.0, abs=1e-3)


def test_required_for_letter_with_a_custom_scale():
    cutoffs = (("Pass", 70.0), ("Fail", 0.0))
    model = course(
        [
            assignment(1, "PS1", earned=18, possible=20),
            assignment(2, "Final", possible=80, status="ungraded"),
        ],
        scheme=Scheme(mode="points", cutoffs=cutoffs),
    )

    required = required_for_letter(model, "Pass", [2])

    # 18 + 0.8p = 70 -> p = 65%
    assert required.target_percent == approx(70.0)
    assert required.score_percent == pytest.approx(65.0, abs=1e-3)


def test_required_for_an_unknown_letter_is_impossible():
    model = course([assignment(1, "PS1", earned=18, possible=20)])

    assert required_for_letter(model, "Z+", [1]).status == "impossible"


# --- (m) letter cutoffs ---------------------------------------------------


@pytest.mark.parametrize(
    "percent, letter",
    [
        (100.0, "A"),
        (93.0, "A"),
        (92.999, "A-"),
        (90.0, "A-"),  # exactly on the boundary
        (89.999, "B+"),
        (87.0, "B+"),
        (83.0, "B"),
        (80.0, "B-"),
        (77.0, "C+"),
        (73.0, "C"),
        (70.0, "C-"),
        (67.0, "D+"),
        (63.0, "D"),
        (60.0, "D-"),
        (59.999, "F"),
        (0.0, "F"),
        (105.0, "A"),
    ],
)
def test_letter_for(percent, letter):
    assert letter_for(percent) == letter


def test_letter_for_none_is_none():
    assert letter_for(None) is None


def test_cutoff_for():
    assert cutoff_for("A-") == 90.0
    assert cutoff_for("a-") == 90.0
    assert cutoff_for("nonsense") is None


def test_unsorted_cutoffs_are_still_read_in_descending_order():
    cutoffs = (("F", 0.0), ("A", 93.0), ("B", 83.0))

    assert letter_for(95.0, cutoffs) == "A"
    assert letter_for(85.0, cutoffs) == "B"
    assert letter_for(10.0, cutoffs) == "F"


# --- (n) no scheme --------------------------------------------------------


def test_no_scheme_falls_back_to_points_with_a_warning():
    result = current_grade(
        course([assignment(1, "PS1", earned=18, possible=20)], scheme=None)
    )

    assert result.mode == "points"
    assert result.percent == approx(90.0)
    assert result.letter == "A-"
    assert "no confirmed grading rules" in result.warnings


def test_assignment_with_no_point_value_warns():
    result = current_grade(
        course(
            [
                assignment(1, "PS1", earned=18, possible=20),
                assignment(2, "Attendance", earned=5, possible=0),
            ]
        )
    )

    assert result.percent == approx(90.0)
    assert any("no point value" in warning for warning in result.warnings)


# --- effective_items ------------------------------------------------------


def test_overrides_force_an_ungraded_item_in():
    items = effective_items(
        [assignment(1, "Final", possible=100, status="ungraded")], overrides={1: 80.0}
    )

    assert len(items) == 1
    assert items[0].earned == 80.0
    assert items[0].possible == 100.0
    assert items[0].assumed is True


def test_overrides_cannot_resurrect_an_excused_item():
    """Excused means the work does not exist, not that it scores zero."""
    items = effective_items(
        [assignment(1, "Quiz", possible=10, status="excused")], overrides={1: 10.0}
    )

    assert items == []


# --- property checks ------------------------------------------------------


def random_course(rng: random.Random, weighted: bool) -> Course:
    """A small random course, used for the invariants below."""
    categories = tuple(
        Category(
            id=index + 1,
            name=f"Cat {index + 1}",
            weight=rng.choice([10, 20, 25, 30, 40]) if weighted else None,
            drop_lowest=rng.choice([0, 0, 0, 1]),
        )
        for index in range(rng.randint(1, 3))
    )
    assignments = []
    for index in range(rng.randint(1, 8)):
        category = rng.choice(categories)
        possible = rng.choice([1, 5, 10, 20, 100])
        status = rng.choice(["graded", "graded", "graded", "ungraded", "missing", "excused"])
        assignments.append(
            Assignment(
                id=index + 1,
                name=f"Item {index + 1}",
                category_id=category.id,
                earned=round(rng.uniform(0, possible), 2) if status == "graded" else None,
                possible=possible,
                status=status,
                extra_credit=rng.random() < 0.1,
                missing_as_zero=rng.random() < 0.8,
            )
        )
    return Course(
        id=1,
        code="RAND 101",
        name="Random",
        categories=categories,
        assignments=tuple(assignments),
        scheme=Scheme(mode="weighted" if weighted else "points"),
    )


def test_raising_a_score_never_lowers_the_grade():
    """Monotonicity. Also true through a drop rule, since the max over subsets
    is monotone in each item's earned."""
    rng = random.Random(0)

    for _ in range(200):
        model = random_course(rng, weighted=rng.random() < 0.5)
        before = current_grade(model).percent
        if before is None:
            continue

        graded = [a for a in model.assignments if a.status == "graded" and a.earned is not None]
        if not graded:
            continue
        target = rng.choice(graded)
        raised = tuple(
            a if a.id != target.id else Assignment(
                id=a.id,
                name=a.name,
                category_id=a.category_id,
                earned=(a.earned or 0) + rng.choice([0.5, 1, 5]),
                possible=a.possible,
                status=a.status,
                extra_credit=a.extra_credit,
                missing_as_zero=a.missing_as_zero,
            )
            for a in model.assignments
        )
        after = current_grade(
            Course(
                id=model.id,
                code=model.code,
                name=model.name,
                categories=model.categories,
                assignments=raised,
                scheme=model.scheme,
            )
        ).percent

        assert after is not None
        assert after >= before - 1e-9, (before, after, target)


def test_a_perfect_course_is_exactly_one_hundred():
    rng = random.Random(0)

    for _ in range(200):
        weighted = rng.random() < 0.5
        categories = tuple(
            Category(
                id=index + 1,
                name=f"Cat {index + 1}",
                weight=rng.choice([10, 25, 50]) if weighted else None,
                drop_lowest=rng.choice([0, 1]),
            )
            for index in range(rng.randint(1, 3))
        )
        assignments = []
        for index in range(rng.randint(1, 8)):
            category = rng.choice(categories)
            possible = rng.choice([1, 5, 10, 20, 100])
            # Every graded item is full marks; no extra credit, no zero-missing.
            assignments.append(
                Assignment(
                    id=index + 1,
                    name=f"Item {index + 1}",
                    category_id=category.id,
                    earned=possible,
                    possible=possible,
                    status="graded",
                )
            )
        model = Course(
            id=1,
            code="PERFECT 1",
            categories=categories,
            assignments=tuple(assignments),
            scheme=Scheme(mode="weighted" if weighted else "points"),
        )

        result = current_grade(model)

        assert result.percent == pytest.approx(100.0), result
        assert result.letter == "A"


# --- ranked category weights ----------------------------------------------
#
# Two real syllabi. ECON 166: "the midterm on which you earn the highest grade
# is weighted 23% and the other 17%". MATH 116, four midterms: "the best counts
# 20%, the worst 10%, the middle two 15% each".

ECON_RANKS = (23.0, 17.0)
MATH_RANKS = (20.0, 15.0, 15.0, 10.0)


def ranked_course(scores, rank_weights=ECON_RANKS, weight=None, drop_lowest_n=0, **kw):
    """One ranked category of midterms scored out of 100.

    A score of None is an ungraded midterm. The category weight defaults to the
    rank weights' sum, which is what validation requires.
    """
    if weight is None:
        weight = sum(rank_weights)
    assignments = [
        assignment(
            id=index + 1,
            name=f"Midterm {index + 1}",
            category_id=1,
            earned=score,
            possible=100.0,
            status="graded" if score is not None else "ungraded",
        )
        for index, score in enumerate(scores)
    ]
    category = Category(
        id=1,
        name="Midterms",
        weight=weight,
        drop_lowest=drop_lowest_n,
        rank_weights=rank_weights,
    )
    return course(assignments, [category], scheme=WEIGHTED, **kw)


def ranked_percent(model):
    return current_grade(model).categories[0].percent


def test_one_graded_item_is_its_own_percentage():
    """One midterm marked: the category is that midterm, not 23/40 of it."""
    assert ranked_percent(ranked_course([90.0, None])) == approx(90.0)


def test_two_graded_items_take_the_two_weights():
    # (23 * 90 + 17 * 80) / 40 = 85.75
    assert ranked_percent(ranked_course([90.0, 80.0])) == approx(85.75)


def test_the_higher_score_takes_the_higher_weight_whatever_the_order():
    """The ranking is by score, not by the order the items happen to be in."""
    assert ranked_percent(ranked_course([80.0, 90.0])) == approx(85.75)
    assert ranked_percent(ranked_course([90.0, 80.0])) == approx(85.75)


def test_four_ranked_midterms():
    # (20*100 + 15*90 + 15*80 + 10*70) / 60 = 87.5
    model = ranked_course([70.0, 80.0, 90.0, 100.0], MATH_RANKS)

    assert ranked_percent(model) == approx(87.5)


def test_two_of_four_graded_use_the_top_two_weights():
    # (20 * 90 + 15 * 80) / 35 = 85.714...
    model = ranked_course([90.0, 80.0, None, None], MATH_RANKS)

    assert ranked_percent(model) == approx((20 * 90 + 15 * 80) / 35)


def test_a_ranked_category_carries_its_weight_in_the_course_grade():
    """The ranked percentage is what the category's weight then multiplies."""
    midterms = Category(id=1, name="Midterms", weight=40.0, rank_weights=ECON_RANKS)
    homework = Category(id=2, name="Homework", weight=60.0)
    model = course(
        [
            assignment(id=1, name="Midterm 1", category_id=1, earned=90.0, possible=100.0),
            assignment(id=2, name="Midterm 2", category_id=1, earned=80.0, possible=100.0),
            assignment(id=3, name="HW 1", category_id=2, earned=50.0, possible=100.0),
        ],
        [midterms, homework],
        scheme=WEIGHTED,
    )

    # Midterms 85.75 at 40%, Homework 50 at 60% -> 64.3
    assert current_grade(model).percent == approx(0.4 * 85.75 + 0.6 * 50.0)


def test_an_unranked_category_is_unchanged_by_the_feature():
    """Without rank weights the category is still total points over total."""
    model = ranked_course([90.0, 80.0], rank_weights=None, weight=40.0)

    assert ranked_percent(model) == approx(85.0)


def test_ranked_ties_are_broken_by_assignment_id():
    """Equal percentages: the lower id takes the higher weight, so the answer
    is stable rather than dependent on row order."""
    model = ranked_course([80.0, 80.0])

    applied = current_grade(model).categories[0].rank_weights_applied

    assert applied == ((1, 23.0), (2, 17.0))
    assert ranked_percent(model) == approx(80.0)


def test_an_excused_item_is_not_ranked():
    """An excused midterm leaves the other one holding the top weight."""
    model = ranked_course([90.0, None])
    model = course(
        [
            model.assignments[0],
            assignment(id=2, name="Midterm 2", category_id=1, status="excused"),
        ],
        model.categories,
        scheme=WEIGHTED,
    )

    assert ranked_percent(model) == approx(90.0)
    assert current_grade(model).categories[0].rank_weights_applied == ((1, 23.0),)


def test_a_missing_item_ranks_as_zero():
    """missing_as_zero is a real score, so it ranks last at 0%."""
    model = course(
        [
            assignment(id=1, name="Midterm 1", category_id=1, earned=90.0, possible=100.0),
            assignment(
                id=2,
                name="Midterm 2",
                category_id=1,
                possible=100.0,
                status="missing",
                missing_as_zero=True,
            ),
        ],
        [Category(id=1, name="Midterms", weight=40.0, rank_weights=ECON_RANKS)],
        scheme=WEIGHTED,
    )

    # (23 * 90 + 17 * 0) / 40 = 51.75
    assert ranked_percent(model) == approx(51.75)


def test_a_missing_item_not_counted_as_zero_is_simply_unranked():
    model = course(
        [
            assignment(id=1, name="Midterm 1", category_id=1, earned=90.0, possible=100.0),
            assignment(
                id=2,
                name="Midterm 2",
                category_id=1,
                possible=100.0,
                status="missing",
                missing_as_zero=False,
            ),
        ],
        [Category(id=1, name="Midterms", weight=40.0, rank_weights=ECON_RANKS)],
        scheme=WEIGHTED,
    )

    assert ranked_percent(model) == approx(90.0)


def test_more_items_than_rank_weights_warns_and_counts_the_top_ones():
    model = ranked_course([90.0, 80.0, 70.0])

    result = current_grade(model)

    assert ranked_percent(model) == approx(85.75)
    assert any("1 more item(s) than rank weights" in w for w in result.warnings)


def test_extra_credit_in_a_ranked_category_is_reported_not_counted():
    """A bonus has no denominator, and a ranked percentage has no points total
    to add it to, so saying so beats inventing a rule."""
    model = course(
        [
            assignment(id=1, name="Midterm 1", category_id=1, earned=90.0, possible=100.0),
            assignment(id=2, name="Midterm 2", category_id=1, earned=80.0, possible=100.0),
            assignment(
                id=3, name="Bonus", category_id=1, earned=5.0, possible=0.0, extra_credit=True
            ),
        ],
        [Category(id=1, name="Midterms", weight=40.0, rank_weights=ECON_RANKS)],
        scheme=WEIGHTED,
    )

    result = current_grade(model)

    assert result.categories[0].percent == approx(85.75)
    assert any("extra-credit item(s) in Midterms" in w for w in result.warnings)


def test_no_graded_items_in_a_ranked_category_is_none():
    model = ranked_course([None, None])

    assert ranked_percent(model) is None


def test_rank_weights_are_ignored_and_reported_in_points_mode():
    model = ranked_course([90.0, 80.0])
    model = course(model.assignments, model.categories, scheme=POINTS)

    result = current_grade(model)

    assert result.percent == approx(85.0)
    assert any("rank weights are ignored in points mode" in w for w in result.warnings)


def test_points_mode_shows_no_ranked_percentage_or_weights():
    """Ignored means ignored: the category must not report 85.75 and "(23%)"
    next to a course grade computed as 85."""
    from gradetracker.engine.calculate import rank_weight_labels

    model = ranked_course([90.0, 80.0])
    model = course(model.assignments, model.categories, scheme=POINTS)

    category_result = current_grade(model).categories[0]

    assert category_result.percent == approx(85.0)
    assert category_result.rank_weights_applied == ()
    assert rank_weight_labels(model.categories[0], category_result) == {}


def test_graded_count_counts_only_the_items_that_received_a_weight():
    model = ranked_course([90.0, 80.0, 70.0])

    assert current_grade(model).categories[0].graded_count == 2


def test_a_zero_rank_weight_is_not_a_counted_item():
    model = ranked_course([90.0, 80.0], rank_weights=(40.0, 0.0), weight=40.0)

    result = current_grade(model).categories[0]

    assert result.graded_count == 1
    assert result.percent == approx(90.0)


def test_so_far_stops_once_every_item_that_exists_is_marked():
    """Three midterm rows against four ranks: finished at three."""
    from gradetracker.engine.calculate import rank_weight_labels

    model = ranked_course([90.0, 80.0, 70.0], MATH_RANKS, weight=60.0)

    labels = rank_weight_labels(model.categories[0], current_grade(model).categories[0])

    assert labels == {1: "20%", 2: "15%", 3: "15%"}


def test_so_far_stays_while_an_item_is_unmarked():
    from gradetracker.engine.calculate import rank_weight_labels

    model = ranked_course([90.0, 80.0, None], MATH_RANKS, weight=60.0)

    labels = rank_weight_labels(model.categories[0], current_grade(model).categories[0])

    assert labels == {1: "20% so far", 2: "15% so far"}


def test_an_excused_item_does_not_keep_a_category_unsettled():
    """Excused work is never going to be marked, so it is not pending."""
    from gradetracker.engine.calculate import rank_weight_labels

    model = course(
        [
            assignment(id=1, name="Midterm 1", category_id=1, earned=90.0, possible=100.0),
            assignment(id=2, name="Midterm 2", category_id=1, status="excused"),
        ],
        [Category(id=1, name="Midterms", weight=40.0, rank_weights=ECON_RANKS)],
        scheme=WEIGHTED,
    )

    labels = rank_weight_labels(model.categories[0], current_grade(model).categories[0])

    assert labels == {1: "23%"}


def test_the_projection_fills_then_ranks():
    """Ungraded midterms are assumed at the category average, then ranked."""
    model = ranked_course([90.0, 80.0, None, None], MATH_RANKS)
    assumed = (20 * 90 + 15 * 80) / 35

    percent = projected_grade(model).categories[0].percent

    # 90 keeps the top weight; the two assumed ones outrank the 80.
    expected = (20 * 90 + 15 * assumed + 15 * assumed + 10 * 80) / 60
    assert percent == approx(expected)


def test_rank_weight_labels_say_so_far_until_the_category_is_complete():
    from gradetracker.engine.calculate import rank_weight_labels

    partial = ranked_course([90.0, None])
    complete = ranked_course([90.0, 80.0])

    assert rank_weight_labels(
        partial.categories[0], current_grade(partial).categories[0]
    ) == {1: "23% so far"}
    assert rank_weight_labels(
        complete.categories[0], current_grade(complete).categories[0]
    ) == {1: "23%", 2: "17%"}


def test_an_unranked_category_has_no_labels():
    from gradetracker.engine.calculate import rank_weight_labels

    model = ranked_course([90.0, 80.0], rank_weights=None, weight=40.0)

    assert rank_weight_labels(
        model.categories[0], current_grade(model).categories[0]
    ) == {}


def test_a_ranked_grade_never_falls_when_a_score_rises():
    """The solver bisects on this monotonicity, and ranking must preserve it.

    Raising one midterm can move it up the ranking, which changes which weight
    every other midterm carries; the course percentage must still not fall.
    """
    previous = None
    for score in range(0, 101, 5):
        model = ranked_course([float(score), 60.0, 85.0, 40.0], MATH_RANKS)
        percent = current_grade(model).percent
        if previous is not None:
            assert percent >= previous - 1e-9, (score, previous, percent)
        previous = percent


def test_what_do_i_need_solves_through_a_ranked_category():
    """One midterm left, and the answer has to respect the ranking."""
    model = ranked_course([90.0, None], weight=100.0)

    answer = required_score(model, 88.0, [2])

    assert answer.status == "ok"
    # Scoring p on the second midterm: if p <= 90 the grade is
    # (23*90 + 17*p)/40, which reaches 88 at p = (88*40 - 23*90)/17 = 85.88...
    assert answer.score_percent == pytest.approx((88.0 * 40 - 23 * 90) / 17, abs=0.01)


def test_dropping_never_hurts():
    """The best drop-n subset is always at least as good as dropping nothing."""
    rng = random.Random(0)

    for _ in range(200):
        count = rng.randint(1, 7)
        items = effective_items(
            [
                Assignment(
                    id=index + 1,
                    name=f"I{index}",
                    earned=round(rng.uniform(0, 10), 2),
                    possible=rng.choice([1, 5, 10, 100]),
                    status="graded",
                )
                for index in range(count)
            ]
        )
        base_possible = sum(item.possible for item in items)
        if base_possible <= 0:
            continue
        base = sum(item.earned for item in items) / base_possible * 100.0

        for n in (1, 2):
            kept, _ = drop_lowest(items, n)
            possible = sum(item.possible for item in kept)
            if possible <= 0:
                continue
            after = sum(item.earned for item in kept) / possible * 100.0
            assert after >= base - 1e-9, (base, after, n)
