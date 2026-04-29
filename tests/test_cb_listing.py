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
    parse_affiliate_onlinerooms,
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


def test_clean_subject_strips_anchor_tags_and_hashtags() -> None:
    """Anchors get unwrapped, hashtags get stripped (they appear separately
    in the green tag-line below).
    """
    raw = 'goal: cum #threesum <a href="/tag/trans/">#trans</a> #natural'
    assert clean_subject(raw) == "goal: cum"


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


def test_format_seconds_online_under_one_minute_returns_empty() -> None:
    """v0.7.16: tiny windows aren't worth a line; the model is hardly
    online yet. Skip rather than show '0m'.
    """
    from resources.lib.cb_listing import _format_seconds_online
    assert _format_seconds_online(0) == ""
    assert _format_seconds_online(45) == ""


def test_format_seconds_online_minutes_only() -> None:
    """Less than an hour -> 'Xm'."""
    from resources.lib.cb_listing import _format_seconds_online
    assert _format_seconds_online(60) == "1m"
    assert _format_seconds_online(150) == "2m"
    assert _format_seconds_online(59 * 60) == "59m"


def test_format_seconds_online_hours_and_minutes() -> None:
    """Less than a day -> 'Xh Ym'."""
    from resources.lib.cb_listing import _format_seconds_online
    assert _format_seconds_online(3600) == "1h 0m"
    assert _format_seconds_online(6697) == "1h 51m"
    assert _format_seconds_online(23 * 3600 + 59 * 60) == "23h 59m"


def test_format_seconds_online_days_and_hours() -> None:
    """Day or more -> 'Xd Yh'. Minutes dropped at this scale; users
    care about days vs hours, not minutes after a full day."""
    from resources.lib.cb_listing import _format_seconds_online
    assert _format_seconds_online(86400) == "1d 0h"
    assert _format_seconds_online(86400 + 3 * 3600) == "1d 3h"
    assert _format_seconds_online(3 * 86400 + 14 * 3600 + 59 * 60) == "3d 14h"


def test_plot_for_includes_online_duration_when_seconds_online_present() -> None:
    """v0.7.16: when the affiliate API returns seconds_online, the
    plot includes an 'Online:' line so users can see how long the
    model has been broadcasting before they pick.
    """
    plot = plot_for({"username": "alice", "num_users": 100, "num_followers": 1000,
                     "seconds_online": 6697})
    assert "Online:" in plot
    assert "1h 51m" in plot


def test_plot_for_omits_online_when_seconds_online_missing() -> None:
    """If the API doesn't carry seconds_online (e.g. older bulk fetch
    fallback path), don't add the line."""
    plot = plot_for({"username": "alice", "num_users": 100, "num_followers": 1000})
    assert "Online:" not in plot


def test_plot_for_omits_online_when_seconds_online_under_a_minute() -> None:
    """Match _format_seconds_online's empty-string contract: tiny
    windows don't get a line."""
    plot = plot_for({"username": "alice", "num_users": 100, "num_followers": 1000,
                     "seconds_online": 30})
    assert "Online:" not in plot


def test_plot_for_includes_online_when_only_start_timestamp_present() -> None:
    """v0.7.17: the per-gender ``/api/ts/roomlist/`` endpoint (Top Cams,
    Female, Male, Couple, Trans) returns start_timestamp (Unix epoch
    of broadcast start) instead of seconds_online. plot_for must
    derive duration from start_timestamp when seconds_online is
    absent so all browse views show the Online line.
    """
    plot = plot_for(
        {"username": "alice", "num_users": 100, "num_followers": 1000,
         "start_timestamp": 1_000_000},
        now=1_000_000 + 6697,
    )
    assert "Online:" in plot
    assert "1h 51m" in plot


def test_plot_for_prefers_seconds_online_over_start_timestamp() -> None:
    """When both are present (defensive: we don't expect this in
    practice but the affiliate endpoint might add start_timestamp
    later), prefer seconds_online since it's already computed by the
    server and avoids local-clock skew.
    """
    plot = plot_for(
        {"username": "alice", "num_users": 100, "num_followers": 1000,
         "seconds_online": 6697,           # 1h 51m
         "start_timestamp": 1_000_000},    # would compute as 9999h
        now=1_000_000 + 9_999 * 3600,
    )
    assert "1h 51m" in plot


