"""Current and projected grades (M5).

Pure functions over course data: no database, network or UI.

For a weighted course the current grade averages each category's percentage by
its weight, using only categories that have graded work and renormalising those
weights so they sum to 1:

    grade = sum(w_c * pct_c for c in G) / sum(w_c for c in G)

where G is the set of categories with at least one graded item. A points-based
course is simply total earned over total possible.

Drop-lowest is resolved against whatever the grade actually is. In a weighted
course that is the category's own percentage, so each category is optimised
alone. In a points course the grade is one ratio over the whole course, so the
drops are chosen together: see choose_points_drops.
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, replace

from gradetracker.engine.models import (
    EXCUSED,
    GRADED,
    MISSING,
    SUBMITTED,
    UNGRADED,
    Assignment,
    Category,
    CategoryResult,
    Course,
    GradeResult,
    Scheme,
    letter_for,
)

WEIGHTED = "weighted"
POINTS = "points"

# Brute-forcing drop-lowest is exact but combinatorial. Categories are small in
# practice; this cap stops a pathological course from hanging the dashboard.
_MAX_COMBINATIONS = 100_000

# Dinkelbach converges in a handful of rounds; the cap is pure insurance.
_MAX_DINKELBACH_ROUNDS = 50

_PUNCTUATION = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class EffectiveItem:
    """One assignment reduced to the points it actually contributes."""

    id: int
    category_id: int | None
    earned: float
    possible: float
    extra_credit: bool = False
    assumed: bool = False


def _needs_assumption(assignment: Assignment) -> bool:
    """True for items with no real score yet, which what-if can fill in.

    A missing item that counts as a zero is a real zero, not an unknown.
    """
    if assignment.status in (UNGRADED, SUBMITTED):
        return True
    return assignment.status == MISSING and not assignment.missing_as_zero


def _possible_of(assignment: Assignment) -> float:
    """Extra credit adds to earned without adding to possible."""
    if assignment.extra_credit:
        return 0.0
    return float(assignment.possible or 0.0)


def effective_items(
    assignments: tuple[Assignment, ...] | list[Assignment],
    overrides: dict[int, float] | None = None,
) -> list[EffectiveItem]:
    """The per-item view after applying the status rules.

    - excused: removed from both earned and possible
    - excluded: likewise removed, because you hid it by hand
    - ungraded / submitted: excluded, unless an override supplies a score
    - missing: a zero when missing_as_zero, otherwise excluded
    - extra credit: earned counts, possible does not
    - overrides: forces an item in with that earned, as though it were graded

    An item with no possible points and no extra-credit flag is dropped: there
    is no arithmetic to do with it, and guessing would be worse.
    """
    overrides = overrides or {}
    items: list[EffectiveItem] = []

    for assignment in assignments:
        if assignment.status == EXCUSED or assignment.excluded:
            continue

        possible = _possible_of(assignment)
        if possible <= 0 and not assignment.extra_credit:
            continue

        overridden = assignment.id in overrides
        if overridden:
            earned = float(overrides[assignment.id])
        elif assignment.status == GRADED:
            if assignment.earned is None:
                continue
            earned = float(assignment.earned)
        elif assignment.status == MISSING and assignment.missing_as_zero:
            earned = 0.0
        else:
            # Ungraded, submitted, or missing-but-not-counted: nothing to add.
            continue

        items.append(
            EffectiveItem(
                id=assignment.id,
                category_id=assignment.category_id,
                earned=earned,
                possible=possible,
                extra_credit=assignment.extra_credit,
                assumed=overridden,
            )
        )

    return items


def _percent(items: list[EffectiveItem]) -> float | None:
    """Points earned over points possible, or None when nothing counts."""
    possible = sum(item.possible for item in items)
    if possible <= 0:
        return None
    return sum(item.earned for item in items) / possible * 100.0


def _droppable(items: list[EffectiveItem], n: int) -> list[EffectiveItem]:
    """The items a drop rule may remove, or nothing if the rule cannot apply.

    Extra credit is never droppable: removing a bonus can only hurt. With fewer
    than n+1 candidates nothing is dropped, because you cannot drop work that
    has not happened yet and doing so would flatter the grade.
    """
    if n <= 0:
        return []
    candidates = [item for item in items if not item.extra_credit]
    if len(candidates) < n + 1:
        return []
    return candidates


def drop_lowest(
    items: list[EffectiveItem], n: int
) -> tuple[list[EffectiveItem], tuple[int, ...]]:
    """Drop the n items that maximise THIS CATEGORY's percentage.

    Not the n lowest percentages: with unequal point values those differ. Given
    0/1, 50/100 and 10/10, dropping the 0/1 (the lowest percentage) leaves
    60/110 = 54.5%, while dropping the 50/100 leaves 10/11 = 90.9%. So every
    combination is tried.

    This is the right objective in a weighted course, where the category's own
    percentage is what carries its weight. In a points course the grade is one
    ratio over the whole course, and choose_points_drops handles that instead.
    """
    candidates = _droppable(items, n)
    if not candidates:
        return items, ()

    bonuses = [item for item in items if item.extra_credit]

    if math.comb(len(candidates), n) > _MAX_COMBINATIONS:
        # Pathological size: fall back to the lowest percentages, which is the
        # usual intent even if it is not provably optimal.
        ranked = sorted(
            candidates,
            key=lambda item: (item.earned / item.possible) if item.possible else 0.0,
        )
        dropped_ids = tuple(item.id for item in ranked[:n])
        kept = [item for item in items if item.id not in set(dropped_ids)]
        return kept, dropped_ids

    best_kept: list[EffectiveItem] | None = None
    best_dropped: tuple[int, ...] = ()
    best_percent: float | None = None

    for combination in itertools.combinations(range(len(candidates)), n):
        removed = set(combination)
        kept_candidates = [
            item for index, item in enumerate(candidates) if index not in removed
        ]
        kept = kept_candidates + bonuses
        percent = _percent(kept)
        if percent is None:
            # Dropping these would leave nothing to divide by.
            continue
        if best_percent is None or percent > best_percent:
            best_percent = percent
            best_kept = kept
            best_dropped = tuple(candidates[index].id for index in combination)

    if best_kept is None:
        return items, ()
    return best_kept, best_dropped


def choose_points_drops(
    items_by_category: dict[int, list[EffectiveItem]],
    drop_counts: dict[int, int],
    loose_items: list[EffectiveItem],
) -> dict[int, tuple[int, ...]]:
    """Choose every category's drop set together, to maximise the COURSE ratio.

    In points mode the grade is total earned over total possible for the whole
    course, so which item is worth dropping in one category depends on the rest
    of the course. Maximising each category's own percentage can actively lower
    the course grade: with Cat A (drop 1) holding 0/1, 50/100 and 10/10 and
    Cat B holding 0/1000, the per-category optimum drops the 50/100 and scores
    0.99%, while dropping the 0/1 scores 5.41%. That also made the grade fall
    when a score rose, which broke the solver's monotonicity assumption.

    This is a linear-fractional maximisation, so Dinkelbach's method applies:
    for a trial ratio lambda, the kept set maximising sum(earned - lambda *
    possible) is found by dropping, in each category, the n items with the
    largest (lambda * possible - earned) -- the ones that hurt the ratio most at
    that lambda. Recomputing lambda from the chosen set and repeating converges
    to the exact optimum in a few rounds.
    """
    candidates: dict[int, tuple[list[EffectiveItem], int]] = {}
    for category_id, items in items_by_category.items():
        n = drop_counts.get(category_id, 0)
        droppable = _droppable(items, n)
        candidates[category_id] = (droppable, n if droppable else 0)

    every_item = list(loose_items) + [
        item for items in items_by_category.values() for item in items
    ]

    def ratio(dropped_ids: set[int]) -> float | None:
        kept = [item for item in every_item if item.id not in dropped_ids]
        possible = sum(item.possible for item in kept)
        if possible <= 0:
            return None
        return sum(item.earned for item in kept) / possible

    no_drop_ratio = ratio(set())
    lam = 0.0 if no_drop_ratio is None else no_drop_ratio

    best_drops: dict[int, tuple[int, ...]] | None = None
    best_ratio: float | None = None
    seen: set[tuple] = set()

    for _ in range(_MAX_DINKELBACH_ROUNDS):
        drops: dict[int, tuple[int, ...]] = {}
        for category_id, (droppable, n) in candidates.items():
            if n <= 0:
                drops[category_id] = ()
                continue
            # The id breaks ties deterministically, so the loop cannot oscillate
            # between two equally good choices.
            ranked = sorted(
                droppable,
                key=lambda item: (lam * item.possible - item.earned, item.id),
                reverse=True,
            )
            drops[category_id] = tuple(item.id for item in ranked[:n])

        dropped_ids = {item_id for ids in drops.values() for item_id in ids}
        current = ratio(dropped_ids)

        # Every drop set the loop produces is feasible, so any of them beats
        # having no answer. The no-drop set is NOT feasible when a rule applies.
        if best_drops is None or (
            current is not None and (best_ratio is None or current > best_ratio)
        ):
            best_drops, best_ratio = drops, current

        key = tuple(sorted((cid, drops[cid]) for cid in drops))
        if key in seen or current is None:
            break
        seen.add(key)
        if abs(current - lam) < 1e-15:
            break
        lam = current

    if best_drops is None:
        return {category_id: () for category_id in items_by_category}
    return best_drops


def _partition(
    course: Course, overrides: dict[int, float] | None
) -> tuple[dict[int, list[EffectiveItem]], list[EffectiveItem]]:
    """Effective items split per category, plus the uncategorised ones."""
    known = {category.id for category in course.categories}
    by_category: dict[int, list[EffectiveItem]] = {
        category.id: [] for category in course.categories
    }
    loose: list[EffectiveItem] = []

    for item in effective_items(course.assignments, overrides):
        if item.category_id in known:
            by_category[item.category_id].append(item)
        else:
            loose.append(item)

    return by_category, loose


def _item_percent(item: EffectiveItem) -> float:
    return item.earned / item.possible * 100.0


def _ranked_percent(
    items: list[EffectiveItem], rank_weights: tuple[float, ...], category_name: str
) -> tuple[float | None, tuple[tuple[int, float], ...], list[str]]:
    """A percentage from per-item ranks rather than from total points.

    "The midterm you score highest on is worth 23% and the other 17%": the
    items are sorted by their own percentage, highest first, and take the
    weights in that order. The result is

        sum(w_i * p_i) / sum(w_i)

    over the items that received a weight, so two graded midterms out of four
    are judged against the two highest weights instead of against the whole
    category. Dividing by the weights actually used is what makes a term with
    one midterm marked read as that midterm's percentage.

    Equal percentages are ranked by assignment id, so the answer is stable
    rather than dependent on dictionary order. Items beyond the last weight
    receive nothing and are reported in a warning: that is a rule the syllabus
    does not cover, and quietly averaging them in would be a guess.
    """
    warnings: list[str] = []

    bonuses = [item for item in items if item.extra_credit]
    if bonuses:
        warnings.append(
            f"{len(bonuses)} extra-credit item(s) in {category_name} are not counted: "
            "a ranked weighting has no points total to add them to"
        )

    rankable = [item for item in items if not item.extra_credit and item.possible > 0]
    ranked = sorted(rankable, key=lambda item: (-_item_percent(item), item.id))

    applied = tuple(
        (item.id, float(weight)) for item, weight in zip(ranked, rank_weights)
    )
    if len(ranked) > len(rank_weights):
        warnings.append(
            f"{len(ranked) - len(rank_weights)} more item(s) than rank weights in "
            f"{category_name}; the extra item(s) are not counted"
        )

    total = sum(weight for _, weight in applied)
    if total <= 0:
        return None, applied, warnings

    by_id = {item.id: item for item in ranked}
    weighted_sum = sum(
        weight * _item_percent(by_id[item_id]) for item_id, weight in applied
    )
    return weighted_sum / total, applied, warnings


def _build_result(
    category: Category,
    items: list[EffectiveItem],
    dropped_ids: tuple[int, ...],
    ranked: bool = True,
) -> CategoryResult:
    """A CategoryResult for a category whose drop set has already been chosen.

    `ranked` is False in points mode, where the grade is one ratio over the
    whole course and a per-rank weighting has nothing to weigh: computing it
    anyway would print a percentage and per-item weights that the grade does
    not use.
    """
    removed = set(dropped_ids)
    kept = [item for item in items if item.id not in removed]

    percent = _percent(kept)
    applied: tuple[tuple[int, float], ...] = ()
    warnings: list[str] = []
    counted = len(kept)
    if ranked and category.rank_weights:
        percent, applied, warnings = _ranked_percent(
            kept, tuple(category.rank_weights), category.name
        )
        # An item past the last weight, or on a weight of zero, is not counted,
        # so saying "3 item(s)" next to a two-item percentage would be wrong.
        counted = sum(1 for _, weight in applied if weight > 0)

    return CategoryResult(
        category_id=category.id,
        name=category.name,
        weight=category.weight,
        earned=sum(item.earned for item in kept),
        possible=sum(item.possible for item in kept),
        percent=percent,
        graded_count=counted,
        dropped_ids=dropped_ids,
        extra_credit_earned=sum(item.earned for item in kept if item.extra_credit),
        rank_weights_applied=applied,
        warnings=tuple(warnings),
    )


def category_result(
    category: Category,
    assignments: tuple[Assignment, ...] | list[Assignment],
    overrides: dict[int, float] | None = None,
) -> CategoryResult:
    """One category's earned, possible and percentage, after its drop rule.

    Optimises the category alone, which is the weighted-mode objective. Points
    mode goes through current_grade, which chooses every drop set together.
    """
    items = effective_items(
        [a for a in assignments if a.category_id == category.id], overrides
    )
    _, dropped_ids = drop_lowest(items, category.drop_lowest)
    return _build_result(category, items, dropped_ids)


def _awaiting_scores(course: Course) -> dict[int | None, int]:
    """How many items per category still have no score of their own.

    Not the same as "fewer items than rank weights": a course with three
    midterm rows against four ranks is finished once all three are marked,
    and should stop saying "so far" for the rest of the term.
    """
    counts: dict[int | None, int] = {}
    for assignment in course.assignments:
        if assignment.excluded or assignment.status == EXCUSED:
            continue
        if assignment.extra_credit or _possible_of(assignment) <= 0:
            continue
        if not _needs_assumption(assignment):
            continue
        counts[assignment.category_id] = counts.get(assignment.category_id, 0) + 1
    return counts


def rank_weight_labels(category: Category, result: CategoryResult) -> dict[int, str]:
    """Per-assignment weight labels for a ranked category, e.g. {7: "23%"}.

    "so far" while the weights can still move: either a rank is unclaimed or
    an item in the category is still unmarked. Once everything that exists is
    marked, the weights are settled even if there are fewer items than ranks.
    Shared by gt grade and the course page so the two cannot disagree.
    """
    weights = tuple(category.rank_weights or ())
    if not weights or not result.rank_weights_applied:
        return {}

    settled = (
        len(result.rank_weights_applied) >= len(weights) or not result.awaiting_scores
    )
    suffix = "" if settled else " so far"
    return {
        item_id: f"{weight:g}%{suffix}"
        for item_id, weight in result.rank_weights_applied
    }


def _scheme_of(course: Course) -> tuple[Scheme, list[str]]:
    """The course's scheme, or a points-mode default plus a warning."""
    if course.scheme is not None:
        return course.scheme, []
    return Scheme(), ["no confirmed grading rules"]


