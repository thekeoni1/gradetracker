"""In-process scheduling, and the OS-level job (M8).

While gt serve is running, an APScheduler BackgroundScheduler syncs every four
hours plus once shortly after startup. It goes through the same lock the Sync
now button uses, so a scheduled run and a manual click cannot overlap.

For syncing when the app is not open there is an OS-level task instead:
schtasks on Windows. The argv is built by a pure function so it can be tested
without installing anything, and it is run as a list, never through a shell.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TASK_NAME = "GradeTracker Sync"
DEFAULT_INTERVAL_HOURS = 4
STARTUP_DELAY_S = 10

IS_WINDOWS = sys.platform == "win32"


# --- in-process scheduler -------------------------------------------------


class SyncScheduler:
    """Runs a callable every few hours, plus once just after startup."""

    def __init__(
        self,
        job,
        interval_hours: float = DEFAULT_INTERVAL_HOURS,
        startup_delay_s: float = STARTUP_DELAY_S,
    ) -> None:
        self.job = job
        self.interval_hours = interval_hours
        self.startup_delay_s = startup_delay_s
        self._scheduler = None

    def start(self) -> None:
        from apscheduler.schedulers.background import BackgroundScheduler

        if self._scheduler is not None:
            return

        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            self.job,
            "interval",
            hours=self.interval_hours,
            id="gt-sync-interval",
            # A laptop that was asleep must not wake to a queue of six syncs.
            coalesce=True,
            max_instances=1,
            # An hour, not ten minutes: a laptop that wakes up late should still
            # run its missed tick rather than wait up to four hours.
            misfire_grace_time=3600,
        )
        scheduler.add_job(
            self.job,
            "date",
            run_date=datetime.now(timezone.utc) + timedelta(seconds=self.startup_delay_s),
            id="gt-sync-startup",
        )
        scheduler.start()
        self._scheduler = scheduler

    def shutdown(self) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.shutdown(wait=False)
        finally:
            self._scheduler = None

    @property
    def running(self) -> bool:
        return self._scheduler is not None

    def jobs(self) -> list[str]:
        if self._scheduler is None:
            return []
        return [job.id for job in self._scheduler.get_jobs()]


# --- OS-level task --------------------------------------------------------


def gt_executable() -> str:
    """The absolute path to this install's gt.exe, for the scheduled command.

    Resolved from the running interpreter rather than PATH, so the task keeps
    working regardless of what PATH looks like when Windows runs it.
    """
    scripts = Path(sys.executable).parent
    candidate = scripts / ("gt.exe" if IS_WINDOWS else "gt")
    if candidate.exists():
        return str(candidate)
    return str(candidate)


def build_schtasks_args(
    every_hours: int = DEFAULT_INTERVAL_HOURS,
    task_name: str = TASK_NAME,
    executable: str | None = None,
) -> list[str]:
    """The argv for creating the scheduled task. Pure, so it is testable.

    /F overwrites an existing task of the same name, which makes re-running
    install idempotent rather than an error.
    """
    command = executable or gt_executable()
    return [
        "schtasks.exe",
        "/Create",
        "/SC",
        "HOURLY",
        "/MO",
        str(every_hours),
        "/TN",
        task_name,
        "/TR",
        f'"{command}" sync',
        "/F",
    ]


def build_schtasks_delete_args(task_name: str = TASK_NAME) -> list[str]:
    return ["schtasks.exe", "/Delete", "/TN", task_name, "/F"]


def build_schtasks_query_args(task_name: str = TASK_NAME) -> list[str]:
    return ["schtasks.exe", "/Query", "/TN", task_name]


def _run(args: list[str]) -> subprocess.CompletedProcess:
    # A list, never shell=True: the task name contains a space.
    return subprocess.run(args, capture_output=True, text=True, check=False)


def install(every_hours: int = DEFAULT_INTERVAL_HOURS) -> subprocess.CompletedProcess:
    return _run(build_schtasks_args(every_hours))


def uninstall() -> subprocess.CompletedProcess:
    return _run(build_schtasks_delete_args())


def status() -> subprocess.CompletedProcess:
    return _run(build_schtasks_query_args())


NON_WINDOWS_HINT = """Scheduled syncing outside the app is Windows-only for now.

On macOS, write a launchd plist to ~/Library/LaunchAgents that runs
    <gt path> sync
with StartInterval 14400, then: launchctl load ~/Library/LaunchAgents/<name>.plist

On Linux, a crontab line does the same:
    0 */4 * * * <gt path> sync
"""
