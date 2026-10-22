"""Pure dataclasses: Course, Category, Assignment, Scheme (M5).

No I/O and no sqlite import anywhere in gradetracker/engine, so the grade maths
is fully unit-testable and reusable by the UI. storage/load.py does the
database-to-model translation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A grade that has not been earned yet is not a zero, and an excused item is not
# a zero either. The engine treats each of these differently.
GRADED = "graded"
SUBMITTED = "submitted"
UNGRADED = "ungraded"
MISSING = "missing"
EXCUSED = "excused"

# The standard scale, used until a syllabus is confirmed.
DEFAULT_CUTOFFS: tuple[tuple[str, float], ...] = (
    ("A", 93.0),
    ("A-", 90.0),
    ("B+", 87.0),
    ("B", 83.0),
    ("B-", 80.0),
    ("C+", 77.0),
    ("C", 73.0),
    ("C-", 70.0),
    ("D+", 67.0),
    ("D", 63.0),
    ("D-", 60.0),
    ("F", 0.0),
)


@dataclass(frozen=True)
class Assignment:
    """One graded item, from either platform or added by hand."""

    id: int
    name: str
    category_id: int | None = None
    earned: float | None = None
    possible: float | None = None
    status: str = UNGRADED
    # Adds to earned without adding to possible.
    extra_credit: bool = False
    # When missing, whether it counts as a zero or is simply not counted yet.
    missing_as_zero: bool = True
    # Which platform course this came from. Two rows with the same name from
    # DIFFERENT source courses are probably the same work mirrored to both.
    source_course_id: int | None = None
    # Hidden by hand, e.g. the duplicate copy of a mirrored assignment. Kept in
    # the list so the UI can show it greyed out rather than vanishing.
    excluded: bool = False


@dataclass(frozen=True)
class Category:
    """A grading bucket. weight is None in points-based courses."""

    id: int
    name: str
    weight: float | None = None
    drop_lowest: int = 0
    # Per-rank weights, highest first, for a syllabus that weighs the same kind
    # of work unequally: ECON 166's "the midterm you score highest on is worth
    # 23% and the other 17%". The items are ranked by their own percentage and
    # take these weights in that order. None means the ordinary rule, where the
    # category is simply its total points earned over total possible.
    rank_weights: tuple[float, ...] | None = None


@dataclass(frozen=True)
class Scheme:
    """The confirmed grading rules for a course."""

    mode: str = "points"  # weighted | points
    cutoffs: tuple[tuple[str, float], ...] = DEFAULT_CUTOFFS
    # A flat bonus added to the final percentage, e.g. a course-wide curve.
    bonus_percent: float = 0.0
    notes: str = ""


@dataclass(frozen=True)
class Course:
    """Everything the engine needs about one course."""

    id: int
    code: str
    name: str | None = None
    categories: tuple[Category, ...] = ()
    assignments: tuple[Assignment, ...] = ()
    scheme: Scheme | None = None


@dataclass(frozen=True)
class CategoryResult:
    """One category's contribution, including what a drop rule removed."""

    category_id: int | None
    name: str
    weight: float | None
    earned: float
    possible: float
    percent: float | None
    graded_count: int
    dropped_ids: tuple[int, ...] = ()
    extra_credit_earned: float = 0.0
    # (assignment id, weight) for a ranked category, in rank order, so the UI
    # can show the weight an item actually received.
    rank_weights_applied: tuple[tuple[int, float], ...] = ()
    # Items in this category with no score of their own yet, which is what
    # decides whether a ranked weight is settled or still "so far".
    awaiting_scores: int = 0
    # Problems with this category alone, e.g. more items than rank weights.
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class GradeResult:
    """A computed grade, plus every reason to distrust it.

    A confident-looking wrong number is the worst failure here, so warnings
    travel with the number rather than being logged and forgotten.
    """

    percent: float | None
    letter: str | None
    categories: tuple[CategoryResult, ...] = ()
    warnings: tuple[str, ...] = ()
    mode: str = "points"
    # Items that were filled with an assumed score (projected grades only).
    assumed_ids: tuple[int, ...] = ()
    # Pairs of counted assignments that look like the same work counted twice.
    duplicate_pairs: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class Required:
    """The answer to "what do I need on the final?"."""

    status: str  # ok | impossible | locked_in | not_counted
    score_percent: float | None = None
    per_item: dict[int, float] = field(default_factory=dict)
    target_percent: float | None = None
    letter: str | None = None
    # Why a status was reached, when the bare status would mislead.
    message: str = ""


def letter_for(
    percent: float | None, cutoffs: tuple[tuple[str, float], ...] = DEFAULT_CUTOFFS
) -> str | None:
    """The letter for a percentage, using the first cutoff it reaches.

    Cutoffs are read in descending order, so exactly 90.0 on the standard scale
    is an A- rather than an A. A percentage below every cutoff (which a negative
    curve can produce) earns the bottom of the scale, not no letter at all;
    only an unknown percentage has no letter.
    """
    if percent is None:
        return None
    descending = sorted(cutoffs, key=lambda pair: pair[1], reverse=True)
    for letter, minimum in descending:
        if percent >= minimum:
            return letter
    return descending[-1][0] if descending else None


def cutoff_for(
    letter: str, cutoffs: tuple[tuple[str, float], ...] = DEFAULT_CUTOFFS
) -> float | None:
    """The minimum percentage that earns a letter."""
    wanted = letter.strip().upper()
    for name, minimum in cutoffs:
        if name.strip().upper() == wanted:
            return minimum
    return None
