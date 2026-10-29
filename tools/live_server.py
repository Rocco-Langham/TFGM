#!/usr/bin/env python3
"""Serve the map locally, with live bus positions from the Bus Open Data Service (BODS).

BODS needs a free API key (register at https://data.bus-data.dft.gov.uk/) and doesn't let web
pages call it directly, so this small server fetches positions with the key and passes them
to the page. The key stays here; it's never sent to the browser.

    echo YOUR_KEY > .bods_key            # or: export BODS_API_KEY=YOUR_KEY
    python3 tools/live_server.py         # then open http://localhost:8000

Without a key the map still works, showing timetabled positions.
"""

import argparse
import json
import os
import sys
import threading
import time
import xml.etree.ElementTree as ET
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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


class Handler(SimpleHTTPRequestHandler):
    feed = None

    def do_GET(self):
        if self.path.split("?")[0] == "/api/live":
            body = self.feed.get() if self.feed else {"ok": False, "error": "No BODS API key configured"}
            data = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()

    def log_message(self, fmt, *args):  # keep the console for errors only
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    key = api_key()
    Handler.feed = LiveFeed(key) if key else None
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, directory=str(REPO)))
    print(f"Map: http://localhost:{args.port}  ({'live buses on' if key else 'no BODS key: timetable positions only'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
