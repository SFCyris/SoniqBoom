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

# Parse --port (default 8080) so the port-based fallback below targets the
# right listener.  Other forwarded args (from restart.sh) are ignored.
PORT=8080
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="${2:-8080}"; shift 2 ;;
    *) shift ;;
  esac
done

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
# scripts/modules we know are real soniqboom processes.  NOTE: the real
# server runs as ``.../Python .venv/bin/soniqboom --port N`` — its argv
# contains ``/bin/soniqboom`` but NOT ``soniqboom.main`` or ``uvicorn``, so
# ``/bin/soniqboom`` MUST be in this pattern or a stale pidfile leaves the
# server unkillable (the restart bug this comment documents).
if [ -z "$PID" ]; then
  PID=$(pgrep -f '/bin/soniqboom|soniqboom\.main|soniqboom_app\.py|soniqboom-menubar\.py|uvicorn.*soniqboom' 2>/dev/null \
    | head -1 || true)
fi

# Last-resort fallback: whoever is LISTENing on the server port.  This is the
# most reliable identifier — it finds the server no matter how it was launched
# or whether the pidfile/argv match.  Guarded so we only adopt a Python /
# soniqboom process and never kill an unrelated app that happens to hold the
# port.  Degrades safely if lsof is unavailable.
if [ -z "$PID" ]; then
  PORT_PID=$(lsof -ti "TCP:${PORT}" -sTCP:LISTEN 2>/dev/null | head -1 || true)
  if [ -n "$PORT_PID" ]; then
    PORT_CMD=$(ps -p "$PORT_PID" -o command= 2>/dev/null || true)
    case "$PORT_CMD" in
      *[Pp]ython*|*soniqboom*)
        PID="$PORT_PID" ;;
      *)
        echo "Port ${PORT} is held by a non-SoniqBoom process (pid $PORT_PID):"
        echo "  $PORT_CMD"
        echo "Refusing to kill it.  Stop it manually or restart with --port <number>."
        exit 1 ;;
    esac
  fi
fi

if [ -z "$PID" ]; then
  echo "SoniqBoom is not running."
  exit 0
fi

# ── Graceful shutdown ────────────────────────────────────────────────────────
echo -e "${BOLD}Shutting down SoniqBoom${RESET} (pid $PID)..."
echo -e "  ${DIM}Flushing AOF → writing snapshot → stopping merger${NC}"

kill -TERM "$PID" 2>/dev/null || true

# Wait up to 30 s for the target process to exit gracefully.
GONE=""
for i in $(seq 1 30); do
  if ! kill -0 "$PID" 2>/dev/null; then GONE=1; break; fi
  sleep 1
done
if [ -z "$GONE" ]; then
  echo -e "  ${RED}Process still running after 30s — force killing...${NC}"
  kill -9 "$PID" 2>/dev/null || true
  sleep 1
fi
rm -f "$PID_FILE"
pkill -f 'soniqboom-menubar\.py' 2>/dev/null || true

# ── Wait until the PORT is genuinely free ────────────────────────────────────
# The target PID is not necessarily the (only) thing holding ${PORT}.  A stale
# pidfile, a duplicate/orphaned instance, or an instance still mid-boot can keep
# the port bound after our target exits — which is exactly what makes a
# follow-up start (or ``restart.sh``) report "port already in use".  So don't
# return until the port is actually released: terminate any lingering
# *SoniqBoom* listener still on it (an unrelated process is left untouched).
if command -v lsof &>/dev/null; then
  for i in $(seq 1 30); do
    HOLDER=$(lsof -ti "TCP:${PORT}" -sTCP:LISTEN 2>/dev/null | head -1 || true)
    if [ -z "$HOLDER" ]; then break; fi
    HCMD=$(ps -p "$HOLDER" -o command= 2>/dev/null || true)
    case "$HCMD" in
      *soniqboom*)
        if [ "$i" -le 5 ]; then
          kill -TERM "$HOLDER" 2>/dev/null || true
        else
          kill -9 "$HOLDER" 2>/dev/null || true
        fi ;;
      *)
        echo -e "  ${RED}Port ${PORT} is held by a non-SoniqBoom process (pid ${HOLDER}) — leaving it alone.${NC}"
        break ;;
    esac
    sleep 1
  done
  HOLDER=$(lsof -ti "TCP:${PORT}" -sTCP:LISTEN 2>/dev/null | head -1 || true)
  if [ -n "$HOLDER" ]; then
    echo -e "  ${RED}⚠  Port ${PORT} is still in use (pid ${HOLDER}) — a restart may fail to bind.${NC}"
  else
    echo -e "  ${GREEN}✓${NC} SoniqBoom stopped; port ${PORT} is free."
  fi
else
  echo -e "  ${GREEN}✓${NC} SoniqBoom stopped."
fi
LAST=$(grep -i 'shutdown\|snapshot\|stopped' "$LOG_FILE" 2>/dev/null | tail -1 || true)
[ -n "$LAST" ] && echo -e "  ${DIM}$LAST${NC}"
echo ""
