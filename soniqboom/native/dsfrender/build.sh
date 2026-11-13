#!/bin/sh
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build dsfrender (Sega Dreamcast / Naomi DSF → WAV) against a pinned Highly
# Theoretical and psflib.
#
#   [LICENSE_DIR=dir] build.sh DEST_DIR [HT_SOURCE_DIR PSFLIB_SOURCE_DIR]
#
# Installs DEST_DIR/dsfrender (and, with LICENSE_DIR, the licence files).
# Without the source folders, Highly Theoretical is cloned at HT_COMMIT and
# psflib at PSFLIB_COMMIT.  Only the Dreamcast part of Highly Theoretical is
# compiled (DISABLE_SSF: sega.c, dcsound.c, yam.c, arm.c) — none of its 68000
# cores (Musashi, Starscream, C68K), which only the Saturn needs — and of
# psflib only psflib.c.  yam.c's x86 `regparm` calling-convention attribute
# is compiled away (-D'regparm(n)=').  Needs a C compiler, git and zlib's
# headers.  Licences: soniqboom/native/dsfrender/README.md.
set -eu

HT_COMMIT="${HT_COMMIT:-2998a4bf550949cd2daee249e725a64462cf15e0}"
PSFLIB_COMMIT="${PSFLIB_COMMIT:-95509e0c6f13d769593bbf51a1b0e0efdc355ba1}"
dest="${1:?usage: build.sh DEST_DIR [HT_SOURCE_DIR PSFLIB_SOURCE_DIR]}"
here="$(cd "$(dirname "$0")" && pwd)"
cc="${CC:-cc}"

work="$(mktemp -d "${TMPDIR:-/tmp}/dsfrender.XXXXXX")"
trap 'rm -rf "$work"' EXIT

if [ "${2:-}" ]; then
  ht="$2"
  pl="${3:?usage: build.sh DEST_DIR [HT_SOURCE_DIR PSFLIB_SOURCE_DIR]}"
else
  ht="$work/Highly_Theoretical"
  pl="$work/psflib"
  git clone --quiet https://github.com/kode54/Highly_Theoretical.git "$ht"
  git -C "$ht" checkout --quiet "$HT_COMMIT"
  git clone --quiet https://github.com/kode54/psflib.git "$pl"
  git -C "$pl" checkout --quiet "$PSFLIB_COMMIT"
fi

core="$ht/Core"
"$cc" -O2 -std=gnu99 -w \
  -DEMU_COMPILE -DEMU_LITTLE_ENDIAN -DHAVE_STDINT_H -DDISABLE_SSF -D'regparm(n)=' \
  -DHT_COMMIT="\"$(echo "$HT_COMMIT" | cut -c1-12)\"" \
  -DPSFLIB_COMMIT="\"$(echo "$PSFLIB_COMMIT" | cut -c1-12)\"" \
  -I"$core" -I"$pl" \
  "$here/dsfrender.c" \
  "$core/sega.c" "$core/dcsound.c" "$core/yam.c" "$core/arm.c" \
  "$pl/psflib.c" \
  -lz -lm -o "$work/dsfrender"

mkdir -p "$dest"
install -m 0755 "$work/dsfrender" "$dest/dsfrender"
# LICENSE_DIR: where Highly Theoretical's and psflib's licence files go.
if [ "${LICENSE_DIR:-}" ]; then
  mkdir -p "$LICENSE_DIR"
  cp "$ht/LICENSE.TXT" "$LICENSE_DIR/Highly_Theoretical-LICENSE.TXT"
  cp "$pl/LICENSE" "$LICENSE_DIR/psflib-LICENSE"
fi
"$dest/dsfrender" --version
