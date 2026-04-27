"""Localhost rewriting proxy for Chaturbate HLS streams.

ISA v20+ rejects ``file://`` paths and won't accept the raw upstream
URL when it needs custom request headers per-segment, so we run a
tiny localhost HTTP server that:

1. Fetches the master playlist from Chaturbate with the correct
   ``User-Agent`` + ``Referer`` headers.
2. Rewrites every relative URL inside the master to its absolute
   upstream form (so subsequent ISA fetches reach the CDN directly
   without going through us again - that part is the Phase 4c
   hardening; this MVP only proxies the master).
3. Serves the rewritten master to ISA on ``http://127.0.0.1:<port>/master.m3u8``.

This file is the Phase 4a MVP. No gzip handling, no chunklist proxy,
no segment proxy, no session refresh, no CDN rotation, no reconnect
loop. All of that lands in Phase 4c. For the MVP we accept that
streams die when the JWT expires (~60-90s) - we just need ISA to be
able to start playback through us at all.

The proxy runs in a daemon background thread. ``start_proxy`` returns
a ``ProxyHandle`` that the caller (``playvid_resolver``) keeps a
reference to until playback ends; ``handle.stop()`` shuts the server
down cleanly.

Threading model: ``ThreadingHTTPServer`` is used so multiple ISA
connections (master + chunklist + segment) can be served in parallel
once Phase 4c lands. For Phase 4a one request at a time would be
fine, but we lock in the threaded server now to avoid ripping it out
later.
"""
from __future__ import annotations

import contextlib
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen


# iPad-style UA matches what  uses; Chaturbate blocks default
# urllib UAs and strict bot UAs alike. Don't touch this casually.
_IPAD_UA = (
    "Mozilla/5.0 (iPad; CPU OS 8_1 like Mac OS X) "
    "AppleWebKit/600.1.4 (KHTML, like Gecko) Version/8.0 "
    "Mobile/12B410 Safari/600.1.4"
)


_FETCH_TIMEOUT = 10.0


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #


@dataclass
class ProxyHandle:
    """Return value of ``start_proxy``.

    ``master_url`` is the URL the resolver hands to ISA. ``stop()``
    shuts the server down and releases the bound port.
    """

    host: str
    port: int
    master_url: str
    _server: ThreadingHTTPServer
    _thread: threading.Thread

    def stop(self) -> None:
        """Shut the proxy down. Safe to call more than once.

        ``shutdown()`` and ``server_close()`` get separate suppression
        blocks because the  history (PR #1818) showed that
        wrapping them together leaks the socket: if ``shutdown()``
        throws on a race, ``server_close()`` never runs and the port
        stays bound.
        """
        with contextlib.suppress(Exception):
            self._server.shutdown()
        with contextlib.suppress(Exception):
            self._server.server_close()
        # Don't join the serve thread forever; the daemon flag means the
        # process can exit even if the join times out.
        self._thread.join(timeout=2.0)


# --------------------------------------------------------------------------- #
# Master playlist rewriting (pure)
# --------------------------------------------------------------------------- #


_NON_COMMENT_LINE = re.compile(r"^(?!https?://)(?!#)(.+)$", re.MULTILINE)
_URI_ATTR = re.compile(r'URI="(?!https?://)(.*?)"', re.IGNORECASE)


def rewrite_master(body: str, base_url: str) -> str:
    """Make every relative URL in a master.m3u8 absolute against ``base_url``.

    Pure function: no I/O, just regex. Two transforms:

    1. Standalone playlist lines (``chunklist_xxx.m3u8``) -> absolute.
    2. ``URI="..."`` attributes (used by ``EXT-X-MEDIA``) -> absolute.

    Lines that are already absolute or are comments are left alone.
    """
    body = _NON_COMMENT_LINE.sub(
        lambda m: urljoin(base_url, m.group(1)),
        body,
    )
    body = _URI_ATTR.sub(
        lambda m: f'URI="{urljoin(base_url, m.group(1))}"',
        body,
    )
    return body


# --------------------------------------------------------------------------- #
# Internal: per-proxy state + handler
# --------------------------------------------------------------------------- #


@dataclass
class _State:
    """Captures everything the request handler needs at runtime."""

    stream_url: str
    headers: dict[str, str]


def _make_handler(state: _State) -> type[BaseHTTPRequestHandler]:
    """Build a BaseHTTPRequestHandler subclass that closes over ``state``.

    Subclassing rather than instance-attaching lets the standard
    ``HTTPServer`` machinery instantiate the handler per-request.
    """

    class _Handler(BaseHTTPRequestHandler):
        # Quiet the stdlib stderr access log; we'll add our own gated
        # logging in Phase 4c.
        def log_message(self, *_args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path.startswith("/master.m3u8"):
                self._serve_master()
                return
            self.send_error(404)

        def _serve_master(self) -> None:
            try:
                req = Request(state.stream_url, headers=state.headers)  # noqa: S310 - chaturbate edge URL
                with urlopen(req, timeout=_FETCH_TIMEOUT) as resp:  # noqa: S310 - chaturbate edge URL
                    raw = resp.read()
                body = raw.decode("utf-8", "replace")
                base = state.stream_url.rsplit("/", 1)[0] + "/"
                rewritten = rewrite_master(body, base).encode("utf-8")
            except Exception:  # upstream sideways -> 502 (no triage needed in MVP)
                self.send_error(502)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(rewritten)))
            self.end_headers()
            self.wfile.write(rewritten)

    return _Handler


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def start_proxy(stream_url: str, room_url: str) -> ProxyHandle:
    """Bind a localhost HTTP server and start serving the rewritten master.

    Args:
        stream_url: The Chaturbate ``hls_source`` we got from the
            dossier. Carries a single-use JWT in the path; expires in
            ~60-90s.
        room_url: ``https://chaturbate.com/<slug>/`` - used as the
            ``Referer`` upstream. Chaturbate edges 403 without it.

    Returns:
        A ``ProxyHandle`` whose ``master_url`` is the URL to hand to
        ISA. The caller must keep a reference until playback ends and
        then call ``handle.stop()``.
    """
    headers = {
        "User-Agent": _IPAD_UA,
        "Referer": room_url,
    }
    state = _State(stream_url=stream_url, headers=headers)
    handler_cls = _make_handler(state)

    # Bind to port 0 -> kernel picks a free port. allow_reuse_address
    # makes restarts on the same port safe if the previous proxy
    # released it cleanly.
    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = _Server(("127.0.0.1", 0), handler_cls)
    # server_address types as ``str | bytes | bytearray`` because the
    # base TCPServer covers Unix sockets too. We bound AF_INET above so
    # the host is a real string; coerce defensively for mypy --strict
    # and to keep the f-string from emitting ``b'127.0.0.1'``.
    raw_host = server.server_address[0]
    host = raw_host if isinstance(raw_host, str) else raw_host.decode("ascii")
    port = int(server.server_address[1])

    thread = threading.Thread(
        target=server.serve_forever,
        name=f"chaturbatetv-hls-proxy-{port}",
        daemon=True,
    )
    thread.start()

    return ProxyHandle(
        host=host,
        port=port,
        master_url=f"http://{host}:{port}/master.m3u8",
        _server=server,
        _thread=thread,
    )
