"""Fresh, rotating iPad Safari User-Agents for cbtv's HTTP requests.

Single source of truth. Replaces the three hardcoded UAs that used to live in
cb_resolve, cb_client and hls_proxy -- one of which (the stream proxy) was still
sending an iPad iOS 8.1 string from 2014, exactly the kind of stale/bot-flavoured
fingerprint that gets a request flagged.

Chaturbate serves the low-latency-HLS streams to iPad-family UAs and blocks
default urllib / bot UAs alike, so the whole pool stays iPad Safari -- the only
change is that the versions are FRESH (iOS 17.x-18.x) and RANDOMIZED per run, so
each session looks like a different real iPad instead of one fixed, stale one.

``session_ua()`` picks once per run and caches, so the resolve path (cb_resolve /
cb_client) and the stream proxy (hls_proxy) present the SAME UA within a session
-- avoids introducing a resolve-vs-stream UA mismatch. ``pick()`` is the raw
randomizer if a caller genuinely wants a fresh draw.
"""
from __future__ import annotations

import random

# Fresh iPad Safari UAs (iOS 17.x-18.x). Version/ tracks the iOS major.minor;
# AppleWebKit/605.1.15 + Safari/604.1 are the modern mobile-Safari constants.
_UA_POOL: tuple[str, ...] = (
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.7 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 18_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 18_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 18_3 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 18_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
)

_session_ua: str | None = None


def all_uas() -> tuple[str, ...]:
    """The full pool of fresh iPad Safari UAs (used by tests + audits)."""
    return _UA_POOL


def pick() -> str:
    """Return a random UA from the pool (raw draw, no caching)."""
    return random.choice(_UA_POOL)


def session_ua() -> str:
    """The UA for this run: chosen once (randomized), then cached.

    Cached so every request in a session -- resolve and stream alike -- uses the
    same fresh UA. Rotation happens ACROSS runs (each Kodi/addon launch draws a
    new one), which is enough to shed a fixed fingerprint without risking a
    mid-session resolve-vs-stream mismatch.
    """
    global _session_ua
    if _session_ua is None:
        _session_ua = pick()
    return _session_ua


def reset_session_ua() -> None:
    """Clear the cached session UA (test hook; also lets a long-lived process
    re-roll on demand)."""
    global _session_ua
    _session_ua = None
