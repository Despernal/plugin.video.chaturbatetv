"""Tests for resources.lib.cb_listing - JSON room-list parser.

Pure module: takes a parsed JSON dict (or a JSON string/bytes) from
Chaturbate's ``/api/ts/roomlist/room-list/`` endpoint and returns a
``RoomListPage`` carrying domain ``Model`` objects plus pagination
totals. No network, no Kodi imports.
"""
from __future__ import annotations

import json
from pathlib import Path

from resources.lib.cb_listing import (
    RoomListPage,
    clean_subject,
    parse_roomlist,
    plot_for,
)
from resources.lib.cb_models import Gender, Model

FIXTURES = Path(__file__).parent / "fixtures"


def _load_sample() -> dict:
    return json.loads((FIXTURES / "sample_roomlist.json").read_text())


# parse_roomlist ------------------------------------------------------------ #


def test_parse_roomlist_returns_RoomListPage() -> None:
    page = parse_roomlist(_load_sample())
    assert isinstance(page, RoomListPage)


def test_parse_roomlist_models_are_Model() -> None:
    page = parse_roomlist(_load_sample())
    assert all(isinstance(m, Model) for m in page.models)


def test_parse_roomlist_count_matches_fixture() -> None:
    sample = _load_sample()
    page = parse_roomlist(sample)
    assert len(page.models) == len(sample["rooms"])


def test_parse_roomlist_extracts_slugs() -> None:
    page = parse_roomlist(_load_sample())
    slugs = {m.slug for m in page.models}
    assert "sample_room_1" in slugs


def test_parse_roomlist_builds_room_url_for_each_model() -> None:
    page = parse_roomlist(_load_sample())
    for m in page.models:
        assert m.url == f"https://chaturbate.com/{m.slug}/"


def test_parse_roomlist_extracts_viewer_count_as_int() -> None:
    page = parse_roomlist(_load_sample())
    by_slug = {m.slug: m for m in page.models}
    sample = _load_sample()
    expected = {r["username"]: r["num_users"] for r in sample["rooms"]}
    for slug, n in expected.items():
        assert by_slug[slug].viewers == n


def test_parse_roomlist_maps_gender_codes() -> None:
    page = parse_roomlist(_load_sample())
    by_slug = {m.slug: m for m in page.models}
    sample_genders = {r["username"]: r["gender"] for r in _load_sample()["rooms"]}
    code_to_enum = {"f": Gender.FEMALE, "m": Gender.MALE,
                    "c": Gender.COUPLE, "s": Gender.TRANS}
    for slug, code in sample_genders.items():
        assert by_slug[slug].gender is code_to_enum.get(code, Gender.UNKNOWN)


def test_parse_roomlist_is_live_when_label_public() -> None:
    """The fixture has all rooms with current_show=public."""
    page = parse_roomlist(_load_sample())
    assert all(m.is_live for m in page.models)


def test_parse_roomlist_total_count() -> None:
    sample = _load_sample()
    page = parse_roomlist(sample)
    assert page.total_count == sample["total_count"]


def test_parse_roomlist_all_rooms_count() -> None:
    sample = _load_sample()
    page = parse_roomlist(sample)
    assert page.all_rooms_count == sample["all_rooms_count"]


def test_parse_roomlist_accepts_json_string() -> None:
    raw = json.dumps(_load_sample())
    page = parse_roomlist(raw)
    assert len(page.models) > 0


def test_parse_roomlist_accepts_json_bytes() -> None:
    raw = json.dumps(_load_sample()).encode("utf-8")
    page = parse_roomlist(raw)
    assert len(page.models) > 0


def test_parse_roomlist_empty_payload() -> None:
    page = parse_roomlist({"rooms": [], "total_count": 0, "all_rooms_count": 0})
    assert page.models == []
    assert page.total_count == 0
    assert page.all_rooms_count == 0


