"""Typer entry point: gt login | status | sync | serve | link."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import typer

from gradetracker import config

app = typer.Typer(help="Sync and track your Blackboard and Gradescope grades.", no_args_is_help=True)

_SOURCES = ("blackboard", "gradescope")


def _require_bb_base_url() -> str:
    """The configured Blackboard host, or a clear error and exit 1."""
    if not config.BB_BASE_URL:
        typer.echo(
            "BB_BASE_URL is not set. Copy .env.example to .env and set it to "
            "Hamilton's Blackboard host, e.g. https://blackboard.hamilton.edu",
            err=True,
        )
        raise typer.Exit(1)
    return config.BB_BASE_URL


def _record_login(source: str) -> None:
    """Note a fresh login, which opens a session span for the OAuth numbers.

    Never fatal: a login that worked must not be reported as failed because
    the bookkeeping did not.
    """
    from gradetracker.storage import connections, db

    try:
        with db.connection() as conn:
            connections.record_login(conn, source)
    except Exception as exc:
        typer.echo(f"(could not record the login: {exc})", err=True)


def _login_gradescope() -> None:
    """Prompt for credentials, prove they work, then store them in the keychain."""
    from gradetracker.auth import gradescope_auth
    from gradetracker.auth.base import AuthExpired

    email = typer.prompt("Gradescope email", default=config.GS_EMAIL or None)
    password = typer.prompt("Gradescope password", hide_input=True)

    try:
        # Verify before storing, so a typo never lands in the keychain.
        gradescope_auth.verify_credentials(email, password)
    except AuthExpired as exc:
        from gradetracker.redact import scrub

        typer.echo(f"Gradescope login failed: {scrub(str(exc))}", err=True)
        typer.echo("Nothing was saved. Check the password and try again.", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        # Class name only: on the login path the exception text can contain the
        # password, because the library puts credentials in the request URL.
        from gradetracker.redact import safe_error

        typer.echo(f"Could not reach Gradescope ({safe_error(exc)})", err=True)
        raise typer.Exit(1)

    gradescope_auth.store_credentials(email, password)
    _record_login("gradescope")
    typer.echo("Gradescope login saved.")


@app.command()
def login(source: str = typer.Argument(..., help="blackboard | gradescope")) -> None:
    """Authenticate a source: a browser window for Blackboard, a prompt for Gradescope."""
    source = source.lower()
    if source not in _SOURCES:
        typer.echo(f"Unknown source {source!r}. Choose blackboard or gradescope.", err=True)
        raise typer.Exit(1)

    if source == "gradescope":
        _login_gradescope()
        return

    base_url = _require_bb_base_url()
    config.ensure_dirs()

    # Imported here so `gt status` and the other commands don't pay for Playwright.
    from gradetracker.auth import launcher

    typer.echo("Opening a browser window. Log in and approve Duo, then leave it to me.")
    ok = launcher.login_blackboard(
        base_url=base_url,
        check_path=config.BB_CHECK_PATH,
        profile_dir=config.BB_PROFILE_DIR,
        channel=config.BB_BROWSER_CHANNEL,
    )
    if ok:
        launcher.write_login_timestamp(config.BB_PROFILE_DIR)
        _record_login("blackboard")
        typer.echo("Blackboard login saved.")
        return
    typer.echo("Timed out after 5 minutes waiting for login.", err=True)
    raise typer.Exit(1)


@app.command()
def status() -> None:
    """Report whether each saved session is still valid."""
    if not config.BB_BASE_URL:
        typer.echo("blackboard: BB_BASE_URL not set")
    else:
        from gradetracker.auth import launcher
        from gradetracker.auth.cookie_auth import CookieAuth

        auth = CookieAuth(
            base_url=config.BB_BASE_URL,
            check_path=config.BB_CHECK_PATH,
            profile_dir=config.BB_PROFILE_DIR,
            channel=config.BB_BROWSER_CHANNEL,
        )
        state = auth.check()
        if state == "valid":
            since = launcher.read_login_timestamp(config.BB_PROFILE_DIR)
            suffix = f" (logged in {since})" if since else ""
            typer.echo(f"blackboard: valid session{suffix}")
        elif state == "offline":
            host = urlparse(config.BB_BASE_URL).netloc
            typer.echo(f"blackboard: could not reach {host} (offline?)")
        else:
            typer.echo("blackboard: needs login (run gt login blackboard)")

    from gradetracker.auth.gradescope_auth import GradescopeAuth

    gs = GradescopeAuth()
    gs_state = gs.check()
    if gs_state == "valid":
        typer.echo(f"gradescope: valid (as {gs.email})")
    elif gs_state == "offline":
        typer.echo("gradescope: could not reach gradescope.com (offline?)")
    else:
        typer.echo("gradescope: needs login (run gt login gradescope)")


@app.command()
def sync(
    source: str = typer.Option(None, "--source", help="blackboard | gradescope; default both"),
    raw_only: bool = typer.Option(False, "--raw-only", help="Fetch and dump raw JSON, then stop."),
) -> None:
    """Fetch from each source, dump raw, then normalize into the database."""
    from gradetracker.sync import SOURCES, run_sync

    sources = [source.lower()] if source else list(SOURCES)
    code = run_sync(sources, raw_only=raw_only)
    if code:
        raise typer.Exit(code)


@app.command()
def grade(
    course_code: str = typer.Argument(None, help="e.g. ECON 166. Omit for every course."),
) -> None:
    """Show the current and projected grade for a course, or for all of them."""
    from gradetracker.engine.calculate import current_grade, projected_grade
    from gradetracker.storage import db, load, mapping, rules as rules_store

    config.ensure_dirs()
    with db.connection() as conn:
        if course_code:
            course_id = load.find_course_id(conn, course_code)
            if course_id is None:
                typer.echo(f"No course matching {course_code!r}. Try gt link --list.", err=True)
                raise typer.Exit(1)
            courses = [load.load_course(conn, course_id)]
        else:
            courses = load.load_all_courses(conn)

        if not courses:
            typer.echo("No courses yet. Run gt sync, then gt link.")
            return

        # Gathered inside the connection; printed after, so output stays grouped.
        context = {
            course.id: (
                rules_store.rules_summary(conn, course.id),
                mapping.count_unmapped_graded(conn, course.id),
            )
            for course in courses
        }

    for index, course in enumerate(courses):
        if index:
            typer.echo("")
        summary, unmapped = context[course.id]
        _print_course_grade(
            course,
            current_grade(course),
            projected_grade(course),
            rules_summary=summary,
            unmapped_graded=unmapped,
        )


def _format_percent(percent: float | None) -> str:
    return "--" if percent is None else f"{percent:.2f}%"


def _print_rank_weights(course, result) -> None:
    """The weight each item of a ranked category actually received.

    Ranked weights depend on how the scores came out, so the only way to check
    them against the syllabus is to see which item got which weight.
    """
    from gradetracker.engine.calculate import rank_weight_labels

    if not result.rank_weights_applied:
        return

    category = next(
        (c for c in course.categories if c.id == result.category_id), None
    )
    if category is None:
        return

    labels = rank_weight_labels(category, result)
    assignments = {a.id: a for a in course.assignments}
    for assignment_id, _ in result.rank_weights_applied:
        assignment = assignments.get(assignment_id)
        name = assignment.name if assignment is not None else f"#{assignment_id}"
        score = ""
        if assignment is not None and assignment.earned is not None and assignment.possible:
            score = f"   {assignment.earned:g}/{assignment.possible:g}"
        typer.echo(f"      {name:<16} ({labels[assignment_id]}){score}")


def _print_course_grade(
    course, current, projected, rules_summary: str = "", unmapped_graded: int = 0
) -> None:
    """One block per course: the number, then everything qualifying it."""
    header = f"{course.code}"
    if course.name:
        header += f" - {course.name}"
    typer.echo(header)

    letter = current.letter or "--"
    typer.echo(
        f"  current   {_format_percent(current.percent)} ({letter})"
        f"   projected {_format_percent(projected.percent)} ({projected.letter or '--'})"
        f"   [{current.mode}]"
    )

    for result in current.categories:
        weight = "--" if result.weight is None else f"{result.weight:g}%"
        dropped = f"  (dropped {len(result.dropped_ids)})" if result.dropped_ids else ""
        # Said out loud, because a ranked percentage is deliberately not the
        # points total sitting next to it.
        ranked = ", ranked" if result.rank_weights_applied else ""
        typer.echo(
            f"    {result.name:<18} weight {weight:>6}"
            f"   {result.earned:g}/{result.possible:g}"
            f"   {_format_percent(result.percent):>8}"
            f"   {result.graded_count} item(s){ranked}{dropped}"
        )
        _print_rank_weights(course, result)

    # Trust signals: a confident-looking wrong number is the worst outcome.
    if rules_summary:
        typer.echo(f"    {rules_summary}")
    if unmapped_graded:
        typer.echo(
            f"    ! {unmapped_graded} graded assignment(s) are not mapped to a category"
            f" (run gt map {course.code!r})"
        )
    for warning in current.warnings:
        typer.echo(f"    ! {warning}")

    if current.duplicate_pairs:
        for left, right in current.duplicate_pairs:
            typer.echo(f"      -> gt exclude {right}   (or gt exclude {left})")


RULES_JSON_HELP = """Shape of the --file JSON (see docs/rules-example.json):

