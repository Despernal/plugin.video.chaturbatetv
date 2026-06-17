"""HTTP client for Chaturbate.

Thin wrapper around stdlib urllib that:

- Sends iPad-style headers Chaturbate doesn't block (default-UA gets a
  Cloudflare challenge; bot-flavoured UAs get blocked outright).
- Lets callers inject a ``fetch_func`` so the network is mockable from
  pytest with no hooks into urlopen.
- Hides the JSON listing routes (``/api/ts/roomlist/``,
  ``/affiliates/api/onlinerooms/``) and the AJAX status route
  (``/get_edge_hls_url_ajax/``) behind small, named functions so callers
  don't keep that knowledge.

The AJAX endpoint is the cheap "is this slug live now?" check; the
listing routes return rich room data (image, plot, viewers) for browse
and bulk-live-set use.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from resources.lib.cb_endpoints import room_url

_AJAX_URL = "https://chaturbate.com/get_edge_hls_url_ajax/"


HTTP_HEADERS_IPAD: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (iPad; CPU OS 16_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "navigate",
}


_FetchFn = Callable[..., str]


def _default_fetch(url: str, body: bytes | None = None,
                   headers: dict[str, str] | None = None,
                   method: str = "GET", timeout: float = 15.0) -> str:
    """Stdlib urllib fetcher. Imported lazily so unit tests never touch network.

    v0.7.39 (audit pass #5 HIGH, agent 2): all URL log lines route
    through ``hls_proxy._redact_url`` to strip query strings before
    they hit cb_feature.log. Pre-fix, any future caller passing a
    JWT-bearing URL (CDN sessions, signed Akamai tokens) would leak
    the secret into a world-readable log file.
    """
    from urllib.request import Request, urlopen

    from resources.lib import logger
    from resources.lib.hls_proxy import _redact_url

    safe_url = _redact_url(url)
    logger._log(
        f"cb_client._default_fetch: {method} {safe_url} "
        f"body_len={len(body) if body else 0}"
    )
    req = Request(url, data=body, headers=headers or {}, method=method)  # noqa: S310
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw: bytes = resp.read()
            status = getattr(resp, "status", None)
    except Exception as exc:
        logger._log(
            f"cb_client._default_fetch: FAIL {method} {safe_url} "
            f"err={exc!r}"
        )
        raise
    text = raw.decode("utf-8", errors="replace")
    logger._log(
        f"cb_client._default_fetch: OK {method} {safe_url} "
        f"status={status} bytes={len(raw)}"
    )
    return text


def _resolved(fetch_func: _FetchFn | None) -> _FetchFn:
    return fetch_func if fetch_func is not None else _default_fetch


def fetch_room_dossier(slug: str, fetch_func: _FetchFn | None = None) -> str:
    """Return the room's HTML page body. Exceptions propagate."""
    if not slug:
        raise ValueError("fetch_room_dossier requires a non-empty slug")
    from resources.lib import logger
    logger._log(f"cb_client.fetch_room_dossier: slug={slug!r}")
    fetch = _resolved(fetch_func)
    url = room_url(slug)
    headers = dict(HTTP_HEADERS_IPAD)
    headers["Referer"] = "https://chaturbate.com/"
    body = fetch(url, body=None, headers=headers, method="GET")
    logger._log(
        f"cb_client.fetch_room_dossier: slug={slug!r} response_bytes={len(body)}"
    )
    return body


def _safe_status_default() -> dict[str, Any]:
    return {
        "success": False,
        "url": "",
        "room_status": "offline",
        "hidden_message": "",
        "cmaf_edge": False,
        # v0.7.59: False on every failure fallback (network OSError, Cloudflare
        # HTML, non-dict JSON). room_status='offline' here is a SAFE DEFAULT,
        # not a confirmed answer -- the resolve chain reads fetch_ok so a
        # network-wide outage can't poison the TV offline blocklist.
        "fetch_ok": False,
    }


_BIOCONTEXT_URL_TMPL = "https://chaturbate.com/api/biocontext/{slug}/"


def fetch_biocontext(slug: str) -> dict[str, Any]:
    """Pull /api/biocontext/<slug>/ -- the breakthrough endpoint that
    returns a model's full public profile in one shot.

    Critically requires the ``Referer: https://chaturbate.com/p/<slug>/``
    header; without it CB returns 404. With it: HTTP 200 and a JSON
    body shaped like
    ``{follower_count, location, real_name, last_broadcast,
    time_since_last_broadcast, display_birthday, about_me, wish_list,
    fan_club_cost, performer_has_fanclub, interested_in,
    display_age, sex, subgender, room_status, photo_sets,
    social_medias, ...}`` -- enough to populate the offline-favs
    view with rich metadata even for models we've NEVER seen
    broadcasting.

    Return shapes:

    - HTTP 200 + valid JSON: the populated profile dict.
    - HTTP 404: ``{"_http_404": True}`` -- the profile page literally
      doesn't exist, which means the account was deleted or banned.
      Callers (deep_refresh, refresh_one_model) treat this as a hard
      "gone" signal and stamp last_room_status accordingly.
    - HTTP 401 / network error / non-JSON / non-dict: ``{}`` -- the
      account may exist (private models 401, edge timeouts blip),
      so callers fall back to the cheap AJAX status + thumb HEAD
      path instead of marking gone.
    """
    import urllib.error
    from urllib.request import Request, urlopen

    from resources.lib import logger

    if not slug:
        return {}
    url = _BIOCONTEXT_URL_TMPL.format(slug=slug)
    headers = dict(HTTP_HEADERS_IPAD)
    headers["Referer"] = f"https://chaturbate.com/p/{slug}/"
    headers["X-Requested-With"] = "XMLHttpRequest"
    headers["Accept"] = "application/json"
    headers["Cookie"] = "cb_legacy=1; agreeterms=1"
    req = Request(url, headers=headers, method="GET")  # noqa: S310
    # v0.7.39 (audit pass #5 LOW, agent 3): cap the body to defend
    # against a malicious / MitM'd biocontext returning multi-MB
    # payloads that fill the model_meta DB (bio_full_json is stored
    # verbatim in sqlite). 256 KB is ~5x what a real biocontext is
    # (~50 KB max observed); anything larger is hostile.
    _BIOCONTEXT_MAX_BYTES = 256 * 1024
    try:
        with urlopen(req, timeout=12.0) as resp:  # noqa: S310
            raw: bytes = resp.read(_BIOCONTEXT_MAX_BYTES + 1)
            if len(raw) > _BIOCONTEXT_MAX_BYTES:
                logger._log(
                    f"cb_client.fetch_biocontext: BODY_TOO_LARGE "
                    f"slug={slug!r} bytes>{_BIOCONTEXT_MAX_BYTES}; "
                    f"refusing to parse"
                )
                return {}
    except urllib.error.HTTPError as exc:
        logger._log(
            f"cb_client.fetch_biocontext: FAIL slug={slug!r} err={exc!r}"
        )
        if exc.code == 404:
            return {"_http_404": True}
        return {}
    except Exception as exc:
        logger._log(f"cb_client.fetch_biocontext: FAIL slug={slug!r} err={exc!r}")
        return {}
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError, RecursionError):
        logger._log(
            f"cb_client.fetch_biocontext: NON_JSON slug={slug!r} bytes={len(raw)}"
        )
        return {}
    if not isinstance(data, dict):
        return {}
    logger._log(
        f"cb_client.fetch_biocontext: OK slug={slug!r} "
        f"keys={len(data)} room_status={data.get('room_status')!r}"
    )
    return data


