#!/bin/bash
cd "$(dirname "$0")"
NEW=$(find ready -name "*.mp4" -mmin -6 2>/dev/null | head -3)
[ -n "$NEW" ] && osascript -e "display notification \"$(echo "$NEW" | wc -l | tr -d ' ') new clip(s) in ready/ - post them\" with title \"CLIP READY\" sound name \"Glass\""
