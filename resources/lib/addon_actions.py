"""Side-effect entry points reachable through ``mode=...`` URLs.

These are the verbs the addon performs in response to a ctxmenu /
runplugin (not just listing renders). Each takes the same shape:
``(handle: int, **params: Any) -> None``.

Login (Phase 7) is still deferred - everything here works against the
public Chaturbate endpoints without authentication.
"""
from __future__ import annotations

import re
import threading
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


def _model_meta_db_path() -> Path:
    """Path to the sqlite DB that accumulates per-model metadata.

    Lives under addon_data so it survives addon upgrades and Kodi
    profile copies. Auto-track in ``_tv_bulk_refresh`` writes every
    room dict it sees from any poll into this DB; the favs/TV/browse
    views read from it to render rich info for offline models.
    """
    return _addon_data_dir() / "model_meta.db"


def _notify(heading: str, msg: str) -> None:
    try:
        import xbmcgui
        xbmcgui.Dialog().notification(heading, msg, xbmcgui.NOTIFICATION_INFO, 4000)
    except Exception:
        return


# v0.7.39 (audit pass #5 LOW, agent 1+3): slugs come from CB JSON
# AND from ctxmenu URLs (which can be crafted by skins/scripts on the
# same Kodi). Chaturbate's actual slug rule is alphanumeric +
# underscore/dot up to ~32 chars. Reject early at every verb entry
# point so a crafted slug can't reach a string-format URL builder
# (CRLF injection into Referer), a [B]<name>[/B] dialog (BBCode
# breakout), or any future code path that interpolates the slug into
# a filesystem path.
_VALID_SLUG_RE = re.compile(r"^[A-Za-z0-9_.]{1,64}$")


def _is_valid_slug(slug: str) -> bool:
    """Return True iff the slug matches CB's alphanum+underscore+dot
    shape. Empty string and None return False; the verbs that need to
    distinguish "missing" from "invalid" check both."""
    if not slug:
        return False
    return bool(_VALID_SLUG_RE.match(slug))


# --------------------------------------------------------------------------- #
# Favorites verbs
# --------------------------------------------------------------------------- #


def fav_add(handle: int, slug: str = "", name: str = "",
            url: str = "", gender: str = "unknown",
            store_path: Path | None = None, **_params: Any) -> None:
    """Add a model to local favorites. Idempotent: existing slug -> no-op.

    v0.7.37 (race audit pass 1, agent 3 HIGH #1): wraps load+save in
    a cross-process flock. Pre-fix, two ``fav_add`` invocations from
    sibling Kodi processes (e.g., user double-tap) both loaded the
    file, both appended, both wrote -- second write wholesale-clobbered
    the first, silently losing the earlier append.
    """
    from resources.lib import file_lock, logger
    logger._log(f"fav_add: enter slug={slug!r} name={name!r} gender={gender!r}")
    if not slug:
        logger._log("fav_add: missing slug, abort")
        _notify("Chaturbate TV", "Add to favorites: missing slug")
        return
    if not _is_valid_slug(slug):
        logger._log(f"fav_add: invalid slug shape {slug!r}, abort")
        _notify("Chaturbate TV", "Add to favorites: invalid slug")
        return
    path = store_path if store_path is not None else _favs_path()
    fav = Favorite(
        name=name or slug,
        slug=slug,
        url=url or f"https://chaturbate.com/{slug}/",
        gender=Gender.from_str(gender),
    )
    with file_lock.locked(path):
        favs = favs_store.load(path)
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
    """Remove a slug from local favorites.

    v0.7.37: same flock-protected RMW as fav_add.
    """
    from resources.lib import file_lock, logger
    logger._log(f"fav_remove: enter slug={slug!r}")
    if not slug:
        logger._log("fav_remove: missing slug, abort")
        _notify("Chaturbate TV", "Remove from favorites: missing slug")
        return
    path = store_path if store_path is not None else _favs_path()
    with file_lock.locked(path):
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
    """Prompt the user for a query, then run search_view with it.

    v0.7.39 (audit pass #5 MEDIUM, agent 1): the previous f-string
    interpolation of ``query`` straight into ``Container.Update(...)``
    let a query containing ``)`` or ``,`` break out of the builtin
    parser. ``urlencode`` percent-escapes the value so the builtin
    sees it as a single literal query-string token. Mirrors the
    pattern every other Container.Update / RunPlugin builder in the
    codebase already follows.
    """
    try:
        import xbmc
        import xbmcgui
    except ImportError:
        return
    from urllib.parse import urlencode
    query = xbmcgui.Dialog().input("Chaturbate Search", "")
    if not query:
        return
    qs = urlencode({"mode": "search", "query": query})
    cmd = f"Container.Update(plugin://plugin.video.chaturbatetv/?{qs})"
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
    if not _is_valid_slug(slug):
        logger._log(f"playvid: invalid slug shape {slug!r}, abort")
        return

    # v0.7.47: stamp the in-addon-switch marker on the Kodi home window
    # so the running TV-loop's _classify_after_stop can tell "this stop
    # is a switch in flight" from "this stop is the user pressing Stop
    # to exit". Cross-process visibility because playvid and tv_loop
    # run in separate Python invocations.
    try:
        import time as _time
        import xbmcgui
        xbmcgui.Window(10000).setProperty(
            "chaturbatetv_pending_play_epoch", str(_time.time()),
        )
    except Exception:  # noqa: S110 - best-effort marker
        pass

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
            # v0.7.15 update: setResolvedUrl(True, silent_stub) instead of
            # the (False, _empty_listitem) pattern from v0.7.14. Kodi's
            # behavior on (False, ...) was to mark the item as failed-to-
            # resolve, and when the playlist had nothing else to fall back
            # on (solo-slug tier scenario), Kodi fired the "one or more
            # items failed to play" dialog AND blocked the addon thread
            # behind that modal until dismissed -- in one observed case the
            # tv_loop went silent for 2h 43m waiting on the dialog. v0.7.14
            # changed (False) timing only, not the dialog itself.
            #
            # The silent stub is a 1-second silent .mp4 bundled at
            # resources/media/silent.mp4. Kodi plays it, hits natural end,
            # fires onPlayBackEnded -> tv_loop iterates. No dialog ever
            # appears. PlayerControl(Next) is no longer needed because the
            # natural end-of-stub advances the playlist on its own.
            logger._log(
                f"playvid: TV active + offline -> "
                f"silent stub setResolvedUrl(True) for slug={slug!r}"
            )
            xbmcplugin.setResolvedUrl(handle, True, _silent_stub_listitem())
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


def _silent_stub_path() -> str:
    """Resolve the on-disk path to the bundled 1-second silent stub.

    Used by the TV-active-offline branch in playvid: instead of telling
    Kodi the resolve failed (which fires the "one or more items failed
    to play" dialog and blocks the addon thread until dismissed), we
    return a successful resolve pointing at a tiny silent .mp4. Kodi
    plays it for ~1s, the player ends naturally, the TV loop's stop
    event fires, and the loop iterates without any user-visible dialog.

    The file lives at ``resources/media/silent.mp4`` inside the addon.
    Built once at package time (ffmpeg lavfi color + anullsrc, AAC, 2784
    bytes), so this is just a path-resolve at runtime.
    """
    try:
        import xbmcaddon
        addon = xbmcaddon.Addon()
        return str(Path(addon.getAddonInfo("path")) / "resources" / "media" / "silent.mp4")
    except Exception:  # pragma: no cover - outside Kodi
        # Fallback path used by tests; never resolves in production.
        return str(Path(__file__).parent.parent.parent / "resources" / "media" / "silent.mp4")


