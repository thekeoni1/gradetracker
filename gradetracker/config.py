"""Paths and environment loading.

Every endpoint path is configurable so it can be swapped after real recon
without touching connector code.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

# --- Blackboard ---
BB_BASE_URL = os.getenv("BB_BASE_URL", "").rstrip("/")
BB_CHECK_PATH = os.getenv("BB_CHECK_PATH", "/learn/api/public/v1/users/me")
# None = Playwright's bundled Chromium. Set to "chrome" if SSO flags it.
BB_BROWSER_CHANNEL = os.getenv("BB_BROWSER_CHANNEL") or None

# Endpoint templates. These are the standard Blackboard Learn public REST paths;
# every one is overridable so real recon can swap in whatever Hamilton serves
# without touching connector code.
BB_PATH_ME = os.getenv("BB_PATH_ME", "/learn/api/public/v1/users/me")
BB_PATH_COURSES = os.getenv("BB_PATH_COURSES", "/learn/api/public/v1/users/{userId}/courses")
BB_PATH_COLUMNS = os.getenv(
    "BB_PATH_COLUMNS", "/learn/api/public/v2/courses/{courseId}/gradebook/columns"
)
BB_PATH_GRADES = os.getenv(
    "BB_PATH_GRADES", "/learn/api/public/v2/courses/{courseId}/gradebook/users/{userId}"
)

# Optional exact term filter, e.g. "_123_1". Empty = keep every available course.
BB_TERM_ID = os.getenv("BB_TERM_ID") or None

# --- Gradescope ---
# Only a default for the login prompt; the real email and password live in the
# OS keychain (see auth/gradescope_auth.py).
GS_EMAIL = os.getenv("GS_EMAIL", "")

# Optional exact term, e.g. "Fall 2026". Empty = sync the newest term present.
GS_TERM = os.getenv("GS_TERM") or None

# --- notifications ---
# GT_NOTIFY=0 turns off every desktop notification.
GT_NOTIFY = os.getenv("GT_NOTIFY", "1")

# --- Paths ---
# Deliberately outside the project tree: this folder may live in OneDrive, which
# would sync session tokens to the cloud and lock Chromium's profile SQLite
# mid-sync. data/ stays in .gitignore anyway, in case GT_DATA_DIR points back in.


def _default_data_dir() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "GradeTracker"
    return Path.home() / ".local" / "share" / "gradetracker"


DATA_DIR = Path(os.getenv("GT_DATA_DIR") or _default_data_dir())
RAW_DIR = DATA_DIR / "raw"
PROFILES_DIR = DATA_DIR / "profiles"
BB_PROFILE_DIR = PROFILES_DIR / "blackboard"
SYLLABI_DIR = DATA_DIR / "syllabi"
DB_PATH = DATA_DIR / "grades.db"


def ensure_dirs() -> None:
    """Create the local data directories. Safe to call repeatedly."""
    for path in (
        DATA_DIR,
        RAW_DIR,
        RAW_DIR / "blackboard",
        RAW_DIR / "gradescope",
        PROFILES_DIR,
        SYLLABI_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
