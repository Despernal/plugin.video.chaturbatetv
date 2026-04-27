"""Tests for resources.lib.tv_classify - pure classifiers for the TV loop.

Ported (rewritten) from the  snippet tests for
_cb_tv_is_natural_playlist_end, _cb_tv_classify_stop,
_cb_tv_is_internal_advance, plus _cb_decide_after_stop from the
reconnect snippet.
"""
from __future__ import annotations

from resources.lib.tv_classify import (
    classify_stop,
    decide_after_stop,
    is_internal_advance,
    is_natural_playlist_end,
)


# is_natural_playlist_end -----------------------------------------------------

def test_natural_end_at_last_position() -> None:
    assert is_natural_playlist_end(1, 2) is True


def test_natural_end_in_middle() -> None:
    assert is_natural_playlist_end(0, 2) is False


def test_natural_end_past_end_defensive() -> None:
    """Kodi can report pos == size after final advance."""
    assert is_natural_playlist_end(2, 2) is True


def test_natural_end_single_item_at_zero() -> None:
    assert is_natural_playlist_end(0, 1) is True


def test_natural_end_empty_playlist() -> None:
    assert is_natural_playlist_end(0, 0) is False


def test_natural_end_invalid_position() -> None:
    assert is_natural_playlist_end(-1, 2) is False


def test_natural_end_three_item_at_first() -> None:
    assert is_natural_playlist_end(0, 3) is False


def test_natural_end_three_item_at_last() -> None:
    assert is_natural_playlist_end(2, 3) is True


def test_natural_end_negative_size_defensive() -> None:
    assert is_natural_playlist_end(0, -1) is False


# classify_stop ---------------------------------------------------------------

def test_classify_stop_not_at_end_returns_false() -> None:
    assert classify_stop(False, 0, 100.0) is False
    assert classify_stop(False, 99.0, 100.0) is False


def test_classify_stop_first_at_end_is_natural() -> None:
    """No prior natural-end timestamp -> first at-end is natural."""
    assert classify_stop(True, 0, 100.0) is True


def test_classify_stop_second_at_end_within_threshold_is_user_stop() -> None:
    """Within 5s of prior end -> user double-tapped Stop, real exit."""
    assert classify_stop(True, 97.5, 100.0) is False
    assert classify_stop(True, 99.9, 100.0) is False


def test_classify_stop_second_at_end_outside_threshold_is_natural() -> None:
    assert classify_stop(True, 50.0, 100.0) is True
    # Exactly at threshold (delta == 5s) -> separate event, natural.
    assert classify_stop(True, 95.0, 100.0) is True


def test_classify_stop_custom_threshold() -> None:
    assert classify_stop(True, 99.0, 100.0, threshold=2.0) is False  # 1s diff < 2s
    assert classify_stop(True, 97.0, 100.0, threshold=2.0) is True   # 3s diff >= 2s


def test_classify_stop_threshold_zero_always_natural_when_at_end() -> None:
    """threshold=0 -> any non-zero delta is outside, always natural."""
    assert classify_stop(True, 99.99999, 100.0, threshold=0.0) is True


# is_internal_advance ---------------------------------------------------------

def test_internal_advance_in_queued_set() -> None:
    queued = {
        "plugin://plugin.video.chaturbatetv/?mode=play&slug=alice",
        "plugin://plugin.video.chaturbatetv/?mode=play&slug=bob",
    }
    assert is_internal_advance(
        "plugin://plugin.video.chaturbatetv/?mode=play&slug=bob", queued,
    ) is True


def test_internal_advance_not_in_queued_set() -> None:
    queued = {"plugin://plugin.video.chaturbatetv/?mode=play&slug=alice"}
    assert is_internal_advance(
        "plugin://plugin.video.chaturbatetv/?mode=play&slug=charlie", queued,
    ) is False


def test_internal_advance_local_video_file() -> None:
    queued = {"plugin://plugin.video.chaturbatetv/?mode=play&slug=alice"}
    assert is_internal_advance("/storage/.kodi/videos/movie.mp4", queued) is False


def test_internal_advance_other_plugin() -> None:
    queued = {"plugin://plugin.video.chaturbatetv/?mode=play&slug=alice"}
    assert is_internal_advance(
        "plugin://plugin.video.youtube/?mode=play&id=xyz", queued,
    ) is False


def test_internal_advance_empty_path() -> None:
    queued = {"plugin://plugin.video.chaturbatetv/?mode=play&slug=alice"}
    assert is_internal_advance("", queued) is False


def test_internal_advance_none_path() -> None:
    queued = {"plugin://plugin.video.chaturbatetv/?mode=play&slug=alice"}
    assert is_internal_advance(None, queued) is False


def test_internal_advance_empty_queued_set() -> None:
    """Before TV builds its playlist, queued is empty -> no advance counts as internal."""
    assert is_internal_advance(
        "plugin://plugin.video.chaturbatetv/?mode=play&slug=alice", set(),
    ) is False


# decide_after_stop -----------------------------------------------------------

def test_decide_after_stop_no_user_stopped_means_reconnect() -> None:
    """Stream ended naturally (Ended/Error path) -> always try to keep going."""
    assert decide_after_stop(user_stopped=False, model_live=False) is True
    assert decide_after_stop(user_stopped=False, model_live=True) is True


def test_decide_after_stop_real_user_stop_when_idle_low_and_offline() -> None:
    """Idle < 3s + model offline -> user pressed stop on a dying stream.
    decide_after_stop returns True (treat as Kodi-internal stop, reconnect)
    because model is offline; the offline-skip path handles that.
    Wait, our contract: returns True = continue, False = exit.
    Idle < 3s + model_live -> real user stop, return False (exit).
    Anything else -> return True (continue / reconnect).
    """
    assert decide_after_stop(user_stopped=True, model_live=True, idle_at_stop=0) is False
    assert decide_after_stop(user_stopped=True, model_live=True, idle_at_stop=2) is False


def test_decide_after_stop_high_idle_means_kodi_internal() -> None:
    """idle >= 3s suggests kodi self-terminated, not a user stop."""
    assert decide_after_stop(user_stopped=True, model_live=True, idle_at_stop=3) is True
    assert decide_after_stop(user_stopped=True, model_live=True, idle_at_stop=10) is True


def test_decide_after_stop_offline_model_falls_through() -> None:
    """Even with idle=0, an offline model is the ISA misfire pattern -> continue (return True)."""
    assert decide_after_stop(user_stopped=True, model_live=False, idle_at_stop=0) is True


def test_decide_after_stop_default_idle_zero() -> None:
    """Without idle param, behave like idle=0."""
    assert decide_after_stop(user_stopped=True, model_live=True) is False
    assert decide_after_stop(user_stopped=True, model_live=False) is True


def test_decide_after_stop_negative_idle_treated_as_zero() -> None:
    """Defensive: negative idle (Kodi shouldn't return this) is still a real user stop."""
    assert decide_after_stop(user_stopped=True, model_live=True, idle_at_stop=-1) is False
