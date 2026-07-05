"""Idle screensaver for TV mode.

When ``tv_loop`` walks the priority list and finds nothing live, it
hands control to ``screensaver.run`` which shows a fullscreen Window
with a bouncing label. Periodically re-walks the live list; if a model
comes online, returns it so the loop can resume. Any user input
dismisses (returns None).

Lesson 20: ``xbmcgui.Window``, NOT ``WindowDialog``. WindowDialog is
overlay-style with transparent compositing - the underlying Kodi UI
bleeds through even with a fullscreen black ControlImage. Window swaps
the screen out fully so we get a real "screensaver" look.

The PNG used as the background is shipped at
``resources/media/black_1x1.png``. Stretching a 1x1 black PNG to
1920x1080 is the -tested way to defensively black-fill in
case the skin leaves any transparent regions.
"""
from __future__ import annotations

import random
from collections.abc import Callable
from pathlib import Path
from typing import Any


# HALO palette default per PLANNING.md. 8-char AARRGGBB hex required.
_DEFAULT_COLOR = "FF00d4ff"

# Geometry. Skins scale on top of the 1920x1080 base.
_SCREEN_W = 1920
_SCREEN_H = 1080
_LABEL_W = 800
_LABEL_H = 120
_BG_PNG_RELATIVE = "resources/media/black_1x1.png"


def _safe_log(msg: str) -> None:
    """Log helper that tolerates a logger import failure."""
    try:
        from resources.lib import logger
        logger._log(msg)
    except Exception:
        return


def _bg_png_path() -> str:
    """Resolve the path to the 1x1 black PNG shipped with the addon.

    Falls back to the addon-relative path if xbmcvfs / addon location
    can't be queried (in the pure test environment, xbmcvfs.translatePath
    is mocked to a string round-trip).
    """
    try:
        import xbmcaddon
        addon = xbmcaddon.Addon()
        addon_path = addon.getAddonInfo("path")
        if addon_path:
            return str(Path(addon_path) / _BG_PNG_RELATIVE)
    except Exception:  # noqa: S110 - silent fallback intentional
        pass
    return _BG_PNG_RELATIVE


def _make_screensaver_class() -> type:
    """Build IdleScreensaver lazily so xbmcgui isn't imported at module-load."""
    import xbmcgui

    class _IdleScreensaver(xbmcgui.Window):
        """Fullscreen Window with a bouncing label.

        ``tick`` updates the label position (called once per frame by
        :func:`run`). ``onAction`` flips ``dismissed`` so the run loop
        exits on any user input.
        """

        def __init__(self, color: str = _DEFAULT_COLOR) -> None:
            super().__init__()
            self.dismissed = False
            self._color = color
            # Defensive black background: stretch a 1x1 PNG over the
            # whole screen so the underlying Kodi UI cannot bleed
            # through (Lesson 20 belt-and-braces).
            bg_path = _bg_png_path()
            _safe_log(f"screensaver: bg_path={bg_path}")
            bg = xbmcgui.ControlImage(
                0, 0, _SCREEN_W, _SCREEN_H, bg_path,
            )
            self.addControl(bg)
            # Bouncing label.
            self._x = random.randint(0, _SCREEN_W - _LABEL_W)
            self._y = random.randint(0, _SCREEN_H - _LABEL_H)
            self._dx = random.choice([-6, 6])
            self._dy = random.choice([-4, 4])
            text_color = color if color.startswith("0x") else f"0x{color}"
            self._label = xbmcgui.ControlLabel(
                self._x, self._y, _LABEL_W, _LABEL_H,
                "[B]Chaturbate TV[/B][CR]waiting for live models",
                font="font30_title",
                textColor=text_color,
                alignment=2,
            )
            self.addControl(self._label)
            _safe_log(f"screensaver: shown color={color}")

        def tick(self) -> None:
            """Advance the label one frame; bounce off edges."""
            self._x += self._dx
            self._y += self._dy
            if self._x <= 0:
                self._x = 0
                self._dx = abs(self._dx)
            elif self._x + _LABEL_W >= _SCREEN_W:
                self._x = _SCREEN_W - _LABEL_W
                self._dx = -abs(self._dx)
            if self._y <= 0:
                self._y = 0
                self._dy = abs(self._dy)
            elif self._y + _LABEL_H >= _SCREEN_H:
                self._y = _SCREEN_H - _LABEL_H
                self._dy = -abs(self._dy)
            try:
                self._label.setPosition(self._x, self._y)
            except Exception:  # noqa: S110 - control may be invalid
                pass

        def onAction(self, action: Any) -> None:
            """Any user input -> dismissed = True; the run loop closes us."""
            _safe_log("screensaver: onAction -> dismissed")
            self.dismissed = True
            try:
                self.close()
            except Exception:  # noqa: S110 - close already torn down
                pass

    return _IdleScreensaver


