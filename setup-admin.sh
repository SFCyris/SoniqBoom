#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# ── SoniqBoom · Admin account setup ──────────────────────────────────────────
# Create or update a SoniqBoom user from the command line — a thin wrapper
# around the venv's ``soniqboom-setadm`` so you don't need to know the venv
# path.  The web UI keeps registration LOCKED until at least one admin exists,
# so this is how you bootstrap (or later reset) the admin on a fresh install.
#
# Usage:
#   bash setup-admin.sh                         # interactive: create the first
#                                               #   admin if none exists yet
#                                               #   (prompts for name + password)
#   bash setup-admin.sh -user alice -passwd 's3cret-9-things'
#   bash setup-admin.sh -user alice -passwd 'new-pw-9' -role admin   # reset pw
#   bash setup-admin.sh -user bob   -role readonly
#   bash setup-admin.sh -user bob   -disable
#   bash setup-admin.sh --help                  # full flag list
#
# The password is read by the underlying Python tool (hidden prompt via
# getpass when interactive, or the -passwd value you pass) — it is never
# handled by this shell wrapper.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETADM="$SCRIPT_DIR/.venv/bin/soniqboom-setadm"

if [ ! -x "$SETADM" ]; then
  echo "SoniqBoom isn't installed yet (missing $SETADM)." >&2
  echo "Run:  bash install.sh" >&2
  exit 1
fi

if [ "$#" -eq 0 ]; then
  # No arguments → first-run bootstrap: prompt for a username + password and
  # create an admin, but ONLY if none exists yet.  Already have an admin?  This
  # is a silent no-op — pass -user/-passwd (see usage above) to add or reset a
  # specific account.
  exec "$SETADM" --ensure-admin
fi

# Otherwise forward all arguments straight through to soniqboom-setadm
# (-user / -passwd / -role / -display-name / -disable / -enable / --help).
exec "$SETADM" "$@"
