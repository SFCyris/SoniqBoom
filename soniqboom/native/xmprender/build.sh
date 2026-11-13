#!/bin/sh
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build xmprender (libxmp modules → WAV) against a pinned libxmp release.
#
#   [LICENSE_DIR=dir] build.sh DEST_DIR [LIBXMP_SOURCE_DIR]
#
# Installs DEST_DIR/xmprender (and, with LICENSE_DIR, libxmp's licence).
# Without LIBXMP_SOURCE_DIR the release archive libxmp-$LIBXMP_VERSION.tar.gz
# is downloaded from GitHub and checked against LIBXMP_SHA256.  libxmp is
# built unmodified as a static library with its own CMake build (no shared
# library, no libxmp-lite), and xmprender.c is linked against it.  Needs
# cmake, make, a C compiler and curl.  Licences:
# soniqboom/native/xmprender/README.md.
set -eu

LIBXMP_VERSION="${LIBXMP_VERSION:-4.7.4}"
LIBXMP_SHA256="${LIBXMP_SHA256:-a25583aa3b031c78ba0b4e83387fa596fb05742622e60f60d1540ee2af79b8d9}"
dest="${1:?usage: build.sh DEST_DIR [LIBXMP_SOURCE_DIR]}"
here="$(cd "$(dirname "$0")" && pwd)"
cc="${CC:-cc}"

work="$(mktemp -d "${TMPDIR:-/tmp}/xmprender.XXXXXX")"
trap 'rm -rf "$work"' EXIT

if [ "${2:-}" ]; then
  src="$2"
else
  tgz="$work/libxmp-$LIBXMP_VERSION.tar.gz"
  curl -fsSL -o "$tgz" \
    "https://github.com/libxmp/libxmp/releases/download/libxmp-$LIBXMP_VERSION/libxmp-$LIBXMP_VERSION.tar.gz"
  if command -v sha256sum >/dev/null 2>&1; then
    got="$(sha256sum "$tgz" | cut -d' ' -f1)"
  else
    got="$(shasum -a 256 "$tgz" | cut -d' ' -f1)"
  fi
  if [ "$got" != "$LIBXMP_SHA256" ]; then
    echo "build.sh: libxmp-$LIBXMP_VERSION.tar.gz has SHA-256 $got, expected $LIBXMP_SHA256" >&2
    exit 1
  fi
  tar -xzf "$tgz" -C "$work"
  src="$work/libxmp-$LIBXMP_VERSION"
fi

cmake -S "$src" -B "$work/build" -DCMAKE_BUILD_TYPE=Release -DBUILD_STATIC=ON \
  -DBUILD_SHARED=OFF -DBUILD_LITE=OFF -DCMAKE_POSITION_INDEPENDENT_CODE=ON >/dev/null
cmake --build "$work/build" --parallel 4 >/dev/null
lib="$(find "$work/build" -name 'libxmp.a' | head -1)"
if [ -z "$lib" ]; then
  echo "build.sh: libxmp's build left no libxmp.a" >&2
  exit 1
fi

"$cc" -O2 -std=c99 -DLIBXMP_STATIC -I"$src/include" "$here/xmprender.c" "$lib" -lm \
  -o "$work/xmprender"

mkdir -p "$dest"
install -m 0755 "$work/xmprender" "$dest/xmprender"
if [ "${LICENSE_DIR:-}" ]; then
  mkdir -p "$LICENSE_DIR"
  # (libxmp: docs/COPYING; its ProWizard loaders: their own MIT notice; a
  # few depackers and the Vorbis decoder say they are in the public domain)
  {
    echo "libxmp $LIBXMP_VERSION (https://github.com/libxmp/libxmp)"
    echo
    cat "$src/docs/COPYING"
    echo
    echo "src/loaders/prowizard:"
    echo
    cat "$src/src/loaders/prowizard/LICENSE.txt"
  } > "$LICENSE_DIR/libxmp-LICENSE"
fi
"$dest/xmprender" --version