def _normalise_name(name: str | None) -> str:
    """Casefolded, with punctuation and spacing collapsed, for matching."""
    return _PUNCTUATION.sub(" ", (name or "").casefold()).strip()


def _same_work(left: str, right: str) -> bool:
    """Two normalised names that look like one piece of work, not two.

    Equal, or one is the other with exactly one extra word on the end: a
    Gradescope "Quiz 1 ALL" mirrors a Blackboard "Quiz 1". The extra word has
    to be an addition, which is why "homework 1" and "homework 12" do not
    match -- the last token differs rather than being appended to -- and
    neither do "midterm 1" and "midterm 2".
    """
    if left == right:
        return True
    shorter, longer = sorted((left.split(), right.split()), key=len)
    return len(longer) == len(shorter) + 1 and longer[: len(shorter)] == shorter


def mirrored_pairs(course: Course) -> tuple[tuple[int, int], ...]:
    """Every pair in this course that looks like one piece of work on both platforms.

    Wider than the duplicate warning below in two ways, because this is what
    the preferred-platform rule acts on rather than what the grade complains
    about: category is ignored, since a brand-new twin usually has none yet,
    and so is whether the item counts, since a twin that arrives ungraded
    would otherwise only be caught once it had already doubled the grade.
    """
    named = [
        (_normalise_name(assignment.name), assignment)
        for assignment in course.assignments
        if _normalise_name(assignment.name) and assignment.source_course_id is not None
    ]
    named.sort(key=lambda entry: (entry[0], entry[1].id))

    pairs: list[tuple[int, int]] = []
    for (left_name, left), (right_name, right) in itertools.combinations(named, 2):
        # Two rows from the same platform course are two real assignments that
        # happen to share a name, not a mirrored copy.
        if left.source_course_id == right.source_course_id:
            continue
        if not _same_work(left_name, right_name):
            continue
        pairs.append((left.id, right.id))
    return tuple(pairs)


