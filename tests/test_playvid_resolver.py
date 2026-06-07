"""Tests for resources.lib.playvid_resolver - slug -> playable ListItem.

The resolver bridges three things:

1. ``cb_resolve.resolve(slug, fetch_func)`` -> Resolution.
2. ``hls_proxy.start_proxy(stream_url, room_url)`` -> ProxyHandle.
3. ``xbmcgui.ListItem`` with the Matrix+ ISA properties:
    - ``inputstream`` = ``inputstream.adaptive`` (NOT old key
      ``inputstreamaddon`` which silently fails on Nexus+).
    - ``inputstream.adaptive.manifest_type`` = ``hls``.
    - ``inputstream.adaptive.stream_headers`` = ``UA & Referer`` urlencoded.
    - ``inputstream.adaptive.manifest_headers`` = same string.

Tests inject the resolve-callback and the proxy-start-callback so we
never actually hit the network here. xbmcgui is mocked.
"""
from __future__ import annotations

import sys
import urllib.parse
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from resources.lib.cb_models import Gender
from resources.lib.cb_resolve import Resolution


# --------------------------------------------------------------------------- #
# Mock xbmcgui.ListItem that records every setProperty call
# --------------------------------------------------------------------------- #


class _FakeListItem:
    def __init__(self, label: str = "", **_kwargs: Any) -> None:
        self.label = label
        self._props: dict[str, str] = {}
        self._art: dict[str, str] = {}
        self._info: dict[str, dict[str, Any]] = {}
        self._path: str | None = None
        self._mime: str | None = None
        self._content_lookup: bool | None = None

    def setProperty(self, key: str, value: str) -> None:
        self._props[key] = value

    def getProperty(self, key: str) -> str:
        return self._props.get(key, "")

    def setArt(self, art: dict[str, str]) -> None:
        self._art.update(art)

    def setInfo(self, typ: str, info: dict[str, Any]) -> None:
        self._info[typ] = info

    def setPath(self, path: str) -> None:
        self._path = path

    def setMimeType(self, mime: str) -> None:
        self._mime = mime

    def setContentLookup(self, enabled: bool) -> None:
        self._content_lookup = enabled


@pytest.fixture
def mock_xbmcgui(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    fake = MagicMock()
    fake.ListItem = _FakeListItem  # the class, not an instance
    fake.NOTIFICATION_INFO = "info"
    monkeypatch.setitem(sys.modules, "xbmcgui", fake)
    sys.modules.pop("resources.lib.playvid_resolver", None)
    yield fake


def _import_resolver() -> Any:
    import resources.lib.playvid_resolver as mod
    return mod


# --------------------------------------------------------------------------- #
# Helpers for the injected callbacks
# --------------------------------------------------------------------------- #


def _live_resolution(slug: str = "alice") -> Resolution:
    return Resolution(
        is_live=True,
        hls_source="https://edge42.live.mmcdn.com/hls/abc/master.m3u8",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (iPad; CPU OS 8_1 like Mac OS X) "
                "AppleWebKit/600.1.4 (KHTML, like Gecko) "
                "Version/8.0 Mobile/12B410 Safari/600.1.4"
            ),
            "Referer": f"https://chaturbate.com/{slug}/",
        },
        gender=Gender.FEMALE,
    )


def _offline_resolution() -> Resolution:
    return Resolution(
        is_live=False,
        hls_source=None,
        headers={
            "User-Agent": "ipad",
            "Referer": "https://chaturbate.com/ghost/",
        },
        gender=Gender.UNKNOWN,
    )


class _FakeProxyHandle:
    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = 39191
        self.master_url = "http://127.0.0.1:39191/master.m3u8"
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_resolve_live_slug_returns_listitem_with_proxy_url(
    mock_xbmcgui: MagicMock,
) -> None:
    resolver = _import_resolver()
    handle = _FakeProxyHandle()

    result = resolver.resolve_to_listitem(
        slug="alice",
        name="alice",
        resolve_func=lambda s: _live_resolution(s),
        start_proxy_func=lambda stream_url, room_url: handle,
    )

    assert result.success is True
    assert result.listitem is not None
    # Path is set to the proxy master URL, NOT the raw chaturbate URL.
    assert result.listitem._path == handle.master_url
    # Sanity: we did NOT leak the raw upstream URL anywhere on the item.
    assert "edge42.live.mmcdn.com" not in (result.listitem._path or "")


