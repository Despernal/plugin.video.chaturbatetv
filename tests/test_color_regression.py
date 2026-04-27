"""Lesson 4 regression guard: every emitted ``[COLOR <hex>]`` tag must
use 8-char AARRGGBB hex.

Kodi's ``[COLOR ...]`` tag accepts named colors (``red``, ``deeppink``)
or 8-char hex with full alpha (``FF00d4ff``). Bare 6-char hex
(``00d4ff``) silently renders the wrapped label as BLANK - Kodi parses
the tag but rejects the color and drops the wrapped text. Two
production bugs from this in v0.3.0 and v0.4.0; the 8-char rule is
recorded in ``docs/LESSONS-LEARNED.md`` Lesson 4.

This test walks every label string emitted by every browse / favs /
TV view (via the kodi_helpers mock layer) and asserts that any
``[COLOR <hex>]`` substring uses 8-char hex (or a named color).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from resources.lib.cb_models import Favorite, Gender, TVEntry


# Matches [COLOR <token>] where token is the value (hex or name).
_COLOR_TAG_RE = re.compile(r"\[COLOR\s+([^\]]+)\]")
# 8-char hex marker. Anything that's all hex chars must be exactly 8 long.
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")
# Kodi's named colors (the ones we use); extend if a future view starts
# emitting another).
_KNOWN_NAMED_COLORS = {
    "red", "blue", "green", "yellow", "cyan", "magenta", "white",
    "black", "deeppink", "hotpink", "violet", "orange",
}


def _assert_color_tags_valid(labels: list[str]) -> None:
    """Walk every label, find every [COLOR ...] tag, assert validity."""
    for label in labels:
        for token in _COLOR_TAG_RE.findall(label):
            value = token.strip()
            if value.lower() in _KNOWN_NAMED_COLORS:
                continue
            if _HEX_RE.match(value):
                assert len(value) == 8, (
                    f"Lesson 4 regression: COLOR {value!r} in label "
                    f"{label!r} is {len(value)}-char hex; Kodi requires "
                    f"8-char AARRGGBB or it renders blank"
                )
                continue
            # Anything else (template substitution leftover, etc.) is
            # noise we tolerate.


def _kodi_mocks(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    """Install the minimum Kodi shims for browse/favs views to render."""
    fake_xbmc = MagicMock()
    fake_xbmcgui = MagicMock()
    fake_xbmcplugin = MagicMock()
    fake_xbmcaddon = MagicMock()
    fake_xbmcvfs = MagicMock()
    fake_xbmcvfs.translatePath = lambda p: p

    def make_listitem(*args: Any, **kwargs: Any) -> MagicMock:
        li = MagicMock()
        li._props: dict[str, str] = {}
        li.setProperty = lambda k, v: li._props.update({k: v})
        li.setArt = lambda art: None
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
    sys.modules.pop("resources.lib.favs_views", None)
    sys.modules.pop("resources.lib.addon_actions", None)
    return fake_xbmcgui, fake_xbmcplugin


def _captured_labels(xbmcgui_mock: MagicMock,
                     xbmcplugin_mock: MagicMock) -> list[str]:
    """Pull every label string that was passed to ListItem() or
    addDirectoryItem() during the view render."""
    out: list[str] = []
    for call in xbmcgui_mock.ListItem.call_args_list:
        if "label" in call.kwargs:
            out.append(call.kwargs["label"])
        elif call.args:
            out.append(str(call.args[0]))
    for call in xbmcgui_mock.ListItem.call_args_list:
        # Defensive: catch any positional-only label too.
        if call.args and isinstance(call.args[0], str):
            out.append(call.args[0])
    return out


def test_main_menu_color_tags_all_8_char_hex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, plug = _kodi_mocks(monkeypatch)
    import resources.lib.browse_views as bv
    bv.main_menu(handle=42)
    _assert_color_tags_valid(_captured_labels(gui, plug))


def test_top_cams_view_color_tags_all_8_char_hex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, plug = _kodi_mocks(monkeypatch)
    import resources.lib.browse_views as bv

    body = json.dumps({
        "rooms": [
            {"username": "alice", "gender": "f", "num_users": 100,
             "label": "public"},
            {"username": "bob", "gender": "m", "num_users": 50,
             "label": "public"},
            {"username": "couple1", "gender": "c", "num_users": 30,
             "label": "public"},
            {"username": "trans1", "gender": "t", "num_users": 10,
             "label": "public"},
        ],
        "total_count": 4, "all_rooms_count": 4,
    })

    def fetch(url: str, **_kw: Any) -> str:
        return body

    bv.top_cams_view(handle=42, fetch_func=fetch)
    _assert_color_tags_valid(_captured_labels(gui, plug))


def test_tv_list_color_tags_all_8_char_hex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, plug = _kodi_mocks(monkeypatch)
    from resources.lib import tv_store
    import resources.lib.addon_actions as actions

    tv_path = tmp_path / "tv.json"
    tv_store.save(tv_path, [
        TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=12),
        TVEntry(name="bob", url="https://chaturbate.com/bob/", priority=5),
    ])

    actions.tv_list(handle=42, store_path=tv_path)
    _assert_color_tags_valid(_captured_labels(gui, plug))


def test_favs_render_color_tags_all_8_char_hex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover all four genders so every ``_GENDER_COLORS`` entry gets
    walked. Without this the dict could regress to 6-char and a single
    gender's color tag would silently render blank in Kodi."""
    gui, plug = _kodi_mocks(monkeypatch)
    from resources.lib import favs_store
    import resources.lib.favs_views as fv

    favs_path = tmp_path / "favs.json"
    favs_store.save(favs_path, [
        Favorite(name="alice", slug="alice", url="https://x", gender=Gender.FEMALE),
        Favorite(name="bob", slug="bob", url="https://x", gender=Gender.MALE),
        Favorite(name="couple1", slug="couple1", url="https://x", gender=Gender.COUPLE),
        Favorite(name="trans1", slug="trans1", url="https://x", gender=Gender.TRANS),
        Favorite(name="ghost", slug="ghost", url="https://x", gender=Gender.UNKNOWN),
    ])
    # Render the favs (any helper that triggers _render_favs works).
    fv._render_favs(handle=42, favs=favs_store.load(favs_path))
    _assert_color_tags_valid(_captured_labels(gui, plug))
