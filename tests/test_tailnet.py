"""Serving to your own phone: finding the tailnet, and refusing anything wider."""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from gradetracker import tailnet
from gradetracker.cli import app as cli_app

runner = CliRunner()


class FakeResult:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


# --- telling a tailnet address from any other -----------------------------


@pytest.mark.parametrize(
    "address",
    ["100.101.102.103", "100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0::1"],
)
def test_a_tailscale_address_is_recognised(address):
    assert tailnet.is_tailnet_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "192.168.1.14",   # the LAN, which campus wifi is also on
        "10.0.0.5",
        "127.0.0.1",
        "100.128.0.1",    # just outside the CGNAT range
        "100.63.255.255",
        "8.8.8.8",
        "",
        "not an address",
    ],
)
def test_anything_else_is_not(address):
    assert not tailnet.is_tailnet_address(address)


def test_parse_takes_the_tailnet_lines_and_drops_the_rest():
    output = "\nwarning: something\n100.101.102.103\n192.168.1.14\nfd7a:115c:a1e0::1\n"

    assert tailnet.parse_addresses(output) == ["100.101.102.103", "fd7a:115c:a1e0::1"]


def test_find_address_returns_the_first_ipv4(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return FakeResult("100.101.102.103\nfd7a:115c:a1e0::1\n")

    assert tailnet.find_address(run=fake_run, exe="tailscale") == "100.101.102.103"
    # The full list, not just -4: --host has to validate v6 literals too.
    assert captured["args"] == ["tailscale", "ip"]


def test_find_addresses_returns_both_families():
    addresses = tailnet.find_addresses(
        run=lambda *a, **k: FakeResult("100.101.102.103\nfd7a:115c:a1e0::1\n"),
        exe="tailscale",
    )

    assert addresses == ["100.101.102.103", "fd7a:115c:a1e0::1"]


def test_find_address_skips_a_machine_with_only_ipv6():
    assert tailnet.find_address(
        run=lambda *a, **k: FakeResult("fd7a:115c:a1e0::1\n"), exe="tailscale"
    ) is None


def test_lists_address_asks_the_daemon_rather_than_the_range():
    """A phone hotspot hands out 100.x too; only the daemon's list is proof."""

    def listed(*args, **kwargs):
        return FakeResult("100.101.102.103\nfd7a:115c:a1e0::1\n")

    assert tailnet.lists_address("100.101.102.103", run=listed, exe="tailscale")
    assert tailnet.lists_address("fd7a:115c:a1e0::1", run=listed, exe="tailscale")
    # In the tailnet range, but not an address this machine holds.
    assert not tailnet.lists_address("100.64.9.9", run=listed, exe="tailscale")
    assert not tailnet.lists_address("192.168.1.5", run=listed, exe="tailscale")
    assert not tailnet.lists_address("mypc.local", run=listed, exe="tailscale")


def test_lists_address_accepts_an_equivalent_ipv6_spelling():
    """fd7a:115c:a1e0:0000::1 and fd7a:115c:a1e0::1 are one address."""
    assert tailnet.lists_address(
        "fd7a:115c:a1e0:0000::1",
        run=lambda *a, **k: FakeResult("fd7a:115c:a1e0::1\n"),
        exe="tailscale",
    )


def test_find_address_is_none_when_tailscale_is_not_installed(monkeypatch):
    monkeypatch.setattr(tailnet, "executable", lambda: None)
    monkeypatch.setattr(tailnet, "KNOWN_PATHS", ())

    assert tailnet.find_address() is None
    assert tailnet.find_addresses() == []
    assert not tailnet.lists_address("100.101.102.103")


def test_find_address_is_none_when_tailscale_is_logged_out():
    assert tailnet.find_address(
        run=lambda *a, **k: FakeResult("", returncode=1), exe="tailscale"
    ) is None


def test_find_address_is_none_when_the_command_fails():
    def explode(*args, **kwargs):
        raise OSError("no such file")

    assert tailnet.find_address(run=explode, exe="tailscale") is None


def test_find_address_survives_a_hung_tailscale():
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("tailscale", 10)

    assert tailnet.find_address(run=timeout, exe="tailscale") is None


# --- what gt serve binds to -----------------------------------------------


class FakeFlask:
    def __init__(self):
        self.kwargs = {}

    def run(self, **kwargs):
        self.kwargs.update(kwargs)


@pytest.fixture
def served(monkeypatch):
    """Capture what gt serve would have bound to, without binding."""
    fake = FakeFlask()
    captured = {}

    def fake_create_app(**kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr("gradetracker.web.app.create_app", fake_create_app)
    monkeypatch.setattr("webbrowser.open", lambda url: None)
    return fake, captured


def test_tailscale_binds_to_the_tailnet_address(served, monkeypatch):
    fake, captured = served
    monkeypatch.setattr(tailnet, "find_address", lambda *a, **k: "100.101.102.103")

    result = runner.invoke(cli_app, ["serve", "--tailscale", "--no-browser"])

    assert result.exit_code == 0
    assert fake.kwargs["host"] == "100.101.102.103"
    # And the app is told, or its own guard would turn the phone away.
    assert captured["extra_hosts"] == ("100.101.102.103",)
    assert "100.101.102.103:5000" in result.output
    assert "no login" in result.output


def test_tailscale_without_tailscale_running_is_a_clear_error(served, monkeypatch):
    monkeypatch.setattr(tailnet, "find_address", lambda *a, **k: None)

    result = runner.invoke(cli_app, ["serve", "--tailscale", "--no-browser"])

    assert result.exit_code == 1
    assert "Could not find a Tailscale address" in result.output
    assert "tailscale.com/download" in result.output


def test_a_wildcard_is_refused(served):
    """Campus wifi is not a network to put an unauthenticated dashboard on."""
    result = runner.invoke(cli_app, ["serve", "--host", "0.0.0.0", "--no-browser"])

    assert result.exit_code == 1
    assert "Refusing to listen on every address" in result.output


def test_both_flags_at_once_is_refused(served):
    result = runner.invoke(
        cli_app, ["serve", "--tailscale", "--host", "192.168.1.5", "--no-browser"]
    )

    assert result.exit_code == 1
    assert "not both" in result.output


def test_a_lan_address_is_refused(served, monkeypatch):
    """Campus wifi: the dorm could read every grade, with no login in the way."""
    monkeypatch.setattr(tailnet, "find_addresses", lambda *a, **k: ["100.101.102.103"])

    result = runner.invoke(cli_app, ["serve", "--host", "192.168.1.5", "--no-browser"])

    assert result.exit_code == 1
    assert "Refusing to listen on '192.168.1.5'" in result.output
    assert "--tailscale" in result.output


def test_a_name_is_refused(served, monkeypatch):
    """Whoever controls the name could point it here; only literals pass."""
    monkeypatch.setattr(tailnet, "find_addresses", lambda *a, **k: ["100.101.102.103"])

    result = runner.invoke(cli_app, ["serve", "--host", "mypc.local", "--no-browser"])

    assert result.exit_code == 1
    assert "Refusing to listen on 'mypc.local'" in result.output


def test_an_unlisted_tailnet_range_address_is_refused(served, monkeypatch):
    """A phone hotspot hands these out on the wifi interface, not the tailnet."""
    monkeypatch.setattr(tailnet, "find_addresses", lambda *a, **k: ["100.101.102.103"])

    result = runner.invoke(cli_app, ["serve", "--host", "100.64.9.9", "--no-browser"])

    assert result.exit_code == 1
    assert "Refusing to listen on '100.64.9.9'" in result.output


def test_a_tailscale_listed_address_is_allowed(served, monkeypatch):
    fake, captured = served
    monkeypatch.setattr(
        tailnet,
        "find_addresses",
        lambda *a, **k: ["100.101.102.103", "fd7a:115c:a1e0::1"],
    )

    result = runner.invoke(
        cli_app, ["serve", "--host", "fd7a:115c:a1e0::1", "--no-browser"]
    )

    assert result.exit_code == 0
    assert fake.kwargs["host"] == "fd7a:115c:a1e0::1"
    assert captured["extra_hosts"] == ("fd7a:115c:a1e0::1",)
    # Bracketed in the URL, or it is not an address anyone can type.
    assert "http://[fd7a:115c:a1e0::1]:5000/" in result.output
    assert "no login" in result.output


def test_another_loopback_address_needs_no_daemon(served, monkeypatch):
    """127.0.0.2 is still this machine, so it passes with Tailscale absent."""
    fake, captured = served
    monkeypatch.setattr(tailnet, "find_addresses", lambda *a, **k: [])

    result = runner.invoke(cli_app, ["serve", "--host", "127.0.0.2", "--no-browser"])

    assert result.exit_code == 0
    assert fake.kwargs["host"] == "127.0.0.2"
    # Loopback is already allowed by the guard, so nothing is added to it.
    assert captured["extra_hosts"] == ()
    assert "no login" not in result.output


def test_the_default_is_still_loopback_and_says_nothing_about_phones(served):
    fake, captured = served

    result = runner.invoke(cli_app, ["serve", "--no-browser"])

    assert result.exit_code == 0
    assert fake.kwargs["host"] == "127.0.0.1"
    assert captured["extra_hosts"] == ()
    assert "no login" not in result.output


def test_the_browser_opens_the_address_the_phone_would_use(monkeypatch):
    opened = []
    monkeypatch.setattr("gradetracker.web.app.create_app", lambda **k: FakeFlask())
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    monkeypatch.setattr(tailnet, "find_address", lambda *a, **k: "100.101.102.103")
    # The timer fires a second after the server starts; run it now instead.
    monkeypatch.setattr(
        "threading.Timer", lambda delay, fn: type("T", (), {"start": lambda s: fn()})()
    )

    runner.invoke(cli_app, ["serve", "--tailscale"])

    assert opened == ["http://100.101.102.103:5000/"]
