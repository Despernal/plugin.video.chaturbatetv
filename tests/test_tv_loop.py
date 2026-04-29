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


def test_was_silent_stub_played_matches_silent_mp4_path(
    kodi_mods: dict[str, Any],
) -> None:
    """v0.7.21 loop unblock: the outer loop must recognize when the
    inner monitor's tracked_file points at the offline-skip stub
    asset so it can drop the slug from the bulk-live cache.

    True only for paths that include 'silent.mp4'. False for live
    proxy URLs, queued plugin URLs, and None.
    """
    tl = _import()
    assert tl._was_silent_stub_played(
        "/storage/.kodi/addons/plugin.video.chaturbatetv/resources/media/silent.mp4"
    ) is True
    assert tl._was_silent_stub_played(
        "C:\\Kodi\\addons\\plugin.video.chaturbatetv\\resources\\media\\silent.mp4"
    ) is True
    assert tl._was_silent_stub_played(
        "http://127.0.0.1:42327/master.m3u8"
    ) is False
    assert tl._was_silent_stub_played(
        "plugin://plugin.video.chaturbatetv/?mode=playvid&slug=alice&name=alice"
    ) is False
    assert tl._was_silent_stub_played(None) is False
    assert tl._was_silent_stub_played("") is False


def test_slug_from_playlist_path_extracts_slug(kodi_mods: dict[str, Any]) -> None:
    """The plugin URL Kodi has at the playlist position carries the
    slug we queued for this slot. Pulling it out lets the loop tell
    _tv_bulk_mark_offline exactly which slug to drop."""
    tl = _import()
    assert tl._slug_from_playlist_path(
        "plugin://plugin.video.chaturbatetv/?mode=playvid&slug=alice&name=alice"
    ) == "alice"
    # URL-encoded names with spaces should still surface a clean slug:
    assert tl._slug_from_playlist_path(
        "plugin://plugin.video.chaturbatetv/?mode=playvid&slug=bob&name=Bob+the+Cam+Star"
    ) == "bob"
    # Missing / malformed -> "":
    assert tl._slug_from_playlist_path(
        "plugin://plugin.video.chaturbatetv/?mode=tv_list"
    ) == ""
    assert tl._slug_from_playlist_path(None) == ""
    assert tl._slug_from_playlist_path("") == ""


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


def _patch_playlist(kodi_mods: dict[str, Any], size: int, pos: int) -> None:
    """Make ``xbmc.PlayList(VIDEO).size()`` and ``getposition()`` return
    the integers we want. The default MagicMock returns more MagicMocks,
    which compare against ints unpredictably.
    """
    fake_pl = MagicMock()
    fake_pl.size.return_value = size
    fake_pl.getposition.return_value = pos
    kodi_mods["xbmc"].PlayList = MagicMock(return_value=fake_pl)


def test_tvplayer_tier_next_button_does_not_fire_takeover(
    kodi_mods: dict[str, Any],
) -> None:
    """Regression: when the user clicks Next in the player to advance
    to the next tier member, Kodi resolves the queued plugin URL via
    playvid, which produces a localhost proxy URL like
    ``http://127.0.0.1:42327/master.m3u8``. The queued_paths set holds
    the UNRESOLVED plugin URLs; the naive ``cur in queued_paths`` check
    would always fail and fire TAKEOVER, stopping TV mode.

    The fix: also accept the path when it's a 127.0.0.1 proxy URL AND
    the playlist size + position match what we queued. User direct-play
    REPLACES the playlist, so size mismatch still classifies a real
    takeover correctly.
    """
    tl = _import()
    p = tl._TVPlayer()
    p.queued_paths = {
        tl._build_playlist_url("alice", "alice"),
        tl._build_playlist_url("bob", "bob"),
    }
    p.tracked_file = "http://127.0.0.1:42327/master.m3u8"  # alice's proxy
    # Playlist still has 2 items (our tier), advanced to position 1.
    _patch_playlist(kodi_mods, size=2, pos=1)
    # Override getPlayingFile to return the new proxy URL.
    p.getPlayingFile = lambda: "http://127.0.0.1:55555/master.m3u8"  # type: ignore[method-assign]

    p.onAVStarted()

    assert p.switched is False, (
        "tier-internal advance must NOT fire takeover"
    )


