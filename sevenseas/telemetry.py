"""Telemetry parsing (GPX / CSV / VKX) and time-based sampling.

All times are UNIX epoch seconds (UTC). Angles are degrees 0-360 and are
interpolated along the shortest arc. Speeds are stored in knots.
"""

import bisect
import csv
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

KTS_PER_MS = 1.9438444924406
EARTH_R = 6371000.0
SAMPLE_GRACE = 3.0  # seconds of tolerance beyond data range before returning None

# streams the app knows by name; everything else becomes an "extra" linear stream
ANGULAR_STREAMS = {"heading", "cog", "twd", "awd", "awa"}
STREAM_UNITS = {
    "sog": "kn", "tws": "kn", "heading": "°", "cog": "°",
    "twd": "°", "roll": "°", "pitch": "°", "ele": "m",
    "stw": "kn", "depth": "m", "watertemp": "°C",
    "awa": "°", "awd": "°", "aws": "kn",
}
STREAM_LABELS = {
    "sog": "SOG", "heading": "HDG", "cog": "COG", "twd": "TWD",
    "tws": "TWS", "roll": "HEEL", "pitch": "PITCH", "ele": "ELEV",
    "stw": "STW", "depth": "DEPTH", "watertemp": "WATER",
    "awa": "AWA", "awd": "AWD", "aws": "AWS",
}

# priority order matters for the fuzzy (contains) matching pass;
# apparent-wind streams come before true-wind so "apparent_wind_*"
# columns are claimed correctly
SYNONYMS = [
    ("time", ("time", "timestamp", "datetime", "datetime_utc", "date_time",
              "utc", "date", "gpstime", "gps_time")),
    ("lat", ("lat", "latitude", "gps_lat")),
    ("lon", ("lon", "lng", "long", "longitude", "gps_lon")),
    ("sog", ("sog", "sogkn", "sogkts", "speed", "spd", "gpsspeed",
             "boatspeed", "bsp", "speedkts", "speedkn")),
    ("heading", ("heading", "hdg", "hdt", "hdm", "yaw", "compass", "head")),
    ("cog", ("cog", "course", "courseoverground", "ctrack", "trackdeg",
             "bearing")),
    ("roll", ("roll", "heel", "tilt", "bank", "heeldeg", "rolldeg")),
    ("pitch", ("pitch", "trim", "pitchdeg")),
    ("awa", ("awa", "apparentwindangle", "windangle")),
    ("aws", ("aws", "apparentwindspeed", "appwindspeed")),
    ("awd", ("awd", "apparentwinddirection", "apparentwinddir")),
    ("tws", ("tws", "truewindspeed", "windspeed", "wspd", "windspd")),
    ("twd", ("twd", "truewinddirection", "winddirection", "winddir",
             "wdir", "wind")),
]
SPEED_STREAMS = {"sog", "tws", "aws"}
SPEED_UNIT_FACTORS = {"kn": 1.0, "m/s": KTS_PER_MS, "km/h": 0.5399568, "mph": 0.8689762}


def parse_time(value):
    """Parse a timestamp (ISO8601, common formats, or numeric epoch) -> epoch seconds."""
    s = str(value).strip()
    if not s:
        return None
    try:
        f = float(s)
        if f > 1e11:            # epoch milliseconds
            f /= 1000.0
        if f > 1e8:             # epoch seconds (after ~1973)
            return f
        return None             # small bare numbers are ambiguous - reject
    except ValueError:
        pass
    iso = s.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S",
                "%d-%m-%Y %H:%M:%S", "%H:%M:%S.%f", "%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt.startswith("%H"):
                # time-only logs: pin to an arbitrary date; the sync offset absorbs it
                dt = dt.replace(year=1970, month=1, day=2)
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _norm(header):
    return re.sub(r"[^a-z0-9]", "", str(header).lower())


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def lerp_angle(a, b, f):
    d = ((b - a + 180.0) % 360.0) - 180.0
    return (a + d * f) % 360.0


