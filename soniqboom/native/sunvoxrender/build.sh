#!/bin/sh
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build sunvoxrender (SunVox project → WAV) against a pinned SunVox Library.
#
#   [LICENSE_DIR=dir] [SELFTEST_DIR=dir] build.sh DEST_DIR [SUNVOX_LIB_ZIP]
#
# Installs DEST_DIR/sunvoxrender (and, with LICENSE_DIR, the SunVox Library's
# licence texts).  Without SUNVOX_LIB_ZIP, the library's release archive
# (sunvox_lib-SUNVOX_LIB_VERSION.zip, which holds its full source) is
# downloaded from warmplace.ru and checked against SUNVOX_LIB_SHA256; the
# library is built from that source, unmodified, as a static library with
# its own Makefile and linked in.  Needs make, a C and a C++ compiler, curl
# and unzip; on Linux the ALSA and JACK headers (libasound2-dev,
# libjack-jackd2-dev — the library links ALSA and loads JACK only when asked
# to; sunvoxrender opens no audio device).  Licences:
# soniqboom/native/sunvoxrender/README.md.
set -eu

SUNVOX_LIB_VERSION="${SUNVOX_LIB_VERSION:-2.1.4d}"
SUNVOX_LIB_SHA256="${SUNVOX_LIB_SHA256:-abe851de9d65a10e06673bf33257154591a2ff63ad2bb298488a5941cc5f4057}"
dest="${1:?usage: build.sh DEST_DIR [SUNVOX_LIB_ZIP]}"
here="$(cd "$(dirname "$0")" && pwd)"
cc="${CC:-cc}"
jobs="${JOBS:-$( (nproc || sysctl -n hw.ncpu) 2>/dev/null || echo 2)}"

work="$(mktemp -d "${TMPDIR:-/tmp}/sunvoxrender.XXXXXX")"
trap 'rm -rf "$work"' EXIT

zip="${2:-}"
if [ -z "$zip" ]; then
  zip="$work/sunvox_lib.zip"
  curl -sfL --max-time 300 -o "$zip" \
    "https://warmplace.ru/soft/sunvox/sunvox_lib-$SUNVOX_LIB_VERSION.zip"
fi
sum="$( (sha256sum "$zip" 2>/dev/null || shasum -a 256 "$zip") | cut -d' ' -f1)"
if [ "$sum" != "$SUNVOX_LIB_SHA256" ]; then
  echo "build.sh: sunvox_lib-$SUNVOX_LIB_VERSION.zip has SHA-256 $sum, not $SUNVOX_LIB_SHA256" >&2
  exit 1
fi
unzip -q "$zip" -d "$work/src"
lib="$work/src/sunvox_lib"

case "$(uname -s)" in
  Darwin) os=macos
          libs="-lpthread -lc++ -framework Cocoa -framework Carbon -framework AudioUnit -framework CoreAudio"
          # (the Makefile's own SDK and toolchain paths name one Xcode version;
          # its macOS static-library rule uses a libtool it sets only for iOS)
          extra="MACOS_SDK=$(xcrun --show-sdk-path) MACOS_TOOLCHAIN_BIN=$(dirname "$(xcrun -f clang)")"
          extra="$extra LIBTOOL=$(xcrun -f libtool) LIBTOOL_FLAGS=-static" ;;
  *)      os=linux
          libs="-lpthread -lstdc++ -lasound -lm -ldl"
          extra="STYPE=PS_STYPE_FLOAT32" ;;
esac
case "$(uname -m)" in
  x86_64|amd64)  arch=x86_64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo "build.sh: no SunVox Library build for $(uname -m)" >&2; exit 1 ;;
esac

# shellcheck disable=SC2086
( cd "$lib/sunvox_lib/make" \
  && make -j"$jobs" MAKE_STATIC_LIB=true TARGET_OS="$os" TARGET_ARCH="$arch" $extra \
       > "$work/make.log" 2>&1 ) || { tail -40 "$work/make.log" >&2; exit 1; }

# shellcheck disable=SC2086
"$cc" -O2 -I"$lib/sunvox_lib/headers" \
  -DSUNVOX_LIB_VERSION="\"$SUNVOX_LIB_VERSION\"" \
  "$here/sunvoxrender.c" "$lib/sunvox_lib/make/sunvox.a" $libs -o "$work/sunvoxrender"

mkdir -p "$dest"
install -m 0755 "$work/sunvoxrender" "$dest/sunvoxrender"
# LICENSE_DIR: the library's licence texts — its use terms (the "Powered by
# SunVox" line) with the texts they name, and its source's MIT licence.
if [ "${LICENSE_DIR:-}" ]; then
  mkdir -p "$LICENSE_DIR"
  cp "$lib/sunvox_lib/docs/license/"*.txt "$LICENSE_DIR/"
  cp "$lib/lib_sunvox/LICENSE" "$LICENSE_DIR/lib_sunvox-LICENSE"
fi
# SELFTEST_DIR: one of the example projects the archive ships (the Docker
# build's render check — never installed).
if [ "${SELFTEST_DIR:-}" ]; then
  mkdir -p "$SELFTEST_DIR"
  cp "$lib/sunvox_lib/resources/song01.sunvox" "$SELFTEST_DIR/"
fi
"$dest/sunvoxrender" --version
