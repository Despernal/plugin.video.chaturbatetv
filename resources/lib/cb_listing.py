"""Pure parser for Chaturbate room-list JSON.

The API at ``/api/ts/roomlist/room-list/`` returns a payload like::

    {
      "rooms": [{"username": ..., "gender": "f", "num_users": 1234, ...}, ...],
      "total_count": 50000,
      "all_rooms_count": 60000,
      ...
    }

This module turns that into a ``RoomListPage`` value: a list of
``Model`` plus pagination metadata. It is intentionally tolerant: a
single malformed room dict gets skipped, not raised.

No HTTP. The caller fetches the body, hands the string/bytes/dict to
:func:`parse_roomlist`. Tests can build the input dict by hand.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from resources.lib.cb_endpoints import BASE_URL
from resources.lib.cb_models import Gender, Model


# strip <a href="/tag/x/">#x</a> markup from ``subject`` strings so the
# Kodi list plot lines stay clean. The <a> tags wrap each tag word
# returned in ``subject``.
_ANCHOR_RE = re.compile(r"<a [^>]*>([^<]*)</a>", re.IGNORECASE)

# Strip inline #word hashtags from a room subject so they don't show up
# twice (the tag-line at the bottom of the plot already lists them in
# green). We only strip ASCII-word hashtag tokens; subject prose with a
# stray '#' that isn't a tag stays put.
_HASHTAG_RE = re.compile(r"#\w+")


@dataclass(frozen=True)
class RoomListPage:
    """One page of room-list JSON, parsed to our domain types."""
    models: list[Model]
    total_count: int
    all_rooms_count: int


def _payload_snippet(payload: str | bytes, n: int = 120) -> str:
    """Return the first N chars of a string/bytes payload as a
    readable snippet for diagnostic logs. v0.7.38 (audit pass #4
    HIGH #3): collapses 12 MB Cloudflare-HTML / rate-limit-page /
    login-redirect bodies into a leading marker that distinguishes
    them in cb_feature.log."""
    try:
        if isinstance(payload, bytes):
            text = payload[:n].decode("utf-8", errors="replace")
        else:
            text = payload[:n]
        # Collapse whitespace runs so the snippet fits on one log
        # line and the leading marker (<!DOCTYPE, {"error":, etc) is
        # the first thing visible.
        return " ".join(text.split())
    except Exception:
        return "<unreadable>"


def _to_int(v: Any, default: int = 0) -> int:
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _model_from_room(room: dict[str, Any]) -> Model | None:
    """Convert one room dict to a Model. Returns None on a missing slug.

    v0.7.35 dropped the ``label`` fallback for liveness -- ``label`` is
    a UI badge ("HD", "New", "Hot") not a state enum, and the affiliate
    parser doesn't fall back to it either. Symmetric handling now: only
    ``current_show`` decides liveness; missing/unknown -> not live.
    """
    slug = (room.get("username") or "").strip()
    if not slug:
        return None
    status = (room.get("current_show") or "").strip().lower()
    return Model(
        name=slug,
        slug=slug,
        url=f"{BASE_URL}/{slug}/",
        is_live=status == "public",
        viewers=_to_int(room.get("num_users"), 0),
        gender=Gender.from_str(room.get("gender")),
        image=str(room.get("img") or ""),
        plot=plot_for(room),
        status=status,
    )


def _model_from_affiliate_room(room: dict[str, Any]) -> Model | None:
    """Convert one affiliate-API room dict to a Model.

    Field shape differs from the room-list endpoint: ``image_url`` not
    ``img``, ``room_subject`` not ``subject``, ``num_users`` is the
    same.

    v0.7.31 correction: the affiliate endpoint returns rooms in EVERY
    broadcasting state -- public, hidden (paid show), private (1-on-1),
    away, password_protected. The previous comment ("only returns live
    rooms") was wrong. ``current_show`` carries the actual state. We
    parse it the same way as the room-list endpoint and only flag
    ``is_live=True`` when ``current_show == 'public'`` so the TV loop
    doesn't keep promoting hidden/private models we can't actually
    watch (they fall through to silent stub forever).
    """
    slug = (room.get("username") or room.get("slug") or "").strip()
    if not slug:
        return None
    status = (room.get("current_show") or "").strip().lower()
    return Model(
        name=slug,
        slug=slug,
        url=f"{BASE_URL}/{slug}/",
        is_live=status == "public",
        viewers=_to_int(room.get("num_users"), 0),
        gender=Gender.from_str(room.get("gender")),
        image=str(room.get("image_url") or ""),
        plot=plot_for(room),
        status=status,
    )


def parse_affiliate_onlinerooms(
    payload: list[Any] | str | bytes,
) -> list[Model]:
    """Parse the affiliate ``/affiliates/api/onlinerooms/?format=json&wm=XXX``
    response into our Model list.

    Unlike room-list (which is a paginated dict), this endpoint returns
    a flat JSON array of every currently-online model in a single call.
    Single-call = no pagination = the live-favs view becomes instant
    ('s pattern, ported wholesale).

    Tolerant: garbage input collapses to an empty list rather than
    raising; one malformed entry gets skipped, not propagated.
    """
    try:
        from resources.lib import logger
        _log: Any = logger._log
    except Exception:  # pragma: no cover - never raises
        def _log(_msg: object) -> None: ...
    if isinstance(payload, (str, bytes)):
        raw_body = payload
        try:
            payload = json.loads(raw_body)
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
            # v0.7.38 (audit pass #4 HIGH #3): log a snippet of the
            # body so Cloudflare interstitials, rate-limit HTML, and
            # login redirects are diagnosable. Pre-fix the failure
            # collapsed to "empty list" indistinguishable from
            # "no live rooms".
            snippet = _payload_snippet(raw_body)
            _log(
                "cb_listing.parse_affiliate_onlinerooms: JSON decode "
                f"failed err={exc!r} bytes={len(raw_body)} "
                f"snippet={snippet!r}"
            )
            return []
    if not isinstance(payload, list):
        _log(
            f"cb_listing.parse_affiliate_onlinerooms: payload not list "
            f"(type={type(payload).__name__})"
        )
        return []
    out: list[Model] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        m = _model_from_affiliate_room(item)
        if m is not None:
            out.append(m)
    _log(f"cb_listing.parse_affiliate_onlinerooms: rooms={len(out)}")
    return out


def parse_roomlist(payload: dict[str, Any] | str | bytes) -> RoomListPage:
    """Parse a room-list JSON payload.

    Accepts either a parsed dict, a JSON string, or bytes. Garbage
    inputs collapse to an empty page rather than raising; the caller
    can treat empty as "site/network hiccup, try later".
    """
    # Lazy import to keep this module importable without Kodi mocks.
    try:
        from resources.lib import logger
        _log: Any = logger._log
    except Exception:  # pragma: no cover - never raises
        def _log(_msg: object) -> None: ...
    if isinstance(payload, (str, bytes)):
        raw_body = payload
        try:
            payload = json.loads(raw_body)
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
            # v0.7.38: snippet logging so Cloudflare HTML / rate-
            # limit pages / login redirects are diagnosable.
            snippet = _payload_snippet(raw_body)
            _log(
                f"cb_listing.parse_roomlist: JSON decode failed "
                f"err={exc!r} bytes={len(raw_body)} snippet={snippet!r} "
                f"-> empty page"
            )
            return RoomListPage(models=[], total_count=0, all_rooms_count=0)
    if not isinstance(payload, dict):
        _log(
            f"cb_listing.parse_roomlist: payload not dict "
            f"(type={type(payload).__name__}) -> empty page"
        )
        return RoomListPage(models=[], total_count=0, all_rooms_count=0)

    rooms = payload.get("rooms") or []
    if not isinstance(rooms, list):
        rooms = []

    models: list[Model] = []
    for room in rooms:
        if not isinstance(room, dict):
            continue
        m = _model_from_room(room)
        if m is not None:
            models.append(m)

    page = RoomListPage(
        models=models,
        total_count=_to_int(payload.get("total_count"), 0),
        all_rooms_count=_to_int(payload.get("all_rooms_count"), 0),
    )
    _log(
        f"cb_listing.parse_roomlist: rooms={len(models)} "
        f"total_count={page.total_count} all_rooms_count={page.all_rooms_count}"
    )
    return page


def clean_subject(subject: str | None) -> str:
    """Strip <a> tag markup AND inline #word hashtags from ``subject``.

    The hashtags are removed because we render the room's tag list as a
    separate green line at the bottom of the plot; leaving them in the
    subject would show every tag twice.
    """
    if not subject:
        return ""
    no_anchors = _ANCHOR_RE.sub(r"\1", subject)
    no_tags = _HASHTAG_RE.sub("", no_anchors)
    # Collapse whitespace introduced by the strip.
    return re.sub(r"\s+", " ", no_tags).strip()


def _format_seconds_online(seconds: int) -> str:
    """Render the affiliate API's ``seconds_online`` as a short human
    string. Empty string for "barely online yet" so the plot omits the
    line. Hours+minutes when under a day; days+hours when at or above a
    day (minutes drop because at that scale the user cares about
    days/hours not the trailing minutes).

    Examples: 0 -> "", 45 -> "", 60 -> "1m", 6697 -> "1h 51m",
    86400 -> "1d 0h", 3*86400 + 14*3600 + 59*60 -> "3d 14h".
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


def _seconds_online_from_room(room: dict[str, Any], now: float | None = None) -> int:
    """Pick the on-air duration from a room dict.

    Two API shapes feed the addon: the affiliate-onlinerooms endpoint
    (favs / TV mode bulk) returns ``seconds_online`` already computed
    by the server; the per-gender ``/api/ts/roomlist/`` endpoint (Top,
    Female, Male, Couple, Trans, Search) returns ``start_timestamp``
    (Unix epoch of broadcast start) instead. This helper unifies them
    so plot_for produces the same "Online:" line regardless of which
    endpoint the room came from.

    ``seconds_online`` wins when present (server-computed, no local
    clock skew). Otherwise we derive from ``start_timestamp`` and
    ``now``. Returns 0 on missing or future-dated start_timestamp so
    plot_for can omit the line cleanly.
    """
    s = _to_int(room.get("seconds_online"), 0)
    if s > 0:
        return s
    start_ts = _to_int(room.get("start_timestamp"), 0)
    if start_ts <= 0:
        return 0
    current = int(now if now is not None else time.time())
    delta = current - start_ts
    return delta if delta > 0 else 0


def plot_for(room: dict[str, Any], now: float | None = None) -> str:
    """Build a Kodi plot line for a list item from a room dict.

    Format mirrors the  layout (Subject / Age / Location /
    Watching / Followers / Online / Tags) but uses HALO cyan accents
    instead of 's deeppink, and HALO green for the tag line.

    ``now`` is exposed for tests; production callers leave it at None
    so the helper falls back to ``time.time()``.
    """
    age = room.get("display_age") or "Unknown"
    location = room.get("location") or ""
    viewers = _to_int(room.get("num_users"), 0)
    followers = _to_int(room.get("num_followers"), 0)
    online_str = _format_seconds_online(_seconds_online_from_room(room, now=now))
    subject = clean_subject(room.get("subject") or room.get("room_subject"))
    parts: list[str] = []
    if subject:
        parts.append(subject)
    parts.append(f"[COLOR FF00d4ff]Age:[/COLOR] {age}")
    if location:
        parts.append(f"[COLOR FF00d4ff]Location:[/COLOR] {location}")
    parts.append(f"[COLOR FF00d4ff]Watching:[/COLOR] {viewers}")
    parts.append(f"[COLOR FF00d4ff]Followers:[/COLOR] {followers}")
    if online_str:
        parts.append(f"[COLOR FF00d4ff]Online:[/COLOR] {online_str}")
    tags = room.get("tags") or []
    if isinstance(tags, list) and tags:
        tag_str = ", ".join(f"#{t}" for t in tags)
        parts.append(f"[COLOR FF00ff88]{tag_str}[/COLOR]")
    return "\n".join(parts)
