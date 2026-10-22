"""Scheduler, connection state, notifications and change detection (M8).

Nothing here installs a scheduled task, raises a real toast, or touches the
network: schtasks argv is built by a pure function, and the notification
backend is a recorder.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gradetracker import config, notify, scheduler, sync
from gradetracker.auth.base import AuthExpired
from gradetracker.connectors.base import ConnectorError
from gradetracker.storage import changes as changes_store
from gradetracker.storage import connections as connections_store
from gradetracker.storage import db, link as linking
from gradetracker.storage.normalize import normalize_blackboard

from tests.test_normalize import bb_payload


class Recorder:
    """A notification backend that remembers what it was asked to show."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def __call__(self, title: str, body: str) -> None:
        self.sent.append((title, body))


@pytest.fixture
def recorder(monkeypatch):
    backend = Recorder()
    monkeypatch.setattr(notify, "_backend", backend)
    monkeypatch.setattr(config, "GT_NOTIFY", "1")
    return backend


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(config, "BB_PROFILE_DIR", tmp_path / "profiles" / "blackboard")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "grades.db")
    monkeypatch.setattr(config, "BB_BASE_URL", "https://bb.example.edu")
    return tmp_path


@pytest.fixture
def conn(configured):
    connection = db.connect(configured / "grades.db")
    yield connection
    connection.close()


# --- notify ---------------------------------------------------------------


def test_notify_uses_the_injected_backend(recorder):
    assert notify.notify("Title", "Body") is True

    assert recorder.sent == [("Title", "Body")]


def test_gt_notify_zero_disables_notifications(recorder, monkeypatch):
    monkeypatch.setattr(config, "GT_NOTIFY", "0")

    assert notify.notify("Title", "Body") is False
    assert recorder.sent == []


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_disabling_accepts_several_spellings(monkeypatch, value):
    monkeypatch.setattr(config, "GT_NOTIFY", value)

    assert notify.enabled() is False


def test_a_failing_backend_never_raises(monkeypatch):
    def explode(title, body):
        raise RuntimeError("no desktop session")

    monkeypatch.setattr(notify, "_backend", explode)
    monkeypatch.setattr(config, "GT_NOTIFY", "1")

    # A toast is not worth failing a sync over.
    assert notify.notify("Title", "Body") is False


def test_the_toast_script_takes_its_text_from_the_environment():
    """Nothing is interpolated into the PowerShell, so names cannot inject."""
    assert "$env:GT_TOAST_TITLE" in notify._TOAST_SCRIPT
    assert "$env:GT_TOAST_BODY" in notify._TOAST_SCRIPT


