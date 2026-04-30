"""Tests for resources.lib.file_lock -- the cross-process advisory
lock used by fav_add / fav_remove / tv_add / tv_remove / tv_edit to
serialize load+save critical sections.

The cross-process correctness is exercised by
test_addon_actions.py::test_fav_add_concurrent_processes_dont_lose_writes
(uses threads as a stand-in). This file pins the small unit
contracts: sentinel return, fail-open behavior, lock-file creation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from resources.lib import file_lock


def test_locked_yields_true_on_successful_acquire(tmp_path: Path) -> None:
    """v0.7.38 (audit pass #4 HIGH #9): the context manager yields a
    bool sentinel so callers can detect the degraded fail-open path.
    Successful acquire -> True."""
    target = tmp_path / "data.json"
    with file_lock.locked(target) as got:
        assert got is True


def test_locked_creates_sibling_lock_file(tmp_path: Path) -> None:
    """The lock file is ``<path>.lock`` next to the target. v0.7.37
    didn't pin this; new test guards the path-construction shape."""
    target = tmp_path / "data.json"
    with file_lock.locked(target):
        # Lock file should exist while we hold the lock.
        lock_path = tmp_path / "data.json.lock"
        assert lock_path.exists()


def test_locked_yields_false_on_acquire_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.7.38: when os.open or flock raises (read-only FS, fd
    exhaustion, missing parent), the sentinel is False so callers
    can branch. The block still runs (fail-open) so production
    doesn't wedge; the False signals "we were not protected"."""
    target = tmp_path / "data.json"

    def boom(*_a: object, **_kw: object) -> int:
        raise OSError("simulated EROFS")

    monkeypatch.setattr(file_lock.os, "open", boom)

    saw_value: list[bool] = []
    with file_lock.locked(target) as got:
        saw_value.append(got)
        # Body still runs (fail-open) -- production isn't blocked.

    assert saw_value == [False], (
        f"sentinel must be False on acquire fail; got {saw_value!r}"
    )


def test_locked_creates_parent_dir_if_missing(tmp_path: Path) -> None:
    """The lock helper makes the parent dir if it doesn't exist
    (avoids a chicken-and-egg on first save to a fresh
    addon_data dir)."""
    target = tmp_path / "newdir" / "data.json"
    assert not target.parent.exists()
    with file_lock.locked(target) as got:
        assert got is True
        assert target.parent.exists()


def test_locked_releases_on_exception_in_block(tmp_path: Path) -> None:
    """If the with-block body raises, the lock still gets released
    (the second acquire below would block forever otherwise)."""
    target = tmp_path / "data.json"

    with pytest.raises(RuntimeError):
        with file_lock.locked(target):
            raise RuntimeError("body raised")

    # Second acquire must not deadlock.
    with file_lock.locked(target) as got:
        assert got is True
