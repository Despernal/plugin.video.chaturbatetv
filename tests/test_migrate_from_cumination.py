"""Tests for tools/migrate_from_cumination.py.

The script:

- Reads cumination's favorites.db (sqlite), filters
  ``mode='chaturbate.Playvid'``, strips the [COLOR ...] markup from
  the name, extracts the slug from the URL, and writes via favs_store.
- Reads cookies.lwp, filters chaturbate.com cookies, saves through
  our cb_session.

We keep the script importable as a module so we can call its
functions directly. ``main(argv)`` does the CLI parsing and is the
shape exercised in the integration test.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from http.cookiejar import LWPCookieJar
from pathlib import Path

import pytest

# Add tools/ to path so the script imports.
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))


@pytest.fixture(autouse=True)
def _restore_path() -> None:
    """Ensure tools/ stays on sys.path between tests but doesn't bleed elsewhere."""
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))


def _import() -> object:
    if "migrate_from_cumination" in sys.modules:
        del sys.modules["migrate_from_cumination"]
    import migrate_from_cumination as mod  # type: ignore
    return mod


# --------------------------------------------------------------------------- #
# strip_color_markup
# --------------------------------------------------------------------------- #


def test_strip_color_markup_removes_kodi_color() -> None:
    mod = _import()
    out = mod.strip_color_markup("[COLOR deeppink]alice[/COLOR]")
    assert out == "alice"


def test_strip_color_markup_handles_nested() -> None:
    mod = _import()
    out = mod.strip_color_markup("[COLOR red]hi [B]there[/B][/COLOR]")
    assert out == "hi there"


def test_strip_color_markup_passthrough_when_no_markup() -> None:
    mod = _import()
    assert mod.strip_color_markup("plain") == "plain"


# --------------------------------------------------------------------------- #
# slug_from_url
# --------------------------------------------------------------------------- #


def test_slug_from_url_basic() -> None:
    mod = _import()
    assert mod.slug_from_url("https://chaturbate.com/alice/") == "alice"


def test_slug_from_url_with_query_string() -> None:
    mod = _import()
    assert mod.slug_from_url("https://chaturbate.com/alice/?foo=bar") == "alice"


def test_slug_from_url_invalid_returns_empty() -> None:
    mod = _import()
    assert mod.slug_from_url("not a url") == ""


# --------------------------------------------------------------------------- #
# migrate_favorites_db
# --------------------------------------------------------------------------- #


