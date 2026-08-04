"""FFmpeg-backed video I/O: probing, preview frames, realtime play streams,
and the export pipeline.

Export architecture: the source video is decoded by ffmpeg; Python pipes
RGBA overlay frames (at a modest overlay fps) into ffmpeg's stdin; ffmpeg
composites them with the `overlay` filter and encodes H.264 into an .mp4.
Hardware encoders (QuickSync / MediaFoundation) are auto-detected with a
software x264 fallback.

Output size is governed by `QUALITY_PRESETS` (high / medium / low), which set
the output resolution, frame rate, encoder quality and a peak bitrate cap.
The cap is also held near the *source* bitrate: re-encoding a lean 1 Mbps
clip at 6 Mbps only stores its compression artifacts in higher fidelity.
"""

import io
import math
import os
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone

from PIL import Image

from . import gauges

NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
PLAY_FPS = 12


class ExportCancelled(Exception):
    pass


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


class VideoInfo:
    def __init__(self, path, duration, width, height, fps, creation_time=None,
                 bitrate_kbps=None):
        self.path = path
        self.duration = duration
        self.width = width
        self.height = height
        self.fps = fps
        self.creation_time = creation_time  # epoch seconds or None
        self.bitrate_kbps = bitrate_kbps    # container bitrate or None

    def __repr__(self):
        return (f"VideoInfo({self.width}x{self.height} @ {self.fps:.3g} fps, "
                f"{self.duration:.1f}s)")