# Per-test imports rebuild xbmcgui.Window with a fresh stub class, so
# we cannot cache the class globally - issubclass(IdleScreensaver,
# xbmcgui.Window) checks the live xbmcgui at the moment of the
# assertion. Build fresh on every access via the module __getattr__ hook.
def __getattr__(name: str) -> Any:
    if name == "IdleScreensaver":
        return _make_screensaver_class()
    raise AttributeError(name)


# --------------------------------------------------------------------------- #
# Run loop
# --------------------------------------------------------------------------- #


_TickFn = Callable[[float], bool]
_GuardFn = Callable[[], bool]


def run(
    *,
    re_walk_func: Callable[[], Any],
    is_active_func: _GuardFn,
    wait_for_abort: _TickFn,
    heartbeat: Callable[[], None] | None = None,
    re_walk_interval_seconds: float = 60.0,
    min_wait_seconds: float = 0.0,
    tick_interval_seconds: float = 0.05,
    color: str = _DEFAULT_COLOR,
    window_factory: Callable[[], Any] | None = None,
) -> Any:
    """Show the screensaver and drive its tick loop.

    Returns the live entry from ``re_walk_func`` if/when one comes
    online; returns None if dismissed by the user, the active flag was
    cleared, or Kodi is shutting down.

    All "external world" calls (re-walk, active-flag check, abort
    wait) are injected so this function is unit-testable without Kodi.

    ``heartbeat`` (optional) is called once per re-walk beat. The tv_loop
    passes its ``_stamp_progress`` so the out-of-loop wedge watchdog keeps
    seeing progress while the saver idles (v0.7.60); without it a healthy
    all-favs-offline lull would false-trip the 150s watchdog.
    """
    if window_factory is not None:
        win = window_factory()
    else:
        win = _make_screensaver_class()(color=color)
    try:
        win.show()
    except Exception as exc:
        # v0.7.38 (audit pass #4 HIGH #5): if show() raised, the
        # window isn't visible -- onAction won't fire, so the
        # ``while not win.dismissed`` loop below would block until
        # is_active_func flips OR the loop is killed. Bail
        # immediately instead so the TV loop falls through to the
        # next outer iteration.
        _safe_log(f"screensaver.run: show() failed err={exc!r}; bailing")
        return None
    elapsed = 0.0
    # v0.7.63: total wall-clock the saver has been up (accumulated from
    # tick_interval, deterministic + test-drivable). min_wait_seconds > 0 is
    # the forbidden-backoff hold: even when re_walk finds a 'live' target we
    # do NOT resume until total >= min_wait, so on an IP/CDN block TV mode
    # rests the full backoff instead of thrashing straight back into the 403.
    total = 0.0
    try:
        while not win.dismissed:
            if wait_for_abort(tick_interval_seconds):
                _safe_log("screensaver.run: abort requested")
                return None
            if not is_active_func():
                _safe_log("screensaver.run: active flag cleared")
                return None
            # Update geometry one frame.
            try:
                win.tick()
            except Exception as exc:
                _safe_log(f"screensaver.run: tick err={exc!r}")
            elapsed += tick_interval_seconds
            total += tick_interval_seconds
            if elapsed >= re_walk_interval_seconds:
                elapsed = 0.0
                if heartbeat is not None:
                    # v0.7.60: tv_loop is parked inside run() while the saver
                    # idles, so it never reaches its onAVStarted / inner-loop
                    # heartbeat stamp points -- the out-of-loop wedge watchdog
                    # (150s progress threshold) would starve and false-positive
                    # on a healthy idle saver. Stamp on each re-walk beat
                    # (<= re_walk_interval << 150s); a genuinely stuck saver
                    # loop stops beating and still trips the watchdog.
                    heartbeat()
                target = re_walk_func()
                if target is not None:
                    if total >= min_wait_seconds:
                        _safe_log("screensaver.run: re_walk found live target")
                        return target
                    # forbidden-backoff hold: a target is available but we
                    # haven't rested the full backoff yet -- keep idling.
                    _safe_log(
                        f"screensaver.run: holding backoff "
                        f"({total:.0f}/{min_wait_seconds:.0f}s) before resume"
                    )
                else:
                    _safe_log("screensaver.run: re_walk still no live")
        _safe_log("screensaver.run: dismissed by user")
        return None
    finally:
        try:
            win.close()
        except Exception:  # noqa: S110 - already torn down
            pass
