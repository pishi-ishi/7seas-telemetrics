"""Vakaros VKX binary log parser (Atlas / Atlas 2).

Implements the official spec: https://github.com/vakaros/vkx
A VKX file is a stream of rows: one U8 key byte followed by a fixed-size
little-endian payload. The 0x02 Position/Velocity/Orientation row carries
GNSS position, SOG (m/s), COG (radians) and an attitude quaternion in the
NED frame, from which heading / heel (roll) / pitch are derived.
"""

import math
import struct

KTS_PER_MS = 1.9438444924406

# payload sizes (bytes, excluding the key byte) — documented + internal rows
ROW_SIZES = {
    0xFF: 7,    # page header
    0xFE: 2,    # page terminator
    0x02: 44,   # position, velocity, orientation
    0x03: 20,   # declination
    0x04: 13,   # race timer event
    0x05: 17,   # line position
    0x06: 18,   # shift angle
    0x08: 13,   # device configuration
    0x0A: 16,   # wind (Calypso, apparent)
    0x0B: 16,   # speed through water
    0x0C: 12,   # depth
    0x0F: 16,   # load cell
    0x10: 12,   # temperature
    # internal/undocumented rows — parsed length only, content skipped
    0x01: 32, 0x07: 12, 0x0E: 16, 0x20: 13, 0x21: 52,
}

_PVO = struct.Struct("<Qiifffffff")   # ts_ms, lat, lon, sog, cog, alt, w,x,y,z
_WIND = struct.Struct("<Qff")         # ts_ms, direction deg, speed m/s
_STW = struct.Struct("<Qff")          # ts_ms, forward m/s, horizontal m/s
_DEPTH = struct.Struct("<Qf")         # ts_ms, meters
_TEMP = struct.Struct("<Qf")          # ts_ms, deg C
_TIMER = struct.Struct("<QBi")        # ts_ms, event id, timer seconds

RACE_TIMER_EVENTS = {0: "RESET", 1: "START", 2: "SYNC",
                     3: "RACE_START", 4: "RACE_END"}


def quat_to_euler_deg(w, x, y, z):
    """NED quaternion -> (heading, pitch, roll) in degrees.
    Roll is positive when heeled to starboard."""
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if n < 1e-9:
        return None
    w, x, y, z = w / n, x / n, y / n, z / n
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    sp = max(-1.0, min(1.0, 2 * (w * y - z * x)))
    pitch = math.asin(sp)
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return (math.degrees(yaw) % 360.0, math.degrees(pitch), math.degrees(roll))


def parse_vkx(path):
    """Parse a .vkx file.

    Returns a dict of lists:
      pvo:   (t, lat, lon, sog_kn, cog_deg, heading, pitch, roll)
      wind:  (t, awd_deg, aws_kn)
      stw:   (t, kn)
      depth: (t, m)
      temp:  (t, degC)
      race_events: (t, event_name, timer_seconds)
    """
    with open(path, "rb") as f:
        buf = f.read()
    n = len(buf)
    out = {"pvo": [], "wind": [], "stw": [], "depth": [], "temp": [],
           "race_events": []}
    i = 0
    bad_keys = 0
    while i < n:
        key = buf[i]
        i += 1
        size = ROW_SIZES.get(key)
        if size is None:
            # unknown row: cannot know its length — try to resync by scanning
            # for the next page header (0xFF) which repeats every ~2 kB
            bad_keys += 1
            nxt = buf.find(b"\xff", i)
            if nxt == -1 or bad_keys > 50:
                break
            i = nxt
            continue
        if i + size > n:
            break
        p = buf[i:i + size]
        i += size
        try:
            if key == 0x02:
                ts, lat, lon, sog, cog, _alt, qw, qx, qy, qz = _PVO.unpack(p)
                t = ts / 1000.0
                if not (1e8 < t < 4e9):
                    continue
                la, lo = lat * 1e-7, lon * 1e-7
                if abs(la) > 90 or abs(lo) > 180 or (la == 0 and lo == 0):
                    continue
                eul = quat_to_euler_deg(qw, qx, qy, qz)
                hdg, pitch, roll = eul if eul else (None, None, None)
                cog_deg = math.degrees(cog) % 360.0 if math.isfinite(cog) else None
                sog_kn = sog * KTS_PER_MS if math.isfinite(sog) else None
                out["pvo"].append((t, la, lo, sog_kn, cog_deg, hdg, pitch, roll))
            elif key == 0x0A:
                ts, wdir, wspd = _WIND.unpack(p)
                if math.isfinite(wdir) and math.isfinite(wspd):
                    out["wind"].append((ts / 1000.0, wdir % 360.0,
                                        wspd * KTS_PER_MS))
            elif key == 0x0B:
                ts, fwd, _horiz = _STW.unpack(p)
                if math.isfinite(fwd):
                    out["stw"].append((ts / 1000.0, fwd * KTS_PER_MS))
            elif key == 0x0C:
                ts, d = _DEPTH.unpack(p)
                if math.isfinite(d):
                    out["depth"].append((ts / 1000.0, d))
            elif key == 0x10:
                ts, c = _TEMP.unpack(p)
                if math.isfinite(c):
                    out["temp"].append((ts / 1000.0, c))
            elif key == 0x04:
                ts, ev, val = _TIMER.unpack(p)
                out["race_events"].append(
                    (ts / 1000.0, RACE_TIMER_EVENTS.get(ev, str(ev)), val))
        except struct.error:
            continue
    if not out["pvo"]:
        raise ValueError("No position rows (0x02) found — not a valid VKX log?")
    out["pvo"].sort(key=lambda r: r[0])
    return out
