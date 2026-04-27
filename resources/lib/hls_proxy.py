"""Localhost rewriting proxy for Chaturbate HLS streams (Phase 4c).

The Phase 4a MVP only proxied the master playlist. ISA then went direct
to chaturbate edges for chunklists and segments, which 403'd because we
weren't able to inject the iPad UA + Referer on those follow-on
requests, and any single-use JWT in the chunklist URL was burned the
first time ISA fetched it (a refetch would 403 even with the right
headers).

This Phase 4c version proxies every layer (master -> chunklist ->
segment) and applies all eleven  PR #1818 lessons:

    47ccca1 - chunklist proxying + reconnect path skeleton
    1883c5a - retry reconnect on stream switch
    28ab198 - debug logging via gated _log
    00bdb74 - reconnect crash safety + STOP reinforcement
    5a01bef - watchdog after successful reconnect
    85b848d - serve cached playlists during reconnect
    ea3d1e7 - delay shutdown so ISA doesn't see ECONNREFUSED
    ba44c62 - segment proxying with three-tier fallback
    11721a3 - orphan cleanup + segment-fail wakes reconnect
    23c2cbb - lock the reconnect guard, split shutdown/close
    53acf18 - cache invalidation on refresh
    eb7785c - decompress gzipped manifests from mmcdn edges
    652f89d - terminal flag fast-path so ISA drains cleanly

Plus 7df873b's prefetch-master pattern is what start_proxy() does in
the entry path: master is fetched once with our headers so the JWT in
the URL gets used now, not on a later ISA hit.

State model (one ``_State`` instance per proxy invocation, protected
by the same lock as the ``ProxyHandle``):

    stream_url        - current upstream master URL (rotated on refresh)
    headers           - iPad UA + Referer
    url_map           - chunklist short-name -> absolute CDN URL
    chunklist_cache   - chunklist short-name -> last-known-good body
    seg_cdn_urls      - segment basename -> absolute CDN URL
    latest_seg        - chunklist short-name -> last-seen segment URL
    last_request      - timestamp of last ISA request
    last_refresh      - timestamp of last successful refresh_session
    reconnecting      - reentrancy guard
    stopping          - shutdown flag; threads exit cooperatively
    terminal          - giving up; handlers serve ENDLIST/410 fast path

Every meaningful state change calls ``logger._log`` so the user can
``tail -f chaturbatetv_feature.log`` and see the lifecycle of each
proxy instance. Logging is gated on the ``enh_debug`` setting; when
off, ``_log`` returns immediately with no I/O cost.
"""
from __future__ import annotations

import contextlib
import gzip
import re
import threading
import time
import zlib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse
from urllib.request import Request, urlopen


# iPad UA matches what  uses; Chaturbate blocks default UAs
# and bot-flavoured UAs alike. Don't touch this casually.
_IPAD_UA = (
    "Mozilla/5.0 (iPad; CPU OS 8_1 like Mac OS X) "
    "AppleWebKit/600.1.4 (KHTML, like Gecko) Version/8.0 "
    "Mobile/12B410 Safari/600.1.4"
)


_FETCH_TIMEOUT = 10.0

# Chaturbate's chunklist URLs follow ``chunklist_<bandwidth>_<track>``;
# we extract a stable "short name" key so we can map the same logical
# chunklist across session rotations. The middle token can be ``w12345``
# style on real chaturbate edges, ``\d+`` style on 's stub,
# and a-zA-Z digits in either - so we just match a word-class run.
# We also accept ``audio_*`` short names (EXT-X-MEDIA AUDIO entries are
# their own chunklists and must be proxied too).
_CHUNKLIST_NAME_RE = re.compile(r"((?:chunklist|audio)_\w+)")

# The big regex that finds an absolute chunklist URL anywhere in a
# master body so we can rewrite it to /chunklist?name=X.
_ABS_CHUNKLIST_RE = re.compile(
    r"^(https?://[^\s]+\.m3u8[^\s]*)$",
    re.MULTILINE,
)
_ABS_CHUNKLIST_URI_RE = re.compile(
    r'URI="(https?://[^"]+\.m3u8[^"]*)"',
    re.IGNORECASE,
)

