#!/bin/sh
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build Furnace's headless player (FamiTracker .ftm → WAV) at a pinned commit.
#
#   [LICENSE_DIR=dir] [SELFTEST_DIR=dir] build.sh DEST_DIR [FURNACE_SOURCE_DIR]
#
# Installs DEST_DIR/furnace-headless (and, with LICENSE_DIR, its licence files).
# Without FURNACE_SOURCE_DIR, Furnace is cloned at FURNACE_COMMIT (the
# v0.6.8.3 release) with only the two submodules a headless build compiles
# (extern/fmt, extern/adpcm) and built unmodified with CMake: no GUI, no SDL,
# no audio device, MIDI, JACK or PortAudio — it plays a song to a WAV file
# (``furnace -output OUT.wav FILE``) through its vendored libsndfile.  Needs
# CMake, a C and a C++17 compiler, git and zlib's headers (the system zlib:
# Furnace's vendored copy doesn't compile against current macOS SDKs).  Licences:
# soniqboom/native/furnace/README.md.
set -eu

FURNACE_COMMIT="${FURNACE_COMMIT:-f2619dbd988c92bf9c7173648d191648f60aecbf}"
dest="${1:?usage: build.sh DEST_DIR [FURNACE_SOURCE_DIR]}"
jobs="${JOBS:-$( (nproc || sysctl -n hw.ncpu) 2>/dev/null || echo 2)}"

work="$(mktemp -d "${TMPDIR:-/tmp}/furnace.XXXXXX")"
trap 'rm -rf "$work"' EXIT

if [ "${2:-}" ]; then
  src="$2"
else
  src="$work/furnace"
  git init --quiet "$src"
  git -C "$src" remote add origin https://github.com/tildearrow/furnace.git
  git -C "$src" fetch --quiet --depth 1 origin "$FURNACE_COMMIT"
  git -C "$src" checkout --quiet FETCH_HEAD
  git -C "$src" submodule update --init --quiet --depth 1 extern/fmt extern/adpcm
fi

cmake -S "$src" -B "$work/build" -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_GUI=OFF -DUSE_SDL2=OFF -DUSE_RTMIDI=OFF -DWITH_PORTAUDIO=OFF \
  -DWITH_JACK=OFF -DUSE_BACKWARD=OFF -DWITH_LOCALE=OFF -DWITH_INSTRUMENTS=OFF \
  -DWITH_DEMOS=OFF -DUSE_FREETYPE=OFF -DUSE_SNDFILE=ON \
  -DWITH_RENDER_SDL=OFF -DWITH_RENDER_OPENGL=OFF -DWITH_RENDER_OPENGL1=OFF \
  -DWITH_RENDER_METAL=OFF -DSYSTEM_ZLIB=ON > "$work/cmake.log" 2>&1 \
  || { tail -40 "$work/cmake.log" >&2; exit 1; }
cmake --build "$work/build" --target furnace -j"$jobs" > "$work/build.log" 2>&1 \
  || { tail -60 "$work/build.log" >&2; exit 1; }

mkdir -p "$dest"
# (named furnace-headless: a "furnace" on PATH is Furnace the tracker, which
# an install must never replace)
install -m 0755 "$work/build/furnace" "$dest/furnace-headless"
# LICENSE_DIR: Furnace's licence (GPL-2.0-or-later) and its LICENSE notes.
# The licence files of its third-party code (extern/: emulation cores,
# libsndfile, fmt …) too, each under extern/<its folder>/.
if [ "${LICENSE_DIR:-}" ]; then
  mkdir -p "$LICENSE_DIR"
  cp "$src/LICENSE" "$LICENSE_DIR/LICENSE"
  [ -f "$src/LICENSE.md" ] && cp "$src/LICENSE.md" "$LICENSE_DIR/LICENSE.md" || true
  for f in "$src"/extern/*/LICENSE* "$src"/extern/*/COPYING* "$src"/extern/*/*/LICENSE* \
           "$src"/extern/*/*/COPYING*; do
    [ -f "$f" ] || continue
    rel="${f#"$src"/}"
    mkdir -p "$LICENSE_DIR/$(dirname "$rel")"
    cp "$f" "$LICENSE_DIR/$rel"
  done
fi
# SELFTEST_DIR: one of its NES demo songs (the Docker build's render check —
# never installed: the demo songs are not under the GPL).
if [ "${SELFTEST_DIR:-}" ]; then
  mkdir -p "$SELFTEST_DIR"
  cp "$src/demos/nes/Famitune1.fur" "$SELFTEST_DIR/"
fi
# (Furnace writes a config folder under HOME — ~/.config/furnace, on macOS
# ~/Library/Application Support/Furnace — even for --version: a temp HOME)
mkdir -p "$work/home"
env -u XDG_CONFIG_HOME HOME="$work/home" "$dest/furnace-headless" --version 2>/dev/null | grep -a "^Furnace version" || true
