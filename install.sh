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

  # game-music-emu (libgme) — renders console chiptunes (NSF/SPC/GBS/VGM/AY/
  # KSS/SAP/…).  SoniqBoom binds the shared library directly via ctypes
  # (Homebrew ffmpeg has no libgme demuxer), so the library just needs to exist.
  if ! brew list game-music-emu &>/dev/null 2>&1; then
    info "Installing game-music-emu (console chiptune renderer)…"
    brew install game-music-emu || warn "game-music-emu install failed — NSF/SPC/etc. won't play"
  fi
  info "game-music-emu: $(brew list game-music-emu &>/dev/null 2>&1 && echo installed || echo 'not found — NSF/SPC/etc. disabled')"

  # uade (uade123) — renders AHX and other Amiga formats (Unix Amiga Delitracker
  # Emulator).  Neither openmpt123 nor ffmpeg decode AHX.
  if ! command -v uade123 &>/dev/null; then
    info "Installing uade (Amiga AHX renderer)…"
    brew install uade || warn "uade install failed — AHX (.ahx) won't play"
  fi
  info "uade123: $(command -v uade123 || echo 'not found — AHX disabled')"

  # adplay (AdPlug) — renders AdLib/OPL2 FM music: id/Apogee IMF (Wolfenstein
  # 3D, Commander Keen, …), ROL, CMF, D00, RAD, LucasArts LAA, Sierra SCI,
  # DOSBox DRO, …  Neither openmpt123 nor ffmpeg decode these.
  if ! command -v adplay &>/dev/null; then
    info "Installing adplay (AdPlug — AdLib/OPL renderer)…"
    brew install adplay || warn "adplay install failed — AdLib/OPL formats won't play"
  fi
  info "adplay: $(command -v adplay || echo 'not found — AdLib/OPL disabled')"

  # lhasa provides the reference ``lha`` CLI — it decodes every LHA method,
  # including ``-lh1-`` (common in older Amiga archives) that the in-process
  # ``lhafile`` reader rejects.  Optional: LHA scanning degrades without it.
  if ! command -v lha &>/dev/null; then
    info "Installing lhasa (LHA/LZH archive decoder)…"
    brew install lhasa || warn "lhasa install failed — LHA -lh1- archives won't be scanned"
  fi
  info "lha (lhasa): $(command -v lha || echo 'not found — LHA -lh1- archives skipped')"

  # sc68 — native Atari ST .sc68 disks.  (SNDH uses psgplay, built from
  # source below for both platforms; .ym uses the bundled StSound engine.)
  if ! command -v sc68 &>/dev/null; then
    info "Installing sc68 (Atari ST .sc68 renderer)…"
    brew install sc68 || warn "sc68 install failed — .sc68 files won't play"
  fi
  info "sc68: $(command -v sc68 || echo 'not found — .sc68 disabled')"

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
    # python3-dev + build-essential: some pinned deps (e.g. lhafile) ship no
    # prebuilt aarch64/musl wheel and compile a C extension at install time,
    # which needs Python.h (python3-dev) and a C toolchain (build-essential).
    # Debian/Ubuntu split these out of the base python3, so without them the
    # ``pip install`` fails with ``fatal error: Python.h: No such file`` — the
    # Fedora (python3-devel) and macOS (framework Python) paths ship them already.
    run_pkg apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip python3-dev build-essential ffmpeg \
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
      python3 python3-virtualenv python3-pip python3-devel gcc ffmpeg \
      fluidsynth libopenmpt \
      sidplayfp \
      curl ca-certificates xz || true
  elif [ "$PKG" = "pacman" ]; then
    section "Installing system dependencies (pacman)"
    run_pkg pacman -S --noconfirm --needed \
      python python-virtualenv python-pip gcc ffmpeg \
      fluidsynth libopenmpt \
      sidplayfp \
      curl ca-certificates xz || true
  elif [ "$PKG" = "zypper" ]; then
    section "Installing system dependencies (zypper)"
    run_pkg zypper --non-interactive install \
      python3 python3-virtualenv python3-pip python3-devel gcc ffmpeg \
      fluidsynth libopenmpt0 \
      sidplayfp \
      curl ca-certificates xz || true
  fi

  # openmpt123 CLI (tracker renderer — SoniqBoom shells out to the *binary*,
  # not the libopenmpt library).  Several distros split the CLI out of the
  # library package (Fedora: ``openmpt123`` vs ``libopenmpt``; openSUSE:
  # ``openmpt123`` vs ``libopenmpt0``), so installing only the library leaves
  # tracker rendering disabled.  Ensure the binary itself is present —
  # best-effort and isolated (its own command per manager) so a distro that
  # names it differently never blocks the core deps.  apt installs it in the
  # main list above; Arch's ``libopenmpt`` package bundles the binary.
  if [ "$PKG" != "none" ] && ! command -v openmpt123 &>/dev/null; then
    case "$PKG" in
      apt)    run_pkg apt-get install -y --no-install-recommends openmpt123 || true ;;
      dnf)    run_pkg dnf install -y openmpt123 || true ;;
      zypper) run_pkg zypper --non-interactive install openmpt123 || true ;;
      pacman) run_pkg pacman -S --noconfirm --needed libopenmpt || true ;;
    esac
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

  # libgme (console-chiptune renderer; SoniqBoom binds it via ctypes — see
  # soniqboom/core/gme_render.py).  Best-effort: package name varies per distro.
  if [ "$PKG" != "none" ]; then
    case "$PKG" in
      apt)    run_pkg apt-get install -y --no-install-recommends libgme0 || true ;;
      dnf)    run_pkg dnf install -y game-music-emu || true ;;
      pacman) run_pkg pacman -S --noconfirm --needed libgme || true ;;
      zypper) run_pkg zypper --non-interactive install libgme || true ;;
    esac
  fi

  # uade123 — AHX / Amiga formats.  Niche; not packaged on every distro.
  # Best-effort; the app surfaces a clear install hint if it's still missing.
  if [ "$PKG" != "none" ] && ! command -v uade123 &>/dev/null; then
    case "$PKG" in
      apt)    run_pkg apt-get install -y --no-install-recommends uade123 || true ;;
      dnf)    run_pkg dnf install -y uade || true ;;
      pacman) run_pkg pacman -S --noconfirm --needed uade || true ;;
      zypper) run_pkg zypper --non-interactive install uade || true ;;
    esac
  fi

  # adplay (AdPlug) — AdLib/OPL2 FM: id/Apogee IMF, ROL, CMF, D00, RAD, …
  # Debian/Ubuntu ship the player in ``adplug-utils``; other distros vary.
  if [ "$PKG" != "none" ] && ! command -v adplay &>/dev/null; then
    case "$PKG" in
      apt)    run_pkg apt-get install -y --no-install-recommends adplug-utils || true ;;
      dnf)    run_pkg dnf install -y adplay || true ;;
      pacman) run_pkg pacman -S --noconfirm --needed adplay || true ;;
      zypper) run_pkg zypper --non-interactive install adplay || true ;;
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

  # sc68 — native Atari ST .sc68 disks (best-effort; not in every distro).
  if ! command -v sc68 &>/dev/null; then
    case "$PKG" in
      apt)    run_pkg apt-get install -y --no-install-recommends sc68 || true ;;
      dnf)    run_pkg dnf install -y sc68 || true ;;
      pacman) warn "sc68 is in the AUR — install with an AUR helper for .sc68 support" ;;
      zypper) run_pkg zypper --non-interactive install sc68 || true ;;
    esac
  fi
  info "sc68:             $(command -v sc68       || echo 'not found — .sc68 disabled')"
