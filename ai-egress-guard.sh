#!/bin/bash
# Stable SwiftBar/launchd entrypoint; implementation uses macOS bundled Python.
set -eu
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
exec /usr/bin/python3 "$HOME/.dsh/bin/ai-egress-guard.py" "$@"
