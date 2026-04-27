"""Tests for resources.lib.addon_actions - side-effect verbs.

Covers the four verb families: favorites add/remove, playvid resolver
hookup (incl. TV-mode offline auto-skip), and the TV mgmt verbs
(tv_play / tv_stop / tv_list / tv_add / tv_remove / tv_edit).
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


def test_playvid_offline_during_tv_mode_fires_action_next(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lesson v6.1: when TV mode is active and a slot in the playlist
    resolves offline, ``playvid`` must fire ``Action(Next)`` so the
    player advances. Sitting on ``setResolvedUrl(False)`` mid-playlist
    leaves Kodi on a black screen until the user mashes Next themselves.
    """
    state: dict[str, str] = {"chaturbatetv_active": "1"}

    class _Win:
        def __init__(self, _id: int = 0) -> None:
            pass

        def getProperty(self, k: str) -> str:
            return state.get(k, "")

        def setProperty(self, k: str, v: str) -> None:
            state[k] = v

    kodi_mocks["xbmcgui"].Window = _Win

    cap = _patch_resolver_and_xbmcplugin(monkeypatch, success=False)
    actions = _import()

    actions.playvid(handle=42, slug="ghost", name="ghost")

    # Action(Next) was fired - player advances to next TV slot.
    builtins_called = [c.args[0] for c in
                       kodi_mocks["xbmc"].executebuiltin.call_args_list]
    assert "Action(Next)" in builtins_called
    # And setResolvedUrl was NOT called (we early-returned).
    assert cap["resolved"] == []