def head_thumb(slug: str) -> int:
    """HEAD the static thumbnail URL for ``slug``; return the HTTP
    status code, or 0 on network error.

    Used by the v0.7.22 deep-refresh path to determine whether a
    model's account still exists and has a cached thumbnail. CB
    serves the LAST thumbnail at this URL even after the model goes
    offline, so a 200 means "account is alive" while a 404 means
    "thumbnail never cached, account likely deleted/banned".

    Network errors return 0 -- caller should treat 0 as "couldn't
    check, try again next refresh."
    """
    from urllib.request import Request, urlopen

    from resources.lib import logger

    if not slug:
        return 0
    url = f"https://thumb.live.mmcdn.com/ri/{slug}.jpg"
    headers = dict(HTTP_HEADERS_IPAD)
    req = Request(url, headers=headers, method="HEAD")  # noqa: S310
    try:
        with urlopen(req, timeout=10.0) as resp:  # noqa: S310
            status = int(getattr(resp, "status", 0) or 0)
    except Exception as exc:
        logger._log(f"cb_client.head_thumb: FAIL slug={slug!r} err={exc!r}")
        return 0
    return status


def fetch_room_status_json(slug: str, fetch_func: _FetchFn | None = None) -> dict[str, Any]:
    """Hit ``get_edge_hls_url_ajax`` for a cheap is_live + hls_source.

    Always returns a dict shaped like the AJAX response. On any failure
    (network, non-JSON body, Cloudflare HTML) returns a safe default
    with ``success=False`` and ``room_status='offline'`` so the caller
    can treat the room as not live without any extra checks.
    """
    if not slug:
        raise ValueError("fetch_room_status_json requires a non-empty slug")
    from resources.lib import logger
    logger._log(f"cb_client.fetch_room_status_json: slug={slug!r}")
    fetch = _resolved(fetch_func)
    body = urlencode({"room_slug": slug, "bandwidth": "high"}).encode("ascii")
    headers = dict(HTTP_HEADERS_IPAD)
    headers["X-Requested-With"] = "XMLHttpRequest"
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    headers["Referer"] = f"https://chaturbate.com/{slug}/"
    try:
        raw = fetch(_AJAX_URL, body=body, headers=headers, method="POST")
    except OSError as exc:
        logger._log(
            f"cb_client.fetch_room_status_json: NETWORK FAIL slug={slug!r} err={exc!r}"
        )
        return _safe_status_default()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger._log(
            f"cb_client.fetch_room_status_json: NON_JSON slug={slug!r} bytes={len(raw)}"
        )
        return _safe_status_default()
    if not isinstance(data, dict):
        logger._log(
            f"cb_client.fetch_room_status_json: NOT_DICT slug={slug!r}"
        )
        return _safe_status_default()
    # v0.7.35 (audit agent 1 MED): drop the whitelist filter --
    # ``out.update({k: v for k, v in data.items() if k in out})``
    # silently discarded any field CB added past the original five
    # default keys. Future state-classification (e.g., a new
    # ``banned`` or ``deleted`` marker) would have hit the same
    # "field not present" foot-gun that bit v0.7.31. Pass the full
    # response through; only fall back to defaults for missing keys.
    out = _safe_status_default()
    out.update(data)
    # We parsed a real 200 JSON dict (live OR a clean offline): the status is
    # KNOWN. Set after update() so a stray fetch_ok in the response can't lie.
    out["fetch_ok"] = True
    logger._log(
        f"cb_client.fetch_room_status_json: slug={slug!r} success={out.get('success')} "
        f"status={out.get('room_status')!r} hls_present={bool(out.get('url'))}"
    )
    return out