def probe(path):
    """Parse `ffmpeg -i` banner output. Raises ValueError on failure."""
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path],
                       capture_output=True, **NOWIN)
    text = p.stderr.decode("utf-8", "replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
    if not m:
        raise ValueError("Could not read video file (no duration found):\n"
                         + text.strip()[-400:])
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    width = height = None
    fps = 30.0
    for line in text.splitlines():
        if "Video:" in line and width is None:
            rm = re.search(r"[,\s](\d{2,5})x(\d{2,5})[\s,\[]", line + " ")
            if rm:
                width, height = int(rm.group(1)), int(rm.group(2))
            fm = re.search(r"([\d.]+)\s*fps", line) or re.search(r"([\d.]+)\s*tbr", line)
            if fm:
                try:
                    fps = float(fm.group(1))
                except ValueError:
                    pass
    if width is None:
        raise ValueError("No video stream found in file")
    # phone videos: rotation side data swaps effective w/h (ffmpeg autorotates)
    rot = re.search(r"rotation of (-?[\d.]+)", text)
    if rot and abs(abs(float(rot.group(1))) - 90) < 1:
        width, height = height, width

    creation = None
    cm = re.search(r"creation_time\s*:\s*([0-9][0-9T:\-. Z+]+)", text)
    if cm:
        from .telemetry import parse_time
        creation = parse_time(cm.group(1).strip())

    bitrate = None
    bm = re.search(r"bitrate:\s*(\d+)\s*kb/s", text)
    if bm:
        bitrate = int(bm.group(1))
    return VideoInfo(path, duration, width, height, fps, creation, bitrate)


def extract_frame(path, t, max_w=None):
    """Return one video frame at time t as a PIL RGB image, or None."""
    t = max(0.0, t)
    cmd = [ffmpeg_exe(), "-loglevel", "error", "-ss", f"{t:.3f}", "-i", path,
           "-frames:v", "1"]
    if max_w:
        cmd += ["-vf", f"scale='min({int(max_w)},iw)':-2"]
    cmd += ["-f", "image2pipe", "-vcodec", "png", "pipe:1"]
    p = subprocess.run(cmd, capture_output=True, **NOWIN)
    if p.returncode != 0 or not p.stdout:
        if t > 0.5:  # near EOF: step back and retry once
            return extract_frame(path, t - 0.5, max_w)
        return None
    try:
        return Image.open(io.BytesIO(p.stdout)).convert("RGB")
    except Exception:
        return None


# ---------------- realtime play stream (sync preview) ----------------

def play_dims(info, max_w=960):
    """Scaled even dimensions for playback."""
    sw = min(max_w, info.width)
    sh = int(round(info.height * sw / info.width / 2)) * 2
    sw = int(sw // 2) * 2
    return sw, max(2, sh)


def open_play_stream(path, t0, sw, sh, fps=PLAY_FPS):
    """Start ffmpeg decoding at realtime pace; caller reads sw*sh*3-byte
    RGB frames from proc.stdout. Kill the process to stop."""
    cmd = [ffmpeg_exe(), "-loglevel", "error",
           "-re", "-ss", f"{max(0.0, t0):.3f}", "-i", path,
           "-vf", f"scale={sw}:{sh},fps={fps}",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, **NOWIN)


# ---------------- quality presets ----------------

class Quality:
    """One output-size preset.

    max_h       output height cap (source height is never upscaled)
    fps         output frame rate cap
    overlay_fps rate at which gauge frames are composed and piped in
    crf/qsv_q   encoder quality target (higher = smaller/softer)
    bpp         bits per pixel per frame, sets the peak bitrate budget
    abr         AAC audio kbps, or None to copy the source audio stream
    """

    def __init__(self, key, label, max_h, fps, overlay_fps, crf, qsv_q, bpp,
                 abr):
        self.key = key
        self.label = label
        self.max_h = max_h
        self.fps = fps
        self.overlay_fps = overlay_fps
        self.crf = crf
        self.qsv_q = qsv_q
        self.bpp = bpp
        self.abr = abr


QUALITY_PRESETS = {
    q.key: q for q in (
        Quality("high", "HIGH", max_h=1080, fps=30, overlay_fps=15,
                crf=23, qsv_q=25, bpp=0.10, abr=None),
        Quality("medium", "MEDIUM", max_h=720, fps=24, overlay_fps=12,
                crf=26, qsv_q=29, bpp=0.08, abr=96),
        Quality("low", "LOW", max_h=480, fps=15, overlay_fps=10,
                crf=30, qsv_q=34, bpp=0.06, abr=64),
    )
}
QUALITY_DEFAULT = "medium"


def get_quality(quality):
    if isinstance(quality, Quality):
        return quality
    return QUALITY_PRESETS.get(quality or QUALITY_DEFAULT,
                               QUALITY_PRESETS[QUALITY_DEFAULT])


def target_dims(info, q):
    """Even output dimensions for a preset (never upscales)."""
    h = min(q.max_h, info.height)
    w = max(2, int(round(info.width * h / info.height / 2)) * 2)
    return w, max(2, int(h // 2) * 2)


def target_fps(info, q):
    return min(float(q.fps), info.fps if info.fps > 0 else q.fps)


def bitrate_cap(q, info):
    """Peak video bitrate in kbps — the lower of two limits:

    * a resolution/frame-rate budget (`bpp`), and
    * the source's own bitrate, scaled down for the smaller frame. A lean
      source cannot be improved by spending more bits than it was encoded
      with; the sqrt keeps that conservative, since a downscaled frame needs
      proportionally *more* bits per pixel than the original did.
    """
    w, h = target_dims(info, q)
    fps = target_fps(info, q)
    cap = w * h * fps * q.bpp / 1000.0
    if info.bitrate_kbps:
        src_rate = info.width * info.height * (info.fps or fps)
        shrink = min(1.0, (w * h * fps) / max(1.0, src_rate))
        cap = min(cap, max(300.0, info.bitrate_kbps * 1.6 * math.sqrt(shrink)))
    return max(200, int(cap))


def estimate_size_mb(info, quality):
    """Rough expected output size in MB (quality-based encodes usually land
    a little under the cap)."""
    q = get_quality(quality)
    audio = q.abr or 160
    return ((bitrate_cap(q, info) * 0.8 + audio)
            * info.duration / 8.0 / 1024.0)


# ---------------- encoder selection ----------------

def _encoder_args(name, q, cap_kbps, fps):
    gop = str(max(30, int(fps * 10)))          # long GOP: fewer keyframes
    buf = f"{cap_kbps * 2}k"
    cap = f"{cap_kbps}k"
    if name == "h264_qsv":
        # QVBR: quality target *and* a bitrate target - without -b:v the QSV
        # rate control ignores -maxrate and overshoots badly
        return ["-c:v", "h264_qsv", "-global_quality", str(q.qsv_q),
                "-b:v", f"{int(cap_kbps * 0.75)}k", "-maxrate", cap,
                "-bufsize", buf, "-g", gop]
    if name == "h264_mf":                      # no CRF equivalent: plain VBR
        return ["-c:v", "h264_mf", "-b:v", f"{int(cap_kbps * 0.8)}k",
                "-maxrate", cap, "-g", gop]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(q.crf),
            "-maxrate", cap, "-bufsize", buf, "-g", gop]


_encoder_cache = None


def pick_encoder():
    """Test-run candidate encoders once; return the first that works."""
    global _encoder_cache
    if _encoder_cache:
        return _encoder_cache
    probe_q = QUALITY_PRESETS[QUALITY_DEFAULT]
    for name in ("h264_qsv", "h264_mf", "libx264"):
        cmd = [ffmpeg_exe(), "-loglevel", "error",
               "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.2:r=10",
               *_encoder_args(name, probe_q, 400, 10), "-f", "null", "-"]
        try:
            if subprocess.run(cmd, capture_output=True, timeout=30,
                              **NOWIN).returncode == 0:
                _encoder_cache = name
                return name
        except (subprocess.TimeoutExpired, OSError):
            continue
    _encoder_cache = "libx264"
    return _encoder_cache


# ---------------- export ----------------

def _stderr_drain(pipe, sink):
    for line in iter(pipe.readline, b""):
        sink.append(line.decode("utf-8", "replace").rstrip())
    pipe.close()


def export(info, tele, gauge_list, offset, out_path, quality=QUALITY_DEFAULT,
           overlay_fps=None, encoder=None,
           progress=None, cancel=None, _audio_mode="copy"):
    """Composite gauges over info.path and write out_path (.mp4).

    offset: data epoch seconds corresponding to video t=0
            (data_time = offset + video_time).
    quality: key into QUALITY_PRESETS (or a Quality) — drives output size.
    overlay_fps: overrides the preset's overlay frame rate.
    progress: callable(done_frames, total_frames) or None.
    cancel: threading.Event or None.
    Returns elapsed seconds. Raises ExportCancelled or RuntimeError.
    """
    q = get_quality(quality)
    overlay_fps = int(overlay_fps or q.overlay_fps)
    w, h = target_dims(info, q)                # overlay is composed at output
    out_fps = target_fps(info, q)              # size, so gauges scale with it
    cap = bitrate_cap(q, info)
    encoder = encoder or pick_encoder()
    total = max(1, int(math.ceil(info.duration * overlay_fps)))
    if q.abr is None and _audio_mode == "copy":
        audio = ["-map", "0:a?", "-c:a", "copy"]
    else:
        audio = ["-map", "0:a?", "-c:a", "aac", "-b:a", f"{q.abr or 160}k"]
    chain = []
    if (w, h) != (info.width, info.height):
        chain.append(f"scale={w}:{h}")
    chain.append(f"fps={out_fps:.6g}")
    cmd = [
        ffmpeg_exe(), "-y", "-loglevel", "error",
        "-i", info.path,
        "-f", "rawvideo", "-pixel_format", "rgba",
        "-video_size", f"{w}x{h}", "-framerate", str(overlay_fps),
        "-i", "pipe:0",
        "-filter_complex",
        f"[0:v]{','.join(chain)}[base];[base][1:v]overlay=0:0[v]",
        "-map", "[v]", *audio,
        *_encoder_args(encoder, q, cap, out_fps),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        out_path,
    ]
    t0 = time.time()
    errors = deque(maxlen=40)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                            **NOWIN)
    drain = threading.Thread(target=_stderr_drain, args=(proc.stderr, errors),
                             daemon=True)
    drain.start()
    written = 0
    try:
        for i in range(total):
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.wait()
                raise ExportCancelled()
            frame = gauges.compose(w, h, gauge_list, tele, offset + i / overlay_fps)
            try:
                proc.stdin.write(frame.tobytes())
                written += 1
            except OSError:
                break  # ffmpeg died - rc check below reports it
            if progress is not None:
                progress(i + 1, total)
        try:
            proc.stdin.close()
        except OSError:
            pass
        rc = proc.wait()
    finally:
        drain.join(timeout=5)
        if proc.poll() is None:
            proc.kill()
    if rc != 0:
        err = "\n".join(list(errors)[-12:])
        # muxer/codec rejections (e.g. PCM audio into mp4) fail almost
        # immediately - retry once transcoding the audio instead of copying
        if (q.abr is None and _audio_mode == "copy"
                and written < overlay_fps * 20):
            return export(info, tele, gauge_list, offset, out_path, quality=q,
                          overlay_fps=overlay_fps, encoder=encoder,
                          progress=progress, cancel=cancel, _audio_mode="aac")
        raise RuntimeError(f"ffmpeg export failed (rc={rc}):\n{err}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
        raise RuntimeError("Export produced no output file")
    return time.time() - t0


def default_output_path(video_path):
    base, _ = os.path.splitext(video_path)
    return base + "_7seas.mp4"


def make_test_clip(path, seconds=60, w=1920, h=1080, fps=30):
    """Generate a synthetic test video with audio (used by --selftest)."""
    subprocess.run([
        ffmpeg_exe(), "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        "-metadata", "creation_time="
        + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
        path,
    ], check=True, capture_output=True, **NOWIN)
