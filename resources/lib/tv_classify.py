"""Pure classifiers for TV-loop and reconnect decisions.

These functions are the heart of the TV mode's edge-case handling. They
encode the lessons we've learned the hard way (ISA misfires, double-tap
stop, takeover detection, natural playlist end). All pure - inputs are
ints, floats, sets, strings; outputs are bool.

Every classifier here has a corresponding bug story in the 
TV history; treat them as load-bearing.
"""
from __future__ import annotations


def is_natural_playlist_end(pos: int, size: int) -> bool:
    """True when the player just finished the last item of a non-empty
    playlist (or has gone one past, which Kodi can report after a final
    advance).

    Rationale: when the user presses Next at the last item, Kodi has
    nothing to advance to and stops the player. ``onPlayBackStopped``
    fires with ``idle == 0`` and the model still live, identical to a
    real Stop press. The position-vs-size signal is what differentiates
    them: at-end means rebuild the playlist; mid-playlist means real
    user stop.
    """
    if size <= 0 or pos < 0:
        return False
    return pos >= size - 1


def classify_stop(
    at_end: bool,
    last_natural_end_time: float,
    now: float,
    threshold: float = 5.0,
) -> bool:
    """Classify an ``onPlayBackStopped`` event when at end-of-playlist.

    Returns True if the stop should be treated as a natural end (fall
    through to rebuild). Returns False if it should be treated as a
    real user stop (the standard idle/live check applies).

    Mid-playlist stops are always real user stops (False).
    Any at-end stop is a natural end - rebuild forever (True). The
    v0.7.5-0.7.12 era had a "second at-end within 5s = exit" double-
    tap escape, removed in v0.7.13 per user directive: no double-press
    gestures, sticky-playback wins. Users exit via Stop+dialog or the
    "Stop TV mode" menu item.

    ``last_natural_end_time`` and ``threshold`` are preserved in the
    signature for backward-compat with existing callers; they're
    ignored as of v0.7.13.
    """
    if not at_end:
        return False
    return True


def is_internal_advance(pl_item_path: str | None, queued_paths: set[str]) -> bool:
    """True if the playing item is one of the playlist items we queued.

    The naive ``size > 0 and 0 <= pos < size`` check fails when the user
    plays a different item directly: Kodi REPLACES the playlist with a
    one-item one which still satisfies the check, but is no longer ours.
    By tracking the exact set of plugin URLs we put in, we get a
    reliable "is this our queue or theirs?" answer.
    """
    if not pl_item_path:
        return False
    return pl_item_path in queued_paths


def decide_after_stop(
    user_stopped: bool,
    model_live: bool,
    idle_at_stop: int = 0,
) -> bool:
    """Decide whether to keep going after playback terminated.

    Returns True to continue (reconnect / fall through to next target),
    False to exit (real user stop confirmed).

    Logic:

    - If no user-stop event fired (Ended / Error path): always continue.
    - If a stop event fired AND idle was very recent (< 3s) AND the
      model is still live: real user stop -> exit.
    - Otherwise (high idle or model offline): treat as ISA misfire or
      Kodi-internal stop -> continue.

    The idle-time check distinguishes "user just hit Stop" (idle ~ 0)
    from "Kodi self-terminated a still-running stream" (idle is whatever
    it was when the user last touched controls, often double digits).
    """
    if not user_stopped:
        return True
    if idle_at_stop < 3 and model_live:
        return False
    return True
