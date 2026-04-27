"""Construction of Chaturbate listing / search / room URLs.

Stdlib-only. Pure functions; they take ints and Gender values, return
strings. No I/O. The HTTP client picks these up and fetches them.

Note: Chaturbate browsing pages are 1-indexed in the URL. A caller who
asks for ``page=0`` is silently bumped to 1 (matches the site
behaviour - page 0 is just page 1 with a confusing query string).
"""
from __future__ import annotations

from urllib.parse import urlencode

from resources.lib.cb_models import Gender


BASE_URL = "https://chaturbate.com"


_GENDER_PATH = {
    Gender.FEMALE: "/female-cams/",
    Gender.MALE: "/male-cams/",
    Gender.COUPLE: "/couple-cams/",
    Gender.TRANS: "/trans-cams/",
}


def _safe_page(page: int) -> int:
    """Clamp non-positive page numbers to 1 (Chaturbate is 1-indexed)."""
    return max(1, page)


def top_cams_url(page: int = 1) -> str:
    """Most-viewers listing across all genders."""
    qs = urlencode({"page": _safe_page(page)})
    return f"{BASE_URL}/?{qs}"


def new_cams_url(page: int = 1) -> str:
    """Recently-online listing."""
    qs = urlencode({"page": _safe_page(page)})
    return f"{BASE_URL}/new-cams/?{qs}"


def gender_filter_url(gender: Gender, page: int = 1) -> str:
    """Listing filtered to a single broadcaster gender.

    Raises ``ValueError`` for ``Gender.UNKNOWN`` since there is no
    public 'unknown' filter on the site.
    """
    path = _GENDER_PATH.get(gender)
    if path is None:
        raise ValueError(f"no listing endpoint for gender={gender!r}")
    qs = urlencode({"page": _safe_page(page)})
    return f"{BASE_URL}{path}?{qs}"


def search_url(query: str, page: int = 1) -> str:
    """Search by keyword. Empty queries pass through (caller's call)."""
    qs = urlencode({"keywords": query, "page": _safe_page(page)})
    return f"{BASE_URL}/?{qs}"


def room_url(slug: str) -> str:
    """Canonical room URL for a model slug.

    Tolerant of common stray characters in the input: leading ``@``
    (we sometimes see this in mention-style copies) and surrounding
    slashes.
    """
    cleaned = slug.strip().strip("/").lstrip("@").strip()
    if not cleaned:
        raise ValueError("room_url requires a non-empty slug")
    return f"{BASE_URL}/{cleaned}/"
