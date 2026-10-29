"""Snap bus route shapes onto OpenStreetMap roads, in the lane a bus would use.

TfGM's route shapes follow the roads only roughly: a few metres either side of the centre line,
and sometimes on the other carriageway of a dual carriageway. That shows once roads are drawn at
their real width. This moves each shape point onto the nearest road running the same way (a
one-way road only counts in its own direction, so each carriageway of a dual carriageway gets
the buses going its way) and, on a two-way road, over into the left-hand lane.

Roads come from OpenFreeMap's vector tiles at zoom 14, the same data the map is drawn from.
Tiles are kept in tools/.cache between runs, per OpenFreeMap release.
"""

import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import fetch

TILEJSON = "https://tiles.openfreemap.org/planet"
ZOOM = 14
CACHE = Path(__file__).resolve().parent / ".cache" / "roads"

# Roads a bus might use, and their usual width in metres (as the map draws them).
ROAD_WIDTH_M = {"motorway": 22, "trunk": 14, "primary": 13, "secondary": 11, "tertiary": 9,
                "minor": 7, "service": 4.5, "busway": 7, "bus_guideway": 7}
SNAP_M = 20             # a road further than this from the shape isn't the one it's on
MAX_TURN_DEG = 50       # and nor is one heading more than this far off the shape's direction
SAME_ROAD_BONUS_M = 3   # staying on the road it was on beats a slightly nearer one
DIRECTION_SPAN_M = 15   # the shape's direction is taken over this far either side

M_PER_DEG_LAT = 110_540


# ---- Reading vector tiles (Mapbox Vector Tile protobufs) ------------------------------------

def _varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7


def _fields(buf):
    """(field number, wire type, value) for each field of a protobuf message."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            length, i = _varint(buf, i)
            value, i = buf[i:i + length], i + length
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield field, wire, value


def _packed(buf):
    out, i = [], 0
    while i < len(buf):
        v, i = _varint(buf, i)
        out.append(v)
    return out


def _value(buf):
    for field, _, v in _fields(buf):
        if field == 1:
            return bytes(v).decode()
        if field in (4, 5):
            return v
        if field == 6:
            return (v >> 1) ^ -(v & 1)
        if field == 7:
            return bool(v)
    return None


def _lines(geometry):
    """Linestrings, in tile units, from a feature's geometry commands."""
    out, x, y, i, current = [], 0, 0, 0, None
    while i < len(geometry):
        cmd, count = geometry[i] & 7, geometry[i] >> 3
        i += 1
        if cmd == 7:  # ClosePath
            continue
        for _ in range(count):
            dx, dy = geometry[i], geometry[i + 1]
            i += 2
            x += (dx >> 1) ^ -(dx & 1)
            y += (dy >> 1) ^ -(dy & 1)
            if cmd == 1:  # MoveTo starts a new line
                current = [(x, y)]
                out.append(current)
            else:
                current.append((x, y))
    return [ln for ln in out if len(ln) >= 2]


def tile_roads(data, tx, ty):
    """(class, oneway, [(lon, lat), ...]) for each road in a zoom-14 tile."""
    roads = []
    for field, _, layer in _fields(data):
        if field != 3:
            continue
        name, keys, values, features, extent = None, [], [], [], 4096
        for f, _, v in _fields(layer):
            if f == 1:
                name = bytes(v).decode()
            elif f == 2:
                features.append(v)
            elif f == 3:
                keys.append(bytes(v).decode())
            elif f == 4:
                values.append(_value(v))
            elif f == 5:
                extent = v
        if name != "transportation":
            continue
        n = 2 ** ZOOM
        for feature in features:
            tags, geometry, kind = {}, [], 0
            for f, _, v in _fields(feature):
                if f == 2:
                    t = _packed(v)
                    tags = {keys[t[k]]: values[t[k + 1]] for k in range(0, len(t), 2)}
                elif f == 3:
                    kind = v
                elif f == 4:
                    geometry = _packed(v)
            if kind != 2 or tags.get("class") not in ROAD_WIDTH_M:
                continue
            for line in _lines(geometry):
                pts = []
                for px, py in line:
                    lon = (tx + px / extent) / n * 360 - 180
                    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + py / extent) / n))))
                    pts.append((lon, lat))
                roads.append((tags["class"], int(tags.get("oneway") or 0), pts))
    return roads


