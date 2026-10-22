"""Pydantic models for grading rules (M6).

Shapes the manual-entry form and validates it. Pydantic enforces the types; the
judgement calls live in validate(), which reports problems and never silently
fixes them, because one wrong weight corrupts every number downstream.

Two severities. Blocking problems make the rules self-contradictory and cannot
be saved: a category with no name or a duplicated one cannot be matched to
assignments, and duplicated or non-descending cutoffs cannot map a percentage to
a letter. Everything else is advisory -- a syllabus really can have weights that
sum to 95% -- so it is shown and can be confirmed through.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field

from gradetracker.engine.models import DEFAULT_CUTOFFS

WEIGHT_TOLERANCE = 0.01


class CategoryDraft(BaseModel):
    """One grading bucket as typed on the form."""

    name: str
    weight: float | None = None
    drop_lowest: int = 0
    # Per-rank weights, highest first: [23, 17] for "the midterm you score
    # highest on is worth 23% and the other 17%". They must add up to this
    # category's weight, because together they ARE its weight.
    rank_weights: list[float] | None = None
    # What the user typed in the rank weights box that could not be read as a
    # number. Kept rather than dropped or silently turned into a zero, so
    # validate() can refuse the draft and name the token back to them.
    rank_weights_invalid: list[str] = Field(default_factory=list)


class CutoffDraft(BaseModel):
    """One row of the letter scale."""

    letter: str
    min_percent: float


def default_cutoffs() -> list[CutoffDraft]:
    """The standard 93/90/87 scale, used until a syllabus says otherwise."""
    return [
        CutoffDraft(letter=letter, min_percent=minimum)
        for letter, minimum in DEFAULT_CUTOFFS
    ]


class RulesDraft(BaseModel):
    """A course's grading rules, before they are confirmed."""

    mode: Literal["weighted", "points"] = "points"
    categories: list[CategoryDraft] = Field(default_factory=list)
    cutoffs: list[CutoffDraft] = Field(default_factory=default_cutoffs)
    bonus_percent: float = 0.0
    notes: str = ""

    def weight_total(self) -> float:
        """The declared weights, treating a missing one as zero."""
        return sum(float(c.weight or 0.0) for c in self.categories)


class RuleWarning(BaseModel):
    """Something worth saying about a draft.

    Named RuleWarning rather than Warning so it cannot be mistaken for, or
    shadow, the builtin exception class.
    """

    field: str
    message: str
    blocking: bool = False


def _normalise(text: str | None) -> str:
    return (text or "").strip().casefold()


def _not_a_number(value: float | None) -> bool:
    """True for NaN and the infinities, which pass every other check.

    abs(nan - 40) > 0.01 is False and nan < 0 is False, so a NaN slips through
    a sum check and a sign check alike and only shows up as "nan%" on the
    dashboard. It has to be refused where it is typed.
    """
    return value is not None and not math.isfinite(float(value))


def _rank_weight_warnings(
    draft: RulesDraft, category: CategoryDraft, label: str
) -> list[RuleWarning]:
    """Everything that makes a ranked category unusable.

    All of these are blocking, because each one makes the rule mean something
    the syllabus does not say. Ranked weights replace the category's own
    weight, so they have to add up to it and have to be real numbers; points
    mode has no weights to replace; a drop rule plus a ranking would decide
    twice which item counts least; and the order is the rule itself, so a list
    that climbs would hand the top weight to the lowest score.
    """
    warnings: list[RuleWarning] = []
    name = category.name or "this category"

    if category.rank_weights_invalid:
        listed = ", ".join(repr(token) for token in category.rank_weights_invalid)
        warnings.append(
            RuleWarning(
                field=f"{label}.rank_weights",
                message=(
                    f"rank weights for {name!r} must be numbers separated by "
                    f"commas; could not read {listed}"
                ),
                blocking=True,
            )
        )

    weights = category.rank_weights
    if not weights:
        return warnings

    unreal = [weight for weight in weights if _not_a_number(weight)]
    if unreal:
        warnings.append(
            RuleWarning(
                field=f"{label}.rank_weights",
                message=f"every rank weight for {name!r} must be a number",
                blocking=True,
            )
        )
        # The sum and order checks below cannot say anything useful about a
        # NaN: every comparison with one is False.
        return warnings

    if draft.mode != "weighted":
        warnings.append(
            RuleWarning(
                field=f"{label}.rank_weights",
                message=(
                    f"rank weights need weighted mode; {name!r} is in points mode, "
                    "where every point already counts once"
                ),
                blocking=True,
            )
        )

    if category.drop_lowest:
        warnings.append(
            RuleWarning(
                field=f"{label}.rank_weights",
                message=(
                    f"{name!r} has rank weights and drops {category.drop_lowest}; "
                    "the lowest rank already carries the smallest weight, so use "
                    "one rule or the other"
                ),
                blocking=True,
            )
        )

    total = sum(float(weight) for weight in weights)
    declared = float(category.weight or 0.0)
    if abs(total - declared) > WEIGHT_TOLERANCE:
        listed = ", ".join(f"{float(weight):g}" for weight in weights)
        warnings.append(
            RuleWarning(
                field=f"{label}.rank_weights",
                message=(
                    f"rank weights for {name!r} add up to {total:g}% ({listed}), "
                    f"not the category's {declared:g}%"
                ),
                blocking=True,
            )
        )

    negative = [weight for weight in weights if float(weight) < 0]
    if negative:
        warnings.append(
            RuleWarning(
                field=f"{label}.rank_weights",
                message=f"rank weights for {name!r} cannot be negative",
                blocking=True,
            )
        )

    climbing = [
        (index, weights[index], weights[index + 1])
        for index in range(len(weights) - 1)
        if float(weights[index + 1]) > float(weights[index])
    ]
    if climbing:
        _, lower, higher = climbing[0]
        warnings.append(
            RuleWarning(
                field=f"{label}.rank_weights",
                message=(
                    f"rank weights for {name!r} must not climb: {float(higher):g} "
                    f"comes after {float(lower):g}. List the highest weight first, "
                    "because the first weight goes to the highest score"
                ),
                blocking=True,
            )
        )

    return warnings


