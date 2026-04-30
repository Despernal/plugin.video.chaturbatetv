"""Phase 4c proxy hardening tests.

Distinct file from test_hls_proxy.py because the surface area expands
considerably: chunklist proxying, segment proxying, gzipped manifests,
cached playlists, session refresh, reconnect/watchdog, cache
invalidation, deferred shutdown, terminal-flag fast-paths.

These tests stand up a stub upstream that simulates the Chaturbate CDN
and exercise the proxy through urllib (the way ISA would). Where a
test needs to drive internal state (force a refresh, flip the terminal
flag, fail a chunklist mid-flight), it pokes the proxy via its public
``ProxyHandle`` rather than reaching into private attributes.
"""
from __future__ import annotations

import gzip
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest


# --------------------------------------------------------------------------- #
# Stub upstream "CDN" with controllable per-path responses + failure injection
# --------------------------------------------------------------------------- #


class _StubState:
    """Controllable stub Chaturbate CDN."""

    def __init__(self) -> None:
        # Path -> (status, body, content_encoding, content_type)
        self.responses: dict[str, tuple[int, bytes, str, str]] = {}
        # Per-path forced behaviour: 'fail' -> always 502; 'gzip' -> serve gzipped.
        self.path_modes: dict[str, str] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        # Allows tests to assert how many times a path was hit.
        self.hit_counts: dict[str, int] = {}

    def set_response(self, path: str, body: bytes, status: int = 200,
                     content_encoding: str = "",
                     content_type: str = "application/vnd.apple.mpegurl") -> None:
        self.responses[path] = (status, body, content_encoding, content_type)

    def fail_path(self, path: str) -> None:
        self.path_modes[path] = "fail"

    def unfail_path(self, path: str) -> None:
        self.path_modes.pop(path, None)


def _make_stub_handler(state: _StubState) -> type[BaseHTTPRequestHandler]:
    class _H(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            state.requests.append((self.path, dict(self.headers.items())))
            state.hit_counts[path] = state.hit_counts.get(path, 0) + 1
            mode = state.path_modes.get(path)
            if mode == "fail":
                self.send_error(502)
                return
            if path not in state.responses:
                self.send_error(404)
                return
            status, body, ce, ct = state.responses[path]
            self.send_response(status)
            self.send_header("Content-Type", ct)
            if ce:
                self.send_header("Content-Encoding", ce)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _H


@pytest.fixture
def stub_cdn(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[str, _StubState]]:
    """Stub upstream "CDN" that listens on 127.0.0.1.

    v0.7.39: the production SSRF guard in ``hls_proxy._fetch``
    rejects 127.0.0.1 URLs. Tests stand up a real listener on
    loopback, so we monkey-patch ``cb_endpoints.is_trusted_url`` to
    accept loopback for the test scope. The SSRF guard itself is
    exercised by dedicated tests in test_hls_proxy_phase4c that
    feed crafted URLs to ``_fetch`` directly without this fixture.
    """
    from resources.lib import cb_endpoints
    real_is_trusted = cb_endpoints.is_trusted_url

    def _test_is_trusted(url: str) -> bool:
        # Accept localhost for the in-test stub CDN; production hosts
        # still go through the real check.
        from urllib.parse import urlparse
        try:
            host = (urlparse(url).hostname or "").lower()
        except (ValueError, TypeError):
            return real_is_trusted(url)
        if host in ("127.0.0.1", "localhost"):
            return True
        return real_is_trusted(url)

    monkeypatch.setattr(cb_endpoints, "is_trusted_url", _test_is_trusted)

    state = _StubState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_stub_handler(state))
    host, port = server.server_address[0], server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=2.0)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _master_body() -> bytes:
    return (
        b"#EXTM3U\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n"
        b"chunklist_w12345_video.m3u8\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
        b"chunklist_w67890_video.m3u8\n"
    )


def _chunklist_body(seg_url: str = "seg_1_video.m4s") -> bytes:
    return (
        b"#EXTM3U\n"
        b"#EXT-X-VERSION:3\n"
        b"#EXTINF:2.0,\n"
        + seg_url.encode("ascii") + b"\n"
        b"#EXT-X-ENDLIST\n"
    )


# --------------------------------------------------------------------------- #
# 0. v0.7.36 backfill: _force_player_stop must actually fire
#    PlayerControl(Stop) and respect the rate-limit. Lesson 30 mandates
#    this from three call sites; pre-backfill, NO test patched
#    xbmc.executebuiltin to verify the builtin actually got invoked.
# --------------------------------------------------------------------------- #


