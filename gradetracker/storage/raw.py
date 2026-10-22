"""Timestamped raw dumps under <DATA_DIR>/raw/<source>/<ISO timestamp>.json (M2).

Raw before parsed: every sync saves what it pulled before anything normalizes
it, so parsing bugs stay debuggable and cookie-vs-OAuth output can be diffed
later. Keeps the last 30 dumps per source.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gradetracker import config

KEEP_DEFAULT = 30


def _timestamp() -> str:
    """A filesystem-safe, lexicographically sortable UTC stamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")


def _source_dir(source: str, raw_dir: Path | None = None) -> Path:
    return Path(raw_dir if raw_dir is not None else config.RAW_DIR) / source


def write_raw(
    source: str,
    payload: Any,
    raw_dir: Path | None = None,
    keep: int = KEEP_DEFAULT,
) -> Path:
    """Write one dump and prune older ones. Returns the new file's path."""
    directory = _source_dir(source, raw_dir)
    directory.mkdir(parents=True, exist_ok=True)

    path = directory / f"{_timestamp()}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    _prune(directory, keep)
    return path


def _prune(directory: Path, keep: int) -> None:
    """Delete all but the newest `keep` dumps. Filenames sort chronologically."""
    if keep <= 0:
        return
    dumps = sorted(directory.glob("*.json"))
    for stale in dumps[:-keep]:
        try:
            stale.unlink()
        except OSError:
            pass


def list_raw(source: str, raw_dir: Path | None = None) -> list[Path]:
    """Every dump for a source, oldest first. Empty if the source has none."""
    directory = _source_dir(source, raw_dir)
    if not directory.exists():
        return []
    return sorted(directory.glob("*.json"))


def latest_raw(source: str, raw_dir: Path | None = None) -> Path | None:
    """The newest dump for a source, or None."""
    dumps = list_raw(source, raw_dir)
    return dumps[-1] if dumps else None


def read_raw(path: Path) -> Any:
    """Parse a dump back. Handy for M4's normalizer and for debugging."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
