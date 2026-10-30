#!/usr/bin/env python3
"""Serve the map locally, with live bus positions from the Bus Open Data Service (BODS), and
shared "how busy is it?" ratings.

BODS needs a free API key (register at https://data.bus-data.dft.gov.uk/) and doesn't let web
pages call it directly, so this small server fetches positions with the key and passes them
to the page. The key stays here; it's never sent to the browser.

    echo YOUR_KEY > .bods_key            # or: export BODS_API_KEY=YOUR_KEY
    python3 tools/live_server.py         # then open http://localhost:8000

Without a key the map still works, showing timetabled positions.

Riders' crowding ratings are kept in .crowding.json (one per rider per journey, for a day and
a half) and shown to everyone using this server.
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import fetch  # noqa: E402  (urllib with a curl fallback for old macOS TLS)

REPO = Path(__file__).resolve().parent.parent
BODS_URL = "https://data.bus-data.dft.gov.uk/api/v1/datafeed/"
GM_BOX = "-2.75,53.32,-1.90,53.70"  # minLongitude,minLatitude,maxLongitude,maxLatitude
REFRESH_SECONDS = 15                # BODS positions update every 10-30 s
SIRI = {"s": "http://www.siri.org.uk/siri"}


def api_key():
    key = os.environ.get("BODS_API_KEY", "").strip()
    if not key and (REPO / ".bods_key").exists():
        key = (REPO / ".bods_key").read_text().strip()
    return key


def parse_siri_vm(xml_bytes):
    """SIRI-VM XML -> compact vehicle list."""
    root = ET.fromstring(xml_bytes)
    vehicles = []
    for va in root.iterfind(".//s:VehicleActivity", SIRI):
        mvj = va.find("s:MonitoredVehicleJourney", SIRI)
        loc = mvj.find("s:VehicleLocation", SIRI) if mvj is not None else None
        if loc is None:
            continue

        def text(path, base=mvj):
            el = base.find(path, SIRI)
            return el.text.strip() if el is not None and el.text else ""

        try:
            lng, lat = float(text("s:Longitude", loc)), float(text("s:Latitude", loc))
        except ValueError:
            continue
        vehicles.append({
            "line": text("s:PublishedLineName") or text("s:LineRef"),
            "operator": text("s:OperatorRef"),
            "direction": text("s:DirectionRef"),
            "originAimed": text("s:OriginAimedDepartureTime"),   # ISO time the journey was due to start
            "destination": text("s:DestinationName"),
            "vehicle": text("s:VehicleRef"),
            "bearing": float(text("s:Bearing")) if text("s:Bearing") else None,
            "recordedAt": text("s:RecordedAtTime", va),
            "lngLat": [round(lng, 6), round(lat, 6)],
        })
    return vehicles


class LiveFeed:
    """Fetches BODS at most every REFRESH_SECONDS, however many pages ask."""

    def __init__(self, key):
        self.key = key
        self.lock = threading.Lock()
        self.payload = None
        self.fetched = 0.0

    def get(self):
        with self.lock:
            if self.payload is None or time.time() - self.fetched > REFRESH_SECONDS:
                try:
                    xml_bytes = fetch(f"{BODS_URL}?boundingBox={GM_BOX}&api_key={self.key}")
                    vehicles = parse_siri_vm(xml_bytes)
                    self.payload = {"ok": True, "fetchedAt": time.time(), "vehicles": vehicles}
                except Exception as e:  # report, and keep serving the last good data if any
                    print(f"BODS fetch failed: {e}", file=sys.stderr)
                    if self.payload is None:
                        return {"ok": False, "error": "Couldn't reach the Bus Open Data Service"}
                self.fetched = time.time()
            return self.payload


class Crowding:
    """Riders' ratings of how busy a journey is: 0 empty, 1 plenty of seats, 2 a few seats,
    3 standing room, 4 packed. One rating per rider per journey; a new one replaces the old."""

    LEVELS = 5

    KEEP_SECONDS = 36 * 3600
    JOURNEY = re.compile(r"^\d{4}-\d{2}-\d{2}\|\d+\|\d+\|-?\d+$")  # date|line|variant|start, as the page makes them
    FILE = REPO / ".crowding-v2.json"  # "-v2": five levels (the first version had four)

    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.ratings = json.loads(self.FILE.read_text())  # journey -> rider -> [level, unix time]
        except (OSError, ValueError):
            self.ratings = {}

    def summary(self, journey):
        given = self.ratings.get(journey, {}).values()
        levels = [0] * self.LEVELS
        for level, _ in given:
            levels[level] += 1
        return {"ok": True, "count": len(given), "levels": levels, "latest": max((t for _, t in given), default=0)}

    def get(self, journey):
        with self.lock:
            return self.summary(journey)

    def rate(self, journey, level, rider):
        now = time.time()
        with self.lock:
            self.ratings.setdefault(journey, {})[rider] = [level, now]
            for j in list(self.ratings):
                self.ratings[j] = {r: v for r, v in self.ratings[j].items() if now - v[1] < self.KEEP_SECONDS}
                if not self.ratings[j]:
                    del self.ratings[j]
            self.FILE.write_text(json.dumps(self.ratings))
            return self.summary(journey)


class Handler(SimpleHTTPRequestHandler):
    feed = None
    crowding = None

    def send_json(self, body, status=200):
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/api/live":
            return self.send_json(self.feed.get() if self.feed else {"ok": False, "error": "No BODS API key configured"})
        if path == "/api/crowding":
            journey = parse_qs(query).get("journey", [""])[0]
            if not Crowding.JOURNEY.match(journey):
                return self.send_json({"ok": False, "error": "Unknown journey"}, 400)
            return self.send_json(self.crowding.get(journey))
        super().do_GET()

    def do_POST(self):
        if self.path != "/api/crowding":
            return self.send_json({"ok": False, "error": "Not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 1000:
                raise ValueError("too long")
            body = json.loads(self.rfile.read(length))
            journey, level = body["journey"], body["level"]
            if not (isinstance(journey, str) and Crowding.JOURNEY.match(journey) and level in range(Crowding.LEVELS)):
                raise ValueError("bad rating")
        except (ValueError, KeyError, TypeError):
            return self.send_json({"ok": False, "error": "Couldn't read that rating"}, 400)
        self.send_json(self.crowding.rate(journey, level, self.client_address[0]))

    def log_message(self, fmt, *args):  # keep the console for errors only
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    key = api_key()
    Handler.feed = LiveFeed(key) if key else None
    Handler.crowding = Crowding()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, directory=str(REPO)))
    print(f"Map: http://localhost:{args.port}  ({'live buses on' if key else 'no BODS key: timetable positions only'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
