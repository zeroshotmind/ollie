#!/bin/bash
# Build dist/Ollie-Installer.dmg — a proper Mac disk image: one
# double-clickable "Install Ollie.app" (Ollie icon and all) that opens
# Terminal and runs install.sh. No loose scripts in a folder view.
set -euo pipefail
cd "$(dirname "$0")/.."

STAGING="$(mktemp -d)/stage"
APP="$STAGING/Install Ollie.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" dist

cp installer/install.sh "$APP/Contents/Resources/install.sh"
cp docs/Ollie.icns "$APP/Contents/Resources/Ollie.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Install Ollie</string>
  <key>CFBundleDisplayName</key><string>Install Ollie</string>
  <key>CFBundleIdentifier</key><string>com.zeroshotmind.ollie.installer</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleExecutable</key><string>installer</string>
  <key>CFBundleIconFile</key><string>Ollie</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/installer" <<'EXEC'
#!/bin/bash
# Open Terminal and run the bundled install.sh, so the user watches
# exactly what the installer does.
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
osascript <<OSA
tell application "Terminal"
  activate
  do script "clear; echo '🔮  Ollie installer'; echo; bash '$RES/install.sh'"
end tell
OSA
EXEC
chmod +x "$APP/Contents/MacOS/installer"

# ad-hoc signature: unsigned bundles get flat-out refused on newer macOS,
# ad-hoc ones get the standard right-click -> Open path
codesign --force --deep -s - "$APP"

cat > "$STAGING/READ ME FIRST.txt" <<'TXT'
🔮 Ollie — voice companion for terminal coding agents
     https://zeroshotmind.github.io/ollie

To install: right-click "Install Ollie" → Open → Open.

(Plain double-click is blocked the first time because this download is
not notarized by Apple — Ollie is open source and built locally on your
Mac instead. The Terminal window that opens shows exactly what it does.)

The installer builds Ollie.app on your Mac and puts it in /Applications
itself — no dragging needed. That local build is what lets macOS grant
microphone and accessibility permissions to Ollie by name.

Requirements: Apple Silicon Mac. Ollama (https://ollama.com) gives the
narration its brain — the installer will remind you.
TXT

rm -f dist/Ollie-Installer.dmg
hdiutil create -volname "Install Ollie" -srcfolder "$STAGING" \
  -ov -format UDZO dist/Ollie-Installer.dmg >/dev/null
echo "built dist/Ollie-Installer.dmg ($(du -h dist/Ollie-Installer.dmg | cut -f1))"
