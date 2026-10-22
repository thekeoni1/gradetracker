"""Finding this machine's Tailscale address, for serving to your own phone.

The dashboard has no login, so the only thing keeping it private is the
address it listens on. Loopback keeps it on this machine; a Tailscale address
keeps it on your own devices, wherever they are, because the tailnet is a
private network that campus wifi is not part of. Anything wider than that
would put your grades on whatever network you happened to join.

Tailscale hands out addresses from the 100.64.0.0/10 carrier-grade NAT range
and an fd7a:115c:a1e0::/48 ULA prefix, which is what lets us tell one of its
addresses apart from an ordinary LAN address without asking the daemon.
"""

from __future__ import annotations

import ipaddress
import shutil
import subprocess
from pathlib import Path

# The ranges Tailscale assigns from. Nothing else routes in these, so an
# address inside one is a tailnet address and not, say, a hotel router's.
TAILNET_RANGES = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)

# Where the Windows installer puts the CLI when it is not on PATH.
KNOWN_PATHS = (
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)


def is_tailnet_address(value: str) -> bool:
    """True for an address Tailscale would have assigned."""
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return False
    return any(address in network for network in TAILNET_RANGES)


def parse_addresses(output: str) -> list[str]:
    """The tailnet addresses in `tailscale ip` output, in the order printed.

    Kept separate from running the command so the parsing is testable without
    Tailscale installed, and so odd output (a warning line, a blank line, an
    address from some other interface) is dropped rather than returned.
    """
    return [
        line.strip()
        for line in (output or "").splitlines()
        if is_tailnet_address(line)
    ]


def executable() -> str | None:
    """The tailscale CLI, from PATH or where the installer puts it."""
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in KNOWN_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def find_addresses(run=None, exe: str | None = None) -> list[str]:
    """Every address Tailscale lists for this machine, v4 and v6.

    An empty list covers every way this can fail -- Tailscale not installed,
    not running, logged out -- because the caller says the same thing in each
    case: tell the user how to turn it on.

    This is the only trustworthy answer to "is that address mine on the
    tailnet?". The ranges above narrow the field, but a phone hotspot hands
    out 100.x addresses from the same CGNAT range on the wifi interface, and
    nothing but the daemon's own list tells one of those from a tailnet peer.
    """
    tailscale = exe or executable()
    if tailscale is None:
        return []

    runner = run or subprocess.run
    try:
        result = runner(
            [tailscale, "ip"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    if getattr(result, "returncode", 1) != 0:
        return []
    return parse_addresses(result.stdout)


def find_address(run=None, exe: str | None = None) -> str | None:
    """This machine's Tailscale IPv4 address, or None if there is not one.

    IPv4 because that is what to bind to and what to type on a phone; the v6
    address is listed too and `find_addresses` returns both.
    """
    for address in find_addresses(run=run, exe=exe):
        if ipaddress.ip_address(address).version == 4:
            return address
    return None


def lists_address(value: str, run=None, exe: str | None = None) -> bool:
    """True when Tailscale itself claims this exact address for this machine."""
    try:
        wanted = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return False
    return any(
        ipaddress.ip_address(address) == wanted
        for address in find_addresses(run=run, exe=exe)
    )
