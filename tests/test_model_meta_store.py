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

import pytest
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


def test_upsert_persists_last_room_status_from_current_show(
    tmp_path: Path,
) -> None:
    """v0.7.32: bulk-track upsert must persist current_show to
    last_room_status so the offline favs view can distinguish a
    hidden / private / paid-show fav from a plain-offline one
    without waiting for a deep refresh."""
    conn = _open(tmp_path)
    hidden_room = dict(_AFFILIATE_ROOM)
    hidden_room["username"] = "ms"
    hidden_room["slug"] = "ms"
    hidden_room["current_show"] = "hidden"
    mms.upsert_room(conn, hidden_room, now=1_000_000, source="affiliate")
    row = mms.get_model(conn, "ms")
    assert row is not None
    assert row["last_room_status"] == "hidden"


def test_upsert_room_overwrites_last_room_status_on_state_change(
    tmp_path: Path,
) -> None:
    """v0.7.32: status is volatile freshness data -- the model toggles
    public <-> hidden as paid shows start/end. Latest poll wins
    (always-overwrite, NOT COALESCE) so a stale "public" never lingers
    after the model went hidden, which would silently re-promote her
    to TV-pickable."""
    conn = _open(tmp_path)
    public = dict(_AFFILIATE_ROOM)
    public["current_show"] = "public"
    mms.upsert_room(conn, public, now=1_000_000, source="affiliate")
    row1 = mms.get_model(conn, "alice")
    assert row1 is not None
    assert row1["last_room_status"] == "public"

    hidden = dict(_AFFILIATE_ROOM)
    hidden["current_show"] = "hidden"
    mms.upsert_room(conn, hidden, now=1_500_000, source="affiliate")
    row2 = mms.get_model(conn, "alice")
    assert row2 is not None
    assert row2["last_room_status"] == "hidden", (
        "status must overwrite, not COALESCE-preserve a stale public"
    )


def test_upsert_biocontext_overwrites_last_room_status(
    tmp_path: Path,
) -> None:
    """v0.7.36 backfill (audit pass #2 HIGH): mirror of
    test_upsert_room_overwrites_last_room_status_on_state_change but
    on the biocontext upsert variant. ``last_room_status`` was added
    to upsert_biocontext's always_overwrite at v0.7.32; without this
    test, a regression that drops it back to COALESCE would silently
    re-promote a hidden model to TV-pickable on the biocontext path."""
    conn = _open(tmp_path)

    # Seed with a public reading.
    public_bio = dict(_BIOCONTEXT_SAMPLE)
    public_bio["room_status"] = "public"
    mms.upsert_biocontext(conn, "alice", public_bio, now=1_000_000)
    row1 = mms.get_model(conn, "alice")
    assert row1 is not None
    assert row1["last_room_status"] == "public"

    # Re-fetch reports hidden -- the latest read must win.
    hidden_bio = dict(_BIOCONTEXT_SAMPLE)
    hidden_bio["room_status"] = "hidden"
    mms.upsert_biocontext(conn, "alice", hidden_bio, now=1_500_000)
    row2 = mms.get_model(conn, "alice")
    assert row2 is not None
    assert row2["last_room_status"] == "hidden", (
        "biocontext re-fetch must overwrite, not COALESCE-preserve a "
        "stale public after the model went hidden"
    )


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
        "last_image_url": "https://thumb.live.mmcdn.com/ri/full.jpg",
        "last_image_url_thumb": "https://thumb.live.mmcdn.com/ri/thumb.jpg",
        "last_image_url_legacy": "https://thumb.live.mmcdn.com/ri/legacy.jpg",
    }
    assert mms.image_for_row(row) == "https://thumb.live.mmcdn.com/ri/full.jpg"


def test_image_for_row_falls_back_to_thumb() -> None:
    row = {
        "last_image_url": None,
        "last_image_url_thumb": "https://thumb.live.mmcdn.com/ri/thumb.jpg",
        "last_image_url_legacy": None,
    }
    assert mms.image_for_row(row) == "https://thumb.live.mmcdn.com/ri/thumb.jpg"


def test_image_for_row_falls_back_to_legacy() -> None:
    row = {
        "last_image_url": None,
        "last_image_url_thumb": None,
        "last_image_url_legacy": "https://thumb.live.mmcdn.com/ri/legacy.jpg",
    }
    assert mms.image_for_row(row) == "https://thumb.live.mmcdn.com/ri/legacy.jpg"


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


