"""Side-effect entry points reachable through ``mode=...`` URLs.

These are the verbs the addon performs in response to a ctxmenu /
runplugin (not just listing renders). Each takes the same shape:
``(handle: int, **params: Any) -> None``.

Phase 5 will fill in tv_play / tv_stop / playvid; Phase 7 covers login.
For Phase 2 we ship the favs verbs and the search dialog; the TV
verbs are stubs that show a notification so the router stays wired.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from resources.lib import favs_store
from resources.lib.cb_models import Favorite, Gender


def _favs_path() -> Path:
    try:
        import xbmcvfs
        base = xbmcvfs.translatePath(
            "special://profile/addon_data/plugin.video.chaturbatetv/")
        return Path(base) / "favs.json"
    except Exception:
        return Path.home() / ".kodi" / "userdata" / "addon_data" / \
            "plugin.video.chaturbatetv" / "favs.json"


def _notify(heading: str, msg: str) -> None:
    try:
        import xbmcgui
        xbmcgui.Dialog().notification(heading, msg, xbmcgui.NOTIFICATION_INFO, 4000)
    except Exception:
        return


# --------------------------------------------------------------------------- #
# Favorites verbs
# --------------------------------------------------------------------------- #


def fav_add(handle: int, slug: str = "", name: str = "",
            url: str = "", gender: str = "unknown",
            store_path: Path | None = None, **_params: Any) -> None:
    """Add a model to local favorites. Idempotent: existing slug -> no-op."""
    if not slug:
        _notify("Chaturbate TV", "Add to favorites: missing slug")
        return
    path = store_path if store_path is not None else _favs_path()
    favs = favs_store.load(path)
    fav = Favorite(
        name=name or slug,
        slug=slug,
        url=url or f"https://chaturbate.com/{slug}/",
        gender=Gender.from_str(gender),
    )
    new_favs = favs_store.add(favs, fav)
    if len(new_favs) == len(favs):
        _notify("Chaturbate TV", f"{slug} is already in favorites")
        return
    favs_store.save(path, new_favs)
    _notify("Chaturbate TV", f"Added {slug} to favorites")


def fav_remove(handle: int, slug: str = "",
               store_path: Path | None = None, **_params: Any) -> None:
    """Remove a slug from local favorites."""
    if not slug:
        _notify("Chaturbate TV", "Remove from favorites: missing slug")
        return
    path = store_path if store_path is not None else _favs_path()
    favs = favs_store.load(path)
    new_favs = favs_store.remove(favs, slug)
    if len(new_favs) == len(favs):
        _notify("Chaturbate TV", f"{slug} was not in favorites")
        return
    favs_store.save(path, new_favs)
    _notify("Chaturbate TV", f"Removed {slug} from favorites")


# --------------------------------------------------------------------------- #
# Search verb (opens an input dialog, then re-routes)
# --------------------------------------------------------------------------- #


def search(handle: int, **_params: Any) -> None:  # pragma: no cover - thin Kodi shim
    """Prompt the user for a query, then run search_view with it."""
    try:
        import xbmc
        import xbmcgui
    except ImportError:
        return
    query = xbmcgui.Dialog().input("Chaturbate Search", "")
    if not query:
        return
    cmd = (
        f"Container.Update(plugin://plugin.video.chaturbatetv/?"
        f"mode=search&query={query})"
    )
    xbmc.executebuiltin(cmd)


# --------------------------------------------------------------------------- #
# Playvid + TV verbs (Phase 4-5 stubs)
# --------------------------------------------------------------------------- #


def playvid(handle: int, slug: str = "", **_params: Any) -> None:
    """Stub. Phase 4b lights up real ISA playback. Phase 2 just notifies."""
    _notify("Chaturbate TV",
            f"Playback for {slug or 'unknown'} arrives in Phase 4")


def tv_play(handle: int, **_params: Any) -> None:
    _notify("Chaturbate TV", "TV mode arrives in Phase 5")


def tv_stop(handle: int, **_params: Any) -> None:
    _notify("Chaturbate TV", "TV mode arrives in Phase 5")


def tv_list(handle: int, **_params: Any) -> None:
    _notify("Chaturbate TV", "TV mode arrives in Phase 5")


def tv_add(handle: int, slug: str = "", **_params: Any) -> None:
    _notify("Chaturbate TV",
            f"AddToTV for {slug or 'unknown'} arrives in Phase 5")


def tv_remove(handle: int, slug: str = "", **_params: Any) -> None:
    _notify("Chaturbate TV",
            f"RemoveFromTV for {slug or 'unknown'} arrives in Phase 5")


def tv_edit(handle: int, slug: str = "", **_params: Any) -> None:
    _notify("Chaturbate TV",
            f"EditTVPriority for {slug or 'unknown'} arrives in Phase 5")
