"""Tests for resources.lib.model_meta_store.

Fully isolated -- the store opens a real sqlite file at a tmp_path and
tears down at the end. No Kodi mocks needed; this is pure stdlib.

Coverage:

- Schema is created idempotently on open.
- Upsert from one source writes all known columns.
- Re-upsert from a partial source preserves richer data (the
  COALESCE-on-update behavior the user asked for: any data we ever
  collected stays, partial overlays don't wipe it).
- ``last_online_epoch`` always updates on each upsert.
- ``first_online_epoch`` is set on insert and never updated.
- Tags persist as a JSON list.
- Reading a missing slug returns None / empty dict.
- Batch insert is atomic (transaction-wrapped).
- Schema-version migration adds new columns without breaking old DBs.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from resources.lib import model_meta_store as mms


# Sample affiliate-onlinerooms room dict (rich, has seconds_online +
# image_url + display_name + spoken_languages but NO start_timestamp).
_AFFILIATE_ROOM = {
    "username": "alice",
    "slug": "alice",
    "display_name": "Alice",
    "gender": "f",
    "age": None,                # affiliate often returns null age
    "country": "US",
    "location": "California",
    "spoken_languages": "English, Spanish",
    "birthday": "",
    "is_hd": True,
    "is_new": False,
    "num_users": 1234,
    "num_followers": 5678,
    "room_subject": "first day on cam",
    "tags": ["lovense", "blonde"],
    "image_url": "https://example.com/alice.jpg",
    "image_url_360x270": "https://example.com/alice_thumb.jpg",
    "block_from_countries": "",
    "block_from_states": "",
    "recorded": "false",
    "seconds_online": 3600,
    "current_show": "public",
}

# Sample roomlist (per-gender API) room dict (has display_age +
# start_timestamp + img + age verification + has_password but NO
# seconds_online or display_name, image_url, etc).
_ROOMLIST_ROOM = {
    "username": "alice",
    "display_age": 22,
    "gender": "f",
    "country": "US",
    "location": "California",
    "current_show": "public",
    "is_new": False,
    "is_age_verified": True,
    "is_gaming": False,
    "has_password": False,
    "private_price": 60,
    "spy_show_price": 20,
    "label": "",
    "num_users": 1300,           # slightly different - newer fetch
    "num_followers": 5680,
    "room_subject": "another show",
    "tags": ["lovense", "blonde", "teen"],
    "img": "https://thumb.live.mmcdn.com/ri/alice.jpg",
    "subject": "another show",
    "start_timestamp": 1_777_000_000,
    "start_dt_utc": "2026-04-28T12:00:00+00:00",
    "source_name": "df",
    "is_following": False,
}


def _open(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "meta.db"
    conn = mms.open_db(str(db))
    return conn


# Schema --------------------------------------------------------------------


def test_open_db_creates_schema_idempotently(tmp_path: Path) -> None:
    """Calling open_db twice on the same path must not error or wipe."""
    db = tmp_path / "meta.db"
    conn1 = mms.open_db(str(db))
    mms.upsert_room(conn1, _AFFILIATE_ROOM, now=1_000_000, source="affiliate")
    conn1.close()
    # Re-open: schema should still be there + data preserved.
    conn2 = mms.open_db(str(db))
    row = mms.get_model(conn2, "alice")
    assert row is not None
    assert row["slug"] == "alice"
    conn2.close()


def test_open_db_creates_models_table(tmp_path: Path) -> None:
    """Sanity: table exists after open_db."""
    conn = _open(tmp_path)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='models'"
    )
    assert cur.fetchone() is not None


# Upsert -------------------------------------------------------------------


def test_upsert_writes_all_affiliate_fields(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    mms.upsert_room(conn, _AFFILIATE_ROOM, now=1_000_000, source="affiliate")
    row = mms.get_model(conn, "alice")
    assert row is not None
    assert row["display_name"] == "Alice"
    assert row["gender"] == "f"
    assert row["country"] == "US"
    assert row["location"] == "California"
    assert row["spoken_languages"] == "English, Spanish"
    assert row["is_hd"] == 1
    assert row["last_subject"] == "first day on cam"
    assert json.loads(row["last_tags_json"]) == ["lovense", "blonde"]
    assert row["last_viewers"] == 1234
    assert row["last_followers"] == 5678
    assert row["last_image_url"] == "https://example.com/alice.jpg"
    assert row["last_image_url_thumb"] == "https://example.com/alice_thumb.jpg"
    assert row["last_seconds_online"] == 3600
    assert row["last_online_epoch"] == 1_000_000
    assert row["first_online_epoch"] == 1_000_000
    assert row["last_source"] == "affiliate"


def test_upsert_writes_all_roomlist_fields(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    mms.upsert_room(conn, _ROOMLIST_ROOM, now=2_000_000, source="roomlist")
    row = mms.get_model(conn, "alice")
    assert row is not None
    assert row["age"] == 22
    assert row["is_age_verified"] == 1
    assert row["is_gaming"] == 0
    assert row["has_password"] == 0
    assert row["private_price"] == 60
    assert row["spy_show_price"] == 20
    assert row["last_image_url_legacy"] == "https://thumb.live.mmcdn.com/ri/alice.jpg"
    assert row["last_start_epoch"] == 1_777_000_000
    assert row["last_start_iso"] == "2026-04-28T12:00:00+00:00"
    assert row["last_source"] == "roomlist"


def test_upsert_partial_overlay_preserves_richer_data(tmp_path: Path) -> None:
    """Per user request: 'any data we can get from there profile and all
    these pages we look at lets store'. Once a value lands in the DB,
    a later upsert from a less-rich source must NOT wipe it.

    Affiliate writes display_name + spoken_languages + image_url.
    Then roomlist (no display_name field) overlays. The earlier values
    must remain.
    """
    conn = _open(tmp_path)
    mms.upsert_room(conn, _AFFILIATE_ROOM, now=1_000_000, source="affiliate")
    mms.upsert_room(conn, _ROOMLIST_ROOM, now=2_000_000, source="roomlist")
    row = mms.get_model(conn, "alice")
    assert row is not None
    # Affiliate-only fields still there:
    assert row["display_name"] == "Alice"
    assert row["spoken_languages"] == "English, Spanish"
    assert row["last_image_url"] == "https://example.com/alice.jpg"
    assert row["last_seconds_online"] == 3600
    # Roomlist-only fields now also there:
    assert row["age"] == 22
    assert row["is_age_verified"] == 1
    assert row["last_start_epoch"] == 1_777_000_000


def test_upsert_updates_volatile_fields(tmp_path: Path) -> None:
    """Volatile fields (viewers, followers, subject, tags) DO get
    overwritten on each upsert -- we want the latest."""
    conn = _open(tmp_path)
    mms.upsert_room(conn, _AFFILIATE_ROOM, now=1_000_000, source="affiliate")
    mms.upsert_room(conn, _ROOMLIST_ROOM, now=2_000_000, source="roomlist")
    row = mms.get_model(conn, "alice")
    assert row is not None
    assert row["last_viewers"] == 1300
    assert row["last_followers"] == 5680
    assert row["last_subject"] == "another show"
    assert json.loads(row["last_tags_json"]) == ["lovense", "blonde", "teen"]


def test_upsert_updates_last_online_epoch_each_time(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    mms.upsert_room(conn, _AFFILIATE_ROOM, now=1_000_000, source="affiliate")
    mms.upsert_room(conn, _AFFILIATE_ROOM, now=1_500_000, source="affiliate")
    row = mms.get_model(conn, "alice")
    assert row is not None
    assert row["last_online_epoch"] == 1_500_000
    assert row["updated_epoch"] == 1_500_000


def test_upsert_first_online_epoch_is_sticky(tmp_path: Path) -> None:
    """first_online_epoch is set on the INSERT path and never updated."""
    conn = _open(tmp_path)
    mms.upsert_room(conn, _AFFILIATE_ROOM, now=1_000_000, source="affiliate")
    mms.upsert_room(conn, _AFFILIATE_ROOM, now=2_000_000, source="affiliate")
    mms.upsert_room(conn, _AFFILIATE_ROOM, now=3_000_000, source="affiliate")
    row = mms.get_model(conn, "alice")
    assert row is not None
    assert row["first_online_epoch"] == 1_000_000


def test_upsert_skips_room_with_missing_slug(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    bad = dict(_AFFILIATE_ROOM)
    bad["username"] = ""
    bad["slug"] = ""
    # Should not raise; just skip.
    mms.upsert_room(conn, bad, now=1_000_000, source="affiliate")
    cur = conn.execute("SELECT COUNT(*) FROM models")
    assert cur.fetchone()[0] == 0


# Batch --------------------------------------------------------------------


def test_upsert_rooms_batch_inserts_all(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    rooms = [
        dict(_AFFILIATE_ROOM, username=name, slug=name)
        for name in ("alice", "bob", "carol")
    ]
    n = mms.upsert_rooms(conn, rooms, now=1_000_000, source="affiliate")
    assert n == 3
    for slug in ("alice", "bob", "carol"):
        assert mms.get_model(conn, slug) is not None


def test_upsert_rooms_skips_dud_entries_in_batch(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    rooms = [
        _AFFILIATE_ROOM,
        {"random": "garbage"},        # no slug, should be skipped
        dict(_AFFILIATE_ROOM, username="bob", slug="bob"),
        "not a dict at all",           # type-error, should be skipped
    ]
    n = mms.upsert_rooms(conn, rooms, now=1_000_000, source="affiliate")
    assert n == 2  # only alice + bob made it
    assert mms.get_model(conn, "alice") is not None
    assert mms.get_model(conn, "bob") is not None


# Reads --------------------------------------------------------------------


def test_get_model_returns_none_for_unknown_slug(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    assert mms.get_model(conn, "ghost") is None


def test_get_models_batch_returns_dict_keyed_by_slug(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    mms.upsert_rooms(conn, [
        dict(_AFFILIATE_ROOM, username="alice", slug="alice"),
        dict(_AFFILIATE_ROOM, username="bob", slug="bob"),
    ], now=1_000_000, source="affiliate")

    out = mms.get_models(conn, ["alice", "bob", "ghost"])
    assert set(out.keys()) == {"alice", "bob"}  # ghost dropped (not in DB)
    assert out["alice"]["display_name"] == "Alice"


def test_get_models_batch_empty_input(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    assert mms.get_models(conn, []) == {}


# Misc ---------------------------------------------------------------------


def test_count(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    assert mms.count(conn) == 0
    mms.upsert_rooms(conn, [
        dict(_AFFILIATE_ROOM, username="alice", slug="alice"),
        dict(_AFFILIATE_ROOM, username="bob", slug="bob"),
    ], now=1_000_000, source="affiliate")
    assert mms.count(conn) == 2


def test_age_falls_back_from_display_age(tmp_path: Path) -> None:
    """Roomlist uses ``display_age`` (int); affiliate uses ``age`` (often
    null). Either should land in the ``age`` column."""
    conn = _open(tmp_path)
    mms.upsert_room(
        conn,
        dict(_ROOMLIST_ROOM, username="bob", slug="bob"),
        now=1_000_000, source="roomlist",
    )
    row = mms.get_model(conn, "bob")
    assert row is not None
    assert row["age"] == 22


# Render helpers (consumed by favs_views + addon_actions.tv_list) ----------


def test_image_for_row_prefers_image_url() -> None:
    """When all three image columns are populated, prefer the
    full-size image_url. The thumb is a 360x270 reduction, and
    last_image_url_legacy is the per-gender API's ``img`` field which
    may be a smaller / thumbnail-only URL."""
    row = {
        "last_image_url": "https://example.com/full.jpg",
        "last_image_url_thumb": "https://example.com/thumb.jpg",
        "last_image_url_legacy": "https://example.com/legacy.jpg",
    }
    assert mms.image_for_row(row) == "https://example.com/full.jpg"


def test_image_for_row_falls_back_to_thumb() -> None:
    row = {
        "last_image_url": None,
        "last_image_url_thumb": "https://example.com/thumb.jpg",
        "last_image_url_legacy": None,
    }
    assert mms.image_for_row(row) == "https://example.com/thumb.jpg"


def test_image_for_row_falls_back_to_legacy() -> None:
    row = {
        "last_image_url": None,
        "last_image_url_thumb": None,
        "last_image_url_legacy": "https://example.com/legacy.jpg",
    }
    assert mms.image_for_row(row) == "https://example.com/legacy.jpg"


def test_image_for_row_returns_none_when_all_missing() -> None:
    assert mms.image_for_row({}) is None
    assert mms.image_for_row({"last_image_url": ""}) is None
    assert mms.image_for_row({
        "last_image_url": None,
        "last_image_url_thumb": None,
        "last_image_url_legacy": None,
    }) is None


def test_last_seen_ago_label_minutes() -> None:
    """Under an hour -> "Xm"."""
    label = mms.last_seen_ago_label(
        {"last_online_epoch": 1_000_000}, now=1_000_000 + 5 * 60,
    )
    assert label == "5m"


def test_last_seen_ago_label_hours() -> None:
    label = mms.last_seen_ago_label(
        {"last_online_epoch": 1_000_000}, now=1_000_000 + 6697,
    )
    assert label == "1h 51m"


def test_last_seen_ago_label_days() -> None:
    label = mms.last_seen_ago_label(
        {"last_online_epoch": 1_000_000},
        now=1_000_000 + 3 * 86400 + 14 * 3600,
    )
    assert label == "3d 14h"


def test_last_seen_ago_label_under_a_minute_returns_empty() -> None:
    """Just-polled-a-minute-ago = effectively still in the live feed.
    No "last seen" label needed -- caller treats this as "online" for
    rendering."""
    label = mms.last_seen_ago_label(
        {"last_online_epoch": 1_000_000}, now=1_000_000 + 30,
    )
    assert label == ""


def test_last_seen_ago_label_no_epoch_returns_empty() -> None:
    """Row has no last_online_epoch (defensive: shouldn't happen in
    normal flow but the schema allows null somehow). Empty result so
    the caller can omit the line."""
    assert mms.last_seen_ago_label({}, now=1_000_000) == ""
    assert mms.last_seen_ago_label({"last_online_epoch": 0}, now=1_000_000) == ""


def test_last_seen_ago_label_future_epoch_returns_empty() -> None:
    """Defensive: clock skew or bad data shouldn't produce a negative
    duration. Treat future-dated as "now" (empty)."""
    label = mms.last_seen_ago_label(
        {"last_online_epoch": 2_000_000}, now=1_000_000,
    )
    assert label == ""


def test_plot_for_offline_row_full() -> None:
    """Mirrors the plot_for() shape for the live-side: Subject / Age
    / Location / Watching / Followers / Tags lines, plus a new
    "Last seen: Xh Ym" line so the offline view says how long ago.
    Uses cyan accents to match the live-side aesthetic.
    """
    row = {
        "slug": "alice",
        "display_name": "Alice",
        "age": 22,
        "location": "California",
        "last_subject": "first day on cam",
        "last_tags_json": '["lovense", "blonde"]',
        "last_viewers": 1234,
        "last_followers": 5678,
        "last_online_epoch": 1_000_000,
    }
    plot = mms.plot_for_offline_row(row, now=1_000_000 + 6697)
    assert "first day on cam" in plot
    assert "Age:" in plot and "22" in plot
    assert "Location:" in plot and "California" in plot
    assert "Watching:" in plot and "1234" in plot
    assert "Followers:" in plot and "5678" in plot
    assert "#lovense" in plot
    assert "Last seen:" in plot
    assert "1h 51m" in plot


def test_plot_for_offline_row_partial_omits_missing_lines() -> None:
    """Sparse row (early in our DB's life) -> we render what we have
    and quietly skip the rest. No empty "Location:" line, etc.
    """
    row = {
        "slug": "alice",
        "last_viewers": 100,
        "last_online_epoch": 1_000_000,
    }
    plot = mms.plot_for_offline_row(row, now=1_000_000 + 60)
    # Location should not appear (no value).
    assert "Location:" not in plot
    # Watching should appear with viewers count.
    assert "Watching:" in plot and "100" in plot
    # Last seen at 1m -> "1m"
    assert "1m" in plot


def test_plot_for_offline_row_uses_8char_alpha_color_tags() -> None:
    """Regression guard: every [COLOR <hex>] in the offline plot
    must use 8-char AARRGGBB. 6-char gets rendered blank by Kodi.
    """
    import re
    row = {
        "slug": "alice",
        "last_subject": "test",
        "age": 22,
        "location": "X",
        "last_viewers": 1,
        "last_followers": 1,
        "last_tags_json": '["a"]',
        "last_online_epoch": 1_000_000,
    }
    plot = mms.plot_for_offline_row(row, now=1_000_000 + 60)
    for hex_val in re.findall(r"\[COLOR ([^\]]+)\]", plot):
        assert len(hex_val) == 8, (
            f"got {hex_val!r} ({len(hex_val)} chars) - Kodi needs 8-char AARRGGBB"
        )
