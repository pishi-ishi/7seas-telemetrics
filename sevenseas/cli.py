"""Command line entry: GUI by default, plus headless --export and --selftest."""

import argparse
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone


def _logger(logfile):
    f = open(logfile, "a", encoding="utf-8")

    def log(msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        f.write(line + "\n")
        f.flush()
        if sys.stdout is not None:  # absent in --windowed builds
            try:
                print(line, flush=True)
            except OSError:
                pass
    return log


def make_synthetic_csv(path, t0, seconds, hz=1):
    """Synthetic sailing log: circular course with plausible instrument data."""
    rows = ["timestamp,lat,lon,heading_deg,sog_kn,heel_deg,wind_dir,wind_speed"]
    n = int(seconds * hz)
    for i in range(n + 1):
        t = t0 + i / hz
        th = (i / n) * 2 * math.pi * 1.25
        lat = 37.8100 + 0.0050 * math.sin(th)
        lon = -122.4400 + 0.0063 * math.cos(th)
        hdg = (math.degrees(th) + 90.0) % 360.0
        sog = 6.5 + 2.8 * math.sin(i / 17.0)
        heel = 14.0 * math.sin(i / 9.0)
        twd = (225.0 + 18.0 * math.sin(i / 31.0)) % 360.0
        tws = 14.0 + 4.0 * math.sin(i / 23.0)
        iso = datetime.fromtimestamp(t, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        rows.append(f"{iso},{lat:.6f},{lon:.6f},{hdg:.1f},{sog:.2f},"
                    f"{heel:.2f},{twd:.1f},{tws:.2f}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(rows))


def resolve_offset(spec, tele, info):
    """'start' | 'meta' | float-string -> data epoch at video t=0."""
    if spec in (None, "start"):
        return tele.t_start
    if spec == "meta":
        if info.creation_time is None:
            raise SystemExit("Video has no creation_time metadata; "
                             "use --offset start or a number")
        return info.creation_time
    return tele.t_start + float(spec)


def cmd_export(args, log):
    from . import gauges, telemetry, videoio
    from .project import load_project

    doc = load_project(args.project) if args.project else None
    video = args.video or (doc and doc.get("video"))
    data = args.data or (doc and doc.get("data"))
    if not video or not data:
        raise SystemExit("--export needs --video and --data (or --project)")
    log(f"probing {video}")
    info = videoio.probe(video)
    log(f"  {info}")
    log(f"loading {data}")
    mapping = doc.get("mapping") if doc else None
    units = doc.get("speed_units") if doc else None
    tele = telemetry.load(data, mapping=mapping, speed_units=units)
    if doc:
        trim = doc.get("trim") or [None, None]
        if trim[0] is not None or trim[1] is not None:
            tele = tele.trimmed(trim[0], trim[1])
            log(f"  trimmed to {trim}")
        tele.maneuvers = doc.get("maneuvers") or []
    log(f"  streams: {', '.join(sorted(tele.streams)) or '(none)'}"
        + (", track" if tele.track else ""))
    if doc and doc.get("gauge_objects"):
        glist = doc["gauge_objects"]
        for g in glist:
            g.enabled = g.enabled and g.available(tele)
    else:
        glist = gauges.default_gauges(tele)
    if doc and args.offset is None:
        offset = tele.t_start + float(doc.get("user_offset", 0.0))
    else:
        offset = resolve_offset(args.offset, tele, info)
    out = args.out or videoio.default_output_path(video)
    enc = videoio.pick_encoder()
    log(f"encoder: {enc}; output: {out}")
    t0 = time.time()
    last = [0.0]

    def prog(done, total):
        now = time.time()
        if now - last[0] >= 5.0:
            last[0] = now
            pct = 100.0 * done / total
            speed = (done / args.overlay_fps) / max(1e-9, now - t0)
            log(f"  {pct:5.1f}%  ({speed:.2f}x realtime)")

    elapsed = videoio.export(info, tele, glist, offset, out,
                             overlay_fps=args.overlay_fps, encoder=enc,
                             progress=prog)
    log(f"done in {elapsed:.1f}s ({info.duration / elapsed:.2f}x realtime): {out}")
    return 0


def cmd_selftest(args, log):
    from . import gauges, telemetry, videoio

    log("=== 7seas-telemetrics selftest ===")
    work = tempfile.mkdtemp(prefix="7seas_selftest_")
    log(f"workdir: {work}")
    clip = os.path.join(work, "test_in.mp4")
    csv_path = os.path.join(work, "test_log.csv")
    out = os.path.join(work, "test_out.mp4")
    seconds = args.selftest_seconds

    log(f"1/5 generating {seconds}s 1080p test clip + synthetic sailing log")
    videoio.make_test_clip(clip, seconds=seconds)
    t0 = datetime.now(timezone.utc).timestamp()
    make_synthetic_csv(csv_path, t0, seconds + 30)

    log("2/5 parsing CSV (auto-mapping)")
    tele = telemetry.load(csv_path)
    need = ["heading", "sog", "roll", "twd", "tws", "cog"]
    missing = [s for s in need if not tele.has(s)]
    if missing or tele.track is None:
        log(f"FAIL: missing streams {missing}, track={tele.track is not None}")
        return 1
    log(f"   mapped: {tele.mapping}")
    tele.maneuvers = telemetry.detect_maneuvers(tele)
    log(f"   maneuvers detected: {len(tele.maneuvers)}")

    info = videoio.probe(clip)
    log(f"3/5 probed clip: {info}, creation_time="
        f"{'ok' if info.creation_time else 'missing'}")

    glist = gauges.default_gauges(tele)
    enc = videoio.pick_encoder()
    log(f"4/5 exporting with encoder {enc} ...")
    elapsed = videoio.export(info, tele, glist, tele.t_start, out,
                             overlay_fps=args.overlay_fps, encoder=enc)
    speed = info.duration / elapsed
    log(f"   export took {elapsed:.1f}s = {speed:.2f}x realtime"
        f" -> 60 min video est. {60 / speed:.0f} min")

    out_info = videoio.probe(out)
    ok_dur = abs(out_info.duration - info.duration) < 2.5
    frame = videoio.extract_frame(out, min(30.0, seconds / 2))
    frame_png = os.path.join(work, "selftest_frame.png")
    if frame is not None:
        frame.save(frame_png)
    log(f"5/5 output: {out_info}, duration_ok={ok_dur}, "
        f"frame_extracted={frame is not None}")
    log(f"   sample frame: {frame_png}")
    if not ok_dur or frame is None:
        log("FAIL")
        return 1
    if not args.keep:
        log("   (workdir kept for inspection)")
    log(f"PASS  ({speed:.2f}x realtime with {enc})")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="7seas-telemetrics",
        description="Sailing telemetry video overlay tool")
    ap.add_argument("--selftest", action="store_true",
                    help="run an end-to-end synthetic test and exit")
    ap.add_argument("--selftest-seconds", type=int, default=60)
    ap.add_argument("--export", action="store_true", help="headless export")
    ap.add_argument("--video", help="input video file")
    ap.add_argument("--data", help="input GPX, CSV, or VKX file")
    ap.add_argument("--out", help="output .mp4 path")
    ap.add_argument("--project", help=".7seas.json project file")
    ap.add_argument("--offset", default=None,
                    help="'start' (default), 'meta', or seconds into the data "
                         "at video t=0")
    ap.add_argument("--overlay-fps", type=int, default=15)
    ap.add_argument("--keep", action="store_true", help="keep selftest workdir")
    args = ap.parse_args(argv)

    logfile = os.path.join(tempfile.gettempdir(), "7seas_log.txt")
    log = _logger(logfile)

    try:
        if args.selftest:
            return cmd_selftest(args, log)
        if args.export:
            return cmd_export(args, log)
    except SystemExit:
        raise
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        return 1

    from . import gui
    gui.run()
    return 0
