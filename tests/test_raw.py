"""Raw dump store tests (M2)."""

from __future__ import annotations

import json

from gradetracker.storage import raw


def test_write_raw_creates_a_json_dump(tmp_path):
    path = raw.write_raw("blackboard", {"courses": [1, 2]}, raw_dir=tmp_path)

    assert path.exists()
    assert path.parent == tmp_path / "blackboard"
    assert path.suffix == ".json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"courses": [1, 2]}


def test_write_raw_is_indented_for_reading_by_hand(tmp_path):
    path = raw.write_raw("blackboard", {"a": 1}, raw_dir=tmp_path)

    assert "\n" in path.read_text(encoding="utf-8")


def test_filenames_have_no_colons(tmp_path):
    """Windows rejects colons in filenames, so the ISO stamp is sanitized."""
    path = raw.write_raw("blackboard", {}, raw_dir=tmp_path)

    assert ":" not in path.name


def test_successive_dumps_do_not_collide(tmp_path):
    first = raw.write_raw("blackboard", {"n": 1}, raw_dir=tmp_path)
    second = raw.write_raw("blackboard", {"n": 2}, raw_dir=tmp_path)

    assert first != second
    assert len(raw.list_raw("blackboard", raw_dir=tmp_path)) == 2


def test_prune_keeps_only_the_newest(tmp_path):
    directory = tmp_path / "blackboard"
    directory.mkdir(parents=True)
    # Names sort chronologically, so stale ones are the lexicographically first.
    for i in range(35):
        (directory / f"2026-10-20T00-00-{i:02d}-000000Z.json").write_text("{}", encoding="utf-8")

    raw.write_raw("blackboard", {"newest": True}, raw_dir=tmp_path, keep=30)

    remaining = raw.list_raw("blackboard", raw_dir=tmp_path)
    assert len(remaining) == 30
    assert remaining[0].name == "2026-10-20T00-00-06-000000Z.json"
    assert json.loads(remaining[-1].read_text(encoding="utf-8")) == {"newest": True}


def test_prune_leaves_small_collections_alone(tmp_path):
    for _ in range(3):
        raw.write_raw("blackboard", {}, raw_dir=tmp_path)

    assert len(raw.list_raw("blackboard", raw_dir=tmp_path)) == 3


def test_keep_zero_disables_pruning(tmp_path):
    raw.write_raw("blackboard", {}, raw_dir=tmp_path, keep=0)
    raw.write_raw("blackboard", {}, raw_dir=tmp_path, keep=0)

    assert len(raw.list_raw("blackboard", raw_dir=tmp_path)) == 2


def test_sources_are_kept_apart(tmp_path):
    raw.write_raw("blackboard", {"src": "bb"}, raw_dir=tmp_path)
    raw.write_raw("gradescope", {"src": "gs"}, raw_dir=tmp_path)

    assert len(raw.list_raw("blackboard", raw_dir=tmp_path)) == 1
    assert len(raw.list_raw("gradescope", raw_dir=tmp_path)) == 1


def test_list_raw_is_empty_for_an_unused_source(tmp_path):
    assert raw.list_raw("gradescope", raw_dir=tmp_path) == []


def test_latest_raw_returns_the_newest_dump(tmp_path):
    raw.write_raw("blackboard", {"n": 1}, raw_dir=tmp_path)
    newest = raw.write_raw("blackboard", {"n": 2}, raw_dir=tmp_path)

    assert raw.latest_raw("blackboard", raw_dir=tmp_path) == newest


def test_latest_raw_is_none_without_dumps(tmp_path):
    assert raw.latest_raw("blackboard", raw_dir=tmp_path) is None


def test_read_raw_round_trips(tmp_path):
    payload = {"courses": [{"name": "ECON 166"}]}
    path = raw.write_raw("blackboard", payload, raw_dir=tmp_path)

    assert raw.read_raw(path) == payload
