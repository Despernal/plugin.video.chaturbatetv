"""Tests for resources.lib.ctxmenu - state-aware context-menu builder.

Pure module: takes the current TV-list, current favs-list, and a
target (slug/url) and returns a list of (label, runplugin_url) tuples.
"""
from __future__ import annotations

from resources.lib.cb_models import Favorite, Gender, TVEntry
from resources.lib.ctxmenu import build_ctxmenu


def _model(slug: str, name: str | None = None) -> dict[str, str]:
    return {
        "slug": slug,
        "name": name or slug,
        "url": f"https://chaturbate.com/{slug}/",
    }


# --------------------------------------------------------------------------- #
# Membership: not in TV, not in favs
# --------------------------------------------------------------------------- #


def test_ctxmenu_not_in_tv_not_in_favs() -> None:
    items = build_ctxmenu(_model("alice"), tv_entries=[], favs=[])
    labels = [label for label, _url in items]
    assert "Add to Favorites" in labels
    assert "Add to TV" in labels
    assert all("Edit TV Priority" not in label for label in labels)


def test_ctxmenu_runplugin_url_for_add_to_favs() -> None:
    items = build_ctxmenu(_model("alice"), tv_entries=[], favs=[])
    add_url = next(url for label, url in items if label == "Add to Favorites")
    assert "RunPlugin(" in add_url
    assert "mode=fav_add" in add_url
    assert "slug=alice" in add_url


def test_ctxmenu_runplugin_url_for_add_to_tv() -> None:
    items = build_ctxmenu(_model("alice"), tv_entries=[], favs=[])
    add_url = next(url for label, url in items if label == "Add to TV")
    assert "RunPlugin(" in add_url
    assert "mode=tv_add" in add_url
    assert "slug=alice" in add_url


# --------------------------------------------------------------------------- #
# In TV
# --------------------------------------------------------------------------- #


def test_ctxmenu_in_tv_shows_priority_and_edit_remove() -> None:
    tv = [TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=17)]
    items = build_ctxmenu(_model("alice"), tv_entries=tv, favs=[])
    labels = [label for label, _url in items]
    assert any("[In TV P17]" in label for label in labels)
    assert any("Edit TV Priority" in label for label in labels)
    assert any("Remove from TV" in label for label in labels)
    assert "Add to TV" not in labels


def test_ctxmenu_remove_from_tv_runplugin_uses_tv_remove_mode() -> None:
    tv = [TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=10)]
    items = build_ctxmenu(_model("alice"), tv_entries=tv, favs=[])
    remove_url = next(url for label, url in items if "Remove from TV" in label)
    assert "mode=tv_remove" in remove_url
    assert "slug=alice" in remove_url


def test_ctxmenu_edit_tv_runplugin_uses_tv_edit_mode() -> None:
    tv = [TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=10)]
    items = build_ctxmenu(_model("alice"), tv_entries=tv, favs=[])
    edit_url = next(url for label, url in items if "Edit TV Priority" in label)
    assert "mode=tv_edit" in edit_url


# --------------------------------------------------------------------------- #
# In favs
# --------------------------------------------------------------------------- #


def test_ctxmenu_in_favs_shows_remove_only() -> None:
    favs = [Favorite(name="alice", slug="alice", url="https://chaturbate.com/alice/",
                     gender=Gender.FEMALE)]
    items = build_ctxmenu(_model("alice"), tv_entries=[], favs=favs)
    labels = [label for label, _url in items]
    assert "Remove from Favorites" in labels
    assert "Add to Favorites" not in labels


def test_ctxmenu_remove_from_favs_runplugin_uses_fav_remove() -> None:
    favs = [Favorite(name="alice", slug="alice", url="https://chaturbate.com/alice/",
                     gender=Gender.FEMALE)]
    items = build_ctxmenu(_model("alice"), tv_entries=[], favs=favs)
    remove_url = next(url for label, url in items if label == "Remove from Favorites")
    assert "mode=fav_remove" in remove_url
    assert "slug=alice" in remove_url


# --------------------------------------------------------------------------- #
# Both
# --------------------------------------------------------------------------- #


def test_ctxmenu_in_both_tv_and_favs() -> None:
    tv = [TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=5)]
    favs = [Favorite(name="alice", slug="alice", url="https://chaturbate.com/alice/",
                     gender=Gender.FEMALE)]
    items = build_ctxmenu(_model("alice"), tv_entries=tv, favs=favs)
    labels = [label for label, _url in items]
    assert any("[In TV P5]" in label for label in labels)
    assert "Remove from Favorites" in labels
    assert "Add to Favorites" not in labels
    assert "Add to TV" not in labels


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #


def test_ctxmenu_returns_list_of_tuples() -> None:
    items = build_ctxmenu(_model("alice"), tv_entries=[], favs=[])
    for entry in items:
        assert isinstance(entry, tuple)
        assert len(entry) == 2
        label, url = entry
        assert isinstance(label, str)
        assert isinstance(url, str)


def test_ctxmenu_handles_missing_slug_returns_empty() -> None:
    """A model dict with no slug -> no actionable verbs."""
    items = build_ctxmenu({"name": "x", "url": ""}, tv_entries=[], favs=[])
    assert items == []


def test_ctxmenu_unicode_slug_url_encoded() -> None:
    """Non-ASCII slugs survive URL encoding into the runplugin payload."""
    items = build_ctxmenu(_model("test_unicode_user"), tv_entries=[], favs=[])
    add_url = next(url for label, url in items if label == "Add to Favorites")
    assert "slug=test_unicode_user" in add_url
