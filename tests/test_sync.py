"""Sync orchestration tests (M2).

Covers the failure messages a user actually sees, so an expired session never
surfaces as a traceback.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from gradetracker import config, sync
from gradetracker.auth.base import AuthExpired
from gradetracker.cli import app
from gradetracker.connectors.base import ConnectorError
from gradetracker.storage import raw

runner = CliRunner()

PAYLOAD = {
    "fetched_at": "2026-10-20T18:00:00+00:00",
    "source": "blackboard",
    "user": {"id": "_8675_1"},
    "courses": [
        {"courseId": "_2100_1", "columns": [1, 2, 3], "grades": [1, 2, 3]},
        {"courseId": "_2101_1", "columns": [1, 2], "grades": [1, 2]},
    ],
}


@pytest.fixture
def bb_configured(tmp_path, monkeypatch):
    """A configured Blackboard source writing dumps into tmp_path."""
    monkeypatch.setattr(config, "BB_BASE_URL", "https://bb.example.edu")
    monkeypatch.setattr(config, "BB_PROFILE_DIR", tmp_path / "profiles" / "blackboard")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # Without this a full sync would write to the real %LOCALAPPDATA% database.
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "grades.db")
    return tmp_path


def _fetch_result(monkeypatch, result):
    """Replace the connector's fetch with a stub returning or raising `result`."""
    from gradetracker.connectors.blackboard import BlackboardConnector

    def fake_fetch(self):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(BlackboardConnector, "fetch", fake_fetch)


# --- run_sync -------------------------------------------------------------


def test_unset_base_url_is_a_clear_error(monkeypatch, capsys):
    monkeypatch.setattr(config, "BB_BASE_URL", "")

    code = sync.run_sync(["blackboard"], raw_only=True)

    assert code == 1
    assert "BB_BASE_URL is not set" in capsys.readouterr().err


def test_successful_sync_dumps_raw_and_reports_counts(bb_configured, monkeypatch, capsys):
    from gradetracker.auth.cookie_auth import CookieAuth

    monkeypatch.setattr(CookieAuth, "persist", lambda self: True)
    _fetch_result(monkeypatch, PAYLOAD)

    code = sync.run_sync(["blackboard"], raw_only=True)

    assert code == 0
    out = capsys.readouterr().out
    assert "2 courses, 5 columns, 5 grades" in out

    dumps = raw.list_raw("blackboard", raw_dir=bb_configured / "raw")
    assert len(dumps) == 1
    assert raw.read_raw(dumps[0]) == PAYLOAD


def test_expired_session_reports_the_reconnect_instruction(bb_configured, monkeypatch, capsys):
    _fetch_result(monkeypatch, AuthExpired("no cookies"))

    code = sync.run_sync(["blackboard"], raw_only=True)

    assert code == 1
    assert "blackboard: session expired, run gt login blackboard" in capsys.readouterr().err


def test_connector_error_is_reported_without_a_dump(bb_configured, monkeypatch, capsys):
    """A changed endpoint leaves the last good data alone."""
    _fetch_result(monkeypatch, ConnectorError("Blackboard returned 500 for /columns"))

    code = sync.run_sync(["blackboard"], raw_only=True)

    assert code == 1
    assert "fetch failed" in capsys.readouterr().err
    assert raw.list_raw("blackboard", raw_dir=bb_configured / "raw") == []


def test_unknown_source_is_rejected(capsys):
    code = sync.run_sync(["canvas"], raw_only=True)

    assert code == 1
    assert "Unknown source" in capsys.readouterr().err


def test_gradescope_without_credentials_is_a_clean_error(bb_configured, capsys):
    code = sync.run_sync(["gradescope"], raw_only=True)

    assert code == 1
    assert (
        "gradescope: credentials missing or rejected, run gt login gradescope"
        in capsys.readouterr().err
    )


def test_sources_are_independent(bb_configured, monkeypatch, capsys):
    """An expired Blackboard session must not stop Gradescope from being tried."""
    _fetch_result(monkeypatch, AuthExpired("expired"))
    gs_calls = []
    monkeypatch.setattr(
        sync, "_sync_gradescope", lambda raw_only, min_interval_s=None: gs_calls.append(1) or 0
    )

    code = sync.run_sync(["blackboard", "gradescope"], raw_only=True)

    assert code == 1  # the worst code wins
    assert gs_calls == [1]  # gradescope still ran
    assert "blackboard: session expired" in capsys.readouterr().err


def test_normalization_only_runs_without_raw_only(bb_configured, monkeypatch, capsys):
    """--raw-only stops after the dump; a full sync goes on to the database."""
    from gradetracker.auth.cookie_auth import CookieAuth

    from tests.test_normalize import bb_payload

    monkeypatch.setattr(CookieAuth, "persist", lambda self: True)
    _fetch_result(monkeypatch, bb_payload())

    sync.run_sync(["blackboard"], raw_only=True)
    assert "normalized" not in capsys.readouterr().out

    sync.run_sync(["blackboard"], raw_only=False)
    assert "normalized 2 courses, 10 assignments" in capsys.readouterr().out


# --- CLI ------------------------------------------------------------------


