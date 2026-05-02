"""Tests for resources.lib.tv_loop - the TV mode orchestration loop.

We exercise the loop through the kodi_mock harness so we don't need
to round-trip every iteration. Strategy:

- Use ``MockMonitor`` / ``MockPlayer`` / ``MockPlayList`` /
  ``MockWindow`` from tests/kodi_mock.
- Inject these into the loop via dependency injection so the production
  module never imports xbmc unless real Kodi is around.

The lessons we explicitly cover:

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
from pathlib import Path
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


def test_should_attempt_silent_stub_mark_fires_on_natural_end(
    kodi_mods: dict[str, Any],
) -> None:
    """v0.7.36 backfill (audit pass #2 HIGH): the v0.7.33 widening
    drops ``not user_stopped`` from the gate. On Kodi versions where
    natural-end fires onPlayBackStopped (instead of onPlayBackEnded),
    user_stopped becomes True for stub plays -- the pre-0.7.33 gate
    skipped the mark and the loop re-picked the offline slug forever.
    Pin the relaxed contract: stub played + not switched = mark, even
    when user_stopped=True."""
    tl = _import()

    class _State:
        # silent stub just finished playing
        tracked_file = (
            "/storage/.kodi/addons/plugin.video.chaturbatetv/"
            "resources/media/silent.mp4"
        )
        switched = False
        # Kodi flagged onPlayBackStopped on natural-end (the bug shape)
        user_stopped = True

    assert tl._should_attempt_silent_stub_mark(_State()) is True, (
        "v0.7.33 widened gate must mark even when user_stopped=True"
    )


def test_should_attempt_silent_stub_mark_skips_on_takeover(
    kodi_mods: dict[str, Any],
) -> None:
    """User pressed 'Play' on a different model mid-iteration: switched
    flag fires. Marking offline in that case would over-mark whatever
    we WERE playing as if it had stub-failed -- skip."""
    tl = _import()

    class _State:
        tracked_file = (
            "/storage/.kodi/addons/plugin.video.chaturbatetv/"
            "resources/media/silent.mp4"
        )
        switched = True  # takeover happened
        user_stopped = False

    assert tl._should_attempt_silent_stub_mark(_State()) is False


def test_should_attempt_silent_stub_mark_skips_on_real_playback(
    kodi_mods: dict[str, Any],
) -> None:
    """Tracked file is the localhost proxy URL (live HLS playing), not
    the silent stub. No mark."""
    tl = _import()

    class _State:
        tracked_file = "http://127.0.0.1:42327/master.m3u8"
        switched = False
        user_stopped = False

    assert tl._should_attempt_silent_stub_mark(_State()) is False


def test_resolve_silent_stub_slug_uses_live_path_when_present(
    kodi_mods: dict[str, Any],
) -> None:
    """v0.7.29: when Kodi's playlist position is still valid post-stub
    the live path wins -- no fallback needed. ``fallback_used`` is
    False in this case."""
    tl = _import()
    slug, fb = tl._resolve_silent_stub_slug(
        "plugin://plugin.video.chaturbatetv/?mode=playvid&slug=alice",
        {"plugin://plugin.video.chaturbatetv/?mode=playvid&slug=alice"},
    )
    assert slug == "alice"
    assert fb is False


def test_resolve_silent_stub_slug_falls_back_when_path_empty(
    kodi_mods: dict[str, Any],
) -> None:
    """v0.7.29 regression fix: pl.getposition() returns -1 once the
    silent stub finishes, so live ``queued_path`` is "". For
    single-slug tiers we fall back to the queued_paths set captured
    at playlist-build time. ``fallback_used`` is True so the caller
    can log the branch."""
    tl = _import()
    slug, fb = tl._resolve_silent_stub_slug(
        "",
        {"plugin://plugin.video.chaturbatetv/?mode=playvid&slug=alice&name=alice"},
    )
    assert slug == "alice"
    assert fb is True


def test_resolve_silent_stub_slug_skips_fallback_for_multi_slug_tiers(
    kodi_mods: dict[str, Any],
) -> None:
    """Multi-slug tier + empty live path = we don't know which item
    played the stub, so we DON'T mark anything (over-marking would
    drop a still-live model). Returns ('', False); caller logs and
    moves on, periodic bulk-poll refresh cleans up."""
    tl = _import()
    slug, fb = tl._resolve_silent_stub_slug(
        "",
        {
            "plugin://plugin.video.chaturbatetv/?mode=playvid&slug=alice",
            "plugin://plugin.video.chaturbatetv/?mode=playvid&slug=bob",
        },
    )
    assert slug == ""
    assert fb is False


def test_resolve_silent_stub_slug_handles_empty_queued_paths(
    kodi_mods: dict[str, Any],
) -> None:
    """No queued_paths captured (defensive) + empty live path -> no
    mark, no crash."""
    tl = _import()
    slug, fb = tl._resolve_silent_stub_slug("", set())
    assert slug == ""
    assert fb is False
    # None should also be tolerated (player.queued_paths can be None
    # before the first playlist build).
    slug2, fb2 = tl._resolve_silent_stub_slug("", None)
    assert slug2 == ""
    assert fb2 is False


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

    The fix (v0.7.40 refresh): check ``pl[pos].getPath()`` against
    queued_paths. Kodi keeps the original queued plugin URL accessible
    via that, even after the resolver swapped getPlayingFile() for the
    localhost proxy URL. If the queued URL is in queued_paths, this
    advance is ours.
    """
    tl = _import()
    p = tl._TVPlayer()
    bob_queued = tl._build_playlist_url("bob", "bob")
    p.queued_paths = {
        tl._build_playlist_url("alice", "alice"),
        bob_queued,
    }
    p.tracked_file = "http://127.0.0.1:42327/master.m3u8"  # alice's proxy
    # Playlist still has 2 items (our tier), advanced to position 1
    # (bob); Kodi exposes bob's queued plugin URL via pl[1].getPath().
    _patch_playlist_with_queued(
        kodi_mods, size=2, pos=1, queued_url=bob_queued,
    )
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
    Kodi REPLACES the playlist with a one-item one whose plugin URL is
    the user's clicked one (NOT in queued_paths); takeover MUST fire."""
    tl = _import()
    p = tl._TVPlayer()
    p.queued_paths = {
        tl._build_playlist_url("alice", "alice"),
        tl._build_playlist_url("bob", "bob"),
    }
    p.tracked_file = "http://127.0.0.1:42327/master.m3u8"
    # User direct-play replaced the playlist with the URL THEY clicked
    # (slug-only, no name=, since browse_views constructs it via
    # add_play_item without a name kwarg). Not in queued_paths.
    user_clicked = (
        "plugin://plugin.video.chaturbatetv/?mode=playvid&slug=carol"
    )
    _patch_playlist_with_queued(
        kodi_mods, size=1, pos=0, queued_url=user_clicked,
    )
    p.getPlayingFile = lambda: "http://127.0.0.1:99999/master.m3u8"  # type: ignore[method-assign]

    p.onAVStarted()

    assert p.switched is True, (
        "user direct-play must fire takeover (queued URL not in our set)"
    )