def _duplicate_pairs(
    course: Course, counted_ids: set[int]
) -> tuple[tuple[tuple[int, int], ...], list[str]]:
    """Counted assignments that look like one piece of work counted twice.

    A course can legitimately draw from both platforms, but if an instructor
    mirrors the same assignment to both, it lands in one category twice and
    doubles its share of the grade. Point values are not compared: the
    "instructor typed the Gradescope score into a Blackboard column" case has
    different points and still doubles the weight.
    """
    # Grouped by category alone, because the names within one no longer have
    # to match exactly for the pair to be the same piece of work.
    groups: dict[int | None, list[tuple[str, Assignment]]] = {}
    for assignment in course.assignments:
        if assignment.id not in counted_ids:
            continue
        name = _normalise_name(assignment.name)
        if not name:
            continue
        groups.setdefault(assignment.category_id, []).append((name, assignment))

    pairs: list[tuple[int, int]] = []
    warnings: list[str] = []

    for _, members in sorted(
        groups.items(), key=lambda item: (item[0] is None, item[0] or 0)
    ):
        if len(members) < 2:
            continue
        for (left_name, left), (right_name, right) in itertools.combinations(
            sorted(members, key=lambda entry: (entry[0], entry[1].id)), 2
        ):
            if not _same_work(left_name, right_name):
                continue
            # Two rows from the SAME platform course are two real assignments
            # that happen to share a name, not a mirrored copy.
            if left.source_course_id is None or right.source_course_id is None:
                continue
            if left.source_course_id == right.source_course_id:
                continue
            pairs.append((left.id, right.id))
            warnings.append(
                f"{left.name!r} (#{left.id}) and {right.name!r} (#{right.id}) came from "
                "different platforms and are both counted; one may be a duplicate"
            )

    return tuple(pairs), warnings


