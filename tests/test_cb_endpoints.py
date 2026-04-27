"""Tests for resources.lib.cb_endpoints - URL construction.

Endpoints are deliberately minimal; we use Chaturbate's public listing
URLs and just construct them from named arguments.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from resources.lib.cb_endpoints import (
    BASE_URL,
    gender_filter_url,
    new_cams_url,
    room_url,
    search_url,
    top_cams_url,
)
from resources.lib.cb_models import Gender


def _qs(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def test_base_url_is_https_chaturbate() -> None:
    assert BASE_URL == "https://chaturbate.com"


def test_top_cams_url_default_page() -> None:
    url = top_cams_url()
    assert url.startswith("https://chaturbate.com/")
    assert "page=1" in url


def test_top_cams_url_with_page() -> None:
    url = top_cams_url(page=3)
    assert "page=3" in url


def test_new_cams_url_default_page() -> None:
    url = new_cams_url()
    assert url.startswith("https://chaturbate.com/")
    assert "page=1" in url


def test_new_cams_url_with_page() -> None:
    url = new_cams_url(page=2)
    assert "page=2" in url


def test_top_and_new_are_distinct() -> None:
    assert top_cams_url() != new_cams_url()


def test_gender_filter_female() -> None:
    url = gender_filter_url(Gender.FEMALE)
    assert "/female-cams/" in url
    assert "page=1" in url


def test_gender_filter_male() -> None:
    url = gender_filter_url(Gender.MALE)
    assert "/male-cams/" in url


def test_gender_filter_couple() -> None:
    url = gender_filter_url(Gender.COUPLE)
    assert "/couple-cams/" in url


def test_gender_filter_trans() -> None:
    url = gender_filter_url(Gender.TRANS)
    assert "/trans-cams/" in url


def test_gender_filter_unknown_raises() -> None:
    with pytest.raises(ValueError):
        gender_filter_url(Gender.UNKNOWN)


def test_gender_filter_with_page() -> None:
    url = gender_filter_url(Gender.FEMALE, page=4)
    assert "page=4" in url


def test_search_url_simple() -> None:
    url = search_url("alice")
    qs = _qs(url)
    assert qs.get("keywords") == ["alice"]


def test_search_url_with_spaces() -> None:
    url = search_url("hello world")
    qs = _qs(url)
    assert qs.get("keywords") == ["hello world"]


def test_search_url_with_special_chars() -> None:
    """URL-encoding catches & / # / =."""
    url = search_url("a&b=c")
    qs = _qs(url)
    assert qs.get("keywords") == ["a&b=c"]


def test_search_url_default_page() -> None:
    url = search_url("alice")
    assert "page=1" in url


def test_search_url_page_arg() -> None:
    url = search_url("alice", page=5)
    assert "page=5" in url


def test_room_url_basic() -> None:
    assert room_url("alice") == "https://chaturbate.com/alice/"


def test_room_url_strips_leading_at() -> None:
    """Models sometimes get referenced as '@slug' in URLs; tolerate it."""
    assert room_url("@alice") == "https://chaturbate.com/alice/"


def test_room_url_strips_surrounding_slashes() -> None:
    assert room_url("/alice/") == "https://chaturbate.com/alice/"


def test_room_url_empty_slug_raises() -> None:
    with pytest.raises(ValueError):
        room_url("")


def test_room_url_whitespace_slug_raises() -> None:
    with pytest.raises(ValueError):
        room_url("   ")


def test_page_zero_clamps_to_one() -> None:
    """Chaturbate listings are 1-indexed; we accept 0 and silently clamp."""
    url = top_cams_url(page=0)
    assert "page=1" in url


def test_negative_page_clamps_to_one() -> None:
    url = top_cams_url(page=-3)
    assert "page=1" in url
