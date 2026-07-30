#!/bin/bash
# Build dist/Ollie-Installer.dmg — the clickable wrapper around install.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

STAGING="$(mktemp -d)/Ollie Installer"
mkdir -p "$STAGING" dist

cp installer/install.sh "$STAGING/install.sh"

cat > "$STAGING/Install Ollie.command" <<'CMD'
#!/bin/bash
# Double-click me. (First time: right-click → Open, because this file
# came from the internet and is not notarized.)
clear
echo "🔮  Ollie installer"
echo
DIR="$(cd "$(dirname "$0")" && pwd)"
exec bash "$DIR/install.sh"
CMD
chmod +x "$STAGING/Install Ollie.command"

cat > "$STAGING/READ ME FIRST.txt" <<'TXT'
🔮 Ollie — voice companion for terminal coding agents
     https://zeroshotmind.github.io/ollie

To install: right-click "Install Ollie.command" → Open → Open.

(Plain double-click is blocked the first time because this download is
not notarized by Apple — Ollie is open source and built locally on your
Mac instead. The Terminal window that opens shows exactly what it does.)

Requirements: Apple Silicon Mac. Ollama (https://ollama.com) gives the
narration its brain — the installer will remind you.
TXT

rm -f dist/Ollie-Installer.dmg
hdiutil create -volname "Ollie Installer" -srcfolder "$(dirname "$STAGING")" \
  -ov -format UDZO dist/Ollie-Installer.dmg >/dev/null
echo "built dist/Ollie-Installer.dmg ($(du -h dist/Ollie-Installer.dmg | cut -f1))"