def test_tvplayer_user_direct_play_fires_takeover(
    kodi_mods: dict[str, Any],
) -> None:
    """User clicks a non-queued model directly while TV is playing:
    Kodi REPLACES the playlist with a one-item one. Size no longer
    matches our queued count (2 -> 1); takeover MUST fire."""
    tl = _import()
    p = tl._TVPlayer()
    p.queued_paths = {
        tl._build_playlist_url("alice", "alice"),
        tl._build_playlist_url("bob", "bob"),
    }
    p.tracked_file = "http://127.0.0.1:42327/master.m3u8"
    # User direct-play replaced the playlist with a single item.
    _patch_playlist(kodi_mods, size=1, pos=0)
    p.getPlayingFile = lambda: "http://127.0.0.1:99999/master.m3u8"  # type: ignore[method-assign]

    p.onAVStarted()

    assert p.switched is True, (
        "user direct-play must fire takeover (playlist size mismatch)"
    )


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
    """Screensaver returns a live entry -> loop builds a playlist.

    Uses a 2-entry tier so playlist size > 1 and pos=0 isn't at-end.
    With sticky-playback, an at-end stop is treated as natural_end and
    rebuilds; mid-playlist user stops with low idle take the
    user_stopped branch (Lesson 17 disambiguator). The test exercises
    the latter so we can verify the screensaver-resume path produced a
    valid play call AND that the standard exit gesture still works.
    """
    tl = _import()
    rt = MockKodiRuntime()
    alice = TVEntry(name="alice", url="https://chaturbate.com/alice/",
                    priority=10)
    bob = TVEntry(name="bob", url="https://chaturbate.com/bob/",
                  priority=10)

    walks = {"n": 0}

    def is_live(url: str) -> bool:
        # First pick walk (one is_live call per entry): both offline.
        # After screensaver: both live.
        walks["n"] += 1
        return walks["n"] > 2

    plays = []

    def play_call(_pl: Any) -> None:
        plays.append(1)
        rt.player.simulate_av_started("plugin://x/?slug=alice")
        # User stops shortly after - mid-playlist (pos=0 of 2-item tier).
        rt.player.simulate_stopped()

    def screensaver(**_kw: Any) -> Any:
        return alice

    out = tl.run_once_for_test(
        runtime=rt,
        entries=[alice, bob],
        is_live_func=is_live,
        screensaver_func=screensaver,
        play_func=play_call,
        idle_func=lambda: 1,  # < 3s -> real user stop
    )
    assert plays  # we did call play
    # Mid-playlist user stop with low idle + model live -> user_stopped.
    assert out["exit_reason"] == "user_stopped"


# --------------------------------------------------------------------------- #
# Lesson 17: idle-time disambiguator
# --------------------------------------------------------------------------- #


def _patch_yesno(kodi_mods: dict[str, Any], answer: bool) -> list[Any]:
    """Stub xbmcgui.Dialog().yesno() to return ``answer`` and capture calls."""
    captured: list[Any] = []

    class _Dialog:
        def yesno(self, *args: Any, **kwargs: Any) -> bool:
            captured.append({"args": args, "kwargs": kwargs})
            return answer

        def notification(self, *_a: Any, **_kw: Any) -> None:
            return

    kodi_mods["xbmcgui"].Dialog = _Dialog
    return captured


def test_classify_yesno_exit_returns_user_stopped(
    kodi_mods: dict[str, Any],
) -> None:
    """Lesson 33 (v0.7.11 was good, restored in v0.7.13): on a
    user-input stop where standard logic would continue, fire a
    Yes/No dialog. Without defaultbutton (which broke focus on
    Kodi 21 in v0.7.12), the dialog is navigable - user picks Exit
    -> user_stopped.
    """
    tl = _import()
    captured = _patch_yesno(kodi_mods, answer=True)

    class _State:
        user_stopped = True
        idle_at_stop = 1
        current_playlist_path = ""
        playlist_ended_naturally = False
        previous_user_stop_time = 0.0

    s = _State()
    decision = tl._classify_after_stop(s, lambda url: False)
    assert decision == "user_stopped"
    assert captured, "yesno dialog should have fired"
    # Regression guard: defaultbutton MUST NOT be passed - that param
    # broke focus on Kodi 21 in v0.7.12.
    kwargs = captured[0]["kwargs"]
    assert "defaultbutton" not in kwargs, (
        f"defaultbutton broke navigation in v0.7.12, do not re-add: {kwargs!r}"
    )


