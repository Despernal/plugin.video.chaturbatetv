"""HTTP client for Chaturbate.

Thin wrapper around stdlib urllib that:

- Sends iPad-style headers Chaturbate doesn't block (default-UA gets a
  Cloudflare challenge; bot-flavoured UAs get blocked outright).
- Lets callers inject a ``fetch_func`` so the network is mockable from
  pytest with no hooks into urlopen.
- Hides the dossier HTML route AND the AJAX status route behind small,
  named functions so callers don't keep that knowledge.

The AJAX endpoint (``get_edge_hls_url_ajax``) is much cheaper than the
full HTML dossier when all you need is "is this room live?". The HTML
dossier is still useful when you want gender/viewers/etc, so we keep
both.
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
    """Stdlib urllib fetcher. Imported lazily so unit tests never touch network."""
    from urllib.request import Request, urlopen

    req = Request(url, data=body, headers=headers or {}, method=method)  # noqa: S310
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310
        raw: bytes = resp.read()
    return raw.decode("utf-8", errors="replace")


def _resolved(fetch_func: _FetchFn | None) -> _FetchFn:
    return fetch_func if fetch_func is not None else _default_fetch


def fetch_room_dossier(slug: str, fetch_func: _FetchFn | None = None) -> str:
    """Return the room's HTML page body. Exceptions propagate."""
    if not slug:
        raise ValueError("fetch_room_dossier requires a non-empty slug")
    fetch = _resolved(fetch_func)
    url = room_url(slug)
    headers = dict(HTTP_HEADERS_IPAD)
    headers["Referer"] = "https://chaturbate.com/"
    return fetch(url, body=None, headers=headers, method="GET")


def _safe_status_default() -> dict[str, Any]:
    return {
        "success": False,
        "url": "",
        "room_status": "offline",
        "hidden_message": "",
        "cmaf_edge": False,
    }


def fetch_room_status_json(slug: str, fetch_func: _FetchFn | None = None) -> dict[str, Any]:
    """Hit ``get_edge_hls_url_ajax`` for a cheap is_live + hls_source.

    Always returns a dict shaped like the AJAX response. On any failure
    (network, non-JSON body, Cloudflare HTML) returns a safe default
    with ``success=False`` and ``room_status='offline'`` so the caller
    can treat the room as not live without any extra checks.
    """
    if not slug:
        raise ValueError("fetch_room_status_json requires a non-empty slug")
    fetch = _resolved(fetch_func)
    body = urlencode({"room_slug": slug, "bandwidth": "high"}).encode("ascii")
    headers = dict(HTTP_HEADERS_IPAD)
    headers["X-Requested-With"] = "XMLHttpRequest"
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    headers["Referer"] = f"https://chaturbate.com/{slug}/"
    try:
        raw = fetch(_AJAX_URL, body=body, headers=headers, method="POST")
    except OSError:
        return _safe_status_default()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return _safe_status_default()
    if not isinstance(data, dict):
        return _safe_status_default()
    out = _safe_status_default()
    out.update({k: v for k, v in data.items() if k in out})
    return out


def fetch_browse_page(url: str, fetch_func: _FetchFn | None = None) -> str:
    """Fetch a category / search / listing HTML page."""
    if not url:
        raise ValueError("fetch_browse_page requires a non-empty url")
    fetch = _resolved(fetch_func)
    headers = dict(HTTP_HEADERS_IPAD)
    headers["Referer"] = "https://chaturbate.com/"
    return fetch(url, body=None, headers=headers, method="GET")


def is_model_live(slug: str, fetch_func: _FetchFn | None = None) -> bool:
    """Cheap "is this model live right now" check via the AJAX endpoint.

    Treats any failure (timeout, blocked, malformed JSON, missing url,
    non-public room_status) as "not live". The TV loop's behaviour
    around offline targets is the same regardless of the cause.
    """
    try:
        data = fetch_room_status_json(slug, fetch_func=fetch_func)
    except ValueError:
        raise
    except OSError:
        return False
    if not data.get("success"):
        return False
    if data.get("room_status") != "public":
        return False
    return bool(data.get("url"))
