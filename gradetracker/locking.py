"""A cross-process lock so two syncs cannot run at once.

The in-process lock in the web app only covers one process. With `gt schedule
install` AND `gt serve` both running, two syncs can overlap, and since each one
rewrites the cookie snapshot at the end, the loser's stale cookies can land on
top of the winner's fresh ones.

A lock file is the right tool here: it works across processes without a daemon,
and O_CREAT|O_EXCL makes the create-and-claim atomic.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from gradetracker import config

LOCK_NAME = "sync.lock"

# A sync that has held the lock for longer than this has almost certainly died;
# a crashed process must not block syncing forever.
STALE_AFTER_S = 2 * 60 * 60


def lock_path(directory: Path | str | None = None) -> Path:
    base = Path(directory) if directory is not None else Path(config.DATA_DIR)
    return base / LOCK_NAME


def _age(path: Path) -> float | None:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _try_acquire(path: Path, stale_after_s: float) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = _age(path)
            if age is not None and age > stale_after_s:
                # Whoever held this is gone. Clear it and try once more.
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            return False
        else:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(f"{os.getpid()}\n{time.time()}\n")
            return True
    return False


@contextlib.contextmanager
def sync_lock(directory: Path | str | None = None, stale_after_s: float = STALE_AFTER_S):
    """Yield True if this process got the lock, False if another holds it.

    Yields rather than raising so the caller can decide: a scheduled run that
    loses the race should exit quietly, not report a failure.
    """
    path = lock_path(directory)
    acquired = _try_acquire(path, stale_after_s)
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                path.unlink()
