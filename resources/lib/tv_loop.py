"""TV mode outer loop and ``_TVPlayer`` subclass.

Loop shape mirrors 's v11 TVPlay - the bug-survivor
algorithm we earned over eleven iterations:

1. Outer guard: refuse to start if ``chaturbatetv_active`` is already
   ``"1"`` ( Lesson 12 - manual reset path is provided
   separately, but we won't double-spawn the loop).
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

from resources.lib import tv_classify, tv_select
from resources.lib.cb_models import TVEntry


_PLUGIN_PREFIX = "plugin://plugin.video.chaturbatetv/"
_ACTIVE_KEY = "chaturbatetv_active"


def _safe_log(msg: str) -> None:
    try:
        from resources.lib import logger
        logger._log(msg)
    except Exception:
        return


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
            self.idle_at_stop: int = 0
            self.current_playlist_path: str = ""
            self.playlist_ended_naturally: bool = False
            self.last_natural_end_time: float = 0.0
            self.queued_paths: set[str] = set()

        def reset_for_iteration(self) -> None:
            """Clear all per-iteration event state.

            ``last_natural_end_time`` is INTENTIONALLY preserved across
            iterations - it's the timestamp the double-tap-stop detector
            compares against.
            """
            self.user_stopped = False
            self.tracked_file = None
            self.switched = False
            self.idle_at_stop = 0
            self.current_playlist_path = ""
            self.playlist_ended_naturally = False
            self.queued_paths = set()

        def onAVStarted(self) -> None:
            try:
                cur = self.getPlayingFile()
            except Exception:
                cur = ""
            self.current_playlist_path = cur
            internal = _is_internal_advance(cur, self.queued_paths)
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


def _build_playlist_url(slug: str, name: str) -> str:
    """Build the plugin URL the TV loop queues for a model.

    Must round-trip cleanly through Kodi's playlist - the URL we put
    in is exactly what onAVStarted will see in getPlayingFile(), and
    that's how takeover detection differentiates queued vs external.
    """
    qs = urlencode({"mode": "playvid", "slug": slug, "name": name})
    return f"{_PLUGIN_PREFIX}?{qs}"


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

    Decision order:

    - If ``playlist_ended_naturally`` is True (first at-end stop, or
      at-end stop more than 5s after the previous natural end):
      ``natural_end`` - rebuild the playlist, no idle/live check.
    - Else if ``decide_after_stop(user_stopped, model_live, idle)``
      says exit (idle<3s + model still live): ``user_stopped``. This
      catches the natural_end double-tap (``classify_stop`` returns
      False the second time and the standard idle check kicks in).
    - Else: ``fall_through`` (ISA misfire / Kodi internal stop).
    """
    cur_url = ""
    cur_path = getattr(player_state, "current_playlist_path", "") or ""
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
    if natural:
        return "natural_end"
    if not tv_classify.decide_after_stop(user_stopped, model_live, idle):
        return "user_stopped"
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
) -> dict[str, Any]:
    """Drive the TV loop with the kodi_mock harness for unit tests.

    ``runtime`` is a ``tests.kodi_mock.MockKodiRuntime`` (not imported
    here so this module stays importable in the production Kodi env).
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
                pu = _build_playlist_url(m.name, m.name)
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
) -> str:
    """Run the TV loop with real Kodi.

    Returns the exit reason (mostly for test parity). Production
    callers ignore the return.

    This function imports ``xbmc`` / ``xbmcgui`` lazily so the module
    stays importable from pure-test contexts that don't mock them.
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
    iter_count = 0
    def _should_continue() -> bool:
        return not monitor.abortRequested() and win.getProperty(_ACTIVE_KEY) == "1"

    def _is_active() -> bool:
        return win.getProperty(_ACTIVE_KEY) == "1"

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
                sorted_entries = tv_select.priority_sort(entries)
                target = tv_select.pick_target(
                    sorted_entries, is_live_func,
                    should_continue=_should_continue,
                )
                if target is None:
                    _safe_log("tv_loop.tv_play: no live target, screensaver")
                    from resources.lib import screensaver
                    _entries_iter = sorted_entries

                    def _re_walk(
                        _e: list[TVEntry] = _entries_iter,
                        _il: Callable[[str], bool] = is_live_func,
                    ) -> Any:
                        return tv_select.pick_target(
                            _e, _il, should_continue=_should_continue,
                        )

                    resumed = screensaver.run(
                        re_walk_func=_re_walk,
                        is_active_func=_is_active,
                        wait_for_abort=monitor.waitForAbort,
                    )
                    if resumed is None:
                        return "screensaver_dismissed"
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
                    pu = _build_playlist_url(m.name, m.name)
                    li = xbmcgui.ListItem(label=m.name)
                    playlist.add(pu, li)
                    queued.add(pu)
                player.queued_paths = queued
                _safe_log(
                    f"tv_loop.tv_play: built tier P{target_priority} "
                    f"slugs={[m.name for m in tier]}"
                )
                xbmc.Player().play(playlist)

                # Wait up to 30s for playback to start.
                started = False
                for _ in range(30):
                    if not _should_continue():
                        return "aborted"
                    if player.isPlaying():
                        started = True
                        break
                    if monitor.waitForAbort(1):
                        return "aborted"
                if not started:
                    _safe_log("tv_loop.tv_play: never started, skip")
                    if monitor.waitForAbort(2):
                        return "aborted"
                    continue

                # Inner monitor loop.
                elapsed = 0
                step = 5
                self_promoted = False
                while player.isPlaying():
                    if not _should_continue():
                        return "aborted"
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
                        return "takeover"
                    if monitor.waitForAbort(step):
                        return "aborted"
                    elapsed += step
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

                if self_promoted:
                    continue

                if player.user_stopped:
                    decision = _classify_after_stop(player, is_live_func)
                    if decision == "user_stopped":
                        return "user_stopped"
                consecutive_errors = 0
                if monitor.waitForAbort(1):
                    return "aborted"
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
                    return "errors_exhausted"
                if monitor.waitForAbort(30):
                    return "aborted"
        return "active_flag_cleared"
    finally:
        win.setProperty(_ACTIVE_KEY, "0")
        _safe_log(f"tv_loop.tv_play: exited iters={iter_count}")