def test_tvplayer_single_model_tier_user_direct_play_fires_takeover(
    kodi_mods: dict[str, Any],
) -> None:
    """v0.7.40 regression: when the active tier holds exactly ONE model,
    the user clicking a different model REPLACES the playlist with a
    one-item one. Pre-fix, the takeover-detection fallback was
    ``pl_size == len(queued_paths)`` -- 1 == 1 -- so the heuristic
    misclassified the takeover as an internal advance. TV loop kept
    running invisibly underneath the user's click.

    The fix uses ``pl[pos].getPath() in queued_paths`` instead, which is
    insensitive to playlist size: queued URL match -> internal, non-
    match -> takeover.

    Real-world repro: tier P7 had only one model (model_a), user
    browsed Female and clicked vesia, log showed
    ``onAVStarted: internal advance to ...`` instead of the expected
    ``TAKEOVER detected`` line.
    """
    tl = _import()
    p = tl._TVPlayer()
    model_a_queued = tl._build_playlist_url("model_a", "model_a")
    p.queued_paths = {model_a_queued}  # tier of exactly 1 model
    p.tracked_file = "http://127.0.0.1:43415/master.m3u8"  # model_a's proxy

    # User clicked vesia in Female browse. Kodi replaces the playlist
    # with a one-item playlist whose plugin URL is vesia's, NOT in our
    # queued_paths (also: shape differs -- browse_views.add_play_item
    # builds slug-only URLs, no name= param).
    user_clicked = (
        "plugin://plugin.video.chaturbatetv/?mode=playvid&slug=vesia"
    )
    _patch_playlist_with_queued(
        kodi_mods, size=1, pos=0, queued_url=user_clicked,
    )
    p.getPlayingFile = lambda: "http://127.0.0.1:36641/master.m3u8"  # vesia's proxy

    p.onAVStarted()

    assert p.switched is True, (
        "single-model tier (size=1) collides with user direct-play "
        "(also size=1) under the size-equality heuristic; the queued-URL "
        "check must classify this as TAKEOVER, not internal advance"
    )


