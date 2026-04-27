"""Tests for resources.lib.tv_loop - the TV mode orchestration loop.

We exercise the loop through the kodi_mock harness so we don't need
to  round-trip every iteration. Strategy:

- Use ``MockMonitor`` / ``MockPlayer`` / ``MockPlayList`` /
  ``MockWindow`` from tests/kodi_mock.
- Inject these into the loop via dependency injection so the production
  module never imports xbmc unless real Kodi is around.

The lessons we explicitly cover are listed in PLANNING.md and the
-patches LESSONS-LEARNED.md:

- Lesson 2: ISA misfire (stop fires on offline) -> _cb_decide_after_stop
  should fall through.
- Lesson 10: long-lived player state must reset between iterations.
- Lesson 11: file-switch takeover detection.
- Lesson 14: random tier pick (already covered in tv_select tests, but
  re-verified here at the loop level).
- Lesson 17: idle-time disambiguator inside onPlayBackStopped.
- Lesson 20: screensaver class is xbmcgui.Window (asserted in
  test_screensaver.py, but we also assert that the loop calls
  ``screensaver.run`` rather than its own ad-hoc dialog).

A real Kodi player would carry the URL the user is actively playing
in ``getPlayingFile``. The mocks simulate that via the ``simulate_*``
methods.
"""
from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from resources.lib.cb_models import TVEntry
from tests.kodi_mock import (
    MockKodiRuntime,
)