fi

# ─────────────────────────────────────────────────────────────────────────────
# psgplay (Atari ST SNDH renderer) — both platforms, built from source
# ─────────────────────────────────────────────────────────────────────────────
# No package manager ships psgplay.  Plain C, no dependencies, ~10 s build.
# Pinned to a verified commit (rendered 10/10 SNDH test files, including
# every file the 2003-era sc68 CLI rejects).  The .ym renderer needs no step
# here — the BSD StSound engine is vendored and compiled on first use.
section "psgplay (Atari ST SNDH)"
PSGPLAY_COMMIT="869992cbbb8488b519149d8c0dd7afafb78aae5e"
if command -v psgplay &>/dev/null; then
  info "psgplay already installed: $(command -v psgplay)"
else
  if [ "$PLATFORM" = "macos" ]; then
    PSG_DEST="$(brew --prefix)/bin"
  else
    PSG_DEST="/usr/local/bin"
  fi
  PSG_TMP="$(mktemp -d)"
  if git clone -q https://github.com/frno7/psgplay.git "$PSG_TMP/psgplay" \
      && git -C "$PSG_TMP/psgplay" checkout -q "$PSGPLAY_COMMIT" \
      && make -C "$PSG_TMP/psgplay" -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)" psgplay >/dev/null 2>&1 \
      && [ -x "$PSG_TMP/psgplay/psgplay" ]; then
    if [ -w "$PSG_DEST" ]; then
      cp "$PSG_TMP/psgplay/psgplay" "$PSG_DEST/psgplay"
    elif command -v sudo &>/dev/null; then
      sudo cp "$PSG_TMP/psgplay/psgplay" "$PSG_DEST/psgplay"
    fi
  fi
  rm -rf "$PSG_TMP"
  if command -v psgplay &>/dev/null || [ -x "$PSG_DEST/psgplay" ]; then
    info "psgplay: $(command -v psgplay || echo "$PSG_DEST/psgplay")"
  else
    warn "psgplay build failed — Atari ST SNDH files won't play (re-run install.sh to retry)"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# zxtune123 (PSF console-music family: PSF/PSF2/USF/GSF/2SF/SSF/DSF/NCSF)
