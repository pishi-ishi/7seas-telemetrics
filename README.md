# 7seas-telemetrics

Desktop app for overlaying sailing telemetry (heading, SOG, heel, true &
apparent wind, COG, track with map background, tack/gybe pointers) onto
video footage. Everything runs locally — no server, no account. Output is
a standard `.mp4`.

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
5. **Maneuvers** — detected automatically when data loads; the
   **Auto-detect / T @ cursor / G @ cursor / Edit…** buttons manage them.
   Course changes are classified as tacks (**T1, T2, …**, default
   65–120°) or gybes (**G1, G2, …**, default 15–60°) — both angle windows
   editable in **Edit…**. Untick false positives, ✕ to delete. Markers
   appear on the track map (in the rendered video) and on the timeline
   (click a mark to jump there).
6. **Arrange gauges** — drag them on the preview; **mouse-wheel over a
   gauge to zoom it**. Right-click for larger/smaller; the heel bar,
   digits boxes and track map also stretch **Wider / Narrower / Taller /
   Shorter** (e.g. widen the heel bar so a 20° heel is more dramatic).
   **Add readout…** shows any stream as digits (up to 12 gauges).
7. **Track map** — right-click it for a **map background** (none /
   street OSM / satellite Esri — tiles fetched once, cached, attributed)
   and the **view**: whole route, or **follow-boat window** (500 m / 1 km /
   2 km) that pans with the boat. Works in the render, not just preview.
8. **Quality — HIGH / MEDIUM / LOW** — picks the output size. The line
   underneath shows the resolution, frame rate and estimated file size for
   the loaded video, so you can see the cost before rendering.

   | | Resolution | Frame rate | Typical size vs. a 300 MB / 38 min source |
   |---|---|---|---|
   | HIGH | source, up to 1080p | 30 fps | ≈ 400 MB |
   | MEDIUM *(default)* | up to 720p | 24 fps | ≈ 240 MB |
   | LOW | up to 480p | 15 fps | ≈ 85 MB |

   The encoder also holds its bitrate near the source's own — re-encoding a
   lean clip at a much higher bitrate only preserves its artifacts in
   greater fidelity.
9. **RENDER VIDEO** — H.264 .mp4 next to your source video by default.
   Intel QuickSync hardware encoding is used automatically when available.

Built-in gauges: compass card (HDG), speed dial (SOG), heel bar
(green = starboard, crimson = port), wind roses (TWD / COG), apparent
wind angle dial (AWA, port/starboard sectors), TWS & AWS readouts, track
map with maneuver pointers and map background, digits box (any stream).

**Save project…** stores file paths, column mapping, sync offset, trim,
maneuvers, gauge layout and the quality preset in a `.7seas.json` you can
reload later.

## Command line

```
7seas-telemetrics.exe --selftest            # end-to-end synthetic test
7seas-telemetrics.exe --export --video V.mp4 --data log.vkx
                      [--offset start|meta|SECONDS] [--out OUT.mp4]
                      [--quality high|medium|low] [--project saved.7seas.json]
```

The app is windowed, so CLI runs log to `%TEMP%\7seas_log.txt`.

## Performance

Measured on an i5-8350U + UHD 620 with QuickSync — lower quality settings
also render faster, because the gauges are composed at the output size:

| Quality | Speed | 60-minute video |
|---|---|---|
| HIGH (1080p30) | ~2.0x realtime | ≈ 30 min |
| MEDIUM (720p24) | ~3.4x realtime | ≈ 18 min |
| LOW (480p15) | ~5.6x realtime | ≈ 11 min |

Software x264 fallback is roughly 1.5x slower. RAM use is modest (< 1 GB);
overlay frames are piped, never written to disk.

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
`sevenseas/gauges.py` (Pillow gauge rendering), `sevenseas/maptiles.py`
(OSM/Esri tile mosaics), `sevenseas/videoio.py` (ffmpeg
probe/preview/play/export — overlay frames are piped into ffmpeg's
`overlay` filter), `sevenseas/gui.py` (tkinter editor), `sevenseas/cli.py`.
FFmpeg comes from the `imageio-ffmpeg` wheel and is bundled into the exe.
See `HANDOVER.md` for a full contributor briefing.

## Credits

- Concept inspired by **[Cyclemetry](https://github.com/walkersutton/cyclemetry)**
  by [Walker Sutton](https://github.com/walkersutton) — the original
  open-source telemetry video overlay tool for cycling.
- Designed and implemented with **[Claude Code](https://claude.com/claude-code)**
  (Anthropic Claude Fable 5).
- VKX format per the official [Vakaros spec](https://github.com/vakaros/vkx).
- Map data: © [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors; satellite imagery © [Esri](https://www.esri.com) World Imagery.
