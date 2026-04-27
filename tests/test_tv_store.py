"""Tests for resources.lib.tv_store.

Adapted from the  snippet's _cb_tv_load / _cb_tv_save tests, but
typed against our TVEntry dataclass. Same edge cases (missing file,
malformed JSON, missing 'models' key, atomic-write parent-dir creation).
"""
from __future__ import annotations

import json


from resources.lib.cb_models import TVEntry
from resources.lib.tv_store import load, save


def test_load_missing_file_returns_empty(tmp_path) -> None:
    p = tmp_path / "tv.json"
    assert not p.exists()
    assert load(p) == []


def test_load_returns_entries(tmp_path) -> None:
    p = tmp_path / "tv.json"
    p.write_text(json.dumps({"models": [
        {"url": "https://chaturbate.com/alice/", "name": "alice", "priority": 5},
        {"url": "https://chaturbate.com/bob/", "name": "bob", "priority": 10},
    ]}))
    entries = load(p)
    assert len(entries) == 2
    assert entries[0] == TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=5)
    assert entries[1] == TVEntry(name="bob", url="https://chaturbate.com/bob/", priority=10)


def test_load_malformed_json_returns_empty(tmp_path) -> None:
    p = tmp_path / "tv.json"
    p.write_text("{not valid json")
    assert load(p) == []


def test_load_missing_models_key_returns_empty(tmp_path) -> None:
    p = tmp_path / "tv.json"
    p.write_text(json.dumps({"other": [1, 2, 3]}))
    assert load(p) == []


def test_load_models_value_not_a_list_returns_empty(tmp_path) -> None:
    p = tmp_path / "tv.json"
    p.write_text(json.dumps({"models": "oops"}))
    assert load(p) == []


def test_load_skips_unparsable_entries(tmp_path) -> None:
    """A row missing required fields is dropped, not crashing the whole load."""
    p = tmp_path / "tv.json"
    p.write_text(json.dumps({"models": [
        {"url": "u1", "name": "good", "priority": 5},
        {"name": "no_url"},  # missing url -> drop
        "not a dict",  # garbage -> drop
        {"url": "u2", "name": "good2", "priority": 7},
    ]}))
    entries = load(p)
    assert [e.name for e in entries] == ["good", "good2"]


def test_load_defaults_priority_to_1_when_missing(tmp_path) -> None:
    p = tmp_path / "tv.json"
    p.write_text(json.dumps({"models": [
        {"url": "u", "name": "n"},
    ]}))
    entries = load(p)
    assert entries[0].priority == 1


def test_load_coerces_string_priority(tmp_path) -> None:
    """tv.json was historically written with int priorities, but be tolerant
    of a hand-edited file with string numerics."""
    p = tmp_path / "tv.json"
    p.write_text(json.dumps({"models": [
        {"url": "u", "name": "n", "priority": "12"},
    ]}))
    entries = load(p)
    assert entries[0].priority == 12


def test_load_drops_entry_with_uncoercible_priority(tmp_path) -> None:
    p = tmp_path / "tv.json"
    p.write_text(json.dumps({"models": [
        {"url": "u", "name": "n", "priority": "not-a-number"},
        {"url": "u2", "name": "n2", "priority": 5},
    ]}))
    entries = load(p)
    assert [e.name for e in entries] == ["n2"]


def test_save_writes_json(tmp_path) -> None:
    p = tmp_path / "tv.json"
    ok = save(p, [TVEntry(name="alice", url="u1", priority=3)])
    assert ok is True
    assert p.exists()
    data = json.loads(p.read_text())
    assert data == {"models": [{"url": "u1", "name": "alice", "priority": 3}]}


def test_save_creates_parent_dir(tmp_path) -> None:
    p = tmp_path / "newdir" / "deep" / "tv.json"
    ok = save(p, [TVEntry(name="n", url="u", priority=1)])
    assert ok is True
    assert p.exists()


def test_save_round_trip(tmp_path) -> None:
    p = tmp_path / "tv.json"
    entries = [
        TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=5),
        TVEntry(name="bob", url="https://chaturbate.com/bob/", priority=10),
    ]
    assert save(p, entries) is True
    loaded = load(p)
    assert loaded == entries


def test_save_is_atomic_via_replace(tmp_path) -> None:
    """The temp file should not linger after a successful save."""
    p = tmp_path / "tv.json"
    save(p, [TVEntry(name="n", url="u", priority=1)])
    siblings = [c.name for c in tmp_path.iterdir()]
    assert "tv.json" in siblings
    # No leftover .tmp / .swap files
    assert not any(s.endswith(".tmp") or s.endswith(".swap") for s in siblings)


def test_save_overwrites_existing(tmp_path) -> None:
    p = tmp_path / "tv.json"
    save(p, [TVEntry(name="first", url="u1", priority=1)])
    save(p, [TVEntry(name="second", url="u2", priority=2)])
    loaded = load(p)
    assert len(loaded) == 1
    assert loaded[0].name == "second"


def test_save_empty_list_writes_empty_models(tmp_path) -> None:
    p = tmp_path / "tv.json"
    assert save(p, []) is True
    data = json.loads(p.read_text())
    assert data == {"models": []}


def test_save_returns_false_on_unwritable_path(tmp_path) -> None:
    """Trying to save into a path whose parent is a regular file fails gracefully."""
    block = tmp_path / "blocker"
    block.write_text("file in the way")
    p = block / "child" / "tv.json"
    assert save(p, [TVEntry(name="n", url="u", priority=1)]) is False


def test_load_tolerates_concurrent_partial_write(tmp_path) -> None:
    """Mid-write the file may be empty or truncated; load returns []."""
    p = tmp_path / "tv.json"
    p.write_text("")  # zero bytes mid-write
    assert load(p) == []
