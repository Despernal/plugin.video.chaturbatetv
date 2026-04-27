"""Local favorites views: online + offline + a top-level menu.

Backed by ``favs_store`` (Phase 1) for persistence. Online vs offline
split uses a paginated bulk-fetch of the public room-list (Lesson 11);
falls back to per-slug AJAX polls only if the bulk path returns
nothing.

Tests inject ``fetch_func`` and ``store_path`` so we never poll the
real network or write to user-paths.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from resources.lib import (
    cb_client,
    cb_listing,
    ctxmenu,
    favs_store,
    kodi_helpers,
    tv_store,
)
from resources.lib.cb_endpoints import online_rooms_affiliate_url
from resources.lib.cb_models import Favorite, Gender, Model


# In-memory cache TTL: 30 seconds. Short enough that re-entering Online
# Favorites picks up newly-online models; long enough that paging
# through the result (Next page clicks within the same session) reuses
# the cache instead of hammering the API. Each fresh fetch is sub-second
# (single-call affiliate endpoint) so we can afford a short TTL.
_BULK_CACHE_TTL = 30.0

# NO disk cache: would only carry slugs (Model is heavy to serialize),
# which means a disk-cache HIT on a Kodi restart would render online
# favs WITHOUT thumbnails or plot info. The affiliate endpoint fetch
# is fast enough that doing a fresh call on first entry is the right
# tradeoff. Kodi-restart re-entry takes ~1s vs instant; thumbnails and
# plot info are worth that much.

# Filename used by older versions for an on-disk slug cache. We delete
# it on entry so a leftover from 0.6.0 - 0.6.5 doesn't poison the new
# slug-only-vs-model-rich semantics.
_BULK_CACHE_FILE = "bulk_live_cache.json"

# Affiliate watermarks ('s rotating array). The affiliate API
# requires a wm= param to return data; without one it hands back []. The
# watermark is the affiliate's tracking ID.  ships a rotating
# array so any single tracker doesn't get all the credit; we copy that
# pattern verbatim. This was already the user's de-facto behavior under
#  - swapping addons doesn't change the affiliate distribution.
_AFFILIATE_WATERMARKS = (
    "C9m5N", "tfZSl", "jQrKO", "5XO2a", "WXomN",
    "zM6MR", "Lb2aB", "cIbs3", "mnzQo", "N6TZA",
)


# How many favorites to render per directory page. Kodi's directory
# scroll is fine with thousands of entries, but rendering 1000+ rows
# with full ctxmenus on a Pi-class device blocks the UI thread for
# many seconds.
_FAVS_PER_PAGE = 50

_FetchFn = Callable[..., str]


# Kodi requires 8-char AARRGGBB hex; bare 6-char silently renders blank.
_GENDER_COLORS: dict[Gender, str] = {
    Gender.FEMALE: "FF00d4ff",
    Gender.MALE: "FF66e3ff",
    Gender.COUPLE: "FF00ff88",
    Gender.TRANS: "FFff0080",
    Gender.UNKNOWN: "FFc8e8f8",
}


def _color_label(label: str, gender: Gender) -> str:
    color = _GENDER_COLORS.get(gender, "FFc8e8f8")
    return f"[COLOR {color}]{label}[/COLOR]"


# In-memory bulk-fetch cache. Tuple is (timestamp, set_of_slugs).
_bulk_cache: tuple[float, set[str]] | None = None
# In-memory per-slug Model cache, populated alongside the slug set when
# we do a full network bulk-fetch. Used to enrich Online Favorites rows
# with thumbnails / viewer counts / plot info. NOT persisted to disk
# (Model carries enough fields that JSON-serializing it would bloat the
# cache file; on a Kodi restart we get the slug set but lose the thumbs
# until the next live bulk-refresh).
_bulk_models: dict[str, Model] = {}


def _bulk_disk_cache_path() -> Path:
    """Path of the legacy on-disk slug cache. Older versions wrote
    here; we delete any stale file on startup so it doesn't poison the
    new model-rich semantics."""
    return _favs_path().parent / _BULK_CACHE_FILE


def _delete_stale_disk_cache(cache_path: Path | None = None) -> None:
    """One-time cleanup: remove the legacy slug-only disk cache.

    Versions 0.6.3 - 0.6.5 cached the slug set on disk for 30 minutes.
    The new code populates a sibling ``_bulk_models`` dict for thumbnail
    rendering, but that dict is in-memory only; a disk-cache hit would
    therefore render online favs WITHOUT thumbnails until the next
    fresh fetch. Easier to drop the disk cache entirely now that the
    affiliate endpoint is sub-second.
    """
    path = cache_path if cache_path is not None else _bulk_disk_cache_path()
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        return


def _bulk_live_slugs(
    fetch_func: _FetchFn | None,
    *,
    now_func: Callable[[], float] = time.time,
    cache_path: Path | None = None,
) -> set[str] | None:
    """Return the set of all currently-live model slugs.

    Single-call to the affiliate endpoint, in-memory cached for 30s so
    paginating within an Online Favorites session doesn't refetch on
    every Next-page click. Re-entering Online Favorites after the TTL
    expires triggers a fresh fetch (sub-second).

    Returns None on network failure.
    """
    from resources.lib import logger
    global _bulk_cache
    nowt = now_func()
    if _bulk_cache is not None:
        ts, cached = _bulk_cache
        if nowt - ts < _BULK_CACHE_TTL:
            logger._log(
                f"favs_views._bulk_live_slugs: mem-cache HIT ts={ts:.1f} "
                f"slugs={len(cached)}"
            )
            return cached

    # Drop any leftover legacy disk cache so it doesn't accumulate.
    _delete_stale_disk_cache(cache_path)

    slugs: set[str] = set()
    models_by_slug: dict[str, Model] = {}
    # Single-call affiliate endpoint: returns ALL online rooms in one
    # GET (~5-10MB body). This is 's pattern - way faster than
    # walking 50 pages of the room-list endpoint.
    import random
    wm = random.choice(_AFFILIATE_WATERMARKS)
    url = online_rooms_affiliate_url(wm)
    logger._log(f"favs_views._bulk_live_slugs: single-call url={url}")
    try:
        body = cb_client.fetch_browse_page(url, fetch_func=fetch_func)
    except OSError as exc:
        logger._log(
            f"favs_views._bulk_live_slugs: NETWORK FAIL err={exc!r}"
        )
        return None
    models = cb_listing.parse_affiliate_onlinerooms(body)
    for m in models:
        slugs.add(m.slug)
        models_by_slug[m.slug] = m
    logger._log(
        f"favs_views._bulk_live_slugs: single-call got={len(slugs)}"
    )
    if not slugs:
        logger._log("favs_views._bulk_live_slugs: bulk returned 0 slugs")
        return None
    _bulk_cache = (nowt, slugs)
    # Stash the rich per-slug model data so online favs render with
    # thumbnails + viewer counts + plot info (mirrors browse_views).
    _bulk_models.clear()
    _bulk_models.update(models_by_slug)
    logger._log(
        f"favs_views._bulk_live_slugs: cached {len(slugs)} live slugs "
        f"(memory only, 30s TTL)"
    )
    return slugs


def _bulk_cache_clear(cache_path: Path | None = None) -> None:
    """Drop the in-memory bulk-fetch cache and any legacy on-disk cache."""
    global _bulk_cache
    _bulk_cache = None
    _bulk_models.clear()
    _delete_stale_disk_cache(cache_path)


def _favs_path() -> Path:
    """Default favs.json path under the addon's userdata.

    Resolved through xbmcvfs so LibreELEC's special:// protocols work.
    """
    try:
        import xbmcvfs
        base = xbmcvfs.translatePath(
            "special://profile/addon_data/plugin.video.chaturbatetv/")
        return Path(base) / "favs.json"
    except Exception:
        return Path.home() / ".kodi" / "userdata" / "addon_data" / \
            "plugin.video.chaturbatetv" / "favs.json"


def _dismiss_busy_dialog() -> None:
    """Close any lingering Kodi busy dialog. Some legacy code paths
    ('s pattern) leave it stuck open if the previous addon
    invocation timed out; dismissing on view entry clears it so the user
    isn't staring at a spinner over a working video."""
    try:
        import xbmc
        xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
        xbmc.executebuiltin("Dialog.Close(busydialog)")
    except Exception:
        return


def favs_menu(handle: int, store_path: Path | None = None,
              fetch_func: _FetchFn | None = None, **_params: Any) -> None:
    """Top-level Favorites menu: just the Online/Offline drill-downs.

    Renders instantly. We deliberately do NOT bulk-fetch here so opening
    Favorites with 1000+ rows doesn't block the UI thread for a multi-
    second remote scan. The bulk-fetch fires lazily when the user clicks
    Online or Offline, then is cached for 5 minutes so paginating within
    the result is instant.

    The label shows the local favorite count so the user has a
    breadcrumb without paying for a network call.
    """
    from resources.lib import logger
    _dismiss_busy_dialog()
    path = store_path if store_path is not None else _favs_path()
    favs = favs_store.load(path)
    logger._log(f"favs_views.favs_menu: total={len(favs)} (no network call)")
    kodi_helpers.add_dir(
        handle, f"Online Favorites ({len(favs)} total)", "favs_online",
    )
    kodi_helpers.add_dir(
        handle, f"Offline Favorites ({len(favs)} total)", "favs_offline",
    )
    kodi_helpers.end_directory(handle, content_type="videos")


def _render_favs(handle: int, favs: list[Favorite],
                 enrich_with_models: bool = False) -> None:
    """Add favorite entries with state-aware context menus.

    When ``enrich_with_models`` is True (online favs only - we have
    live data for them), we look up each slug in ``_bulk_models``
    and pass the room thumbnail + viewer count + plot info through
    to the ListItem just like browse_views does. Offline favs render
    as bare entries (no thumbnail or plot is available; we deliberately
    do NOT scan 1000+ slugs to fish out stale metadata).
    """
    data_dir = _favs_path().parent
    tv_entries = tv_store.load(data_dir / "tv.json")
    for f in favs:
        # Decorate the label with viewer count when we have the
        # live model data (matches browse_views.add_play_item shape).
        live_model = _bulk_models.get(f.slug) if enrich_with_models else None
        label = _color_label(f.name, f.gender)
        if live_model and live_model.viewers:
            label = f"{label} [{live_model.viewers}]"
        ctx = ctxmenu.build_ctxmenu(
            {"slug": f.slug, "name": f.name, "url": f.url},
            tv_entries=tv_entries,
            favs=favs,
        )
        kodi_helpers.add_play_item(
            handle, label, f.slug,
            image=(live_model.image if live_model else None) or None,
            plot=(live_model.plot if live_model else None) or None,
            ctx_items=ctx,
        )


def _coerce_page(value: Any) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _slice_page(items: list[Favorite], page: int) -> list[Favorite]:
    """Return the slice of favorites for the requested 1-indexed page."""
    start = (page - 1) * _FAVS_PER_PAGE
    return items[start:start + _FAVS_PER_PAGE]


def _classify_favs(favs: list[Favorite],
                   fetch_func: _FetchFn | None) -> tuple[list[Favorite], list[Favorite]]:
    """Split the local favorites list into (online, offline) using the
    cached bulk-fetch. If the bulk path fails, returns the full list as
    offline and an empty online list - we'd rather show the user
    everything than block forever on per-slug AJAX (which is what
    locked up  on a 1224-fav library).
    """
    online_slugs = _bulk_live_slugs(fetch_func)
    if online_slugs is None:
        return [], list(favs)
    online = [f for f in favs if f.slug in online_slugs]
    offline = [f for f in favs if f.slug not in online_slugs]
    return online, offline


def online_favs_view(handle: int, store_path: Path | None = None,
                     fetch_func: _FetchFn | None = None,
                     page: Any = 1,
                     **_params: Any) -> None:
    """Currently-live favorites, paginated."""
    from resources.lib import logger
    path = store_path if store_path is not None else _favs_path()
    favs = favs_store.load(path)
    p = _coerce_page(page)
    online, _offline = _classify_favs(favs, fetch_func)
    page_items = _slice_page(online, p)
    logger._log(
        f"favs_views.online_favs_view: total={len(favs)} live={len(online)} "
        f"page={p} showing={len(page_items)}"
    )
    _render_favs(handle, page_items, enrich_with_models=True)
    if (p * _FAVS_PER_PAGE) < len(online):
        kodi_helpers.add_dir(handle, f"Next page ({p + 1})",
                             "favs_online", page=p + 1)
    kodi_helpers.end_directory(handle, content_type="videos")


def offline_favs_view(handle: int, store_path: Path | None = None,
                      fetch_func: _FetchFn | None = None,
                      page: Any = 1,
                      **_params: Any) -> None:
    """Currently-offline favorites, paginated."""
    from resources.lib import logger
    path = store_path if store_path is not None else _favs_path()
    favs = favs_store.load(path)
    p = _coerce_page(page)
    _online, offline = _classify_favs(favs, fetch_func)
    page_items = _slice_page(offline, p)
    logger._log(
        f"favs_views.offline_favs_view: total={len(favs)} offline={len(offline)} "
        f"page={p} showing={len(page_items)}"
    )
    _render_favs(handle, page_items)
    if (p * _FAVS_PER_PAGE) < len(offline):
        kodi_helpers.add_dir(handle, f"Next page ({p + 1})",
                             "favs_offline", page=p + 1)
    kodi_helpers.end_directory(handle, content_type="videos")
