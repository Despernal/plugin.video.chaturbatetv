"""Cross-process advisory file lock for read-modify-write critical sections.

Kodi spawns each plugin URL invocation as a separate Python interpreter
process. ``tv_add`` running in process A and a sibling ``tv_add`` in
process B both load ``tv.json``, both compute their own new list, both
``os.replace`` the result -- the second writer wins wholesale, the
first writer's append is silently lost (race-audit pass 1, agent 3
HIGH #1 + #2).

``fcntl.flock`` is the cheapest POSIX advisory lock that lets us
serialize across processes. It's advisory (other code that doesn't
acquire the lock can still write the file), but our addon is the only
mutator of these files in production.

Usage::

    from resources.lib import file_lock

    with file_lock.locked(path):
        items = store.load(path)
        items.append(new_item)
        store.save(path, items)

The lock file is ``<path>.lock`` next to the target. We don't lock the
target file itself because (a) ``os.replace`` swaps the inode, breaking
any flock held on the original, and (b) we want readers to be
unhindered (load() is a one-shot read; we're only protecting
load+save sequences).
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# fcntl is POSIX-only. Kodi runs on Linux (LibreELEC, OSMC, distro
# packages) and macOS where fcntl is available. On Windows, importing
# fcntl raises ImportError -- we fall through to a no-op lock so
# unit tests on a Windows dev box still pass (they won't be racing
# anyway). Production on LibreELEC always has fcntl.
try:
    import fcntl as _fcntl
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX
    _HAS_FCNTL = False


def _safe_log(msg: str) -> None:
    try:
        from resources.lib import logger
        logger._log(msg)
    except Exception:
        return


@contextmanager
def locked(path: Path | str, timeout_s: float | None = None) -> Iterator[bool]:
    """Acquire an exclusive advisory lock for the duration of the block.

    Yields ``True`` when the lock is held, ``False`` when acquisition
    failed (read-only FS, fd exhaustion, missing parent dir, non-POSIX
    platform). v0.7.38 (audit pass #4 HIGH #9) added the sentinel:
    callers that care about correctness can branch on the value to
    notify the user, retry, or accept the degraded path explicitly.
    Existing call sites that ignore the value still get fail-open
    behaviour, but the silent-downgrade is now traceable.

    The lock file is ``<path>.lock`` next to ``path``. It's created on
    first acquire and intentionally never deleted -- the file's
    presence is harmless and removing it introduces a race where a
    sibling could re-create+lock between our unlink and the next open.

    ``timeout_s`` is ignored on POSIX flock (which doesn't support
    timed waits) -- the lock blocks until acquired. POSIX flock blocks
    cooperatively across processes.

    On non-POSIX platforms (Windows dev), this yields ``False`` so
    tests still run; race protection there is not required because
    Kodi addons don't ship there.
    """
    target = Path(path)
    lock_path = target.parent / (target.name + ".lock")
    if not _HAS_FCNTL:
        # Non-POSIX fallback: no-op + sentinel False so callers can
        # detect.
        yield False
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    fd: int = -1
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        try:
            yield True
        finally:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            except OSError as exc:
                _safe_log(
                    f"file_lock.locked: unlock failed path={target} "
                    f"err={exc!r}"
                )
    except OSError as exc:
        _safe_log(
            f"file_lock.locked: acquire failed path={target} err={exc!r} "
            f"(falling through unlocked -- caller gets sentinel False)"
        )
        # Fall through unlocked rather than wedge production. The
        # race we're guarding is rare; a fail-open here is preferable
        # to a wedged tv_add. Sentinel False tells the caller they
        # were degraded.
        yield False
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
