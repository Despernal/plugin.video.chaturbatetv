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

_DIALOG_TIMEOUT_DEFAULT = 10
_DIALOG_TIMEOUT_FLOOR = 5
_DIALOG_TIMEOUT_CEIL = 60

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


_RESOLUTION_CAPS: dict[str, str] = {
    "auto": "",
    "1080p": "1920x1080",
    "720p": "1280x720",
    "480p": "854x480",
}


def max_resolution() -> str:
    """Return the ISA ``max_resolution`` value for the current setting.

    Empty string = auto (don't cap; ISA picks the highest variant). Any
    other value is a ``WIDTHxHEIGHT`` string ISA accepts directly via
    ``setProperty('inputstream.adaptive.max_resolution', ...)``.

    Capping to 720p is the right call on slower edges or under-powered
    devices: ISA stops trying to upshift to 1080p, audio cadence stays
    matched to a buffer the host can keep full, and "buffering pause"
    events drop sharply.

    Unknown / Kodi-missing -> empty (auto), since silently capping a
    user's stream is more annoying than letting ISA pick wrong.
    """
    try:
        name = str(_addon().getSettingString("max_resolution"))
    except Exception:
        return ""
    return _RESOLUTION_CAPS.get(name.lower(), "")


def show_gender(gender_key: str) -> bool:
    """Whether the main menu should display the named gender entry.

    ``gender_key`` is one of ``"female"``, ``"male"``, ``"couple"``,
    ``"trans"``. Maps to the ``show_<key>`` boolean setting; default
    True so a fresh install shows everything until the user opts out.

    Any failure (missing setting, no Kodi, malformed value) returns
    True - we'd rather show too much than mysteriously hide a category
    if the settings layer has a glitch.
    """
    setting_id = f"show_{gender_key.lower()}"
    try:
        return bool(_addon().getSettingBool(setting_id))
    except Exception:
        return True


def dialog_timeout_seconds() -> int:
    """How long the exit Yes/No dialog stays up before auto-closing.

    Auto-close defaults to "Keep playing" so an accidental Stop press
    that the user walks away from preserves sticky-playback. The
    setting lets users tune the trade-off: lower values resume sooner
    after a misclick, higher values give more time to actually pick
    Exit on a slow remote.

    Floor 5s, ceil 60s, default 10s. Out-of-range / Kodi-missing falls
    back to default (rather than to whatever the slider returned)
    because a 0 here would dismiss the dialog before the user could
    react.
    """
    try:
        v = int(_addon().getSettingInt("dialog_timeout_seconds"))
    except Exception:
        return _DIALOG_TIMEOUT_DEFAULT
    if v < _DIALOG_TIMEOUT_FLOOR or v > _DIALOG_TIMEOUT_CEIL:
        return _DIALOG_TIMEOUT_DEFAULT
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
