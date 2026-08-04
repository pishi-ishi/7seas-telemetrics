# HANDOVER — 7seas-telemetrics

Briefing for the next agent/contributor. State as of **v0.3.0, 2026-08-04**.

## What this is

Windows desktop app (Python 3.12 + tkinter + Pillow + bundled FFmpeg via
`imageio-ffmpeg`, packaged with PyInstaller) that overlays sailing telemetry
gauges onto video and exports H.264 mp4. Owner: ruzbeh (GitHub:
**phishi-ishi**). Everything runs locally. UI design tokens follow
`docs/DESIGN.md` (dark, crimson `#DC143C` accent, Consolas for data digits).

## Architecture (all in `sevenseas/`)

| Module | Role |
|---|---|
| `telemetry.py` | GPX/CSV/VKX loading → `Telemetry` (streams dict, track, maneuvers). Epoch-seconds timeline; angles interpolated shortest-arc; speeds stored in **knots**. `trimmed()` cuts sample-exact. `detect_maneuvers()` classifies course changes: T (default 65–120°) / G (15–60°), 12 s half-window on unwrapped smoothed heading. |
| `vkx.py` | Vakaros Atlas binary parser per the official spec (github.com/vakaros/vkx). Row 0x02 gives GNSS + NED quaternion → heading/pitch/roll. 0x0A wind is **apparent** → `awd`/`aws`; `awa` derived = AWD − HDG. Race-timer events (0x04) are parsed into `tele.race_events` but **unused so far** (auto-trim idea below). Unknown row keys resync by scanning for the next 0xFF page header. |
| `gauges.py` | All gauge rendering. 1080p design space, scaled by frame height. Perf pattern: static faces drawn at 2x → cached at 1x; rotating parts (compass card, needles, dot) are prebuilt 1x sprites rotated per frame (rotation resampling = antialiasing); text is FreeType-AA at 1x. **Never draw large per-frame supersampled surfaces** — that was 70 ms/frame before this pattern (now ~16–23 ms). `HeelBar`/`DigitsBox`/`TrackMap` support per-axis stretch (`sx`/`sy`); circular gauges uniform only. `TrackMap` has `map_style` (none/street/satellite), `view_mode` (route/follow), `follow_m`. |
| `maptiles.py` | Slippy-tile mosaics: OSM street / Esri World Imagery, disk cache in `%LOCALAPPDATA%\7seas-telemetrics\tiles`, ≤140 tiles, zoom auto ≤17, darkened for overlay contrast. `service.prepare(track, style, pad)` blocking, or async with callback. Respect OSM tile policy (User-Agent set, light use only — do NOT bulk download). |
| `videoio.py` | ffmpeg probe (banner parse), frame extraction, realtime play stream (`-re`, 12 fps, rgb24 pipe), export: RGBA overlay frames piped into ffmpeg `overlay` filter at 15 fps; encoder auto-pick h264_qsv → h264_mf → libx264; audio copied with one aac-transcode retry on early mux failure. |
| `gui.py` | tkinter single-window editor. Threads do all ffmpeg/parse/tile work; results come back via `self.q` polled every 80 ms — **never touch tk from worker threads**. `tele_full` vs `tele` (trimmed view); `offset = tele.t_start + user_off`. Known tk gotcha handled in `_on_scrub`: `Scale.set()` fires its command **at idle**, so programmatic-echo values are ignored by comparing to `cur_t`. |
| `cli.py` | `--selftest` (synthetic end-to-end, logs to `%TEMP%\7seas_log.txt`), `--export` headless, GUI default. |
| `project.py` | `.7seas.json` save/load (paths, mapping, offset, trim, maneuvers, gauge layout incl. stretch + map settings). |

## Build & test

```powershell
.venv\Scripts\python main.py --selftest      # end-to-end, ~90 s
.\build.ps1                                  # → dist\7seas-telemetrics.exe (windowed)
dist\7seas-telemetrics.exe --selftest        # verify the bundle; exit code + %TEMP%\7seas_log.txt
```

Scratch test suites used during development (unit: VKX round-trip, trim,
maneuver classification, tile math; GUI smoke with hidden window incl.
playback and drag) lived in the session scratchpad — worth recreating as
`tests/` under pytest. Measured perf on the dev machine (i5-8350U + UHD620,
QSV): 1.2–2.2× realtime for 1080p30 export depending on thermals.

## Conventions & invariants

- Speeds knots; angles deg 0–360 (AWA displayed ±180, `S`=starboard green,
  `P`=port crimson). Heel: **positive = starboard = green**.
- `data_time = offset + video_time`; user-facing offset is relative to the
  (trimmed) data start.
- Gauge positions are normalized top-left (x/width, y/height); size is a
  uniform multiplier (mouse wheel), sx/sy stretch where `STRETCH = True`.
- Windowed exe: `print` is unsafe → `cli._logger` writes to the log file.
- All subprocesses need `CREATE_NO_WINDOW` (see `videoio.NOWIN`).

## Known issues / risks

1. **Concurrent sessions destroyed files twice** (v0.1 sources + the whole
   7pilots project were permanently deleted by another agent session working
   in the same folder). Backup zip: `Documents\7seas-telemetrics-v0.2.0-backup.zip`.
   Keep one session per folder; commit early.
2. VKX parser is verified against spec-synthesized files only — needs a real
   Atlas 2 log (owner has one). Heading from quaternion may be magnetic;
   declination row (0x03) is parsed but not applied.
3. Export shares gauge objects with the live preview; scrubbing during
   export could race on face caches (GIL makes it benign in practice —
   worst case duplicated work; a deep-copy of gauges at export start would
   be the clean fix).
4. Preview scrub extraction spawns one ffmpeg per frame (~0.3 s); fine, but
   a persistent seek process would feel snappier.
5. tk `Scale.set` idle-echo: see `_on_scrub`; don't "simplify" it away.
6. `7pilots-telemetrics/` sibling repo is a scaffold; sources lost (see its
   README).

## Roadmap ideas

- Auto-trim from VKX race timer (`RACE_START`/`RACE_END` events → IN/OUT).
- True wind (TWD/TWS) computed from AWA/AWS + SOG/HDG vector triangle when
  only apparent wind is logged.
- Real pytest suite from the scratch tests; CI via GitHub Actions.
- Course-up option for the follow map; wake/laylines; polar overlays.
- GitHub Release automation for the exe (dist/ is gitignored on purpose).

## Repo / publishing state

Git repo initialized at project root (branch `main`), MIT license, credits
to walkersutton/cyclemetry and Claude Code in README. Destination:
**public repo under github.com/phishi-ishi**. GitHub CLI is installed at
`C:\Program Files\GitHub CLI\gh.exe`; if auth isn't completed yet:
`gh auth login --hostname github.com --git-protocol https --web` (device
flow), then
`gh repo create 7seas-telemetrics --public --source . --push`.