\b
{
  "mode": "weighted" | "points",
  "categories": [
    {"name": "Problem Sets", "weight": 40, "drop_lowest": 1},
    {"name": "Midterms", "weight": 40, "rank_weights": [23, 17]}
  ],
  "cutoffs": [{"letter": "A", "min_percent": 93}],
  "bonus_percent": 0,
  "notes": "free text"
}

\b
categories, cutoffs, bonus_percent and notes may all be omitted; cutoffs
default to the standard 93/90/87 scale. weight is ignored in points mode.
rank_weights, highest first, weighs the items of one category unequally
("the better midterm counts 23%, the other 17%") and must add up to that
category's weight.
"""


def _resolve_course(conn, course: str) -> int:
    """A course id from a code the user typed, or a clear error."""
    from gradetracker.storage import load

    course_id = load.find_course_id(conn, course)
    if course_id is None:
        typer.echo(f"No course matching {course!r}. Try gt link --list.", err=True)
        raise typer.Exit(1)
    return course_id


def _print_warnings(warnings) -> None:
    for warning in warnings:
        marker = "BLOCKING" if warning.blocking else "note"
        typer.echo(f"  [{marker}] {warning.field}: {warning.message}")


def _print_rules(draft, status: str, summary: str) -> None:
    typer.echo(summary)
    typer.echo(f"  status: {status}")
    if draft is None:
        typer.echo("  no rules saved yet")
        return

    typer.echo(f"  mode: {draft.mode}")
    if draft.categories:
        for category in draft.categories:
            weight = "--" if category.weight is None else f"{category.weight:g}%"
            drop = f", drop lowest {category.drop_lowest}" if category.drop_lowest else ""
            ranks = ""
            if category.rank_weights:
                listed = ", ".join(f"{float(w):g}" for w in category.rank_weights)
                ranks = f", ranked {listed}"
            typer.echo(f"    {category.name:<20} {weight:>6}{drop}{ranks}")
        if draft.mode == "weighted":
            typer.echo(f"    {'total':<20} {draft.weight_total():g}%")
    else:
        typer.echo("    (no categories)")

    letters = ", ".join(
        f"{cutoff.letter} {cutoff.min_percent:g}" for cutoff in draft.cutoffs
    )
    typer.echo(f"  cutoffs: {letters}")
    if draft.bonus_percent:
        typer.echo(f"  bonus: {draft.bonus_percent:g}%")
    if draft.notes:
        typer.echo(f"  notes: {draft.notes}")


@app.command(help="Enter or review a course's grading rules.\n\n" + RULES_JSON_HELP)
def rules(
    course: str = typer.Argument(..., help="Course code, e.g. ECON 166."),
    file: str = typer.Option(None, "--file", help="Save rules from a JSON file."),
    show: bool = typer.Option(False, "--show", help="Print the current rules and exit."),
) -> None:
    """Manual entry for categories, weights, drop rules and letter cutoffs."""
    from gradetracker.storage import db, rules as rules_store
    from gradetracker.syllabus.schema import RulesDraft, has_blocking, validate

    config.ensure_dirs()
    with db.connection() as conn:
        course_id = _resolve_course(conn, course)
        current = rules_store.load_rules(conn, course_id)

        if show:
            _print_rules(
                current,
                rules_store.rules_status(conn, course_id),
                rules_store.rules_summary(conn, course_id),
            )
            return

        if file:
            path = Path(file)
            try:
                draft = RulesDraft.model_validate_json(path.read_text(encoding="utf-8"))
            except OSError as exc:
                typer.echo(f"Could not read {file}: {exc}", err=True)
                raise typer.Exit(1)
            except ValueError as exc:
                typer.echo(f"{file} is not a valid rules file:\n{exc}", err=True)
                raise typer.Exit(1)
        else:
            draft = _prompt_for_rules(current)

        warnings = validate(draft)
        if warnings:
            typer.echo("Checks:")
            _print_warnings(warnings)

        if has_blocking(warnings):
            typer.echo("Not saved: fix the blocking problems above.", err=True)
            raise typer.Exit(1)

        if not file and not typer.confirm("Confirm these rules?", default=True):
            typer.echo("Not saved.")
            return

        result = rules_store.save_rules(conn, course_id, draft, origin="manual")

    typer.echo(
        f"Saved: {result.created} category(ies) added, {result.updated} updated, "
        f"{result.deleted} removed."
    )
    if result.deleted_names:
        typer.echo(
            f"  removed {', '.join(repr(name) for name in result.deleted_names)}; "
            f"{result.unlinked} assignment(s) unmapped"
            + (f", {result.unlocked} lock(s) cleared" if result.unlocked else "")
        )
    if result.categorised:
        typer.echo(f"  auto-mapped {result.categorised} assignment(s)")


def _prompt_rank_weights():
    """Per-rank weights for one category, or None when the rule does not apply.

    For "the midterm you score highest on is worth 23% and the other 17%",
    which is 23, 17. They have to add up to the category's own weight, and
    validate() says so rather than this prompt, so the whole draft is checked
    in one place.
    """
    while True:
        raw = typer.prompt(
            "    Rank weights, highest first, comma separated (Enter for none)",
            default="",
            show_default=False,
        ).strip()
        if not raw:
            return None
        try:
            return [
                float(part) for part in raw.replace(";", ",").split(",") if part.strip()
            ]
        except ValueError:
            typer.echo("      numbers separated by commas, e.g. 23, 17")


def _prompt_for_rules(current):
    """The interactive form. Every prompt goes through typer so it is testable."""
    from gradetracker.syllabus.schema import (
        CategoryDraft,
        CutoffDraft,
        RulesDraft,
        default_cutoffs,
    )

    if current is not None:
        typer.echo("Current rules:")
        _print_rules(current, "", "")
        typer.echo("")

    default_mode = current.mode if current is not None else "weighted"
    mode = ""
    while mode not in ("weighted", "points"):
        mode = typer.prompt("Mode (weighted/points)", default=default_mode).strip().lower()
        if mode not in ("weighted", "points"):
            typer.echo("  enter 'weighted' or 'points'")

    typer.echo("Add categories. Leave the name blank to finish.")
    categories: list[CategoryDraft] = []
    while True:
        name = typer.prompt(f"  Category {len(categories) + 1} name", default="").strip()
        if not name:
            break

        weight = None
        ranks = None
        if mode == "weighted":
            weight = float(typer.prompt("    weight %", default="0") or 0)
        drop = int(typer.prompt("    drop lowest", default="0") or 0)
        if mode == "weighted":
            ranks = _prompt_rank_weights()

        categories.append(
            CategoryDraft(
                name=name, weight=weight, drop_lowest=drop, rank_weights=ranks
            )
        )
        if mode == "weighted":
            running = sum(float(c.weight or 0) for c in categories)
            typer.echo(f"    running total: {running:g}%")

    cutoffs = current.cutoffs if current is not None else default_cutoffs()
    if typer.confirm("Edit letter cutoffs?", default=False):
        edited: list[CutoffDraft] = []
        for cutoff in cutoffs:
            minimum = float(
                typer.prompt(f"  {cutoff.letter} minimum %", default=f"{cutoff.min_percent:g}")
            )
            edited.append(CutoffDraft(letter=cutoff.letter, min_percent=minimum))
        cutoffs = edited

    bonus = float(
        typer.prompt(
            "Flat bonus % on the final grade",
            default=f"{current.bonus_percent:g}" if current is not None else "0",
        )
        or 0
    )
    notes = typer.prompt(
        "Notes (curves, participation, anything non-mechanical)",
        default=current.notes if current is not None else "",
    )

    return RulesDraft(
        mode=mode,
        categories=categories,
        cutoffs=cutoffs,
        bonus_percent=bonus,
        notes=notes,
    )


# Named map_ so it does not shadow the builtin inside this module; typer takes
# the command name from the decorator.
@app.command("map")
def map_(
    course: str = typer.Argument(..., help="Course code, e.g. ECON 166."),
    auto: bool = typer.Option(False, "--auto", help="Accept every suggestion, unlocked."),
    set_: tuple[int, str] = typer.Option(
        (None, None), "--set", help="ASSIGNMENT_ID CATEGORY_NAME, one locked choice."
    ),
) -> None:
    """Put assignments into categories, one at a time or all at once."""
    from gradetracker.storage import db, mapping

    config.ensure_dirs()
    with db.connection() as conn:
        course_id = _resolve_course(conn, course)

        assignment_id, category_name = set_
        if assignment_id is not None:
            category = mapping.find_category(conn, course_id, category_name)
            if category is None:
                names = [row["name"] for row in mapping.list_categories(conn, course_id)]
                typer.echo(
                    f"No category named {category_name!r}. "
                    + (f"Try one of: {', '.join(names)}" if names else "Run gt rules first."),
                    err=True,
                )
                raise typer.Exit(1)
            mapping.set_category(conn, assignment_id, int(category["id"]), locked=True)
            typer.echo(f"Assignment #{assignment_id} -> {category['name']} (locked).")
            return

        if auto:
            mapped = mapping.accept_all_suggestions(conn, course_id)
            typer.echo(f"Mapped {mapped} assignment(s) from suggestions.")
            remaining = mapping.list_unmapped(conn, course_id)
            if remaining:
                typer.echo(f"{len(remaining)} still unmapped; run gt map {course!r}.")
            return

        _map_interactively(conn, course_id, mapping)


def _map_interactively(conn, course_id: int, mapping) -> None:
    """One prompt per unmapped assignment, with its suggestion offered first."""
    categories = mapping.list_categories(conn, course_id)
    if not categories:
        typer.echo("No categories yet. Run gt rules first.", err=True)
        raise typer.Exit(1)

    unmapped = mapping.list_unmapped(conn, course_id)
    if not unmapped:
        typer.echo("Every assignment is mapped.")
        return

    typer.echo("Categories:")
    for index, category in enumerate(categories, start=1):
        typer.echo(f"  {index}. {category['name']}")

    for row in unmapped:
        suggestion = row["suggested_category_name"]
        hint = f" [suggested: {suggestion}]" if suggestion else ""
        typer.echo(f"\n{row['source']} {row['name']!r}{hint}")

        default = "y" if suggestion else "s"
        answer = typer.prompt(
            "  accept (y) / category number / skip (s)", default=default
        ).strip().lower()

        if answer in ("s", "skip", ""):
            continue
        if answer in ("y", "yes") and row["suggested_category_id"]:
            mapping.set_category(conn, row["id"], row["suggested_category_id"], locked=True)
            typer.echo(f"  -> {suggestion} (locked)")
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(categories):
            chosen = categories[int(answer) - 1]
            mapping.set_category(conn, row["id"], int(chosen["id"]), locked=True)
            typer.echo(f"  -> {chosen['name']} (locked)")
            continue
        typer.echo("  skipped (unrecognised answer)")


@app.command()
def exclude(
    assignment_id: int = typer.Argument(..., help="The assignment id, as gt grade prints it."),
    undo: bool = typer.Option(False, "--undo", help="Count this assignment again."),
) -> None:
    """Stop counting one assignment, e.g. a copy mirrored to both platforms.

    The row stays in the database so it can be shown greyed out and restored.
    """
    from gradetracker.storage import db

    config.ensure_dirs()
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT a.id, a.name, a.excluded, sc.source
            FROM assignments AS a
            JOIN source_courses AS sc ON sc.id = a.source_course_id
            WHERE a.id = ?
            """,
            (assignment_id,),
        ).fetchone()
        if row is None:
            typer.echo(f"No assignment with id {assignment_id}.", err=True)
            raise typer.Exit(1)

        # Marked as yours in both directions, so the preferred-platform rule
        # never excludes a row you have just chosen to count again.
        conn.execute(
            "UPDATE assignments SET excluded = ?, exclusion_source = 'user' WHERE id = ?",
            (0 if undo else 1, assignment_id),
        )
        conn.commit()

    verb = "now counted again" if undo else "excluded"
    typer.echo(f"{row['source']} {row['name']!r} (#{assignment_id}) {verb}.")


