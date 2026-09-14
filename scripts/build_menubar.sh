#!/bin/bash
# Build the Otto menu bar app (bin/Otto.app) and the find_window helper.
#
# Requires the Xcode command line tools (`xcode-select --install`).
# Usage: scripts/build_menubar.sh [--python /path/to/python3]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$PROJECT_DIR/src/otto/menubar"
APP_DIR="$PROJECT_DIR/bin/Otto.app"
CONTENTS="$APP_DIR/Contents"
MACOS="$CONTENTS/MacOS"
PORT="${OTTO_PORT:-7077}"

PYTHON=""
while [ $# -gt 0 ]; do
  case "$1" in
    --python) PYTHON="$2"; shift 2 ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
done
if [ -z "$PYTHON" ]; then
  if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then PYTHON="$PROJECT_DIR/.venv/bin/python3"
  else PYTHON="$(command -v python3 || echo /usr/bin/python3)"; fi
fi

if ! command -v swiftc >/dev/null 2>&1; then
  echo "swiftc not found. Install the Xcode command line tools:  xcode-select --install"
  exit 1
fi

echo "Building Otto.app (python: $PYTHON, port: $PORT)…"
rm -rf "$APP_DIR"
mkdir -p "$MACOS"

swiftc -O -o "$MACOS/OttoMenuBar" "$SRC_DIR/OttoMenuBar.swift" \
  -framework Cocoa -framework UserNotifications
chmod +x "$MACOS/OttoMenuBar"

# Info.plist — bundle identity (needed for UNUserNotificationCenter) plus the
# runtime hints the app reads (project root, interpreter, port).
cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.otto.menubar</string>
    <key>CFBundleName</key>
    <string>Otto</string>
    <key>CFBundleDisplayName</key>
    <string>Otto</string>
    <key>CFBundleExecutable</key>
    <string>OttoMenuBar</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>OttoHome</key>
    <string>${PROJECT_DIR}</string>
    <key>OttoPython</key>
    <string>${PYTHON}</string>
    <key>OttoPort</key>
    <integer>${PORT}</integer>
</dict>
</plist>
PLIST

plutil -lint "$CONTENTS/Info.plist" >/dev/null

# find_window helper (background Slack screenshots without focus changes)
swiftc -O -o "$PROJECT_DIR/bin/find_window" "$SRC_DIR/find_window.swift" -framework CoreGraphics
chmod +x "$PROJECT_DIR/bin/find_window"

# Ad-hoc signature so the bundle can post notifications.
codesign --force --sign - "$APP_DIR" 2>/dev/null || true

echo "Built: $APP_DIR"
echo "       $PROJECT_DIR/bin/find_window"
echo "Run:   open \"$APP_DIR\""