def current_grade(
    course: Course, overrides: dict[int, float] | None = None
) -> GradeResult:
    """The grade from graded work only, plus any overrides supplied.

    Returns percent None when nothing counts yet, rather than a misleading 0.
    """
    scheme, warnings = _scheme_of(course)
    overrides = overrides or {}

    by_category, loose = _partition(course, overrides)
    drop_counts = {category.id: category.drop_lowest for category in course.categories}

    if scheme.mode == WEIGHTED:
        # Each category's own percentage is what carries its weight.
        drops = {
            category_id: drop_lowest(items, drop_counts[category_id])[1]
            for category_id, items in by_category.items()
        }
    else:
        # One ratio over the whole course, so the drops interact.
        drops = choose_points_drops(by_category, drop_counts, loose)

    awaiting = _awaiting_scores(course)
    results = tuple(
        _build_result(
            category,
            by_category[category.id],
            drops.get(category.id, ()),
            ranked=scheme.mode == WEIGHTED,
        )
        for category in course.categories
    )
    results = tuple(
        replace(result, awaiting_scores=awaiting.get(result.category_id, 0))
        for result in results
    )
    for result in results:
        warnings.extend(result.warnings)

    ranked_names = [
        category.name for category in course.categories if category.rank_weights
    ]
    if ranked_names and scheme.mode != WEIGHTED:
        warnings.append(
            "rank weights are ignored in points mode (set on "
            + ", ".join(repr(name) for name in ranked_names)
            + ")"
        )

    if scheme.mode == WEIGHTED:
        percent, mode_warnings = _weighted_percent(results, loose)
    else:
        percent, mode_warnings = _points_percent(results, loose)
    warnings.extend(mode_warnings)

    if percent is not None and scheme.bonus_percent:
        percent += scheme.bonus_percent

    # An item with points but nothing to divide by is silently uncountable.
    unusable = [
        a
        for a in course.assignments
        if a.status == GRADED
        and a.earned is not None
        and not a.extra_credit
        and not a.excluded
        and not (a.possible or 0) > 0
    ]
    if unusable:
        warnings.append(
            f"{len(unusable)} graded assignment(s) have no point value and are not counted"
        )

    # Which ids actually reached the grade, so duplicates are only flagged when
    # they really are being counted twice.
    dropped = {item_id for ids in drops.values() for item_id in ids}
    counted = {
        item.id
        for items in by_category.values()
        for item in items
        if item.id not in dropped
    }
    if scheme.mode != WEIGHTED:
        counted |= {item.id for item in loose}

    pairs, duplicate_warnings = _duplicate_pairs(course, counted)
    warnings.extend(duplicate_warnings)

    return GradeResult(
        percent=percent,
        letter=letter_for(percent, scheme.cutoffs),
        categories=results,
        warnings=tuple(warnings),
        mode=scheme.mode,
        duplicate_pairs=pairs,
    )


