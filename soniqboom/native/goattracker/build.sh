#!/bin/sh
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build GoatTracker 2's gt2reloc (song .sng → C64 .sid) at a pinned release.
#
#   [LICENSE_DIR=dir] [SELFTEST_DIR=dir] build.sh DEST_DIR [GOATTRACKER_ZIP]
#
# Installs DEST_DIR/gt2reloc (and, with LICENSE_DIR, GoatTracker's licence
# and author notes).  Without GOATTRACKER_ZIP, the release archive
# GoatTracker_GOATTRACKER_VERSION.zip is downloaded from SourceForge and
# checked against GOATTRACKER_SHA256; gt2reloc is built from its source,
# unmodified, with GoatTracker's own Linux makefile (the object files the
# archive ships are removed first).  SoniqBoom plays the .sid it writes with
# sidplayfp.  Needs make, a C and a C++ compiler, curl, unzip and SDL 1.2's
# headers (sdl12-compat: `sdl-config`) — gt2reloc doesn't link or load SDL
# (sdlstub.c stands in for it).  Licences:
# soniqboom/native/goattracker/README.md.
set -eu

GOATTRACKER_VERSION="${GOATTRACKER_VERSION:-2.77}"
GOATTRACKER_SHA256="${GOATTRACKER_SHA256:-96c2bd6a6ab3aca2f5bb18b1c764ac6ea69ac245cae14002a72cd87c554561ef}"
dest="${1:?usage: build.sh DEST_DIR [GOATTRACKER_ZIP]}"
here="$(cd "$(dirname "$0")" && pwd)"

work="$(mktemp -d "${TMPDIR:-/tmp}/goattracker.XXXXXX")"
trap 'rm -rf "$work"' EXIT

zip="${2:-}"
if [ -z "$zip" ]; then
  zip="$work/goattracker.zip"
  curl -sfL --max-time 300 -o "$zip" \
    "https://sourceforge.net/projects/goattracker2/files/GoatTracker%202/$GOATTRACKER_VERSION/GoatTracker_$GOATTRACKER_VERSION.zip/download"
fi
sum="$( (sha256sum "$zip" 2>/dev/null || shasum -a 256 "$zip") | cut -d' ' -f1)"
if [ "$sum" != "$GOATTRACKER_SHA256" ]; then
  echo "build.sh: GoatTracker_$GOATTRACKER_VERSION.zip has SHA-256 $sum, not $GOATTRACKER_SHA256" >&2
  exit 1
fi
unzip -q "$zip" -d "$work/gt"
src="$work/gt/src"
command -v sdl-config >/dev/null 2>&1 \
  || { echo "build.sh: sdl-config not found — install SDL 1.2 (sdl12-compat)" >&2; exit 1; }

# (the archive ships Windows object files; the generated data file goatdata.c
# is up to date — keep make from trying to regenerate it)
find "$src" -name '*.o' -exec rm -f {} +
touch "$src/goatdata.c"
mkdir -p "$work/gt/linux"
# SDL's headers only: gt2reloc is linked against sdlstub.c (stand-ins for the
# SDL functions the shared tracker code names; gt2reloc calls none) instead
# of the SDL library — see sdlstub.c.  GoatTracker includes <SDL/SDL.h>: the
# folder above sdl-config's (which names the SDL folder itself) is added,
# passed in the environment so the makefile's own CFLAGS += still apply.  On
# macOS SDL.h renames main to SDL_main (for SDLmain, which isn't linked).
sdl_cflags="$(sdl-config --cflags)"
sdl_parent="$(echo "$sdl_cflags" | tr ' ' '\n' | sed -n 's|^-I\(.*\)/SDL$|\1|p' | head -1)"
# shellcheck disable=SC2086
"${CC:-cc}" -O2 $sdl_cflags -c "$here/sdlstub.c" -o "$work/sdlstub.o"
( cd "$src" && CFLAGS="${sdl_parent:+-I$sdl_parent} -DSDL_main=main" \
    make -f makefile CC="${CC:-cc}" CXX="${CXX:-c++}" LIBS="$work/sdlstub.o" \
      ../linux/gt2reloc > "$work/make.log" 2>&1 ) \
  || { tail -40 "$work/make.log" >&2; exit 1; }

mkdir -p "$dest"
install -m 0755 "$work/gt/linux/gt2reloc" "$dest/gt2reloc"
# LICENSE_DIR: GoatTracker's licence (GPL-2.0-or-later) and author notes.
if [ "${LICENSE_DIR:-}" ]; then
  mkdir -p "$LICENSE_DIR"
  cp "$work/gt/copying" "$LICENSE_DIR/COPYING"
  cp "$work/gt/authors" "$LICENSE_DIR/AUTHORS"
  cp "$work/gt/readme_resid.txt" "$LICENSE_DIR/readme_resid.txt"
fi
# SELFTEST_DIR: one of the example songs the archive ships (the Docker
# build's render check — never installed).
if [ "${SELFTEST_DIR:-}" ]; then
  mkdir -p "$SELFTEST_DIR"
  cp "$work/gt/examples/sanction.sng" "$SELFTEST_DIR/"
fi
echo "gt2reloc (GoatTracker $GOATTRACKER_VERSION) installed in $dest"
