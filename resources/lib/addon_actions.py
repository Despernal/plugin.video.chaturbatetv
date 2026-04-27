"""Side-effect entry points reachable through ``mode=...`` URLs.

These are the verbs the addon performs in response to a ctxmenu /
runplugin (not just listing renders). Each takes the same shape:
``(handle: int, **params: Any) -> None``.

Login (Phase 7) is still deferred - everything here works against the
public Chaturbate endpoints without authentication.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from resources.lib import ctxmenu, favs_store, tv_store
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
    from resources.lib import logger
    logger._log(f"fav_add: enter slug={slug!r} name={name!r} gender={gender!r}")
    if not slug:
        logger._log("fav_add: missing slug, abort")
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
        logger._log(f"fav_add: {slug!r} already exists, no-op")
        _notify("Chaturbate TV", f"{slug} is already in favorites")
        return
    favs_store.save(path, new_favs)
    logger._log(f"fav_add: added {slug!r} ({len(favs)} -> {len(new_favs)})")
    _notify("Chaturbate TV", f"Added {slug} to favorites")


def fav_remove(handle: int, slug: str = "",
               store_path: Path | None = None, **_params: Any) -> None:
    """Remove a slug from local favorites."""
    from resources.lib import logger
    logger._log(f"fav_remove: enter slug={slug!r}")
    if not slug:
        logger._log("fav_remove: missing slug, abort")
        _notify("Chaturbate TV", "Remove from favorites: missing slug")
        return
    path = store_path if store_path is not None else _favs_path()
    favs = favs_store.load(path)
    new_favs = favs_store.remove(favs, slug)
    if len(new_favs) == len(favs):
        logger._log(f"fav_remove: {slug!r} not in list, no-op")
        _notify("Chaturbate TV", f"{slug} was not in favorites")
        return
    favs_store.save(path, new_favs)
    logger._log(f"fav_remove: removed {slug!r} ({len(favs)} -> {len(new_favs)})")
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
# Playvid + TV verbs
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
        # Lesson v6.1 + Lesson 32: when TV mode is active and a slug
        # resolves offline, do TWO things: (1) invalidate the
        # bulk-live cache so the loop's next pick_target doesn't
        # re-pick this same offline slug for the rest of the poll
        # cycle, (2) fire ``PlayerControl(Next)`` to advance past
        # this slot in the current playlist.
        #
        # Action(Next) was the original here but Kodi 21+ logs
        # "Keymapping error: no such action 'next' defined" - the
        # builtin name is wrong. ``PlayerControl(Next)`` is the
        # canonical playlist-advance builtin.
        if _tv_mode_active():
            _tv_bulk_mark_offline(slug)
            logger._log(
                f"playvid: TV active + offline -> "
                f"PlayerControl(Next) for slug={slug!r}"
            )
            try:
                import xbmc
                xbmc.executebuiltin("PlayerControl(Next)")
            except Exception:  # noqa: S110 - best-effort: no Kodi outside addon
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
    # Dismiss any lingering Kodi busy dialog before we start the long-
    # running TV loop. Kodi shows one when a previous addon invocation
    # took too long to return ('s pattern - dismiss on entry
    # to every long-running verb).
    try:
        import xbmc
        xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
        xbmc.executebuiltin("Dialog.Close(busydialog)")
    except Exception:  # noqa: S110 - best-effort UI cleanup
        pass
    _notify("Chaturbate TV", "Starting TV mode...")
    # Real-Kodi path: import tv_loop lazily so unit tests of the
    # verb-shim layer don't need the whole xbmc shim.
    from resources.lib import tv_loop
    is_live = _make_bulk_is_live_func(pm)
    tv_loop.tv_play(
        entries=entries,
        is_live_func=is_live,
        poll_minutes=pm,
    )


def restart_kodi(handle: int, **_params: Any) -> None:
    """Restart Kodi to clear stuck audio/video engine state.

    LL-HLS streams occasionally drift the audio renderer into a state
    where ``CDVDAudio::AddPacketsRenderer - timeout`` errors repeat
    forever - the symptom the user sees is "video plays but audio
    constantly buffers." A full Kodi restart is the only reliable fix
    (Lesson from a real  session: 2026-04-27).

    On LibreELEC ``Quit`` causes systemd to respawn Kodi automatically,
    so this is the addon equivalent of ``systemctl restart kodi``
    without needing ssh.
    """
    from resources.lib import logger
    logger._log("restart_kodi: user requested restart")
    _notify("Chaturbate TV", "Restarting Kodi - reloading...")
    try:
        import xbmc
        # 1.5s sleep so the notification is visible BEFORE the restart.
        xbmc.sleep(1500)
        xbmc.executebuiltin("Quit")
    except Exception as exc:
        logger._log(f"restart_kodi: builtin failed err={exc!r}")
        _notify("Chaturbate TV", "Restart failed - try ssh")


def refresh_artwork(handle: int, **_params: Any) -> None:
    """Force Kodi to re-fetch the addon's icon + fanart on next render.

    Kodi caches every texture (icon, fanart, room thumbnails) in
    ``special://database/Textures<N>.db`` and serves it from
    ``special://thumbnails/`` for the lifetime of the install. The DB
    schema version is part of the filename: Kodi 19/20 used
    ``Textures13.db``, Kodi 21+ uses ``Textures14.db``, and the
    schema bumps every couple of major releases. We walk EVERY
    ``Textures*.db`` we find in the Database dir so the verb works
    forward-compatibly.

    For each DB, this verb finds rows whose source URL contains
    ``plugin.video.chaturbatetv``, deletes the cached file from
    Thumbnails/, and removes the DB row. The next directory render
    re-caches from disk.

    's ``clean_database`` for chaturbate room thumbs is the
    reference implementation; we narrow the scope to the addon's own
    artwork only.
    """
    from resources.lib import logger
    import sqlite3
    logger._log("refresh_artwork: start")
    deleted = 0
    db_paths_walked = 0
    try:
        import xbmcvfs
        db_dir = Path(xbmcvfs.translatePath("special://database/"))
        thumbs_root = Path(xbmcvfs.translatePath("special://thumbnails/"))
    except Exception as exc:
        logger._log(f"refresh_artwork: xbmcvfs unavailable err={exc!r}")
        _notify("Chaturbate TV", "Refresh artwork: Kodi paths unavailable")
        return

    # Walk every Textures<N>.db. Both the legacy and current schema get
    # cleaned; if a future Kodi release bumps the schema again, our
    # verb keeps working without a code change.
    for db_path in sorted(db_dir.glob("Textures*.db")):
        db_paths_walked += 1
        logger._log(f"refresh_artwork: scanning {db_path}")
        try:
            conn = sqlite3.connect(str(db_path))
        except sqlite3.Error as exc:
            logger._log(
                f"refresh_artwork: db connect FAIL {db_path} err={exc!r}"
            )
            continue
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT id, cachedurl FROM texture WHERE url LIKE ?",
                    ("%plugin.video.chaturbatetv%",),
                )
                rows = cur.fetchall()
            except sqlite3.Error as exc:
                # Schema changed (table missing, columns missing) - skip
                # gracefully. Avoid crashing the verb on unknown DB shapes.
                logger._log(
                    f"refresh_artwork: schema mismatch {db_path} err={exc!r}"
                )
                continue
            logger._log(
                f"refresh_artwork: {db_path.name} found {len(rows)} entries"
            )
            for row_id, cached in rows:
                cached_path = thumbs_root / str(cached)
                try:
                    cached_path.unlink()
                except (FileNotFoundError, OSError) as exc:
                    logger._log(
                        f"refresh_artwork: unlink fail {cached_path} err={exc!r}"
                    )
                try:
                    conn.execute("DELETE FROM texture WHERE id = ?", (row_id,))
                    deleted += 1
                except sqlite3.Error as exc:
                    logger._log(f"refresh_artwork: db delete FAIL err={exc!r}")
            conn.commit()
        finally:
            conn.close()

    logger._log(
        f"refresh_artwork: done dbs_walked={db_paths_walked} deleted={deleted}"
    )
    if db_paths_walked == 0:
        _notify("Chaturbate TV", "Refresh artwork: no Textures DB found")
    else:
        _notify("Chaturbate TV", f"Refreshed {deleted} cached art entries")
    # Refresh the current container so the user sees the new artwork
    # immediately.
    _refresh_container()


# TV-mode bulk-live cache. Module-level so playvid can invalidate
# stale entries when it discovers a slug is offline despite the cache
# saying live - prevents the TV loop from looping on the same offline
# model for a full poll cycle.
_TV_BULK_CACHE: dict[str, Any] = {
    "slugs": frozenset(),
    "ts": 0.0,
    "ttl_s": 0.0,
}

# Affiliate watermarks ('s rotating array; same set favs_views uses).
_TV_BULK_WATERMARKS = (
    "C9m5N", "tfZSl", "jQrKO", "5XO2a", "WXomN",
    "zM6MR", "Lb2aB", "cIbs3", "mnzQo", "N6TZA",
)


def _tv_bulk_refresh() -> bool:
    """Single affiliate-onlinerooms fetch into ``_TV_BULK_CACHE``.

    Network failure preserves the stale set so a transient 5xx doesn't
    silently mark every model offline.
    """
    import random
    import time as _time
    from resources.lib import cb_client, cb_listing, logger
    from resources.lib.cb_endpoints import online_rooms_affiliate_url

    wm = random.choice(_TV_BULK_WATERMARKS)
    url = online_rooms_affiliate_url(wm)
    logger._log(f"addon_actions._tv_bulk_refresh: url={url}")
    try:
        body = cb_client.fetch_browse_page(url)
    except OSError as exc:
        logger._log(
            f"addon_actions._tv_bulk_refresh: FAIL err={exc!r} "
            f"(stale set has {len(_TV_BULK_CACHE['slugs'])} slugs)"
        )
        return False
    models = cb_listing.parse_affiliate_onlinerooms(body)
    new_slugs = frozenset(m.slug for m in models)
    _TV_BULK_CACHE["slugs"] = new_slugs
    _TV_BULK_CACHE["ts"] = _time.time()
    logger._log(
        f"addon_actions._tv_bulk_refresh: refreshed slugs={len(new_slugs)}"
    )
    return True


def _tv_bulk_mark_offline(slug: str) -> None:
    """Remove ``slug`` from the cached live set.

    Called from playvid when the per-slug AJAX confirms a model is
    offline despite the bulk cache saying live. Without this, the TV
    loop's next iteration would re-pick the same model (cache still
    fresh per its TTL), playvid would offline-Action(Next) again, and
    the loop would tightly cycle on the same dead model for the full
    poll-cycle TTL (~9.5 min default).

    Idempotent: if slug isn't in the set, no-op.
    """
    from resources.lib import logger
    current = _TV_BULK_CACHE["slugs"]
    if slug not in current:
        return
    _TV_BULK_CACHE["slugs"] = frozenset(s for s in current if s != slug)
    logger._log(
        f"addon_actions._tv_bulk_mark_offline: {slug!r} removed from cache "
        f"({len(current)} -> {len(_TV_BULK_CACHE['slugs'])})"
    )


def _make_bulk_is_live_func(poll_minutes: int) -> Any:
    """Build a TV-mode is_live callback backed by ``_TV_BULK_CACHE``.

    Single affiliate-API call per poll cycle replaces 60 sequential
    per-slug AJAX hits. The cache is module-level so playvid can
    invalidate stale entries via ``_tv_bulk_mark_offline``.
    """
    import time as _time
    from resources.lib import cb_client, logger

    # TTL slightly LESS than the poll interval so we always have fresh
    # data at the start of each tier walk. Floor at 30s for testing.
    ttl_seconds = max(30, int(poll_minutes * 60) - 30)
    _TV_BULK_CACHE["ttl_s"] = ttl_seconds

    def is_live(url: str) -> bool:
        nowt = _time.time()
        if (not _TV_BULK_CACHE["slugs"]
                or nowt - _TV_BULK_CACHE["ts"] > ttl_seconds):
            ok = _tv_bulk_refresh()
            if not ok and not _TV_BULK_CACHE["slugs"]:
                # Bulk failed AND we have no cached set (cold-start +
                # affiliate-endpoint outage). Fall back to per-slug AJAX
                # for THIS query so TV mode can pick a target. Don't
                # cache the per-slug answer; next call retries bulk
                # first so we recover automatically when the affiliate
                # endpoint comes back.
                slug = _slug_from_url(url)
                logger._log(
                    f"addon_actions._tv_bulk_is_live: cold + bulk FAIL, "
                    f"per-slug fallback slug={slug!r}"
                )
                try:
                    return cb_client.is_model_live(slug)
                except Exception as exc:
                    logger._log(
                        f"addon_actions._tv_bulk_is_live: per-slug fail "
                        f"slug={slug!r} err={exc!r}"
                    )
                    return False
        slug = _slug_from_url(url)
        return slug in _TV_BULK_CACHE["slugs"]

    return is_live


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
    # Build the favs once so each row's ctxmenu builder can ask "is
    # this also in favorites?" without per-row disk reads.
    favs = favs_store.load(_favs_path())
    sorted_entries = priority_sort(entries)
    for e in sorted_entries:
        label = f"[COLOR FF00d4ff][P{e.priority:02d}][/COLOR] {e.name}"
        slug = _slug_from_url(e.url)
        ctx = ctxmenu.build_ctxmenu(
            {"slug": slug, "name": e.name, "url": e.url},
            tv_entries=entries,
            favs=favs,
        )
        kodi_helpers.add_play_item(
            handle, label, slug=slug, ctx_items=ctx,
        )
    # unsorted=True so Kodi keeps our priority-descending order; without
    # it, Kodi alpha-sorts by label and "[P01]" lands above "[P17]" -
    # the OPPOSITE of "highest priority on top" which is the whole point
    # of the view.
    kodi_helpers.end_directory(handle, content_type="videos", unsorted=True)


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
