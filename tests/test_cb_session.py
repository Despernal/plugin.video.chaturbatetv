"""Tests for resources.lib.cb_session - cookie jar wrapper with expiry.

We use http.cookiejar.LWPCookieJar so the on-disk format matches what
 already uses; that gives us a smooth migration path.
"""
from __future__ import annotations

import time
from pathlib import Path

from resources.lib.cb_session import Session


# Helper to write an LWP cookie file we can load ------------------------------

def _write_lwp_file(path: Path, expires: int, domain: str = "chaturbate.com",
                    name: str = "sessionid", value: str = "abc123") -> None:
    """Write a minimal LWPCookieJar-compatible file with one cookie."""
    content = (
        "#LWP-Cookies-2.0\n"
        f'Set-Cookie3: {name}={value}; path="/"; '
        f'domain={domain}; path_spec; secure; '
        f'expires="{time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(expires))}"; '
        'version=0\n'
    )
    path.write_text(content)


# load_from / save_to ---------------------------------------------------------

def test_load_from_missing_file_yields_empty_session(tmp_path) -> None:
    s = Session()
    s.load_from(tmp_path / "nonexistent.lwp")
    assert s.cookies_for_url("https://chaturbate.com/") == {}


def test_load_from_existing_file(tmp_path) -> None:
    p = tmp_path / "cookies.lwp"
    _write_lwp_file(p, expires=int(time.time()) + 3600)
    s = Session()
    s.load_from(p)
    cookies = s.cookies_for_url("https://chaturbate.com/")
    assert cookies.get("sessionid") == "abc123"


def test_save_to_round_trip(tmp_path) -> None:
    src = tmp_path / "src.lwp"
    dst = tmp_path / "dst.lwp"
    _write_lwp_file(src, expires=int(time.time()) + 3600)
    s = Session()
    s.load_from(src)
    s.save_to(dst)
    assert dst.exists()
    s2 = Session()
    s2.load_from(dst)
    assert s2.cookies_for_url("https://chaturbate.com/") == {"sessionid": "abc123"}


def test_save_to_creates_parent_dir(tmp_path) -> None:
    s = Session()
    p = tmp_path / "deep" / "cookies.lwp"
    s.save_to(p)
    assert p.exists()


# is_expired ------------------------------------------------------------------

def test_is_expired_when_no_session_cookie() -> None:
    """Empty jar -> nothing to expire, treat as expired (must re-auth)."""
    s = Session()
    assert s.is_expired() is True


def test_is_expired_when_cookie_in_future(tmp_path) -> None:
    p = tmp_path / "c.lwp"
    _write_lwp_file(p, expires=int(time.time()) + 3600)
    s = Session()
    s.load_from(p)
    assert s.is_expired() is False


def test_is_expired_when_cookie_in_past(tmp_path) -> None:
    p = tmp_path / "c.lwp"
    _write_lwp_file(p, expires=int(time.time()) - 60)
    s = Session()
    s.load_from(p)
    # LWPCookieJar drops expired cookies on load by default; result is "no session" = expired.
    assert s.is_expired() is True


def test_is_expired_uses_injected_clock(tmp_path) -> None:
    """Session takes a now() callable so frozen-time tests are easy.

    LWPCookieJar drops past-expiry cookies on load by default (uses real
    wall-clock); to test the injected clock we use a future-dated cookie
    and check that an injected ``now`` past its expiry flips the result.
    """
    p = tmp_path / "c.lwp"
    far_future = int(time.time()) + 7 * 24 * 3600  # one week from now
    _write_lwp_file(p, expires=far_future)

    s_before = Session(now=lambda: float(time.time()))
    s_before.load_from(p)
    assert s_before.is_expired() is False

    s_after = Session(now=lambda: float(far_future + 1))
    s_after.load_from(p)
    assert s_after.is_expired() is True


# cookies_for_url -------------------------------------------------------------

def test_cookies_for_url_only_returns_chaturbate_cookies(tmp_path) -> None:
    p = tmp_path / "c.lwp"
    expiry = int(time.time()) + 3600
    p.write_text(
        '#LWP-Cookies-2.0\n'
        f'Set-Cookie3: cb=value1; path="/"; domain=chaturbate.com; path_spec; secure; '
        f'expires="{time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(expiry))}"; version=0\n'
        f'Set-Cookie3: other=value2; path="/"; domain=example.com; path_spec; secure; '
        f'expires="{time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(expiry))}"; version=0\n'
    )
    s = Session()
    s.load_from(p)
    cb_cookies = s.cookies_for_url("https://chaturbate.com/")
    other_cookies = s.cookies_for_url("https://example.com/")
    assert cb_cookies == {"cb": "value1"}
    assert other_cookies == {"other": "value2"}


def test_cookies_for_url_empty_when_no_match(tmp_path) -> None:
    p = tmp_path / "c.lwp"
    _write_lwp_file(p, expires=int(time.time()) + 3600, domain="chaturbate.com")
    s = Session()
    s.load_from(p)
    assert s.cookies_for_url("https://example.com/") == {}


def test_cookies_for_url_handles_subdomain(tmp_path) -> None:
    """A cookie scoped to chaturbate.com should also send to st.chaturbate.com."""
    p = tmp_path / "c.lwp"
    expiry = int(time.time()) + 3600
    p.write_text(
        '#LWP-Cookies-2.0\n'
        f'Set-Cookie3: cb=v; path="/"; domain=.chaturbate.com; path_spec; '
        f'domain_dot; secure; '
        f'expires="{time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(expiry))}"; version=0\n'
    )
    s = Session()
    s.load_from(p)
    assert s.cookies_for_url("https://st.chaturbate.com/") == {"cb": "v"}


def test_cookies_for_url_invalid_url_returns_empty() -> None:
    """Defensive: garbage URL returns {} rather than crashing."""
    s = Session()
    assert s.cookies_for_url("not-a-url") == {}


def test_session_init_no_cookies() -> None:
    s = Session()
    assert s.cookies_for_url("https://chaturbate.com/") == {}
