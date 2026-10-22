"""Visible-browser login with a persistent Playwright profile (M1).

Opens a real Chromium window, lets the user complete Hamilton SSO and Duo by
hand, and polls the cheap authenticated endpoint until it answers. The password
never passes through this code.

Polling an API endpoint beats waiting on a URL pattern: SSO redirects pass
through several URLs and some look like Blackboard before you are actually
authenticated.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import typer
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from gradetracker.auth import secretbox

# A check_fn returns (status_code, body_text).
CheckFn = Callable[[], "tuple[int, str]"]

# Keys that mark a real Blackboard user object (/learn/api/public/v1/users/me).
_USER_KEYS = ("id", "userName")


class PollingAborted(Exception):
    """The window went away before login completed, so polling cannot continue."""


def _is_authenticated(status: int, body: str) -> bool:
    """True only for a 200 carrying a JSON user object.

    A 401/403, an HTML login page, or a 200 of something else all mean
    "not authenticated yet".
    """
    if status != 200:
        return False
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    return any(key in payload for key in _USER_KEYS)


def _poll_until_authenticated(
    check_fn: CheckFn,
    timeout_s: float = 300.0,
    poll_interval_s: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Call check_fn until it reports an authenticated response or time runs out.

    Browser-free so it can be unit-tested with fakes. Returns True on the first
    success, False on timeout or if check_fn raises PollingAborted.
    """
    deadline = clock() + timeout_s
    while True:
        try:
            status, body = check_fn()
        except PollingAborted:
            return False
        if _is_authenticated(status, body):
            return True
        if clock() >= deadline:
            return False
        sleep(poll_interval_s)


def _login_stamp_path(profile_dir: Path) -> Path:
    """Sibling of the profile dir, e.g. <DATA_DIR>/profiles/blackboard.login.json."""
    return profile_dir.parent / f"{profile_dir.name}.login.json"


