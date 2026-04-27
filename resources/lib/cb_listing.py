"""Pure parser for Chaturbate listing pages.

Takes raw HTML, returns a ``list[Model]``. Tolerant of missing fields
(empty pages, partial cards, garbage) - returns ``[]`` instead of
crashing. The TV loop / browse views never want to die on a single bad
page.

We use small regex-based field extraction rather than a full DOM
parser. Chaturbate's listing markup changes shape on each redesign;
loose regexes survive minor DOM tweaks better than tight selectors.
The parser only cares about a few invariants that have held for years:

- Each card is a ``<li class="roomCard">`` (or close enough) with the
  slug embedded both as the ``href`` and the ``data-room`` attribute.
- Viewers count is rendered as ``NNN viewers`` somewhere inside.
- Gender appears as ``data-gender="f|m|c|t"`` or as the long word
  inside a ``.gender`` span.
"""
from __future__ import annotations

import re

from resources.lib.cb_models import Gender, Model

_BASE = "https://chaturbate.com"

# A roomCard <li> ... </li> chunk. Greedy across newlines but stops at
# the next <li so we don't run all cards into one match.
_CARD_RE = re.compile(
    r'<li[^>]*\bclass="[^"]*roomCard[^"]*"[^>]*>(?P<body>.*?)</li>',
    re.DOTALL,
)

# Slug: prefer data-room (always machine-clean), fall back to href.
_SLUG_RE = re.compile(r'data-room="([A-Za-z0-9_\-]+)"')
_HREF_SLUG_RE = re.compile(r'href="/([A-Za-z0-9_\-]+)/"')

# Viewers: "NN viewers".
_VIEWERS_RE = re.compile(r'(\d+)\s*viewers', re.IGNORECASE)

# Gender: data-gender="f" / "m" / "c" / "t".
_GENDER_DATA_RE = re.compile(r'data-gender="([fmcst])"')

# Title text: <div class="title">name</div>. Optional - falls back to slug.
_TITLE_RE = re.compile(
    r'<div[^>]*\bclass="[^"]*title[^"]*"[^>]*>([^<]+)</div>',
    re.IGNORECASE,
)


def _parse_card(card_html: str) -> Model | None:
    """Try to build a Model from a single card body. None if no slug."""
    slug_match = _SLUG_RE.search(card_html) or _HREF_SLUG_RE.search(card_html)
    if not slug_match:
        return None
    slug = slug_match.group(1)

    title_match = _TITLE_RE.search(card_html)
    name = title_match.group(1).strip() if title_match else slug

    viewers = 0
    viewers_match = _VIEWERS_RE.search(card_html)
    if viewers_match:
        try:
            viewers = int(viewers_match.group(1))
        except ValueError:
            viewers = 0

    gender = Gender.UNKNOWN
    gender_match = _GENDER_DATA_RE.search(card_html)
    if gender_match:
        gender = Gender.from_str(gender_match.group(1))

    return Model(
        name=name,
        slug=slug,
        url=f"{_BASE}/{slug}/",
        is_live=True,
        viewers=viewers,
        gender=gender,
    )


def parse_top_cams(html: str) -> list[Model]:
    """Parse a Top Cams listing. Returns ``[]`` on empty or malformed input."""
    if not html:
        return []
    seen: set[str] = set()
    out: list[Model] = []
    for match in _CARD_RE.finditer(html):
        model = _parse_card(match.group("body"))
        if model is None or model.slug in seen:
            continue
        seen.add(model.slug)
        out.append(model)
    return out


def parse_gender_filter(html: str, gender: Gender) -> list[Model]:
    """Parse a listing and keep only models matching ``gender``."""
    return [m for m in parse_top_cams(html) if m.gender is gender]


def parse_search_results(html: str, query: str) -> list[Model]:
    """Parse a search-results page. The query is accepted for future
    fuzzy-filtering use; today we trust whatever Chaturbate returned.
    """
    _ = query
    return parse_top_cams(html)
