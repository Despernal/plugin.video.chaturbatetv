"""TV mode outer loop and ``_TVPlayer`` subclass.

Bug-survivor loop shape we earned over many iterations:

1. Outer guard: refuse to start if ``chaturbatetv_active`` is already
   ``"1"`` (manual reset path is provided separately, but we won't
   double-spawn the loop).
2. Set ``chaturbatetv_active=1``.
3. Outer iter (try/except, consecutive-error cap):
   a. Load + sort the tv.json list.
   b. ``pick_target`` (random within highest live tier - Lesson 14).
   c. If no live target: hand off to ``screensaver.run`` for the idle
      walk; resume when re-walk finds a live entry, exit when user
      dismisses or the active flag clears.
   d. Reset ALL player state for this iteration (Lesson 10).
   e. Build playlist of every live model in the target tier; track
      the exact set of plugin URLs in ``player.queued_paths`` so
      ``onAVStarted`` can distinguish internal advance from user
      takeover (Lesson 11).
   f. ``Player().play(playlist)``; wait up to 30s for ``isPlaying()``.
   g. Inner monitor loop: every step seconds, check switched + active;
      every poll_seconds, check for a promotion candidate
      (``min_priority=target_priority``, strict).
   h. On loop exit: classify the stop. ``decide_after_stop`` says
      whether to exit (real user stop) or fall through. Natural
      playlist end + double-tap-stop within 5s also exits.
4. ``finally``: clear ``chaturbatetv_active`` and log.

For testability, ``run_once_for_test`` wraps the same logic but takes
an injected runtime (the ``tests.kodi_mock`` harness), entries, and
fns, with a cap on outer iterations.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from resources.lib import tv_classify, tv_select, tv_state
from resources.lib.cb_models import TVEntry


_PLUGIN_PREFIX = "plugin://plugin.video.chaturbatetv/"
# Single source of truth: the literal lives in tv_state.ACTIVE_KEY.
_ACTIVE_KEY = tv_state.ACTIVE_KEY


def _safe_log(msg: str) -> None:
    try:
        from resources.lib import logger
        logger._log(msg)
    except Exception:
        return


_PENDING_PLAY_KEY = "chaturbatetv_pending_play_epoch"
_PENDING_PLAY_TTL_SEC = 5.0
# v0.7.50: caching-wedge watchdog. Closes the gap left by the v0.7.42
# stall watchdog. The 0.7.42 watchdog watches getTime() advancement, but
# a decoder freeze where audio creeps wildly out-of-sync (1603-89463s
# drift observed 2026-05-10 07:30 CDT) keeps getTime() moving slowly so
# 0.7.42 never trips. Meanwhile Kodi already knows the stream is wedged
# -- it draws a "Loading X%" overlay -- via the Player.Caching condition.
# Read it directly and trip if Caching is True for more than the grace
# window. Verified in production via probe-xbmc: cond Player.Caching is the
# exact source of truth for the on-screen caching overlay.
_CACHING_WEDGE_GRACE_SEC = 120.0

# v0.7.52: post-Stop wedge watchdog. Production wedge 2026-05-17:
# hls_proxy fired PlayerControl(Stop) 3x after a 5-attempt
# reconnect GIVE UP, but Kodi's player never honored any of them --
# isPlaying() stayed True for 7+ hours, the inner monitor loop's
# ``while player.isPlaying():`` never exited, no heartbeats, Kodi UI
# stuck on a busy spinner.
#
# Both the v0.7.41 stall watchdog (getTime) and the v0.7.50 caching
# watchdog (Player.Caching) queried the *player* for state -- but the
# player itself was unresponsive, so neither tripped. The fix uses
# an EXTERNAL signal: hls_proxy stamps this Window(10000) property
# every time it fires force_player_stop, and tv_loop trips if
# isPlaying() is still True grace_sec past the most recent stamp.
_FORCE_STOP_AT_KEY = "chaturbatetv_force_stop_at"
_STOP_WEDGE_GRACE_SEC = 30.0

# v0.7.58 OUT-OF-LOOP progress watchdog. Every existing watchdog runs INSIDE
# the inner monitor loop, so when the loop itself hangs (a modal dialog
# blocking the main thread -- the v0.7.15 'playback failed' freeze -- or the
# silent-stub/post-stop wedge that blocks before the next heartbeat) they are
# blind, and the only recovery was a Kodi restart (3 wedges: 05-29, 06-03,
# 06-09). The tv_play loop now stamps this Window(10000) prop with
# str(time.time()) on every sign of life (iter start, onAVStarted, each
# inner-loop heartbeat). A separate daemon thread -- which reads ONLY window
# props, never the (possibly hung) player API -- trips if the age exceeds
# _WEDGE_THRESHOLD_SEC while TV mode is active. POSITIVE-progress signal: a
# healthy stream refreshes <=60s so it can NEVER look wedged, unlike the
# v0.7.52 force-stop-stamp approach that adopted stale cross-iter stamps and
# false-positived (killed 6 healthy streams -> v0.7.53). 150s = 2.5x margin.
_PROGRESS_AT_KEY = "chaturbatetv_tv_progress_at"
_WEDGE_THRESHOLD_SEC = 150.0
_WEDGE_BACKOFF_SEC = 90.0
_WEDGE_POLL_SEC = 20.0

_SILENT_STUB_SLUG_KEY = "chaturbatetv_silent_stub_slug"
_SILENT_STUB_EPOCH_KEY = "chaturbatetv_silent_stub_epoch"
# v0.7.49: bumped from 5.0s. Production wedge 2026-05-08 10:47 CDT:
# the zombie-stop race takes ~5+ seconds end-to-end (5 proxy reconnect
# attempts at ~1s each + exit-log overhead), so the v0.7.48 5s TTL
# missed the live case. Diff was exactly 5.0s -> fallback returned
# empty -> mark-offline skipped -> next iter re-picked offline slug ->
# wedge in xbmc.Player().play(). 15s gives plenty of headroom.
_SILENT_STUB_TTL_SEC = 15.0


def _pending_play_recent() -> bool:
    """True if the playvid handler stamped the in-addon-switch marker
    within the last ``_PENDING_PLAY_TTL_SEC`` seconds.

    Marker lives on the global Kodi home window (id 10000) so it's
    visible across the addon's separate playvid + tv_loop processes.
    Empty / missing / unparseable / stale: False.
    """
    try:
        import time as _time
        import xbmcgui
        raw = xbmcgui.Window(10000).getProperty(_PENDING_PLAY_KEY)
        if not raw:
            return False
        return (_time.time() - float(raw)) < _PENDING_PLAY_TTL_SEC
    except Exception:
        return False


def _silent_stub_pending_slug() -> str:
    """Return the slug for which playvid recently served a silent stub,
    or empty string if no fresh marker is set.

    v0.7.48 fix: when an old zombie proxy fires PlayerControl(Stop) at
    the exact moment the silent stub is starting (cross-process race),
    Kodi never fires onAVStarted for the stub, so ``tracked_file``
    stays None. The pre-fix mark-offline gate keyed off tracked_file
    alone and so silently no-op'd, leaving the slug in the cache and
    the loop re-picking it forever (eventually wedging Kodi's player).

    playvid stamps Window(10000) properties when serving the silent
    stub: the slug + the current epoch. We read both here. If the
    epoch is fresh (< ``_SILENT_STUB_TTL_SEC`` old, currently 15s),
    return the slug; else return "" (stale, never set, malformed).
    """
    try:
        import time as _time
        import xbmcgui
        win = xbmcgui.Window(10000)
        slug = win.getProperty(_SILENT_STUB_SLUG_KEY)
        if not slug:
            return ""
        epoch_raw = win.getProperty(_SILENT_STUB_EPOCH_KEY)
        if not epoch_raw:
            return ""
        if (_time.time() - float(epoch_raw)) >= _SILENT_STUB_TTL_SEC:
            return ""
        return slug
    except Exception:
        return ""


def _read_caching_state() -> tuple[bool, str]:
    """Read Kodi's current Player.Caching condition + Player.CacheLevel.

    These are the same values Kodi uses internally to render the
    "Loading X%" buffering overlay. Best-effort: if xbmc isn't
    importable (test context) returns (False, '?') so the watchdog
    never trips spuriously when the API isn't available.
    """
    try:
        import xbmc
        is_caching = bool(xbmc.getCondVisibility("Player.Caching"))
        level = xbmc.getInfoLabel("Player.CacheLevel") or ""
        return is_caching, level
    except Exception:
        return False, "?"


def _is_caching_wedged(
    caching_started_at: float | None,
    is_caching_now: bool,
    now: float,
    grace_sec: float,
) -> tuple[bool, float | None]:
    """v0.7.50 pure helper: track how long Player.Caching has been True.

    Returns ``(wedged, new_caching_started_at)``. The caller threads the
    state through the per-tick monitor loop:

    - ``is_caching_now`` False -> clear the timer; (False, None)
    - first tick of caching=True -> start the timer; (False, now)
    - caching continues within grace -> (False, started_at) preserve
    - caching exceeds grace -> (True, started_at) trip + keep the
      original start time so the caller can log how long it was stuck

    Pure function: no xbmc imports, no time.time() calls, fully
    deterministic given inputs. Tests pin the boundary behavior.
    """
    if not is_caching_now:
        return False, None
    if caching_started_at is None:
        return False, now
    if (now - caching_started_at) > grace_sec:
        return True, caching_started_at
    return False, caching_started_at


def _read_force_stop_at() -> float | None:
    """Read the most recent force_player_stop wall-clock from Window(10000).

    hls_proxy._force_player_stop stamps Window(10000).chaturbatetv_force_stop_at
    with str(time.time()) right after firing PlayerControl(Stop). We read it
    here from the tv_loop process (separate from the playvid/hls_proxy
    process). Best-effort: missing / unparseable / xbmcgui-not-importable
    (test context) returns None so the watchdog never trips spuriously.
    """
    try:
        import xbmcgui
        raw = xbmcgui.Window(10000).getProperty(_FORCE_STOP_AT_KEY)
        if not raw:
            return None
        return float(raw)
    except Exception:
        return None


def _stamp_progress() -> None:
    """Stamp Window(10000) with 'the TV loop made progress just now' (v0.7.58).

    Best-effort: in pure-test contexts xbmcgui isn't importable, so this is a
    no-op there. Cheap setProperty; called at iter start, onAVStarted, and
    each inner-loop heartbeat.
    """
    try:
        import time

        import xbmcgui
        xbmcgui.Window(10000).setProperty(_PROGRESS_AT_KEY, str(time.time()))
    except Exception:
        # best-effort; xbmcgui not importable in pure-test contexts
        return


def _read_progress_at() -> float | None:
    """Read the last progress stamp; None if missing/unparseable (so the
    watchdog never trips spuriously, e.g. before the first stamp)."""
    try:
        import xbmcgui
        raw = xbmcgui.Window(10000).getProperty(_PROGRESS_AT_KEY)
        if not raw:
            return None
        return float(raw)
    except Exception:
        return None


def _wedge_recover() -> None:
    """In-process wedge recovery (v0.7.58). Dismiss any modal dialog blocking
    the main thread (the v0.7.15 'playback failed' / busy-spinner freeze) and
    stop the player so the hung loop can advance. Re-stamps progress so the
    recovered loop resets the clock and the watchdog backs off. If a harder
    Kodi-level hang survives this, the autonomous cron's Kodi restart is still
    the backstop -- but this catches the common modal-dialog case in-process.
    """
    _safe_log("tv_loop: WEDGE-WATCHDOG recover -> Dialog.Close(all) + Stop")
    try:
        import xbmc
        xbmc.executebuiltin("Dialog.Close(all,true)")
        xbmc.executebuiltin("PlayerControl(Stop)")
    except Exception:  # noqa: S110 - best-effort
        pass
    _stamp_progress()


def _run_progress_watchdog(
    *,
    monitor: Any,
    is_active: Callable[[], bool],
    recover_fn: Callable[[], None] | None = None,
    threshold_s: float = _WEDGE_THRESHOLD_SEC,
    backoff_s: float = _WEDGE_BACKOFF_SEC,
    poll_s: float = _WEDGE_POLL_SEC,
) -> None:
    """Daemon-thread target: the out-of-loop wedge watchdog (v0.7.58).

    Reads ONLY window props (never the player API), so it stays responsive
    even when the main invoker thread is hung. Fires ``recover_fn`` when
    ``tv_classify.watchdog_should_recover`` says the loop has made no progress
    for ``threshold_s``, backing off ``backoff_s`` between fires. Exits when
    TV mode goes inactive or Kodi aborts.
    """
    import time
    recover = recover_fn if recover_fn is not None else _wedge_recover
    last_recover_at: float | None = None
    while is_active():
        if monitor.waitForAbort(poll_s):
            return
        if not is_active():
            return
        now = time.time()
        if tv_classify.watchdog_should_recover(
            progress_at=_read_progress_at(),
            now=now,
            active=True,
            last_recover_at=last_recover_at,
            threshold_s=threshold_s,
            backoff_s=backoff_s,
        ):
            _safe_log(
                f"tv_loop: WEDGE-WATCHDOG tripped (no progress >{threshold_s:.0f}s "
                f"while active); firing recovery"
            )
            recover()
            last_recover_at = now


def _select_stop_signal(
    new_stop_at: float | None,
    play_start_at: float,
) -> float | None:
    """v0.7.53 pure helper: gate Window-prop stamps to current play session.

    The v0.7.52 watchdog tripped on stale stamps from prior iters because
    Window(10000).chaturbatetv_force_stop_at persists across iter
    rotations (it lives on the Kodi-global window, not per-iter local
    state). Production false-positive 2026-05-17 10:11-10:33 CDT: 6
    healthy streams killed in 22min because each fresh iter
    immediately adopted the previous iter's exit-time stamp.

    Filter rule: only stamps from at-or-after the current play_start_at
    are in-session signals. Earlier stamps are zombies, return None.
    Inclusive bound (>=) handles microsecond-jitter on a stamp written
    within the same instant as play_start was captured.

    Pure function: no xbmc, no time.time(), fully deterministic.
    """
    if new_stop_at is None or new_stop_at < play_start_at:
        return None
    return new_stop_at


def _is_stop_wedged(
    force_stop_at: float | None,
    is_playing_now: bool,
    now: float,
    grace_sec: float,
) -> tuple[bool, float | None]:
    """v0.7.52 pure helper: detect when the player ignored a stop request.

    Returns ``(wedged, new_force_stop_at)``. Threading model mirrors
    :func:`_is_caching_wedged`:

    - ``is_playing_now`` False -> stop worked (or never armed); (False, None)
    - no stop has been requested yet -> (False, None)
    - stop requested, still playing within grace -> (False, force_stop_at)
    - stop requested, still playing past grace -> (True, force_stop_at)

    The "latest request wins" semantics are upstream of this helper:
    the caller reads the Window-property value (always the most recent
    stamp from hls_proxy) and passes it in. A fresh request resets the
    grace simply because (now - force_stop_at) becomes small again.

    Pure function: no xbmc imports, no time.time(), fully deterministic.
    """
    if not is_playing_now:
        return False, None
    if force_stop_at is None:
        return False, None
    if (now - force_stop_at) > grace_sec:
        return True, force_stop_at
    return False, force_stop_at


def _current_dialog_id() -> int:
    """Return the id of the topmost Kodi modal dialog, or 0 if none.

    Used by the diagnostic logs around `xbmc.Player().play()` and the
    inner monitor loop heartbeat. Window id 10100 is the "playback
    failed" dialog historically; anything non-zero means a modal is
    obstructing user input and may be holding the addon thread on
    Player API calls. Best-effort: if xbmcgui isn't importable (test
    context) we just report 0.
    """
    try:
        import xbmcgui
        return int(xbmcgui.getCurrentWindowDialogId())
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# _TVPlayer
# --------------------------------------------------------------------------- #


def _build_player_class() -> type:
    """Build _TVPlayer lazily so xbmc.Player isn't required at module-load."""
    import xbmc

    class _TVPlayerImpl(xbmc.Player):
        """Subclass of ``xbmc.Player`` that records lifecycle events.

        The flags get reset between outer iterations via
        :func:`reset_for_iteration` (Lesson 10).
        """

        def __init__(self) -> None:
            super().__init__()
            self.user_stopped: bool = False
            self.tracked_file: str | None = None
            self.switched: bool = False
            # v0.7.41: set by the inner monitor loop's stall watchdog
            # when ``getTime()`` hasn't advanced for ``stall_seconds``.
            # Distinct from ``user_stopped`` so the post-loop classify
            # path can fall through cleanly without prompting "Exit TV
            # mode?". ISA-decoder freezes (corrupt CMAF, ProcessMoof
            # TRAF errors) leave ``isPlaying()`` returning True forever,
            # so the loop needs an out-of-band signal.
            self.stall_detected: bool = False
            self.idle_at_stop: int = 0
            self.current_playlist_path: str = ""
            # v0.7.34: original slug-bearing plugin URL queued for
            # this slot. ``current_playlist_path`` is whatever Kodi
            # ends up actually playing (post-resolution: localhost
            # proxy or silent stub), so ``slug=`` never appears there
            # and ``_classify_after_stop`` couldn't tell the user-
            # stopped-but-model-live disambiguator apart from the
            # always-fallthrough case. With the queued URL captured
            # alongside, _classify_after_stop can compute model_live
            # against the actual queued slug.
            self.current_queued_plugin_url: str = ""
            self.playlist_ended_naturally: bool = False
            self.last_natural_end_time: float = 0.0
            self.queued_paths: set[str] = set()

        def reset_for_iteration(self) -> None:
            """Clear all per-iteration event state.

            ``last_natural_end_time`` is preserved across iterations
            for ``classify_stop`` book-keeping (currently unused for
            decisions since v0.7.13 dropped the double-tap exit, but
            kept for diagnostic logs).
            """
            self.user_stopped = False
            self.tracked_file = None
            self.switched = False
            self.stall_detected = False
            self.idle_at_stop = 0
            self.current_playlist_path = ""
            self.current_queued_plugin_url = ""
            self.playlist_ended_naturally = False
            self.queued_paths = set()

        def onAVStarted(self) -> None:
            _stamp_progress()  # v0.7.58: playback started = a sign of life
            try:
                cur = self.getPlayingFile()
            except Exception:
                cur = ""
            self.current_playlist_path = cur
            # v0.7.34: capture the queued plugin URL alongside the
            # resolved path. Kodi's playlist keeps the original queued
            # URL accessible via ``pl[pos].getPath()`` during playback,
            # even though ``getPlayingFile()`` returns the post-
            # resolution URL.
            queued_url = ""
            try:
                pl = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
                pos = pl.getposition()
                if 0 <= pos < pl.size():
                    queued_url = pl[pos].getPath() or ""
            except Exception as exc:
                _safe_log(
                    f"_TVPlayer.onAVStarted: queued URL capture failed "
                    f"err={exc!r}"
                )
                queued_url = ""
            if queued_url and "slug=" in queued_url:
                self.current_queued_plugin_url = queued_url
            internal = _is_internal_advance(cur, self.queued_paths)
            if not internal and queued_url and queued_url in self.queued_paths:
                # ``cur`` is the post-resolution URL (a localhost proxy
                # URL the playvid resolver swapped in), so it never
                # appears in ``queued_paths``. But Kodi keeps the
                # original queued plugin URL accessible via
                # ``pl[pos].getPath()`` -- if THAT URL is one we queued,
                # this is an internal advance, not a takeover.
                #
                # v0.7.40 fix: replaced a size-equality fallback (
                # ``pl_size == len(queued_paths)``) that misclassified
                # takeovers as internal whenever the queued tier had
                # exactly one model: 1 == 1 collides with the user's
                # one-item direct-play replacement. Comparing the queued
                # URL directly is insensitive to playlist size and
                # cleanly separates "ours" from "theirs."
                internal = True
            if self.tracked_file is None:
                self.tracked_file = cur
                _safe_log(
                    f"_TVPlayer.onAVStarted: tracking {cur!r} internal={internal}"
                )
            elif cur and cur != self.tracked_file:
                self.tracked_file = cur
                if internal:
                    _safe_log(
                        f"_TVPlayer.onAVStarted: internal advance to {cur!r}"
                    )
                else:
                    self.switched = True
                    _safe_log(
                        f"_TVPlayer.onAVStarted: TAKEOVER detected url={cur!r}"
                    )

        def onPlayBackStopped(self) -> None:
            self.user_stopped = True
            try:
                self.idle_at_stop = int(xbmc.getGlobalIdleTime())
            except Exception:
                self.idle_at_stop = 0
            try:
                pl = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
                at_end = tv_classify.is_natural_playlist_end(
                    pl.getposition(), pl.size(),
                )
            except Exception:
                at_end = False
            now = time.time()
            self.playlist_ended_naturally = tv_classify.classify_stop(
                at_end, self.last_natural_end_time, now,
            )
            if at_end:
                self.last_natural_end_time = now
            _safe_log(
                f"_TVPlayer.onPlayBackStopped: idle_at_stop={self.idle_at_stop}s "
                f"at_end={at_end} natural={self.playlist_ended_naturally}"
            )

    return _TVPlayerImpl


