"""Tests for browser_ua: fresh, rotating iPad Safari User-Agents.

Contract (written before implementation, TDD):
- a pool of >=5 FRESH iPad Safari UAs (iOS >= 15, never the old iOS 8.1 smell)
- pick() returns a random pool member
- session_ua() chooses once + caches, so the resolve path and the stream
  proxy present the SAME UA within one run (no mid-session mismatch).
"""
from __future__ import annotations

import re

from resources.lib import browser_ua


def test_pool_has_variety() -> None:
    pool = browser_ua.all_uas()
    assert len(pool) >= 5, "need a real pool to randomize over"
    assert len(set(pool)) == len(pool), "no duplicate UAs in the pool"


def test_every_ua_is_ipad_safari() -> None:
    # Chaturbate serves the llhls streams to iPad UAs and blocks bot/default
    # UAs; keep the whole pool iPad Safari.
    for ua in browser_ua.all_uas():
        assert ua.startswith("Mozilla/5.0 (iPad;"), ua
        assert "AppleWebKit/605.1.15" in ua, ua
        assert "Safari/604.1" in ua, ua


def test_no_ancient_ios() -> None:
    # the old hls_proxy UA was iOS 8.1 (2014) -- exactly the stale fingerprint
    # we are killing. Everything in the pool must be iOS 15+.
    for ua in browser_ua.all_uas():
        m = re.search(r"CPU OS (\d+)_", ua)
        assert m is not None, f"no iOS version in {ua!r}"
        assert int(m.group(1)) >= 15, f"stale iOS in {ua!r}"
        assert "OS 8_1" not in ua


def test_pick_returns_pool_member() -> None:
    pool = browser_ua.all_uas()
    for _ in range(20):
        assert browser_ua.pick() in pool


def test_session_ua_is_cached_and_in_pool() -> None:
    browser_ua.reset_session_ua()
    first = browser_ua.session_ua()
    assert first in browser_ua.all_uas()
    assert browser_ua.session_ua() == first, "session UA must be stable within a run"
