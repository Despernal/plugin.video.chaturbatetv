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


# resolve_ajax — preferred path, uses the JSON status endpoint -------------- #
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
