"""Sync orchestration: connectors -> raw dump -> normalize -> database (M2+).

Each source syncs independently, so an expired Blackboard session never blocks
Gradescope. Returns a process exit code rather than raising, so the CLI stays
thin and M8's scheduler can call the same function.

Every attempt records a sync_runs row, including the failures: a dashboard that
cannot say "this is stale because the session expired" is worse than useless.
"""

from __future__ import annotations

from datetime import datetime, timezone

import typer

from gradetracker import config, locking, notify
from gradetracker.redact import scrub
from gradetracker.auth.base import AuthExpired
from gradetracker.connectors.base import ConnectorError
from gradetracker.storage import changes as changes_store
from gradetracker.storage import connections, db, duplicates, normalize, raw

SOURCES = ("blackboard", "gradescope")

# What a scheduled run passes, so a manual sync minutes earlier is not repeated.
SCHEDULED_MIN_INTERVAL_S = 3600.0


def _seconds_since_last_ok(source: str) -> float | None:
    """How long since this source last synced successfully, or None if never."""
    try:
        with db.connection() as conn:
            row = conn.execute(
                """
                SELECT finished_at, started_at FROM sync_runs
                WHERE source = ? AND status = 'ok'
                ORDER BY started_at DESC, id DESC LIMIT 1
                """,
                (source,),
            ).fetchone()
    except Exception:
        return None

    if row is None:
        return None

    stamp = row["finished_at"] or row["started_at"]
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _skip_as_recent(source: str, min_interval_s: float | None) -> bool:
    """True when this source synced too recently to bother again.

    Keeps a scheduled run from piling on top of a manual one. Manual runs pass
    min_interval_s=None and are never skipped.
    """
    if not min_interval_s:
        return False
    age = _seconds_since_last_ok(source)
    if age is None or age >= min_interval_s:
        return False
    typer.echo(f"{source}: skipped: synced {int(age // 60)} min ago")
    return True


def _record_connection(source: str, outcome: str, message: str | None = None) -> bool:
    """Update the source's connection state. Returns True to notify an expiry."""
    try:
        with db.connection() as conn:
            if outcome == "ok":
                connections.record_success(conn, source)
                return False
            if outcome == "auth_expired":
                return connections.record_auth_expired(conn, source)
    except Exception as exc:
        typer.echo(f"{source}: could not record connection state ({exc})", err=True)
    return False


def _announce_expiry(source: str) -> None:
    """One desktop notification per expiry, not one per failed sync."""
    notify.notify(
        f"{source.title()} session expired",
        "Open the dashboard to reconnect.",
    )


def _record(source: str, started_at: str, status: str, **kwargs) -> None:
    """Record an attempt. A bookkeeping failure must not fail the sync itself.

    Every message is scrubbed: a Gradescope login error can carry the password
    in a request URL, and this row is rendered on the Connections page.
    """
    if "message" in kwargs:
        kwargs["message"] = scrub(kwargs["message"])
    try:
        with db.connection() as conn:
            db.record_sync_run(conn, source, started_at, status, **kwargs)
    except Exception as exc:  # a locked or unwritable database
        typer.echo(f"{source}: could not record the sync run ({exc})", err=True)


def _normalize_and_report(source: str, payload: dict, path, started_at: str) -> int:
    """Normalise a fetched payload, record what changed, and announce new grades."""
    try:
        with db.connection() as conn:
            # Taken before the upserts, and judged against the runs that came
            # before this one, so "first sync" means what it says.
            first_sync = changes_store.is_first_sync(conn, source)
            before = changes_store.snapshot(conn, source)

            result = normalize.NORMALIZERS[source](conn, payload)
            recorded = changes_store.diff_and_record(conn, source, before)

            db.record_sync_run(conn, source, started_at, "ok", raw_path=str(path))
    except Exception as exc:
        typer.echo(f"{source}: normalize failed: {exc}", err=True)
        _record(source, started_at, "error", raw_path=str(path), message=str(exc))
        return 1

    _record_connection(source, "ok")

    # On the very first sync everything is new and already graded; announcing it
    # would mean a wall of toasts on day one.
    if first_sync:
        if recorded:
            typer.echo(f"{source}: first sync, recorded {len(recorded)} item(s) as new")
    else:
        for title, body in changes_store.notifications_for(recorded):
            notify.notify(title, body)
        grades = sum(1 for change in recorded if change.is_new_grade)
        if grades:
            typer.echo(f"{source}: {grades} new grade(s)")

    details = [
        f"{result.source_courses} courses",
        f"{result.assignments} assignments",
    ]
    if result.dropped:
        details.append(f"{result.dropped} dropped")
    if result.categorised:
        details.append(f"{result.categorised} auto-categorised")
    typer.echo(f"{source}: normalized {', '.join(details)}")
    return 0


