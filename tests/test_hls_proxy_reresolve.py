"""0.7.65 - re-resolve a REVOKED stream URL in place instead of giving up.

Observed live 2026-10-04: every ~6-11 min the CDN edge started answering
403 for the stream's master llhls.m3u8. _refresh_session re-fetched that
SAME stored URL five times (2s apart), all 403, then GIVING UP ->
PlayerControl(Stop) -> tv_loop re-resolved the SAME room via the AJAX
endpoint, which worked first time ~7s later. The viewer saw a stop/restart
of the same cam ("blips"). A refresh cannot fix a revoked URL; a fresh
resolve can, and it can do it without stopping the player.

Contract pinned here:
- a refresh failure records the HTTP status (or None for non-HTTP errors)
- on 403/404/410 the reconnect calls state.re_resolve ONCE per cycle,
  swaps state.stream_url and refreshes again immediately
- no re_resolve wired -> exactly the old behaviour
- transient errors (5xx, timeouts) never trigger a re-resolve
- re_resolve returning None or raising falls back to the old give-up path
"""

from __future__ import annotations

import time
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

OLD = "https://edge9-atl.example/v1/edge/streams/origin.room.OLD/llhls.m3u8"
NEW = "https://edge3-atl.example/v1/edge/streams/origin.room.NEW/llhls.m3u8"
MASTER = (
    b"#EXTM3U\n"
    b"#EXT-X-STREAM-INF:BANDWIDTH=1000\n"
    b"chunklist_4_video_llhls.m3u8\n"
), "application/vnd.apple.mpegurl"


def _http_error(url: str, code: int) -> HTTPError:
    return HTTPError(url, code, "nope", Message(), None)