def test_refresh_session_increments_refresh_gen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.37 (race audit pass 1, agent 2 HIGH #2 + #3): the
    refresh_gen counter is the linchpin of detecting a refresh
    rotation that happened while a handler was mid-fetch. This test
    pins the bump invariant: every successful refresh_session call
    increments the counter under the lock."""
    from resources.lib import hls_proxy
    state = hls_proxy._State(
        stream_url="https://x/master.m3u8",
        headers={"User-Agent": "test"},
    )
    initial = state.refresh_gen

    # Stub _fetch (the lower-level fetcher refresh_session uses) to
    # return a minimal valid master manifest so the refresh path
    # completes without a real network call.
    def fake_fetch(*_a: Any, **_kw: Any) -> tuple[bytes, str]:
        return (
            b"#EXTM3U\n"
            b"#EXT-X-STREAM-INF:BANDWIDTH=1000\n"
            b"https://cdn/x/chunklist.m3u8\n"
        ), "application/vnd.apple.mpegurl"

    monkeypatch.setattr(hls_proxy, "_fetch", fake_fetch)

    ok = hls_proxy._refresh_session(state)
    assert ok is True
    assert state.refresh_gen == initial + 1, (
        f"refresh_session must bump refresh_gen; "
        f"got {state.refresh_gen} (was {initial})"
    )


def test_snapshot_returns_atomic_view_of_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.37: _snapshot collapses the lockless ``if state.terminal``
    + ``if state.stopping`` compound checks into a single coherent
    view. The returned tuple is frozen so even if the caller hangs
    onto it, subsequent state mutations don't change what they're
    branching on."""
    from resources.lib import hls_proxy
    state = hls_proxy._State(stream_url="https://x", headers={})

    snap1 = hls_proxy._snapshot(state)
    assert snap1.terminal is False
    assert snap1.stopping is False
    assert snap1.refresh_gen == 0

    # Mutate state after the snapshot.
    state.terminal = True
    state.refresh_gen = 99

    # snap1 stays unchanged (frozen dataclass).
    assert snap1.terminal is False
    assert snap1.refresh_gen == 0

    # New snapshot reflects the mutation.
    snap2 = hls_proxy._snapshot(state)
    assert snap2.terminal is True
    assert snap2.refresh_gen == 99


def test_harvest_segment_maps_drops_data_when_refresh_gen_advances_midway(
) -> None:
    """v0.7.38 (audit pass #4 HIGH #6): v0.7.37 added the refresh_gen
    check at the chunklist handler's outer cache write, but a refresh
    that landed BETWEEN the outer check and the per-line harvest
    writes would still poison the just-cleared seg_cdn_urls. Fix:
    pass gen_before into _harvest_segment_maps; the bulk-update
    under one lock acquire compares gens and drops on mismatch.
    """
    from resources.lib import hls_proxy

    state = hls_proxy._State(stream_url="https://x", headers={})
    state.refresh_gen = 5

    chunklist = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXTINF:2.0,\n"
        "https://cdn-old/seg_video_0_42.m4s\n"
        "#EXTINF:2.0,\n"
        "https://cdn-old/seg_video_0_43.m4s\n"
        "#EXT-X-ENDLIST\n"
    )

    # Simulate refresh-during-harvest by advancing refresh_gen BEFORE
    # the harvest call. With gen_before=5 and current gen=6, the bulk-
    # update should detect the mismatch and drop everything.
    state.refresh_gen = 6
    hls_proxy._harvest_segment_maps(
        chunklist, "chunklist_video_0", state, gen_before=5,
    )
    assert state.seg_cdn_urls == {}, (
        f"refresh-during-harvest must drop the harvest; "
        f"got {state.seg_cdn_urls!r}"
    )
    assert state.latest_seg == {}


def test_harvest_segment_maps_writes_when_gen_unchanged() -> None:
    """v0.7.38 happy-path counterpoint: when gen_before matches the
    current gen, the harvest goes through normally (single bulk
    update under one lock acquire)."""
    from resources.lib import hls_proxy

    state = hls_proxy._State(stream_url="https://x", headers={})
    state.refresh_gen = 5

    chunklist = (
        "#EXTM3U\n"
        "#EXTINF:2.0,\n"
        "https://cdn-good/seg_video_0_42.m4s\n"
        "#EXT-X-ENDLIST\n"
    )
    hls_proxy._harvest_segment_maps(
        chunklist, "chunklist_video_0", state, gen_before=5,
    )

    assert "seg_video_0_42.m4s" in state.seg_cdn_urls
    assert state.latest_seg.get("chunklist_video_0") == (
        "https://cdn-good/seg_video_0_42.m4s"
    )


