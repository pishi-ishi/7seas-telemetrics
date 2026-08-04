"""Gauge rendering (Pillow). Gauges are authored in a 1080p design space and
scaled to the target frame height.

Rendering strategy (keeps export fast):
- static faces are drawn once at 2x, downsampled and cached at 1x
- rotating parts (compass card, needles, position dot) are prebuilt the same
  way and rotated per frame as small 1x sprites — rotation resampling keeps
  their edges antialiased
- per-frame text uses FreeType antialiasing directly at 1x

Bar / digits / track-map gauges can be stretched per-axis (sx, sy); the
circular instruments only scale uniformly (size).
"""

import bisect
import math
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

SS = 2  # supersampling for prebuilt artwork
DESIGN_H = 1080.0

# palette (DESIGN.md)
PANEL = (16, 16, 16, 186)
PANEL_EDGE = (255, 255, 255, 18)
TEXT = (250, 250, 250, 255)
MUTED = (167, 167, 167, 255)
DIM = (111, 111, 111, 255)
ACCENT = (220, 20, 60, 255)
ACCENT_SOFT = (220, 20, 60, 205)
GREEN = (34, 197, 94, 255)
GREEN_SOFT = (34, 197, 94, 205)
TRACKLINE = (255, 255, 255, 110)

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
FONT_FILES = {
    "ui": ("segoeui.ttf", "arial.ttf"),
    "uib": ("seguisb.ttf", "arialbd.ttf"),
    "mono": ("consola.ttf", "cour.ttf"),
    "monob": ("consolab.ttf", "courbd.ttf"),
}


