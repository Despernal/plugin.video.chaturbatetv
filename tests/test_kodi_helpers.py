"""Tests for resources.lib.kodi_helpers - thin wrappers over xbmcplugin.

Two functions:
- ``add_dir(handle, label, mode, **params)`` - add a folder ListItem.
- ``add_play_item(handle, label, slug, image=None, **props)`` - add a
  playable ListItem with stream metadata.

We mock xbmc / xbmcgui / xbmcplugin via sys.modules; the helpers just
shape calls and pass through.
"""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def kodi_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    fake_xbmc = MagicMock()
    fake_xbmcgui = MagicMock()
    fake_xbmcplugin = MagicMock()
    fake_xbmcaddon = MagicMock()

    # ListItem instances need to track setProperty / setArt / setInfo
    # calls so the tests can inspect them.
    def make_listitem(*args: Any, **kwargs: Any) -> MagicMock:
        li = MagicMock()
        li.label = kwargs.get("label") or (args[0] if args else "")
        li._props: dict[str, str] = {}
        li._art: dict[str, str] = {}
        li._info: dict[str, dict[str, Any]] = {}
        li._ctx_menu: list[Any] = []

        def set_property(k: str, v: str) -> None:
            li._props[k] = v

        def set_art(art: dict[str, str]) -> None:
            li._art.update(art)

        def set_info(typ: str, info: dict[str, Any]) -> None:
            li._info[typ] = info

        def add_ctx_menu(items: Any, replace: bool = False) -> None:
            li._ctx_menu = list(items)

        li.setProperty = set_property
        li.setArt = set_art
        li.setInfo = set_info
        li.addContextMenuItems = add_ctx_menu
        return li

    fake_xbmcgui.ListItem.side_effect = make_listitem

    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)
    monkeypatch.setitem(sys.modules, "xbmcgui", fake_xbmcgui)
    monkeypatch.setitem(sys.modules, "xbmcplugin", fake_xbmcplugin)
    monkeypatch.setitem(sys.modules, "xbmcaddon", fake_xbmcaddon)

    sys.modules.pop("resources.lib.kodi_helpers", None)
    return {
        "xbmc": fake_xbmc,
        "xbmcgui": fake_xbmcgui,
        "xbmcplugin": fake_xbmcplugin,
        "xbmcaddon": fake_xbmcaddon,
    }


def _import() -> Any:
    import resources.lib.kodi_helpers as mod
    return mod


# --------------------------------------------------------------------------- #
# add_dir
# --------------------------------------------------------------------------- #


def test_add_dir_calls_addDirectoryItem(kodi_mocks: dict[str, MagicMock]) -> None:
    helpers = _import()
    helpers.add_dir(handle=42, label="Top Cams", mode="top")

    plugin = kodi_mocks["xbmcplugin"]
    assert plugin.addDirectoryItem.call_count == 1
    args, kwargs = plugin.addDirectoryItem.call_args
    # Our helper passes handle, url, listitem, isFolder
    handle = kwargs.get("handle", args[0] if args else None)
    assert handle == 42


def test_add_dir_builds_plugin_url_with_mode(kodi_mocks: dict[str, MagicMock]) -> None:
    helpers = _import()
    helpers.add_dir(handle=42, label="Top Cams", mode="top")

    plugin = kodi_mocks["xbmcplugin"]
    args, kwargs = plugin.addDirectoryItem.call_args
    url = kwargs.get("url", args[1] if len(args) > 1 else None)
    assert isinstance(url, str)
    assert "mode=top" in url
    assert url.startswith("plugin://plugin.video.chaturbatetv/")


def test_add_dir_passes_extra_params_in_url(kodi_mocks: dict[str, MagicMock]) -> None:
    helpers = _import()
    helpers.add_dir(handle=42, label="Female", mode="gender", gender="female", page=2)

    plugin = kodi_mocks["xbmcplugin"]
    args, kwargs = plugin.addDirectoryItem.call_args
    url = kwargs.get("url", args[1] if len(args) > 1 else None)
    assert "mode=gender" in url
    assert "gender=female" in url
    assert "page=2" in url


