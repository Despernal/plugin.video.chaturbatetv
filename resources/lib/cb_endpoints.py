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

from urllib.parse import urlencode, urlparse

from resources.lib.cb_models import Gender


BASE_URL = "https://chaturbate.com"

# v0.7.39 (audit pass #5 HIGH, agents 1+2): allowlist of host suffixes
# the addon trusts as legitimate Chaturbate / mmcdn / highwebmedia
# origins. Used by hls_proxy._fetch (SSRF defense), addon_actions.
# show_picture (ShowPicture builtin sandbox), and model_meta_store.
# image_for_row (Kodi image cache sandbox). Anything outside this
# list -- file:///, ftp://, javascript:, data:, http://localhost:,
# http://192.168.x.x -- gets rejected before it hits a network or
# Kodi sink.
TRUSTED_HOST_SUFFIXES = (
    "chaturbate.com",
    "mmcdn.com",
    "highwebmedia.com",
)


def is_trusted_url(url: str) -> bool:
    """Return True iff ``url`` is an http/https URL whose host ends
    with one of our trusted Chaturbate-CDN suffixes. Guards against
    file:///, javascript:, ftp://, http://localhost, LAN pivots, and
    any other non-CB host. Empty / unparseable -> False.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in TRUSTED_HOST_SUFFIXES
    )
ROOMLIST_API = f"{BASE_URL}/api/ts/roomlist/room-list/"
ONLINEROOMS_AFFILIATE_API = f"{BASE_URL}/affiliates/api/onlinerooms/"
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


def online_rooms_affiliate_url(wm: str) -> str:
    """The single-call online-rooms endpoint.

    Returns ALL currently-online models in a flat JSON array - typically
    ~5-10 MB of body, fetched in one HTTP call. The room-list paginated
    endpoint caps at 100/page and required a 50-page walk to cover the
    same ground (with politeness pacers and 30-min disk caching to make
    it tolerable). This single-call path is what  uses for its
    Online Favorites view; intersecting locally is sub-second even with
    1000+ favs.

    The ``wm`` (watermark) param is required - the endpoint returns
    ``[]`` without it. It's the affiliate-tracking ID.  ships
    a rotating array of established watermarks so any single tracker
    doesn't get all the credit; we copy that pattern.

    Field shape differs from the room-list endpoint - field names
    ``username`` / ``image_url`` / ``room_subject`` / ``num_users`` map
    to ``slug`` / ``image`` / ``plot`` / ``viewers`` in our Model.
    """
    if not wm:
        raise ValueError("online_rooms_affiliate_url requires a watermark")
    qs = urlencode({"format": "json", "wm": wm})
    return f"{ONLINEROOMS_AFFILIATE_API}?{qs}"


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
