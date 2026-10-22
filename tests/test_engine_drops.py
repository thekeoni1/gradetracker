"""Points-mode drop selection and solver-status tests (M5 fixes).

The headline here is B1: choosing drops to maximise each CATEGORY's percentage
is wrong in points mode, where the grade is one ratio over the whole course. It
made the grade fall when a score rose, which also broke the solver's
monotonicity assumption.
"""

from __future__ import annotations

import itertools
import random

import pytest

from gradetracker.engine import calculate
from gradetracker.engine.calculate import (
    choose_points_drops,
    current_grade,
    projection_overrides,
)
from gradetracker.engine.models import Assignment, Category, Course, Scheme
from gradetracker.engine.solve import _grade_at, required_score

POINTS = Scheme(mode="points")
WEIGHTED = Scheme(mode="weighted")


# --- the reported failing input -------------------------------------------


def b1_course(c_earned: float, status: str = "graded") -> Course:
    """Cat A (drop 1): 0/1, 50/100, c/10.  Cat B: 0/1000."""
    return Course(
        id=1,
        code="BUG 101",
        categories=(
            Category(1, "Cat A", weight=None, drop_lowest=1),
            Category(2, "Cat B", weight=None, drop_lowest=0),
        ),
        assignments=(
            Assignment(1, "a", category_id=1, earned=0, possible=1, status="graded"),
            Assignment(2, "b", category_id=1, earned=50, possible=100, status="graded"),
            Assignment(3, "c", category_id=1, earned=c_earned, possible=10, status=status),
            Assignment(4, "d", category_id=2, earned=0, possible=1000, status="graded"),
        ),
        scheme=POINTS,
    )


def test_b1_points_mode_drops_are_chosen_for_the_course_not_the_category():
    """The exact input from the report.

    With c = 0/10 the best course ratio is 4.5413% (drop c), and with c = 10/10
    it is 5.4054% (drop a = 0/1). The old per-category rule dropped the 50/100
    at c = 10/10 and scored 0.9891%, so raising c LOWERED the grade.
    """
    low = current_grade(b1_course(0.0))
    high = current_grade(b1_course(10.0))

    assert low.percent == pytest.approx(4.5413, abs=1e-3)
    assert high.percent == pytest.approx(5.4054, abs=1e-3)
    assert high.percent > low.percent  # monotone again

    # c itself is the right thing to drop when it is a zero.
    assert low.categories[0].dropped_ids == (3,)
    # Once c is perfect, the 0/1 is the cheapest thing to lose.
    assert high.categories[0].dropped_ids == (1,)


def test_b1_solver_no_longer_says_impossible():
    """Scoring 0 on c already clears 3%, so the target is locked in."""
    model = b1_course(0.0, status="ungraded")

    required = required_score(model, 3.0, [3])

    assert required.status == "locked_in"


def test_b1_weighted_mode_is_unchanged():
    """Weighted mode keeps the per-category objective, which is correct there."""
    model = Course(
        id=1,
        code="BUG 101",
        categories=(
            Category(1, "Cat A", weight=50, drop_lowest=1),
            Category(2, "Cat B", weight=50, drop_lowest=0),
        ),
        assignments=b1_course(10.0).assignments,
        scheme=WEIGHTED,
    )

    result = current_grade(model)
    # Cat A maximises its own percentage by dropping the 50/100: 10/11 = 90.9%.
    assert result.categories[0].dropped_ids == (2,)
    assert result.categories[0].percent == pytest.approx(10 / 11 * 100, abs=1e-6)


# --- Dinkelbach vs brute force --------------------------------------------


def brute_force_points_optimum(course: Course) -> float | None:
    """The true maximum over the product of every category's drop choices."""
    by_category, loose = calculate._partition(course, None)
    drop_counts = {c.id: c.drop_lowest for c in course.categories}

    options: list[list[tuple[int, ...]]] = []
    for category_id, items in by_category.items():
        droppable = calculate._droppable(items, drop_counts[category_id])
        if not droppable:
            options.append([()])
            continue
        n = drop_counts[category_id]
        options.append(
            [
                tuple(item.id for item in combo)
                for combo in itertools.combinations(droppable, n)
            ]
        )

    every = loose + [item for items in by_category.values() for item in items]
    best: float | None = None
    for choice in itertools.product(*options):
        dropped = {item_id for ids in choice for item_id in ids}
        kept = [item for item in every if item.id not in dropped]
        possible = sum(item.possible for item in kept)
        if possible <= 0:
            continue
        ratio = sum(item.earned for item in kept) / possible * 100.0
        if best is None or ratio > best:
            best = ratio
    return best


