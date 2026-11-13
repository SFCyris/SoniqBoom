#!/bin/sh
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build sawteethrender (Sawteeth modules → WAV, a port of NostalgicPlayer's
# Sawteeth player).
#
#   [LICENSE_DIR=dir] build.sh DEST_DIR
#
# Installs DEST_DIR/sawteethrender (and, with LICENSE_DIR, NostalgicPlayer's
# licence as NostalgicPlayer-LICENSE).  sawteethrender.c is one C99 file
# using libc and libm only; nothing is downloaded.  Needs a C compiler.
# Licences: soniqboom/native/sawteethrender/README.md.
set -eu

dest="${1:?usage: build.sh DEST_DIR}"
here="$(cd "$(dirname "$0")" && pwd)"
cc="${CC:-cc}"

work="$(mktemp -d "${TMPDIR:-/tmp}/sawteethrender.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# -ffp-contract=off: no fused multiply-adds (the player's float arithmetic
# is single precision, one rounding per operation).
"$cc" -O2 -std=c99 -ffp-contract=off "$here/sawteethrender.c" -lm -o "$work/sawteethrender"

mkdir -p "$dest"
install -m 0755 "$work/sawteethrender" "$dest/sawteethrender"
if [ "${LICENSE_DIR:-}" ]; then
  mkdir -p "$LICENSE_DIR"
  cp "$here/NostalgicPlayer-LICENSE" "$LICENSE_DIR/NostalgicPlayer-LICENSE"
fi
"$dest/sawteethrender" --version
