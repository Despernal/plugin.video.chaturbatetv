"""Tests for resources.lib.tv_classify - pure classifiers for the TV loop.

Covers is_natural_playlist_end, classify_stop,
is_internal_advance, plus decide_after_stop on the
reconnect path.
"""
from __future__ import annotations

from resources.lib.tv_classify import (
    classify_stop,
    decide_after_stop,
    is_internal_advance,
    is_natural_playlist_end,
    is_progress_stalled,
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


def test_classify_stop_at_end_always_natural_v0_7_13() -> None:
    """v0.7.13 dropped the second-at-end-within-5s exit per user
    directive: no double-press gestures, sticky-playback wins.

    Any at-end stop is now treated as a natural end -> rebuild.
    Users exit via the Yes/No dialog or the "Stop TV mode" menu item.
    The ``last_natural_end_time`` and ``threshold`` parameters are
    preserved in the signature for backward-compat with existing
    callers but are ignored.
    """
    # First at-end: natural.
    assert classify_stop(True, 0, 100.0) is True
    # Second at-end "within 5s" used to be a force-exit; now natural.
    assert classify_stop(True, 97.5, 100.0) is True
    assert classify_stop(True, 99.9, 100.0) is True
    # Outside threshold: also natural.
    assert classify_stop(True, 50.0, 100.0) is True
    # Custom threshold no longer affects behavior.
    assert classify_stop(True, 99.0, 100.0, threshold=2.0) is True
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


# --------------------------------------------------------------------------- #
# is_progress_stalled (v0.7.41) - watches getTime() to detect ISA-side
# decoder stalls that don't surface as onPlayBackStopped events.
# --------------------------------------------------------------------------- #


def test_progress_stalled_first_sample_seeds_state() -> None:
    """No prior position: seed last_position to current and last_advance_at
    to now, no stall fires regardless of elapsed."""
    stalled, last_pos, last_at = is_progress_stalled(
        cur_position=0.0,
        last_position=None,
        last_advance_at=0.0,
        is_paused=False,
        now=100.0,
        elapsed_in_inner_loop=999.0,  # even past grace, no stall on first call
    )
    assert stalled is False
    assert last_pos == 0.0
    assert last_at == 100.0


def test_progress_stalled_advancing_position_resets_timer() -> None:
    """Position advanced (>0.1s tolerance): refresh both counters, no stall."""
    stalled, last_pos, last_at = is_progress_stalled(
        cur_position=12.5,
        last_position=10.0,
        last_advance_at=50.0,
        is_paused=False,
        now=53.0,
        elapsed_in_inner_loop=15.0,
    )
    assert stalled is False
    assert last_pos == 12.5
    assert last_at == 53.0


def test_progress_stalled_within_grace_no_stall() -> None:
    """Position stuck but we're still in the initial buffering grace
    window: state preserved, no stall."""
    stalled, last_pos, last_at = is_progress_stalled(
        cur_position=0.0,
        last_position=0.0,
        last_advance_at=100.0,
        is_paused=False,
        now=105.0,  # 5s after the first seed
        elapsed_in_inner_loop=5.0,  # grace=10s default, still inside
    )
    assert stalled is False
    assert last_pos == 0.0
    assert last_at == 100.0


def test_progress_stalled_past_grace_within_stall_window() -> None:
    """Position stuck, past grace, but the stuck-window hasn't been long
    enough yet (default stall=20s)."""
    stalled, _, _ = is_progress_stalled(
        cur_position=42.0,
        last_position=42.0,
        last_advance_at=100.0,
        is_paused=False,
        now=115.0,  # 15s since last advance
        elapsed_in_inner_loop=30.0,  # past grace
    )
    assert stalled is False


def test_progress_stalled_past_grace_past_stall_fires() -> None:
    """Position hasn't advanced for >= stall_seconds while past grace:
    stalled=True. This is the v0.7.41 ISA-decoder-freeze case."""
    stalled, _, _ = is_progress_stalled(
        cur_position=42.0,
        last_position=42.0,
        last_advance_at=100.0,
        is_paused=False,
        now=121.0,  # 21s of no advance, default stall=20s
        elapsed_in_inner_loop=30.0,
    )
    assert stalled is True


def test_progress_stalled_paused_does_not_fire_even_past_window() -> None:
    """User pause should never fire stall, no matter how long. Defer the
    advance timer to ``now`` so resume gets a fresh window."""
    stalled, last_pos, last_at = is_progress_stalled(
        cur_position=42.0,
        last_position=42.0,
        last_advance_at=100.0,
        is_paused=True,
        now=200.0,  # 100s paused, far past stall window
        elapsed_in_inner_loop=120.0,
    )
    assert stalled is False
    assert last_pos == 42.0  # position unchanged
    assert last_at == 200.0  # timer deferred to now


def test_progress_stalled_resume_after_pause_gets_fresh_window() -> None:
    """After a pause defers the timer, the post-resume sample finds a
    fresh stall_seconds window, NOT the cumulative pre-pause stuck time.
    """
    # Step 1: paused for 60s, timer deferred each tick.
    _, last_pos, last_at = is_progress_stalled(
        cur_position=42.0,
        last_position=42.0,
        last_advance_at=100.0,
        is_paused=True,
        now=160.0,
        elapsed_in_inner_loop=70.0,
    )
    assert last_at == 160.0  # deferred to now
    # Step 2: now resumed but the stream's still genuinely stuck. The
    # stall window starts counting from the post-pause defer (160).
    # 5s of no advance after resume should NOT fire yet.
    stalled, _, _ = is_progress_stalled(
        cur_position=42.0,
        last_position=last_pos,
        last_advance_at=last_at,
        is_paused=False,
        now=165.0,
        elapsed_in_inner_loop=75.0,
    )
    assert stalled is False  # only 5s of post-resume stuck time


def test_progress_stalled_floating_point_jitter_tolerance() -> None:
    """Tiny position deltas (<0.1s) shouldn't count as advances - they're
    typically Kodi's getTime() rounding noise on a stalled stream."""
    stalled, last_pos, _last_at = is_progress_stalled(
        cur_position=42.05,
        last_position=42.0,
        last_advance_at=100.0,
        is_paused=False,
        now=130.0,  # 30s past last_advance, well past stall=20s
        elapsed_in_inner_loop=40.0,
    )
    assert stalled is True
    assert last_pos == 42.0  # unchanged - sub-tolerance jitter ignored


def test_progress_stalled_custom_thresholds() -> None:
    """Caller can pass tighter thresholds for tests or aggressive setups.
    Position has advanced past 0 first so the never-advanced guard doesn't
    short-circuit."""
    # First seed with cur=10, then test stuck-at-10 with custom thresholds.
    stalled, _, _ = is_progress_stalled(
        cur_position=10.0,
        last_position=10.0,
        last_advance_at=0.0,
        is_paused=False,
        now=6.0,
        elapsed_in_inner_loop=6.0,
        grace_seconds=2.0,
        stall_seconds=5.0,
    )
    assert stalled is True


def test_progress_stalled_never_advanced_past_zero_does_not_fire() -> None:
    """v0.7.42 regression: live HLS streams in Kodi+ISA frequently keep
    ``getTime()`` pinned at 0.0 even while playing fine. The user reported
    the v0.7.41 watchdog killing model_a's live stream as a false positive
    -- she was actively watching the playback when the stall toast fired.

    The fix: only fire stall once we've seen ``last_position`` move past
    zero. A position that never advanced past 0 is indistinguishable from
    a healthy live LL-HLS where the seek window's reference time is 0.
    The proxy's reconnect-and-give-up path (5 attempts, ~10s) covers the
    genuinely-no-segments-served case separately.
    """
    stalled, last_pos, last_at = is_progress_stalled(
        cur_position=0.0,
        last_position=0.0,
        last_advance_at=100.0,
        is_paused=False,
        now=200.0,  # 100s past last_advance, far past any stall threshold
        elapsed_in_inner_loop=120.0,  # well past grace
    )
    assert stalled is False
    # State preserved so a later actual advance will start a fresh window.
    assert last_pos == 0.0
    assert last_at == 100.0


def test_progress_stalled_zero_then_advance_then_stuck_does_fire() -> None:
    """Once the position has crossed past zero (stream actually decoded
    something), a subsequent stuck-window MUST fire stall. This is the
    original ProcessMoof-corrupt-CMAF case: 17 minutes of clean playback,
    decoder freezes at position ~1020, getTime stays at 1020 forever.
    """
    stalled, _, _ = is_progress_stalled(
        cur_position=1020.0,
        last_position=1020.0,
        last_advance_at=100.0,
        is_paused=False,
        now=125.0,  # 25s of no advance, past stall=20s
        elapsed_in_inner_loop=1100.0,  # well past grace
    )
    assert stalled is True


def test_progress_stalled_tiny_nonzero_position_can_fire() -> None:
    """Boundary: position juuust past the 0.1 threshold (first decoded
    frame would be at ~0.04s for some codecs, but >0.1 is the line)
    must qualify as "we saw progress" so a subsequent freeze fires."""
    stalled, _, _ = is_progress_stalled(
        cur_position=0.5,
        last_position=0.5,
        last_advance_at=100.0,
        is_paused=False,
        now=125.0,
        elapsed_in_inner_loop=130.0,
    )
    assert stalled is True


# --------------------------------------------------------------------------- #
# v0.7.57: error-dialog watchdog (2026-06-07 husk incident)
#
# A prefetch-failed proxy served ISA a 25-byte master; Kodi popped
# "no audio/video stream can be played" (dialog 12002) while
# isPlaying() stayed True. The stall watchdog never armed (it requires
# first-advance), so the loop heartbeated under the error dialog for
# minutes. Signature: error dialog + never advanced + past grace.
# --------------------------------------------------------------------------- #


def test_error_dialog_stuck_detects_husk_signature() -> None:
    from resources.lib.tv_classify import is_error_dialog_stuck
    assert is_error_dialog_stuck(
        dialog_id=12002, has_first_advance=False, elapsed_in_inner_loop=20.0,
    ) is True


def test_error_dialog_not_stuck_before_grace() -> None:
    from resources.lib.tv_classify import is_error_dialog_stuck
    assert is_error_dialog_stuck(
        dialog_id=12002, has_first_advance=False, elapsed_in_inner_loop=10.0,
    ) is False


def test_error_dialog_ignored_after_first_advance() -> None:
    # A healthy decoding stream with some unrelated OK dialog up must
    # NOT be killed (v0.7.53 false-positive lesson).
    from resources.lib.tv_classify import is_error_dialog_stuck
    assert is_error_dialog_stuck(
        dialog_id=12002, has_first_advance=True, elapsed_in_inner_loop=300.0,
    ) is False


def test_no_dialog_is_not_stuck() -> None:
    from resources.lib.tv_classify import is_error_dialog_stuck
    assert is_error_dialog_stuck(
        dialog_id=9999, has_first_advance=False, elapsed_in_inner_loop=60.0,
    ) is False


def test_non_error_dialog_ignored() -> None:
    from resources.lib.tv_classify import is_error_dialog_stuck
    assert is_error_dialog_stuck(
        dialog_id=13003, has_first_advance=False, elapsed_in_inner_loop=60.0,
    ) is False
