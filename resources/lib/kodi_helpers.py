"""Thin shims over xbmcplugin.addDirectoryItem.

Two helpers cover ~all the call sites in the addon:

- ``add_dir``       -> folder entry that drills into a sub-listing.
- ``add_play_item`` -> playable entry that resolves through ``mode=playvid``.

Both build the plugin URL the same way: ``plugin://<id>/?mode=...&...``.
The router (``resources.lib.router``) parses these back out on the way
in, so URL shape is single-source-of-truth between this module and that
one.

Importing xbmc/xbmcgui/xbmcplugin is deferred to function bodies so a
top-level import of this module from a pure test never pulls Kodi into
sys.modules.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

_PLUGIN_PREFIX = "plugin://plugin.video.chaturbatetv/"


def _build_url(mode: str, **params: Any) -> str:
    """Compose a plugin URL with the given mode and arbitrary string params."""
    qs = {"mode": mode}
    for k, v in params.items():
        if v is None:
            continue
        qs[k] = str(v)
    return f"{_PLUGIN_PREFIX}?{urlencode(qs)}"


def add_dir(handle: int, label: str, mode: str, image: str | None = None,
            **params: Any) -> None:
    """Add a sub-folder ListItem to the current directory."""
    import xbmcgui
    import xbmcplugin

    url = _build_url(mode, **params)
    li = xbmcgui.ListItem(label=label)
    if image:
        li.setArt({"thumb": image, "icon": image})
    xbmcplugin.addDirectoryItem(handle=handle, url=url, listitem=li, isFolder=True)


def add_play_item(handle: int, label: str, slug: str,
                  image: str | None = None,
                  plot: str | None = None,
                  ctx_items: list[tuple[str, str]] | None = None,
                  **props: Any) -> None:
    """Add a playable ListItem that routes through ``mode=playvid``.

    ``plot`` is a Kodi video-info string (Age / Location / etc); when
    non-empty it lands as ``setInfo("video", {"plot": ...})`` so Kodi
    renders it in the right-pane on hover. ``plot`` is metadata for
    the ListItem and is intentionally NOT added to the plugin URL
    query string (the URL is for routing, not display).

    ``ctx_items`` is an optional list of ``(label, runplugin_url)``
    tuples for the right-click context menu. Use the ``ctxmenu`` module
    to build state-aware entries (Add to TV vs In TV / Edit / Remove,
    etc.) and pass the result here.
    """
    import xbmcgui
    import xbmcplugin

    url = _build_url("playvid", slug=slug, **props)
    li = xbmcgui.ListItem(label=label)
    li.setProperty("IsPlayable", "true")
    if image:
        li.setArt({"thumb": image, "icon": image, "fanart": image})
    if plot:
        li.setInfo("video", {"plot": plot, "title": label})
    if ctx_items:
        li.addContextMenuItems(ctx_items)
    xbmcplugin.addDirectoryItem(handle=handle, url=url, listitem=li, isFolder=False)


def end_directory(handle: int, succeeded: bool = True,
                  content_type: str | None = None,
                  unsorted: bool = False) -> None:
    """Close the current directory listing.

    ``content_type``, when non-empty, declares the directory's content
    flavour to Kodi via ``xbmcplugin.setContent`` BEFORE the directory
    is finalised. The common value is ``"videos"``, which unlocks
    Kodi's video-specific view modes (InfoWall, MediaList, Wide) that
    put the thumbnail on the right and the plot on the left - the
    layout that fits Chaturbate's verbose room descriptions far better
    than the default 'files' view mode (thumb-on-left, plot truncated).

    ``unsorted=True`` declares ``SORT_METHOD_UNSORTED`` so Kodi keeps
    the insertion order the addon specified rather than alphabetizing
    by label. Critical for any view where the label embeds an ordering
    hint (e.g. ``[P17]`` priority prefix in TV mode) - alphabetical sort
    would put ``[P01]`` before ``[P17]`` which is the opposite of what
    descending-priority sort intends.

    Order matters: setContent must run BEFORE endOfDirectory or Kodi
    has already locked the listing as 'files' content and the hint is
    ignored.
    """
    import xbmcplugin
    if content_type:
        xbmcplugin.setContent(handle, content_type)
    if unsorted:
        xbmcplugin.addSortMethod(handle, xbmcplugin.SORT_METHOD_UNSORTED)
    xbmcplugin.endOfDirectory(handle, succeeded=succeeded)
