#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Collect the upstream source code of every third-party program the Docker
# image builds or downloads (and of the browser SID engine), exactly at the
# versions pinned in the Dockerfile and soniqboom/native/sidwasm/build.sh.
#
#   scripts/collect_third_party_sources.sh <out-dir>
#
# Writes <out-dir>/<name>-<version>.tar.* for each component plus SOURCES.txt
# (component, version, origin, SHA-256).  Debian packages installed with apt
# are not collected here; the release lists them in a package manifest.
set -euo pipefail

OUT="${1:?usage: $0 <out-dir>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DF="$ROOT/Dockerfile"
SIDWASM_BUILD="$ROOT/soniqboom/native/sidwasm/build.sh"

arg() {   # value of `ARG NAME=value` in the Dockerfile
    local v
    v="$(sed -n "s/^ARG $1=//p" "$DF" | head -1)"
    [ -n "$v" ] || { echo "Dockerfile has no ARG $1" >&2; exit 1; }
    printf '%s' "$v"
}

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
: > "$OUT/SOURCES.txt"

record() {   # record <file> <component> <version> <origin>
    local sum
    sum="$(sha256sum "$OUT/$1" 2>/dev/null || shasum -a 256 "$OUT/$1")"
    printf '%s\t%s\t%s\t%s\t%s\n' "$2" "$3" "$4" "$1" "${sum%% *}" >> "$OUT/SOURCES.txt"
}

fetch() {    # fetch <url> <file> <component> <version> [<expected sha256>]
    curl -sfL --retry 3 --max-time 600 "$1" -o "$OUT/$2"
    record "$2" "$3" "$4" "$1"
    if [ -n "${5:-}" ]; then
        local got; got="$(tail -1 "$OUT/SOURCES.txt" | cut -f5)"
        [ "$got" = "$5" ] || { echo "$3: SHA-256 mismatch: got $got, pinned $5" >&2; exit 1; }
    fi
}

git_snapshot() {   # git_snapshot <repo> <ref> <component> [<paths left out>] — recursive clone incl. submodules
    local dir="$WORK/$3-$2"
    git clone -q "$1" "$dir"
    git -C "$dir" checkout -q "$2"
    git -C "$dir" submodule update -q --init --recursive
    local commit; commit="$(git -C "$dir" rev-parse HEAD)"
    rm -rf "$dir/.git"
    find "$dir" -name .git -prune -exec rm -rf {} +
    local p
    for p in ${4:-}; do rm -rf "${dir:?}/$p"; done
    tar -C "$WORK" -czf "$OUT/$3-$2.tar.gz" "$3-$2"
    local version="$2"; [ "$commit" = "$2" ] || version="$2 ($commit)"
    record "$3-$2.tar.gz" "$3" "$version" "$1"
}

# psgplay — pinned commit, with its submodules
git_snapshot https://github.com/frno7/psgplay.git "$(arg PSGPLAY_COMMIT)" psgplay

# libkss (kssrender) — pinned commit with the submodules the image builds:
# the chip cores and kmz80, never kss-drivers (not under libkss's licence;
# of its drivers only MGSDRV is built — its header, below)
LIBKSS_COMMIT="$(arg LIBKSS_COMMIT)"
LIBKSS_DIR="$WORK/libkss-$LIBKSS_COMMIT"
git clone -q https://github.com/digital-sound-antiques/libkss.git "$LIBKSS_DIR"
git -C "$LIBKSS_DIR" checkout -q "$LIBKSS_COMMIT"
git -C "$LIBKSS_DIR" submodule update -q --init modules/emu2149 modules/emu2212 \
    modules/emu2413 modules/emu8950 modules/emu76489 modules/kmz80
find "$LIBKSS_DIR" -name .git -prune -exec rm -rf {} +
tar -C "$WORK" -czf "$OUT/libkss-$LIBKSS_COMMIT.tar.gz" "libkss-$LIBKSS_COMMIT"
record "libkss-$LIBKSS_COMMIT.tar.gz" libkss "$LIBKSS_COMMIT" \
    https://github.com/digital-sound-antiques/libkss.git

# MGSDRV (kssrender) — the one driver header of kss-drivers the image builds,
# as published (MGSDRV may be redistributed unmodified — Ain./Gigamix)
KSS_DRIVERS_COMMIT="$(arg KSS_DRIVERS_COMMIT)"
fetch "https://raw.githubusercontent.com/digital-sound-antiques/kss-drivers/${KSS_DRIVERS_COMMIT}/mgsdrv.h" \
      "mgsdrv-${KSS_DRIVERS_COMMIT}.h" mgsdrv "$KSS_DRIVERS_COMMIT" "$(arg MGSDRV_H_SHA256)"

