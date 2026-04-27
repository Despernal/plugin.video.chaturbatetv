"""Tests for resources.lib.addon_actions - side-effect verbs.

Phase 2 ships fav_add / fav_remove for real, plus stubs for the
TV-mode and playvid verbs (those land in Phase 4-5). The stubs only
need to not crash and to acknowledge they were called.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from resources.lib import favs_store
from resources.lib.cb_models import Favorite, Gender


@pytest.fixture
def kodi_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    fake_xbmc = MagicMock()
    fake_xbmcgui = MagicMock()

    notifications: list[tuple[str, str]] = []

    class _Dialog:
        def notification(self, heading: str, message: str,
                         icon: str = "", time: int = 0,
                         sound: bool = False) -> None:
            notifications.append((heading, message))

        def input(self, *args: Any, **kwargs: Any) -> str:
            return ""

        def ok(self, *args: Any, **kwargs: Any) -> bool:
            return True

    fake_xbmcgui.Dialog = _Dialog
    fake_xbmcgui.NOTIFICATION_INFO = "info"
    fake_xbmc.executebuiltin = MagicMock()

    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)
    monkeypatch.setitem(sys.modules, "xbmcgui", fake_xbmcgui)

    sys.modules.pop("resources.lib.addon_actions", None)

    return {
        "xbmc": fake_xbmc,
        "xbmcgui": fake_xbmcgui,
        "notifications": notifications,  # type: ignore[dict-item]
    }


def _import() -> Any:
    import resources.lib.addon_actions as mod
    return mod


# --------------------------------------------------------------------------- #
# fav_add / fav_remove
# --------------------------------------------------------------------------- #


def test_fav_add_writes_to_store(tmp_path: Path,
                                 kodi_mocks: dict[str, MagicMock]) -> None:
    actions = _import()
    favs_path = tmp_path / "favs.json"

    actions.fav_add(handle=42, slug="alice", name="alice",
                    url="https://chaturbate.com/alice/", gender="female",
                    store_path=favs_path)

    favs = favs_store.load(favs_path)
    assert len(favs) == 1
    assert favs[0].slug == "alice"
    assert favs[0].gender is Gender.FEMALE


def test_fav_add_idempotent(tmp_path: Path,
                            kodi_mocks: dict[str, MagicMock]) -> None:
    actions = _import()
    favs_path = tmp_path / "favs.json"

    actions.fav_add(handle=42, slug="alice", name="alice",
                    url="https://chaturbate.com/alice/", gender="female",
                    store_path=favs_path)
    actions.fav_add(handle=42, slug="alice", name="alice",
                    url="https://chaturbate.com/alice/", gender="female",
                    store_path=favs_path)

    favs = favs_store.load(favs_path)
    assert len(favs) == 1


def test_fav_add_missing_slug_no_op(tmp_path: Path,
                                    kodi_mocks: dict[str, MagicMock]) -> None:
    actions = _import()
    favs_path = tmp_path / "favs.json"

    actions.fav_add(handle=42, store_path=favs_path)

    assert favs_store.load(favs_path) == []


def test_fav_remove_drops_slug(tmp_path: Path,
                               kodi_mocks: dict[str, MagicMock]) -> None:
    actions = _import()
    favs_path = tmp_path / "favs.json"
    favs_store.save(favs_path, [
        Favorite(name="alice", slug="alice", url="https://x", gender=Gender.FEMALE),
        Favorite(name="bob", slug="bob", url="https://x", gender=Gender.MALE),
    ])

    actions.fav_remove(handle=42, slug="alice", store_path=favs_path)

    favs = favs_store.load(favs_path)
    assert {f.slug for f in favs} == {"bob"}


def test_fav_remove_unknown_slug_is_no_op(tmp_path: Path,
                                          kodi_mocks: dict[str, MagicMock]) -> None:
    actions = _import()
    favs_path = tmp_path / "favs.json"
    favs_store.save(favs_path, [
        Favorite(name="alice", slug="alice", url="https://x", gender=Gender.FEMALE),
    ])

    actions.fav_remove(handle=42, slug="ghost", store_path=favs_path)

    favs = favs_store.load(favs_path)
    assert {f.slug for f in favs} == {"alice"}


# --------------------------------------------------------------------------- #
# Phase 4-5 stubs
# --------------------------------------------------------------------------- #


def test_playvid_stub_runs_without_crashing(kodi_mocks: dict[str, MagicMock]) -> None:
    actions = _import()
    actions.playvid(handle=42, slug="alice")
    # Stub should call notification but not crash.
    assert kodi_mocks["notifications"]


def test_tv_play_stub_runs_without_crashing(kodi_mocks: dict[str, MagicMock]) -> None:
    actions = _import()
    actions.tv_play(handle=42)
    assert kodi_mocks["notifications"]


def test_tv_stop_stub_runs_without_crashing(kodi_mocks: dict[str, MagicMock]) -> None:
    actions = _import()
    actions.tv_stop(handle=42)
    assert kodi_mocks["notifications"]


def test_tv_list_stub_runs_without_crashing(kodi_mocks: dict[str, MagicMock]) -> None:
    actions = _import()
    actions.tv_list(handle=42)
    assert kodi_mocks["notifications"]


def test_tv_add_stub_runs_without_crashing(kodi_mocks: dict[str, MagicMock]) -> None:
    actions = _import()
    actions.tv_add(handle=42, slug="alice")
    assert kodi_mocks["notifications"]


def test_tv_remove_stub_runs_without_crashing(kodi_mocks: dict[str, MagicMock]) -> None:
    actions = _import()
    actions.tv_remove(handle=42, slug="alice")
    assert kodi_mocks["notifications"]


def test_tv_edit_stub_runs_without_crashing(kodi_mocks: dict[str, MagicMock]) -> None:
    actions = _import()
    actions.tv_edit(handle=42, slug="alice")
    assert kodi_mocks["notifications"]
