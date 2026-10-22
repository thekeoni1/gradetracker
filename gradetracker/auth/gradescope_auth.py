"""GradescopeAuth: keyring credentials -> logged-in gradescopeapi session (M3).

Unlike Blackboard there is no browser step: a Gradescope account has a native
password with no 2FA, so this provider can re-login on its own and never needs a
reconnect prompt. The password lives in the OS keychain, never in a file or the
database; GS_EMAIL in .env is only a default for the login prompt.

Attribute names this module depends on, in gradescopeapi==1.8.1. The library
reshapes these between releases, which is why the version is pinned:

    GSConnection(gradescope_base_url=...)   classes/connection.py
    GSConnection.login(email, password)     raises ValueError on bad credentials
    GSConnection.session                    the underlying requests.Session
    GSConnection.logged_in                  bool
    GSConnection.account                    an Account, set only after login
    Account.get_courses()                   {"student": {id: Course}, "instructor": {...}}
    Account.get_assignments(course_id)      list[Assignment]

Login reaches into the page's HTML for an authenticity token and a csrf meta
tag, so a Gradescope redesign surfaces as TypeError rather than a clean failure.
Every such breakage is mapped to AuthExpired here, because the PDF wants a
changed login page to flow into the same reconnect path as a wrong password.

SSO fallback (not needed today): if Hamilton ever forces Gradescope through
school login, the M1 launcher pattern would be reused with a "gradescope"
profile plus CookieAuth, and the connector would scrape with that session.
"""

from __future__ import annotations

import contextlib
from typing import Literal

import keyring
import keyring.errors
import requests
from requests.adapters import HTTPAdapter

from gradetracker.auth.base import AuthExpired
from gradetracker.redact import safe_error

SERVICE = "gradetracker"

# Gradescope can stall; without this a sync thread hangs for the life of the
# process and "Sync now" reports "already running" forever.
REQUEST_TIMEOUT_S = 30
EMAIL_KEY = "gradescope:email"
PASSWORD_KEY = "gradescope:password"

SessionState = Literal["valid", "expired", "offline"]

# Raised by requests when the network, not Gradescope, is the problem.
_OFFLINE_ERRORS = (requests.ConnectionError, requests.Timeout)


def store_credentials(email: str, password: str) -> None:
    """Save both halves in the OS keychain. Neither touches disk in plaintext."""
    try:
        keyring.set_password(SERVICE, EMAIL_KEY, email)
        keyring.set_password(SERVICE, PASSWORD_KEY, password)
    except keyring.errors.KeyringError as exc:
        raise AuthExpired(f"Could not write to the OS keychain: {exc}") from exc


def load_credentials() -> tuple[str, str] | None:
    """The saved (email, password), or None if either half is missing."""
    try:
        email = keyring.get_password(SERVICE, EMAIL_KEY)
        password = keyring.get_password(SERVICE, PASSWORD_KEY)
    except keyring.errors.KeyringError:
        return None
    if not email or not password:
        return None
    return email, password


def clear_credentials() -> None:
    """Forget the saved login. Missing entries are not an error."""
    for key in (EMAIL_KEY, PASSWORD_KEY):
        try:
            keyring.delete_password(SERVICE, key)
        except keyring.errors.KeyringError:
            pass


def verify_credentials(email: str, password: str) -> None:
    """Log in once to prove a password works. Raises AuthExpired if it does not.

    Used by gt login gradescope before anything is stored, so a typo never ends
    up in the keychain.
    """
    _login(email, password)


class _TimeoutAdapter(HTTPAdapter):
    """Applies a default timeout to every request through a session."""

    def __init__(self, timeout: float = REQUEST_TIMEOUT_S, **kwargs) -> None:
        self._timeout = timeout
        super().__init__(**kwargs)

    def send(self, request, **kwargs):  # noqa: ANN001
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(request, **kwargs)


