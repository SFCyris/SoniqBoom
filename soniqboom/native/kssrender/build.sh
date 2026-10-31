#!/bin/sh
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build kssrender (MSX / Sega Master System KSS → WAV) against a pinned libkss.
#
#   [LICENSE_DIR=dir] build.sh DEST_DIR [LIBKSS_SOURCE_DIR]
#
# Installs DEST_DIR/kssrender (and, with LICENSE_DIR, the licence files).
# Without LIBKSS_SOURCE_DIR, libkss is cloned at LIBKSS_COMMIT with only its
# chip-emulator submodules (emu2149, emu2212,
# emu2413, emu8950, emu76489 — MIT; kmz80 — public domain): never its
# kss-drivers submodule, whose drivers are not under libkss's licence.  It is
# compiled with EXCLUDE_DRIVER_ALL — a KSS file carries its own driver; the
# MGS / MuSICA / OPX / MPK formats those drivers convert are not played.
# Needs a C compiler and git (no cmake).  Licences: soniqboom/native/kssrender/README.md.
set -eu

LIBKSS_COMMIT="${LIBKSS_COMMIT:-2ed958823237d054da1ca7965620d8e62a0f9a1d}"
dest="${1:?usage: build.sh DEST_DIR [LIBKSS_SOURCE_DIR]}"
here="$(cd "$(dirname "$0")" && pwd)"
cc="${CC:-cc}"

work="$(mktemp -d "${TMPDIR:-/tmp}/kssrender.XXXXXX")"
trap 'rm -rf "$work"' EXIT

if [ "${2:-}" ]; then
  src="$2"
else
  src="$work/libkss"
  git clone --quiet https://github.com/digital-sound-antiques/libkss.git "$src"
  git -C "$src" checkout --quiet "$LIBKSS_COMMIT"
  git -C "$src" submodule update --init --quiet \
    modules/emu2149 modules/emu2212 modules/emu2413 modules/emu8950 \
    modules/emu76489 modules/kmz80
fi

m="$src/modules"
# shellcheck disable=SC2046
"$cc" -O2 -std=gnu99 -DEXCLUDE_DRIVER_ALL \
  -DLIBKSS_COMMIT="\"$(echo "$LIBKSS_COMMIT" | cut -c1-12)\"" \
  -I"$src/src" -I"$m" -I"$m/kmz80" \
  "$here/kssrender.c" \
  $(find "$src/src" -name '*.c' | sort) \
  "$m/emu2149/emu2149.c" "$m/emu2212/emu2212.c" "$m/emu2413/emu2413.c" \
  "$m/emu8950/emu8950.c" "$m/emu8950/emuadpcm.c" "$m/emu76489/emu76489.c" \
  "$m/kmz80/kmdmg.c" "$m/kmz80/kmevent.c" "$m/kmz80/kmr800.c" "$m/kmz80/kmz80.c" \
  "$m/kmz80/kmz80c.c" "$m/kmz80/kmz80t.c" \
  -lm -o "$work/kssrender"

mkdir -p "$dest"
install -m 0755 "$work/kssrender" "$dest/kssrender"
# LICENSE_DIR: where libkss's and its chip cores' licence files go.
if [ "${LICENSE_DIR:-}" ]; then
  mkdir -p "$LICENSE_DIR"
  cp "$src/LICENSE.md" "$LICENSE_DIR/libkss-LICENSE.md"
  for core in emu2149 emu2212 emu2413 emu8950 emu76489; do
    cp "$m/$core/LICENSE" "$LICENSE_DIR/$core-LICENSE"
  done
  cp "$m/kmz80/kmz80.txt" "$LICENSE_DIR/kmz80.txt"
fi
"$dest/kssrender" --version
