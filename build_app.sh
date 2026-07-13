#!/bin/bash
# Build "Cloud Duplicate Finder.app" — a lightweight macOS bundle that launches
# cloud_duplicate_finder.py with a Python that has tkinter. Being a real .app
# means macOS activates it properly, so the native 'aqua' theme works.
#
# Usage:  ./build_app.sh
# Result: ./Cloud Duplicate Finder.app  (double-clickable; drag to /Applications)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="Cloud Duplicate Finder"
APP="$PROJECT_DIR/$APP_NAME.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RES="$CONTENTS/Resources"
SCRIPT="cloud_duplicate_finder.py"

if [ ! -f "$PROJECT_DIR/$SCRIPT" ]; then
  echo "error: $SCRIPT not found next to build_app.sh" >&2
  exit 1
fi

echo "Building $APP_NAME.app ..."
rm -rf "$APP"
mkdir -p "$MACOS" "$RES"

# Copy the app source into the bundle so it's self-contained.
cp "$PROJECT_DIR/$SCRIPT" "$RES/$SCRIPT"

# Info.plist
cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>$APP_NAME</string>
    <key>CFBundleDisplayName</key><string>$APP_NAME</string>
    <key>CFBundleIdentifier</key><string>com.wiltzie75.cloudduplicatefinder</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>LSMinimumSystemVersion</key><string>10.15</string>
</dict>
</plist>
PLIST

# Launcher executable: find a Python with tkinter, then run the bundled script.
cat > "$MACOS/launcher" <<'LAUNCHER'
#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
RES="$DIR/../Resources"
export CDF_BUNDLED=1
CANDIDATES=(
  /opt/homebrew/bin/python3.13
  /opt/homebrew/bin/python3.12
  /opt/homebrew/bin/python3
  /usr/local/bin/python3.13
  /usr/local/bin/python3
  /usr/bin/python3
)
for PY in "${CANDIDATES[@]}"; do
  if [ -x "$PY" ] && "$PY" -c "import tkinter" >/dev/null 2>&1; then
    exec "$PY" "$RES/cloud_duplicate_finder.py"
  fi
done
/usr/bin/osascript -e 'display alert "Cloud Duplicate Finder" message "No Python with tkinter was found.\n\nInstall it with:\n    brew install python-tk@3.13"'
exit 1
LAUNCHER
chmod +x "$MACOS/launcher"

echo "Done: $APP"
echo "Double-click it, or drag it into /Applications."
