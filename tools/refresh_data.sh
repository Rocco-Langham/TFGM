#!/bin/bash
# Rebuild the timetable data from TfGM's latest published timetable.
#
# TfGM's calendars only run a few weeks ahead, so this runs weekly (see
# tools/com.tfgm-map.refresh.plist). It builds into a temporary folder and only swaps the
# new files in once everything has built, so a failed download never leaves the map with
# half-written data. Stand layouts come from OpenStreetMap and rarely change: rebuild those
# by hand with tools/build_layouts.py.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "$(date '+%Y-%m-%d %H:%M') refreshing timetable data in $repo"
curl -sSfL --retry 3 -o "$tmp/gtfs.zip" "https://odata.tfgm.com/opendata/downloads/TfGMgtfsnew.zip"
curl -sSfL --retry 3 -o "$tmp/naptan.csv" "https://naptan.api.dft.gov.uk/v1/access-nodes?dataFormat=csv&atcoAreaCodes=180"

/usr/bin/env python3 "$repo/tools/build_timetable.py" --gtfs "$tmp/gtfs.zip" --naptan "$tmp/naptan.csv" --out "$tmp/data"

# Swap the new files in. Folders go via a rename so the old copy is only removed afterwards.
for item in meta.js bus-departures.js stops.js metrolink.js stops lines; do
  if [ -e "$repo/data/$item" ]; then mv "$repo/data/$item" "$tmp/old-$item"; fi
  mv "$tmp/data/$item" "$repo/data/$item"
done
echo "$(date '+%Y-%m-%d %H:%M') done"