def write_login_timestamp(profile_dir: Path) -> None:
    """Record when this profile last logged in. M8 reads it for session duration."""
    path = _login_stamp_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = {"logged_in_at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(stamp), encoding="utf-8")


def read_login_timestamp(profile_dir: Path) -> str | None:
    """The stored ISO login time, or None if absent or unreadable."""
    path = _login_stamp_path(profile_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("logged_in_at")
    except (OSError, ValueError, AttributeError):
        return None


def _cookie_snapshot_path(profile_dir: Path) -> Path:
    """Sibling of the profile dir, e.g. <data>/profiles/blackboard.cookies.bin."""
    return profile_dir.parent / f"{profile_dir.name}.cookies.bin"


def save_cookie_snapshot(profile_dir: Path, cookies: list[dict]) -> Path:
    """Persist the cookies captured at the moment login succeeded.

    Chromium discards session cookies (no Max-Age or Expires) when a persistent
    profile closes, and Blackboard's auth cookies are session cookies, so
    reopening the profile later can come back without them. Capturing them while
    the window is still open is what makes a later headless sync possible.

    These are live session tokens, so the file is DPAPI-encrypted for the
    current Windows user and written atomically: a half-written snapshot would
    otherwise read back as "no snapshot" and force a pointless re-login.

    Off Windows there is no DPAPI and secretbox passes the bytes through, so the
    only thing standing between the tokens and another account on the machine is
    the file mode. mkstemp already creates at 0600, but that is incidental to
    this file's secrecy; chmod states it, so a later move off mkstemp cannot
    quietly widen it. The mode is set before the rename, because the rename is
    what publishes the name an attacker could open.
    """
    path = _cookie_snapshot_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    blob = secretbox.protect(json.dumps(cookies).encode("utf-8"))

    # Same directory, so os.replace is an atomic rename on the same volume.
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as tmp_file:
            tmp_file.write(blob)
        if not secretbox.IS_WINDOWS:
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except OSError:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def read_cookie_snapshot(profile_dir: Path) -> list[dict]:
    """The saved cookies, or an empty list if there is no usable snapshot.

    A missing, truncated, corrupt or foreign-user file all mean the same thing
    to the caller: fall back to the profile, or ask for a fresh login.
    """
    path = _cookie_snapshot_path(profile_dir)
    try:
        blob = path.read_bytes()
    except OSError:
        return []

    try:
        cookies = json.loads(secretbox.unprotect(blob).decode("utf-8"))
    except (secretbox.SecretBoxError, ValueError, UnicodeDecodeError):
        return []
    return cookies if isinstance(cookies, list) else []


def _check_via_context(context, url: str) -> tuple[int, str, str]:
    """One request through the browser context, so it carries the profile cookies.

    Returns (status, body, final url); the URL is what reveals a host redirect.
    Raises PollingAborted if the window has been closed.
    """
    try:
        response = context.request.get(
            url,
            headers={"Accept": "application/json"},
            fail_on_status_code=False,
        )
        return response.status, response.text(), getattr(response, "url", "") or ""
    except PlaywrightError as exc:
        if "closed" in str(exc).lower():
            raise PollingAborted(str(exc)) from exc
        raise


def login_blackboard(
    base_url: str,
    check_path: str,
    profile_dir: Path,
    timeout_s: float = 300.0,
    poll_interval_s: float = 2.0,
    headless: bool = False,
    channel: str | None = None,
) -> bool:
    """Open a browser, wait for the user to finish SSO, and keep the profile.

    Returns True once the check endpoint returns a user object, False on timeout
    or if the user closes the window first. The persistent profile survives, so
    a ticked "Remember me" on Duo may skip the prompt next time.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    check_url = f"{base_url.rstrip('/')}{check_path}"
    expected_host = urlparse(base_url).netloc

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=headless,
                channel=channel,
            )
        except PlaywrightError as exc:
            # Usually the profile is locked because a headless sync is reading
            # it right now. Reporting a timeout here would be a lie.
            typer.echo(
                f"Could not open the browser profile ({exc}). Another sync or "
                "login window may be using it; wait a moment and try again.",
                err=True,
            )
            return False

        # The URL the check request actually landed on, so a host redirect is
        # visible rather than silently poisoning the cookie snapshot.
        last = {"url": ""}

        def check():
            status, body, url = _check_via_context(context, check_url)
            last["url"] = url
            return status, body

        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(base_url, wait_until="domcontentloaded")
                typer.echo(f"Login page opened at {page.url}")
            except PlaywrightError as exc:
                if "closed" in str(exc).lower():
                    return False
                # A stalled first load (seen on the very first launch of a
                # freshly installed Chromium) must not end the attempt: the
                # window is still open, so say so and let the user reload.
                typer.echo(
                    f"The login page did not load ({type(exc).__name__}). The "
                    f"window stays open: press F5, or type {base_url} into its "
                    "address bar, and log in as usual.",
                    err=True,
                )
            authenticated = _poll_until_authenticated(
                check,
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
            )
            if authenticated:
                final_host = urlparse(last["url"] or "").netloc
                if final_host and expected_host and final_host != expected_host:
                    # Cookies were stored for the host we were redirected TO, so
                    # a snapshot filtered by BB_BASE_URL would come back empty
                    # and every later sync would look like an expired session.
                    typer.echo(
                        f"Blackboard redirected to {final_host}; set BB_BASE_URL to "
                        f"https://{final_host} and log in again.",
                        err=True,
                    )
                    return False

                # Must happen before the context closes: see save_cookie_snapshot.
                # Unfiltered: this profile is only ever used for Blackboard, and
                # filtering by base_url loses cookies set on a redirect target.
                try:
                    save_cookie_snapshot(profile_dir, context.cookies())
                except (OSError, secretbox.SecretBoxError) as exc:
                    typer.echo(
                        f"Warning: could not save the session cookies ({exc}). "
                        "The browser profile is still saved, so a sync may still "
                        "work; if it reports an expired session, log in again.",
                        err=True,
                    )
            return authenticated
        except PlaywrightError as exc:
            # The user closed the window mid-login: an abort, not a crash.
            if "closed" in str(exc).lower():
                return False
            raise
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass


# Session validity is CookieAuth.is_valid's job (M2): it reuses _is_authenticated
# above, so the login check and the validity check agree by construction.
