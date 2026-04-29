"""Browse menus: Top Cams, New Cams, gender filters, search.

The main menu surfaces the high-level entries with HALO color tags per
gender. Sub-views fetch + parse + render via cb_client + cb_listing +
kodi_helpers.

Network calls are routed through cb_client; tests inject a fetch_func
to keep the unit tests off the real network.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pathlib import Path

from resources.lib import (
    cb_client,
    cb_listing,
    ctxmenu,
    favs_store,
    kodi_helpers,
    tv_store,
)
from resources.lib.cb_endpoints import (
    gender_filter_url,
    new_cams_url,
    search_url,
    top_cams_url,
)
from resources.lib.cb_models import Gender, Model

_FetchFn = Callable[..., str]


# HALO palette per PLANNING.md.
# Kodi's [COLOR <hex>] requires 8-char AARRGGBB (or named colors).
# 6-char hex (RRGGBB) silently renders blank.
GENDER_COLORS: dict[Gender, str] = {
    Gender.FEMALE: "FF00d4ff",
    Gender.MALE: "FF66e3ff",
    Gender.COUPLE: "FF00ff88",
    Gender.TRANS: "FFff0080",
    Gender.UNKNOWN: "FFc8e8f8",
}


def _color_label(label: str, gender: Gender) -> str:
    """Wrap a label in Kodi's [COLOR AARRGGBB]...[/COLOR] tag for the gender."""
    color = GENDER_COLORS.get(gender, "FFc8e8f8")
    return f"[COLOR {color}]{label}[/COLOR]"


def main_menu(handle: int, **_params: Any) -> None:
    """Top-level addon entries with HALO color tags per gender.

    Each gender entry can be hidden via ``show_<gender>`` boolean
    settings (defaulted on); useful if the user only ever browses one
    or two categories and wants the main menu uncluttered. Favorites
    and TV mode lists are NOT filtered - those are personal collections
    where the user picked the model directly.
    """
    from resources.lib import addon_settings, logger
    logger._log(f"browse_views.main_menu: handle={handle}")
    kodi_helpers.add_dir(handle, "Top Cams", "top")
    kodi_helpers.add_dir(handle, "New Cams", "new")
    if addon_settings.show_gender("female"):
        kodi_helpers.add_dir(handle, _color_label("Female", Gender.FEMALE),
                             "gender", gender="female")
    if addon_settings.show_gender("male"):
        kodi_helpers.add_dir(handle, _color_label("Male", Gender.MALE),
                             "gender", gender="male")
    if addon_settings.show_gender("couple"):
        kodi_helpers.add_dir(handle, _color_label("Couple", Gender.COUPLE),
                             "gender", gender="couple")
    if addon_settings.show_gender("trans"):
        kodi_helpers.add_dir(handle, _color_label("Trans", Gender.TRANS),
                             "gender", gender="trans")
    kodi_helpers.add_dir(handle, "Search", "search_prompt")
    kodi_helpers.add_dir(handle, "TV Mode", "tv_list")
    kodi_helpers.add_dir(handle, "Favorites", "favs")
    # Maintenance entries at the bottom - rarely needed but always visible
    # so the user has a path back without ssh access.
    kodi_helpers.add_dir(
        handle,
        "[COLOR FFc8e8f8]>> Refresh artwork (icon / fanart)[/COLOR]",
        "refresh_artwork",
    )
    kodi_helpers.add_dir(
        handle,
        "[COLOR FFc8e8f8]>> Refresh offline model info (last seen, thumbnails)[/COLOR]",
        "refresh_offline_meta",
    )
    kodi_helpers.add_dir(
        handle,
        "[COLOR FFff8080]>> Restart Kodi (clears stuck audio buffer)[/COLOR]",
        "restart_kodi",
    )
    kodi_helpers.add_dir(
        handle,
        "[COLOR FFc8e8f8]>> Settings[/COLOR]",
        "open_settings",
    )
    kodi_helpers.end_directory(handle, content_type="videos")


def _addon_data_dir() -> Path:
    """Resolve the addon's userdata dir for tv.json / favs.json."""
    try:
        import xbmcvfs
        base = xbmcvfs.translatePath(
            "special://profile/addon_data/plugin.video.chaturbatetv/")
        return Path(base)
    except Exception:
        return Path.home() / ".kodi" / "userdata" / "addon_data" / \
            "plugin.video.chaturbatetv"


