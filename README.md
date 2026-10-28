# Greater Manchester bus and tram map

A single-page map of Greater Manchester with light, dark and satellite styles, showing:

- the 17 major bus interchanges, each with a to-scale stand diagram and the buses due to leave
- every other bus stop, with next departures, and stand diagrams for groups of lettered stops
- the Metrolink network and next trams
- the route of any bus or tram you click, and search by stop, town or route number

Times come from TfGM's published timetable, so they are scheduled, not live.

## Running it

The timetable data isn't stored in git (it's ~30 MB and changes weekly). Build it once:

```sh
bash tools/refresh_data.sh
```

Then open `index.html` in a browser. It works straight from disk; no server is needed.

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
| `data/interchange-layouts.js` | `tools/build_layouts.py` (rarely needed) | NaPTAN, [OpenStreetMap](https://www.openstreetmap.org/copyright) via Overpass |

Map tiles: [OpenFreeMap](https://openfreemap.org/) (light and dark) and Esri World Imagery (satellite).
