#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

# SoniqBoom installer — macOS (Homebrew) and Linux (apt / dnf / pacman / zypper).
# Usage: bash install.sh
set -euo pipefail

BOLD=$(tput bold 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

info()    { echo -e "${GREEN}▶ $*${NC}"; }
warn()    { echo -e "${YELLOW}⚠ $*${NC}"; }
section() { echo -e "\n${BOLD}── $* ──${RESET}"; }
die()     { echo -e "${RED}✗ $*${NC}" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

OS_KIND="$(uname -s)"
case "$OS_KIND" in
  Darwin) PLATFORM=macos ;;
  Linux)  PLATFORM=linux ;;
  *)      die "Unsupported platform: $OS_KIND.  SoniqBoom targets macOS and Linux." ;;
esac
info "Detected platform: ${PLATFORM}"

PYTHON=""

# ─────────────────────────────────────────────────────────────────────────────
# macOS (Homebrew) install path
# ─────────────────────────────────────────────────────────────────────────────
if [ "$PLATFORM" = "macos" ]; then

  section "Homebrew"
  if ! command -v brew &>/dev/null; then
    info "Installing Homebrew…"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Probe both common brew installation prefixes so Intel and Apple Silicon
    # Macs are equally supported.
    if [ -x /opt/homebrew/bin/brew ]; then
      eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -x /usr/local/bin/brew ]; then
      eval "$(/usr/local/bin/brew shellenv)"
    fi
  fi
  info "Homebrew $(brew --version | head -1)"

  section "Core dependencies (Homebrew)"

  if ! brew list python@3.12 &>/dev/null; then
    info "Installing python@3.12…"
    brew install python@3.12
  fi
  PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
  info "Python: $($PYTHON --version)"

  # ffmpeg via Homebrew — we still install the system one because the rest of
  # the dependency tree (sidplayfp etc.) wants ffmpeg's libraries on PATH.
  # The bundled static ffmpeg is laid down by fetch_ffmpeg.py below as a
  # known-good fallback that SoniqBoom prefers at runtime if its DSD demuxer
  # set is more complete than the system build's.
  if ! command -v ffmpeg &>/dev/null; then
    info "Installing ffmpeg…"
    brew install ffmpeg
  fi
  info "ffmpeg (system): $(ffmpeg -version 2>&1 | head -1)"

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

  if ! command -v fluidsynth &>/dev/null; then
    info "Installing FluidSynth (MIDI synth)…"
    brew install fluid-synth
  fi
  info "fluidsynth: $(fluidsynth --version 2>&1 | head -1 || echo 'installed')"

  if ! command -v openmpt123 &>/dev/null; then
    info "Installing libopenmpt (tracker player)…"
    brew install libopenmpt
  fi
  info "openmpt123: $(command -v openmpt123 || echo 'not found')"

  # lhasa provides the reference ``lha`` CLI — it decodes every LHA method,
  # including ``-lh1-`` (common in older Amiga archives) that the in-process
  # ``lhafile`` reader rejects.  Optional: LHA scanning degrades without it.
  if ! command -v lha &>/dev/null; then
    info "Installing lhasa (LHA/LZH archive decoder)…"
    brew install lhasa || warn "lhasa install failed — LHA -lh1- archives won't be scanned"
  fi
  info "lha (lhasa): $(command -v lha || echo 'not found — LHA -lh1- archives skipped')"

  section "Optional dependencies (informational)"
  for pkg in cmus cava; do
    if brew list "$pkg" &>/dev/null 2>&1; then
      info "$pkg detected (available for integration)"
    else
      warn "$pkg not installed — available via 'brew install $pkg' for extended features"
    fi
  done