def test_tvplayer_single_model_tier_internal_does_not_fire_takeover(
    kodi_mods: dict[str, Any],
) -> None:
    """v0.7.40 regression sibling: same single-model tier shape as the
    misclassification test above, but the post-resolution localhost
    proxy URL changed (e.g., HLS proxy rebound to a new port). The
    queued URL at pl[0].getPath() IS still our queued one -- so this
    is a genuine internal advance and must NOT fire takeover.
    """
    tl = _import()
    p = tl._TVPlayer()
    queued = tl._build_playlist_url("model_a", "model_a")
    p.queued_paths = {queued}
    p.tracked_file = "http://127.0.0.1:43415/master.m3u8"  # old port
    _patch_playlist_with_queued(
        kodi_mods, size=1, pos=0, queued_url=queued,  # OUR url at pos 0
    )
    p.getPlayingFile = lambda: "http://127.0.0.1:55555/master.m3u8"  # new port

    p.onAVStarted()

    assert p.switched is False, (
        "queued URL at pl[pos].getPath() matches queued_paths -- "
        "this is our own internal advance, not a takeover"
    )


def _patch_playlist_with_queued(
    kodi_mods: dict[str, Any], size: int, pos: int, queued_url: str,
) -> None:
    """Like _patch_playlist but also makes pl[pos].getPath() return a
    real string so onAVStarted can capture current_queued_plugin_url
    (v0.7.34). The default MagicMock returned more MagicMocks, which
    failed the ``"slug=" in queued_url`` check silently.
    """
    fake_item = MagicMock()
    fake_item.getPath.return_value = queued_url
    fake_pl = MagicMock()
    fake_pl.size.return_value = size
    fake_pl.getposition.return_value = pos
    fake_pl.__getitem__ = lambda _self, _i: fake_item
    kodi_mods["xbmc"].PlayList = MagicMock(return_value=fake_pl)


def test_tvplayer_captures_current_queued_plugin_url_with_slug(
    kodi_mods: dict[str, Any],
) -> None:
    """v0.7.36 backfill (audit pass #2 HIGH): the v0.7.34 capture branch
    in onAVStarted populates ``current_queued_plugin_url`` from
    ``pl[pos].getPath()`` so _classify_after_stop can compute model_live
    against the actual queued slug. Existing tests use a MagicMock
    playlist where pl[pos].getPath() returns a MagicMock (no string),
    so the capture branch never actually fired; a regression making it
    skip the assignment would silently revert the Lesson-17
    disambiguator to dead code (v0.7.21 - v0.7.34 era).
    """
    tl = _import()
    p = tl._TVPlayer()
    queued = (
        "plugin://plugin.video.chaturbatetv/"
        "?mode=playvid&slug=alice&name=alice"
    )
    p.queued_paths = {queued}
    _patch_playlist_with_queued(
        kodi_mods, size=1, pos=0, queued_url=queued,
    )
    p.getPlayingFile = lambda: "http://127.0.0.1:42327/master.m3u8"  # type: ignore[method-assign]

    p.onAVStarted()

    assert p.current_queued_plugin_url == queued, (
        f"queued plugin URL should round-trip into the player state; "
        f"got {p.current_queued_plugin_url!r}"
    )


