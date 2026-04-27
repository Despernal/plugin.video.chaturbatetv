"""Self-tests for the kodi_mock harness.

These tests do NOT exercise resources.lib.* code; they just verify the
mocks behave the way the TV-loop tests expect. If these go red, the
TV-loop tests will be debugging the mocks instead of the production code.
"""
from __future__ import annotations

import pytest

from tests.kodi_mock import (
    MockGlobalIdleTime,
    MockKodiRuntime,
    MockMonitor,
    MockPlayer,
    MockPlayList,
    MockWindow,
    make_runtime,
)


# Monitor ---------------------------------------------------------------------

def test_monitor_default_not_aborted() -> None:
    m = MockMonitor()
    assert m.abortRequested() is False


def test_monitor_wait_returns_immediately_records_call() -> None:
    m = MockMonitor()
    assert m.waitForAbort(2.5) is False
    assert m.wait_calls == [2.5]


def test_monitor_wait_returns_true_after_abort() -> None:
    m = MockMonitor()
    m.abort = True
    assert m.waitForAbort(1.0) is True
    assert m.abortRequested() is True


# Player ----------------------------------------------------------------------

def test_player_initial_not_playing() -> None:
    p = MockPlayer()
    assert p.isPlaying() is False


def test_player_get_playing_file_raises_when_idle() -> None:
    p = MockPlayer()
    with pytest.raises(RuntimeError):
        p.getPlayingFile()


def test_player_simulate_av_started_marks_playing_and_records_event() -> None:
    p = MockPlayer()
    p.simulate_av_started("plugin://x/?slug=alice")
    assert p.isPlaying() is True
    assert p.getPlayingFile() == "plugin://x/?slug=alice"
    assert p.events[-1] == ("av_started", {"path": "plugin://x/?slug=alice"})


def test_player_simulate_stopped_clears_playing() -> None:
    p = MockPlayer()
    p.simulate_av_started("u")
    p.simulate_stopped()
    assert p.isPlaying() is False
    assert ("stopped", {}) in p.events


def test_player_simulate_ended_and_error_clear_playing() -> None:
    p = MockPlayer()
    p.simulate_av_started("u")
    p.simulate_ended()
    assert p.isPlaying() is False
    p.simulate_av_started("u2")
    p.simulate_error()
    assert p.isPlaying() is False


def test_player_subclass_can_override_callbacks() -> None:
    """Production pattern: subclass MockPlayer to mirror _TVPlayer."""

    class _Tracking(MockPlayer):
        def __init__(self) -> None:
            super().__init__()
            self.av_started_count = 0
            self.stopped_count = 0

        def onAVStarted(self) -> None:
            self.av_started_count += 1

        def onPlayBackStopped(self) -> None:
            self.stopped_count += 1

    t = _Tracking()
    t.simulate_av_started("u")
    t.simulate_stopped()
    t.simulate_av_started("u2")
    assert t.av_started_count == 2
    assert t.stopped_count == 1


# PlayList --------------------------------------------------------------------

def test_playlist_starts_empty() -> None:
    pl = MockPlayList()
    assert pl.size() == 0
    assert pl.getposition() == 0


def test_playlist_add_appends() -> None:
    pl = MockPlayList()
    pl.add("plugin://a")
    pl.add("plugin://b")
    assert pl.size() == 2
    assert pl.paths() == ["plugin://a", "plugin://b"]


def test_playlist_index_returns_item_with_get_path() -> None:
    pl = MockPlayList()
    pl.add("plugin://x/?slug=alice")
    item = pl[0]
    assert item.getPath() == "plugin://x/?slug=alice"


def test_playlist_clear() -> None:
    pl = MockPlayList()
    pl.add("u1")
    pl.add("u2")
    pl.clear()
    assert pl.size() == 0
    assert pl.paths() == []


def test_playlist_set_position() -> None:
    pl = MockPlayList()
    pl.add("a")
    pl.add("b")
    pl.add("c")
    pl.set_position(2)
    assert pl.getposition() == 2


def test_playlist_add_rejects_non_str() -> None:
    pl = MockPlayList()
    with pytest.raises(TypeError):
        pl.add(42)  # type: ignore[arg-type]


# Window ----------------------------------------------------------------------

def test_window_unset_property_returns_empty_string() -> None:
    w = MockWindow()
    assert w.getProperty("anything") == ""


def test_window_set_get_round_trip() -> None:
    w = MockWindow()
    w.setProperty("k", "v")
    assert w.getProperty("k") == "v"


def test_window_clear_property() -> None:
    w = MockWindow()
    w.setProperty("k", "v")
    w.clearProperty("k")
    assert w.getProperty("k") == ""


def test_window_snapshot_returns_copy() -> None:
    w = MockWindow()
    w.setProperty("a", "1")
    snap = w.snapshot()
    snap["a"] = "tampered"
    assert w.getProperty("a") == "1"


# Idle time -------------------------------------------------------------------

def test_idle_default_zero() -> None:
    idle = MockGlobalIdleTime()
    assert idle.get() == 0


def test_idle_set_get() -> None:
    idle = MockGlobalIdleTime()
    idle.idle_seconds = 42
    assert idle.get() == 42


def test_idle_initial_value() -> None:
    idle = MockGlobalIdleTime(idle_seconds=10)
    assert idle.get() == 10


# MockKodiRuntime -------------------------------------------------------------

def test_runtime_bundles_default_mocks() -> None:
    rt = MockKodiRuntime()
    assert isinstance(rt.monitor, MockMonitor)
    assert isinstance(rt.player, MockPlayer)
    assert isinstance(rt.playlist, MockPlayList)
    assert isinstance(rt.window, MockWindow)
    assert isinstance(rt.idle, MockGlobalIdleTime)


def test_runtime_sleep_log() -> None:
    rt = MockKodiRuntime()
    rt.sleep(1.0)
    rt.sleep(2.5)
    assert rt.sleep_log == [1.0, 2.5]


def test_make_runtime_with_custom_player_class() -> None:
    class _Custom(MockPlayer):
        marker: str = "custom"

    rt = make_runtime(player_cls=_Custom)
    assert isinstance(rt.player, _Custom)
    assert rt.player.marker == "custom"