def __getattr__(name: str) -> Any:
    """Lazy attribute access so importing tv_loop in a pure test (no
    xbmc available) still works for the helper functions.
    """
    if name == "_TVPlayer":
        return _build_player_class()
    raise AttributeError(name)


# --------------------------------------------------------------------------- #
# Pure helpers (testable without Kodi)
# --------------------------------------------------------------------------- #


def _is_internal_advance(cur: str | None, queued_paths: set[str]) -> bool:
    """Wrap tv_classify.is_internal_advance with the right type contract."""
    return tv_classify.is_internal_advance(cur, queued_paths)


def _was_silent_stub_played(tracked_file: str | None) -> bool:
    """True if the inner-loop's tracked file matches the offline-skip
    silent stub at resources/media/silent.mp4 -- that's how playvid
    signals "this slug is offline" without firing a Kodi failure
    dialog.

    The outer loop uses this signal to drop the slug from the local
    bulk-live cache so pick_target won't re-pick it on the next iter.
    Without this, a solo-tier offline model causes the loop to spin
    forever: pick -> playvid offline -> stub plays 1s -> pick same
    slug -> stub again. ``addon_actions._tv_bulk_mark_offline`` does
    work, but only INSIDE the process that calls it; playvid runs in
    a separate Kodi-spawned default.py process so its cache mutation
    never reaches the TV-loop process. This in-loop detection is the
    fix.
    """
    if not tracked_file:
        return False
    return "silent.mp4" in tracked_file


