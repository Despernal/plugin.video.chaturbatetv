"""Tests for resources.lib.browse_views.

We mock Kodi modules through sys.modules so the helpers' lazy imports
hit our fakes. Then we drive each view with a stub fetch_func and
inspect the addDirectoryItem call shapes.
"""
from __future__ import annotations

import json
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


def test_main_menu_hides_gender_when_show_setting_false(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User can toggle off any gender they don't want cluttering the
    main menu (settings.xml: show_female / show_male / show_couple /
    show_trans). Top Cams + New Cams + Search + TV + Favorites stay."""
    import resources.lib.addon_settings as addon_settings_mod
    # Hide trans + male; show female + couple.
    visible = {"female": True, "male": False, "couple": True, "trans": False}
    monkeypatch.setattr(
        addon_settings_mod, "show_gender",
        lambda key: visible.get(key, True),
    )
    bv = _import()
    bv.main_menu(handle=42)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    assert any("gender=female" in u for u in urls)
    assert any("gender=couple" in u for u in urls)
    assert not any("gender=male" in u for u in urls)
    assert not any("gender=trans" in u for u in urls)
    # Non-gender entries always present.
    assert any("mode=search_prompt" in u for u in urls)
    assert any("mode=tv_list" in u for u in urls)


def test_main_menu_adds_search_tv_favs(kodi_mocks: dict[str, MagicMock]) -> None:
    """The main-menu Search entry must route to search_prompt (the input
    dialog opener), NOT the bare search result-renderer. ``search_view``
    closes the directory on empty query, so a click would silently show
    a blank page if we routed to mode=search.
    """
    bv = _import()
    bv.main_menu(handle=42)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    assert any("mode=search_prompt" in u for u in urls), (
        f"main-menu Search must route to search_prompt; urls={urls}"
    )
    assert any("mode=tv_list" in u for u in urls)
    assert any("mode=favs" in u for u in urls)


def test_main_menu_calls_endOfDirectory(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    bv.main_menu(handle=42)
    kodi_mocks["xbmcplugin"].endOfDirectory.assert_called_once()


def test_main_menu_color_tags_female_label(kodi_mocks: dict[str, MagicMock]) -> None:
    """Color tags must include the 8-char hex with FF alpha prefix; Kodi
    silently drops [COLOR <hex>] when the hex is 6 chars (no alpha) and
    renders the wrapped label as blank.
    """
    bv = _import()
    bv.main_menu(handle=42)
    gui = kodi_mocks["xbmcgui"]
    labels = [
        call.kwargs.get("label", call.args[0] if call.args else "")
        for call in gui.ListItem.call_args_list
    ]
    female_labels = [label for label in labels if "Female" in label]
    assert female_labels
    assert any("FF00d4ff" in lab for lab in female_labels), (
        "Color tags must use 8-char hex (FF<RRGGBB>); 6-char hex makes Kodi render blank"
    )


def test_main_menu_all_color_tags_have_alpha_prefix(kodi_mocks: dict[str, MagicMock]) -> None:
    """Regression guard: every [COLOR <hex>] in main menu must be 8-char hex."""
    import re
    bv = _import()
    bv.main_menu(handle=42)
    gui = kodi_mocks["xbmcgui"]
    labels = [
        call.kwargs.get("label", call.args[0] if call.args else "")
        for call in gui.ListItem.call_args_list
    ]
    color_tag = re.compile(r"\[COLOR ([0-9A-Fa-f]+)\]")
    for label in labels:
        for hex_str in color_tag.findall(label):
            assert len(hex_str) == 8, (
                f"Color {hex_str!r} in {label!r} is {len(hex_str)} chars; "
                "Kodi requires 8-char AARRGGBB"
            )


# --------------------------------------------------------------------------- #
# top_cams_view
# --------------------------------------------------------------------------- #


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


SAMPLE_JSON = "sample_roomlist.json"
EMPTY_JSON = '{"rooms": [], "total_count": 0, "all_rooms_count": 0}'


def test_top_cams_view_renders_models(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    body_text = _read(SAMPLE_JSON)

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        return body_text

    bv.top_cams_view(handle=42, fetch_func=fetch)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    # 5 models in the fixture + 1 next-page entry
    assert sum(1 for u in urls if "mode=playvid" in u) == 5
    assert any("mode=top" in u and "page=2" in u for u in urls)


def test_top_cams_view_sorts_by_viewers_descending(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Regression: chaturbate's room-list API returns rooms in mixed
    order (top ~8 high-traffic then a roughly-random tail). Browse views
    must client-side sort by num_users descending so the user sees
    most-watched on top - which is what their site does, what 
    would do if they sorted, and what the user expects for "top cams".
    """
    bv = _import()
    body = json.dumps({
        "rooms": [
            {"username": "low", "gender": "f", "num_users": 18,
             "label": "public"},
            {"username": "mid", "gender": "f", "num_users": 500,
             "label": "public"},
            {"username": "high", "gender": "f", "num_users": 15000,
             "label": "public"},
            {"username": "med2", "gender": "f", "num_users": 4000,
             "label": "public"},
        ],
        "total_count": 4, "all_rooms_count": 4,
    })

    def fetch(*_a: Any, **_kw: Any) -> str:
        return body

    bv.top_cams_view(handle=42, fetch_func=fetch)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    play_urls = [u for u in urls if "mode=playvid" in u]
    slugs_in_order = [u.split("slug=")[1].split("&")[0] for u in play_urls]
    assert slugs_in_order == ["high", "med2", "mid", "low"]


def test_gender_view_sorts_by_viewers_descending(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Same sort applies to ``gender_view`` (Female/Male/Couple/Trans)."""
    bv = _import()
    body = json.dumps({
        "rooms": [
            {"username": "x", "gender": "f", "num_users": 100,
             "label": "public"},
            {"username": "y", "gender": "f", "num_users": 9999,
             "label": "public"},
            {"username": "z", "gender": "f", "num_users": 1,
             "label": "public"},
        ],
        "total_count": 3, "all_rooms_count": 3,
    })

    def fetch(*_a: Any, **_kw: Any) -> str:
        return body

    bv.gender_view(handle=42, gender="female", fetch_func=fetch)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    play_urls = [u for u in urls if "mode=playvid" in u]
    slugs_in_order = [u.split("slug=")[1].split("&")[0] for u in play_urls]
    assert slugs_in_order == ["y", "x", "z"]


def test_top_cams_view_passes_page_to_fetch(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    seen: list[str] = []

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        seen.append(url)
        return EMPTY_JSON

    bv.top_cams_view(handle=42, page=3, fetch_func=fetch)
    # Page 3 with default limit 100 = offset 200
    assert any("offset=200" in u for u in seen)


def test_top_cams_view_handles_empty_page(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        return EMPTY_JSON

    bv.top_cams_view(handle=42, fetch_func=fetch)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    # No playvid items, just a next-page link.
    assert all("mode=playvid" not in u for u in urls)
    assert any("mode=top" in u and "page=2" in u for u in urls)


# new_cams_view ----------------------------------------------------------- #


def test_new_cams_view_renders(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    body_text = _read(SAMPLE_JSON)

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        return body_text

    bv.new_cams_view(handle=42, fetch_func=fetch)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    assert sum(1 for u in urls if "mode=playvid" in u) == 5


def test_new_cams_view_uses_new_cams_param(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    seen: list[str] = []

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        seen.append(url)
        return EMPTY_JSON

    bv.new_cams_view(handle=42, fetch_func=fetch)
    assert any("new_cams=true" in u for u in seen)


# gender_view ------------------------------------------------------------- #


def test_gender_view_passes_gender_code_to_fetch(kodi_mocks: dict[str, MagicMock]) -> None:
    """The view passes the right gender code (m for male) to the API URL.

    With JSON API, the API does the gender filter server-side - we no longer
    re-filter client-side. Test asserts the URL is correct.
    """
    bv = _import()
    seen: list[str] = []

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        seen.append(url)
        return EMPTY_JSON

    bv.gender_view(handle=42, gender="male", fetch_func=fetch)
    assert any("genders=m" in u for u in seen)


def test_gender_view_renders_what_api_returns(kodi_mocks: dict[str, MagicMock]) -> None:
    """The view trusts the server-filtered response - no client-side re-filter."""
    bv = _import()
    body_text = _read(SAMPLE_JSON)

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        return body_text

    bv.gender_view(handle=42, gender="male", fetch_func=fetch)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    play_urls = [u for u in urls if "mode=playvid" in u]
    # All 5 fixture rooms render (we trust the API response).
    assert len(play_urls) == 5


def test_gender_view_unknown_gender_closes_directory(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    bv = _import()
    bv.gender_view(handle=42, gender="bogus", fetch_func=lambda *a, **k: "")
    kodi_mocks["xbmcplugin"].endOfDirectory.assert_called_once()


# search_view ------------------------------------------------------------- #


def test_search_view_with_query_renders(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    body_text = _read(SAMPLE_JSON)

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        return body_text

    bv.search_view(handle=42, query="sample", fetch_func=fetch)
    urls = _added_urls(kodi_mocks["xbmcplugin"])
    assert any("slug=sample_room_1" in u for u in urls)


def test_search_view_passes_keyword_to_url(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    seen: list[str] = []

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        seen.append(url)
        return EMPTY_JSON

    bv.search_view(handle=42, query="blonde", fetch_func=fetch)
    assert any("keywords=blonde" in u for u in seen)


def test_search_view_empty_query_closes_directory(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    bv = _import()
    bv.search_view(handle=42, query="", fetch_func=lambda *a, **k: "")
    kodi_mocks["xbmcplugin"].endOfDirectory.assert_called_once()


# --------------------------------------------------------------------------- #
# Content-type declaration: every browse view must mark the directory as
# 'videos' so Kodi exposes the InfoWall / MediaList / Wide view modes
# (thumb-on-right, plot-on-left layout). Without this the directory is
# treated as generic 'files' and the view-selector only shows file-shaped
# layouts that cut off the room plot.
# --------------------------------------------------------------------------- #


def _empty_fetch(*_a: Any, **_kw: Any) -> str:
    return EMPTY_JSON


def test_main_menu_sets_content_videos(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    bv.main_menu(handle=42)
    kodi_mocks["xbmcplugin"].setContent.assert_called_once_with(42, "videos")


def test_top_cams_view_sets_content_videos(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    bv.top_cams_view(handle=42, fetch_func=_empty_fetch)
    kodi_mocks["xbmcplugin"].setContent.assert_called_once_with(42, "videos")


def test_new_cams_view_sets_content_videos(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    bv.new_cams_view(handle=42, fetch_func=_empty_fetch)
    kodi_mocks["xbmcplugin"].setContent.assert_called_once_with(42, "videos")


def test_gender_view_sets_content_videos(kodi_mocks: dict[str, MagicMock]) -> None:
    bv = _import()
    bv.gender_view(handle=42, gender="female", fetch_func=_empty_fetch)
    kodi_mocks["xbmcplugin"].setContent.assert_called_once_with(42, "videos")


def test_gender_view_unknown_still_sets_content_videos(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Even on the bogus-gender error path the directory is closed as
    'videos' so the user's view-mode preference doesn't reset to 'files'
    on a stray click.
    """
    bv = _import()
    bv.gender_view(handle=42, gender="bogus", fetch_func=_empty_fetch)
    kodi_mocks["xbmcplugin"].setContent.assert_called_once_with(42, "videos")


def test_search_view_with_query_sets_content_videos(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    bv = _import()
    bv.search_view(handle=42, query="alice", fetch_func=_empty_fetch)
    kodi_mocks["xbmcplugin"].setContent.assert_called_once_with(42, "videos")


def test_search_view_empty_query_still_sets_content_videos(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    bv = _import()
    bv.search_view(handle=42, query="", fetch_func=_empty_fetch)
    kodi_mocks["xbmcplugin"].setContent.assert_called_once_with(42, "videos")


# --------------------------------------------------------------------------- #
# Ctxmenu integration: every model row gets a state-aware right-click menu
# --------------------------------------------------------------------------- #


def test_top_cams_view_attaches_ctxmenu_per_row(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each playable row gets ``addContextMenuItems(...)`` with at least
    one entry. With an empty tv.json + favs.json, the menu reads
    Add to TV + Add to Favorites.
    """
    # Point the addon data dir at tmp_path so the views read the empty
    # tv.json / favs.json.
    fake_xbmcvfs = kodi_mocks["xbmcvfs"]
    fake_xbmcvfs.translatePath = lambda p: str(tmp_path) + "/"

    bv = _import()
    body_text = _read(SAMPLE_JSON)

    captured_ctx: list[Any] = []

    def make_listitem(*args: Any, **kwargs: Any) -> MagicMock:
        li = MagicMock()
        li.label = kwargs.get("label") or (args[0] if args else "")
        li._props: dict[str, str] = {}
        li.setProperty = lambda k, v: li._props.update({k: v})
        li.setArt = lambda art: None
        li.setInfo = lambda *a, **k: None

        def add_ctx(items: Any, replace: bool = False) -> None:
            captured_ctx.append(list(items))

        li.addContextMenuItems = add_ctx
        return li

    kodi_mocks["xbmcgui"].ListItem.side_effect = make_listitem

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        return body_text

    bv.top_cams_view(handle=42, fetch_func=fetch)
    # 5 models in the fixture, each should have got a ctx menu attached.
    assert len(captured_ctx) == 5
    for ctx in captured_ctx:
        labels = [t[0] for t in ctx]
        # With no tv.json membership, default is "Add to TV".
        assert any("Add to TV" in lab for lab in labels)
        # With no favs.json, default is "Add to Favorites".
        assert any("Add to Favorites" in lab for lab in labels)
