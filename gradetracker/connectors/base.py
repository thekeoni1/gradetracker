"""Connector protocol: fetch() -> dict, one dictionary per sync (M2)."""

from __future__ import annotations

from typing import Any, Protocol


class ConnectorError(Exception):
    """A fetch failed for a reason that is not authentication.

    A changed endpoint or an unexpected response shape lands here. M8 records
    these as sync_runs with status "error" and leaves the last good data alone.
    """


class AccessDenied(Exception):
    """This account may not read one specific resource.

    Deliberately not an AuthExpired: a 403 on one course's gradebook (a hidden
    gradebook, an organization, a course that withholds columns from students)
    says nothing about the session. Callers skip that resource and carry on,
    rather than sending the user through Duo for a course they cannot see
    anyway. Deliberately not a ConnectorError either, so a generic error
    handler cannot silently swallow it.
    """

    def __init__(self, message: str, status: int, url: str) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class Connector(Protocol):
    """Takes an AuthProvider, returns one raw dict per sync."""

    source: str

    def fetch(self) -> dict[str, Any]:
        ...