def test_the_windows_backend_passes_an_argv_list(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        captured["timeout"] = kwargs.get("timeout")

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(notify.subprocess, "run", fake_run)

    notify.windows_backend("Quiz 1'; rm -rf", "18/20")

    assert isinstance(captured["args"], list)
    assert captured["args"][0] == "powershell.exe"
    assert captured["timeout"] == 10
    # The dangerous-looking name travels as data, not as script text.
    assert captured["env"]["GT_TOAST_TITLE"] == "Quiz 1'; rm -rf"
    assert all("rm -rf" not in str(arg) for arg in captured["args"])


# --- connection state -----------------------------------------------------


def test_record_success_marks_connected(conn):
    connections_store.record_success(conn, "blackboard")

    row = connections_store.get(conn, "blackboard")
    assert row["state"] == connections_store.CONNECTED
    assert row["last_success_at"] is not None
    assert row["notified_at"] is None


def test_record_auth_expired_is_true_only_the_first_time(conn):
    first = connections_store.record_auth_expired(conn, "blackboard")
    second = connections_store.record_auth_expired(conn, "blackboard")
    third = connections_store.record_auth_expired(conn, "blackboard")

    assert first is True
    assert (second, third) == (False, False)
    assert connections_store.get(conn, "blackboard")["state"] == connections_store.NEEDS_RECONNECT


def test_a_success_rearms_the_expiry_notification(conn):
    assert connections_store.record_auth_expired(conn, "blackboard") is True
    assert connections_store.record_auth_expired(conn, "blackboard") is False

    connections_store.record_success(conn, "blackboard")

    # A later expiry is news again.
    assert connections_store.record_auth_expired(conn, "blackboard") is True


def test_a_login_rearms_the_expiry_notification(conn):
    connections_store.record_auth_expired(conn, "blackboard")
    connections_store.record_login(conn, "blackboard")

    assert connections_store.record_auth_expired(conn, "blackboard") is True


def test_sources_have_independent_state(conn):
    connections_store.record_auth_expired(conn, "blackboard")

    assert connections_store.record_auth_expired(conn, "gradescope") is True
    assert connections_store.get(conn, "gradescope")["state"] == connections_store.NEEDS_RECONNECT
    assert connections_store.get(conn, "blackboard")["state"] == connections_store.NEEDS_RECONNECT


# --- session spans --------------------------------------------------------


def test_a_login_opens_a_span_and_an_expiry_closes_it(conn):
    connections_store.record_login(conn, "blackboard")
    spans = connections_store.session_spans(conn, "blackboard")
    assert len(spans) == 1
    assert spans[0]["open"] is True
    assert spans[0]["duration"] is None

    connections_store.record_auth_expired(conn, "blackboard")

    spans = connections_store.session_spans(conn, "blackboard")
    assert len(spans) == 1
    assert spans[0]["open"] is False
    assert spans[0]["duration"] is not None


def test_only_one_span_is_ever_open(conn):
    connections_store.record_login(conn, "blackboard")
    connections_store.record_login(conn, "blackboard")

    spans = connections_store.session_spans(conn, "blackboard")

    assert len(spans) == 2
    assert sum(1 for span in spans if span["open"]) == 1


def test_average_session_duration(conn):
    now = datetime.now(timezone.utc)
    rows = [
        (now - timedelta(hours=30), now - timedelta(hours=20)),  # 10 hours
        (now - timedelta(hours=15), now - timedelta(hours=11)),  # 4 hours
        (now - timedelta(hours=2), None),  # still open, excluded
    ]
    for started, ended in rows:
        conn.execute(
            "INSERT INTO session_spans (source, logged_in_at, expired_at) VALUES (?, ?, ?)",
            ("blackboard", started.isoformat(), ended.isoformat() if ended else None),
        )
    conn.commit()

    average = connections_store.average_session_duration(conn, "blackboard")

    # (10 + 4) / 2 = 7 hours; the open span must not drag it down.
    assert average == timedelta(hours=7)


def test_average_is_none_without_a_completed_session(conn):
    connections_store.record_login(conn, "blackboard")

    assert connections_store.average_session_duration(conn, "blackboard") is None


def test_spans_are_capped_by_the_limit(conn):
    for _ in range(12):
        connections_store.record_login(conn, "blackboard")

    assert len(connections_store.session_spans(conn, "blackboard", limit=10)) == 10


# --- the skip rule --------------------------------------------------------


def _fake_fetch(monkeypatch, payload=None, raises=None):
    from gradetracker.auth.cookie_auth import CookieAuth
    from gradetracker.connectors.blackboard import BlackboardConnector

    monkeypatch.setattr(CookieAuth, "persist", lambda self: True)

    def fetch(self):
        if raises is not None:
            raise raises
        return payload if payload is not None else bb_payload()

    monkeypatch.setattr(BlackboardConnector, "fetch", fetch)


def test_a_recent_sync_is_skipped_for_a_scheduled_run(configured, monkeypatch, capsys):
    _fake_fetch(monkeypatch)
    assert sync.run_sync(["blackboard"], raw_only=False) == 0
    capsys.readouterr()

    code = sync.run_sync(["blackboard"], raw_only=False, min_interval_s=3600)

    assert code == 0
    assert "skipped: synced 0 min ago" in capsys.readouterr().out


def test_a_manual_run_is_never_skipped(configured, monkeypatch, capsys):
    _fake_fetch(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)
    capsys.readouterr()

    sync.run_sync(["blackboard"], raw_only=False, min_interval_s=None)

    out = capsys.readouterr().out
    assert "skipped" not in out
    assert "normalized" in out


def test_an_old_sync_is_not_skipped(configured, monkeypatch, capsys):
    _fake_fetch(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)

    # Move the successful run back two hours.
    conn = db.connect(configured / "grades.db")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn.execute("UPDATE sync_runs SET started_at = ?, finished_at = ?", (old, old))
    conn.commit()
    conn.close()
    capsys.readouterr()

    sync.run_sync(["blackboard"], raw_only=False, min_interval_s=3600)

    assert "skipped" not in capsys.readouterr().out


def test_a_failed_run_does_not_count_as_recent(configured, monkeypatch, capsys):
    """Only a successful sync should hold off the next attempt."""
    _fake_fetch(monkeypatch, raises=ConnectorError("columns moved"))
    sync.run_sync(["blackboard"], raw_only=False)
    capsys.readouterr()

    _fake_fetch(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False, min_interval_s=3600)

    assert "skipped" not in capsys.readouterr().out


# --- exactly-once expiry notification -------------------------------------


def test_three_expired_syncs_notify_exactly_once(configured, monkeypatch, recorder, capsys):
    """A sync failing every four hours must prompt once, not six times a day."""
    _fake_fetch(monkeypatch, raises=AuthExpired("no cookies"))

    for _ in range(3):
        assert sync.run_sync(["blackboard"], raw_only=False) == 1

    assert len(recorder.sent) == 1
    title, body = recorder.sent[0]
    assert "session expired" in title.lower()
    assert "reconnect" in body.lower()

    conn = db.connect(configured / "grades.db")
    try:
        assert connections_store.get(conn, "blackboard")["state"] == connections_store.NEEDS_RECONNECT
    finally:
        conn.close()


def test_a_recovery_then_a_new_expiry_notifies_again(configured, monkeypatch, recorder):
    _fake_fetch(monkeypatch, raises=AuthExpired("no cookies"))
    sync.run_sync(["blackboard"], raw_only=False)
    assert len(recorder.sent) == 1

    _fake_fetch(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)

    _fake_fetch(monkeypatch, raises=AuthExpired("expired again"))
    sync.run_sync(["blackboard"], raw_only=False)

    expiries = [entry for entry in recorder.sent if "expired" in entry[0].lower()]
    assert len(expiries) == 2


# --- change detection -----------------------------------------------------


def test_the_first_sync_records_new_rows_and_notifies_nothing(configured, monkeypatch, recorder, capsys):
    """Thirty toasts on day one would be worse than none."""
    _fake_fetch(monkeypatch)

    sync.run_sync(["blackboard"], raw_only=False)

    assert recorder.sent == []
    out = capsys.readouterr().out
    assert "first sync, recorded" in out

    conn = db.connect(configured / "grades.db")
    try:
        kinds = [row["kind"] for row in conn.execute("SELECT kind FROM changes")]
        assert kinds
        assert set(kinds) == {"new"}
    finally:
        conn.close()


def test_a_second_sync_with_a_new_grade_notifies_once(configured, monkeypatch, recorder):
    """The PDF's example: "New grade in ECON 166: Problem Set 4, 18/20"."""
    _fake_fetch(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)
    # Linked, so the notification can use the course code rather than the
    # platform course's long name.
    conn = db.connect(configured / "grades.db")
    linking.link_auto(conn)
    conn.close()
    recorder.sent.clear()

    # The midterm comes back graded.
    payload = bb_payload()
    for grade in payload["courses"][0]["grades"]:
        if grade["columnId"] == "_14004_1":
            grade["score"] = 91.0
    _fake_fetch(monkeypatch, payload=payload)

    sync.run_sync(["blackboard"], raw_only=False)

    assert len(recorder.sent) == 1
    title, body = recorder.sent[0]
    assert title == "New grade in ECON 166"
    assert body == "Midterm Exam, 91/100"


def test_an_unlinked_course_falls_back_to_the_platform_name(configured, monkeypatch, recorder):
    """Without a link there is no course code, so name the platform course."""
    _fake_fetch(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)
    recorder.sent.clear()

    payload = bb_payload()
    for grade in payload["courses"][0]["grades"]:
        if grade["columnId"] == "_14004_1":
            grade["score"] = 91.0
    _fake_fetch(monkeypatch, payload=payload)
    sync.run_sync(["blackboard"], raw_only=False)

    assert len(recorder.sent) == 1
    assert recorder.sent[0][0] == "New grade in ECON 166: Intermediate Microeconomics"


def test_a_score_change_is_recorded_with_old_and_new(configured, monkeypatch, recorder):
    _fake_fetch(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)

    payload = bb_payload()
    for grade in payload["courses"][0]["grades"]:
        if grade["columnId"] == "_14001_1":
            grade["score"] = 20.0
    _fake_fetch(monkeypatch, payload=payload)
    sync.run_sync(["blackboard"], raw_only=False)

    conn = db.connect(configured / "grades.db")
    try:
        row = conn.execute(
            "SELECT * FROM changes WHERE kind = 'score' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["old_value"] == "18/20"
        assert row["new_value"] == "20/20"
    finally:
        conn.close()


def test_a_status_change_is_recorded(configured, monkeypatch, recorder):
    _fake_fetch(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)

    payload = bb_payload()
    for grade in payload["courses"][0]["grades"]:
        if grade["columnId"] == "_14003_1":  # Quiz 1 was excused
            grade["exempt"] = False
            grade["score"] = 9.0
    _fake_fetch(monkeypatch, payload=payload)
    sync.run_sync(["blackboard"], raw_only=False)

    conn = db.connect(configured / "grades.db")
    try:
        kinds = {
            row["kind"]
            for row in conn.execute("SELECT kind FROM changes WHERE kind != 'new'")
        }
        assert "status" in kinds
    finally:
        conn.close()


def test_an_unchanged_resync_records_nothing_new(configured, monkeypatch, recorder):
    _fake_fetch(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)
    conn = db.connect(configured / "grades.db")
    before = conn.execute("SELECT COUNT(*) AS n FROM changes").fetchone()["n"]
    conn.close()
    recorder.sent.clear()

    sync.run_sync(["blackboard"], raw_only=False)

    conn = db.connect(configured / "grades.db")
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM changes").fetchone()["n"] == before
    finally:
        conn.close()
    assert recorder.sent == []


def test_many_new_grades_collapse_into_one_summary():
    changes = [
        changes_store.Change(
            assignment_id=index,
            kind=changes_store.SCORE,
            old_value="-/20",
            new_value="18/20",
            name=f"PS {index}",
            course_label="ECON 166",
            is_new_grade=True,
        )
        for index in range(1, 8)
    ]

    notifications = changes_store.notifications_for(changes)

    assert len(notifications) == 1
    assert notifications[0][0] == "7 new grades"
    assert "ECON 166" in notifications[0][1]


def test_a_handful_of_grades_get_one_notification_each():
    changes = [
        changes_store.Change(
            assignment_id=index,
            kind=changes_store.SCORE,
            old_value="-/20",
            new_value="18/20",
            name=f"PS {index}",
            course_label="ECON 166",
            is_new_grade=True,
        )
        for index in range(1, 4)
    ]

    assert len(changes_store.notifications_for(changes)) == 3


def test_status_only_changes_do_not_notify():
    changes = [
        changes_store.Change(
            assignment_id=1,
            kind=changes_store.STATUS,
            old_value="ungraded",
            new_value="submitted",
            name="PS 1",
            course_label="ECON 166",
        )
    ]

    assert changes_store.notifications_for(changes) == []


def test_recent_changes_names_the_course_and_assignment(configured, monkeypatch, recorder):
    _fake_fetch(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)
    conn = db.connect(configured / "grades.db")
    linking.link_auto(conn)
    conn.close()

    _fake_fetch(monkeypatch)
    payload = bb_payload()
    for grade in payload["courses"][0]["grades"]:
        if grade["columnId"] == "_14004_1":
            grade["score"] = 88.0
    _fake_fetch(monkeypatch, payload=payload)
    sync.run_sync(["blackboard"], raw_only=False)

    conn = db.connect(configured / "grades.db")
    try:
        rows = changes_store.recent_changes(conn, limit=20)
        assert rows
        latest = rows[0]
        assert latest["assignment_name"]
        assert latest["course_code"]
    finally:
        conn.close()


def test_is_first_sync_flips_after_a_successful_run(configured, monkeypatch, recorder):
    conn = db.connect(configured / "grades.db")
    assert changes_store.is_first_sync(conn, "blackboard") is True
    conn.close()

    _fake_fetch(monkeypatch)
    sync.run_sync(["blackboard"], raw_only=False)

    conn = db.connect(configured / "grades.db")
    try:
        assert changes_store.is_first_sync(conn, "blackboard") is False
        # The other source has still never synced.
        assert changes_store.is_first_sync(conn, "gradescope") is True
    finally:
        conn.close()


# --- schtasks argv --------------------------------------------------------


def test_build_schtasks_args_default():
    args = scheduler.build_schtasks_args(executable=r"C:\gt\.venv\Scripts\gt.exe")

    assert args == [
        "schtasks.exe",
        "/Create",
        "/SC",
        "HOURLY",
        "/MO",
        "4",
        "/TN",
        "GradeTracker Sync",
        "/TR",
        r'"C:\gt\.venv\Scripts\gt.exe" sync',
        "/F",
    ]


def test_build_schtasks_args_honours_the_interval():
    args = scheduler.build_schtasks_args(every_hours=2, executable="gt.exe")

    assert args[args.index("/MO") + 1] == "2"


def test_the_task_command_quotes_a_path_with_spaces():
    args = scheduler.build_schtasks_args(executable=r"C:\Program Files\gt\gt.exe")

    command = args[args.index("/TR") + 1]
    assert command == r'"C:\Program Files\gt\gt.exe" sync'


def test_the_argv_is_a_list_so_no_shell_is_involved():
    args = scheduler.build_schtasks_args(executable="gt.exe")

    assert isinstance(args, list)
    assert all(isinstance(arg, str) for arg in args)


def test_delete_and_query_argv():
    assert scheduler.build_schtasks_delete_args() == [
        "schtasks.exe",
        "/Delete",
        "/TN",
        "GradeTracker Sync",
        "/F",
    ]
    assert scheduler.build_schtasks_query_args() == [
        "schtasks.exe",
        "/Query",
        "/TN",
        "GradeTracker Sync",
    ]


def test_gt_executable_points_into_this_install():
    path = scheduler.gt_executable()

    assert path.endswith("gt.exe") or path.endswith("gt")


def test_schedule_install_never_runs_during_tests(monkeypatch):
    """Guard: the installer must go through subprocess with an argv list."""
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["shell"] = kwargs.get("shell", False)

        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Result()

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)

    scheduler.install(4)

    assert captured["args"][0] == "schtasks.exe"
    assert captured["shell"] is False


# --- in-process scheduler -------------------------------------------------


def test_the_scheduler_registers_an_interval_and_a_startup_job():
    calls = []
    background = scheduler.SyncScheduler(lambda: calls.append(1), startup_delay_s=3600)

    background.start()
    try:
        assert background.running is True
        assert sorted(background.jobs()) == ["gt-sync-interval", "gt-sync-startup"]
    finally:
        background.shutdown()

    assert background.running is False


def test_starting_twice_is_harmless():
    background = scheduler.SyncScheduler(lambda: None, startup_delay_s=3600)
    background.start()
    try:
        background.start()
        assert len(background.jobs()) == 2
    finally:
        background.shutdown()


def test_the_startup_job_actually_fires():
    fired = []
    background = scheduler.SyncScheduler(lambda: fired.append(1), startup_delay_s=0.01)

    background.start()
    try:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
        while not fired and datetime.now(timezone.utc) < deadline:
            import time

            time.sleep(0.02)
    finally:
        background.shutdown()

    assert fired == [1]


def test_create_app_does_not_start_a_scheduler_by_default(configured):
    from gradetracker.web.app import create_app

    app = create_app(db_path=configured / "grades.db", sync_runner=lambda: [], launcher=lambda: [])

    assert "gt_scheduler" not in app.extensions


def test_create_app_with_a_scheduler_shares_the_sync_lock(configured):
    """A scheduled run and a manual click must not overlap."""
    from gradetracker.web.app import create_app

    ran = []
    app = create_app(
        db_path=configured / "grades.db",
        sync_runner=lambda: ran.append(1) or ["ok"],
        launcher=lambda: [],
        scheduler=True,
    )
    try:
        background = app.extensions["gt_scheduler"]
        assert background.running is True
        # The scheduled job drives the same BackgroundJob the button uses.
        assert app.extensions["gt_sync_job"] is not None
    finally:
        app.extensions["gt_scheduler"].shutdown()