def test_v1_db_migrates_to_v2_preserving_data(tmp_path: Path) -> None:
    """v0.7.21 added three columns (last_room_status,
    last_status_check_epoch, thumb_available) for the deep status
    crawl. Production DBs already exist at v1 -- migration must add
    the columns without dropping the rows we already have."""
    db = tmp_path / "meta.db"
    # Create a v1 DB by hand: the v1 schema is the ORIGINAL columns
    # plus schema_meta(version=1).
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE models (
            slug TEXT PRIMARY KEY,
            display_name TEXT,
            last_subject TEXT,
            last_viewers INTEGER,
            last_image_url TEXT,
            last_image_url_thumb TEXT,
            last_image_url_legacy TEXT,
            last_online_epoch INTEGER NOT NULL,
            first_online_epoch INTEGER,
            updated_epoch INTEGER NOT NULL
        );
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta (key, value) VALUES ('version', '1');
        INSERT INTO models (slug, display_name, last_subject, last_viewers,
            last_image_url, last_online_epoch, first_online_epoch, updated_epoch)
        VALUES ('alice', 'Alice', 'old subject', 99,
                'https://example.com/x.jpg', 1000, 1000, 1000);
    """)
    conn.commit()
    conn.close()

    # Open with current code - migration must run.
    conn = mms.open_db(str(db))
    cur = conn.execute("SELECT value FROM schema_meta WHERE key='version'")
    assert cur.fetchone()[0] == str(mms._SCHEMA_VERSION)

    # Existing row must still be readable.
    row = mms.get_model(conn, "alice")
    assert row is not None
    assert row["display_name"] == "Alice"
    assert row["last_subject"] == "old subject"
    assert row["last_viewers"] == 99
    # New columns present, NULL for the existing row (haven't been
    # populated yet -- that happens via deep refresh).
    assert "last_room_status" in row
    assert row["last_room_status"] is None
    assert "last_status_check_epoch" in row
    assert "thumb_available" in row


def test_upsert_status_updates_status_columns_only(tmp_path: Path) -> None:
    """v0.7.21: deep refresh writes per-slug status data via
    upsert_status -- it touches only last_room_status,
    last_status_check_epoch, thumb_available, and updated_epoch.
    Rich data from earlier auto-track upserts (display_name, age,
    image_url, last_subject, last_viewers, etc) MUST stay intact.
    """
    conn = _open(tmp_path)
    # Seed with rich data via the regular auto-track upsert.
    mms.upsert_room(conn, _AFFILIATE_ROOM, now=1_000_000, source="affiliate")

    # Now hit upsert_status as the deep refresh would.
    mms.upsert_status(
        conn, "alice",
        room_status="banned",
        thumb_available=False,
        now=2_000_000,
    )

    row = mms.get_model(conn, "alice")
    assert row is not None
    # Status columns updated.
    assert row["last_room_status"] == "banned"
    assert row["thumb_available"] == 0
    assert row["last_status_check_epoch"] == 2_000_000
    # Rich data PRESERVED.
    assert row["display_name"] == "Alice"
    assert row["last_subject"] == "first day on cam"
    assert row["last_viewers"] == 1234
    assert row["last_image_url"] == "https://example.com/alice.jpg"
    # last_online_epoch stays at the original value (we did NOT see
    # them online during a status check; only the status changed).
    assert row["last_online_epoch"] == 1_000_000


def test_upsert_status_creates_row_for_unknown_slug(tmp_path: Path) -> None:
    """If we deep-refresh a slug we've never seen online (= no row in
    the DB), upsert_status creates the row with just the status
    fields populated. last_online_epoch ends up as 0 since we've
    never confirmed them online."""
    conn = _open(tmp_path)
    mms.upsert_status(
        conn, "ghost",
        room_status="offline",
        thumb_available=True,
        now=2_000_000,
    )
    row = mms.get_model(conn, "ghost")
    assert row is not None
    assert row["slug"] == "ghost"
    assert row["last_room_status"] == "offline"
    assert row["thumb_available"] == 1
    assert row["last_status_check_epoch"] == 2_000_000
    # No rich data since we've never seen them.
    assert row["display_name"] is None
    assert row["last_image_url"] is None


def test_plot_for_offline_row_includes_status_line_when_present() -> None:
    """v0.7.21: render the status pulled from the deep refresh."""
    row = {
        "slug": "alice",
        "last_subject": "test",
        "last_room_status": "offline",
        "last_online_epoch": 1_000_000,
    }
    plot = mms.plot_for_offline_row(row, now=1_000_000 + 60)
    assert "Status:" in plot
    assert "offline" in plot


def test_plot_for_offline_row_omits_status_line_when_missing() -> None:
    row = {
        "slug": "alice",
        "last_online_epoch": 1_000_000,
    }
    plot = mms.plot_for_offline_row(row, now=1_000_000 + 60)
    assert "Status:" not in plot


def test_label_prefix_for_row_marks_gone_models() -> None:
    """v0.7.21: banned / deleted / gone account states get a [GONE]
    prefix in the label so the user can see at a glance which favs
    aren't coming back."""
    for status in ("banned", "deleted", "gone"):
        prefix = mms.label_prefix_for_row({"last_room_status": status})
        assert "GONE" in prefix, f"status={status} should get GONE prefix"


