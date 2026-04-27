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
from dataclasses import dataclass
from typing import Any

from resources.lib.cb_endpoints import BASE_URL
from resources.lib.cb_models import Gender, Model


# strip <a href="/tag/x/">#x</a> markup from ``subject`` strings so the
# Kodi list plot lines stay clean. The <a> tags wrap each tag word
# returned in ``subject``.
_ANCHOR_RE = re.compile(r"<a [^>]*>([^<]*)</a>", re.IGNORECASE)


@dataclass(frozen=True)
class RoomListPage:
    """One page of room-list JSON, parsed to our domain types."""
    models: list[Model]
    total_count: int
    all_rooms_count: int


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
    """Convert one room dict to a Model. Returns None on a missing slug."""
    slug = (room.get("username") or "").strip()
    if not slug:
        return None
    label = (room.get("current_show") or room.get("label") or "").lower()
    return Model(
        name=slug,
        slug=slug,
        url=f"{BASE_URL}/{slug}/",
        is_live=label == "public",
        viewers=_to_int(room.get("num_users"), 0),
        gender=Gender.from_str(room.get("gender")),
    )


def parse_roomlist(payload: dict[str, Any] | str | bytes) -> RoomListPage:
    """Parse a room-list JSON payload.

    Accepts either a parsed dict, a JSON string, or bytes. Garbage
    inputs collapse to an empty page rather than raising; the caller
    can treat empty as "site/network hiccup, try later".
    """
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return RoomListPage(models=[], total_count=0, all_rooms_count=0)
    if not isinstance(payload, dict):
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

    return RoomListPage(
        models=models,
        total_count=_to_int(payload.get("total_count"), 0),
        all_rooms_count=_to_int(payload.get("all_rooms_count"), 0),
    )


def clean_subject(subject: str | None) -> str:
    """Strip <a> tag markup from ``subject``; return clean inner text."""
    if not subject:
        return ""
    return _ANCHOR_RE.sub(r"\1", subject).strip()


def plot_for(room: dict[str, Any]) -> str:
    """Build a Kodi plot line for a list item from a room dict.

    Format mirrors the  layout (Subject / Age / Location /
    Watching / Followers / Tags) but uses HALO cyan accents instead of
    's deeppink, and HALO green for the tag line.
    """
    age = room.get("display_age") or "Unknown"
    location = room.get("location") or ""
    viewers = _to_int(room.get("num_users"), 0)
    followers = _to_int(room.get("num_followers"), 0)
    subject = clean_subject(room.get("subject") or room.get("room_subject"))
    parts: list[str] = []
    if subject:
        parts.append(subject)
    parts.append(f"[COLOR FF00d4ff]Age:[/COLOR] {age}")
    if location:
        parts.append(f"[COLOR FF00d4ff]Location:[/COLOR] {location}")
    parts.append(f"[COLOR FF00d4ff]Watching:[/COLOR] {viewers}")
    parts.append(f"[COLOR FF00d4ff]Followers:[/COLOR] {followers}")
    tags = room.get("tags") or []
    if isinstance(tags, list) and tags:
        tag_str = ", ".join(f"#{t}" for t in tags)
        parts.append(f"[COLOR FF00ff88]{tag_str}[/COLOR]")
    return "\n".join(parts)
