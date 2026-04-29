"""SQLite-backed accumulating store for model metadata.

The TV / favs / browse views currently fetch model info on demand
from Chaturbate's affiliate-onlinerooms feed (and the per-gender
roomlist endpoint for Top / New / Female / Male / Couple / Trans /
Search). Both are LIVE-only -- once a model goes offline she
disappears from the feed and we have no rich metadata for the offline
favs view.

This module persists everything we ever see about a model (across
both API shapes) in a sqlite db so:

1. Offline favs render with the LAST KNOWN thumbnail, subject, tags,
   viewers/followers count, etc -- no extra HTTP needed.
2. The TV-list view can show "last seen N hours ago" next to each
   entry.
3. If a model bounces between online / offline, the row accumulates
   richer data over time -- one source might be missing
   spoken_languages but a later one has it; we keep both.

The store is intentionally append-overlay: every upsert COALESCEs
non-null incoming values onto the existing row. Volatile fields
(viewers, followers, subject, tags, last_online_epoch, last_seconds_online)
DO get overwritten because we want the latest. Stable identity
fields (display_name, gender, age, country, location, spoken_languages,
is_hd, etc) are sticky -- once seen, kept. ``first_online_epoch`` is
set on insert and never updated.

No Kodi imports here -- pure stdlib so the tests don't need any
xbmc mocks. Production callers pass an absolute path inside
``addon_data/`` derived via xbmcvfs.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any


_SCHEMA_VERSION = 3


# Schema is split into TABLES + INDEXES so we can run an
# add-missing-columns step between them. CREATE INDEX on a column
# that doesn't exist (because the DB is at an older schema) would
# fail otherwise.
_DDL_TABLES = """
CREATE TABLE IF NOT EXISTS models (
    slug                 TEXT PRIMARY KEY,
    display_name         TEXT,
    gender               TEXT,
    age                  INTEGER,
    country              TEXT,
    location             TEXT,
    spoken_languages     TEXT,
    birthday             TEXT,
    is_hd                INTEGER,
    is_age_verified      INTEGER,
    is_gaming            INTEGER,
    is_new               INTEGER,
    has_password         INTEGER,
    recorded             INTEGER,
    private_price        INTEGER,
    spy_show_price       INTEGER,
    last_subject         TEXT,
    last_tags_json       TEXT,
    last_viewers         INTEGER,
    last_followers       INTEGER,
    last_image_url       TEXT,
    last_image_url_thumb TEXT,
    last_image_url_legacy TEXT,
    block_from_countries TEXT,
    block_from_states    TEXT,
    last_start_epoch     INTEGER,
    last_start_iso       TEXT,
    last_online_epoch    INTEGER NOT NULL,
    first_online_epoch   INTEGER,
    last_seconds_online  INTEGER,
    last_source          TEXT,
    updated_epoch        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns that may be missing on older DBs; ALTER TABLE adds them in
# place without dropping data. Format: "col_name DDL_FRAGMENT".
_EXPECTED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("display_name", "TEXT"),
    ("gender", "TEXT"),
    ("age", "INTEGER"),
    ("country", "TEXT"),
    ("location", "TEXT"),
    ("spoken_languages", "TEXT"),
    ("birthday", "TEXT"),
    ("is_hd", "INTEGER"),
    ("is_age_verified", "INTEGER"),
    ("is_gaming", "INTEGER"),
    ("is_new", "INTEGER"),
    ("has_password", "INTEGER"),
    ("recorded", "INTEGER"),
    ("private_price", "INTEGER"),
    ("spy_show_price", "INTEGER"),
    ("last_subject", "TEXT"),
    ("last_tags_json", "TEXT"),
    ("last_viewers", "INTEGER"),
    ("last_followers", "INTEGER"),
    ("last_image_url", "TEXT"),
    ("last_image_url_thumb", "TEXT"),
    ("last_image_url_legacy", "TEXT"),
    ("block_from_countries", "TEXT"),
    ("block_from_states", "TEXT"),
    ("last_start_epoch", "INTEGER"),
    ("last_start_iso", "TEXT"),
    ("first_online_epoch", "INTEGER"),
    ("last_seconds_online", "INTEGER"),
    ("last_source", "TEXT"),
    # v0.7.21 (schema v2):
    ("last_room_status", "TEXT"),
    ("last_status_check_epoch", "INTEGER"),
    ("thumb_available", "INTEGER"),
    # v0.7.24 (schema v3) -- the biocontext capture columns. The
    # /api/biocontext/<slug>/ endpoint returns the model's full
    # public profile in one shot (including last_broadcast which is
    # otherwise unobtainable for offline models). We extract the
    # render-relevant fields into structured columns for fast access
    # AND keep the full raw response in bio_full_json so future-us
    # can backfill new fields without a re-crawl.
    ("last_broadcast_iso", "TEXT"),
    ("last_broadcast_epoch", "INTEGER"),
    ("last_broadcast_human", "TEXT"),
    ("real_name", "TEXT"),
    ("bio_about_html", "TEXT"),
    ("bio_wish_list_html", "TEXT"),
    ("bio_birthday", "TEXT"),
    ("bio_sex", "TEXT"),
    ("bio_subgender", "TEXT"),
    ("bio_interested_in_json", "TEXT"),
    ("bio_body_type", "TEXT"),
    ("bio_body_decorations", "TEXT"),
    ("bio_smoke_drink", "TEXT"),
    ("bio_fan_club_cost", "INTEGER"),
    ("bio_performer_has_fanclub", "INTEGER"),
    ("bio_fan_club_join_url", "TEXT"),
    ("bio_needs_supporter_to_pm", "INTEGER"),
    ("bio_is_broadcaster_or_staff", "INTEGER"),
    ("bio_photo_sets_json", "TEXT"),
    ("bio_social_medias_json", "TEXT"),
    ("photo_set_cover_url", "TEXT"),
    ("bio_full_json", "TEXT"),
    ("bio_fetched_epoch", "INTEGER"),
)

_DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_models_last_online ON models(last_online_epoch);
CREATE INDEX IF NOT EXISTS idx_models_gender ON models(gender);
CREATE INDEX IF NOT EXISTS idx_models_status ON models(last_room_status);
"""


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """ALTER TABLE for any column missing on the existing DB. Older
    DBs created before a column was added end up with the new column
    set to NULL on existing rows -- callers tolerate NULL (the upsert
    code uses COALESCE, the renderer guards every read).
    """
    cur = conn.execute("PRAGMA table_info(models)")
    existing = {row[1] for row in cur.fetchall()}
    for col, type_ in _EXPECTED_COLUMNS:
        if col not in existing:
            # col + type_ come from a fixed module-level tuple; no
            # user input lands in the SQL string.
            conn.execute(f"ALTER TABLE models ADD COLUMN {col} {type_}")


def open_db(path: str) -> sqlite3.Connection:
    """Open (or create) the meta DB at ``path``. Schema is applied
    idempotently so this is safe to call on every addon startup.

    Migration semantics: any column listed in ``_EXPECTED_COLUMNS``
    that's missing on the existing DB gets added via ALTER TABLE.
    Existing rows keep all their data; new columns default to NULL
    until populated by a subsequent upsert.

    The connection is configured with WAL journal mode for read/write
    concurrency (TV loop polls write to it while a render thread reads)
    and with row_factory=Row so callers get column-name access.
    """
    conn = sqlite3.connect(path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_DDL_TABLES)
    _ensure_columns(conn)
    conn.executescript(_DDL_INDEXES)
    # Stamp the version regardless of upgrade path -- INSERT OR
    # REPLACE so we move from v1 to v2 cleanly.
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
        (str(_SCHEMA_VERSION),),
    )
    return conn


# Field extraction ---------------------------------------------------------


def _to_int_or_none(v: Any) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return int(v)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _to_bool_int_or_none(v: Any) -> int | None:
    """Bool-ish columns: True/False -> 1/0; "true"/"false" string also.
    None / unrecognized -> None so COALESCE preserves the prior value.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, int):
        return 1 if v else 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes"):
            return 1
        if s in ("false", "0", "no"):
            return 0
    return None