@pytest.fixture
def kodi_mods(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Install the xbmc/xbmcgui modules backed by the kodi_mock harness."""
    fake_xbmc = MagicMock()
    fake_xbmc.LOGINFO = 1
    fake_xbmc.LOGWARNING = 2
    fake_xbmc.LOGERROR = 3
    fake_xbmc.LOGDEBUG = 0
    fake_xbmc.PLAYLIST_VIDEO = 1
    fake_xbmc.PLAYLIST_MUSIC = 0
    # Provide a real (mock-friendly) Player base class so _TVPlayer can
    # subclass it without inheriting MagicMock's auto-attribute magic.
    class _PlayerBase:
        def __init__(self) -> None:
            pass

        def isPlaying(self) -> bool:
            return False

        def isPlayingVideo(self) -> bool:
            return False

        def getPlayingFile(self) -> str:
            return ""

        def play(self, *_a: Any, **_kw: Any) -> None:
            return

        def stop(self) -> None:
            return
    fake_xbmc.Player = _PlayerBase

    fake_xbmcgui = MagicMock()
    fake_xbmcgui.NOTIFICATION_INFO = "info"

    fake_xbmcvfs = MagicMock()
    fake_xbmcvfs.translatePath = lambda p: p
    fake_xbmcaddon = MagicMock()
    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)
    monkeypatch.setitem(sys.modules, "xbmcgui", fake_xbmcgui)
    monkeypatch.setitem(sys.modules, "xbmcvfs", fake_xbmcvfs)
    monkeypatch.setitem(sys.modules, "xbmcaddon", fake_xbmcaddon)

    sys.modules.pop("resources.lib.tv_loop", None)

    yield {
        "xbmc": fake_xbmc,
        "xbmcgui": fake_xbmcgui,
        "xbmcvfs": fake_xbmcvfs,
        "xbmcaddon": fake_xbmcaddon,
    }


def _import() -> Any:
    import resources.lib.tv_loop as mod
    return mod


# --------------------------------------------------------------------------- #
# _TVPlayer state-reset (Lesson 10)
# --------------------------------------------------------------------------- #


def test_tvplayer_reset_clears_all_event_state(
    kodi_mods: dict[str, Any],
) -> None:
    """All long-lived flags get reset between outer iterations
    (Lesson 10). Includes tracked_file - the  v1 bug was
    that not resetting tracked_file made iter 2's first onAVStarted
    register as switched=True.
    """
    tl = _import()
    p = tl._TVPlayer()
    p.user_stopped = True
    p.switched = True
    p.tracked_file = "iter1.url"
    p.idle_at_stop = 42
    p.current_playlist_path = "x"
    p.playlist_ended_naturally = True
    p.last_natural_end_time = 999.0
    p.queued_paths = {"old"}

    p.reset_for_iteration()

    assert p.user_stopped is False
    assert p.switched is False
    assert p.tracked_file is None
    assert p.idle_at_stop == 0
    assert p.current_playlist_path == ""
    assert p.playlist_ended_naturally is False
    # last_natural_end_time INTENTIONALLY persists across iterations -
    # it's the timestamp the double-tap-stop detector compares against.
    assert p.last_natural_end_time == 999.0
    assert p.queued_paths == set()


# --------------------------------------------------------------------------- #
# Takeover detection (Lesson 11)
# --------------------------------------------------------------------------- #


def test_is_internal_advance_true_for_queued_path(
    kodi_mods: dict[str, Any],
) -> None:
    tl = _import()
    queued = {"plugin://plugin.video.chaturbatetv/?mode=playvid&slug=alice"}
    assert tl._is_internal_advance(
        "plugin://plugin.video.chaturbatetv/?mode=playvid&slug=alice",
        queued,
    ) is True


def test_is_internal_advance_false_for_external_path(
    kodi_mods: dict[str, Any],
) -> None:
    tl = _import()
    queued = {"plugin://plugin.video.chaturbatetv/?mode=playvid&slug=alice"}
    assert tl._is_internal_advance(
        "plugin://plugin.video.somethingelse/?play=foo",
        queued,
    ) is False


def test_is_internal_advance_false_for_empty_path(
    kodi_mods: dict[str, Any],
) -> None:
    tl = _import()
    assert tl._is_internal_advance("", {"x"}) is False
    assert tl._is_internal_advance(None, {"x"}) is False


# --------------------------------------------------------------------------- #
# Build playlist URL helper
# --------------------------------------------------------------------------- #


def test_build_playlist_url_round_trips_slug_and_name(
    kodi_mods: dict[str, Any],
) -> None:
    tl = _import()
    url = tl._build_playlist_url("alice", "Alice the Cam Star")
    assert "mode=playvid" in url
    assert "slug=alice" in url
    assert "Alice" in url or "Alice%20" in url or "Alice+" in url


def test_inner_loop_queues_urls_with_slug_not_name(
    kodi_mods: dict[str, Any],
) -> None:
    """Regression: a TV row whose ``name`` is a fancy display string
    (``Alice the Cam Star``) but whose ``url`` resolves to slug ``alice``
    must be queued with ``slug=alice`` in the plugin URL. Earlier code
    accidentally passed ``m.name`` for both slug and name, which broke
    playback for any user-customized TV-row name (and any spaces in the
    name corrupted the URL).
    """
    tl = _import()
    rt = MockKodiRuntime()
    # Display name diverges from the URL slug:
    fancy = TVEntry(
        name="Alice the Cam Star",
        url="https://chaturbate.com/alice/",
        priority=10,
    )

    def play_call(_pl: Any) -> None:
        rt.player.simulate_av_started(
            tl._build_playlist_url("alice", "Alice the Cam Star"),
        )
        rt.player.simulate_stopped()

    tl.run_once_for_test(
        runtime=rt,
        entries=[fancy],
        is_live_func=lambda u: True,
        play_func=play_call,
        idle_func=lambda: 30,
        max_iterations=1,
    )

    queued = rt.playlist.paths()
    assert queued, "loop did not queue any URL"
    assert any("slug=alice" in q for q in queued), (
        f"queued URLs lack slug=alice: {queued!r}"
    )
    assert not any("slug=Alice" in q for q in queued), (
        f"queued URLs treated name as slug: {queued!r}"
    )


# --------------------------------------------------------------------------- #
# Outer loop integration via injected runtime
# --------------------------------------------------------------------------- #


def _entries(*specs: tuple[str, int]) -> list[TVEntry]:
    return [TVEntry(name=n, url=f"https://chaturbate.com/{n}/", priority=p)
            for n, p in specs]


def test_tv_play_exits_immediately_when_already_active(
    kodi_mods: dict[str, Any],
) -> None:
    """If chaturbatetv_active=='1' on entry, decline duplicate start."""
    tl = _import()
    rt = MockKodiRuntime()
    rt.window.setProperty("chaturbatetv_active", "1")
    out = tl.run_once_for_test(
        runtime=rt,
        entries=_entries(("alice", 10)),
        is_live_func=lambda url: True,
    )
    # Loop returned immediately. The flag was NOT changed (we didn't
    # own it).
    assert out["exit_reason"] == "already_active"
    assert rt.window.getProperty("chaturbatetv_active") == "1"


def test_tv_play_exits_when_list_empty(
    kodi_mods: dict[str, Any],
) -> None:
    """Empty TV list -> notify + exit cleanly. The active flag is
    cleared on exit so a re-run can start."""
    tl = _import()
    rt = MockKodiRuntime()
    out = tl.run_once_for_test(
        runtime=rt,
        entries=[],
        is_live_func=lambda url: True,
    )
    assert out["exit_reason"] == "empty_list"
    assert rt.window.getProperty("chaturbatetv_active") == "0"


def test_tv_play_runs_screensaver_when_nothing_live(
    kodi_mods: dict[str, Any],
) -> None:
    """No live models -> screensaver path. We pass a screensaver_func
    that simulates the user dismissing it (returns None)."""
    tl = _import()
    rt = MockKodiRuntime()
    screensaver_calls = []

    def screensaver(**_kw: Any) -> Any:
        screensaver_calls.append(1)
        return None  # user dismissed

    out = tl.run_once_for_test(
        runtime=rt,
        entries=_entries(("alice", 10), ("bob", 5)),
        is_live_func=lambda url: False,  # all offline
        screensaver_func=screensaver,
    )
    assert screensaver_calls
    assert out["exit_reason"] == "screensaver_dismissed"
    assert rt.window.getProperty("chaturbatetv_active") == "0"


def test_tv_play_resumes_when_screensaver_finds_live(
    kodi_mods: dict[str, Any],
) -> None:
    """Screensaver returns a live entry -> loop builds a playlist."""
    tl = _import()
    rt = MockKodiRuntime()
    target = TVEntry(name="alice", url="https://chaturbate.com/alice/",
                     priority=10)

    walks = {"n": 0}

    def is_live(url: str) -> bool:
        # First pick walk: nothing live. After screensaver: alice live.
        walks["n"] += 1
        if walks["n"] <= 1:
            return False
        return url == target.url

    plays = []

    def play_call(_pl: Any) -> None:
        plays.append(1)
        rt.player.simulate_av_started("plugin://x/?slug=alice")
        # User stops shortly after.
        rt.player.simulate_stopped()

    def screensaver(**_kw: Any) -> Any:
        return target

    out = tl.run_once_for_test(
        runtime=rt,
        entries=[target],
        is_live_func=is_live,
        screensaver_func=screensaver,
        play_func=play_call,
        idle_func=lambda: 1,  # < 3s -> real user stop
    )
    assert plays  # we did call play
    # The user's real stop was detected.
    assert out["exit_reason"] == "user_stopped"


# --------------------------------------------------------------------------- #
# Lesson 17: idle-time disambiguator
# --------------------------------------------------------------------------- #


def test_user_stop_with_low_idle_exits(kodi_mods: dict[str, Any]) -> None:
    """Stop fired AND idle < 3s AND model still live -> real user stop,
    exit cleanly."""
    tl = _import()
    rt = MockKodiRuntime()
    target = TVEntry(name="alice", url="https://chaturbate.com/alice/",
                     priority=10)

    def play_call(_pl: Any) -> None:
        rt.player.simulate_av_started(
            tl._build_playlist_url("alice", "alice"),
        )
        rt.player.simulate_stopped()

    out = tl.run_once_for_test(
        runtime=rt,
        entries=[target],
        is_live_func=lambda u: True,
        play_func=play_call,
        idle_func=lambda: 1,  # recent input
    )
    assert out["exit_reason"] == "user_stopped"


def test_isa_misfire_falls_through(kodi_mods: dict[str, Any]) -> None:
    """Stop fired but idle >> 3s OR model no longer live -> ISA misfire,
    fall through to the next iteration."""
    tl = _import()
    rt = MockKodiRuntime()
    alice = TVEntry(name="alice", url="https://chaturbate.com/alice/",
                    priority=10)
    bob = TVEntry(name="bob", url="https://chaturbate.com/bob/",
                  priority=10)

    live_state = {"alice": True, "bob": True}

    def is_live(url: str) -> bool:
        if url == alice.url:
            return live_state["alice"]
        if url == bob.url:
            return live_state["bob"]
        return False

    iterations = {"n": 0}

    def play_call(_pl: Any) -> None:
        iterations["n"] += 1
        # First iter: alice plays then ISA misfires (stop while she's
        # offline). Second iter: bob plays then user really stops.
        if iterations["n"] == 1:
            rt.player.simulate_av_started(
                tl._build_playlist_url("alice", "alice"),
            )
            live_state["alice"] = False  # alice went offline
            rt.player.simulate_stopped()
        else:
            rt.player.simulate_av_started(
                tl._build_playlist_url("bob", "bob"),
            )
            rt.player.simulate_stopped()

    out = tl.run_once_for_test(
        runtime=rt,
        entries=[alice, bob],
        is_live_func=is_live,
        play_func=play_call,
        idle_func=lambda: 30,  # high idle so we KNOW it's not user
        max_iterations=3,
    )
    # ISA misfire on iter 1 must fall through to iter 2 at minimum -
    # if it had exited (treating the misfire as a real user stop) we'd
    # see iter==1 only.
    assert iterations["n"] >= 2
    assert out["exit_reason"] in {"max_iters", "user_stopped", "fall_through"}


# --------------------------------------------------------------------------- #
# Takeover via file-switch (Lesson 11)
# --------------------------------------------------------------------------- #


def test_takeover_releases_tv_mode(kodi_mods: dict[str, Any]) -> None:
    """User plays an external file mid-stream -> file-switch outside
    queued_paths -> player.switched=True -> TV releases."""
    tl = _import()
    rt = MockKodiRuntime()
    target = TVEntry(name="alice", url="https://chaturbate.com/alice/",
                     priority=10)

    def play_call(_pl: Any) -> None:
        # First the queued playvid plays.
        rt.player.simulate_av_started(
            tl._build_playlist_url("alice", "alice"),
        )
        # Then user manually plays something else - same player
        # object, different URL not in queued_paths.
        rt.player.simulate_av_started("plugin://other.plugin/?play=x")

    out = tl.run_once_for_test(
        runtime=rt,
        entries=[target],
        is_live_func=lambda u: True,
        play_func=play_call,
    )
    assert out["exit_reason"] == "takeover"


# --------------------------------------------------------------------------- #
# State-reset between iterations (Lesson 10) - end-to-end sanity
# --------------------------------------------------------------------------- #


def test_state_resets_between_outer_iterations(
    kodi_mods: dict[str, Any],
) -> None:
    """After iter 1's stop, iter 2 must NOT see lingering switched=True
    or stale tracked_file. This is the scenario where Kodi's first
    onAVStarted in iter 2 would otherwise register as a takeover.
    """
    tl = _import()
    rt = MockKodiRuntime()
    alice = TVEntry(name="alice", url="https://chaturbate.com/alice/",
                    priority=10)
    bob = TVEntry(name="bob", url="https://chaturbate.com/bob/",
                  priority=10)

    seen_switched_in_iter2 = []
    iterations = {"n": 0}

    def play_call(_pl: Any) -> None:
        iterations["n"] += 1
        url = tl._build_playlist_url(
            "alice" if iterations["n"] == 1 else "bob",
            "alice" if iterations["n"] == 1 else "bob",
        )
        rt.player.simulate_av_started(url)
        # Capture the player's switched flag right after av_started in iter 2.
        if iterations["n"] == 2:
            seen_switched_in_iter2.append(rt.player.switched)
        rt.player.simulate_stopped()

    tl.run_once_for_test(
        runtime=rt,
        entries=[alice, bob],
        is_live_func=lambda u: True,
        play_func=play_call,
        idle_func=lambda: 30,  # not a user stop, fall through
        max_iterations=3,
    )
    assert seen_switched_in_iter2 == [False], (
        f"iter 2 saw stale switched flag: {seen_switched_in_iter2}"
    )


# --------------------------------------------------------------------------- #
# Lesson 14: random tier pick at the loop level
# --------------------------------------------------------------------------- #


def test_loop_picks_among_equal_tier(kodi_mods: dict[str, Any]) -> None:
    """When multiple models share the highest live tier, the loop
    picks at random across runs - no model is forever shut out."""
    tl = _import()
    seen_targets: set[str] = set()
    for _ in range(40):
        rt = MockKodiRuntime()
        a = TVEntry(name="a", url="https://chaturbate.com/a/", priority=10)
        b = TVEntry(name="b", url="https://chaturbate.com/b/", priority=10)
        c = TVEntry(name="c", url="https://chaturbate.com/c/", priority=10)
        chosen = []

        def play_call(_pl: Any, _chosen: list[str] = chosen,
                      _rt: MockKodiRuntime = rt) -> None:
            # Capture the URL the playlist's first item carries.
            try:
                first = _rt.playlist[0].getPath()
            except Exception:
                first = ""
            if "slug=a" in first:
                _chosen.append("a")
            elif "slug=b" in first:
                _chosen.append("b")
            elif "slug=c" in first:
                _chosen.append("c")
            _rt.player.simulate_av_started(first)
            _rt.player.simulate_stopped()

        tl.run_once_for_test(
            runtime=rt,
            entries=[a, b, c],
            is_live_func=lambda u: True,
            play_func=play_call,
            idle_func=lambda: 1,  # user stops
        )
        if chosen:
            seen_targets.add(chosen[0])
    # Across 40 runs we should hit ALL three at least once.
    assert seen_targets == {"a", "b", "c"}, seen_targets


# --------------------------------------------------------------------------- #
# Last-natural-end-time tracking + double-tap exit
# --------------------------------------------------------------------------- #


def test_double_tap_stop_at_end_exits(kodi_mods: dict[str, Any]) -> None:
    """Two stops within ~5s while at end-of-playlist -> exit cleanly,
    not infinite rebuild."""
    tl = _import()
    rt = MockKodiRuntime()
    only = TVEntry(name="alice", url="https://chaturbate.com/alice/",
                   priority=10)
    iterations = {"n": 0}

    def play_call(_pl: Any) -> None:
        iterations["n"] += 1
        rt.player.simulate_av_started(
            tl._build_playlist_url("alice", "alice"),
        )
        # Position == size-1 simulates the player at the last item.
        rt.playlist.set_position(rt.playlist.size() - 1)
        rt.player.simulate_stopped()

    # Provide a now_func that flows time forward enough that the second
    # stop falls within the 5s window.
    now = {"t": 1000.0}

    def now_func() -> float:
        now["t"] += 0.5  # half-second gap between stops
        return now["t"]

    out = tl.run_once_for_test(
        runtime=rt,
        entries=[only],
        is_live_func=lambda u: True,
        play_func=play_call,
        idle_func=lambda: 1,  # very low idle so user-stop classifier kicks in
        now_func=now_func,
        max_iterations=4,
    )
    # First stop is at_end -> natural -> rebuild (continue).
    # Second stop is at_end again within 5s -> classify_stop returns
    # False -> standard decide_after_stop runs and idle<3 + model_live
    # -> user_stopped exit.
    assert iterations["n"] <= 3
    assert out["exit_reason"] == "user_stopped"