def test_plot_for_omits_online_when_neither_field_present() -> None:
    """No seconds_online and no start_timestamp -> no Online line."""
    plot = plot_for({"username": "alice", "num_users": 100, "num_followers": 1000})
    assert "Online:" not in plot


def test_plot_for_omits_online_when_start_timestamp_in_future() -> None:
    """Defensive: start_timestamp ahead of now (clock skew, bad data)
    should not produce a negative duration."""
    plot = plot_for(
        {"username": "alice", "num_users": 100, "num_followers": 1000,
         "start_timestamp": 1_000_000 + 100},
        now=1_000_000,
    )
    assert "Online:" not in plot


def test_plot_for_renders_tags_in_green() -> None:
    plot = plot_for({"username": "alice", "tags": ["blonde", "teen"],
                     "num_users": 1, "num_followers": 0})
    assert "#blonde" in plot
    assert "#teen" in plot
    # Must be 8-char hex with alpha; bare 6-char gets rendered blank by Kodi.
    assert "FF00ff88" in plot


def test_plot_for_color_tags_have_alpha_prefix() -> None:
    """Regression guard: every [COLOR <hex>] in the plot must be 8-char hex."""
    import re
    plot = plot_for({"username": "alice", "display_age": 22,
                     "location": "Berlin", "tags": ["blonde"],
                     "num_users": 1, "num_followers": 2})
    for hex_str in re.findall(r"\[COLOR ([0-9A-Fa-f]+)\]", plot):
        assert len(hex_str) == 8, (
            f"Color {hex_str!r} in plot is {len(hex_str)} chars; need 8 (AARRGGBB)"
        )


def test_plot_for_includes_viewers_and_followers() -> None:
    plot = plot_for({"username": "alice", "num_users": 1234, "num_followers": 5678})
    assert "Watching:" in plot
    assert "1234" in plot
    assert "Followers:" in plot
    assert "5678" in plot


def test_plot_for_subject_html_anchors_get_stripped() -> None:
    plot = plot_for({"username": "alice",
                     "subject": 'cum show <a href="/tag/y/">#y</a>',
                     "num_users": 1, "num_followers": 0})
    assert "<a" not in plot


def test_plot_for_subject_strips_hashtags_to_avoid_duplicate_with_tag_line() -> None:
    """The room subject often inlines hashtags (e.g. 'cum show #blonde #natural').
    We render the same tags separately as a green tag-line at the bottom of
    the plot. Strip #word tokens from the subject so the user does not see
    them twice.
    """
    plot = plot_for({"username": "alice",
                     "subject": "cum show #blonde #natural #petite",
                     "tags": ["blonde", "natural", "petite"],
                     "num_users": 1, "num_followers": 0})
    pre_tags, _, post_tags = plot.partition("[COLOR FF00ff88]")
    assert "#blonde" not in pre_tags
    assert "#natural" not in pre_tags
    assert "#petite" not in pre_tags
    assert "cum show" in pre_tags
    assert "#blonde" in post_tags
    assert "#natural" in post_tags


def test_plot_for_subject_strips_hashtags_even_when_tags_empty() -> None:
    """Hashtags in the subject duplicate the tags[] field anyway; strip
    regardless to keep rendering consistent.
    """
    plot = plot_for({"username": "alice",
                     "subject": "cum show #blonde",
                     "num_users": 1, "num_followers": 0})
    assert "#blonde" not in plot
    assert "cum show" in plot


# --------------------------------------------------------------------------- #
# v0.7.31: parse_affiliate_onlinerooms must honor current_show. The
# affiliate endpoint returns rooms in EVERY broadcasting state -- public,
# hidden, private, away, password_protected. Pre-0.7.31 we tagged them
# all is_live=True, which sent the TV loop chasing slugs that resolve
# offline forever (silent-stub-loop trigger).
# --------------------------------------------------------------------------- #


def test_affiliate_parser_marks_public_room_live() -> None:
    """The default broadcasting state. is_live=True so it gets queued
    by the TV loop."""
    rooms = [
        {"username": "alice", "current_show": "public", "num_users": 50,
         "gender": "f", "image_url": "https://x/alice.jpg",
         "room_subject": "hi"},
    ]
    models = parse_affiliate_onlinerooms(rooms)
    assert len(models) == 1
    assert models[0].slug == "alice"
    assert models[0].is_live is True