@lru_cache(maxsize=512)
def font(kind, size):
    size = max(6, int(size))
    for name in FONT_FILES.get(kind, ("arial.ttf",)):
        p = os.path.join(FONT_DIR, name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def az_xy(cx, cy, r, az_deg):
    a = math.radians(az_deg)
    return cx + r * math.sin(a), cy - r * math.cos(a)


def draw_needle(dr, cx, cy, az, r_tip, r_tail, half_w, color):
    tip = az_xy(cx, cy, r_tip, az)
    left = az_xy(cx, cy, half_w, az - 90)
    right = az_xy(cx, cy, half_w, az + 90)
    tail = az_xy(cx, cy, r_tail, az + 180)
    dr.polygon([tip, left, tail, right], fill=color)


def nice_max(v, candidates):
    for c in candidates:
        if v <= c:
            return c
    return candidates[-1]


def fmt_value(v, angular=False):
    if v is None:
        return "---"
    if angular:
        return f"{int(round(v)) % 360:03d}\u00b0"
    a = abs(v)
    if a >= 100:
        return f"{v:.0f}"
    if a >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


class Gauge:
    """Base gauge. x, y are normalized top-left (0..1 of frame w/h);
    size is a uniform scale; sx/sy stretch width/height (STRETCH kinds only)."""

    KIND = "base"
    DESIGN_W = 230
    DESIGN_H_ = 230
    STRETCH = False

    def __init__(self, stream="", label="", x=0.05, y=0.05, size=1.0):
        self.stream = stream
        self.label = label
        self.x = x
        self.y = y
        self.size = size
        self.sx = 1.0
        self.sy = 1.0
        self.enabled = True
        self._face_cache = {}
        self._sprite_cache = {}

    # ---- geometry ----
    def pixel_wh(self, frame_h):
        s = frame_h / DESIGN_H * self.size
        sx = self.sx if self.STRETCH else 1.0
        sy = self.sy if self.STRETCH else 1.0
        return (max(8, int(self.DESIGN_W * s * sx)),
                max(8, int(self.DESIGN_H_ * s * sy)))

    def pixel_rect(self, frame_w, frame_h):
        w, h = self.pixel_wh(frame_h)
        return int(self.x * frame_w), int(self.y * frame_h), w, h

    # ---- rendering ----
    def available(self, tele):
        return tele is not None and tele.has(self.stream)

    def _face_key(self, tele, w, h):
        return (w, h)

    def _face(self, tele, w, h):
        key = self._face_key(tele, w, h)
        img = self._face_cache.get(key)
        if img is None:
            if len(self._face_cache) > 4:
                self._face_cache.clear()
            big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
            self._draw_face(ImageDraw.Draw(big), big, tele, w * SS, h * SS)
            img = big.resize((w, h), Image.LANCZOS)
            self._face_cache[key] = img
        return img

    def _sprite(self, key, scale, size_design, build):
        """Prebuilt 1x sprite: size_design design-units, drawn at 2x.
        scale = pixels per design unit."""
        px1 = max(2, int(size_design * scale))
        full = (key, px1)
        img = self._sprite_cache.get(full)
        if img is None:
            if len(self._sprite_cache) > 8:
                self._sprite_cache.clear()
            px2 = px1 * SS
            big = Image.new("RGBA", (px2, px2), (0, 0, 0, 0))
            build(ImageDraw.Draw(big), big, px2, px2 / size_design)
            img = big.resize((px1, px1), Image.LANCZOS)
            self._sprite_cache[full] = img
        return img

    def render(self, tele, t, frame_h):
        w, h = self.pixel_wh(frame_h)
        img = self._face(tele, w, h).copy()
        self._draw_dynamic(ImageDraw.Draw(img), img, tele, t, w, h,
                           w / self.DESIGN_W, h / self.DESIGN_H_)
        return img

    # subclasses: _draw_face works in 2x pixels; _draw_dynamic at 1x with
    # kx = w/DESIGN_W, ky = h/DESIGN_H_ (equal for non-stretch gauges)
    def _draw_face(self, dr, img, tele, w2, h2):
        pass

    def _draw_dynamic(self, dr, img, tele, t, w, h, kx, ky):
        pass

    # ---- shared bits ----
    def _panel(self, dr, w2, h2, k2, radius=16):
        dr.rounded_rectangle([0, 0, w2 - 1, h2 - 1], radius=radius * k2,
                             fill=PANEL, outline=PANEL_EDGE,
                             width=max(1, int(k2)))

    def _label_txt(self, dr, k2, text=None, x=14, y=9):
        dr.text((x * k2, y * k2), (text or self.label).upper(),
                font=font("uib", 14 * k2), fill=MUTED)

    # ---- persistence ----
    def to_dict(self):
        return {"kind": self.KIND, "stream": self.stream, "label": self.label,
                "x": round(self.x, 4), "y": round(self.y, 4),
                "size": self.size, "sx": round(self.sx, 3),
                "sy": round(self.sy, 3), "enabled": self.enabled}

    @staticmethod
    def from_dict(d):
        cls = GAUGE_KINDS.get(d.get("kind"))
        if not cls:
            return None
        g = cls(stream=d.get("stream", ""), label=d.get("label", ""))
        g.x = float(d.get("x", 0.05))
        g.y = float(d.get("y", 0.05))
        g.size = float(d.get("size", 1.0))
        g.sx = float(d.get("sx", 1.0))
        g.sy = float(d.get("sy", 1.0))
        g.enabled = bool(d.get("enabled", True))
        return g


class CompassCard(Gauge):
    """Rotating marine compass card with fixed lubber line — for heading."""

    KIND = "compass_card"
    CX, CY = 115, 124  # card centre in design units
    CARD_D = 190

    def _build_card(self, dr, px2, k2):
        c = px2 / 2.0
        dr.ellipse([0, 0, px2 - 1, px2 - 1], fill=(13, 13, 13, 232),
                   outline=(255, 255, 255, 34), width=max(1, int(1.2 * k2)))
        r_out = 88 * k2
        for a in range(0, 360, 5):
            if a % 30 == 0:
                r_in, wd, col = 74 * k2, max(1, int(2.4 * k2)), TEXT
            elif a % 10 == 0:
                r_in, wd, col = 78 * k2, max(1, int(1.8 * k2)), MUTED
            else:
                r_in, wd, col = 82 * k2, max(1, int(1.2 * k2)), DIM
            dr.line([az_xy(c, c, r_in, a), az_xy(c, c, r_out, a)],
                    fill=col, width=wd)

    def _card(self, ky):
        def build(dr, img, px2, k2):
            self._build_card(dr, px2, k2)
            c = px2 / 2.0
            # cardinal letters + tens numerals, pre-rotated so they read
            # upright when their azimuth is under the lubber line
            for a in range(0, 360, 30):
                if a % 90 == 0:
                    txt = "NESW"[a // 90]
                    f = font("uib", 24 * k2)
                    col = ACCENT if a == 0 else TEXT
                else:
                    txt = str(a)
                    f = font("ui", 12.5 * k2)
                    col = MUTED
                pad = max(2, int(30 * k2))
                tile = Image.new("RGBA", (pad * 2, pad * 2), (0, 0, 0, 0))
                ImageDraw.Draw(tile).text((pad, pad), txt, font=f, fill=col,
                                          anchor="mm")
                tile = tile.rotate(-a, resample=Image.BICUBIC)
                tx, ty = az_xy(c, c, 60 * k2, a)
                img.alpha_composite(tile, (int(tx - pad), int(ty - pad)))
        return self._sprite("card", ky, self.CARD_D, build)

    def _front(self, ky):
        def build(dr, img, px2, k2):
            c = px2 / 2.0
            top = c - 95 * k2
            dr.polygon([(c, top + 14 * k2), (c - 7 * k2, top - 2 * k2),
                        (c + 7 * k2, top - 2 * k2)], fill=ACCENT)
            r_hub = 40 * k2
            dr.ellipse([c - r_hub, c - r_hub, c + r_hub, c + r_hub],
                       fill=(10, 10, 10, 240), outline=(255, 255, 255, 28),
                       width=max(1, int(k2)))
        return self._sprite("front", ky, 210, build)

    def _draw_face(self, dr, img, tele, w2, h2):
        k2 = h2 / self.DESIGN_H_
        self._panel(dr, w2, h2, k2)
        self._label_txt(dr, k2)

    def _draw_dynamic(self, dr, img, tele, t, w, h, kx, ky):
        cx, cy = self.CX * ky, self.CY * ky
        v = tele.sample(self.stream, t) if tele else None
        card = self._card(ky)
        if v is not None:
            card = card.rotate(v, resample=Image.BILINEAR)
        d = card.size[0]
        img.alpha_composite(card, (int(cx - d / 2), int(cy - d / 2)))
        front = self._front(ky)
        d = front.size[0]
        img.alpha_composite(front, (int(cx - d / 2), int(cy - d / 2)))
        dr = ImageDraw.Draw(img)
        dr.text((cx, cy), fmt_value(v, angular=True),
                font=font("monob", 27 * ky),
                fill=TEXT if v is not None else DIM, anchor="mm")


class NeedleCompass(Gauge):
    """Fixed rose + pointing needle + digits — for wind direction / COG."""

    KIND = "compass_needle"
    CX, CY = 115, 112

    def _draw_face(self, dr, img, tele, w2, h2):
        k2 = h2 / self.DESIGN_H_
        self._panel(dr, w2, h2, k2)
        self._label_txt(dr, k2)
        cx, cy = self.CX * k2, self.CY * k2
        r_out = 74 * k2
        for a in range(0, 360, 5):
            if a % 90 == 0:
                continue
            if a % 30 == 0:
                r_in, wd, col = 64 * k2, max(1, int(2 * k2)), MUTED
            else:
                r_in, wd, col = 69 * k2, max(1, int(1.2 * k2)), DIM
            dr.line([az_xy(cx, cy, r_in, a), az_xy(cx, cy, r_out, a)],
                    fill=col, width=wd)
        for i, letter in enumerate("NESW"):
            a = i * 90
            tx, ty = az_xy(cx, cy, 68 * k2, a)
            dr.text((tx, ty), letter, font=font("uib", 15 * k2),
                    fill=ACCENT if a == 0 else TEXT, anchor="mm")

    def _needle(self, ky):
        def build(dr, img, px2, k2):
            c = px2 / 2.0
            draw_needle(dr, c, c, 0, 56 * k2, 16 * k2, 6.5 * k2, ACCENT)
            dr.ellipse([c - 5 * k2, c - 5 * k2, c + 5 * k2, c + 5 * k2],
                       fill=TEXT)
        return self._sprite("needle", ky, 120, build)

    def _draw_dynamic(self, dr, img, tele, t, w, h, kx, ky):
        cx, cy = self.CX * ky, self.CY * ky
        v = tele.sample(self.stream, t) if tele else None
        if v is not None:
            spr = self._needle(ky).rotate(-v, resample=Image.BILINEAR)
            d = spr.size[0]
            img.alpha_composite(spr, (int(cx - d / 2), int(cy - d / 2)))
            dr = ImageDraw.Draw(img)
        dr.text((cx, 205 * ky), fmt_value(v, angular=True),
                font=font("monob", 23 * ky),
                fill=TEXT if v is not None else DIM, anchor="mm")


class SpeedDial(Gauge):
    """270-degree dial with needle and digital readout — for SOG."""

    KIND = "speed_dial"
    AZ0, SWEEP = -135.0, 270.0
    CX, CY, R = 115, 124, 84
    MAXES = (4, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100)

    def __init__(self, stream="sog", label="SOG", unit="kn", **kw):
        super().__init__(stream, label, **kw)
        self.unit = unit
        self._vmax = 10.0
        self._vmax_key = None

    def vmax_for(self, tele):
        key = id(tele)
        if key != self._vmax_key:
            s = tele.streams.get(self.stream) if tele else None
            top = max(s.values) if s and s.values else 8.0
            self._vmax = float(nice_max(top * 1.05, self.MAXES))
            self._vmax_key = key
            self._face_cache.clear()
        return self._vmax

    def _face_key(self, tele, w, h):
        return (w, h, self.vmax_for(tele))

    def _step(self, vmax):
        return {4: 1, 6: 1, 8: 2, 10: 2, 12: 2, 15: 5, 20: 5, 25: 5,
                30: 5, 40: 10, 50: 10, 60: 10, 80: 20, 100: 20}.get(int(vmax), 5)

    def _az(self, v, vmax):
        f = 0.0 if vmax <= 0 else max(0.0, min(1.0, v / vmax))
        return self.AZ0 + self.SWEEP * f

    def _draw_face(self, dr, img, tele, w2, h2):
        k2 = h2 / self.DESIGN_H_
        vmax = self.vmax_for(tele)
        self._panel(dr, w2, h2, k2)
        self._label_txt(dr, k2)
        cx, cy, r = self.CX * k2, self.CY * k2, self.R * k2
        box = [cx - r, cy - r, cx + r, cy + r]
        dr.arc(box, self.AZ0 - 90, self.AZ0 + self.SWEEP - 90,
               fill=(66, 66, 66, 255), width=max(2, int(4 * k2)))
        step = self._step(vmax)
        minor = step / 2.0
        v = 0.0
        while v <= vmax + 1e-6:
            a = self._az(v, vmax)
            major = abs(v / step - round(v / step)) < 1e-6
            r_in = r - (13 * k2 if major else 7 * k2)
            dr.line([az_xy(cx, cy, r_in, a), az_xy(cx, cy, r - 2 * k2, a)],
                    fill=TEXT if major else DIM,
                    width=max(1, int((2.4 if major else 1.2) * k2)))
            if major:
                tx, ty = az_xy(cx, cy, r - 26 * k2, a)
                dr.text((tx, ty), f"{v:g}", font=font("mono", 13 * k2),
                        fill=MUTED, anchor="mm")
            v += minor

    def _needle(self, ky):
        def build(dr, img, px2, k2):
            c = px2 / 2.0
            draw_needle(dr, c, c, 0, 62 * k2, 16 * k2, 6 * k2, ACCENT)
            dr.ellipse([c - 5 * k2, c - 5 * k2, c + 5 * k2, c + 5 * k2],
                       fill=TEXT)
        return self._sprite("needle", ky, 132, build)

    def _draw_dynamic(self, dr, img, tele, t, w, h, kx, ky):
        vmax = self.vmax_for(tele)
        cx, cy, r = self.CX * ky, self.CY * ky, self.R * ky
        v = tele.sample(self.stream, t) if tele else None
        if v is not None:
            az = self._az(v, vmax)
            # progress arc, drawn 2x on a small scratch for clean edges
            side = int(2 * (r + 4 * ky)) * SS
            scratch = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            sc = side / 2.0
            ImageDraw.Draw(scratch).arc(
                [sc - r * SS, sc - r * SS, sc + r * SS, sc + r * SS],
                self.AZ0 - 90, az - 90, fill=ACCENT,
                width=max(2, int(4 * ky) * SS))
            scratch = scratch.resize((side // SS, side // SS), Image.BILINEAR)
            img.alpha_composite(scratch, (int(cx - side / SS / 2),
                                          int(cy - side / SS / 2)))
            spr = self._needle(ky).rotate(-az, resample=Image.BILINEAR)
            d = spr.size[0]
            img.alpha_composite(spr, (int(cx - d / 2), int(cy - d / 2)))
            dr = ImageDraw.Draw(img)
        dr.text((cx, 186 * ky), fmt_value(v), font=font("monob", 30 * ky),
                fill=TEXT if v is not None else DIM, anchor="mm")
        dr.text((cx, 208 * ky), self.unit, font=font("ui", 12 * ky),
                fill=MUTED, anchor="mm")


class HeelBar(Gauge):
    """Horizontal bar from center zero — for roll / heel / tilt.
    Fill is GREEN when heeled to starboard (right), CRIMSON to port (left).
    Stretch it horizontally (right-click > Wider) to magnify small angles."""

    KIND = "heel_bar"
    DESIGN_W = 300
    DESIGN_H_ = 110
    STRETCH = True

    def __init__(self, stream="roll", label="HEEL", **kw):
        super().__init__(stream, label, **kw)
        self._rng = 15.0
        self._rng_key = None

    def rng_for(self, tele):
        key = id(tele)
        if key != self._rng_key:
            s = tele.streams.get(self.stream) if tele else None
            top = max((abs(v) for v in s.values), default=10.0) if s else 10.0
            self._rng = float(nice_max(max(top * 1.1, 10.0),
                                       (10, 15, 20, 30, 45, 60, 90)))
            self._rng_key = key
            self._face_cache.clear()
        return self._rng

    def _face_key(self, tele, w, h):
        return (w, h, self.rng_for(tele))

    @staticmethod
    def _geom(w, h, ky):
        m = 26 * ky
        return m, w - m, 0.60 * h, 12 * ky

    def _draw_face(self, dr, img, tele, w2, h2):
        k2 = h2 / self.DESIGN_H_
        rng = self.rng_for(tele)
        self._panel(dr, w2, h2, k2, radius=14)
        self._label_txt(dr, k2)
        x0, x1, yc, hh = self._geom(w2, h2, k2)
        dr.rounded_rectangle([x0, yc - hh, x1, yc + hh], radius=hh,
                             fill=(36, 36, 36, 220))
        cx = (x0 + x1) / 2
        step = 5 if rng <= 20 else (10 if rng <= 45 else 15)
        d = step
        while d <= rng + 1e-6:
            for sgn in (-1, 1):
                x = cx + sgn * (d / rng) * (x1 - x0) / 2
                dr.line([x, yc + hh + 3 * k2, x, yc + hh + 8 * k2], fill=DIM,
                        width=max(1, int(1.2 * k2)))
                if (d / step) % 2 == 0:
                    dr.text((x, yc + hh + 16 * k2), f"{int(d)}",
                            font=font("ui", 10.5 * k2), fill=DIM, anchor="mm")
            d += step
        dr.text((x0 - 13 * k2, yc), "P", font=font("uib", 12 * k2), fill=DIM,
                anchor="mm")
        dr.text((x1 + 13 * k2, yc), "S", font=font("uib", 12 * k2), fill=DIM,
                anchor="mm")

    def _draw_dynamic(self, dr, img, tele, t, w, h, kx, ky):
        rng = self.rng_for(tele)
        x0, x1, yc, hh = self._geom(w, h, ky)
        cx = (x0 + x1) / 2
        v = tele.sample(self.stream, t) if tele else None
        if v is not None:
            vv = max(-rng, min(rng, v))
            x = cx + (vv / rng) * (x1 - x0) / 2
            bar_h = hh - 3 * ky
            if abs(x - cx) > 1:
                dr.rounded_rectangle([min(cx, x), yc - bar_h,
                                      max(cx, x), yc + bar_h],
                                     radius=bar_h,
                                     fill=GREEN_SOFT if v > 0 else ACCENT_SOFT)
        dr.line([cx, yc - hh - 4 * ky, cx, yc + hh + 4 * ky], fill=TEXT,
                width=max(1, int(2 * ky)))
        txt = "---" if v is None else f"{v:+.1f}\u00b0"
        col = DIM if v is None else (GREEN if v > 0 else
                                     (ACCENT if v < 0 else TEXT))
        dr.text((w - 16 * ky, 20 * ky), txt, font=font("monob", 24 * ky),
                fill=col, anchor="rm")


class TrackMap(Gauge):
    """Mini map: full route + progress trail + current position +
    numbered maneuver pointers (T1, G2, ...)."""

    KIND = "track_map"
    DESIGN_W = 260
    DESIGN_H_ = 260
    STRETCH = True
    MAX_PTS = 1200
    TRAIL_PTS = 250

    def __init__(self, stream="track", label="TRACK", **kw):
        super().__init__(stream, label, **kw)
        self._proj = None
        self._proj_key = None

    def available(self, tele):
        return tele is not None and tele.track is not None

    def _face_key(self, tele, w, h):
        n = len(tele.track[0]) if (tele and tele.track) else 0
        return (w, h, id(tele), n)

    def _project(self, tele, w, h, ky):
        """1x-pixel-space projection of the (decimated) track."""
        key = (id(tele), len(tele.track[0]), w, h)
        if key == self._proj_key:
            return self._proj
        ts, lats, lons = tele.track
        stride = max(1, len(ts) // self.MAX_PTS)
        ts = ts[::stride]
        lats = lats[::stride]
        lons = lons[::stride]
        midlat = math.radians((min(lats) + max(lats)) / 2.0)
        cosm = math.cos(midlat)
        xs = [lo * cosm for lo in lons]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(lats), max(lats)
        pad, top = 26 * ky, 36 * ky
        avail_w = w - 2 * pad
        avail_h = h - top - pad
        span_x = max(x1 - x0, 1e-9)
        span_y = max(y1 - y0, 1e-9)
        s = min(avail_w / span_x, avail_h / span_y)
        offx = pad + (avail_w - span_x * s) / 2.0
        offy = top + (avail_h - span_y * s) / 2.0

        def to_pt(lat, lon):
            return offx + (lon * cosm - x0) * s, offy + (y1 - lat) * s

        pts = [to_pt(la, lo) for la, lo in zip(lats, lons)]
        self._proj = (ts, pts, to_pt)
        self._proj_key = key
        return self._proj

    def _draw_face(self, dr, img, tele, w2, h2):
        k2 = h2 / self.DESIGN_H_
        self._panel(dr, w2, h2, k2)
        self._label_txt(dr, k2)
        nx, ny = w2 - 22 * k2, 20 * k2
        dr.polygon([(nx, ny - 8 * k2), (nx - 5 * k2, ny + 5 * k2),
                    (nx + 5 * k2, ny + 5 * k2)], fill=DIM)
        dr.text((nx, ny + 12 * k2), "N", font=font("uib", 10.5 * k2), fill=DIM,
                anchor="mm")
        _, pts, _ = self._project(tele, w2 // SS, h2 // SS, k2 / SS)
        if len(pts) >= 2:
            dr.line([(x * SS, y * SS) for x, y in pts], fill=TRACKLINE,
                    width=max(2, int(2.6 * k2)), joint="curve")

    def _dot(self, ky):
        def build(dr, img, px2, k2):
            c = px2 / 2.0
            r1, r2 = 8.5 * k2, 4.5 * k2
            dr.ellipse([c - r1, c - r1, c + r1, c + r1], outline=ACCENT,
                       width=max(1, int(2 * k2)))
            dr.ellipse([c - r2, c - r2, c + r2, c + r2], fill=TEXT)
        return self._sprite("dot", ky, 24, build)

    def _draw_dynamic(self, dr, img, tele, t, w, h, kx, ky):
        from .telemetry import numbered_maneuvers
        ts, pts, to_pt = self._project(tele, w, h, ky)
        pos = tele.position(t)
        i = bisect.bisect_right(ts, t)
        if i >= 1 and pos is not None:
            trail = pts[:i] + [to_pt(*pos)]
            if len(trail) > self.TRAIL_PTS:
                stride = max(1, len(trail) // self.TRAIL_PTS)
                trail = trail[::stride] + [trail[-1]]
            if len(trail) >= 2:
                dr.line(trail, fill=ACCENT, width=max(2, int(3 * ky)))
        # maneuver pointers
        for m, lbl in numbered_maneuvers(getattr(tele, "maneuvers", [])):
            mpos = tele.position(m["t"])
            if mpos is None:
                continue
            x, y = to_pt(*mpos)
            col = ACCENT if m["kind"] == "T" else GREEN
            r = 3.5 * ky
            dr.ellipse([x - r, y - r, x + r, y + r], fill=col)
            dr.text((x + 5 * ky, y - 4 * ky), lbl, font=font("uib", 10.5 * ky),
                    fill=TEXT, anchor="ls")
        if pos is not None:
            x, y = to_pt(*pos)
            spr = self._dot(ky)
            d = spr.size[0]
            img.alpha_composite(spr, (int(x - d / 2), int(y - d / 2)))


class DigitsBox(Gauge):
    """Plain numeric readout for any stream."""

    KIND = "digits"
    DESIGN_W = 210
    DESIGN_H_ = 92
    STRETCH = True

    def __init__(self, stream="", label="", unit=None, **kw):
        super().__init__(stream, label, **kw)
        self.unit = unit

    def _draw_face(self, dr, img, tele, w2, h2):
        k2 = h2 / self.DESIGN_H_
        self._panel(dr, w2, h2, k2, radius=14)
        self._label_txt(dr, k2)
        unit = self.unit
        if unit is None and tele and self.stream in tele.streams:
            unit = tele.streams[self.stream].unit
        if unit:
            dr.text((w2 - 14 * k2, 16 * k2), unit,
                    font=font("ui", 12 * k2), fill=DIM, anchor="rm")

    def _draw_dynamic(self, dr, img, tele, t, w, h, kx, ky):
        s = tele.streams.get(self.stream) if tele else None
        v = s.sample(t) if s else None
        angular = bool(s and s.angular)
        dr.text((14 * ky, 0.65 * h), fmt_value(v, angular),
                font=font("monob", 37 * ky),
                fill=TEXT if v is not None else DIM, anchor="lm")


GAUGE_KINDS = {c.KIND: c for c in
               (CompassCard, NeedleCompass, SpeedDial, HeelBar, TrackMap,
                DigitsBox)}

MAX_GAUGES = 10


def default_gauges(tele=None):
    """Standard sailing layout: instruments in left/right columns, center clear."""
    gs = [
        CompassCard("heading", "HDG", x=0.022, y=0.035),
        SpeedDial("sog", "SOG", x=0.022, y=0.290),
        HeelBar("roll", "HEEL", x=0.022, y=0.545),
        NeedleCompass("twd", "TWD", x=0.858, y=0.035),
        NeedleCompass("cog", "COG", x=0.858, y=0.290),
        TrackMap(x=0.843, y=0.545),
    ]
    if tele is not None:
        for g in gs:
            g.enabled = g.available(tele)
    return gs


def compose(frame_w, frame_h, gauges, tele, t_data):
    """Render all enabled gauges onto a transparent RGBA canvas."""
    canvas = Image.new("RGBA", (frame_w, frame_h), (0, 0, 0, 0))
    for g in gauges:
        if not g.enabled or not g.available(tele):
            continue
        img = g.render(tele, t_data, frame_h)
        px = max(0, min(frame_w - img.size[0], int(g.x * frame_w)))
        py = max(0, min(frame_h - img.size[1], int(g.y * frame_h)))
        canvas.alpha_composite(img, (px, py))
    return canvas