def test_chunklist_skips_harvest_when_refresh_during_fetch(
    stub_cdn: tuple[str, _StubState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.37 (race audit pass 1, agent 2 HIGH #2): if refresh_gen
    advanced between the url_map read and the chunklist payload's
    cache write, the segment URLs in the chunklist body reference the
    OLD CDN. Without the gen-check, _harvest_segment_maps would
    repopulate the freshly-cleared seg_cdn_urls / latest_seg with
    those stale URLs (poisoning future tier-2 lookups). The fix
    skips the harvest AND the cache write when the refresh_gen
    rotation is detected."""
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.set_response("/hls/abc/master.m3u8", _master_body())
    state.set_response(
        "/hls/abc/chunklist_w12345_video.m3u8",
        _chunklist_body(),
    )

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        # Bump refresh_gen while a chunklist fetch is "in flight" by
        # patching _fetch to bump the gen as a side effect of the
        # fetch returning. Simulates the race deterministically.
        original_fetch = hls_proxy._fetch

        def fetch_with_concurrent_refresh(
            url: str, headers: dict[str, str], **kw: Any,
        ) -> tuple[bytes, str]:
            result = original_fetch(url, headers, **kw)
            if "chunklist_w12345_video" in url:
                with handle._state.lock:
                    handle._state.refresh_gen += 1
            return result

        monkeypatch.setattr(hls_proxy, "_fetch", fetch_with_concurrent_refresh)

        # Get master so we have a chunklist URL to hit.
        with urlopen(handle.master_url, timeout=5) as resp:
            master = resp.read().decode("utf-8")
        cl_url = next(
            line for line in master.splitlines()
            if line and not line.startswith("#")
        )

        # Pre-populate seg_cdn_urls so we can detect whether the
        # poisoning happened. The chunklist body has "seg_1_video.m4s"
        # which would normally land in seg_cdn_urls under that key.
        with handle._state.lock:
            handle._state.seg_cdn_urls.clear()

        # Hit the chunklist; the patched _fetch bumps refresh_gen mid-
        # request so the post-fetch gen check should detect the race.
        with urlopen(cl_url, timeout=5) as resp:
            resp.read()

        # Verify: seg_cdn_urls was NOT poisoned with stale segment
        # URLs because the harvest was skipped.
        with handle._state.lock:
            harvested = dict(handle._state.seg_cdn_urls)
        assert harvested == {}, (
            f"refresh-during-fetch must skip _harvest_segment_maps; "
            f"got {harvested!r}"
        )
    finally:
        handle.stop()


def test_force_player_stop_invokes_player_control_stop_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct unit test on _force_player_stop. Patch xbmc.executebuiltin
    via sys.modules and assert the call lands on PlayerControl(Stop).
    Pre-backfill: a typo (PlayerControl(Stop) -> PlayerControl(stop)
    or PlayerControls(Stop)) would have shipped green because no test
    asserted the builtin.
    """
    import sys
    from unittest.mock import MagicMock

    fake_xbmc = MagicMock()
    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)

    from resources.lib import hls_proxy
    # v0.7.43: reset _active_proxy module global so the zombie-proxy
    # guard in _force_player_stop sees "no active" -> fires normally.
    # Other tests in this run may leave it set.
    monkeypatch.setattr(hls_proxy, "_active_proxy", None)
    state = hls_proxy._State(stream_url="https://x", headers={})
    state.last_force_stop = 0.0

    hls_proxy._force_player_stop(state)

    builtin_calls = [
        c.args[0] for c in fake_xbmc.executebuiltin.call_args_list
        if c.args
    ]
    assert "PlayerControl(Stop)" in builtin_calls, (
        f"expected PlayerControl(Stop) builtin, got {builtin_calls!r}"
    )


def test_force_player_stop_rate_limited_within_throttle_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lesson 30: the chunklist handler can fire force-stop on every
    request when ISA hammers us at 30+/sec. _FORCE_STOP_THROTTLE_S
    (1.0s) gates repeated calls. Pre-backfill, NO test verified the
    rate-limit -- a regression flipping the comparison would either
    flood Kodi's event queue or never fire at all.
    """
    import sys
    from unittest.mock import MagicMock

    fake_xbmc = MagicMock()
    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)

    from resources.lib import hls_proxy
    monkeypatch.setattr(hls_proxy, "_active_proxy", None)
    state = hls_proxy._State(stream_url="https://x", headers={})
    state.last_force_stop = 0.0

    # Two back-to-back calls inside the 1s window.
    hls_proxy._force_player_stop(state)
    hls_proxy._force_player_stop(state)

    builtin_calls = [
        c.args[0] for c in fake_xbmc.executebuiltin.call_args_list
        if c.args and c.args[0] == "PlayerControl(Stop)"
    ]
    assert len(builtin_calls) == 1, (
        f"rate-limit should suppress the second call, "
        f"got {len(builtin_calls)} PlayerControl(Stop) firings"
    )


def test_force_player_stop_fires_again_after_throttle_window_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flip-side: once the throttle window (1s) elapses, a second
    call should fire. Test by manually rewinding state.last_force_stop
    to simulate elapsed time."""
    import sys
    from unittest.mock import MagicMock

    fake_xbmc = MagicMock()
    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)

    from resources.lib import hls_proxy
    monkeypatch.setattr(hls_proxy, "_active_proxy", None)
    state = hls_proxy._State(stream_url="https://x", headers={})
    state.last_force_stop = 0.0

    hls_proxy._force_player_stop(state)
    # Pretend 2 seconds passed (more than the 1s throttle).
    state.last_force_stop -= 2.0
    hls_proxy._force_player_stop(state)

    builtin_calls = [
        c.args[0] for c in fake_xbmc.executebuiltin.call_args_list
        if c.args and c.args[0] == "PlayerControl(Stop)"
    ]
    assert len(builtin_calls) == 2, (
        f"second call after throttle window should fire; "
        f"got {len(builtin_calls)} firings"
    )


def test_force_player_stop_skips_when_proxy_no_longer_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.43 regression: an abandoned proxy (replaced by a newer one
    via playvid takeover) must NOT fire PlayerControl(Stop) when its
    own reconnect-give-up path runs. Pre-fix repro from : TV mode
    played model_e, user picked model_b from TV List, takeover released
    TV mode and model_b started -- but model_e's old proxy was still
    grinding through its 5-attempt 403-reconnect chain. When that
    exhausted, it fired PlayerControl(Stop), killing model_b's playback.

    Fix: ``_force_player_stop`` checks ``_active_proxy._state is state``.
    A zombie proxy whose state isn't the global-active one bails before
    firing the builtin.
    """
    import sys
    from unittest.mock import MagicMock

    fake_xbmc = MagicMock()
    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)

    from resources.lib import hls_proxy
    zombie_state = hls_proxy._State(stream_url="https://model_e", headers={})
    zombie_state.last_force_stop = 0.0
    active_state = hls_proxy._State(stream_url="https://model_b", headers={})

    # Build a minimal handle that ``_active_proxy`` can hold and whose
    # ``_state`` attribute exposes the active state.
    fake_active_handle = MagicMock()
    fake_active_handle._state = active_state
    monkeypatch.setattr(hls_proxy, "_active_proxy", fake_active_handle)

    hls_proxy._force_player_stop(zombie_state)

    builtin_calls = [
        c.args[0] for c in fake_xbmc.executebuiltin.call_args_list
        if c.args and c.args[0] == "PlayerControl(Stop)"
    ]
    assert builtin_calls == [], (
        "zombie proxy must NOT fire PlayerControl(Stop) on the new "
        f"player; fired {builtin_calls!r}"
    )


def test_force_player_stop_fires_when_proxy_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sibling test: when the proxy IS the currently-active one, the
    v0.7.43 guard must NOT block legitimate stop firings. Same setup
    as the zombie test except ``_active_proxy._state is state``.
    """
    import sys
    from unittest.mock import MagicMock

    fake_xbmc = MagicMock()
    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)

    from resources.lib import hls_proxy
    state = hls_proxy._State(stream_url="https://model_e", headers={})
    state.last_force_stop = 0.0
    fake_active_handle = MagicMock()
    fake_active_handle._state = state  # this proxy IS active
    monkeypatch.setattr(hls_proxy, "_active_proxy", fake_active_handle)

    hls_proxy._force_player_stop(state)

    builtin_calls = [
        c.args[0] for c in fake_xbmc.executebuiltin.call_args_list
        if c.args and c.args[0] == "PlayerControl(Stop)"
    ]
    assert builtin_calls == ["PlayerControl(Stop)"], (
        "active proxy must still fire stop; "
        f"got {builtin_calls!r}"
    )