@app.command()
def prefer(
    course: str = typer.Argument(..., help="The course code, e.g. MATH 116."),
    platform: str = typer.Argument(..., help="gradescope | blackboard | none"),
) -> None:
    """Choose which platform to keep when both carry the same assignment.

    The other copy is excluded as soon as it is seen, so mirrored work never
    doubles its share of the grade. "none" goes back to being asked.
    """
    from gradetracker.storage import db, duplicates
    from gradetracker.storage import link as linking

    wanted = platform.strip().lower()
    if wanted not in (*duplicates.PLATFORMS, "none"):
        typer.echo(
            f"Unknown platform {platform!r}. Choose gradescope, blackboard or none.",
            err=True,
        )
        raise typer.Exit(1)

    config.ensure_dirs()
    with db.connection() as conn:
        row = linking.find_course_by_code(conn, course)
        if row is None:
            typer.echo(f"No course matching {course!r}. Try gt link --list.", err=True)
            raise typer.Exit(1)

        course_id = int(row["id"])
        duplicates.set_preference(conn, course_id, None if wanted == "none" else wanted)

        if wanted == "none":
            typer.echo(
                f"{row['code']}: no preferred platform; mirrored copies are "
                "flagged rather than excluded. Rows already excluded stay "
                "excluded (gt exclude --undo counts one again)."
            )
            return

        excluded = duplicates.apply_preference(conn, course_id)
        typer.echo(f"{row['code']}: prefers {wanted.title()}.")
        if excluded:
            typer.echo(duplicates.summarise(row["code"], wanted, excluded))
        else:
            typer.echo("Nothing to exclude: no mirrored copy is counted twice.")


