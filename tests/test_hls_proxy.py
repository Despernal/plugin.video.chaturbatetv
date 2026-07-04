"""Tests for resources.lib.hls_proxy - MVP localhost rewriting proxy.

Strategy: stand up a stub HTTP server in a thread that pretends to be
the Chaturbate CDN. The proxy fetches from it. We exercise the proxy
through urllib (the same way ISA would) and assert it rewrote the
playlist correctly and injected our headers when calling upstream.

The stub server records every request the proxy makes against it,
so we can assert UA + Referer were forwarded.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.request import urlopen

import pytest


# --------------------------------------------------------------------------- #
# Stub upstream "CDN"
# --------------------------------------------------------------------------- #


class _StubState:
    """Per-test bag of stub responses + recorded requests."""

    def __init__(self) -> None:
        self.master_body: bytes = b""
        self.master_status: int = 200
        self.chunklist_body: bytes = b""
        self.chunklist_status: int = 200
        # Each captured request is (path, headers-dict).
        self.requests: list[tuple[str, dict[str, str]]] = []


def _make_stub_handler(state: _StubState) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:  # silence stub
            return

        def _record(self) -> None:
            state.requests.append((self.path, dict(self.headers.items())))

        def do_GET(self) -> None:
            self._record()
            # Match by basename so the stub plays the role of any CDN
            # path the test feeds the proxy as upstream (e.g.
            # /hls/abc/master.m3u8 or /hls/zz/master.m3u8).
            basename = self.path.rsplit("/", 1)[-1].split("?", 1)[0]
            if basename == "master.m3u8" or basename.startswith("master"):
                self.send_response(state.master_status)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(state.master_body)))
                self.end_headers()
                self.wfile.write(state.master_body)
                return
            if basename.startswith("chunklist"):
                self.send_response(state.chunklist_status)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(state.chunklist_body)))
                self.end_headers()
                self.wfile.write(state.chunklist_body)
                return
            self.send_error(404)

    return _Handler


@pytest.fixture
def stub_cdn(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[str, _StubState]]:
    """Run a stub HTTP server on 127.0.0.1:<random> and yield its base URL.

    v0.7.39: production SSRF guard rejects 127.0.0.1; tests
    monkey-patch ``cb_endpoints.is_trusted_url`` to accept loopback
    for the test scope (real production hosts still go through the
    real check). The SSRF guard itself is exercised by dedicated
    tests in test_hls_proxy_phase4c.
    """
    from resources.lib import cb_endpoints
    real_is_trusted = cb_endpoints.is_trusted_url

    def _test_is_trusted(url: str) -> bool:
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
    handler = _make_stub_handler(state)
    server = HTTPServer(("127.0.0.1", 0), handler)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_proxy_starts_and_returns_host_port_and_master_url(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """start_proxy returns (host, port, master_url) and the URL is on 127.0.0.1."""
    from resources.lib import hls_proxy

    cdn_base, _state = stub_cdn
    upstream = f"{cdn_base}/master.m3u8"
    handle = hls_proxy.start_proxy(
        stream_url=upstream,
        room_url="https://chaturbate.com/alice/",
    )
    try:
        assert handle.host == "127.0.0.1"
        assert isinstance(handle.port, int) and handle.port > 0
        assert handle.master_url == f"http://127.0.0.1:{handle.port}/master.m3u8"
    finally:
        handle.stop()


def test_master_playlist_relative_urls_are_rewritten_to_proxy(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """A master.m3u8 with relative chunklist refs comes back with the
    chunklist URLs rewritten to ``/chunklist?name=...`` on the proxy
    (Phase 4c). Direct upstream URLs in the master would let ISA bypass
    our header injection layer.
    """
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.master_body = (
        b"#EXTM3U\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n"
        b"chunklist_w12345_video.m3u8\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
        b"chunklist_w67890_video.m3u8\n"
    )
    upstream = f"{cdn_base}/hls/abc/master.m3u8"
    handle = hls_proxy.start_proxy(
        stream_url=upstream,
        room_url="https://chaturbate.com/alice/",
    )
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            body = resp.read().decode("utf-8")
        prefix = f"http://{handle.host}:{handle.port}/chunklist?name="
        assert f"{prefix}chunklist_w12345_video" in body
        assert f"{prefix}chunklist_w67890_video" in body
        # No raw upstream URL should remain in the master.
        assert cdn_base not in body
        # No bare relative path on a non-comment line.
        for line in body.splitlines():
            if line and not line.startswith("#"):
                assert line.startswith("http://"), f"unrewritten: {line!r}"
    finally:
        handle.stop()


def test_master_playlist_uri_quoted_relative_urls_routed_through_proxy(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """URI="..." attributes (EXT-X-MEDIA) get rewritten to the proxy too."""
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.master_body = (
        b'#EXTM3U\n'
        b'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="en",URI="audio_w7777.m3u8"\n'
        b'#EXT-X-STREAM-INF:BANDWIDTH=2000000,AUDIO="aud"\n'
        b'chunklist_w12345_video.m3u8\n'
    )
    upstream = f"{cdn_base}/hls/zz/master.m3u8"
    handle = hls_proxy.start_proxy(
        stream_url=upstream,
        room_url="https://chaturbate.com/bob/",
    )
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            body = resp.read().decode("utf-8")
        prefix = f"http://{handle.host}:{handle.port}/chunklist?name="
        assert f'URI="{prefix}audio_w7777"' in body
    finally:
        handle.stop()


def test_master_playlist_already_absolute_urls_routed_through_proxy(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """Absolute upstream URLs ALSO get rewritten to /chunklist?name=...
    so ISA never goes direct to the CDN regardless of master shape.
    """
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    pre = (
        b"#EXTM3U\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
        b"https://other-edge.example.com/abs/chunklist_xyz999_video.m3u8\n"
    )
    state.master_body = pre
    upstream = f"{cdn_base}/hls/zz/master.m3u8"
    handle = hls_proxy.start_proxy(
        stream_url=upstream,
        room_url="https://chaturbate.com/alice/",
    )
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            body = resp.read().decode("utf-8")
        prefix = f"http://{handle.host}:{handle.port}/chunklist?name="
        assert f"{prefix}chunklist_xyz999_video" in body
        # Upstream URL is gone.
        assert "other-edge.example.com" not in body
    finally:
        handle.stop()


def test_proxy_forwards_user_agent_and_referer_to_upstream(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """The upstream fetch includes the ipad UA + Referer set to the room URL."""
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.master_body = b"#EXTM3U\n#EXT-X-ENDLIST\n"
    upstream = f"{cdn_base}/hls/aa/master.m3u8"
    room_url = "https://chaturbate.com/alice/"
    handle = hls_proxy.start_proxy(stream_url=upstream, room_url=room_url)
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            resp.read()
    finally:
        handle.stop()
    # The first stub request is the proxy fetching the master upstream.
    # (urlopen on the proxy may also issue HEAD; we only care that AT LEAST
    # one master GET arrived with the expected headers.)
    masters = [r for r in state.requests if r[0].startswith("/hls/aa/master.m3u8")]
    assert masters, f"no master fetch on upstream, saw: {state.requests}"
    _, hdrs = masters[0]
    # Header keys are case-insensitive; the BaseHTTPRequestHandler dict
    # preserves the raw casing. Match either way.
    norm = {k.lower(): v for k, v in hdrs.items()}
    assert "ipad" in norm.get("user-agent", "").lower()
    assert norm.get("referer") == room_url


def test_chunklist_passthrough(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """One chunklist passed through end-to-end via the proxy /chunklist
    route (Phase 4c). Confirms ISA can fetch the rewritten URL and we
    pass the body back (with segment URLs rewritten further).
    """
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.master_body = (
        b"#EXTM3U\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
        b"chunklist_w12345_video.m3u8\n"
    )
    state.chunklist_body = (
        b"#EXTM3U\n"
        b"#EXT-X-VERSION:3\n"
        b"#EXTINF:2.0,\n"
        b"seg_1.m4s\n"
        b"#EXT-X-ENDLIST\n"
    )
    upstream = f"{cdn_base}/chunklist/master.m3u8"
    handle = hls_proxy.start_proxy(
        stream_url=upstream,
        room_url="https://chaturbate.com/alice/",
    )
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            master_body = resp.read().decode("utf-8")
        chunklist_url = next(
            line for line in master_body.splitlines()
            if line and not line.startswith("#")
        )
        # Phase 4c: the URL is /chunklist?name=... on the proxy.
        assert chunklist_url.startswith(
            f"http://{handle.host}:{handle.port}/chunklist?name="
        )
        with urlopen(chunklist_url, timeout=5) as resp:
            chunklist = resp.read().decode("utf-8")
        # Body was rewritten so segment URLs go through /segment?url=...
        seg_lines = [
            ln for ln in chunklist.splitlines()
            if ln and not ln.startswith("#")
        ]
        assert seg_lines
        for seg in seg_lines:
            assert seg.startswith(
                f"http://{handle.host}:{handle.port}/segment?url="
            )
    finally:
        handle.stop()


def test_chunklist_rewrites_rendition_report_uri(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """LL-HLS chunklists carry ``#EXT-X-RENDITION-REPORT:URI="..."``
    pointing at SIBLING quality-level chunklists. ISA reads those URIs
    to do fast quality switching without a master refetch. If they
    point straight at chaturbate's edges, ISA fetches them WITHOUT our
    UA + Referer headers and gets 403'd, breaking quality switch.

    The chunklist rewriter must redirect those URIs through the proxy
    too, just like the master rewriter already does for the top-level
    chunklist URIs.
    """
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.master_body = (
        b"#EXTM3U\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
        b"chunklist_w12345_video.m3u8\n"
    )
    state.chunklist_body = (
        b"#EXTM3U\n"
        b"#EXT-X-VERSION:6\n"
        b"#EXT-X-TARGETDURATION:2\n"
        b"#EXTINF:2.0,\n"
        b"seg_1.m4s\n"
        b'#EXT-X-RENDITION-REPORT:URI="chunklist_w99999_video.m3u8",'
        b'LAST-MSN=42,LAST-PART=2\n'
        b"#EXT-X-ENDLIST\n"
    )
    upstream = f"{cdn_base}/hls/aa/master.m3u8"
    handle = hls_proxy.start_proxy(
        stream_url=upstream,
        room_url="https://chaturbate.com/alice/",
    )
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            master_body = resp.read().decode("utf-8")
        chunklist_url = next(
            line for line in master_body.splitlines()
            if line and not line.startswith("#")
        )
        with urlopen(chunklist_url, timeout=5) as resp:
            chunklist = resp.read().decode("utf-8")
        # The RENDITION-REPORT URI must NOT point at the upstream CDN.
        assert cdn_base not in chunklist, (
            f"upstream URL leaked through RENDITION-REPORT: {chunklist!r}"
        )
        # And it MUST be rewritten to /chunklist?name=... so ISA's
        # quality-switch fetch keeps our UA+Referer.
        prefix = f"http://{handle.host}:{handle.port}/chunklist?name="
        assert f'URI="{prefix}chunklist_w99999_video"' in chunklist
    finally:
        handle.stop()


def test_stop_releases_port(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """After stop(), the bound socket is released."""
    import socket

    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.master_body = b"#EXTM3U\n#EXT-X-ENDLIST\n"
    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    port = handle.port
    handle.stop()
    # Re-bind on the same port; if stop didn't release the socket this
    # raises OSError. SO_REUSEADDR is set by the proxy so this is the
    # canonical way to verify the port is genuinely free for re-bind.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    finally:
        s.close()


def test_two_proxies_get_different_ports(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """Each start_proxy gets its own port (no global singleton)."""
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.master_body = b"#EXTM3U\n#EXT-X-ENDLIST\n"
    a = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    b = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/master.m3u8",
        room_url="https://chaturbate.com/bob/",
    )
    try:
        assert a.port != b.port
    finally:
        a.stop()
        b.stop()


def test_start_proxy_stops_previous_active_proxy(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """Lesson 31: when starting a new proxy, stop any previously-tracked
    one first. Without this, daemon threads from prior proxies pile up
    until process exit because no caller calls .stop() on the old
    handle returned by the previous start_proxy().

    Lesson 34 (v0.7.13): the cleanup happens on a background daemon
    thread so the new playvid script doesn't block on
    ``server.shutdown()`` (which waits up to 5+ seconds for in-flight
    handler threads to drain - long enough for Kodi's CPythonInvoker
    to time out the addon script). Test waits for the bg cleanup to
    finish before asserting the port is rebindable.
    """
    import socket
    import time
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.master_body = b"#EXTM3U\n#EXT-X-ENDLIST\n"

    a = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    a_port = a.port

    b = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/master.m3u8",
        room_url="https://chaturbate.com/bob/",
    )
    try:
        # Cleanup is async (bg thread); poll up to 5s for the port to
        # be rebindable. Real-Kodi cleanup completes near-instantly
        # under stub_cdn since there are no in-flight requests.
        deadline = time.monotonic() + 5.0
        rebound = False
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", a_port))
                rebound = True
                break
            except OSError as exc:
                last_err = exc
                time.sleep(0.1)
            finally:
                s.close()
        assert rebound, (
            f"previous proxy port {a_port} still bound after 5s; "
            f"last err={last_err!r}"
        )
    finally:
        b.stop()


def test_master_url_path_distinct_from_internal_routes(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """The master path is /master.m3u8 (clean URL, ISA-friendly)."""
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.master_body = b"#EXTM3U\n#EXT-X-ENDLIST\n"
    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        assert handle.master_url.endswith("/master.m3u8")
    finally:
        handle.stop()


def test_unknown_path_returns_404(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """Random paths under the proxy return 404 (not a stack trace)."""
    from urllib.error import HTTPError

    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    state.master_body = b"#EXTM3U\n#EXT-X-ENDLIST\n"
    handle = hls_proxy.start_proxy(
        stream_url=f"{cdn_base}/master.m3u8",
        room_url="https://chaturbate.com/alice/",
    )
    try:
        url = f"http://{handle.host}:{handle.port}/something-random"
        with pytest.raises(HTTPError) as exc:
            urlopen(url, timeout=5)
        assert exc.value.code == 404
    finally:
        handle.stop()


# --------------------------------------------------------------------------- #
# v0.7.57: prefetch_ok flag (2026-06-07 husk incident)
# --------------------------------------------------------------------------- #


def test_start_proxy_sets_prefetch_ok_false_on_fetch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import resources.lib.hls_proxy as hp

    def _boom(url: str, headers: dict, timeout: float = 10.0):
        raise OSError("simulated 403 on stale edge session")

    monkeypatch.setattr(hp, "_fetch", _boom)
    handle = hp.start_proxy(
        "https://edge99.live.mmcdn.com/hls/stale/master.m3u8",
        "https://chaturbate.com/husk/",
    )
    try:
        assert handle.prefetch_ok is False
    finally:
        handle.stop()


def test_start_proxy_sets_prefetch_ok_true_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import resources.lib.hls_proxy as hp

    body = (b"#EXTM3U\n#EXT-X-VERSION:3\n"
            b"#EXT-X-STREAM-INF:BANDWIDTH=1000000\n"
            b"chunklist_1_video_42_llhls.m3u8\n")

    def _ok(url: str, headers: dict, timeout: float = 10.0):
        return body, "application/vnd.apple.mpegurl"

    monkeypatch.setattr(hp, "_fetch", _ok)
    handle = hp.start_proxy(
        "https://edge99.live.mmcdn.com/hls/fresh/master.m3u8",
        "https://chaturbate.com/healthy/",
    )
    try:
        assert handle.prefetch_ok is True
    finally:
        handle.stop()


# --------------------------------------------------------------------------- #
# v0.7.61: keep-alive connection pool (the llhls handshake-per-segment lag fix)
# --------------------------------------------------------------------------- #


def test_fetch_pools_keepalive_connection_for_same_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_fetch REUSES one keep-alive connection across back-to-back fetches to
    the same host instead of a fresh TCP+TLS handshake every time.

    Root cause of the llhls stutter (measured 2026-07-04): plain urlopen opens
    a new connection per segment (~180ms handshake vs ~39ms reused over the
    VPN), so the low-latency-HLS buffer drains -> renderer starves ->
    OutputPicture timeout. Pooling keeps one warm connection per edge host.
    """
    import http.server

    from resources.lib import cb_endpoints
    from resources.lib import hls_proxy as hp

    getattr(hp, "_pool_clear", lambda: None)()  # isolate from other tests

    accepted: list[Any] = []  # one entry per NEW tcp connection accepted

    class _CountingServer(http.server.ThreadingHTTPServer):
        # Threading (like the real proxy) so shutdown() doesn't block on the
        # handler thread that's parked reading the pooled keep-alive socket.
        daemon_threads = True

        def get_request(self) -> Any:
            pair = super().get_request()
            accepted.append(pair[1])
            return pair

    class _H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive capable

        def log_message(self, *_a: Any) -> None:
            return

        def do_GET(self) -> None:
            body = b"seg-bytes"
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    monkeypatch.setattr(cb_endpoints, "is_trusted_url", lambda _u: True)
    server = _CountingServer(("127.0.0.1", 0), _H)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://{host}:{port}/seg_1.m4s"
        b1, _c1 = hp._fetch(url, {})
        b2, _c2 = hp._fetch(url, {})
        b3, _c3 = hp._fetch(url, {})
        assert b1 == b2 == b3 == b"seg-bytes"
        assert len(accepted) == 1, (
            f"expected 1 pooled connection reused across 3 fetches, "
            f"got {len(accepted)} (no keep-alive)"
        )
    finally:
        # Close pooled conns FIRST so the parked keep-alive handler thread
        # unblocks, then the server shuts down cleanly.
        hp._pool_clear()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
