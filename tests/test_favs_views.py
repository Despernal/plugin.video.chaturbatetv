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


@pytest.fixture(autouse=True)
def _isolate_disk_cache(tmp_path: Path,
                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets its own disk-cache path so the bulk-fetch cache
    doesn't leak from one test into the next. Without this, the first
    test that runs ``_bulk_live_slugs`` writes the live-slugs set to a
    real path on the developer's filesystem and every subsequent test
    sees a 'disk-cache HIT' before its fetch_func ever fires.
    """
    import resources.lib.favs_views as fv
    cache_path = tmp_path / "test_bulk_live_cache.json"
    monkeypatch.setattr(fv, "_bulk_disk_cache_path", lambda: cache_path)


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


def test_favs_menu_shows_online_offline_drilldowns(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Top-level shows Online/Offline drill-downs WITHOUT a remote fetch
    (large favorites lists were locking up the UI on ).
    """
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

    fetch_calls: list[str] = []

    def fetch(url: str, **_kw: Any) -> str:
        fetch_calls.append(url)
        return ""

    fv.favs_menu(handle=42, store_path=favs_path, fetch_func=fetch)

    gui = kodi_mocks["xbmcgui"]
    labels = [
        call.kwargs.get("label") or (call.args[0] if call.args else "")
        for call in gui.ListItem.call_args_list
    ]
    assert any("Online" in lab for lab in labels)
    assert any("Offline" in lab for lab in labels)
    # Critical: NO network call on the top-level menu open.
    assert fetch_calls == [], f"favs_menu must not network: {fetch_calls!r}"


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
    # Drill-downs still appear; user just sees "0 total".
    assert any("Online" in lab for lab in labels)
    assert any("Offline" in lab for lab in labels)


# --------------------------------------------------------------------------- #
# online_favs_view / offline_favs_view
# --------------------------------------------------------------------------- #


def test_online_favs_view_renders_only_live(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    fv = _import()
    fv._bulk_cache_clear()
    favs_path = tmp_path / "favs.json"
    _write_favs(favs_path, [
        Favorite(name="alice", slug="alice", url="https://chaturbate.com/alice/",
                 gender=Gender.FEMALE),
        Favorite(name="bob", slug="bob", url="https://chaturbate.com/bob/",
                 gender=Gender.MALE),
    ])
    fetch = _bulk_fetch([["alice"]])

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
    fv._bulk_cache_clear()
    favs_path = tmp_path / "favs.json"
    _write_favs(favs_path, [
        Favorite(name="alice", slug="alice", url="https://chaturbate.com/alice/",
                 gender=Gender.FEMALE),
        Favorite(name="bob", slug="bob", url="https://chaturbate.com/bob/",
                 gender=Gender.MALE),
    ])
    fetch = _bulk_fetch([["alice"]])

    fv.offline_favs_view(handle=42, store_path=favs_path, fetch_func=fetch)

    urls = _added_urls(kodi_mocks["xbmcplugin"])
    play_urls = [u for u in urls if "mode=playvid" in u]
    assert len(play_urls) == 1
    assert "slug=bob" in play_urls[0]


# --------------------------------------------------------------------------- #
# Content-type declaration: favs views must declare the directory as
# 'videos' so Kodi shows InfoWall / MediaList / Wide view modes (thumb on
# right, plot on left).
# --------------------------------------------------------------------------- #


def test_favs_menu_sets_content_videos(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    fv = _import()
    favs_path = tmp_path / "favs.json"
    fv.favs_menu(handle=42, store_path=favs_path,
                 fetch_func=_live_fetch(set()))
    kodi_mocks["xbmcplugin"].setContent.assert_called_once_with(42, "videos")


def test_online_favs_view_sets_content_videos(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    fv = _import()
    favs_path = tmp_path / "favs.json"
    _write_favs(favs_path, [
        Favorite(name="alice", slug="alice", url="https://chaturbate.com/alice/",
                 gender=Gender.FEMALE),
    ])
    fv.online_favs_view(handle=42, store_path=favs_path,
                        fetch_func=_live_fetch({"alice"}))
    kodi_mocks["xbmcplugin"].setContent.assert_called_once_with(42, "videos")


def test_offline_favs_view_sets_content_videos(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    fv = _import()
    favs_path = tmp_path / "favs.json"
    _write_favs(favs_path, [
        Favorite(name="bob", slug="bob", url="https://chaturbate.com/bob/",
                 gender=Gender.MALE),
    ])
    fv.offline_favs_view(handle=42, store_path=favs_path,
                         fetch_func=_live_fetch(set()))
    kodi_mocks["xbmcplugin"].setContent.assert_called_once_with(42, "videos")


# --------------------------------------------------------------------------- #
# Lesson 11 - bulk-fetch live slugs (10 calls instead of 1224)
# --------------------------------------------------------------------------- #


def _bulk_fetch(live_slugs_per_page: list[list[str]]) -> Any:
    """fetch_func that responds to BOTH the affiliate-onlinerooms URL
    AND the legacy paginated room-list URL.

    Tests pass a list-of-lists for the legacy paginated path (one inner
    list per page). The affiliate endpoint flattens them all into a
    single response since it's a one-call API.
    """
    state = {"page": 0}

    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str = "GET") -> str:
        if "/affiliates/api/onlinerooms/" in url:
            # Single-call affiliate endpoint: flatten all pages into
            # one JSON array.
            all_slugs = [s for page in live_slugs_per_page for s in page]
            return json.dumps([
                {"username": s, "slug": s, "gender": "f",
                 "num_users": 100, "image_url": f"https://thumb/{s}.jpg",
                 "room_subject": f"hi from {s}"}
                for s in all_slugs
            ])
        if "/api/ts/roomlist/" not in url:
            # AJAX status endpoint - default to "offline" so the
            # fallback path doesn't accidentally pick a slug as live.
            return json.dumps({
                "success": True, "url": "", "room_status": "offline",
                "hidden_message": "", "cmaf_edge": False,
            })
        idx = state["page"]
        state["page"] += 1
        if idx >= len(live_slugs_per_page):
            return json.dumps({"rooms": [], "total_count": 0,
                              "all_rooms_count": 0})
        slugs = live_slugs_per_page[idx]
        return json.dumps({
            "rooms": [
                {"username": s, "gender": "f", "num_users": 100,
                 "label": "public"}
                for s in slugs
            ],
            "total_count": sum(len(p) for p in live_slugs_per_page),
            "all_rooms_count": sum(len(p) for p in live_slugs_per_page),
        })
    return fetch


def test_online_favs_view_uses_bulk_path_for_live_intersection(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """The bulk-fetch happens on Online/Offline drill-down (NOT on the
    top-level menu). Verifies the room-list intersection still works.
    """
    fv = _import()
    fv._bulk_cache_clear()
    favs_path = tmp_path / "favs.json"
    _write_favs(favs_path, [
        Favorite(name="alice", slug="alice", url="https://chaturbate.com/alice/",
                 gender=Gender.FEMALE),
        Favorite(name="bob", slug="bob", url="https://chaturbate.com/bob/",
                 gender=Gender.MALE),
        Favorite(name="cara", slug="cara", url="https://chaturbate.com/cara/",
                 gender=Gender.FEMALE),
    ])
    fetch = _bulk_fetch([["alice", "cara"]])
    fv.online_favs_view(handle=42, store_path=favs_path, fetch_func=fetch)

    urls = _added_urls(kodi_mocks["xbmcplugin"])
    play_urls = [u for u in urls if "mode=playvid" in u]
    play_slugs = {u.split("slug=")[1].split("&")[0] for u in play_urls}
    assert play_slugs == {"alice", "cara"}


def test_bulk_live_slugs_uses_limit_100_per_page(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Regression: Chaturbate's room-list API returns HTTP 400 with
    'Ensure this value is less than or equal to 100' for any limit > 100.
    A previous version sent limit=500 and silently failed every bulk fetch,
    which broke Online Favorites entirely.
    """
    fv = _import()
    fv._bulk_cache_clear()
    captured_urls: list[str] = []

    def fetch(url: str, **_kw: Any) -> str:
        captured_urls.append(url)
        # First page short to stop the walk.
        return json.dumps({
            "rooms": [{"username": "alice", "gender": "f",
                       "num_users": 1, "label": "public"}],
            "total_count": 1, "all_rooms_count": 1,
        })

    fv._bulk_live_slugs(fetch)
    assert captured_urls, "no fetch was made"
    # No URL should request more than 100 results per page.
    for url in captured_urls:
        assert "limit=100" in url or "limit=" not in url, (
            f"limit must be <= 100, got {url!r}"
        )
        assert "limit=500" not in url
        assert "limit=200" not in url


def test_bulk_live_slugs_paginates_until_short_page(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Pages of full size (100/page, the API max) keep going; first
    short page stops the walk."""
    fv = _import()
    fv._bulk_cache_clear()
    full_page = [f"user{i:04d}" for i in range(100)]
    short_page = ["last1", "last2"]
    fetch = _bulk_fetch([full_page, short_page])
    out = fv._bulk_live_slugs(fetch)
    assert out is not None
    assert len(out) == 102
    assert "user0000" in out
    assert "last2" in out


def test_bulk_live_slugs_returns_none_on_total_failure(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """No rooms at all -> None so caller falls back to per-slug AJAX."""
    fv = _import()
    fv._bulk_cache_clear()

    def fetch(url: str, **_kw: Any) -> str:
        # Return empty rooms (parse_roomlist sees 0 models).
        return json.dumps({"rooms": [], "total_count": 0,
                          "all_rooms_count": 0})

    out = fv._bulk_live_slugs(fetch)
    assert out is None


def test_bulk_live_slugs_caches_results_for_60s(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Re-entry within 30s reuses the cache (no re-fetch)."""
    fv = _import()
    fv._bulk_cache_clear()
    fetch_calls = {"n": 0}

    def fetch(url: str, **_kw: Any) -> str:
        fetch_calls["n"] += 1
        # Affiliate endpoint shape: flat JSON array.
        return json.dumps([
            {"username": "alice", "slug": "alice", "gender": "f",
             "num_users": 1, "image_url": "https://thumb/alice.jpg",
             "room_subject": "hi"}
        ])

    fv._bulk_live_slugs(fetch, now_func=lambda: 100.0)
    fv._bulk_live_slugs(fetch, now_func=lambda: 120.0)  # within 30s TTL
    assert fetch_calls["n"] == 1  # cached, no re-fetch


def test_bulk_live_slugs_cache_expires_after_ttl(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Cache TTL is 30s - short so re-entering Online Favorites picks
    up newly-online models. The single-call affiliate endpoint makes
    each fresh fetch fast, so we don't need a long cache."""
    fv = _import()
    fv._bulk_cache_clear()
    fetch_calls = {"n": 0}

    def fetch(url: str, **_kw: Any) -> str:
        fetch_calls["n"] += 1
        return json.dumps([
            {"username": "alice", "slug": "alice", "gender": "f",
             "num_users": 1, "image_url": "", "room_subject": ""}
        ])

    fv._bulk_live_slugs(fetch, now_func=lambda: 100.0)
    fv._bulk_live_slugs(fetch, now_func=lambda: 200.0)  # past 30s TTL
    assert fetch_calls["n"] == 2


def test_online_favs_view_renders_all_entries_no_pagination(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """v0.7.30 dropped pagination on online/offline favs. A 120-fav
    library renders ALL 120 entries in one directory and emits NO
    "Next page" link. Bulk-live cache + meta DB make per-row work
    cheap enough that the 50/page split from the Pi-era is just
    extra clicks.
    """
    fv = _import()
    fv._bulk_cache_clear()
    favs_path = tmp_path / "favs.json"
    favs = [
        Favorite(
            name=f"user{i:04d}",
            slug=f"user{i:04d}",
            url=f"https://chaturbate.com/user{i:04d}/",
            gender=Gender.FEMALE,
        )
        for i in range(120)
    ]
    _write_favs(favs_path, favs)
    fetch = _bulk_fetch([[f"user{i:04d}" for i in range(120)]])

    fv.online_favs_view(handle=42, store_path=favs_path, fetch_func=fetch)

    urls = _added_urls(kodi_mocks["xbmcplugin"])
    play_urls = [u for u in urls if "mode=playvid" in u]
    assert len(play_urls) == 120, (
        f"all 120 favs should render, got {len(play_urls)}"
    )
    # No "Next page" link on either favs view post-v0.7.30.
    next_links = [
        u for u in urls
        if ("mode=favs_online" in u or "mode=favs_offline" in u)
        and "page=" in u
    ]
    assert next_links == [], (
        f"no pagination -> no Next-page link, got {next_links!r}"
    )


def test_online_favs_view_ignores_stale_page_param(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Old "Next page" links from pre-0.7.30 builds may still be in
    Kodi's history -- a click sends ``page=3`` to the handler.
    Post-removal the param must be silently ignored, NOT crash. All
    favs still render."""
    fv = _import()
    fv._bulk_cache_clear()
    favs_path = tmp_path / "favs.json"
    favs = [
        Favorite(
            name=f"user{i:04d}",
            slug=f"user{i:04d}",
            url=f"https://chaturbate.com/user{i:04d}/",
            gender=Gender.FEMALE,
        )
        for i in range(40)
    ]
    _write_favs(favs_path, favs)
    fetch = _bulk_fetch([[f"user{i:04d}" for i in range(40)]])

    # Stray page=3 from a stale link must not crash.
    fv.online_favs_view(handle=42, store_path=favs_path, fetch_func=fetch,
                        page="3")

    urls = _added_urls(kodi_mocks["xbmcplugin"])
    play_urls = [u for u in urls if "mode=playvid" in u]
    assert len(play_urls) == 40, (
        f"stale page param must be ignored, all 40 still render, "
        f"got {len(play_urls)}"
    )


def test_bulk_live_slugs_drops_legacy_disk_cache_on_entry(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """Versions 0.6.3 - 0.6.5 wrote a slug-only disk cache to
    ``addon_data/.../bulk_live_cache.json``. The new code does NOT
    re-use that cache (model data was never persisted, so a hit would
    render online favs without thumbnails). Any leftover file should be
    deleted on first call so it doesn't pile up.
    """
    fv = _import()
    fv._bulk_cache_clear()
    # Plant a stale legacy cache file at the path the test fixture
    # has redirected the cache helper to.
    import resources.lib.favs_views as fv_mod
    cache_path = fv_mod._bulk_disk_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"timestamp": 9999999999.0,
                    "slugs": ["zoe", "alice"]}),
        encoding="utf-8",
    )
    assert cache_path.exists()

    def fetch(url: str, **_kw: Any) -> str:
        return json.dumps([
            {"username": "fresh", "slug": "fresh", "gender": "f",
             "num_users": 1, "image_url": "", "room_subject": ""}
        ])

    out = fv._bulk_live_slugs(fetch)
    # Live set comes from the FRESH fetch, not the stale disk cache.
    assert out == {"fresh"}
    # And the legacy cache file got cleaned up.
    assert not cache_path.exists()


def test_online_favs_view_treats_all_as_offline_when_bulk_fails(
    tmp_path: Path,
    kodi_mocks: dict[str, MagicMock],
) -> None:
    """If the bulk room-list fetch fails (site outage / rate limit),
    show NOTHING in Online and EVERYTHING in Offline rather than blocking
    the UI on per-slug AJAX. The user can still edit/remove offline favs
    and the next time they re-enter (5min cache window) we retry bulk.
    """
    fv = _import()
    fv._bulk_cache_clear()
    favs_path = tmp_path / "favs.json"
    _write_favs(favs_path, [
        Favorite(name="alice", slug="alice", url="https://chaturbate.com/alice/",
                 gender=Gender.FEMALE),
    ])
    # _live_fetch returns AJAX-shaped JSON which has no "rooms" key, so
    # parse_roomlist returns empty and the bulk path returns None.
    fetch = _live_fetch({"alice"})
    fv.online_favs_view(handle=42, store_path=favs_path, fetch_func=fetch)

    urls = _added_urls(kodi_mocks["xbmcplugin"])
    play_urls = [u for u in urls if "mode=playvid" in u]
    # Nothing is reported as online when bulk fails.
    assert play_urls == []