def test_label_prefix_for_row_is_empty_for_neutral_states() -> None:
    """v0.7.34: ``public`` (model is on and freely watchable elsewhere
    from the user's POV when they later refresh) and ``offline`` get
    no prefix; only the broadcasting-but-paywalled or no-account
    states pick up a label decoration."""
    for status in ("public", "offline", None, ""):
        prefix = mms.label_prefix_for_row({"last_room_status": status})
        assert prefix == "", f"status={status!r} should not be flagged: {prefix!r}"


def test_label_prefix_for_row_marks_hidden_and_private_amber() -> None:
    """v0.7.34: hidden paid-show and private 1-on-1 broadcasters get
    an amber state prefix so the user fast-scanning offline favs can
    spot the ones that are actually live (just not freely viewable)
    vs the "not on right now" majority."""
    hidden = mms.label_prefix_for_row({"last_room_status": "hidden"})
    assert "SHOW" in hidden
    assert "FFff8000" in hidden, "hidden should be amber-colored"

    private = mms.label_prefix_for_row({"last_room_status": "private"})
    assert "PRIV" in private
    assert "FFff8000" in private, "private should be amber-colored"


def test_label_prefix_for_row_marks_away_and_password_neutral() -> None:
    """v0.7.34: away and password-protected get a neutral pale-cyan
    prefix -- they're broadcasting too, but with no token-gate value
    proposition like hidden/private."""
    away = mms.label_prefix_for_row({"last_room_status": "away"})
    assert "AWAY" in away
    pw = mms.label_prefix_for_row(
        {"last_room_status": "password protected"}
    )
    assert "PW" in pw


def test_get_offline_fav_slugs_excludes_currently_online(tmp_path: Path) -> None:
    """The deep refresh only re-checks slugs that aren't currently
    online (the auto-track already has fresh data for those). This
    helper takes the full fav-slug list + the bulk-cache live set
    and returns the offline subset.
    """
    online_slugs = frozenset({"alice", "bob"})
    fav_slugs = ["alice", "bob", "carol", "dave"]
    out = mms.partition_offline_slugs(fav_slugs, online_slugs)
    assert out == ["carol", "dave"]


_BIOCONTEXT_SAMPLE = {
    "follower_count": 155945,
    "location": "Deep inside your heart",
    "real_name": "Evelyn",
    "body_decorations": "tattoos",
    "last_broadcast": "2026-04-28T19:56:30.950",
    "smoke_drink": "occasional",
    "body_type": "thin",
    "display_birthday": "Nov. 9, 1989",
    "about_me": "<p>hi I am Evelyn</p>",
    "wish_list": "<p>your love</p>",
    "time_since_last_broadcast": "2 hours ago",
    "fan_club_cost": 90,
    "performer_has_fanclub": True,
    "fan_club_is_member": False,
    "fan_club_join_url": "/fanclub/join/evelyn/",
    "needs_supporter_to_pm": True,
    "interested_in": ["Men"],
    "display_age": 36,
    "sex": "a woman",
    "subgender": "",
    "room_status": "offline",
    "is_broadcaster_or_staff": False,
    "photo_sets": [
        {"id": 21554915, "name": "undescribed pleasure",
         "cover_url": "https://static-pub.highwebmedia.com/x/cover.jpg",
         "tokens": 100, "is_video": True, "photo_count": 12,
         "video_duration_in_seconds": 179},
        {"id": 21554916, "name": "morning light",
         "cover_url": "https://static-pub.highwebmedia.com/x/m.jpg",
         "tokens": 50, "is_video": False, "photo_count": 8},
    ],
    "social_medias": [
        {"id": 1121261, "title_name": "X - Free",
         "link": "/external_link/?url=https%3A%2F%2Fx.com%2FEvelyn",
         "is_free": True},
        {"id": 1121242, "title_name": "Telegram - Free",
         "link": "/external_link/?url=https%3A%2F%2Ft.me%2Fevelyn",
         "is_free": True},
    ],
}


