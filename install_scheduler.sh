#!/bin/bash
set -euo pipefail

PROJECT="$HOME/Downloads/goyal-rate-watcher-v2"
PYTHON="$PROJECT/.venv312/bin/python"
SCRIPT="$PROJECT/nationwide_scraper.py"
PLIST="$HOME/Library/LaunchAgents/com.goyalmortgages.ratewatcher.plist"
LABEL="com.goyalmortgages.ratewatcher"

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT/logs"

if [ ! -x "$PYTHON" ] || [ ! -f "$SCRIPT" ]; then
    echo "ERROR: Python environment or scraper not found."
    exit 1
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$PROJECT</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>10</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>$PROJECT/logs/watcher.log</string>

    <key>StandardErrorPath</key>
    <string>$PROJECT/logs/watcher_error.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "SUCCESS: Daily 10:00 AM Nationwide monitoring installed."