def test_force_player_stop_fires_when_no_active_proxy_tracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case: ``_active_proxy`` is None (between teardown and new
    start, or fresh-process). The guard should NOT block in this case
    -- "no tracked active" is treated as "this caller's state is fine".
    Pre-fix this case existed only at module-import time; preserving
    it keeps the existing _force_player_stop tests green.
    """
    import sys
    from unittest.mock import MagicMock

    fake_xbmc = MagicMock()
    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)

    from resources.lib import hls_proxy
    state = hls_proxy._State(stream_url="https://x", headers={})
    state.last_force_stop = 0.0
    monkeypatch.setattr(hls_proxy, "_active_proxy", None)

    hls_proxy._force_player_stop(state)

    builtin_calls = [
        c.args[0] for c in fake_xbmc.executebuiltin.call_args_list
        if c.args and c.args[0] == "PlayerControl(Stop)"
    ]
    assert builtin_calls == ["PlayerControl(Stop)"], (
        f"no-active-proxy edge case should still fire; got {builtin_calls!r}"
    )


def test_force_player_stop_skips_when_player_on_different_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.44 regression: the v0.7.43 in-process ``_active_proxy``
    guard couldn't see sibling Kodi-spawned playvid processes (each
    has its own ``_active_proxy`` global). Real-world repro persisted:
    rapid model-switching had every old proxy fire ``PlayerControl(
    Stop)`` on its reconnect-give-up, killing whichever sibling
    process's stream the player was actually serving.

    The fix uses ``xbmc.Player().getPlayingFile()`` -- a Kodi-global
    (cross-process) -- and bails when the player's URL doesn't contain
    this proxy's bound port token (``:PORT/``).
    """
    import sys
    from unittest.mock import MagicMock

    fake_xbmc = MagicMock()
    # Player is currently serving port 35655 (model_b); this proxy is
    # the zombie at port 41575 (model_e).
    fake_xbmc.Player.return_value.getPlayingFile.return_value = (
        "http://127.0.0.1:35655/master.m3u8"
    )
    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)

    from resources.lib import hls_proxy
    monkeypatch.setattr(hls_proxy, "_active_proxy", None)
    state = hls_proxy._State(stream_url="https://model_e", headers={})
    state.port = 41575  # zombie port
    state.last_force_stop = 0.0

    hls_proxy._force_player_stop(state)

    builtin_calls = [
        c.args[0] for c in fake_xbmc.executebuiltin.call_args_list
        if c.args and c.args[0] == "PlayerControl(Stop)"
    ]
    assert builtin_calls == [], (
        "zombie sibling-process proxy must not stop the active player; "
        f"fired {builtin_calls!r}"
    )


def test_force_player_stop_fires_when_player_on_matching_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sibling: when the player IS serving from this proxy's port, the
    v0.7.44 cross-process guard must NOT block legitimate stop firings.
    """
    import sys
    from unittest.mock import MagicMock

    fake_xbmc = MagicMock()
    fake_xbmc.Player.return_value.getPlayingFile.return_value = (
        "http://127.0.0.1:41575/master.m3u8"
    )
    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)

    from resources.lib import hls_proxy
    monkeypatch.setattr(hls_proxy, "_active_proxy", None)
    state = hls_proxy._State(stream_url="https://model_e", headers={})
    state.port = 41575  # matches what the player is on
    state.last_force_stop = 0.0

    hls_proxy._force_player_stop(state)

    builtin_calls = [
        c.args[0] for c in fake_xbmc.executebuiltin.call_args_list
        if c.args and c.args[0] == "PlayerControl(Stop)"
    ]
    assert builtin_calls == ["PlayerControl(Stop)"], (
        f"matching-port active proxy must fire stop; got {builtin_calls!r}"
    )


def test_force_player_stop_fires_when_player_returns_empty_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case: ``getPlayingFile()`` returns empty (player not
    playing or between-files transition). The v0.7.44 guard treats
    this as "no signal, fall through to existing logic" rather than
    silently suppressing legitimate stops. The in-process v0.7.43 guard
    then handles whatever comes next."""
    import sys
    from unittest.mock import MagicMock

    fake_xbmc = MagicMock()
    fake_xbmc.Player.return_value.getPlayingFile.return_value = ""
    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)

    from resources.lib import hls_proxy
    monkeypatch.setattr(hls_proxy, "_active_proxy", None)
    state = hls_proxy._State(stream_url="https://x", headers={})
    state.port = 41575
    state.last_force_stop = 0.0

    hls_proxy._force_player_stop(state)

    builtin_calls = [
        c.args[0] for c in fake_xbmc.executebuiltin.call_args_list
        if c.args and c.args[0] == "PlayerControl(Stop)"
    ]
    assert builtin_calls == ["PlayerControl(Stop)"], (
        "empty getPlayingFile() should not block stop; "
        f"fall through to in-process check, got {builtin_calls!r}"
    )


def test_force_player_stop_falls_through_when_state_port_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: if state.port is 0 (state was constructed but proxy
    never bound), skip the cross-process check entirely. Otherwise the
    f':0/' token would falsely match nothing and bail every time.
    Hits in test harness paths where ``_State`` is constructed directly.
    """
    import sys
    from unittest.mock import MagicMock

    fake_xbmc = MagicMock()
    fake_xbmc.Player.return_value.getPlayingFile.return_value = (
        "http://127.0.0.1:99999/master.m3u8"  # would not match port 0
    )
    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)

    from resources.lib import hls_proxy
    monkeypatch.setattr(hls_proxy, "_active_proxy", None)
    state = hls_proxy._State(stream_url="https://x", headers={})
    # state.port stays at default 0
    state.last_force_stop = 0.0

    hls_proxy._force_player_stop(state)

    builtin_calls = [
        c.args[0] for c in fake_xbmc.executebuiltin.call_args_list
        if c.args and c.args[0] == "PlayerControl(Stop)"
    ]
    assert builtin_calls == ["PlayerControl(Stop)"], (
        f"unset state.port should skip cross-process check, not block; "
        f"got {builtin_calls!r}"
    )


def test_stop_active_proxy_marks_stopping_synchronously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.43: ``_stop_active_proxy`` must set ``prev._state.stopping``
    True synchronously, BEFORE spawning the daemon cleanup thread.
    Otherwise an in-flight reconnect thread on the old proxy may pass
    its ``not state.stopping`` check and fire stop in the gap between
    "_active_proxy cleared" and "daemon called .stop()".

    Test: stub the daemon-thread spawn to a no-op so the test stays
    deterministic, then assert state.stopping flipped True before
    _stop_active_proxy returned.
    """
    from unittest.mock import MagicMock
    from resources.lib import hls_proxy

    state = hls_proxy._State(stream_url="https://x", headers={})
    assert state.stopping is False  # baseline

    fake_handle = MagicMock()
    fake_handle._state = state
    fake_handle.port = 12345
    fake_handle.stop = MagicMock()
    monkeypatch.setattr(hls_proxy, "_active_proxy", fake_handle)

    # Stub Thread so we don't actually spawn or join a real thread; we
    # just need to assert the synchronous flag flip happens before the
    # spawn would have run.
    import threading
    fake_thread_cls = MagicMock(return_value=MagicMock(start=lambda: None))
    monkeypatch.setattr(threading, "Thread", fake_thread_cls)

    hls_proxy._stop_active_proxy()

    assert state.stopping is True, (
        "stopping flag must flip synchronously so in-flight reconnect "
        "threads see it on their next tick"
    )


