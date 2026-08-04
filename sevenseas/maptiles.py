"""Slippy-map tile fetching + mosaics for the track-map background.

Two free tile sources (used lightly, with attribution, per their policies):
  street    — OpenStreetMap standard tiles
  satellite — Esri World Imagery

A mosaic covering the whole track (plus follow-window padding) is fetched
once per (track, style) and cached on disk + in memory; the track gauge
then crops it per frame. Failures are silent — the gauge just renders
without a background.
"""

import io
import math
import os
import threading
import urllib.request

from PIL import Image, ImageEnhance

TILE = 256
MAX_TILES = 140
MAX_ZOOM = 17
TARGET_MPP = 4.5          # ~"10 m or better" per requirement
UA = "7seas-telemetrics/0.3 (open-source desktop overlay tool)"

STYLES = {
    "street": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
               "© OpenStreetMap", 0.55),
    "satellite": ("https://server.arcgisonline.com/ArcGIS/rest/services/"
                  "World_Imagery/MapServer/tile/{z}/{y}/{x}",
                  "© Esri", 0.70),
}

CACHE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "7seas-telemetrics", "tiles")


def tile_xy(lat, lon, z):
    """Fractional slippy tile coordinates."""
    lat = max(-85.05, min(85.05, lat))
    n = 2.0 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def meters_per_pixel(lat, z):
    return 156543.03392 * math.cos(math.radians(lat)) / (2.0 ** z)


def _fetch_tile(style, z, x, y):
    n = 2 ** z
    if not (0 <= y < n):
        return None
    x %= n
    path = os.path.join(CACHE_DIR, style, str(z), f"{x}_{y}.png")
    if os.path.exists(path):
        try:
            return Image.open(path).convert("RGB")
        except OSError:
            pass
    url_t = STYLES[style][0]
    try:
        req = urllib.request.Request(url_t.format(z=z, x=x, y=y),
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = r.read()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None


class Mosaic:
    """A stitched, darkened tile image with geo<->pixel mapping."""

    def __init__(self, style, z, tx0, ty0, img, attribution, midlat):
        self.style = style
        self.z = z
        self.tx0 = tx0
        self.ty0 = ty0
        self.img = img
        self.attribution = attribution
        self.mpp = meters_per_pixel(midlat, z)

    def px(self, lat, lon):
        fx, fy = tile_xy(lat, lon, self.z)
        return (fx - self.tx0) * TILE, (fy - self.ty0) * TILE


def build_mosaic(lat_min, lat_max, lon_min, lon_max, style,
                 target_mpp=TARGET_MPP):
    """Fetch and stitch tiles covering the bbox. Returns Mosaic or None."""
    if style not in STYLES:
        return None
    midlat = (lat_min + lat_max) / 2.0
    z = min(MAX_ZOOM,
            max(4, int(math.log2(156543.03392 * math.cos(math.radians(midlat))
                                 / target_mpp))))
    while z > 4:
        xa, ya = tile_xy(lat_max, lon_min, z)   # north-west corner
        xb, yb = tile_xy(lat_min, lon_max, z)   # south-east corner
        tx0, ty0 = int(xa), int(ya)
        tx1, ty1 = int(xb), int(yb)
        count = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
        if count <= MAX_TILES:
            break
        z -= 1
    img = Image.new("RGB", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE),
                    (26, 26, 26))
    got = 0
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            t = _fetch_tile(style, z, tx, ty)
            if t is not None:
                img.paste(t, ((tx - tx0) * TILE, (ty - ty0) * TILE))
                got += 1
    if got == 0:
        return None
    darken = STYLES[style][2]
    img = ImageEnhance.Brightness(img).enhance(darken)
    img = ImageEnhance.Color(img).enhance(0.85)
    return Mosaic(style, z, tx0, ty0, img, STYLES[style][1], midlat)


class MapService:
    """Caches mosaics per (track-extent, style); builds sync or async."""

    def __init__(self):
        self._cache = {}
        self._pending = set()
        self._lock = threading.Lock()

    @staticmethod
    def _key(track, style, pad_m):
        ts, lats, lons = track
        return (style, round(min(lats), 4), round(max(lats), 4),
                round(min(lons), 4), round(max(lons), 4), int(pad_m))

    @staticmethod
    def _bbox(track, pad_m):
        _, lats, lons = track
        la0, la1 = min(lats), max(lats)
        lo0, lo1 = min(lons), max(lons)
        dlat = pad_m / 111320.0
        dlon = pad_m / (111320.0 * max(0.2, math.cos(math.radians((la0 + la1) / 2))))
        return la0 - dlat, la1 + dlat, lo0 - dlon, lo1 + dlon

    def get(self, track, style, pad_m=800.0):
        """Non-blocking cache lookup. Returns Mosaic or None."""
        if style == "none" or track is None:
            return None
        return self._cache.get(self._key(track, style, pad_m))

    def prepare(self, track, style, pad_m=800.0, callback=None):
        """Build the mosaic. With callback: async in a thread (callback fires
        on success). Without: blocking, returns Mosaic or None."""
        if style == "none" or track is None:
            return None
        key = self._key(track, style, pad_m)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            if callback is not None and key in self._pending:
                return None
            if callback is not None:
                self._pending.add(key)

        def build():
            m = None
            try:
                la0, la1, lo0, lo1 = self._bbox(track, pad_m)
                m = build_mosaic(la0, la1, lo0, lo1, style)
            finally:
                with self._lock:
                    self._pending.discard(key)
                    if m is not None:
                        self._cache[key] = m
            if m is not None and callback is not None:
                callback()
            return m

        if callback is None:
            return build()
        threading.Thread(target=build, daemon=True).start()
        return None


service = MapService()
