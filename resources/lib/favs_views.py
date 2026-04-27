"""Local favorites views: online + offline + a top-level menu.

Backed by ``favs_store`` (Phase 1) for persistence. Online vs offline
split is computed at view-render time using ``cb_client.is_model_live``;
slow-but-correct: every render polls the AJAX endpoint per favorite.

Tests inject ``fetch_func`` and ``store_path`` so we never poll the
real network or write to user-paths.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from resources.lib import cb_client, favs_store, kodi_helpers
from resources.lib.cb_models import Favorite, Gender

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
    """Top-level Favorites menu: Online / Offline split + bulk reset."""
    path = store_path if store_path is not None else _favs_path()
    favs = favs_store.load(path)
    online: list[Favorite] = []
    offline: list[Favorite] = []
    for f in favs:
        if cb_client.is_model_live(f.slug, fetch_func=fetch_func):
            online.append(f)
        else:
            offline.append(f)
    kodi_helpers.add_dir(handle, f"Online ({len(online)})", "favs_online")
    kodi_helpers.add_dir(handle, f"Offline ({len(offline)})", "favs_offline")
    kodi_helpers.end_directory(handle, content_type="videos")


def _render_favs(handle: int, favs: list[Favorite]) -> None:
    for f in favs:
        label = _color_label(f.name, f.gender)
        kodi_helpers.add_play_item(handle, label, f.slug)


def online_favs_view(handle: int, store_path: Path | None = None,
                     fetch_func: _FetchFn | None = None,
                     **_params: Any) -> None:
    """Currently-live favorites only."""
    path = store_path if store_path is not None else _favs_path()
    favs = favs_store.load(path)
    live = [f for f in favs if cb_client.is_model_live(f.slug, fetch_func=fetch_func)]
    _render_favs(handle, live)
    kodi_helpers.end_directory(handle, content_type="videos")


def offline_favs_view(handle: int, store_path: Path | None = None,
                      fetch_func: _FetchFn | None = None,
                      **_params: Any) -> None:
    """Currently-offline favorites only (still listed so user can edit/remove)."""
    path = store_path if store_path is not None else _favs_path()
    favs = favs_store.load(path)
    offline = [
        f for f in favs
        if not cb_client.is_model_live(f.slug, fetch_func=fetch_func)
    ]
    _render_favs(handle, offline)
    kodi_helpers.end_directory(handle, content_type="videos")
