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
# Phase 4b: playvid wired to playvid_resolver + setResolvedUrl
# --------------------------------------------------------------------------- #


def _patch_resolver_and_xbmcplugin(monkeypatch: pytest.MonkeyPatch,
                                    success: bool = True) -> dict[str, Any]:
    """Patch playvid_resolver.resolve_to_listitem and xbmcplugin.setResolvedUrl
    so a playvid call is testable without Kodi or hls_proxy.

    Returns a dict carrying the captured calls.
    """
    import resources.lib.playvid_resolver as pvr

    captured_resolved_url: list[tuple[int, bool, Any]] = []
    captured_resolve_calls: list[dict[str, Any]] = []

    class _FakeListItem:
        def __init__(self, label: str = "") -> None:
            self.label = label
            self._props: dict[str, str] = {}

        def setProperty(self, k: str, v: str) -> None:
            self._props[k] = v

        def getProperty(self, k: str) -> str:
            return self._props.get(k, "")

    fake_li = _FakeListItem(label="alice") if success else None

    class _FakeProxy:
        def stop(self) -> None: ...

    def fake_resolve(slug: str = "", name: str = "",
                     **_kw: Any) -> pvr.PlayvidResult:
        captured_resolve_calls.append({"slug": slug, "name": name})
        if success:
            return pvr.PlayvidResult(success=True, listitem=fake_li,
                                     proxy=_FakeProxy())
        return pvr.PlayvidResult(success=False, listitem=None, proxy=None)

    monkeypatch.setattr(pvr, "resolve_to_listitem", fake_resolve)

    fake_xbmcplugin = MagicMock()

    def set_resolved(handle: int, succeeded: bool, listitem: Any) -> None:
        captured_resolved_url.append((handle, succeeded, listitem))

    fake_xbmcplugin.setResolvedUrl = set_resolved
    monkeypatch.setitem(sys.modules, "xbmcplugin", fake_xbmcplugin)

    return {
        "resolved": captured_resolved_url,
        "resolve_calls": captured_resolve_calls,
        "fake_li": fake_li,
    }


def test_playvid_calls_setResolvedUrl_with_listitem_on_success(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _patch_resolver_and_xbmcplugin(monkeypatch, success=True)
    actions = _import()

    actions.playvid(handle=42, slug="alice", name="alice")

    assert len(cap["resolved"]) == 1
    handle, succeeded, listitem = cap["resolved"][0]
    assert handle == 42
    assert succeeded is True
    assert listitem is cap["fake_li"]


def test_playvid_passes_slug_and_name_to_resolver(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _patch_resolver_and_xbmcplugin(monkeypatch, success=True)
    actions = _import()

    actions.playvid(handle=42, slug="alice", name="Alice the Cam Star")

    assert cap["resolve_calls"] == [{"slug": "alice", "name": "Alice the Cam Star"}]


def test_playvid_calls_setResolvedUrl_with_failure_when_offline(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline room -> setResolvedUrl(handle, False, ...) so Kodi
    cleans up cleanly instead of hanging on a missing item.
    """
    cap = _patch_resolver_and_xbmcplugin(monkeypatch, success=False)
    actions = _import()

    actions.playvid(handle=42, slug="ghost", name="ghost")

    assert len(cap["resolved"]) == 1
    handle, succeeded, _li = cap["resolved"][0]
    assert handle == 42
    assert succeeded is False


def test_playvid_with_no_slug_does_not_call_resolver(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _patch_resolver_and_xbmcplugin(monkeypatch, success=True)
    actions = _import()

    actions.playvid(handle=42)  # no slug

    assert cap["resolve_calls"] == []
    assert cap["resolved"] == []
    assert kodi_mocks["notifications"]  # told the user


def test_playvid_notifies_user_when_offline(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline -> setResolvedUrl(False, ...) AND a user-visible toast.
    The toast is what tells the user 'they're offline', not silence.
    """
    _patch_resolver_and_xbmcplugin(monkeypatch, success=False)
    actions = _import()

    actions.playvid(handle=42, slug="ghost", name="ghost")

    msgs = [m for _h, m in kodi_mocks["notifications"]]
    assert any("ghost" in m for m in msgs)


def test_playvid_falls_back_to_slug_when_name_missing(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some browse rows pass slug only (no separate display name); we
    should still hand a non-empty name through to the resolver."""
    cap = _patch_resolver_and_xbmcplugin(monkeypatch, success=True)
    actions = _import()

    actions.playvid(handle=42, slug="alice")  # no name

    assert cap["resolve_calls"] == [{"slug": "alice", "name": "alice"}]


def test_playvid_does_not_set_legacy_inputstreamaddon_key(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: addon_actions.playvid must not somehow fall
    through to a legacy code path that sets the old 'inputstreamaddon'
    key. We assert the resolver-built ListItem reaches setResolvedUrl
    with the Matrix+ key, never the legacy one.

    This is the single most expensive bug to chase on  (Kodi
    silently fails to invoke ISA), so we pin it at the action layer
    too, not just at the resolver layer.
    """
    import resources.lib.playvid_resolver as pvr

    captured_resolved: list[tuple[int, bool, Any]] = []

    class _RealishLI:
        def __init__(self) -> None:
            self._props: dict[str, str] = {}

        def setProperty(self, k: str, v: str) -> None:
            self._props[k] = v

        def getProperty(self, k: str) -> str:
            return self._props.get(k, "")

    li = _RealishLI()
    li.setProperty("inputstream", "inputstream.adaptive")  # what resolver sets

    def fake_resolve(**_kw: Any) -> pvr.PlayvidResult:
        return pvr.PlayvidResult(success=True, listitem=li, proxy=object())

    monkeypatch.setattr(pvr, "resolve_to_listitem", fake_resolve)

    fake_xbmcplugin = MagicMock()
    fake_xbmcplugin.setResolvedUrl = lambda h, ok, lit: captured_resolved.append(
        (h, ok, lit))
    monkeypatch.setitem(sys.modules, "xbmcplugin", fake_xbmcplugin)

    actions = _import()
    actions.playvid(handle=42, slug="alice", name="alice")

    assert len(captured_resolved) == 1
    _h, ok, passed_li = captured_resolved[0]
    assert ok is True
    assert passed_li.getProperty("inputstream") == "inputstream.adaptive"
    # The legacy key MUST NOT have been silently set on the way through.
    assert passed_li.getProperty("inputstreamaddon") == ""


# --------------------------------------------------------------------------- #
# Phase 5 stubs - TV verbs still placeholders
# --------------------------------------------------------------------------- #


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
