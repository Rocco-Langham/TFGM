#!/usr/bin/env python3
"""Build every timetable data file from TfGM's GTFS timetable and DfT's NaPTAN stop list.

  data/meta.js             lines (route number, name, operator, bus/tram), destinations, calendars
  data/bus-departures.js   the 17 interchanges: stands and departures (loaded with the page)
  data/stops.js            every other bus stop and Metrolink platform, and clusters of lettered stops
  data/stops/<cell>.js     departures from the stops in each ~2 km cell (loaded when one is clicked)
  data/lines/<line>.js     each line's journeys: stops, path and timings (loaded to draw a route)
  data/metrolink.js        tram stations and the coloured network

Every departure points at a journey "variant" in its line's file plus its position along it,
so any bus or tram, from any stop, can show the rest of its route.

TfGM's calendars run only a few weeks ahead, so rebuild weekly (tools/refresh_data.sh does):

    python3 tools/build_timetable.py
    python3 tools/build_timetable.py --gtfs TfGMgtfsnew.zip --naptan naptan.csv   # reuse downloads
"""

import argparse
import bisect
import io
import json
import math
import re
import shutil
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from common import (BOARDING_TYPES, DATA, GTFS_URL, INTERCHANGES, NAPTAN_URL, fetch, gtfs_seconds, read_csv, slug,
                    write_js)

CELL_LON, CELL_LAT = 0.03, 0.02      # ~2 km x 2.2 km departure files
CLUSTER_RADIUS_M = 200               # same-named stops this close count as one place
PATH_TOLERANCE_M = 6                 # route paths are simplified to within this of the real line
LETTERED = re.compile(r"^(Stop|Stand|Stance)\s+[A-Z0-9]{1,3}$", re.I)
COMPASS = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}
TRAM_COLOURS = {  # Metrolink's line colours
    "Blue Line": "#0b78d0", "Green Line": "#3a9d3a", "Navy Line": "#1f2f7a", "Pink Line": "#e2457a",
    "Purple Line": "#7b3fa0", "Red Line": "#d6312b", "Yellow Line": "#f2c500",
}
# Bee Network routes are franchised: TfGM's timetable names them all "Bee Network", and tells
# the companies running them apart by National Operator Code.
BEE_NETWORK_COMPANIES = {
    "BNDB": "Diamond", "BNFM": "First Manchester", "BNGN": "Go North West",
    "BNML": "Metroline", "BNSM": "Stagecoach", "BNVB": "Vision Bus",
}
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def metres(a, b):
    k = 111_320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((a[0] - b[0]) * k, (a[1] - b[1]) * 110_540)


def simplify(points, tolerance):
    """Douglas-Peucker on [x, y] points; returns the indexes kept."""
    if len(points) < 3:
        return list(range(len(points)))
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = points[a], points[b]
        dx, dy = bx - ax, by - ay
        seg = math.hypot(dx, dy)
        far, far_d = None, tolerance
        for k in range(a + 1, b):
            px, py = points[k][0] - ax, points[k][1] - ay
            # A loop that ends where it starts has no line to measure from: use the distance
            # from that point instead, or every point in between would be dropped.
            d = abs(dy * px - dx * py) / seg if seg > 1e-9 else math.hypot(px, py)
            if d > far_d:
                far, far_d = k, d
        if far is not None:
            keep.add(far)
            stack += [(a, far), (far, b)]
    return sorted(keep)


def clusters_of(stops):
    """Same-named bus stops within CLUSTER_RADIUS_M of each other with 2+ lettered stands."""
    by_name = defaultdict(list)
    for i, s in enumerate(stops):
        if s["mode"] == "bus":
            by_name[(s["name"], s["locality"])].append(i)
    out = []
    for members in by_name.values():
        unvisited = set(members)
        while unvisited:
            group, frontier = set(), [unvisited.pop()]
            while frontier:
                i = frontier.pop()
                group.add(i)
                near = {j for j in unvisited if metres(stops[i]["pos"], stops[j]["pos"]) <= CLUSTER_RADIUS_M}
                unvisited -= near
                frontier += near
            # A pair either side of the road isn't a multi-stand stop; lettered stands are.
            if sum(bool(LETTERED.match(stops[i]["indicator"])) for i in group) >= 2:
                out.append(sorted(group, key=lambda i: stops[i]["indicator"]))
    return out


