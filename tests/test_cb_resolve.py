"""Tests for resources.lib.cb_resolve - dossier-to-Resolution.

Pure module: takes a fetch callback so the test can return canned HTML.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from resources.lib.cb_models import Gender
from resources.lib.cb_resolve import Resolution, resolve


FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_resolution_dataclass_fields() -> None:
    r = Resolution(
        is_live=True,
        hls_source="https://edge.example.com/playlist.m3u8",
        headers={"User-Agent": "x", "Referer": "y"},
        gender=Gender.FEMALE,
    )
    assert r.is_live is True
    assert r.hls_source == "https://edge.example.com/playlist.m3u8"
    assert r.headers["User-Agent"] == "x"
    assert r.gender is Gender.FEMALE


def test_resolution_is_frozen() -> None:
    r = Resolution(is_live=False, hls_source=None, headers={}, gender=Gender.UNKNOWN)
    with pytest.raises((AttributeError, Exception)):
        r.is_live = True  # type: ignore[misc]


def test_resolve_live_room() -> None:
    html = _read("sample_live_room.html")
    r = resolve("sample_live_room", lambda url: html)
    assert r.is_live is True
    assert r.hls_source == "https://edge42-fra.live.mmcdn.com/hls/abc123.m3u8"
    assert r.gender is Gender.FEMALE


def test_resolve_offline_room() -> None:
    html = _read("sample_offline_room.html")
    r = resolve("sample_offline_room", lambda url: html)
    assert r.is_live is False
    assert r.hls_source is None
    assert r.gender is Gender.MALE


def test_resolve_passes_correct_url_to_fetch() -> None:
    seen: list[str] = []

    def fetch(url: str) -> str:
        seen.append(url)
        return ""

    resolve("alice", fetch)
    assert seen == ["https://chaturbate.com/alice/"]


def test_resolve_includes_referer_and_user_agent() -> None:
    html = _read("sample_live_room.html")
    r = resolve("sample_live_room", lambda url: html)
    assert "User-Agent" in r.headers
    assert "Referer" in r.headers
    assert r.headers["Referer"] == "https://chaturbate.com/sample_live_room/"


def test_resolve_safe_default_when_fetch_returns_empty() -> None:
    r = resolve("ghost", lambda url: "")
    assert r.is_live is False
    assert r.hls_source is None


def test_resolve_propagates_fetch_exception() -> None:
    """If fetch raises, resolve does NOT swallow it; the caller decides."""

    def bad_fetch(url: str) -> str:
        raise OSError("network unreachable")

    with pytest.raises(OSError):
        resolve("alice", bad_fetch)


def test_resolve_couple_room() -> None:
    html = _read("sample_couple_room.html")
    r = resolve("sample_couple_room", lambda url: html)
    assert r.is_live is True
    assert r.gender is Gender.COUPLE
    assert r.hls_source == "https://edge42-iad.live.mmcdn.com/hls/zzz/playlist.m3u8"


def test_resolve_handles_invalid_slug() -> None:
    """Empty slug -> ValueError from room_url, propagated up."""
    with pytest.raises(ValueError):
        resolve("", lambda url: "")


# resolve_ajax - preferred path, uses the JSON status endpoint -------------- #
# Switched to in 0.4.3 because HTML scrape of initialRoomDossier is fragile
# (Chaturbate is JS-rendered now; the blob is sometimes absent and we got
# is_live=False for live rooms, then "Cannot download manifest" from ISA).

def test_resolve_ajax_live_room() -> None:
    """AJAX returns success+url+room_status=public -> is_live=True with that hls."""
    from resources.lib.cb_resolve import resolve_ajax
    fake_status = {
        "success": True,
        "url": "https://edge99-foo.live.mmcdn.com/hls/abc/llhls.m3u8?token=xyz",
        "room_status": "public",
        "hidden_message": "",
        "cmaf_edge": False,
    }
    r = resolve_ajax("alice", lambda slug: fake_status)
    assert r.is_live is True
    assert r.hls_source == fake_status["url"]


def test_resolve_ajax_offline_room() -> None:
    from resources.lib.cb_resolve import resolve_ajax
    r = resolve_ajax("ghost", lambda slug: {
        "success": True, "url": "", "room_status": "offline",
        "hidden_message": "", "cmaf_edge": False,
    })
    assert r.is_live is False
    assert r.hls_source is None


def test_resolve_ajax_treats_empty_url_as_offline() -> None:
    """Even if room_status=public, no URL means we cannot play."""
    from resources.lib.cb_resolve import resolve_ajax
    r = resolve_ajax("alice", lambda slug: {
        "success": True, "url": "", "room_status": "public",
    })
    assert r.is_live is False
    assert r.hls_source is None


def test_resolve_ajax_passes_slug_to_fetch() -> None:
    from resources.lib.cb_resolve import resolve_ajax
    seen: list[str] = []

    def fetch(slug: str) -> dict:
        seen.append(slug)
        return {"success": False, "url": "", "room_status": "offline"}

    resolve_ajax("alice", fetch)
    assert seen == ["alice"]


def test_resolve_ajax_includes_referer_and_user_agent() -> None:
    from resources.lib.cb_resolve import resolve_ajax
    r = resolve_ajax("alice", lambda slug: {
        "success": False, "url": "", "room_status": "offline",
    })
    assert "User-Agent" in r.headers
    assert r.headers["Referer"] == "https://chaturbate.com/alice/"


def test_resolve_ajax_propagates_fetch_exception() -> None:
    from resources.lib.cb_resolve import resolve_ajax

    def bad_fetch(slug: str) -> dict:
        raise OSError("network unreachable")

    with pytest.raises(OSError):
        resolve_ajax("alice", bad_fetch)


def test_resolve_ajax_handles_invalid_slug() -> None:
    from resources.lib.cb_resolve import resolve_ajax
    with pytest.raises(ValueError):
        resolve_ajax("", lambda slug: {})


def test_resolve_ajax_safe_when_status_missing_keys() -> None:
    """A truncated/malformed status dict shouldn't crash."""
    from resources.lib.cb_resolve import resolve_ajax
    r = resolve_ajax("alice", lambda slug: {})
    assert r.is_live is False
    assert r.hls_source is None