def _slug_from_playlist_path(path: str | None) -> str:
    """Extract the slug query param from a queued playvid plugin URL.

    Each tier playlist item is built by ``_build_playlist_url`` with
    ``mode=playvid&slug=<slug>&name=<name>``. After playvid has
    resolved that URL into a localhost proxy URL (live) or the silent
    stub (offline), the queued ORIGINAL is what playlist[pos].getPath
    returns. This helper pulls the slug out so the silent-stub-detection
    can mark exactly that slug offline.
    """
    if not path:
        return ""
    try:
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(path).query)
        return (qs.get("slug") or [""])[0]
    except Exception:
        return ""


def _should_attempt_silent_stub_mark(player_state: Any) -> bool:
    """v0.7.33: gate for the silent-stub-mark-offline branch.

    Returns True when the inner monitor loop just finished playing the
    silent stub and we should treat it as a "this slug is offline"
    signal. Two conditions: the silent stub actually played AND the
    user didn't switch to something else (which would have populated
    a different ``tracked_file``).

    Notably does NOT gate on ``player.user_stopped``: on Kodi versions
    where the last item of a playlist ending fires
    ``onPlayBackStopped`` instead of ``onPlayBackEnded``,
    ``user_stopped`` is True for natural-end stub plays. The pre-0.7.33
    gate skipped the mark in those cases, leaving the loop to re-pick
    the same offline slug forever (audit agent 2 HIGH #1, exact shape
    of the v0.7.21 wedge). Mark-offline is idempotent so widening is
    safe.
    """
    if getattr(player_state, "switched", False):
        return False
    if _was_silent_stub_played(getattr(player_state, "tracked_file", None)):
        return True
    # v0.7.48 fallback: zombie-old-proxy Stop racing the silent stub
    # leaves tracked_file=None (onAVStarted never fires). Detect via
    # the playvid-stamped Window marker so mark-offline still runs.
    return bool(_silent_stub_pending_slug())


