"""Tests for resources.lib.screensaver - idle bouncing label window.

Lesson 20 (-patches): use ``xbmcgui.Window``, NOT
``WindowDialog``. WindowDialog overlays the existing screen with
transparent compositing - even with a fullscreen black ControlImage,
the underlying Kodi UI bleeds through. Window swaps the screen out
fully.

Tests assert:
- The screensaver class subclasses xbmcgui.Window (not WindowDialog).
- It adds a black background ControlImage covering 1920x1080.
- It adds a ControlLabel with the configured screensaver color.
- The bounce step keeps the label inside the screen bounds.
- show / dismiss / re_walk hooks work.
- An action of any kind sets dismissed=True.
"""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def kodi_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    fake_xbmc = MagicMock()
    fake_xbmc.LOGINFO = 1

    class _Window:
        """Plain stand-in for xbmcgui.Window. Subclassable."""
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._added: list[Any] = []
            self._showed = False
            self._closed = False

        def addControl(self, c: Any) -> None:
            self._added.append(c)

        def show(self) -> None:
            self._showed = True

        def close(self) -> None:
            self._closed = True

        def setProperty(self, k: str, v: str) -> None: ...

        def getProperty(self, k: str) -> str:
            return ""

    class _WindowDialog(_Window):
        pass

    class _ControlImage:
        def __init__(self, x: int, y: int, w: int, h: int, fname: str,
                     **kw: Any) -> None:
            self.x, self.y, self.w, self.h = x, y, w, h
            self.fname = fname
            self.kwargs = kw

    class _ControlLabel:
        def __init__(self, x: int, y: int, w: int, h: int, label: str,
                     **kw: Any) -> None:
            self.x, self.y, self.w, self.h = x, y, w, h
            self.label = label
            self.kwargs = kw

        def setPosition(self, x: int, y: int) -> None:
            self.x, self.y = x, y

        def setLabel(self, label: str) -> None:
            self.label = label

    fake_xbmcgui = MagicMock()
    fake_xbmcgui.Window = _Window
    fake_xbmcgui.WindowDialog = _WindowDialog
    fake_xbmcgui.ControlImage = _ControlImage
    fake_xbmcgui.ControlLabel = _ControlLabel

    fake_xbmcvfs = MagicMock()
    fake_xbmcvfs.translatePath = lambda p: p

    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)
    monkeypatch.setitem(sys.modules, "xbmcgui", fake_xbmcgui)
    monkeypatch.setitem(sys.modules, "xbmcvfs", fake_xbmcvfs)

    sys.modules.pop("resources.lib.screensaver", None)
    return {
        "xbmc": fake_xbmc,
        "xbmcgui": fake_xbmcgui,
        "xbmcvfs": fake_xbmcvfs,
        "Window": _Window,
        "WindowDialog": _WindowDialog,
    }


