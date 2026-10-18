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

git_snapshot() {   # git_snapshot <repo> <ref> <component> — recursive clone incl. submodules
    local dir="$WORK/$3-$2"
    git clone -q "$1" "$dir"
    git -C "$dir" checkout -q "$2"
    git -C "$dir" submodule update -q --init --recursive
    local commit; commit="$(git -C "$dir" rev-parse HEAD)"
    rm -rf "$dir/.git"
    find "$dir" -name .git -prune -exec rm -rf {} +
    tar -C "$WORK" -czf "$OUT/$3-$2.tar.gz" "$3-$2"
    local version="$2"; [ "$commit" = "$2" ] || version="$2 ($commit)"
    record "$3-$2.tar.gz" "$3" "$version" "$1"
}

# psgplay — pinned commit, with its submodules
git_snapshot https://github.com/frno7/psgplay.git "$(arg PSGPLAY_COMMIT)" psgplay

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