# ─────────────────────────────────────────────────────────────────────────────
# The only cross-format CLI bundling the reference cores (Highly Experimental,
# Highly Theoretical, lazyusf2, mGBA, vio2sf).  Linux: official prebuilt from
# storage.zxtune.ru.  macOS: built from a pinned revision (verified build,
# 2026-07-01, arm64) with two small compat patches for modern clang.
# OPTIONAL — skips gracefully; SoniqBoom 501s PSF files with a clear hint.
section "zxtune123 (console music rips — optional)"
ZXTUNE_REV="be510430c54b78f230881ece66b6e2799f92748d"
ZXTUNE_LINUX_URL="https://storage.zxtune.ru/builds/public/r5100/linux/x86_64/zxtune_r5100_linux_x86_64.tar.gz"
if command -v zxtune123 &>/dev/null; then
  info "zxtune123 already installed: $(command -v zxtune123)"
elif [ "$PLATFORM" = "linux" ] && [ "$(uname -m)" = "x86_64" ]; then
  ZX_TMP="$(mktemp -d)"
  if curl -sfL --max-time 300 "$ZXTUNE_LINUX_URL" -o "$ZX_TMP/zx.tar.gz" \
      && tar xzf "$ZX_TMP/zx.tar.gz" -C "$ZX_TMP" \
      && ZXBIN="$(find "$ZX_TMP" -name zxtune123 -type f | head -1)" \
      && [ -n "$ZXBIN" ]; then
    if [ -w /usr/local/bin ]; then cp "$ZXBIN" /usr/local/bin/zxtune123
    elif command -v sudo &>/dev/null; then sudo cp "$ZXBIN" /usr/local/bin/zxtune123; fi
  fi
  rm -rf "$ZX_TMP"
  info "zxtune123: $(command -v zxtune123 || echo 'not installed — PSF/USF/GSF/… disabled')"
elif [ "$PLATFORM" = "macos" ]; then
  warn "Building zxtune123 from source (one-time, ~10-15 min; needs boost)…"
  brew list boost &>/dev/null || brew install boost
  ZX_TMP="$(mktemp -d)"
  if git clone -q https://github.com/vitamin-caig/zxtune.git "$ZX_TMP/zxtune" \
      && git -C "$ZX_TMP/zxtune" checkout -q "$ZXTUNE_REV"; then
    ( cd "$ZX_TMP/zxtune" \
      && sed -i '' 's/if (!subLocation\.unique())/if (subLocation.use_count() != 1)/; s/if (!Subdata\.unique())/if (Subdata.use_count() != 1)/' src/core/plugins/archives/raw_supp.cpp \
      && sed -i '' 's/#    define FMT_CONSTEVAL consteval/#    define FMT_CONSTEVAL/' 3rdparty/fmt/include/fmt/core.h \
      && env CPATH="$(brew --prefix boost)/include" LIBRARY_PATH="$(brew --prefix boost)/lib" \
         make system.zlib=1 platform=darwin -C apps/zxtune123 -j"$(sysctl -n hw.ncpu)" ) \
      >/dev/null 2>&1 || true
    ZXBIN="$ZX_TMP/zxtune/bin/darwin/release/zxtune123"
    if [ -x "$ZXBIN" ]; then
      cp "$ZXBIN" "$(brew --prefix)/bin/zxtune123"
    fi
  fi
  rm -rf "$ZX_TMP"
  info "zxtune123: $(command -v zxtune123 || echo 'build failed — PSF/USF/GSF/… disabled (re-run install.sh to retry)')"
else
  warn "zxtune123: no prebuilt for this platform — PSF-family formats disabled"
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
# First-run admin account
# ─────────────────────────────────────────────────────────────────────────────
# The web UI keeps registration LOCKED until at least one admin exists (so an
# anonymous LAN visitor can't make themselves admin), which means a fresh
# install needs the admin bootstrapped from the trusted local terminal.  Do it
# now, while we still own this TTY.  ``--ensure-admin`` prompts for a username +
# password only when NO admin exists yet, and is a silent no-op once one does —
# so re-running install.sh never re-prompts.  The CLI self-detects a
# non-interactive install (piped / CI: no TTY) and prints a hint instead of
# hanging.  You can re-run this any time with:  bash setup-admin.sh
section "Admin account"
SETADM="$VENV/bin/soniqboom-setadm"
if [ -x "$SETADM" ]; then
  "$SETADM" --ensure-admin || true
else
  warn "soniqboom-setadm not found — create an admin later with:  bash setup-admin.sh"
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
echo "  Manage admin:     bash setup-admin.sh   (create / reset the admin account)"
echo ""
if [ "$PLATFORM" = "macos" ]; then
  echo "  Config:           ~/Library/Application Support/SoniqBoom/SoniqBoom.conf"
  echo "  Data:             ~/Library/Application Support/SoniqBoom/"
else
  echo "  Config:           \${XDG_DATA_HOME:-~/.local/share}/soniqboom/SoniqBoom.conf"
  echo "  Data:             \${XDG_DATA_HOME:-~/.local/share}/soniqboom/"
fi
echo ""
