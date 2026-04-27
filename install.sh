#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

# SoniqBoom installer — macOS / Homebrew
# Usage: bash install.sh
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This installer is for macOS only. See the README for Linux instructions."
  exit 1
fi

BOLD=$(tput bold 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

info()    { echo -e "${GREEN}▶ $*${NC}"; }
warn()    { echo -e "${YELLOW}⚠ $*${NC}"; }
section() { echo -e "\n${BOLD}── $* ──${RESET}"; }
die()     { echo -e "${RED}✗ $*${NC}" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

# ── 1. Homebrew ────────────────────────────────────────────────────────────────
section "Homebrew"
if ! command -v brew &>/dev/null; then
  info "Installing Homebrew…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Add brew to PATH for Apple Silicon
  eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
fi
info "Homebrew $(brew --version | head -1)"

# ── 2. Core dependencies ───────────────────────────────────────────────────────
section "Core dependencies (Homebrew)"

# Python 3.12+
if ! brew list python@3.12 &>/dev/null; then
  info "Installing python@3.12…"
  brew install python@3.12
fi
PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
info "Python: $($PYTHON --version)"

# ffmpeg — GPL build; includes free codecs (MP3/AAC patents expired or licensed via ffmpeg build)
if ! command -v ffmpeg &>/dev/null; then
  info "Installing ffmpeg…"
  brew install ffmpeg
fi
info "ffmpeg: $(ffmpeg -version 2>&1 | head -1)"

# Format renderers (SID, MIDI, Tracker modules)
# libsidplayfp — GPL-2.0 (subprocess only) — C64 SID chip emulation
# Homebrew only provides the library; the CLI player must be built from source.
if ! command -v sidplayfp &>/dev/null; then
  info "Installing libsidplayfp library…"
  brew install libsidplayfp
  brew install pkgconf 2>/dev/null || true

  SIDPLAYFP_VER="2.16.2"
  info "Building sidplayfp CLI player v${SIDPLAYFP_VER} from source…"
  TMPBUILD="$(mktemp -d)"
  curl -sL "https://github.com/libsidplayfp/sidplayfp/releases/download/v${SIDPLAYFP_VER}/sidplayfp-${SIDPLAYFP_VER}.tar.gz" \
    -o "$TMPBUILD/sidplayfp.tar.gz"
  tar xzf "$TMPBUILD/sidplayfp.tar.gz" -C "$TMPBUILD"
  (cd "$TMPBUILD/sidplayfp-${SIDPLAYFP_VER}" && \
    ./configure --prefix="$(brew --prefix)" --quiet && \
    make -j"$(sysctl -n hw.ncpu)" --quiet && \
    make install --quiet)
  rm -rf "$TMPBUILD"
fi
info "sidplayfp: $(command -v sidplayfp || echo 'not found')"

# FluidSynth — LGPL-2.1 — MIDI synthesis
if ! command -v fluidsynth &>/dev/null; then
  info "Installing FluidSynth (MIDI synth)…"
  brew install fluid-synth
fi
info "fluidsynth: $(fluidsynth --version 2>&1 | head -1 || echo 'installed')"

# libopenmpt — BSD-3-Clause — tracker module playback (MOD/S3M/XM/IT/…)
if ! command -v openmpt123 &>/dev/null; then
  info "Installing libopenmpt (tracker player)…"
  brew install libopenmpt
fi
info "openmpt123: $(command -v openmpt123 || echo 'not found')"

# ── 3. Optional / informational ───────────────────────────────────────────────
section "Optional dependencies (informational)"

# CMUS, cava — detected but not auto-installed
for pkg in cmus cava; do
  if brew list "$pkg" &>/dev/null 2>&1; then
    info "$pkg detected (available for integration)"
  else
    warn "$pkg not installed — available via 'brew install $pkg' for extended features"
  fi
done

# ── 4. Python virtual environment ─────────────────────────────────────────────
section "Python virtual environment"
if [ ! -d "$VENV" ]; then
  info "Creating virtualenv at $VENV"
  "$PYTHON" -m venv "$VENV"
fi
PIP="$VENV/bin/pip"

info "Installing Python dependencies…"
"$PIP" install --upgrade pip -q
"$PIP" install -r "$SCRIPT_DIR/requirements.txt" -q
info "Dependencies installed"

# Install soniqboom package itself (editable, with macOS extras)
info "Installing SoniqBoom package…"
"$PIP" install -e "$SCRIPT_DIR[macos]" -q

# ── 5. Done ───────────────────────────────────────────────────────────────────
section "Installation complete"
echo ""
echo -e "${GREEN}${BOLD}SoniqBoom installed successfully!${RESET}"
echo ""
echo "  Start SoniqBoom:  bash run.sh"
echo "  Or directly:      $VENV/bin/soniqboom"
echo "  Browser UI:       http://127.0.0.1:8080"
echo ""
echo "  Config:           ~/Library/Application Support/SoniqBoom/SoniqBoom.conf"
echo "  Data:             ~/Library/Application Support/SoniqBoom/"
echo ""
