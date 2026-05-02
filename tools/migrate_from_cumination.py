#!/usr/bin/env python3
"""Migrate cumination's chaturbate user data into chaturbatetv's userdata.

Reads three files from cumination's ``addon_data`` dir:

- ``tv.json``       - same schema we use; just copy-and-validate.
- ``favorites.db``  - SQLite. Filter ``mode='chaturbate.Playvid'``,
                      strip ``[COLOR ...]`` markup from name, derive
                      slug from URL.
- ``cookies.lwp``   - LWP cookie jar. Keep only chaturbate.com.

Writes into chaturbatetv's userdata. Idempotent: re-running merges
without dups via favs_store.add and tv_store.save (which writes a
fresh JSON each time).

Usage::

    python3 tools/migrate_from_cumination.py \
        --src /storage/.kodi/userdata/addon_data/plugin.video.cumination/ \
        --dst /storage/.kodi/userdata/addon_data/plugin.video.chaturbatetv/

Pass ``--dry-run`` to print actions without writing.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from http.cookiejar import LWPCookieJar
from pathlib import Path
from urllib.parse import urlparse

# Make resources.lib importable regardless of where this script is run from.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from resources.lib import favs_store, tv_store  # noqa: E402
from resources.lib.cb_models import Favorite, Gender, TVEntry  # noqa: E402

_DEFAULT_SRC = "/storage/.kodi/userdata/addon_data/plugin.video.cumination/"
_DEFAULT_DST = "/storage/.kodi/userdata/addon_data/plugin.video.chaturbatetv/"

_COLOR_RE = re.compile(r"\[/?(?:COLOR(?:\s+[^\]]+)?|B|I)\]", re.IGNORECASE)


def strip_color_markup(text: str) -> str:
    """Remove Kodi ``[COLOR ...]`` and ``[B]`` style markup."""
    return _COLOR_RE.sub("", text).strip()


def slug_from_url(url: str) -> str:
    """Return the model slug from a chaturbate.com room URL.

    Empty string for anything that doesn't look like a CB room URL.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if "chaturbate.com" not in (parsed.netloc or ""):
        return ""
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return ""
    return parts[0]


# --------------------------------------------------------------------------- #
# Migrators
# --------------------------------------------------------------------------- #


def migrate_tv_json(src: Path, dst: Path, dry_run: bool = False) -> int:
    """Copy cumination's tv.json into our tv_store. Returns row count."""
    if not src.exists():
        return 0
    entries = tv_store.load(src)
    if dry_run:
        return len(entries)
    # Merge with existing dst (idempotent re-runs).
    existing = tv_store.load(dst)
    seen = {e.url for e in existing}
    merged = list(existing)
    for e in entries:
        if e.url in seen:
            continue
        merged.append(e)
        seen.add(e.url)
    tv_store.save(dst, merged)
    return len(entries)


def migrate_favorites_db(src: Path, dst: Path, dry_run: bool = False) -> int:
    """Convert cumination's chaturbate favorites into our favs.json."""
    if not src.exists():
        return 0
    try:
        conn = sqlite3.connect(str(src))
    except sqlite3.Error:
        return 0
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT name, url FROM favorites WHERE mode = ?",
                ("chaturbate.Playvid",),
            )
            rows = cur.fetchall()
        except sqlite3.Error:
            # Table missing, columns missing, db corrupt - skip gracefully
            # rather than crashing the whole migration. Other steps (tv.json,
            # cookies) may still be salvageable.
            return 0
    finally:
        conn.close()

    seen_slugs: set[str] = set()
    new_favs: list[Favorite] = []
    for name_raw, url_raw in rows:
        url = str(url_raw or "")
        slug = slug_from_url(url)
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        clean_name = strip_color_markup(str(name_raw or "")) or slug
        new_favs.append(Favorite(
            name=clean_name,
            slug=slug,
            url=url,
            gender=Gender.UNKNOWN,
        ))

    if dry_run:
        return len(new_favs)

    existing = favs_store.load(dst)
    merged = list(existing)
    have = {f.slug for f in merged}
    for fav in new_favs:
        if fav.slug in have:
            continue
        merged.append(fav)
        have.add(fav.slug)
    favs_store.save(dst, merged)
    return len(new_favs)


def migrate_cookies(src: Path, dst: Path, dry_run: bool = False) -> int:
    """Filter cookies.lwp to chaturbate.com cookies only."""
    if not src.exists():
        return 0
    jar = LWPCookieJar(str(src))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except OSError:
        return 0

    cb_cookies = [c for c in jar if "chaturbate.com" in (c.domain or "")]
    if dry_run:
        return len(cb_cookies)

    out_jar = LWPCookieJar(str(dst))
    for cookie in cb_cookies:
        out_jar.set_cookie(cookie)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out_jar.save(ignore_discard=True, ignore_expires=True)
    return len(cb_cookies)


# --------------------------------------------------------------------------- #
# CLI entry
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate cumination's chaturbate data to chaturbatetv.",
    )
    parser.add_argument("--src", default=_DEFAULT_SRC,
                        help="cumination addon_data dir (default: %(default)s)")
    parser.add_argument("--dst", default=_DEFAULT_DST,
                        help="chaturbatetv addon_data dir (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print actions without writing")
    args = parser.parse_args(argv)

    src = Path(args.src)
    dst = Path(args.dst)

    if not args.dry_run:
        dst.mkdir(parents=True, exist_ok=True)

    tv_count = migrate_tv_json(src / "tv.json", dst / "tv.json",
                               dry_run=args.dry_run)
    fav_count = migrate_favorites_db(src / "favorites.db", dst / "favs.json",
                                     dry_run=args.dry_run)
    cookie_count = migrate_cookies(src / "cookies.lwp", dst / "cookies.lwp",
                                   dry_run=args.dry_run)

    flag = " (dry-run)" if args.dry_run else ""
    print(f"tv.json       : {tv_count} entries{flag}")
    print(f"favorites.db  : {fav_count} chaturbate favorites{flag}")
    print(f"cookies.lwp   : {cookie_count} chaturbate cookies{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
