"""Tests for resources.lib.cb_endpoints - JSON-API URL construction.

Chaturbate publishes the room list at
``/api/ts/roomlist/room-list/`` with limit/offset pagination. These
tests pin the URL shape so we cannot accidentally regress to HTML
scraping.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from resources.lib.cb_endpoints import (
    BASE_URL,
    DEFAULT_LIMIT,
    DOSSIER_AJAX,
    ROOMLIST_API,
    gender_filter_url,
    new_cams_url,
    room_url,
    search_url,
    top_cams_url,
)
from resources.lib.cb_models import Gender


def _qs(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query, keep_blank_values=True)


def test_base_url() -> None:
    assert BASE_URL == "https://chaturbate.com"


def test_roomlist_api_is_the_json_endpoint() -> None:
    assert ROOMLIST_API == "https://chaturbate.com/api/ts/roomlist/room-list/"


def test_dossier_ajax_is_the_post_endpoint() -> None:
    assert DOSSIER_AJAX == "https://chaturbate.com/get_edge_hls_url_ajax/"


def test_default_limit_is_at_least_50() -> None:
    assert DEFAULT_LIMIT >= 50


# top_cams_url --------------------------------------------------------------- #


def test_top_cams_url_uses_roomlist_api() -> None:
    assert top_cams_url().startswith(ROOMLIST_API)


def test_top_cams_url_default_offset_is_zero() -> None:
    assert _qs(top_cams_url())["offset"] == ["0"]


def test_top_cams_url_default_limit() -> None:
    assert _qs(top_cams_url())["limit"] == [str(DEFAULT_LIMIT)]


def test_top_cams_url_page_2_offsets_by_limit() -> None:
    assert _qs(top_cams_url(page=2))["offset"] == [str(DEFAULT_LIMIT)]


def test_top_cams_url_page_3_offsets_by_double_limit() -> None:
    assert _qs(top_cams_url(page=3))["offset"] == [str(DEFAULT_LIMIT * 2)]


def test_top_cams_url_clamps_zero_page() -> None:
    assert _qs(top_cams_url(page=0))["offset"] == ["0"]


def test_top_cams_url_clamps_negative_page() -> None:
    assert _qs(top_cams_url(page=-5))["offset"] == ["0"]


def test_top_cams_url_custom_limit() -> None:
    qs = _qs(top_cams_url(page=1, limit=25))
    assert qs["limit"] == ["25"]
    assert qs["offset"] == ["0"]


def test_top_cams_url_does_not_set_genders_or_keywords() -> None:
    qs = _qs(top_cams_url())
    assert "genders" not in qs
    assert "keywords" not in qs


# new_cams_url --------------------------------------------------------------- #


def test_new_cams_url_uses_roomlist_api() -> None:
    assert new_cams_url().startswith(ROOMLIST_API)


def test_new_cams_url_sets_new_cams_true() -> None:
    assert _qs(new_cams_url())["new_cams"] == ["true"]


def test_new_cams_url_pagination() -> None:
    assert _qs(new_cams_url(page=4))["offset"] == [str(DEFAULT_LIMIT * 3)]


# gender_filter_url ---------------------------------------------------------- #


def test_gender_filter_female_sends_f() -> None:
    assert _qs(gender_filter_url(Gender.FEMALE))["genders"] == ["f"]


def test_gender_filter_male_sends_m() -> None:
    assert _qs(gender_filter_url(Gender.MALE))["genders"] == ["m"]


def test_gender_filter_couple_sends_c() -> None:
    assert _qs(gender_filter_url(Gender.COUPLE))["genders"] == ["c"]


def test_gender_filter_trans_sends_s() -> None:
    """Chaturbate's API uses 's' for trans (historical 'shemale' code)."""
    assert _qs(gender_filter_url(Gender.TRANS))["genders"] == ["s"]


def test_gender_filter_unknown_raises() -> None:
    with pytest.raises(ValueError):
        gender_filter_url(Gender.UNKNOWN)


def test_gender_filter_uses_roomlist_api() -> None:
    assert gender_filter_url(Gender.FEMALE).startswith(ROOMLIST_API)


def test_gender_filter_pagination() -> None:
    assert _qs(gender_filter_url(Gender.MALE, page=3))["offset"] == [str(DEFAULT_LIMIT * 2)]