def _import() -> Any:
    import resources.lib.screensaver as mod
    return mod


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def test_screensaver_is_window_not_window_dialog(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Lesson 20: must be xbmcgui.Window, not WindowDialog (regression
    guard - WindowDialog renders transparent compositing and the
    underlying Kodi UI bleeds through, defeating the point of an idle
    screensaver).
    """
    ss = _import()
    cls = ss.IdleScreensaver
    assert issubclass(cls, kodi_mocks["Window"])
    assert not issubclass(cls, kodi_mocks["WindowDialog"])


def test_screensaver_adds_black_background_image_first(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """A defensive 1x1 black PNG stretched to full 1920x1080 covers any
    transparent regions inherited from the skin.
    """
    ss = _import()
    win = ss.IdleScreensaver()
    bg = win._added[0]
    assert bg.x == 0 and bg.y == 0
    assert bg.w == 1920 and bg.h == 1080
    assert bg.fname.endswith("black_1x1.png")


def test_screensaver_adds_label_with_halo_cyan_default(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Default screensaver color is HALO cyan FF00d4ff (8-char AARRGGBB)."""
    ss = _import()
    win = ss.IdleScreensaver()
    label = win._added[1]
    color = label.kwargs.get("textColor", "")
    assert color.lower().endswith("ff00d4ff")  # 0xFF00d4ff or FF00d4ff


def test_screensaver_label_color_overridable(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    ss = _import()
    win = ss.IdleScreensaver(color="FF00ff88")  # green
    label = win._added[1]
    color = label.kwargs.get("textColor", "")
    assert "ff00ff88" in color.lower()


# --------------------------------------------------------------------------- #
# Bounce
# --------------------------------------------------------------------------- #


def test_screensaver_tick_bounces_off_screen_edges(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    ss = _import()
    win = ss.IdleScreensaver()
    win._x = 0
    win._y = 0
    win._dx = -6  # would go off the left edge
    win._dy = -4
    win.tick()
    # Bounced - position non-negative + velocity flipped.
    assert win._x >= 0
    assert win._dx > 0
    assert win._y >= 0
    assert win._dy > 0


def test_screensaver_tick_advances_position(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    ss = _import()
    win = ss.IdleScreensaver()
    win._x = 100
    win._y = 100
    win._dx = 6
    win._dy = 4
    before_x, before_y = win._x, win._y
    win.tick()
    assert win._x == before_x + 6
    assert win._y == before_y + 4


# --------------------------------------------------------------------------- #
# Dismissal
# --------------------------------------------------------------------------- #


def test_screensaver_action_marks_dismissed(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Any user input -> dismissed = True so the run loop exits."""
    ss = _import()
    win = ss.IdleScreensaver()
    assert win.dismissed is False
    win.onAction(action=MagicMock())
    assert win.dismissed is True


# --------------------------------------------------------------------------- #
# Run loop with re-walk
# --------------------------------------------------------------------------- #


def test_run_returns_target_when_re_walk_finds_live(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Periodic re-walk: if the live walker returns a live entry, the
    screensaver closes and run() returns the entry so the TV loop can
    resume playback.
    """
    ss = _import()
    found = object()
    abort_after_calls = {"n": 0}

    def re_walk() -> object | None:
        abort_after_calls["n"] += 1
        if abort_after_calls["n"] >= 2:
            return found
        return None

    def is_active() -> bool:
        return True

    waited: list[float] = []

    def wait(_t: float) -> bool:
        waited.append(_t)
        return False

    out = ss.run(
        re_walk_func=re_walk,
        is_active_func=is_active,
        wait_for_abort=wait,
        re_walk_interval_seconds=1.0,
        tick_interval_seconds=1.0,
    )
    assert out is found


def test_run_returns_none_when_dismissed(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    ss = _import()
    waited: list[float] = []

    def wait(_t: float) -> bool:
        waited.append(_t)
        # After two ticks, simulate dismissal by setting a flag we
        # check via a mutable closure.
        if len(waited) >= 2:
            holder["win"].dismissed = True
        return False

    holder: dict[str, Any] = {}

    def factory() -> Any:
        win = ss.IdleScreensaver()
        holder["win"] = win
        return win

    out = ss.run(
        re_walk_func=lambda: None,
        is_active_func=lambda: True,
        wait_for_abort=wait,
        re_walk_interval_seconds=10.0,
        tick_interval_seconds=1.0,
        window_factory=factory,
    )
    assert out is None


def test_run_returns_none_when_active_flag_clears(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """User cleared the chaturbatetv_active prop (ResetTVMode) -> exit."""
    ss = _import()
    out = ss.run(
        re_walk_func=lambda: None,
        is_active_func=lambda: False,
        wait_for_abort=lambda _t: False,
        re_walk_interval_seconds=10.0,
        tick_interval_seconds=1.0,
    )
    assert out is None


def test_run_returns_none_when_abort_requested(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Kodi shutting down -> wait_for_abort returns True -> exit."""
    ss = _import()
    out = ss.run(
        re_walk_func=lambda: None,
        is_active_func=lambda: True,
        wait_for_abort=lambda _t: True,
        re_walk_interval_seconds=10.0,
        tick_interval_seconds=1.0,
    )
    assert out is None


def test_run_calls_re_walk_at_interval(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """re_walk fires every re_walk_interval_seconds, not every tick."""
    ss = _import()
    re_walks: list[int] = []
    counter = {"n": 0}

    def re_walk() -> Any:
        re_walks.append(counter["n"])
        return None

    def wait(_t: float) -> bool:
        counter["n"] += 1
        # After a bunch of ticks, dismiss via the active-flag.
        return counter["n"] > 10

    ss.run(
        re_walk_func=re_walk,
        is_active_func=lambda: True,
        wait_for_abort=wait,
        re_walk_interval_seconds=3.0,
        tick_interval_seconds=1.0,
    )
    # Re-walk should have fired roughly counter / re_walk_ratio times.
    # With tick=1s and interval=3s, expect ~3 walks before exit at
    # counter==11.
    assert 2 <= len(re_walks) <= 4