# GoatTracker 2 (gt2reloc) — the release archive soniqboom/native/goattracker/
# build.sh builds unmodified
GOATTRACKER_VERSION="$(arg GOATTRACKER_VERSION)"
fetch "https://sourceforge.net/projects/goattracker2/files/GoatTracker%202/${GOATTRACKER_VERSION}/GoatTracker_${GOATTRACKER_VERSION}.zip/download" \
      "GoatTracker_${GOATTRACKER_VERSION}.zip" goattracker "$GOATTRACKER_VERSION" \
      "$(arg GOATTRACKER_SHA256)"

# SunVox Library (sunvoxrender) — its release archive (the library's source)
SUNVOX_LIB_VERSION="$(arg SUNVOX_LIB_VERSION)"
fetch "https://warmplace.ru/soft/sunvox/sunvox_lib-${SUNVOX_LIB_VERSION}.zip" \
      "sunvox_lib-${SUNVOX_LIB_VERSION}.zip" sunvox_lib "$SUNVOX_LIB_VERSION" \
      "$(arg SUNVOX_LIB_SHA256)"

# libxmp (xmprender) — its release archive, built unmodified
LIBXMP_VERSION="$(arg LIBXMP_VERSION)"
fetch "https://github.com/libxmp/libxmp/releases/download/libxmp-${LIBXMP_VERSION}/libxmp-${LIBXMP_VERSION}.tar.gz" \
      "libxmp-${LIBXMP_VERSION}.tar.gz" libxmp "$LIBXMP_VERSION" "$(arg LIBXMP_SHA256)"

# Furnace (furnace-headless) — the pinned commit with the two submodules the
# headless build compiles (extern/fmt, extern/adpcm); its demo songs (not
# under the GPL), instruments and wavetables (not built, no licence stated)
# are left out
FURNACE_COMMIT="$(arg FURNACE_COMMIT)"
FURNACE_DIR="$WORK/furnace-$FURNACE_COMMIT"
git init -q "$FURNACE_DIR"
git -C "$FURNACE_DIR" remote add origin https://github.com/tildearrow/furnace.git
git -C "$FURNACE_DIR" fetch -q --depth 1 origin "$FURNACE_COMMIT"
git -C "$FURNACE_DIR" checkout -q FETCH_HEAD
git -C "$FURNACE_DIR" submodule update -q --init --depth 1 extern/fmt extern/adpcm
find "$FURNACE_DIR" -name .git -prune -exec rm -rf {} +
rm -rf "${FURNACE_DIR:?}/demos" "${FURNACE_DIR:?}/instruments" "${FURNACE_DIR:?}/wavetables"
tar -C "$WORK" -czf "$OUT/furnace-$FURNACE_COMMIT.tar.gz" "furnace-$FURNACE_COMMIT"
record "furnace-$FURNACE_COMMIT.tar.gz" furnace "$FURNACE_COMMIT" \
    https://github.com/tildearrow/furnace.git

# Highly Theoretical and psflib (dsfrender) — pinned commits.  Of Highly
# Theoretical only its Dreamcast core is built (soniqboom/native/dsfrender/
# build.sh); never its 68000 cores Musashi (Core/m68k) and Starscream
# (Core/Starscream), under their own licences, and C68K (Core/c68k,
# GPL-2.0-or-later) — none of them built — which are left out.
git_snapshot https://github.com/kode54/Highly_Theoretical.git "$(arg HT_COMMIT)" Highly_Theoretical \
    "Core/m68k Core/Starscream Core/c68k"
git_snapshot https://github.com/kode54/psflib.git "$(arg PSFLIB_COMMIT)" psflib
# MilkyTracker (milkyrender builds MilkyPlay, its src/milkyplay) — pinned commit
MILKYTRACKER_COMMIT="$(arg MILKYTRACKER_COMMIT)"
fetch "https://github.com/milkytracker/MilkyTracker/archive/${MILKYTRACKER_COMMIT}.tar.gz" \
      "milkytracker-${MILKYTRACKER_COMMIT}.tar.gz" milkytracker "$MILKYTRACKER_COMMIT"

# musicline_playback (mlrender) — the files soniqboom/native/mlrender/build.sh
# builds, from DEViLBOX at the pinned commit: DEViLBOX's licence and
# third-party notices and the port's replayer sources (never the rest of
# DEViLBOX, nor the port's uade copy); build.sh resizes MAX_INTERNAL_TICK in
# module.h and applies soniqboom/native/mlrender/musicline_playback.patch
# (with ml_bounds.h, both in the SoniqBoom source) when it builds them
DEVILBOX_COMMIT="$(arg DEVILBOX_COMMIT)"
ML_DIR="$WORK/musicline_playback-$DEVILBOX_COMMIT"
ML_PORT=third-party/musicline_playback-main/musicline
git init -q "$ML_DIR"
git -C "$ML_DIR" remote add origin https://github.com/spotUP/DEViLBOX.git
git -C "$ML_DIR" config core.sparseCheckout true
printf '%s\n' /LICENSE /THIRD_PARTY_NOTICES.md "/$ML_PORT/*.cpp" "/$ML_PORT/*.h" \
    "/$ML_PORT/player/mline_backend.cpp" "/$ML_PORT/player/mline_backend.h" \
    > "$ML_DIR/.git/info/sparse-checkout"
