#!/usr/bin/env bash
# Install the 5-minute poller as a launchd job. Idempotent: safe to re-run after edits.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.harris.baywheels-poller"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs"
sed "s|__REPO__|$REPO|g" "$REPO/scripts/$LABEL.plist" > "$TARGET"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TARGET"

echo "Installed $LABEL"
echo "  plist   $TARGET"
echo "  logs    $REPO/logs/poller.log"
echo
launchctl print "gui/$(id -u)/$LABEL" | grep -E "state|program|runs" || true
echo
echo "Stop with:  launchctl bootout gui/$(id -u)/$LABEL"