def test_add_dir_marks_isFolder_true(kodi_mocks: dict[str, MagicMock]) -> None:
    helpers = _import()
    helpers.add_dir(handle=42, label="Female", mode="gender")

    plugin = kodi_mocks["xbmcplugin"]
    args, kwargs = plugin.addDirectoryItem.call_args
    is_folder = kwargs.get("isFolder", args[3] if len(args) > 3 else None)
    assert is_folder is True


def test_add_dir_listitem_has_label(kodi_mocks: dict[str, MagicMock]) -> None:
    helpers = _import()
    helpers.add_dir(handle=42, label="Top Cams", mode="top")

    gui = kodi_mocks["xbmcgui"]
    assert gui.ListItem.called
    call = gui.ListItem.call_args
    args = call.args
    kwargs = call.kwargs
    label = kwargs.get("label", args[0] if args else None)
    assert label == "Top Cams"


def _listitem_from_call(call: Any) -> MagicMock:
    args, kwargs = call
    if "listitem" in kwargs:
        return kwargs["listitem"]
    return args[2]


def test_add_dir_supports_image(kodi_mocks: dict[str, MagicMock]) -> None:
    helpers = _import()
    helpers.add_dir(handle=42, label="Top Cams", mode="top",
                    image="https://example.com/icon.png")

    plugin = kodi_mocks["xbmcplugin"]
    listitem = _listitem_from_call(plugin.addDirectoryItem.call_args)
    assert "thumb" in listitem._art or "icon" in listitem._art


# --------------------------------------------------------------------------- #
# add_play_item
# --------------------------------------------------------------------------- #


def test_add_play_item_calls_addDirectoryItem_with_isFolder_false(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    helpers = _import()
    helpers.add_play_item(handle=42, label="alice", slug="alice")

    plugin = kodi_mocks["xbmcplugin"]
    args, kwargs = plugin.addDirectoryItem.call_args
    is_folder = kwargs.get("isFolder", args[3] if len(args) > 3 else None)
    assert is_folder is False


def test_add_play_item_url_uses_playvid_mode(kodi_mocks: dict[str, MagicMock]) -> None:
    helpers = _import()
    helpers.add_play_item(handle=42, label="alice", slug="alice")

    plugin = kodi_mocks["xbmcplugin"]
    args, kwargs = plugin.addDirectoryItem.call_args
    url = kwargs.get("url", args[1] if len(args) > 1 else None)
    assert "mode=playvid" in url
    assert "slug=alice" in url


def test_add_play_item_marks_isPlayable(kodi_mocks: dict[str, MagicMock]) -> None:
    helpers = _import()
    helpers.add_play_item(handle=42, label="alice", slug="alice")

    plugin = kodi_mocks["xbmcplugin"]
    listitem = _listitem_from_call(plugin.addDirectoryItem.call_args)
    assert listitem._props.get("IsPlayable") == "true"


def test_add_play_item_sets_image(kodi_mocks: dict[str, MagicMock]) -> None:
    helpers = _import()
    helpers.add_play_item(handle=42, label="alice", slug="alice",
                          image="https://example.com/alice.jpg")

    plugin = kodi_mocks["xbmcplugin"]
    listitem = _listitem_from_call(plugin.addDirectoryItem.call_args)
    assert listitem._art.get("thumb") == "https://example.com/alice.jpg"


def test_add_play_item_passes_extra_props(kodi_mocks: dict[str, MagicMock]) -> None:
    helpers = _import()
    helpers.add_play_item(handle=42, label="alice", slug="alice", custom="x")

    plugin = kodi_mocks["xbmcplugin"]
    listitem = _listitem_from_call(plugin.addDirectoryItem.call_args)
    url_str = kwargs_or_args(plugin.addDirectoryItem.call_args, "url")
    assert "custom=x" in url_str or listitem._props.get("custom") == "x"


def kwargs_or_args(call: Any, kw: str) -> str:
    args, kwargs = call
    if kw in kwargs:
        return str(kwargs[kw])
    if kw == "url" and len(args) > 1:
        return str(args[1])
    return ""


# --------------------------------------------------------------------------- #
# end_directory
# --------------------------------------------------------------------------- #


def test_end_directory_calls_endOfDirectory(kodi_mocks: dict[str, MagicMock]) -> None:
    helpers = _import()
    helpers.end_directory(42)
    plugin = kodi_mocks["xbmcplugin"]
    plugin.endOfDirectory.assert_called_once()