def _tags_to_json_or_none(v: Any) -> str | None:
    if not isinstance(v, list):
        return None
    if not v:
        return None
    return json.dumps([str(t) for t in v])


def _row_values(room: dict[str, Any], *, now: int, source: str) -> dict[str, Any] | None:
    """Convert a raw room dict to a column-keyed values dict, or
    None if the room lacks an identifying slug.

    Affiliate-API and roomlist-API have overlapping but non-identical
    field names; this function handles both shapes.
    """
    slug = (room.get("slug") or room.get("username") or "").strip()
    if not slug:
        return None

    # Age: affiliate has ``age`` (often null); roomlist has ``display_age``.
    age = _to_int_or_none(room.get("age"))
    if age is None:
        age = _to_int_or_none(room.get("display_age"))

    return {
        "slug": slug,
        "display_name": _to_str_or_none(room.get("display_name")),
        "gender": _to_str_or_none(room.get("gender")),
        "age": age,
        "country": _to_str_or_none(room.get("country")),
        "location": _to_str_or_none(room.get("location")),
        "spoken_languages": _to_str_or_none(room.get("spoken_languages")),
        "birthday": _to_str_or_none(room.get("birthday")),
        "is_hd": _to_bool_int_or_none(room.get("is_hd")),
        "is_age_verified": _to_bool_int_or_none(room.get("is_age_verified")),
        "is_gaming": _to_bool_int_or_none(room.get("is_gaming")),
        "is_new": _to_bool_int_or_none(room.get("is_new")),
        "has_password": _to_bool_int_or_none(room.get("has_password")),
        "recorded": _to_bool_int_or_none(room.get("recorded")),
        "private_price": _to_int_or_none(room.get("private_price")),
        "spy_show_price": _to_int_or_none(room.get("spy_show_price")),
        "last_subject": _to_str_or_none(
            room.get("room_subject") or room.get("subject")
        ),
        "last_tags_json": _tags_to_json_or_none(room.get("tags")),
        "last_viewers": _to_int_or_none(room.get("num_users")),
        "last_followers": _to_int_or_none(room.get("num_followers")),
        "last_image_url": _to_str_or_none(room.get("image_url")),
        "last_image_url_thumb": _to_str_or_none(room.get("image_url_360x270")),
        "last_image_url_legacy": _to_str_or_none(room.get("img")),
        "block_from_countries": _to_str_or_none(room.get("block_from_countries")),
        "block_from_states": _to_str_or_none(room.get("block_from_states")),
        "last_start_epoch": _to_int_or_none(room.get("start_timestamp")),
        "last_start_iso": _to_str_or_none(room.get("start_dt_utc")),
        "last_online_epoch": int(now),
        "first_online_epoch": int(now),
        "last_seconds_online": _to_int_or_none(room.get("seconds_online")),
        "last_source": str(source),
        "updated_epoch": int(now),
    }


# Upsert -------------------------------------------------------------------


# Mental model for upserts:
# - Every row carries up to ~30 metadata columns. Different API sources
#   populate different subsets -- the affiliate API has display_name +
#   spoken_languages but no display_age; the roomlist API has
#   display_age + start_timestamp but no spoken_languages. Etc.
# - On every upsert we COALESCE incoming non-null values onto the
#   stored row. Non-null wins; null preserves the old value. So a
#   partial later source can't wipe richer earlier data, and a richer
#   later source upgrades a partial row.
# - Three columns are always-overwrite (no COALESCE) because they
#   describe the SIGHTING, not the model: ``last_online_epoch``,
#   ``updated_epoch``, ``last_source``.
# - ``first_online_epoch`` is on the INSERT path only -- never updated.