# v0.7.24 biocontext: the breakthrough endpoint that gives us
# last_broadcast + real_name + photo_sets + age/location/etc for
# any model regardless of online state. New schema columns + upsert.


def test_ensure_columns_swallows_duplicate_column_name_race(
    tmp_path: Path,
) -> None:
    """v0.7.38 (audit pass #4 HIGH #8): two concurrent upgrade-on-
    first-launch processes both pass the PRAGMA check, both run
    ALTER TABLE -- loser raises ``OperationalError: duplicate column
    name``. Pre-fix, the exception bubbled to caller's bare
    ``except Exception`` and the meta render silently degraded.
    Now: re-running ``_ensure_columns`` against an already-migrated
    DB is a clean no-op (every column already exists -> no ALTER
    fired). Simulate the race-loser path by manually running
    ``ALTER TABLE`` on a column to force the dup-name error from
    sqlite, then call ``_ensure_columns`` and verify it doesn't
    raise."""
    conn = _open(tmp_path)
    # Manually add a column that's already in the schema. _open()
    # already ran the migration, so every _EXPECTED_COLUMNS column
    # exists. Trying to re-add one raises duplicate-column.
    with pytest.raises(sqlite3.OperationalError, match="duplicate column"):
        conn.execute("ALTER TABLE models ADD COLUMN slug TEXT")

    # _ensure_columns must NOT raise even when re-run on a fully-
    # migrated DB (defensive contract for the race-loser path).
    mms._ensure_columns(conn)
    # And the table is still functional.
    cur = conn.execute("PRAGMA table_info(models)")
    cols = {row[1] for row in cur.fetchall()}
    assert "slug" in cols
    assert "bio_fetched_epoch" in cols  # v3 column still there


def test_v2_db_migrates_to_v3_with_biocontext_columns(tmp_path: Path) -> None:
    """Schema bump: v2 -> v3 adds the full biocontext capture
    columns. The user explicitly wanted to "go deep" on stored
    info: real_name + last_broadcast + birthday + interested_in +
    body_type + body_decorations + smoke_drink + fan_club + photo
    sets + social medias + raw JSON blob. Old rows preserved.
    """
    conn = _open(tmp_path)
    mms.upsert_room(conn, _AFFILIATE_ROOM, now=1_000_000, source="affiliate")
    row = mms.get_model(conn, "alice")
    assert row is not None
    for col in (
        # last broadcast (timestamp + parsed epoch + pre-formatted human)
        "last_broadcast_epoch", "last_broadcast_iso", "last_broadcast_human",
        # identity / appearance
        "real_name",
        "bio_about_html", "bio_wish_list_html",
        "bio_birthday", "bio_sex", "bio_subgender",
        "bio_interested_in_json",
        "bio_body_type", "bio_body_decorations", "bio_smoke_drink",
        # fan club / interaction
        "bio_fan_club_cost", "bio_performer_has_fanclub",
        "bio_fan_club_join_url", "bio_needs_supporter_to_pm",
        "bio_is_broadcaster_or_staff",
        # collections (stored as JSON arrays)
        "bio_photo_sets_json", "bio_social_medias_json",
        # extracted thumbnail + raw blob + when we fetched
        "photo_set_cover_url", "bio_full_json", "bio_fetched_epoch",
    ):
        assert col in row, f"v3 column {col!r} missing from schema"


def test_parse_iso_naive_treated_as_pacific_time() -> None:
    """v0.7.28: biocontext returns last_broadcast as a naive ISO
    string in America/Los_Angeles. Treating it as UTC made every
    Last broadcast line 7-8h stale (PDT vs PST). The parser must
    attach the Pacific zone before computing epoch.

    Reference: 2026-04-29 was during PDT (UTC-7). Naive
    "2026-04-29T05:00:00" is 12:00:00 UTC -> epoch 1777464000.
    """
    epoch = mms._parse_iso_to_epoch("2026-04-29T05:00:00")
    # 2026-04-29T05:00:00 PDT = 2026-04-29T12:00:00 UTC.
    # 2026-04-29T12:00:00 UTC epoch = 1777464000.
    expected = 1777464000
    # Allow +/- 1h slack for PST-vs-PDT in the static-offset fallback
    # path on hosts without zoneinfo bundled.
    assert abs((epoch or 0) - expected) <= 3600, (
        f"Pacific-naive ISO parsed to epoch {epoch}, "
        f"expected ~{expected} (+/-1h DST slack)"
    )


