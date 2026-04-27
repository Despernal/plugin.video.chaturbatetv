# plugin.video.chaturbatetv

Standalone Kodi addon for Chaturbate with first-class TV mode.

See [PLANNING.md](PLANNING.md) for the full design and phase breakdown.

## Status

Phase 1: scaffolding + pure modules.

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