def _silent_stub_listitem() -> Any:
    """ListItem pointing at the silent stub for setResolvedUrl(True, ...).

    Kodi requires a real, playable URL on a successful resolve. If the
    stub file is missing for any reason (broken install, missing
    asset), fall back to an empty ListItem and let Kodi handle the
    resolve-failure path (worst case = the old dialog comes back, which
    is at least informative, not silently broken).
    """
    try:
        import xbmcgui
        path = _silent_stub_path()
        li = xbmcgui.ListItem(path=path)
        return li
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
    # took too long to return; dismiss on entry to every long-running
    # verb so the spinner never stays up over playing video.
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
        entries_path=path,  # v0.7.34: re-read each outer iter
    )


def refresh_offline_meta(
    handle: int,
    *,
    refresh_func: Any = None,
    notify_func: Any = None,
    spawn_func: Any = None,
    **_params: Any,
) -> None:
    """Kick off a one-shot bulk-refresh that re-pulls the affiliate
    feed and upserts every visible room into the model_meta DB.

    Useful when the user wants the "Last seen" timestamps and cached
    thumbnails on offline favs / TV-list refreshed RIGHT NOW instead
    of waiting for the next 10-minute auto-poll. Runs on a daemon
    thread so the directory click returns immediately and Kodi
    doesn't show a spinner.

    Notification at start ("Refreshing model info...") and finish
    ("Done. Refreshed N rooms").

    Dependency injection on the keyword-only args is for tests; the
    default values point at the production paths.
    """
    from resources.lib import logger

    if refresh_func is None:
        refresh_func = _tv_bulk_refresh
    if notify_func is None:
        notify_func = _notify
    if spawn_func is None:
        def _default_spawn(target: Any) -> None:
            t = threading.Thread(
                target=target,
                name="chaturbatetv-refresh-meta",
                daemon=True,
            )
            t.start()
        spawn_func = _default_spawn

    logger._log("refresh_offline_meta: kicking off background refresh")

    # Close the directory request immediately so Kodi's busy spinner
    # disappears -- the actual work happens on the daemon thread.
    _close_directory_handle(handle)

    def _bg() -> None:
        # If another refresh (auto-poll, favs view fetch, or a previous
        # click) is already running, don't start a duplicate. Toast the
        # user so they know their click was acknowledged but a no-op.
        # Race window: another caller could acquire between this check
        # and refresh_func()'s own acquire; in that case refresh_func
        # returns False and the user sees "failed", which is acceptable
        # noise vs the much worse "double-fetch the 12 MB JSON" outcome.
        if _BULK_REFRESH_LOCK.locked():
            notify_func(
                "Chaturbate TV",
                "A refresh is already in progress",
            )
            logger._log("refresh_offline_meta: skipped (lock held)")
            return
        notify_func(
            "Chaturbate TV",
            "Refreshing model info... (will toast when done)",
        )
        try:
            ok = refresh_func()
        except Exception as exc:
            logger._log(f"refresh_offline_meta: refresh raised err={exc!r}")
            ok = False
        if ok:
            count_str = ""
            try:
                count_str = f" ({len(_TV_BULK_CACHE['slugs'])} rooms)"
            except Exception:  # noqa: S110 - count is decorative; never block
                pass            # the 'done' toast on a count-format hiccup.
            notify_func(
                "Chaturbate TV",
                f"Model info refresh done{count_str}",
            )
            logger._log("refresh_offline_meta: done")
        else:
            notify_func(
                "Chaturbate TV",
                "Model info refresh failed - check logs",
            )
            logger._log("refresh_offline_meta: refresh returned False")

    spawn_func(_bg)


def refresh_one_model(
    handle: int,
    *,
    slug: str = "",
    fetch_biocontext_func: Any = None,
    fetch_status_func: Any = None,
    head_thumb_func: Any = None,
    notify_func: Any = None,
    **_params: Any,
) -> None:
    """Single-slug refresh wired to the per-row "Update model info"
    context menu (v0.7.23).

    v0.7.24 expanded: prefer biocontext (one HTTP, returns the full
    profile including last_broadcast + real_name + photo_sets +
    room_status). Falls back to the cheaper status+thumb-HEAD pair
    if biocontext is empty (network blip / account gone). Persists
    to the model_meta DB via upsert_biocontext or upsert_status.
    Synchronous because one slug is ~2-3 seconds total.

    Available on browse views, favs, TV-list rows -- right-click any
    model anywhere to refresh just that one.
    """
    from resources.lib import logger

    if notify_func is None:
        notify_func = _notify
    if fetch_biocontext_func is None:
        from resources.lib import cb_client as _cb_client
        fetch_biocontext_func = _cb_client.fetch_biocontext
    if fetch_status_func is None:
        from resources.lib import cb_client as _cb_client
        fetch_status_func = _cb_client.fetch_room_status_json
    if head_thumb_func is None:
        from resources.lib import cb_client as _cb_client
        head_thumb_func = _cb_client.head_thumb

    # Make sure Kodi's spinner clears either way.
    _close_directory_handle(handle)

    slug = (slug or "").strip()
    if not slug:
        logger._log("refresh_one_model: empty slug, skipping")
        return
    if not _is_valid_slug(slug):
        logger._log(f"refresh_one_model: invalid slug shape {slug!r}, skipping")
        return

    logger._log(f"refresh_one_model: starting for slug={slug!r}")
    from resources.lib import model_meta_store as mms
    import time as _time
    try:
        conn = mms.open_db(str(_model_meta_db_path()))
        try:
            bio = {}
            try:
                bio = fetch_biocontext_func(slug)
            except Exception:
                bio = {}
            if bio.get("_http_404"):
                # Profile page is gone -- account deleted or banned.
                # Stamp last_room_status='gone' so the [GONE] prefix
                # surfaces in the offline favs view.
                mms.upsert_status(
                    conn, slug, room_status="gone",
                    thumb_available=False, now=int(_time.time()),
                )
                room_status = "gone"
                summary = f"{slug}: gone (profile 404)"
            elif bio:
                mms.upsert_biocontext(conn, slug, bio, now=int(_time.time()))
                room_status = (bio.get("room_status") or "offline")
                last_bc_human = bio.get("time_since_last_broadcast") or ""
                summary = f"{slug}: {room_status}"
                if last_bc_human:
                    summary += f" -- last broadcast {last_bc_human}"
            else:
                # Fallback to cheap status + thumb HEAD.
                data = fetch_status_func(slug)
                room_status = (data.get("room_status") or "") or "offline"
                thumb_code = head_thumb_func(slug)
                thumb_ok = thumb_code == 200
                # v0.7.35 (audit agent 3 LOW): only override status to
                # "gone" when AJAX gave us no real signal. A transient
                # thumb-CDN miss (404) on a hidden / private / away
                # model used to clobber the more accurate state with
                # "gone" until the next deep refresh corrected her.
                # Now we only fall back to "gone" when AJAX status is
                # empty or plain offline.
                if thumb_code == 404 and room_status in ("", "offline"):
                    room_status = "gone"
                mms.upsert_status(
                    conn, slug,
                    room_status=room_status,
                    thumb_available=thumb_ok,
                    now=int(_time.time()),
                )
                summary = f"{slug}: {room_status}"
                if not thumb_ok:
                    summary += " (no thumb)"
            notify_func("Chaturbate TV", summary)
            logger._log(
                f"refresh_one_model: done slug={slug!r} {summary!r}"
            )
        finally:
            conn.close()
    except Exception as exc:
        logger._log(f"refresh_one_model: FAIL slug={slug!r} err={exc!r}")
        notify_func("Chaturbate TV", f"{slug}: refresh failed")


