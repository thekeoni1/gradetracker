"""CookieAuth: saved browser login -> requests.Session (M2).

Turns the login the launcher captured into a plain HTTP session with JSON accept
headers. Validity is checked against the same endpoint the launcher polls.

Two cookie sources, in order: the snapshot the launcher wrote while its window
was open (which holds the session cookies Chromium drops when a profile closes),
then the profile itself (which holds the cookies that carry an expiry). If the
snapshot turns out to be stale, the profile gets a turn before giving up.

This is the first AuthProvider implementation. M9's OAuthAuth satisfies the same
interface, so nothing downstream changes when it arrives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Literal

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from gradetracker.auth.base import AuthExpired

# The launcher owns the definition of "this response proves we're logged in",
# and the snapshot format; reusing both keeps login and validity in agreement.
from gradetracker.auth.launcher import (
    _is_authenticated,
    read_cookie_snapshot,
    save_cookie_snapshot,
)

# What a session check concluded. "offline" is not "logged out": reporting a
# dead network as an expired session would send the user through Duo for nothing.
SessionState = Literal["valid", "expired", "offline"]

# A plain desktop UA. The bundled Chromium's own UA advertises HeadlessChrome,
# which some SSO stacks treat differently.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _cookies_to_session(
    cookie_dicts: Iterable[dict[str, Any]], session: requests.Session
) -> requests.Session:
    """Copy Playwright-shaped cookie dicts into a requests session.

    Pure and browser-free so it can be unit-tested. Only name, value, domain and
    path matter to requests; expiry and the sameSite/httpOnly flags are the
    browser's business and are dropped deliberately.
    """
    for cookie in cookie_dicts:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        session.cookies.set(
            name,
            value,
            domain=cookie.get("domain", ""),
            path=cookie.get("path", "/"),
        )
    return session


def _session_to_cookie_dicts(session: requests.Session) -> list[dict[str, Any]]:
    """The inverse of _cookies_to_session, for writing a snapshot back."""
    return [
        {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
        }
        for cookie in session.cookies
    ]


class CookieAuth:
    """An AuthProvider backed by the login the launcher saved."""

    def __init__(
        self,
        base_url: str,
        check_path: str,
        profile_dir: Path,
        channel: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.check_path = check_path
        self.profile_dir = Path(profile_dir)
        self.channel = channel
        self._session: requests.Session | None = None
        self._state: SessionState | None = None

    @property
    def check_url(self) -> str:
        return f"{self.base_url}{self.check_path}"

    # --- cookie sources --------------------------------------------------

    def _read_cookies_from_profile(self) -> list[dict[str, Any]]:
        """Open the profile headlessly just long enough to read its cookies.

        Playwright filters to the cookies that would be sent to base_url, which
        covers the Blackboard host and its parent domains.
        """
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=True,
                channel=self.channel,
            )
            try:
                return context.cookies(self.base_url)
            finally:
                try:
                    context.close()
                except PlaywrightError:
                    pass

    def _build_session(self, cookies: Iterable[dict[str, Any]]) -> requests.Session:
        session = requests.Session()
        session.headers.update({"Accept": "application/json", "User-Agent": _USER_AGENT})
        _cookies_to_session(cookies, session)
        return session

    # --- validation ------------------------------------------------------

    def _probe(self, session: requests.Session) -> SessionState:
        """One GET against the check endpoint. The only place state is decided."""
        try:
            response = session.get(self.check_url, timeout=30)
        except requests.RequestException:
            return "offline"
        return "valid" if _is_authenticated(response.status_code, response.text) else "expired"

    # --- AuthProvider ----------------------------------------------------

    def session(self) -> requests.Session:
        """An authenticated HTTP session, or AuthExpired if login is needed.

        Tries the snapshot first and only falls back to the profile if the
        snapshot proves stale, so the usual path opens no browser at all.
        """
        if self._session is not None:
            return self._session

        if not self.profile_dir.exists():
            raise AuthExpired(
                f"No saved Blackboard profile at {self.profile_dir}. Run gt login blackboard."
            )

        snapshot = read_cookie_snapshot(self.profile_dir)
        if snapshot:
            candidate = self._build_session(snapshot)
            state = self._probe(candidate)
            # Offline says nothing about staleness, so keep this session rather
            # than launching a browser to re-read cookies we cannot test.
            if state in ("valid", "offline"):
                return self._accept(candidate, state)

        try:
            profile_cookies = self._read_cookies_from_profile()
        except PlaywrightError as exc:
            raise AuthExpired(f"Could not read the saved browser profile: {exc}") from exc

        if profile_cookies:
            candidate = self._build_session(profile_cookies)
            state = self._probe(candidate)
            if state in ("valid", "offline"):
                return self._accept(candidate, state)

        raise AuthExpired(
            "The saved Blackboard session is no longer valid. Run gt login blackboard."
        )

    def _accept(self, session: requests.Session, state: SessionState) -> requests.Session:
        self._session = session
        self._state = state
        return session

    def check(self) -> SessionState:
        """Report the session's state without raising. Probes at most once."""
        try:
            session = self.session()
        except AuthExpired:
            return "expired"
        if self._state is not None:
            return self._state
        return self._probe(session)

    def is_valid(self) -> bool:
        """The AuthProvider protocol's boolean view of check()."""
        return self.check() == "valid"

    def persist(self) -> bool:
        """Write the session's current cookies back to the snapshot.

        Blackboard re-issues its auth cookie with a pushed-out expiry on every
        response, so saving the jar after a sync is what lets a sliding session
        actually slide. Without it the session dies one idle timeout after login
        however often we sync.

        Returns False if nothing was saved; a failed save must not fail a sync.
        """
        if self._session is None:
            return False
        try:
            save_cookie_snapshot(self.profile_dir, _session_to_cookie_dicts(self._session))
        except OSError:
            return False
        except Exception:
            # A DPAPI failure is not worth losing a good sync over.
            return False
        return True