def validate(draft: RulesDraft) -> list[RuleWarning]:
    """Every problem with a draft, worst first. Fixes nothing."""
    warnings: list[RuleWarning] = []

    # --- categories ---
    if not draft.categories:
        warnings.append(
            RuleWarning(
                field="categories",
                message="no categories: every assignment will be uncategorised",
            )
        )

    seen: dict[str, int] = {}
    for index, category in enumerate(draft.categories):
        label = f"categories[{index}]"

        if not (category.name or "").strip():
            warnings.append(
                RuleWarning(
                    field=f"{label}.name",
                    message="category name is empty",
                    blocking=True,
                )
            )
        else:
            key = _normalise(category.name)
            if key in seen:
                warnings.append(
                    RuleWarning(
                        field=f"{label}.name",
                        message=(
                            f"duplicate category name {category.name!r} "
                            f"(also row {seen[key] + 1})"
                        ),
                        blocking=True,
                    )
                )
            else:
                seen[key] = index

        if category.drop_lowest < 0:
            warnings.append(
                RuleWarning(
                    field=f"{label}.drop_lowest",
                    message=(
                        f"drop lowest is {category.drop_lowest}, which is negative; "
                        "it will be treated as 0"
                    ),
                )
            )

        if _not_a_number(category.weight):
            warnings.append(
                RuleWarning(
                    field=f"{label}.weight",
                    message=f"the weight for {category.name!r} must be a number",
                    blocking=True,
                )
            )

        if draft.mode == "weighted" and category.weight is None:
            warnings.append(
                RuleWarning(
                    field=f"{label}.weight",
                    message=(
                        f"no weight set for {category.name!r}; it will not count "
                        "toward the grade"
                    ),
                )
            )

        warnings.extend(_rank_weight_warnings(draft, category, label))

    # --- weights ---
    if draft.mode == "weighted" and draft.categories:
        total = draft.weight_total()
        if abs(total - 100.0) > WEIGHT_TOLERANCE:
            warnings.append(
                RuleWarning(
                    field="categories",
                    message=(
                        f"weights sum to {total:g}%, not 100%; they will be "
                        "renormalised over the categories with graded work"
                    ),
                )
            )
    elif draft.mode == "points":
        weighted = [c.name for c in draft.categories if c.weight is not None]
        if weighted:
            warnings.append(
                RuleWarning(
                    field="categories",
                    message=(
                        "weights are ignored in points mode (set on "
                        + ", ".join(repr(name) for name in weighted)
                        + ")"
                    ),
                )
            )

    # --- cutoffs ---
    if not draft.cutoffs:
        warnings.append(
            RuleWarning(
                field="cutoffs",
                message="no letter cutoffs: the standard scale will be used",
            )
        )

    if _not_a_number(draft.bonus_percent):
        warnings.append(
            RuleWarning(
                field="bonus_percent",
                message="the bonus percentage must be a number",
                blocking=True,
            )
        )

    letters: dict[str, int] = {}
    previous: float | None = None
    for index, cutoff in enumerate(draft.cutoffs):
        label = f"cutoffs[{index}]"

        if _not_a_number(cutoff.min_percent):
            warnings.append(
                RuleWarning(
                    field=f"{label}.min_percent",
                    message=f"the minimum for {cutoff.letter!r} must be a number",
                    blocking=True,
                )
            )

        key = _normalise(cutoff.letter)
        if not key:
            warnings.append(
                RuleWarning(
                    field=f"{label}.letter", message="letter is empty", blocking=True
                )
            )
        elif key in letters:
            warnings.append(
                RuleWarning(
                    field=f"{label}.letter",
                    message=(
                        f"duplicate letter {cutoff.letter!r} (also row {letters[key] + 1})"
                    ),
                    blocking=True,
                )
            )
        else:
            letters[key] = index

        if previous is not None and cutoff.min_percent >= previous:
            warnings.append(
                RuleWarning(
                    field=f"{label}.min_percent",
                    message=(
                        f"cutoffs must descend: {cutoff.letter!r} at "
                        f"{cutoff.min_percent:g}% does not come below {previous:g}%"
                    ),
                    blocking=True,
                )
            )
        previous = cutoff.min_percent

    return sorted(warnings, key=lambda warning: not warning.blocking)


def has_blocking(warnings: list[RuleWarning]) -> bool:
    """True when the draft cannot be saved as it stands."""
    return any(warning.blocking for warning in warnings)
