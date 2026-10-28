#!/usr/bin/env python3
"""Build data/interchange-layouts.js: to-scale stand diagrams for bus interchanges.

Sources (both free, no API key):
  - DfT NaPTAN stops   official stand list and labels ("Stand A")
  - OpenStreetMap      (via Overpass) building outline, bus roads, the angled bay each
                       bus parks in, and surrounding streets/buildings for context

Output coordinates are metres in a local frame centred on the stands:
x grows east, y grows south (SVG convention), so the diagram is north-up like the map.

    python3 tools/build_layouts.py
    python3 tools/build_layouts.py --naptan naptan.csv --osm-cache scratch/   # reuse downloads
"""

import argparse
import csv
import io
import json
import math
import subprocess
import time
import urllib.error
import urllib.parse
from pathlib import Path

NAPTAN_URL = "https://naptan.api.dft.gov.uk/v1/access-nodes?dataFormat=csv&atcoAreaCodes=180"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OUT = Path(__file__).resolve().parent.parent / "data" / "interchange-layouts.js"

from common import INTERCHANGES, fetch  # one list of interchanges for every build script

# Display name -> NaPTAN CommonName, LocalityName
LAYOUTS = {name: (common, locality) for name, _borough, common, locality in INTERCHANGES}

MARGIN_M = 30        # context drawn around the outermost stands
BAY_SNAP_M = 4       # a bay way must end this close to its stand
BAY_MAX_LEN_M = 35   # bays are short spurs (Stockport's run 18-30 m); longer is a through road
BUS_LENGTH_M = 12    # a standard single/double decker
CONTEXT_ROADS = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
                 "residential", "service", "primary_link", "secondary_link", "trunk_link"}


def overpass(query, attempts=5):
    """Overpass is a shared free service: pause between queries and back off when it's busy."""
    for attempt in range(attempts):
        time.sleep(2 if attempt == 0 else 15 * 2 ** (attempt - 1))
        try:
            return json.loads(fetch(OVERPASS_URL, urllib.parse.urlencode({"data": query}).encode()))
        except (urllib.error.URLError, subprocess.CalledProcessError, json.JSONDecodeError) as e:
            print(f"  Overpass busy ({e.__class__.__name__}), retrying ...")
    raise SystemExit("Overpass kept failing; try again later")


class Frame:
    """Equirectangular projection to metres around an origin; plenty accurate over ~200 m."""

    def __init__(self, lon0, lat0):
        self.lon0, self.lat0 = lon0, lat0
        self.kx = 111_320 * math.cos(math.radians(lat0))
        self.ky = 110_540

    def xy(self, lon, lat):
        return [round((lon - self.lon0) * self.kx, 1), round(-(lat - self.lat0) * self.ky, 1)]

    def lonlat(self, x, y):
        return self.lon0 + x / self.kx, self.lat0 - y / self.ky


def heading(a, b):
    """Compass heading in degrees from point a to point b (x east, y south)."""
    return round(math.degrees(math.atan2(b[0] - a[0], -(b[1] - a[1]))) % 360, 1)


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def length(pts):
    return sum(dist(p, q) for p, q in zip(pts, pts[1:]))


def point_along(pts, distance):
    """The point `distance` metres along a polyline from its first point."""
    for a, b in zip(pts, pts[1:]):
        seg = dist(a, b)
        if seg >= distance:
            t = distance / seg if seg else 0
            return [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])]
        distance -= seg
    return pts[-1]


def nearest_segment(point, polylines):
    best = None
    for line in polylines:
        for a, b in zip(line, line[1:]):
            ax, ay = b[0] - a[0], b[1] - a[1]
            seg2 = ax * ax + ay * ay or 1e-9
            t = max(0, min(1, ((point[0] - a[0]) * ax + (point[1] - a[1]) * ay) / seg2))
            d = dist(point, (a[0] + t * ax, a[1] + t * ay))
            if best is None or d < best[0]:
                best = (d, a, b)
    return best


