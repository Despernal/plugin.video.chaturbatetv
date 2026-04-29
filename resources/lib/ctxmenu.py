"""State-aware context-menu builder.

Pure module: no Kodi imports. Returns a list of
``(label, runplugin_url)`` tuples that the caller wraps into
``ListItem.addContextMenuItems``. Lifting this logic out of the view
modules makes the state-permutations (in TV vs not, in favs vs not)
unit-testable without Kodi harness.

The shape of the returned ``runplugin_url`` is::

    RunPlugin(plugin://plugin.video.chaturbatetv/?mode=...&slug=...)

Kodi parses ``RunPlugin(...)`` as a builtin and dispatches the inner
plugin URL through our router.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from resources.lib.cb_models import Favorite, TVEntry

_PLUGIN_PREFIX = "plugin://plugin.video.chaturbatetv/"


def _runplugin(mode: str, **params: Any) -> str:
    qs_dict = {"mode": mode}
    for k, v in params.items():
        if v is None:
            continue
        qs_dict[k] = str(v)
    qs = urlencode(qs_dict)
    return f"RunPlugin({_PLUGIN_PREFIX}?{qs})"


def _find_tv_entry(url: str, tv_entries: list[TVEntry]) -> TVEntry | None:
    for e in tv_entries:
        if e.url == url:
            return e
    return None


def _is_in_favs(slug: str, favs: list[Favorite]) -> bool:
    return any(f.slug == slug for f in favs)


def build_ctxmenu(
    model: dict[str, Any],
    tv_entries: list[TVEntry],
    favs: list[Favorite],
) -> list[tuple[str, str]]:
    """Build the context menu for a model.

    ``model`` is the small dict shape used throughout the addon
    (``slug``, ``name``, ``url``). Returns ``[]`` if the model has no
    slug since no verb is meaningful.
    """
    slug = str(model.get("slug") or "")
    if not slug:
        return []
    name = str(model.get("name") or slug)
    url = str(model.get("url") or f"https://chaturbate.com/{slug}/")

    items: list[tuple[str, str]] = []

    tv_match = _find_tv_entry(url, tv_entries)
    if tv_match is None:
        items.append(("Add to TV", _runplugin("tv_add", slug=slug, name=name)))
    else:
        items.append(
            (f"[In TV P{tv_match.priority}]",
             _runplugin("tv_edit", slug=slug)),
        )
        items.append(("Edit TV Priority", _runplugin("tv_edit", slug=slug)))
        items.append(("Remove from TV", _runplugin("tv_remove", slug=slug)))

    if _is_in_favs(slug, favs):
        items.append(("Remove from Favorites", _runplugin("fav_remove", slug=slug)))
    else:
        items.append(
            ("Add to Favorites",
             _runplugin("fav_add", slug=slug, name=name, url=url)),
        )

    # v0.7.23: per-row "Update model info" entry. Always present so
    # the user can refresh any single model from any view (browse,
    # favs, TV list) without firing the 20-minute deep crawl.
    items.append(
        ("Update model info",
         _runplugin("refresh_one_model", slug=slug)),
    )

    # v0.7.25: "View info" opens the rich profile directory -- full
    # bio in the right pane plus browseable photo_sets. Always
    # present; the directory does an inline biocontext fetch when
    # the DB row is empty so even never-seen-online models render.
    items.append(
        ("View info",
         _runplugin("view_model_info", slug=slug)),
    )

    return items