# ─────────────────────────────────────────────────────────────────────────────
# Linux install path — detect a supported package manager and use it
# ─────────────────────────────────────────────────────────────────────────────
elif [ "$PLATFORM" = "linux" ]; then

  section "Linux package manager"
  if command -v apt-get &>/dev/null;  then PKG=apt
  elif command -v dnf     &>/dev/null;  then PKG=dnf
  elif command -v pacman  &>/dev/null;  then PKG=pacman
  elif command -v zypper  &>/dev/null;  then PKG=zypper
  else
    warn "No supported package manager (apt/dnf/pacman/zypper) found."
    warn "Install python3, ffmpeg, sidplayfp, fluidsynth, libopenmpt manually,"
    warn "then re-run this installer."
    PKG=none
  fi
  [ "$PKG" != "none" ] && info "Using ${PKG}"

  # Run package-manager install commands.  We do not require root: if the
  # operator is already root we run directly, otherwise we prefix with sudo
  # (and surface a clear hint if sudo isn't available).
  run_pkg() {
    if [ "$(id -u)" = "0" ]; then
      "$@"
    elif command -v sudo &>/dev/null; then
      sudo "$@"
    else
      die "These steps need root.  Either re-run with sudo, or install the deps manually:  $*"
    fi
  }

  if [ "$PKG" = "apt" ]; then
    section "Installing system dependencies (apt)"
    run_pkg apt-get update -qq
    # python3-venv is the bit Debian splits out; libopenmpt0 ships openmpt123
    # in the openmpt-tools package on bookworm+.
    run_pkg apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip ffmpeg \
      fluidsynth libfluidsynth3 \
      openmpt123 \
      curl ca-certificates xz-utils
    # sidplayfp ships in some recent Debian/Ubuntu repos as ``sidplayfp``,
    # but the package name varies — try a few before falling back to source.
    if ! command -v sidplayfp &>/dev/null; then
      if apt-cache show sidplayfp &>/dev/null 2>&1; then
        run_pkg apt-get install -y --no-install-recommends sidplayfp || true
      fi
    fi
  elif [ "$PKG" = "dnf" ]; then
    section "Installing system dependencies (dnf)"
    run_pkg dnf install -y \
      python3 python3-virtualenv python3-pip ffmpeg \
      fluidsynth libopenmpt \
      sidplayfp \
      curl ca-certificates xz || true
  elif [ "$PKG" = "pacman" ]; then
    section "Installing system dependencies (pacman)"
    run_pkg pacman -S --noconfirm --needed \
      python python-virtualenv python-pip ffmpeg \
      fluidsynth libopenmpt \
      sidplayfp \
      curl ca-certificates xz || true
  elif [ "$PKG" = "zypper" ]; then
    section "Installing system dependencies (zypper)"
    run_pkg zypper --non-interactive install \
      python3 python3-virtualenv python3-pip ffmpeg \
      fluidsynth libopenmpt0 \
      sidplayfp \
      curl ca-certificates xz || true
  fi

  # LHA archive support: the reference ``lha`` CLI (from lhasa) decodes Amiga
  # ``-lh1-`` archives the in-process ``lhafile`` reader can't.  Best-effort and
  # isolated (its own command per manager) so a distro that doesn't package it
  # never blocks the core deps.
  if [ "$PKG" != "none" ] && ! command -v lha &>/dev/null; then
    case "$PKG" in
      apt)    run_pkg apt-get install -y --no-install-recommends lhasa || true ;;
      dnf)    run_pkg dnf install -y lhasa || true ;;
      zypper) run_pkg zypper --non-interactive install lhasa || true ;;
      pacman) warn "lhasa is in the AUR — install it with an AUR helper for LHA -lh1- support" ;;
    esac
  fi

  PYTHON="$(command -v python3 || true)"
  [ -z "$PYTHON" ] && die "python3 not found after install.  Aborting."
  info "Python: $($PYTHON --version)"
  info "ffmpeg (system): $(ffmpeg -version 2>&1 | head -1 || echo 'not installed')"
  info "sidplayfp:        $(command -v sidplayfp  || echo 'not found — SID rendering disabled')"
  info "fluidsynth:       $(command -v fluidsynth || echo 'not found — MIDI rendering disabled')"
  info "openmpt123:       $(command -v openmpt123 || echo 'not found — tracker rendering disabled')"
  info "lha (lhasa):      $(command -v lha        || echo 'not found — LHA -lh1- archives skipped')"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Python virtualenv (both platforms)
# ─────────────────────────────────────────────────────────────────────────────
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

# Install soniqboom package itself.  Use the macos extras only on macOS
# (pulls pyobjc / rumps for the menubar); plain install on Linux.
section "SoniqBoom package"
if [ "$PLATFORM" = "macos" ]; then
  "$PIP" install -e "$SCRIPT_DIR[macos]" -q
else
  "$PIP" install -e "$SCRIPT_DIR" -q
fi

# ─────────────────────────────────────────────────────────────────────────────
# Bundled static ffmpeg
# ─────────────────────────────────────────────────────────────────────────────
# This always lays down a known-good static ffmpeg with full DSD demuxer
# coverage into the user data dir, even if the system ffmpeg is fine — at
# runtime SoniqBoom prefers whichever has all the demuxers it needs (dsf,
# iff, wsd).  The download is idempotent: re-running install.sh is cheap.
section "Bundled static ffmpeg"
if [ -f "$SCRIPT_DIR/scripts/fetch_ffmpeg.py" ]; then
  if "$VENV/bin/python" "$SCRIPT_DIR/scripts/fetch_ffmpeg.py"; then
    info "Bundled ffmpeg ready (run 'soniqboom fetch-ffmpeg --force' anytime to refresh)"
  else
    warn "Bundled ffmpeg download failed — SoniqBoom will fall back to the system ffmpeg."
    warn "Re-run later with:  $VENV/bin/soniqboom fetch-ffmpeg"
  fi
else
  warn "scripts/fetch_ffmpeg.py not found — skipping bundled ffmpeg.  DSD playback"
  warn "will use whatever the system ffmpeg provides."
fi

# ─────────────────────────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────────────────────────
section "Installation complete"
echo ""
echo -e "${GREEN}${BOLD}SoniqBoom installed successfully!${RESET}"
echo ""
echo "  Start SoniqBoom:  bash run.sh"
echo "  Or directly:      $VENV/bin/soniqboom"
echo "  Browser UI:       http://127.0.0.1:8080"
echo ""
if [ "$PLATFORM" = "macos" ]; then
  echo "  Config:           ~/Library/Application Support/SoniqBoom/SoniqBoom.conf"
  echo "  Data:             ~/Library/Application Support/SoniqBoom/"
else
  echo "  Config:           \${XDG_CONFIG_HOME:-~/.config}/soniqboom/SoniqBoom.conf"
  echo "  Data:             \${XDG_DATA_HOME:-~/.local/share}/soniqboom/"
fi
echo ""
