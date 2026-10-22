"""AuthProvider protocol and AuthExpired, the seam between auth and fetching.

Connectors depend only on this interface, so M9 can swap OAuth in without
touching connector code. Filled in at M2.
"""

from __future__ import annotations

from typing import Protocol

import requests


class AuthExpired(Exception):
    """Raised when credentials no longer work (a 401/403, or a missing secret)."""


class AuthProvider(Protocol):
    """Hands back an authenticated session and reports whether it still works."""

    def session(self) -> requests.Session:
        ...

    def is_valid(self) -> bool:
        ...
