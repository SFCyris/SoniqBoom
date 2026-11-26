#!/bin/sh
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build midirender (MIDI-family songs → WAV through OPL3 / OPN2 FM or an
# MT-32) against pinned releases of libADLMIDI, libOPNMIDI and libmt32emu.
#
#   [LICENSE_DIR=dir] build.sh DEST_DIR [ADLMIDI_SRC OPNMIDI_SRC MUNT_SRC]
#
# Installs DEST_DIR/midirender (and, with LICENSE_DIR — the third-party
# licence folder, e.g. /usr/local/share/doc/third-party —, the three
# libraries' licence texts and the FM banks' notices in its libadlmidi/,
# libopnmidi/ and munt/ folders).  Without source folders the
# release archives are downloaded from GitHub and checked against their
# SHA-256 (ADLMIDI_SHA256 / OPNMIDI_SHA256 / MUNT_SHA256).  Each library is
# built unmodified as a static library with its own CMake build:
#   * libADLMIDI with its embedded FM banks minus the "grey zone" ones whose
#     terms are unclear (BUILD_NO_GREY_BANKS), the Nuked OPL3 and DOSBox
#     emulators, the MIDI sequencer and XMI support;
#   * libOPNMIDI with the Nuked OPN2 and YMFM emulators, the sequencer and
#     XMI support — not its MAME emulators: the YM2608 one compiles a
#     GPL-2.0-only resampler (src/chips/mamefm/resampler.*), which can't be
#     combined with AGPL-3.0 code, and the YM2612 one's file states no
#     licence; its xg.wopn bank (MIT) is compiled into midirender;
#   * libmt32emu (munt) with its C interface only — no ROMs: they are the
#     user's (Settings → Renderers → MT-32 ROM folder).
# libADLMIDI is required; a libOPNMIDI or libmt32emu that fails to build
# leaves midirender without OPN2 / MT-32 (`midirender -L` lists what it has).
# Needs cmake, make, a C and a C++14 compiler and curl.  Licences:
# soniqboom/native/midirender/README.md.
set -eu

ADLMIDI_VERSION="${ADLMIDI_VERSION:-1.6.2.1}"
ADLMIDI_SHA256="${ADLMIDI_SHA256:-e01aec7b2bc284476eedab77a8b6e177c5beda2bc2f4545166339badfc0464c5}"
OPNMIDI_VERSION="${OPNMIDI_VERSION:-1.6.2}"
OPNMIDI_SHA256="${OPNMIDI_SHA256:-59bc4e9a88d57fa810b499932b829cd80cb88fce100bab9e3da82158edd5550d}"
MUNT_VERSION="${MUNT_VERSION:-2_8_4}"
MUNT_SHA256="${MUNT_SHA256:-75f29ed4b56c830e71005ce2d3ffc0b6b19a89ca8e3119054fa0d315b8bcc384}"
dest="${1:?usage: build.sh DEST_DIR [ADLMIDI_SRC OPNMIDI_SRC MUNT_SRC]}"
here="$(cd "$(dirname "$0")" && pwd)"
cc="${CC:-cc}"
cxx="${CXX:-c++}"
jobs="${JOBS:-$( (nproc || sysctl -n hw.ncpu) 2>/dev/null || echo 2)}"

work="$(mktemp -d "${TMPDIR:-/tmp}/midirender.XXXXXX")"
trap 'rm -rf "$work"' EXIT

_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# _fetch NAME URL SHA256 → the unpacked source folder on stdout
_fetch() {
  tgz="$work/$1.tar.gz"
  curl -fsSL --max-time 300 -o "$tgz" "$2"
  got="$(_sha256 "$tgz")"
  if [ "$got" != "$3" ]; then
    echo "build.sh: $1.tar.gz has SHA-256 $got, expected $3" >&2
    return 1
  fi
  mkdir -p "$work/src-$1"
  tar -xzf "$tgz" -C "$work/src-$1" --strip-components=1
  echo "$work/src-$1"
}

if [ "${2:-}" ]; then
  adl_src="$2"; opn_src="${3:-}"; munt_src="${4:-}"
else
  adl_src="$(_fetch libADLMIDI \
    "https://github.com/Wohlstand/libADLMIDI/archive/refs/tags/v$ADLMIDI_VERSION.tar.gz" \
    "$ADLMIDI_SHA256")"
  opn_src="$(_fetch libOPNMIDI \
    "https://github.com/Wohlstand/libOPNMIDI/archive/refs/tags/v$OPNMIDI_VERSION.tar.gz" \
    "$OPNMIDI_SHA256")" || opn_src=""
  munt_src="$(_fetch munt \
    "https://github.com/munt/munt/archive/refs/tags/munt_$MUNT_VERSION.tar.gz" \
    "$MUNT_SHA256")" || munt_src=""
