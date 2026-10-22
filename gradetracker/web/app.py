"""Flask + HTMX dashboard (M7).

Screens: dashboard, course, what-if, what do I need, rules, map, connections,
links.

Calculation stays server-side. Every slider change posts the scenario to an
endpoint that runs the engine and returns an HTML fragment, so the grade maths
exists only once, in Python.

The sync runner and the browser launcher are injected, so tests never touch the
network and never open a window.
"""

from __future__ import annotations

import ipaddress
import threading

from urllib.parse import urlparse

from flask import (
    Flask,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from gradetracker import config
from gradetracker.engine.calculate import current_grade, projected_grade
from gradetracker.engine.models import letter_for
from gradetracker.engine.solve import required_for_letter, required_score
from gradetracker.redact import safe_error, scrub
from gradetracker.storage import changes as changes_store
from gradetracker.storage import connections as connections_store
from gradetracker.storage import duplicates as duplicates_store
from gradetracker.storage import db, link as linking, load, mapping
from gradetracker.storage import rules as rules_store
from gradetracker.syllabus.schema import (
    CategoryDraft,
    CutoffDraft,
    RulesDraft,
    default_cutoffs,
    has_blocking,
    validate,
)
from gradetracker.web import queries


class BackgroundJob:
    """One at a time, with a status a fragment can poll.

    A second Sync-now click while one is running must not start a second run:
    two concurrent syncs would race on the same rows.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self.running = False
        self.finished = False
        self.lines: list[str] = []
        self.error: str | None = None

    def start(self, target) -> bool:
        """Begin the job, or return False if it is already running."""
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.finished = False
            self.lines = []
            self.error = None

        def run() -> None:
            try:
                result = target()
                if result:
                    self.lines = [str(line) for line in result]
            except Exception as exc:  # a background crash must still show up
                self.error = f"{type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    self.running = False
                    self.finished = True

        threading.Thread(target=run, daemon=True, name=self.name).start()
        return True

    @property
    def state(self) -> str:
        if self.running:
            return "running"
        if self.error:
            return "error"
        if self.finished:
            return "done"
        return "idle"


def _default_sync_runner() -> list[str]:
    """Run a real sync. Replaced in tests.

    No min_interval: someone who clicked Sync now means it.
    """
    from gradetracker.sync import SOURCES, run_sync

    code = run_sync(list(SOURCES), raw_only=False)
    return [f"sync finished with code {code}"]


def _default_launcher() -> list[str]:
    """Open the real login window. Replaced in tests.

    Raises on failure: the launcher contract is "returning without raising
    means the login succeeded", which is what lets the caller record a login
    without having to re-check the session over the network.
    """
    from gradetracker.auth import launcher

    ok = launcher.login_blackboard(
        base_url=config.BB_BASE_URL,
        check_path=config.BB_CHECK_PATH,
        profile_dir=config.BB_PROFILE_DIR,
        channel=config.BB_BROWSER_CHANNEL,
    )
    if not ok:
        raise RuntimeError("Timed out after 5 minutes waiting for login.")
    launcher.write_login_timestamp(config.BB_PROFILE_DIR)
    return ["Blackboard login saved."]


def get_conn():
    """One connection per request, closed on teardown."""
    if "conn" not in g:
        g.conn = db.connect(current_app.config["GT_DB_PATH"])
    return g.conn


def _floats(form, prefix: str) -> dict[int, float]:
    """Pull score_<id> fields out of a form, skipping blanks."""
    out: dict[int, float] = {}
    for key, value in form.items():
        if not key.startswith(prefix):
            continue
        raw = (value or "").strip()
        if raw == "":
            continue
        try:
            out[int(key[len(prefix) :])] = float(raw)
        except ValueError:
            continue
    return out


def _category_groups(course, items, graded=()) -> list[dict]:
    """Unknown items grouped by category, in the course's own order.

    Anything whose category was never set, or was set to a category this course
    no longer has, goes in one "Unmapped" group at the end.

    Every category gets a group, including the ones with nothing to guess at.
    "posted" counts what the category holds in all, which is what tells an
    empty group that is finished apart from a final nobody has posted from one
    that is simply all marked.

    graded are the already-scored items, grouped the same way and handed back
    under "graded", so a regrade row sits with the category it belongs to.
    They are kept apart from "items" because the two answer different
    questions, and everything that counts what is still unknown reads
    "items".
    """
    categories = {category.id: category for category in course.categories}
    posted: dict[int | None, int] = {}
    for assignment in course.assignments:
        if not assignment.excluded:
            posted[assignment.category_id] = posted.get(assignment.category_id, 0) + 1

    groups = []
    for category_id, category in categories.items():
        chosen = [item for item in items if item["category_id"] == category_id]
        groups.append(
            {
                "category_id": category_id,
                "name": category.name,
                "weight": category.weight,
                "items": chosen,
                "graded": [
                    item for item in graded if item["category_id"] == category_id
                ],
                "posted": posted.get(category_id, 0),
            }
        )
    loose = [item for item in items if item["category_id"] not in categories]
    loose_graded = [item for item in graded if item["category_id"] not in categories]
    if loose or loose_graded:
        groups.append(
            {
                "category_id": None,
                "name": "Unmapped",
                "weight": None,
                "items": loose,
                "graded": loose_graded,
                "posted": len(loose) + len(loose_graded),
            }
        )
    return groups


def _plannable(groups) -> bool:
    """True when some real category has nothing posted in it yet.

    Those categories are the reason the what-if page renders at all for a
    course with every score already in: the final that no platform has heard
    of yet is exactly the thing worth planning for.
    """
    return any(
        group["category_id"] is not None and not group["items"] for group in groups
    )


def _is_weighted(course) -> bool:
    return course.scheme is not None and course.scheme.mode == "weighted"


def _split_unmapped(course, items) -> tuple[list[dict], list[dict]]:
    """Items that belong to one of this course's categories, and the rest."""
    known = {category.id for category in course.categories}
    mapped = [item for item in items if item["category_id"] in known]
    return mapped, [item for item in items if item["category_id"] not in known]


def _need_by_category(course, unknown_ids, target_percent, letter) -> dict:
    """One answer per category: what that category alone would have to do.

    Each row re-solves with only that category's unknown items unknown, so the
    rest of the term keeps the ordinary projection assumptions. That is what
    makes the rows readable one at a time, and why they do not add up to the
    whole-course answer.
    """
    weighted = course.scheme is not None and course.scheme.mode == "weighted"
    categories = {category.id: category for category in course.categories}
    chosen = [item for item in course.assignments if item.id in set(unknown_ids)]
    results = {
        result.category_id: result for result in current_grade(course).categories
    }

    groups: list[tuple[int | None, str, float | None, list]] = []
    loose = [item for item in chosen if item.category_id not in categories]

    if not categories:
        # A points course with no categories at all still deserves one row,
        # otherwise the table would be empty on the very courses that have the
        # fewest other clues.
        if chosen:
            groups.append((None, "Whole course", None, chosen))
        loose = []
    else:
        for category_id, category in categories.items():
            items = [item for item in chosen if item.category_id == category_id]
            if items:
                groups.append((category_id, category.name, category.weight, items))
        if loose and not weighted:
            groups.append((None, "Unmapped", None, loose))
            loose = []

    rows = []
    for category_id, name, weight, items in groups:
        ids = [item.id for item in items]
        if target_percent is not None:
            required = required_score(course, target_percent, ids)
        else:
            required = required_for_letter(course, letter, ids)
        rows.append(
            {
                "category_id": category_id,
                "name": name,
                "weight": weight,
                "result": results.get(category_id),
                "points_left": sum(item.possible or 0.0 for item in items),
                "points_needed": (
                    sum(required.per_item.values()) if required.per_item else None
                ),
                "required": required,
            }
        )

    note = None
    if loose:
        # Weighted mode only: an unmapped item carries no weight, so a row for
        # it would say "not counted" and teach the user nothing.
        note = (
            f"{len(loose)} unknown item(s) are not mapped to a category and are "
            "ignored here."
        )
    return {"rows": rows, "unmapped_note": note}


def _whatif_by_category(
    course, items, overrides, current, scenario, graded=()
) -> list[dict]:
    """Per-category current vs scenario, with what is still blank in each.

    "Filled in" counts the unknowns only. A regrade is an override too, but it
    answers nothing that was open: counting it as a filled unknown would make
    a category of finished work report progress it never made. It gets its
    own count instead.
    """
    current_results = {result.category_id: result for result in current.categories}
    scenario_results = {result.category_id: result for result in scenario.categories}

    rows = []
    for group in _category_groups(course, items, graded):
        unknown = group["items"]
        filled = [item for item in unknown if item["id"] in overrides]
        blank = [item for item in unknown if item["id"] not in overrides]
        rows.append(
            {
                "name": group["name"],
                "weight": group["weight"],
                "current": current_results.get(group["category_id"]),
                "scenario": scenario_results.get(group["category_id"]),
                "unknown_count": len(unknown),
                "filled_count": len(filled),
                "points_blank": sum(item["possible"] or 0.0 for item in blank),
                "regraded_count": sum(
                    1 for item in group["graded"] if item["id"] in overrides
                ),
            }
        )
    return rows


def _parse_rank_weights(raw: str | None) -> tuple[list[float] | None, list[str]]:
    """"23, 17" from the rules form, as (weights, tokens that would not read).

    A token that is not a number is handed back rather than turned into a
    zero: "40, abc" on a 40% category would otherwise add up correctly as
    [40, 0] and save silently, quietly deciding that the second-best item
    counts for nothing. validate() refuses the draft and names the token.
    """
    text = (raw or "").strip()
    if not text:
        return None, []

    weights: list[float] = []
    invalid: list[str] = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            weights.append(float(part))
        except ValueError:
            invalid.append(part)
    return (weights or None), invalid


def _draft_from_form(form) -> RulesDraft:
    """Build a RulesDraft from the rules form's repeated fields."""
    names = form.getlist("category_name")
    weights = form.getlist("category_weight")
    drops = form.getlist("category_drop")
    ranks = form.getlist("category_ranks")

    categories = []
    for index, name in enumerate(names):
        if not (name or "").strip():
            continue
        weight = None
        if index < len(weights):
            raw = (weights[index] or "").strip()
            if raw != "":
                try:
                    weight = float(raw)
                except ValueError:
                    weight = None
        drop = 0
        if index < len(drops):
            try:
                drop = int((drops[index] or "0").strip() or 0)
            except ValueError:
                drop = 0
        rank_weights, unreadable = _parse_rank_weights(
            ranks[index] if index < len(ranks) else ""
        )
        categories.append(
            CategoryDraft(
                name=name.strip(),
                weight=weight,
                drop_lowest=drop,
                rank_weights=rank_weights,
                rank_weights_invalid=unreadable,
            )
        )

    letters = form.getlist("cutoff_letter")
    minimums = form.getlist("cutoff_min")
    cutoffs = []
    for index, letter in enumerate(letters):
        if not (letter or "").strip():
            continue
        try:
            minimum = float((minimums[index] or "0").strip() or 0)
        except (ValueError, IndexError):
            minimum = 0.0
        cutoffs.append(CutoffDraft(letter=letter.strip(), min_percent=minimum))
    if not cutoffs:
        cutoffs = default_cutoffs()

    try:
        bonus = float((form.get("bonus_percent") or "0").strip() or 0)
    except ValueError:
        bonus = 0.0

    return RulesDraft(
        mode=form.get("mode", "points"),
        categories=categories,
        cutoffs=cutoffs,
        bonus_percent=bonus,
        notes=form.get("notes", "") or "",
    )


def _as_host(value: str) -> str:
    """An address in the form a Host header carries it.

    IPv6 literals are bracketed there, so an unbracketed one would never
    match and the extra host would silently do nothing.

    Literals only, and loudly: a name is exactly what the Host check exists to
    refuse, since whoever controls it can point it at this machine. The caller
    is supposed to pass addresses it bound a socket to, and anything else is a
    programming mistake worth failing on rather than quietly widening the set.
    """
    text = (value or "").strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        raise ValueError(
            f"extra_hosts takes literal IP addresses, not {value!r}: a name can "
            "be pointed at this machine by whoever controls it, which is what "
            "the Host check exists to stop."
        ) from None
    return f"[{address}]" if address.version == 6 else str(address)


def create_app(
    db_path=None, sync_runner=None, launcher=None, scheduler=False, extra_hosts=()
) -> Flask:
    """Build the app. Everything external is injected so tests stay offline.

    scheduler defaults to False so that importing or testing the app never
    starts background threads; gt serve passes True.

    extra_hosts are addresses this app is additionally reachable at, which
    `gt serve --tailscale` passes so a phone on the tailnet is not turned away
    by the loopback check below. They must be literal addresses the server was
    actually bound to, never names and never a wildcard.
    """
    app = Flask(__name__)
    app.config["GT_DB_PATH"] = db_path if db_path is not None else config.DB_PATH
    app.config["GT_SYNC_RUNNER"] = sync_runner or _default_sync_runner
    app.config["GT_LAUNCHER"] = launcher or _default_launcher
    # Local single-user app; no login, so this only salts flashes.
    app.secret_key = "gradetracker-local"

    sync_job = BackgroundJob("gt-sync")
    reconnect_job = BackgroundJob("gt-reconnect")
    app.extensions["gt_sync_job"] = sync_job
    app.extensions["gt_reconnect_job"] = reconnect_job

    # Safe methods; everything else changes state and is origin-checked.
    _SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
    _LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
    _ALLOWED_HOSTS = _LOCAL_HOSTS | {
        host for host in (_as_host(value) for value in extra_hosts or ()) if host
    }
    app.config["GT_ALLOWED_HOSTS"] = _ALLOWED_HOSTS

    def _host_only(value: str) -> str:
        """The hostname from a Host or Origin value, without the port."""
        if not value:
            return ""
        if value.startswith("["):  # IPv6 literal
            return value.split("]")[0] + "]"
        return value.split(":")[0]

    @app.before_request
    def _guard_origin():
        """Refuse requests that did not come from this machine's own page.

        The app listens on localhost with no authentication, so a page on the
        open web could otherwise form-POST to it: storing attacker-chosen
        Gradescope credentials, deleting assignments, or popping a real SSO
        window. Two checks:

        1. The Host header must be a loopback name, or a literal address this
           server was explicitly bound to, which blocks DNS rebinding (a
           hostile domain resolving to 127.0.0.1 would otherwise pass). A
           literal cannot be rebound, since no name is being resolved.
        2. For state-changing methods, a cross-site Sec-Fetch-Site, or an
           Origin/Referer from another host, is rejected.

        HTMX sends same-origin requests with matching headers, so nothing
        legitimate here breaks.
        """
        if _host_only(request.host) not in _ALLOWED_HOSTS:
            return ("This app only accepts requests addressed to localhost.", 403)

        if request.method in _SAFE_METHODS:
            return None

        if (request.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
            return ("Cross-site requests are not accepted.", 403)

        # Absent headers mean a same-origin XHR or a local tool; present ones
        # must match. Only the first is trusted when both appear.
        for header in ("Origin", "Referer"):
            value = request.headers.get(header)
            if not value:
                continue
            candidate = urlparse(value).netloc
            if _host_only(candidate) not in _ALLOWED_HOSTS:
                return (f"{header} is not local.", 403)
            if candidate and candidate != request.host:
                return (f"{header} does not match this host.", 403)
            break

        return None

    @app.context_processor
    def _client_location():
        """remote_client: this page is being read from another device.

        True on a phone over Tailscale, false in the laptop's own browser.
        The difference matters for anything that opens a window: the SSO
        browser and the Chromium profile live on the machine running gt
        serve, so a button tapped on the phone takes effect somewhere else.

        An extra host is by definition not loopback, so this is the loopback
        set rather than _ALLOWED_HOSTS.
        """
        return {"remote_client": _host_only(request.host) not in _LOCAL_HOSTS}

    @app.teardown_appcontext
    def _close(exception):  # noqa: ANN001
        conn = g.pop("conn", None)
        if conn is None:
            return
        try:
            if exception is None:
                conn.commit()
            else:
                conn.rollback()
        finally:
            conn.close()

    @app.template_filter("pct")
    def _pct(value):  # noqa: ANN001
        return "--" if value is None else f"{value:.2f}%"

    @app.template_filter("num")
    def _num(value):  # noqa: ANN001
        if value is None:
            return "--"
        return f"{value:g}"

    @app.template_filter("bar")
    def _bar(value):  # noqa: ANN001
        """A width percentage, clamped so a 105% category cannot overflow."""
        if value is None:
            return 0
        return max(0, min(100, round(float(value))))

    # --- dashboard --------------------------------------------------------

    @app.get("/")
    def dashboard():
        conn = get_conn()
        return render_template(
            "dashboard.html",
            cards=queries.dashboard(conn),
            sync_job=sync_job,
            reconnect_job=reconnect_job,
            connections=queries.connection_states(conn),
            changes=changes_store.recent_changes(conn, limit=20),
        )

    @app.post("/sync")
    def start_sync():
        started = sync_job.start(current_app.config["GT_SYNC_RUNNER"])
        return render_template(
            "partials/sync_status.html", job=sync_job, started=started, cards=None
        )

    @app.get("/sync/status")
    def sync_status():
        cards = None
        if sync_job.state in ("done", "error"):
            cards = queries.dashboard(get_conn())
        return render_template(
            "partials/sync_status.html", job=sync_job, started=True, cards=cards
        )

    # --- course -----------------------------------------------------------

    def _course_or_404(course_id: int):
        detail = queries.course_detail(get_conn(), course_id)
        if detail is None:
            return None
        return detail

    @app.get("/courses/<int:course_id>")
    def course(course_id: int):
        detail = _course_or_404(course_id)
        if detail is None:
            return render_template("not_found.html", what=f"course {course_id}"), 404
        return render_template("course.html", **detail)

    def _course_fragment(course_id: int):
        detail = queries.course_detail(get_conn(), course_id)
        return render_template("partials/course_detail.html", **detail)

    @app.post("/assignments/<int:assignment_id>/category")
    def set_assignment_category(assignment_id: int):
        conn = get_conn()
        course_id = queries.assignment_course_id(conn, assignment_id)
        if course_id is None:
            return render_template("not_found.html", what="assignment"), 404

        raw = (request.form.get("category_id") or "").strip()
        category_id = int(raw) if raw else None
        # A choice made by hand is locked: sync must never move it.
        mapping.set_category(conn, assignment_id, category_id, locked=True)
        return _course_fragment(course_id)

    @app.post("/assignments/<int:assignment_id>/<flag>")
    def toggle_assignment_flag(assignment_id: int, flag: str):
        if flag not in {"missing-as-zero", "excluded", "extra-credit"}:
            return render_template("not_found.html", what=f"action {flag}"), 404

        conn = get_conn()
        course_id = queries.assignment_course_id(conn, assignment_id)
        if course_id is None:
            return render_template("not_found.html", what="assignment"), 404

        column = flag.replace("-", "_")
        current = conn.execute(
            f"SELECT {column} AS value FROM assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        queries.set_flag(conn, assignment_id, column, not bool(current["value"]))
        return _course_fragment(course_id)

    @app.post("/courses/<int:course_id>/preference")
    def set_course_preference(course_id: int):
        """Choose the platform to keep, and settle what is already doubled.

        Applied straight away rather than at the next sync, so the effect of
        the choice is visible in the same swap that records it.
        """
        conn = get_conn()
        if queries.course_detail(conn, course_id) is None:
            return render_template("not_found.html", what=f"course {course_id}"), 404

        wanted = (request.form.get("preferred_source") or "").strip().lower()
        duplicates_store.set_preference(conn, course_id, wanted or None)
        duplicates_store.apply_preference(conn, course_id)
        return _course_fragment(course_id)

    @app.post("/courses/<int:course_id>/duplicates/resolve")
    def resolve_duplicates(course_id: int):
        """Exclude the twin of whichever platform the user wants to keep.

        Only rows in a pair the engine detected right now are touched, and a
        second click finds no pairs left, so repeating it is harmless.
        """
        conn = get_conn()
        detail = queries.course_detail(conn, course_id)
        if detail is None:
            return render_template("not_found.html", what=f"course {course_id}"), 404

        keep = (request.form.get("keep") or "").strip()
        wanted = (request.form.get("pair") or "").strip()

        for row in detail["duplicate_rows"]:
            if wanted and row["pair"] != wanted:
                continue
            left, right = row["left"], row["right"]
            # Both members from the same platform (two linked Blackboard
            # courses, say) would otherwise lose both or neither.
            if left["source"] == keep and right["source"] != keep:
                queries.set_flag(conn, int(right["id"]), "excluded", True)
            elif right["source"] == keep and left["source"] != keep:
                queries.set_flag(conn, int(left["id"]), "excluded", True)

        return _course_fragment(course_id)

    @app.post("/courses/<int:course_id>/assignments")
    def add_assignment(course_id: int):
        conn = get_conn()
        name = (request.form.get("name") or "").strip()
        if not name:
            return _course_fragment(course_id)

        def number(field):
            raw = (request.form.get(field) or "").strip()
            if raw == "":
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        category_raw = (request.form.get("category_id") or "").strip()
        queries.add_manual_assignment(
            conn,
            course_id,
            name=name,
            earned=number("earned"),
            possible=number("possible"),
            status=request.form.get("status") or "graded",
            category_id=int(category_raw) if category_raw else None,
            extra_credit=bool(request.form.get("extra_credit")),
        )
        return _course_fragment(course_id)

    @app.post("/assignments/<int:assignment_id>/delete")
    def delete_assignment(assignment_id: int):
        conn = get_conn()
        course_id = queries.assignment_course_id(conn, assignment_id)
        if course_id is None:
            return render_template("not_found.html", what="assignment"), 404
        queries.delete_manual_assignment(conn, assignment_id)
        # The same button appears on the what-if page, where swapping in a
        # course fragment would replace the sliders with the course screen.
        # In the query string, because that button sits inside the slider
        # form and posts no fields of its own.
        if (request.args.get("return_to") or "") == "whatif":
            return redirect(url_for("whatif", course_id=course_id))
        return _course_fragment(course_id)

    # --- what-if ----------------------------------------------------------

    @app.get("/courses/<int:course_id>/whatif")
    def whatif(course_id: int):
        conn = get_conn()
        course = load.load_course(conn, course_id)
        if course is None:
            return render_template("not_found.html", what=f"course {course_id}"), 404

        items = queries.unscored_assignments(conn, course_id)
        graded = queries.graded_assignments(conn, course_id)
        current = current_grade(course)
        groups = _category_groups(course, items, graded)
        return render_template(
            "whatif.html",
            course=course,
            current=current,
            items=items,
            graded=graded,
            groups=groups,
            plannable=_plannable(groups),
            weighted=_is_weighted(course),
            category_rows=_whatif_by_category(
                course, items, {}, current, current, graded
            ),
            scenarios=queries.list_scenarios(conn, course_id),
            preset={},
            preset_name="",
        )

    @app.post("/courses/<int:course_id>/whatif/placeholder/<int:category_id>")
    def whatif_placeholder(course_id: int, category_id: int):
        """Add an item to a category that has nothing posted in it yet.

        An ordinary by-hand row, created from the what-if page so that
        planning for a final the platforms have never mentioned does not mean
        leaving for the course screen and coming back.

        The category is in the URL and its two fields are named after it
        because this posts from inside the slider form, which carries every
        category's boxes at once; see the template.
        """
        conn = get_conn()
        course = load.load_course(conn, course_id)
        if course is None:
            return render_template("not_found.html", what=f"course {course_id}"), 404

        back = redirect(url_for("whatif", course_id=course_id))
        categories = {category.id: category for category in course.categories}
        category = categories.get(category_id)
        if category is None:
            flash("That category does not belong to this course, so nothing was added.")
            return back

        # The box arrives filled with the category's name, so a blank one means
        # the user cleared it rather than that there is nothing to call this.
        name = (request.form.get(f"ph_name_{category_id}") or "").strip() or category.name

        raw_possible = (request.form.get(f"ph_possible_{category_id}") or "").strip()
        try:
            possible = float(raw_possible)
        except ValueError:
            possible = None
        if possible is None or possible <= 0:
            # A row worth nothing out of nothing is accepted by the database
            # and then invisible everywhere -- what-if skips it, the
            # projection skips it -- so it is refused here, out loud.
            flash(
                f"\"{name}\" needs a point value above zero to be worth "
                "planning around, so it was not added."
            )
            return back

        queries.add_manual_assignment(
            conn,
            course_id,
            name=name,
            earned=None,
            possible=possible,
            status="ungraded",
            category_id=category.id,
        )
        return back

    @app.post("/courses/<int:course_id>/whatif/calc")
    def whatif_calc(course_id: int):
        conn = get_conn()
        course = load.load_course(conn, course_id)
        if course is None:
            return render_template("not_found.html", what=f"course {course_id}"), 404

        overrides = _floats(request.form, "score_")
        current = current_grade(course)
        scenario = current_grade(course, overrides=overrides)
        delta = (
            None
            if scenario.percent is None or current.percent is None
            else scenario.percent - current.percent
        )
        items = queries.unscored_assignments(conn, course_id)
        graded = queries.graded_assignments(conn, course_id)
        return render_template(
            "partials/whatif_result.html",
            current=current,
            scenario=scenario,
            delta=delta,
            count=len(overrides),
            category_rows=_whatif_by_category(
                course, items, overrides, current, scenario, graded
            ),
        )

    @app.get("/courses/<int:course_id>/whatif/scenario/<int:scenario_id>")
    def load_scenario(course_id: int, scenario_id: int):
        conn = get_conn()
        course = load.load_course(conn, course_id)
        if course is None:
            return render_template("not_found.html", what=f"course {course_id}"), 404

        scenarios = queries.list_scenarios(conn, course_id)
        chosen = next((s for s in scenarios if s["id"] == scenario_id), None)
        items = queries.unscored_assignments(conn, course_id)
        graded = queries.graded_assignments(conn, course_id)
        current = current_grade(course)
        groups = _category_groups(course, items, graded)
        return render_template(
            "whatif.html",
            course=course,
            current=current,
            items=items,
            graded=graded,
            groups=groups,
            plannable=_plannable(groups),
            weighted=_is_weighted(course),
            category_rows=_whatif_by_category(
                course, items, {}, current, current, graded
            ),
            scenarios=scenarios,
            preset=chosen["overrides"] if chosen else {},
            preset_name=chosen["name"] if chosen else "",
        )

    @app.post("/courses/<int:course_id>/scenarios")
    def create_scenario(course_id: int):
        conn = get_conn()
        name = (request.form.get("name") or "").strip() or "unnamed"
        queries.save_scenario(conn, course_id, name, _floats(request.form, "score_"))
        return redirect(url_for("whatif", course_id=course_id))

    @app.post("/courses/<int:course_id>/scenarios/<int:scenario_id>/delete")
    def remove_scenario(course_id: int, scenario_id: int):
        queries.delete_scenario(get_conn(), scenario_id)
        return redirect(url_for("whatif", course_id=course_id))

    # --- what do I need ---------------------------------------------------

    @app.get("/courses/<int:course_id>/need")
    def need(course_id: int):
        conn = get_conn()
        course = load.load_course(conn, course_id)
        if course is None:
            return render_template("not_found.html", what=f"course {course_id}"), 404

        cutoffs = course.scheme.cutoffs if course.scheme else default_cutoffs_tuple()
        items = queries.unscored_assignments(conn, course_id)
        mapped, unmapped = _split_unmapped(course, items)
        return render_template(
            "need.html",
            course=course,
            current=current_grade(course),
            items=items,
            mapped_items=mapped,
            unmapped_items=unmapped,
            weighted=_is_weighted(course),
            cutoffs=cutoffs,
        )

    def default_cutoffs_tuple():
        return tuple((c.letter, c.min_percent) for c in default_cutoffs())

    @app.post("/courses/<int:course_id>/need/calc")
    def need_calc(course_id: int):
        conn = get_conn()
        course = load.load_course(conn, course_id)
        if course is None:
            return render_template("not_found.html", what=f"course {course_id}"), 404

        unknown_ids = [int(value) for value in request.form.getlist("unknown")]
        if not unknown_ids:
            unknown_ids = [item["id"] for item in queries.unscored_assignments(conn, course_id)]

        letter = (request.form.get("letter") or "").strip()
        percent_raw = (request.form.get("percent") or "").strip()

        target = None
        if percent_raw:
            try:
                target = float(percent_raw)
            except ValueError:
                target = 0.0
            result = required_score(course, target, unknown_ids)
        else:
            letter = letter or "A-"
            result = required_for_letter(course, letter, unknown_ids)

        names = {
            item["id"]: item["name"]
            for item in queries.unscored_assignments(conn, course_id)
        }
        return render_template(
            "partials/need_result.html",
            result=result,
            names=names,
            course=course,
            by_category=_need_by_category(course, unknown_ids, target, letter),
        )

    # --- rules ------------------------------------------------------------

    @app.get("/courses/<int:course_id>/rules")
    def rules_form(course_id: int):
        conn = get_conn()
        course = load.load_course(conn, course_id)
        if course is None:
            return render_template("not_found.html", what=f"course {course_id}"), 404

        draft = rules_store.load_rules(conn, course_id) or RulesDraft(mode="weighted")
        return render_template(
            "rules.html",
            course=course,
            draft=draft,
            warnings=[],
            status=rules_store.rules_status(conn, course_id),
            summary=rules_store.rules_summary(conn, course_id),
            confirm_needed=False,
        )

    @app.post("/courses/<int:course_id>/rules/validate")
    def rules_validate(course_id: int):
        draft = _draft_from_form(request.form)
        return render_template(
            "partials/rules_warnings.html",
            warnings=validate(draft),
            total=draft.weight_total(),
            mode=draft.mode,
        )

    @app.post("/courses/<int:course_id>/rules")
    def rules_save(course_id: int):
        conn = get_conn()
        course = load.load_course(conn, course_id)
        if course is None:
            return render_template("not_found.html", what=f"course {course_id}"), 404

        draft = _draft_from_form(request.form)
        warnings = validate(draft)
        confirmed = bool(request.form.get("confirm_anyway"))

        # Blocking problems never save. Advisory ones need one more click, so a
        # weight that does not add up is a decision rather than an accident.
        if has_blocking(warnings) or (warnings and not confirmed):
            return render_template(
                "rules.html",
                course=course,
                draft=draft,
                warnings=warnings,
                status=rules_store.rules_status(conn, course_id),
                summary=rules_store.rules_summary(conn, course_id),
                confirm_needed=not has_blocking(warnings),
            )

        rules_store.save_rules(conn, course_id, draft, origin="manual")
        return redirect(url_for("map_page", course_id=course_id))

    # --- mapping ----------------------------------------------------------

    @app.get("/courses/<int:course_id>/map")
    def map_page(course_id: int):
        conn = get_conn()
        course = load.load_course(conn, course_id)
        if course is None:
            return render_template("not_found.html", what=f"course {course_id}"), 404
        return render_template(
            "map.html",
            course=course,
            unmapped=mapping.list_unmapped(conn, course_id),
            categories=mapping.list_categories(conn, course_id),
        )

    @app.post("/courses/<int:course_id>/map/accept-all")
    def map_accept_all(course_id: int):
        mapping.accept_all_suggestions(get_conn(), course_id)
        return redirect(url_for("map_page", course_id=course_id))

    @app.post("/courses/<int:course_id>/map/save")
    def map_save(course_id: int):
        """Set every row the user chose on the Map page, in one submit.

        One select per request meant one page load per assignment, and the
        reply was the course page, so 29 codelets cost 29 round trips and 29
        trips back to the Map page. Rows left empty are left alone, and the
        redirect returns to the Map page with whatever is still unmapped.
        """
        conn = get_conn()
        # Only this course's categories, so a crafted form cannot file an
        # assignment under another course's category.
        allowed = {int(row["id"]) for row in mapping.list_categories(conn, course_id)}

        for field, raw in request.form.items():
            if not field.startswith("category_"):
                continue
            value = (raw or "").strip()
            if not value:
                continue
            try:
                assignment_id = int(field[len("category_") :])
                category_id = int(value)
            except ValueError:
                continue
            if category_id not in allowed:
                continue
            if queries.assignment_course_id(conn, assignment_id) != course_id:
                continue
            # Chosen by hand, so locked, exactly as the single-row route does.
            mapping.set_category(conn, assignment_id, category_id, locked=True)

        return redirect(url_for("map_page", course_id=course_id))

    # --- connections ------------------------------------------------------

    @app.get("/connections")
    def connections():
        conn = get_conn()
        return render_template(
            "connections.html",
            connections=queries.connection_states(conn),
            job=reconnect_job,
            spans={
                source: connections_store.session_spans(conn, source)
                for source in queries.SOURCES
            },
            averages={
                source: connections_store.average_session_duration(conn, source)
                for source in queries.SOURCES
            },
        )

    def _reconnect_target():
        """Run the launcher, then record the login if it did not raise."""
        launcher_fn = app.config["GT_LAUNCHER"]
        lines = launcher_fn()
        try:
            with db.connection(app.config["GT_DB_PATH"]) as conn:
                connections_store.record_login(conn, "blackboard")
        except Exception:
            # The login worked; failing to note it must not look like failure.
            pass
        # A reconnect is only ever done to get the grades moving again, so
        # start the sync here instead of making the user click twice. False
        # means one is already running, which serves the same purpose.
        # app.config, not current_app: this runs in a plain worker thread.
        if sync_job.start(app.config["GT_SYNC_RUNNER"]):
            lines = [*(lines or []), "A sync has started."]
        return lines

    @app.post("/connections/blackboard/reconnect")
    def reconnect_blackboard():
        started = reconnect_job.start(_reconnect_target)
        return render_template(
            "partials/reconnect_status.html", job=reconnect_job, started=started
        )

    @app.get("/connections/blackboard/status")
    def reconnect_status():
        return render_template(
            "partials/reconnect_status.html", job=reconnect_job, started=True
        )

    @app.post("/connections/gradescope")
    def save_gradescope():
        from gradetracker.auth import gradescope_auth
        from gradetracker.auth.base import AuthExpired

        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        message = ""
        ok = False

        if not email or not password:
            message = "Both an email and a password are needed."
        else:
            try:
                gradescope_auth.verify_credentials(email, password)
                gradescope_auth.store_credentials(email, password)
                ok = True
                message = "Gradescope login saved."
            except AuthExpired as exc:
                message = scrub(f"Gradescope rejected that login: {exc}")
            except Exception as exc:
                # Class name only: the exception text can carry the password.
                message = f"Could not reach Gradescope ({safe_error(exc)})"

        # The password is never echoed back into the page.
        return render_template(
            "partials/gradescope_result.html", ok=ok, message=message, email=email
        )

    # --- links ------------------------------------------------------------

    @app.get("/link")
    def link_page():
        conn = get_conn()
        return render_template(
            "link.html",
            rows=queries.link_rows(conn),
            courses=queries.courses_for_select(conn),
        )

    @app.post("/link/<int:source_course_id>")
    def do_link(source_course_id: int):
        conn = get_conn()
        action = request.form.get("action") or "link"

        if action == "unlink":
            linking.unlink(conn, source_course_id)
        elif action == "create":
            row = conn.execute(
                "SELECT * FROM source_courses WHERE id = ?", (source_course_id,)
            ).fetchone()
            if row is not None and row["code"]:
                course_id = linking.create_course(conn, row["code"], row["name"], row["term"])
                _note_preference(conn, course_id, linking.link(conn, source_course_id, course_id))
        else:
            raw = (request.form.get("course_id") or "").strip()
            if raw:
                course_id = int(raw)
                _note_preference(conn, course_id, linking.link(conn, source_course_id, course_id))

        return redirect(url_for("link_page"))

    def _note_preference(conn, course_id: int, preferred: bool) -> None:
        """Say what just became true, on the page the user lands back on."""
        if not preferred:
            return
        row = conn.execute(
            "SELECT code FROM courses WHERE id = ?", (course_id,)
        ).fetchone()
        if row is not None:
            flash(duplicates_store.default_notice(row["code"]))

    # --- misc -------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "schema": db.schema_version(get_conn())}

    app.jinja_env.globals.update(letter_for=letter_for, projected_grade=projected_grade)

    if scheduler:
        # The scheduled job goes through the SAME lock as the Sync now button,
        # so a manual click and a scheduled run cannot overlap.
        from gradetracker.scheduler import SyncScheduler
        from gradetracker.sync import SCHEDULED_MIN_INTERVAL_S, SOURCES, run_sync

        def scheduled_sync():
            def runner():
                code = run_sync(
                    list(SOURCES),
                    raw_only=False,
                    min_interval_s=SCHEDULED_MIN_INTERVAL_S,
                )
                return [f"scheduled sync finished with code {code}"]

            sync_job.start(runner)

        background = SyncScheduler(scheduled_sync)
        background.start()
        app.extensions["gt_scheduler"] = background

    return app
