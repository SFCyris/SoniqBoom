#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

# ── Capture SoniqBoom screenshots for documentation ──────────────────────────
# Requires: SoniqBoom running on localhost:8080, macOS with Safari
# Output:   docs/images/*.png
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMG_DIR="$SCRIPT_DIR/images"
mkdir -p "$IMG_DIR"

PORT=8080
BASE="http://localhost:$PORT"

echo "SoniqBoom Screenshot Capture"
echo "============================"
echo ""
echo "This script opens Safari windows and captures screenshots."
echo "Make sure SoniqBoom is running on port $PORT."
echo ""

# Check server is running
if ! curl -s "$BASE/api/health" &>/dev/null; then
  echo "ERROR: SoniqBoom is not running on port $PORT"
  exit 1
fi

echo "Press Enter to begin (each screenshot opens a Safari window)..."
read -r

capture() {
  local name="$1"
  local url="$2"
  local msg="${3:-}"

  echo ""
  echo "── $name ──"
  [ -n "$msg" ] && echo "  $msg"
  echo "  Opening: $url"
  open "$url"
  sleep 2
  echo "  Press Enter when the page is ready, then click the window to capture..."
  read -r
  screencapture -i "$IMG_DIR/$name.png"
  echo "  Saved: $IMG_DIR/$name.png"
}

capture "ui-main"    "$BASE"                    "Main library view (All Tracks)"
capture "ui-artists" "$BASE"                    "Click 'Artists' in the sidebar first"
capture "ui-search"  "$BASE"                    "Type something in the search bar first"
capture "ui-folders" "$BASE"                    "Expand a folder in the sidebar first"
capture "ui-admin"   "$BASE"                    "Open the Admin panel (gear icon) first"

echo ""
echo "Done! Screenshots saved to $IMG_DIR/"
echo "Resize to 700px wide for the docs if needed."
