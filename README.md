# 7seas-telemetrics

Desktop app for overlaying sailing telemetry (heading, SOG, heel, wind, COG,
track, maneuvers) onto video footage. Everything runs locally — no server,
no account. Output is a standard `.mp4`.

Inspired by [Cyclemetry](https://github.com/walkersutton/cyclemetry); built
for sailing data.

## Run

Double-click **`dist\7seas-telemetrics.exe`** (Windows 10/11, 64-bit).
No install needed. First launch takes a few seconds (self-extracting bundle).

## Workflow

1. **Open video** — mp4 / mov / avi / mkv, any resolution.
2. **Open GPX / CSV / VKX** — telemetry log.
   - *GPX*: position + time are read; SOG and COG are derived from GPS,
     heading falls back to COG if the file has no compass data.
   - *CSV*: columns are auto-detected by name (`timestamp`, `lat`, `lon`,
     `sog`, `heading`, `heel`/`roll`/`tilt`, `wind_dir`, `wind_speed`, …).
     Click **Map…** to correct the mapping or set speed units
     (kn / m/s / km/h / mph). Any other numeric column becomes an extra
     stream you can show as a digits readout.
   - *VKX* (Vakaros Atlas / Atlas 2): native binary logs, parsed per the
     [official spec](https://github.com/vakaros/vkx). Heading, heel and
     pitch come from the Atlas IMU quaternion; SOG/COG from GNSS; apparent
     wind (AWD/AWS) if a Calypso sensor was paired; STW / depth /
     water temperature as extra streams when present.
3. **Sync** — if the video has a `creation_time` tag, click
   **From metadata**. Otherwise scrub to a recognizable moment (a tack, a
   gybe, passing a mark), press **▶ Play** to watch gauges move over the
   footage, and nudge the offset (±0.1 s … ±1 h) until they match.
4. **Trim** — scrub to where the raced part begins and press
   **IN = cursor**; same for **OUT = cursor**. The track map, "align
   starts" reference, and maneuver detection all follow the trimmed window.
   **Clear trim** restores the full log.
5. **Maneuvers** — click **Maneuvers…** then **Auto-detect**: course
   changes are classified as tacks (**T1, T2, …**, default 65–120°) or
   gybes (**G1, G2, …**, default 15–60°) — both angle windows are
   editable. Untick false positives, ✕ to delete, or add missed ones at
   the cursor. Markers appear on the track map (in the rendered video) and
   on the timeline (click a mark to jump there).
6. **Arrange gauges** — drag them on the preview. Right-click for
   larger / smaller; the heel bar, digits boxes and track map can also be
   stretched **Wider / Narrower / Taller / Shorter** (e.g. widen the heel
   bar so a 20° heel is more dramatic). **Add readout…** shows any stream
   as digits (up to 10 gauges).
7. **RENDER VIDEO** — H.264 .mp4 next to your source video by default.
   Intel QuickSync hardware encoding is used automatically when available.

Built-in gauges: compass card (HDG), speed dial (SOG), heel bar
(green = starboard, crimson = port), wind rose (TWD/AWD), course rose
(COG), mini track map with maneuver pointers, digits box (any stream).

**Save project…** stores file paths, column mapping, sync offset, trim,
maneuvers and gauge layout in a `.7seas.json` you can reload later.

## Command line

```
7seas-telemetrics.exe --selftest            # end-to-end synthetic test
7seas-telemetrics.exe --export --video V.mp4 --data log.vkx
                      [--offset start|meta|SECONDS] [--out OUT.mp4]
                      [--project saved.7seas.json]
```

The app is windowed, so CLI runs log to `%TEMP%\7seas_log.txt`.

## Performance

Measured on an i5-8350U + UHD 620: 1080p30 export runs at ~2.2x realtime
with QuickSync — a 60-minute video takes ≈ 28 minutes. Software x264
fallback is ~1.4x realtime. RAM use is modest (< 1 GB); overlay frames are
piped, never written to disk.

## Developing

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py            # run the GUI from source
.venv\Scripts\python main.py --selftest # verify the pipeline
.\build.ps1                             # rebuild dist\7seas-telemetrics.exe
```

Architecture: `sevenseas/telemetry.py` (GPX/CSV parsing, trimming, maneuver
detection), `sevenseas/vkx.py` (Vakaros binary parser),
`sevenseas/gauges.py` (Pillow gauge rendering), `sevenseas/videoio.py`
(ffmpeg probe/preview/play/export — overlay frames are piped into ffmpeg's
`overlay` filter), `sevenseas/gui.py` (tkinter editor), `sevenseas/cli.py`.
FFmpeg comes from the `imageio-ffmpeg` wheel and is bundled into the exe.
