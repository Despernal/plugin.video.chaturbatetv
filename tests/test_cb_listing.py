"""Tests for resources.lib.cb_listing - parse Chaturbate listing pages.

Pure module: takes HTML strings, returns list[Model]. No network, no
Kodi imports.
"""
from __future__ import annotations

from pathlib import Path

from resources.lib.cb_listing import (
    parse_gender_filter,
    parse_search_results,
    parse_top_cams,
)
from resources.lib.cb_models import Gender, Model

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# parse_top_cams
# --------------------------------------------------------------------------- #


def test_parse_top_cams_returns_list_of_models() -> None:
    models = parse_top_cams(_read("sample_top_cams.html"))
    assert all(isinstance(m, Model) for m in models)


def test_parse_top_cams_finds_all_rooms() -> None:
    models = parse_top_cams(_read("sample_top_cams.html"))
    assert len(models) == 4


def test_parse_top_cams_extracts_slugs() -> None:
    models = parse_top_cams(_read("sample_top_cams.html"))
    slugs = {m.slug for m in models}
    assert slugs == {"sample_room_1", "sample_room_2", "sample_room_3", "sample_room_4"}


def test_parse_top_cams_extracts_names() -> None:
    """Name defaults to slug when the title-text matches the slug exactly."""
    models = parse_top_cams(_read("sample_top_cams.html"))
    names = {m.name for m in models}
    assert "sample_room_1" in names


def test_parse_top_cams_extracts_viewers() -> None:
    models = parse_top_cams(_read("sample_top_cams.html"))
    by_slug = {m.slug: m for m in models}
    assert by_slug["sample_room_1"].viewers == 1234
    assert by_slug["sample_room_2"].viewers == 567
    assert by_slug["sample_room_3"].viewers == 42


def test_parse_top_cams_extracts_gender() -> None:
    models = parse_top_cams(_read("sample_top_cams.html"))
    by_slug = {m.slug: m for m in models}
    assert by_slug["sample_room_1"].gender is Gender.FEMALE
    assert by_slug["sample_room_2"].gender is Gender.COUPLE
    assert by_slug["sample_room_3"].gender is Gender.MALE
    assert by_slug["sample_room_4"].gender is Gender.TRANS


def test_parse_top_cams_builds_canonical_url() -> None:
    models = parse_top_cams(_read("sample_top_cams.html"))
    by_slug = {m.slug: m for m in models}
    assert by_slug["sample_room_1"].url == "https://chaturbate.com/sample_room_1/"


def test_parse_top_cams_marks_listing_models_as_live() -> None:
    """Listing pages only show currently-live rooms; treat all as live."""
    models = parse_top_cams(_read("sample_top_cams.html"))
    assert all(m.is_live for m in models)


def test_parse_top_cams_handles_empty_listing() -> None:
    models = parse_top_cams(_read("sample_empty_listing.html"))
    assert models == []


def test_parse_top_cams_handles_blank_input() -> None:
    assert parse_top_cams("") == []


def test_parse_top_cams_handles_garbage_html() -> None:
    """Not even close to a Chaturbate page -> empty list, no exceptions."""
    assert parse_top_cams("<html><body>Hello</body></html>") == []


def test_parse_top_cams_dedups_repeated_slugs() -> None:
    """Defensive: a single model card duplicated in markup must not be double-counted."""
    html = _read("sample_top_cams.html")
    doubled = html + html
    models = parse_top_cams(doubled)
    slugs = [m.slug for m in models]
    # Each slug appears at most once.
    assert len(slugs) == len(set(slugs))


# --------------------------------------------------------------------------- #
# parse_gender_filter
# --------------------------------------------------------------------------- #


def test_parse_gender_filter_female() -> None:
    models = parse_gender_filter(_read("sample_female_filter.html"), Gender.FEMALE)
    assert len(models) == 2
    assert all(m.gender is Gender.FEMALE for m in models)


def test_parse_gender_filter_keeps_only_matching_gender() -> None:
    """Mixed page (top cams) filtered by a gender returns only that gender."""
    models = parse_gender_filter(_read("sample_top_cams.html"), Gender.MALE)
    assert len(models) == 1
    assert models[0].slug == "sample_room_3"


def test_parse_gender_filter_empty_when_no_match() -> None:
    models = parse_gender_filter(_read("sample_female_filter.html"), Gender.MALE)
    assert models == []


def test_parse_gender_filter_extracts_viewers() -> None:
    models = parse_gender_filter(_read("sample_female_filter.html"), Gender.FEMALE)
    by_slug = {m.slug: m for m in models}
    assert by_slug["sample_female_1"].viewers == 2010


# --------------------------------------------------------------------------- #
# parse_search_results
# --------------------------------------------------------------------------- #


def test_parse_search_results_returns_models() -> None:
    models = parse_search_results(_read("sample_search_results.html"), "sample")
    assert len(models) == 1
    assert models[0].slug == "sample_search_1"


def test_parse_search_results_empty_when_no_matches() -> None:
    models = parse_search_results(_read("sample_empty_listing.html"), "ghost")
    assert models == []


def test_parse_search_results_query_is_currently_unused_but_accepted() -> None:
    """The query is reserved for future fuzzy filtering; for now we just
    return whatever the page rendered."""
    models = parse_search_results(_read("sample_search_results.html"), "anything")
    assert len(models) == 1


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #


def test_parse_top_cams_skips_card_without_slug() -> None:
    html = """
    <html><body><ul>
    <li class="roomCard"><a><div class="title">no-slug</div></a></li>
    <li class="roomCard"><a href="/good_slug/" data-room="good_slug">
        <img src="https://x/good_slug.jpg" alt="good_slug">
        <div class="title">good_slug</div>
        <div class="details"><span class="viewers">5 viewers</span>
        <span class="gender" data-gender="f">Female</span></div>
    </a></li>
    </ul></body></html>
    """
    models = parse_top_cams(html)
    assert len(models) == 1
    assert models[0].slug == "good_slug"


def test_parse_top_cams_handles_zero_viewers() -> None:
    html = """
    <html><body><ul>
    <li class="roomCard"><a href="/quiet/" data-room="quiet">
        <img src="https://x/quiet.jpg" alt="quiet">
        <div class="title">quiet</div>
        <div class="details"><span class="viewers">0 viewers</span>
        <span class="gender" data-gender="f">Female</span></div>
    </a></li>
    </ul></body></html>
    """
    models = parse_top_cams(html)
    assert len(models) == 1
    assert models[0].viewers == 0


def test_parse_top_cams_unknown_gender_falls_back() -> None:
    html = """
    <html><body><ul>
    <li class="roomCard"><a href="/x/" data-room="x">
        <img src="https://x/x.jpg" alt="x">
        <div class="title">x</div>
        <div class="details"><span class="viewers">1 viewers</span></div>
    </a></li>
    </ul></body></html>
    """
    models = parse_top_cams(html)
    assert len(models) == 1
    assert models[0].gender is Gender.UNKNOWN