def small_points_course(rng: random.Random) -> Course:
    """A small course, biased toward the shapes where the two rules diverge.

    Unequal point values inside a category with a drop rule, plus a large item
    elsewhere to pull the course ratio away from the category ratio.
    """
    categories = []
    assignments = []
    next_id = 1

    for index in range(rng.randint(1, 3)):
        drop = rng.choice([0, 1, 1, 2])
        categories.append(Category(index + 1, f"C{index}", weight=None, drop_lowest=drop))
        for _ in range(rng.randint(1, 4)):
            possible = rng.choice([1, 1, 5, 10, 100, 1000])
            assignments.append(
                Assignment(
                    next_id,
                    f"I{next_id}",
                    category_id=index + 1,
                    earned=round(rng.uniform(0, possible), 2),
                    possible=possible,
                    status="graded",
                    extra_credit=rng.random() < 0.08,
                )
            )
            next_id += 1

    # Sometimes a loose item, which has no drop rule but joins the ratio.
    if rng.random() < 0.3:
        assignments.append(
            Assignment(
                next_id,
                "loose",
                category_id=None,
                earned=round(rng.uniform(0, 50), 2),
                possible=rng.choice([10, 500]),
                status="graded",
            )
        )

    return Course(
        id=1,
        code="RAND 1",
        categories=tuple(categories),
        assignments=tuple(assignments),
        scheme=POINTS,
    )


def test_dinkelbach_matches_the_brute_force_optimum():
    """300 seeded courses: the iteration must find the exact maximum."""
    rng = random.Random(0)
    compared = 0

    for _ in range(300):
        model = small_points_course(rng)
        expected = brute_force_points_optimum(model)
        actual = current_grade(model).percent

        if expected is None:
            assert actual is None
            continue
        assert actual == pytest.approx(expected, abs=1e-9), (
            expected,
            actual,
            [(a.id, a.earned, a.possible, a.category_id) for a in model.assignments],
        )
        compared += 1

    assert compared > 250  # the generator really did produce gradable courses


def test_dinkelbach_returns_a_feasible_drop_set():
    """A drop rule is a syllabus policy, not an option: n really are dropped."""
    model = Course(
        id=1,
        code="X",
        categories=(Category(1, "Quizzes", weight=None, drop_lowest=1),),
        assignments=(
            Assignment(1, "Q1", category_id=1, earned=10, possible=10, status="graded"),
            Assignment(2, "Q2", category_id=1, earned=10, possible=10, status="graded"),
            Assignment(3, "E", category_id=None, earned=0, possible=1000, status="graded"),
        ),
        scheme=POINTS,
    )

    result = current_grade(model)

    # Dropping a perfect quiz lowers the grade here, but the rule still applies.
    assert len(result.categories[0].dropped_ids) == 1
    assert result.percent == pytest.approx(10 / 1010 * 100, abs=1e-9)


def test_choose_points_drops_respects_the_too_few_items_rule():
    items = calculate.effective_items(
        [
            Assignment(1, "Q1", category_id=1, earned=5, possible=10, status="graded"),
            Assignment(2, "Q2", category_id=1, earned=10, possible=10, status="graded"),
        ]
    )

    drops = choose_points_drops({1: items}, {1: 2}, [])

    assert drops[1] == ()


def test_choose_points_drops_never_drops_extra_credit():
    items = calculate.effective_items(
        [
            Assignment(1, "Q1", category_id=1, earned=0, possible=10, status="graded"),
            Assignment(2, "Q2", category_id=1, earned=10, possible=10, status="graded"),
            Assignment(3, "B", category_id=1, earned=5, possible=0, status="graded", extra_credit=True),
        ]
    )

    drops = choose_points_drops({1: items}, {1: 1}, [])

    assert drops[1] == (1,)


# --- monotonicity ---------------------------------------------------------


