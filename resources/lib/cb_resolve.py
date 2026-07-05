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

from resources.lib import browser_ua
from resources.lib.cb_dossier import parse_room_dossier
from resources.lib.cb_endpoints import room_url
from resources.lib.cb_models import Gender


# UA comes from browser_ua now: a fresh iPad Safari string, randomized per run,
# single source of truth (see browser_ua.py). Chaturbate blocks default/bot UAs
# so the pool stays iPad Safari; session_ua() keeps resolve + stream on the same
# UA within a run. Don't hardcode a UA here again.
_USER_AGENT = browser_ua.session_ua()


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
    # v0.7.59: True when the live-status fetch actually succeeded (a real 200,
    # live OR a clean offline). False only when we fell back to a safe default
    # after a network/blocked/malformed fetch -- the room's status is then
    # UNKNOWN, not confirmed offline. Defaults True so existing constructions
    # and the HTML resolve() path stay 'known'. Read by the TV loop so a
    # network-wide outage never poisons the offline blocklist.
    status_known: bool = True


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
    # v0.7.59: a status dict carries fetch_ok from cb_client.fetch_room_status_json.
    # Missing key -> assume known (conservative; preserves pre-0.7.59 behavior for
    # any stub/caller not setting it). A non-dict status means the fetch path
    # broke entirely -> unknown.
    status_known = bool(status.get("fetch_ok", True)) if isinstance(status, dict) else False
    logger._log(
        f"cb_resolve.resolve_ajax: slug={slug!r} is_live={is_live} "
        f"room_status={room_status!r} hls_present={bool(hls)} status_known={status_known}"
    )
    return Resolution(
        is_live=is_live,
        hls_source=hls or None,
        headers=headers,
        gender=Gender.UNKNOWN,
        status_known=status_known,
    )