def clean(name):
    """Tram stops and destinations come as "Bury (Manchester Metrolink)"; the map says Metrolink already."""
    return name.replace(" (Manchester Metrolink)", "")


class Table:
    """Strings stored once and referred to by index."""

    def __init__(self):
        self.items, self.index = [], {}

    def __call__(self, value):
        if value not in self.index:
            self.index[value] = len(self.items)
            self.items.append(value)
        return self.index[value]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gtfs", default=GTFS_URL, help="GTFS zip path or URL")
    ap.add_argument("--naptan", default=NAPTAN_URL, help="NaPTAN CSV path or URL")
    ap.add_argument("--out", default=str(DATA), help="folder to write into (default: data/)")
    args = ap.parse_args()
    out = Path(args.out)

    # ---- NaPTAN: interchange stands, other bus stops, towns ------------------------------
    wanted = {(common, loc): i for i, (_, _, common, loc) in enumerate(INTERCHANGES)}
    stand_of = {}                      # ATCO -> (interchange, stand label)
    ix_coords = defaultdict(list)
    small = {}                         # ATCO -> NaPTAN row, for every other bus stop
    naptan = {}
    for r in read_csv(fetch(args.naptan)):
        naptan[r["ATCOCode"]] = r
        if r["Status"] != "active" or r["StopType"] not in BOARDING_TYPES:
            continue
        if r["Indicator"].strip().lower() == "arrivals":
            continue
        i = wanted.get((r["CommonName"], r["LocalityName"]))
        if i is not None:
            stand_of[r["ATCOCode"]] = (i, r["Indicator"].strip())
            ix_coords[i].append((float(r["Longitude"]), float(r["Latitude"])))
        else:
            small[r["ATCOCode"]] = r
    missing = [INTERCHANGES[i][0] for i in range(len(INTERCHANGES)) if i not in ix_coords]
    if missing:
        raise SystemExit(f"No NaPTAN stands found for: {', '.join(missing)}")

    # ---- GTFS reference tables -------------------------------------------------------------
    gtfs = zipfile.ZipFile(io.BytesIO(fetch(args.gtfs)))
    stop_info = {r["stop_id"]: r for r in read_csv(gtfs, "stops.txt")}
    agency_rows = {r["agency_id"]: r for r in read_csv(gtfs, "agency.txt")}
    agencies = {a: r["agency_name"] for a, r in agency_rows.items()}
    # The company that runs each agency's buses: its own name, except that Bee Network
    # (franchised) routes are run by different companies, told apart by operator code.
    companies = {a: BEE_NETWORK_COMPANIES.get(r.get("agency_noc", ""), r["agency_name"])
                 for a, r in agency_rows.items()}
    routes = {r["route_id"]: r for r in read_csv(gtfs, "routes.txt")}
    trips = {r["trip_id"]: r for r in read_csv(gtfs, "trips.txt")}

    def is_tram(route):
        return route["route_type"] == "0" and route["route_short_name"] != "Replacement Bus"

    tram_routes = {rid for rid, r in routes.items() if is_tram(r)}
    lines = Table()                    # route_id -> line index
    headsigns = Table()
    services = Table()

    ix_stand = {}                      # GTFS stop_id -> (interchange, stand label)
    stop_key = {}                      # GTFS stop_id -> key in `stop_deps`
    for sid, r in stop_info.items():
        if r["stop_code"] in stand_of:
            ix_stand[sid] = stand_of[r["stop_code"]]
        elif r["stop_code"] in small:
            stop_key[sid] = sid

    # ---- One pass over stop_times, a trip at a time ----------------------------------------
    patterns = defaultdict(dict)       # line -> (shape, stops) -> pattern index
    pattern_info = defaultdict(dict)   # line -> pattern -> {"shape", "stops", "dists", "trips", "dirs", "heads"}
    variants = defaultdict(dict)       # line -> (pattern, offsets) -> variant index
    ix_deps = defaultdict(set)
    stop_deps = defaultdict(lambda: defaultdict(list))  # stop_id -> (line, headsign, service) -> [(minute, variant, pos)]
    tram_stop_ids = set()
    tram_trips = set()

    def finish_trip(trip_id, rows):
        # rows: (stop_id, departure secs, shape distance or None, pickup_type, stop_headsign)
        trip = trips[trip_id]
        route_id = trip["route_id"]
        line = lines(route_id)
        key = (trip["shape_id"], tuple(r[0] for r in rows))
        p = patterns[line].setdefault(key, len(patterns[line]))
        info = pattern_info[line].setdefault(p, {"shape": trip["shape_id"], "stops": key[1],
                                                 "dists": [r[2] for r in rows], "trips": 0,
                                                 "dirs": Counter(), "heads": Counter()})
        info["trips"] += 1
        info["dirs"][trip["direction_id"]] += 1
        info["heads"][clean(trip["trip_headsign"])] += 1
        offsets = tuple(round((r[1] - rows[0][1]) / 60) for r in rows)
        v = variants[line].setdefault((p, offsets), len(variants[line]))
        svc = services(trip["service_id"])
        if route_id in tram_routes:
            tram_stop_ids.update(r[0] for r in rows)
            tram_trips.add((line, v, rows[0][1], svc))  # every tram, to draw the whole network moving
        for pos, (sid, secs, _dist, pickup, stop_headsign) in enumerate(rows[:-1]):  # nobody boards at the end
            if pickup == "1":
                continue
            h = headsigns(clean(stop_headsign or trip["trip_headsign"]))
            if sid in ix_stand:
                i, stand = ix_stand[sid]
                ix_deps[i].add((secs, line, h, svc, stand, v, pos))
            elif sid in stop_key or route_id in tram_routes:
                stop_deps[sid][(line, h, svc)].append((secs // 60, v, pos))

    print("Scanning stop_times.txt ...")
    done, current, rows, last_seq = set(), None, [], -1
    for r in read_csv(gtfs, "stop_times.txt"):
        if r["trip_id"] != current:
            if current is not None:
                finish_trip(current, rows)
            if r["trip_id"] in done:
                raise SystemExit("stop_times.txt is not grouped by trip; this script relies on that ordering")
            done.add(current)
            current, rows, last_seq = r["trip_id"], [], -1
        seq = int(r["stop_sequence"])
        if seq < last_seq:
            raise SystemExit(f"stop_times.txt rows for trip {current} are not in stop_sequence order")
        last_seq = seq
        dist = float(r["shape_dist_traveled"]) if r["shape_dist_traveled"] else None
        rows.append((r["stop_id"], gtfs_seconds(r["departure_time"]), dist, r["pickup_type"], r["stop_headsign"]))
    if current is not None:
        finish_trip(current, rows)

    # ---- Line files: each journey pattern's path, stops and timings ------------------------
    print("Reading shapes.txt ...")
    used_shapes = {info["shape"] for pats in pattern_info.values() for info in pats.values()}
    shapes = defaultdict(list)
    for r in read_csv(gtfs, "shapes.txt"):
        if r["shape_id"] in used_shapes:
            dist = float(r["shape_dist_traveled"]) if r["shape_dist_traveled"] else None
            shapes[r["shape_id"]].append((int(r["shape_pt_sequence"]), dist, float(r["shape_pt_lon"]), float(r["shape_pt_lat"])))
    for pts in shapes.values():
        pts.sort()

    def town(info):
        loc = naptan.get(info["stop_code"], {}).get("LocalityName", "")
        return "" if loc in info["stop_name"] else loc

    shutil.rmtree(out / "lines", ignore_errors=True)
    line_meta = []
    tram_paths = defaultdict(list)
    for route_id in lines.items:
        line = lines.index[route_id]
        route = routes[route_id]
        stop_table = Table()
        out_patterns = []
        for p in range(len(patterns[line])):
            info = pattern_info[line][p]
            stop_lnglat = [(float(stop_info[s]["stop_lon"]), float(stop_info[s]["stop_lat"])) for s in info["stops"]]
            pts = shapes.get(info["shape"], [])
            dists = info["dists"]
            usable = len(pts) >= 2 and pts[0][1] is not None and None not in (dists[0], dists[-1])
            if usable:
                pts = [q for q in pts if dists[0] - 1 <= q[1] <= dists[-1] + 1]
                usable = len(pts) >= 2
            if usable:
                path = [(q[2], q[3]) for q in pts]
                path_dists = [q[1] for q in pts]
            else:  # no usable shape: join the stops
                path, path_dists = stop_lnglat, None
            lat0 = path[0][1]
            xy = [(lon * 111_320 * math.cos(math.radians(lat0)), lat * 110_540) for lon, lat in path]
            kept = simplify(xy, PATH_TOLERANCE_M)
            # Where each stop's onward path starts, as an index into the simplified path.
            if path_dists is not None and None not in dists:
                cuts = [max(0, bisect.bisect_right(kept, bisect.bisect_left(path_dists, d - 1)) - 1) for d in dists]
            else:
                cuts = list(range(len(dists))) if path_dists is None else [0] * len(dists)
            simple = [[round(path[k][0], 5), round(path[k][1], 5)] for k in kept]
            stops_out = []
            for s in info["stops"]:
                si = stop_info[s]
                stops_out.append(stop_table((clean(si["stop_name"]), town(si),
                                             round(float(si["stop_lon"]), 5), round(float(si["stop_lat"]), 5))))
            out_patterns.append({"path": simple, "cuts": cuts, "stops": stops_out, "trips": info["trips"],
                                 "dir": info["dirs"].most_common(1)[0][0], "to": info["heads"].most_common(1)[0][0]})
            if route_id in tram_routes:
                tram_paths[route["route_short_name"]].append((info["trips"], simple))
        out_variants = [None] * len(variants[line])
        for (p, offsets), v in variants[line].items():
            out_variants[v] = [p, *offsets]
        file = slug(route_id)
        write_js(out / "lines" / f"{file}.js", f"(window.GM_LINES ??= {{}})[{json.dumps(file)}]",
                 {"stops": [list(s) for s in stop_table.items], "patterns": out_patterns, "variants": out_variants})
        mode = "tram" if route_id in tram_routes else "bus"
        line_meta.append([route["route_short_name"], route["route_long_name"], agencies.get(route["agency_id"], ""),
                          mode, file, TRAM_COLOURS.get(route["route_short_name"], "") if mode == "tram" else "",
                          companies.get(route["agency_id"], "")])

    # ---- Calendars ------------------------------------------------------------------------
    cal = {}
    used = set(services.items)
    for r in read_csv(gtfs, "calendar.txt"):
        if r["service_id"] in used:
            cal[r["service_id"]] = {"days": "".join(r[d] for d in DAYS), "start": int(r["start_date"]),
                                    "end": int(r["end_date"]), "add": [], "remove": []}
    for r in read_csv(gtfs, "calendar_dates.txt"):
        if r["service_id"] in used:
            c = cal.setdefault(r["service_id"], {"days": "0000000", "start": 0, "end": 0, "add": [], "remove": []})
            c["add" if r["exception_type"] == "1" else "remove"].append(int(r["date"]))

    write_js(out / "meta.js", "window.GM_META", {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        # [route number, name, operator, "bus" | "tram", file under data/lines/, tram colour,
        #  company running it]
        "lines": line_meta,
        "headsigns": headsigns.items,
        "services": [cal.get(s, {"days": "0000000", "start": 0, "end": 0, "add": [], "remove": []})
                     for s in services.items],
    })

    # ---- Interchanges ---------------------------------------------------------------------
    out_ix = []
    for i, (name, borough, _, _) in enumerate(INTERCHANGES):
        pts = ix_coords[i]
        stands = Table()
        deps = sorted((secs, line, h, svc, stands(stand), v, pos) for secs, line, h, svc, stand, v, pos in ix_deps[i])
        out_ix.append({
            "name": name, "borough": borough,
            "lngLat": [round(sum(p[0] for p in pts) / len(pts), 5), round(sum(p[1] for p in pts) / len(pts), 5)],
            "stands": stands.items,
            # [seconds after service-day midnight, line, headsign, service, stand, variant, position]
            "departures": [list(d) for d in sorted(deps)],
        })
    write_js(out / "bus-departures.js", "window.GM_BUS_DATA", {"interchanges": out_ix})

    # ---- Other stops and tram platforms ---------------------------------------------------
    stops = []
    for sid, groups in stop_deps.items():
        si = stop_info[sid]
        row = small.get(si["stop_code"]) or naptan.get(si["stop_code"], {})
        tram = sid in tram_stop_ids and si["stop_code"] not in small
        stops.append({
            "sid": sid, "mode": "tram" if tram else "bus",
            "name": clean(row.get("CommonName") or si["stop_name"]),
            "indicator": "" if tram else row.get("Indicator", "").strip(),
            "locality": row.get("LocalityName", ""),
            "pos": (float(si["stop_lon"]), float(si["stop_lat"])),
            "bearing": None if tram else COMPASS.get(row.get("Bearing", "").strip().upper()),
        })
    stops.sort(key=lambda s: (s["mode"], s["locality"], s["name"], s["indicator"]))
    clusters = clusters_of(stops)
    cluster_of = {i: c for c, members in enumerate(clusters) for i in members}

    localities, cells = Table(), Table()
    out_stops = []
    by_cell = defaultdict(dict)
    for i, s in enumerate(stops):
        lon, lat = s["pos"]
        cell = f"{math.floor(lon / CELL_LON)}_{math.floor(lat / CELL_LAT)}"
        out_stops.append([s["name"], s["indicator"], round(lon, 5), round(lat, 5), s["bearing"],
                          localities(s["locality"]), cells(cell), cluster_of.get(i, -1), 1 if s["mode"] == "tram" else 0])
        groups = []
        for (line, h, svc), deps in sorted(stop_deps[s["sid"]].items()):
            deps.sort()
            minutes = [d[0] for d in deps]
            runs = []  # [variant, position, count, ...]: consecutive departures usually share a journey shape
            for _minute, v, pos in deps:
                if runs and runs[-3] == v and runs[-2] == pos:
                    runs[-1] += 1
                else:
                    runs += [v, pos, 1]
            groups.append([line, h, svc, [minutes[0], *[b - a for a, b in zip(minutes, minutes[1:])]], runs])
        by_cell[cell][i] = groups

    write_js(out / "stops.js", "window.GM_STOPS", {
        "localities": localities.items, "cells": cells.items,
        # [name, indicator, lng, lat, bearing or null, locality, cell, cluster or -1, 1 if tram]
        "stops": out_stops,
        # [name, locality, [stop indexes]]
        "clusters": [[stops[m[0]]["name"], localities.index[stops[m[0]]["locality"]], m] for m in clusters],
    })
    shutil.rmtree(out / "stops", ignore_errors=True)
    for cell, deps in by_cell.items():
        # Per stop: [line, headsign, service, [first minute, gaps...], [variant, position, count, ...]]
        write_js(out / "stops" / f"{cell}.js", f"(window.GM_STOP_DEPS ??= {{}})[{json.dumps(cell)}]", deps)

    # ---- Metrolink: stations (platforms grouped by name) and the network ------------------
    stations = defaultdict(list)
    for i, s in enumerate(stops):
        if s["mode"] == "tram":
            stations[s["name"]].append(i)
    network = []
    for name, paths in sorted(tram_paths.items()):
        # Every pattern, even rare short workings: a tram must never appear off the drawn track.
        unique = {json.dumps(p): p for _, p in paths}  # many patterns run along identical track
        network.append({"name": name, "colour": TRAM_COLOURS.get(name, "#555555"), "paths": list(unique.values())})
    write_js(out / "metrolink.js", "window.GM_METROLINK", {
        "stations": [[name, round(sum(stops[i]["pos"][0] for i in idx) / len(idx), 5),
                      round(sum(stops[i]["pos"][1] for i in idx) / len(idx), 5), idx]
                     for name, idx in sorted(stations.items())],
        "network": network,
        # [line, journey variant, start (seconds after service-day midnight), service]
        "trips": sorted(list(t) for t in tram_trips),
    })

    # ---- Summary --------------------------------------------------------------------------
    def size(path):
        return sum(f.stat().st_size for f in path.rglob("*.js")) if path.is_dir() else path.stat().st_size

    print(f"{len(lines.items)} lines, {sum(len(p) for p in patterns.values())} journey patterns, "
          f"{sum(len(v) for v in variants.values())} timing variants")
    print(f"{sum(len(d) for d in ix_deps.values())} interchange departures; "
          f"{sum(s['mode'] == 'bus' for s in stops)} other bus stops ({len(clusters)} clusters); "
          f"{len(stations)} tram stations")
    for name in ("meta.js", "bus-departures.js", "stops.js", "metrolink.js", "stops", "lines"):
        print(f"  {name:20s} {size(out / name) / 1e6:6.2f} MB")


if __name__ == "__main__":
    main()
