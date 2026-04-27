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

from resources.lib import cb_client, cb_listing, kodi_helpers
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
    """Top-level addon entries with HALO color tags per gender."""
    kodi_helpers.add_dir(handle, "Top Cams", "top")
    kodi_helpers.add_dir(handle, "New Cams", "new")
    kodi_helpers.add_dir(handle, _color_label("Female", Gender.FEMALE),
                         "gender", gender="female")
    kodi_helpers.add_dir(handle, _color_label("Male", Gender.MALE),
                         "gender", gender="male")
    kodi_helpers.add_dir(handle, _color_label("Couple", Gender.COUPLE),
                         "gender", gender="couple")
    kodi_helpers.add_dir(handle, _color_label("Trans", Gender.TRANS),
                         "gender", gender="trans")
    kodi_helpers.add_dir(handle, "Search", "search")
    kodi_helpers.add_dir(handle, "TV Mode", "tv_list")
    kodi_helpers.add_dir(handle, "Favorites", "favs")
    kodi_helpers.end_directory(handle, content_type="videos")


def _render_models(handle: int, models: list[Model]) -> None:
    """Add Model entries as playable items, color-tagged by gender, with
    each room's actual thumb URL (not a hardcoded pattern - Chaturbate's
    img URLs include cache-busting timestamps so guessing fails). The
    plot string lands via setInfo("video") so Kodi shows Age/Location/
    Watching/Followers/Tags in the right-pane on hover.
    """
    for m in models:
        label = _color_label(m.name, m.gender)
        if m.viewers:
            label = f"{label} [{m.viewers}]"
        kodi_helpers.add_play_item(
            handle, label, m.slug,
            image=m.image or None,
            plot=m.plot or None,
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


def top_cams_view(handle: int, page: Any = 1,
                  fetch_func: _FetchFn | None = None,
                  **_params: Any) -> None:
    """Top-cams listing: most-viewers first, all genders."""
    url = top_cams_url(_coerce_page(page))
    _render_models(handle, _fetch_models(url, fetch_func))
    kodi_helpers.add_dir(handle, "Next page", "top", page=_coerce_page(page) + 1)
    kodi_helpers.end_directory(handle, content_type="videos")


def new_cams_view(handle: int, page: Any = 1,
                  fetch_func: _FetchFn | None = None,
                  **_params: Any) -> None:
    """Recently-online listing."""
    url = new_cams_url(_coerce_page(page))
    _render_models(handle, _fetch_models(url, fetch_func))
    kodi_helpers.add_dir(handle, "Next page", "new", page=_coerce_page(page) + 1)
    kodi_helpers.end_directory(handle, content_type="videos")


def gender_view(handle: int, gender: str = "female", page: Any = 1,
                fetch_func: _FetchFn | None = None,
                **_params: Any) -> None:
    """Single-gender listing. Filter is server-side via the JSON API."""
    g = Gender.from_str(gender)
    if g is Gender.UNKNOWN:
        kodi_helpers.end_directory(handle, succeeded=False, content_type="videos")
        return
    url = gender_filter_url(g, _coerce_page(page))
    _render_models(handle, _fetch_models(url, fetch_func))
    kodi_helpers.add_dir(handle, "Next page", "gender",
                         gender=gender, page=_coerce_page(page) + 1)
    kodi_helpers.end_directory(handle, content_type="videos")


def search_view(handle: int, query: str = "", page: Any = 1,
                fetch_func: _FetchFn | None = None,
                **_params: Any) -> None:
    """Keyword search. Empty query -> close directory (the input dialog
    is handled by addon_actions.search which then re-routes here with
    a populated query).
    """
    if not query:
        kodi_helpers.end_directory(handle, succeeded=False, content_type="videos")
        return
    url = search_url(query, _coerce_page(page))
    _render_models(handle, _fetch_models(url, fetch_func))
    kodi_helpers.add_dir(handle, "Next page", "search",
                         query=query, page=_coerce_page(page) + 1)
    kodi_helpers.end_directory(handle, content_type="videos")