_COALESCE_COLS = (
    "display_name", "gender", "age", "country", "location",
    "spoken_languages", "birthday", "is_hd", "is_age_verified",
    "is_gaming", "is_new", "has_password", "recorded",
    "private_price", "spy_show_price",
    "last_subject", "last_tags_json", "last_viewers", "last_followers",
    "last_image_url", "last_image_url_thumb", "last_image_url_legacy",
    "block_from_countries", "block_from_states",
    "last_start_epoch", "last_start_iso",
    "last_seconds_online",
)

_OVERWRITE_COLS = (
    "last_online_epoch", "last_source", "updated_epoch",
)

_ALL_INSERT_COLS = (
    "slug",
    *_COALESCE_COLS,
    *_OVERWRITE_COLS,
    "first_online_epoch",
)


def _build_upsert_sql() -> str:
    cols = ",".join(_ALL_INSERT_COLS)
    placeholders = ",".join(":" + c for c in _ALL_INSERT_COLS)
    coalesce_assign = ",\n  ".join(
        f"{c}=COALESCE(excluded.{c}, {c})" for c in _COALESCE_COLS
    )
    overwrite_assign = ",\n  ".join(
        f"{c}=excluded.{c}" for c in _OVERWRITE_COLS
    )
    # Column names come from fixed tuples in this module, no user
    # input ever lands here. Values are bound via :name placeholders.
    return f"""
INSERT INTO models ({cols}) VALUES ({placeholders})
ON CONFLICT(slug) DO UPDATE SET
  {coalesce_assign},
  {overwrite_assign}
""".strip()  # noqa: S608


_UPSERT_SQL = _build_upsert_sql()


def upsert_room(
    conn: sqlite3.Connection,
    room: dict[str, Any],
    *,
    now: int,
    source: str,
) -> bool:
    """Insert or merge one room. Returns True on write, False on skip
    (room had no slug or was malformed).

    ``now`` is the epoch we associate with this sighting -- written to
    ``last_online_epoch`` always, and to ``first_online_epoch`` only if
    the row didn't exist before. ``source`` is a free-form tag stored
    in ``last_source`` for diagnostics ("affiliate", "roomlist", etc).
    """
    values = _row_values(room, now=now, source=source)
    if values is None:
        return False
    conn.execute(_UPSERT_SQL, values)
    return True


def upsert_rooms(
    conn: sqlite3.Connection,
    rooms: list[Any],
    *,
    now: int,
    source: str,
) -> int:
    """Batch upsert. Wraps the whole list in a single transaction for
    speed and atomicity. Non-dict / no-slug entries are silently
    skipped -- garbage in one row never breaks the whole batch.

    Returns the number of rows actually written.
    """
    written = 0
    rows: list[dict[str, Any]] = []
    for r in rooms:
        if not isinstance(r, dict):
            continue
        v = _row_values(r, now=now, source=source)
        if v is None:
            continue
        rows.append(v)
        written += 1
    if not rows:
        return 0
    with conn:
        conn.executemany(_UPSERT_SQL, rows)
    return written


# Read ---------------------------------------------------------------------