# --- v0.7.59: status_known threads "was the fetch real?" up from fetch_ok ---- #
# is_live=False is ambiguous: a model who genuinely signed off (clean 200) and a
# model we simply couldn't reach (403 storm -> safe default) both land here.
# status_known carries the difference so the TV loop only poisons its offline
# blocklist on a CONFIRMED offline, never on a network-wide outage.
def test_resolution_status_known_defaults_true() -> None:
    """Existing constructions (and the HTML resolve path) are 'known' by default."""
    r = Resolution(is_live=False, hls_source=None, headers={}, gender=Gender.UNKNOWN)
    assert r.status_known is True


def test_resolve_ajax_status_known_true_when_fetch_ok() -> None:
    """A real 200 (fetch_ok True) -> status is known, even when offline."""
    from resources.lib.cb_resolve import resolve_ajax
    r = resolve_ajax("alice", lambda slug: {
        "success": True, "url": "", "room_status": "offline", "fetch_ok": True,
    })
    assert r.is_live is False
    assert r.status_known is True


def test_resolve_ajax_status_unknown_when_fetch_failed() -> None:
    """A safe default from a network/blocked fetch (fetch_ok False) -> status
    UNKNOWN; the caller must not treat 'offline' as a confirmed answer."""
    from resources.lib.cb_resolve import resolve_ajax
    r = resolve_ajax("alice", lambda slug: {
        "success": False, "url": "", "room_status": "offline", "fetch_ok": False,
    })
    assert r.is_live is False
    assert r.status_known is False


def test_resolve_ajax_status_known_true_when_fetch_ok_absent() -> None:
    """Conservative default: a status dict with no fetch_ok key is treated as
    known, preserving pre-0.7.59 behavior for any caller/stub not setting it."""
    from resources.lib.cb_resolve import resolve_ajax
    r = resolve_ajax("alice", lambda slug: {
        "success": True, "url": "x", "room_status": "public",
    })
    assert r.status_known is True
