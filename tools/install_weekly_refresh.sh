#!/bin/bash
# Install (or reinstall) the weekly timetable refresh as a macOS launchd job for this user.
# To remove it:  launchctl bootout gui/$(id -u)/com.tfgm-map.refresh
#                rm ~/Library/LaunchAgents/com.tfgm-map.refresh.plist
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
target="$HOME/Library/LaunchAgents/com.tfgm-map.refresh.plist"
log="$HOME/Library/Logs/tfgm-map-refresh.log"

mkdir -p "$(dirname "$target")" "$(dirname "$log")"
sed -e "s#REPO_PATH#$repo#g" -e "s#LOG_PATH#$log#g" "$repo/tools/com.tfgm-map.refresh.plist" > "$target"

launchctl bootout "gui/$(id -u)/com.tfgm-map.refresh" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$target"
echo "Installed: refreshes every Monday at 06:00. Log: $log"
