#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

# ── SoniqBoom · Restart ──────────────────────────────────────────────────────
# Graceful shutdown followed by a fresh start.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Forward all args (e.g. --port 9090) to run.sh
ARGS=("$@")

# ── Stop (if running) ───────────────────────────────────────────────────────
# Forward args (e.g. --port) so shutdown targets the same port's listener.
"$SCRIPT_DIR/shutdown.sh" "${ARGS[@]}" 2>/dev/null || true

# ``shutdown.sh`` already blocks until the port is genuinely free (it drains any
# lingering SoniqBoom listener), so no fixed sleep is needed here — that sleep
# used to be too short for a big-library shutdown and let the start below race
# the not-yet-released port.  A tiny settle margin only.
sleep 0.3

# ── Start ────────────────────────────────────────────────────────────────────
exec "$SCRIPT_DIR/run.sh" "${ARGS[@]}"