def _sync_blackboard(raw_only: bool, min_interval_s: float | None = None) -> int:
    """Auth, fetch, dump, normalise. Returns 0 on success, 1 on a handled failure."""
    if _skip_as_recent("blackboard", min_interval_s):
        return 0

    if not config.BB_BASE_URL:
        typer.echo(
            "blackboard: BB_BASE_URL is not set. Copy .env.example to .env.",
            err=True,
        )
        return 1

    # Imported late so sources that don't need Playwright don't pay for it.
    from gradetracker.auth.cookie_auth import CookieAuth
    from gradetracker.connectors.blackboard import BlackboardConnector

    auth = CookieAuth(
        base_url=config.BB_BASE_URL,
        check_path=config.BB_CHECK_PATH,
        profile_dir=config.BB_PROFILE_DIR,
        channel=config.BB_BROWSER_CHANNEL,
    )
    connector = BlackboardConnector(
        auth=auth,
        base_url=config.BB_BASE_URL,
        term_id=config.BB_TERM_ID,
    )

    started_at = db.now_iso()
    try:
        payload = connector.fetch()
    except AuthExpired as exc:
        typer.echo("blackboard: session expired, run gt login blackboard", err=True)
        if not raw_only:
            _record("blackboard", started_at, "auth_expired", message=str(exc))
            if _record_connection("blackboard", "auth_expired", str(exc)):
                _announce_expiry("blackboard")
        return 1
    except ConnectorError as exc:
        # Keep the last good data in place; the run is recorded as an error, and
        # the Connections page surfaces the message and the raw dump path.
        typer.echo(f"blackboard: fetch failed: {exc}", err=True)
        if not raw_only:
            _record("blackboard", started_at, "error", message=str(exc))
        return 1

    # Blackboard pushes out the cookie's expiry on every response, so saving the
    # jar after a successful sync is what keeps a sliding session alive.
    if not auth.persist():
        typer.echo(
            "blackboard: could not refresh the saved session; it may expire sooner",
            err=True,
        )

    path = raw.write_raw("blackboard", payload)
    courses = payload.get("courses", [])
    columns = sum(len(course.get("columns", [])) for course in courses)
    grades = sum(len(course.get("grades", [])) for course in courses)
    typer.echo(
        f"blackboard: {len(courses)} courses, {columns} columns, {grades} grades -> {path}"
    )

    _explain_empty_blackboard(payload)

    for entry in payload.get("skipped") or []:
        typer.echo(
            f"blackboard: skipped course {entry['courseId']} "
            f"(HTTP {entry['status']} on {entry['url']})",
            err=True,
        )

    if raw_only:
        return 0
    return _normalize_and_report("blackboard", payload, path, started_at)


def _explain_empty_blackboard(payload: dict) -> None:
    """Say why a sync that saw courses kept none of them.

    Without this, a filter that rejects everything just prints "0 courses" and
    the user has no way to know the availability value or the term involved.
    """
    if payload.get("courses"):
        return
    seen = payload.get("memberships_seen") or 0
    if not seen:
        return

    availability = ", ".join(payload.get("availability_seen") or []) or "none"
    terms = ", ".join(payload.get("terms_seen") or []) or "none"
    typer.echo(
        f"blackboard: saw {seen} enrolment(s) but kept none. "
        f"Availability values seen: {availability}. Term ids seen: {terms}.",
        err=True,
    )
    if payload.get("term_filter"):
        typer.echo(
            f"blackboard: BB_TERM_ID is set to {payload['term_filter']!r}; "
            "clear it or set it to one of the term ids above.",
            err=True,
        )
    else:
        typer.echo(
            "blackboard: if those courses are the current term, set BB_TERM_ID in "
            ".env to the right term id.",
            err=True,
        )


