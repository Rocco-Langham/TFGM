# Greater Manchester bus and tram map

A single-page map of Greater Manchester with light, dark and satellite styles, showing:

- the 17 major bus interchanges, each with a to-scale stand diagram and the buses due to leave
- every other bus stop, with next departures, and stand diagrams for groups of lettered stops, including
  stops just beyond Greater Manchester that Bee Network routes run to
- the Metrolink network and next trams
- the route of any bus or tram you click, who runs it, and search by stop, town or route number

Times come from TfGM's published timetable, so they are scheduled, not live.

## Running it

The timetable data isn't stored in git (it's ~30 MB and changes weekly). Build it once:

```sh
bash tools/refresh_data.sh
```

Then open `index.html` in a browser. It works straight from disk; no server is needed.

## Live buses (optional)

Without live data, buses and trams are drawn where the timetable puts them. To make that look
less mechanical, each bus is also held up at random at some junctions, stopping for a few seconds
or slowing for traffic, while still reaching every stop at its timetabled time. To see real bus
positions instead, and whether each bus is early or late:

1. Register for a free key at the [Bus Open Data Service](https://data.bus-data.dft.gov.uk/).
2. Save it in a file called `.bods_key` in this folder (it's git-ignored).
3. Run `python3 tools/live_server.py` and open <http://localhost:8000>.

The server keeps the key to itself and passes positions to the page. BODS has no tram positions,
so trams always follow the timetable.

## How busy is it?

Click a bus or tram and you can say how crowded it is. Ratings are saved in your browser. When
the page is served by `tools/live_server.py`, they're also shared through it (kept for a day and a
half in `.crowding-v2.json`), so everyone using that server sees what other riders said.

## Keeping the timetable current

TfGM's timetable only runs a few weeks ahead. To rebuild it every Monday at 06:00 automatically:

```sh
bash tools/install_weekly_refresh.sh
```

The log is at `~/Library/Logs/tfgm-map-refresh.log`. To stop it:

```sh
launchctl bootout gui/$(id -u)/com.tfgm-map.refresh
rm ~/Library/LaunchAgents/com.tfgm-map.refresh.plist
```

## Data and tools

| File | Built by | Source |
| --- | --- | --- |
| `data/meta.js`, `data/bus-departures.js`, `data/stops.js`, `data/stops/`, `data/lines/`, `data/metrolink.js` | `tools/build_timetable.py` (via `tools/refresh_data.sh`) | [TfGM GTFS](https://odata.tfgm.com/opendata/downloads/TfGMgtfsnew.zip), [NaPTAN](https://naptan.api.dft.gov.uk/) |
| (used by the above) | `tools/osm_roads.py` snaps bus routes onto the roads, in the left-hand lane | [OpenFreeMap](https://openfreemap.org/) vector tiles (OpenStreetMap), cached in `tools/.cache/` |
| `data/interchange-layouts.js` | `tools/build_layouts.py` (rarely needed) | NaPTAN, [OpenStreetMap](https://www.openstreetmap.org/copyright) via Overpass |

Map tiles: [OpenFreeMap](https://openfreemap.org/) (light and dark) and Esri World Imagery (satellite).
