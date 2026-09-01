#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# ── SoniqBoom · Start ────────────────────────────────────────────────────────
# Thin alias for run.sh (the canonical start script), so `bash startup.sh` and
# `bash run.sh` are equivalent.  All arguments (e.g. --port 9000) are forwarded.
# Stop with:  bash shutdown.sh     Restart with:  bash restart.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/run.sh" "$@"
