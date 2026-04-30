"""Tests for resources.lib.favs_store - local favorites with atomic save."""
from __future__ import annotations

import json

from resources.lib.cb_models import Favorite, Gender
from resources.lib.favs_store import add, load, remove, save


def _fav(slug: str, gender: Gender = Gender.FEMALE) -> Favorite:
    return Favorite(name=slug, slug=slug, url=f"https://chaturbate.com/{slug}/", gender=gender)


# load ------------------------------------------------------------------------

def test_load_missing_file_returns_empty(tmp_path) -> None:
    p = tmp_path / "favs.json"
    assert load(p) == []


def test_load_empty_file_returns_empty(tmp_path) -> None:
    p = tmp_path / "favs.json"
    p.write_text("")
    assert load(p) == []


def test_load_returns_favorites(tmp_path) -> None:
    p = tmp_path / "favs.json"
    p.write_text(json.dumps({"favorites": [
        {"slug": "alice", "name": "alice", "url": "https://chaturbate.com/alice/", "gender": "female"},
        {"slug": "bob", "name": "bob", "url": "https://chaturbate.com/bob/", "gender": "male"},
    ]}))
    favs = load(p)
    assert len(favs) == 2
    assert favs[0].slug == "alice"
    assert favs[0].gender is Gender.FEMALE
    assert favs[1].gender is Gender.MALE


def test_load_malformed_json_returns_empty(tmp_path) -> None:
    p = tmp_path / "favs.json"
    p.write_text("{not valid")
    assert load(p) == []


def test_load_missing_favorites_key_returns_empty(tmp_path) -> None:
    p = tmp_path / "favs.json"
    p.write_text(json.dumps({"models": []}))
    assert load(p) == []


def test_load_skips_unparseable_rows(tmp_path) -> None:
    p = tmp_path / "favs.json"
    p.write_text(json.dumps({"favorites": [
        {"slug": "ok", "name": "ok", "url": "u", "gender": "female"},
        "garbage",
        {"name": "no slug"},
        {"slug": "ok2", "name": "ok2", "url": "u2", "gender": "couple"},
    ]}))
    favs = load(p)
    assert [f.slug for f in favs] == ["ok", "ok2"]


def test_load_unknown_gender_falls_back(tmp_path) -> None:
    p = tmp_path / "favs.json"
    p.write_text(json.dumps({"favorites": [
        {"slug": "a", "name": "a", "url": "u", "gender": "alien"},
    ]}))
    favs = load(p)
    assert favs[0].gender is Gender.UNKNOWN


# save ------------------------------------------------------------------------

def test_save_writes_json(tmp_path) -> None:
    p = tmp_path / "favs.json"
    assert save(p, [_fav("alice")]) is True
    data = json.loads(p.read_text())
    assert data["favorites"][0]["slug"] == "alice"
    assert data["favorites"][0]["gender"] == "female"


def test_save_creates_parent_dir(tmp_path) -> None:
    p = tmp_path / "deep" / "nested" / "favs.json"
    assert save(p, [_fav("a")]) is True
    assert p.exists()


def test_save_round_trip(tmp_path) -> None:
    p = tmp_path / "favs.json"
    favs_in = [_fav("alice", Gender.FEMALE), _fav("bob", Gender.MALE)]
    save(p, favs_in)
    favs_out = load(p)
    assert favs_out == favs_in


def test_save_empty_list(tmp_path) -> None:
    p = tmp_path / "favs.json"
    assert save(p, []) is True
    assert load(p) == []


def test_save_returns_false_on_unwritable(tmp_path) -> None:
    block = tmp_path / "blocker"
    block.write_text("file in the way")
    p = block / "child" / "favs.json"
    assert save(p, [_fav("a")]) is False


# add -------------------------------------------------------------------------

def test_add_appends_to_empty() -> None:
    fav = _fav("alice")
    assert add([], fav) == [fav]


def test_add_dup_by_slug_is_noop() -> None:
    a = _fav("alice")
    a2 = Favorite(name="ALICE", slug="alice", url="other", gender=Gender.FEMALE)
    out = add([a], a2)
    assert out == [a]  # original kept, no append


def test_add_returns_new_list_does_not_mutate() -> None:
    favs: list[Favorite] = []
    fav = _fav("alice")
    out = add(favs, fav)
    assert favs == []
    assert out == [fav]


def test_add_preserves_order() -> None:
    a = _fav("alice")
    b = _fav("bob")
    c = _fav("carol")
    out = add(add([a], b), c)
    assert [f.slug for f in out] == ["alice", "bob", "carol"]


# remove ----------------------------------------------------------------------

def test_remove_filters_out_slug() -> None:
    a = _fav("alice")
    b = _fav("bob")
    out = remove([a, b], "alice")
    assert out == [b]


def test_remove_missing_is_noop() -> None:
    a = _fav("alice")
    out = remove([a], "ghost")
    assert out == [a]


def test_remove_empty_list_is_empty() -> None:
    assert remove([], "anyone") == []


def test_remove_does_not_mutate_input() -> None:
    a = _fav("alice")
    b = _fav("bob")
    favs = [a, b]
    out = remove(favs, "alice")
    assert favs == [a, b]  # input unchanged
    assert out == [b]


def test_remove_only_first_match_when_dups_present() -> None:
    """Defensive: if the file got two entries with same slug somehow (race),
    remove takes them all out."""
    a1 = Favorite(name="alice", slug="alice", url="u1", gender=Gender.FEMALE)
    a2 = Favorite(name="alice", slug="alice", url="u2", gender=Gender.FEMALE)
    b = _fav("bob")
    out = remove([a1, a2, b], "alice")
    assert out == [b]


def test_load_sweeps_orphan_tempfiles_older_than_1h(tmp_path) -> None:
    """v0.7.38 (audit pass #4 HIGH #10): Kodi-SIGKILL between mkstemp
    and os.replace orphans .favs-*.json tempfiles. Pass #1 agent 3
    flagged this; the load-time sweep is the fix. Bounded by max_age
    so concurrent in-flight saves aren't disturbed."""
    import os as _os
    import time as _time

    p = tmp_path / "favs.json"
    p.write_text('{"favorites": []}')

    old = tmp_path / ".favs-OLD12345.json"
    fresh = tmp_path / ".favs-FRESH567.json"
    old.write_text("orphan")
    fresh.write_text("inflight")
    twohr_ago = _time.time() - 7200
    _os.utime(old, (twohr_ago, twohr_ago))

    load(p)

    assert not old.exists(), "stale tempfile (>1h) should be swept"
    assert fresh.exists(), (
        "young tempfile (<1h) must be left alone (could be in-flight save)"
    )
