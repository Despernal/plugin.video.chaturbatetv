"""Construction of Chaturbate JSON-API URLs.

Stdlib-only. Pure functions; they take ints, strings, and Gender values
and return URLs. No I/O. The HTTP client picks these up and fetches
them.

Chaturbate exposes a clean room-list JSON endpoint at
``/api/ts/roomlist/room-list/`` that takes ``limit``, ``offset``,
``genders``, ``new_cams``, ``keywords`` and a few other filters. We
target only that endpoint - no HTML scraping. Pages are 1-indexed in
our public API and translated to ``offset = (page - 1) * limit``.
"""
from __future__ import annotations

from urllib.parse import urlencode

from resources.lib.cb_models import Gender


BASE_URL = "https://chaturbate.com"
ROOMLIST_API = f"{BASE_URL}/api/ts/roomlist/room-list/"
DOSSIER_AJAX = f"{BASE_URL}/get_edge_hls_url_ajax/"
DEFAULT_LIMIT = 100


_GENDER_CODE = {
    Gender.FEMALE: "f",
    Gender.MALE: "m",
    Gender.COUPLE: "c",
    Gender.TRANS: "s",
}


def _safe_page(page: int) -> int:
    """Clamp non-positive page numbers to 1."""
    return max(1, page)


def _offset(page: int, limit: int = DEFAULT_LIMIT) -> int:
    return (_safe_page(page) - 1) * limit


def top_cams_url(page: int = 1, limit: int = DEFAULT_LIMIT) -> str:
    """Most-viewers listing across all genders."""
    qs = urlencode({"limit": limit, "offset": _offset(page, limit)})
    return f"{ROOMLIST_API}?{qs}"


def new_cams_url(page: int = 1, limit: int = DEFAULT_LIMIT) -> str:
    """Recently-online listing (``new_cams=true``)."""
    qs = urlencode({
        "limit": limit,
        "offset": _offset(page, limit),
        "new_cams": "true",
    })
    return f"{ROOMLIST_API}?{qs}"


def gender_filter_url(gender: Gender, page: int = 1, limit: int = DEFAULT_LIMIT) -> str:
    """Listing filtered to a single broadcaster gender.

    Raises ``ValueError`` for ``Gender.UNKNOWN`` since there is no
    public 'unknown' filter in the API.
    """
    code = _GENDER_CODE.get(gender)
    if code is None:
        raise ValueError(f"no listing endpoint for gender={gender!r}")
    qs = urlencode({
        "limit": limit,
        "offset": _offset(page, limit),
        "genders": code,
    })
    return f"{ROOMLIST_API}?{qs}"


def search_url(query: str, page: int = 1, limit: int = DEFAULT_LIMIT) -> str:
    """Search by keyword. Empty queries are passed through (caller's call)."""
    qs = urlencode({
        "limit": limit,
        "offset": _offset(page, limit),
        "keywords": query,
    })
    return f"{ROOMLIST_API}?{qs}"


def room_url(slug: str) -> str:
    """Canonical browser-facing room URL for a model slug.

    Tolerant of common stray characters in the input: leading ``@``
    (we sometimes see this in mention-style copies) and surrounding
    slashes.
    """
    cleaned = slug.strip().strip("/").lstrip("@").strip()
    if not cleaned:
        raise ValueError("room_url requires a non-empty slug")
    return f"{BASE_URL}/{cleaned}/"