def test_resolve_live_slug_sets_isa_properties_matrix_plus(
    mock_xbmcgui: MagicMock,
) -> None:
    """Phase 4b critical: the Matrix+ ``inputstream`` key must be set;
    the old ``inputstreamaddon`` key silently fails on Nexus+."""
    resolver = _import_resolver()

    result = resolver.resolve_to_listitem(
        slug="alice",
        name="alice",
        resolve_func=lambda s: _live_resolution(s),
        start_proxy_func=lambda *_a, **_kw: _FakeProxyHandle(),
    )
    li = result.listitem
    assert li is not None
    assert li.getProperty("inputstream") == "inputstream.adaptive"
    # And explicitly: the OLD key must NOT be set (regression guard).
    assert li.getProperty("inputstreamaddon") == ""


def test_resolve_live_slug_applies_max_resolution_when_set(
    mock_xbmcgui: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the user picks a max_resolution cap, the ListItem must
    carry ``inputstream.adaptive.max_resolution = WIDTHxHEIGHT`` so ISA
    refuses to upshift past that variant. Empty cap (``auto``) means
    the property is NOT set at all so ISA picks unconstrained.
    """
    import resources.lib.addon_settings as addon_settings_mod
    monkeypatch.setattr(addon_settings_mod, "max_resolution", lambda: "1280x720")

    resolver = _import_resolver()
    result = resolver.resolve_to_listitem(
        slug="alice",
        name="alice",
        resolve_func=lambda s: _live_resolution(s),
        start_proxy_func=lambda *_a, **_kw: _FakeProxyHandle(),
    )
    li = result.listitem
    assert li is not None
    assert li.getProperty("inputstream.adaptive.max_resolution") == "1280x720"


def test_resolve_live_slug_omits_max_resolution_when_auto(
    mock_xbmcgui: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``auto`` cap = empty string from settings = property NOT set.
    Without this, ISA would interpret an empty string as an invalid cap
    and refuse to play."""
    import resources.lib.addon_settings as addon_settings_mod
    monkeypatch.setattr(addon_settings_mod, "max_resolution", lambda: "")

    resolver = _import_resolver()
    result = resolver.resolve_to_listitem(
        slug="alice",
        name="alice",
        resolve_func=lambda s: _live_resolution(s),
        start_proxy_func=lambda *_a, **_kw: _FakeProxyHandle(),
    )
    li = result.listitem
    assert li is not None
    assert li.getProperty("inputstream.adaptive.max_resolution") == ""


def test_resolve_live_slug_sets_manifest_type_hls(mock_xbmcgui: MagicMock) -> None:
    resolver = _import_resolver()
    result = resolver.resolve_to_listitem(
        slug="alice",
        name="alice",
        resolve_func=lambda s: _live_resolution(s),
        start_proxy_func=lambda *_a, **_kw: _FakeProxyHandle(),
    )
    li = result.listitem
    assert li is not None
    assert li.getProperty("inputstream.adaptive.manifest_type") == "hls"


def test_resolve_live_slug_sets_stream_and_manifest_headers(
    mock_xbmcgui: MagicMock,
) -> None:
    """Both stream_headers and manifest_headers must be set, with the
    same urlencoded string carrying the iPad UA + Referer."""
    resolver = _import_resolver()
    result = resolver.resolve_to_listitem(
        slug="alice",
        name="alice",
        resolve_func=lambda s: _live_resolution(s),
        start_proxy_func=lambda *_a, **_kw: _FakeProxyHandle(),
    )
    li = result.listitem
    assert li is not None
    stream_h = li.getProperty("inputstream.adaptive.stream_headers")
    manifest_h = li.getProperty("inputstream.adaptive.manifest_headers")
    assert stream_h
    assert stream_h == manifest_h
    # The header string is urlencoded ``key=val&key=val``.
    parsed = dict(urllib.parse.parse_qsl(stream_h))
    assert "iPad" in parsed.get("User-Agent", "")
    assert parsed.get("Referer") == "https://chaturbate.com/alice/"


def test_resolve_offline_slug_returns_failure(mock_xbmcgui: MagicMock) -> None:
    """If the room is offline, no proxy is started; success=False."""
    resolver = _import_resolver()
    started: list[Any] = []

    def starter(*args: Any, **kwargs: Any) -> _FakeProxyHandle:
        started.append((args, kwargs))
        return _FakeProxyHandle()

    result = resolver.resolve_to_listitem(
        slug="ghost",
        name="ghost",
        resolve_func=lambda _s: _offline_resolution(),
        start_proxy_func=starter,
    )
    assert result.success is False
    assert result.listitem is None
    assert result.proxy is None
    assert started == []  # proxy was never started


def test_resolve_passes_correct_args_to_proxy(mock_xbmcgui: MagicMock) -> None:
    """start_proxy is called with stream_url + room_url from the resolution."""
    resolver = _import_resolver()
    captured: dict[str, str] = {}

    def starter(stream_url: str, room_url: str) -> _FakeProxyHandle:
        captured["stream_url"] = stream_url
        captured["room_url"] = room_url
        return _FakeProxyHandle()

    resolver.resolve_to_listitem(
        slug="alice",
        name="alice",
        resolve_func=lambda s: _live_resolution(s),
        start_proxy_func=starter,
    )
    assert captured["stream_url"] == "https://edge42.live.mmcdn.com/hls/abc/master.m3u8"
    assert captured["room_url"] == "https://chaturbate.com/alice/"


def test_listitem_label_uses_provided_name(mock_xbmcgui: MagicMock) -> None:
    resolver = _import_resolver()
    result = resolver.resolve_to_listitem(
        slug="alice",
        name="Alice the Cam Star",
        resolve_func=lambda s: _live_resolution(s),
        start_proxy_func=lambda *_a, **_kw: _FakeProxyHandle(),
    )
    li = result.listitem
    assert li is not None
    assert li.label == "Alice the Cam Star"


def test_resolve_propagates_resolve_func_exception(mock_xbmcgui: MagicMock) -> None:
    """If the resolve-callback raises, return failure (don't crash)."""
    resolver = _import_resolver()

    def bad_resolve(_slug: str) -> Resolution:
        raise OSError("network unreachable")

    result = resolver.resolve_to_listitem(
        slug="alice",
        name="alice",
        resolve_func=bad_resolve,
        start_proxy_func=lambda *_a, **_kw: _FakeProxyHandle(),
    )
    assert result.success is False
    assert result.listitem is None


def test_resolve_returns_proxy_handle_for_lifecycle_management(
    mock_xbmcgui: MagicMock,
) -> None:
    """Caller needs the ProxyHandle to call .stop() when playback ends."""
    resolver = _import_resolver()
    fake_handle = _FakeProxyHandle()
    result = resolver.resolve_to_listitem(
        slug="alice",
        name="alice",
        resolve_func=lambda s: _live_resolution(s),
        start_proxy_func=lambda *_a, **_kw: fake_handle,
    )
    assert result.proxy is fake_handle


def test_listitem_path_is_proxy_master_url_not_raw_hls(
    mock_xbmcgui: MagicMock,
) -> None:
    """Critical: the URL ISA sees is the proxys 127.0.0.1 URL, never the
    raw hls_source - otherwise we lose the header injection layer.
    """
    resolver = _import_resolver()
    fake_handle = _FakeProxyHandle()
    result = resolver.resolve_to_listitem(
        slug="alice",
        name="alice",
        resolve_func=lambda s: _live_resolution(s),
        start_proxy_func=lambda *_a, **_kw: fake_handle,
    )
    li = result.listitem
    assert li is not None
    assert li._path == "http://127.0.0.1:39191/master.m3u8"


def test_listitem_sets_isplayable(mock_xbmcgui: MagicMock) -> None:
    """ISA needs IsPlayable=true on the resolved ListItem."""
    resolver = _import_resolver()
    result = resolver.resolve_to_listitem(
        slug="alice",
        name="alice",
        resolve_func=lambda s: _live_resolution(s),
        start_proxy_func=lambda *_a, **_kw: _FakeProxyHandle(),
    )
    li = result.listitem
    assert li is not None
    assert li.getProperty("IsPlayable") == "true"


def test_listitem_sets_mime_type_hls(mock_xbmcgui: MagicMock) -> None:
    """MIME type hint helps Kodi auto-route to ISA without sniffing."""
    resolver = _import_resolver()
    result = resolver.resolve_to_listitem(
        slug="alice",
        name="alice",
        resolve_func=lambda s: _live_resolution(s),
        start_proxy_func=lambda *_a, **_kw: _FakeProxyHandle(),
    )
    li = result.listitem
    assert li is not None
    assert li._mime == "application/vnd.apple.mpegurl"


def test_resolve_returns_failure_when_proxy_start_raises(
    mock_xbmcgui: MagicMock,
) -> None:
    """If start_proxy crashes (port collision, OS error), bubble up as
    a clean failure result rather than letting the addon UI explode."""
    resolver = _import_resolver()

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise OSError("address already in use")

    result = resolver.resolve_to_listitem(
        slug="alice",
        name="alice",
        resolve_func=lambda s: _live_resolution(s),
        start_proxy_func=boom,
    )
    assert result.success is False
    assert result.listitem is None
    assert result.proxy is None


def test_resolve_returns_failure_when_hls_source_missing(
    mock_xbmcgui: MagicMock,
) -> None:
    """A live=True but hls_source=None response is unplayable; treat the
    same as offline. Defends against an upstream parser regression."""
    resolver = _import_resolver()
    inconsistent = Resolution(
        is_live=True,
        hls_source=None,  # nonsense state, but defend against it anyway
        headers={"User-Agent": "ipad", "Referer": "https://chaturbate.com/x/"},
        gender=Gender.UNKNOWN,
    )
    started: list[Any] = []

    def starter(*args: Any, **kwargs: Any) -> _FakeProxyHandle:
        started.append((args, kwargs))
        return _FakeProxyHandle()

    result = resolver.resolve_to_listitem(
        slug="x",
        name="x",
        resolve_func=lambda _s: inconsistent,
        start_proxy_func=starter,
    )
    assert result.success is False
    assert started == []  # never tried to start the proxy


def test_listitem_label_falls_back_to_slug_when_name_empty(
    mock_xbmcgui: MagicMock,
) -> None:
    """If addon_actions.playvid passes name='', use the slug as label."""
    resolver = _import_resolver()
    result = resolver.resolve_to_listitem(
        slug="alice",
        name="",
        resolve_func=lambda s: _live_resolution(s),
        start_proxy_func=lambda *_a, **_kw: _FakeProxyHandle(),
    )
    li = result.listitem
    assert li is not None
    assert li.label == "alice"


def test_default_resolve_plumbs_cb_client_ajax_into_cb_resolve(
    mock_xbmcgui: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-resolve uses the AJAX endpoint (Lesson 3): it hands
    cb_resolve.resolve_ajax a callback that calls
    cb_client.fetch_room_status_json under the hood.
    """
    captured: dict[str, object] = {}

    import resources.lib.cb_client as cb_client
    import resources.lib.cb_resolve as cb_resolve

    def fake_fetch_status(slug: str, fetch_func: Any = None) -> dict[str, object]:
        captured["slug"] = slug
        return {
            "success": True,
            "url": "https://edge99-fake.live.mmcdn.com/hls/abc/llhls.m3u8",
            "room_status": "public",
        }

    def fake_resolve_ajax(slug: str, fetch_status_func: Any) -> Resolution:
        captured["status"] = fetch_status_func(slug)
        return _live_resolution(slug)

    monkeypatch.setattr(cb_client, "fetch_room_status_json", fake_fetch_status)
    monkeypatch.setattr(cb_resolve, "resolve_ajax", fake_resolve_ajax)

    resolver = _import_resolver()
    result = resolver.resolve_to_listitem(
        slug="alice",
        name="alice",
        # No resolve_func -> exercise _default_resolve.
        start_proxy_func=lambda *_a, **_kw: _FakeProxyHandle(),
    )
    assert result.success is True
    assert captured["slug"] == "alice"
    assert isinstance(captured["status"], dict)
    assert captured["status"]["room_status"] == "public"


# --------------------------------------------------------------------------- #
# v0.7.57: prefetch-husk regression (2026-06-07)
#
# start_proxy swallows prefetch failure and used to return a handle whose
# master is a 25-byte EXTM3U envelope. ISA sees zero streams -> Kodi error
# dialog. The resolver must detect prefetch_ok=False, tear the husk down,
# re-resolve ONCE (fresh edge session - CB's ajax can return a stale one
# right after a stream dies), and fail cleanly if it happens again.
# --------------------------------------------------------------------------- #


class _PrefetchAwareProxy(_FakeProxyHandle):
    def __init__(self, prefetch_ok: bool) -> None:
        super().__init__()
        self.prefetch_ok = prefetch_ok


def test_prefetch_fail_retries_resolve_once_and_succeeds(
    mock_xbmcgui: MagicMock,
) -> None:
    mod = _import_resolver()
    resolve_calls: list[str] = []

    def rf(slug: str) -> Resolution:
        resolve_calls.append(slug)
        return _live_resolution(slug)

    proxies = [_PrefetchAwareProxy(False), _PrefetchAwareProxy(True)]
    started: list[str] = []

    def sp(stream_url: str, room_url: str) -> Any:
        started.append(stream_url)
        return proxies[len(started) - 1]

    result = mod.resolve_to_listitem(
        "alice", "alice", resolve_func=rf, start_proxy_func=sp)
    assert result.success is True
    assert len(resolve_calls) == 2, "must re-resolve for a fresh edge session"
    assert len(started) == 2
    assert proxies[0].stopped is True, "husk proxy must be torn down"
    assert result.proxy is proxies[1]


def test_prefetch_fail_twice_returns_failure(mock_xbmcgui: MagicMock) -> None:
    mod = _import_resolver()
    proxies = [_PrefetchAwareProxy(False), _PrefetchAwareProxy(False)]
    started: list[str] = []

    def sp(stream_url: str, room_url: str) -> Any:
        started.append(stream_url)
        return proxies[len(started) - 1]

    result = mod.resolve_to_listitem(
        "alice", "alice",
        resolve_func=lambda s: _live_resolution(s), start_proxy_func=sp)
    assert result.success is False
    assert len(started) == 2
    assert proxies[0].stopped is True
    assert proxies[1].stopped is True, "second husk must also be torn down"


def test_prefetch_fail_then_offline_returns_failure(
    mock_xbmcgui: MagicMock,
) -> None:
    mod = _import_resolver()
    resolutions = [_live_resolution(), _offline_resolution()]
    resolve_calls: list[str] = []

    def rf(slug: str) -> Resolution:
        resolve_calls.append(slug)
        return resolutions[len(resolve_calls) - 1]

    proxy = _PrefetchAwareProxy(False)
    started: list[str] = []

    def sp(stream_url: str, room_url: str) -> Any:
        started.append(stream_url)
        return proxy

    result = mod.resolve_to_listitem(
        "alice", "alice", resolve_func=rf, start_proxy_func=sp)
    assert result.success is False
    assert len(started) == 1, "offline re-resolve must not start another proxy"
    assert proxy.stopped is True


def test_proxy_without_prefetch_flag_treated_as_ok(
    mock_xbmcgui: MagicMock,
) -> None:
    # Back-compat: handles without the flag (older mocks) behave as before.
    mod = _import_resolver()
    resolve_calls: list[str] = []

    def rf(slug: str) -> Resolution:
        resolve_calls.append(slug)
        return _live_resolution(slug)

    result = mod.resolve_to_listitem(
        "alice", "alice",
        resolve_func=rf, start_proxy_func=lambda u, r: _FakeProxyHandle())
    assert result.success is True
    assert len(resolve_calls) == 1
