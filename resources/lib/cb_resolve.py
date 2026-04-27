"""Pure resolver: slug -> ``Resolution``.

The TV loop and the playvid resolver both need the same answer to
"is this room live, and if so, what HLS URL?". This module is the
single place that knows how to ask. The network is injected via a
``fetch_html_func`` callback so the module stays pure-test-friendly.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from resources.lib.cb_dossier import parse_room_dossier
from resources.lib.cb_endpoints import room_url
from resources.lib.cb_models import Gender


# iPad-style UA matches what  uses; Chaturbate blocks default
# urllib UAs and strict bot UAs alike. Don't touch this casually.
_USER_AGENT = (
    "Mozilla/5.0 (iPad; CPU OS 16_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 "
    "Mobile/15E148 Safari/604.1"
)


_FetchFn = Callable[[str], str]


@dataclass(frozen=True)
class Resolution:
    """Result of ``resolve()``. Frozen so playvid_resolver / tv_loop can
    pass it around without aliasing concerns.

    ``headers`` is the set of request headers needed to actually fetch the
    HLS URL through ISA / the proxy. Always includes UA + Referer.
    """

    is_live: bool
    hls_source: str | None
    headers: dict[str, str] = field(default_factory=dict)
    gender: Gender = Gender.UNKNOWN


def resolve(slug: str, fetch_html_func: _FetchFn) -> Resolution:
    """Build a Resolution for ``slug``.

    1. Construct the room URL.
    2. Call ``fetch_html_func(url)``; the caller chooses the HTTP layer.
    3. Parse the dossier with ``cb_dossier.parse_room_dossier``.
    4. Return a Resolution with the parsed fields plus the headers
       ISA needs (UA + Referer matching the room).

    Lets exceptions from ``fetch_html_func`` propagate; the TV loop
    catches them at the outer level. If the fetch returns an empty
    string (offline page, redirect to login), we return an
    ``is_live=False`` Resolution with the same headers (so the caller
    can still act on a 'not playable' answer).
    """
    url = room_url(slug)
    html = fetch_html_func(url)
    parsed = parse_room_dossier(html)
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": url,
    }
    return Resolution(
        is_live=bool(parsed["is_live"]),
        hls_source=parsed["hls_source"],
        headers=headers,
        gender=parsed["gender"],
    )
