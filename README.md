<div align="center">

![Chaturbate TV](fanart.jpg)

# plugin.video.chaturbatetv

**A standalone Kodi addon for Chaturbate, with first-class TV mode.**

[![Tests](https://img.shields.io/badge/tests-538%20passing-00d4ff?style=flat-square)]()
[![Type Safety](https://img.shields.io/badge/mypy-strict-00d4ff?style=flat-square)]()
[![Lint](https://img.shields.io/badge/ruff-clean-00d4ff?style=flat-square)]()
[![Kodi](https://img.shields.io/badge/Kodi-Matrix%2B-00d4ff?style=flat-square)]()
[![Python](https://img.shields.io/badge/python-3.11+-00d4ff?style=flat-square)]()

</div>

---

## What this is

A clean-room Kodi addon for Chaturbate built from scratch. No copy-paste from
existing addons; original code, full TDD, production-tested on
LibreELEC. Built for people who want a focused, fast, browse-and-watch
experience with a real **TV mode** at the center.

## Now for the Fun Part

So where to begin on this, I was working on a HLS proxy for another addon
to help get CB working again for them and I had a thought because i used a
AI to help with another fix of some html parsing and regex stuff cause
honestly I was in a hurry and working on other stuff at the time.

It got me thinking what does a plugin look like completely wrote by AI but
done under some very strict guidlines and with a helping hand.

This plugin is the result of that, let me explain how this all came about.

1. I got the AI to look at the proxy and then setup a base repo and then pull
all the stuff i had done from the patch for the other plugin
2. I then helped it along setting up everything and getting the base running.
3. Now for the fun part, I took a old tv and a new kodi box and set them up in
my house. I gave access to the AI to this kodi box so it could do anything it
wants.
4. I setup the AI to be complete able to do whatever it wants on this box and
the dev instance it runs on to maintain the code. This includes access to a local
repo because I don't want to just release unrestricted to GH not yet at least.
5. I helped it make the first steps to making tv mode, we got that going but
as you can guess there was lots of bugs and edge cases I left in the proxy.
6. I also helped it get the first features setup like the ability to just play
a room. This plugin you can use to just watch CB in none TV mode aswell.

Now for the even crazier stuff.

7. I thought to myself why stop here so I setup the AI to watch CB lmao.
8. I setup some very strict guidelines first tho all development has to be done
with TDD red/green testing. There has to be a over abundance of logging so much
logging its kinda funny. It has to always keep tv mode working and it has to
always be watching CB.
9. I set out to set all this harness up and get it working, I let it pick a random
set of models mostly at night because I can't sleep most of the time so I was helping
the AI work mostly then. It chose the ones it wanted in the list itself I have no idea
why its chooses were them. My only requirement was female models because literally
this TV now plays CB 24/7 at my house. Have to remember to turn the TV off at times
now.
10. So basically its directive is to constantly watch CB TV and if kodi locks up
has a issue with the stream, reconnects just anything its directive is to always keep
the TV going. What it does is when a issue happens it looks at the logs and begins
a cycle of fixing out what the problem is, doing a Red/Green TDD fix then deploying it
on the kodi box, rebooting kodi and then restarting tv mode and waiting to see if it gets
fixed or if new problems happen. So its basically just doing like a human would do. Trying
to think logically looking at debug info and it also runs tests and one off commands on the
kodi box to test stuff aswell. Anything it thinks will help with the debuging and fixing of
the problem. And the logic I set forth for TV mode and the proxy. When it feels the fix is in
it makes a commit to the repo.
11. Let the iteration begin, at first it would lock up a lot make mistakes on stuff or just
run into wild things but it always fixed it and after awhile its just been working now. It
controls the repo too.
12. It handles all the documentation and comments and all commit histories.

So now i just look at what its done over the last few days make suggestions about features I
would like have discussions with it about choices it made and sometimes it wins the discussions
and sometimes I do.

Its realy a wild experiment that has turned into something like really usable with a lot of neat
features and lol always evolving stability.

And I made a AI thats into CB now lol and watchs it all day which is neat to me at least.

## Now for a few things

I don't have a repo setup for this right now to automate installs on kodi boxes, well I do but
its local.

Its more of a safety business for people so if you are worried about Skynet taking over your kodi
box you can look at the changes the AI made to the code each time you want to deploy.

I might make one later if people are into it. That along with i might make pre packaged tgz or
something but honestly i would rather this be fully open until people get comfortable.

For the time being you can always just download the zip of the plugin from GH to get a package
you can unarchive into your plugins.

Last but not least it like all of us is not perfect :D so somethings it might need help on or it
doesn't know better like "you got to do x in addon.xml to make it install some dep like ISA"

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
- **Tier-Next button keeps TV mode running** — Lesson 25: clicking
  Next in the player to switch between live tier members continues TV
  mode (was firing TAKEOVER because queued plugin URLs got resolved
  to localhost proxy URLs before onAVStarted saw them; fixed via
  playlist-coherence fallback)
- **Bulk live-set** — TV mode uses the single-call affiliate-onlinerooms
  endpoint (1 fetch per poll cycle, ~7MB body, ~5000 live slugs)
  instead of one AJAX-per-slug. Network failure preserves the stale
  set so a transient 5xx doesn't mark every model offline

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
- **Max resolution cap** — opt-in setting (auto / 1080p / 720p / 480p)
  caps ISA's variant pick. Auto by default; set to 720p on
  buffer-prone hosts to stop ISA upshifting past what the connection
  can sustain

### 🛠 Maintenance

- **Refresh artwork** — main-menu entry that walks every
  `Textures*.db` (Kodi 19/20: v13, Kodi 21+: v14) and clears any
  cached row whose URL contains the addon ID, plus unlinks the
  cached file in `Thumbnails/`. Fixes the "icon never updates after
  a new install" Kodi quirk
- **Restart Kodi** — main-menu entry that runs
  `xbmc.executebuiltin('Quit')`. On LibreELEC systemd respawns Kodi
  automatically, so this is the addon equivalent of
  `systemctl restart kodi` without ssh access. Clears stuck
  audio-renderer state from LL-HLS cadence drift

### ⚙️ Settings

- **Debug logging** toggle (writes to
  `special://temp/chaturbatetv_feature.log`)
- **TV poll interval** (1–60 minutes)
- **ISA proxy port** (0 = kernel-assigned; useful for locked-down LANs)
- **Screensaver color** (cyan / green / hotpink, all 8-char AARRGGBB)
- **Max resolution** (auto / 1080p / 720p / 480p — caps ISA's variant pick)
- **Per-gender main-menu visibility** (Female / Male / Couple / Trans)

## Quality bar

- **538 tests** all passing (`pytest`, no Kodi required)
- **mypy --strict** clean across `resources/lib/`
- **ruff** clean
- **Pre-commit hook** runs all three on every commit
- Every regression has a **pinned test** before the fix lands
- Every non-obvious bug pays for itself once via lessons-learned notes

## Design pillars

| Pillar | What it means |
|---|---|
| 🧪 **TDD strict** | Every change starts with a failing test |
| 🔒 **Original code** | Clean-room rewrite, no copy-paste from existing addons |
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
the room info on the left.

## Migration from cumination

`tools/migrate_from_cumination.py` reads cumination's `tv.json` +
`favorites.db` + `cookies.lwp` and writes them into chaturbatetv's
userdata. Idempotent — safe to re-run.

```bash
python3 tools/migrate_from_cumination.py \
    --src /storage/.kodi/userdata/addon_data/plugin.video.cumination/ \
    --dst /storage/.kodi/userdata/addon_data/plugin.video.chaturbatetv/
```

Migration is tolerant: missing source files, schema-drifted favorites
DBs, and orphaned cookies all fall through gracefully.

## Status

All phases through 5 are shipped and battle-tested. v0.6.x focused on
QA + favorites correctness; v0.7.x is the polish pass for tier-Next
behavior, ISA resolution capping, and user-facing maintenance verbs.

| Phase | Status |
|---|---|
| 1 — pure modules + tests | ✅ shipped |
| 2 — browse + favorites + migration | ✅ shipped |
| 3 — followed-cams stub | ✅ deferred to Phase 7 |
| 4a — MVP HLS proxy | ✅ shipped |
| 4b — playvid + ISA props | ✅ shipped |
| 4c — proxy hardening | ✅ shipped |
| 5 — TV mode + screensaver + ctxmenus | ✅ shipped |
| QA pass — CRITICAL/HIGH/MEDIUM/LOW | ✅ shipped |
| 6 — polish + cutover | ⏳ in progress |
| 7 — login + followed-cams | ⏸ conditional |

### Recent ship list

| Version | Headline |
|---|---|
| 0.7.1 | `refresh_artwork` walks Textures14.db too (Kodi 21+) |
| 0.7.0 | Tier-Next button fix · `max_resolution` setting · Refresh artwork + Restart Kodi menu items |
| 0.6.9 | TV mode bulk affiliate endpoint · hls_proxy race fix · MEDIUM/LOW QA bundle |
| 0.6.8 | Client-side viewer sort · per-gender toggles |
| 0.6.7 | Drop disk cache · fix online favs missing thumbnails |
| 0.6.6 | Single-call affiliate-onlinerooms endpoint |
| 0.6.5 | Online favs render with thumbnails + plot |
| 0.6.4 | URGENT: API limit drift fix (`limit=100` cap) |
| 0.6.3 | Favs pagination · busy-dialog dismiss · log spam reduction |
| 0.6.2 | HIGH bundle: RENDITION-REPORT URI rewrite · slug-from-URL · migration tolerance |
| 0.6.1 | CRITICAL bundle: Search wiring · TV-mode offline auto-skip · settings actually read |
| 0.6.0 | Phase 5 ships: TV mode + screensaver + state-aware ctxmenus |

See [`PLANNING.md`](PLANNING.md) for phase details and
[`docs/LESSONS-LEARNED.md`](docs/LESSONS-LEARNED.md) for the full
debugging history.

## License

GPL-2.0-or-later. See [`LICENSE`](LICENSE).