# Segment URLs in chunklists - we rewrite these too so ISA never goes
# direct to the CDN.
_ABS_SEG_RE = re.compile(
    r"^(https?://[^\s]+\.m4s[^\s]*)$",
    re.MULTILINE,
)
_ABS_SEG_URI_RE = re.compile(
    r'URI="(https?://[^"]+\.m4s[^"]*)"',
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Logging helper - gated, redacted
# --------------------------------------------------------------------------- #


def _redact_url(url: str) -> str:
    """Drop secret-looking query values before they hit a log line.

    Chaturbate's master URL carries a single-use JWT in ``?token=...``
    and a few other parameters that uniquely identify the session. We
    don't want any of those persisting in a user-readable log on disk,
    so this function rewrites the value of any sensitive key to
    ``REDACTED``.
    """
    parsed = urlparse(url)
    if not parsed.query:
        return url
    redacted_keys = {"token", "sig", "session", "auth", "key"}
    parts = []
    for kv in parsed.query.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            if k.lower() in redacted_keys and v:
                parts.append(f"{k}=REDACTED")
            else:
                parts.append(kv)
        else:
            parts.append(kv)
    redacted_qs = "&".join(parts)
    return parsed._replace(query=redacted_qs).geturl()


def _log(msg: str) -> None:
    """Defer to resources.lib.logger for the gated write. Never raises."""
    try:
        from resources.lib import logger
        logger._log(f"hls_proxy: {msg}")
    except Exception:
        return


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #


@dataclass
class _State:
    """All per-proxy state. Lives behind ``_State.lock``."""

    stream_url: str
    headers: dict[str, str]
    url_map: dict[str, str] = field(default_factory=dict)
    chunklist_cache: dict[str, bytes] = field(default_factory=dict)
    seg_cdn_urls: dict[str, str] = field(default_factory=dict)
    latest_seg: dict[str, str] = field(default_factory=dict)
    # Master body (absolutized + rewritten to /chunklist?name=...) that
    # ISA fetches via /master.m3u8. Rebuilt on every successful refresh.
    master_body: bytes = b""
    last_request: float = 0.0
    last_refresh: float = 0.0
    reconnecting: bool = False
    stopping: bool = False
    terminal: bool = False
    # Tracks how many reconnect threads are alive at once. Used by the
    # test suite to assert the lock-protected guard works; production
    # never reads it.
    active_reconnect_threads: int = 0
    peak_reconnect_threads: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class ProxyHandle:
    """Return value of ``start_proxy``.

    The handle exposes a small, deliberate API:

    - ``master_url``: the URL to hand to ISA
    - ``stop()``: tear down the listener (idempotent)
    - ``refresh_session()``: re-fetch the master with fresh JWT
    - ``trigger_reconnect(reason)``: spawn the reconnect thread (or
      return immediately if one's already running). Lock-protected.
    - ``set_terminal()``: flip the terminal flag; subsequent /chunklist
      and /segment requests serve the fast-path bodies.
    - Test introspection: ``chunklist_cache_size()``,
      ``peak_reconnect_thread_count()``.
    """

    host: str
    port: int
    master_url: str
    _server: ThreadingHTTPServer
    _thread: threading.Thread
    _state: _State
    _stopped: bool = False

    def stop(self) -> None:
        """Shut the proxy down. Safe to call more than once."""
        if self._stopped:
            return
        self._stopped = True
        _log(f"stop: shutting down port={self.port}")
        # Mark stopping BEFORE shutdown so any in-flight reconnect or
        # watchdog thread bails on its next tick instead of churning.
        self._state.stopping = True
        # shutdown() and server_close() in SEPARATE try blocks (
        # 23c2cbb): if shutdown() ever throws on a race, server_close()
        # still releases the listening socket. Single try block leaks the
        # port until process exit.
        with contextlib.suppress(Exception):
            self._server.shutdown()
        with contextlib.suppress(Exception):
            self._server.server_close()
        self._thread.join(timeout=2.0)
        _log(f"stop: shutdown complete port={self.port}")

    # ------------------------------------------------------------------ #
    # Reconnect / refresh interface
    # ------------------------------------------------------------------ #

    def refresh_session(self) -> bool:
        """Re-fetch the master URL and rebuild the url_map under the lock.

        Returns True when the new master parsed cleanly, False on any
        upstream / parse failure. On success: also clears chunklist_cache,
        seg_cdn_urls, and latest_seg so the next ISA request forces a
        fresh CDN fetch with the new JWT (commit 53acf18 - "stale cache
        served dead segments after a session rotation"). The rewritten
        master body is also rebuilt so ISA's next /master.m3u8 hit sees
        the latest CDN URLs.
        """
        return _refresh_session(self._state, host=self.host, port=self.port)

    def trigger_reconnect(self, reason: str) -> None:
        """Spawn a background reconnect thread. Lock-guarded so concurrent
        chunklist / segment failures never spawn duplicate threads.
        """
        with self._state.lock:
            if self._state.reconnecting or self._state.stopping or self._state.terminal:
                return
            self._state.reconnecting = True
        _log(f"trigger_reconnect: reason={reason!r}")
        t = threading.Thread(
            target=_run_reconnect,
            args=(self._state,),
            name=f"chaturbatetv-reconnect-{self.port}",
            daemon=True,
        )
        t.start()

    def set_terminal(self) -> None:
        """Flip the terminal flag. Subsequent requests get the fast-path
        bodies.  commit 652f89d: this used to also call
        srv.shutdown() inline, which made ISA storm with ECONNREFUSED.
        Now we let the caller (or the player monitor) close the proxy
        when it's actually idle.
        """
        with self._state.lock:
            if self._state.terminal:
                return
            self._state.terminal = True
        _log("set_terminal: terminal flag raised, future requests serve fast-path")

    # ------------------------------------------------------------------ #
    # Test introspection
    # ------------------------------------------------------------------ #

    def chunklist_cache_size(self) -> int:
        with self._state.lock:
            return len(self._state.chunklist_cache)

    def peak_reconnect_thread_count(self) -> int:
        with self._state.lock:
            return self._state.peak_reconnect_threads


# --------------------------------------------------------------------------- #
# Master playlist rewriting (pure)
# --------------------------------------------------------------------------- #


_NON_COMMENT_LINE = re.compile(r"^(?!https?://)(?!#)(.+)$", re.MULTILINE)
_URI_ATTR_RELATIVE = re.compile(r'URI="(?!https?://)(.*?)"', re.IGNORECASE)


def rewrite_master(body: str, base_url: str) -> str:
    """Make every relative URL in a master.m3u8 absolute against ``base_url``.

    Pure function: no I/O, just regex. The chunklist-rewrite-to-proxy
    step is NOT done here; that's done in ``_build_master_for_isa``
    after we know the proxy port. This function is kept exposed so
    tests can hit just the absolutize step.
    """
    body = _NON_COMMENT_LINE.sub(
        lambda m: urljoin(base_url, m.group(1)),
        body,
    )
    body = _URI_ATTR_RELATIVE.sub(
        lambda m: f'URI="{urljoin(base_url, m.group(1))}"',
        body,
    )
    return body


def _harvest_chunklist_map(absolutized: str) -> dict[str, str]:
    """Pull ``chunklist_*`` short names out of an absolutized master so
    we can resolve ``/chunklist?name=X`` requests later.
    """
    out: dict[str, str] = {}
    # Plain lines.
    for line in absolutized.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "chunklist_" not in line:
            continue
        m = _CHUNKLIST_NAME_RE.search(line)
        if m:
            out[m.group(1)] = line
    # URI="..." attributes.
    for mi in _ABS_CHUNKLIST_URI_RE.finditer(absolutized):
        m = _CHUNKLIST_NAME_RE.search(mi.group(1))
        if m:
            out[m.group(1)] = mi.group(1)
    return out


def _rewrite_master_for_isa(absolutized: str, host: str, port: int) -> str:
    """Replace every absolute chunklist URL with a /chunklist?name=X hop
    through our localhost proxy. Leaves segment / non-chunklist lines
    alone (segment routing is handled inside the chunklist body).
    """
    def _repl(m: re.Match[str]) -> str:
        url = m.group(1)
        nm = _CHUNKLIST_NAME_RE.search(url)
        if not nm:
            return url
        return f"http://{host}:{port}/chunklist?name={nm.group(1)}"

    rewritten = _ABS_CHUNKLIST_RE.sub(_repl, absolutized)

    def _uri_repl(m: re.Match[str]) -> str:
        url = m.group(1)
        nm = _CHUNKLIST_NAME_RE.search(url)
        if not nm:
            return f'URI="{url}"'
        return f'URI="http://{host}:{port}/chunklist?name={nm.group(1)}"'

    rewritten = _ABS_CHUNKLIST_URI_RE.sub(_uri_repl, rewritten)
    return rewritten


def _rewrite_chunklist_for_isa(absolutized: str, host: str, port: int) -> str:
    """Replace every absolute segment URL in a chunklist body with a
    /segment?url=Y hop. Same shape as the master rewrite but for .m4s
    URIs.

    Also rewrites ``EXT-X-RENDITION-REPORT:URI="..."`` references to
    sibling chunklists (LL-HLS quality-switch hint) so ISA's fast-switch
    fetch goes through the proxy and keeps our UA+Referer. Without this,
    ISA sees the absolutized upstream URL, fetches it directly, and gets
    403'd by the chaturbate edge.
    """
    def _repl(m: re.Match[str]) -> str:
        seg = m.group(1)
        return f"http://{host}:{port}/segment?url={quote(seg, safe='')}"

    out = _ABS_SEG_RE.sub(_repl, absolutized)

    def _uri_repl(m: re.Match[str]) -> str:
        seg = m.group(1)
        return f'URI="http://{host}:{port}/segment?url={quote(seg, safe="")}"'

    out = _ABS_SEG_URI_RE.sub(_uri_repl, out)

    # RENDITION-REPORT URI rewrite: keep ISA quality-switch fetches
    # going through the proxy.
    def _chunk_uri_repl(m: re.Match[str]) -> str:
        url = m.group(1)
        nm = _CHUNKLIST_NAME_RE.search(url)
        if not nm:
            return f'URI="{url}"'
        return f'URI="http://{host}:{port}/chunklist?name={nm.group(1)}"'

    out = _ABS_CHUNKLIST_URI_RE.sub(_chunk_uri_repl, out)
    return out


def _harvest_segment_maps(absolutized_chunklist: str, type_key: str | None,
                          state: _State) -> None:
    """Walk the absolutized chunklist body, recording each segment's
    canonical CDN URL into ``seg_cdn_urls`` (keyed by basename) and the
    last segment per chunklist into ``latest_seg``. Used by the segment
    handler's three-tier fallback.
    """
    last_seen: str = ""
    for line in absolutized_chunklist.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ".m4s" not in line:
            continue
        basename = line.rsplit("/", 1)[-1].split("?", 1)[0]
        with state.lock:
            state.seg_cdn_urls[basename] = line
        last_seen = line
    if last_seen and type_key:
        with state.lock:
            state.latest_seg[type_key] = last_seen


# --------------------------------------------------------------------------- #
# HTTP fetcher with gzip/deflate handling
# --------------------------------------------------------------------------- #


def _fetch(url: str, headers: dict[str, str],
           timeout: float = _FETCH_TIMEOUT) -> tuple[bytes, str]:
    """Fetch ``url``, decompressing gzip/deflate bodies.

    Returns ``(body_bytes, content_type)``. Some mmcdn edges send gzip
    without ``Content-Encoding`` set, so we also detect by magic bytes
    (eb7785c).
    """
    req = Request(url, headers=headers)  # noqa: S310 - chaturbate edge URL
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - chaturbate edge URL
        raw: bytes = resp.read()
        ce = (resp.headers.get("Content-Encoding") or "").lower()
        ct: str = resp.headers.get("Content-Type") or ""
    if ce == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, gzip.BadGzipFile):
            pass
    elif ce == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                pass
    return raw, ct


