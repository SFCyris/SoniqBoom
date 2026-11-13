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
# kss-drivers submodule, whose drivers are not under libkss's licence.  Of
# those drivers only MGSDRV (Ain./Gigamix: free, may be embedded unmodified —
# libkss's form counts as unmodified) is fetched, as the one header file
# drivers/mgsdrv.h at KSS_DRIVERS_COMMIT, checked against MGSDRV_H_SHA256;
# every other driver is compiled out (EXCLUDE_DRIVER_*) — a KSS file carries
# its own driver; the MuSICA / MoonBlaster / OPX / MPK formats those drivers
# convert are not played.  Needs a C compiler, git and curl (no cmake).
# Licences: soniqboom/native/kssrender/README.md.
set -eu

LIBKSS_COMMIT="${LIBKSS_COMMIT:-2ed958823237d054da1ca7965620d8e62a0f9a1d}"
KSS_DRIVERS_COMMIT="${KSS_DRIVERS_COMMIT:-e8032da38ea8439caca699954fce82b5050bf86f}"
MGSDRV_H_SHA256="${MGSDRV_H_SHA256:-2e3a3f7f146e1b8e4dc7d5201245114ff2dca8958fc420c222daa2e9c8f799ca}"
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
# MGSDRV: the one driver header, at the commit libkss's submodule pins.
if [ ! -f "$m/drivers/mgsdrv.h" ]; then
  mkdir -p "$m/drivers"
  curl -sfL --max-time 120 -o "$m/drivers/mgsdrv.h" \
    "https://raw.githubusercontent.com/digital-sound-antiques/kss-drivers/$KSS_DRIVERS_COMMIT/mgsdrv.h"
fi
sum="$( (sha256sum "$m/drivers/mgsdrv.h" 2>/dev/null || shasum -a 256 "$m/drivers/mgsdrv.h") | cut -d' ' -f1)"
if [ "$sum" != "$MGSDRV_H_SHA256" ]; then
  echo "build.sh: drivers/mgsdrv.h has SHA-256 $sum, not $MGSDRV_H_SHA256" >&2
  exit 1
fi
# shellcheck disable=SC2046
"$cc" -O2 -std=gnu99 -DEXCLUDE_DRIVER_KINROU -DEXCLUDE_DRIVER_MBR143 \
  -DEXCLUDE_DRIVER_MPK106 -DEXCLUDE_DRIVER_OPX4KSS \
  -DLIBKSS_COMMIT="\"$(echo "$LIBKSS_COMMIT" | cut -c1-12)\"" \
  -I"$src/src" -I"$m" -I"$m/kmz80" \
  "$here/kssrender.c" \
  $(find "$src/src" -name '*.c' | sort) \
  "$m/emu2149/emu2149.c" "$m/emu2212/emu2212.c" "$m/emu2413/emu2413.c" \
  "$m/emu8950/emu8950.c" "$m/emu8950/emuadpcm.c" "$m/emu76489/emu76489.c" \
  "$m/kmz80/kmdmg.c" "$m/kmz80/kmevent.c" "$m/kmz80/kmr800.c" "$m/kmz80/kmz80.c" \
  "$m/kmz80/kmz80c.c" "$m/kmz80/kmz80t.c" \
  -lm -o "$work/kssrender"

# The MGSDRV driver must be in the binary (a build without it plays an MGS
# song as silence): its first 64 bytes, from the header, found in kssrender.
python3 - "$m/drivers/mgsdrv.h" "$work/kssrender" <<'PY' || { echo "build.sh: MGSDRV is not in kssrender" >&2; exit 1; }
import re, sys
drv = bytes(int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{2})", open(sys.argv[1]).read())[:64])
sys.exit(0 if len(drv) == 64 and drv in open(sys.argv[2], "rb").read() else 1)
PY

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
  # MGSDRV's terms: its copyright line (the header's own comment) and the
  # credit its embedding licence asks for.
  { sed -n '1,/\*\//p' "$m/drivers/mgsdrv.h"
    echo "MGSDRV (C) Ain./Gigamix — https://www.gigamix.jp/mgsdrv/"
    echo "Free software; embedded unmodified (libkss's driver header, as published)."
  } > "$LICENSE_DIR/MGSDRV-NOTICE.txt"
fi
"$dest/kssrender" --version