def get_model(conn: sqlite3.Connection, slug: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM models WHERE slug=?", (slug,))
    row = cur.fetchone()
    return dict(row) if row is not None else None


def get_models(conn: sqlite3.Connection, slugs: list[str]) -> dict[str, dict[str, Any]]:
    """Batched lookup. Returns ``{slug: row_dict}``. Slugs not in the
    DB are simply absent from the result (caller decides what to do
    with the gap).
    """
    if not slugs:
        return {}
    placeholders = ",".join("?" * len(slugs))
    cur = conn.execute(
        # placeholders is a fixed string of '?,?,?'; slugs are bound.
        f"SELECT * FROM models WHERE slug IN ({placeholders})",  # noqa: S608
        slugs,
    )
    return {row["slug"]: dict(row) for row in cur.fetchall()}


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM models").fetchone()[0])


# v0.7.22 deep status crawl ---------------------------------------------- #


_STATUS_GONE = ("banned", "deleted", "gone")


def upsert_status(
    conn: sqlite3.Connection,
    slug: str,
    *,
    room_status: str | None,
    thumb_available: bool | None,
    now: int,
) -> None:
    """Persist a per-slug status check (deep refresh path).

    Touches only the v2 status columns + ``updated_epoch``. Rich data
    from the bulk-track upserts (display_name, age, image_url, last_subject,
    last_viewers, etc) stays intact; this helper does NOT advance
    ``last_online_epoch`` because a status check doesn't confirm the
    model is broadcasting -- it just confirms account state.

    If the row doesn't exist yet (we deep-refresh a slug we've never
    polled online), creates a sparse row with just the status fields
    populated. ``last_online_epoch`` defaults to 0 since we've never
    seen them online; the renderer's last_seen_ago_label() returns ""
    in that case so no misleading "0s ago" appears.
    """
    slug = (slug or "").strip()
    if not slug:
        return
    rs = room_status if room_status is not None else None
    ta = (1 if thumb_available else 0) if thumb_available is not None else None
    conn.execute(
        """
        INSERT INTO models (slug, last_room_status, last_status_check_epoch,
                            thumb_available, last_online_epoch, updated_epoch)
        VALUES (:slug, :rs, :checked, :ta, 0, :now)
        ON CONFLICT(slug) DO UPDATE SET
          last_room_status = COALESCE(excluded.last_room_status, last_room_status),
          last_status_check_epoch = excluded.last_status_check_epoch,
          thumb_available = COALESCE(excluded.thumb_available, thumb_available),
          updated_epoch = excluded.updated_epoch
        """,
        {"slug": slug, "rs": rs, "ta": ta,
         "checked": int(now), "now": int(now)},
    )


def _parse_iso_to_epoch(iso: str | None) -> int | None:
    """Best-effort parse of an ISO-8601 timestamp into a Unix epoch.

    Biocontext returns ``last_broadcast`` shaped like
    ``"2026-04-28T19:56:30.950"`` -- assumed UTC, no timezone suffix.
    datetime.fromisoformat accepts that on Python 3.11+. Failures
    return None so the caller can fall back to last_broadcast_human.
    """
    if not iso:
        return None
    try:
        from datetime import datetime, timezone
        # No-tz string -> assume UTC. fromisoformat tolerates the
        # ms fraction.
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError):
        return None