def test_affiliate_parser_marks_hidden_room_NOT_live() -> None:
    """model_a's actual state on : hidden show (paid). The
    affiliate feed includes her, but the AJAX endpoint returns
    room_status='hidden' with no HLS, so the TV loop can't actually
    play her stream. is_live must be False so she stays out of the
    bulk-live cache and out of TV-pickable slugs."""
    rooms = [
        {"username": "model_a", "current_show": "hidden",
         "num_users": 546, "gender": "f",
         "image_url": "https://x/ms.jpg",
         "room_subject": "550 tkns full show"},
    ]
    models = parse_affiliate_onlinerooms(rooms)
    assert len(models) == 1
    assert models[0].slug == "model_a"
    assert models[0].is_live is False


def test_affiliate_parser_other_non_public_states_NOT_live() -> None:
    """Same treatment for private 1-on-1, away, password_protected."""
    rooms = [
        {"username": "a", "current_show": "private", "num_users": 1,
         "gender": "f", "image_url": "", "room_subject": ""},
        {"username": "b", "current_show": "away", "num_users": 5,
         "gender": "f", "image_url": "", "room_subject": ""},
        {"username": "c", "current_show": "password protected",
         "num_users": 0, "gender": "f", "image_url": "",
         "room_subject": ""},
    ]
    models = parse_affiliate_onlinerooms(rooms)
    assert {m.slug: m.is_live for m in models} == {
        "a": False, "b": False, "c": False,
    }


def test_affiliate_parser_records_status_field() -> None:
    """v0.7.32: Model.status carries the lowercase current_show so
    views can render non-public broadcasters with state prefixes
    rather than silently dropping them."""
    rooms = [
        {"username": "alice", "current_show": "public", "num_users": 1,
         "gender": "f", "image_url": "", "room_subject": ""},
        {"username": "ms", "current_show": "hidden", "num_users": 1,
         "gender": "f", "image_url": "", "room_subject": ""},
        {"username": "afk", "current_show": "away", "num_users": 1,
         "gender": "f", "image_url": "", "room_subject": ""},
    ]
    models = parse_affiliate_onlinerooms(rooms)
    assert {m.slug: m.status for m in models} == {
        "alice": "public",
        "ms": "hidden",
        "afk": "away",
    }


def test_room_list_parser_records_status_field() -> None:
    """Same status round-trip in the room-list parser used by browse
    views (Top / New / gender / Search). v0.7.35 dropped the wrong-
    type ``label`` fallback so only ``current_show`` decides liveness;
    asymmetry with the affiliate parser is gone."""
    payload = json.dumps({
        "rooms": [
            {"username": "alice", "current_show": "public",
             "num_users": 1, "gender": "f"},
            {"username": "ms", "current_show": "hidden",
             "num_users": 1, "gender": "f"},
        ],
        "total_count": 2, "all_rooms_count": 2,
    })
    page = parse_roomlist(payload)
    statuses = {m.slug: (m.status, m.is_live) for m in page.models}
    assert statuses == {
        "alice": ("public", True),
        "ms": ("hidden", False),
    }


def test_room_list_parser_drops_label_fallback_for_liveness() -> None:
    """v0.7.35 audit-fix: ``label`` is a UI badge ("HD", "New", "Hot"),
    NOT a state enum. Pre-fix, _model_from_room fell back to
    ``label == "public"`` if current_show was missing -- which would
    misclassify any future room whose label happened to read "public"
    (and was asymmetric vs the affiliate parser). Now: missing
    current_show -> not live, regardless of label."""
    payload = json.dumps({
        "rooms": [
            {"username": "x", "label": "public", "num_users": 1,
             "gender": "f"},
        ],
        "total_count": 1, "all_rooms_count": 1,
    })
    page = parse_roomlist(payload)
    assert len(page.models) == 1
    assert page.models[0].is_live is False, (
        "label='public' must NOT promote a missing-current_show room "
        "to is_live=True"
    )


def test_affiliate_parser_missing_current_show_NOT_live() -> None:
    """Defensive: if CB ever drops the current_show field on a room
    we'd rather treat it as not-playable than over-promote and trigger
    another silent-stub loop. Empty / missing -> is_live=False."""
    rooms = [
        {"username": "x", "num_users": 1, "gender": "f",
         "image_url": "", "room_subject": "no current_show key"},
        {"username": "y", "current_show": "", "num_users": 1,
         "gender": "f", "image_url": "", "room_subject": ""},
    ]
    models = parse_affiliate_onlinerooms(rooms)
    assert {m.slug: m.is_live for m in models} == {
        "x": False, "y": False,
    }