def _render_models(handle: int, models: list[Model]) -> None:
    """Add Model entries as playable items, color-tagged by gender, with
    each room's actual thumb URL (not a hardcoded pattern - Chaturbate's
    img URLs include cache-busting timestamps so guessing fails). The
    plot string lands via setInfo("video") so Kodi shows Age/Location/
    Watching/Followers/Tags in the right-pane on hover.

    Also attaches a state-aware context menu (Add to TV / In TV /
    Add to Favorites / Remove from Favorites) per :func:`ctxmenu.build_ctxmenu`.
    Loads tv.json + favs.json once per render so the membership lookup is
    cheap regardless of model count (Lesson 13 from -patches).
    """
    data_dir = _addon_data_dir()
    tv_entries = tv_store.load(data_dir / "tv.json")
    favs = favs_store.load(data_dir / "favs.json")
    for m in models:
        label = _color_label(m.name, m.gender)
        if m.viewers:
            label = f"{label} [{m.viewers}]"
        ctx = ctxmenu.build_ctxmenu(
            {"slug": m.slug, "name": m.name, "url": m.url},
            tv_entries=tv_entries,
            favs=favs,
        )
        kodi_helpers.add_play_item(
            handle, label, m.slug,
            image=m.image or None,
            plot=m.plot or None,
            ctx_items=ctx,
        )


def _coerce_page(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _fetch_models(url: str, fetch_func: _FetchFn | None) -> list[Model]:
    """GET the JSON room-list, return parsed Models. Empty on any failure."""
    body = cb_client.fetch_browse_page(url, fetch_func=fetch_func)
    return cb_listing.parse_roomlist(body).models


def _by_viewers_desc(models: list[Model]) -> list[Model]:
    """Sort models by viewer count descending. The room-list API returns
    rooms in some internal mixed order (top ~8 high-traffic rooms, then
    a roughly-random tail). Chaturbate's own website re-sorts by viewers
    client-side; we do the same so Female / Male / Couple / Trans all
    show "most-watched on top, walking down" the way users expect.
    """
    return sorted(models, key=lambda m: -m.viewers)


def top_cams_view(handle: int, page: Any = 1,
                  fetch_func: _FetchFn | None = None,
                  **_params: Any) -> None:
    """Top-cams listing: most-viewers first, all genders."""
    from resources.lib import logger
    p = _coerce_page(page)
    url = top_cams_url(p)
    logger._log(f"browse_views.top_cams_view: page={p} url={url}")
    models = _by_viewers_desc(_fetch_models(url, fetch_func))
    logger._log(f"browse_views.top_cams_view: page={p} models={len(models)}")
    _render_models(handle, models)
    kodi_helpers.add_dir(handle, "Next page", "top", page=p + 1)
    kodi_helpers.end_directory(handle, content_type="videos")


def new_cams_view(handle: int, page: Any = 1,
                  fetch_func: _FetchFn | None = None,
                  **_params: Any) -> None:
    """Recently-online listing."""
    from resources.lib import logger
    p = _coerce_page(page)
    url = new_cams_url(p)
    logger._log(f"browse_views.new_cams_view: page={p} url={url}")
    models = _fetch_models(url, fetch_func)
    logger._log(f"browse_views.new_cams_view: page={p} models={len(models)}")
    _render_models(handle, models)
    kodi_helpers.add_dir(handle, "Next page", "new", page=p + 1)
    kodi_helpers.end_directory(handle, content_type="videos")


def gender_view(handle: int, gender: str = "female", page: Any = 1,
                fetch_func: _FetchFn | None = None,
                **_params: Any) -> None:
    """Single-gender listing. Filter is server-side via the JSON API."""
    from resources.lib import logger
    g = Gender.from_str(gender)
    p = _coerce_page(page)
    logger._log(f"browse_views.gender_view: gender={gender!r} page={p}")
    if g is Gender.UNKNOWN:
        logger._log(f"browse_views.gender_view: unknown gender={gender!r}, abort")
        kodi_helpers.end_directory(handle, succeeded=False, content_type="videos")
        return
    url = gender_filter_url(g, p)
    logger._log(f"browse_views.gender_view: gender={gender!r} url={url}")
    models = _by_viewers_desc(_fetch_models(url, fetch_func))
    logger._log(f"browse_views.gender_view: gender={gender!r} page={p} models={len(models)}")
    _render_models(handle, models)
    kodi_helpers.add_dir(handle, "Next page", "gender",
                         gender=gender, page=p + 1)
    kodi_helpers.end_directory(handle, content_type="videos")


def search_view(handle: int, query: str = "", page: Any = 1,
                fetch_func: _FetchFn | None = None,
                **_params: Any) -> None:
    """Keyword search. Empty query -> close directory (the input dialog
    is handled by addon_actions.search which then re-routes here with
    a populated query).
    """
    from resources.lib import logger
    p = _coerce_page(page)
    logger._log(f"browse_views.search_view: query={query!r} page={p}")
    if not query:
        logger._log("browse_views.search_view: empty query -> close")
        kodi_helpers.end_directory(handle, succeeded=False, content_type="videos")
        return
    url = search_url(query, p)
    models = _by_viewers_desc(_fetch_models(url, fetch_func))
    logger._log(
        f"browse_views.search_view: query={query!r} page={p} models={len(models)}"
    )
    _render_models(handle, models)
    kodi_helpers.add_dir(handle, "Next page", "search",
                         query=query, page=p + 1)
    kodi_helpers.end_directory(handle, content_type="videos")