def view_model_info(
    handle: int,
    *,
    slug: str = "",
    fetch_biocontext_func: Any = None,
    **_params: Any,
) -> None:
    """v0.7.25: open a directory with the model's full profile and
    browseable photo_sets.

    The directory contains:

    1. A non-playable "Profile" entry whose ``plot`` is the full bio
       rendering (Sex, Age, Body, Fan club, About, Wish list, etc).
       Kodi shows the plot in the right pane on hover, so the user
       reads the bio without an extra click.
    2. One entry per ``bio_photo_sets`` set, each with the set's
       ``cover_url`` as the thumbnail. Public images only -- the
       photos themselves are paywalled, but the cover is the part
       biocontext gives us free.

    When the DB row for this slug has no ``bio_fetched_epoch`` (never
    crawled), we do an inline biocontext fetch + upsert so the user
    gets fresh data on first click. Synchronous; biocontext is one
    HTTP and ~1s in the common case.
    """
    from resources.lib import logger

    if fetch_biocontext_func is None:
        from resources.lib import cb_client as _cb_client
        fetch_biocontext_func = _cb_client.fetch_biocontext

    slug = (slug or "").strip()
    if not slug:
        logger._log("view_model_info: empty slug, closing directory")
        _close_directory_handle(handle)
        return
    if not _is_valid_slug(slug):
        logger._log(
            f"view_model_info: invalid slug shape {slug!r}, "
            f"closing directory"
        )
        _close_directory_handle(handle)
        return

    logger._log(f"view_model_info: starting slug={slug!r}")

    from resources.lib import model_meta_store as mms
    import time as _time
    row: dict[str, Any] = {}
    try:
        conn = mms.open_db(str(_model_meta_db_path()))
        try:
            row = mms.get_model(conn, slug) or {}
            # Fetch inline when the row has no bio coverage yet.
            if not row.get("bio_fetched_epoch"):
                bio: dict[str, Any] = {}
                try:
                    bio = fetch_biocontext_func(slug)
                except Exception as exc:
                    logger._log(
                        f"view_model_info: biocontext fetch failed "
                        f"slug={slug!r} err={exc!r}"
                    )
                    bio = {}
                if bio:
                    mms.upsert_biocontext(
                        conn, slug, bio, now=int(_time.time()),
                    )
                    row = mms.get_model(conn, slug) or {}
        finally:
            conn.close()
    except Exception as exc:
        logger._log(f"view_model_info: DB error slug={slug!r} err={exc!r}")
        row = {}

    _render_view_model_info(handle, slug=slug, row=row)


