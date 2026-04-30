"""Tests for resources.lib.router - mode dispatch from sys.argv.

The router parses ``sys.argv[2]`` (the query string Kodi hands us)
and dispatches to a registered handler. We expose:

- ``parse_qs(qs) -> dict[str, str]`` - pure, stdlib-backed.
- ``dispatch(argv, handlers) -> None`` - look up handler by mode and
  call with handle + parsed kwargs.
- ``DEFAULT_HANDLERS`` - the production registry. We don't import it
  in tests; we pass our own.

When the query string is empty (top-level addon click), the router
calls the ``main`` handler.
"""
from __future__ import annotations

from typing import Any

import pytest

from resources.lib import router


# --------------------------------------------------------------------------- #
# parse_qs
# --------------------------------------------------------------------------- #


def test_parse_qs_empty_returns_empty_dict() -> None:
    assert router.parse_qs("") == {}


def test_parse_qs_strips_leading_question_mark() -> None:
    assert router.parse_qs("?mode=top") == {"mode": "top"}


def test_parse_qs_extracts_multiple_keys() -> None:
    out = router.parse_qs("?mode=gender&gender=female&page=2")
    assert out == {"mode": "gender", "gender": "female", "page": "2"}


def test_parse_qs_decodes_percent_escapes() -> None:
    out = router.parse_qs("?mode=search&query=hello%20world")
    assert out["query"] == "hello world"


def test_parse_qs_handles_no_leading_question_mark() -> None:
    assert router.parse_qs("mode=top") == {"mode": "top"}


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def _make_argv(qs: str, handle: str = "1") -> list[str]:
    return ["plugin://plugin.video.chaturbatetv/", handle, qs]


def test_dispatch_calls_main_when_qs_empty() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def main(handle: int, **params: Any) -> None:
        calls.append(("main", {"handle": handle, **params}))

    router.dispatch(_make_argv(""), {"main": main})
    assert calls == [("main", {"handle": 1})]


def test_dispatch_calls_handler_by_mode() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def top(handle: int, **params: Any) -> None:
        calls.append(("top", {"handle": handle, **params}))

    router.dispatch(_make_argv("?mode=top"), {"top": top})
    assert calls == [("top", {"handle": 1})]


def test_dispatch_passes_extra_params_as_kwargs() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def gender(handle: int, **params: Any) -> None:
        calls.append(("gender", params))

    router.dispatch(_make_argv("?mode=gender&gender=female&page=2"),
                    {"gender": gender})
    assert calls == [("gender", {"gender": "female", "page": "2"})]


def test_dispatch_passes_handle_as_int() -> None:
    captured: dict[str, Any] = {}

    def top(handle: int, **params: Any) -> None:
        captured["handle"] = handle
        captured["type"] = type(handle)

    router.dispatch(_make_argv("?mode=top", handle="42"), {"top": top})
    assert captured["handle"] == 42
    assert captured["type"] is int


def test_dispatch_unknown_mode_calls_fallback_when_provided() -> None:
    calls: list[str] = []

    def fallback(handle: int, **params: Any) -> None:
        calls.append("fallback")

    router.dispatch(_make_argv("?mode=ghost"),
                    {"main": lambda **k: None, "_fallback": fallback})
    assert calls == ["fallback"]


def test_dispatch_unknown_mode_no_fallback_silently_drops() -> None:
    """Don't crash, just return."""
    router.dispatch(_make_argv("?mode=ghost"), {"main": lambda **k: None})


def test_dispatch_does_not_raise_on_short_argv() -> None:
    """argv with fewer than 3 elements -> dispatch returns cleanly."""
    router.dispatch(["plugin://"], {"main": lambda **k: None})


def test_dispatch_catches_handler_exception_and_closes_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.38 (audit pass #4 HIGH #1): handler exceptions used to
    propagate uncaught -> Kodi showed "Plugin failed to start" toast
    AND left the busy spinner spinning until ~30s timeout. Now the
    router catches, logs, and calls endOfDirectory(handle,
    succeeded=False) so the spinner clears immediately and the user
    sees a "Action failed" notification instead of generic Kodi
    failure UI.
    """
    import sys
    from unittest.mock import MagicMock

    fake_xbmcplugin = MagicMock()
    fake_xbmcgui = MagicMock()
    fake_xbmcgui.NOTIFICATION_ERROR = "error"
    monkeypatch.setitem(sys.modules, "xbmcplugin", fake_xbmcplugin)
    monkeypatch.setitem(sys.modules, "xbmcgui", fake_xbmcgui)

    def boom(handle: int, **params: Any) -> None:
        raise RuntimeError("dispatcher-test")

    # Should NOT raise.
    router.dispatch(_make_argv("?mode=top"), {"top": boom})

    # endOfDirectory was called with succeeded=False on the handle.
    end_calls = fake_xbmcplugin.endOfDirectory.call_args_list
    # _make_argv defaults handle="1"
    assert any(
        c.args and c.args[0] == 1
        and c.kwargs.get("succeeded") is False
        for c in end_calls
    ), (
        f"expected endOfDirectory(1, succeeded=False); got "
        f"{end_calls!r}"
    )

    # And the user got a notification mentioning the mode.
    notif_calls = fake_xbmcgui.Dialog.return_value.notification.call_args_list
    assert any(
        "top" in str(c).lower() or "failed" in str(c).lower()
        for c in notif_calls
    ), f"expected user notification about failure; got {notif_calls!r}"


def test_dispatch_handler_exception_skips_endOfDirectory_when_handle_is_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RunPlugin invocations have handle=-1 (action verbs, no
    directory). Calling endOfDirectory(-1, ...) is invalid and would
    raise inside Kodi. Skip it on negative handles."""
    import sys
    from unittest.mock import MagicMock

    fake_xbmcplugin = MagicMock()
    fake_xbmcgui = MagicMock()
    fake_xbmcgui.NOTIFICATION_ERROR = "error"
    monkeypatch.setitem(sys.modules, "xbmcplugin", fake_xbmcplugin)
    monkeypatch.setitem(sys.modules, "xbmcgui", fake_xbmcgui)

    def boom(handle: int, **params: Any) -> None:
        raise RuntimeError("dispatcher-test")

    # handle=-1 from a RunPlugin invocation
    router.dispatch(["plugin://", "-1", "?mode=fav_add"],
                    {"fav_add": boom})

    end_calls = fake_xbmcplugin.endOfDirectory.call_args_list
    assert end_calls == [], (
        "must NOT call endOfDirectory on handle=-1 (RunPlugin path)"
    )


# --------------------------------------------------------------------------- #
# Registered modes
# --------------------------------------------------------------------------- #


def test_default_handlers_registry_has_expected_modes() -> None:
    """The shipped registry must cover every URL the addon emits."""
    needed = {
        "main", "top", "new", "gender", "search", "search_prompt",
        "favs", "favs_online", "favs_offline",
        "playvid", "tv_play", "tv_stop", "tv_list",
        "tv_add", "tv_remove", "tv_edit",
        "fav_add", "fav_remove",
    }
    missing = needed - set(router.DEFAULT_HANDLERS.keys())
    assert missing == set(), f"DEFAULT_HANDLERS missing: {missing}"
