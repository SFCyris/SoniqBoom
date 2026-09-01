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

# ── Self-contained bundling (macOS) ──────────────────────────────────────────
# From-source renderers (zxtune123, sidplayfp) link Homebrew dylibs dynamically,
# so a later `brew upgrade` can orphan them (dyld "Symbol not found" / "Library
# not loaded").  These copy a binary's full non-system dylib closure next to it
# and repoint every reference to @loader_path, so it carries its own libs and no
# future upgrade can break it.  Call _bundle_selfcontained with errexit+nounset
# OFF (the array/otool/grep plumbing legitimately returns non-zero).
_deps_src(){  # <macho> -> absolute SOURCE paths of bundle-worthy deps (resolves @loader_path)
  local f="$1" dir; dir="$(cd "$(dirname "$f")" 2>/dev/null && pwd)"
  otool -L "$f" | awk '/\(compatibility version/{print $1}' | while read -r p; do
    case "$p" in
      /usr/lib/*|/System/*|"") ;;                          # system: skip
      @loader_path/*) echo "$dir/${p#@loader_path/}" ;;    # sibling: resolve to abs
      @rpath/*|@executable_path/*) ;;                       # unsupported: leak-check catches
      /*) echo "$p" ;;                                      # absolute non-system: include
    esac
  done
}
_bundle_selfcontained(){  # <src_binary> <dest_dir>; non-zero on leak/missing dep
  local src="$1" dest="$2" base; base="$(basename "$src")"
  mkdir -p "$dest"; cp "$src" "$dest/$base"; chmod u+w "$dest/$base"
  local -a todo closure=(); todo=($(_deps_src "$src"))
  while [ "${#todo[@]}" -gt 0 ]; do
    local dep="${todo[0]}"; todo=("${todo[@]:1}")
    case " ${closure[*]} " in *" $dep "*) continue;; esac
    [ -f "$dep" ] || { warn "  bundle: dep not found: $dep"; continue; }
    closure+=("$dep"); todo+=($(_deps_src "$dep"))
  done
  local dep b f p
  for dep in "${closure[@]}"; do                            # copy each in; clean @loader_path id
    b="$(basename "$dep")"; cp "$dep" "$dest/$b"; chmod u+w "$dest/$b"
    install_name_tool -id "@loader_path/$b" "$dest/$b" 2>/dev/null
  done
  for f in "$dest/$base" "$dest"/*.dylib; do                # repoint absolute non-system deps
    [ -e "$f" ] || continue
    for p in $(otool -L "$f" | awk '/\(compatibility version/{print $1}' | grep -E '^/' | grep -vE '^/usr/lib/|^/System/'); do
      install_name_tool -change "$p" "@loader_path/$(basename "$p")" "$f" 2>/dev/null
    done
  done
  for f in "$dest/$base" "$dest"/*.dylib; do codesign --force --sign - "$f" >/dev/null 2>&1; done
  local leak=""                                             # falsifiable self-containment check
  for f in "$dest"/*; do
    for p in $(otool -L "$f" | awk '/\(compatibility version/{print $1}'); do
      case "$p" in
        /usr/lib/*|/System/*|"") ;;
        @loader_path/*) [ -f "$dest/${p#@loader_path/}" ] || leak="$leak ${p}[missing]" ;;
        *) leak="$leak $p" ;;
      esac
    done
  done
  [ -z "${leak// /}" ] || { warn "  bundle: unbundled refs remain:$leak"; return 1; }
  return 0
}
_install_selfcontained(){  # <stage_dir> <dest_bin_dir>: install binary+dylibs, root-owned-safe
  local stage="$1" dest="$2" sudo=""
  [ -w "$dest" ] || sudo="sudo"
  if [ -n "$sudo" ] && ! command -v sudo &>/dev/null; then
    warn "cannot write $dest — copy $stage/* there by hand"; return 1
  fi
  $sudo cp "$stage"/* "$dest/" || { warn "could not install to $dest"; return 1; }
}

# ── Renderer cleanup (--clean / --rebuild) ───────────────────────────────────
# Remove ONLY the renderers install.sh built from source (real files in the
# install prefix) + their bundled @loader_path dylib closure + the build-on-first-
# use helpers.  A Homebrew-managed player is a symlink into the Cellar — brew owns
# it, so we skip it (the operator uses `brew reinstall <name>`).  Call with
# errexit+nounset OFF (array/otool/grep plumbing).
_clean_bundled_siblings(){  # <binary> <dir>: rm the binary's transitive @loader_path dylibs
  command -v otool >/dev/null 2>&1 || return 0
  local bin="$1" dir="$2"
  local -a todo seen=()
  todo=($(otool -L "$bin" 2>/dev/null | awk '/\(compatibility version/{print $1}' | sed -n 's#^@loader_path/##p'))
  while [ "${#todo[@]}" -gt 0 ]; do
    local n="${todo[0]}"; todo=("${todo[@]:1}")
    case " ${seen[*]} " in *" $n "*) continue;; esac
    [ -f "$dir/$n" ] || continue
    seen+=("$n")
    todo+=($(otool -L "$dir/$n" 2>/dev/null | awk '/\(compatibility version/{print $1}' | sed -n 's#^@loader_path/##p' | grep -vFx "$n"))
  done
  local n; for n in "${seen[@]}"; do rm -f "$dir/$n"; done
}
_clean_renderers(){
  local bindir removed=0 name bin
  bindir="$(brew --prefix 2>/dev/null || echo /usr/local)/bin"
  for name in zxtune123 sidplayfp psgplay uade123 sc68; do
    bin="$bindir/$name"
    if [ -L "$bin" ]; then
      warn "  $name is Homebrew-managed (symlink) — skipping (use 'brew reinstall $name')"
    elif [ -f "$bin" ]; then
      _clean_bundled_siblings "$bin" "$bindir"
      rm -f "$bin" && { info "  removed $name + bundled libs"; removed=$((removed + 1)); }
    fi
  done
  for name in ym2wav hvl2wav; do
    if [ -f "$DATA_DIR/native/$name" ]; then
      rm -f "$DATA_DIR/native/$name" && { info "  removed $name (rebuilds on first play)"; removed=$((removed + 1)); }
    fi
  done
  info "cleaned $removed renderer artifact(s)"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

# ── CLI options ──────────────────────────────────────────────────────────────
MODE=install
while [ $# -gt 0 ]; do
  case "$1" in
    --clean)   MODE=clean ;;
    --rebuild) MODE=rebuild ;;
    -h|--help)
      cat <<'EOF'
Usage: install.sh [--clean | --rebuild | --help]

  (no option)   Install/update SoniqBoom and its renderers (idempotent —
                already-present renderers are left as-is).
  --rebuild     Delete the from-source renderers, then run the install so they
                are rebuilt from scratch.
  --clean       Delete the from-source renderers and exit (no install).
  --help        Show this message.

--clean/--rebuild affect ONLY the renderers install.sh builds from source —
zxtune123, sidplayfp, psgplay (+ their bundled libraries) and the build-on-first-
use ym2wav/hvl2wav.  Homebrew-managed players (fluidsynth, openmpt123, adplay,
sc68, uade123) are left untouched; use `brew reinstall <name>` to rebuild those.
EOF
      exit 0 ;;
    *) warn "unknown option: $1 (try --help)" ;;
  esac
  shift
done

OS_KIND="$(uname -s)"
case "$OS_KIND" in
  Darwin) PLATFORM=macos ;;
  Linux)  PLATFORM=linux ;;
  *)      die "Unsupported platform: $OS_KIND.  SoniqBoom targets macOS and Linux." ;;
esac
info "Detected platform: ${PLATFORM}"

# Data dir (mirrors run.sh) — where the build-on-first-use helpers live.
if [ -n "${SONIQBOOM_DATA_DIR:-}" ]; then
  DATA_DIR="$SONIQBOOM_DATA_DIR"
elif [ "$PLATFORM" = "macos" ]; then
  DATA_DIR="$HOME/Library/Application Support/SoniqBoom"
else
  DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/soniqboom"
fi

# --clean / --rebuild: wipe the from-source renderers up front.  --clean stops
# here; --rebuild falls through into the normal install, which rebuilds them.
if [ "$MODE" = clean ] || [ "$MODE" = rebuild ]; then
  section "Removing from-source renderers (--$MODE)"
  set +eu; _clean_renderers; set -eu
  if [ "$MODE" = clean ]; then
    info "Clean complete.  Run './install.sh' (or './install.sh --rebuild') to rebuild."
    exit 0
  fi
fi

# ── Pre-flight: tools we assume are already present ──────────────────────────
# On Linux we build a couple of players from source (git clone + make), and git
# and curl are NOT pulled in by the package installs below — so check for them up
# front and fail with one clear message instead of a cryptic error deep in the
# run.  (On macOS these arrive with the Xcode Command Line Tools that Homebrew
# triggers, so we don't pre-check there.)
if [ "$PLATFORM" = "linux" ]; then
  _missing=""
  command -v curl >/dev/null 2>&1 || _missing="$_missing curl"
  command -v git  >/dev/null 2>&1 || _missing="$_missing git"
  if [ -n "$_missing" ]; then
    die "Missing required tool(s):$_missing — install them first (e.g. 'sudo apt install -y$_missing', or the dnf/pacman/zypper equivalent), then re-run  bash install.sh"
  fi
fi

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

  # sidplayfp (C64 SID) — built from source with the accurate reSIDfp engine.
  # Homebrew's libsidplayfp 3.x ships ONLY the lightweight `sidlite` engine
  # (reSIDfp was split into an external libresidfp that has no brew formula), and
  # a from-source binary linking Homebrew's rolling libsidplayfp gets orphaned by
  # the next `brew upgrade libsidplayfp` (dyld "Library not loaded").  So build the
  # whole chain — libresidfp → libsidplayfp(+reSIDfp) → sidplayfp CLI — into a
  # private prefix and bundle its dylib closure via @loader_path
  # (_bundle_selfcontained).  All three are GPL-2.0-or-later, used at arm's length
  # (subprocess) and built from upstream source, so SoniqBoom's AGPL-3.0 is
  # unaffected.  The --version probe re-heals a present-but-orphaned binary.
  if command -v sidplayfp &>/dev/null && sidplayfp --version &>/dev/null; then
    info "sidplayfp already installed: $(command -v sidplayfp)"
  else
    BREW_PREFIX="$(brew --prefix 2>/dev/null || true)"
    RESIDFP_VER="1.2.2"; LIBSID_VER="3.1.0"; SIDCLI_VER="3.1.0"
    if [ -z "$BREW_PREFIX" ]; then
      warn "sidplayfp: brew unavailable — skipping (SID disabled)"
    else
      brew install pkgconf 2>/dev/null || true
      SID_TMP="$(mktemp -d)"; SID_PREFIX="$SID_TMP/prefix"; mkdir -p "$SID_PREFIX"
      SID_PKGCONF="$SID_PREFIX/lib/pkgconfig:$BREW_PREFIX/lib/pkgconfig"
      info "Building the accurate SID chain (libresidfp→libsidplayfp→sidplayfp, ~2-3 min)…"
      _sid_lib(){  # <url> <subdir> <log>: fetch + configure + make + make install
        curl -sfL "$1" -o "$SID_TMP/s.tgz" && tar xzf "$SID_TMP/s.tgz" -C "$SID_TMP" \
          && ( cd "$SID_TMP/$2" && PKG_CONFIG_PATH="$SID_PKGCONF" ./configure --prefix="$SID_PREFIX" --quiet \
               && make -j"$(sysctl -n hw.ncpu)" && make install ) >"$SID_TMP/$3" 2>&1
      }
      if _sid_lib "https://github.com/libsidplayfp/libresidfp/releases/download/v${RESIDFP_VER}/libresidfp-${RESIDFP_VER}.tar.gz" "libresidfp-${RESIDFP_VER}" "1_residfp.log" \
         && _sid_lib "https://github.com/libsidplayfp/libsidplayfp/releases/download/v${LIBSID_VER}/libsidplayfp-${LIBSID_VER}.tar.gz" "libsidplayfp-${LIBSID_VER}" "2_libsid.log" \
         && curl -sfL "https://github.com/libsidplayfp/sidplayfp/releases/download/v${SIDCLI_VER}/sidplayfp-${SIDCLI_VER}.tar.gz" -o "$SID_TMP/cli.tgz" \
         && tar xzf "$SID_TMP/cli.tgz" -C "$SID_TMP" \
         && ( cd "$SID_TMP/sidplayfp-${SIDCLI_VER}" && PKG_CONFIG_PATH="$SID_PKGCONF" ./configure --prefix="$SID_PREFIX" --quiet \
              && make -j"$(sysctl -n hw.ncpu)" ) >"$SID_TMP/3_cli.log" 2>&1; then
        SIDBIN="$(find "$SID_TMP/sidplayfp-${SIDCLI_VER}" -name sidplayfp -type f -perm -u+x -print -quit 2>/dev/null)"
        SID_STAGE="$SID_TMP/stage"
        if [ -n "$SIDBIN" ] && [ -x "$SIDBIN" ]; then
          set +eu; _bundle_selfcontained "$SIDBIN" "$SID_STAGE"; _sid_ok=$?; set -eu
          if [ "$_sid_ok" -eq 0 ] && "$SID_STAGE/sidplayfp" --version >/dev/null 2>&1; then
            _install_selfcontained "$SID_STAGE" "$BREW_PREFIX/bin" || true
          else
            warn "sidplayfp self-contained bundling failed — see $SID_TMP (SID disabled)"
          fi
        else
          warn "sidplayfp CLI build produced no binary — see $SID_TMP/3_cli.log (SID disabled)"
        fi
      else
        warn "sidplayfp chain build failed — see $SID_TMP/*.log (SID disabled)"
      fi
      # Clean up only on a verified-working install (keep logs for diagnosis otherwise).
      command -v sidplayfp &>/dev/null && sidplayfp --version &>/dev/null && rm -rf "$SID_TMP"
    fi
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

  NPROC="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"

  # Run a single command as root when we aren't already (used for `make install`
  # into /usr/local).  Unlike run_pkg this never dies — the caller treats a
  # failure as "renderer unavailable" and warns, matching the best-effort steps.
  _root() {
    if [ "$(id -u)" = "0" ]; then "$@"
    elif command -v sudo &>/dev/null; then sudo "$@"
    else "$@"; fi
  }

  # Install the toolchain a source build needs (best-effort; only pulled when we
  # actually fall back to compiling uade/sc68 because the distro doesn't ship
  # them).  Package names differ per manager; the set covers both builds.
  _ensure_build_deps() {
    case "$PKG" in
      apt)    run_pkg apt-get install -y --no-install-recommends \
                build-essential pkg-config libao-dev bzip2 automake zlib1g-dev || true ;;
      dnf)    run_pkg dnf install -y \
                gcc gcc-c++ make pkgconf-pkg-config libao-devel bzip2 automake zlib-devel || true ;;
      pacman) run_pkg pacman -S --noconfirm --needed \
                base-devel libao bzip2 automake zlib || true ;;
      zypper) run_pkg zypper --non-interactive install \
                gcc gcc-c++ make pkg-config libao-devel bzip2 automake zlib-devel || true ;;
    esac
  }

  _have_cc() { command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1; }

  # uade123 — Amiga AHX + ~150 "exotica" formats (TFMX, Future Composer, David
  # Whittaker, Rob Hubbard, Hippel, …).  Not in any mainstream Linux archive, so
  # built from source exactly as the Docker image does: bencodetools + libzakalwe
  # + uade 3.05, into /usr/local.  --without-write-audio skips uade's SDL2/GL
  # scope tool (whose installer needs python-distutils, gone in 3.12) — WAV comes
  # from libao inside uade123 itself.  Build as the calling user; only the three
  # `make install` steps touch /usr/local, so they go through _root.
  build_uade_from_source() {
    section "uade (Amiga AHX / exotica) — building from source"
    _ensure_build_deps
    _have_cc || { warn "no C compiler found — uade skipped (install a toolchain, then re-run)"; return; }
    local T; T="$(mktemp -d)"
    if ( set -e
         git clone -q https://gitlab.com/heikkiorsila/bencodetools.git "$T/bt"
         cd "$T/bt"; git checkout -q v1.0.1 2>/dev/null || true
         ./configure --prefix=/usr/local --without-python >/dev/null
         { make -j"$NPROC" >/dev/null || make -j"$NPROC" >/dev/null || make -j1 >/dev/null; }
         _root make install >/dev/null
         git clone -q https://gitlab.com/hors/libzakalwe.git "$T/lz"
         cd "$T/lz"; git checkout -q v1.0.0 2>/dev/null || true
         ./configure >/dev/null
         { make CC=cc >/dev/null || make CC=cc >/dev/null || make -j1 CC=cc >/dev/null; }
         _root make install PREFIX=/usr/local >/dev/null
         curl -sfL --max-time 300 https://zakalwe.fi/uade/uade3/uade-3.05.tar.bz2 -o "$T/uade.tar.bz2"
         tar xjf "$T/uade.tar.bz2" -C "$T"
         cd "$T"/uade-3.05
         ./configure --prefix=/usr/local --libzakalwe-prefix=/usr/local \
                     --bencode-tools-prefix=/usr/local --without-uadefs --without-write-audio >/dev/null
         { make -j"$NPROC" >/dev/null || make -j"$NPROC" >/dev/null || make -j1 >/dev/null; }
         _root make install >/dev/null
       ); then :; fi
    _root ldconfig 2>/dev/null || true
    rm -rf "$T" 2>/dev/null || _root rm -rf "$T" 2>/dev/null || true
    if command -v uade123 >/dev/null 2>&1; then info "uade123: $(command -v uade123)"
    else warn "uade build failed — AHX/exotica disabled (re-run install.sh to retry)"; fi
  }

  # sc68 — native Atari ST .sc68 disks.  The 2.2.1 release predates modern
  # autotools, so three fixes make it build on current Debian: fresh
  # config.guess/config.sub (from the automake package) + explicit --build; the
  # six unsubstituted `#if @HAVE_*@` knobs hardcoded; and -O3→-O1 (emu68 has -O3
  # UB).  Built into /usr/local, incl. its share/sc68/Replay m68k drivers.
  build_sc68_from_source() {
    section "sc68 (Atari ST .sc68) — building from source"
    _ensure_build_deps
    _have_cc || { warn "no C compiler found — sc68 skipped (install a toolchain, then re-run)"; return; }
    local T; T="$(mktemp -d)"
    if ( set -e
         curl -sfL --max-time 300 \
           "https://downloads.sourceforge.net/project/sc68/sc68/2.2.1/sc68-2.2.1.tar.gz" \
           -o "$T/sc68.tar.gz"
         tar xzf "$T/sc68.tar.gz" -C "$T"
         cd "$T"/sc68-2.2.1
         cp /usr/share/automake-*/config.guess ./config.guess
         cp /usr/share/automake-*/config.sub   ./config.sub
         sed -i -e 's/#if @HAVE_VSPRINTF@/#if 1/'  -e 's/#if @HAVE_VSNPRINTF@/#if 1/' \
                -e 's/#if @HAVE_GETENV@/#if 1/'     -e 's/#if @HAVE_ZLIB_H@/#if 1/' \
                -e 's/#if @HAVE_READLINE_READLINE_H@/#if 0/' \
                -e 's/#if @HAVE_READLINE_HISTORY_H@/#if 0/' config_platform68.h.in
         CFLAGS="-fcommon -Wno-implicit-function-declaration" \
           ./configure --prefix=/usr/local --build="$(gcc -dumpmachine)" >/dev/null
         find . -name Makefile -exec sed -i 's/-O3/-O1/g' {} +
         { make -j"$NPROC" >/dev/null 2>&1 || make -j"$NPROC" >/dev/null 2>&1 || make -j1 >/dev/null 2>&1; }
         _root make install >/dev/null
       ); then :; fi
    _root ldconfig 2>/dev/null || true
    rm -rf "$T" 2>/dev/null || _root rm -rf "$T" 2>/dev/null || true
    if command -v sc68 >/dev/null 2>&1; then info "sc68: $(command -v sc68)"
    else warn "sc68 build failed — .sc68 disabled (re-run install.sh to retry)"; fi
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

  # uade123 — AHX + Amiga "exotica".  Not in Debian/Ubuntu's archive; a few
  # distros do package it, so try that first (fast), then fall back to a source
  # build so bare-metal Linux reaches the same renderer set as the Docker image.
  if [ "$PKG" != "none" ] && ! command -v uade123 &>/dev/null; then
    case "$PKG" in
      dnf)    run_pkg dnf install -y uade || true ;;
      pacman) run_pkg pacman -S --noconfirm --needed uade || true ;;
      zypper) run_pkg zypper --non-interactive install uade || true ;;
    esac
    command -v uade123 &>/dev/null || build_uade_from_source
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

  # sc68 — native Atari ST .sc68 disks.  Try a distro package first (a few ship
  # it), then fall back to a source build so .sc68 works on bare-metal Linux the
  # same as in the Docker image.
  if [ "$PKG" != "none" ] && ! command -v sc68 &>/dev/null; then
    case "$PKG" in
      dnf)    run_pkg dnf install -y sc68 || true ;;
      zypper) run_pkg zypper --non-interactive install sc68 || true ;;
    esac
    command -v sc68 &>/dev/null || build_sc68_from_source
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
# Official Linux prebuilts (storage.zxtune.ru) ship for BOTH x86_64 and arm64.
case "$(uname -m)" in
  x86_64)        ZXARCH="x86_64" ;;
  aarch64|arm64) ZXARCH="arm64"  ;;
  *)             ZXARCH="" ;;
