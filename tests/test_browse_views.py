"""Tests for resources.lib.browse_views.

We mock Kodi modules through sys.modules so the helpers' lazy imports
hit our fakes. Then we drive each view with a stub fetch_func and
inspect the addDirectoryItem call shapes.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def kodi_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    fake_xbmc = MagicMock()
    fake_xbmcgui = MagicMock()
    fake_xbmcplugin = MagicMock()
    fake_xbmcaddon = MagicMock()
    fake_xbmcvfs = MagicMock()
    fake_xbmcvfs.translatePath = lambda p: "/tmp/" + p.split("//")[-1]

    def make_listitem(*args: Any, **kwargs: Any) -> MagicMock:
        li = MagicMock()
        li.label = kwargs.get("label") or (args[0] if args else "")
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
    sys.modules.pop("resources.lib.browse_views", None)

    return {
        "xbmc": fake_xbmc,
        "xbmcgui": fake_xbmcgui,
        "xbmcplugin": fake_xbmcplugin,
        "xbmcaddon": fake_xbmcaddon,
        "xbmcvfs": fake_xbmcvfs,
    }


def _import() -> Any:
    import resources.lib.browse_views as mod
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


# --------------------------------------------------------------------------- #
# main_menu
# --------------------------------------------------------------------------- #


def test_main_menu_adds_top_cams(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    bv.main_menu(handle=42)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    assert any("mode=top" in u for u in urls)


def test_main_menu_adds_new_cams(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    bv.main_menu(handle=42)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    assert any("mode=new" in u for u in urls)


def test_main_menu_adds_gender_filters(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    bv.main_menu(handle=42)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    for g in ("female", "male", "couple", "trans"):
        assert any("mode=gender" in u and f"gender={g}" in u for u in urls), g


def test_main_menu_adds_search_tv_favs(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    bv.main_menu(handle=42)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    assert any("mode=search" in u for u in urls)
    assert any("mode=tv_list" in u for u in urls)
    assert any("mode=favs" in u for u in urls)


def test_main_menu_calls_endOfDirectory(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    bv.main_menu(handle=42)
    kodi_mocks["xbmcplugin"].endOfDirectory.assert_called_once()


def test_main_menu_color_tags_female_label(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    bv.main_menu(handle=42)
    gui = kodi_mocks["xbmcgui"]
    labels = [
        call.kwargs.get("label", call.args[0] if call.args else "")
        for call in gui.ListItem.call_args_list
    ]
    female_labels = [label for label in labels if "Female" in label]
    assert female_labels
    assert any("00d4ff" in lab for lab in female_labels)


# --------------------------------------------------------------------------- #
# top_cams_view
# --------------------------------------------------------------------------- #


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_top_cams_view_renders_models(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    html = _read("sample_top_cams.html")

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        return html

    bv.top_cams_view(handle=42, fetch_func=fetch)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    # 4 models + 1 next-page entry
    assert sum(1 for u in urls if "mode=playvid" in u) == 4
    assert any("mode=top" in u and "page=2" in u for u in urls)


def test_top_cams_view_passes_page_to_fetch(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    seen: list[str] = []

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        seen.append(url)
        return _read("sample_empty_listing.html")

    bv.top_cams_view(handle=42, page=3, fetch_func=fetch)
    assert any("page=3" in u for u in seen)


def test_top_cams_view_handles_empty_page(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        return _read("sample_empty_listing.html")

    bv.top_cams_view(handle=42, fetch_func=fetch)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    # No playvid items, just a next-page link.
    assert all("mode=playvid" not in u for u in urls)
    assert any("mode=top" in u and "page=2" in u for u in urls)


# --------------------------------------------------------------------------- #
# new_cams_view
# --------------------------------------------------------------------------- #


def test_new_cams_view_renders(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        return _read("sample_top_cams.html")

    bv.new_cams_view(handle=42, fetch_func=fetch)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    assert sum(1 for u in urls if "mode=playvid" in u) == 4


# --------------------------------------------------------------------------- #
# gender_view
# --------------------------------------------------------------------------- #


def test_gender_view_filters_to_gender(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        return _read("sample_top_cams.html")

    bv.gender_view(handle=42, gender="male", fetch_func=fetch)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    play_urls = [u for u in urls if "mode=playvid" in u]
    # Only sample_room_3 is male in the fixture.
    assert len(play_urls) == 1
    assert "slug=sample_room_3" in play_urls[0]


def test_gender_view_unknown_gender_closes_directory(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    bv = _import()
    bv.gender_view(handle=42, gender="bogus", fetch_func=lambda *a, **k: "")
    kodi_mocks["xbmcplugin"].endOfDirectory.assert_called_once()


# --------------------------------------------------------------------------- #
# search_view
# --------------------------------------------------------------------------- #


def test_search_view_with_query_renders(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        return _read("sample_search_results.html")

    bv.search_view(handle=42, query="sample", fetch_func=fetch)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    assert any("slug=sample_search_1" in u for u in urls)


def test_search_view_empty_query_closes_directory(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    bv = _import()
    bv.search_view(handle=42, query="", fetch_func=lambda *a, **k: "")
    kodi_mocks["xbmcplugin"].endOfDirectory.assert_called_once()