def test_cli_sync_passes_the_source_and_flag(monkeypatch):
    seen = {}

    def fake_run_sync(sources, raw_only=False):
        seen["sources"] = sources
        seen["raw_only"] = raw_only
        return 0

    monkeypatch.setattr(sync, "run_sync", fake_run_sync)

    result = runner.invoke(app, ["sync", "--source", "blackboard", "--raw-only"])

    assert result.exit_code == 0
    assert seen == {"sources": ["blackboard"], "raw_only": True}


def test_cli_sync_defaults_to_both_sources(monkeypatch):
    seen = {}
    monkeypatch.setattr(sync, "run_sync", lambda sources, raw_only=False: seen.update(sources=sources) or 0)

    result = runner.invoke(app, ["sync"])

    assert result.exit_code == 0
    assert seen["sources"] == ["blackboard", "gradescope"]


def test_cli_sync_propagates_the_exit_code(monkeypatch):
    monkeypatch.setattr(sync, "run_sync", lambda sources, raw_only=False: 1)

    result = runner.invoke(app, ["sync", "--raw-only"])

    assert result.exit_code == 1


# --- cookie refresh and skipped courses -----------------------------------


def test_successful_sync_persists_the_refreshed_cookies(bb_configured, monkeypatch, capsys):
    """Blackboard slides the cookie expiry, so a sync must save the jar back."""
    from gradetracker.auth.cookie_auth import CookieAuth

    _fetch_result(monkeypatch, PAYLOAD)
    persisted = []
    monkeypatch.setattr(CookieAuth, "persist", lambda self: persisted.append(True) or True)

    assert sync.run_sync(["blackboard"], raw_only=True) == 0
    assert persisted == [True]


def test_cookies_are_not_persisted_after_a_failed_sync(bb_configured, monkeypatch):
    from gradetracker.auth.cookie_auth import CookieAuth

    _fetch_result(monkeypatch, AuthExpired("expired"))
    persisted = []
    monkeypatch.setattr(CookieAuth, "persist", lambda self: persisted.append(True) or True)

    assert sync.run_sync(["blackboard"], raw_only=True) == 1
    assert persisted == []


def test_skipped_courses_are_reported(bb_configured, monkeypatch, capsys):
    """A hidden gradebook is worth a line, but not a failed sync."""
    from gradetracker.auth.cookie_auth import CookieAuth

    monkeypatch.setattr(CookieAuth, "persist", lambda self: True)
    payload = dict(PAYLOAD)
    payload["skipped"] = [
        {"courseId": "_2102_1", "status": 403, "url": "/learn/api/.../columns"}
    ]
    _fetch_result(monkeypatch, payload)

    code = sync.run_sync(["blackboard"], raw_only=True)
    captured = capsys.readouterr()

    assert code == 0
    assert "skipped course _2102_1 (HTTP 403" in captured.err
    assert "2 courses, 5 columns, 5 grades" in captured.out


# --- gradescope sync ------------------------------------------------------


GS_PAYLOAD = {
    "fetched_at": "2026-10-20T18:00:00+00:00",
    "source": "gradescope",
    "term": "Fall 2026",
    "courses": [
        {"id": "812345", "name": "ECON 166", "assignments": [1, 2, 3, 4, 5]},
        {"id": "812346", "name": "CS 220", "assignments": [1, 2, 3]},
    ],
    "skipped": [],
}


def _gs_fetch_result(monkeypatch, result):
    from gradetracker.connectors.gradescope import GradescopeConnector

    def fake_fetch(self):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(GradescopeConnector, "fetch", fake_fetch)


def test_gradescope_sync_dumps_raw_and_reports_counts(bb_configured, monkeypatch, capsys):
    _gs_fetch_result(monkeypatch, GS_PAYLOAD)

    code = sync.run_sync(["gradescope"], raw_only=True)

    assert code == 0
    assert "gradescope: 2 courses, 8 assignments" in capsys.readouterr().out
    dumps = raw.list_raw("gradescope", raw_dir=bb_configured / "raw")
    assert len(dumps) == 1
    assert raw.read_raw(dumps[0])["term"] == "Fall 2026"


def test_gradescope_fetch_failure_is_reported(bb_configured, monkeypatch, capsys):
    _gs_fetch_result(monkeypatch, ConnectorError("course page moved"))

    code = sync.run_sync(["gradescope"], raw_only=True)

    assert code == 1
    assert "gradescope: fetch failed: course page moved" in capsys.readouterr().err


def test_gradescope_skipped_courses_are_reported(bb_configured, monkeypatch, capsys):
    payload = dict(GS_PAYLOAD)
    payload["skipped"] = [{"id": "812399", "name": "X", "error": "ValueError: table moved"}]
    _gs_fetch_result(monkeypatch, payload)

    code = sync.run_sync(["gradescope"], raw_only=True)

    assert code == 0
    assert "gradescope: skipped course 812399 (ValueError: table moved)" in capsys.readouterr().err


def test_failed_cookie_refresh_warns_without_failing_the_sync(bb_configured, monkeypatch, capsys):
    """Step 2b note 2: a failed persist is worth a warning, not an error."""
    from gradetracker.auth.cookie_auth import CookieAuth

    _fetch_result(monkeypatch, PAYLOAD)
    monkeypatch.setattr(CookieAuth, "persist", lambda self: False)

    code = sync.run_sync(["blackboard"], raw_only=True)
    captured = capsys.readouterr()

    assert code == 0
    assert "could not refresh the saved session; it may expire sooner" in captured.err
    assert "2 courses, 5 columns, 5 grades" in captured.out