def monotone_probe_course(rng: random.Random) -> Course:
    """A course with graded work, a drop rule on unequal points, and unknowns."""
    categories = (
        Category(1, "Dropped", weight=rng.choice([30, 40, 50]), drop_lowest=rng.choice([1, 1, 2])),
        Category(2, "Heavy", weight=rng.choice([50, 60, 70]), drop_lowest=0),
    )
    assignments = []
    next_id = 1
    for _ in range(rng.randint(3, 5)):
        possible = rng.choice([1, 5, 10, 100])
        assignments.append(
            Assignment(
                next_id,
                f"D{next_id}",
                category_id=1,
                earned=round(rng.uniform(0, possible), 2),
                possible=possible,
                status="graded",
            )
        )
        next_id += 1
    assignments.append(
        Assignment(next_id, "Big", category_id=2, earned=round(rng.uniform(0, 1000), 2),
                   possible=1000, status="graded")
    )
    next_id += 1
    # The unknown the solver will sweep.
    assignments.append(
        Assignment(next_id, "Final", category_id=rng.choice([1, 2]),
                   possible=rng.choice([10, 100, 500]), status="ungraded")
    )

    return Course(
        id=1,
        code="MONO 1",
        categories=categories,
        assignments=tuple(assignments),
        scheme=Scheme(mode=rng.choice(["points", "weighted"])),
    )


def test_grade_at_is_non_decreasing_in_the_unknown_score():
    """The solver bisects assuming this; with drop rules present it must hold."""
    rng = random.Random(1)

    for trial in range(300):
        model = monotone_probe_course(rng)
        unknown = model.assignments[-1]
        unknowns = {unknown.id: float(unknown.possible or 0)}
        base, _ = projection_overrides(model, exclude=set(unknowns))

        previous = None
        for step in range(21):
            percent = step * 5.0
            grade = _grade_at(model, base, unknowns, percent)
            if grade is None:
                continue
            if previous is not None:
                assert grade >= previous - 1e-9, (
                    trial,
                    model.scheme.mode,
                    percent,
                    previous,
                    grade,
                )
            previous = grade


def raisable_course(rng: random.Random) -> Course:
    """Always has graded work, and leans on the drop-rule shapes."""
    categories = (
        Category(1, "Dropped", weight=rng.choice([25, 40]), drop_lowest=rng.choice([1, 1, 2])),
        Category(2, "Heavy", weight=rng.choice([60, 75]), drop_lowest=0),
    )
    assignments = []
    next_id = 1
    for _ in range(rng.randint(3, 6)):
        possible = rng.choice([1, 2, 10, 100])
        assignments.append(
            Assignment(
                next_id,
                f"D{next_id}",
                category_id=1,
                earned=round(rng.uniform(0, possible), 2),
                possible=possible,
                status="graded",
                extra_credit=rng.random() < 0.08,
            )
        )
        next_id += 1
    for _ in range(rng.randint(1, 2)):
        possible = rng.choice([100, 1000])
        assignments.append(
            Assignment(
                next_id,
                f"H{next_id}",
                category_id=2,
                earned=round(rng.uniform(0, possible), 2),
                possible=possible,
                status="graded",
            )
        )
        next_id += 1
    for _ in range(rng.randint(0, 2)):
        assignments.append(
            Assignment(
                next_id,
                f"X{next_id}",
                category_id=rng.choice([1, 2]),
                possible=rng.choice([10, 50]),
                status=rng.choice(["ungraded", "missing", "excused"]),
                missing_as_zero=rng.random() < 0.7,
            )
        )
        next_id += 1

    return Course(
        id=1,
        code="RAISE 1",
        categories=categories,
        assignments=tuple(assignments),
        scheme=Scheme(mode=rng.choice(["points", "weighted"])),
    )


def test_raising_any_score_never_lowers_the_grade():
    """1000 trials over courses that always have graded work."""
    rng = random.Random(2)
    checked = 0

    for _ in range(1000):
        model = raisable_course(rng)
        before = current_grade(model).percent
        if before is None:
            continue

        graded = [a for a in model.assignments if a.status == "graded" and a.earned is not None]
        if not graded:
            continue
        target = rng.choice(graded)
        bump = rng.choice([0.5, 1.0, 5.0, 50.0])
        raised = tuple(
            a if a.id != target.id else Assignment(
                id=a.id,
                name=a.name,
                category_id=a.category_id,
                earned=(a.earned or 0) + bump,
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
                categories=model.categories,
                assignments=raised,
                scheme=model.scheme,
            )
        ).percent

        assert after is not None
        assert after >= before - 1e-9, (model.scheme.mode, before, after, target.id, bump)
        checked += 1

    assert checked > 900
