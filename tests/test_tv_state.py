"""Tests for resources.lib.tv_state - thin wrapper over Window props.

The wrapper takes a (get, set) callable pair so tests can stub Kodi
out completely. The real production callable pair is built in
``resources.lib.tv_state.kodi_window_pair`` (uses xbmcgui.Window(10000)).
"""
from __future__ import annotations

from resources.lib.tv_state import TVState


def _stubs() -> tuple[dict[str, str], TVState]:
    store: dict[str, str] = {}

    def getter(key: str) -> str:
        return store.get(key, "")

    def setter(key: str, value: str) -> None:
        store[key] = value

    return store, TVState(getter, setter)


def test_initial_state_is_inactive() -> None:
    store, state = _stubs()
    assert state.is_active() is False
    # No write happened just from reading.
    assert store == {}


def test_set_active_true_writes_flag() -> None:
    store, state = _stubs()
    state.set_active(True)
    assert state.is_active() is True
    assert store["chaturbatetv_active"] == "1"


def test_set_active_false_writes_zero() -> None:
    store, state = _stubs()
    state.set_active(True)
    state.set_active(False)
    assert state.is_active() is False
    assert store["chaturbatetv_active"] == "0"


def test_clear_resets_flag() -> None:
    store, state = _stubs()
    state.set_active(True)
    state.clear()
    assert state.is_active() is False
    assert store["chaturbatetv_active"] == "0"


def test_state_uses_chaturbatetv_active_key() -> None:
    """The key must be chaturbatetv_active so we don't collide with
    legacy ``cb_tv_active`` keys from older addons during a transition.
    """
    store, state = _stubs()
    state.set_active(True)
    assert "chaturbatetv_active" in store
    assert "cb_tv_active" not in store


def test_is_active_treats_only_one_as_active() -> None:
    """Window props are stringly typed; '1' is active, anything else inactive."""
    store: dict[str, str] = {"chaturbatetv_active": "1"}
    state = TVState(lambda k: store.get(k, ""), lambda k, v: store.update({k: v}))
    assert state.is_active() is True

    store["chaturbatetv_active"] = "0"
    assert state.is_active() is False

    store["chaturbatetv_active"] = ""
    assert state.is_active() is False

    store["chaturbatetv_active"] = "true"
    assert state.is_active() is False  # only the literal '1' counts


def test_state_double_set_active_idempotent() -> None:
    store, state = _stubs()
    state.set_active(True)
    state.set_active(True)
    assert store["chaturbatetv_active"] == "1"
    assert state.is_active() is True