class Stream:
    __slots__ = ("times", "values", "angular", "unit", "label")

    def __init__(self, times, values, angular=False, unit="", label=""):
        self.times = times
        self.values = values
        self.angular = angular
        self.unit = unit
        self.label = label

    def sample(self, t):
        ts = self.times
        if not ts:
            return None
        if t <= ts[0]:
            return self.values[0] if t >= ts[0] - SAMPLE_GRACE else None
        if t >= ts[-1]:
            return self.values[-1] if t <= ts[-1] + SAMPLE_GRACE else None
        i = bisect.bisect_right(ts, t)
        t0, t1 = ts[i - 1], ts[i]
        v0, v1 = self.values[i - 1], self.values[i]
        f = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        if self.angular:
            return lerp_angle(v0, v1, f)
        return v0 + (v1 - v0) * f


def _smooth_linear(vals, win=5):
    if len(vals) < 3 or win < 2:
        return vals
    half = win // 2
    out = []
    for i in range(len(vals)):
        lo, hi = max(0, i - half), min(len(vals), i + half + 1)
        out.append(sum(vals[lo:hi]) / (hi - lo))
    return out


def _smooth_angular(vals, win=5):
    if len(vals) < 3 or win < 2:
        return vals
    half = win // 2
    out = []
    for i in range(len(vals)):
        lo, hi = max(0, i - half), min(len(vals), i + half + 1)
        sx = sum(math.sin(math.radians(v)) for v in vals[lo:hi])
        cx = sum(math.cos(math.radians(v)) for v in vals[lo:hi])
        out.append(math.degrees(math.atan2(sx, cx)) % 360.0)
    return out


