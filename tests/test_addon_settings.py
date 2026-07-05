"""Tests for resources.lib.addon_settings - typed accessors over Kodi's
xbmcaddon settings layer.

Each accessor reads via ``xbmcaddon.Addon().getSetting{Int,String}``,
clamps/validates, and returns a safe default if Kodi is missing or the
setting is malformed. Tests run with a stubbed xbmcaddon so we don't
need a live Kodi.
"""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest


def _install_xbmcaddon(monkeypatch: pytest.MonkeyPatch,
                       values: dict[str, Any]) -> None:
    """Install a fake xbmcaddon whose Addon().getSettingX returns ``values``."""
    fake_addon = MagicMock()
    fake_addon.getSettingInt = lambda k: values.get(k, 0)
    fake_addon.getSettingString = lambda k: values.get(k, "")
    fake_addon.getSettingBool = lambda k: bool(values.get(k, False))

    fake_xbmcaddon = MagicMock()
    fake_xbmcaddon.Addon = lambda *args, **kwargs: fake_addon
    monkeypatch.setitem(sys.modules, "xbmcaddon", fake_xbmcaddon)
    sys.modules.pop("resources.lib.addon_settings", None)


def _import() -> Any:
    import resources.lib.addon_settings as mod
    return mod


# --------------------------------------------------------------------------- #
# poll_minutes
# --------------------------------------------------------------------------- #


def test_poll_minutes_reads_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_xbmcaddon(monkeypatch, {"poll_minutes": 5})
    assert _import().poll_minutes() == 5


def test_forbidden_backoff_minutes_reads_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.63: minutes TV mode rests in the screensaver on a forbidden/IP
    block before retrying."""
    _install_xbmcaddon(monkeypatch, {"forbidden_backoff_minutes": 30})
    assert _import().forbidden_backoff_minutes() == 30


def test_forbidden_backoff_minutes_defaults_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """getSettingInt returns 0 when never written -> fall back to 20, never 0
    (0 would busy-retry with no rest)."""
    _install_xbmcaddon(monkeypatch, {"forbidden_backoff_minutes": 0})
    assert _import().forbidden_backoff_minutes() == 20


def test_forbidden_backoff_minutes_clamps_above_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_xbmcaddon(monkeypatch, {"forbidden_backoff_minutes": 500})
    assert _import().forbidden_backoff_minutes() == 120


def test_poll_minutes_clamps_below_one_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``getSettingInt`` returns 0 when the setting was never written;
    we must not feed 0 to the loop (would 100% CPU spin)."""
    _install_xbmcaddon(monkeypatch, {"poll_minutes": 0})
    assert _import().poll_minutes() == 10


def test_poll_minutes_clamps_above_60(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_xbmcaddon(monkeypatch, {"poll_minutes": 999})
    assert _import().poll_minutes() == 60


def test_poll_minutes_default_when_no_kodi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No xbmcaddon -> 10 (matches PLANNING.md default)."""
    monkeypatch.delitem(sys.modules, "xbmcaddon", raising=False)

    def boom(name: str, *_a: Any, **_k: Any) -> Any:
        if name == "xbmcaddon":
            raise ImportError("no Kodi")
        return None

    sys.modules.pop("resources.lib.addon_settings", None)
    # Block the import: install a sentinel that raises on Addon().
    fake = MagicMock()
    fake.Addon = MagicMock(side_effect=RuntimeError("no Kodi"))
    monkeypatch.setitem(sys.modules, "xbmcaddon", fake)
    assert _import().poll_minutes() == 10


# --------------------------------------------------------------------------- #
# isa_proxy_port
# --------------------------------------------------------------------------- #


def test_isa_proxy_port_reads_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_xbmcaddon(monkeypatch, {"isa_proxy_port": 12345})
    assert _import().isa_proxy_port() == 12345


def test_isa_proxy_port_zero_means_kernel_assigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0 is the documented sentinel for 'let the kernel pick'.
    The accessor must preserve that."""
    _install_xbmcaddon(monkeypatch, {"isa_proxy_port": 0})
    assert _import().isa_proxy_port() == 0


def test_isa_proxy_port_negative_falls_back_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_xbmcaddon(monkeypatch, {"isa_proxy_port": -1})
    assert _import().isa_proxy_port() == 0


def test_isa_proxy_port_above_65535_falls_back_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_xbmcaddon(monkeypatch, {"isa_proxy_port": 70000})
    assert _import().isa_proxy_port() == 0


# --------------------------------------------------------------------------- #
# screensaver_color
# --------------------------------------------------------------------------- #


def test_screensaver_color_cyan(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_xbmcaddon(monkeypatch, {"screensaver_color": "cyan"})
    assert _import().screensaver_color() == "FF00d4ff"


def test_screensaver_color_green(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_xbmcaddon(monkeypatch, {"screensaver_color": "green"})
    assert _import().screensaver_color() == "FF00ff88"


def test_screensaver_color_hotpink(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_xbmcaddon(monkeypatch, {"screensaver_color": "hotpink"})
    assert _import().screensaver_color() == "FFff0080"


def test_screensaver_color_unknown_falls_back_to_cyan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_xbmcaddon(monkeypatch, {"screensaver_color": "magenta"})
    assert _import().screensaver_color() == "FF00d4ff"


def test_screensaver_color_empty_falls_back_to_cyan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_xbmcaddon(monkeypatch, {"screensaver_color": ""})
    assert _import().screensaver_color() == "FF00d4ff"


# --------------------------------------------------------------------------- #
# max_resolution
# --------------------------------------------------------------------------- #


def test_max_resolution_auto_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """``auto`` -> empty string -> caller skips setProperty so ISA picks."""
    _install_xbmcaddon(monkeypatch, {"max_resolution": "auto"})
    assert _import().max_resolution() == ""


def test_max_resolution_1080p(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_xbmcaddon(monkeypatch, {"max_resolution": "1080p"})
    assert _import().max_resolution() == "1920x1080"


def test_max_resolution_720p(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_xbmcaddon(monkeypatch, {"max_resolution": "720p"})
    assert _import().max_resolution() == "1280x720"


def test_max_resolution_480p(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_xbmcaddon(monkeypatch, {"max_resolution": "480p"})
    assert _import().max_resolution() == "854x480"


def test_max_resolution_unknown_falls_back_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_xbmcaddon(monkeypatch, {"max_resolution": "8k"})
    assert _import().max_resolution() == ""


def test_max_resolution_kodi_missing_falls_back_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock()
    fake.Addon = MagicMock(side_effect=RuntimeError("no Kodi"))
    monkeypatch.setitem(sys.modules, "xbmcaddon", fake)
    sys.modules.pop("resources.lib.addon_settings", None)
    assert _import().max_resolution() == ""


# --------------------------------------------------------------------------- #
# show_gender
# --------------------------------------------------------------------------- #


def test_show_gender_reads_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_xbmcaddon(monkeypatch, {
        "show_female": True, "show_male": True,
        "show_couple": False, "show_trans": False,
    })
    fv = _import()
    assert fv.show_gender("female") is True
    assert fv.show_gender("male") is True
    assert fv.show_gender("couple") is False
    assert fv.show_gender("trans") is False


def test_show_gender_default_true_when_kodi_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """xbmcaddon import explodes -> default True (don't silently hide
    a category if the settings layer is broken)."""
    fake = MagicMock()
    fake.Addon = MagicMock(side_effect=RuntimeError("no Kodi"))
    monkeypatch.setitem(sys.modules, "xbmcaddon", fake)
    sys.modules.pop("resources.lib.addon_settings", None)
    assert _import().show_gender("female") is True