# Biocontext fields -> column mapping. Source key on the left, target
# column + converter on the right. Anything not in this map ends up
# captured only via bio_full_json.
def _biocontext_row_values(
    bio: dict[str, Any], slug: str, *, now: int,
) -> dict[str, Any]:
    """Extract typed values from a biocontext dict for the upsert
    INSERT/UPDATE binding. Cross-populates standard columns
    (age, location, last_followers, last_room_status, last_status_*)
    with biocontext data so the bio call can stand in for the regular
    status check.
    """
    photo_sets = bio.get("photo_sets") or []
    cover_url: str | None = None
    if isinstance(photo_sets, list) and photo_sets:
        first = photo_sets[0]
        if isinstance(first, dict):
            cover_url = _to_str_or_none(first.get("cover_url"))

    interested = bio.get("interested_in")
    interested_json: str | None = None
    if isinstance(interested, list) and interested:
        interested_json = json.dumps([str(x) for x in interested])

    photo_sets_json: str | None = None
    if isinstance(photo_sets, list) and photo_sets:
        photo_sets_json = json.dumps(photo_sets, default=str)

    socials = bio.get("social_medias")
    socials_json: str | None = None
    if isinstance(socials, list) and socials:
        socials_json = json.dumps(socials, default=str)

    iso = _to_str_or_none(bio.get("last_broadcast"))

    return {
        "slug": slug,
        # Standard cross-populated fields (sticky-COALESCE on update).
        "age": _to_int_or_none(bio.get("display_age")),
        "location": _to_str_or_none(bio.get("location")),
        "last_followers": _to_int_or_none(bio.get("follower_count")),
        "last_room_status": _to_str_or_none(bio.get("room_status")),
        "last_status_check_epoch": int(now),
        "thumb_available": 1 if cover_url else None,
        # v3 biocontext columns
        "last_broadcast_iso": iso,
        "last_broadcast_epoch": _parse_iso_to_epoch(iso),
        "last_broadcast_human": _to_str_or_none(
            bio.get("time_since_last_broadcast")
        ),
        "real_name": _to_str_or_none(bio.get("real_name")),
        "bio_about_html": _to_str_or_none(bio.get("about_me")),
        "bio_wish_list_html": _to_str_or_none(bio.get("wish_list")),
        "bio_birthday": _to_str_or_none(bio.get("display_birthday")),
        "bio_sex": _to_str_or_none(bio.get("sex")),
        "bio_subgender": _to_str_or_none(bio.get("subgender")),
        "bio_interested_in_json": interested_json,
        "bio_body_type": _to_str_or_none(bio.get("body_type")),
        "bio_body_decorations": _to_str_or_none(bio.get("body_decorations")),
        "bio_smoke_drink": _to_str_or_none(bio.get("smoke_drink")),
        "bio_fan_club_cost": _to_int_or_none(bio.get("fan_club_cost")),
        "bio_performer_has_fanclub": _to_bool_int_or_none(
            bio.get("performer_has_fanclub")
        ),
        "bio_fan_club_join_url": _to_str_or_none(bio.get("fan_club_join_url")),
        "bio_needs_supporter_to_pm": _to_bool_int_or_none(
            bio.get("needs_supporter_to_pm")
        ),
        "bio_is_broadcaster_or_staff": _to_bool_int_or_none(
            bio.get("is_broadcaster_or_staff")
        ),
        "bio_photo_sets_json": photo_sets_json,
        "bio_social_medias_json": socials_json,
        "photo_set_cover_url": cover_url,
        "bio_full_json": json.dumps(bio, default=str),
        "bio_fetched_epoch": int(now),
        # Sighting metadata
        "last_online_epoch": 0,        # biocontext doesn't confirm broadcasting
        "last_source": "biocontext",
        "updated_epoch": int(now),
    }


def upsert_biocontext(
    conn: sqlite3.Connection,
    slug: str,
    biocontext: dict[str, Any],
    *,
    now: int,
) -> bool:
    """Persist a /api/biocontext/<slug>/ response into the meta DB.

    Touches (with COALESCE-on-non-null) every column we extract from
    biocontext, plus stashes the full raw response in bio_full_json
    so future-us can pull new fields without a re-crawl.

    Empty / falsy biocontext (network failure, account gone) is a
    no-op; the deep crawler treats biocontext-empty separately to
    flag the slug as gone via the dedicated upsert_status path.

    Returns True on write, False on skip.
    """
    if not biocontext or not isinstance(biocontext, dict):
        return False
    slug = (slug or "").strip()
    if not slug:
        return False
    values = _biocontext_row_values(biocontext, slug, now=now)
    # Build INSERT ... ON CONFLICT DO UPDATE that COALESCEs every
    # column except the always-overwrite ones (last_status_check_epoch,
    # bio_fetched_epoch, last_source, updated_epoch). last_online_epoch
    # we COALESCE-with-MAX so a higher value already there from a bulk
    # poll wins (biocontext doesn't confirm broadcasting).
    cols = list(values.keys())
    col_list = ",".join(cols)
    placeholders = ",".join(":" + c for c in cols)
    always_overwrite = {
        "last_status_check_epoch", "bio_fetched_epoch",
        "last_source", "updated_epoch",
    }
    parts: list[str] = []
    for c in cols:
        if c == "slug":
            continue
        if c == "last_online_epoch":
            # Don't lower a higher existing value -- a bulk poll's
            # observation wins.
            parts.append(f"{c}=MAX(COALESCE(excluded.{c},0), COALESCE({c},0))")
        elif c in always_overwrite:
            parts.append(f"{c}=excluded.{c}")
        else:
            parts.append(f"{c}=COALESCE(excluded.{c}, {c})")
    # cols/parts/placeholders are all derived from a fixed allowlist
    # (_EXPECTED_COLUMNS) and never accept user input, so the f-string
    # SQL build is safe; values bind via parameters.
    sql = (
        f"INSERT INTO models ({col_list}) VALUES ({placeholders})\n"  # noqa: S608
        f"ON CONFLICT(slug) DO UPDATE SET\n  " + ",\n  ".join(parts)
    )
    conn.execute(sql, values)
    return True