class Telemetry:
    def __init__(self):
        self.streams = {}        # name -> Stream
        self.track = None        # (times, lats, lons) or None
        self.source = ""
        self.kind = ""           # "gpx" | "csv" | "vkx"
        self.columns = []        # csv headers (for the mapping UI)
        self.mapping = {}        # stream name -> source column/field used
        self.speed_units = {}    # stream name -> unit string used at parse time
        self.maneuvers = []      # [{"t": epoch, "kind": "T"|"G",
                                 #   "mag": deg, "enabled": bool}]
        self.race_events = []    # vkx race timer events [(t, name, seconds)]

    # ---- introspection ----
    def has(self, name):
        return name in self.streams and bool(self.streams[name].times)

    @property
    def t_start(self):
        ts = [s.times[0] for s in self.streams.values() if s.times]
        if self.track:
            ts.append(self.track[0][0])
        return min(ts) if ts else 0.0

    @property
    def t_end(self):
        ts = [s.times[-1] for s in self.streams.values() if s.times]
        if self.track:
            ts.append(self.track[0][-1])
        return max(ts) if ts else 0.0

    def sample(self, name, t):
        s = self.streams.get(name)
        return s.sample(t) if s else None

    def position(self, t):
        if not self.track:
            return None
        ts, lats, lons = self.track
        if t < ts[0] - SAMPLE_GRACE or t > ts[-1] + SAMPLE_GRACE:
            return None
        if t <= ts[0]:
            return lats[0], lons[0]
        if t >= ts[-1]:
            return lats[-1], lons[-1]
        i = bisect.bisect_right(ts, t)
        t0, t1 = ts[i - 1], ts[i]
        f = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        return (lats[i - 1] + (lats[i] - lats[i - 1]) * f,
                lons[i - 1] + (lons[i] - lons[i - 1]) * f)

    # ---- trimming ----
    def trimmed(self, t0=None, t1=None):
        """Return a copy restricted exactly to [t0, t1] (absolute epoch
        seconds). Boundary samples are interpolated at the cut times so the
        trimmed range starts/ends precisely at the cuts."""
        a = self.t_start if t0 is None else t0
        b = self.t_end if t1 is None else t1
        new = Telemetry()
        new.source, new.kind = self.source, self.kind
        new.columns = list(self.columns)
        new.mapping = dict(self.mapping)
        new.speed_units = dict(self.speed_units)
        new.race_events = list(self.race_events)
        for name, s in self.streams.items():
            lo = bisect.bisect_left(s.times, a)
            hi = bisect.bisect_right(s.times, b)
            times = list(s.times[lo:hi])
            vals = list(s.values[lo:hi])
            va, vb = s.sample(a), s.sample(b)
            if va is not None and (not times or times[0] > a):
                times.insert(0, a)
                vals.insert(0, va)
            if vb is not None and (not times or times[-1] < b):
                times.append(b)
                vals.append(vb)
            if len(times) >= 2:
                new.streams[name] = Stream(times, vals, s.angular, s.unit,
                                           s.label)
        if self.track:
            ts, la, lo_ = self.track
            i0 = bisect.bisect_left(ts, a)
            i1 = bisect.bisect_right(ts, b)
            tt = list(ts[i0:i1])
            aa = list(la[i0:i1])
            bb = list(lo_[i0:i1])
            pa, pb = self.position(a), self.position(b)
            if pa is not None and (not tt or tt[0] > a):
                tt.insert(0, a)
                aa.insert(0, pa[0])
                bb.insert(0, pa[1])
            if pb is not None and (not tt or tt[-1] < b):
                tt.append(b)
                aa.append(pb[0])
                bb.append(pb[1])
            if len(tt) >= 2:
                new.track = (tt, aa, bb)
        new.maneuvers = [m for m in self.maneuvers if a <= m["t"] <= b]
        return new

    # ---- construction helpers ----
    def _add(self, name, times, values, angular=None, unit=None, label=None):
        if not times:
            return
        self.streams[name] = Stream(
            times, values,
            angular=(name in ANGULAR_STREAMS) if angular is None else angular,
            unit=STREAM_UNITS.get(name, "") if unit is None else unit,
            label=STREAM_LABELS.get(name, name.upper()[:8]) if label is None else label,
        )

    def _derive_from_track(self):
        """Fill sog/cog (and heading fallback) from GPS positions if missing."""
        if not self.track:
            return
        ts, lats, lons = self.track
        if len(ts) < 3:
            return
        dts, sogs, cogs = [], [], []
        for i in range(1, len(ts)):
            dt = ts[i] - ts[i - 1]
            if dt <= 0:
                continue
            d = haversine_m(lats[i - 1], lons[i - 1], lats[i], lons[i])
            dts.append(ts[i])
            sogs.append((d / dt) * KTS_PER_MS)
            cogs.append(bearing_deg(lats[i - 1], lons[i - 1], lats[i], lons[i]))
        if not dts:
            return
        sogs = _smooth_linear(sogs, 5)
        cogs = _smooth_angular(cogs, 5)
        if not self.has("sog"):
            self._add("sog", dts, sogs)
            self.mapping.setdefault("sog", "(derived from GPS)")
        if not self.has("cog"):
            self._add("cog", dts, cogs)
            self.mapping.setdefault("cog", "(derived from GPS)")
        if not self.has("heading") and self.has("cog"):
            c = self.streams["cog"]
            self._add("heading", c.times, c.values, angular=True,
                      label="HDG")
            self.mapping.setdefault("heading", "(COG used as heading)")

    # ---- GPX ----
    @classmethod
    def from_gpx(cls, path):
        tele = cls()
        tele.source = path
        tele.kind = "gpx"
        root = ET.parse(path).getroot()
        pts = []
        for el in root.iter():
            if not el.tag.endswith("trkpt"):
                continue
            try:
                lat = float(el.get("lat"))
                lon = float(el.get("lon"))
            except (TypeError, ValueError):
                continue
            t = None
            ele = None
            ext = {}
            for ch in el.iter():
                tag = ch.tag.rpartition("}")[2].lower()
                txt = (ch.text or "").strip()
                if not txt:
                    continue
                if tag == "time":
                    t = parse_time(txt)
                elif tag == "ele":
                    try:
                        ele = float(txt)
                    except ValueError:
                        pass
                elif tag != "trkpt":
                    try:
                        ext[tag] = float(txt)
                    except ValueError:
                        pass
            if t is not None:
                pts.append((t, lat, lon, ele, ext))
        if len(pts) < 2:
            raise ValueError("GPX contains fewer than 2 timestamped track points")
        pts.sort(key=lambda p: p[0])
        # drop duplicate timestamps
        dedup = [pts[0]]
        for p in pts[1:]:
            if p[0] > dedup[-1][0]:
                dedup.append(p)
        pts = dedup

        ts = [p[0] for p in pts]
        tele.track = (ts, [p[1] for p in pts], [p[2] for p in pts])
        eles = [(p[0], p[3]) for p in pts if p[3] is not None]
        if len(eles) >= 2:
            tele._add("ele", [e[0] for e in eles], [e[1] for e in eles])

        # recognized GPX extensions
        ext_keys = {}
        for p in pts:
            for k in p[4]:
                ext_keys[k] = ext_keys.get(k, 0) + 1
        for key, count in ext_keys.items():
            if count < len(pts) * 0.5:
                continue
            series = [(p[0], p[4][key]) for p in pts if key in p[4]]
            st = [s[0] for s in series]
            sv = [s[1] for s in series]
            if key == "speed":  # GPX speed extension is m/s
                tele._add("sog", st, [v * KTS_PER_MS for v in sv])
                tele.mapping["sog"] = "gpx:speed"
            elif key in ("course", "cog"):
                tele._add("cog", st, sv)
                tele.mapping["cog"] = "gpx:" + key
            elif key in ("heading", "hdg"):
                tele._add("heading", st, sv)
                tele.mapping["heading"] = "gpx:" + key
            elif key in ("roll", "heel"):
                tele._add("roll", st, sv)
                tele.mapping["roll"] = "gpx:" + key
            else:
                tele._add(key, st, sv, angular=False, unit="", label=key.upper()[:8])
        tele._derive_from_track()
        return tele

    # ---- CSV ----
    @staticmethod
    def sniff_csv(path):
        """Return (headers, list-of-row-dicts). Handles , ; and tab delimiters."""
        with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
            head = f.read(8192)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(head, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(f, dialect=dialect)
            headers = [h.strip() for h in (reader.fieldnames or [])]
            rows = []
            for raw in reader:
                rows.append({(k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
                             for k, v in raw.items()})
        return headers, rows

    @staticmethod
    def auto_map(headers):
        """Guess {stream: column} from header names."""
        normed = {h: _norm(h) for h in headers}
        mapping = {}
        claimed = set()
        # pass 1: exact normalized match
        for stream, syns in SYNONYMS:
            for h in headers:
                if h in claimed:
                    continue
                if normed[h] in syns:
                    mapping[stream] = h
                    claimed.add(h)
                    break
        # pass 2: substring match
        for stream, syns in SYNONYMS:
            if stream in mapping:
                continue
            for h in headers:
                if h in claimed:
                    continue
                if any(syn in normed[h] for syn in syns if len(syn) >= 3):
                    mapping[stream] = h
                    claimed.add(h)
                    break
        return mapping

    @classmethod
    def from_csv(cls, path, mapping=None, speed_units=None):
        """Parse a CSV log. mapping: {stream: column} (auto-detected when None).
        speed_units: {"sog": "kn"|"m/s"|"km/h"|"mph", "tws": ...}."""
        headers, rows = cls.sniff_csv(path)
        if not headers or not rows:
            raise ValueError("CSV appears to be empty")
        tele = cls()
        tele.source = path
        tele.kind = "csv"
        tele.columns = headers
        auto = cls.auto_map(headers)
        mapping = dict(auto if mapping is None else mapping)
        tcol = mapping.get("time")
        if not tcol or tcol not in headers:
            raise ValueError(
                "No time column found. Columns: " + ", ".join(headers[:12]))
        speed_units = speed_units or {}

        # epoch times per row (None where unparseable)
        times = [parse_time(r.get(tcol, "")) for r in rows]
        order = sorted((t, i) for i, t in enumerate(times) if t is not None)
        if len(order) < 2:
            raise ValueError(f"Could not parse timestamps in column '{tcol}'")
        # keep strictly increasing
        keep = [order[0]]
        for t, i in order[1:]:
            if t > keep[-1][0]:
                keep.append((t, i))

        def column_series(col):
            st, sv = [], []
            for t, i in keep:
                raw = rows[i].get(col)
                if raw in (None, ""):
                    continue
                try:
                    sv.append(float(raw))
                    st.append(t)
                except ValueError:
                    continue
            return st, sv

        used = {tcol}
        for stream, _ in SYNONYMS:
            if stream == "time":
                continue
            col = mapping.get(stream)
            if not col or col not in headers:
                continue
            st, sv = column_series(col)
            if not st:
                continue
            used.add(col)
            if stream in SPEED_STREAMS:
                unit = speed_units.get(stream, "kn")
                fac = SPEED_UNIT_FACTORS.get(unit, 1.0)
                sv = [v * fac for v in sv]
                tele.speed_units[stream] = unit
            if stream in ("lat", "lon"):
                continue  # handled as track below
            tele._add(stream, st, sv)
            tele.mapping[stream] = col
        tele.mapping["time"] = tcol

        # track from lat/lon
        latc, lonc = mapping.get("lat"), mapping.get("lon")
        if latc in headers and lonc in headers:
            tt, la, lo = [], [], []
            for t, i in keep:
                try:
                    a = float(rows[i].get(latc, ""))
                    b = float(rows[i].get(lonc, ""))
                except (TypeError, ValueError):
                    continue
                if abs(a) <= 90 and abs(b) <= 180 and (a != 0.0 or b != 0.0):
                    tt.append(t)
                    la.append(a)
                    lo.append(b)
            if len(tt) >= 2:
                tele.track = (tt, la, lo)
                used.update((latc, lonc))
                tele.mapping["lat"] = latc
                tele.mapping["lon"] = lonc

        # every other mostly-numeric column becomes an extra stream
        for col in headers:
            if col in used or not col:
                continue
            st, sv = column_series(col)
            if len(st) >= max(2, len(keep) * 0.5):
                name = _norm(col) or col
                if name not in tele.streams:
                    tele._add(name, st, sv, angular=False, unit="",
                              label=col.upper()[:10])
        tele._derive_from_track()
        return tele

    # ---- VKX (Vakaros Atlas / Atlas 2) ----
    @classmethod
    def from_vkx(cls, path):
        from .vkx import parse_vkx
        data = parse_vkx(path)
        tele = cls()
        tele.source = path
        tele.kind = "vkx"

        rows = data["pvo"]
        dedup = [rows[0]]
        for r in rows[1:]:
            if r[0] > dedup[-1][0]:
                dedup.append(r)
        rows = dedup
        ts = [r[0] for r in rows]
        tele.track = (ts, [r[1] for r in rows], [r[2] for r in rows])

        def series(idx):
            st, sv = [], []
            for r in rows:
                if r[idx] is not None:
                    st.append(r[0])
                    sv.append(r[idx])
            return st, sv

        for idx, name in ((3, "sog"), (4, "cog"), (5, "heading"),
                          (6, "pitch"), (7, "roll")):
            st, sv = series(idx)
            if len(st) >= 2:
                tele._add(name, st, sv)
                tele.mapping[name] = "vkx:attitude" if idx >= 5 else "vkx:gnss"

        if len(data["wind"]) >= 2:
            wt = [w[0] for w in data["wind"]]
            awd_vals = [w[1] for w in data["wind"]]
            tele._add("awd", wt, awd_vals)
            tele._add("aws", wt, [w[2] for w in data["wind"]])
            tele.mapping["awd"] = "vkx:wind (apparent)"
            tele.mapping["aws"] = "vkx:wind (apparent)"
            if tele.has("heading"):
                h = tele.streams["heading"]
                at, av = [], []
                for t, awd in zip(wt, awd_vals):
                    hv = h.sample(t)
                    if hv is not None:
                        at.append(t)
                        av.append((awd - hv) % 360.0)
                if len(at) >= 2:
                    tele._add("awa", at, av)
                    tele.mapping["awa"] = "(derived: AWD − HDG)"
        for key, name in (("stw", "stw"), ("depth", "depth"),
                          ("temp", "watertemp")):
            if len(data[key]) >= 2:
                tele._add(name, [r[0] for r in data[key]],
                          [r[1] for r in data[key]])
                tele.mapping[name] = "vkx:" + key
        tele.race_events = data["race_events"]
        tele._derive_from_track()
        return tele


# ---- maneuver detection (tacks / gybes) ----

def detect_maneuvers(tele, t_range=(65.0, 120.0), g_range=(15.0, 60.0),
                     window=12.0):
    """Detect course changes from the heading (or COG) stream.

    A maneuver is a local peak of |heading(t+w) - heading(t-w)| (unwrapped).
    Peaks whose magnitude falls in t_range become tacks ("T"), in g_range
    gybes ("G"); anything else (e.g. a 360 penalty spin) is ignored.
    Returns [{"t", "kind", "mag", "enabled"}] sorted by time.
    """
    s = tele.streams.get("heading") or tele.streams.get("cog")
    if not s or len(s.times) < 10:
        return []
    t0, t1 = s.times[0], s.times[-1]
    n = int(t1 - t0)
    w = max(3, int(window))
    if n < 2 * w + 2:
        return []
    hs = [s.sample(t0 + i) for i in range(n + 1)]
    unw = [hs[0] or 0.0]
    for h in hs[1:]:
        h = unw[-1] if h is None else h
        d = ((h - (unw[-1] % 360.0) + 180.0) % 360.0) - 180.0
        unw.append(unw[-1] + d)
    unw = _smooth_linear(unw, 5)
    deltas = [unw[i + w] - unw[i - w] for i in range(w, len(unw) - w)]
    lo_min = min(t_range[0], g_range[0])
    events = []
    i = 0
    while i < len(deltas):
        if abs(deltas[i]) >= lo_min:
            j, peak = i, i
            while j < len(deltas) and abs(deltas[j]) >= lo_min:
                if abs(deltas[j]) > abs(deltas[peak]):
                    peak = j
                j += 1
            mag = abs(deltas[peak])
            kind = None
            if t_range[0] <= mag <= t_range[1]:
                kind = "T"
            elif g_range[0] <= mag <= g_range[1]:
                kind = "G"
            if kind:
                events.append({"t": t0 + peak + w, "kind": kind,
                               "mag": round(mag, 1), "enabled": True})
            i = j + w
        else:
            i += 1
    return events


def _mean_heading(s, t, half=2.0, step=1.0):
    """Circular mean of a heading stream over [t-half, t+half]; averaging
    rides out the yaw wobble a single sample would pick up."""
    xs = ys = 0.0
    tt = t - half
    while tt <= t + half + 1e-9:
        v = s.sample(tt)
        if v is not None:
            a = math.radians(v)
            xs += math.cos(a)
            ys += math.sin(a)
        tt += step
    if abs(xs) < 1e-9 and abs(ys) < 1e-9:
        return None
    return math.degrees(math.atan2(ys, xs)) % 360.0


def course_change(tele, t, window=12.0):
    """Signed course change (±180°) across ±window seconds at t — the same
    quantity detect_maneuvers thresholds, for one hand-picked instant.
    None when there is no heading/COG data around t."""
    s = tele.streams.get("heading") or tele.streams.get("cog")
    if not s or len(s.times) < 3:
        return None
    a = _mean_heading(s, t - window)
    b = _mean_heading(s, t + window)
    if a is None or b is None:
        return None
    return ((b - a + 180.0) % 360.0) - 180.0


def numbered_maneuvers(maneuvers):
    """[(maneuver, "T1"/"G2"/...)] for enabled maneuvers in time order."""
    out = []
    counts = {}
    for m in sorted((m for m in maneuvers if m.get("enabled", True)),
                    key=lambda m: m["t"]):
        k = m.get("kind", "T")
        counts[k] = counts.get(k, 0) + 1
        out.append((m, f"{k}{counts[k]}"))
    return out


def load(path, mapping=None, speed_units=None):
    """Load telemetry by file extension."""
    low = path.lower()
    if low.endswith(".gpx"):
        return Telemetry.from_gpx(path)
    if low.endswith(".vkx"):
        return Telemetry.from_vkx(path)
    return Telemetry.from_csv(path, mapping=mapping, speed_units=speed_units)
