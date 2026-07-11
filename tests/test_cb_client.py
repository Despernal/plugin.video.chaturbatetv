"""Tests for resources.lib.cb_client - HTTP client for Chaturbate.

The module exposes:

- ``HTTP_HEADERS_IPAD`` constant (UA, Accept, Accept-Language,
  Sec-Fetch-Mode) matching what  uses to dodge default-UA
  blocks.
- ``fetch_room_dossier(slug, fetch_func=None) -> str``
- ``fetch_room_status_json(slug, fetch_func=None) -> dict``
- ``fetch_browse_page(url, fetch_func=None) -> str``
- ``is_model_live(slug, fetch_func=None) -> bool``

``fetch_func`` defaults to a small urllib wrapper but tests always
inject their own to avoid network calls.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from resources.lib import cb_client


# Shape: list of (url, body, headers, method) tuples returned by the test fetch.
_FetchCall = tuple[str, bytes | None, dict[str, str], str]


def _record_fetch(response: str | bytes, calls: list[_FetchCall]) -> Any:
    """Build a fetch_func that records every call and returns ``response``."""
    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None, method: str = "GET") -> str:
        calls.append((url, body, dict(headers or {}), method))
        if isinstance(response, bytes):
            return response.decode("utf-8")
        return response
    return fetch


# --------------------------------------------------------------------------- #
# HTTP_HEADERS_IPAD
# --------------------------------------------------------------------------- #


def test_http_headers_ipad_has_ua() -> None:
    headers = cb_client.HTTP_HEADERS_IPAD
    assert "User-Agent" in headers
    assert "iPad" in headers["User-Agent"]


def test_http_headers_ipad_has_accept_language() -> None:
    assert "Accept-Language" in cb_client.HTTP_HEADERS_IPAD


def test_http_headers_ipad_has_accept() -> None:
    assert "Accept" in cb_client.HTTP_HEADERS_IPAD


def test_http_headers_ipad_has_sec_fetch_mode() -> None:
    assert "Sec-Fetch-Mode" in cb_client.HTTP_HEADERS_IPAD


def test_http_headers_ipad_is_dict_of_str() -> None:
    for k, v in cb_client.HTTP_HEADERS_IPAD.items():
        assert isinstance(k, str)
        assert isinstance(v, str)


# --------------------------------------------------------------------------- #
# fetch_room_dossier
# --------------------------------------------------------------------------- #


def test_fetch_room_dossier_hits_room_url() -> None:
    calls: list[_FetchCall] = []
    fetch = _record_fetch("<html>ok</html>", calls)

    body = cb_client.fetch_room_dossier("alice", fetch_func=fetch)

    assert body == "<html>ok</html>"
    assert len(calls) == 1
    url, _body, _headers, method = calls[0]
    assert url == "https://chaturbate.com/alice/"
    assert method == "GET"


def test_fetch_room_dossier_sends_ipad_headers() -> None:
    calls: list[_FetchCall] = []
    fetch = _record_fetch("<html>ok</html>", calls)

    cb_client.fetch_room_dossier("alice", fetch_func=fetch)

    _url, _body, headers, _method = calls[0]
    assert headers.get("User-Agent") == cb_client.HTTP_HEADERS_IPAD["User-Agent"]


def test_fetch_room_dossier_propagates_oserror() -> None:
    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None, method: str = "GET") -> str:
        raise OSError("network down")

    with pytest.raises(OSError):
        cb_client.fetch_room_dossier("alice", fetch_func=fetch)


def test_fetch_room_dossier_rejects_empty_slug() -> None:
    with pytest.raises(ValueError):
        cb_client.fetch_room_dossier("", fetch_func=_record_fetch("", []))


# --------------------------------------------------------------------------- #
# fetch_room_status_json
# --------------------------------------------------------------------------- #


def test_fetch_room_status_json_hits_ajax_endpoint() -> None:
    calls: list[_FetchCall] = []
    payload = json.dumps({"success": True, "url": "https://x.example/p.m3u8",
                          "room_status": "public", "hidden_message": "",
                          "cmaf_edge": False})
    fetch = _record_fetch(payload, calls)

    out = cb_client.fetch_room_status_json("bob", fetch_func=fetch)

    assert out["success"] is True
    assert out["url"] == "https://x.example/p.m3u8"
    assert out["room_status"] == "public"

    url, body, _headers, method = calls[0]
    assert url == "https://chaturbate.com/get_edge_hls_url_ajax/"
    assert method == "POST"
    assert body is not None
    assert b"room_slug=bob" in body
    assert b"bandwidth=high" in body


def test_fetch_room_status_json_sends_xrequestedwith() -> None:
    calls: list[_FetchCall] = []
    fetch = _record_fetch(json.dumps({"success": True, "url": "",
                                      "room_status": "offline",
                                      "hidden_message": "",
                                      "cmaf_edge": False}), calls)

    cb_client.fetch_room_status_json("bob", fetch_func=fetch)

    _url, _body, headers, _method = calls[0]
    assert headers.get("X-Requested-With") == "XMLHttpRequest"


def test_fetch_room_status_json_returns_safe_default_on_invalid_json() -> None:
    fetch = _record_fetch("not json at all", [])
    out = cb_client.fetch_room_status_json("bob", fetch_func=fetch)
    assert out["success"] is False
    assert out["url"] == ""
    assert out["room_status"] == "offline"


def test_fetch_room_status_json_returns_safe_default_on_oserror() -> None:
    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None, method: str = "GET") -> str:
        raise OSError("timeout")

    out = cb_client.fetch_room_status_json("bob", fetch_func=fetch)
    assert out["success"] is False
    assert out["url"] == ""
    assert out["room_status"] == "offline"


def test_fetch_room_status_json_returns_safe_default_on_blocked() -> None:
    """Cloudflare/HTML-instead-of-JSON page -> safe default."""
    fetch = _record_fetch("<html>blocked</html>", [])
    out = cb_client.fetch_room_status_json("bob", fetch_func=fetch)
    assert out["success"] is False
    assert out["url"] == ""


def test_fetch_room_status_json_rejects_empty_slug() -> None:
    with pytest.raises(ValueError):
        cb_client.fetch_room_status_json("", fetch_func=_record_fetch("", []))


# --- v0.7.59: fetch_ok distinguishes a real 200 from a safe-default fallback - #
# A network OSError (403 storm), a Cloudflare HTML body, or a non-dict JSON
# payload all return the same safe default with room_status='offline'. But those
# are NOT confirmed-offline answers -- the fetch FAILED and the room's true
# status is unknown. fetch_ok=True only when we actually parsed a 200 JSON dict
# (live OR a clean offline). The resolve chain reads fetch_ok so a network-wide
# outage can't poison the TV offline blocklist (the bug that wedged TV mode on
# 2026-06-17: every model 403'd -> all marked offline -> no self-recovery).
def test_fetch_room_status_json_marks_fetch_ok_true_on_success() -> None:
    fetch = _record_fetch(json.dumps({"success": True,
                                      "url": "https://x.example/p.m3u8",
                                      "room_status": "public"}), [])
    out = cb_client.fetch_room_status_json("bob", fetch_func=fetch)
    assert out["fetch_ok"] is True


def test_fetch_room_status_json_marks_fetch_ok_true_on_confirmed_offline() -> None:
    """A clean 200 saying the room is offline is a KNOWN status -> fetch_ok True."""
    fetch = _record_fetch(json.dumps({"success": True, "url": "",
                                      "room_status": "offline"}), [])
    out = cb_client.fetch_room_status_json("bob", fetch_func=fetch)
    assert out["fetch_ok"] is True
    assert out["room_status"] == "offline"


def test_fetch_room_status_json_marks_fetch_ok_false_on_oserror() -> None:
    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None, method: str = "GET") -> str:
        raise OSError("403 Forbidden")
    out = cb_client.fetch_room_status_json("bob", fetch_func=fetch)
    assert out["fetch_ok"] is False


def test_fetch_room_status_json_marks_fetch_ok_false_on_blocked_html() -> None:
    fetch = _record_fetch("<html>cloudflare</html>", [])
    out = cb_client.fetch_room_status_json("bob", fetch_func=fetch)
    assert out["fetch_ok"] is False


def test_fetch_room_status_json_marks_fetch_ok_false_on_non_dict_json() -> None:
    fetch = _record_fetch("[1, 2, 3]", [])
    out = cb_client.fetch_room_status_json("bob", fetch_func=fetch)
    assert out["fetch_ok"] is False


# --------------------------------------------------------------------------- #
# fetch_browse_page
# --------------------------------------------------------------------------- #


def test_fetch_browse_page_passes_url_through() -> None:
    calls: list[_FetchCall] = []
    fetch = _record_fetch("<html>browse</html>", calls)

    body = cb_client.fetch_browse_page("https://chaturbate.com/?page=2",
                                       fetch_func=fetch)
    assert body == "<html>browse</html>"
    assert calls[0][0] == "https://chaturbate.com/?page=2"


def test_fetch_browse_page_sends_ipad_headers() -> None:
    calls: list[_FetchCall] = []
    fetch = _record_fetch("<html>browse</html>", calls)

    cb_client.fetch_browse_page("https://chaturbate.com/", fetch_func=fetch)

    _url, _body, headers, _method = calls[0]
    assert "User-Agent" in headers


def test_fetch_browse_page_sends_xrequestedwith() -> None:
    """The room-list JSON endpoint is now gated behind the XHR header.

    v0.7.64 (2026-07-11): Chaturbate started answering
    ``/api/ts/roomlist/room-list/`` with ``302 -> /?next=<path>`` (an HTML
    homepage) for any request missing ``X-Requested-With: XMLHttpRequest``.
    urlopen follows the redirect, parse_roomlist gets HTML instead of JSON,
    and Top Cams / New Cams / Female / Male / Couple / Trans / Search all go
    blank. The sibling endpoints (fetch_room_status_json, fetch_biocontext)
    already send this header; the browse path was the only listing route that
    didn't. Pin it so the gate can't silently un-fix (Lesson 15 / 37).
    """
    calls: list[_FetchCall] = []
    fetch = _record_fetch("{}", calls)

    cb_client.fetch_browse_page(
        "https://chaturbate.com/api/ts/roomlist/room-list/?limit=100&offset=0",
        fetch_func=fetch,
    )

    _url, _body, headers, _method = calls[0]
    assert headers.get("X-Requested-With") == "XMLHttpRequest"


def test_fetch_browse_page_rejects_empty_url() -> None:
    with pytest.raises(ValueError):
        cb_client.fetch_browse_page("", fetch_func=_record_fetch("", []))


# --------------------------------------------------------------------------- #
# is_model_live
# --------------------------------------------------------------------------- #


def test_is_model_live_true_when_room_status_public_and_url_present() -> None:
    fetch = _record_fetch(json.dumps({"success": True, "url": "https://e/p.m3u8",
                                      "room_status": "public",
                                      "hidden_message": "", "cmaf_edge": False}),
                          [])
    assert cb_client.is_model_live("alice", fetch_func=fetch) is True


def test_is_model_live_false_when_room_status_offline() -> None:
    fetch = _record_fetch(json.dumps({"success": True, "url": "",
                                      "room_status": "offline",
                                      "hidden_message": "", "cmaf_edge": False}),
                          [])
    assert cb_client.is_model_live("alice", fetch_func=fetch) is False


def test_is_model_live_false_when_room_status_private() -> None:
    fetch = _record_fetch(json.dumps({"success": True, "url": "",
                                      "room_status": "private",
                                      "hidden_message": "members only",
                                      "cmaf_edge": False}),
                          [])
    assert cb_client.is_model_live("alice", fetch_func=fetch) is False


def test_is_model_live_false_when_url_empty_even_if_public() -> None:
    """Defensive: room_status public but no HLS - treat as not live."""
    fetch = _record_fetch(json.dumps({"success": True, "url": "",
                                      "room_status": "public",
                                      "hidden_message": "", "cmaf_edge": False}),
                          [])
    assert cb_client.is_model_live("alice", fetch_func=fetch) is False


def test_is_model_live_false_on_network_error() -> None:
    """A timeout while polling should never crash; treat as not live."""
    def fetch(url: str, body: bytes | None = None,
              headers: dict[str, str] | None = None, method: str = "GET") -> str:
        raise OSError("timeout")

    assert cb_client.is_model_live("alice", fetch_func=fetch) is False


# --------------------------------------------------------------------------- #
# Default fetch_func uses urllib (smoke - we just verify it's wired)
# --------------------------------------------------------------------------- #


def test_default_fetch_func_exists_and_is_callable() -> None:
    """We don't call the network, but the symbol must be a callable."""
    assert callable(cb_client._default_fetch)