def _resolve_silent_stub_slug(
    queued_path: str | None,
    queued_paths: set[str] | frozenset[str] | None,
) -> tuple[str, bool]:
    """Pick the slug that just played the silent stub.

    Tries the live ``playlist[pos].getPath()`` path first
    (``queued_path``). When the silent stub finishes Kodi often resets
    the playlist position, leaving ``queued_path`` empty -- the v0.7.21
    fix relied on the live path and so silently no-op'd, wedging the
    loop in a black-screen cycle on solo-tier offlines (v0.7.29
    regression observed by the user).

    Fallback (v0.7.29): if the live path is empty AND ``queued_paths``
    has exactly one entry (single-slug tier), pull the slug from
    that. Returns ``(slug, fallback_used)`` so the caller can log the
    branch taken.

    Multi-slug tiers with empty live path get ``("", False)``: we
    can't tell which item played the stub, so we leave the cache
    alone and let the periodic bulk-poll refresh clean it up.
    """
    skip_slug = _slug_from_playlist_path(queued_path)
    if skip_slug:
        return skip_slug, False
    if queued_paths and len(queued_paths) == 1:
        only_path = next(iter(queued_paths))
        slug = _slug_from_playlist_path(only_path)
        if slug:
            return slug, True
    # v0.7.48 fallback: ambiguous queue (multi-slug or empty). The
    # playvid handler stamps the slug it just served the silent stub
    # for on a Window property — read it as the tiebreaker so we can
    # mark-offline correctly even when neither path source resolves.
    pending = _silent_stub_pending_slug()
    if pending:
        return pending, True
    return "", False


def _build_playlist_url(slug: str, name: str) -> str:
    """Build the plugin URL the TV loop queues for a model.

    Must round-trip cleanly through Kodi's playlist - the URL we put
    in is exactly what onAVStarted will see in getPlayingFile(), and
    that's how takeover detection differentiates queued vs external.
    """
    qs = urlencode({"mode": "playvid", "slug": slug, "name": name})
    return f"{_PLUGIN_PREFIX}?{qs}"


def _slug_from_url(url: str) -> str:
    """``https://chaturbate.com/alice/`` -> ``alice``.

    TVEntry stores ``url`` and ``name`` (display label) but no slug -
    so playback URL building must derive the slug from the URL, never
    from the name (display names are user-customizable and may contain
    spaces, capitals, or full sentences).
    """
    return url.rstrip("/").rsplit("/", 1)[-1]


# --------------------------------------------------------------------------- #
# Test-injected loop
# --------------------------------------------------------------------------- #


@dataclass
class _LoopOutcome:
    exit_reason: str
    iterations: int = 0
    promotions: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def _classify_after_stop(
    player_state: Any,
    is_live_func: Callable[[str], bool],
) -> str:
    """Decide what to do after the inner loop exits with a stop event.

    Returns one of: ``"user_stopped"``, ``"natural_end"``, ``"fall_through"``.

    Decision order (no double-press gestures - sticky playback wins
    by default; user gestures are single-press only):

    - If ``playlist_ended_naturally`` (at-end stop): ``natural_end``
      - rebuild the playlist. Keep playing forever.
    - Else if Lesson-17 disambiguator says exit (user_stopped + idle<3
      + model still live): ``user_stopped`` immediately, no dialog.
      This is the common "I'm watching, model is live, I want out"
      single-Stop case.
    - Else if the input WAS user-driven (idle<3s) but the model went
      offline mid-stop: fire a Yes/No dialog asking the user whether
      to exit. The dialog handles the case where Lesson-17's
      model_live guard would otherwise trap the user in the loop.
    - Else: ``fall_through`` (ISA misfire / Kodi internal stop -
      idle high, no recent user input). Keep playing forever.
    """
    cur_url = ""
    # v0.7.34: prefer the queued plugin URL (slug-bearing) which we now
    # capture in onAVStarted. ``current_playlist_path`` is the post-
    # resolution path (localhost proxy URL or silent stub) and never
    # has ``slug=`` in production -- the original lookup against
    # current_playlist_path always failed silently and the entire
    # Lesson-17 disambiguator branch was dead code.
    cur_path = (
        getattr(player_state, "current_queued_plugin_url", "")
        or getattr(player_state, "current_playlist_path", "")
        or ""
    )
    if "slug=" in cur_path:
        try:
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(cur_path).query)
            slug = (qs.get("slug") or [""])[0]
            if slug:
                cur_url = f"https://chaturbate.com/{slug}/"
        except Exception:
            cur_url = ""
    model_live = is_live_func(cur_url) if cur_url else False
    natural = bool(getattr(player_state, "playlist_ended_naturally", False))
    user_stopped = bool(getattr(player_state, "user_stopped", False))
    idle = int(getattr(player_state, "idle_at_stop", 0))
    is_user_input_stop = user_stopped and idle < 3

    if natural:
        return "natural_end"
    if not tv_classify.decide_after_stop(user_stopped, model_live, idle):
        # v0.7.47: in-addon-switch guard. When the user clicks a different
        # model from inside the addon (TV list, favs, etc.), the playvid
        # handler stamps a Window property with the current epoch. The
        # OLD playback's Stop event then fires here with idle=0 +
        # model_live=True, which decide_after_stop classifies as a real
        # user stop -> EXIT. But the user wasn't exiting; they were
        # switching. The TAKEOVER fix in onAVStarted would have handled
        # this correctly, but the loop dies here BEFORE Kodi resolves
        # the new item. Suppress the exit when the marker is fresh
        # (<5s) so onAVStarted gets a chance to run.
        if _pending_play_recent():
            _safe_log(
                "_classify_after_stop: pending playvid within 5s -> "
                "fall_through (in-addon switch in flight)"
            )
            return "fall_through"
        return "user_stopped"

    # We're going to continue (ISA misfire / model offline / idle stop).
    # If the user JUST hit Stop AND the model went offline (so Lesson-17
    # blocked the immediate exit), ask via Yes/No dialog. Without
    # defaultbutton (which broke focus on Kodi 21 in v0.7.12), the
    # dialog is navigable - user picks Exit if they want out.
    #
    # Autoclose=30000 returns False (= "Keep playing") on timeout so
    # accidental walk-away preserves sticky-playback default.
    if is_user_input_stop:
        try:
            import xbmcgui
            from resources.lib import addon_settings
            timeout_ms = addon_settings.dialog_timeout_seconds() * 1000
            choice = xbmcgui.Dialog().yesno(
                "Chaturbate TV",
                "Exit TV mode?",
                nolabel="Keep playing",
                yeslabel="Exit",
                autoclose=timeout_ms,
            )
            if choice:
                _safe_log(
                    "_classify_after_stop: yesno=Exit -> user_stopped"
                )
                return "user_stopped"
            _safe_log(
                "_classify_after_stop: yesno=Keep playing (or autoclose)"
            )
        except Exception:  # noqa: S110 - dialog best-effort
            pass

    return "fall_through"


