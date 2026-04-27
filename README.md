<div align="center">

![Chaturbate TV](fanart.jpg)

# plugin.video.chaturbatetv

**A standalone Kodi addon for Chaturbate, with first-class TV mode.**

[![Tests](https://img.shields.io/badge/tests-524%20passing-00d4ff?style=flat-square)]()
[![Type Safety](https://img.shields.io/badge/mypy-strict-00d4ff?style=flat-square)]()
[![Lint](https://img.shields.io/badge/ruff-clean-00d4ff?style=flat-square)]()
[![Kodi](https://img.shields.io/badge/Kodi-Matrix%2B-00d4ff?style=flat-square)]()
[![Python](https://img.shields.io/badge/python-3.11+-00d4ff?style=flat-square)]()

</div>

---

## What this is

A clean-room Kodi addon for Chaturbate built from scratch. No copy-paste from
existing addons; original code, full TDD, production-tested on
LibreELEC. Designed to replace 's chaturbate handler for
people who want a focused, fast, browse-and-watch experience with a
real **TV mode** at the center.

## Features

### 📺 TV Mode (the centerpiece)

Build a **priority list** of your favorite models. Hit "Play TV" and the
loop walks the list, picks a live target at the highest priority tier,
plays it, and promotes to a higher tier when one comes online.

- **Priority-tier playback** — multi-member tiers play as a randomized
  playlist so the same model doesn't dominate
- **Idle screensaver** when nothing's live — bouncing HALO-cyan label
  on a fullscreen black background, periodically re-walks the list
- **Takeover detection** — manually playing something else releases
  the loop cleanly
- **ISA-misfire fallback** — Lesson v2: when ISA fires Stopped on a
  still-online stream, we re-check liveness and fall through instead
  of exiting the loop
- **Offline auto-skip** — Lesson v6.1: when a tier member goes offline
  mid-playlist, fire `Action(Next)` so the player advances
- **State-reset between iterations** — Lesson 10: every long-lived
  player property is reset between tier-rebuilds so a stop event in
  iter N doesn't poison iter N+1

### 🌐 Browse + Search + Favorites

- **Browse modes**: Top Cams · New Cams · Female · Male · Couple · Trans · Search
- **Per-gender toggles** in settings to hide categories you don't want
- **Local favorites** with paginated Online / Offline split (50/page)
- **State-aware ctxmenu** — every row's right-click shows the right
  set of actions: Add to TV / In TV / Edit / Remove · Add to / Remove
  from Favorites
- **Online favorites render with thumbnails + plot + viewer count**
  (single-call affiliate-onlinerooms endpoint, sub-second response)
- **Offline favorites stay bare** by design — we never poll 1000+
  slugs to fetch stale metadata
- **30-second in-memory cache** so paging within a session is instant

### 🎬 Playback

- **Localhost HLS rewriting proxy** — every layer (master, chunklist,
  segments, even LL-HLS RENDITION-REPORT URIs) routes through
  `127.0.0.1` so ISA always uses our headers
- **Three-tier segment fallback** — current URL → cached CDN URL →
  latest known segment URL — keeps the buffer warm during edge rotation
- **Reconnect watchdog** with cached chunklists during refresh
- **Single-use JWT redaction** in logs so `?token=...` never persists
  to disk
- **Matrix+ ISA properties** — the `inputstream` key, NOT the legacy
  `inputstreamaddon` (silently broken on Nexus+, would just look like
  playback was off)
- **Gzip-aware fetcher** + magic-byte fallback for misconfigured edges

### ⚙️ Settings

- **Debug logging** toggle (writes to
  `special://temp/chaturbatetv_feature.log`)
- **TV poll interval** (1–60 minutes)
- **ISA proxy port** (0 = kernel-assigned; useful for locked-down LANs)
- **Screensaver color** (cyan / green / hotpink, all 8-char AARRGGBB)
- **Per-gender main-menu visibility** (Female / Male / Couple / Trans)

## Quality bar

- **524 tests** all passing (`pytest`, no Kodi required)
- **mypy --strict** clean across `resources/lib/`
- **ruff** clean
- **Pre-commit hook** runs all three on every commit
- Every regression has a **pinned test** before the fix lands
- Every non-obvious bug pays for itself once via
  [`docs/LESSONS-LEARNED.md`](docs/LESSONS-LEARNED.md) — 24 lessons and
  counting

## Design pillars

| Pillar | What it means |
|---|---|
| 🧪 **TDD strict** | Every change starts with a failing test |
| 🔒 **Original code** | No copy-paste from ; clean-room rewrite |
| 📊 **Gated logging** | `_log` everywhere, off by default, redacts secrets |
| 🌐 **JSON over HTML** | All listings via the JSON API; no HTML scraping |
| 🚦 **Boundary tolerance** | Every parser tolerates malformed input |
| 🔌 **Inject the network** | Tests hand in `fetch_func`; no urllib hooks |
| ⚡ **Single-call where possible** | Affiliate-onlinerooms beats paginated walks |

## Layout

```
addon.xml                 Kodi manifest (single source of truth for version)
default.py                Plugin entry point
resources/
  lib/                    All Python modules (pure + Kodi-shaped)
  language/               strings.po
  media/                  icons, screensaver assets
  settings.xml            User-facing settings declarations
docs/
  LESSONS-LEARNED.md      Every non-obvious bug and how to avoid it twice
tests/                    pytest test suite
  fixtures/               sample JSON for parser tests
  kodi_mock/              Mock Kodi runtime for offline TV-loop tests
  stubs/                  type stubs for xbmc* modules (mypy --strict)
tools/                    standalone CLIs (icon-design pipeline, migration)
PLANNING.md               Phased delivery plan + open issues
```

## Module map

| Module | Responsibility |
|---|---|
| `default.py` | Entry point — dispatches via `router` |
| `resources.lib.router` | Mode dispatch from `sys.argv` |
| `resources.lib.cb_endpoints` | URL builders (room-list, affiliate-onlinerooms, AJAX status) |
| `resources.lib.cb_client` | HTTP client (iPad UA, injectable fetch) |
| `resources.lib.cb_listing` | Pure parser — JSON → `Model` |
| `resources.lib.cb_resolve` | slug → `Resolution` (live + HLS URL + headers) |
| `resources.lib.cb_models` | Domain types (`Model`, `Favorite`, `TVEntry`, `Gender`) |
| `resources.lib.browse_views` | Top / New / Female / Male / Couple / Trans / Search views |
| `resources.lib.favs_views` | Favorites menu + Online/Offline paginated views |
| `resources.lib.tv_loop` | TV mode outer loop + `_TVPlayer` event handler |
| `resources.lib.tv_select` | Pure tier-pick / collect-live-tier / walk-live |
| `resources.lib.tv_classify` | Pure event-classifier (ISA misfire vs user stop) |
| `resources.lib.tv_state` | Single-source-of-truth for `chaturbatetv_active` |
| `resources.lib.tv_store` | Atomic `tv.json` read/write |
| `resources.lib.favs_store` | Atomic `favs.json` read/write |
| `resources.lib.hls_proxy` | Localhost rewriting proxy (master + chunklist + segment + RENDITION-REPORT) |
| `resources.lib.playvid_resolver` | slug → ListItem with ISA props |
| `resources.lib.screensaver` | Idle screensaver Window + bouncing label |
| `resources.lib.ctxmenu` | State-aware context menu builder |
| `resources.lib.kodi_helpers` | Thin Kodi UI wrappers |
| `resources.lib.addon_settings` | Typed settings accessors with safe defaults |
| `resources.lib.addon_actions` | Side-effect verbs (fav_add/remove, tv_*, search, playvid) |
| `resources.lib.logger` | Gated `_log` to chaturbatetv_feature.log |

## Dev workflow

```bash
uv pip install --python .venv/bin/python pytest pytest-randomly mypy ruff
.venv/bin/python -m pytest -v
.venv/bin/python -m ruff check resources/ tests/
MYPYPATH=tests/stubs .venv/bin/python -m mypy --strict resources/lib/
```

The `.git/hooks/pre-commit` hook runs all three on every commit. Don't
disable it.

## View modes

For best UX, set the view mode to **InfoWall** or **MediaList** in
Kodi's view-selector when browsing — puts thumbnails on the right and
the room info on the left, matching 's layout.

## Distribution

Built as a standard Kodi addon zip. Companion repo
[`repository.flux-kodi`](https:///flux/repository.flux-kodi)
hosts the addons.xml + zips for one-click install in Kodi.

## Migration from 

`tools/migrate_from_.py` reads 's `tv.json` +
`favorites.db` + `cookies.lwp` and writes them into chaturbatetv's
userdata. Idempotent — safe to re-run.

```bash
python3 tools/migrate_from_.py \
    --src /storage/.kodi/userdata/addon_data/plugin.video./ \
    --dst /storage/.kodi/userdata/addon_data/plugin.video.chaturbatetv/
```

Migration is tolerant: missing source files, schema-drifted favorites
DBs, and orphaned cookies all fall through gracefully.

## Status

✅ **v0.6.9 deployed and stable on  (LibreELEC).**

All phases through 5 are shipped and battle-tested:

| Phase | Status |
|---|---|
| 1 — pure modules + tests | ✅ shipped |
| 2 — browse + favorites + migration | ✅ shipped |
| 3 — followed-cams stub | ✅ deferred to Phase 7 |
| 4a — MVP HLS proxy | ✅ shipped |
| 4b — playvid + ISA props | ✅ shipped |
| 4c — proxy hardening (11  lessons) | ✅ shipped |
| 5 — TV mode + screensaver + ctxmenus | ✅ shipped |
| 5.5 — gitea + nginx kodi-repo | ✅ shipped |
| QA pass — CRITICAL/HIGH/MEDIUM/LOW | ✅ shipped (24 lessons captured) |
| 6 — polish +  cutover | ⏳ in progress |
| 7 — login + followed-cams | ⏸ conditional |

See [`PLANNING.md`](PLANNING.md) for phase details and
[`docs/LESSONS-LEARNED.md`](docs/LESSONS-LEARNED.md) for the full
debugging history.

## License

GPL-2.0-or-later. See [`LICENSE`](LICENSE).
