"""FFmpeg-backed video I/O: probing, preview frames, realtime play streams,
and the export pipeline.

Export architecture: the source video is decoded by ffmpeg; Python pipes
RGBA overlay frames (at a modest overlay fps) into ffmpeg's stdin; ffmpeg
composites them with the `overlay` filter and encodes H.264 into an .mp4.
Hardware encoders (QuickSync / MediaFoundation) are auto-detected with a
software x264 fallback.
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
OVERLAY_FPS_DEFAULT = 15
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
    def __init__(self, path, duration, width, height, fps, creation_time=None):
        self.path = path
        self.duration = duration
        self.width = width
        self.height = height
        self.fps = fps
        self.creation_time = creation_time  # epoch seconds or None

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
    return VideoInfo(path, duration, width, height, fps, creation)


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


# ---------------- encoder selection ----------------

def _encoder_args(name, w, h, fps):
    if name == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", "23"]
    if name == "h264_mf":
        kbps = max(4000, int(w * h * max(fps, 24) * 0.2 / 1000))
        return ["-c:v", "h264_mf", "-b:v", f"{kbps}k"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]


_encoder_cache = None


def pick_encoder():
    """Test-run candidate encoders once; return the first that works."""
    global _encoder_cache
    if _encoder_cache:
        return _encoder_cache
    for name in ("h264_qsv", "h264_mf", "libx264"):
        cmd = [ffmpeg_exe(), "-loglevel", "error",
               "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.2:r=10",
               *_encoder_args(name, 320, 240, 10), "-f", "null", "-"]
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


def export(info, tele, gauge_list, offset, out_path,
           overlay_fps=OVERLAY_FPS_DEFAULT, encoder=None,
           progress=None, cancel=None, _audio_mode="copy"):
    """Composite gauges over info.path and write out_path (.mp4).

    offset: data epoch seconds corresponding to video t=0
            (data_time = offset + video_time).
    progress: callable(done_frames, total_frames) or None.
    cancel: threading.Event or None.
    Returns elapsed seconds. Raises ExportCancelled or RuntimeError.
    """
    w, h = info.width, info.height
    encoder = encoder or pick_encoder()
    total = max(1, int(math.ceil(info.duration * overlay_fps)))
    audio = (["-map", "0:a?", "-c:a", "copy"] if _audio_mode == "copy"
             else ["-map", "0:a?", "-c:a", "aac", "-b:a", "192k"])
    cmd = [
        ffmpeg_exe(), "-y", "-loglevel", "error",
        "-i", info.path,
        "-f", "rawvideo", "-pixel_format", "rgba",
        "-video_size", f"{w}x{h}", "-framerate", str(overlay_fps),
        "-i", "pipe:0",
        "-filter_complex", "[0:v][1:v]overlay=0:0[v]",
        "-map", "[v]", *audio,
        *_encoder_args(encoder, w, h, info.fps),
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
        if _audio_mode == "copy" and written < overlay_fps * 20:
            return export(info, tele, gauge_list, offset, out_path,
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