def label_prefix_for_row(row: dict[str, Any]) -> str:
    """Return a short HALO-red ``[GONE] `` prefix when ``last_room_status``
    is one of {banned, deleted, gone}; empty string otherwise.

    The prefix gets prepended to the offline-fav list label so accounts
    that are unlikely to ever come back are visually distinct from
    just-temporarily-offline models the user might want to keep
    waiting on.
    """
    status = (row.get("last_room_status") or "").strip().lower()
    if status in _STATUS_GONE:
        return "[COLOR FFff8080][GONE][/COLOR] "
    return ""


def partition_offline_slugs(
    slugs: list[str],
    online_slugs: frozenset[str] | set[str],
) -> list[str]:
    """Return the subset of ``slugs`` that aren't currently in the
    online cache. Order-preserving so a UI showing progress reflects
    the user's fav order.

    The deep-refresh handler walks this list at one slug per second
    or so, hitting per-slug AJAX + thumb HEAD for each.
    """
    return [s for s in slugs if s not in online_slugs]


# Render helpers (consumed by favs_views, addon_actions.tv_list) ---------- #


def image_for_row(row: dict[str, Any]) -> str | None:
    """Pick the best image URL we have cached for an offline row.

    Preference order:
      1. ``last_image_url`` -- full-size from the affiliate API.
      2. ``last_image_url_thumb`` -- 360x270 from the affiliate API.
      3. ``last_image_url_legacy`` -- ``img`` field from the per-gender
         roomlist API.
      4. (v0.7.24) ``photo_set_cover_url`` -- first profile photo set's
         cover from biocontext. Curated profile image, often higher
         quality than live thumbs and still meaningful when the model
         hasn't broadcast in months.
      5. (v0.7.24) Synthesized canonical static URL when biocontext /
         deep refresh confirmed the static thumb still serves
         (``thumb_available=1``). The static URL pattern is
         ``https://thumb.live.mmcdn.com/ri/<slug>.jpg``.

    Returns None if nothing is available so the caller can omit the
    image instead of feeding Kodi an empty path.
    """
    for key in ("last_image_url", "last_image_url_thumb",
                "last_image_url_legacy", "photo_set_cover_url"):
        v = row.get(key)
        if v:
            return str(v)
    # Synthesize the static canonical URL when deep refresh confirmed
    # the thumbnail still resolves. Requires a slug.
    if row.get("thumb_available") == 1:
        slug = row.get("slug")
        if slug:
            return f"https://thumb.live.mmcdn.com/ri/{slug}.jpg"
    return None


def _format_seconds_ago(seconds: int) -> str:
    """Mirror cb_listing._format_seconds_online so the Online line on
    live views and the Last seen line on offline views read with
    the same shape ("Xm" / "Xh Ym" / "Xd Yh"). Empty for under a
    minute (the model effectively still being polled).
    """
    if seconds < 60:
        return ""
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h {m}m"
    d, rem = divmod(seconds, 86400)
    h = rem // 3600
    return f"{d}d {h}h"


def last_seen_ago_label(row: dict[str, Any], now: float | int | None = None) -> str:
    """Return a "Xh Ym" / "Xd Yh" / "Xm" label or "" if we don't have
    enough info to render. The latter case covers a brand-new row that
    was just polled (no useful "ago"), a row missing
    ``last_online_epoch``, or a future-dated epoch (clock skew).
    """
    epoch = row.get("last_online_epoch") or 0
    try:
        epoch = int(epoch)
    except (TypeError, ValueError):
        return ""
    if epoch <= 0:
        return ""
    current = int(now if now is not None else time.time())
    delta = current - epoch
    if delta <= 0:
        return ""
    return _format_seconds_ago(delta)