def _weighted_percent(
    results: tuple[CategoryResult, ...], loose: list[EffectiveItem]
) -> tuple[float | None, list[str]]:
    """Weighted average over categories that have graded work.

    Weights are renormalised across those categories, so a term that has only
    touched two of five buckets still reports a meaningful number.
    """
    warnings: list[str] = []

    if loose:
        warnings.append(
            f"{len(loose)} graded assignments have no category and are not counted"
        )

    declared = [
        float(result.weight) for result in results if result.weight is not None
    ]
    if declared and abs(sum(declared) - 100.0) > 0.01:
        warnings.append(
            f"category weights sum to {sum(declared):g}%, not 100%; "
            "they are renormalised over the categories with graded work"
        )

    graded = [result for result in results if result.percent is not None]
    missing_weights = [result.name for result in graded if result.weight in (None, 0)]
    if missing_weights:
        warnings.append("no weight set for " + ", ".join(sorted(missing_weights)))

    total_weight = sum(float(result.weight or 0.0) for result in graded)
    if total_weight <= 0:
        return None, warnings

    weighted_sum = sum(
        float(result.weight or 0.0) * float(result.percent) for result in graded
    )
    return weighted_sum / total_weight, warnings


def _points_percent(
    results: tuple[CategoryResult, ...], loose: list[EffectiveItem]
) -> tuple[float | None, list[str]]:
    """Total earned over total possible, drop rules already applied."""
    earned = sum(result.earned for result in results) + sum(
        item.earned for item in loose
    )
    possible = sum(result.possible for result in results) + sum(
        item.possible for item in loose
    )
    if possible <= 0:
        return None, []
    return earned / possible * 100.0, []