# search_url ----------------------------------------------------------------- #


def test_search_url_uses_roomlist_api() -> None:
    assert search_url("blonde").startswith(ROOMLIST_API)


def test_search_url_passes_keyword() -> None:
    assert _qs(search_url("blonde"))["keywords"] == ["blonde"]


def test_search_url_keyword_with_spaces() -> None:
    qs = _qs(search_url("hot tub"))
    assert qs["keywords"] == ["hot tub"]


def test_search_url_empty_query_passes_through() -> None:
    qs = _qs(search_url(""))
    assert qs["keywords"] == [""]


def test_search_url_pagination() -> None:
    assert _qs(search_url("test", page=2))["offset"] == [str(DEFAULT_LIMIT)]


# room_url (unchanged behaviour) -------------------------------------------- #


def test_room_url_basic() -> None:
    assert room_url("alice") == "https://chaturbate.com/alice/"


def test_room_url_strips_at_sign() -> None:
    assert room_url("@alice") == "https://chaturbate.com/alice/"


def test_room_url_strips_slashes() -> None:
    assert room_url("/alice/") == "https://chaturbate.com/alice/"


def test_room_url_empty_raises() -> None:
    with pytest.raises(ValueError):
        room_url("")


def test_room_url_whitespace_only_raises() -> None:
    with pytest.raises(ValueError):
        room_url("   ")


# --------------------------------------------------------------------------- #
# v0.7.39 (audit pass #5 HIGH): is_trusted_url -- the host-allowlist
# helper used by hls_proxy._fetch (SSRF defense), addon_actions.
# show_picture (ShowPicture builtin sandbox), and model_meta_store.
# image_for_row (Kodi image cache sandbox).
# --------------------------------------------------------------------------- #


def test_is_trusted_url_accepts_chaturbate_apex() -> None:
    from resources.lib.cb_endpoints import is_trusted_url
    assert is_trusted_url("https://chaturbate.com/api/foo")


def test_is_trusted_url_accepts_subdomains_of_trusted_apex() -> None:
    from resources.lib.cb_endpoints import is_trusted_url
    assert is_trusted_url("https://edge42.live.mmcdn.com/hls/abc/master.m3u8")
    assert is_trusted_url("https://thumb.live.mmcdn.com/ri/alice.jpg")
    assert is_trusted_url("https://static-pub.highwebmedia.com/cover.jpg")


def test_is_trusted_url_rejects_file_scheme() -> None:
    from resources.lib.cb_endpoints import is_trusted_url
    assert not is_trusted_url("file:///etc/shadow")
    assert not is_trusted_url(
        "file:///storage/.kodi/userdata/passwords.xml"
    )


def test_is_trusted_url_rejects_ftp_javascript_data_schemes() -> None:
    from resources.lib.cb_endpoints import is_trusted_url
    assert not is_trusted_url("ftp://chaturbate.com/foo")
    assert not is_trusted_url("javascript:alert(1)")
    assert not is_trusted_url("data:text/html,<script>")


def test_is_trusted_url_rejects_lan_pivots() -> None:
    """The LAN-pivot SSRF that the localhost HLS proxy guards against."""
    from resources.lib.cb_endpoints import is_trusted_url
    assert not is_trusted_url("http://127.0.0.1:8088/admin")
    assert not is_trusted_url("http://192.168.1.1/cgi-bin/admin")
    assert not is_trusted_url("http://192.168.1.1:8088/")
    assert not is_trusted_url("http://localhost/")


def test_is_trusted_url_rejects_lookalike_hosts() -> None:
    """Common SSRF bypass attempts: substring/lookalike domains."""
    from resources.lib.cb_endpoints import is_trusted_url
    # Suffix check -- not a substring match.
    assert not is_trusted_url("https://evil-chaturbate.com.attacker.tld/")
    # Different TLD with same prefix.
    assert not is_trusted_url("https://chaturbate.com.attacker.tld/")


def test_is_trusted_url_rejects_empty_or_unparseable() -> None:
    from resources.lib.cb_endpoints import is_trusted_url
    assert not is_trusted_url("")
    assert not is_trusted_url("not-a-url")