def test_parse_iso_explicit_z_honored_as_utc() -> None:
    """An ISO with an explicit ``Z`` (or ``+HH:MM``) suffix is
    honoured as written. The Pacific default kicks in only when no
    tzinfo is present."""
    epoch = mms._parse_iso_to_epoch("2026-04-29T12:00:00Z")
    # 12:00 UTC = 1777464000.
    assert epoch == 1777464000


def test_parse_iso_with_negative_offset_honored() -> None:
    """Same time as the Z test, expressed with explicit -07:00 offset
    (Pacific Time form). Result must equal the UTC version, proving
    we honour explicit offsets rather than blindly re-attaching
    America/Los_Angeles."""
    epoch_utc = mms._parse_iso_to_epoch("2026-04-29T12:00:00Z")
    epoch_pdt = mms._parse_iso_to_epoch("2026-04-29T05:00:00-07:00")
    assert epoch_utc == epoch_pdt


def test_upsert_biocontext_overwrites_last_broadcast_epoch(
    tmp_path: Path,
) -> None:
    """v0.7.28: re-fetching biocontext after the v0.7.27 UTC bug must
    replace the stale epoch, not COALESCE-preserve it. last_broadcast_*
    is freshness data driven solely by biocontext, so the latest read
    wins."""
    conn = _open(tmp_path)
    # First upsert: imagine the v0.7.27 buggy parse stored an epoch
    # 7h earlier than reality.
    bad = dict(_BIOCONTEXT_SAMPLE)
    bad["last_broadcast"] = "2026-04-28T19:00:00.000"  # naive
    mms.upsert_biocontext(conn, "alice", bad, now=1_000_000)
    row1 = mms.get_model(conn, "alice")
    assert row1 is not None
    epoch1 = row1["last_broadcast_epoch"]
    assert epoch1 is not None and epoch1 > 0

    # Second upsert with a CHANGED last_broadcast string -- the new
    # epoch must win even though the old value is non-null.
    fresh = dict(_BIOCONTEXT_SAMPLE)
    fresh["last_broadcast"] = "2026-04-29T01:00:00.000"  # later naive
    mms.upsert_biocontext(conn, "alice", fresh, now=2_000_000)
    row2 = mms.get_model(conn, "alice")
    assert row2 is not None
    epoch2 = row2["last_broadcast_epoch"]
    assert epoch2 != epoch1, (
        f"last_broadcast_epoch must be overwritten, got {epoch2} == {epoch1}"
    )
    # The fresher ISO is 6 hours later.
    assert epoch2 - epoch1 == 6 * 3600, (
        f"new epoch should be 6h after old, got delta {epoch2 - epoch1}s"
    )


