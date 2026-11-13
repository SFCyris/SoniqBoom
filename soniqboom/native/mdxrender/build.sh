#!/bin/sh
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build mdxrender (Sharp X68000 MDX + PDX → WAV) against a pinned portable_mdx.
#
#   [LICENSE_DIR=dir] build.sh DEST_DIR [PORTABLE_MDX_SOURCE_DIR]
#
# Installs DEST_DIR/mdxrender (and, with LICENSE_DIR, portable_mdx's licence
# notes).  Without PORTABLE_MDX_SOURCE_DIR, portable_mdx is cloned at
# PORTABLE_MDX_COMMIT and built unmodified: its MXDRV driver (src/mxdrv), its
# X68Sound emulation (src/x68sound) and mdx_util.  Needs a C and a C++
# compiler (C++11) and git (no cmake; macOS: its libiconv).  Licences:
# soniqboom/native/mdxrender/README.md.
set -eu

PORTABLE_MDX_COMMIT="${PORTABLE_MDX_COMMIT:-dc50b060405a9e82c3b64573b4e524cd8ba83ff3}"
dest="${1:?usage: build.sh DEST_DIR [PORTABLE_MDX_SOURCE_DIR]}"
here="$(cd "$(dirname "$0")" && pwd)"
cc="${CC:-cc}"
cxx="${CXX:-c++}"

work="$(mktemp -d "${TMPDIR:-/tmp}/mdxrender.XXXXXX")"
trap 'rm -rf "$work"' EXIT

if [ "${2:-}" ]; then
  src="$2"
else
  src="$work/portable_mdx"
  git clone --quiet https://github.com/yosshin4004/portable_mdx.git "$src"
  git -C "$src" checkout --quiet "$PORTABLE_MDX_COMMIT"
fi

commit12="$(echo "$PORTABLE_MDX_COMMIT" | cut -c1-12)"
# iconv (Shift-JIS PDX names): in the C library on Linux, libiconv on macOS
iconv_lib=""
[ "$(uname -s)" = "Darwin" ] && iconv_lib="-liconv"
"$cc" -O2 -I"$src/include" -c "$src/src/mdx_util.c" -o "$work/mdx_util.o"
# shellcheck disable=SC2046
# (-include new: mxdrv_context.cpp uses placement new without <new>, which
# GCC's headers — unlike clang's — don't pull in on their own)
"$cxx" -O2 -std=c++11 -w -include new -I"$src/include" \
  -DPORTABLE_MDX_COMMIT="\"$commit12\"" \
  "$here/mdxrender.cpp" \
  "$src/src/mxdrv/mxdrv.cpp" "$src/src/mxdrv/mxdrv_context.cpp" "$src/src/mxdrv/sound_iocs.cpp" \
  "$src/src/x68sound/x68sound_adpcm.cpp" "$src/src/x68sound/x68sound_lfo.cpp" \
  "$src/src/x68sound/x68sound_op.cpp" "$src/src/x68sound/x68sound_opm.cpp" \
  "$src/src/x68sound/x68sound_pcm8.cpp" "$src/src/x68sound/x68sound.cpp" \
  "$src/src/x68sound/x68sound_context.cpp" \
  "$work/mdx_util.o" -lpthread $iconv_lib -o "$work/mdxrender"

mkdir -p "$dest"
install -m 0755 "$work/mdxrender" "$dest/mdxrender"
# LICENSE_DIR: portable_mdx keeps its licence terms in its readme (its
# "ライセンス" section: Apache-2.0 for Yosshin's files, X68Sound's terms, MXDRV).
if [ "${LICENSE_DIR:-}" ]; then
  mkdir -p "$LICENSE_DIR"
  cp "$src/readme.md" "$LICENSE_DIR/portable_mdx-readme.md"
fi
"$dest/mdxrender" --version
