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
from typing import Any


_SCHEMA_VERSION = 1


# Affiliate-API field shape uses ``image_url`` and ``image_url_360x270``.
# Roomlist-API field shape uses ``img``. We persist both into separate
# columns so neither overwrites the other and the renderer can prefer
# whichever it has.
_DDL = """
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
CREATE INDEX IF NOT EXISTS idx_models_last_online ON models(last_online_epoch);
CREATE INDEX IF NOT EXISTS idx_models_gender ON models(gender);
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def open_db(path: str) -> sqlite3.Connection:
    """Open (or create) the meta DB at ``path``. Schema is applied
    idempotently so this is safe to call on every addon startup.

    The connection is configured with WAL journal mode for read/write
    concurrency (TV loop polls write to it while a render thread reads)
    and with row_factory=Row so callers get column-name access.
    """
    conn = sqlite3.connect(path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('version', ?)",
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
