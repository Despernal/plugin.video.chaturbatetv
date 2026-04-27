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


# Bulk-fetch result is cached for ~60 seconds so menu re-entry is
# instant. Cache is per-process (no persistence between Kodi sessions).
_BULK_CACHE_TTL = 60.0
# Paginate; chaturbate's room-list returns up to 500 per page, often
# fewer if the front-page rooms run thin. We stop at <500 returned or
# when total_count says we've covered everything.
_BULK_PAGE_LIMIT = 500
# Hard ceiling on pages so we don't spin if the API misbehaves.
_BULK_MAX_PAGES = 20

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


def _bulk_live_slugs(
    fetch_func: _FetchFn | None,
    *,
    now_func: Callable[[], float] = time.time,
) -> set[str] | None:
    """Return the set of all currently-live model slugs.

    Hits the public room-list API in pages of 500 until it returns less
    than a full page (or we hit ``_BULK_MAX_PAGES``). Cached for 60
    seconds (process-local) so re-entry into the favorites menu is
    instant.

    Returns None on hard failure (no rooms came back at all). The
    caller falls back to per-slug AJAX in that case so the favorites
    menu still works during a partial outage.
    """
    from resources.lib import logger
    global _bulk_cache
    nowt = now_func()
    if _bulk_cache is not None:
        ts, cached = _bulk_cache
        if nowt - ts < _BULK_CACHE_TTL:
            logger._log(
                f"favs_views._bulk_live_slugs: cache HIT ts={ts:.1f} "
                f"slugs={len(cached)}"
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
    if not slugs:
        logger._log("favs_views._bulk_live_slugs: bulk returned 0 slugs")
        return None
    _bulk_cache = (nowt, slugs)
    logger._log(
        f"favs_views._bulk_live_slugs: cached {len(slugs)} live slugs"
    )
    return slugs


def _bulk_cache_clear() -> None:
    """Drop the bulk-fetch cache. Used by tests."""
    global _bulk_cache
    _bulk_cache = None


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
    """Top-level Favorites menu: Online / Offline split via bulk-fetch.

    Uses :func:`bulk_live_slugs` to do ~10 paginated room-list calls and
    intersect with our local favorites instead of polling each one
    individually (Lesson 11). Falls back to per-slug polling only when
    the bulk fetch returns an empty set (e.g. site is partially down).
    """
    from resources.lib import logger
    path = store_path if store_path is not None else _favs_path()
    favs = favs_store.load(path)
    logger._log(f"favs_views.favs_menu: total={len(favs)}")
    online_slugs = _bulk_live_slugs(fetch_func)
    if online_slugs is None:
        # Bulk path failed; fall back to per-slug AJAX. Slow but correct.
        logger._log(
            "favs_views.favs_menu: bulk path FAILED, falling back to per-slug"
        )
        online: list[Favorite] = []
        offline: list[Favorite] = []
        for f in favs:
            if cb_client.is_model_live(f.slug, fetch_func=fetch_func):
                online.append(f)
            else:
                offline.append(f)
    else:
        online = [f for f in favs if f.slug in online_slugs]
        offline = [f for f in favs if f.slug not in online_slugs]
    logger._log(
        f"favs_views.favs_menu: online={len(online)} offline={len(offline)}"
    )
    kodi_helpers.add_dir(handle, f"Online ({len(online)})", "favs_online")
    kodi_helpers.add_dir(handle, f"Offline ({len(offline)})", "favs_offline")
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


def online_favs_view(handle: int, store_path: Path | None = None,
                     fetch_func: _FetchFn | None = None,
                     **_params: Any) -> None:
    """Currently-live favorites only."""
    from resources.lib import logger
    path = store_path if store_path is not None else _favs_path()
    favs = favs_store.load(path)
    online_slugs = _bulk_live_slugs(fetch_func)
    if online_slugs is None:
        live = [
            f for f in favs
            if cb_client.is_model_live(f.slug, fetch_func=fetch_func)
        ]
    else:
        live = [f for f in favs if f.slug in online_slugs]
    logger._log(
        f"favs_views.online_favs_view: total={len(favs)} live={len(live)}"
    )
    _render_favs(handle, live)
    kodi_helpers.end_directory(handle, content_type="videos")


def offline_favs_view(handle: int, store_path: Path | None = None,
                      fetch_func: _FetchFn | None = None,
                      **_params: Any) -> None:
    """Currently-offline favorites only (still listed so user can edit/remove)."""
    from resources.lib import logger
    path = store_path if store_path is not None else _favs_path()
    favs = favs_store.load(path)
    online_slugs = _bulk_live_slugs(fetch_func)
    if online_slugs is None:
        offline = [
            f for f in favs
            if not cb_client.is_model_live(f.slug, fetch_func=fetch_func)
        ]
    else:
        offline = [f for f in favs if f.slug not in online_slugs]
    logger._log(
        f"favs_views.offline_favs_view: total={len(favs)} offline={len(offline)}"
    )
    _render_favs(handle, offline)
    kodi_helpers.end_directory(handle, content_type="videos")