# --------------------------------------------------------------------------- #
# Refresh + reconnect
# --------------------------------------------------------------------------- #


def _refresh_session(state: _State, host: str = "", port: int = 0) -> bool:
    """Re-fetch the master URL and rebuild the url_map under the lock.

    Caches are invalidated on success (53acf18). Same-URL refreshes
    within 2 seconds are deduped to avoid hot-spinning when ISA fires
    multiple chunklist failures in rapid succession.

    ``host`` / ``port`` are used to also rebuild ``state.master_body``
    so a /master.m3u8 fetch after a refresh sees the new structure.
    Default empty/0 means "leave master_body alone" - useful from
    tests that don't care about it.
    """
    now = time.time()
    with state.lock:
        if now - state.last_refresh < 2:
            _log("refresh_session: dedup skip (within 2s of last refresh)")
            return False
        state.last_refresh = now
    redacted = _redact_url(state.stream_url)
    try:
        raw, _ct = _fetch(state.stream_url, state.headers)
    except Exception as exc:
        _log(f"refresh_session: FAIL upstream={redacted!r} err={exc!r}")
        return False
    try:
        body = raw.decode("utf-8", "replace")
    except Exception as exc:
        _log(f"refresh_session: FAIL decode err={exc!r}")
        return False
    base = state.stream_url.rsplit("/", 1)[0] + "/"
    absolutized = rewrite_master(body, base)
    new_map = _harvest_chunklist_map(absolutized)
    new_master_body = b""
    if host and port:
        new_master_body = _rewrite_master_for_isa(absolutized, host, port).encode("utf-8")
    with state.lock:
        state.url_map.update(new_map)
        if new_master_body:
            state.master_body = new_master_body
        # 53acf18: clear caches so the next ISA hit forces a fresh fetch
        # with the new JWT.
        state.chunklist_cache.clear()
        state.seg_cdn_urls.clear()
        state.latest_seg.clear()
    _log(f"refresh_session: OK new_keys={list(new_map.keys())} caches_cleared")
    return True


