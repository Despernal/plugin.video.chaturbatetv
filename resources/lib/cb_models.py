"""Pure data primitives for the Chaturbate TV addon.

Holds the dataclasses that flow between modules:

- ``Gender`` (enum) - broadcaster gender taxonomy.
- ``Model``         - a Chaturbate room snapshot from a dossier.
- ``TVEntry``       - a row in tv.json (priority list).
- ``Favorite``      - a row in the local favorites file.

No Kodi imports, no I/O, no network. Importing this module from any pure
test should never touch the filesystem or HTTP.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


_CHATURBATE_BASE = "https://chaturbate.com/"


class Gender(Enum):
    """Broadcaster gender. Values match the lowercase tokens we use on disk
    (in tv.json, favs.json) so serialization is just ``g.value``.
    """

    FEMALE = "female"
    MALE = "male"
    COUPLE = "couple"
    TRANS = "trans"
    UNKNOWN = "unknown"

    @classmethod
    def from_str(cls, raw: str | None) -> Gender:
        """Best-effort parse from Chaturbate's short codes (``f``/``m``/``c``/``t``)
        or the long names. Anything unrecognised becomes ``UNKNOWN``.
        """
        if not raw:
            return cls.UNKNOWN
        token = raw.strip().lower()
        # Short codes from the dossier broadcaster_gender field.
        short = {
            "f": cls.FEMALE,
            "m": cls.MALE,
            "c": cls.COUPLE,
            "s": cls.TRANS,  # Chaturbate uses 's' for shemale historically
            "t": cls.TRANS,
        }
        if token in short:
            return short[token]
        # Try the enum's own long names.
        try:
            return cls(token)
        except ValueError:
            return cls.UNKNOWN


@dataclass(frozen=True)
class Model:
    """One model snapshot. ``frozen`` because we treat these as values; if a
    fact about the room changes, we build a fresh Model.

    ``status`` carries the lowercase Chaturbate state ("public", "hidden",
    "private", "away", "password protected", "offline" -- empty when the
    parser couldn't determine it). ``is_live`` is the playable derivative:
    only "public" with a real HLS gets True. Two fields rather than one
    so views can render non-public broadcasters with a state-prefix
    label (`[HIDDEN]` etc) rather than dropping them silently.
    """

    name: str
    slug: str
    url: str
    is_live: bool
    viewers: int
    gender: Gender = Gender.UNKNOWN
    image: str = ""
    plot: str = ""
    status: str = ""

    @classmethod
    def from_dossier(cls, dossier: dict[str, Any]) -> Model:
        """Build a Model from a parsed ``initialRoomDossier`` dict.

        Tolerates missing keys; missing ``username`` defaults to empty
        string (caller can decide whether that's an error). ``is_live``
        requires BOTH ``room_status == "public"`` AND a non-empty
        ``hls_source``. The HLS-only check we used pre-v0.7.32 was the
        sibling of the v0.7.31 affiliate-parser bug -- a stale or
        cached HLS URL on a now-private/away room would have flagged
        the model live and triggered the same silent-stub-loop family.
        """
        username = str(dossier.get("username") or "")
        hls = dossier.get("hls_source") or ""
        room_status = str(dossier.get("room_status") or "").lower()
        is_live = (room_status == "public") and bool(hls)
        viewers_raw = dossier.get("num_users")
        try:
            viewers = int(viewers_raw) if viewers_raw is not None else 0
        except (TypeError, ValueError):
            viewers = 0
        gender = Gender.from_str(dossier.get("broadcaster_gender"))
        return cls(
            name=username,
            slug=username,
            url=_CHATURBATE_BASE + username + "/" if username else "",
            is_live=is_live,
            viewers=viewers,
            gender=gender,
            status=room_status,
        )


@dataclass(frozen=True)
class TVEntry:
    """A row in ``tv.json``. Any int priority is accepted at the dataclass
    level; clamping into a UI range happens at the AddToTV/EditTVPriority
    boundary.
    """

    name: str
    url: str
    priority: int = field(default=1)

    def __post_init__(self) -> None:
        # Strict int check so a string "10" does not silently land in a TV row.
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise TypeError(
                f"TVEntry.priority must be int, got {type(self.priority).__name__}"
            )


@dataclass(frozen=True)
class Favorite:
    """A locally-stored favorite. ``slug`` is the natural key; we de-dupe by
    slug in favs_store.add.
    """

    name: str
    url: str
    slug: str
    gender: Gender = Gender.UNKNOWN