def test_classify_yesno_keep_playing_returns_fall_through(
    kodi_mods: dict[str, Any],
) -> None:
    """User picks "Keep playing" or autoclose fires -> fall through,
    TV mode continues (sticky-playback default preserved)."""
    tl = _import()
    _patch_yesno(kodi_mods, answer=False)

    class _State:
        user_stopped = True
        idle_at_stop = 1
        current_playlist_path = ""
        playlist_ended_naturally = False
        previous_user_stop_time = 0.0

    s = _State()
    decision = tl._classify_after_stop(s, lambda url: False)
    assert decision == "fall_through"


def test_classify_no_double_stop_fast_path_v0_7_13(
    kodi_mods: dict[str, Any],
) -> None:
    """Regression guard: v0.7.13 dropped the double-stop fast-path per
    user directive ("like the double stop and the double next is a no
    go from now on"). Even if a previous user stop was recorded
    seconds ago, the dialog must still fire - no shortcut.
    """
    tl = _import()
    captured = _patch_yesno(kodi_mods, answer=False)  # "Keep playing"

    class _State:
        def __init__(self) -> None:
            self.user_stopped = True
            self.idle_at_stop = 1
            self.current_playlist_path = ""
            self.playlist_ended_naturally = False
            # Even setting this should NOT short-circuit the dialog.
            self.previous_user_stop_time = 0.0

    s = _State()
    import time as _time
    s.previous_user_stop_time = _time.time() - 2.0
    decision = tl._classify_after_stop(s, lambda url: False)
    # Dialog fires (mocked to "Keep playing"), so fall_through.
    assert decision == "fall_through"
    assert captured, "dialog must always fire on user-input stop"


def test_classify_high_idle_stop_falls_through_no_dialog(
    kodi_mods: dict[str, Any],
) -> None:
    """ISA misfire (Kodi self-fired stop, idle>>3s, user wasn't
    interacting): treated as continue without firing the dialog.
    Keeps TV mode alive through model-going-offline events."""
    tl = _import()
    captured = _patch_yesno(kodi_mods, answer=True)

    class _State:
        user_stopped = True
        idle_at_stop = 30
        current_playlist_path = ""
        playlist_ended_naturally = False
        previous_user_stop_time = 0.0

    s = _State()
    decision = tl._classify_after_stop(s, lambda url: False)
    assert decision == "fall_through"
    assert not captured, "dialog should NOT fire on idle (ISA-misfire) stop"


def test_user_stop_with_low_idle_exits(kodi_mods: dict[str, Any]) -> None:
    """Mid-playlist user stop (idle < 3s, model still live) -> real
    user stop, exit cleanly.

    The tier holds 2 entries so the playlist size is 2 and pos=0 is
    NOT at-end - that takes the user_stopped branch via the Lesson-17
    disambiguator (model_live + low idle = user really hit Stop).
    Single-item tiers always at-end so they take the natural_end path
    instead (sticky-playback rebuilds forever).
    """
    tl = _import()
    rt = MockKodiRuntime()
    alice = TVEntry(name="alice", url="https://chaturbate.com/alice/",
                    priority=10)
    bob = TVEntry(name="bob", url="https://chaturbate.com/bob/",
                  priority=10)

    def play_call(_pl: Any) -> None:
        rt.player.simulate_av_started(
            tl._build_playlist_url("alice", "alice"),
        )
        rt.player.simulate_stopped()

    out = tl.run_once_for_test(
        runtime=rt,
        entries=[alice, bob],
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
# Sticky-playback at end-of-playlist (Lesson 14, v0.7.13 sticky-only)
# --------------------------------------------------------------------------- #


def test_at_end_stop_keeps_rebuilding_forever(
    kodi_mods: dict[str, Any],
) -> None:
    """v0.7.13 sticky-playback: every at-end stop rebuilds the playlist,
    no matter how many times the user hits Stop. The previous double-
    tap-at-end-within-5s exit was removed per user directive ("like
    the double stop and the double next is a no go from now on" /
    "i never want to remove something that is there to keep it
    playing forever"). The exit gesture is the Yes/No dialog, which
    only fires on a NOT-at-end user stop.
    """
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

    out = tl.run_once_for_test(
        runtime=rt,
        entries=[only],
        is_live_func=lambda u: True,
        play_func=play_call,
        idle_func=lambda: 1,  # low idle - would have been user-stop in old logic
        max_iterations=4,
    )
    # Every iteration is at_end, so every stop is natural_end -> rebuild
    # -> continue. The loop runs to max_iters without exiting.
    assert iterations["n"] == 4
    assert out["exit_reason"] == "max_iters"
