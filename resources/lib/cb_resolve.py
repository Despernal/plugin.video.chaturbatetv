"""Pure resolver: slug -> ``Resolution``.

The TV loop and the playvid resolver both need the same answer to
"is this room live, and if so, what HLS URL?". This module is the
single place that knows how to ask.

Two paths are exposed:

- ``resolve_ajax(slug, fetch_status_func)`` (preferred, since v0.4.3) -
  hits the JSON status endpoint. Reliable, cheap, returns a clean
  ``Resolution`` with ``is_live``, ``hls_source``, and the request
  headers ISA needs.
- ``resolve(slug, fetch_html_func)`` (legacy) - parses the HTML room
  page for the ``initialRoomDossier`` blob. Only kept because it's
  exercised by the cb_dossier tests. Production code uses the AJAX path.

Both fetchers are injected so the module stays pure-test-friendly (no
urllib, no network, no Kodi).
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
_StatusFn = Callable[[str], dict[str, object]]


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


def resolve_ajax(slug: str, fetch_status_func: _StatusFn) -> Resolution:
    """Build a Resolution via Chaturbate's clean JSON status endpoint.

    Preferred over :func:`resolve` because the AJAX endpoint
    (``POST /get_edge_hls_url_ajax/``) returns the room's live status
    and HLS URL as structured JSON, while the room HTML's
    ``initialRoomDossier`` blob is JS-rendered and not always present
    in the static page (Lesson 3).

    ``fetch_status_func(slug)`` is expected to return the parsed JSON
    dict from the AJAX endpoint, with at least the keys ``url`` and
    ``room_status``. Use ``cb_client.fetch_room_status_json`` as the
    real-network implementation; tests pass a stub.

    Live = ``room_status == "public"`` AND non-empty ``url``. An empty
    URL means "the room is up but the edge has not handed us a stream
    yet" - we treat as not playable to avoid handing ISA an empty
    string.

    Gender is not in the AJAX response; left as ``UNKNOWN`` here.
    Browse views populate gender from the listing JSON; TV mode and
    playvid only need is_live + hls_source.
    """
    from resources.lib import logger
    logger._log(f"cb_resolve.resolve_ajax: slug={slug!r}")
    url = room_url(slug)
    status = fetch_status_func(slug)
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": url,
    }
    hls = str(status.get("url") or "") if isinstance(status, dict) else ""
    room_status = str(status.get("room_status") or "") if isinstance(status, dict) else ""
    is_live = room_status == "public" and bool(hls)
    logger._log(
        f"cb_resolve.resolve_ajax: slug={slug!r} is_live={is_live} "
        f"room_status={room_status!r} hls_present={bool(hls)}"
    )
    return Resolution(
        is_live=is_live,
        hls_source=hls or None,
        headers=headers,
        gender=Gender.UNKNOWN,
    )