def _build_sample_db(path: Path) -> None:
    """Create a tiny sqlite db that mirrors cumination's schema."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE favorites (
            name TEXT, url TEXT, mode TEXT, image TEXT,
            duration TEXT, quality TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO favorites VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("[COLOR deeppink]alice[/COLOR]", "https://chaturbate.com/alice/",
             "chaturbate.Playvid", "img1", "", ""),
            ("[COLOR deeppink]bob[/COLOR]", "https://chaturbate.com/bob/",
             "chaturbate.Playvid", "img2", "", ""),
            ("other_site_user", "https://example.com/user/",
             "other.site.Playvid", "img3", "", ""),
            # Duplicate alice with extra whitespace - dedup target
            ("[COLOR deeppink]alice[/COLOR]", "https://chaturbate.com/alice/",
             "chaturbate.Playvid", "img1", "", ""),
        ],
    )
    conn.commit()
    conn.close()


def test_migrate_favorites_db_filters_chaturbate(tmp_path: Path) -> None:
    mod = _import()
    db_path = tmp_path / "favorites.db"
    _build_sample_db(db_path)
    favs_path = tmp_path / "dst" / "favs.json"

    count = mod.migrate_favorites_db(db_path, favs_path)

    assert count == 2  # alice + bob, dup deduped, other-site dropped
    data = json.loads(favs_path.read_text(encoding="utf-8"))
    slugs = {row["slug"] for row in data["favorites"]}
    assert slugs == {"alice", "bob"}


def test_migrate_favorites_db_strips_color_markup(tmp_path: Path) -> None:
    mod = _import()
    db_path = tmp_path / "favorites.db"
    _build_sample_db(db_path)
    favs_path = tmp_path / "dst" / "favs.json"

    mod.migrate_favorites_db(db_path, favs_path)

    data = json.loads(favs_path.read_text(encoding="utf-8"))
    names = {row["name"] for row in data["favorites"]}
    assert names == {"alice", "bob"}


def test_migrate_favorites_db_dry_run_does_not_write(tmp_path: Path) -> None:
    mod = _import()
    db_path = tmp_path / "favorites.db"
    _build_sample_db(db_path)
    favs_path = tmp_path / "dst" / "favs.json"

    count = mod.migrate_favorites_db(db_path, favs_path, dry_run=True)

    assert count == 2
    assert not favs_path.exists()


def test_migrate_favorites_db_missing_source_returns_zero(tmp_path: Path) -> None:
    mod = _import()
    db_path = tmp_path / "missing.db"
    favs_path = tmp_path / "dst" / "favs.json"
    assert mod.migrate_favorites_db(db_path, favs_path) == 0
    assert not favs_path.exists()


def test_migrate_favorites_db_missing_table_returns_zero(tmp_path: Path) -> None:
    """A real sqlite file that lacks the ``favorites`` table - e.g. a
    fresh install or a corrupt db - must return 0, not crash. Some users
    have ``favorites.db`` files left behind by uninstalled plugins; those
    files are valid sqlite but have no rows we recognize.
    """
    mod = _import()
    db_path = tmp_path / "favorites.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE unrelated_thing (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    favs_path = tmp_path / "dst" / "favs.json"
    assert mod.migrate_favorites_db(db_path, favs_path) == 0
    assert not favs_path.exists()


def test_migrate_favorites_db_wrong_columns_returns_zero(tmp_path: Path) -> None:
    """``favorites`` table exists but lacks ``name``/``url``/``mode`` columns
    ( schema drift, third-party plugin reusing the name) -
    fall back to 0, not raise.
    """
    mod = _import()
    db_path = tmp_path / "favorites.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE favorites (id INTEGER PRIMARY KEY, blob TEXT)")
        conn.commit()
    finally:
        conn.close()
    favs_path = tmp_path / "dst" / "favs.json"
    assert mod.migrate_favorites_db(db_path, favs_path) == 0
    assert not favs_path.exists()


def test_migrate_favorites_db_idempotent_merge(tmp_path: Path) -> None:
    mod = _import()
    db_path = tmp_path / "favorites.db"
    _build_sample_db(db_path)
    favs_path = tmp_path / "dst" / "favs.json"

    mod.migrate_favorites_db(db_path, favs_path)
    mod.migrate_favorites_db(db_path, favs_path)

    data = json.loads(favs_path.read_text(encoding="utf-8"))
    assert len(data["favorites"]) == 2


# --------------------------------------------------------------------------- #
# migrate_cookies
# --------------------------------------------------------------------------- #


def _build_sample_cookies(path: Path) -> None:
    jar = LWPCookieJar(str(path))
    # Build raw LWP file by hand since the writer is fussy about
    # required fields. Two cookies: one chaturbate, one bogus.
    path.write_text(
        '#LWP-Cookies-2.0\n'
        'Set-Cookie3: sessionid="abc123"; '
        'path="/"; domain=".chaturbate.com"; path_spec; domain_dot; '
        'expires="2099-01-01 00:00:00Z"; version=0\n'
        'Set-Cookie3: othercookie="def456"; '
        'path="/"; domain=".example.com"; path_spec; domain_dot; '
        'expires="2099-01-01 00:00:00Z"; version=0\n',
        encoding="utf-8",
    )
    _ = jar  # keep linter happy


def test_migrate_cookies_keeps_only_chaturbate(tmp_path: Path) -> None:
    mod = _import()
    src = tmp_path / "src" / "cookies.lwp"
    dst = tmp_path / "dst" / "cookies.lwp"
    src.parent.mkdir(parents=True)
    _build_sample_cookies(src)

    count = mod.migrate_cookies(src, dst)

    assert count == 1
    out = LWPCookieJar(str(dst))
    out.load(ignore_discard=True, ignore_expires=True)
    domains = {c.domain for c in out}
    assert any("chaturbate.com" in d for d in domains)
    assert not any("example.com" in d for d in domains)


def test_migrate_cookies_missing_source_returns_zero(tmp_path: Path) -> None:
    mod = _import()
    src = tmp_path / "missing.lwp"
    dst = tmp_path / "dst" / "cookies.lwp"
    assert mod.migrate_cookies(src, dst) == 0


def test_migrate_cookies_dry_run_does_not_write(tmp_path: Path) -> None:
    mod = _import()
    src = tmp_path / "src" / "cookies.lwp"
    dst = tmp_path / "dst" / "cookies.lwp"
    src.parent.mkdir(parents=True)
    _build_sample_cookies(src)

    count = mod.migrate_cookies(src, dst, dry_run=True)

    assert count == 1
    assert not dst.exists()


# --------------------------------------------------------------------------- #
# main (end-to-end CLI)
# --------------------------------------------------------------------------- #


def test_main_runs_full_migration(tmp_path: Path) -> None:
    mod = _import()
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    _build_sample_db(src / "favorites.db")
    _build_sample_cookies(src / "cookies.lwp")

    rc = mod.main(["--src", str(src), "--dst", str(dst)])
    assert rc == 0

    assert (dst / "favs.json").exists()
    assert (dst / "cookies.lwp").exists()


def test_main_dry_run_writes_nothing(tmp_path: Path) -> None:
    mod = _import()
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    _build_sample_db(src / "favorites.db")

    rc = mod.main(["--src", str(src), "--dst", str(dst), "--dry-run"])
    assert rc == 0
    assert not dst.exists() or list(dst.iterdir()) == []
