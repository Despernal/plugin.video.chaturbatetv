"""Tests for resources.lib.tv_select - priority sort + tier picker.

Ported (rewritten, with TVEntry dataclasses) from the  snippet
tests for _cb_tv_priority_sort, _cb_tv_pick_target, _cb_tv_collect_live_tier,
_cb_tv_walk_live.
"""
from __future__ import annotations

from collections.abc import Callable

from resources.lib.cb_models import TVEntry
from resources.lib.tv_select import (
    collect_live_tier,
    pick_target,
    priority_sort,
    walk_live,
)


def _entries(*specs: tuple[str, int]) -> list[TVEntry]:
    """Helper: build TVEntry list from (name, priority) pairs.
    URL is set to the name for easy is_live_func lookups."""
    return [TVEntry(name=n, url=n, priority=p) for n, p in specs]


def _live(*urls: str) -> Callable[[str], bool]:
    s = set(urls)
    return lambda u: u in s


# priority_sort ---------------------------------------------------------------

def test_priority_sort_desc_by_priority() -> None:
    es = _entries(("low", 1), ("high", 20), ("mid", 10))
    assert [e.name for e in priority_sort(es)] == ["high", "mid", "low"]


def test_priority_sort_tiebreak_by_name_asc() -> None:
    es = _entries(("zara", 10), ("alice", 10), ("mike", 10))
    assert [e.name for e in priority_sort(es)] == ["alice", "mike", "zara"]


def test_priority_sort_empty_list() -> None:
    assert priority_sort([]) == []


def test_priority_sort_single_item() -> None:
    es = _entries(("solo", 5))
    assert priority_sort(es) == es


def test_priority_sort_returns_new_list() -> None:
    es = _entries(("b", 1), ("a", 2))
    out = priority_sort(es)
    assert out is not es
    assert [e.name for e in es] == ["b", "a"]  # input not mutated


def test_priority_sort_stable_within_tier() -> None:
    """Equal priority + equal name preserves input order (stable sort)."""
    e1 = TVEntry(name="x", url="u1", priority=10)
    e2 = TVEntry(name="x", url="u2", priority=10)
    out = priority_sort([e1, e2])
    assert out == [e1, e2]


# pick_target -----------------------------------------------------------------

def test_pick_target_returns_none_for_empty() -> None:
    assert pick_target([], lambda u: True) is None


def test_pick_target_returns_none_when_nothing_live() -> None:
    es = _entries(("a", 10), ("b", 5))
    assert pick_target(es, lambda u: False) is None


def test_pick_target_random_within_top_live_tier() -> None:
    """All P10 live -> random.choice across all of them, every name reachable."""
    es = priority_sort(_entries(("alice", 10), ("bob", 10), ("carol", 10)))
    is_live = lambda u: True  # noqa: E731
    seen: set[str] = set()
    for _ in range(200):
        chosen = pick_target(es, is_live)
        assert chosen is not None
        seen.add(chosen.name)
    assert seen == {"alice", "bob", "carol"}


def test_pick_target_picks_only_from_highest_live_tier() -> None:
    es = priority_sort(_entries(("high", 15), ("mid_a", 10), ("mid_b", 10)))
    is_live = lambda u: True  # noqa: E731
    for _ in range(50):
        chosen = pick_target(es, is_live)
        assert chosen is not None
        assert chosen.priority == 15


def test_pick_target_falls_through_offline_higher_tier() -> None:
    es = priority_sort(_entries(("high", 15), ("mid_a", 10), ("mid_b", 10)))
    is_live = _live("mid_a", "mid_b")  # high offline
    seen: set[str] = set()
    for _ in range(100):
        chosen = pick_target(es, is_live)
        assert chosen is not None
        seen.add(chosen.name)
    assert seen == {"mid_a", "mid_b"}


def test_pick_target_min_priority_excludes_equal_and_lower() -> None:
    """Promotion poll semantics: only swap STRICTLY upward."""
    es = priority_sort(_entries(("p10a", 10), ("p10b", 10), ("p5", 5)))
    assert pick_target(es, lambda u: True, min_priority=10) is None


def test_pick_target_min_priority_includes_strictly_higher() -> None:
    es = priority_sort(_entries(("p11", 11), ("p10", 10)))
    chosen = pick_target(es, lambda u: True, min_priority=10)
    assert chosen is not None
    assert chosen.name == "p11"


