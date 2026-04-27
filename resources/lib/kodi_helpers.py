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


def add_play_item(handle: int, label: str, slug: str, image: str | None = None,
                  **props: Any) -> None:
    """Add a playable ListItem that routes through ``mode=playvid``."""
    import xbmcgui
    import xbmcplugin

    url = _build_url("playvid", slug=slug, **props)
    li = xbmcgui.ListItem(label=label)
    li.setProperty("IsPlayable", "true")
    if image:
        li.setArt({"thumb": image, "icon": image, "fanart": image})
    xbmcplugin.addDirectoryItem(handle=handle, url=url, listitem=li, isFolder=False)


def end_directory(handle: int, succeeded: bool = True) -> None:
    """Close the current directory listing."""
    import xbmcplugin
    xbmcplugin.endOfDirectory(handle, succeeded=succeeded)
