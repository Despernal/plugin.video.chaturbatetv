"""Tests for resources.lib.cb_dossier - parse Chaturbate room HTML."""
from __future__ import annotations

from pathlib import Path

import pytest

from resources.lib.cb_dossier import parse_room_dossier
from resources.lib.cb_models import Gender


FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_live_room_extracts_is_live() -> None:
    parsed = parse_room_dossier(_read("sample_live_room.html"))
    assert parsed["is_live"] is True


def test_parse_live_room_extracts_hls_source() -> None:
    parsed = parse_room_dossier(_read("sample_live_room.html"))
    assert parsed["hls_source"] == "https://edge42-fra.live.mmcdn.com/hls/abc123.m3u8"


def test_parse_live_room_extracts_name() -> None:
    parsed = parse_room_dossier(_read("sample_live_room.html"))
    assert parsed["name"] == "sample_live_room"


def test_parse_live_room_extracts_gender_female() -> None:
    parsed = parse_room_dossier(_read("sample_live_room.html"))
    assert parsed["gender"] is Gender.FEMALE


def test_parse_offline_room_is_not_live() -> None:
    parsed = parse_room_dossier(_read("sample_offline_room.html"))
    assert parsed["is_live"] is False


def test_parse_offline_room_no_hls() -> None:
    parsed = parse_room_dossier(_read("sample_offline_room.html"))
    assert parsed["hls_source"] is None


def test_parse_offline_room_gender_male() -> None:
    parsed = parse_room_dossier(_read("sample_offline_room.html"))
    assert parsed["gender"] is Gender.MALE


def test_parse_couple_room_unescapes_hls_source() -> None:
    """Slashes are escaped in the embedded JSON; we should unescape them."""
    parsed = parse_room_dossier(_read("sample_couple_room.html"))
    assert parsed["hls_source"] == "https://edge42-iad.live.mmcdn.com/hls/zzz/playlist.m3u8"
    assert parsed["is_live"] is True


def test_parse_couple_room_gender_couple() -> None:
    parsed = parse_room_dossier(_read("sample_couple_room.html"))
    assert parsed["gender"] is Gender.COUPLE


def test_parse_no_dossier_returns_safe_default() -> None:
    """HTML with no dossier (Cloudflare page, login redirect) returns a safe default."""
    parsed = parse_room_dossier("<html><body>No dossier here</body></html>")
    assert parsed["is_live"] is False
    assert parsed["hls_source"] is None
    assert parsed["gender"] is Gender.UNKNOWN


def test_parse_empty_string_returns_safe_default() -> None:
    parsed = parse_room_dossier("")
    assert parsed["is_live"] is False
    assert parsed["hls_source"] is None
    assert parsed["name"] == ""


def test_parse_malformed_dossier_returns_safe_default() -> None:
    """Dossier marker present but JSON inside is broken -> safe default."""
    bad = '<script>window.initialRoomDossier = "{not-json";</script>'
    parsed = parse_room_dossier(bad)
    assert parsed["is_live"] is False
    assert parsed["hls_source"] is None


def test_parse_returns_dict_keys() -> None:
    """Contract: result has the four documented keys."""
    parsed = parse_room_dossier(_read("sample_live_room.html"))
    assert set(parsed.keys()) >= {"is_live", "hls_source", "gender", "name"}


def test_parse_gender_unknown_when_missing() -> None:
    """Dossier without broadcaster_gender -> Gender.UNKNOWN."""
    html = '<script>window.initialRoomDossier = "{\\"username\\":\\"x\\",\\"hls_source\\":\\"\\"}";</script>'
    parsed = parse_room_dossier(html)
    assert parsed["gender"] is Gender.UNKNOWN


@pytest.mark.parametrize("hls,expected", [
    ("https://edge.example.com/playlist.m3u8", True),
    ("", False),
    (None, False),
])
def test_parse_is_live_from_hls(hls: str | None, expected: bool) -> None:
    """is_live is true iff hls_source is present and non-empty."""
    if hls is None:
        embed = '\\"hls_source\\":null'
    else:
        embed = f'\\"hls_source\\":\\"{hls}\\"'
    html = f'<script>window.initialRoomDossier = "{{\\"username\\":\\"x\\",{embed}}}";</script>'
    parsed = parse_room_dossier(html)
    assert parsed["is_live"] is expected
