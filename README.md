# plugin.video.chaturbatetv

Standalone Kodi addon for Chaturbate with first-class TV mode.

See [PLANNING.md](PLANNING.md) for the full design and phase breakdown.
See [docs/LESSONS-LEARNED.md](docs/LESSONS-LEARNED.md) for every non-obvious
bug we've paid for and how to avoid paying for it twice.

## Status

**v0.6.4 (2026-04-27)** - deployed and stable on . All phases through
5 are shipped: pure modules, browse views with JSON API, local favorites
with paginated Online/Offline split + 30-min disk cache, HLS proxy
(master + chunklist + segment + RENDITION-REPORT URI rewriting), playvid
resolver with Matrix+ ISA props, TV mode with priority list / random
tier pick / state-aware ctxmenus / idle-time disambiguator / offline
auto-skip / idle screensaver. 513 tests passing, ruff clean,
mypy --strict clean.

Login (Phase 7) is conditionally deferred. TV mode as a Kodi service
addon (instead of a plugin invocation) is a Phase 6 candidate for
suppressing the "addon busy" spinner during long-running playback.

## Layout

```
addon.xml                 Kodi manifest
resources/
  lib/                    All Python (pure modules + Kodi-shaped modules)
  language/               strings.po
  media/                  icons, screensaver assets
tests/                    pytest test suite
  stubs/                  type stubs for xbmc* modules (for mypy --strict)
  fixtures/               sample HTML / JSON for parser tests
  kodi_mock/              Mock Kodi runtime for offline TV-loop tests
tools/                    standalone CLIs (tv-edit.py etc.)
```

## Dev workflow

```
uv pip install --python .venv/bin/python pytest pytest-randomly beautifulsoup4 mypy ruff
.venv/bin/python -m pytest -v
.venv/bin/python -m ruff check resources/ tests/
MYPYPATH=tests/stubs .venv/bin/python -m mypy --strict resources/lib/
```

The `.git/hooks/pre-commit` hook runs all three on every commit.

## View modes

For best UX, set the view mode to InfoWall or MediaList in Kodi's
view-selector when browsing - puts thumbnails on the right and the
room info on the left, like .