def fetch_browse_page(url: str, fetch_func: _FetchFn | None = None) -> str:
    """Fetch a JSON listing page (room-list or affiliate-onlinerooms).

    Despite the name (kept for backwards compat), the response body is
    JSON, not HTML. Browse views and the bulk live-set fetcher both use
    this; callers parse via ``cb_listing.parse_roomlist`` or
    ``cb_listing.parse_affiliate_onlinerooms`` depending on the URL.
    """
    if not url:
        raise ValueError("fetch_browse_page requires a non-empty url")
    from resources.lib import logger
    from resources.lib.hls_proxy import _redact_url
    safe_url = _redact_url(url)
    logger._log(f"cb_client.fetch_browse_page: url={safe_url}")
    fetch = _resolved(fetch_func)
    headers = dict(HTTP_HEADERS_IPAD)
    headers["Referer"] = "https://chaturbate.com/"
    body = fetch(url, body=None, headers=headers, method="GET")
    logger._log(
        f"cb_client.fetch_browse_page: url={safe_url} bytes={len(body)}"
    )
    return body


def is_model_live(slug: str, fetch_func: _FetchFn | None = None) -> bool:
    """Cheap "is this model live right now" check via the AJAX endpoint.

    Treats any failure (timeout, blocked, malformed JSON, missing url,
    non-public room_status) as "not live". The TV loop's behaviour
    around offline targets is the same regardless of the cause.

    v0.7.35 (audit agent 1 MED): dropped the ``success`` field check.
    ``cb_resolve.resolve_ajax`` already classifies live by
    ``room_status == 'public' and bool(hls)`` and not the ``success``
    flag -- the inconsistency could mask a live model in
    ``is_model_live`` while ``resolve_ajax`` saw it as live, leading
    to mark-offline-loop symptoms. Now the two functions agree.
    """
    try:
        data = fetch_room_status_json(slug, fetch_func=fetch_func)
    except ValueError:
        raise
    except OSError:
        return False
    if data.get("room_status") != "public":
        return False
    return bool(data.get("url"))
