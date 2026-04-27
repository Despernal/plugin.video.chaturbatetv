"""Pure selection logic for TV mode.

Picks a target out of a TV priority list, builds the playlist tier
around that target, and walks the list for first-live discovery.

All functions take an ``is_live_func`` callable so the network is
injected; tests pass a deterministic stub. Same trick for
``should_continue`` (cooperative cancellation from the TV outer loop).
"""
from __future__ import annotations

import random
from collections.abc import Callable

from resources.lib.cb_models import TVEntry


_LiveFn = Callable[[str], bool]
_ContinueFn = Callable[[], bool]


def priority_sort(entries: list[TVEntry]) -> list[TVEntry]:
    """Sort desc by priority; ties broken by ``name`` ascending. Pure -
    returns a fresh list, does not mutate the input.
    """
    return sorted(entries, key=lambda e: (-e.priority, e.name))


def pick_target(
    entries: list[TVEntry],
    is_live_func: _LiveFn,
    min_priority: int | None = None,
    should_continue: _ContinueFn | None = None,
) -> TVEntry | None:
    """Pick a random model in the highest live tier of ``entries``.

    Walks ``entries`` in priority order (caller is expected to have
    sorted with ``priority_sort`` already, but we tolerate unsorted
    input by walking until we find ANY live tier; mixing tiers will
    break "highest" semantics, so don't do that). On the first live
    entry found, captures its priority as the target tier and collects
    every other live entry in the same tier; returns
    ``random.choice`` of that pool.

    ``min_priority``: if set, only entries with ``priority > min_priority``
    are eligible. Used by the promotion poll to enforce upgrade-only.

    ``should_continue``: returns False to abort the walk. Returns None
    if abort fires before a candidate is found.
    """
    candidates: list[TVEntry] = []
    highest_p: int | None = None
    for entry in entries:
        if should_continue is not None and not should_continue():
            return None
        if min_priority is not None and entry.priority <= min_priority:
            continue
        if highest_p is not None and entry.priority < highest_p:
            break
        if is_live_func(entry.url):
            if highest_p is None:
                highest_p = entry.priority
            candidates.append(entry)
    if not candidates:
        return None
    return random.choice(candidates)


def collect_live_tier(
    entries: list[TVEntry],
    target: TVEntry,
    is_live_func: _LiveFn,
) -> list[TVEntry]:
    """Build the playlist member list around ``target``.

    Returns ``[target] + shuffled(other live entries at target's priority)``.
    Used to populate ``xbmc.PlayList`` so the player's native Next
    button cycles within the same priority tier.

    The caller is responsible for ensuring ``target`` is itself a live
    entry; we put it at position zero unconditionally so a "single
    member" tier (target alone alive) still plays.
    """
    others: list[TVEntry] = []
    for entry in entries:
        if entry.priority != target.priority:
            continue
        if entry.url == target.url:
            continue
        if is_live_func(entry.url):
            others.append(entry)
    random.shuffle(others)
    return [target, *others]


def walk_live(
    entries: list[TVEntry],
    is_live_func: _LiveFn,
    min_priority: int | None = None,
    should_continue: _ContinueFn | None = None,
) -> TVEntry | None:
    """Return the first live entry in input order. Kept as a utility.

    The TV main loop uses ``pick_target`` instead (random tier pick
    avoids hammering the same model every cycle), but ``walk_live`` is
    handy for ad-hoc utilities and the screensaver's periodic re-scan.
    """
    for entry in entries:
        if should_continue is not None and not should_continue():
            return None
        if min_priority is not None and entry.priority <= min_priority:
            continue
        if is_live_func(entry.url):
            return entry
    return None
