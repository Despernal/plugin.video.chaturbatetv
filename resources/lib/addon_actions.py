"""Side-effect entry points reachable through ``mode=...`` URLs.

These are the verbs the addon performs in response to a ctxmenu /
runplugin (not just listing renders). Each takes the same shape:
``(handle: int, **params: Any) -> None``.

Phase 5 wired the TV verbs to ``tv_loop`` and ``tv_store`` for real.
Phase 7 (login) is still deferred.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from resources.lib import favs_store, tv_store
from resources.lib.cb_models import Favorite, Gender, TVEntry


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


def _favs_path() -> Path:
    return _addon_data_dir() / "favs.json"


def _tv_path() -> Path:
    return _addon_data_dir() / "tv.json"


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


def playvid(handle: int, slug: str = "", name: str = "",
            **_params: Any) -> None:
    """Resolve a slug, start the localhost proxy, set ISA props, return
    the ListItem to Kodi via ``setResolvedUrl``.

    Phase 4b: this is the real playback entry point. Offline rooms
    (or any resolve failure) hand back ``setResolvedUrl(handle, False)``
    so Kodi tears the playback attempt down cleanly instead of hanging
    on a missing item.
    """
    from resources.lib import logger
    logger._log(f"playvid: enter handle={handle} slug={slug!r} name={name!r}")
    if not slug:
        logger._log("playvid: missing slug, abort")
        _notify("Chaturbate TV", "Play: missing slug")
        return

    from resources.lib import playvid_resolver
    result = playvid_resolver.resolve_to_listitem(slug=slug, name=name or slug)
    logger._log(f"playvid: resolve_to_listitem success={result.success} slug={slug!r}")

    try:
        import xbmcplugin
    except ImportError:  # pragma: no cover - only happens outside Kodi
        return

    if not result.success:
        # Lesson v6.1: when TV mode is active, fire Action(Next) so the
        # playlist advances past the offline slot instead of leaving Kodi
        # on a black screen. Outside TV mode, hand back the failed resolve
        # so the user's own click is acknowledged cleanly.
        if _tv_mode_active():
            logger._log(
                f"playvid: TV active + offline -> Action(Next) for slug={slug!r}"
            )
            try:
                import xbmc
                xbmc.executebuiltin("Action(Next)")
            except Exception:  # noqa: S110 - best-effort: builtin missing means no Kodi
                pass
            return
        xbmcplugin.setResolvedUrl(handle, False, _empty_listitem())
        _notify("Chaturbate TV", f"{slug} is offline or unreachable")
        return

    xbmcplugin.setResolvedUrl(handle, True, result.listitem)
    logger._log(f"playvid: setResolvedUrl success slug={slug!r}")


def _tv_mode_active() -> bool:
    """True when the TV-mode playlist loop is the one driving playback.

    Window(10000) is the persistent ``Home`` window — its props live for
    the whole Kodi process, which is how the loop signals across slug
    boundaries. Any failure to read the window (no xbmcgui, no Window)
    means we treat it as inactive and fall back to the regular failure
    path.
    """
    try:
        import xbmcgui
        from resources.lib import tv_state
        return xbmcgui.Window(10000).getProperty(tv_state.ACTIVE_KEY) == "1"
    except Exception:
        return False


def _empty_listitem() -> Any:
    """Minimal placeholder ListItem for failed-resolve setResolvedUrl
    calls (Kodi's setResolvedUrl with succeeded=False still wants a
    ListItem instance).
    """
    try:
        import xbmcgui
        return xbmcgui.ListItem()
    except ImportError:  # pragma: no cover - outside Kodi
        return None


def tv_play(handle: int, store_path: Path | None = None,
            poll_minutes: int | None = None,
            **_params: Any) -> None:
    """Run the TV loop. Loads tv.json, hands off to tv_loop.tv_play.

    Loading is done at this layer so a missing/empty file does NOT
    silently no-op the loop. ``poll_minutes`` is read from settings.xml
    when not passed explicitly (tests pass it directly).
    """
    from resources.lib import addon_settings, logger
    path = store_path if store_path is not None else _tv_path()
    entries = tv_store.load(path)
    pm = poll_minutes if poll_minutes is not None else addon_settings.poll_minutes()
    logger._log(
        f"addon_actions.tv_play: entries={len(entries)} path={path} "
        f"poll_minutes={pm}"
    )
    if not entries:
        _notify("Chaturbate TV", "TV list is empty - use 'Add to TV' on a model")
        return
    # Real-Kodi path: import tv_loop lazily so unit tests of the
    # verb-shim layer don't need the whole xbmc shim.
    from resources.lib import cb_client, tv_loop
    tv_loop.tv_play(
        entries=entries,
        is_live_func=lambda url: cb_client.is_model_live(_slug_from_url(url)),
        poll_minutes=pm,
    )


def tv_stop(handle: int, **_params: Any) -> None:
    """Clear the chaturbatetv_active flag so the running loop exits.

    Equivalent to 's ResetTVMode - if the loop self-locked
    due to a glitch, this is the user-facing recovery path.
    """
    from resources.lib import logger, tv_state
    try:
        import xbmcgui
        win = xbmcgui.Window(10000)
        prior = win.getProperty(tv_state.ACTIVE_KEY)
        win.setProperty(tv_state.ACTIVE_KEY, "0")
        logger._log(f"addon_actions.tv_stop: prior={prior!r} cleared")
        if prior == "1":
            _notify("Chaturbate TV", "TV mode flag cleared")
        else:
            _notify("Chaturbate TV", "TV mode already idle")
    except Exception:
        return


def tv_list(handle: int, store_path: Path | None = None,
            **_params: Any) -> None:
    """Render the TV priority list as a Kodi directory.

    Each row gets a [P{priority}] prefix and an Edit/Remove ctxmenu.
    A header row links to ``mode=tv_play``.
    """
    from resources.lib import logger
    from resources.lib.tv_select import priority_sort
    path = store_path if store_path is not None else _tv_path()
    entries = tv_store.load(path)
    logger._log(f"addon_actions.tv_list: handle={handle} entries={len(entries)}")
    try:
        from resources.lib import kodi_helpers
    except ImportError:  # pragma: no cover - outside Kodi
        return
    if not entries:
        kodi_helpers.add_dir(
            handle,
            "[COLOR FFff8080]TV list empty - use 'Add to TV' on any model[/COLOR]",
            "tv_list",
        )
        kodi_helpers.end_directory(handle, content_type="videos")
        return
    kodi_helpers.add_dir(
        handle,
        "[COLOR FF00d4ff][B]>> Play TV (highest-priority live)[/B][/COLOR]",
        "tv_play",
    )
    kodi_helpers.add_dir(
        handle,
        "[COLOR FFff8080]>> Stop TV mode[/COLOR]",
        "tv_stop",
    )
    sorted_entries = priority_sort(entries)
    for e in sorted_entries:
        label = f"[COLOR FF00d4ff][P{e.priority:02d}][/COLOR] {e.name}"
        kodi_helpers.add_play_item(handle, label, slug=_slug_from_url(e.url))
    kodi_helpers.end_directory(handle, content_type="videos")


def tv_add(handle: int, slug: str = "", name: str = "",
           url: str = "",
           priority: str = "",
           store_path: Path | None = None,
           **_params: Any) -> None:
    """Add a model to the TV list. Idempotent: existing url -> no-op.

    Priority: if not provided as a query param, prompts via
    ``Dialog().numeric``. Clamps to 1..20.
    """
    from resources.lib import logger
    if not slug:
        _notify("Chaturbate TV", "Add to TV: missing slug")
        return
    path = store_path if store_path is not None else _tv_path()
    target_url = url or f"https://chaturbate.com/{slug}/"
    entries = tv_store.load(path)
    if any(e.url == target_url for e in entries):
        logger._log(f"addon_actions.tv_add: {slug!r} already in list")
        existing_priority = next(
            (e.priority for e in entries if e.url == target_url), 1,
        )
        _notify(
            "Chaturbate TV",
            f"{slug} already in TV list (priority {existing_priority})",
        )
        return
    p = _resolve_priority(priority, name or slug)
    if p is None:
        return
    entries.append(TVEntry(name=name or slug, url=target_url, priority=p))
    tv_store.save(path, entries)
    logger._log(f"addon_actions.tv_add: added {slug!r} P{p}")
    _notify("Chaturbate TV", f"Added {slug} (priority {p})")


def tv_remove(handle: int, slug: str = "", url: str = "",
              store_path: Path | None = None,
              **_params: Any) -> None:
    """Remove a model from the TV list."""
    from resources.lib import logger
    if not slug and not url:
        _notify("Chaturbate TV", "Remove from TV: missing slug/url")
        return
    target_url = url or f"https://chaturbate.com/{slug}/"
    path = store_path if store_path is not None else _tv_path()
    entries = tv_store.load(path)
    new_entries = [e for e in entries if e.url != target_url]
    if len(new_entries) == len(entries):
        logger._log(f"addon_actions.tv_remove: {slug!r} not in list")
        _notify("Chaturbate TV", f"{slug or 'entry'} was not in TV list")
        return
    tv_store.save(path, new_entries)
    logger._log(
        f"addon_actions.tv_remove: removed {slug!r} ({len(entries)} -> {len(new_entries)})"
    )
    _notify("Chaturbate TV", f"Removed {slug or 'entry'}")
    _refresh_container()


def tv_edit(handle: int, slug: str = "", url: str = "",
            priority: str = "",
            store_path: Path | None = None,
            **_params: Any) -> None:
    """Edit a TV entry's priority. Prompts numeric if priority param
    is empty.
    """
    from resources.lib import logger
    if not slug and not url:
        _notify("Chaturbate TV", "Edit TV: missing slug/url")
        return
    target_url = url or f"https://chaturbate.com/{slug}/"
    path = store_path if store_path is not None else _tv_path()
    entries = tv_store.load(path)
    cur = next((e for e in entries if e.url == target_url), None)
    if cur is None:
        logger._log(f"addon_actions.tv_edit: {slug!r} not in list")
        _notify("Chaturbate TV", f"{slug or 'entry'} not in TV list")
        return
    new_p = _resolve_priority(priority, cur.name, default=cur.priority)
    if new_p is None:
        return
    new_entries = [
        TVEntry(name=e.name, url=e.url, priority=new_p) if e.url == target_url else e
        for e in entries
    ]
    tv_store.save(path, new_entries)
    logger._log(
        f"addon_actions.tv_edit: {slug!r} P{cur.priority} -> P{new_p}"
    )
    _notify("Chaturbate TV", f"{cur.name} priority -> {new_p}")
    _refresh_container()


# ---------- helpers ---------- #


def _slug_from_url(url: str) -> str:
    """``https://chaturbate.com/alice/`` -> ``alice``."""
    return url.rstrip("/").rsplit("/", 1)[-1]


def _resolve_priority(raw: str, name: str,
                      default: int = 10) -> int | None:
    """Either parse the query-param priority or prompt the user.

    Returns None if the user cancels the prompt.
    """
    if raw:
        try:
            p = int(raw)
            return max(1, min(20, p))
        except (TypeError, ValueError):
            pass
    try:
        import xbmcgui
        kb = xbmcgui.Dialog().numeric(
            0, f"Priority for {name} (1-20, higher = preferred)",
            str(default),
        )
        if not kb:
            return None
        try:
            return max(1, min(20, int(kb)))
        except (TypeError, ValueError):
            return None
    except ImportError:  # pragma: no cover - outside Kodi
        return default


def _refresh_container() -> None:
    """Trigger Kodi to refresh the current directory listing."""
    try:
        import xbmc
        xbmc.executebuiltin("Container.Refresh")
    except Exception:
        return
