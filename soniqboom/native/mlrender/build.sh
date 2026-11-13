#!/bin/sh
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build mlrender (Musicline Editor module → WAV) against a pinned
# musicline_playback, the C++ port of the Musicline Editor 1.16 replayer that
# DEViLBOX carries in third-party/musicline_playback-main.
#
#   [LICENSE_DIR=dir] build.sh DEST_DIR [DEVILBOX_SOURCE_DIR]
#
# Installs DEST_DIR/mlrender (and, with LICENSE_DIR, the licence files).
# Without DEVILBOX_SOURCE_DIR, DEViLBOX is fetched at DEVILBOX_COMMIT as a
# shallow, blob-less sparse checkout of only DEViLBOX's LICENSE and
# THIRD_PARTY_NOTICES.md and the port's replayer sources
# (musicline_playback-main/musicline/*.cpp, *.h and player/mline_backend.*):
# never the rest of DEViLBOX, the port's uade/ copy or anything else in it.
# The port is built with its tick buffers resized (MAX_INTERNAL_TICK, below)
# and SoniqBoom's bounds checks (musicline_playback.patch) applied.
# Needs a C++20 compiler and git (no cmake).  Licences:
# soniqboom/native/mlrender/README.md.
set -eu

DEVILBOX_COMMIT="${DEVILBOX_COMMIT:-57b3e19d2f312c70501f9675436062b947f94acb}"
dest="${1:?usage: build.sh DEST_DIR [DEVILBOX_SOURCE_DIR]}"
here="$(cd "$(dirname "$0")" && pwd)"
cxx="${CXX:-c++}"

work="$(mktemp -d "${TMPDIR:-/tmp}/mlrender.XXXXXX")"
trap 'rm -rf "$work"' EXIT

port=third-party/musicline_playback-main/musicline
if [ "${2:-}" ]; then
  src="$2"
else
  src="$work/devilbox"
  git init --quiet "$src"
  git -C "$src" remote add origin https://github.com/spotUP/DEViLBOX.git
  git -C "$src" config core.sparseCheckout true
  printf '%s\n' /LICENSE /THIRD_PARTY_NOTICES.md "/$port/*.cpp" "/$port/*.h" \
    "/$port/player/mline_backend.cpp" "/$port/player/mline_backend.h" \
    > "$src/.git/info/sparse-checkout"
  git -C "$src" fetch --quiet --depth 1 --filter=blob:none origin "$DEVILBOX_COMMIT"
  git -C "$src" checkout --quiet FETCH_HEAD
fi

# The replayer's tick buffers (MAX_INTERNAL_TICK frames at its 28150 Hz) are
# sized here for its slowest tempo, 32: 2400 frames (1200 upstream).
m="$work/port"
mkdir -p "$m/player"
cp "$src/$port/"*.cpp "$src/$port/"*.h "$m/"
cp "$src/$port/player/mline_backend.cpp" "$src/$port/player/mline_backend.h" "$m/player/"
sed 's/^static constexpr int MAX_INTERNAL_TICK = 1200;/static constexpr int MAX_INTERNAL_TICK = 2400;/' \
  "$src/$port/module.h" > "$m/module.h"
grep -q '^static constexpr int MAX_INTERNAL_TICK = 2400;' "$m/module.h" \
  || { echo "build.sh: module.h has no MAX_INTERNAL_TICK = 1200 to resize" >&2; exit 1; }
# musicline_playback.patch: bounds checks in the replayer (README.md, "Changes
# to the replayer").  git apply changes nothing unless every hunk applies.
git init --quiet "$m"
cp "$here/ml_bounds.h" "$m/"
git -C "$m" apply "$here/musicline_playback.patch" \
  || { echo "build.sh: musicline_playback.patch does not apply at $DEVILBOX_COMMIT" >&2; exit 1; }
# (-fsigned-char: the replayer's tables and sample reads take plain char as signed;
# C++20: its shifts of negative values are defined there)
# shellcheck disable=SC2046
"$cxx" -std=c++20 -O2 -fsigned-char -w \
  -DMUSICLINE_COMMIT="\"$(echo "$DEVILBOX_COMMIT" | cut -c1-12)\"" \
  -I"$m" -I"$m/player" \
  "$here/mlrender.cpp" "$m/player/mline_backend.cpp" \
  $(find "$m" -maxdepth 1 -name '*.cpp' | sort) \
  -lm -o "$work/mlrender"

mkdir -p "$dest"
install -m 0755 "$work/mlrender" "$dest/mlrender"
# LICENSE_DIR: where the licence files go — DEViLBOX's licence (GPL-3.0) and
# its third-party notices, which state the port's licence.
if [ "${LICENSE_DIR:-}" ]; then
  mkdir -p "$LICENSE_DIR"
  cp "$src/LICENSE" "$LICENSE_DIR/DEViLBOX-LICENSE"
  cp "$src/THIRD_PARTY_NOTICES.md" "$LICENSE_DIR/DEViLBOX-THIRD_PARTY_NOTICES.md"
fi
"$dest/mlrender" --version
