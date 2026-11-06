#!/bin/bash
# Deploy ai-egress-guard to this Mac: guard + scout (launchd) + SwiftBar panel.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.dsh/bin"
LA="$HOME/Library/LaunchAgents"
SWIFTBAR="$HOME/Library/Application Support/SwiftBar/Plugins"

install -d -m 755 "$BIN" "$LA" "$SWIFTBAR"
install -m 755 "$SRC/ai-egress-guard.py" "$BIN/ai-egress-guard.py"
install -m 755 "$SRC/ai-egress-guard.sh" "$BIN/ai-egress-guard.sh"

for f in com.b612.ai-egress-guard com.b612.ai-egress-scout; do
  sed "s|/Users/b612-lp|$HOME|g" "$SRC/launchd/$f.plist" > "$LA/$f.plist"
  launchctl bootout "gui/$(id -u)/$f" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LA/$f.plist"
done

install -m 755 "$SRC/swiftbar/status.30s.sh" "$SWIFTBAR/status.30s.sh"

echo "Deployed:"
echo "  $BIN/ai-egress-guard.sh        (entrypoint)"
echo "  $LA/com.b612.ai-egress-*.plist (launchd, 10s/60s)"
echo "  $SWIFTBAR/status.30s.sh        (menu bar panel)"
echo
echo "Verify:  bash $BIN/ai-egress-guard.sh && echo OK"
echo "Secret:  export CLASH_API_SECRET=... if your Clash external-controller secret differs from the placeholder."