def _sync_gradescope(raw_only: bool, min_interval_s: float | None = None) -> int:
    """Log in with the stored password, fetch, dump, normalise."""
    if _skip_as_recent("gradescope", min_interval_s):
        return 0

    from gradetracker.auth.gradescope_auth import GradescopeAuth
    from gradetracker.connectors.gradescope import GradescopeConnector

    auth = GradescopeAuth()
    connector = GradescopeConnector(auth=auth, term=config.GS_TERM)

    started_at = db.now_iso()
    try:
        payload = connector.fetch()
    except AuthExpired as exc:
        typer.echo(
            "gradescope: credentials missing or rejected, run gt login gradescope",
            err=True,
        )
        if not raw_only:
            _record("gradescope", started_at, "auth_expired", message=str(exc))
            if _record_connection("gradescope", "auth_expired", str(exc)):
                _announce_expiry("gradescope")
        return 1
    except ConnectorError as exc:
        typer.echo(f"gradescope: fetch failed: {scrub(str(exc))}", err=True)
        if not raw_only:
            _record("gradescope", started_at, "error", message=str(exc))
        return 1

    path = raw.write_raw("gradescope", payload)
    courses = payload.get("courses", [])
    assignments = sum(len(course.get("assignments", [])) for course in courses)
    typer.echo(
        f"gradescope: {len(courses)} courses, {assignments} assignments -> {path}"
    )

    for entry in payload.get("skipped") or []:
        typer.echo(
            f"gradescope: skipped course {entry['id']} ({entry['error']})", err=True
        )

    if raw_only:
        return 0
    return _normalize_and_report("gradescope", payload, path, started_at)


def _handlers():
    """Resolved per call, so the names stay overridable rather than frozen at import."""
    return {
        "blackboard": _sync_blackboard,
        "gradescope": _sync_gradescope,
    }


def run_sync(
    sources: list[str], raw_only: bool = False, min_interval_s: float | None = None
) -> int:
    """Sync each source in turn. Returns the worst exit code seen.

    min_interval_s skips a source that synced successfully more recently than
    that. Scheduled runs pass an hour; a manual sync passes None, because
    someone who clicked the button means it.
    """
    config.ensure_dirs()

    # Across processes, not just threads: gt serve's scheduler and an OS-level
    # scheduled task can otherwise overlap, and each rewrites the cookie
    # snapshot at the end.
    with locking.sync_lock() as acquired:
        if not acquired:
            typer.echo("another sync is running; skipping this one")
            return 0

        handlers = _handlers()
        worst = 0
        for source in sources:
            handler = handlers.get(source)
            if handler is None:
                typer.echo(
                    f"Unknown source {source!r}. Choose blackboard or gradescope.", err=True
                )
                worst = max(worst, 1)
                continue
            worst = max(worst, handler(raw_only, min_interval_s))

        if not raw_only:
            # After every source, so a twin that arrived on one platform this
            # run is matched against the copy on the other.
            _apply_preferences()
            _report_unlinked()

        return worst


def _apply_preferences() -> None:
    """Settle mirrored copies for every course that has chosen a platform.

    A failure here must not fail the sync: the grades are already in, and the
    duplicates card still offers the same decision by hand.
    """
    done: list[tuple[str, str, int]] = []
    try:
        with db.connection() as conn:
            courses = duplicates.courses_with_preference(conn)
            for row in courses:
                excluded = duplicates.apply_preference(conn, int(row["id"]))
                if excluded:
                    typer.echo(
                        duplicates.summarise(
                            row["code"], row["preferred_source"], excluded
                        )
                    )
                    done.append((row["code"], row["preferred_source"], len(excluded)))
    except Exception as exc:
        typer.echo(f"could not apply platform preferences ({exc})", err=True)

    if done:
        notify.notify(*_exclusion_toast(done))


def _exclusion_toast(done: list[tuple[str, str, int]]) -> tuple[str, str]:
    """One toast for the whole sync, however many copies were set aside.

    Twelve toasts saying the same thing would be the day-one wall that
    first-sync suppression exists to prevent.
    """
    total = sum(count for _code, _preferred, count in done)
    if len(done) == 1:
        code, preferred, count = done[0]
        other = "Blackboard" if preferred == "gradescope" else "Gradescope"
        return (
            f"{code}: {count} {other} copies excluded",
            f"Mirrored work, already counted on {preferred.title()}.",
        )
    return (
        f"{total} mirrored copies excluded",
        ", ".join(code for code, _preferred, _count in done),
    )


def _report_unlinked() -> None:
    """Nudge once if anything new arrived that has no logical course yet.

    An unlinked source course means its assignments reach no grade, so this is
    worth saying out loud rather than leaving to the dashboard.
    """
    try:
        with db.connection() as conn:
            from gradetracker.storage.link import list_unlinked

            unlinked = list_unlinked(conn)
    except Exception:
        return

    if unlinked:
        typer.echo(
            f"{len(unlinked)} source course(s) are not linked to a course yet. "
            "Run gt link (or gt link --auto)."
        )