# --------------------------------------------------------------------------- #
# 1. Chunklist proxying (commit 47ccca1)
# --------------------------------------------------------------------------- #


def test_master_chunklist_uris_rewritten_to_proxy_chunklist_path(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """After hardening, the master that ISA sees should route chunklist
    URLs back through the proxy at /chunklist?... so we control the CDN
    fetch (and can refresh the JWT under it). Without this, ISA hits
    chaturbate edges directly without our headers and 403s.
    """
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.set_response("/hls/abc/master.m3u8", _master_body())

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            body = resp.read().decode("utf-8")
        # Each chunklist line should be absolute proxy URL.
        non_comments = [
            line for line in body.splitlines()
            if line and not line.startswith("#")
        ]
        assert len(non_comments) == 2
        for line in non_comments:
            assert line.startswith(f"http://{handle.host}:{handle.port}/chunklist")
    finally:
        handle.stop()


def test_chunklist_endpoint_fetches_upstream_with_iPad_headers(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """A GET on the proxy /chunklist?... fetches the upstream chunklist
    with iPad UA + Referer set to the room URL.
    """
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.set_response("/hls/abc/master.m3u8", _master_body())
    state.set_response("/hls/abc/chunklist_w12345_video.m3u8",
                       _chunklist_body())

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        # Drive a chunklist hit by parsing the proxy's master and fetching
        # the rewritten URL.
        with urlopen(handle.master_url, timeout=5) as resp:
            master_body = resp.read().decode("utf-8")
        chunklist_proxy_url = next(
            line for line in master_body.splitlines()
            if line and not line.startswith("#")
        )
        with urlopen(chunklist_proxy_url, timeout=5) as resp:
            resp.read()
    finally:
        handle.stop()
    # The stub captured the upstream chunklist fetch; assert headers.
    chunklist_hits = [
        r for r in state.requests
        if r[0].startswith("/hls/abc/chunklist_")
    ]
    assert chunklist_hits, f"no chunklist upstream fetch, requests={state.requests}"
    norm = {k.lower(): v for k, v in chunklist_hits[0][1].items()}
    assert "ipad" in norm.get("user-agent", "").lower()
    assert norm.get("referer") == "https://chaturbate.com/alice/"


def test_chunklist_segment_uris_rewritten_to_proxy_segment_path(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """Segment URLs in the chunklist response come back rewritten to
    /segment?url=... so ISA never goes direct to the CDN.
    """
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.set_response("/hls/abc/master.m3u8", _master_body())
    state.set_response(
        "/hls/abc/chunklist_w12345_video.m3u8",
        _chunklist_body("seg_1_video.m4s"),
    )

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            master = resp.read().decode("utf-8")
        cl_url = next(
            line for line in master.splitlines()
            if line and not line.startswith("#")
        )
        with urlopen(cl_url, timeout=5) as resp:
            cl_body = resp.read().decode("utf-8")
        seg_lines = [
            line for line in cl_body.splitlines()
            if line and not line.startswith("#")
        ]
        assert seg_lines, "chunklist body has no segment lines"
        for line in seg_lines:
            assert line.startswith(
                f"http://{handle.host}:{handle.port}/segment?url="
            ), f"unrewritten segment: {line!r}"
    finally:
        handle.stop()


# --------------------------------------------------------------------------- #
# 2. Segment proxying (commit ba44c62) - THIS is what fixes ISA playback
# --------------------------------------------------------------------------- #


def test_segment_endpoint_fetches_upstream_with_iPad_headers(
    stub_cdn: tuple[str, _StubState],
) -> None:
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    seg_path = "/hls/abc/seg_1_video.m4s"
    state.set_response(seg_path, b"BYTES_OF_VIDEO", content_type="video/mp4")

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        # Hit the proxy segment endpoint directly with the absolute
        # upstream URL urlencoded.
        from urllib.parse import quote
        seg_url = f"{cdn_base}{seg_path}"
        proxy_seg_url = (
            f"http://{handle.host}:{handle.port}/segment?url={quote(seg_url, safe='')}"
        )
        with urlopen(proxy_seg_url, timeout=5) as resp:
            data = resp.read()
            ct = resp.headers.get("Content-Type", "")
        assert data == b"BYTES_OF_VIDEO"
        assert "video/mp4" in ct
    finally:
        handle.stop()
    seg_hits = [r for r in state.requests if r[0] == seg_path]
    assert seg_hits, "segment fetch never reached upstream"
    norm = {k.lower(): v for k, v in seg_hits[0][1].items()}
    assert "ipad" in norm.get("user-agent", "").lower()
    assert norm.get("referer") == "https://chaturbate.com/alice/"


def test_segment_tier2_fallback_uses_seg_cdn_urls(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """v0.7.36 backfill (audit pass #2 HIGH): segment Tier-2 fallback
    is the meat of three-tier segment proxying (commit ba44c62 /
    Lesson 12). Tier-1 (the URL ISA asked for) fails -- tier 2 looks
    up state.seg_cdn_urls[basename] and serves that. Pre-backfill,
    only the all-tiers-fail-502 path was tested. A regression in the
    seg_cdn_urls key shape would have shipped green; ISA would have
    started 502'ing during real CDN rotation.
    """
    from urllib.parse import quote

    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.set_response("/hls/abc/master.m3u8", _master_body())
    # Tier-2 destination: serves real segment bytes.
    state.set_response("/hls/cdn-rotated/seg_video_0_42.m4s",
                       b"TIER2-BYTES",
                       content_type="video/mp4")
    # Tier-1 path will fail (we never define it -> 404 by stub).
    state.fail_path("/hls/cdn-original/seg_video_0_42.m4s")

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        # Pre-populate tier-2 lookup as if a chunklist response had
        # been parsed and seeded the basename->canonical mapping. Also
        # set ``reconnecting=True`` so the trigger_reconnect path
        # (which fires on tier-1 failure) doesn't race
        # _refresh_session into clearing seg_cdn_urls before tier-2
        # gets a chance to read it.
        with handle._state.lock:
            handle._state.seg_cdn_urls["seg_video_0_42.m4s"] = (
                f"{cdn_base}/hls/cdn-rotated/seg_video_0_42.m4s"
            )
            handle._state.reconnecting = True

        tier1_url = f"{cdn_base}/hls/cdn-original/seg_video_0_42.m4s"
        proxy_seg_url = (
            f"http://{handle.host}:{handle.port}/segment?"
            f"url={quote(tier1_url, safe='')}"
        )
        with urlopen(proxy_seg_url, timeout=5) as resp:
            body = resp.read()
        assert body == b"TIER2-BYTES", (
            f"tier-2 fallback should serve seg_cdn_urls bytes, "
            f"got {body!r}"
        )
    finally:
        handle.stop()


def test_segment_tier3_fallback_uses_latest_seg(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """v0.7.36 backfill: segment Tier-3 fallback regex-matches
    ``(video|audio)_(\\d+)`` and searches ``state.latest_seg`` for a
    matching kind+idx track. A regression in the regex or the search
    loop would have shipped green pre-backfill; ISA gets 502s during
    real CDN rotation when neither Tier-1 nor Tier-2 has a hit.
    """
    from urllib.parse import quote

    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.set_response("/hls/abc/master.m3u8", _master_body())
    # Tier-3 destination.
    state.set_response("/hls/cdn-emergency/last_video_3.m4s",
                       b"TIER3-BYTES",
                       content_type="video/mp4")

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        # Tier-1 will fail (path not defined). Tier-2 has no match for
        # this seg_name. Tier-3 will match because latest_seg has a
        # key containing both 'video' and '3' AND a value to use.
        # Set ``reconnecting=True`` so the trigger_reconnect path
        # (fires on tier-1 fail) doesn't race _refresh_session into
        # clearing latest_seg before tier-3 reads it.
        with handle._state.lock:
            handle._state.latest_seg["chunklist_video_3"] = (
                f"{cdn_base}/hls/cdn-emergency/last_video_3.m4s"
            )
            handle._state.reconnecting = True

        # seg_name = "seg_xxx_video_3_42.m4s" -> regex matches kind=
        # 'video', idx='3'.
        tier1_url = (
            f"{cdn_base}/hls/cdn-original/seg_xxx_video_3_42.m4s"
        )
        proxy_seg_url = (
            f"http://{handle.host}:{handle.port}/segment?"
            f"url={quote(tier1_url, safe='')}"
        )
        with urlopen(proxy_seg_url, timeout=5) as resp:
            body = resp.read()
        assert body == b"TIER3-BYTES", (
            f"tier-3 fallback should serve latest_seg bytes, "
            f"got {body!r}"
        )
    finally:
        handle.stop()


def test_segment_endpoint_returns_502_on_upstream_failure(
    stub_cdn: tuple[str, _StubState],
) -> None:
    from urllib.parse import quote

    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    seg_path = "/hls/abc/dead_seg.m4s"
    state.fail_path(seg_path)

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        seg_url = f"{cdn_base}{seg_path}"
        proxy_seg_url = (
            f"http://{handle.host}:{handle.port}/segment?url={quote(seg_url, safe='')}"
        )
        with pytest.raises(HTTPError) as exc:
            urlopen(proxy_seg_url, timeout=5)
        assert exc.value.code == 502
    finally:
        handle.stop()


def test_segment_endpoint_400_on_missing_url_param() -> None:
    """The segment handler validates input - no url= param means 400."""
    from resources.lib import hls_proxy

    handle = hls_proxy.start_proxy(
        stream_url="http://does-not-resolve.invalid/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        with pytest.raises(HTTPError) as exc:
            urlopen(
                f"http://{handle.host}:{handle.port}/segment",
                timeout=5,
            )
        assert exc.value.code == 400
    finally:
        handle.stop()


# --------------------------------------------------------------------------- #
# 3. Gzip handling (commit eb7785c)
# --------------------------------------------------------------------------- #


def test_master_with_gzip_content_encoding_is_decompressed(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """mmcdn edges sometimes serve gzipped manifests. Without
    decompression, the rewriter sees garbage and the resulting body
    fails to parse in ISA ('Non-compliant HLS manifest').
    """
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    raw_master = _master_body()
    gz = gzip.compress(raw_master)
    state.set_response("/hls/abc/master.m3u8", gz, content_encoding="gzip")

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            body = resp.read().decode("utf-8")
        # The rewriter must have seen the decompressed text.
        assert "EXT-X-STREAM-INF" in body
        non_comments = [
            line for line in body.splitlines()
            if line and not line.startswith("#")
        ]
        assert len(non_comments) == 2
    finally:
        handle.stop()


def test_master_with_gzip_magic_bytes_no_header_is_decompressed(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """Some edges return gzip without setting Content-Encoding. Must
    detect by magic bytes (\\x1f\\x8b) too.
    """
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    raw_master = _master_body()
    gz = gzip.compress(raw_master)
    # No content_encoding -> only magic-byte detection saves us.
    state.set_response("/hls/abc/master.m3u8", gz)

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            body = resp.read().decode("utf-8")
        assert "EXT-X-STREAM-INF" in body
    finally:
        handle.stop()


# --------------------------------------------------------------------------- #
# 4. Cached playlists (commit 85b848d)
# --------------------------------------------------------------------------- #


def test_chunklist_endpoint_serves_cache_when_upstream_fails(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """First chunklist fetch succeeds and gets cached. When the upstream
    starts failing, the proxy should serve the cached body instead of
    EXT-X-ENDLIST so ISA stays alive while reconnect runs in the
    background.
    """
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.set_response("/hls/abc/master.m3u8", _master_body())
    state.set_response(
        "/hls/abc/chunklist_w12345_video.m3u8",
        _chunklist_body("seg_1.m4s"),
    )

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            master = resp.read().decode("utf-8")
        cl_url = next(
            line for line in master.splitlines()
            if line and not line.startswith("#")
        )
        # First hit warms the cache.
        with urlopen(cl_url, timeout=5) as resp:
            ok_body = resp.read()
        assert b"EXTM3U" in ok_body
        # Now upstream goes south.
        state.fail_path("/hls/abc/chunklist_w12345_video.m3u8")
        with urlopen(cl_url, timeout=5) as resp:
            cached_body = resp.read()
        # Cache hit -> 200, NOT ENDLIST.
        assert b"EXTM3U" in cached_body
        assert b"EXT-X-ENDLIST" not in cached_body or cached_body.count(b"\n") > 2
    finally:
        handle.stop()


def test_chunklist_endpoint_returns_finished_vod_when_no_cache_and_upstream_fails(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """No cache yet AND upstream failing -> finished-VOD ENDLIST body
    + force player stop. ISA logs "Download failed" on 410 and retries
    even on terminal, so we use 's tested pattern: serve a
    body that LOOKS like a completed VOD playlist and fire
    ``PlayerControl(Stop)`` to tear the player down at the Kodi side.
    """
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.set_response("/hls/abc/master.m3u8", _master_body())
    state.fail_path("/hls/abc/chunklist_w12345_video.m3u8")

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            master = resp.read().decode("utf-8")
        cl_url = next(
            line for line in master.splitlines()
            if line and not line.startswith("#")
        )
        with urlopen(cl_url, timeout=5) as resp:
            body = resp.read()
        assert b"#EXT-X-ENDLIST" in body
        assert b"#EXT-X-PLAYLIST-TYPE:VOD" in body
        assert b"#EXTINF:" in body
    finally:
        handle.stop()


# --------------------------------------------------------------------------- #
# 5. Session refresh (commit 1883c5a + 53acf18 cache invalidation)
# --------------------------------------------------------------------------- #


def test_refresh_session_refetches_master_and_swaps_url_map(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """Calling proxy.refresh_session() re-fetches the master URL so a
    later /chunklist?name=X looks up the latest CDN URL (with fresh
    JWT). After a successful refresh, internal caches are cleared.
    """
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    initial = (
        b"#EXTM3U\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
        b"chunklist_w12345_video.m3u8\n"
    )
    state.set_response("/hls/abc/master.m3u8", initial)
    state.set_response(
        "/hls/abc/chunklist_w12345_video.m3u8",
        _chunklist_body("seg_1.m4s"),
    )

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        # Warm the cache.
        with urlopen(handle.master_url, timeout=5) as resp:
            resp.read()
        # Now the master "rotates" - same chunklist name, but the body
        # changed.
        rotated = (
            b"#EXTM3U\n"
            b"#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
            b"chunklist_w12345_video.m3u8\n"
        )
        state.set_response("/hls/abc/master.m3u8", rotated)
        ok = handle.refresh_session()
        assert ok is True
        # Caches should be cleared.
        assert handle.chunklist_cache_size() == 0
    finally:
        handle.stop()


def test_refresh_session_returns_false_when_master_fetch_fails(
    stub_cdn: tuple[str, _StubState],
) -> None:
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.set_response("/hls/abc/master.m3u8", _master_body())

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        # Now break it.
        state.fail_path("/hls/abc/master.m3u8")
        ok = handle.refresh_session()
        assert ok is False
    finally:
        handle.stop()


# --------------------------------------------------------------------------- #
# 6. Reconnect guard (commit 23c2cbb) - lock-protected
# --------------------------------------------------------------------------- #


def test_trigger_reconnect_is_lock_protected_against_duplicate_threads(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """Concurrent _trigger_reconnect calls must spawn at most ONE
    reconnect thread WHILE the first is still in flight ('s
    race with check-then-set on the reconnecting flag).

    Test strategy: fail the master URL so each refresh_session attempt
    blocks/fails (the dedupe-within-2s clause keeps subsequent refreshes
    from also running). We then fire 20 trigger_reconnects in rapid
    succession; the lock guard means only ONE thread should be inside
    the reconnect path at once.
    """
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.set_response("/hls/abc/master.m3u8", _master_body())

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        # Make every subsequent fetch slow + failing so the reconnect
        # thread stays alive long enough for the spam to land.
        state.fail_path("/hls/abc/master.m3u8")
        # Start 20 threads that each call trigger_reconnect.
        threads = [
            threading.Thread(target=lambda: handle.trigger_reconnect("test"))
            for _ in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)
        # Give the reconnect thread a moment to actually be inside the
        # critical path so we observe peak==1.
        time.sleep(0.2)
        # The crucial invariant: at no point did 2 reconnect threads
        # exist simultaneously.
        assert handle.peak_reconnect_thread_count() <= 1
    finally:
        handle.stop()


# --------------------------------------------------------------------------- #
# 7. Terminal-flag fast path (commit 652f89d)
# --------------------------------------------------------------------------- #


def test_chunklist_endpoint_returns_finished_vod_when_terminal_flag_set(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """Once the terminal flag is set (reconnect exhausted), every
    chunklist request gets a finished-VOD playlist body (PLAYLIST-TYPE:VOD
    + ENDLIST + a single placeholder segment) and we fire
    ``PlayerControl(Stop)`` from the handler so Kodi tears the player
    down.

    Three earlier attempts loop-trapped:
    - 0.7.3 served empty ENDLIST -> ISA "No segments" -> retry 30+/sec
    - 0.7.4 served HTTP 410 -> ISA "Download failed" -> retry 30+/sec
    - 0.7.7 settles on 's pattern: VOD-ish body + executebuiltin
    """
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.set_response("/hls/abc/master.m3u8", _master_body())
    state.set_response(
        "/hls/abc/chunklist_w12345_video.m3u8",
        _chunklist_body(),
    )

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            master = resp.read().decode("utf-8")
        cl_url = next(
            line for line in master.splitlines()
            if line and not line.startswith("#")
        )
        handle.set_terminal()
        with urlopen(cl_url, timeout=5) as resp:
            body = resp.read()
        # Body must look like a finished VOD so ISA stops retrying.
        assert b"#EXT-X-ENDLIST" in body
        assert b"#EXT-X-PLAYLIST-TYPE:VOD" in body
        # And carry at least one #EXTINF so the manifest isn't "empty"
        # which is what triggered the 0.7.3 retry loop.
        assert b"#EXTINF:" in body
    finally:
        handle.stop()


def test_segment_endpoint_returns_410_when_terminal_flag_set(
    stub_cdn: tuple[str, _StubState],
) -> None:
    from urllib.parse import quote

    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.set_response("/hls/abc/master.m3u8", _master_body())

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        handle.set_terminal()
        seg_url = f"{cdn_base}/hls/abc/seg_1.m4s"
        proxy_seg_url = (
            f"http://{handle.host}:{handle.port}/segment?url={quote(seg_url, safe='')}"
        )
        with pytest.raises(HTTPError) as exc:
            urlopen(proxy_seg_url, timeout=5)
        assert exc.value.code == 410
    finally:
        handle.stop()


# --------------------------------------------------------------------------- #
# 8. Idempotent stop + double-stop safety
# --------------------------------------------------------------------------- #


def test_stop_is_idempotent(stub_cdn: tuple[str, _StubState]) -> None:
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.set_response("/hls/abc/master.m3u8", b"#EXTM3U\n#EXT-X-ENDLIST\n")

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/abc/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    handle.stop()
    handle.stop()  # must not raise


# --------------------------------------------------------------------------- #
# 9. URI="..." style chunklist references in the master are also rewritten
# --------------------------------------------------------------------------- #


def test_master_uri_quoted_chunklists_routed_through_proxy(
    stub_cdn: tuple[str, _StubState],
) -> None:
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    body = (
        b'#EXTM3U\n'
        b'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="en",URI="audio_w7777.m3u8"\n'
        b'#EXT-X-STREAM-INF:BANDWIDTH=2000000,AUDIO="aud"\n'
        b'chunklist_w12345_video.m3u8\n'
    )
    state.set_response("/hls/zz/master.m3u8", body)

    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/hls/zz/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            out = resp.read().decode("utf-8")
        assert f'http://{handle.host}:{handle.port}/chunklist' in out
        # The URI= attribute also got rewritten (m3u8 audio chunklist).
        assert (
            f'URI="http://{handle.host}:{handle.port}/chunklist' in out
        ), out
    finally:
        handle.stop()


# --------------------------------------------------------------------------- #
# 10. JWT/session redaction in logs (security: no secrets in logs)
# --------------------------------------------------------------------------- #


def test_fetch_rejects_untrusted_url_with_value_error() -> None:
    """v0.7.39 (audit pass #5 HIGH, agent 2): the SSRF defense that
    closes the localhost-proxy ``/segment?url=`` attack. _fetch
    rejects any URL whose host isn't in the cb_endpoints CB allowlist
    BEFORE urlopen runs. ValueError lets the caller treat it like
    any other fetch failure (handlers fall back to cache or 502)."""
    from resources.lib import hls_proxy
    import pytest

    for bad in (
        "file:///etc/shadow",
        "http://192.168.1.1/cgi-bin/admin",
        "ftp://chaturbate.com/secret",
        "javascript:alert(1)",
        "http://localhost:8088/internal",
    ):
        with pytest.raises(ValueError, match="untrusted URL"):
            hls_proxy._fetch(bad, headers={})


def test_log_redacts_query_string_entirely() -> None:
    """v0.7.39 (audit pass #5 HIGH, agent 2): _redact_url now strips
    the entire query + fragment instead of allowlist-redacting
    specific keys. Inverted rule: anything past the path is presumed
    secret. Maintenance-free against future Akamai/CloudFront tokens
    (hdnts, Signature, KeyPair-Id, etc.) that the previous allowlist
    didn't cover.
    """
    from resources.lib.hls_proxy import _redact_url

    s = "https://edge42.live.mmcdn.com/hls/abc/master.m3u8?token=SECRET&q=2"
    out = _redact_url(s)
    assert "SECRET" not in out
    assert "token=" not in out, (
        f"v0.7.39 inversion: query string should be GONE, got {out!r}"
    )
    # scheme + host + path preserved for debug context.
    assert out == "https://edge42.live.mmcdn.com/hls/abc/master.m3u8"


def test_log_redact_strips_akamai_token_in_query() -> None:
    """Future-proof: Akamai's hdnts / hdnea / Signature / Policy /
    KeyPair-Id and similar -- the v0.7.39 inversion catches them all
    by definition since EVERY query param is dropped."""
    from resources.lib.hls_proxy import _redact_url

    s = (
        "https://cdn.example.com/hls/x/seg.m4s"
        "?hdnts=exp=1234~hmac=DEADBEEF~acl=/foo&"
        "Signature=AKIA-MORESECRET&Expires=99999"
    )
    out = _redact_url(s)
    for secret in ("DEADBEEF", "AKIA-MORESECRET", "exp=", "hmac="):
        assert secret not in out, (
            f"secret-looking token {secret!r} leaked into {out!r}"
        )


def test_log_redact_strips_url_fragment() -> None:
    """Some CDN URLs put session info in the fragment too. Drop it."""
    from resources.lib.hls_proxy import _redact_url

    s = "https://cdn.example.com/hls/x/seg.m4s#session=ABCDEF"
    out = _redact_url(s)
    assert "ABCDEF" not in out
    assert "#" not in out


def test_log_redact_passes_through_url_with_no_query_or_fragment() -> None:
    """No-op for clean URLs."""
    from resources.lib.hls_proxy import _redact_url

    s = "https://chaturbate.com/alice/"
    assert _redact_url(s) == s