def tile_of(lon, lat):
    n = 2 ** ZOOM
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def load_roads(tiles):
    """Roads in the given zoom-14 tiles, downloading any not already cached."""
    template = json.loads(fetch(TILEJSON))["tiles"][0]
    release = template.split("/")[-4]  # e.g. 20261025_080012_pt
    folder = CACHE / release
    folder.mkdir(parents=True, exist_ok=True)

    def get(tile):
        path = folder / f"{tile[0]}_{tile[1]}.pbf"
        if not path.exists():
            url = template.replace("{z}", str(ZOOM)).replace("{x}", str(tile[0])).replace("{y}", str(tile[1]))
            path.write_bytes(fetch(url, quiet=True))
        return tile_roads(path.read_bytes(), *tile)

    missing = sum(not (folder / f"{x}_{y}.pbf").exists() for x, y in tiles)
    print(f"Road tiles: {len(tiles)} ({missing} to download)")
    with ThreadPoolExecutor(8) as pool:
        return [road for roads in pool.map(get, sorted(tiles)) for road in roads]


# ---- Snapping ---------------------------------------------------------------------------------

class RoadSnapper:
    """Snaps shape points onto roads. Coordinates inside are metres east/north of a fixed point."""

    CELL = 40  # metres

    def __init__(self, roads, lat0):
        self.kx = 111_320 * math.cos(math.radians(lat0))
        self.grid = {}
        self.segments = []  # (ax, ay, bx, by, class, oneway, road id)
        for rid, (cls, oneway, pts) in enumerate(roads):
            if oneway == -1:
                pts, oneway = pts[::-1], 1
            xy = [self.to_xy(p) for p in pts]
            for (ax, ay), (bx, by) in zip(xy, xy[1:]):
                if ax == bx and ay == by:
                    continue
                s = len(self.segments)
                self.segments.append((ax, ay, bx, by, cls, oneway, rid))
                for cx in range(int(min(ax, bx) - SNAP_M) // self.CELL, int(max(ax, bx) + SNAP_M) // self.CELL + 1):
                    for cy in range(int(min(ay, by) - SNAP_M) // self.CELL, int(max(ay, by) + SNAP_M) // self.CELL + 1):
                        self.grid.setdefault((cx, cy), []).append(s)

    def to_xy(self, p):
        return p[0] * self.kx, p[1] * M_PER_DEG_LAT

    def to_lonlat(self, x, y):
        return x / self.kx, y / M_PER_DEG_LAT

    def snap_point(self, x, y, hx, hy, previous_road):
        """Best spot for a bus at (x, y) heading along unit vector (hx, hy), or None."""
        best, best_score = None, None
        for s in self.grid.get((int(x) // self.CELL, int(y) // self.CELL), ()):
            ax, ay, bx, by, cls, oneway, rid = self.segments[s]
            dx, dy = bx - ax, by - ay
            length = math.hypot(dx, dy)
            ux, uy = dx / length, dy / length
            along = ux * hx + uy * hy  # cos of the angle between road and travel
            if oneway and along < math.cos(math.radians(MAX_TURN_DEG)):
                continue
            if not oneway and abs(along) < math.cos(math.radians(MAX_TURN_DEG)):
                continue
            t = max(0.0, min(length, (x - ax) * ux + (y - ay) * uy))
            px, py = ax + ux * t, ay + uy * t
            off = math.hypot(x - px, y - py)
            if off > SNAP_M:
                continue
            turn = math.degrees(math.acos(max(-1.0, min(1.0, abs(along)))))
            score = off + 0.1 * turn - (SAME_ROAD_BONUS_M if rid == previous_road else 0)
            if best_score is None or score < best_score:
                if along < 0:  # two-way road drawn the other way: travel runs against its direction
                    ux, uy = -ux, -uy
                lane = 0 if oneway else ROAD_WIDTH_M[cls] / 4
                # Keep left: the lane's middle is a quarter of the road's width left of centre.
                best = (px - uy * lane, py + ux * lane, rid)
                best_score = score
        return best

    def snap_path(self, path):
        """The path (list of (lon, lat)) moved onto its roads, point for point."""
        xy = [self.to_xy(p) for p in path]
        n = len(xy)
        # Distance along the path, to find each point's direction over DIRECTION_SPAN_M.
        cum = [0.0]
        for (ax, ay), (bx, by) in zip(xy, xy[1:]):
            cum.append(cum[-1] + math.hypot(bx - ax, by - ay))
        out, previous_road, lo, hi = [], None, 0, 0
        for i, (x, y) in enumerate(xy):
            while cum[lo] < cum[i] - DIRECTION_SPAN_M:
                lo += 1
            hi = max(hi, i)
            while hi + 1 < n and cum[hi] < cum[i] + DIRECTION_SPAN_M:
                hi += 1
            hx, hy = xy[hi][0] - xy[lo][0], xy[hi][1] - xy[lo][1]
            h = math.hypot(hx, hy)
            snapped = self.snap_point(x, y, hx / h, hy / h, previous_road) if h > 1 else None
            if snapped is None:
                out.append(path[i])
                continue
            sx, sy, previous_road = snapped
            out.append(self.to_lonlat(sx, sy))
        return out
