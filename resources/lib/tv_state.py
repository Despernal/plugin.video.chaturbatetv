"""Thin wrapper over the Kodi Window(10000) properties used by TV mode.

The single source of truth for the active flag. Lives in its own module
so a) tests can stub it without faking xbmcgui, and b) the property
key only appears in one place (avoids the -era bug where
``cb_tv_active`` was sprinkled across five files).

The ``TVState`` constructor takes a (getter, setter) pair. A real Kodi
caller binds them to ``xbmcgui.Window(10000).getProperty`` /
``xbmcgui.Window(10000).setProperty``; tests pass dict-backed stubs.
"""
from __future__ import annotations

from collections.abc import Callable


_KEY = "chaturbatetv_active"


_Getter = Callable[[str], str]
_Setter = Callable[[str, str], None]


def _safe_log(msg: str) -> None:
    """Log helper that tolerates logger import failures (pure-test paths)."""
    try:
        from resources.lib import logger
        logger._log(msg)
    except Exception:
        return


class TVState:
    """Owns the single Window property that says "TV mode is running"."""

    def __init__(self, getter: _Getter, setter: _Setter) -> None:
        self._get = getter
        self._set = setter

    def is_active(self) -> bool:
        """True only when the prop literally equals the string '1'."""
        v = self._get(_KEY) == "1"
        _safe_log(f"tv_state.is_active -> {v}")
        return v

    def set_active(self, active: bool) -> None:
        """Set the active flag. Writes '1' or '0' so we never store
        empty strings (which would round-trip as 'never set' on a
        fresh Kodi instance and we'd lose the distinction)."""
        _safe_log(f"tv_state.set_active({active})")
        self._set(_KEY, "1" if active else "0")

    def clear(self) -> None:
        """Force the flag back to '0'. Used by ResetTVMode after a
        glitch leaves the state stuck. Equivalent to set_active(False)
        but kept as a separate method for log-line clarity."""
        _safe_log("tv_state.clear")
        self._set(_KEY, "0")