schedule_app = typer.Typer(help="Sync on a schedule even when the app is closed.")
app.add_typer(schedule_app, name="schedule")


@schedule_app.command("install")
def schedule_install(
    every_hours: int = typer.Option(4, "--every-hours", help="How often to sync."),
) -> None:
    """Register an OS task that runs gt sync on a schedule."""
    from gradetracker import scheduler

    if not scheduler.IS_WINDOWS:
        typer.echo(scheduler.NON_WINDOWS_HINT, err=True)
        raise typer.Exit(1)

    args = scheduler.build_schtasks_args(every_hours)
    typer.echo(f"Running: {' '.join(args)}")
    result = scheduler.install(every_hours)
    if result.returncode != 0:
        typer.echo(result.stderr.strip() or result.stdout.strip(), err=True)
        typer.echo(
            "Could not create the task. Creating a scheduled task may need an "
            "elevated terminal.",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(result.stdout.strip() or f"Task {scheduler.TASK_NAME!r} created.")


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """Remove the OS task."""
    from gradetracker import scheduler

    if not scheduler.IS_WINDOWS:
        typer.echo(scheduler.NON_WINDOWS_HINT, err=True)
        raise typer.Exit(1)

    result = scheduler.uninstall()
    if result.returncode != 0:
        typer.echo(result.stderr.strip() or result.stdout.strip(), err=True)
        raise typer.Exit(1)
    typer.echo(result.stdout.strip() or f"Task {scheduler.TASK_NAME!r} removed.")


@schedule_app.command("status")
def schedule_status() -> None:
    """Show whether the OS task exists."""
    from gradetracker import scheduler

    if not scheduler.IS_WINDOWS:
        typer.echo(scheduler.NON_WINDOWS_HINT, err=True)
        raise typer.Exit(1)

    result = scheduler.status()
    if result.returncode != 0:
        typer.echo(f"No task named {scheduler.TASK_NAME!r}. Run gt schedule install.")
        return
    typer.echo(result.stdout.strip())


@app.command()
def serve(
    port: int = typer.Option(5000, "--port", help="Port to listen on."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser."),
    no_scheduler: bool = typer.Option(
        False, "--no-scheduler", help="Do not sync automatically every 4 hours."
    ),
    tailscale: bool = typer.Option(
        False,
        "--tailscale",
        help="Also serve to your own devices over Tailscale (e.g. your phone).",
    ),
    host: str = typer.Option(
        None, "--host", help="Listen on one specific address instead of localhost."
    ),
) -> None:
    """Run the dashboard, by default at http://127.0.0.1:<port>."""
    import threading
    import webbrowser

    from gradetracker.web.app import create_app

    address = _bind_address(host, tailscale)
    config.ensure_dirs()
    # The guard in the app refuses any Host it was not bound to, so the
    # address has to be handed to both or the phone gets a 403.
    extra = () if _is_loopback(address) else (address,)
    flask_app = create_app(scheduler=not no_scheduler, extra_hosts=extra)
    url = f"http://{_url_host(address)}:{port}/"

    typer.echo(f"Grade Tracker running at {url}")
    if extra:
        typer.echo(
            "Open that address on your phone. Anything that can reach it can "
            "read your grades: this dashboard has no login."
        )
    if no_scheduler:
        typer.echo("Automatic syncing is off for this run.")
    else:
        typer.echo("Syncing automatically every 4 hours while this is open.")
    typer.echo("Press Ctrl+C to stop.")

    if not no_browser:
        # After the server is listening, not before, or the first load 404s.
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    flask_app.run(host=address, port=port, debug=False)


# A wildcard would listen on every network this machine joins, including
# campus wifi, and the app has no login to put in front of that.
WILDCARD_HOSTS = {"0.0.0.0", "::", "*"}


def _is_loopback(address: str) -> bool:
    import ipaddress

    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return address == "localhost"


def _url_host(address: str) -> str:
    """The address as a URL carries it: IPv6 literals are bracketed."""
    import ipaddress

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    return f"[{parsed}]" if parsed.version == 6 else str(parsed)


def _bind_address(host: str | None, tailscale: bool) -> str:
    """Which address to listen on, or a clear error and exit 1.

    Defaults to loopback, because the dashboard has no authentication and the
    address it listens on is the whole of its access control.
    """
    from gradetracker import tailnet

    if tailscale and host:
        typer.echo("Pass --tailscale or --host, not both.", err=True)
        raise typer.Exit(1)

    if tailscale:
        address = tailnet.find_address()
        if address is None:
            typer.echo(
                "Could not find a Tailscale address on this machine. Install "
                "Tailscale from https://tailscale.com/download, sign in, and "
                "check `tailscale ip -4` prints an address.",
                err=True,
            )
            raise typer.Exit(1)
        return address

    if host is None:
        return "127.0.0.1"

    wanted = host.strip()
    if wanted in WILDCARD_HOSTS or not wanted:
        typer.echo(
            "Refusing to listen on every address: this dashboard has no login, "
            "and it checks the address it was asked for, which a wildcard "
            "cannot tell it. Use --tailscale, or pass one exact address.",
            err=True,
        )
        raise typer.Exit(1)

    if _is_loopback(wanted):
        return wanted

    # Only an address Tailscale itself claims. The range alone is not enough:
    # a phone hotspot hands out 100.x addresses on the wifi interface from the
    # same range, and binding to one of those would serve every grade to
    # whoever else is on that wifi, with no login in the way.
    if tailnet.lists_address(wanted):
        return wanted

    typer.echo(
        f"Refusing to listen on {wanted!r}: this dashboard has no login, so it "
        "serves only to this machine or to your own Tailscale devices. Use "
        "--tailscale, or pass a loopback address or one that `tailscale ip` "
        "lists for this machine.",
        err=True,
    )
    raise typer.Exit(1)


@app.command()
def link(
    auto: bool = typer.Option(False, "--auto", help="Accept every suggestion without asking."),
    show: bool = typer.Option(False, "--list", help="Print the current links and exit."),
    set_: tuple[int, str] = typer.Option(
        (None, None),
        "--set",
        help="SOURCE_COURSE_ID COURSE, attach one platform course to an existing course.",
    ),
    unlink_id: int = typer.Option(
        None,
        "--unlink",
        metavar="SOURCE_COURSE_ID",
        help="Detach one platform course, as gt link --list numbers it.",
    ),
) -> None:
    """Link platform courses to the logical course you take."""
    from gradetracker.storage import db
    from gradetracker.storage import link as linking

    config.ensure_dirs()
    with db.connection() as conn:
        source_course_id, course = set_
        if source_course_id is not None:
            source_course = _source_course(conn, source_course_id)
            if not _link_to_existing(conn, linking, source_course, course):
                raise typer.Exit(1)
            return

        if unlink_id is not None:
            source_course = _source_course(conn, unlink_id)
            linking.unlink(conn, int(source_course["id"]))
            typer.echo(f"unlinked {source_course['source']} {source_course['name']!r}")
            return

        if show:
            _print_links(conn, linking)
            return

        if auto:
            actions = linking.link_auto(conn)
            if not actions:
                typer.echo("Nothing to link.")
                return
            for action in actions:
                if action["action"] == "skipped":
                    typer.echo(
                        f"skipped {action['source']} {action['name']!r}: {action['reason']}",
                        err=True,
                    )
                else:
                    verb = "created and linked" if action["action"] == "created" else "linked"
                    typer.echo(f"{verb} {action['source']} {action['name']!r} -> {action['code']}")
                    if action.get("preferred"):
                        from gradetracker.storage import duplicates

                        typer.echo(duplicates.default_notice(action["code"]))
            return

        _link_interactively(conn, linking)


def _source_course(conn, source_course_id: int):
    """One source_courses row, by the id gt link --list prints."""
    row = conn.execute(
        "SELECT * FROM source_courses WHERE id = ?", (source_course_id,)
    ).fetchone()
    if row is None:
        typer.echo(
            f"No platform course with id {source_course_id}. Try gt link --list.", err=True
        )
        raise typer.Exit(1)
    return row


def _course_codes(conn) -> list[str]:
    """Every logical course code, for the "try one of these" hints."""
    rows = conn.execute("SELECT code FROM courses ORDER BY code").fetchall()
    return [row["code"] for row in rows if row["code"]]


def _link_to_existing(conn, linking, source_course, course: str) -> bool:
    """Attach one platform course to the existing course the user named.

    The single lookup behind both --set and the interactive prompt, so the two
    cannot drift on what "econ 166" means. False, with the reason printed, when
    no course matches: the codes differ across platforms often enough
    (Gradescope's "ECONOMICS 166" is Blackboard's "ECON 166") that naming the
    target by hand has to be possible, but inventing one here would just split
    the course's data the way --auto does.
    """
    existing = linking.find_course_by_code(conn, course)
    if existing is None:
        codes = _course_codes(conn)
        typer.echo(
            f"No course matching {course!r}. "
            + (f"Try one of: {', '.join(codes)}" if codes else "Run gt link first."),
            err=True,
        )
        return False

    preferred = linking.link(conn, int(source_course["id"]), int(existing["id"]))
    typer.echo(
        f"linked {source_course['source']} {source_course['name']!r} -> {existing['code']}"
    )
    if preferred:
        from gradetracker.storage import duplicates

        typer.echo(duplicates.default_notice(existing["code"]))
    return True


def _print_links(conn, linking) -> None:
    """One line per platform course, showing what it feeds."""
    rows = linking.list_source_courses(conn)
    if not rows:
        typer.echo("No source courses yet. Run gt sync first.")
        return

    # The leading id is what gt link --set and --unlink take.
    typer.echo(f"{'id':<4} {'source':<11} course")
    for row in rows:
        target = (
            f"-> {row['course_code']}" if row["course_id"] else "-> (unlinked)"
        )
        label = row["code"] or row["name"] or row["platform_id"]
        # 14, because a platform code can be as long as "ECONOMICS 166".
        typer.echo(f"{row['id']:<4} {row['source']:<11} {label:<14} {target}")


def _link_interactively(conn, linking) -> None:
    """Confirm one suggestion per unlinked source course."""
    unlinked = linking.list_unlinked(conn)
    if not unlinked:
        typer.echo("Everything is linked.")
        return

    for source_course in unlinked:
        label = f"{source_course['source']} {source_course['name']!r}"
        code = source_course["code"]
        if not code:
            typer.echo(f"{label}: no course code could be parsed, skipping.", err=True)
            continue

        existing = linking.suggest_link(conn, source_course)
        if existing is not None:
            question = f"Link {label} to existing {existing['code']}?"
        else:
            question = f"Link {label} to a new course {code}?"

        if not typer.confirm(question, default=True):
            if not _offer_existing_course(conn, linking, source_course):
                typer.echo(f"skipped {label}")
            continue

        course_id = (
            int(existing["id"])
            if existing is not None
            else linking.create_course(
                conn, code, source_course["name"], source_course["term"]
            )
        )
        linking.link(conn, int(source_course["id"]), course_id)
        typer.echo(f"linked {label} -> {code}")


def _offer_existing_course(conn, linking, source_course) -> bool:
    """Second chance after a declined suggestion: name a course to link to.

    Declining used to be the end of it, which left no way to attach a platform
    course whose code differs from every other platform's. Stays quiet when
    there are no courses yet, because then there is nothing to name.
    """
    codes = _course_codes(conn)
    if not codes:
        return False

    if len(codes) <= 10:
        typer.echo(f"  existing courses: {', '.join(codes)}")
    answer = typer.prompt(
        "  Type the code of an existing course to link instead (Enter to skip)",
        default="",
        show_default=False,
    ).strip()
    if not answer:
        return False
    return _link_to_existing(conn, linking, source_course, answer)


if __name__ == "__main__":
    app()
