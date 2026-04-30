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
    return _was_silent_stub_played(getattr(player_state, "tracked_file", None))


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
                    # Heartbeat every 60s of inner-loop time so a
                    # silenced log = something is wedged. We need
                    # enough breadcrumbs to spot a gap.
                    if ticks_since_log >= 12:  # 12 * 5s = 60s
                        ticks_since_log = 0
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
