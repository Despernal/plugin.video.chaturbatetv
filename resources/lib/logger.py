"""Gated debug logger that mirrors 's cb_feature.log helper.

A single function ``_log`` is exposed. It only writes when the addon's
``enh_debug`` boolean setting is on; otherwise it drops silently. Any
exception during the settings read or the disk write is swallowed - the
TV loop must never crash on a log line.

Default log path is ``special://temp/chaturbatetv_feature.log`` resolved
through ``xbmcvfs.translatePath``. Tests pass an explicit ``log_path``
to keep the temp dir off the user's system.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

_LOG_FILENAME = "chaturbatetv_feature.log"


def _is_debug_enabled() -> bool:
    """Read ``enh_debug`` via xbmcaddon. Any failure -> False (silent)."""
    try:
        import xbmcaddon
        return bool(xbmcaddon.Addon().getSettingBool("enh_debug"))
    except Exception:
        return False


def _default_log_path() -> Path | None:
    """Resolve the default log path through xbmcvfs. None if Kodi missing."""
    try:
        import xbmcvfs
        base = xbmcvfs.translatePath("special://temp/")
        return Path(base) / _LOG_FILENAME
    except Exception:
        return None


def _log(msg: Any, log_path: Path | None = None) -> None:
    """Append ``msg`` to the feature log when ``enh_debug`` is on.

    ``msg`` is coerced to ``str()`` so callers can hand in dicts (handy
    for structured-ish logging like ``{"event": "tv_promote"}``).

    Never raises. Disk full, permission denied, settings.xml missing -
    all silent drops.
    """
    if not _is_debug_enabled():
        return
    target = log_path if log_path is not None else _default_log_path()
    if target is None:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = f"{_dt.datetime.now().isoformat(timespec='seconds')} {msg!s}\n"
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        return
