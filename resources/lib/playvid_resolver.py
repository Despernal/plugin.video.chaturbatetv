"""Slug -> playable ListItem orchestrator.

Bridges three layers without importing them tightly:

1. ``cb_resolve.resolve(slug, fetch_func)`` returns a ``Resolution``
   carrying ``is_live``, ``hls_source`` and the request headers.
2. ``hls_proxy.start_proxy(stream_url, room_url)`` returns a
   ``ProxyHandle`` whose ``master_url`` is the ``http://127.0.0.1:<port>``
   URL we hand to ISA.
3. ``xbmcgui.ListItem`` carries the Matrix+ ISA properties so ISA
   actually picks up the proxy URL and threads our headers through.

Both the resolve callback and the start_proxy callback are injected
so this module stays unit-testable without Kodi or live HTTP.

Critical Kodi gotchas (from PLANNING.md):

- ``setProperty('inputstream', 'inputstream.adaptive')`` is the
  Matrix+ key. The old key ``inputstreamaddon`` silently fails on
  Nexus+, which is exactly the kind of regression that would make
  playback look broken with no error log.
- ``manifest_type=hls`` tells ISA to parse our master.m3u8 as HLS.
- ``stream_headers`` and ``manifest_headers`` must BOTH carry the
  iPad UA + Referer string. ISA uses ``manifest_headers`` for the
  master fetch and ``stream_headers`` for chunklist+segment fetches.
  Set them to the same string for safety.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from resources.lib.cb_endpoints import room_url as build_room_url
from resources.lib.cb_resolve import Resolution


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #


@dataclass
class PlayvidResult:
    """Outcome of ``resolve_to_listitem``.

    On success: ``listitem`` is the populated xbmcgui.ListItem and
    ``proxy`` is the live ProxyHandle (caller must keep the ref so
    the proxy doesn't shut down before playback starts).

    On failure: both are None; caller passes the result straight to
    ``xbmcplugin.setResolvedUrl(handle, False, ...)`` so Kodi tears
    the playback attempt down cleanly.
    """

    success: bool
    listitem: Any | None
    proxy: Any | None


# Callback types - keep the resolver decoupled from the network and from
# the proxy's exact start signature. Tests pass plain lambdas.
_ResolveFn = Callable[[str], Resolution]
_StartProxyFn = Callable[[str, str], Any]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _build_isa_header_string(headers: dict[str, str]) -> str:
    """Encode request headers into the ``key=val&key=val`` form ISA expects.

    ISA's ``stream_headers`` / ``manifest_headers`` properties are
    parsed via the same urlencoded grammar as a query string. We use
    ``urlencode`` rather than hand-rolled concat so values containing
    ``&`` or ``=`` (rare in headers, but possible in custom UAs) are
    escaped properly.
    """
    return urlencode(headers)


def _build_listitem(
    name: str,
    master_url: str,
    headers: dict[str, str],
) -> Any:
    """Construct the xbmcgui.ListItem with all the ISA props set."""
    # Deferred import: keeps this module importable in pure-tests where
    # xbmcgui is mocked into sys.modules just-in-time.
    import xbmcgui

    li = xbmcgui.ListItem(label=name)
    # Path is the proxy URL; ISA reads it via getPlayingFile() too,
    # which the proxy's monitor thread will use later (Phase 4c) to
    # detect rapid clicks.
    li.setPath(master_url)
    li.setProperty("IsPlayable", "true")

    # Kodi uses MIME type to pre-route to ISA without sniffing the
    # response body. application/vnd.apple.mpegurl is the canonical
    # HLS MIME. Some test stubs don't implement these setters, so we
    # tolerate AttributeError without swallowing real bugs.
    if hasattr(li, "setMimeType"):
        li.setMimeType("application/vnd.apple.mpegurl")
    if hasattr(li, "setContentLookup"):
        li.setContentLookup(False)

    # The Matrix+ ISA properties. Order doesn't matter; we set them
    # all in one place so future maintenance edits hit one block.
    header_str = _build_isa_header_string(headers)
    li.setProperty("inputstream", "inputstream.adaptive")
    li.setProperty("inputstream.adaptive.manifest_type", "hls")
    li.setProperty("inputstream.adaptive.stream_headers", header_str)
    li.setProperty("inputstream.adaptive.manifest_headers", header_str)
    return li


def _default_resolve(slug: str) -> Resolution:
    """Resolve via the AJAX endpoint (Lesson 3). The HTML dossier path
    was unreliable - Chaturbate's page is JS-rendered and the
    ``initialRoomDossier`` blob is often missing, which produced
    is_live=False for live rooms and "Cannot download manifest" from
    ISA. The AJAX endpoint returns clean JSON with a stable shape.
    """
    from resources.lib import cb_client, logger
    from resources.lib.cb_resolve import resolve_ajax

    logger._log(f"playvid_resolver: resolve_ajax slug={slug!r}")
    res = resolve_ajax(slug, cb_client.fetch_room_status_json)
    logger._log(
        f"playvid_resolver: resolved slug={slug!r} is_live={res.is_live} "
        f"hls_present={bool(res.hls_source)}"
    )
    return res


def _default_start_proxy(stream_url: str, room_url: str) -> Any:
    from resources.lib import addon_settings
    from resources.lib.hls_proxy import start_proxy
    return start_proxy(
        stream_url=stream_url,
        room_url=room_url,
        port=addon_settings.isa_proxy_port(),
    )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def resolve_to_listitem(
    slug: str,
    name: str,
    resolve_func: _ResolveFn | None = None,
    start_proxy_func: _StartProxyFn | None = None,
) -> PlayvidResult:
    """Turn a slug into a fully-wired ListItem ready for setResolvedUrl.

    Args:
        slug: Chaturbate model slug, e.g. ``"alice"``.
        name: Human label for the ListItem.
        resolve_func: Callable[(slug)] -> Resolution. Defaults to
            cb_resolve.resolve with cb_client as the HTTP layer.
        start_proxy_func: Callable[(stream_url, room_url)] ->
            ProxyHandle. Defaults to hls_proxy.start_proxy.

    Returns:
        PlayvidResult. Success when the model is live and the proxy
        started cleanly; failure when offline, when the resolve call
        throws, or (TBD Phase 4c) when proxy startup races.
    """
    rf = resolve_func or _default_resolve
    sp = start_proxy_func or _default_start_proxy

    try:
        resolution = rf(slug)
    except Exception:
        # Network blip, parser miss, or upstream changed. Don't crash
        # the addon UI; let the caller report failure to Kodi.
        return PlayvidResult(success=False, listitem=None, proxy=None)

    if not resolution.is_live or not resolution.hls_source:
        return PlayvidResult(success=False, listitem=None, proxy=None)

    room_url = build_room_url(slug)
    try:
        proxy = sp(resolution.hls_source, room_url)
    except Exception:
        return PlayvidResult(success=False, listitem=None, proxy=None)

    listitem = _build_listitem(
        name=name or slug,
        master_url=proxy.master_url,
        headers=resolution.headers,
    )
    return PlayvidResult(success=True, listitem=listitem, proxy=proxy)