def test_tvplayer_skips_queued_capture_for_paths_without_slug(
    kodi_mods: dict[str, Any],
) -> None:
    """A playlist item that doesn't carry ``slug=`` (e.g., legacy
    queued shape, MagicMock from older tests) leaves
    current_queued_plugin_url empty rather than recording a useless
    value. Defensive contract."""
    tl = _import()
    p = tl._TVPlayer()
    p.queued_paths = set()
    _patch_playlist_with_queued(
        kodi_mods, size=1, pos=0, queued_url="some/random/path",
    )
    p.getPlayingFile = lambda: "http://127.0.0.1:42327/master.m3u8"  # type: ignore[method-assign]

    p.onAVStarted()

    assert p.current_queued_plugin_url == "", (
        "non-slug path must not be recorded as the queued plugin URL"
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


def test_silent_stub_mark_offline_excludes_slug_from_next_iter_pick(
    kodi_mods: dict[str, Any],
    tmp_path: Path,
) -> None:
    """v0.7.36 backfill (audit pass #2 HIGH): the iter-N-marks /
    iter-N+1-skips transition. Pre-backfill, _was_silent_stub_played
    + _resolve_silent_stub_slug + _tv_bulk_mark_offline each had unit
    tests, but the chain that wires them in tv_play was untested
    (run_once_for_test pre-extension didn't exercise it). A regression
    breaking the wiring would have shipped green and re-paid for the
    v0.7.21 / v0.7.29 / v0.7.33 wedges.

    Strategy: drive run_once_for_test with a fixed alice + bob tier.
    iter 1 plays the silent stub on alice; harness calls our
    silent_stub_mark_func with 'alice'. Our fake mark records the
    slug AND removes it from a fake online set so iter 2's
    is_live_func reports alice as offline. We assert iter 2 picks bob,
    not alice.
    """
    tl = _import()
    rt = MockKodiRuntime()

    online = {"alice", "bob"}
    iter_picks: list[str] = []

    def is_live(url: str) -> bool:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        return slug in online

    def fake_mark(slug: str) -> None:
        online.discard(slug)

    silent_stub = (
        "/storage/.kodi/addons/plugin.video.chaturbatetv/"
        "resources/media/silent.mp4"
    )

    iters = {"n": 0}

    def play_call(pl: Any) -> None:
        iters["n"] += 1
        # Track which slug got picked this iteration via the queued
        # playlist path.
        first_path = pl[0].getPath()
        iter_picks.append(first_path)
        if iters["n"] == 1:
            # iter 1: alice resolves to silent stub.
            rt.player.simulate_av_started(silent_stub)
            rt.player.simulate_stopped()
        else:
            # iter 2 onward: real playback (bob).
            rt.player.simulate_av_started(
                "http://127.0.0.1:42327/master.m3u8"
            )
            rt.player.simulate_stopped()

    tl.run_once_for_test(
        runtime=rt,
        entries=[
            TVEntry(name="alice", url="https://chaturbate.com/alice/",
                    priority=10),
            TVEntry(name="bob", url="https://chaturbate.com/bob/",
                    priority=10),
        ],
        is_live_func=is_live,
        play_func=play_call,
        idle_func=lambda: 30,
        max_iterations=2,
        silent_stub_mark_func=fake_mark,
    )

    # iter 1's pick may be alice OR bob (random within tier); iter 2's
    # pick MUST be the survivor. After fake_mark removed whoever played
    # the stub, the next iter's pick_target excludes them.
    assert len(iter_picks) >= 2, (
        f"expected at least 2 iterations, got {iter_picks!r}"
    )
    iter1_slug = iter_picks[0].split("slug=")[1].split("&")[0]
    iter2_slug = iter_picks[1].split("slug=")[1].split("&")[0]
    assert iter1_slug != iter2_slug, (
        f"iter 2 picked the same slug as iter 1 ({iter1_slug}); the "
        f"silent-stub mark-offline + cache eviction chain didn't "
        f"prevent re-pick. picks={iter_picks!r}"
    )


def test_tv_loop_reloads_tv_json_each_iter_for_cross_process_edits(
    kodi_mods: dict[str, Any],
    tmp_path: Path,
) -> None:
    """v0.7.36 backfill (audit pass #2 HIGH): tv_add / tv_remove /
    tv_edit run in a separate Kodi-spawned default.py process from
    tv_play. Pre-v0.7.34 the loop snapshot the entries list at session
    start and never re-read tv.json -- mid-session edits were
    invisible until restart. Post-fix, entries_path triggers a reload
    each outer iter. Pre-backfill, no test exercised the reload, so a
    regression dropping the reload (or moving it past the active-key
    check) would have shipped green.

    Strategy: write tv.json with [alice]; spin one iter with a fake
    is_live that reports alice live; mid-iter rewrite tv.json to
    include bob at higher priority; spin another iter; assert the
    queued playlist now includes bob.
    """
    tl = _import()
    from resources.lib import tv_store

    tv_json = tmp_path / "tv.json"
    tv_store.save(tv_json, [
        TVEntry(name="alice", url="https://chaturbate.com/alice/",
                priority=10),
    ])

    rt = MockKodiRuntime()

    iter_idx = {"n": 0}

    def play_call(_pl: Any) -> None:
        iter_idx["n"] += 1
        if iter_idx["n"] == 1:
            # Mid-session, a sibling process adds bob at P15.
            tv_store.save(tv_json, [
                TVEntry(name="alice",
                        url="https://chaturbate.com/alice/",
                        priority=10),
                TVEntry(name="bob",
                        url="https://chaturbate.com/bob/",
                        priority=15),
            ])
        rt.player.simulate_av_started(
            "http://127.0.0.1:42327/master.m3u8"
        )
        rt.player.simulate_stopped()

    tl.run_once_for_test(
        runtime=rt,
        entries=tv_store.load(tv_json),
        is_live_func=lambda u: True,
        play_func=play_call,
        idle_func=lambda: 30,
        max_iterations=2,
        entries_path=tv_json,
    )

    # iter 2 should have queued bob (higher priority) -- verifies the
    # reload picked up the mid-session write.
    queued_paths = rt.playlist.paths()
    assert any("slug=bob" in p for p in queued_paths), (
        f"iter 2 should have queued bob after mid-session tv.json "
        f"rewrite; queued_paths={queued_paths!r}"
    )


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


def test_classify_after_stop_uses_queued_plugin_url_for_slug(
    kodi_mods: dict[str, Any],
) -> None:
    """v0.7.34: ``current_playlist_path`` is the post-resolution path
    (localhost proxy URL or silent stub) and never has ``slug=`` in
    production -- the v0.7.21+ disambiguator branch was dead code.
    Fix captures ``current_queued_plugin_url`` (slug-bearing) in
    ``onAVStarted`` and ``_classify_after_stop`` reads that. Verify a
    user-stop on a still-live model exits cleanly (no dialog) per
    Lesson 17, which is what the disambiguator was for.
    """
    tl = _import()

    class _State:
        user_stopped = True
        idle_at_stop = 1
        # Resolved path (what production sets via getPlayingFile()):
        current_playlist_path = "http://127.0.0.1:54321/edge/proxied.m3u8"
        # Queued plugin URL (what we now also capture):
        current_queued_plugin_url = (
            "plugin://plugin.video.chaturbatetv/"
            "?mode=playvid&slug=alice&name=alice"
        )
        playlist_ended_naturally = False
        previous_user_stop_time = 0.0

    s = _State()

    # is_live should be queried for alice's URL specifically. Capture
    # the call to verify the slug round-trips through.
    queried: list[str] = []

    def fake_is_live(url: str) -> bool:
        queried.append(url)
        return True  # alice is still live -> Lesson 17 says exit

    decision = tl._classify_after_stop(s, fake_is_live)
    assert "https://chaturbate.com/alice/" in queried, (
        f"is_live must be queried with the queued slug's room URL, "
        f"got {queried!r}"
    )
    assert decision == "user_stopped", (
        "user-stop on a still-live model should exit cleanly (no "
        "dialog), but the disambiguator was dead code pre-0.7.34"
    )


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