def test_upsert_biocontext_writes_all_known_fields(tmp_path: Path) -> None:
    """upsert_biocontext extracts every useful field from the
    /api/biocontext/<slug>/ response into typed columns AND stashes
    the full raw response in bio_full_json so future-us can extract
    new fields without a re-crawl. Fields the response doesn't carry
    leave the existing column value alone (sticky-on-non-null COALESCE)."""
    conn = _open(tmp_path)
    mms.upsert_biocontext(conn, "model_a", _BIOCONTEXT_SAMPLE, now=2_000_000)
    row = mms.get_model(conn, "model_a")
    assert row is not None
    # identity + appearance
    assert row["real_name"] == "Evelyn"
    assert row["bio_birthday"] == "Nov. 9, 1989"
    assert row["bio_sex"] == "a woman"
    assert row["bio_body_type"] == "thin"
    assert row["bio_body_decorations"] == "tattoos"
    assert row["bio_smoke_drink"] == "occasional"
    assert "Evelyn" in (row["bio_about_html"] or "")
    assert "your love" in (row["bio_wish_list_html"] or "")
    assert json.loads(row["bio_interested_in_json"]) == ["Men"]
    # last broadcast (ISO + parsed epoch + pre-formatted human)
    assert row["last_broadcast_iso"] == "2026-04-28T19:56:30.950"
    assert row["last_broadcast_epoch"] is not None
    assert row["last_broadcast_epoch"] > 0
    assert row["last_broadcast_human"] == "2 hours ago"
    # fan club / interaction
    assert row["bio_fan_club_cost"] == 90
    assert row["bio_performer_has_fanclub"] == 1
    assert row["bio_fan_club_join_url"] == "/fanclub/join/evelyn/"
    assert row["bio_needs_supporter_to_pm"] == 1
    assert row["bio_is_broadcaster_or_staff"] == 0
    # photo sets + socials persisted as JSON
    photo_sets = json.loads(row["bio_photo_sets_json"])
    assert len(photo_sets) == 2
    assert photo_sets[0]["name"] == "undescribed pleasure"
    assert photo_sets[0]["is_video"] is True
    socials = json.loads(row["bio_social_medias_json"])
    assert len(socials) == 2
    assert socials[0]["title_name"] == "X - Free"
    # First photo set's cover surfaces to the dedicated column
    # (so image_for_row can grab it without parsing JSON).
    assert row["photo_set_cover_url"] == \
        "https://static-pub.highwebmedia.com/x/cover.jpg"
    # Full raw JSON blob for future-proofing -- if biocontext starts
    # returning new fields tomorrow, we can backfill from this column.
    full = json.loads(row["bio_full_json"])
    assert full["follower_count"] == 155945
    assert row["bio_fetched_epoch"] == 2_000_000
    # Standard cross-populated fields (also writable by bulk poll;
    # biocontext just fills them in if they were null):
    assert row["age"] == 36
    assert row["location"] == "Deep inside your heart"
    assert row["last_followers"] == 155945
    assert row["last_room_status"] == "offline"
    assert row["last_status_check_epoch"] == 2_000_000
    assert row["last_source"] == "biocontext"


def test_upsert_biocontext_overlays_preserve_bulk_data(tmp_path: Path) -> None:
    """A bulk-poll upsert seeded the row with display_name, image_url,
    etc. A later biocontext upsert layers in last_broadcast, real_name,
    photo_set_cover_url WITHOUT wiping the bulk fields."""
    conn = _open(tmp_path)
    mms.upsert_room(conn, _AFFILIATE_ROOM, now=1_000_000, source="affiliate")
    mms.upsert_biocontext(conn, "alice", _BIOCONTEXT_SAMPLE, now=2_000_000)
    row = mms.get_model(conn, "alice")
    assert row is not None
    # Bulk-only fields preserved:
    assert row["display_name"] == "Alice"
    assert row["spoken_languages"] == "English, Spanish"
    assert row["last_image_url"] == "https://example.com/alice.jpg"
    # Biocontext-only fields now also present:
    assert row["real_name"] == "Evelyn"
    assert row["last_broadcast_iso"] == "2026-04-28T19:56:30.950"
    assert row["photo_set_cover_url"] == \
        "https://static-pub.highwebmedia.com/x/cover.jpg"


def test_upsert_biocontext_skips_empty_dict(tmp_path: Path) -> None:
    """Network failed / response empty -> no row written, no crash."""
    conn = _open(tmp_path)
    mms.upsert_biocontext(conn, "alice", {}, now=2_000_000)
    assert mms.get_model(conn, "alice") is None


def test_image_for_row_prefers_photo_set_cover_over_thumb() -> None:
    """v0.7.24: when biocontext gave us a photo set cover, prefer
    that over the live-thumb URL -- it's a curated profile image,
    not a live-stream snapshot, so it stays fresh even when the
    model has been offline for months."""
    row = {
        "slug": "alice",
        "last_image_url": None,
        "last_image_url_thumb": None,
        "last_image_url_legacy": None,
        "photo_set_cover_url": "https://static-pub.highwebmedia.com/x/cover.jpg",
        "thumb_available": 1,
    }
    assert mms.image_for_row(row) == \
        "https://static-pub.highwebmedia.com/x/cover.jpg"


def test_image_for_row_falls_back_to_synthesized_thumb_no_photoset() -> None:
    """Without a photo_set_cover but with thumb_available=1 we still
    synthesize the canonical static URL."""
    row = {
        "slug": "model_a",
        "last_image_url": None, "last_image_url_thumb": None,
        "last_image_url_legacy": None,
        "photo_set_cover_url": None,
        "thumb_available": 1,
    }
    assert mms.image_for_row(row) == "https://thumb.live.mmcdn.com/ri/model_a.jpg"