def _apply_timeouts(session: requests.Session) -> requests.Session:
    adapter = _TimeoutAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _login_with_body(
    session, email, password, auth_token, gradescope_base_url=None
) -> bool:
    """Post the credentials in the BODY rather than the query string.

    gradescopeapi 1.8.1 calls session.post(..., params=login_data), which puts
    the password in the URL. requests then embeds that URL in any connection
    error's str(), and such a string used to reach sync_runs.message and the
    dashboard. Sending the same fields as form data is what a browser does and
    keeps the password out of every URL, log and exception.

    The success check mirrors the library's exactly, so behaviour is unchanged.
    """
    from bs4 import BeautifulSoup
    from gradescopeapi import DEFAULT_GRADESCOPE_BASE_URL

    base = gradescope_base_url or DEFAULT_GRADESCOPE_BASE_URL
    login_data = {
        "utf8": "✓",
        "session[email]": email,
        "session[password]": password,
        "session[remember_me]": 0,
        "commit": "Log In",
        "session[remember_me_sso]": 0,
        "authenticity_token": auth_token,
    }

    response = session.post(f"{base}/login", data=login_data)

    if (
        len(response.history) != 0
        and response.history[0].status_code == requests.codes.found
    ):
        soup = BeautifulSoup(response.text, "html.parser")
        token = soup.select_one('meta[name="csrf-token"]')
        session.cookies.update(response.cookies)
        if token is not None:
            session.headers.update({"X-CSRF-Token": token["content"]})
        return True
    return False


@contextlib.contextmanager
def _body_login_patch():
    """Swap the library's login helper for the body-posting one, temporarily."""
    from gradescopeapi.classes import connection as connection_module

    original = connection_module.login_set_session_cookies
    connection_module.login_set_session_cookies = _login_with_body
    try:
        yield
    finally:
        connection_module.login_set_session_cookies = original


def _login(email: str, password: str):
    """One login attempt, with every failure mode mapped onto AuthExpired.

    NOTHING from a requests exception is interpolated into a message here: on
    this code path the exception text can contain the password. Only the
    exception class name is reported.
    """
    from gradescopeapi.classes.connection import GSConnection

    connection = GSConnection()
    _apply_timeouts(connection.session)

    try:
        with _body_login_patch():
            connection.login(email, password)
    except _OFFLINE_ERRORS:
        raise
    except ValueError as exc:
        # The library raises ValueError("Invalid credentials.") -- safe text,
        # but reported by class name anyway so this path cannot regress.
        raise AuthExpired("Gradescope rejected the saved credentials") from exc
    except Exception as exc:
        # TypeError/AttributeError here means the login page moved; the PDF wants
        # that handled by the same reconnect flow as a wrong password.
        raise AuthExpired(
            f"Gradescope login failed ({safe_error(exc)}). The login page may have changed."
        ) from exc
    return connection


class GradescopeAuth:
    """An AuthProvider backed by a password in the OS keychain."""

    source = "gradescope"

    def __init__(self) -> None:
        self.connection = None
        self.email: str | None = None
        self._session: requests.Session | None = None
        self._state: SessionState | None = None

    def session(self) -> requests.Session:
        """A logged-in HTTP session. Logs in once per instance."""
        if self._session is not None:
            return self._session

        credentials = load_credentials()
        if credentials is None:
            raise AuthExpired(
                "No saved Gradescope credentials. Run gt login gradescope."
            )

        email, password = credentials
        try:
            connection = _login(email, password)
        except _OFFLINE_ERRORS as exc:
            # Class name only: the exception text carries the request URL.
            raise AuthExpired(f"Could not reach Gradescope ({safe_error(exc)})") from exc

        self.connection = connection
        self.email = email
        self._session = connection.session
        self._state = "valid"
        return self._session

    def check(self) -> SessionState:
        """Report the credentials' state without raising."""
        if self._state is not None:
            return self._state

        credentials = load_credentials()
        if credentials is None:
            return "expired"

        email, password = credentials
        try:
            connection = _login(email, password)
        except _OFFLINE_ERRORS:
            return "offline"
        except AuthExpired:
            return "expired"

        # Keep the successful login rather than throwing the work away.
        self.connection = connection
        self.email = email
        self._session = connection.session
        self._state = "valid"
        return "valid"

    def is_valid(self) -> bool:
        """The AuthProvider protocol's boolean view of check()."""
        return self.check() == "valid"