def test_playvid_offline_without_tv_mode_still_fails_cleanly(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside TV mode, offline rooms must still hand back
    ``setResolvedUrl(False)`` rather than firing Action(Next) (which
    would skip past the user's own click).
    """
    state: dict[str, str] = {"chaturbatetv_active": "0"}

    class _Win:
        def __init__(self, _id: int = 0) -> None:
            pass

        def getProperty(self, k: str) -> str:
            return state.get(k, "")

        def setProperty(self, k: str, v: str) -> None:
            state[k] = v

    kodi_mocks["xbmcgui"].Window = _Win

    cap = _patch_resolver_and_xbmcplugin(monkeypatch, success=False)
    actions = _import()

    actions.playvid(handle=42, slug="ghost", name="ghost")

    builtins_called = [c.args[0] for c in
                       kodi_mocks["xbmc"].executebuiltin.call_args_list]
    assert "Action(Next)" not in builtins_called
    assert len(cap["resolved"]) == 1
    _h, succeeded, _li = cap["resolved"][0]
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
# Phase 5: TV verbs wired to tv_store + tv_loop
# --------------------------------------------------------------------------- #


def test_tv_play_loads_entries_and_invokes_loop(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tv_play loads tv.json and hands off to tv_loop.tv_play with
    the parsed entries."""
    from resources.lib import tv_store
    from resources.lib.cb_models import TVEntry

    tv_path = tmp_path / "tv.json"
    tv_store.save(tv_path, [
        TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=10),
        TVEntry(name="bob", url="https://chaturbate.com/bob/", priority=5),
    ])

    # Patch tv_loop.tv_play to capture the call without spinning a real
    # outer loop.
    captured: dict[str, Any] = {}

    def fake_tv_play(*, entries: Any, is_live_func: Any,
                     poll_minutes: int = 10) -> str:
        captured["entries"] = entries
        captured["poll_minutes"] = poll_minutes
        return "user_stopped"

    import resources.lib.tv_loop as tv_loop_mod
    monkeypatch.setattr(tv_loop_mod, "tv_play", fake_tv_play)

    actions = _import()
    actions.tv_play(handle=42, store_path=tv_path)
    assert "entries" in captured
    assert {e.name for e in captured["entries"]} == {"alice", "bob"}


def test_tv_play_empty_list_notifies_and_returns(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    actions = _import()
    actions.tv_play(handle=42, store_path=tmp_path / "tv.json")
    msgs = [m for _h, m in kodi_mocks["notifications"]]
    assert any("empty" in m.lower() for m in msgs)


def test_restart_kodi_runs_quit_builtin(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """LibreELEC respawns Kodi on Quit, so this is the addon equivalent
    of ``systemctl restart kodi`` (the workaround for stuck audio
    renderer underruns on LL-HLS streams).
    """
    actions = _import()
    actions.restart_kodi(handle=42)
    builtins = [c.args[0] for c in
                kodi_mocks["xbmc"].executebuiltin.call_args_list]
    assert "Quit" in builtins


def test_refresh_artwork_clears_textures_db(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Should DELETE rows from Textures13.db whose url matches the
    addon ID, plus unlink the cached files in Thumbnails/."""
    import sqlite3 as _sqlite
    db_path = tmp_path / "Textures13.db"
    thumbs_dir = tmp_path / "thumbnails"
    thumbs_dir.mkdir()
    (thumbs_dir / "a.png").write_bytes(b"x")
    (thumbs_dir / "b.png").write_bytes(b"y")

    conn = _sqlite.connect(str(db_path))
    conn.execute("CREATE TABLE texture (id INTEGER PRIMARY KEY, url TEXT, cachedurl TEXT)")
    conn.execute(
        "INSERT INTO texture (url, cachedurl) VALUES (?, ?)",
        ("special://home/addons/plugin.video.chaturbatetv/icon.png", "a.png"),
    )
    conn.execute(
        "INSERT INTO texture (url, cachedurl) VALUES (?, ?)",
        ("special://home/addons/plugin.video.chaturbatetv/fanart.jpg", "b.png"),
    )
    conn.execute(
        "INSERT INTO texture (url, cachedurl) VALUES (?, ?)",
        ("https://example.com/some-other.jpg", "c.png"),
    )
    conn.commit()
    conn.close()

    # Redirect xbmcvfs.translatePath at the affected paths.
    fake_xbmcvfs = MagicMock()
    paths = {
        "special://database/Textures13.db": str(db_path),
        "special://thumbnails/": str(thumbs_dir) + "/",
    }
    fake_xbmcvfs.translatePath = lambda p: paths.get(p, p)
    monkeypatch.setitem(sys.modules, "xbmcvfs", fake_xbmcvfs)
    sys.modules.pop("resources.lib.addon_actions", None)

    actions = _import()
    actions.refresh_artwork(handle=42)

    # The two addon textures should be gone from the DB; the unrelated
    # row should remain.
    conn = _sqlite.connect(str(db_path))
    rows = conn.execute("SELECT url FROM texture").fetchall()
    conn.close()
    urls = [r[0] for r in rows]
    assert "https://example.com/some-other.jpg" in urls
    assert all("plugin.video.chaturbatetv" not in u for u in urls)
    # And the cached files were unlinked.
    assert not (thumbs_dir / "a.png").exists()
    assert not (thumbs_dir / "b.png").exists()


def test_make_bulk_is_live_func_uses_affiliate_endpoint(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TV mode's is_live check should hit the single-call affiliate
    endpoint ('s pattern), NOT call is_model_live per slug.
    For a 60-entry TV list this turns 60 sequential AJAX calls per
    poll cycle into 1 affiliate call.
    """
    import json as _json
    fetch_calls: list[str] = []

    def fake_fetch(url: str, **_kw: Any) -> str:
        fetch_calls.append(url)
        return _json.dumps([
            {"username": "alice", "slug": "alice", "gender": "f",
             "num_users": 100, "image_url": "", "room_subject": ""},
            {"username": "bob", "slug": "bob", "gender": "m",
             "num_users": 50, "image_url": "", "room_subject": ""},
        ])

    import resources.lib.cb_client as cb_client_mod
    monkeypatch.setattr(cb_client_mod, "fetch_browse_page",
                        lambda url, **_kw: fake_fetch(url))

    actions = _import()
    is_live = actions._make_bulk_is_live_func(poll_minutes=10)

    # Multiple is_live calls within the TTL share ONE affiliate fetch.
    assert is_live("https://chaturbate.com/alice/") is True
    assert is_live("https://chaturbate.com/bob/") is True
    assert is_live("https://chaturbate.com/ghost/") is False
    assert len(fetch_calls) == 1, (
        f"expected 1 affiliate fetch, got {len(fetch_calls)}: {fetch_calls!r}"
    )
    # And the URL was the affiliate endpoint, not per-slug AJAX.
    assert "/affiliates/api/onlinerooms/" in fetch_calls[0]


def test_make_bulk_is_live_keeps_stale_set_on_network_failure(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient 5xx must NOT silently mark every model offline. Keep
    the stale slug set; the next refresh attempt will retry.
    """
    import json as _json
    state = {"call": 0}

    def fake_fetch(url: str, **_kw: Any) -> str:
        state["call"] += 1
        if state["call"] == 1:
            return _json.dumps([
                {"username": "alice", "slug": "alice", "gender": "f",
                 "num_users": 1, "image_url": "", "room_subject": ""},
            ])
        raise OSError("server hiccup")

    import resources.lib.cb_client as cb_client_mod
    monkeypatch.setattr(cb_client_mod, "fetch_browse_page",
                        lambda url, **_kw: fake_fetch(url))

    actions = _import()
    # poll_minutes=1 -> ttl=30s (clamped floor); first refresh succeeds.
    is_live = actions._make_bulk_is_live_func(poll_minutes=1)
    assert is_live("https://chaturbate.com/alice/") is True

    # Force the cache to look stale, then the next call refreshes - which
    # raises OSError. We should still report alice as live (the stale set).
    import time as _time
    _orig_time = _time.time
    monkeypatch.setattr(_time, "time", lambda: _orig_time() + 10000)
    assert is_live("https://chaturbate.com/alice/") is True


def test_tv_play_reads_poll_minutes_from_settings(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: when tv_play is called without an explicit poll_minutes,
    it must read from addon_settings.poll_minutes() rather than hardcode 10.
    """
    from resources.lib import tv_store
    from resources.lib.cb_models import TVEntry

    tv_path = tmp_path / "tv.json"
    tv_store.save(tv_path, [
        TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=10),
    ])

    captured: dict[str, Any] = {}

    def fake_tv_play(*, entries: Any, is_live_func: Any,
                     poll_minutes: int = 10) -> str:
        captured["poll_minutes"] = poll_minutes
        return "user_stopped"

    import resources.lib.addon_settings as addon_settings_mod
    import resources.lib.tv_loop as tv_loop_mod
    monkeypatch.setattr(tv_loop_mod, "tv_play", fake_tv_play)
    monkeypatch.setattr(addon_settings_mod, "poll_minutes", lambda: 7)

    actions = _import()
    actions.tv_play(handle=42, store_path=tv_path)
    assert captured["poll_minutes"] == 7


def test_tv_stop_clears_active_flag(kodi_mocks: dict[str, MagicMock],
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """tv_stop sets chaturbatetv_active='0' so the running loop exits
    on its next guard check.
    """
    state: dict[str, str] = {"chaturbatetv_active": "1"}

    class _Win:
        def __init__(self, _id: int = 0) -> None:
            pass

        def getProperty(self, k: str) -> str:
            return state.get(k, "")

        def setProperty(self, k: str, v: str) -> None:
            state[k] = v

    kodi_mocks["xbmcgui"].Window = _Win
    actions = _import()
    actions.tv_stop(handle=42)
    assert state["chaturbatetv_active"] == "0"


def _patch_kodi_helpers(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace kodi_helpers with a MagicMock at both attribute layers.

    ``from resources.lib import kodi_helpers`` reads the binding off the
    package object, not sys.modules - so we need to patch BOTH the
    sys.modules entry (for fresh imports) AND the package attribute
    (for already-cached imports).
    """
    import resources.lib as _pkg
    fake = MagicMock()
    monkeypatch.setitem(sys.modules, "resources.lib.kodi_helpers", fake)
    monkeypatch.setattr(_pkg, "kodi_helpers", fake, raising=False)
    sys.modules.pop("resources.lib.addon_actions", None)
    return fake


def test_tv_list_renders_priority_rows(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from resources.lib import tv_store
    from resources.lib.cb_models import TVEntry

    tv_path = tmp_path / "tv.json"
    tv_store.save(tv_path, [
        TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=10),
        TVEntry(name="bob", url="https://chaturbate.com/bob/", priority=5),
    ])

    fake_kh = _patch_kodi_helpers(monkeypatch)

    actions = _import()
    actions.tv_list(handle=42, store_path=tv_path)
    # Header rows + one play row per entry.
    assert fake_kh.add_dir.called
    assert fake_kh.add_play_item.called
    play_calls = fake_kh.add_play_item.call_args_list
    labels = [c.args[1] if len(c.args) > 1 else c.kwargs.get("label", "")
              for c in play_calls]
    assert any("alice" in lab for lab in labels)
    assert any("bob" in lab for lab in labels)
    fake_kh.end_directory.assert_called_once()


def test_tv_list_empty_shows_help_row(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_kh = _patch_kodi_helpers(monkeypatch)

    actions = _import()
    actions.tv_list(handle=42, store_path=tmp_path / "tv.json")
    assert fake_kh.add_dir.called
    label = fake_kh.add_dir.call_args.args[1]
    assert "empty" in label.lower()


def test_tv_add_with_explicit_priority(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    from resources.lib import tv_store

    actions = _import()
    tv_path = tmp_path / "tv.json"
    actions.tv_add(handle=42, slug="alice", name="alice",
                   url="https://chaturbate.com/alice/",
                   priority="11", store_path=tv_path)
    entries = tv_store.load(tv_path)
    assert len(entries) == 1
    assert entries[0].priority == 11
    assert entries[0].name == "alice"


def test_tv_add_idempotent_on_same_url(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    from resources.lib import tv_store

    actions = _import()
    tv_path = tmp_path / "tv.json"
    actions.tv_add(handle=42, slug="alice",
                   url="https://chaturbate.com/alice/",
                   priority="5", store_path=tv_path)
    actions.tv_add(handle=42, slug="alice",
                   url="https://chaturbate.com/alice/",
                   priority="20", store_path=tv_path)
    entries = tv_store.load(tv_path)
    assert len(entries) == 1
    # Priority not changed - duplicate add no-ops.
    assert entries[0].priority == 5


def test_tv_add_clamps_priority_to_1_20(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    from resources.lib import tv_store

    actions = _import()
    tv_path = tmp_path / "tv.json"
    actions.tv_add(handle=42, slug="alice",
                   url="https://chaturbate.com/alice/",
                   priority="999", store_path=tv_path)
    entries = tv_store.load(tv_path)
    assert entries[0].priority == 20


def test_tv_remove_drops_entry(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    from resources.lib import tv_store
    from resources.lib.cb_models import TVEntry

    tv_path = tmp_path / "tv.json"
    tv_store.save(tv_path, [
        TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=10),
        TVEntry(name="bob", url="https://chaturbate.com/bob/", priority=5),
    ])
    actions = _import()
    actions.tv_remove(handle=42, slug="alice",
                      url="https://chaturbate.com/alice/",
                      store_path=tv_path)
    remaining = {e.url for e in tv_store.load(tv_path)}
    assert remaining == {"https://chaturbate.com/bob/"}


def test_tv_edit_changes_priority(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    from resources.lib import tv_store
    from resources.lib.cb_models import TVEntry

    tv_path = tmp_path / "tv.json"
    tv_store.save(tv_path, [
        TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=10),
    ])
    actions = _import()
    actions.tv_edit(handle=42, slug="alice",
                    url="https://chaturbate.com/alice/",
                    priority="15", store_path=tv_path)
    entries = tv_store.load(tv_path)
    assert entries[0].priority == 15


def test_tv_remove_unknown_entry_is_no_op(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    from resources.lib import tv_store
    from resources.lib.cb_models import TVEntry

    tv_path = tmp_path / "tv.json"
    tv_store.save(tv_path, [
        TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=10),
    ])
    actions = _import()
    actions.tv_remove(handle=42, slug="ghost",
                      url="https://chaturbate.com/ghost/",
                      store_path=tv_path)
    # alice still there.
    assert len(tv_store.load(tv_path)) == 1