fi

prefix="$work/prefix"
cmake -S "$adl_src" -B "$work/adl" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" \
  -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
  -DlibADLMIDI_STATIC=ON -DlibADLMIDI_SHARED=OFF -DBUILD_NO_GREY_BANKS=ON \
  -DWITH_MIDI_SEQUENCER=ON -DWITH_EMBEDDED_BANKS=ON -DWITH_XMI_SUPPORT=ON \
  -DWITH_HQ_RESAMPLER=OFF -DWITH_GENADLDATA=OFF -DUSE_NUKED_EMULATOR=ON \
  -DUSE_DOSBOX_EMULATOR=ON -DUSE_OPAL_EMULATOR=OFF -DUSE_JAVA_EMULATOR=OFF \
  -DUSE_ESFMU_EMULATOR=OFF -DUSE_MAME_EMULATOR=OFF -DUSE_YMFM_EMULATOR=OFF >"$work/adl.log" 2>&1 \
  && cmake --build "$work/adl" --parallel "$jobs" >>"$work/adl.log" 2>&1 \
  && cmake --install "$work/adl" >>"$work/adl.log" 2>&1 \
  || { tail -40 "$work/adl.log" >&2; echo "build.sh: libADLMIDI failed to build" >&2; exit 1; }

defs="" libs="$prefix/lib/libADLMIDI.a"
if [ -n "$opn_src" ] && cmake -S "$opn_src" -B "$work/opn" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$prefix" -DCMAKE_INSTALL_LIBDIR=lib \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DlibOPNMIDI_STATIC=ON -DlibOPNMIDI_SHARED=OFF \
    -DWITH_MIDI_SEQUENCER=ON -DWITH_XMI_SUPPORT=ON -DWITH_HQ_RESAMPLER=OFF \
    -DUSE_NUKED_EMULATOR=ON -DUSE_MAME_EMULATOR=OFF -DUSE_YMFM_EMULATOR=ON \
    -DUSE_GENS_EMULATOR=OFF -DUSE_NP2_EMULATOR=OFF -DUSE_MAME_2608_EMULATOR=OFF \
    -DUSE_VGM_FILE_DUMPER=OFF >"$work/opn.log" 2>&1 \
   && cmake --build "$work/opn" --parallel "$jobs" >>"$work/opn.log" 2>&1 \
   && cmake --install "$work/opn" >>"$work/opn.log" 2>&1; then
  # (the default OPN2 bank, compiled in: Wohlstand's xg.wopn, MIT)
  { echo "static const unsigned char opn_bank[] = {"
    od -An -v -tu1 "$opn_src/fm_banks/xg.wopn" | sed 's/[0-9][0-9]*/&,/g'
    echo "};"; } > "$work/opn_bank.h"
  defs="$defs -DHAVE_OPNMIDI"
  libs="$libs $prefix/lib/libOPNMIDI.a"
else
  [ -n "$opn_src" ] && tail -20 "$work/opn.log" >&2
  echo "build.sh: libOPNMIDI didn't build — midirender without OPN2" >&2
fi

if [ -n "$munt_src" ] && cmake -S "$munt_src/mt32emu" -B "$work/mt32" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$prefix" -DCMAKE_INSTALL_LIBDIR=lib \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON -Dlibmt32emu_SHARED=OFF \
    -Dlibmt32emu_C_INTERFACE=ON -Dlibmt32emu_CPP_INTERFACE=OFF \
    -Dlibmt32emu_BUILD_TESTING=OFF >"$work/mt32.log" 2>&1 \
   && cmake --build "$work/mt32" --parallel "$jobs" >>"$work/mt32.log" 2>&1 \
   && cmake --install "$work/mt32" >>"$work/mt32.log" 2>&1; then
  defs="$defs -DHAVE_MT32EMU"
  libs="$libs $prefix/lib/libmt32emu.a"