def is_bus_road(t):
    """Roads only buses use: bus-station lanes and bays, busways."""
    if t.get("highway") == "busway":
        return True
    if t.get("highway") != "service":
        return False
    return (t.get("service") == "bus"
            or t.get("bus") in ("yes", "designated") and t.get("vehicle", t.get("access")) in ("private", "no")
            or t.get("psv") in ("yes", "designated") and t.get("access", t.get("vehicle")) in ("private", "no"))


HALF_BUS_WIDTH_M = 1.275


def kerbside_heading(pos, bus_roads, roads, naptan_bearing):
    """Which way a bus faces at a kerbside stop.

    UK buses board from the kerb on their left, so the bus lies along the nearest road
    pointing whichever way puts the stand on its left. That also settles two-way roads.
    """
    # Covered lanes under a station canopy are mapped as tunnels; stands sit beside those too.
    for lines, reach in (([w["pts"] for w in bus_roads], 15), ([r["pts"] for r in roads], 20)):
        near = nearest_segment(pos, lines)
        if near and near[0] <= reach:
            _, a, b = near
            h = heading(a, b)
            right = (math.cos(math.radians(h)), math.sin(math.radians(h)))
            # Stand to the right of a->b means the bus must travel b->a instead.
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            side = (pos[0] - mid[0]) * right[0] + (pos[1] - mid[1]) * right[1]
            return h if side < 0 else (h + 180) % 360
    return naptan_bearing  # None when there's nothing to go on: better no bus than a wrong one


def parked_bus(stand):
    """Where to draw a waiting bus: front-centre point and heading (None if unknown)."""
    if stand["heading"] is None:
        return None
    h = math.radians(stand["heading"])
    fwd = (math.sin(h), -math.cos(h))
    right = (math.cos(h), math.sin(h))
    if stand["kind"] == "bay":
        bx, by = stand["bayStart"]
        front = (bx - fwd[0] * 1.2, by - fwd[1] * 1.2)
    else:
        # Front door by the stand pole; body just off the kerb, which is on the bus's left.
        off = HALF_BUS_WIDTH_M + 0.3
        front = (stand["x"] + right[0] * off + fwd[0] * 1.5, stand["y"] + right[1] * off + fwd[1] * 1.5)
    return {"front": [round(front[0], 2), round(front[1], 2)], "heading": stand["heading"]}


