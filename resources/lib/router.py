"""Mode dispatch from ``sys.argv``.

Kodi calls our ``default.py`` with three argv elements:

- ``argv[0]`` - plugin URL prefix
- ``argv[1]`` - the directory handle (string-formatted int)
- ``argv[2]`` - query string (e.g. ``?mode=top&page=2``); empty for the
  top-level entry click.

``parse_qs`` turns the query string into a flat ``dict[str, str]``.
``dispatch`` looks up a handler by ``mode`` and calls it with
``handle=<int>`` plus the rest of the params as kwargs.

The shipped ``DEFAULT_HANDLERS`` registry is built lazily so the
modules it imports (browse_views, favs_views, addon_actions) don't need
to load until Kodi actually invokes the plugin. Tests pass their own
registry.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl

_Handler = Callable[..., None]


def parse_qs(qs: str) -> dict[str, str]:
    """Parse a Kodi-style query string into a flat dict.

    Strips the leading ``?`` if present. Decodes percent-escapes. Empty
    input -> empty dict.
    """
    if not qs:
        return {}
    if qs.startswith("?"):
        qs = qs[1:]
    return dict(parse_qsl(qs, keep_blank_values=False))


def dispatch(argv: list[str], handlers: dict[str, _Handler]) -> None:
    """Look up a handler by mode and call it with handle + params."""
    if len(argv) < 3:
        return
    try:
        handle = int(argv[1])
    except (TypeError, ValueError):
        handle = -1
    params = parse_qs(argv[2])
    mode = params.pop("mode", "")
    if not mode:
        main = handlers.get("main")
        if main is not None:
            main(handle=handle, **params)
        return
    handler = handlers.get(mode)
    if handler is None:
        fallback = handlers.get("_fallback")
        if fallback is not None:
            fallback(handle=handle, **params)
        return
    handler(handle=handle, **params)


# --------------------------------------------------------------------------- #
# Default handler registry
# --------------------------------------------------------------------------- #


class _LazyHandlers(dict[str, _Handler]):
    """Dict that fills itself on first read.

    Importing browse_views / favs_views / addon_actions at module-load
    time would force xbmc/xbmcgui imports during ``resources.lib.router``
    import, breaking pure-router tests. Filling lazily means the heavy
    imports only happen when the production code actually runs under
    Kodi.
    """

    _filled = False

    def _fill(self) -> None:
        if self._filled:
            return
        self._filled = True
        from resources.lib import (
            addon_actions,
            browse_views,
            favs_views,
        )

        self["main"] = browse_views.main_menu
        self["top"] = browse_views.top_cams_view
        self["new"] = browse_views.new_cams_view
        self["gender"] = browse_views.gender_view
        self["search"] = browse_views.search_view
        self["favs"] = favs_views.favs_menu
        self["favs_online"] = favs_views.online_favs_view
        self["favs_offline"] = favs_views.offline_favs_view

        self["playvid"] = addon_actions.playvid
        self["tv_play"] = addon_actions.tv_play
        self["tv_stop"] = addon_actions.tv_stop
        self["tv_list"] = addon_actions.tv_list
        self["tv_add"] = addon_actions.tv_add
        self["tv_remove"] = addon_actions.tv_remove
        self["tv_edit"] = addon_actions.tv_edit
        self["fav_add"] = addon_actions.fav_add
        self["fav_remove"] = addon_actions.fav_remove

    def get(self, key: str, default: Any = None) -> Any:
        self._fill()
        return super().get(key, default)

    def __getitem__(self, key: str) -> _Handler:
        self._fill()
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self._fill()
        return super().__contains__(key)

    def keys(self) -> Any:
        self._fill()
        return super().keys()


DEFAULT_HANDLERS: dict[str, _Handler] = _LazyHandlers()