git -C "$ML_DIR" fetch -q --depth 1 --filter=blob:none origin "$DEVILBOX_COMMIT"
git -C "$ML_DIR" checkout -q FETCH_HEAD
rm -rf "$ML_DIR/.git"
tar -C "$WORK" -czf "$OUT/musicline_playback-$DEVILBOX_COMMIT.tar.gz" \
    "musicline_playback-$DEVILBOX_COMMIT"
record "musicline_playback-$DEVILBOX_COMMIT.tar.gz" musicline_playback "$DEVILBOX_COMMIT" \
    https://github.com/spotUP/DEViLBOX.git

# portable_mdx (mdxrender) — the commit soniqboom/native/mdxrender/build.sh
# builds unmodified
PORTABLE_MDX_COMMIT="$(arg PORTABLE_MDX_COMMIT)"
fetch "https://github.com/yosshin4004/portable_mdx/archive/${PORTABLE_MDX_COMMIT}.tar.gz" \
      "portable_mdx-${PORTABLE_MDX_COMMIT}.tar.gz" portable_mdx "$PORTABLE_MDX_COMMIT"

# zxtune — the image installs the official zxtune123 build of this revision
ZXREV="$(arg ZXTUNE_REV)"
fetch "https://github.com/vitamin-caig/zxtune/archive/refs/tags/${ZXREV}.tar.gz" \
      "zxtune-${ZXREV}.tar.gz" zxtune "$ZXREV"

# libgme
LIBGME_URL="$(arg LIBGME_URL)"
fetch "$LIBGME_URL" "$(basename "$LIBGME_URL")" libgme "$(basename "$LIBGME_URL" -src.tar.gz)" "$(arg LIBGME_SHA256)"

# sc68
SC68_URL="$(arg SC68_URL)"
fetch "$SC68_URL" "$(basename "$SC68_URL")" sc68 "$(basename "$SC68_URL" .tar.gz)" "$(arg SC68_SHA256)"

# uade and its libraries bencodetools and libzakalwe
UADE_URL="$(arg UADE_URL)"
fetch "$UADE_URL" "$(basename "$UADE_URL")" uade "$(basename "$UADE_URL" .tar.bz2)" "$(arg UADE_SHA256)"
git_snapshot https://gitlab.com/heikkiorsila/bencodetools.git "$(arg BENCODETOOLS_REF)" bencodetools
git_snapshot https://gitlab.com/hors/libzakalwe.git "$(arg LIBZAKALWE_REF)" libzakalwe

# The C64 SID chain the image builds: libresidfp → libsidplayfp → sidplayfp
for c in libresidfp:RESIDFP libsidplayfp:LIBSIDPLAYFP sidplayfp:SIDPLAYFP; do
    name="${c%%:*}"; key="${c#*:}"
    ver="$(arg "${key}_VERSION")"
    fetch "https://github.com/libsidplayfp/${name}/releases/download/v${ver}/${name}-${ver}.tar.gz" \
          "${name}-${ver}.tar.gz" "$name" "$ver" "$(arg "${key}_SHA256")"
done

# libopenmpt / openmpt123 — the autotools release tarball the image builds
OMPTVER="$(arg LIBOPENMPT_VERSION)"
fetch "https://lib.openmpt.org/files/libopenmpt/src/libopenmpt-${OMPTVER}+release.autotools.tar.gz" \
      "libopenmpt-${OMPTVER}+release.autotools.tar.gz" libopenmpt "$OMPTVER" "$(arg LIBOPENMPT_SHA256)"

# libsidplayfp — the browser SID engine; its wrapper, patches and build
# script are in soniqboom/native/sidwasm/ (URL and SHA-256 pinned there)
pin() { sed -n "s/^$1=\"\(.*\)\"$/\1/p" "$SIDWASM_BUILD" | head -1; }
SIDURL="$(pin LIBSIDPLAYFP_URL)"; SIDSHA="$(pin LIBSIDPLAYFP_SHA256)"
[ -n "$SIDURL" ] && [ -n "$SIDSHA" ] \
    || { echo "no LIBSIDPLAYFP_URL / LIBSIDPLAYFP_SHA256 in $SIDWASM_BUILD" >&2; exit 1; }
fetch "$SIDURL" "$(basename "$SIDURL")" libsidplayfp "$(basename "$SIDURL" .tar.gz)" "$SIDSHA"

echo "Collected into $OUT:"
column -t -s "$(printf '\t')" "$OUT/SOURCES.txt" 2>/dev/null || cat "$OUT/SOURCES.txt"
