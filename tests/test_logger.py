"""Tests for resources.lib.logger - gated _log() to chaturbatetv_feature.log.

The logger reads ``enh_debug`` from xbmcaddon.Addon().getSettingBool() and
writes lines to ``chaturbatetv_feature.log`` under
``special://temp/`` (or, in tests, a tmp_path we feed in).

Pattern lifted from 's cb_feature.log helper:

- enh_debug=True  -> write the line, with a timestamp.
- enh_debug=False -> silently drop.
- Anything raises during getSetting? Drop silently. We never want a
  log line to crash the TV loop.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_addon(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install a fake xbmcaddon module that returns a controllable Addon."""
    fake_xbmcaddon = MagicMock()
    addon = MagicMock()
    fake_xbmcaddon.Addon.return_value = addon
    monkeypatch.setitem(sys.modules, "xbmcaddon", fake_xbmcaddon)

    fake_xbmc = MagicMock()
    fake_xbmc.LOGINFO = 1
    fake_xbmc.LOGWARNING = 2
    fake_xbmc.LOGERROR = 3
    fake_xbmc.LOGDEBUG = 0
    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)

    fake_xbmcvfs = MagicMock()
    fake_xbmcvfs.translatePath = lambda p: p
    monkeypatch.setitem(sys.modules, "xbmcvfs", fake_xbmcvfs)

    # Force a clean re-import of logger so it picks up our fakes.
    sys.modules.pop("resources.lib.logger", None)
    return addon


def _import_logger() -> Any:
    import resources.lib.logger as mod
    return mod


def test_log_writes_when_enh_debug_true(tmp_path: Path, fake_addon: MagicMock) -> None:
    fake_addon.getSettingBool.return_value = True
    log_path = tmp_path / "chaturbatetv_feature.log"

    logger = _import_logger()
    logger._log("hello world", log_path=log_path)

    contents = log_path.read_text(encoding="utf-8")
    assert "hello world" in contents


def test_log_silent_when_enh_debug_false(tmp_path: Path, fake_addon: MagicMock) -> None:
    fake_addon.getSettingBool.return_value = False
    log_path = tmp_path / "chaturbatetv_feature.log"

    logger = _import_logger()
    logger._log("should not appear", log_path=log_path)

    assert not log_path.exists()


def test_log_tolerates_settings_exception(tmp_path: Path, fake_addon: MagicMock) -> None:
    """If getSettingBool raises (e.g. settings.xml missing), drop silently."""
    fake_addon.getSettingBool.side_effect = RuntimeError("no settings")
    log_path = tmp_path / "chaturbatetv_feature.log"

    logger = _import_logger()
    # Must not raise.
    logger._log("never written", log_path=log_path)

    assert not log_path.exists()


def test_log_appends_to_existing_file(tmp_path: Path, fake_addon: MagicMock) -> None:
    fake_addon.getSettingBool.return_value = True
    log_path = tmp_path / "chaturbatetv_feature.log"
    log_path.write_text("first line\n", encoding="utf-8")

    logger = _import_logger()
    logger._log("second line", log_path=log_path)

    contents = log_path.read_text(encoding="utf-8")
    assert "first line" in contents
    assert "second line" in contents


def test_log_includes_timestamp(tmp_path: Path, fake_addon: MagicMock) -> None:
    fake_addon.getSettingBool.return_value = True
    log_path = tmp_path / "chaturbatetv_feature.log"

    logger = _import_logger()
    logger._log("ping", log_path=log_path)

    contents = log_path.read_text(encoding="utf-8")
    # Expect ISO-ish timestamp at the start of the line: 2026-04-26 ...
    assert re.search(r"\d{4}-\d{2}-\d{2}", contents) is not None


def test_log_handles_non_string_input(tmp_path: Path, fake_addon: MagicMock) -> None:
    """A common bug: code passes a non-str (dict, int) and the logger blows up."""
    fake_addon.getSettingBool.return_value = True
    log_path = tmp_path / "chaturbatetv_feature.log"

    logger = _import_logger()
    logger._log({"event": "tv_promote", "from": 10, "to": 17}, log_path=log_path)

    contents = log_path.read_text(encoding="utf-8")
    assert "tv_promote" in contents


def test_log_drop_on_oserror_writing(tmp_path: Path, fake_addon: MagicMock,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Disk full / permission denied must not bubble out of _log."""
    fake_addon.getSettingBool.return_value = True
    log_path = tmp_path / "chaturbatetv_feature.log"

    logger = _import_logger()

    real_open = open

    def bad_open(*args: Any, **kwargs: Any) -> Any:
        if str(args[0]) == str(log_path):
            raise OSError("disk full")
        return real_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", bad_open)
    # Must not raise.
    logger._log("no disk", log_path=log_path)


def test_log_default_path_resolves_via_xbmcvfs(tmp_path: Path, fake_addon: MagicMock,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """No log_path arg -> resolve via xbmcvfs.translatePath('special://temp/')."""
    fake_addon.getSettingBool.return_value = True

    fake_xbmcvfs = sys.modules["xbmcvfs"]
    fake_xbmcvfs.translatePath = lambda p: str(tmp_path)  # type: ignore[attr-defined]

    logger = _import_logger()
    logger._log("default-path")

    expected = tmp_path / "chaturbatetv_feature.log"
    assert expected.exists()
    assert "default-path" in expected.read_text(encoding="utf-8")
