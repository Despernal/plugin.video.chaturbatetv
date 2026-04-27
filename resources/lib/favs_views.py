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
from resources.lib.cb_endpoints import top_cams_url
from resources.lib.cb_models import Favorite, Gender


# In-memory cache TTL: 5 minutes. Long enough that paginating through
# a 1000+ favorite library doesn't re-walk the room list, short enough
# that a category change picks up the latest live status.
_BULK_CACHE_TTL = 300.0
# Disk-cache TTL: 30 minutes. Survives Kodi restarts so re-entering
# Favorites after a reboot is instant rather than triggering a fresh
# 20-page scan against chaturbate.
_BULK_DISK_TTL = 1800.0
# Filename for the on-disk cache, lives next to favs.json so it shares
# the same userdata directory.
_BULK_CACHE_FILE = "bulk_live_cache.json"
# Paginate chaturbate's room-list at 500/page, the API max.
_BULK_PAGE_LIMIT = 500
# Hard ceiling on pages so we don't spin if the API misbehaves.
_BULK_MAX_PAGES = 20
# Politeness delay between page fetches so we don't thunder-herd the
# room-list endpoint.
_BULK_PAGE_DELAY_S = 0.2

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


def _bulk_disk_cache_path() -> Path:
    """Cache file path, sibling of favs.json under the addon's userdata."""
    return _favs_path().parent / _BULK_CACHE_FILE


def _load_bulk_disk_cache(
    nowt: float,
    cache_path: Path | None = None,
) -> tuple[float, set[str]] | None:
    """Read the disk cache; return ``(timestamp, slugs)`` if hot.

    Hot = file exists, the JSON parses, and the timestamp is within
    ``_BULK_DISK_TTL`` seconds. Any failure (missing file, bad JSON,
    OSError) returns None so callers fall back to a fresh fetch.
    """
    import json
    path = cache_path if cache_path is not None else _bulk_disk_cache_path()
    try:
        body = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(body)
        ts = float(data["timestamp"])
        slugs = set(data["slugs"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if nowt - ts > _BULK_DISK_TTL:
        return None
    return ts, slugs


def _save_bulk_disk_cache(
    nowt: float,
    slugs: set[str],
    cache_path: Path | None = None,
) -> None:
    """Persist the live-slugs set so a Kodi restart reuses it. Best-effort."""
    import json
    path = cache_path if cache_path is not None else _bulk_disk_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"timestamp": nowt, "slugs": sorted(slugs)}),
            encoding="utf-8",
        )
        tmp.replace(path)  # atomic on POSIX
    except OSError:
        return


def _bulk_live_slugs(
    fetch_func: _FetchFn | None,
    *,
    now_func: Callable[[], float] = time.time,
    cache_path: Path | None = None,
) -> set[str] | None:
    """Return the set of all currently-live model slugs.

    Three-tier cache:

    1. In-memory cache (``_BULK_CACHE_TTL``=5min). Instant hit during
       the same Kodi session.
    2. On-disk cache (``_BULK_DISK_TTL``=30min). Survives Kodi restarts;
       re-entering Favorites after a reboot reuses the warm set.
    3. Fresh fetch from the public room-list API in pages of 500 with
       a politeness pacer between pages.

    Returns None on hard failure (no rooms came back at all).
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
    disk_hit = _load_bulk_disk_cache(nowt, cache_path)
    if disk_hit is not None:
        ts, cached = disk_hit
        _bulk_cache = (ts, cached)
        logger._log(
            f"favs_views._bulk_live_slugs: disk-cache HIT ts={ts:.1f} "
            f"slugs={len(cached)} age={nowt - ts:.1f}s"
        )
        return cached

    slugs: set[str] = set()
    for page in range(1, _BULK_MAX_PAGES + 1):
        url = top_cams_url(page=page, limit=_BULK_PAGE_LIMIT)
        logger._log(
            f"favs_views._bulk_live_slugs: page={page} url={url}"
        )
        try:
            body = cb_client.fetch_browse_page(url, fetch_func=fetch_func)
        except OSError as exc:
            logger._log(
                f"favs_views._bulk_live_slugs: NETWORK FAIL page={page} err={exc!r}"
            )
            break
        parsed = cb_listing.parse_roomlist(body)
        if not parsed.models:
            logger._log(
                f"favs_views._bulk_live_slugs: empty page={page}, stop"
            )
            break
        for m in parsed.models:
            slugs.add(m.slug)
        logger._log(
            f"favs_views._bulk_live_slugs: page={page} got={len(parsed.models)} "
            f"running_total={len(slugs)}"
        )
        if len(parsed.models) < _BULK_PAGE_LIMIT:
            break
        # Politeness pacer between page fetches; chaturbate's API doesn't
        # advertise a rate limit but a short sleep keeps us off any
        # heuristic that flags rapid-fire scans.
        if _BULK_PAGE_DELAY_S > 0:
            time.sleep(_BULK_PAGE_DELAY_S)
    if not slugs:
        logger._log("favs_views._bulk_live_slugs: bulk returned 0 slugs")
        return None
    _bulk_cache = (nowt, slugs)
    _save_bulk_disk_cache(nowt, slugs, cache_path)
    logger._log(
        f"favs_views._bulk_live_slugs: cached {len(slugs)} live slugs"
    )
    return slugs


def _bulk_cache_clear(cache_path: Path | None = None) -> None:
    """Drop both the in-memory and on-disk bulk-fetch caches. Used by tests."""
    global _bulk_cache
    _bulk_cache = None
    path = cache_path if cache_path is not None else _bulk_disk_cache_path()
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        return


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


def _render_favs(handle: int, favs: list[Favorite]) -> None:
    """Add favorite entries with state-aware context menus."""
    data_dir = _favs_path().parent
    tv_entries = tv_store.load(data_dir / "tv.json")
    for f in favs:
        label = _color_label(f.name, f.gender)
        ctx = ctxmenu.build_ctxmenu(
            {"slug": f.slug, "name": f.name, "url": f.url},
            tv_entries=tv_entries,
            favs=favs,
        )
        kodi_helpers.add_play_item(
            handle, label, f.slug, ctx_items=ctx,
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
    _render_favs(handle, page_items)
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