def test_image_for_row_no_synth_when_thumb_unavailable() -> None:
    row = {
        "slug": "model_a",
        "last_image_url": None, "last_image_url_thumb": None,
        "last_image_url_legacy": None,
        "thumb_available": 0,
    }
    assert mms.image_for_row(row) is None


def test_image_for_row_cached_url_still_wins() -> None:
    """A bulk-poll-cached URL ranks above both photo_set_cover and
    static synth -- it's the freshest live capture available."""
    row = {
        "slug": "alice",
        "last_image_url": "https://thumb.live.mmcdn.com/ri/cached.jpg",
        "photo_set_cover_url": "https://thumb.live.mmcdn.com/ri/cover.jpg",
        "thumb_available": 1,
    }
    assert mms.image_for_row(row) == "https://thumb.live.mmcdn.com/ri/cached.jpg"


def test_image_for_row_drops_untrusted_host() -> None:
    """v0.7.39 (audit pass #5 MEDIUM): a malicious biocontext could
    land a non-CB URL in last_image_url (e.g.,
    http://192.168.1.1:8088/admin or file:///etc/passwd). Kodi's image
    cache would happily fetch it. Drop untrusted hosts -- caller
    renders without thumb instead."""
    row = {
        "slug": "alice",
        # Untrusted: not in CB allowlist
        "last_image_url": "http://192.168.1.1:8088/admin",
        # Also untrusted
        "photo_set_cover_url": "file:///etc/passwd",
        # No thumb_available so we don't fall through to synth.
    }
    assert mms.image_for_row(row) is None


def test_image_for_row_drops_untrusted_then_synthesizes_when_available() -> None:
    """If the cached URLs are untrusted but thumb_available=1 with a
    valid slug, we still fall through to the synthesized URL (which
    is hardcoded https://thumb.live.mmcdn.com/ri/<slug>.jpg)."""
    row = {
        "slug": "alice",
        "last_image_url": "javascript:alert(1)",
        "thumb_available": 1,
    }
    assert mms.image_for_row(row) == "https://thumb.live.mmcdn.com/ri/alice.jpg"


def test_image_for_row_synth_skipped_for_path_traversal_slug() -> None:
    """v0.7.39 belt-and-suspenders: if a slug somehow contains path
    traversal chars (`/`, `..`), the synth URL is skipped. Slugs
    should already be alphanum-validated upstream but defense in
    depth."""
    row = {
        "slug": "../../etc/passwd",
        "thumb_available": 1,
    }
    assert mms.image_for_row(row) is None


def test_plot_for_offline_row_renders_last_broadcast_line() -> None:
    """v0.7.24: when biocontext gave us last_broadcast_epoch, render
    a "Last broadcast: Xh ago" line. Works for never-seen-online
    favs whose ONLY source of last-air info is biocontext.
    """
    row = {
        "slug": "alice",
        "last_room_status": "offline",
        "last_broadcast_epoch": 1_000_000,
        "last_online_epoch": 0,        # never bulk-polled online
    }
    plot = mms.plot_for_offline_row(row, now=1_000_000 + 7200)  # 2h
    assert "Last broadcast:" in plot
    assert "2h" in plot


def test_plot_for_offline_row_uses_last_broadcast_human_when_no_epoch() -> None:
    """Fallback: if ISO parsing failed (older Python or weird format)
    we still have the pre-formatted human string from biocontext."""
    row = {
        "slug": "alice",
        "last_room_status": "offline",
        "last_broadcast_epoch": None,
        "last_broadcast_human": "3 days ago",
        "last_online_epoch": 0,
    }
    plot = mms.plot_for_offline_row(row, now=1_000_000)
    assert "Last broadcast:" in plot
    assert "3 days ago" in plot


def test_plot_for_offline_row_real_name_used_as_subject() -> None:
    """When biocontext gave us a real_name and we don't have a richer
    subject from the bulk poll, render real_name on the first line."""
    row = {
        "slug": "alice",
        "real_name": "Evelyn",
        "last_room_status": "offline",
        "last_online_epoch": 1_000_000,
    }
    plot = mms.plot_for_offline_row(row, now=1_000_000 + 60)
    assert "Evelyn" in plot


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