class _Harness:
    """Patches the proxy's side effects so _run_reconnect runs synchronously."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fail: dict[str, Exception]) -> None:
        from resources.lib import hls_proxy
        self.hp = hls_proxy
        self.fail = fail
        self.fetched: list[str] = []
        self.force_stops = 0
        monkeypatch.setattr(hls_proxy, "_fetch", self._fetch)
        monkeypatch.setattr(hls_proxy, "_sleep_or_stop", self._sleep)
        monkeypatch.setattr(hls_proxy, "_force_player_stop", self._force_stop)

    @staticmethod
    def _sleep(state: Any, seconds: float) -> None:
        # instant, but time still "passes" for _refresh_session's 2s same-URL dedup
        state.last_refresh -= seconds + 0.5

    def _fetch(self, url: str, _headers: dict[str, str], **_kw: Any) -> tuple[bytes, str]:
        self.fetched.append(url)
        if url in self.fail:
            raise self.fail[url]
        return MASTER

    def _force_stop(self, _state: Any) -> None:
        self.force_stops += 1

    def state(self, re_resolve: Any = None) -> Any:
        st = self.hp._State(stream_url=OLD, headers={"User-Agent": "t"})
        st.re_resolve = re_resolve
        st.reconnecting = True
        st.last_request = time.time()  # ISA is talking -> watchdog passes
        return st


class _Resolver:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        a = self.answers.pop(0) if self.answers else None
        if isinstance(a, Exception):
            raise a
        return a  # type: ignore[no-any-return]


def test_refresh_records_the_http_status_of_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch, {OLD: _http_error(OLD, 403)})
    st = h.state()
    assert h.hp._refresh_session(st) is False
    assert st.last_refresh_code == 403


def test_refresh_records_none_for_a_non_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch, {OLD: URLError("timed out")})
    st = h.state()
    st.last_refresh_code = 403  # stale value from an earlier refresh must not survive
    assert h.hp._refresh_session(st) is False
    assert st.last_refresh_code is None


def test_refresh_clears_the_status_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch, {})
    st = h.state()
    st.last_refresh_code = 403
    assert h.hp._refresh_session(st) is True
    assert st.last_refresh_code is None


def test_revoked_url_is_re_resolved_in_place_without_stopping_the_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, {OLD: _http_error(OLD, 403)})
    rr = _Resolver(NEW)
    st = h.state(rr)
    h.hp._run_reconnect(st)
    assert rr.calls == 1
    assert st.stream_url == NEW
    assert h.fetched == [OLD, NEW]          # one dead fetch, then straight to the fresh URL
    assert st.terminal is False
    assert h.force_stops == 0
    assert st.reconnecting is False


@pytest.mark.parametrize("code", [403, 404, 410])
def test_every_revoked_status_triggers_the_re_resolve(
    monkeypatch: pytest.MonkeyPatch, code: int,
) -> None:
    h = _Harness(monkeypatch, {OLD: _http_error(OLD, code)})
    rr = _Resolver(NEW)
    st = h.state(rr)
    h.hp._run_reconnect(st)
    assert rr.calls == 1 and st.stream_url == NEW and st.terminal is False


def test_without_a_resolver_the_old_give_up_path_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch, {OLD: _http_error(OLD, 403)})
    st = h.state(None)
    h.hp._run_reconnect(st)
    assert h.fetched == [OLD] * 5
    assert st.terminal is True
    assert h.force_stops == 1


def test_re_resolve_runs_at_most_once_per_reconnect_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    # the fresh URL is ALSO refused (a real IP block): no API hammering, normal give-up
    h = _Harness(monkeypatch, {OLD: _http_error(OLD, 403), NEW: _http_error(NEW, 403)})
    rr = _Resolver(NEW, NEW, NEW)
    st = h.state(rr)
    h.hp._run_reconnect(st)
    assert rr.calls == 1
    assert st.terminal is True
    assert h.force_stops == 1


@pytest.mark.parametrize("exc", [_http_error(OLD, 502), _http_error(OLD, 503), URLError("timed out")])
def test_transient_errors_never_re_resolve(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    h = _Harness(monkeypatch, {OLD: exc})
    rr = _Resolver(NEW)
    st = h.state(rr)
    h.hp._run_reconnect(st)
    assert rr.calls == 0
    assert st.stream_url == OLD
    assert st.terminal is True


@pytest.mark.parametrize("answer", [None, "", RuntimeError("api down")])
def test_offline_room_or_resolver_error_falls_back_to_give_up(
    monkeypatch: pytest.MonkeyPatch, answer: Any,
) -> None:
    h = _Harness(monkeypatch, {OLD: _http_error(OLD, 403)})
    rr = _Resolver(answer)
    st = h.state(rr)
    h.hp._run_reconnect(st)                 # must not raise / crash the thread
    assert rr.calls == 1
    assert st.stream_url == OLD
    assert st.terminal is True
    assert h.force_stops == 1


def test_start_proxy_hands_the_resolver_to_its_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from resources.lib import hls_proxy
    monkeypatch.setattr(hls_proxy, "_fetch", lambda *_a, **_k: MASTER)
    rr = _Resolver(NEW)
    handle = hls_proxy.start_proxy(OLD, "https://chaturbate.com/someroom/", re_resolve=rr)
    try:
        assert handle._state.re_resolve is rr
    finally:
        handle.stop()


def test_production_start_proxy_wires_a_resolver_for_the_same_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from resources.lib import addon_settings, hls_proxy, playvid_resolver
    from resources.lib.cb_resolve import Resolution

    captured: dict[str, Any] = {}
    monkeypatch.setattr(hls_proxy, "start_proxy", lambda **kw: captured.update(kw))
    monkeypatch.setattr(addon_settings, "isa_proxy_port", lambda: 0)
    seen: list[str] = []

    def fake_resolve(slug: str) -> Resolution:
        seen.append(slug)
        return Resolution(is_live=True, hls_source=NEW)

    monkeypatch.setattr(playvid_resolver, "_default_resolve", fake_resolve)
    playvid_resolver._default_start_proxy(OLD, "https://chaturbate.com/someroom/")
    assert captured["re_resolve"]() == NEW
    assert seen == ["someroom"]

    monkeypatch.setattr(
        playvid_resolver, "_default_resolve",
        lambda slug: Resolution(is_live=False, hls_source=None),
    )
    assert captured["re_resolve"]() is None   # room went offline -> nothing to swap in