def test_parse_roomlist_missing_rooms_key() -> None:
    page = parse_roomlist({"total_count": 5})
    assert page.models == []


def test_parse_roomlist_garbage_string() -> None:
    page = parse_roomlist("not even close to json")
    assert page.models == []
    assert page.total_count == 0


def test_parse_roomlist_garbage_dict() -> None:
    page = parse_roomlist({"rooms": "not a list"})
    assert page.models == []


def test_parse_roomlist_skips_room_without_username() -> None:
    page = parse_roomlist({"rooms": [{"username": ""}, {"gender": "f"}]})
    assert page.models == []


def test_parse_roomlist_skips_non_dict_room_entries() -> None:
    page = parse_roomlist({"rooms": [{"username": "alice"}, 42, "bogus", None]})
    assert len(page.models) == 1
    assert page.models[0].slug == "alice"


def test_parse_roomlist_unknown_gender_falls_back() -> None:
    page = parse_roomlist({"rooms": [{"username": "alice", "gender": "?"}]})
    assert page.models[0].gender is Gender.UNKNOWN


def test_parse_roomlist_int_coercion_for_string_viewers() -> None:
    """Defensive: if num_users ever comes back as a string, we should still get an int."""
    page = parse_roomlist({"rooms": [{"username": "alice", "num_users": "1234"}]})
    assert page.models[0].viewers == 1234


def test_parse_roomlist_garbage_viewer_count_zeroed() -> None:
    page = parse_roomlist({"rooms": [{"username": "alice", "num_users": "abc"}]})
    assert page.models[0].viewers == 0


# clean_subject ------------------------------------------------------------- #


def test_clean_subject_strips_anchor_tags_keeps_inner_text() -> None:
    raw = 'goal: cum #threesum <a href="/tag/trans/">#trans</a> #natural'
    assert clean_subject(raw) == "goal: cum #threesum #trans #natural"


def test_clean_subject_handles_none() -> None:
    assert clean_subject(None) == ""


def test_clean_subject_handles_empty() -> None:
    assert clean_subject("") == ""


def test_clean_subject_no_anchors_passes_through() -> None:
    assert clean_subject("plain text subject") == "plain text subject"


# plot_for ------------------------------------------------------------------ #


def test_plot_for_includes_age() -> None:
    plot = plot_for({"username": "alice", "display_age": 22, "num_users": 10, "num_followers": 100})
    assert "Age:" in plot
    assert "22" in plot


def test_plot_for_unknown_age_when_missing() -> None:
    plot = plot_for({"username": "alice", "num_users": 10, "num_followers": 0})
    assert "Unknown" in plot


def test_plot_for_includes_location_when_present() -> None:
    plot = plot_for({"username": "alice", "location": "Berlin",
                     "num_users": 1, "num_followers": 0})
    assert "Location:" in plot
    assert "Berlin" in plot


def test_plot_for_omits_location_line_when_blank() -> None:
    plot = plot_for({"username": "alice", "location": "",
                     "num_users": 1, "num_followers": 0})
    assert "Location:" not in plot


def test_plot_for_renders_tags_in_green() -> None:
    plot = plot_for({"username": "alice", "tags": ["blonde", "teen"],
                     "num_users": 1, "num_followers": 0})
    assert "#blonde" in plot
    assert "#teen" in plot
    assert "00ff88" in plot


def test_plot_for_includes_viewers_and_followers() -> None:
    plot = plot_for({"username": "alice", "num_users": 1234, "num_followers": 5678})
    assert "Watching:" in plot
    assert "1234" in plot
    assert "Followers:" in plot
    assert "5678" in plot


def test_plot_for_subject_html_anchors_get_stripped() -> None:
    plot = plot_for({"username": "alice",
                     "subject": 'cum #x <a href="/tag/y/">#y</a>',
                     "num_users": 1, "num_followers": 0})
    assert "<a" not in plot
    assert "#y" in plot
