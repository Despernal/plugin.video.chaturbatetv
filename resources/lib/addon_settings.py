"""Typed accessors over Kodi's xbmcaddon settings layer.

``settings.xml`` exposes a handful of ints, bools and string options.
Reading them straight off ``xbmcaddon.Addon().getSettingX`` at every
callsite produces three problems:

1. Defensive boilerplate everywhere (Kodi missing, value malformed,
   default needed) - easy to drift between modules.
2. The bare integer values can be 0 by default which means "never set"
   - we don't want to feed 0 to ``poll_minutes`` (would hot-spin) or
   misinterpret a 0 elsewhere.
3. ``screensaver_color`` is a friendly name (``cyan``, ``green``,
   ``hotpink``) but every consumer wants a Kodi-format AARRGGBB hex.

Each accessor in this module reads, validates, clamps, and returns a
sensible default if Kodi is missing or the value is malformed.
"""
from __future__ import annotations

from typing import Any


_POLL_MIN_DEFAULT = 10
_POLL_MIN_FLOOR = 1
_POLL_MIN_CEIL = 60

_PROXY_PORT_DEFAULT = 0  # 0 = kernel-assigned

# Friendly name -> 8-char AARRGGBB hex. Kodi's [COLOR] tag rejects 6-char
# (silently renders blank), so every entry must be 8 chars.
_SCREENSAVER_COLORS: dict[str, str] = {
    "cyan": "FF00d4ff",
    "green": "FF00ff88",
    "hotpink": "FFff0080",
}
_SCREENSAVER_COLOR_DEFAULT = _SCREENSAVER_COLORS["cyan"]


def _addon() -> Any:
    """Resolve ``xbmcaddon.Addon()`` or raise. Callers swallow."""
    import xbmcaddon
    return xbmcaddon.Addon()


def poll_minutes() -> int:
    """How often the TV loop re-checks the priority list, in minutes.

    settings.xml constrains 1..60 but ``getSettingInt`` returns 0 when
    the setting was never written, so we floor to ``_POLL_MIN_DEFAULT``.
    """
    try:
        v = int(_addon().getSettingInt("poll_minutes"))
    except Exception:
        return _POLL_MIN_DEFAULT
    if v < _POLL_MIN_FLOOR:
        return _POLL_MIN_DEFAULT
    if v > _POLL_MIN_CEIL:
        return _POLL_MIN_CEIL
    return v


def isa_proxy_port() -> int:
    """Localhost port for the HLS proxy. 0 = let the kernel pick.

    Out-of-range values fall back to 0 rather than to a random fixed
    port because forcing a specific port can collide with whatever else
    is running on the host (LibreELEC ships several listeners on
    8080/9090/etc).
    """
    try:
        v = int(_addon().getSettingInt("isa_proxy_port"))
    except Exception:
        return _PROXY_PORT_DEFAULT
    if v < 0 or v > 65535:
        return _PROXY_PORT_DEFAULT
    return v


def screensaver_color() -> str:
    """Map the named option to its 8-char AARRGGBB hex.

    Unknown / empty / Kodi-missing all fall back to cyan so the
    screensaver always renders a visible label.
    """
    try:
        name = str(_addon().getSettingString("screensaver_color"))
    except Exception:
        return _SCREENSAVER_COLOR_DEFAULT
    return _SCREENSAVER_COLORS.get(name.lower(), _SCREENSAVER_COLOR_DEFAULT)