esac
ZXTUNE_LINUX_URL="https://storage.zxtune.ru/builds/public/r5100/linux/${ZXARCH}/zxtune_r5100_linux_${ZXARCH}.tar.gz"
if command -v zxtune123 &>/dev/null && zxtune123 --version &>/dev/null; then
  info "zxtune123 already installed: $(command -v zxtune123)"
elif [ "$PLATFORM" = "linux" ] && [ -n "$ZXARCH" ]; then
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
  brew list boost &>/dev/null || brew install boost || true
  ZX_TMP="$(mktemp -d)"
  # Capture brew paths defensively — a failed `brew install boost` or `brew --prefix`
  # must skip this OPTIONAL section, not abort the whole installer (set -e).
  BREW_BOOST="$(brew --prefix boost 2>/dev/null || true)"
  BREW_PREFIX="$(brew --prefix 2>/dev/null || true)"
  if [ -z "$BREW_BOOST" ] || [ -z "$BREW_PREFIX" ]; then
    warn "zxtune123: boost/brew unavailable — skipping (PSF/USF/GSF/… disabled)"
    rm -rf "$ZX_TMP"
  elif git clone -q https://github.com/vitamin-caig/zxtune.git "$ZX_TMP/zxtune" \
      && git -C "$ZX_TMP/zxtune" checkout -q "$ZXTUNE_REV"; then
    ( cd "$ZX_TMP/zxtune" \
      && sed -i '' 's/if (!subLocation\.unique())/if (subLocation.use_count() != 1)/; s/if (!Subdata\.unique())/if (Subdata.use_count() != 1)/' src/core/plugins/archives/raw_supp.cpp \
      && sed -i '' 's/#    define FMT_CONSTEVAL consteval/#    define FMT_CONSTEVAL/' 3rdparty/fmt/include/fmt/core.h \
      && env CPATH="$BREW_BOOST/include" LIBRARY_PATH="$BREW_BOOST/lib" \
         make system.zlib=1 platform=darwin -C apps/zxtune123 -j"$(sysctl -n hw.ncpu)" ) \
      >"$ZX_TMP/build.log" 2>&1 || true
    ZXBIN="$ZX_TMP/zxtune/bin/darwin/release/zxtune123"
    if [ -x "$ZXBIN" ]; then
      # Make the build self-contained so a later `brew upgrade boost` can't orphan
      # it (boost 1.92 relocated program_options::arg into detail:: — renaming the
      # exported symbol — and every earlier build stopped loading).  boost is
      # BSL-1.0 and these are the user's own local dylibs, so bundling is fine.
      # errexit/nounset off around the bundler (its otool/grep plumbing returns 1).
      STAGE="$ZX_TMP/stage"
      set +eu; _bundle_selfcontained "$ZXBIN" "$STAGE"; _ok=$?; set -eu
      if [ "$_ok" -eq 0 ] && "$STAGE/zxtune123" --version >/dev/null 2>&1; then
        _install_selfcontained "$STAGE" "$BREW_PREFIX/bin" || true
      else
        warn "zxtune123 self-contained bundling failed — see $ZX_TMP/build.log (PSF/USF/GSF/… disabled)"
      fi
    else
      warn "zxtune123 build failed — see $ZX_TMP/build.log"
    fi
  fi
  # Keep the temp tree (and build.log) on failure; clean up only on a verified-
  # working install (mirrors sidplayfp — a rebuild-over-orphan failure keeps its log).
  command -v zxtune123 &>/dev/null && zxtune123 --version &>/dev/null && rm -rf "$ZX_TMP"
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

# If no admin exists yet (e.g. the first-run prompt was skipped on a
# non-interactive install), say so loudly — otherwise the user meets a locked
# sign-in page with no hint how to proceed.
if [ -x "$SETADM" ] && ! "$VENV/bin/python" -c "import sys; from soniqboom.config import get_data_dir; from soniqboom.core.users import UserStore; sys.exit(0 if UserStore(get_data_dir()).has_any_admin() else 1)" >/dev/null 2>&1; then
  echo -e "  ${YELLOW}${BOLD}No admin account yet${RESET} — create one before you can sign in:"
  echo -e "      ${BOLD}bash setup-admin.sh -user <name> -passwd '<password>'${RESET}"
  echo ""
fi