def clip_polyline(pts, box):
    """Clip a polyline to an axis-aligned box (Liang-Barsky), returning the pieces inside."""
    x0, y0, x1, y1 = box
    pieces, current = [], []
    for a, b in zip(pts, pts[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
            if p == 0:
                if q < 0:
                    t0, t1 = 1, 0
                    break
            else:
                t = q / p
                if p < 0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
        if t0 > t1:
            if len(current) > 1:
                pieces.append(current)
            current = []
            continue
        start = [round(a[0] + t0 * dx, 1), round(a[1] + t0 * dy, 1)]
        end = [round(a[0] + t1 * dx, 1), round(a[1] + t1 * dy, 1)]
        if not current or current[-1] != start:
            if len(current) > 1:
                pieces.append(current)
            current = [start]
        current.append(end)
        if t1 < 1:
            pieces.append(current)
            current = []
    if len(current) > 1:
        pieces.append(current)
    return pieces


COMPASS = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}


def build(name, common, locality, naptan_rows, osm_cache):
    stands = []
    for r in naptan_rows:
        if (r["CommonName"], r["LocalityName"]) != (common, locality) or r["Status"] != "active":
            continue
        if r["StopType"] not in {"BCS", "BCQ", "BCT"} or r["Indicator"].strip().lower() == "arrivals":
            continue
        stands.append({"atco": r["ATCOCode"], "label": r["Indicator"].strip(),
                       "lon": float(r["Longitude"]), "lat": float(r["Latitude"]),
                       "bearing": COMPASS.get(r["Bearing"].strip().upper())})
    if not stands:
        raise SystemExit(f"No NaPTAN stands for {name}")

    lons = [s["lon"] for s in stands]
    lats = [s["lat"] for s in stands]
    frame = Frame(sum(lons) / len(lons), sum(lats) / len(lats))
    pad_lon, pad_lat = MARGIN_M / frame.kx, MARGIN_M / frame.ky
    bbox = (min(lats) - pad_lat, min(lons) - pad_lon, max(lats) + pad_lat, max(lons) + pad_lon)
    bbox_q = ",".join(f"{v:.6f}" for v in bbox)

    query = f"""[out:json][timeout:90];
(
  nwr["amenity"="bus_station"]["name"="{name}"];
  nwr["amenity"="bus_station"]({bbox_q});
  way["highway"]({bbox_q});
  way["building"]({bbox_q});
  node["naptan:AtcoCode"]({bbox_q});
);
out body geom;"""
    cache = Path(osm_cache) / f"osm-{name.replace(' ', '_')}.json" if osm_cache else None
    if cache and cache.exists():
        osm = json.loads(cache.read_text())
    else:
        print(f"Querying OpenStreetMap for {name} ...")
        osm = overpass(query)
        if cache:
            cache.write_text(json.dumps(osm))

    def pts(geometry):
        return [frame.xy(g["lon"], g["lat"]) for g in geometry if g]

    # OSM stand nodes are drawn to line up with the bays, so prefer them over NaPTAN's point.
    osm_stand = {e["tags"]["naptan:AtcoCode"]: e for e in osm["elements"]
                 if e["type"] == "node" and "naptan:AtcoCode" in e.get("tags", {})}

    building, bus_roads, bay_ways, roads, buildings = [], [], [], [], []
    for e in osm["elements"]:
        t = e.get("tags", {})
        # Outline: the bus station area (or its named building), however it's mapped.
        is_outline = t.get("amenity") == "bus_station" or (t.get("name") == name and "building" in t)
        if e["type"] == "relation" and is_outline:
            building += [pts(m["geometry"]) for m in e.get("members", [])
                         if m.get("role") in ("outer", "inner") and m.get("geometry")]
        if e["type"] != "way" or "geometry" not in e:
            continue
        line = pts(e["geometry"])
        if is_outline:
            building.append(line)
        elif "building" in t:
            buildings.append(line)
        elif is_bus_road(t):
            way = {"pts": line, "tunnel": t.get("tunnel") == "yes", "oneway": t.get("oneway") == "yes"}
            if not way["tunnel"] and not way["oneway"] and length(line) <= BAY_MAX_LEN_M:
                bay_ways.append(way)
            else:
                bus_roads.append(way)
        elif t.get("highway") in CONTEXT_ROADS:
            roads.append({"pts": line, "major": t["highway"] in {"motorway", "trunk", "primary", "secondary",
                                                                  "trunk_link", "primary_link", "secondary_link"}})

    out_stands = []
    used_bays = set()
    for s in sorted(stands, key=lambda s: s["label"]):
        node = osm_stand.get(s["atco"])
        pos = frame.xy(node["lon"], node["lat"]) if node else frame.xy(s["lon"], s["lat"])
        stand = {"atco": s["atco"], "label": s["label"], "short": s["label"].split()[-1], "x": pos[0], "y": pos[1]}

        # Sawtooth bay: the bus parks along it, nose at the stand.
        bay = None
        for i, way in enumerate(bay_ways):
            if i in used_bays:
                continue
            ends = (way["pts"][0], way["pts"][-1])
            near = min(ends, key=lambda p: dist(p, pos))
            if dist(near, pos) <= BAY_SNAP_M:
                line = way["pts"] if near is ends[0] else way["pts"][::-1]  # start at the stand
                bay = (i, line)
                break
        if bay:
            used_bays.add(bay[0])
            stand["kind"] = "bay"
            # Bays bend into the road further out; the parked bus sits on the straight part.
            stand["heading"] = heading(point_along(bay[1], BUS_LENGTH_M), bay[1][0])  # faces the platform
            stand["bayStart"] = bay[1][0]  # buses sit on the bay's centreline, not the stand pole
        else:
            stand["kind"] = "kerb"
            stand["heading"] = kerbside_heading(pos, bus_roads, roads, s["bearing"])
        stand["_bearing"] = s["bearing"]
        out_stands.append(stand)

    # Sawtooth bays at one station all point the same way. A "bay" far off that direction is
    # really a stray short road piece next to a kerbside stand (e.g. Bolton's Stand G).
    bay_stands = [st for st in out_stands if st["kind"] == "bay"]
    if len(bay_stands) >= 3:
        mean = math.degrees(math.atan2(sum(math.sin(math.radians(st["heading"])) for st in bay_stands),
                                       sum(math.cos(math.radians(st["heading"])) for st in bay_stands)))
        for st in bay_stands:
            if abs((st["heading"] - mean + 180) % 360 - 180) > 40:
                st["kind"] = "kerb"
                del st["bayStart"]
                st["heading"] = kerbside_heading([st["x"], st["y"]], bus_roads, roads, st["_bearing"])
    for st in out_stands:
        del st["_bearing"]
        st["bus"] = parked_bus(st)

    # The outline only counts towards the view where it's near the stands; some bus station
    # areas are mapped generously.
    stand_pts = [[s["x"], s["y"]] for s in out_stands]
    near_pts = [p for ring in building for p in ring
                if min(dist(p, q) for q in stand_pts) <= 2 * MARGIN_M]
    xs, ys = [p[0] for p in stand_pts + near_pts], [p[1] for p in stand_pts + near_pts]
    view = [round(min(xs) - MARGIN_M / 2), round(min(ys) - MARGIN_M / 2),
            round(max(xs) - min(xs) + MARGIN_M), round(max(ys) - min(ys) + MARGIN_M)]

    # Long roads run kilometres past the station: keep only what's in and around the diagram.
    clip = (view[0] - MARGIN_M, view[1] - MARGIN_M, view[0] + view[2] + MARGIN_M, view[1] + view[3] + MARGIN_M)
    roads = [{**r, "pts": piece} for r in roads for piece in clip_polyline(r["pts"], clip)]
    bus_roads = [{**w, "pts": piece} for w in bus_roads for piece in clip_polyline(w["pts"], clip)]
    buildings = [b for b in buildings if all(clip[0] <= x <= clip[2] and clip[1] <= y <= clip[3] for x, y in b)]

    print(f"{name}: {len(out_stands)} stands ({sum(s['kind'] == 'bay' for s in out_stands)} in bays), "
          f"{len(building)} building rings, {len(bus_roads)} bus roads, {len(roads)} context roads")
    return {
        "origin": [round(frame.lon0, 6), round(frame.lat0, 6)],
        "viewBox": view,
        "building": building,
        "busRoads": [{"pts": w["pts"], "tunnel": w["tunnel"]} for w in bus_roads],
        "bays": [w["pts"] for i, w in enumerate(bay_ways) if i in used_bays],
        "roads": roads,
        "buildings": buildings,
        "stands": out_stands,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--naptan", default=NAPTAN_URL, help="NaPTAN CSV path or URL")
    ap.add_argument("--osm-cache", help="directory to cache Overpass responses in")
    args = ap.parse_args()

    raw = Path(args.naptan).read_bytes() if Path(args.naptan).exists() else fetch(args.naptan)
    naptan_rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))

    layouts = {name: build(name, common, loc, naptan_rows, args.osm_cache)
               for name, (common, loc) in LAYOUTS.items()}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(
        "// Generated by tools/build_layouts.py from NaPTAN + OpenStreetMap (ODbL). Do not edit by hand.\n"
        f"window.GM_INTERCHANGE_LAYOUTS = {json.dumps(layouts, separators=(',', ':'))};\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUT} ({OUT.stat().st_size / 1e3:.0f} kB)")


if __name__ == "__main__":
    main()