def test_pick_target_should_continue_aborts() -> None:
    es = _entries(("x", 10))
    assert pick_target(es, lambda u: True, should_continue=lambda: False) is None


def test_pick_target_should_continue_called_per_iteration() -> None:
    """Cooperative cancellation between rows."""
    es = priority_sort(_entries(("a", 5), ("b", 4), ("c", 3)))
    calls: list[int] = []

    def sc() -> bool:
        calls.append(1)
        return True

    pick_target(es, lambda u: False, should_continue=sc)
    # Walked through all 3 rows -> at least 3 should_continue calls.
    assert len(calls) >= 3


def test_pick_target_min_priority_zero_includes_priority_one() -> None:
    """Edge: min_priority=0 means strictly > 0, so p1 qualifies."""
    es = _entries(("p1", 1))
    chosen = pick_target(es, lambda u: True, min_priority=0)
    assert chosen is not None
    assert chosen.name == "p1"


# collect_live_tier -----------------------------------------------------------

def test_collect_live_tier_only_target_tier() -> None:
    """All members at target's priority included; other tiers excluded."""
    es = _entries(("a", 11), ("b", 11), ("c", 11), ("d", 10), ("e", 11))
    is_live = lambda u: True  # noqa: E731
    target = TVEntry(name="a", url="a", priority=11)
    tier = collect_live_tier(es, target, is_live)
    names = {e.name for e in tier}
    assert names == {"a", "b", "c", "e"}


def test_collect_live_tier_skips_offline_members() -> None:
    es = _entries(("a", 11), ("b", 11), ("c", 11))
    is_live = _live("a", "b")
    target = TVEntry(name="a", url="a", priority=11)
    tier = collect_live_tier(es, target, is_live)
    assert {e.name for e in tier} == {"a", "b"}


def test_collect_live_tier_target_at_position_zero() -> None:
    es = _entries(("a", 11), ("b", 11), ("c", 11))
    target = TVEntry(name="b", url="b", priority=11)
    tier = collect_live_tier(es, target, lambda u: True)
    assert tier[0].name == "b"
    assert {e.name for e in tier[1:]} == {"a", "c"}


def test_collect_live_tier_only_target_alive() -> None:
    es = _entries(("a", 11), ("b", 11))
    is_live = _live("a")
    target = TVEntry(name="a", url="a", priority=11)
    tier = collect_live_tier(es, target, is_live)
    assert len(tier) == 1
    assert tier[0].name == "a"


def test_collect_live_tier_others_shuffled() -> None:
    es = _entries(*[(str(i), 11) for i in range(5)])
    target = es[0]
    is_live = lambda u: True  # noqa: E731
    seen_at_pos1: set[str] = set()
    for _ in range(100):
        tier = collect_live_tier(es, target, is_live)
        seen_at_pos1.add(tier[1].name)
    assert seen_at_pos1 == {"1", "2", "3", "4"}


def test_collect_live_tier_empty_models() -> None:
    target = TVEntry(name="a", url="a", priority=10)
    assert collect_live_tier([], target, lambda u: True) == [target]


# walk_live -------------------------------------------------------------------

def test_walk_live_returns_first_live() -> None:
    es = priority_sort(_entries(("alice", 10), ("bob", 5), ("carol", 3)))
    chosen = walk_live(es, _live("bob"))
    assert chosen is not None
    assert chosen.name == "bob"


def test_walk_live_none_when_nothing_live() -> None:
    es = _entries(("x", 1))
    assert walk_live(es, lambda u: False) is None


def test_walk_live_respects_min_priority() -> None:
    """min_priority=10 -> only entries with priority > 10 eligible."""
    es = priority_sort(_entries(("a", 10), ("b", 15), ("c", 5)))
    chosen = walk_live(es, lambda u: True, min_priority=10)
    assert chosen is not None
    assert chosen.name == "b"


def test_walk_live_aborts_on_should_continue_false() -> None:
    es = _entries(("a", 1), ("b", 2), ("c", 3))
    assert walk_live(es, lambda u: True, should_continue=lambda: False) is None


def test_walk_live_empty_models() -> None:
    assert walk_live([], lambda u: True) is None


def test_walk_live_walks_in_input_order() -> None:
    """walk_live does not sort; it scans in input order."""
    es = [
        TVEntry(name="last", url="last", priority=1),
        TVEntry(name="first", url="first", priority=10),
    ]
    chosen = walk_live(es, lambda u: True)
    assert chosen is not None
    assert chosen.name == "last"
