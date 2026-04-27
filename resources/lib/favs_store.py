"""Local favorites storage.

Schema on disk::

    {
      "favorites": [
        {"slug": "alice", "name": "alice", "url": "...", "gender": "female"},
        ...
      ]
    }

Same atomic-write pattern as ``tv_store``. ``add``/``remove`` are pure
list transforms (no I/O); the caller decides when to flush.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from resources.lib.cb_models import Favorite, Gender


def _row_to_fav(row: Any) -> Favorite | None:
    if not isinstance(row, dict):
        return None
    slug = row.get("slug")
    name = row.get("name")
    url = row.get("url")
    if not isinstance(slug, str) or not slug:
        return None
    if not isinstance(name, str):
        return None
    if not isinstance(url, str):
        return None
    gender = Gender.from_str(row.get("gender"))
    return Favorite(name=name, url=url, slug=slug, gender=gender)


def _safe_log(msg: str) -> None:
    """Log helper that tolerates a logger import failure (pure-test paths)."""
    try:
        from resources.lib import logger
        logger._log(msg)
    except Exception:
        return


def load(path: Path) -> list[Favorite]:
    """Read ``path`` and return the favorites list. Returns ``[]`` on any
    failure (missing, malformed, schema mismatch).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as exc:
        _safe_log(f"favs_store.load: read fail path={path} err={exc!r}")
        return []
    if not text.strip():
        _safe_log(f"favs_store.load: empty path={path}")
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _safe_log(f"favs_store.load: JSON decode fail path={path} err={exc!r}")
        return []
    if not isinstance(data, dict):
        _safe_log(f"favs_store.load: payload not dict path={path}")
        return []
    rows = data.get("favorites")
    if not isinstance(rows, list):
        _safe_log(f"favs_store.load: 'favorites' not list path={path}")
        return []
    out: list[Favorite] = []
    for row in rows:
        fav = _row_to_fav(row)
        if fav is not None:
            out.append(fav)
    _safe_log(f"favs_store.load: path={path} entries={len(out)}")
    return out


def save(path: Path, favs: list[Favorite]) -> bool:
    """Atomic write. Returns True on success, False on any I/O failure."""
    payload = {
        "favorites": [
            {"slug": f.slug, "name": f.name, "url": f.url, "gender": f.gender.value}
            for f in favs
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, NotADirectoryError) as exc:
        _safe_log(f"favs_store.save: mkdir fail path={path.parent} err={exc!r}")
        return False
    fd: int | None = None
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".favs-", suffix=".json", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, str(path))
        tmp_path = None
        _safe_log(f"favs_store.save: OK path={path} entries={len(favs)}")
        return True
    except OSError as exc:
        _safe_log(f"favs_store.save: I/O fail path={path} err={exc!r}")
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


def add(favs: list[Favorite], fav: Favorite) -> list[Favorite]:
    """Append ``fav`` unless a fav with the same slug already exists.
    Returns a new list (input not mutated).
    """
    if any(f.slug == fav.slug for f in favs):
        return list(favs)
    return [*favs, fav]


def remove(favs: list[Favorite], slug: str) -> list[Favorite]:
    """Filter out every fav matching ``slug``. Returns a new list."""
    return [f for f in favs if f.slug != slug]
