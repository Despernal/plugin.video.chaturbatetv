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
    # v0.7.23: addon_actions._close_directory_handle calls
    # xbmcplugin.endOfDirectory to clear Kodi's busy spinner. Tests
    # need to observe the call so they can pin the spinner-fix
    # behavior.
    fake_xbmcplugin = MagicMock()

    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)
    monkeypatch.setitem(sys.modules, "xbmcgui", fake_xbmcgui)
    monkeypatch.setitem(sys.modules, "xbmcplugin", fake_xbmcplugin)

    sys.modules.pop("resources.lib.addon_actions", None)

    return {
        "xbmc": fake_xbmc,
        "xbmcgui": fake_xbmcgui,
        "xbmcplugin": fake_xbmcplugin,
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


def test_fav_add_concurrent_processes_dont_lose_writes(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """v0.7.37 (race audit pass 1, agent 3 HIGH #1): two sibling Kodi
    processes invoking fav_add on different slugs in rapid succession
    used to silent-clobber each other -- the second writer's
    os.replace wholesale-overwrote the first's append. Now both
    mutations run inside a fcntl.flock critical section so the load+
    save sequence is atomic across processes.

    The test simulates the cross-process race by spawning two threads
    (in real production these would be two processes; threads share
    the flock semantics on the same file for testing purposes).
    """
    import threading
    actions = _import()
    favs_path = tmp_path / "favs.json"

    def add_alice() -> None:
        actions.fav_add(handle=42, slug="alice", name="alice",
                        url="https://chaturbate.com/alice/",
                        gender="female", store_path=favs_path)

    def add_bob() -> None:
        actions.fav_add(handle=42, slug="bob", name="bob",
                        url="https://chaturbate.com/bob/",
                        gender="male", store_path=favs_path)

    t_a = threading.Thread(target=add_alice)
    t_b = threading.Thread(target=add_bob)
    t_a.start()
    t_b.start()
    t_a.join(timeout=3.0)
    t_b.join(timeout=3.0)

    favs = favs_store.load(favs_path)
    slugs = {f.slug for f in favs}
    assert slugs == {"alice", "bob"}, (
        f"both concurrent fav_add calls must land; got {slugs!r}"
    )


def test_tv_add_concurrent_processes_dont_lose_writes(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """v0.7.37 (race audit pass 1, agent 3 HIGH #2): same flock
    protection for tv.json. Two sibling tv_add invocations don't
    clobber each other anymore."""
    import threading
    from resources.lib import tv_store as tv_store_mod
    actions = _import()
    tv_path = tmp_path / "tv.json"

    def add_a() -> None:
        actions.tv_add(handle=42, slug="alice", name="alice",
                       url="https://chaturbate.com/alice/",
                       priority="10", store_path=tv_path)

    def add_b() -> None:
        actions.tv_add(handle=42, slug="bob", name="bob",
                       url="https://chaturbate.com/bob/",
                       priority="15", store_path=tv_path)

    t_a = threading.Thread(target=add_a)
    t_b = threading.Thread(target=add_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=3.0)
    t_b.join(timeout=3.0)

    entries = tv_store_mod.load(tv_path)
    urls = {e.url for e in entries}
    assert urls == {
        "https://chaturbate.com/alice/",
        "https://chaturbate.com/bob/",
    }, f"both concurrent tv_add calls must land; got {urls!r}"


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


def test_playvid_offline_during_tv_mode_serves_silent_stub(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.15 silent-stub fix.

    History:
    - v6.1: when TV is active and the played slug resolves offline,
      we used to fire ``Action(Next)`` and return without calling
      setResolvedUrl. Kodi waited 30s, then displayed "one or more
      items failed to play" with a sad-face dialog.
    - v0.7.14: added ``setResolvedUrl(handle, False, _empty_listitem())``
      before the return to short-circuit the 30s wait. Faster failure,
      same dialog -- (False, ...) still triggers the dialog when the
      playlist has nothing else to fall back on (solo-slug tier).
    - v0.7.15: switch to ``setResolvedUrl(handle, True, silent_stub)``
      pointing at a bundled 1-second silent .mp4. Kodi plays it, hits
      natural end, fires onPlayBackEnded, the TV loop iterates -- no
      dialog ever appears. PlayerControl(Next) is no longer needed
      because the natural end-of-stub advances the playlist on its
      own.

    The intentional Yes/No exit dialog lives in
    ``tv_loop._classify_after_stop`` and is unaffected.
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
    # ListItem stub that captures the path passed in so we can assert
    # it points at the silent stub asset.
    captured_paths: list[str] = []

    class _ListItem:
        def __init__(self, label: str = "", path: str = "") -> None:
            self._path = path
            captured_paths.append(path)

        def getPath(self) -> str:
            return self._path

    kodi_mocks["xbmcgui"].ListItem = _ListItem

    cap = _patch_resolver_and_xbmcplugin(monkeypatch, success=False)
    actions = _import()

    actions.playvid(handle=42, slug="ghost", name="ghost")

    # setResolvedUrl was called exactly once with succeeded=True so Kodi
    # plays the silent stub and naturally advances rather than firing
    # the "one or more items failed to play" dialog.
    assert len(cap["resolved"]) == 1, (
        f"playvid must call setResolvedUrl exactly once "
        f"(got {len(cap['resolved'])} calls)"
    )
    handle, succeeded, _li = cap["resolved"][0]
    assert handle == 42
    assert succeeded is True, (
        "v0.7.15: must resolve TRUE (silent stub) so Kodi never shows "
        "the failure dialog. (False) triggers the dialog AND blocks the "
        "addon thread behind the modal until dismissed."
    )
    # The listitem must point at the bundled silent stub.
    stub_path_used = next(
        (p for p in captured_paths if p.endswith("silent.mp4")), ""
    )
    assert stub_path_used.endswith(
        "resources/media/silent.mp4"
    ), (
        f"silent stub listitem must carry the resources/media/silent.mp4 "
        f"path; got captured paths {captured_paths!r}"
    )
    # PlayerControl(Next) is no longer fired -- silent stub's natural
    # end advances the playlist on its own.
    builtins_called = [c.args[0] for c in
                       kodi_mocks["xbmc"].executebuiltin.call_args_list]
    assert "PlayerControl(Next)" not in builtins_called, (
        "v0.7.15: PlayerControl(Next) is no longer needed; the silent "
        "stub's natural end-of-playback advances the playlist."
    )


def test_playvid_offline_without_tv_mode_still_fails_cleanly(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside TV mode, offline rooms must still hand back
    ``setResolvedUrl(False)`` rather than firing PlayerControl(Next) (which
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
    assert "PlayerControl(Next)" not in builtins_called
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

    This is the single most expensive bug to chase in production (Kodi
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
                     poll_minutes: int = 10,
                     entries_path: Any = None) -> str:
        captured["entries"] = entries
        captured["poll_minutes"] = poll_minutes
        captured["entries_path"] = entries_path
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


def test_refresh_offline_meta_spawns_thread_with_start_and_done_notifications(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """v0.7.20: clicking the 'Refresh offline model info' menu entry
    must (1) toast 'Refreshing model info...' immediately so the
    user sees feedback, (2) spawn a background thread that calls
    _tv_bulk_refresh, (3) toast a 'done' notification when the
    background work returns.

    Tested via dependency injection on refresh_func / notify_func /
    spawn_func so we don't actually fire a network call or a real
    thread.
    """
    actions = _import()
    notifies: list[tuple[str, str]] = []
    spawn_calls: list[Any] = []

    def fake_notify(heading: str, msg: str) -> None:
        notifies.append((heading, msg))

    def fake_refresh() -> bool:
        return True

    def fake_spawn(target: Any) -> None:
        # Run target inline so we can observe the done notification.
        spawn_calls.append(target)
        target()

    actions.refresh_offline_meta(
        handle=42,
        refresh_func=fake_refresh,
        notify_func=fake_notify,
        spawn_func=fake_spawn,
    )

    assert len(spawn_calls) == 1, "must spawn exactly one bg worker"
    headings = [n[0] for n in notifies]
    msgs = [n[1] for n in notifies]
    assert all(h == "Chaturbate TV" for h in headings)
    assert any("Refreshing" in m for m in msgs), (
        f"missing 'Refreshing' start notification in {msgs!r}"
    )
    assert any(("done" in m.lower() or "refreshed" in m.lower()) for m in msgs), (
        f"missing 'done' finish notification in {msgs!r}"
    )


def test_refresh_offline_meta_failure_notifies_user(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """If _tv_bulk_refresh returns False (network failure), the user
    sees a 'failed' notification rather than a misleading 'done'
    one. We don't bubble the exception."""
    actions = _import()
    notifies: list[tuple[str, str]] = []

    def fake_notify(heading: str, msg: str) -> None:
        notifies.append((heading, msg))

    def fake_refresh_failing() -> bool:
        return False

    def fake_spawn(target: Any) -> None:
        target()

    actions.refresh_offline_meta(
        handle=42,
        refresh_func=fake_refresh_failing,
        notify_func=fake_notify,
        spawn_func=fake_spawn,
    )

    msgs = [n[1] for n in notifies]
    assert any("fail" in m.lower() for m in msgs), (
        f"failure notification missing in {msgs!r}"
    )


def test_refresh_offline_meta_skipped_when_another_refresh_in_flight(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """v0.7.20: a non-blocking module-level lock guards
    _tv_bulk_refresh. If a refresh is already in flight (auto poll
    or a previous manual click still running), a manual click toasts
    "already in progress" and exits cleanly without firing a duplicate
    fetch.

    Tested by simulating the lock being held: refresh_func is invoked
    only when the lock is free, so we capture the lock and watch the
    handler skip the call.
    """
    actions = _import()
    notifies: list[tuple[str, str]] = []
    refresh_calls: list[int] = []

    def fake_notify(heading: str, msg: str) -> None:
        notifies.append((heading, msg))

    def fake_refresh() -> bool:
        refresh_calls.append(1)
        return True

    def fake_spawn(target: Any) -> None:
        target()

    # Acquire the production lock to simulate "already in flight".
    actions._BULK_REFRESH_LOCK.acquire()
    try:
        actions.refresh_offline_meta(
            handle=42,
            refresh_func=fake_refresh,
            notify_func=fake_notify,
            spawn_func=fake_spawn,
        )
    finally:
        actions._BULK_REFRESH_LOCK.release()

    # No fetch was attempted while the lock was held.
    assert refresh_calls == [], (
        f"manual refresh fired despite lock held: {refresh_calls!r}"
    )
    msgs = [n[1] for n in notifies]
    assert any("already" in m.lower() or "in progress" in m.lower() for m in msgs), (
        f"expected an 'already refreshing' message, got {msgs!r}"
    )


def test_tv_bulk_refresh_returns_false_when_lock_held(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """The lock acquire is non-blocking. If a fetch is already in
    flight, _tv_bulk_refresh returns False without making a network
    call -- callers (TV loop, favs view) treat False the same as a
    stale cache and continue.
    """
    actions = _import()
    actions._BULK_REFRESH_LOCK.acquire()
    try:
        result = actions._tv_bulk_refresh()
    finally:
        actions._BULK_REFRESH_LOCK.release()
    assert result is False


def test_tv_bulk_refresh_releases_lock_after_completion(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock must be released on both success and failure paths so
    a transient network 5xx doesn't permanently lock out future
    refreshes."""
    actions = _import()

    # Stub the fetch to raise OSError (the path that returns False).
    def boom(*a: Any, **kw: Any) -> str:
        raise OSError("network down")

    import resources.lib.cb_client as cb_client_mod
    monkeypatch.setattr(cb_client_mod, "fetch_browse_page", boom)

    actions._tv_bulk_refresh()  # returns False on OSError
    assert not actions._BULK_REFRESH_LOCK.locked(), (
        "lock must be released even when fetch fails"
    )


def test_tv_bulk_refresh_filters_non_public_from_cache(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """v0.7.31: hidden / private / paid-show rooms appear in the
    affiliate-onlinerooms feed but the AJAX endpoint refuses HLS for
    them, so the TV loop chases them forever (silent-stub loop). The
    bulk cache must filter ``current_show != 'public'`` out of the
    pickable slug set. Non-public rooms STILL get persisted to the
    meta DB so the favs view sees their thumb / status info.
    """
    import json as _json
    actions = _import()

    rooms = [
        {"username": "alice", "current_show": "public", "num_users": 50,
         "gender": "f", "image_url": "", "room_subject": ""},
        {"username": "model_a", "current_show": "hidden",
         "num_users": 546, "gender": "f", "image_url": "",
         "room_subject": "550 tkns full show"},
        {"username": "bob", "current_show": "private", "num_users": 1,
         "gender": "m", "image_url": "", "room_subject": ""},
    ]

    def fake_fetch(url: str, *a: Any, **kw: Any) -> str:
        return _json.dumps(rooms)

    import resources.lib.cb_client as cb_client_mod
    monkeypatch.setattr(cb_client_mod, "fetch_browse_page", fake_fetch)

    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: tmp_path / "meta.db")

    actions._TV_BULK_CACHE["slugs"] = frozenset()
    ok = actions._tv_bulk_refresh()
    assert ok is True

    # Only the public room makes the cache.
    assert actions._TV_BULK_CACHE["slugs"] == frozenset({"alice"})

    # But the meta DB sees ALL three -- offline/favs view still has
    # their thumb, location, viewer count to render.
    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(str(tmp_path / "meta.db"))
    try:
        for slug in ("alice", "model_a", "bob"):
            row = mms_real.get_model(conn, slug)
            assert row is not None, (
                f"meta-store should still have all broadcasting "
                f"rooms persisted, missing {slug!r}"
            )
    finally:
        conn.close()


def test_tv_bulk_mark_offline_records_in_session_set(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """v0.7.33: marking a slug offline must record it in
    _OFFLINE_SESSION_SLUGS so the next bulk-cache refresh subtracts
    it instead of re-adding the just-marked slug from the affiliate
    feed. Idempotent across calls.

    v0.7.45: storage is now a ``dict[str, float]`` mapping slug to
    blocked-at epoch (was set[str]); membership semantics preserved
    via ``in`` / ``.keys()``."""
    actions = _import()
    actions._OFFLINE_SESSION_SLUGS.clear()
    actions._TV_BULK_CACHE["slugs"] = frozenset({"alice", "bob"})

    actions._tv_bulk_mark_offline("alice")
    assert "alice" in actions._OFFLINE_SESSION_SLUGS
    assert actions._TV_BULK_CACHE["slugs"] == frozenset({"bob"})

    # Idempotent: marking again is fine, dict stays consistent.
    actions._tv_bulk_mark_offline("alice")
    assert set(actions._OFFLINE_SESSION_SLUGS.keys()) == {"alice"}

    # A slug not in the cache still gets recorded so a future bulk
    # refresh that re-introduces it gets blocked.
    actions._tv_bulk_mark_offline("ghost")
    assert "ghost" in actions._OFFLINE_SESSION_SLUGS


# --- v0.7.59: a network-wide fetch failure must NOT poison the blocklist ----- #
# Root cause of the 2026-06-17 wedge: egress IP got 403-blocked, so EVERY resolve
# fell to the safe-default 'offline' and every model got _tv_bulk_mark_offline'd.
# The v0.7.45 TTL prune never fired because each cycle RE-STAMPED the slug, so
# recovery was impossible without a Kodi restart. Fix: record per-slug whether
# the status fetch actually succeeded (_note_status_fetch), and have
# _tv_bulk_mark_offline skip the poison while that slug's last fetch is a failure.
def test_mark_offline_skips_poison_when_status_fetch_failed(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    actions = _import()
    actions._OFFLINE_SESSION_SLUGS.clear()
    actions._FETCH_FAILED_SLUGS.clear()
    actions._TV_BULK_CACHE["slugs"] = frozenset({"alice", "bob"})

    # The 403-storm case: alice's status fetch FAILED (status unknown).
    actions._note_status_fetch("alice", status_known=False)
    actions._tv_bulk_mark_offline("alice")

    # No poison: not blocklisted, not evicted from the live cache.
    assert "alice" not in actions._OFFLINE_SESSION_SLUGS
    assert actions._TV_BULK_CACHE["slugs"] == frozenset({"alice", "bob"})


def test_mark_offline_proceeds_on_confirmed_offline(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    actions = _import()
    actions._OFFLINE_SESSION_SLUGS.clear()
    actions._FETCH_FAILED_SLUGS.clear()
    actions._TV_BULK_CACHE["slugs"] = frozenset({"alice", "bob"})

    # A clean 200 saying alice signed off: status KNOWN -> mark as before.
    actions._note_status_fetch("alice", status_known=True)
    actions._tv_bulk_mark_offline("alice")

    assert "alice" in actions._OFFLINE_SESSION_SLUGS
    assert actions._TV_BULK_CACHE["slugs"] == frozenset({"bob"})


def test_mark_offline_proceeds_with_no_fetch_record(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Backward compat: a caller that never recorded a fetch result (e.g. the
    stall watchdog, where the model WAS live) marks offline exactly as before."""
    actions = _import()
    actions._OFFLINE_SESSION_SLUGS.clear()
    actions._FETCH_FAILED_SLUGS.clear()
    actions._TV_BULK_CACHE["slugs"] = frozenset({"alice", "bob"})

    actions._tv_bulk_mark_offline("alice")

    assert "alice" in actions._OFFLINE_SESSION_SLUGS
    assert actions._TV_BULK_CACHE["slugs"] == frozenset({"bob"})


def test_note_status_fetch_success_clears_prior_failure(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """When connectivity returns and a slug resolves cleanly again, the
    network-fail flag clears so a later GENUINE offline can still mark."""
    actions = _import()
    actions._OFFLINE_SESSION_SLUGS.clear()
    actions._FETCH_FAILED_SLUGS.clear()
    actions._TV_BULK_CACHE["slugs"] = frozenset({"alice", "bob"})

    actions._note_status_fetch("alice", status_known=False)   # outage
    actions._note_status_fetch("alice", status_known=True)    # recovered
    actions._tv_bulk_mark_offline("alice")                    # now genuinely off

    assert "alice" in actions._OFFLINE_SESSION_SLUGS
    assert "alice" not in actions._FETCH_FAILED_SLUGS


def test_mark_offline_poisons_again_after_fetch_fail_ttl_expires(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """A stale network-fail flag (older than the TTL) must not suppress a real
    mark-offline forever -- otherwise a never-re-fetched slug stays unmarkable."""
    import time as _t
    actions = _import()
    actions._OFFLINE_SESSION_SLUGS.clear()
    actions._FETCH_FAILED_SLUGS.clear()
    actions._TV_BULK_CACHE["slugs"] = frozenset({"alice", "bob"})

    actions._FETCH_FAILED_SLUGS["alice"] = _t.time() - (actions._FETCH_FAIL_TTL_SEC + 10)
    actions._tv_bulk_mark_offline("alice")

    assert "alice" in actions._OFFLINE_SESSION_SLUGS
    assert actions._TV_BULK_CACHE["slugs"] == frozenset({"bob"})


def test_tv_stop_clears_fetch_failed_slugs(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """tv_stop resets the per-session network-fail record alongside the
    offline blocklist, so a new session starts with a clean slate."""
    import time as _t
    actions = _import()
    actions._FETCH_FAILED_SLUGS["alice"] = _t.time()
    actions.tv_stop(handle=42)
    assert actions._FETCH_FAILED_SLUGS == {}


def test_tv_bulk_refresh_subtracts_session_offline_slugs(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """v0.7.33: the bulk refresh wholesale-overwrites _TV_BULK_CACHE
    from the affiliate feed. Without subtracting _OFFLINE_SESSION_SLUGS,
    a slug we just marked offline gets re-added every TTL window and
    the TV loop chases it again (audit agent 2 HIGH #2). The fix
    subtracts the session set after building the public-only set."""
    import json as _json
    actions = _import()
    actions._OFFLINE_SESSION_SLUGS.clear()
    # v0.7.45: dict shape -- value is blocked-at epoch.
    import time as _t
    actions._OFFLINE_SESSION_SLUGS["model_a"] = _t.time()

    rooms = [
        {"username": "alice", "current_show": "public", "num_users": 1,
         "gender": "f", "image_url": "", "room_subject": ""},
        {"username": "model_a", "current_show": "public",
         "num_users": 100, "gender": "f", "image_url": "",
         "room_subject": ""},
    ]

    def fake_fetch(url: str, *a: Any, **kw: Any) -> str:
        return _json.dumps(rooms)

    import resources.lib.cb_client as cb_client_mod
    monkeypatch.setattr(cb_client_mod, "fetch_browse_page", fake_fetch)
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: tmp_path / "meta.db")

    actions._TV_BULK_CACHE["slugs"] = frozenset()
    ok = actions._tv_bulk_refresh()
    assert ok is True
    # model_a was filtered out even though the affiliate feed
    # reported her current_show='public'.
    assert actions._TV_BULK_CACHE["slugs"] == frozenset({"alice"}), (
        f"session-blocked slug must NOT come back via bulk refresh, "
        f"got {actions._TV_BULK_CACHE['slugs']!r}"
    )

    # Cleanup so other tests don't see the lingering block.
    actions._OFFLINE_SESSION_SLUGS.clear()


def test_tv_bulk_refresh_unblocks_slug_after_ttl_expires(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """v0.7.45 regression: model_a real-world repro. TV mode tried
    her at 17:05 while she was in private show; AJAX returned
    is_live=False; silent stub fired; ``_tv_bulk_mark_offline`` added
    her to _OFFLINE_SESSION_SLUGS. She returned to public broadcast
    around 19:30 but every bulk_refresh kept logging "session-blocked
    1" -- her -- while a lower-priority model played at P12. Pre-fix
    the blocklist was session-long. Fix: TTL-bound each entry; entries
    older than ``_OFFLINE_BLOCK_TTL_SEC`` are pruned at the top of
    each refresh, so a recovered model gets re-added to the cache.
    """
    import json as _json
    import time as _t
    actions = _import()
    actions._OFFLINE_SESSION_SLUGS.clear()
    # Simulate "marked offline 16 minutes ago" -- past the 15min TTL.
    actions._OFFLINE_SESSION_SLUGS["model_a"] = (
        _t.time() - actions._OFFLINE_BLOCK_TTL_SEC - 60
    )

    rooms = [
        {"username": "model_a", "current_show": "public",
         "num_users": 100, "gender": "f", "image_url": "",
         "room_subject": ""},
        {"username": "alice", "current_show": "public", "num_users": 1,
         "gender": "f", "image_url": "", "room_subject": ""},
    ]

    def fake_fetch(url: str, *a: Any, **kw: Any) -> str:
        return _json.dumps(rooms)

    import resources.lib.cb_client as cb_client_mod
    monkeypatch.setattr(cb_client_mod, "fetch_browse_page", fake_fetch)
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: tmp_path / "meta.db")

    actions._TV_BULK_CACHE["slugs"] = frozenset()
    ok = actions._tv_bulk_refresh()

    assert ok is True
    # model_a's expired blocklist entry was pruned, AND she's now
    # in the cache because the affiliate feed had her current_show=public.
    assert "model_a" in actions._TV_BULK_CACHE["slugs"], (
        f"after TTL expiry, recovered model must be re-added; "
        f"got {actions._TV_BULK_CACHE['slugs']!r}"
    )
    assert "model_a" not in actions._OFFLINE_SESSION_SLUGS, (
        "expired entry should have been pruned from the blocklist"
    )

    actions._OFFLINE_SESSION_SLUGS.clear()


def test_tv_bulk_refresh_keeps_slug_blocked_within_ttl(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """v0.7.45 sibling: a slug recently marked offline (within the
    TTL window) MUST still be filtered out. This preserves the v0.7.33
    anti-thrash behavior: a model whose affiliate-feed status flips
    quickly between public and non-public shouldn't get re-picked
    every TTL window. Only after ``_OFFLINE_BLOCK_TTL_SEC`` does the
    blocklist release her.
    """
    import json as _json
    import time as _t
    actions = _import()
    actions._OFFLINE_SESSION_SLUGS.clear()
    # Marked 60 seconds ago -- well within the 15min TTL.
    actions._OFFLINE_SESSION_SLUGS["alice"] = _t.time() - 60

    rooms = [
        {"username": "alice", "current_show": "public", "num_users": 1,
         "gender": "f", "image_url": "", "room_subject": ""},
    ]

    def fake_fetch(url: str, *a: Any, **kw: Any) -> str:
        return _json.dumps(rooms)

    import resources.lib.cb_client as cb_client_mod
    monkeypatch.setattr(cb_client_mod, "fetch_browse_page", fake_fetch)
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: tmp_path / "meta.db")

    actions._TV_BULK_CACHE["slugs"] = frozenset()
    ok = actions._tv_bulk_refresh()

    assert ok is True
    # alice still in blocklist + still filtered from cache.
    assert "alice" not in actions._TV_BULK_CACHE["slugs"], (
        "within TTL, blocklisted slug must NOT come back"
    )
    assert "alice" in actions._OFFLINE_SESSION_SLUGS, (
        "within TTL, blocklist entry must persist"
    )

    actions._OFFLINE_SESSION_SLUGS.clear()


def test_tv_stop_clears_offline_session_slugs(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """v0.7.33: tv_stop must reset _OFFLINE_SESSION_SLUGS so a slug
    blocked from a prior session can be reconsidered in a new one
    (a model who was hidden an hour ago might be public now).

    v0.7.45: storage is dict[str, float] (slug -> blocked-at)."""
    import time as _t
    actions = _import()
    actions._OFFLINE_SESSION_SLUGS["alice"] = _t.time()
    actions._OFFLINE_SESSION_SLUGS["bob"] = _t.time()
    actions.tv_stop(handle=42)
    assert actions._OFFLINE_SESSION_SLUGS == {}


def test_refresh_offline_meta_swallows_refresh_exception(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """A raised exception inside the background refresh must not
    propagate out of the bg worker -- daemon thread crashes are
    silent and we'd lose the 'failure' notification too. Instead the
    handler catches and notifies."""
    actions = _import()
    notifies: list[tuple[str, str]] = []

    def fake_notify(heading: str, msg: str) -> None:
        notifies.append((heading, msg))

    def fake_refresh_raising() -> bool:
        raise OSError("boom")

    def fake_spawn(target: Any) -> None:
        target()  # would raise if uncaught -> test would fail

    # Should not raise.
    actions.refresh_offline_meta(
        handle=42,
        refresh_func=fake_refresh_raising,
        notify_func=fake_notify,
        spawn_func=fake_spawn,
    )

    msgs = [n[1] for n in notifies]
    assert any("fail" in m.lower() for m in msgs)


def test_deep_refresh_offline_meta_iterates_offline_favs(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.22 deep crawl: per-slug status check for every fav that
    isn't currently in the bulk-online cache. Each iteration calls
    fetch_room_status_json + head_thumb (rate-limited at 1/sec via
    sleep_func DI) and writes via model_meta_store.upsert_status.

    Verifies: only OFFLINE favs are iterated (online ones already
    have fresh data); rate-limit sleep is invoked between iterations;
    a final summary toast shows the count by status.
    """
    actions = _import()

    # Stand up a tmp DB for the test.
    db_path = str(tmp_path / "model_meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    notifies: list[tuple[str, str]] = []
    bios_fetched: list[str] = []
    statuses_fetched: list[str] = []
    thumbs_fetched: list[str] = []
    sleeps: list[float] = []
    spawn_calls: list[Any] = []

    def fake_notify(heading: str, msg: str) -> None:
        notifies.append((heading, msg))

    def fake_biocontext(slug: str) -> dict[str, Any]:
        bios_fetched.append(slug)
        # Empty dict forces fallback to status+thumb path -- exercises
        # the network-failure / account-gone branch this test covers.
        return {}

    def fake_status(slug: str, fetch_func: Any = None) -> dict[str, Any]:
        statuses_fetched.append(slug)
        # Return banned for "ghost", offline for the others.
        if slug == "ghost":
            return {"success": True, "room_status": "banned", "url": "",
                    "hidden_message": "", "cmaf_edge": False}
        return {"success": True, "room_status": "offline", "url": "",
                "hidden_message": "", "cmaf_edge": False}

    def fake_head(slug: str) -> int:
        thumbs_fetched.append(slug)
        return 404 if slug == "ghost" else 200

    def fake_sleep(secs: float) -> None:
        sleeps.append(secs)

    def fake_spawn(target: Any) -> None:
        spawn_calls.append(target)
        target()

    fav_slugs = ["alice", "bob", "ghost"]
    online_slugs = frozenset({"alice"})  # alice is currently online -> skip

    actions.deep_refresh_offline_meta(
        handle=42,
        fav_slugs=fav_slugs,
        online_slugs=online_slugs,
        fetch_biocontext_func=fake_biocontext,
        fetch_status_func=fake_status,
        head_thumb_func=fake_head,
        notify_func=fake_notify,
        spawn_func=fake_spawn,
        sleep_func=fake_sleep,
    )

    # Spawned exactly once.
    assert len(spawn_calls) == 1

    # Only offline slugs were probed; alice (online) skipped.
    assert "alice" not in bios_fetched
    assert "alice" not in statuses_fetched
    # Biocontext is the primary source -- always tried first.
    assert set(bios_fetched) == {"bob", "ghost"}
    # When biocontext returns empty (network fail / account gone)
    # the deep crawl falls back to the cheap status + HEAD path.
    assert set(statuses_fetched) == {"bob", "ghost"}
    assert set(thumbs_fetched) == {"bob", "ghost"}

    # Rate-limit sleep was applied between iterations.
    assert len(sleeps) >= 1, f"expected at least one sleep, got {sleeps!r}"
    assert all(s > 0 for s in sleeps), f"sleep durations must be positive: {sleeps!r}"

    # The DB was actually written -- ghost should have status='banned',
    # bob should have status='offline'.
    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        ghost = mms_real.get_model(conn, "ghost")
        assert ghost is not None
        assert ghost["last_room_status"] == "banned"
        assert ghost["thumb_available"] == 0
        bob = mms_real.get_model(conn, "bob")
        assert bob is not None
        assert bob["last_room_status"] == "offline"
        assert bob["thumb_available"] == 1
        # alice should NOT have a row written by deep refresh.
        alice = mms_real.get_model(conn, "alice")
        assert alice is None
    finally:
        conn.close()

    # Notification flow: start + finish toast.
    msgs = [n[1] for n in notifies]
    assert any("deep refresh" in m.lower() or "starting" in m.lower()
               for m in msgs), f"missing start toast: {msgs!r}"
    assert any("done" in m.lower() or "complete" in m.lower()
               for m in msgs), f"missing done toast: {msgs!r}"


def test_deep_refresh_offline_meta_uses_biocontext_primary_path(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.24: when biocontext returns a populated dict, the deep
    crawl writes via ``upsert_biocontext`` and skips the status+HEAD
    fallback entirely. Verifies status/thumb funcs are NOT called.
    """
    actions = _import()
    db_path = str(tmp_path / "model_meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    bios_fetched: list[str] = []
    statuses_fetched: list[str] = []
    thumbs_fetched: list[str] = []

    def fake_biocontext(slug: str) -> dict[str, Any]:
        bios_fetched.append(slug)
        # Realistic response: room_status + last_broadcast.
        return {
            "room_status": "offline",
            "last_broadcast": "2026-04-20T10:00:00Z",
            "real_name": f"Real {slug}",
            "display_age": 25,
            "location": "Earth",
        }

    def fake_status(slug: str, fetch_func: Any = None) -> dict[str, Any]:
        statuses_fetched.append(slug)
        return {"success": False, "room_status": "offline", "url": "",
                "hidden_message": "", "cmaf_edge": False}

    def fake_head(slug: str) -> int:
        thumbs_fetched.append(slug)
        return 200

    actions.deep_refresh_offline_meta(
        handle=42,
        fav_slugs=["bob", "carol"],
        online_slugs=frozenset(),
        fetch_biocontext_func=fake_biocontext,
        fetch_status_func=fake_status,
        head_thumb_func=fake_head,
        notify_func=lambda h, m: None,
        spawn_func=lambda t: t(),
        sleep_func=lambda s: None,
    )

    # Both slugs hit biocontext; status/thumb fallback skipped.
    assert set(bios_fetched) == {"bob", "carol"}
    assert statuses_fetched == []
    assert thumbs_fetched == []

    # Rows were written via upsert_biocontext.
    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        bob = mms_real.get_model(conn, "bob")
        assert bob is not None
        assert bob["last_room_status"] == "offline"
        assert bob["real_name"] == "Real bob"
        assert bob["bio_fetched_epoch"] > 0
    finally:
        conn.close()


def test_refresh_offline_meta_closes_directory_handle(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """v0.7.23 spinner fix: the menu entry is a directory click;
    Kodi waits for endOfDirectory before clearing the busy spinner.
    The handler must close the directory immediately after spawning
    the bg worker so the user doesn't stare at a spinner during a
    long async refresh.
    """
    actions = _import()
    spawn_calls: list[Any] = []

    actions.refresh_offline_meta(
        handle=42,
        refresh_func=lambda: True,
        notify_func=lambda h, m: None,
        spawn_func=lambda t: spawn_calls.append(t),
    )
    end_calls = kodi_mocks["xbmcplugin"].endOfDirectory.call_args_list
    assert any(c.args and c.args[0] == 42 for c in end_calls), (
        "endOfDirectory(42, ...) was not called; busy spinner won't clear"
    )


def test_deep_refresh_offline_meta_closes_directory_handle(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same fix for the deep-refresh menu entry. The crawl runs for
    ~20 minutes -- absolutely cannot leave a spinner up that whole
    time."""
    actions = _import()
    db_path = str(tmp_path / "x.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))
    actions.deep_refresh_offline_meta(
        handle=42,
        fav_slugs=["a"],
        online_slugs=frozenset(),
        fetch_status_func=lambda s, **kw: {"room_status": "offline"},
        head_thumb_func=lambda s: 200,
        notify_func=lambda h, m: None,
        spawn_func=lambda t: None,  # don't run -- we just want the close
        sleep_func=lambda s: None,
    )
    end_calls = kodi_mocks["xbmcplugin"].endOfDirectory.call_args_list
    assert any(c.args and c.args[0] == 42 for c in end_calls)


def test_refresh_one_model_writes_status_to_db(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.23: single-slug refresh wired to the per-row context
    menu. Hits per-slug AJAX status + thumb HEAD, upserts via
    model_meta_store.upsert_status. Synchronous (one slug = ~2s,
    no need for daemon thread).
    """
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    notifies: list[tuple[str, str]] = []
    statuses: list[str] = []
    thumbs: list[str] = []

    def fake_status(slug: str, fetch_func: Any = None) -> dict[str, Any]:
        statuses.append(slug)
        return {"success": True, "room_status": "offline", "url": "",
                "hidden_message": "", "cmaf_edge": False}

    def fake_head(slug: str) -> int:
        thumbs.append(slug)
        return 200

    actions.refresh_one_model(
        handle=42, slug="alice",
        fetch_status_func=fake_status,
        head_thumb_func=fake_head,
        notify_func=lambda h, m: notifies.append((h, m)),
    )

    assert statuses == ["alice"]
    assert thumbs == ["alice"]

    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        row = mms_real.get_model(conn, "alice")
    finally:
        conn.close()
    assert row is not None
    assert row["last_room_status"] == "offline"
    assert row["thumb_available"] == 1

    msgs = [n[1] for n in notifies]
    assert any("alice" in m.lower() for m in msgs), (
        f"expected toast mentioning slug, got {msgs!r}"
    )


def test_refresh_one_model_skips_with_empty_slug(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """No slug provided (e.g., menu invoked from a non-model row) =
    silent no-op."""
    actions = _import()
    statuses: list[str] = []
    actions.refresh_one_model(
        handle=42, slug="",
        fetch_status_func=lambda s, **kw: statuses.append(s) or {},
        head_thumb_func=lambda s: 200,
        notify_func=lambda h, m: None,
    )
    assert statuses == []


# --------------------------------------------------------------------------- #
# v0.7.25: view_model_info -- right-click "View info" handler. Renders
# a directory listing with a Profile entry (full bio in plot) plus one
# entry per photo_set. Fetches biocontext inline when the DB row has
# no bio data yet so a never-seen-online model still renders rich.
# --------------------------------------------------------------------------- #


def test_view_model_info_renders_existing_row_no_inline_fetch(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the DB row already has a bio_fetched_epoch, view_model_info
    renders the directory directly with no biocontext HTTP -- we have
    fresh enough data already."""
    import json as _json
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    # Pre-seed a row with biocontext data.
    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        bio = {
            "room_status": "offline",
            "real_name": "Evelyn",
            "display_age": 24,
            "location": "Earth",
            "follower_count": 100,
            "sex": "Female",
            "subgender": "TGirl",
            "about_me": "hi I'm Evelyn",
            "photo_sets": [
                {"name": "Set A", "cover_url": "http://thumb/seta.jpg",
                 "tip_amount": 100},
                {"name": "Set B", "cover_url": "http://thumb/setb.jpg",
                 "tip_amount": 250},
            ],
        }
        mms_real.upsert_biocontext(conn, "alice", bio, now=1_000_000)
    finally:
        conn.close()

    bios_fetched: list[str] = []

    def fake_biocontext(slug: str) -> dict[str, Any]:
        bios_fetched.append(slug)
        return {}

    actions.view_model_info(
        handle=42,
        slug="alice",
        fetch_biocontext_func=fake_biocontext,
    )

    # Row was fresh -> no inline biocontext call.
    assert bios_fetched == [], (
        f"unexpected biocontext fetch -- row was already fresh: {bios_fetched!r}"
    )

    # Directory was rendered: addDirectoryItem was called at least once
    # (the Profile entry plus one per photo_set = 3).
    add_calls = kodi_mocks["xbmcplugin"].addDirectoryItem.call_args_list
    assert len(add_calls) >= 3, (
        f"expected >=3 directory items (Profile + 2 photo sets), got {len(add_calls)}"
    )

    # endOfDirectory was called to close the listing.
    end_calls = kodi_mocks["xbmcplugin"].endOfDirectory.call_args_list
    assert any(c.args and c.args[0] == 42 for c in end_calls), (
        "endOfDirectory not called -- listing won't close"
    )

    # At least one of the rendered URLs should reference the photo_set
    # cover -- unsure of art-mock shape but verify the URL strings on
    # addDirectoryItem mention the slug or carry the cover URL.
    rendered_urls = [
        c.kwargs.get("url", "") if "url" in c.kwargs else
        (c.args[1] if len(c.args) >= 2 else "")
        for c in add_calls
    ]
    # The photo_set entries should encode the cover URLs somewhere
    # (we look at addDirectoryItem urls AND the listitem art via the
    # ListItem mock's setArt call args).
    li_class = kodi_mocks["xbmcgui"].ListItem
    art_calls = li_class.return_value.setArt.call_args_list
    art_urls = [str(c) for c in art_calls]
    combined = "\n".join(rendered_urls + art_urls)
    assert "seta.jpg" in combined or "setb.jpg" in combined, (
        f"photo_set cover URLs missing from rendered listing: "
        f"urls={rendered_urls!r} art_calls={art_urls!r}"
    )

    _ = _json  # keep import marker


def test_view_model_info_inline_refreshes_when_no_bio(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the DB row is missing OR bio_fetched_epoch is 0
    (never crawled), view_model_info fetches biocontext inline,
    upserts, then renders."""
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    bios_fetched: list[str] = []

    def fake_biocontext(slug: str) -> dict[str, Any]:
        bios_fetched.append(slug)
        return {
            "room_status": "offline",
            "real_name": "Carol",
            "display_age": 22,
            "location": "Mars",
            "about_me": "hello",
        }

    actions.view_model_info(
        handle=42,
        slug="carol",
        fetch_biocontext_func=fake_biocontext,
    )

    # Inline fetch fired exactly once.
    assert bios_fetched == ["carol"], (
        f"expected one inline biocontext for carol, got {bios_fetched!r}"
    )

    # Row was upserted to DB with bio_fetched_epoch set.
    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        row = mms_real.get_model(conn, "carol")
    finally:
        conn.close()
    assert row is not None
    assert row["real_name"] == "Carol"
    assert row["bio_fetched_epoch"] > 0

    # Directory still rendered.
    end_calls = kodi_mocks["xbmcplugin"].endOfDirectory.call_args_list
    assert any(c.args and c.args[0] == 42 for c in end_calls)


def test_view_model_info_renders_when_fetch_fails_and_db_empty(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worst case: no DB row, fetch returns empty (network blip / banned).
    view_model_info still closes the directory cleanly with whatever
    bare-slug rendering it can manage so the user isn't stuck on a
    spinner."""
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    actions.view_model_info(
        handle=42,
        slug="ghost",
        fetch_biocontext_func=lambda slug: {},
    )

    end_calls = kodi_mocks["xbmcplugin"].endOfDirectory.call_args_list
    assert any(c.args and c.args[0] == 42 for c in end_calls), (
        "endOfDirectory must be called even when both DB and fetch are empty"
    )


def test_view_model_info_skips_with_empty_slug(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """No slug -> close directory immediately, no DB or fetch work."""
    actions = _import()
    bios_fetched: list[str] = []
    actions.view_model_info(
        handle=42,
        slug="",
        fetch_biocontext_func=lambda s: bios_fetched.append(s) or {},
    )
    assert bios_fetched == []
    end_calls = kodi_mocks["xbmcplugin"].endOfDirectory.call_args_list
    assert any(c.args and c.args[0] == 42 for c in end_calls)


def test_view_model_info_emits_phase_timing_summary(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.54: view_model_info must emit a consolidated 'done' log line
    with per-phase timings so a future lockup (like the 2026-05-29 70s
    hang on model_d) can be pinpointed without re-running the
    user's session.

    Required keys in the done line: total_ms, db_open_ms, get_model_ms,
    render_ms. Optional keys appear only when their phase ran:
    fetch_bio_ms, upsert_bio_ms, get_model2_ms.
    """
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        mms_real.upsert_biocontext(conn, "trace_me", {
            "real_name": "TraceMe",
            "about_me": "hello there",
        }, now=1_000_000)
    finally:
        conn.close()

    captured: list[str] = []
    from resources.lib import logger as logger_module
    monkeypatch.setattr(logger_module, "_log",
                        lambda msg, log_path=None: captured.append(str(msg)))

    actions.view_model_info(
        handle=42,
        slug="trace_me",
        fetch_biocontext_func=lambda s: {},
    )

    done_lines = [m for m in captured if m.startswith("view_model_info: done")]
    assert len(done_lines) == 1, (
        f"expected exactly one 'view_model_info: done' summary line, "
        f"got {len(done_lines)}: {done_lines!r}"
    )
    line = done_lines[0]
    assert "slug='trace_me'" in line, f"slug missing in summary: {line!r}"
    for key in ("total_ms=", "db_open_ms=", "get_model_ms=", "render_ms="):
        assert key in line, f"missing required phase {key!r} in: {line!r}"


def test_view_model_info_timing_summary_includes_fetch_phase_when_inline_refresh(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.54: when the inline biocontext fetch fires (no
    bio_fetched_epoch yet), the timing summary must include
    fetch_bio_ms so we can distinguish a slow fetch from a slow render.
    """
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    captured: list[str] = []
    from resources.lib import logger as logger_module
    monkeypatch.setattr(logger_module, "_log",
                        lambda msg, log_path=None: captured.append(str(msg)))

    actions.view_model_info(
        handle=42,
        slug="never_cached",
        fetch_biocontext_func=lambda s: {
            "real_name": "Cached",
            "about_me": "first time",
        },
    )

    done_lines = [m for m in captured if m.startswith("view_model_info: done")]
    assert len(done_lines) == 1
    line = done_lines[0]
    assert "fetch_bio_ms=" in line, (
        f"fetch_bio_ms missing despite inline fetch path: {line!r}"
    )
    assert "upsert_bio_ms=" in line, (
        f"upsert_bio_ms missing despite successful fetch+upsert: {line!r}"
    )


def test_view_model_info_photo_set_skips_art_during_playback(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.56: when a player is active, photo_set ListItems MUST skip
    setArt entirely. Even after the v0.7.55 thumb-only fix (51 -> 17
    parallel fetches), the chaturbate image CDN was still slow enough
    that 17 parallel cover fetches starved the video decoder
    (OutputPicture timeouts continued at ~5/sec instead of 18/sec, but
    the freezes still happened). Skipping setArt during active playback
    eliminates the fetch storm completely; the cover is decorative and
    the user can read the photo_set label/cost/count without it.
    """
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        mms_real.upsert_biocontext(conn, "playing_now", {
            "real_name": "PlayingNow",
            "photo_sets": [
                {"name": f"Set {i}", "cover_url": f"http://thumb/{i}.jpg",
                 "tip_amount": 50}
                for i in range(3)
            ],
        }, now=1_000_000)
    finally:
        conn.close()

    kodi_mocks["xbmc"].Player.return_value.isPlaying.return_value = True

    actions.view_model_info(
        handle=42,
        slug="playing_now",
        fetch_biocontext_func=lambda s: {},
    )

    li_class = kodi_mocks["xbmcgui"].ListItem
    art_calls = li_class.return_value.setArt.call_args_list
    photo_set_art_calls = [
        c for c in art_calls
        if c.args and isinstance(c.args[0], dict)
        and any(str(v).startswith("http://thumb/") for v in c.args[0].values())
    ]
    assert len(photo_set_art_calls) == 0, (
        f"expected ZERO photo_set setArt calls when player is active "
        f"(decoder-starvation fix), got {len(photo_set_art_calls)}: "
        f"{photo_set_art_calls!r}"
    )


def test_view_model_info_photo_set_keeps_thumb_when_idle(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.56: when no player is active, photo_set ListItems still set
    a thumb (the decorative cover) -- the skip is conditional on active
    playback, not unconditional.
    """
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        mms_real.upsert_biocontext(conn, "idle_now", {
            "real_name": "IdleNow",
            "photo_sets": [
                {"name": "Set A", "cover_url": "http://thumb/a.jpg",
                 "tip_amount": 50},
            ],
        }, now=1_000_000)
    finally:
        conn.close()

    kodi_mocks["xbmc"].Player.return_value.isPlaying.return_value = False

    actions.view_model_info(
        handle=42,
        slug="idle_now",
        fetch_biocontext_func=lambda s: {},
    )

    li_class = kodi_mocks["xbmcgui"].ListItem
    art_calls = li_class.return_value.setArt.call_args_list
    photo_set_art_calls = [
        c for c in art_calls
        if c.args and isinstance(c.args[0], dict)
        and any(str(v).startswith("http://thumb/") for v in c.args[0].values())
    ]
    assert len(photo_set_art_calls) == 1, (
        f"expected 1 photo_set setArt call when player is idle, "
        f"got {len(photo_set_art_calls)}: {photo_set_art_calls!r}"
    )
    assert set(photo_set_art_calls[0].args[0].keys()) == {"thumb"}


def test_view_model_info_photo_set_art_uses_thumb_only(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.55: photo_set ListItems must call setArt with ONLY a 'thumb'
    key, never 'icon' or 'fanart'. Real-world repro 2026-05-29 01:42 CDT:
    opening View Info for a model with 17 photo_sets WHILE a stream was
    playing wedged Kodi's video decoder ('OutputPicture - timeout waiting
    for buffer' at 18 msg/sec) and ActiveAE reported 52820s audio sync
    error. Root cause: 17 photo_sets * 3 art slots = 51 simultaneous
    thumbnail HTTP fetches saturated the texture cache thread and starved
    the decoder. Trimming to 1 slot reduces the fetch storm 3x.
    """
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        mms_real.upsert_biocontext(conn, "many_sets", {
            "real_name": "ManySets",
            "photo_sets": [
                {"name": f"Set {i}", "cover_url": f"http://thumb/{i}.jpg",
                 "tip_amount": 50}
                for i in range(5)
            ],
        }, now=1_000_000)
    finally:
        conn.close()

    # v0.7.56: player must be idle for thumbs to be set.
    kodi_mocks["xbmc"].Player.return_value.isPlaying.return_value = False

    actions.view_model_info(
        handle=42,
        slug="many_sets",
        fetch_biocontext_func=lambda s: {},
    )

    li_class = kodi_mocks["xbmcgui"].ListItem
    art_calls = li_class.return_value.setArt.call_args_list
    photo_set_art_calls = [
        c for c in art_calls
        if c.args and isinstance(c.args[0], dict)
        and any(str(v).startswith("http://thumb/") for v in c.args[0].values())
    ]
    assert len(photo_set_art_calls) == 5, (
        f"expected 5 photo_set setArt calls, got {len(photo_set_art_calls)}: "
        f"{photo_set_art_calls!r}"
    )
    for call in photo_set_art_calls:
        art_dict = call.args[0]
        assert set(art_dict.keys()) == {"thumb"}, (
            f"photo_set setArt should set ONLY 'thumb' (decoder-starvation "
            f"fix), got keys={set(art_dict.keys())!r} on call {call!r}"
        )


def test_render_view_model_info_emits_phase_timing_summary(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.54: _render_view_model_info must emit its own consolidated
    timing line so we can tell whether the slow path was render vs DB.
    Required keys: render_total_ms, image_ms, add_items_ms,
    end_of_dir_ms.
    """
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        mms_real.upsert_biocontext(conn, "render_me", {
            "real_name": "RenderMe",
            "about_me": "with photos",
            "photo_sets": [
                {"name": "Set A", "cover_url": "http://thumb/a.jpg",
                 "tip_amount": 50},
            ],
        }, now=1_000_000)
    finally:
        conn.close()

    captured: list[str] = []
    from resources.lib import logger as logger_module
    monkeypatch.setattr(logger_module, "_log",
                        lambda msg, log_path=None: captured.append(str(msg)))

    actions.view_model_info(
        handle=42,
        slug="render_me",
        fetch_biocontext_func=lambda s: {},
    )

    render_lines = [m for m in captured
                    if "_render_view_model_info: done" in m]
    assert len(render_lines) == 1, (
        f"expected exactly one '_render_view_model_info: done' line, "
        f"got {len(render_lines)}: {render_lines!r}"
    )
    line = render_lines[0]
    for key in ("render_total_ms=", "image_ms=", "add_items_ms=",
                "end_of_dir_ms="):
        assert key in line, f"missing required render phase {key!r}: {line!r}"


def test_refresh_one_model_marks_404_as_gone(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.28: when biocontext returns the {'_http_404': True}
    sentinel (profile page literally doesn't exist), the row is
    stamped last_room_status='gone' so the [GONE] prefix surfaces
    in offline favs without a thumb-HEAD round trip."""
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    statuses_called: list[str] = []
    thumbs_called: list[str] = []

    actions.refresh_one_model(
        handle=42, slug="ghost",
        fetch_biocontext_func=lambda s: {"_http_404": True},
        fetch_status_func=lambda s, **kw: statuses_called.append(s) or {},
        head_thumb_func=lambda s: thumbs_called.append(s) or 200,
        notify_func=lambda h, m: None,
    )

    # 404 short-circuits the status+thumb fallback path.
    assert statuses_called == [], (
        f"404 path must not hit AJAX status: {statuses_called!r}"
    )
    assert thumbs_called == [], (
        f"404 path must not hit thumb HEAD: {thumbs_called!r}"
    )

    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        row = mms_real.get_model(conn, "ghost")
    finally:
        conn.close()
    assert row is not None
    assert row["last_room_status"] == "gone", (
        f"404 must stamp 'gone', got {row.get('last_room_status')!r}"
    )


def test_deep_refresh_marks_404_as_gone(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same 404-as-gone behaviour applies to the bulk deep crawl.
    A 404'd slug counts as gone in the summary toast and gets the
    [GONE] prefix on offline favs without falling through to the
    cheap status+thumb path."""
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    statuses: list[str] = []
    thumbs: list[str] = []
    bios: list[str] = []
    notifies: list[tuple[str, str]] = []

    def fake_bio(slug: str) -> dict[str, Any]:
        bios.append(slug)
        if slug == "ghost":
            return {"_http_404": True}
        if slug == "alive":
            return {"room_status": "offline", "real_name": "A"}
        return {}  # blip

    actions.deep_refresh_offline_meta(
        handle=42,
        fav_slugs=["ghost", "alive", "blip"],
        online_slugs=frozenset(),
        fetch_biocontext_func=fake_bio,
        fetch_status_func=lambda s, **kw: statuses.append(s) or
            {"room_status": "offline"},
        head_thumb_func=lambda s: thumbs.append(s) or 200,
        notify_func=lambda h, m: notifies.append((h, m)),
        spawn_func=lambda t: t(),
        sleep_func=lambda s: None,
    )

    # ghost -> bio path (404 fast-path)
    # alive -> bio path (real data)
    # blip  -> empty {} -> falls through to status + thumb
    assert "ghost" in bios and "alive" in bios and "blip" in bios
    assert "ghost" not in statuses, (
        f"404 must short-circuit status: {statuses!r}"
    )
    assert "ghost" not in thumbs, (
        f"404 must short-circuit thumb HEAD: {thumbs!r}"
    )
    assert "blip" in statuses, "blip should fall back to status"

    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        ghost = mms_real.get_model(conn, "ghost")
    finally:
        conn.close()
    assert ghost is not None
    assert ghost["last_room_status"] == "gone"

    # Done toast should reflect 1 gone in the count.
    msgs = [n[1] for n in notifies]
    assert any("1 gone" in m for m in msgs), (
        f"summary toast must report 1 gone: {msgs!r}"
    )


def test_show_profile_opens_textviewer_dialog(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.27: clicking the Profile entry inside view_model_info
    opens a scrollable textviewer dialog with the full formatted bio.
    The handler closes the directory with succeeded=False so the user
    stays on the parent view_model_info listing after dismissing the
    dialog.
    """
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        bio = {
            "room_status": "offline",
            "real_name": "Evelyn",
            "display_age": 24,
            "location": "Earth",
            "about_me": "Hi I am Evelyn",
            "follower_count": 123,
        }
        mms_real.upsert_biocontext(conn, "alice", bio, now=1_000_000)
    finally:
        conn.close()

    # Track Dialog().textviewer calls -- the v0.7.27 entry point.
    textviewer_calls: list[tuple[str, str]] = []

    class _DialogTracker:
        def textviewer(self, heading: str, message: str,
                       usemono: bool = False) -> None:
            textviewer_calls.append((heading, message))

        def notification(self, *a: Any, **kw: Any) -> None: ...
        def input(self, *a: Any, **kw: Any) -> str:
            return ""
        def ok(self, *a: Any, **kw: Any) -> bool:
            return True

    kodi_mocks["xbmcgui"].Dialog = _DialogTracker

    actions.show_profile(handle=42, slug="alice")

    assert textviewer_calls, "Dialog().textviewer must be invoked"
    heading, message = textviewer_calls[0]
    assert "alice" in heading.lower() or "evelyn" in heading.lower(), (
        f"heading should mention slug or real_name: {heading!r}"
    )
    # The message contains the full bio.
    assert "Evelyn" in message or "Hi I am Evelyn" in message, (
        f"message missing bio content: {message[:120]!r}"
    )

    # Directory closes with succeeded=False so Kodi keeps the user
    # on the parent view_model_info listing after dismiss.
    end_calls = kodi_mocks["xbmcplugin"].endOfDirectory.call_args_list
    last_call = end_calls[-1]
    handle_arg = last_call.args[0] if last_call.args else None
    succeeded_kw = last_call.kwargs.get("succeeded", True)
    assert handle_arg == 42 and succeeded_kw is False, (
        f"expected endOfDirectory(42, succeeded=False), "
        f"got args={last_call.args!r} kwargs={last_call.kwargs!r}"
    )


def test_show_profile_skips_with_empty_slug(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """No slug -> close dir silently, no Dialog open."""
    actions = _import()
    textviewer_calls: list[Any] = []

    class _D:
        def textviewer(self, h: str, m: str, usemono: bool = False) -> None:
            textviewer_calls.append((h, m))
        def notification(self, *a: Any, **kw: Any) -> None: ...
        def input(self, *a: Any, **kw: Any) -> str:
            return ""
        def ok(self, *a: Any, **kw: Any) -> bool:
            return True

    kodi_mocks["xbmcgui"].Dialog = _D
    actions.show_profile(handle=42, slug="")
    assert textviewer_calls == []
    end_calls = kodi_mocks["xbmcplugin"].endOfDirectory.call_args_list
    assert any(c.args and c.args[0] == 42 for c in end_calls)


def test_view_model_info_profile_entry_routes_to_show_profile(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Profile entry's URL must point at mode=show_profile so
    clicking it opens the dialog instead of being a no-op."""
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        mms_real.upsert_biocontext(
            conn, "alice",
            {"room_status": "offline", "real_name": "Evelyn"},
            now=1_000_000,
        )
    finally:
        conn.close()

    actions.view_model_info(
        handle=42, slug="alice",
        fetch_biocontext_func=lambda s: {},
    )

    add_calls = kodi_mocks["xbmcplugin"].addDirectoryItem.call_args_list
    urls = []
    for c in add_calls:
        if "url" in c.kwargs:
            urls.append(c.kwargs["url"])
        elif len(c.args) >= 2:
            urls.append(c.args[1])
    assert any("mode=show_profile" in u and "slug=alice" in u
               for u in urls), (
        f"Profile entry must route to show_profile: urls={urls!r}"
    )


def test_show_picture_invokes_kodi_builtin(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """v0.7.27: clicking a photo_set entry fires the show_picture
    handler which calls xbmc.executebuiltin('ShowPicture(<url>)')
    so Kodi opens the cover in its fullscreen picture viewer.

    v0.7.39: URL must be on a trusted CB host (allowlist defense
    against ShowPicture builtin injection from a malicious cover_url).
    """
    actions = _import()
    actions.show_picture(
        handle=-1,
        url="https://static-pub.highwebmedia.com/cover.jpg",
    )
    bcalls = kodi_mocks["xbmc"].executebuiltin.call_args_list
    cmds = [str(c.args[0]) if c.args else "" for c in bcalls]
    assert any("ShowPicture(" in c
               and "cover.jpg" in c for c in cmds), (
        f"expected ShowPicture(<url>) builtin, got: {cmds!r}"
    )


def test_show_picture_rejects_untrusted_host(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """v0.7.39 (audit pass #5 HIGH, agent 1): a malicious biocontext
    could publish a photo_set with cover_url pointing at an internal
    LAN service or a file:// scheme. ShowPicture would happily open
    the local file in Kodi's picture viewer (info disclosure) or hit
    the LAN service. Allowlist rejects everything not on a CB host."""
    actions = _import()
    actions.show_picture(handle=-1, url="http://192.168.1.1:8088/admin")
    bcalls = kodi_mocks["xbmc"].executebuiltin.call_args_list
    cmds = [str(c.args[0]) if c.args else "" for c in bcalls]
    assert not any("ShowPicture(" in c for c in cmds), (
        f"untrusted host should NOT fire ShowPicture: {cmds!r}"
    )


def test_show_picture_rejects_url_with_builtin_breakout_char(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """v0.7.39: even a trusted-host URL gets rejected if it contains
    `)` or `,` (the chars that would break out of the
    ShowPicture builtin). Belt-and-braces defense."""
    actions = _import()
    actions.show_picture(
        handle=-1,
        # CB-host URL with crafted ')' that would close ShowPicture(
        # and let a follow-up builtin run.
        url="https://thumb.live.mmcdn.com/ri/a.jpg),Quit,XBMC.ShowPicture(",
    )
    bcalls = kodi_mocks["xbmc"].executebuiltin.call_args_list
    cmds = [str(c.args[0]) if c.args else "" for c in bcalls]
    assert not any("ShowPicture(" in c for c in cmds), (
        f"URL with ')' must be rejected: {cmds!r}"
    )


def test_show_picture_skips_when_url_empty(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """No url -> silent no-op; the builtin is never called with an
    empty arg (would open a blank Kodi picture viewer)."""
    actions = _import()
    actions.show_picture(handle=-1, url="")
    bcalls = kodi_mocks["xbmc"].executebuiltin.call_args_list
    cmds = [str(c.args[0]) if c.args else "" for c in bcalls]
    assert not any("ShowPicture(" in c for c in cmds), (
        f"ShowPicture must NOT fire on empty url: {cmds!r}"
    )


def test_view_model_info_photo_set_entries_use_show_picture(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.27: each photo_set entry's plugin URL should route through
    show_picture so clicking opens the cover fullscreen, NOT loop back
    to view_model_info (which was a no-op in 0.7.25/0.7.26).
    """
    import json as _json
    actions = _import()
    db_path = str(tmp_path / "meta.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    import resources.lib.model_meta_store as mms_real
    conn = mms_real.open_db(db_path)
    try:
        bio = {
            "room_status": "offline",
            "real_name": "Evelyn",
            "photo_sets": [
                {"name": "Pillow Humping", "cover_url":
                 "https://static-pub.example.com/cov1.jpg",
                 "photo_count": 71, "tokens": 150,
                 "is_video": True,
                 "video_duration_in_seconds": 1061,
                 "video_has_sound": True,
                 "user_has_purchased": False, "user_can_access": False},
                {"name": "Beach", "cover_url":
                 "https://static-pub.example.com/cov2.jpg",
                 "photo_count": 30, "tokens": 99,
                 "is_video": False,
                 "user_has_purchased": False, "user_can_access": False},
            ],
        }
        mms_real.upsert_biocontext(conn, "alice", bio, now=1_000_000)
    finally:
        conn.close()

    actions.view_model_info(
        handle=42,
        slug="alice",
        fetch_biocontext_func=lambda s: {},  # row already fresh
    )

    add_calls = kodi_mocks["xbmcplugin"].addDirectoryItem.call_args_list
    rendered_urls = []
    for c in add_calls:
        if "url" in c.kwargs:
            rendered_urls.append(c.kwargs["url"])
        elif len(c.args) >= 2:
            rendered_urls.append(c.args[1])

    # Profile entry stays on view_model_info; the photo_set entries
    # must route through show_picture and carry the cover URL.
    photo_urls = [u for u in rendered_urls if "show_picture" in u]
    assert len(photo_urls) == 2, (
        f"expected 2 show_picture URLs, got: {rendered_urls!r}"
    )
    joined = "\n".join(photo_urls)
    assert "cov1.jpg" in joined or "cov1.jpg" in _json.dumps(joined), (
        f"first cover URL missing from photo_set entries: {photo_urls!r}"
    )
    assert "cov2.jpg" in joined, (
        f"second cover URL missing from photo_set entries: {photo_urls!r}"
    )


def test_deep_refresh_offline_meta_skips_when_locked(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If _BULK_REFRESH_LOCK is held, deep-refresh toasts 'in progress'
    and exits without firing the per-slug fetches."""
    actions = _import()
    db_path = str(tmp_path / "x.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    notifies: list[tuple[str, str]] = []
    fetches: list[str] = []

    # v0.7.37: deep refresh now has its own _DEEP_REFRESH_LOCK so it
    # can run concurrently with bulk-refresh ops. The "already in
    # progress" guard fires when the deep lock specifically is held.
    actions._DEEP_REFRESH_LOCK.acquire()
    try:
        actions.deep_refresh_offline_meta(
            handle=42,
            fav_slugs=["a", "b"],
            online_slugs=frozenset(),
            fetch_status_func=lambda slug, fetch_func=None: fetches.append(slug) or {},
            head_thumb_func=lambda slug: 200,
            notify_func=lambda h, m: notifies.append((h, m)),
            spawn_func=lambda t: t(),
            sleep_func=lambda s: None,
        )
    finally:
        actions._DEEP_REFRESH_LOCK.release()

    assert fetches == [], "must not fetch when lock is held"
    msgs = [n[1] for n in notifies]
    assert any("already" in m.lower() or "in progress" in m.lower() for m in msgs)


def test_deep_refresh_runs_concurrently_with_bulk_refresh_lock(
    kodi_mocks: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.37 (race audit pass 1, agent 1 MEDIUM #6): deep refresh
    no longer holds _BULK_REFRESH_LOCK -- it has its own
    _DEEP_REFRESH_LOCK. The TV loop's bulk-cache TTL refresh can keep
    working during the ~20-min crawl instead of falling through to
    per-slug AJAX (which would stack request rates and risk a CB ban).

    This test pins: holding _BULK_REFRESH_LOCK does NOT block the
    deep refresh from starting.
    """
    actions = _import()
    db_path = str(tmp_path / "x.db")
    monkeypatch.setattr(actions, "_model_meta_db_path",
                        lambda: Path(db_path))

    notifies: list[tuple[str, str]] = []
    bios_fetched: list[str] = []

    def fake_bio(slug: str) -> dict[str, Any]:
        bios_fetched.append(slug)
        return {"room_status": "offline", "real_name": slug}

    actions._BULK_REFRESH_LOCK.acquire()
    try:
        actions.deep_refresh_offline_meta(
            handle=42,
            fav_slugs=["a"],
            online_slugs=frozenset(),
            fetch_biocontext_func=fake_bio,
            fetch_status_func=lambda s, **kw: {},
            head_thumb_func=lambda s: 200,
            notify_func=lambda h, m: notifies.append((h, m)),
            spawn_func=lambda t: t(),
            sleep_func=lambda s: None,
        )
    finally:
        actions._BULK_REFRESH_LOCK.release()

    # _BULK_REFRESH_LOCK was held but deep refresh STILL ran.
    assert bios_fetched == ["a"], (
        f"deep refresh should NOT block on _BULK_REFRESH_LOCK; "
        f"got bios_fetched={bios_fetched!r}"
    )


def test_tv_bulk_mark_offline_under_concurrent_refresh_doesnt_lose_eviction(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """v0.7.37 (race audit pass 1, agent 1 HIGH #2): the
    read-modify-write in _tv_bulk_mark_offline used to race
    _tv_bulk_refresh's wholesale assignment. Now both mutations are
    serialized through _TV_CACHE_LOCK, so the eviction either lands
    before the refresh's snapshot (and thus _OFFLINE_SESSION_SLUGS
    excludes the slug from the new set) or after (and thus the new
    set still has the slug, but we then evict it). Either way, the
    slug ends up evicted -- never both.

    This is a contract test: assert _tv_bulk_mark_offline always
    leaves the slug evicted from _TV_BULK_CACHE['slugs'] AFTER the
    call returns, regardless of any concurrent refresh state.
    """
    import threading
    actions = _import()
    actions._TV_BULK_CACHE["slugs"] = frozenset({"alice", "bob"})
    actions._OFFLINE_SESSION_SLUGS.clear()

    # Simulate a thread mid-refresh by acquiring the cache lock.
    other_lock_held = threading.Event()
    refresh_done = threading.Event()

    def fake_concurrent_refresh() -> None:
        with actions._TV_CACHE_LOCK:
            other_lock_held.set()
            # Simulate refresh writing a fresh set.
            refresh_done.wait(timeout=2.0)
            actions._TV_BULK_CACHE["slugs"] = frozenset(
                {"alice", "bob"} - frozenset(
                    actions._OFFLINE_SESSION_SLUGS.keys()
                )
            )

    t = threading.Thread(target=fake_concurrent_refresh)
    t.start()
    other_lock_held.wait(timeout=2.0)

    # While the refresh thread is mid-critical-section, fire mark.
    # mark_offline must wait for the lock and then evict.
    mark_thread = threading.Thread(
        target=lambda: actions._tv_bulk_mark_offline("alice"),
    )
    mark_thread.start()

    # Let the refresh thread finish first.
    refresh_done.set()
    t.join(timeout=2.0)
    mark_thread.join(timeout=2.0)

    # Final state: alice must be evicted, alice must be in the
    # session blocklist.
    assert "alice" not in actions._TV_BULK_CACHE["slugs"]
    assert "alice" in actions._OFFLINE_SESSION_SLUGS
    # Cleanup so other tests aren't polluted.
    actions._OFFLINE_SESSION_SLUGS.clear()


def test_tv_cache_snapshot_is_atomic_against_concurrent_writes(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """v0.7.37: pick_target / is_live_func now use _tv_cache_snapshot()
    so a within-walk refresh swap doesn't make slot N see a slug live
    and slot N+1 see it offline. Snapshot returns a frozen view that's
    consistent for the duration of one walk."""
    actions = _import()
    actions._TV_BULK_CACHE["slugs"] = frozenset({"alice", "bob", "carol"})

    snap1 = actions._tv_cache_snapshot()
    # Mutate the cache after the snapshot.
    actions._TV_BULK_CACHE["slugs"] = frozenset({"david"})
    snap2 = actions._tv_cache_snapshot()

    assert snap1 == frozenset({"alice", "bob", "carol"})
    assert snap2 == frozenset({"david"})
    assert isinstance(snap1, frozenset), (
        "snapshot must be a frozen view (immune to subsequent "
        "modification)"
    )


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


def _make_textures_db(path: Path, rows: list[tuple[str, str]]) -> None:
    """Create a minimal Textures<N>.db schema with the given (url, cachedurl) rows."""
    import sqlite3 as _sqlite
    conn = _sqlite.connect(str(path))
    conn.execute(
        "CREATE TABLE texture (id INTEGER PRIMARY KEY, url TEXT, cachedurl TEXT)"
    )
    for url, cached in rows:
        conn.execute(
            "INSERT INTO texture (url, cachedurl) VALUES (?, ?)",
            (url, cached),
        )
    conn.commit()
    conn.close()


def test_refresh_artwork_clears_textures_db(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Should DELETE rows whose url matches the addon ID across every
    Textures<N>.db it finds, plus unlink the cached files in Thumbnails/."""
    import sqlite3 as _sqlite
    db_dir = tmp_path / "Database"
    db_dir.mkdir()
    db_path = db_dir / "Textures13.db"
    thumbs_dir = tmp_path / "thumbnails"
    thumbs_dir.mkdir()
    (thumbs_dir / "a.png").write_bytes(b"x")
    (thumbs_dir / "b.png").write_bytes(b"y")
    (thumbs_dir / "c.png").write_bytes(b"z")

    _make_textures_db(db_path, [
        ("special://home/addons/plugin.video.chaturbatetv/icon.png", "a.png"),
        ("special://home/addons/plugin.video.chaturbatetv/fanart.jpg", "b.png"),
        ("https://example.com/some-other.jpg", "c.png"),
    ])

    fake_xbmcvfs = MagicMock()
    paths = {
        "special://database/": str(db_dir) + "/",
        "special://thumbnails/": str(thumbs_dir) + "/",
    }
    fake_xbmcvfs.translatePath = lambda p: paths.get(p, p)
    monkeypatch.setitem(sys.modules, "xbmcvfs", fake_xbmcvfs)
    sys.modules.pop("resources.lib.addon_actions", None)

    actions = _import()
    actions.refresh_artwork(handle=42)

    conn = _sqlite.connect(str(db_path))
    rows = conn.execute("SELECT url FROM texture").fetchall()
    conn.close()
    urls = [r[0] for r in rows]
    assert "https://example.com/some-other.jpg" in urls
    assert all("plugin.video.chaturbatetv" not in u for u in urls)
    assert not (thumbs_dir / "a.png").exists()
    assert not (thumbs_dir / "b.png").exists()
    # Unrelated cached file is preserved.
    assert (thumbs_dir / "c.png").exists()


def test_refresh_artwork_walks_textures13_AND_textures14(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: Kodi 21+ bumped the schema to Textures14.db. The
    verb must clean BOTH (some installs have both around for legacy
    reasons; Kodi just reads from the highest version it knows). A
    user on Kodi 21+ wouldn't see the icon refresh if we only scanned
    Textures13.db.
    """
    import sqlite3 as _sqlite
    db_dir = tmp_path / "Database"
    db_dir.mkdir()
    db13 = db_dir / "Textures13.db"
    db14 = db_dir / "Textures14.db"
    thumbs_dir = tmp_path / "thumbnails"
    thumbs_dir.mkdir()
    (thumbs_dir / "old13.png").write_bytes(b"x")
    (thumbs_dir / "new14.png").write_bytes(b"y")

    _make_textures_db(db13, [
        ("special://home/addons/plugin.video.chaturbatetv/icon.png", "old13.png"),
    ])
    _make_textures_db(db14, [
        ("special://home/addons/plugin.video.chaturbatetv/icon.png", "new14.png"),
    ])

    fake_xbmcvfs = MagicMock()
    paths = {
        "special://database/": str(db_dir) + "/",
        "special://thumbnails/": str(thumbs_dir) + "/",
    }
    fake_xbmcvfs.translatePath = lambda p: paths.get(p, p)
    monkeypatch.setitem(sys.modules, "xbmcvfs", fake_xbmcvfs)
    sys.modules.pop("resources.lib.addon_actions", None)

    actions = _import()
    actions.refresh_artwork(handle=42)

    # Both DBs cleared.
    conn = _sqlite.connect(str(db13))
    assert conn.execute("SELECT COUNT(*) FROM texture").fetchone()[0] == 0
    conn.close()
    conn = _sqlite.connect(str(db14))
    assert conn.execute("SELECT COUNT(*) FROM texture").fetchone()[0] == 0
    conn.close()
    # Both cached files unlinked.
    assert not (thumbs_dir / "old13.png").exists()
    assert not (thumbs_dir / "new14.png").exists()


def test_refresh_artwork_tolerates_schema_mismatch_db(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a future Kodi version bumps the schema (e.g. column rename,
    table dropped), the verb must skip that DB instead of crashing."""
    import sqlite3 as _sqlite
    db_dir = tmp_path / "Database"
    db_dir.mkdir()
    weird = db_dir / "Textures99.db"
    conn = _sqlite.connect(str(weird))
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    thumbs_dir = tmp_path / "thumbnails"
    thumbs_dir.mkdir()

    fake_xbmcvfs = MagicMock()
    paths = {
        "special://database/": str(db_dir) + "/",
        "special://thumbnails/": str(thumbs_dir) + "/",
    }
    fake_xbmcvfs.translatePath = lambda p: paths.get(p, p)
    monkeypatch.setitem(sys.modules, "xbmcvfs", fake_xbmcvfs)
    sys.modules.pop("resources.lib.addon_actions", None)

    actions = _import()
    # Must not raise.
    actions.refresh_artwork(handle=42)


def test_playvid_offline_invalidates_bulk_live_cache(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lesson 32: when playvid resolves offline AND TV mode is active,
    drop that slug from the cached live set so the loop's next
    pick_target doesn't re-pick the same offline slug for the rest of
    the poll cycle (~9.5 min default), each iter offline-skipping again,
    infinite tight loop on a dead model.

    v0.7.15: cache invalidation still happens; PlayerControl(Next) is
    no longer fired (the silent stub's natural end advances the
    playlist on its own). The cache invalidation is what this test
    pins; advance behavior is covered by
    test_playvid_offline_during_tv_mode_serves_silent_stub.
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

    _patch_resolver_and_xbmcplugin(monkeypatch, success=False)

    # Pre-seed the bulk cache with our offline slug so we can verify
    # it gets removed.
    import resources.lib.addon_actions as actions_mod
    monkeypatch.setattr(
        actions_mod, "_TV_BULK_CACHE",
        {"slugs": frozenset({"ghost", "alice", "bob"}), "ts": 1000.0,
         "ttl_s": 300.0},
    )
    actions = _import()
    # Repoint the real cache after _import (sys.modules dance).
    import resources.lib.addon_actions as actions_real
    actions_real._TV_BULK_CACHE = {
        "slugs": frozenset({"ghost", "alice", "bob"}),
        "ts": 1000.0,
        "ttl_s": 300.0,
    }

    actions.playvid(handle=42, slug="ghost", name="ghost")

    # ghost should be removed from the cache.
    assert "ghost" not in actions_real._TV_BULK_CACHE["slugs"]
    # alice + bob untouched.
    assert "alice" in actions_real._TV_BULK_CACHE["slugs"]
    assert "bob" in actions_real._TV_BULK_CACHE["slugs"]
    # v0.7.15: PlayerControl(Next) is no longer fired -- silent stub
    # natural end advances the playlist on its own. Pinning here so
    # nobody re-adds it without thinking through the dialog tradeoff.
    builtins_called = [c.args[0] for c in
                       kodi_mocks["xbmc"].executebuiltin.call_args_list]
    assert "PlayerControl(Next)" not in builtins_called


def test_make_bulk_is_live_func_uses_affiliate_endpoint(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TV mode's is_live check should hit the single-call affiliate
    endpoint, NOT call is_model_live per slug.
    For a 60-entry TV list this turns 60 sequential AJAX calls per
    poll cycle into 1 affiliate call.
    """
    import json as _json
    fetch_calls: list[str] = []

    def fake_fetch(url: str, **_kw: Any) -> str:
        fetch_calls.append(url)
        return _json.dumps([
            # current_show='public' is required post-0.7.31 for the
            # bulk cache to consider these rooms TV-pickable.
            {"username": "alice", "slug": "alice", "gender": "f",
             "current_show": "public",
             "num_users": 100, "image_url": "", "room_subject": ""},
            {"username": "bob", "slug": "bob", "gender": "m",
             "current_show": "public",
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


def test_make_bulk_is_live_falls_back_to_per_slug_on_cold_start_outage(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-world scenario from 2026-04-27: chaturbate's affiliate
    endpoint slowed to ~30s+ timeouts. Cold-start TV mode (no prior
    bulk fetch, cache is empty) would have NO live slugs and pick_target
    would always return None - TV mode silently dies. The fallback hits
    per-slug ``cb_client.is_model_live`` for the entry being queried so
    TV mode keeps working even when the bulk endpoint is down.
    """
    import resources.lib.cb_client as cb_client_mod

    def fake_bulk_fetch(url: str, **_kw: Any) -> str:
        raise OSError("simulated affiliate timeout")

    per_slug_calls: list[str] = []

    def fake_per_slug(slug: str, **_kw: Any) -> bool:
        per_slug_calls.append(slug)
        return slug == "alice"

    monkeypatch.setattr(cb_client_mod, "fetch_browse_page",
                        lambda url, **_kw: fake_bulk_fetch(url))
    monkeypatch.setattr(cb_client_mod, "is_model_live", fake_per_slug)

    actions = _import()
    is_live = actions._make_bulk_is_live_func(poll_minutes=10)

    # alice is live per the per-slug stub.
    assert is_live("https://chaturbate.com/alice/") is True
    # bob is offline per the per-slug stub.
    assert is_live("https://chaturbate.com/bob/") is False
    assert per_slug_calls == ["alice", "bob"]


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
                 "current_show": "public",
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
                     poll_minutes: int = 10,
                     entries_path: Any = None) -> str:
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


def test_tv_list_passes_unsorted_to_end_directory(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: tv_list MUST close with unsorted=True or Kodi
    alpha-sorts the labels and ``[P01]`` ends up above ``[P17]``,
    which is the OPPOSITE of "highest priority on top." User report:
    "the shorting on tv is in reverse like the lows are at the top
    and highs at the bottom love is that by design" - it was not.
    """
    from resources.lib import tv_store
    from resources.lib.cb_models import TVEntry

    tv_path = tmp_path / "tv.json"
    tv_store.save(tv_path, [
        TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=10),
    ])

    fake_kh = _patch_kodi_helpers(monkeypatch)

    actions = _import()
    actions.tv_list(handle=42, store_path=tv_path)

    fake_kh.end_directory.assert_called_once()
    call = fake_kh.end_directory.call_args
    assert call.kwargs.get("unsorted") is True, (
        f"tv_list end_directory must pass unsorted=True, got kwargs={call.kwargs!r}"
    )


def test_tv_list_rows_carry_ctxmenu_with_edit_and_remove(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: tv_list rows MUST carry the state-aware ctxmenu so
    the user can right-click an entry and Edit Priority or Remove it
    from the list. Earlier shipped versions called ``add_play_item``
    without ``ctx_items``, dropping the right-click parity.
    """
    from resources.lib import tv_store
    from resources.lib.cb_models import TVEntry

    tv_path = tmp_path / "tv.json"
    tv_store.save(tv_path, [
        TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=10),
    ])

    fake_kh = _patch_kodi_helpers(monkeypatch)

    actions = _import()
    actions.tv_list(handle=42, store_path=tv_path)

    play_calls = fake_kh.add_play_item.call_args_list
    assert play_calls, "no play rows added"
    # Each row should pass ctx_items containing the TV-mgmt actions.
    ctx_items_kw = play_calls[0].kwargs.get("ctx_items")
    assert ctx_items_kw is not None, (
        "tv_list row must pass ctx_items (regression from )"
    )
    labels = [item[0] for item in ctx_items_kw]
    assert any("Edit TV Priority" in lab for lab in labels)
    assert any("Remove from TV" in lab for lab in labels)


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


def test_tv_add_confirm_no_aborts_without_writing(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """v0.7.28: when no priority is preset (the ctxmenu path) tv_add
    fires a yes/no confirm dialog FIRST. The user pressing No or
    Back must abort cleanly -- no entry written, no priority numpad
    fired. Bulletproof back-out for accidental clicks.
    """
    from resources.lib import tv_store

    actions = _import()
    tv_path = tmp_path / "tv.json"
    confirm_calls: list[str] = []

    def fake_confirm(name: str) -> bool:
        confirm_calls.append(name)
        return False  # user said No / Back

    actions.tv_add(
        handle=42, slug="alice", name="alice",
        url="https://chaturbate.com/alice/",
        priority="",  # ctxmenu path -- no preset priority
        store_path=tv_path,
        confirm_func=fake_confirm,
    )
    assert confirm_calls == ["alice"], (
        f"confirm dialog must fire once, got {confirm_calls!r}"
    )
    entries = tv_store.load(tv_path)
    assert entries == [], (
        f"No-on-confirm must NOT add an entry, got {entries!r}"
    )


def test_tv_add_confirm_yes_proceeds_to_priority_prompt(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """When the confirm dialog returns Yes, the priority numpad still
    fires and the entry gets added with whatever priority the user
    enters (or the default if Kodi's numpad returned the default)."""
    from resources.lib import tv_store

    actions = _import()
    tv_path = tmp_path / "tv.json"

    # Simulate Kodi's numpad returning '12'.
    captured: dict[str, Any] = {}

    class _D:
        def yesno(self, h: str, m: str, **kw: Any) -> bool:
            captured["yesno"] = (h, m)
            return True
        def numeric(self, t: int, h: str, d: str = "") -> str:
            return "12"
        def notification(self, *a: Any, **kw: Any) -> None: ...
        def input(self, *a: Any, **kw: Any) -> str:
            return ""
        def ok(self, *a: Any, **kw: Any) -> bool:
            return True

    kodi_mocks["xbmcgui"].Dialog = _D
    actions.tv_add(
        handle=42, slug="alice", name="alice",
        url="https://chaturbate.com/alice/",
        priority="",
        store_path=tv_path,
    )
    entries = tv_store.load(tv_path)
    assert len(entries) == 1
    assert entries[0].priority == 12


def test_tv_add_with_preset_priority_skips_confirm(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """When priority is passed via query string (the verb is invoked
    from somewhere that already knows the priority -- e.g., a
    pre-baked button), the confirm dialog is skipped. Backward-compat
    with explicit-priority callers."""
    from resources.lib import tv_store

    actions = _import()
    tv_path = tmp_path / "tv.json"
    confirm_calls: list[str] = []

    def fake_confirm(name: str) -> bool:
        confirm_calls.append(name)
        return False

    actions.tv_add(
        handle=42, slug="alice", name="alice",
        url="https://chaturbate.com/alice/",
        priority="9",
        store_path=tv_path,
        confirm_func=fake_confirm,
    )
    assert confirm_calls == [], (
        f"explicit-priority path must skip confirm: {confirm_calls!r}"
    )
    entries = tv_store.load(tv_path)
    assert len(entries) == 1
    assert entries[0].priority == 9


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


# --------------------------------------------------------------------------- #
# search (the prompt-and-redirect shim)
# v0.7.51: pin the spinner-fix behavior. The search prompt is invoked as a
# directory click (`plugin://...?mode=search_prompt`); Kodi waits for an
# endOfDirectory call before the dialog input can complete. Without it,
# Kodi logs "GetDirectory failed" and the dialog never gets a chance to
# return a value to the shim.
# --------------------------------------------------------------------------- #


def test_search_empty_query_still_closes_directory(
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """User cancels dialog (or types nothing): we still close the
    directory so Kodi clears its busy spinner."""
    # Default _Dialog.input returns "" -> empty query path
    actions = _import()
    actions.search(handle=42)
    # Container.Update should NOT be fired for empty query
    builtins = [
        call.args[0] for call in
        kodi_mocks["xbmc"].executebuiltin.call_args_list
    ]
    assert not any("Container.Update" in b for b in builtins), \
        f"Container.Update fired on empty query: {builtins}"
    # endOfDirectory MUST be called
    kodi_mocks["xbmcplugin"].endOfDirectory.assert_called_with(
        42, succeeded=False
    )


def test_search_with_query_invokes_search_view_directly(
    kodi_mocks: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User types a query and submits: shim calls
    ``browse_views.search_view(handle, query=...)`` DIRECTLY on the
    current handle to populate the search_prompt directory in place.

    v0.7.53: Container.Update was a misdirection. The plugin handler
    is invoked as a directory load on a real handle; Kodi expects
    that handle to be populated. Returning without populating it
    triggers Kodi's "GetDirectory failed" error AND drops the user
    back at parent - regardless of whether endOfDirectory was called
    or what builtins we queued. Live trace 2026-05-15
    16:53:21: Container.Update fired (logged), but next dispatch
    was still mode='' (main_menu), AND kodi.log still had the
    GetDirectory failed error. The race wasn't winnable.

    Calling search_view directly with the same handle is the only
    reliable pattern: dialog returns the query, shim populates THIS
    directory, Kodi shows results.
    """
    # Override the dialog to return a non-empty query
    class _DialogWithQuery:
        def input(self, *args: Any, **kwargs: Any) -> str:
            return "model_a"

        def notification(self, *args: Any, **kwargs: Any) -> None:
            return None

    monkeypatch.setattr(kodi_mocks["xbmcgui"], "Dialog", _DialogWithQuery)

    # Patch browse_views.search_view to a spy so we can verify the call
    spy = MagicMock()
    monkeypatch.setattr(
        "resources.lib.browse_views.search_view", spy
    )

    actions = _import()
    actions.search(handle=42)

    # search_view must be called with handle=42 and the query
    spy.assert_called_once()
    call_kwargs = spy.call_args.kwargs
    assert call_kwargs.get("handle") == 42, \
        f"search_view called with wrong handle: {spy.call_args}"
    assert call_kwargs.get("query") == "model_a", \
        f"search_view called with wrong query: {spy.call_args}"

    # Container.Update must NOT be fired - we're populating in-place
    builtins = [
        call.args[0] for call in
        kodi_mocks["xbmc"].executebuiltin.call_args_list
    ]
    assert not any("Container.Update" in b for b in builtins), \
        f"Container.Update should not fire when invoking search_view directly: {builtins}"

    # endOfDirectory: search_view itself handles the listing close,
    # so the shim should NOT call _close_directory_handle on submit.
    assert not kodi_mocks["xbmcplugin"].endOfDirectory.called, \
        ("endOfDirectory must not be called by the shim on submit - "
         "search_view itself closes the listing")
