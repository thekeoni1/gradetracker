"""'What do I need to reach this target' solver (M5).

With one unknown the algebra rearranges directly, but with several the PDF asks
for a common score across them. The grade is monotone non-decreasing in that
common score, so bisection finds the smallest sufficient score and handles one
unknown and many with the same code path.

That monotonicity is load-bearing. It holds because the grade is a maximum over
drop sets of functions each non-decreasing in every score -- which is only true
once the drop sets are chosen against the grade the mode actually computes (see
calculate.choose_points_drops).
"""

from __future__ import annotations

from gradetracker.engine.calculate import (
    WEIGHTED,
    _possible_of,
    current_grade,
    projected_grade,
    projection_overrides,
)
from gradetracker.engine.models import EXCUSED, Course, Required, cutoff_for

IMPOSSIBLE = "impossible"
LOCKED_IN = "locked_in"
NOT_COUNTED = "not_counted"
OK = "ok"

_TOLERANCE = 1e-4
_MAX_ITERATIONS = 200
# Below this, scoring 0 and scoring 100 on the unknowns are the same grade.
_NO_EFFECT = 1e-9


def _grade_at(
    course: Course,
    base_overrides: dict[int, float],
    unknowns: dict[int, float],
    percent: float,
) -> float | None:
    """The course percentage when every unknown scores `percent`."""
    overrides = dict(base_overrides)
    for assignment_id, possible in unknowns.items():
        overrides[assignment_id] = percent / 100.0 * possible
    return current_grade(course, overrides).percent


def _why_not_counted(course: Course, unknown_ids: set[int], mode: str) -> str:
    """Name the reason an unknown cannot move the grade.

    Reporting "impossible" or "locked_in, score 0" here would be technically
    true and thoroughly misleading.
    """
    chosen = [a for a in course.assignments if a.id in unknown_ids]
    if chosen and all(a.status == EXCUSED or a.excluded for a in chosen):
        return (
            "every item you chose is excused or excluded, so no score on it can "
            "change the grade"
        )
    if mode == WEIGHTED:
        weights = {category.id: category.weight for category in course.categories}
        if chosen and all(weights.get(a.category_id) in (None, 0) for a in chosen):
            return (
                "the category holding your chosen item has no weight, so no score "
                "on it can change the grade"
            )
    return "no score on the item you chose can change this grade"


def required_score(
    course: Course,
    target_percent: float,
    unknown_ids,
    assumptions: dict[int, float] | None = None,
) -> Required:
    """The common percentage the unknown items need to reach a target.

    Every other unscored item uses the same projection assumptions as
    projected_grade, so this answers "given how the term is going".
    """
    wanted = set(unknown_ids)
    unknowns = {
        assignment.id: _possible_of(assignment)
        for assignment in course.assignments
        if assignment.id in wanted and _possible_of(assignment) > 0
    }

    if not unknowns:
        matched = [a for a in course.assignments if a.id in wanted]
        if matched:
            # The items exist but carry no countable points (zero possible, or
            # extra credit, which adds to earned without a denominator), so no
            # score on them can move the grade.
            return Required(
                status=NOT_COUNTED,
                target_percent=target_percent,
                message=(
                    "the item you chose carries no points toward the total, so no "
                    "score on it can change the grade"
                ),
            )

        # The ids belong to no assignment in this course, so there is nothing to
        # solve for. Judge the same projected grade every other branch uses.
        projected = projected_grade(course, assumptions).percent
        status = (
            LOCKED_IN
            if projected is not None and projected >= target_percent
            else IMPOSSIBLE
        )
        return Required(
            status=status,
            target_percent=target_percent,
            message=(
                "no unknown item was chosen, so this compares the target against "
                "the projected grade"
            ),
        )

    base_overrides, _ = projection_overrides(course, assumptions, exclude=set(unknowns))

    best = _grade_at(course, base_overrides, unknowns, 100.0)
    worst = _grade_at(course, base_overrides, unknowns, 0.0)
    mode = current_grade(course).mode

    if best is None:
        return Required(
            status=NOT_COUNTED,
            target_percent=target_percent,
            message="this course has no graded points, so there is nothing to solve",
        )

    # Check this before impossible/locked_in: an item that cannot move the grade
    # would otherwise be reported as one or the other, both misleading.
    if worst is not None and abs(best - worst) < _NO_EFFECT:
        return Required(
            status=NOT_COUNTED,
            target_percent=target_percent,
            message=_why_not_counted(course, set(unknowns), mode),
        )

    if best < target_percent:
        return Required(status=IMPOSSIBLE, target_percent=target_percent)
    if worst is not None and worst >= target_percent:
        return Required(
            status=LOCKED_IN,
            score_percent=0.0,
            per_item={aid: 0.0 for aid in unknowns},
            target_percent=target_percent,
        )

    low, high = 0.0, 100.0
    for _ in range(_MAX_ITERATIONS):
        if high - low < _TOLERANCE:
            break
        middle = (low + high) / 2.0
        grade = _grade_at(course, base_overrides, unknowns, middle)
        if grade is not None and grade >= target_percent:
            high = middle
        else:
            low = middle

    return Required(
        status=OK,
        score_percent=high,
        per_item={aid: high / 100.0 * possible for aid, possible in unknowns.items()},
        target_percent=target_percent,
    )


def required_for_letter(
    course: Course,
    letter: str,
    unknown_ids,
    assumptions: dict[int, float] | None = None,
) -> Required:
    """The same question phrased as a target letter, using the course's scheme."""
    cutoffs = course.scheme.cutoffs if course.scheme is not None else None
    minimum = cutoff_for(letter, cutoffs) if cutoffs else cutoff_for(letter)

    if minimum is None:
        return Required(
            status=IMPOSSIBLE,
            letter=letter,
            message=f"{letter!r} is not on this course's grading scale",
        )

    result = required_score(course, minimum, unknown_ids, assumptions)
    return Required(
        status=result.status,
        score_percent=result.score_percent,
        per_item=result.per_item,
        target_percent=minimum,
        letter=letter,
        message=result.message,
    )
