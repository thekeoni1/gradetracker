"""Scrubbing secrets out of text that gets stored or displayed.

gradescopeapi 1.8.1 sends the login POST with params= rather than data=, which
puts the password in the URL query string. A requests exception then carries the
whole URL in str(exc), and that string used to flow into sync_runs.message and
onto the Connections page -- so one dropped Wi-Fi packet mid-login would persist
the password in plaintext.

The real fix is to send the credentials in the body (see
auth/gradescope_auth._login). This module is the belt-and-braces: every place a
message is stored or rendered runs through scrub(), so a path we missed, or a
future library change, cannot leak a password into the database or the UI.
"""

from __future__ import annotations

import re

REDACTED = "[redacted]"

# session[password]=..., session%5Bpassword%5D=..., password=..., and the
# common api_key / token spellings, up to the next separator.
_PATTERNS = (
    re.compile(r"(session(?:%5B|\[)password(?:%5D|\])=)[^&\s\"']*", re.IGNORECASE),
    re.compile(r"\b(password=)[^&\s\"']*", re.IGNORECASE),
    re.compile(r"\b(passwd=)[^&\s\"']*", re.IGNORECASE),
    re.compile(r"\b((?:api_?key|access_token|refresh_token)=)[^&\s\"']*", re.IGNORECASE),
)


def scrub(text: str | None) -> str | None:
    """Replace any credential-looking query value with a placeholder.

    Returns the input unchanged when there is nothing to redact, and passes
    None through so callers can use it on optional messages.
    """
    if not text:
        return text

    scrubbed = str(text)
    for pattern in _PATTERNS:
        scrubbed = pattern.sub(lambda match: match.group(1) + REDACTED, scrubbed)
    return scrubbed


def safe_error(exc: BaseException) -> str:
    """A description of an exception that cannot contain a request URL.

    Used on the login path, where the exception's own text is untrustworthy:
    only the class name is reported. Elsewhere scrub() on the message is enough.
    """
    return type(exc).__name__
