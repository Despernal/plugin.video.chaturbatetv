"""Tests for resources.lib.favs_views.

These views use favs_store for persistence + cb_client.is_model_live
for the online/offline split. Both are injectable for tests.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from resources.lib.cb_models import Favorite, Gender


@pytest.fixture
def kodi_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    fake_xbmc = MagicMock()
    fake_xbmcgui = MagicMock()
    fake_xbmcplugin = MagicMock()
    fake_xbmcaddon = MagicMock()
    fake_xbmcvfs = MagicMock()
    fake_xbmcvfs.translatePath = lambda p: p

    def make_listitem(*args: Any, **kwargs: Any) -> MagicMock:
        li = MagicMock()
        li._props: dict[str, str] = {}
        li._art: dict[str, str] = {}
        li.setProperty = lambda k, v: li._props.update({k: v})
        li.setArt = lambda art: li._art.update(art)
        li.setInfo = lambda *a, **k: None
        return li

    fake_xbmcgui.ListItem.side_effect = make_listitem

    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)
    monkeypatch.setitem(sys.modules, "xbmcgui", fake_xbmcgui)
    monkeypatch.setitem(sys.modules, "xbmcplugin", fake_xbmcplugin)
    monkeypatch.setitem(sys.modules, "xbmcaddon", fake_xbmcaddon)
    monkeypatch.setitem(sys.modules, "xbmcvfs", fake_xbmcvfs)

    sys.modules.pop("resources.lib.kodi_helpers", None)
    sys.modules.pop("resources.lib.favs_views", None)

    return {
        "xbmc": fake_xbmc,
        "xbmcgui": fake_xbmcgui,
        "xbmcplugin": fake_xbmcplugin,
        "xbmcaddon": fake_xbmcaddon,
        "xbmcvfs": fake_xbmcvfs,
    }


def _import() -> Any:
    import resources.lib.favs_views as mod
    return mod


def _added_urls(plugin: MagicMock) -> list[str]:
    out: list[str] = []
    for call in plugin.addDirectoryItem.call_args_list:
        args, kwargs = call
        if "url" in kwargs:
            out.append(str(kwargs["url"]))
        elif len(args) > 1:
            out.append(str(args[1]))
    return out


def _write_favs(path: Path, favs: list[Favorite]) -> None:
    rows = [
        {"slug": f.slug, "name": f.name, "url": f.url, "gender": f.gender.value}
        for f in favs
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"favorites": rows}), encoding="utf-8")


def _live_fetch(live_slugs: set[str]) -> Any:
    """Builds a fetch_func that reports the given slugs as live."""
    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        slug = ""
        if body:
            for piece in body.decode().split("&"):
                if piece.startswith("room_slug="):
                    slug = piece.split("=", 1)[1]
                    break
        if slug in live_slugs:
            return json.dumps({
                "success": True,
                "url": f"https://e/{slug}.m3u8",
                "room_status": "public",
                "hidden_message": "",
                "cmaf_edge": False,
            })
        return json.dumps({
            "success": True,
            "url": "",
            "room_status": "offline",
            "hidden_message": "",
            "cmaf_edge": False,
        })
    return fetch


# --------------------------------------------------------------------------- #
# favs_menu
# --------------------------------------------------------------------------- #


def test_favs_menu_shows_online_offline_split(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    fv = _import()
    favs_path = tmp_path / "favs.json"
    _write_favs(favs_path, [
        Favorite(name="alice", slug="alice", url="https://chaturbate.com/alice/",
                 gender=Gender.FEMALE),
        Favorite(name="bob", slug="bob", url="https://chaturbate.com/bob/",
                 gender=Gender.MALE),
        Favorite(name="cara", slug="cara", url="https://chaturbate.com/cara/",
                 gender=Gender.FEMALE),
    ])
    fetch = _live_fetch({"alice", "cara"})

    fv.favs_menu(handle=42, store_path=favs_path, fetch_func=fetch)

    gui = kodi_mocks["xbmcgui"]
    labels = [
        call.kwargs.get("label") or (call.args[0] if call.args else "")
        for call in gui.ListItem.call_args_list
    ]
    assert any("Online (2)" in lab for lab in labels)
    assert any("Offline (1)" in lab for lab in labels)


def test_favs_menu_empty_when_no_favs(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    fv = _import()
    favs_path = tmp_path / "favs.json"

    fv.favs_menu(handle=42, store_path=favs_path,
                 fetch_func=_live_fetch(set()))

    gui = kodi_mocks["xbmcgui"]
    labels = [
        call.kwargs.get("label") or (call.args[0] if call.args else "")
        for call in gui.ListItem.call_args_list
    ]
    assert any("Online (0)" in lab for lab in labels)
    assert any("Offline (0)" in lab for lab in labels)


# --------------------------------------------------------------------------- #
# online_favs_view / offline_favs_view
# --------------------------------------------------------------------------- #


def test_online_favs_view_renders_only_live(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    fv = _import()
    favs_path = tmp_path / "favs.json"
    _write_favs(favs_path, [
        Favorite(name="alice", slug="alice", url="https://chaturbate.com/alice/",
                 gender=Gender.FEMALE),
        Favorite(name="bob", slug="bob", url="https://chaturbate.com/bob/",
                 gender=Gender.MALE),
    ])
    fetch = _live_fetch({"alice"})

    fv.online_favs_view(handle=42, store_path=favs_path, fetch_func=fetch)

    urls = _added_urls(kodi_mocks["xbmcplugin"])
    play_urls = [u for u in urls if "mode=playvid" in u]
    assert len(play_urls) == 1
    assert "slug=alice" in play_urls[0]


def test_offline_favs_view_renders_only_offline(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    fv = _import()
    favs_path = tmp_path / "favs.json"
    _write_favs(favs_path, [
        Favorite(name="alice", slug="alice", url="https://chaturbate.com/alice/",
                 gender=Gender.FEMALE),
        Favorite(name="bob", slug="bob", url="https://chaturbate.com/bob/",
                 gender=Gender.MALE),
    ])
    fetch = _live_fetch({"alice"})

    fv.offline_favs_view(handle=42, store_path=favs_path, fetch_func=fetch)

    urls = _added_urls(kodi_mocks["xbmcplugin"])
    play_urls = [u for u in urls if "mode=playvid" in u]
    assert len(play_urls) == 1
    assert "slug=bob" in play_urls[0]