else
  [ -n "$munt_src" ] && tail -20 "$work/mt32.log" >&2
  echo "build.sh: libmt32emu didn't build — midirender without MT-32" >&2
fi

# shellcheck disable=SC2086
"$cc" -O2 -std=c99 -D_POSIX_C_SOURCE=200809L $defs -I"$prefix/include" -I"$work" \
  -c "$here/midirender.c" -o "$work/midirender.o"
# (the libraries are C++: linked by the C++ compiler)
# shellcheck disable=SC2086
"$cxx" -O2 "$work/midirender.o" $libs -lm -o "$work/midirender"

mkdir -p "$dest"
install -m 0755 "$work/midirender" "$dest/midirender"
if [ "${LICENSE_DIR:-}" ]; then
  # libADLMIDI: its LICENSE (the LGPL-3.0 text) + GPL-3.0 + LGPL-2.1, its
  # README's per-part licence list, the embedded banks' notices
  d="$LICENSE_DIR/libadlmidi"
  mkdir -p "$d/fm_banks"
  {
    echo "libADLMIDI $ADLMIDI_VERSION (https://github.com/Wohlstand/libADLMIDI)"
    echo "Built with BUILD_NO_GREY_BANKS=ON: the banks of fm_banks_new/greyzone are not included."
    echo
    sed -n '/^# License/,/^## /p' "$adl_src/README.md" | sed '$d'
  } > "$d/NOTICE"
  cp "$adl_src/LICENSE" "$d/LICENSE"
  cp "$adl_src/LICENSE.GPL-3.txt" "$adl_src/LICENSE.LGPL-2.1.txt" "$d/"
  for f in "$adl_src"/fm_banks_new/LICENSE-*.txt; do
    if [ -f "$f" ]; then cp "$f" "$d/fm_banks/"; fi
  done
  # (its MIDI sequencer's MIT text)
  if [ -f "$adl_src/src/midiseq/LICENSE.txt" ]; then
    cp "$adl_src/src/midiseq/LICENSE.txt" "$d/midiseq-LICENSE.txt"; fi
  if [ -n "$opn_src" ] && [ -f "$work/opn_bank.h" ]; then
    d="$LICENSE_DIR/libopnmidi"
    mkdir -p "$d"
    {
      echo "libOPNMIDI $OPNMIDI_VERSION (https://github.com/Wohlstand/libOPNMIDI)"
      echo
      sed -n '/^# License/,/^## /p' "$opn_src/README.md" | sed '$d'
      echo
      echo "fm_banks/xg.wopn (compiled into midirender as its default OPN2 bank):"
      echo
      cat "$opn_src/fm_banks/readme.txt"
    } > "$d/NOTICE"
    cp "$opn_src/LICENSE" "$d/LICENSE"
    cp "$opn_src/LICENSE.GPL-3.txt" "$opn_src/LICENSE.LGPL-2.1.txt" "$d/"
    if [ -f "$opn_src/src/midiseq/LICENSE.txt" ]; then
      cp "$opn_src/src/midiseq/LICENSE.txt" "$d/midiseq-LICENSE.txt"; fi
    # (the YMFM emulator's BSD-3-Clause notice: its source files' header)
    sed -n '1,/^$/p' "$opn_src/src/chips/ymfm/ymfm_opn.cpp" > "$d/ymfm-LICENSE"
  fi
  if [ -n "$munt_src" ] && [ -f "$prefix/lib/libmt32emu.a" ]; then
    d="$LICENSE_DIR/munt"
    mkdir -p "$d"
    cp "$munt_src/mt32emu/COPYING.LESSER.txt" "$munt_src/mt32emu/COPYING.txt" "$d/"
    # (its SHA-1 code's BSD-3-Clause notice: the source file's header)
    sed -n '1,/\*\//p' "$munt_src/mt32emu/src/sha1/sha1.cpp" > "$d/sha1-LICENSE"
    printf '%s\n' "libmt32emu (munt $MUNT_VERSION, https://github.com/munt/munt): LGPL-2.1-or-later" \
      "(COPYING.LESSER.txt; COPYING.txt is the GPL-2.0 it refers to).  No ROM images are included." \
      > "$d/NOTICE"
  fi
fi
"$dest/midirender" --version
