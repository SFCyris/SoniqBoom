#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

# ── SoniqBoom · Shutdown ─────────────────────────────────────────────────────
# Gracefully stops the server: flushes AOF, writes snapshot, stops merger.
set -euo pipefail

BOLD=$(tput bold 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)
GREEN='\033[0;32m'; RED='\033[0;31m'; DIM='\033[2m'; NC='\033[0m'

case "$(uname -s)" in
  Darwin) DEFAULT_DATA_DIR="$HOME/Library/Application Support/SoniqBoom" ;;
  Linux)  DEFAULT_DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/soniqboom" ;;
  *)      DEFAULT_DATA_DIR="$HOME/.soniqboom" ;;
esac
DATA_DIR="${SONIQBOOM_DATA_DIR:-$DEFAULT_DATA_DIR}"
PID_FILE="$DATA_DIR/soniqboom.pid"
LOG_FILE="$DATA_DIR/log/soniqboom.log"

# ── Find the process ─────────────────────────────────────────────────────────
PID=""

# Try pidfile first
if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE" 2>/dev/null || true)
  if [ -n "$PID" ] && ! kill -0 "$PID" 2>/dev/null; then
    echo -e "SoniqBoom is not running (stale pidfile, pid $PID)."
    rm -f "$PID_FILE"
    PID=""
  fi
fi

# Fallback: search by process name.  Previously this used the substring
# 'soniqboom' which matched editor windows, this script itself, and any
# log-viewer whose argv contained the word.  Anchor on the actual entry
# scripts/modules we know are real soniqboom processes.
if [ -z "$PID" ]; then
  PID=$(pgrep -f 'soniqboom\.main|soniqboom_app\.py|soniqboom-menubar\.py|uvicorn.*soniqboom' 2>/dev/null \
    | head -1 || true)
fi

if [ -z "$PID" ]; then
  echo "SoniqBoom is not running."
  exit 0
fi

# ── Graceful shutdown ────────────────────────────────────────────────────────
echo -e "${BOLD}Shutting down SoniqBoom${RESET} (pid $PID)..."
echo -e "  ${DIM}Flushing AOF → writing snapshot → stopping merger${NC}"

kill -TERM "$PID" 2>/dev/null || true

# Wait up to 30 seconds for graceful shutdown
for i in $(seq 1 30); do
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    # Also stop the menu bar icon
    pkill -f 'soniqboom-menubar\.py' 2>/dev/null || true
    echo -e "  ${GREEN}✓${NC} SoniqBoom stopped gracefully."
    # Show the last shutdown log line
    LAST=$(grep -i 'shutdown\|snapshot\|stopped' "$LOG_FILE" 2>/dev/null | tail -1 || true)
    [ -n "$LAST" ] && echo -e "  ${DIM}$LAST${NC}"
    echo ""
    exit 0
  fi
  sleep 1
done

# Still alive — force kill
echo -e "  ${RED}Process still running after 30s — force killing...${NC}"
kill -9 "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
pkill -f 'soniqboom-menubar\.py' 2>/dev/null || true
echo -e "  SoniqBoom killed."
echo ""
