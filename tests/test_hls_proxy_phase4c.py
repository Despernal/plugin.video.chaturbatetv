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
def stub_cdn() -> Iterator[tuple[str, _StubState]]:
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


def test_log_redacts_token_query_string() -> None:
    """If a URL carrying ``?token=...`` ends up in a log line, the
    token MUST be redacted before write so we don't leak single-use
    JWTs into the user-readable log file.
    """
    from resources.lib.hls_proxy import _redact_url

    s = "https://edge42.live.mmcdn.com/hls/abc/master.m3u8?token=SECRET&q=2"
    out = _redact_url(s)
    assert "SECRET" not in out
    assert "token=" in out  # key remains, value redacted