def _run_reconnect(state: _State) -> None:
    """Background reconnect loop. Tries up to 5 refreshes (652f89d
    reduced from 15 -> 5 since each refresh already handles one
    rotation; 5 is enough to survive a brief CDN blip).

    Always runs inside try/except/finally so a crash logs a message
    rather than silently dying ( 00bdb74). Releases the
    reconnecting flag and (only on a clean give-up) flips the terminal
    flag so handlers go fast-path.
    """
    with state.lock:
        state.active_reconnect_threads += 1
        state.peak_reconnect_threads = max(
            state.peak_reconnect_threads, state.active_reconnect_threads,
        )
    needs_terminal = True
    try:
        for attempt in range(1, 6):
            if state.stopping or state.terminal:
                _log(f"reconnect: aborted (stopping/terminal) at attempt={attempt}")
                needs_terminal = False
                return
            _log(f"reconnect: attempt={attempt}/5")
            if _refresh_session(state):
                _log(f"reconnect: OK at attempt={attempt}")
                with state.lock:
                    state.reconnecting = False
                # Watchdog: 8s sleep, then check ISA still talking.
                # Two-stage check (53acf18 /  tuned values)
                # avoids false positives on slow CDN recoveries.
                _log("reconnect: watchdog waiting 8s for ISA")
                _sleep_or_stop(state, 8.0)
                if state.stopping or state.terminal:
                    needs_terminal = False
                    return
                gap = time.time() - state.last_request
                _log(f"reconnect: watchdog gap={gap:.1f}s")
                if gap > 6:
                    _log("reconnect: gap over threshold, rechecking in 3s")
                    _sleep_or_stop(state, 3.0)
                    if state.stopping or state.terminal:
                        needs_terminal = False
                        return
                    gap2 = time.time() - state.last_request
                    _log(f"reconnect: watchdog recheck gap={gap2:.1f}s")
                    if gap2 > 6:
                        _log("reconnect: ISA silent after recheck, going terminal")
                        needs_terminal = True
                        return
                    _log("reconnect: watchdog OK on recheck")
                else:
                    _log("reconnect: watchdog OK first check")
                needs_terminal = False
                return
            # Refresh failed; back off for 2s and try again.
            _sleep_or_stop(state, 2.0)
        _log("reconnect: GIVING UP after 5 attempts")
    except Exception as exc:
        _log(f"reconnect: THREAD CRASHED: {exc!r}")
    finally:
        with state.lock:
            state.active_reconnect_threads = max(
                0, state.active_reconnect_threads - 1,
            )
            state.reconnecting = False
        if needs_terminal and not state.stopping and not state.terminal:
            _log("reconnect: flipping terminal=True")
            with state.lock:
                state.terminal = True


