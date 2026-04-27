#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

# ── SoniqBoom · Start ────────────────────────────────────────────────────────
# Starts the server as a background process and returns to the shell.
# Stop with:  ./shutdown.sh
set -euo pipefail

BOLD=$(tput bold 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)
GREEN='\033[0;32m'; RED='\033[0;31m'; CYAN='\033[0;36m'; DIM='\033[2m'; NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
SONIQBOOM="$VENV/bin/soniqboom"

# ── Paths (platform-aware) ───────────────────────────────────────────────────
case "$(uname -s)" in
  Darwin) DATA_DIR="$HOME/Library/Application Support/SoniqBoom" ;;
  Linux)  DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/soniqboom" ;;
  *)      DATA_DIR="$HOME/.soniqboom" ;;
esac
PID_FILE="$DATA_DIR/soniqboom.pid"
LOG_DIR="$DATA_DIR/log"
LOG_FILE="$LOG_DIR/soniqboom.log"

# ── Parse args ───────────────────────────────────────────────────────────────
PORT=8080
while [[ $# -gt 0 ]]; do
  case $1 in
    --port) PORT="$2"; shift 2 ;;
    *) shift ;;
  esac
done

# ── Pre-flight ───────────────────────────────────────────────────────────────
if [ ! -f "$SONIQBOOM" ]; then
  echo -e "${RED}SoniqBoom not installed. Run: bash install.sh${NC}"
  exit 1
fi

# Check if already running
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo ""
    echo -e "${BOLD}── SoniqBoom ──${RESET}"
    echo ""
    echo -e "  Already running (pid ${BOLD}$OLD_PID${RESET})"
    echo -e "  Server:  ${GREEN}http://127.0.0.1:${PORT}${NC}"
    echo -e "  Stop:    ${BOLD}./shutdown.sh${RESET}  or  ${BOLD}kill $OLD_PID${RESET}"
    echo ""
    exit 0
  fi
  # Stale pidfile — clean up
  rm -f "$PID_FILE"
fi

# ── Start server ─────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"

# Mark a clear start boundary in the log
echo "" >> "$LOG_FILE" 2>/dev/null || true
echo "════════════════════════════════════════════════════" >> "$LOG_FILE" 2>/dev/null || true

"$SONIQBOOM" --port "$PORT" >> "$LOG_FILE" 2>&1 &
APP_PID=$!
disown "$APP_PID"
echo "$APP_PID" > "$PID_FILE"

# ── Wait for ready ───────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}── SoniqBoom ──${RESET}"
echo ""

READY=0
for i in $(seq 1 300); do
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo -e "  ${RED}Server exited unexpectedly. Check log:${NC}"
    echo -e "  ${DIM}$LOG_FILE${NC}"
    echo ""
    tail -20 "$LOG_FILE" 2>/dev/null | sed 's/^/  /'
    rm -f "$PID_FILE"
    exit 1
  fi
  if curl -s -f "http://127.0.0.1:${PORT}/api/health" &>/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done

if [ "$READY" -eq 0 ]; then
  echo -e "  ${RED}Server did not become ready within 5 minutes.${NC}"
  echo -e "  Log: ${DIM}$LOG_FILE${NC}"
  kill -TERM "$APP_PID" 2>/dev/null || true
  rm -f "$PID_FILE"
  exit 1
fi

# ── Collect info ─────────────────────────────────────────────────────────────
# Read version from the health endpoint
VERSION=$(curl -s "http://127.0.0.1:${PORT}/api/health" 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('version',''))" 2>/dev/null || echo "")
[ -z "$VERSION" ] && VERSION="1.0.0"

# Network addresses
HOSTNAME_LOCAL="$(hostname 2>/dev/null || echo "localhost").local"
NET_ADDRS=()
if command -v ifconfig &>/dev/null; then
  while IFS= read -r ip; do
    [ -n "$ip" ] && NET_ADDRS+=("$ip")
  done < <(ifconfig 2>/dev/null | awk '/inet / && !/127.0.0.1/ {print $2}')
elif command -v ip &>/dev/null; then
  while IFS= read -r ip; do
    [ -n "$ip" ] && NET_ADDRS+=("$ip")
  done < <(ip -4 addr show 2>/dev/null | awk '/inet / && !/127.0.0.1/ {split($2,a,"/"); print a[1]}')
elif command -v hostname &>/dev/null; then
  while IFS= read -r ip; do
    [ -n "$ip" ] && NET_ADDRS+=("$ip")
  done < <(hostname -I 2>/dev/null | tr ' ' '\n')
fi

# Track count from the last log line
TRACK_COUNT=$(grep -o '[0-9]* tracks loaded' "$LOG_FILE" 2>/dev/null | tail -1 | awk '{print $1}')
[ -z "$TRACK_COUNT" ] && TRACK_COUNT="—"

# Config file path
CONF_FILE="$DATA_DIR/SoniqBoom.conf"

# ── Banner ───────────────────────────────────────────────────────────────────
echo -e "  ──────────────────────────────────────────────────────"
echo -e "  ${BOLD}SoniqBoom $VERSION${RESET}  ·  ${GREEN}ready${NC}  ·  ${TRACK_COUNT} tracks  ·  pid ${BOLD}$APP_PID${RESET}"
echo -e "  ──────────────────────────────────────────────────────"
echo -e "  Local:     ${GREEN}http://localhost:${PORT}${NC}"
for addr in "${NET_ADDRS[@]}"; do
  echo -e "  Network:   ${CYAN}✓${NC}  http://${addr}:${PORT}"
done
echo -e "  Hostname:  ${CYAN}✓${NC}  http://${HOSTNAME_LOCAL}:${PORT}"
echo -e "  Config:    ${DIM}$CONF_FILE${NC}"
echo -e "  Data:      ${DIM}$DATA_DIR${NC}"
echo -e "  Log:       ${DIM}$LOG_FILE${NC}"
echo -e "  ──────────────────────────────────────────────────────"
echo -e "  Stop:      ${BOLD}./shutdown.sh${RESET}  or  ${BOLD}kill $APP_PID${RESET}"
echo -e "  ──────────────────────────────────────────────────────"
echo ""

# ── Start menu bar icon (macOS only) ─────────────────────────────────────────
if [ "$(uname -s)" = "Darwin" ]; then
  MENUBAR="$SCRIPT_DIR/soniqboom-menubar.py"
  if [ -f "$MENUBAR" ]; then
    # Kill any existing menubar instance
    pkill -f 'soniqboom-menubar\.py' 2>/dev/null || true
    "$VENV/bin/python" "$MENUBAR" "$PORT" "$SCRIPT_DIR" &
    disown
  fi
fi
