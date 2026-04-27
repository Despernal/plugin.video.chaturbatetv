"""Atomic JSON storage for the TV priority list (``tv.json``).

Schema on disk::

    {
      "models": [
        {"url": "https://chaturbate.com/alice/", "name": "alice", "priority": 10},
        ...
      ]
    }

Writes go through ``os.replace`` so a concurrent reader either sees the
old file or the new file - never half a file. Reads tolerate the byte
window where the rename has not landed yet (returns ``[]``).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from resources.lib.cb_models import TVEntry


def _row_to_entry(row: Any) -> TVEntry | None:
    """Best-effort dict -> TVEntry. Returns None for unparseable rows."""
    if not isinstance(row, dict):
        return None
    url = row.get("url")
    name = row.get("name")
    if not isinstance(url, str) or not url:
        return None
    if not isinstance(name, str):
        return None
    raw_priority = row.get("priority", 1)
    try:
        # Reject bools (which are ints in Python) and coerce strings.
        if isinstance(raw_priority, bool):
            return None
        priority = int(raw_priority)
    except (TypeError, ValueError):
        return None
    return TVEntry(name=name, url=url, priority=priority)


def _safe_log(msg: str) -> None:
    """Log helper that tolerates a logger import failure (pure-test paths)."""
    try:
        from resources.lib import logger
        logger._log(msg)
    except Exception:
        return


def load(path: Path) -> list[TVEntry]:
    """Read ``path`` and return the list of TVEntry rows.

    Returns ``[]`` on any failure (missing file, malformed JSON, missing
    ``models`` key, value not a list, all rows unparseable). The TV loop
    treats an empty list as "list is empty, exit cleanly".
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as exc:
        _safe_log(f"tv_store.load: read fail path={path} err={exc!r}")
        return []
    if not text.strip():
        _safe_log(f"tv_store.load: empty path={path}")
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _safe_log(f"tv_store.load: JSON decode fail path={path} err={exc!r}")
        return []
    if not isinstance(data, dict):
        _safe_log(f"tv_store.load: payload not dict path={path}")
        return []
    rows = data.get("models")
    if not isinstance(rows, list):
        _safe_log(f"tv_store.load: 'models' not list path={path}")
        return []
    out: list[TVEntry] = []
    for row in rows:
        entry = _row_to_entry(row)
        if entry is not None:
            out.append(entry)
    _safe_log(f"tv_store.load: path={path} entries={len(out)}")
    return out


def save(path: Path, entries: list[TVEntry]) -> bool:
    """Atomic write of ``entries`` as ``{"models": [...]}`` JSON.

    Creates the parent dir if missing. Returns True on success, False on
    any I/O failure (parent dir cannot be created, write fails, replace
    fails). Never raises - the TV loop calls this from inside a long-
    running player and we never want to crash it on disk hiccups.
    """
    payload = {
        "models": [
            {"url": e.url, "name": e.name, "priority": e.priority}
            for e in entries
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, NotADirectoryError) as exc:
        _safe_log(f"tv_store.save: mkdir fail path={path.parent} err={exc!r}")
        return False
    # Write to a sibling tempfile in the same directory so os.replace is
    # an atomic same-filesystem rename.
    fd: int | None = None
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".tv-", suffix=".json", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None  # ownership transferred to the file object
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, str(path))
        tmp_path = None
        _safe_log(f"tv_store.save: OK path={path} entries={len(entries)}")
        return True
    except OSError as exc:
        _safe_log(f"tv_store.save: I/O fail path={path} err={exc!r}")
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
