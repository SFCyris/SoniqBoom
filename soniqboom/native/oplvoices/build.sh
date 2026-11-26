#!/bin/sh
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build oplvoices (the per-channel levels and channel-masked mixes of an
# AdLib / OPL tune, through AdPlug and its Nuked OPL3 core).
#
#   build.sh DEST_DIR
#
# Installs DEST_DIR/oplvoices.  oplvoices.cpp is one C++11 file linked
# against libadplug (LGPL-2.1-or-later, with its headers: Debian
# libadplug-dev, Homebrew adplug); nothing is downloaded.  Needs a C++
# compiler and pkg-config (or ADPLUG_CFLAGS / ADPLUG_LIBS).
# Licences: soniqboom/native/oplvoices/README.md.
set -eu

dest="${1:?usage: build.sh DEST_DIR}"
here="$(cd "$(dirname "$0")" && pwd)"
cxx="${CXX:-c++}"

if [ -z "${ADPLUG_CFLAGS:-}${ADPLUG_LIBS:-}" ]; then
  ADPLUG_CFLAGS="$(pkg-config --cflags adplug)"
  ADPLUG_LIBS="$(pkg-config --libs adplug)"
fi
# adplug.pc names include/libbinio; the sources include <adplug/…>.
inc="$(pkg-config --variable=includedir adplug 2>/dev/null || true)"

work="$(mktemp -d "${TMPDIR:-/tmp}/oplvoices.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# shellcheck disable=SC2086
"$cxx" -O2 -std=c++11 ${inc:+-I"$inc"} $ADPLUG_CFLAGS "$here/oplvoices.cpp" \
  $ADPLUG_LIBS -o "$work/oplvoices"

mkdir -p "$dest"
install -m 0755 "$work/oplvoices" "$dest/oplvoices"
"$dest/oplvoices" --version