def run_once_for_test(
    *,
    runtime: Any,
    entries: list[TVEntry],
    is_live_func: Callable[[str], bool],
    screensaver_func: Callable[..., Any] | None = None,
    play_func: Callable[[Any], None] | None = None,
    idle_func: Callable[[], int] | None = None,
    now_func: Callable[[], float] = time.time,
    max_iterations: int = 5,
    poll_seconds: float = 600.0,
    entries_path: Any = None,
    silent_stub_mark_func: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Drive the TV loop with the kodi_mock harness for unit tests.

    ``runtime`` is a ``tests.kodi_mock.MockKodiRuntime`` (not imported
    here so this module stays importable in the production Kodi env).

    v0.7.36 backfill hooks:

    - ``entries_path`` (mirrors production tv_play): when provided,
      reload tv.json at the top of each outer iteration so tests can
      simulate mid-session ``tv_add`` / ``tv_remove`` /
      ``tv_edit`` from a sibling process. Without this hook, the
      harness used the constructor's snapshot forever, mirroring the
      pre-v0.7.34 cross-process bug shape.
    - ``silent_stub_mark_func`` (mirrors prod silent-stub branch):
      when the player's tracked_file is the silent stub after
      play_func, this callback receives the resolved slug. Tests
      typically pass ``addon_actions._tv_bulk_mark_offline`` so the
      bulk cache mutates the same way it would in production.
      Without this hook, the iter-N-marks / iter-N+1-skips
      transition was untestable.
    """
    if runtime.window.getProperty(_ACTIVE_KEY) == "1":
        _safe_log("tv_loop.run_once_for_test: already active, decline")
        return _LoopOutcome(exit_reason="already_active").__dict__

    runtime.window.setProperty(_ACTIVE_KEY, "1")
    try:
        if not entries:
            _safe_log("tv_loop.run_once_for_test: empty list, exit")
            return _LoopOutcome(exit_reason="empty_list", iterations=0).__dict__

        promotions = 0
        for it in range(1, max_iterations + 1):
            _safe_log(f"tv_loop.run_once_for_test: iter={it}")
            # v0.7.36: mirror prod's mid-session tv.json reload so the
            # harness exercises the cross-process refresh path.
            if entries_path is not None:
                try:
                    from resources.lib import tv_store as _ts
                    fresh = _ts.load(entries_path)
                    if fresh:
                        entries = fresh
                except Exception as exc:
                    _safe_log(
                        f"tv_loop.run_once_for_test: entries reload "
                        f"failed err={exc!r}"
                    )
            sorted_entries = tv_select.priority_sort(entries)
            target = tv_select.pick_target(sorted_entries, is_live_func)
            if target is None:
                _safe_log("tv_loop.run_once_for_test: no live target, screensaver")
                if screensaver_func is None:
                    return _LoopOutcome(
                        exit_reason="no_live_no_screensaver",
                        iterations=it,
                    ).__dict__
                _entries = sorted_entries
                _is_live = is_live_func
                _runtime = runtime

                def _re_walk(_e: list[TVEntry] = _entries,
                             _il: Callable[[str], bool] = _is_live) -> Any:
                    return tv_select.pick_target(_e, _il)

                def _is_active(_r: Any = _runtime) -> bool:
                    return bool(_r.window.getProperty(_ACTIVE_KEY) == "1")

                resumed = screensaver_func(
                    re_walk_func=_re_walk,
                    is_active_func=_is_active,
                )
                if resumed is None:
                    _safe_log(
                        "tv_loop.run_once_for_test: screensaver dismissed"
                    )
                    return _LoopOutcome(
                        exit_reason="screensaver_dismissed",
                        iterations=it,
                    ).__dict__
                target = resumed
            assert target is not None  # mypy: pick_target / screensaver covers
            # target.priority would be used for promotion-poll if we
            # ran the inner monitor loop here; the test harness skips
            # the monitor loop, so we don't bind it.

            # Build playlist of the live tier. Track queued plugin URLs
            # so onAVStarted can distinguish internal from takeover.
            tier = tv_select.collect_live_tier(
                sorted_entries, target, is_live_func,
            )
            runtime.playlist.clear()
            queued_paths: set[str] = set()
            for m in tier:
                pu = _build_playlist_url(_slug_from_url(m.url), m.name)
                runtime.playlist.add(pu)
                queued_paths.add(pu)

            # Per-iter state-reset on the player (Lesson 10).
            player = runtime.player
            player.user_stopped = False
            player.switched = False
            player.tracked_file = None
            player.idle_at_stop = 0
            player.current_playlist_path = ""
            player.playlist_ended_naturally = False
            player.queued_paths = queued_paths
            # last_natural_end_time INTENTIONALLY persists across
            # iterations - it's the timestamp the double-tap-stop
            # detector compares against. Initialise to 0.0 only on the
            # very first iter so it exists.
            if not hasattr(player, "last_natural_end_time"):
                player.last_natural_end_time = 0.0

            # Wire onAVStarted / onPlayBackStopped on the mock player so
            # the test scenario's simulate_* calls drive our state.
            def on_av_started(_p: Any = player) -> None:
                try:
                    cur = _p.getPlayingFile()
                except Exception:
                    cur = ""
                _p.current_playlist_path = cur
                internal = _is_internal_advance(cur, _p.queued_paths)
                if _p.tracked_file is None:
                    _p.tracked_file = cur
                elif cur and cur != _p.tracked_file:
                    _p.tracked_file = cur
                    if not internal:
                        _p.switched = True

            def on_stopped(_p: Any = player,
                           _runtime: Any = runtime,
                           _idle_func: Callable[[], int] | None = idle_func,
                           _now_func: Callable[[], float] = now_func) -> None:
                _p.user_stopped = True
                _p.idle_at_stop = (_idle_func() if _idle_func else 0)
                try:
                    at_end = tv_classify.is_natural_playlist_end(
                        _runtime.playlist.getposition(),
                        _runtime.playlist.size(),
                    )
                except Exception:
                    at_end = False
                now = _now_func()
                _p.playlist_ended_naturally = tv_classify.classify_stop(
                    at_end, _p.last_natural_end_time, now,
                )
                if at_end:
                    _p.last_natural_end_time = now

            player.onAVStarted = on_av_started
            player.onPlayBackStopped = on_stopped

            # Drive the play. The test's play_func simulates av_started
            # + stopped on the mock player.
            if play_func is not None:
                play_func(runtime.playlist)

            # v0.7.36: mirror prod's silent-stub mark-offline branch
            # (the v0.7.21/v0.7.29/v0.7.33 wedge-fix chain).
            if (silent_stub_mark_func is not None
                    and _should_attempt_silent_stub_mark(player)):
                queued_path = ""
                try:
                    pos = runtime.playlist.getposition()
                    size = runtime.playlist.size()
                    if 0 <= pos < size:
                        queued_path = runtime.playlist[pos].getPath()
                except Exception:
                    queued_path = ""
                skip_slug, _ = _resolve_silent_stub_slug(
                    queued_path, player.queued_paths,
                )
                if skip_slug:
                    silent_stub_mark_func(skip_slug)

            if player.switched:
                _safe_log("tv_loop.run_once_for_test: TAKEOVER, release")
                return _LoopOutcome(
                    exit_reason="takeover", iterations=it,
                    promotions=promotions,
                ).__dict__

            if not player.user_stopped:
                # Stream ended (Ended/Error path); always continue.
                _safe_log("tv_loop.run_once_for_test: ended, fall through")
                continue

            decision = _classify_after_stop(player, is_live_func)
            _safe_log(
                f"tv_loop.run_once_for_test: stop decision={decision!r} "
                f"idle={player.idle_at_stop}s natural={player.playlist_ended_naturally}"
            )
            if decision == "user_stopped":
                # Real user stop: idle<3s AND model still live.
                # Includes the natural-end double-tap path (classify_stop
                # returns False on the second at-end within 5s, the
                # idle check confirms it was the user).
                return _LoopOutcome(
                    exit_reason="user_stopped", iterations=it,
                    promotions=promotions,
                ).__dict__
            # natural_end or fall_through -> next iter
        _safe_log(
            f"tv_loop.run_once_for_test: max_iters reached ({max_iterations})"
        )
        return _LoopOutcome(
            exit_reason="max_iters", iterations=max_iterations,
            promotions=promotions,
        ).__dict__
    finally:
        runtime.window.setProperty(_ACTIVE_KEY, "0")


# --------------------------------------------------------------------------- #
# Production entry: tv_play (called by addon_actions.tv_play)
# --------------------------------------------------------------------------- #


def tv_play(
    *,
    entries: list[TVEntry],
    is_live_func: Callable[[str], bool],
    poll_minutes: int = 10,
    entries_path: Any = None,
) -> str:
    """Run the TV loop with real Kodi.

    Returns the exit reason (mostly for test parity). Production
    callers ignore the return.

    This function imports ``xbmc`` / ``xbmcgui`` lazily so the module
    stays importable from pure-test contexts that don't mock them.

    v0.7.34: when ``entries_path`` is provided, the loop re-reads
    ``tv.json`` at the top of each outer iteration. Keeps the loop in
    sync with mid-session ``tv_add`` / ``tv_remove`` / ``tv_edit``
    calls (which run in a separate Kodi-spawned default.py process
    and would otherwise be invisible until restart -- audit agent 2
    MED #2). When None, the entries snapshot at start is used (test
    callers).
    """
    import xbmc
    import xbmcgui

    win = xbmcgui.Window(10000)
    if win.getProperty(_ACTIVE_KEY) == "1":
        _safe_log("tv_loop.tv_play: already active, decline")
        return "already_active"
    win.setProperty(_ACTIVE_KEY, "1")
    monitor = xbmc.Monitor()
    poll_seconds = max(60, poll_minutes * 60)

    consecutive_errors = 0
    consecutive_stalls = 0
    iter_count = 0
    # Captured by the finally block to fire the right exit-confirmation
    # notification. Assigned before each return statement.
    final_reason = "unknown"
    def _should_continue() -> bool:
        return not monitor.abortRequested() and win.getProperty(_ACTIVE_KEY) == "1"

    def _is_active() -> bool:
        return win.getProperty(_ACTIVE_KEY) == "1"

    # v0.7.58: start the out-of-loop wedge watchdog (daemon). It reads only
    # window props, so it stays responsive if the main thread hangs; it exits
    # when _is_active() flips false (the finally clears _ACTIVE_KEY).
    _stamp_progress()
    import threading
    threading.Thread(
        target=_run_progress_watchdog,
        kwargs={"monitor": monitor, "is_active": _is_active},
        name="cbtv-wedge-watchdog",
        daemon=True,
    ).start()

    try:
        if not entries:
            _safe_log("tv_loop.tv_play: empty list")
            try:
                xbmcgui.Dialog().notification(
                    "Chaturbate TV", "TV list is empty",
                    xbmcgui.NOTIFICATION_INFO, 5000,
                )
            except Exception:  # noqa: S110 - notification is best-effort
                pass
            return "empty_list"

        # Build the player once; reset_for_iteration between iters.
        player_cls = _build_player_class()
        player = player_cls()

        while win.getProperty(_ACTIVE_KEY) == "1":
            try:
                iter_count += 1
                _safe_log(f"tv_loop.tv_play: iter={iter_count}")
                _stamp_progress()  # v0.7.58: new iter = a sign of life
                # v0.7.34: re-read tv.json each outer iteration so
                # mid-session tv_add / tv_remove / tv_edit (which run
                # in a separate Kodi-spawned process) take effect on
                # the next loop turn rather than waiting for restart.
                if entries_path is not None:
                    try:
                        from resources.lib import tv_store as _ts
                        fresh = _ts.load(entries_path)
                        if fresh:
                            entries = fresh
                    except Exception as exc:
                        _safe_log(
                            f"tv_loop.tv_play: tv.json reload failed "
                            f"err={exc!r} (keeping cached entries)"
                        )
                sorted_entries = tv_select.priority_sort(entries)
                target = tv_select.pick_target(
                    sorted_entries, is_live_func,
                    should_continue=_should_continue,
                )
                if target is None:
                    _safe_log("tv_loop.tv_play: no live target, screensaver")
                    from resources.lib import addon_settings, screensaver
                    _entries_iter = sorted_entries

                    def _re_walk(
                        _e: list[TVEntry] = _entries_iter,
                        _il: Callable[[str], bool] = is_live_func,
                    ) -> Any:
                        return tv_select.pick_target(
                            _e, _il, should_continue=_should_continue,
                        )

                    color = addon_settings.screensaver_color()
                    _safe_log(f"tv_loop.tv_play: screensaver color={color}")
                    resumed = screensaver.run(
                        re_walk_func=_re_walk,
                        is_active_func=_is_active,
                        wait_for_abort=monitor.waitForAbort,
                        color=color,
                    )
                    if resumed is None:
                        final_reason = "screensaver_dismissed"
                        return final_reason
                    target = resumed

                target_priority = target.priority
                player.reset_for_iteration()
                tier = tv_select.collect_live_tier(
                    sorted_entries, target, is_live_func,
                )
                playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
                playlist.clear()
                queued: set[str] = set()
                for m in tier:
                    pu = _build_playlist_url(_slug_from_url(m.url), m.name)
                    li = xbmcgui.ListItem(label=m.name)
                    playlist.add(pu, li)
                    queued.add(pu)
                player.queued_paths = queued
                _safe_log(
                    f"tv_loop.tv_play: built tier P{target_priority} "
                    f"slugs={[m.name for m in tier]}"
                )
                # v0.7.15 diagnostic: log around the call to
                # xbmc.Player().play() because we observed the addon
                # thread freeze for 2h 43m on 2026-04-28 around this
                # site (modal "playback failed" dialog blocking the
                # main thread). If this fires but the next "play()
                # returned" log doesn't, we know the dialog modal is
                # holding things up and we know within seconds.
                _safe_log(
                    f"tv_loop.tv_play: iter={iter_count} "
                    f"calling xbmc.Player().play(playlist) "
                    f"items={len(queued)}"
                )
                xbmc.Player().play(playlist)
                _safe_log(
                    f"tv_loop.tv_play: iter={iter_count} "
                    f"xbmc.Player().play() returned, "
                    f"dialog_id={_current_dialog_id()}"
                )

                # Wait up to 30s for playback to start.
                started = False
                wait_start = time.time()
                for tick in range(30):
                    if not _should_continue():
                        final_reason = "aborted"
                        return final_reason
                    if player.isPlaying():
                        started = True
                        _safe_log(
                            f"tv_loop.tv_play: iter={iter_count} "
                            f"isPlaying=True after {tick+1}s"
                        )
                        break
                    if monitor.waitForAbort(1):
                        final_reason = "aborted"
                        return final_reason
                if not started:
                    _safe_log(
                        f"tv_loop.tv_play: iter={iter_count} "
                        f"never started after {time.time()-wait_start:.1f}s, "
                        f"dialog_id={_current_dialog_id()}, skip"
                    )
                    if monitor.waitForAbort(2):
                        final_reason = "aborted"
                        return final_reason
                    continue

                # Inner monitor loop.
                _safe_log(
                    f"tv_loop.tv_play: iter={iter_count} "
                    f"entering inner monitor loop poll_seconds={poll_seconds}"
                )
                elapsed = 0
                step = 5
                ticks_since_log = 0
                self_promoted = False
                # v0.7.41 stall watchdog state (see tv_classify.is_progress_stalled).
                stall_last_pos: float | None = None
                stall_last_at: float = 0.0
                # v0.7.42 diagnostics: log the moment a stream first starts
                # advancing so we can tell "never decoded" from "decoded
                # then froze" in the post-mortem.
                logged_first_advance = False
                # v0.7.50 caching-wedge watchdog state. None = not currently
                # caching; float = wall-clock when Caching first flipped
                # True. Reset to None when Caching flips back to False.
                caching_started_at: float | None = None
                # v0.7.52 post-Stop wedge watchdog state. Tracks the
                # most recent force_player_stop timestamp we've seen
                # (or armed locally). Reset to None when isPlaying
                # flips False, i.e. the stop finally worked.
                force_stop_at: float | None = None
                # v0.7.53 stale-stamp filter: capture the wall-clock
                # at which THIS play started. The Window prop
                # ``chaturbatetv_force_stop_at`` persists across
                # iters; without this gate, fresh iters adopted
                # stamps from the previous iter's exit-time
                # force_player_stop and tripped POST-STOP-WEDGE on
                # healthy streams (production false-positive
                # 2026-05-17 10:11-10:33 CDT, 6 streams killed in
                # 22min before TV mode bailed via consecutive_stalls=5/5).
                play_start_at: float = time.time()
                while player.isPlaying():
                    if not _should_continue():
                        final_reason = "aborted"
                        return final_reason
                    if player.switched:
                        _safe_log("tv_loop.tv_play: takeover, release")
                        try:
                            xbmcgui.Dialog().notification(
                                "Chaturbate TV",
                                "TV mode released - manual play detected",
                                xbmcgui.NOTIFICATION_INFO, 3000,
                            )
                        except Exception:  # noqa: S110 - best-effort notification
                            pass
                        final_reason = "takeover"
                        return final_reason
                    if monitor.waitForAbort(step):
                        final_reason = "aborted"
                        return final_reason
                    elapsed += step
                    ticks_since_log += 1
                    # v0.7.41: stall watchdog. ISA-side decoder freezes
                    # (corrupt CMAF fragment, "ProcessMoof: Cannot get
                    # TRAF atom") leave isPlaying() True forever -- the
                    # outer ``while`` would never exit. Watch getTime()
                    # and force a stop if the position hasn't advanced
                    # past the grace + stall window.
                    try:
                        cur_pos = float(player.getTime())
                        is_paused = bool(player.isPaused())
                    except Exception:
                        cur_pos = 0.0
                        is_paused = False
                    now_ts = time.time()
                    prev_last_pos = stall_last_pos
                    stalled, stall_last_pos, stall_last_at = (
                        tv_classify.is_progress_stalled(
                            cur_position=cur_pos,
                            last_position=stall_last_pos,
                            last_advance_at=stall_last_at,
                            is_paused=is_paused,
                            now=now_ts,
                            elapsed_in_inner_loop=float(elapsed),
                        )
                    )
                    # v0.7.42 diagnostic: log the moment getTime() first
                    # crosses past zero. Tells us "stream actually started
                    # decoding" vs "live HLS getTime stuck at 0 forever"
                    # in post-mortem.
                    if (not logged_first_advance
                            and prev_last_pos is not None
                            and cur_pos > 0.1):
                        logged_first_advance = True
                        _safe_log(
                            f"tv_loop.tv_play: iter={iter_count} "
                            f"first-advance getTime()={cur_pos:.2f}s "
                            f"(elapsed={elapsed}s); stall watchdog now armed"
                        )
                    if stalled:
                        stuck_for = now_ts - stall_last_at
                        _safe_log(
                            f"tv_loop.tv_play: iter={iter_count} STALL "
                            f"detected (getTime() stuck at {cur_pos:.1f}s "
                            f"for {stuck_for:.0f}s past grace, "
                            f"is_paused={is_paused}); firing stop"
                        )
                        player.stall_detected = True
                        try:
                            xbmc.executebuiltin("PlayerControl(Stop)")
                        except Exception:  # noqa: S110 - best-effort
                            pass
                        try:
                            xbmcgui.Dialog().notification(
                                "Chaturbate TV",
                                "Stream stalled - moving on",
                                xbmcgui.NOTIFICATION_INFO, 3000,
                            )
                        except Exception:  # noqa: S110 - best-effort
                            pass
                        break
                    # v0.7.57 error-dialog watchdog (2026-06-07 husk
                    # incident): a play attempt that never decodes can
                    # leave Kodi's "no audio/video stream" OK dialog
                    # (12002) up while isPlaying() stays True. The stall
                    # watchdog never arms (it requires first-advance),
                    # so without this check the loop heartbeats under
                    # the error dialog indefinitely.
                    if tv_classify.is_error_dialog_stuck(
                        dialog_id=_current_dialog_id(),
                        has_first_advance=logged_first_advance,
                        elapsed_in_inner_loop=float(elapsed),
                    ):
                        _safe_log(
                            f"tv_loop.tv_play: iter={iter_count} "
                            f"ERROR-DIALOG detected (dialog_id="
                            f"{_current_dialog_id()}, no first-advance, "
                            f"elapsed={elapsed}s); closing dialog + advancing"
                        )
                        try:
                            xbmc.executebuiltin("Dialog.Close(all,true)")
                        except Exception:  # noqa: S110 - best-effort
                            pass
                        try:
                            xbmc.executebuiltin("PlayerControl(Stop)")
                        except Exception:  # noqa: S110 - best-effort
                            pass
                        break
                    # v0.7.50 caching-wedge watchdog. Closes the gap from
                    # 0.7.42: a decoder freeze where audio creeps wildly
                    # out-of-sync keeps getTime() moving slowly so the
                    # progress watchdog never trips, but Kodi already
                    # knows the stream is wedged via Player.Caching ==
                    # True. If that condition holds for >120s of wall
                    # clock, trip the same recovery path.
                    is_caching_now, cache_level = _read_caching_state()
                    prev_caching_started_at = caching_started_at
                    caching_wedged, caching_started_at = _is_caching_wedged(
                        caching_started_at,
                        is_caching_now,
                        now_ts,
                        _CACHING_WEDGE_GRACE_SEC,
                    )
                    # State-tracking diagnostic: log only on transitions so
                    # the heartbeat stays clean.
                    if (prev_caching_started_at is None
                            and caching_started_at is not None):
                        _safe_log(
                            f"tv_loop.tv_play: iter={iter_count} "
                            f"Player.Caching=True (cache_level={cache_level}); "
                            f"watchdog grace {_CACHING_WEDGE_GRACE_SEC:.0f}s"
                        )
                    elif (prev_caching_started_at is not None
                            and caching_started_at is None):
                        recovered_after = now_ts - prev_caching_started_at
                        _safe_log(
                            f"tv_loop.tv_play: iter={iter_count} "
                            f"Player.Caching=False (recovered after "
                            f"{recovered_after:.0f}s)"
                        )
                    if caching_wedged:
                        stuck_for = now_ts - (caching_started_at or now_ts)
                        _safe_log(
                            f"tv_loop.tv_play: iter={iter_count} "
                            f"CACHING-WEDGE detected (Player.Caching=True "
                            f"for {stuck_for:.0f}s, cache_level="
                            f"{cache_level!r}); firing stop"
                        )
                        player.stall_detected = True
                        try:
                            xbmc.executebuiltin("PlayerControl(Stop)")
                        except Exception:  # noqa: S110 - best-effort
                            pass
                        try:
                            xbmcgui.Dialog().notification(
                                "Chaturbate TV",
                                "Stream caching stuck - moving on",
                                xbmcgui.NOTIFICATION_INFO, 3000,
                            )
                        except Exception:  # noqa: S110 - best-effort
                            pass
                        break
                    # v0.7.52 post-Stop wedge watchdog. Closes the gap
                    # the v0.7.41/v0.7.50 watchdogs can't see: when
                    # Kodi's player itself wedges (e.g. libcurl hang
                    # mid-fetch), PlayerControl(Stop) is fire-and-
                    # forget but isPlaying() never flips False. Both
                    # earlier watchdogs query the player for state, so
                    # they don't trip either. We detect the wedge via
                    # an external signal: hls_proxy stamps
                    # Window(10000).chaturbatetv_force_stop_at every
                    # time it fires force_player_stop. If we're still
                    # playing >grace past the most recent stamp, the
                    # player has wedged and we must break ourselves.
                    # Production wedge 2026-05-17 01:16:56 CDT:
                    # 3 force_player_stop calls within 50s,
                    # then 7+ hours of silence on a busy spinner.
                    new_stop_at = _select_stop_signal(
                        _read_force_stop_at(), play_start_at,
                    )
                    if new_stop_at is not None:
                        # Latest-stamp-wins: pick up newer requests.
                        if force_stop_at is None or new_stop_at > force_stop_at:
                            force_stop_at = new_stop_at
                    prev_force_stop_at = force_stop_at
                    stop_wedged, force_stop_at = _is_stop_wedged(
                        force_stop_at,
                        bool(player.isPlaying()),
                        now_ts,
                        _STOP_WEDGE_GRACE_SEC,
                    )
                    if (prev_force_stop_at is None
                            and force_stop_at is not None):
                        _safe_log(
                            f"tv_loop.tv_play: iter={iter_count} "
                            f"force_stop_at armed (t={force_stop_at:.0f}); "
                            f"watchdog grace {_STOP_WEDGE_GRACE_SEC:.0f}s"
                        )
                    if stop_wedged:
                        stuck_for = now_ts - (force_stop_at or now_ts)
                        _safe_log(
                            f"tv_loop.tv_play: iter={iter_count} "
                            f"POST-STOP-WEDGE detected (isPlaying still "
                            f"True {stuck_for:.0f}s after force_stop_at); "
                            f"breaking inner loop"
                        )
                        player.stall_detected = True
                        try:
                            xbmcgui.Dialog().notification(
                                "Chaturbate TV",
                                "Player wedged - moving on",
                                xbmcgui.NOTIFICATION_INFO, 3000,
                            )
                        except Exception:  # noqa: S110 - best-effort
                            pass
                        break
                    # Heartbeat every 60s of inner-loop time so a
                    # silenced log = something is wedged. We need
                    # enough breadcrumbs to spot a gap.
                    if ticks_since_log >= 12:  # 12 * 5s = 60s
                        ticks_since_log = 0
                        _stamp_progress()  # v0.7.58: heartbeat = a sign of life
                        _safe_log(
                            f"tv_loop.tv_play: iter={iter_count} "
                            f"inner-loop heartbeat elapsed={elapsed}s "
                            f"playing={player.isPlaying()} "
                            f"dialog_id={_current_dialog_id()}"
                        )
                    if elapsed >= poll_seconds:
                        elapsed = 0
                        promoted = tv_select.pick_target(
                            sorted_entries, is_live_func,
                            min_priority=target_priority,
                            should_continue=_should_continue,
                        )
                        if promoted:
                            _safe_log(
                                f"tv_loop.tv_play: promoting "
                                f"P{target_priority} -> P{promoted.priority}"
                            )
                            try:
                                xbmcgui.Dialog().notification(
                                    "Chaturbate TV",
                                    f"Promoting to P{promoted.priority} tier",
                                    xbmcgui.NOTIFICATION_INFO, 3000,
                                )
                            except Exception:  # noqa: S110 - best-effort notification
                                pass
                            self_promoted = True
                            break

                _safe_log(
                    f"tv_loop.tv_play: iter={iter_count} "
                    f"exited inner monitor loop "
                    f"user_stopped={player.user_stopped} "
                    f"switched={player.switched} "
                    f"self_promoted={self_promoted} "
                    f"tracked_file={player.tracked_file!r} "
                    f"dialog_id={_current_dialog_id()}"
                )

                if self_promoted:
                    continue

                # v0.7.21 loop unblock: if the silent stub just played,
                # the slug we picked resolved offline and we need to
                # mark it offline IN-PROCESS (playvid's mark runs in a
                # different Kodi-spawned process and can't update our
                # cache).
                #
                # v0.7.33 hardening: the original guard required
                # ``not player.user_stopped`` because we worried that a
                # user-stop on a silent-stub iteration would over-mark.
                # But mark-offline is idempotent (no-op if the slug
                # isn't in the cache) and on Kodi versions where the
                # last item of a playlist ending fires
                # ``onPlayBackStopped`` instead of ``onPlayBackEnded``,
                # ``user_stopped`` becomes True for a natural-end
                # silent stub -- the mark gets skipped, the loop
                # re-picks the same offline slug forever (audit agent
                # 2 HIGH #1, identical shape to the v0.7.21 wedge).
                # Guard now widened: any silent-stub playback where we
                # didn't switch is fair game to mark.
                if _should_attempt_silent_stub_mark(player):
                    queued_path = ""
                    try:
                        pl = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
                        pos = pl.getposition()
                        size = pl.size()
                        if 0 <= pos < size:
                            queued_path = pl[pos].getPath()
                    except Exception:
                        queued_path = ""
                    skip_slug, fallback_used = _resolve_silent_stub_slug(
                        queued_path, player.queued_paths,
                    )
                    if skip_slug:
                        try:
                            from resources.lib import addon_actions as _aa
                            _aa._tv_bulk_mark_offline(skip_slug)
                            _safe_log(
                                f"tv_loop.tv_play: silent-stub played for "
                                f"slug={skip_slug!r}"
                                + (" (queued_paths fallback)"
                                   if fallback_used else "")
                                + "; dropped from local bulk-live "
                                "cache so next pick_target skips it"
                            )
                        except Exception as exc:
                            _safe_log(
                                f"tv_loop.tv_play: silent-stub mark FAIL "
                                f"err={exc!r}"
                            )
                    else:
                        _safe_log(
                            "tv_loop.tv_play: silent-stub played but "
                            "couldn't extract slug from playlist path "
                            f"{queued_path!r} or queued_paths "
                            f"{player.queued_paths!r}"
                        )

                if player.stall_detected:
                    # v0.7.41: stall watchdog tripped. PlayerControl(Stop)
                    # fired a synthetic onPlayBackStopped which set
                    # user_stopped=True; bypass _classify_after_stop so
                    # we never prompt "Exit TV mode?" on a stall, and
                    # always fall through to the next outer iter. Idle
                    # may legitimately be 0 (user just sat down) or high
                    # (left the room), and either should rotate, not
                    # exit.
                    #
                    # v0.7.42: also mark the stalled slug offline in the
                    # bulk-live cache so the next pick_target picks a
                    # DIFFERENT model. Pre-fix, a single-model tier
                    # whose stream was wedged would re-pick the same
                    # slug forever, firing "Stream stalled" toasts in a
                    # tight loop. Bulk refresh re-adds the slug in
                    # ~10min if Chaturbate's edge recovers.
                    consecutive_stalls += 1
                    queued_path = ""
                    try:
                        pl = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
                        pos = pl.getposition()
                        size = pl.size()
                        if 0 <= pos < size:
                            queued_path = pl[pos].getPath()
                    except Exception as exc:
                        _safe_log(
                            f"tv_loop.tv_play: stall queued-path read "
                            f"FAIL err={exc!r}"
                        )
                        queued_path = ""
                    stall_slug, fallback_used = _resolve_silent_stub_slug(
                        queued_path, player.queued_paths,
                    )
                    if stall_slug:
                        try:
                            from resources.lib import addon_actions as _aa
                            _aa._tv_bulk_mark_offline(stall_slug)
                            _safe_log(
                                f"tv_loop.tv_play: iter={iter_count} "
                                f"stall_detected slug={stall_slug!r}"
                                + (" (queued_paths fallback)"
                                   if fallback_used else "")
                                + f"; marked offline in bulk-live cache "
                                f"(consecutive_stalls={consecutive_stalls}/5)"
                            )
                        except Exception as exc:
                            _safe_log(
                                f"tv_loop.tv_play: stall mark FAIL "
                                f"slug={stall_slug!r} err={exc!r}"
                            )
                    else:
                        _safe_log(
                            f"tv_loop.tv_play: iter={iter_count} "
                            f"stall_detected but couldn't extract slug "
                            f"from queued_path={queued_path!r} or "
                            f"queued_paths={player.queued_paths!r} "
                            f"(consecutive_stalls={consecutive_stalls}/5)"
                        )
                    if consecutive_stalls >= 5:
                        # Circuit breaker: if 5 stalls fire back-to-back
                        # across iters, every model in our reach is
                        # stalling. Network is broken or Chaturbate is
                        # rejecting our edge connections. Stop spinning
                        # and let the user investigate.
                        _safe_log(
                            f"tv_loop.tv_play: {consecutive_stalls} "
                            f"consecutive stalls, exiting TV mode"
                        )
                        try:
                            xbmcgui.Dialog().notification(
                                "Chaturbate TV",
                                "Many streams stalling - exiting TV mode",
                                xbmcgui.NOTIFICATION_WARNING, 5000,
                            )
                        except Exception:  # noqa: S110 - best-effort
                            pass
                        final_reason = "stalls_exhausted"
                        return final_reason
                elif player.user_stopped:
                    consecutive_stalls = 0  # healthy stop resets the counter
                    decision = _classify_after_stop(player, is_live_func)
                    if decision == "user_stopped":
                        final_reason = "user_stopped"
                        return final_reason
                else:
                    # Natural end / takeover already handled higher up;
                    # any clean iter-end resets the stall counter.
                    consecutive_stalls = 0
                consecutive_errors = 0
                if monitor.waitForAbort(1):
                    final_reason = "aborted"
                    return final_reason
            except SystemExit:
                raise
            except Exception as exc:
                consecutive_errors += 1
                _safe_log(
                    f"tv_loop.tv_play: outer iter EXCEPTION "
                    f"({consecutive_errors}/5): {exc!r}"
                )
                if consecutive_errors >= 5:
                    _safe_log("tv_loop.tv_play: too many errors, exit")
                    final_reason = "errors_exhausted"
                    return final_reason
                if monitor.waitForAbort(30):
                    final_reason = "aborted"
                    return final_reason
        final_reason = "active_flag_cleared"
        return final_reason
    finally:
        win.setProperty(_ACTIVE_KEY, "0")
        _safe_log(
            f"tv_loop.tv_play: exited iters={iter_count} "
            f"reason={final_reason!r}"
        )
        # Confirmation notification so the user knows the exit took
        # effect. Skip for "aborted" (Kodi shutting down - any UI
        # call would be late) and "errors_exhausted" (already
        # noisy, user knows something's off). "takeover" already
        # has its own notification fired from inside the inner loop.
        _CONFIRM_REASONS = {
            "user_stopped": "TV mode exited",
            "screensaver_dismissed": "TV mode exited",
            "active_flag_cleared": "TV mode exited",
            "natural_end": "TV mode exited",
        }
        msg = _CONFIRM_REASONS.get(final_reason)
        if msg:
            try:
                xbmcgui.Dialog().notification(
                    "Chaturbate TV", msg,
                    xbmcgui.NOTIFICATION_INFO, 4000,
                )
            except Exception:  # noqa: S110 - notification best-effort
                pass
