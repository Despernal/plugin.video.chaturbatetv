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