def _coalesce_int(v: Any) -> int:
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def plot_for_offline_row(row: dict[str, Any], now: float | int | None = None) -> str:
    """Build the plot info pane (left side in Kodi list views) from a
    cached meta row, mirroring cb_listing.plot_for() so live and
    offline views read the same way. Adds a "Last seen: Xh Ym" line
    using last_online_epoch.

    Sparse rows (we only have a handful of fields cached so far) emit
    only the lines they have data for -- no empty "Location:" lines,
    no "Watching: 0" when we don't actually know.
    """
    parts: list[str] = []

    subject = row.get("last_subject")
    if subject:
        parts.append(str(subject))
    else:
        # v0.7.24: when no broadcast subject is cached but we have a
        # real_name from biocontext, surface that as the heading line
        # so the offline plot doesn't read empty.
        real_name = row.get("real_name")
        if real_name:
            parts.append(str(real_name))

    age = row.get("age")
    if age:
        parts.append(f"[COLOR FF00d4ff]Age:[/COLOR] {age}")

    location = row.get("location")
    if location:
        parts.append(f"[COLOR FF00d4ff]Location:[/COLOR] {location}")

    viewers = _coalesce_int(row.get("last_viewers"))
    if viewers > 0:
        parts.append(f"[COLOR FF00d4ff]Watching:[/COLOR] {viewers}")

    followers = _coalesce_int(row.get("last_followers"))
    if followers > 0:
        parts.append(f"[COLOR FF00d4ff]Followers:[/COLOR] {followers}")

    last_seen = last_seen_ago_label(row, now=now)
    if last_seen:
        parts.append(f"[COLOR FF00d4ff]Last seen:[/COLOR] {last_seen}")

    # v0.7.24: Last broadcast (from biocontext last_broadcast). For
    # never-seen-online favs this is often the only timestamp we
    # have, so it's our highest-value line for offline rendering.
    # Prefer the parsed epoch (renders relative time vs the user's
    # current "now"), fall back to the pre-formatted human string.
    last_bc_epoch = row.get("last_broadcast_epoch")
    if last_bc_epoch:
        bc_label = last_seen_ago_label(
            {"last_online_epoch": last_bc_epoch}, now=now,
        )
        if bc_label:
            parts.append(
                f"[COLOR FF00d4ff]Last broadcast:[/COLOR] {bc_label}"
            )
    elif row.get("last_broadcast_human"):
        parts.append(
            f"[COLOR FF00d4ff]Last broadcast:[/COLOR] "
            f"{row['last_broadcast_human']}"
        )

    # v0.7.24: when the deep refresh has touched a slug, render
    # "Verified: Xh ago" so the user knows how stale the cached
    # state is. Especially useful for never-seen-online favs whose
    # only sighting is the status check.
    check_epoch = row.get("last_status_check_epoch")
    if check_epoch:
        verified_label = last_seen_ago_label(
            {"last_online_epoch": check_epoch}, now=now,
        )
        if verified_label:
            parts.append(
                f"[COLOR FF00d4ff]Verified:[/COLOR] {verified_label}"
            )

    status = (row.get("last_room_status") or "").strip()
    if status:
        # Banned / deleted / gone get the warning red so the user sees
        # at a glance which favs aren't coming back. Other statuses
        # (offline, private, hidden, away, password_protected) use the
        # cyan accent so they read as ordinary metadata.
        status_color = ("FFff8080" if status.lower() in _STATUS_GONE
                        else "FF00d4ff")
        parts.append(
            f"[COLOR FF00d4ff]Status:[/COLOR] [COLOR {status_color}]{status}[/COLOR]"
        )

    tags_raw = row.get("last_tags_json")
    if tags_raw:
        try:
            tags = json.loads(tags_raw)
        except (TypeError, ValueError):
            tags = None
        if isinstance(tags, list) and tags:
            tag_str = ", ".join(f"#{t}" for t in tags)
            parts.append(f"[COLOR FF00ff88]{tag_str}[/COLOR]")

    return "\n".join(parts)