def projection_overrides(
    course: Course,
    assumptions: dict[int, float] | None = None,
    exclude: set[int] | frozenset[int] | tuple[int, ...] = (),
) -> tuple[dict[int, float], list[int]]:
    """Assumed scores for every item that has no real one yet.

    An explicit assumption wins. Otherwise the item is filled with its
    category's average percentage, falling back to the course's current
    percentage, and is left out entirely when there is nothing to go on.

    Shared by projected_grade and the solver, so "what if things continue like
    this" means the same thing in both.
    """
    assumptions = assumptions or {}
    excluded = set(exclude)

    current = current_grade(course)
    category_percent = {
        result.category_id: result.percent for result in current.categories
    }

    overrides: dict[int, float] = {}
    assumed: list[int] = []

    for assignment in course.assignments:
        if assignment.id in excluded or not _needs_assumption(assignment):
            continue
        if assignment.excluded or assignment.status == EXCUSED:
            continue
        # Projecting a bonus you have not earned would flatter the grade.
        if assignment.extra_credit:
            continue
        possible = _possible_of(assignment)
        if possible <= 0:
            continue

        if assignment.id in assumptions:
            overrides[assignment.id] = float(assumptions[assignment.id])
        else:
            percent = category_percent.get(assignment.category_id)
            if percent is None:
                percent = current.percent
            if percent is None:
                continue
            overrides[assignment.id] = percent / 100.0 * possible

        assumed.append(assignment.id)

    return overrides, assumed


def projected_grade(
    course: Course, assumptions: dict[int, float] | None = None
) -> GradeResult:
    """The "if things continue like this" grade.

    Fills every unscored item with an assumption, then applies the weights to
    every category that has any item at all.
    """
    overrides, assumed = projection_overrides(course, assumptions)
    result = current_grade(course, overrides)
    return replace(result, assumed_ids=tuple(assumed))