def _render_view_model_info(
    handle: int, *, slug: str, row: dict[str, Any],
) -> None:
    """Build the v0.7.27 view-info directory: a "View full profile"
    header plus one entry per non-empty bio field plus one entry per
    photo_set.

    Per-field entries put each fact on its own LEFT-side row so long
    bios don't get cut off in the right-pane plot. Click any field to
    open the textviewer dialog with the full multi-line bio. Click a
    photo_set to open its cover_url in Kodi's fullscreen picture
    viewer (the photos themselves are paywalled).
    """
    import xbmcgui
    import xbmcplugin
    from urllib.parse import urlencode
    from resources.lib import model_meta_store as mms
    import json as _json

    title = (row.get("real_name") or row.get("display_name")
             or row.get("last_subject") or slug)
    image = mms.image_for_row(row) or None

    plugin_prefix = "plugin://plugin.video.chaturbatetv/"
    profile_url = (
        f"{plugin_prefix}?{urlencode({'mode': 'show_profile', 'slug': slug})}"
    )

    # Header: click opens the full-bio scrollable textviewer dialog.
    header_label = f"[COLOR FF00d4ff][ View full profile: {title} ][/COLOR]"
    header_li = xbmcgui.ListItem(label=header_label)
    header_li.setProperty("IsPlayable", "false")
    if image:
        header_li.setArt({"thumb": image, "icon": image, "fanart": image})
    full_plot = mms.bio_full_plot_for_view_info(row)
    if full_plot:
        header_li.setInfo("video", {"plot": full_plot, "title": header_label})
    xbmcplugin.addDirectoryItem(
        handle=handle, url=profile_url, listitem=header_li, isFolder=True,
    )

    # Per-field entries -- one row per non-empty bio fact. Click any
    # of them to open the full-bio textviewer dialog (consistent
    # action regardless of which field is highlighted).
    for label, plot in mms.bio_field_entries(row):
        li = xbmcgui.ListItem(label=label)
        li.setProperty("IsPlayable", "false")
        if plot:
            li.setInfo("video", {"plot": plot, "title": label})
        xbmcplugin.addDirectoryItem(
            handle=handle, url=profile_url, listitem=li, isFolder=True,
        )

    # Photo sets -- each carries the cover_url as the thumbnail and
    # a show_picture URL so click opens the cover in Kodi's
    # fullscreen image viewer (the actual photos are paywalled).
    photo_sets_raw = row.get("bio_photo_sets_json") or ""
    photo_sets: list[dict[str, Any]] = []
    if photo_sets_raw:
        try:
            parsed = _json.loads(photo_sets_raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            photo_sets = [p for p in parsed if isinstance(p, dict)]

    for pset in photo_sets:
        name = (pset.get("name") or "Photo set").strip()
        cover = (pset.get("cover_url") or "").strip()
        # Token cost: biocontext gives ``tokens`` (or ``tip_amount`` in
        # older payloads). Fall back to 0 if neither is present.
        cost = pset.get("tokens")
        if not isinstance(cost, int):
            cost = pset.get("tip_amount")
            if not isinstance(cost, int):
                cost = 0
        is_video = bool(pset.get("is_video"))
        photo_count = pset.get("photo_count")
        duration_s = pset.get("video_duration_in_seconds")

        meta_bits: list[str] = []
        if is_video:
            if isinstance(duration_s, int) and duration_s > 0:
                m, s = divmod(duration_s, 60)
                meta_bits.append(f"{m}m{s:02d}s video")
            else:
                meta_bits.append("video")
        elif isinstance(photo_count, int) and photo_count > 0:
            meta_bits.append(f"{photo_count} photos")
        if cost > 0:
            meta_bits.append(f"{cost} tokens")
        meta_bits.append("paywalled")

        kind = "Video" if is_video else "Photo set"
        meta_str = ", ".join(meta_bits)
        label = f"[COLOR FF00d4ff][{kind}][/COLOR] {name} ({meta_str})"
        plot = (
            f"{name}\n\n"
            f"{kind} -- {meta_str}\n"
            f"Click to view the cover image fullscreen "
            f"(the {kind.lower()} content is paywalled)."
        )

        li = xbmcgui.ListItem(label=label)
        li.setProperty("IsPlayable", "false")
        if cover:
            li.setArt({"thumb": cover, "icon": cover, "fanart": cover})
        li.setInfo("video", {"plot": plot, "title": label})

        if cover:
            item_url = (
                f"{plugin_prefix}?"
                f"{urlencode({'mode': 'show_picture', 'url': cover})}"
            )
        else:
            # No cover URL on this set -- routing to show_profile
            # gives a useful click instead of a dead end.
            item_url = profile_url
        xbmcplugin.addDirectoryItem(
            handle=handle, url=item_url, listitem=li, isFolder=True,
        )

    xbmcplugin.setContent(handle, "videos")
    xbmcplugin.endOfDirectory(handle, succeeded=True)


def show_profile(
    handle: int,
    *,
    slug: str = "",
    **_params: Any,
) -> None:
    """v0.7.27: open a fullscreen scrollable textviewer dialog with
    the model's full bio. Wired to clicks on any view_model_info
    entry (header or per-field) so the user can read everything
    without right-pane truncation.

    Closes the directory with succeeded=False so Kodi keeps the user
    on the parent view_model_info listing after they dismiss the
    dialog.
    """
    from resources.lib import logger

    slug = (slug or "").strip()
    if not slug:
        logger._log("show_profile: empty slug, closing")
        _close_directory_handle(handle)
        return
    if not _is_valid_slug(slug):
        logger._log(
            f"show_profile: invalid slug shape {slug!r}, closing"
        )
        _close_directory_handle(handle)
        return

    logger._log(f"show_profile: slug={slug!r}")

    from resources.lib import model_meta_store as mms
    row: dict[str, Any] = {}
    try:
        conn = mms.open_db(str(_model_meta_db_path()))
        try:
            row = mms.get_model(conn, slug) or {}
        finally:
            conn.close()
    except Exception as exc:
        logger._log(f"show_profile: DB error slug={slug!r} err={exc!r}")

    title = (row.get("real_name") or row.get("display_name")
             or row.get("last_subject") or slug)
    plot = mms.bio_full_plot_for_view_info(row)
    if not plot:
        plot = f"No profile data cached for {slug} yet."
    heading = f"Profile: {title} ({slug})"

    try:
        import xbmcgui
        xbmcgui.Dialog().textviewer(heading, plot)
    except Exception as exc:
        logger._log(f"show_profile: textviewer failed slug={slug!r} err={exc!r}")

    # succeeded=False keeps the user on the parent view_model_info
    # listing after the dialog is dismissed.
    try:
        import xbmcplugin
        xbmcplugin.endOfDirectory(handle, succeeded=False)
    except Exception as exc:
        logger._log(f"endOfDirectory failed handle={handle} err={exc!r}")


def show_picture(
    handle: int,
    *,
    url: str = "",
    **_params: Any,
) -> None:
    """v0.7.27: open the given image URL in Kodi's fullscreen picture
    viewer via the ShowPicture builtin. Used by photo_set entries to
    surface the cover (the only public image we have for paywalled
    sets) at full resolution on click.

    v0.7.39 (audit pass #5 HIGH, agent 1): the URL is host-allowlisted
    AND has any ``)``, ``,``, or control chars rejected before being
    interpolated into the ShowPicture builtin. Pre-fix, a malicious
    cover_url with a `)` could break out of the builtin and chain
    another command (Quit, file:// read, etc.). Defense in depth:
    even if a future code path skipped the URL allowlist, the
    builtin-injection guard would still block the breakout.
    """
    from resources.lib import logger
    from resources.lib.cb_endpoints import is_trusted_url

    url = (url or "").strip()
    if not url:
        logger._log("show_picture: empty url, closing")
        _close_directory_handle(handle)
        return
    if not is_trusted_url(url):
        logger._log(
            "show_picture: REJECTED untrusted URL host "
            "(scheme/netloc not in CB allowlist)"
        )
        _close_directory_handle(handle)
        return
    if any(c in url for c in (")", ",", "\n", "\r")):
        # Belt-and-braces: even an https://*.mmcdn.com URL with a
        # crafted `)` would break out of ShowPicture(). Reject.
        logger._log(
            "show_picture: REJECTED URL with builtin-syntax char"
        )
        _close_directory_handle(handle)
        return

    logger._log(f"show_picture: url={url!r}")
    try:
        import xbmc
        xbmc.executebuiltin(f"ShowPicture({url})")
    except Exception as exc:
        logger._log(f"show_picture: builtin failed err={exc!r}")

    try:
        import xbmcplugin
        xbmcplugin.endOfDirectory(handle, succeeded=False)
    except Exception as exc:
        logger._log(f"endOfDirectory failed handle={handle} err={exc!r}")


def deep_refresh_offline_meta(
    handle: int,
    *,
    fav_slugs: list[str] | None = None,
    online_slugs: frozenset[str] | set[str] | None = None,
    fetch_status_func: Any = None,
    head_thumb_func: Any = None,
    fetch_biocontext_func: Any = None,
    notify_func: Any = None,
    spawn_func: Any = None,
    sleep_func: Any = None,
    rate_limit_seconds: float | None = None,
    **_params: Any,
) -> None:
    """v0.7.22 deep refresh: walk every offline fav and per-slug-probe
    the AJAX status + thumb HEAD, persisting into the model_meta DB.

    Different from ``refresh_offline_meta`` (which only re-fetches
    the bulk-online feed): this one specifically targets the slugs
    we DON'T expect to find in any feed. It tells us, for each
    offline fav, whether the account still exists (status=offline)
    or is banned/deleted (status=banned/deleted -> rendered with a
    [GONE] prefix in the offline favs view).

    Cost: ~2 HTTP requests per slug (POST + HEAD), rate-limited at
    one slug per ``rate_limit_seconds`` (default 1s). For 1228 favs
    that's ~20 minutes. Polite, well below CB's threshold.

    Lock-shared with the bulk refresh: if a refresh is in progress,
    we toast "already in progress" and exit. Auto-poll defers while
    we're crawling (lock semantics). 7-day auto-expire on the spawn.
    """
    from resources.lib import logger

    if notify_func is None:
        notify_func = _notify
    if spawn_func is None:
        def _default_spawn(target: Any) -> None:
            t = threading.Thread(
                target=target,
                name="chaturbatetv-deep-refresh",
                daemon=True,
            )
            t.start()
        spawn_func = _default_spawn
    if sleep_func is None:
        import time as _time

        def _default_sleep(s: float) -> None:
            _time.sleep(s)
        sleep_func = _default_sleep
    if fetch_status_func is None:
        from resources.lib import cb_client as _cb_client
        fetch_status_func = _cb_client.fetch_room_status_json
    if head_thumb_func is None:
        from resources.lib import cb_client as _cb_client
        head_thumb_func = _cb_client.head_thumb
    if fetch_biocontext_func is None:
        from resources.lib import cb_client as _cb_client
        fetch_biocontext_func = _cb_client.fetch_biocontext

    # Rate limit: read from settings if not explicitly set so the
    # user can dial it up (lower request rate -> less ban risk).
    if rate_limit_seconds is None:
        try:
            from resources.lib import addon_settings
            rate_limit_seconds = addon_settings.deep_refresh_rate_seconds()
        except Exception:
            rate_limit_seconds = 2.0

    # Compute the worklist BEFORE spawning so the user gets immediate
    # feedback on how many slugs we're about to crawl.
    if fav_slugs is None:
        favs = favs_store.load(_favs_path())
        fav_slugs = [f.slug for f in favs]
    if online_slugs is None:
        online_slugs = _TV_BULK_CACHE.get("slugs") or frozenset()

    from resources.lib import model_meta_store as mms
    worklist = mms.partition_offline_slugs(list(fav_slugs), online_slugs)
    eta_min = max(1, int(len(worklist) * rate_limit_seconds / 60))
    logger._log(
        f"deep_refresh_offline_meta: {len(worklist)} offline favs to crawl "
        f"(rate={rate_limit_seconds}s, ETA ~{eta_min}min)"
    )

    # Close the directory request immediately so Kodi's busy spinner
    # disappears -- the actual crawl runs on the daemon thread for
    # ~20 minutes and we don't want the user staring at a dialog
    # the whole time.
    _close_directory_handle(handle)

    def _bg() -> None:
        # v0.7.37: dedicated lock for the long-running deep crawl so
        # we don't starve the TV loop's bulk-cache TTL auto-refresh
        # (which only needs _BULK_REFRESH_LOCK for ~3s). Concurrent
        # bulk-poll + deep-crawl is fine: different CB endpoints,
        # different DB column sets, always-overwrite columns converge.
        if _DEEP_REFRESH_LOCK.locked():
            notify_func(
                "Chaturbate TV",
                "Deep refresh already in progress",
            )
            logger._log("deep_refresh_offline_meta: skipped (deep lock held)")
            return
        if not _DEEP_REFRESH_LOCK.acquire(blocking=False):
            notify_func(
                "Chaturbate TV",
                "Deep refresh already in progress",
            )
            logger._log("deep_refresh_offline_meta: skipped (deep lock raced)")
            return
        notify_func(
            "Chaturbate TV",
            f"Starting deep refresh: {len(worklist)} offline favs "
            f"(~{eta_min} min)",
        )
        counts: dict[str, int] = {"ok": 0, "gone": 0, "error": 0}
        # v0.7.38 (audit pass #4 HIGH #11): bail after N consecutive
        # sqlite OperationalError exceptions so a wedged DB doesn't
        # silently no-op a 20-min crawl. Only counts CONSECUTIVE
        # errors -- transient locks reset the counter on the next
        # successful upsert.
        consecutive_db_errors = 0
        _DB_ERROR_BAIL_THRESHOLD = 10
        try:
            import sqlite3 as _sqlite3
            db_path = str(_model_meta_db_path())
            conn = mms.open_db(db_path)
            try:
                import time as _time
                for i, slug in enumerate(worklist):
                    try:
                        # v0.7.24: biocontext is the primary source --
                        # ONE HTTP gives us last_broadcast + real_name +
                        # photo_sets + room_status + everything else.
                        # Empty response = network failure or account
                        # gone; falls through to status+thumb-HEAD as a
                        # corroboration / liveness check so we still
                        # detect the "deleted account" case.
                        bio = {}
                        try:
                            bio = fetch_biocontext_func(slug)
                        except Exception:
                            bio = {}
                        if bio.get("_http_404"):
                            # v0.7.28: profile page literally
                            # doesn't exist -> account banned/deleted.
                            # Hard-stamp gone so the [GONE] prefix
                            # surfaces in offline favs without a
                            # thumb-HEAD round trip.
                            mms.upsert_status(
                                conn, slug, room_status="gone",
                                thumb_available=False,
                                now=int(_time.time()),
                            )
                            status = "gone"
                        elif bio:
                            mms.upsert_biocontext(
                                conn, slug, bio, now=int(_time.time()),
                            )
                            status = (bio.get("room_status") or "offline")
                        else:
                            # Biocontext failed (401, network blip,
                            # non-JSON) -- the account may still
                            # exist. Fall back to the cheap status
                            # + thumb HEAD path so we still record
                            # SOMETHING about this slug.
                            data = fetch_status_func(slug)
                            status = (data.get("room_status") or "")
                            thumb_code = head_thumb_func(slug)
                            thumb_ok = thumb_code == 200
                            # v0.7.35: same protect-set narrowing as
                            # refresh_one_model. Only fall back to gone
                            # when AJAX gave no real signal.
                            if thumb_code == 404 and status in ("", "offline"):
                                status = "gone"
                            mms.upsert_status(
                                conn, slug,
                                room_status=status or "offline",
                                thumb_available=thumb_ok,
                                now=int(_time.time()),
                            )
                        if status in ("banned", "deleted", "gone"):
                            counts["gone"] += 1
                        else:
                            counts["ok"] += 1
                        # Successful upsert -> reset DB-error streak.
                        consecutive_db_errors = 0
                    except _sqlite3.OperationalError as exc:
                        # v0.7.38: distinguish DB errors from
                        # network/parse errors. A wedged DB (locked,
                        # disk full, schema mismatch) gives no value
                        # to the next 1227 slugs -- bail early.
                        consecutive_db_errors += 1
                        counts["error"] += 1
                        logger._log(
                            f"deep_refresh_offline_meta: slug={slug!r} "
                            f"DB FAIL err={exc!r} "
                            f"streak={consecutive_db_errors}"
                        )
                        if consecutive_db_errors >= _DB_ERROR_BAIL_THRESHOLD:
                            logger._log(
                                f"deep_refresh_offline_meta: bailing "
                                f"after {consecutive_db_errors} "
                                f"consecutive DB errors at slug "
                                f"{i + 1}/{len(worklist)}"
                            )
                            notify_func(
                                "Chaturbate TV",
                                f"Deep refresh aborted: DB unwritable "
                                f"after {consecutive_db_errors} tries "
                                f"({i + 1}/{len(worklist)} done)",
                            )
                            return
                    except Exception as exc:
                        counts["error"] += 1
                        consecutive_db_errors = 0
                        logger._log(
                            f"deep_refresh_offline_meta: slug={slug!r} "
                            f"FAIL err={exc!r}"
                        )
                    # Sleep between iterations, NOT after the last one
                    # (the user is waiting for the done toast).
                    if i + 1 < len(worklist):
                        sleep_func(rate_limit_seconds)
                    # Progress log every 100 slugs so a very long
                    # crawl is observable without being noisy.
                    if (i + 1) % 100 == 0:
                        logger._log(
                            f"deep_refresh_offline_meta: progress "
                            f"{i + 1}/{len(worklist)}"
                        )
            finally:
                conn.close()
            notify_func(
                "Chaturbate TV",
                f"Deep refresh done: {counts['ok']} ok, "
                f"{counts['gone']} gone, {counts['error']} errors",
            )
            logger._log(
                f"deep_refresh_offline_meta: done counts={counts!r}"
            )
        finally:
            _DEEP_REFRESH_LOCK.release()

    spawn_func(_bg)


def open_settings(handle: int, **_params: Any) -> None:
    """Open the addon's settings dialog.

    Kodi's ``Addon.OpenSettings(<id>)`` builtin is the canonical way to
    surface the settings dialog from inside a directory listing - same
    dialog the gear icon in the addon manager opens. This gives users
    a discoverable path from the main menu without leaving the addon.
    """
    from resources.lib import logger
    logger._log("open_settings: user requested settings")
    try:
        import xbmc
        xbmc.executebuiltin("Addon.OpenSettings(plugin.video.chaturbatetv)")
    except Exception as exc:
        logger._log(f"open_settings: builtin failed err={exc!r}")


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

    Scope is narrowed to the addon's own artwork only.
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

# v0.7.33: slugs we've discovered un-playable during the current TV
# session (silent stub played, AJAX refused HLS, etc). The bulk-cache
# refresh wholesale-overwrites ``_TV_BULK_CACHE['slugs']`` from the
# affiliate feed every TTL window -- if a slug is broadcasting but
# unwatchable from us (geo-block, region wall, transient cmaf, or a
# v0.7.31-style hidden show that briefly toggled public mid-poll), it
# would get re-added to the cache and the TV loop would chase it
# again. Subtracting this dict after each refresh stops the bounce.
# Cleared when TV mode exits (``tv_stop`` flips chaturbatetv_active
# off) so a session-long quirk doesn't permanently hide a model.
#
# v0.7.45 (model_a repro): the v0.7.33 design was a session-long
# ``set[str]``. A model marked offline (e.g., found in private show
# mode at the moment we tried her) would stay blocked for the rest
# of the session even if she returned to public broadcast hours
# later. model_a was caught in private show at 17:05; transitioned
# to public around 19:30; bulk_refresh logs from 19:13 onward show
# "session-blocked 1" filtering her out every cycle while
# model_b played at a lower priority tier. Fix: track the
# blocked-at epoch per slug and prune entries older than
# ``_OFFLINE_BLOCK_TTL_SEC`` at the top of each ``_tv_bulk_refresh``.
# After the TTL expires, the model gets a fresh chance.
_OFFLINE_SESSION_SLUGS: dict[str, float] = {}
# 15 minutes -- long enough to suppress private/public flapping in
# the affiliate feed (the original v0.7.33 bounce we want to keep
# preventing) but short enough that a model who recovers from a
# transient state gets re-considered within a single TV-mode session.
_OFFLINE_BLOCK_TTL_SEC: float = 900.0

# Non-blocking lock that serializes _tv_bulk_refresh() callers.
# Multiple call sites converge on this function: the TV loop's
# ``_make_bulk_is_live_func`` (auto-poll on TTL), favs_views' bulk
# refresh, and the v0.7.20 manual "Refresh offline model info" menu
# entry. Without coordination they could race and double-fetch a 12 MB
# response. Lock semantics: try-acquire only -- never block. Any caller
# that fails to acquire treats it as "another refresh just claimed
# this cycle" and falls back to the stale cache. The user-clicked
# manual handler additionally checks ``locked()`` before kicking off
# the bg worker so it can show a friendly "already refreshing" toast
# instead of misleadingly toasting "failed".
_BULK_REFRESH_LOCK = threading.Lock()

# v0.7.37 (race audit pass 1, agent 1 HIGH #1 + #2): guards
# ``_TV_BULK_CACHE['slugs']`` and ``_OFFLINE_SESSION_SLUGS`` against
# cross-thread mutation. Pre-fix:
#
# (1) ``_OFFLINE_SESSION_SLUGS`` (a plain ``set``) was added-to from
#     the TV-loop thread on silent-stub mark-offline AND read-iterated
#     by ``_tv_bulk_refresh`` in the same process. Python sets aren't
#     thread-safe across add+iter; under load this raised
#     ``RuntimeError: Set changed size during iteration`` or silently
#     dropped the just-marked slug from the subtraction.
#
# (2) ``_TV_BULK_CACHE['slugs']`` did read-modify-write in
#     ``_tv_bulk_mark_offline`` without a lock; concurrent
#     ``_tv_bulk_refresh`` could clobber the eviction (the v0.7.33
#     wedge through a side door).
#
# This is a fast-acquire lock -- holders only do a frozenset rebuild
# (microseconds). Distinct from _BULK_REFRESH_LOCK (which serializes
# the affiliate FETCH) and _DEEP_REFRESH_LOCK (the long crawl).
_TV_CACHE_LOCK = threading.Lock()


def _tv_cache_snapshot() -> frozenset[str]:
    """Atomic snapshot of the current bulk-live slug set. Use this in
    pick_target / is_live_func walks so within-walk drift can't make
    one slot see a slug live and the next slot see it offline.
    """
    with _TV_CACHE_LOCK:
        slugs = _TV_BULK_CACHE.get("slugs") or frozenset()
    if isinstance(slugs, frozenset):
        return slugs
    return frozenset(slugs)


# v0.7.37 (race audit pass 1, agent 1 MEDIUM #6): separate lock for
# the long-running deep refresh (~20 min). Pre-fix, deep_refresh held
# _BULK_REFRESH_LOCK for the entire crawl, which made the TV loop's
# TTL auto-refresh fall through to per-slug AJAX (60+ requests per
# pick walk) for the duration. Risked a CB ban from layered request
# rates. Now deep_refresh has its own lock, _BULK_REFRESH_LOCK stays
# short-lived for actual bulk-cache work, and the two can co-exist
# (they hit different CB endpoints and the model_meta DB writes are
# always-overwrite-friendly so concurrent writes converge correctly).
_DEEP_REFRESH_LOCK = threading.Lock()

# Affiliate watermarks (rotating array; same set favs_views uses).
_TV_BULK_WATERMARKS = (
    "C9m5N", "tfZSl", "jQrKO", "5XO2a", "WXomN",
    "zM6MR", "Lb2aB", "cIbs3", "mnzQo", "N6TZA",
)


def _tv_bulk_refresh() -> bool:
    """Single affiliate-onlinerooms fetch into ``_TV_BULK_CACHE``.

    Network failure preserves the stale set so a transient 5xx doesn't
    silently mark every model offline.

    v0.7.18: also persists every room we see into the model_meta sqlite
    DB so offline favs / TV-list / browse views can render with last-
    known thumbnail, subject, viewers, etc. The DB is append-overlay
    (sticky-on-non-null) so partial polls never wipe richer data
    collected earlier. Meta-store failure is swallowed -- TV mode
    keeps running even if sqlite is wedged.
    """
    import json as _json
    import random
    import time as _time
    from resources.lib import cb_client, cb_listing, logger
    from resources.lib.cb_endpoints import online_rooms_affiliate_url

    if not _BULK_REFRESH_LOCK.acquire(blocking=False):
        # Another caller is mid-fetch -- bail out without making a
        # duplicate request. The cache will be fresh once that one
        # completes. Returning False is what callers already do for
        # transient failures, so existing callers keep working.
        logger._log(
            "addon_actions._tv_bulk_refresh: another refresh in flight, "
            "skipping (cache stays at "
            f"{len(_TV_BULK_CACHE['slugs'])} slugs)"
        )
        return False

    try:
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
        # Parse once into raw rooms; pass to both the Model converter
        # (for the cache) and the meta-store upsert (for offline
        # rendering).
        try:
            parsed = _json.loads(body) if isinstance(body, (str, bytes)) else body
        except Exception:  # pragma: no cover - parse_affiliate_onlinerooms also handles
            parsed = []
        raw_rooms: list[Any] = parsed if isinstance(parsed, list) else []
        models = cb_listing.parse_affiliate_onlinerooms(raw_rooms)
        # v0.7.31: only public rooms make it into the bulk-live cache
        # so the TV loop never picks a hidden/private/paid-show slug
        # that resolves offline forever (the silent-stub-loop trigger
        # from v0.7.29). Non-public rooms still get persisted to the
        # meta DB below so favs view sees their thumb / status.
        public_slugs = frozenset(m.slug for m in models if m.is_live)
        skipped = sum(1 for m in models if not m.is_live)
        # v0.7.33: subtract any slug we marked offline during this TV
        # session (silent-stub played, AJAX refused HLS) so a wholesale
        # cache refresh doesn't re-add a known-unplayable slug. The
        # affiliate feed flips so often we'd otherwise chase the same
        # bad slug every TTL window for the rest of the session.
        # v0.7.37: snapshot the offline-session set under the cache
        # lock so a concurrent _tv_bulk_mark_offline can't add to it
        # mid-iteration (Python sets aren't thread-safe across
        # add+iter; RuntimeError or silent drop possible).
        # v0.7.45: prune entries older than _OFFLINE_BLOCK_TTL_SEC
        # before snapshotting. Pre-fix the blocklist was session-long
        # and a model who recovered from private/hidden mode (e.g.,
        # model_a 17:05 private -> 19:30 public) stayed blocked
        # for the rest of the session while a lower-tier model played.
        now_ts = _time.time()
        with _TV_CACHE_LOCK:
            expired = [
                slug for slug, blocked_at in _OFFLINE_SESSION_SLUGS.items()
                if now_ts - blocked_at > _OFFLINE_BLOCK_TTL_SEC
            ]
            for slug in expired:
                del _OFFLINE_SESSION_SLUGS[slug]
            offline_snapshot = frozenset(_OFFLINE_SESSION_SLUGS.keys())
            new_slugs = public_slugs - offline_snapshot
            _TV_BULK_CACHE["slugs"] = new_slugs
            _TV_BULK_CACHE["ts"] = now_ts
        if expired:
            logger._log(
                f"addon_actions._tv_bulk_refresh: pruned {len(expired)} "
                f"slugs from offline blocklist (TTL "
                f"{_OFFLINE_BLOCK_TTL_SEC:.0f}s elapsed): {expired!r}"
            )
        session_blocked = len(public_slugs) - len(new_slugs)
        logger._log(
            f"addon_actions._tv_bulk_refresh: refreshed slugs={len(new_slugs)} "
            f"(filtered {skipped} non-public from {len(models)} broadcasting; "
            f"session-blocked {session_blocked})"
        )
        if raw_rooms:
            try:
                from resources.lib import model_meta_store as mms
                db_path = str(_model_meta_db_path())
                conn = mms.open_db(db_path)
                try:
                    wrote = mms.upsert_rooms(
                        conn, raw_rooms, now=int(_time.time()), source="affiliate",
                    )
                    logger._log(
                        f"addon_actions._tv_bulk_refresh: meta upsert "
                        f"wrote={wrote} db={db_path}"
                    )
                finally:
                    conn.close()
            except Exception as exc:
                # Meta-store is non-critical; swallow so TV mode doesn't
                # break if sqlite is wedged or the DB file is briefly
                # unwritable.
                logger._log(
                    f"addon_actions._tv_bulk_refresh: meta upsert FAIL "
                    f"err={exc!r}"
                )
        return True
    finally:
        _BULK_REFRESH_LOCK.release()


def _tv_bulk_mark_offline(slug: str) -> None:
    """Remove ``slug`` from the cached live set AND record it in the
    session-long offline blocklist.

    Called from playvid (when per-slug AJAX confirms offline) and from
    the tv_loop silent-stub branch (when the playvid resolver fell
    through to the silent stub). The session blocklist stops the next
    bulk-cache refresh from re-adding a slug we just discovered to be
    un-playable.

    v0.7.37: both mutations now happen under ``_TV_CACHE_LOCK``. A
    concurrent ``_tv_bulk_refresh`` reassigning ``_TV_BULK_CACHE['slugs']``
    can no longer race the eviction (RMW is now a critical section);
    the ``_OFFLINE_SESSION_SLUGS`` set add can no longer race a
    refresh's iteration-difference.

    Idempotent for cache eviction; always records to the session set.

    v0.7.45: stores the blocked-at epoch instead of just adding to a
    plain set. ``_tv_bulk_refresh`` prunes entries older than
    ``_OFFLINE_BLOCK_TTL_SEC`` so a recovered model gets re-considered
    within the same TV session.
    """
    import time as _time
    from resources.lib import logger
    with _TV_CACHE_LOCK:
        _OFFLINE_SESSION_SLUGS[slug] = _time.time()
        current = _TV_BULK_CACHE["slugs"]
        if slug not in current:
            return
        new_slugs = frozenset(s for s in current if s != slug)
        _TV_BULK_CACHE["slugs"] = new_slugs
    logger._log(
        f"addon_actions._tv_bulk_mark_offline: {slug!r} removed from cache "
        f"({len(current)} -> {len(new_slugs)})"
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
        # v0.7.37: snapshot under the cache lock so ttl/refresh
        # decisions and the membership check both see the same
        # cache version. Without the snapshot, _tv_bulk_refresh
        # mid-walk could swap _TV_BULK_CACHE['slugs'] between
        # successive is_live calls in pick_target -- the same slug
        # would be live then offline within a single tier walk.
        with _TV_CACHE_LOCK:
            cur_slugs = _TV_BULK_CACHE["slugs"]
            cur_ts = _TV_BULK_CACHE["ts"]
        if (not cur_slugs or nowt - cur_ts > ttl_seconds):
            ok = _tv_bulk_refresh()
            with _TV_CACHE_LOCK:
                cur_slugs = _TV_BULK_CACHE["slugs"]
            if not ok and not cur_slugs:
                # Bulk failed AND we have no cached set (cold-start +
                # affiliate-endpoint outage). Fall back to per-slug
                # AJAX for THIS query so TV mode can pick a target.
                # Don't cache the per-slug answer; next call retries
                # bulk first so we recover automatically when the
                # affiliate endpoint comes back.
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
        return slug in cur_slugs

    return is_live


def tv_stop(handle: int, **_params: Any) -> None:
    """Clear the chaturbatetv_active flag so the running loop exits.

    User-facing recovery path: if the loop self-locked due to a
    glitch, this clears the flag so it can exit cleanly.

    v0.7.33: also clears _OFFLINE_SESSION_SLUGS so a session-long
    block doesn't permanently hide a slug. Next TV-mode start is a
    fresh blocklist; a slug that was unwatchable an hour ago gets a
    fresh bulk-cache reading at next start.
    """
    from resources.lib import logger, tv_state
    with _TV_CACHE_LOCK:
        _OFFLINE_SESSION_SLUGS.clear()
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
    # v0.7.19: batched meta lookup for all TV entries so each row can
    # surface the last-seen-online time on the right of the name.
    # Best-effort -- if the meta DB is missing or unreadable we just
    # render bare like before.
    meta_map: dict[str, dict[str, Any]] = {}
    try:
        from resources.lib import model_meta_store as mms
        slugs_for_meta = [_slug_from_url(e.url) for e in sorted_entries]
        conn = mms.open_db(str(_model_meta_db_path()))
        try:
            meta_map = mms.get_models(conn, slugs_for_meta)
        finally:
            conn.close()
    except Exception as exc:
        logger._log(f"addon_actions.tv_list: meta lookup FAIL err={exc!r}")
    for e in sorted_entries:
        slug = _slug_from_url(e.url)
        label = f"[COLOR FF00d4ff][P{e.priority:02d}][/COLOR] {e.name}"
        meta_row = meta_map.get(slug)
        plot: str | None = None
        image: str | None = None
        if meta_row:
            from resources.lib import model_meta_store as mms
            ago = mms.last_seen_ago_label(meta_row)
            if ago:
                # Suffix on the right of the name -- HALO cyan so it
                # blends with the existing [P##] tag styling.
                label = f"{label}  [COLOR FF8899bb]({ago} ago)[/COLOR]"
            # v0.7.22: prepend [GONE] for banned/deleted/gone accounts.
            gone_prefix = mms.label_prefix_for_row(meta_row)
            if gone_prefix:
                label = gone_prefix + label
            image = mms.image_for_row(meta_row)
            plot_str = mms.plot_for_offline_row(meta_row)
            plot = plot_str or None
        ctx = ctxmenu.build_ctxmenu(
            {"slug": slug, "name": e.name, "url": e.url},
            tv_entries=entries,
            favs=favs,
        )
        kodi_helpers.add_play_item(
            handle, label, slug=slug,
            image=image, plot=plot,
            ctx_items=ctx,
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
           confirm_func: Any = None,
           **_params: Any) -> None:
    """Add a model to the TV list. Idempotent: existing url -> no-op.

    v0.7.28: when ``priority`` isn't preset (the common ctxmenu path),
    a yes/no confirm dialog fires BEFORE the priority numpad. Kodi's
    numpad cancel is unreliable across versions -- pressing Back
    sometimes returns the default value, leaving the user stuck with
    an accidental Add. The yes/no in front gives a clean back-out
    path: Back/No on the confirm = no add, no numpad.

    Priority: if not provided as a query param, prompts via
    ``Dialog().numeric``. Clamps to 1..20.

    ``confirm_func`` is a DI seam so tests can supply a fake
    yes/no without xbmcgui.
    """
    from resources.lib import file_lock, logger
    if not slug:
        _notify("Chaturbate TV", "Add to TV: missing slug")
        return
    if not _is_valid_slug(slug):
        logger._log(f"addon_actions.tv_add: invalid slug shape {slug!r}")
        _notify("Chaturbate TV", "Add to TV: invalid slug")
        return
    path = store_path if store_path is not None else _tv_path()
    target_url = url or f"https://chaturbate.com/{slug}/"
    # First load is unlocked: we just need to short-circuit on
    # "already in list" without prompting the user. The actual mutation
    # re-loads under the lock so a sibling process's add doesn't get
    # clobbered (race-audit pass 1, agent 3 HIGH #2).
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
    if not priority:
        if confirm_func is None:
            confirm_func = _confirm_add_to_tv
        if not confirm_func(name or slug):
            logger._log(f"addon_actions.tv_add: cancelled by user slug={slug!r}")
            return
    p = _resolve_priority(priority, name or slug)
    if p is None:
        return
    with file_lock.locked(path):
        # Re-load under the lock so a concurrent sibling add hasn't
        # already added this slug (we'd double-add otherwise).
        entries = tv_store.load(path)
        if any(e.url == target_url for e in entries):
            logger._log(
                f"addon_actions.tv_add: {slug!r} added concurrently; "
                f"no-op"
            )
            _notify("Chaturbate TV", f"{slug} was just added")
            return
        entries.append(TVEntry(name=name or slug, url=target_url, priority=p))
        tv_store.save(path, entries)
    logger._log(f"addon_actions.tv_add: added {slug!r} P{p}")
    _notify("Chaturbate TV", f"Added {slug} (priority {p})")


def _confirm_add_to_tv(name: str) -> bool:
    """Yes/no dialog asking the user to confirm before tv_add prompts
    for a priority. Returns True on Yes, False on No / Back / dialog
    failure (defensive default = cancel to avoid surprise adds)."""
    from resources.lib import logger
    try:
        import xbmcgui
        return bool(xbmcgui.Dialog().yesno(
            "Add to TV",
            f"Add [B]{name}[/B] to the TV priority list?",
        ))
    except Exception as exc:
        logger._log(f"_confirm_add_to_tv: dialog failed err={exc!r}")
        return False


def tv_remove(handle: int, slug: str = "", url: str = "",
              store_path: Path | None = None,
              **_params: Any) -> None:
    """Remove a model from the TV list. v0.7.37: flock-protected RMW."""
    from resources.lib import file_lock, logger
    if not slug and not url:
        _notify("Chaturbate TV", "Remove from TV: missing slug/url")
        return
    target_url = url or f"https://chaturbate.com/{slug}/"
    path = store_path if store_path is not None else _tv_path()
    with file_lock.locked(path):
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
    is empty. v0.7.37: flock-protected RMW.
    """
    from resources.lib import file_lock, logger
    if not slug and not url:
        _notify("Chaturbate TV", "Edit TV: missing slug/url")
        return
    target_url = url or f"https://chaturbate.com/{slug}/"
    path = store_path if store_path is not None else _tv_path()
    # Unlocked load is fine for the prompt phase: we just need to find
    # the current priority for the numpad default. Mutation is locked.
    entries = tv_store.load(path)
    cur = next((e for e in entries if e.url == target_url), None)
    if cur is None:
        logger._log(f"addon_actions.tv_edit: {slug!r} not in list")
        _notify("Chaturbate TV", f"{slug or 'entry'} not in TV list")
        return
    new_p = _resolve_priority(priority, cur.name, default=cur.priority)
    if new_p is None:
        return
    with file_lock.locked(path):
        # Re-load under the lock to merge with any sibling-process
        # mutations that happened during the (possibly long) priority
        # numpad prompt.
        entries = tv_store.load(path)
        new_entries = [
            TVEntry(name=e.name, url=e.url, priority=new_p)
            if e.url == target_url else e
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


def _close_directory_handle(handle: int) -> None:
    """Tell Kodi we're done with this plugin invocation so the busy
    spinner stops.

    Plugin URLs that do an action-and-return (rather than populate a
    directory) still need to call ``endOfDirectory`` -- otherwise
    Kodi waits for the directory contents that never arrive and
    keeps the busy dialog visible until it times out (~30s).

    succeeded=False keeps the user on the parent menu rather than
    trying to navigate into an empty directory we just declared.
    """
    try:
        import xbmcplugin
        if handle is not None and handle >= 0:
            xbmcplugin.endOfDirectory(handle, succeeded=False)
    except Exception:
        return