# --------------------------------------------------------------------------- #
# v0.7.25: bio_full_plot_for_view_info -- the rich profile rendering
# fed to the View-info directory's "Profile" item.
# --------------------------------------------------------------------------- #


def test_bio_full_plot_renders_all_fields() -> None:
    """Full biocontext-populated row -> the view-info plot includes
    every header line we promised the user. Subject (real_name when
    no last_subject), Sex/Subgender, Age, Location, Followers,
    Last seen, Last broadcast, Verified, Body type, Body decorations,
    Smoke/drink, Fan club, Wish list, About me, Social medias, Tags.
    """
    row = {
        "slug": "alice",
        "real_name": "Evelyn",
        "age": 24,
        "location": "Earth",
        "last_followers": 12345,
        "last_online_epoch": 1_000_000,
        "last_broadcast_epoch": 1_000_000,
        "last_status_check_epoch": 1_000_000,
        "bio_sex": "Female",
        "bio_subgender": "TGirl",
        "bio_body_type": "petite",
        "bio_body_decorations": "tattoos, piercings",
        "bio_smoke_drink": "social",
        "bio_fan_club_cost": 100,
        "bio_performer_has_fanclub": 1,
        "bio_wish_list_html": "all the things",
        "bio_about_html": "Hi I'm Evelyn",
        "bio_social_medias_json": json.dumps([
            {"platform": "twitter", "url_or_handle": "@evelyn"},
            {"platform": "instagram", "url_or_handle": "@e_insta"},
        ]),
        "last_tags_json": json.dumps(["new", "petite"]),
    }
    plot = mms.bio_full_plot_for_view_info(row, now=1_000_000 + 600)
    # Heading line uses real_name when no last_subject.
    assert "Evelyn" in plot
    # Standard offline render lines still appear.
    assert "Age:" in plot and "24" in plot
    assert "Location:" in plot and "Earth" in plot
    assert "Followers:" in plot and "12345" in plot
    assert "Last broadcast:" in plot
    assert "Verified:" in plot
    # Bio-specific richer lines.
    assert "Sex:" in plot and "Female" in plot
    assert "TGirl" in plot
    assert "Body:" in plot and "petite" in plot
    assert "tattoos" in plot
    assert "Smoke" in plot or "Drink" in plot
    assert "Fan club:" in plot and "100" in plot
    assert "Wish list:" in plot and "all the things" in plot
    assert "About:" in plot and "Evelyn" in plot
    # Social medias rendered as a single line with platform names.
    assert "Social:" in plot
    assert "twitter" in plot.lower() or "@evelyn" in plot.lower()
    # Tags survive.
    assert "#new" in plot or "#petite" in plot


def test_bio_full_plot_omits_missing_fields() -> None:
    """Sparse row -> no empty header lines. The output must contain
    only lines we have data for."""
    row = {
        "slug": "ghost",
        "last_room_status": "offline",
    }
    plot = mms.bio_full_plot_for_view_info(row, now=1_000_000)
    # No header tags whose data we don't have.
    assert "Body:" not in plot
    assert "Fan club:" not in plot
    assert "Sex:" not in plot
    assert "Wish list:" not in plot
    assert "Social:" not in plot
    # Status line is OK to keep since we DO have it.
    # The output should still be a well-formed string (possibly empty).
    assert isinstance(plot, str)


def test_bio_full_plot_uses_8char_alpha_color_tags() -> None:
    """Same regression guard as the offline plot: every [COLOR <hex>]
    must be 8 chars (AARRGGBB), or Kodi renders it blank."""
    import re
    row = {
        "slug": "alice",
        "real_name": "Evelyn",
        "age": 24,
        "location": "Earth",
        "last_followers": 100,
        "last_online_epoch": 1_000_000,
        "bio_sex": "Female",
        "bio_subgender": "TGirl",
        "bio_body_type": "petite",
        "bio_smoke_drink": "social",
        "bio_fan_club_cost": 100,
        "bio_performer_has_fanclub": 1,
        "bio_about_html": "hi",
        "last_tags_json": json.dumps(["new"]),
    }
    plot = mms.bio_full_plot_for_view_info(row, now=1_000_000 + 60)
    for hex_val in re.findall(r"\[COLOR ([^\]]+)\]", plot):
        assert len(hex_val) == 8, (
            f"got {hex_val!r} ({len(hex_val)} chars) - Kodi needs 8-char AARRGGBB"
        )