def _sleep_or_stop(state: _State, seconds: float) -> None:
    """Sleep up to ``seconds`` total, in 0.5s increments so a stop or
    terminal flag wakes us promptly.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if state.stopping or state.terminal:
            return
        remaining = deadline - time.time()
        time.sleep(min(0.5, max(0.0, remaining)))


# --------------------------------------------------------------------------- #
# Request handler
# --------------------------------------------------------------------------- #


_ENDLIST_BODY = b"#EXTM3U\n#EXT-X-ENDLIST\n"


def _make_handler(host: str, port: int, state: _State,
                  refresh_fn: Any, trigger_fn: Any) -> type[BaseHTTPRequestHandler]:
    """Build a BaseHTTPRequestHandler bound to this proxy instance.

    Three routes:

    - ``/master.m3u8`` -> serve the rewritten master (cached at start).
    - ``/chunklist?name=X`` -> fetch the chunklist for X from the
      current CDN URL, absolutize, rewrite segment URLs, cache, return.
      On upstream failure: trigger reconnect, serve cached body if
      present, else ENDLIST.
    - ``/segment?url=Y`` -> fetch Y with iPad headers, three-tier
      fallback on failure. Pass through Content-Type.
    """

    class _H(BaseHTTPRequestHandler):
        # Silence stdlib stderr access log; we have our own _log.
        def log_message(self, *_args: Any) -> None:
            return

        def do_GET(self) -> None:
            with state.lock:
                state.last_request = time.time()
            try:
                if self.path.startswith("/master.m3u8"):
                    self._serve_master()
                    return
                if self.path.startswith("/chunklist"):
                    self._serve_chunklist()
                    return
                if self.path.startswith("/segment"):
                    self._serve_segment()
                    return
                self.send_error(404)
            except Exception as exc:
                _log(f"handler: unhandled error path={self.path!r} err={exc!r}")
                with contextlib.suppress(Exception):
                    self.send_error(500)

        def do_HEAD(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.end_headers()

        # ---------------- handlers -----------------

        def _serve_master(self) -> None:
            # Master is cached in state.url_map's source body; we
            # rebuild from url_map each time so a refresh is reflected.
            body = _build_master_for_isa(host, port, state)
            _log(f"handler: master.m3u8 -> {len(body)} bytes")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_chunklist(self) -> None:
            # Terminal fast-path: HTTP 410 so ISA gives up cleanly.
            # ENDLIST-with-no-segments produces a tight retry loop because
            # ISA logs "ParseChildManifest: No segments in the manifest"
            # and immediately re-fetches (saw 100s of req/sec on 
            # 2026-04-27 07:57). 410 matches the segment terminal path
            # and tells ISA the resource is permanently gone - stop
            # retrying.
            if state.terminal:
                _log("handler: chunklist terminal-flag fast path -> 410")
                self.send_error(410)
                return
            qs = parse_qs(urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            if not name:
                self.send_error(400)
                return
            with state.lock:
                cdn_url = state.url_map.get(name, "")
            if not cdn_url:
                _log(f"handler: chunklist name={name!r} not in url_map")
                # No mapping known. Serve cache if available; else 410.
                # ENDLIST-with-no-segments here triggered the same retry
                # loop ISA does on terminal — 410 stops it cleanly.
                with state.lock:
                    cached = state.chunklist_cache.get(name)
                if cached:
                    self._send_body(cached, "application/vnd.apple.mpegurl")
                    return
                self.send_error(410)
                return
            try:
                raw, _ct = _fetch(cdn_url, state.headers)
            except Exception as exc:
                _log(
                    f"handler: chunklist FAIL name={name!r} "
                    f"url={_redact_url(cdn_url)!r} err={exc!r}"
                )
                # Trigger reconnect (lock-guarded).
                trigger_fn(f"chunklist fail name={name}")
                with state.lock:
                    cached = state.chunklist_cache.get(name)
                if cached:
                    _log(f"handler: chunklist serving cached body name={name!r}")
                    self._send_body(cached, "application/vnd.apple.mpegurl")
                    return
                # No cache + upstream failed = nothing to serve. 410
                # rather than empty-ENDLIST so ISA stops retrying
                # instead of hammering us 100/sec.
                self.send_error(410)
                return
            body = raw.decode("utf-8", "replace")
            cbase = cdn_url.rsplit("/", 1)[0] + "/"
            absolutized = rewrite_master(body, cbase)
            _harvest_segment_maps(absolutized, name, state)
            rewritten = _rewrite_chunklist_for_isa(absolutized, host, port)
            payload = rewritten.encode("utf-8")
            with state.lock:
                state.chunklist_cache[name] = payload
            # Per-chunklist OK is too chatty during steady-state playback
            # (1-2 lines/sec from disk I/O). Errors / reconnects still log.
            self._send_body(payload, "application/vnd.apple.mpegurl")

        def _serve_segment(self) -> None:
            if state.terminal:
                _log("handler: segment terminal-flag fast path -> 410")
                self.send_error(410)
                return
            qs = parse_qs(urlparse(self.path).query)
            seg_url = (qs.get("url") or [""])[0]
            if not seg_url:
                self.send_error(400)
                return
            seg_name = seg_url.rsplit("/", 1)[-1].split("?", 1)[0]
            # Tier 1: try the URL ISA asked for.
            try:
                raw, ct = _fetch(seg_url, state.headers)
                # Per-segment OK on tier 1 is the steady-state hot path
                # (1+ line/sec). Skip the log; tier-2/3 fallbacks DO log
                # since they signal something interesting.
                self._send_body(raw, ct or "video/mp4")
                return
            except Exception as exc:
                _log(
                    f"handler: segment FAIL tier1 name={seg_name!r} "
                    f"url={_redact_url(seg_url)!r} err={exc!r}"
                )
                trigger_fn(f"segment fail name={seg_name}")
            # Tier 2: try the current CDN URL for the same segment name.
            with state.lock:
                fallback = state.seg_cdn_urls.get(seg_name)
            if fallback and fallback != seg_url:
                try:
                    raw, ct = _fetch(fallback, state.headers)
                    _log(f"handler: segment OK tier2 name={seg_name!r}")
                    self._send_body(raw, ct or "video/mp4")
                    return
                except Exception as exc:
                    _log(f"handler: segment FAIL tier2 err={exc!r}")
            # Tier 3: serve the latest known segment for the matching
            # track. Best-effort; might be the wrong frame but keeps
            # the buffer warm during a CDN rotation.
            tier3_url = ""
            tm = re.search(r"(video|audio)_(\d+)", seg_name)
            if tm:
                kind, idx = tm.group(1), tm.group(2)
                with state.lock:
                    for k, v in state.latest_seg.items():
                        if kind in k and idx in k:
                            tier3_url = v
                            break
            if tier3_url:
                try:
                    raw, ct = _fetch(tier3_url, state.headers)
                    _log(f"handler: segment OK tier3 name={seg_name!r}")
                    self._send_body(raw, ct or "video/mp4")
                    return
                except Exception as exc:
                    _log(f"handler: segment FAIL tier3 err={exc!r}")
            self.send_error(502)

        # ---------------- helpers ------------------

        def _send_body(self, body: bytes, ct: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_endlist(self) -> None:
            self._send_body(_ENDLIST_BODY, "application/vnd.apple.mpegurl")

    return _H


def _build_master_for_isa(host: str, port: int, state: _State) -> bytes:
    """Return the master playlist body ISA will see.

    The body was built once at start_proxy / refresh_session time
    (preserving the upstream's EXT-X-MEDIA / EXT-X-STREAM-INF structure)
    and stored in ``state.master_body``. This function just hands it
    back. If the prefetch failed, fall back to a tiny EXTM3U envelope
    so ISA gets something legal until a chunklist hit drives a recovery.
    """
    with state.lock:
        body = state.master_body
    if body:
        return body
    return b"#EXTM3U\n#EXT-X-VERSION:3\n"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def start_proxy(stream_url: str, room_url: str,
                port: int = 0) -> ProxyHandle:
    """Bind a localhost HTTP server and start serving the rewritten master.

    Args:
        stream_url: The Chaturbate ``hls_source`` we got from the
            dossier. Carries a single-use JWT in the path; expires in
            ~60-90s.
        room_url: ``https://chaturbate.com/<slug>/`` - used as the
            ``Referer`` upstream. Chaturbate edges 403 without it.
        port: Localhost port to bind. ``0`` (default) lets the kernel
            assign one. Settings.xml exposes ``isa_proxy_port`` for
            users on locked-down LAN firewalls who need a fixed port.

    Returns:
        A ``ProxyHandle`` whose ``master_url`` is the URL to hand to
        ISA. The caller must keep a reference until playback ends and
        then call ``handle.stop()``.
    """
    headers = {
        "User-Agent": _IPAD_UA,
        "Referer": room_url,
    }
    redacted = _redact_url(stream_url)
    _log(f"start_proxy: stream_url={redacted!r} room_url={room_url!r}")

    state = _State(stream_url=stream_url, headers=headers)
    # Stash the absolutized master so we can rewrite to /chunklist?name=...
    # once we know the bound port.
    prefetch_absolutized = ""
    # Prefetch the master so the JWT in the URL gets used now (7df873b).
    # Best-effort: failure here doesn't kill the proxy, the chunklist
    # handler will trigger reconnect and recover when ISA hits us.
    try:
        raw, _ct = _fetch(stream_url, headers)
        body = raw.decode("utf-8", "replace")
        base = stream_url.rsplit("/", 1)[0] + "/"
        prefetch_absolutized = rewrite_master(body, base)
        with state.lock:
            state.url_map = _harvest_chunklist_map(prefetch_absolutized)
        _log(f"start_proxy: prefetch OK url_map_keys={list(state.url_map.keys())}")
    except Exception as exc:
        _log(f"start_proxy: prefetch FAIL err={exc!r}")
        # Empty url_map; chunklist handler will trigger reconnect on first
        # ISA hit, which will populate the map.

    # Bind to port 0 -> kernel assigns; allow_reuse_address guards
    # against TIME_WAIT noise on rapid restart.
    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    # The handler needs callbacks that ultimately call methods on the
    # ProxyHandle (refresh_session, trigger_reconnect). The handle
    # itself can't be built until the server is bound (it carries the
    # _server attr). Resolve the cycle with a one-element holder list
    # that is populated BEFORE thread.start() so the closures are
    # always non-None by the time any request lands.
    handle_box: list[Any] = [None]

    def _refresh() -> bool:
        h = handle_box[0]
        return bool(h.refresh_session()) if h is not None else False

    def _trigger(reason: str) -> None:
        h = handle_box[0]
        if h is not None:
            h.trigger_reconnect(reason)

    # Step 1: bind the server (with a placeholder handler factory just
    # to allocate a port - port=0 lets the kernel pick one).
    handler_cls = _make_handler("127.0.0.1", 0, state, _refresh, _trigger)
    server = _Server(("127.0.0.1", port), handler_cls)
    raw_host = server.server_address[0]
    host = raw_host if isinstance(raw_host, str) else raw_host.decode("ascii")
    port = int(server.server_address[1])

    # Step 2: with the port now known, rewrite the prefetched master so
    # ISA gets /chunklist?name=X URLs instead of upstream CDN URLs.
    if prefetch_absolutized:
        master_body = _rewrite_master_for_isa(
            prefetch_absolutized, host, port,
        ).encode("utf-8")
        with state.lock:
            state.master_body = master_body

    # Step 3: re-build the handler with the actual port baked in
    # (the URL rewriting needs the bound port).
    server.RequestHandlerClass = _make_handler(
        host, port, state, _refresh, _trigger,
    )

    # Step 4: build the thread (NOT started yet) and the handle.
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"chaturbatetv-hls-proxy-{port}",
        daemon=True,
    )
    handle = ProxyHandle(
        host=host,
        port=port,
        master_url=f"http://{host}:{port}/master.m3u8",
        _server=server,
        _thread=thread,
        _state=state,
    )

    # Step 5: populate the closure box BEFORE serving, so any incoming
    # request (including an immediate ISA fetch) sees a valid callback.
    handle_box[0] = handle
    thread.start()

    _log(f"start_proxy: bound host={host} port={port}")
    return handle
