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
def stub_cdn() -> Iterator[tuple[str, _StubState]]:
    """Run a stub HTTP server on 127.0.0.1:<random> and yield its base URL."""
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


def test_master_playlist_relative_urls_are_rewritten_absolute(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """A master.m3u8 with relative chunklist refs comes back with absolute URLs."""
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
        assert f"{cdn_base}/hls/abc/chunklist_w12345_video.m3u8" in body
        assert f"{cdn_base}/hls/abc/chunklist_w67890_video.m3u8" in body
        # No bare relative path should remain in a non-comment line.
        for line in body.splitlines():
            if line and not line.startswith("#"):
                assert line.startswith("http://"), f"unrewritten: {line!r}"
    finally:
        handle.stop()


def test_master_playlist_uri_quoted_relative_urls_are_rewritten(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """URI="..." attributes (e.g. EXT-X-MEDIA) get absolutized too."""
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
        assert f'URI="{cdn_base}/hls/zz/audio_w7777.m3u8"' in body
    finally:
        handle.stop()


def test_master_playlist_already_absolute_urls_pass_through(
    stub_cdn: tuple[str, _StubState],
) -> None:
    """Absolute URLs in the master should be left alone."""
    from resources.lib import hls_proxy

    cdn_base, state = stub_cdn
    pre = (
        b"#EXTM3U\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
        b"https://other-edge.example.com/abs/chunklist.m3u8\n"
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
        assert "https://other-edge.example.com/abs/chunklist.m3u8" in body
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
    """One chunklist passed through end-to-end via an absolute URL.

    MVP: the master rewrite hands ISA an absolute upstream chunklist URL;
    the proxy itself does not yet handle /chunklist routes (Phase 4c).
    Confirm the rewrite makes the chunklist reachable when fetched
    directly from the stub CDN.
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
    # Stub serves /chunklist for any chunklist path; we just need it to
    # be reachable from the rewritten URL.
    upstream = f"{cdn_base}/chunklist/master.m3u8"
    handle = hls_proxy.start_proxy(
        stream_url=upstream,
        room_url="https://chaturbate.com/alice/",
    )
    try:
        with urlopen(handle.master_url, timeout=5) as resp:
            master_body = resp.read().decode("utf-8")
        # Pull out the absolutized chunklist URL the proxy returned.
        chunklist_url = next(
            line for line in master_body.splitlines()
            if line and not line.startswith("#")
        )
        with urlopen(chunklist_url, timeout=5) as resp:
            chunklist = resp.read()
        assert chunklist == state.chunklist_body
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
