#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Download the Debian source packages of the Docker image, at the exact
# versions listed in its package manifests.  Runs inside a Debian container of
# the image's release (trixie):
#
#   docker run --rm -v "$PWD:/w" -w /w debian:trixie \
#       bash scripts/collect_debian_sources.sh <out-dir> <manifest>...
#
# A manifest is the output of
#   dpkg-query -W -f '${Package}\t${Version}\t${source:Package}\t${source:Version}\n'
# Versions no longer in the Debian archive are fetched from snapshot.debian.org.
# Writes the source files to <out-dir> and DEBIAN-SOURCES.txt (package, version).
set -euo pipefail

OUT="${1:?usage: $0 <out-dir> <manifest>...}"; shift
[ "$#" -gt 0 ] || { echo "no manifest given" >&2; exit 1; }
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

export DEBIAN_FRONTEND=noninteractive
sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources
apt-get update -qq
apt-get install -y -qq --no-install-recommends dpkg-dev curl ca-certificates jq >/dev/null

cut -f3,4 "$@" | sort -u > "$OUT/DEBIAN-SOURCES.txt"

snapshot() {   # snapshot <source> <version> — fetch the .dsc and its files
    local src="$1" ver="$2" files
    files="$(curl -sf "https://snapshot.debian.org/mr/package/${src}/${ver}/srcfiles?fileinfo=1")" || return 1
    # Each file hash maps to its name(s); fetch every listed file by hash.
    jq -r '.fileinfo | to_entries[] | "\(.key)\t\(.value[0].name)"' <<<"$files" \
    | while IFS=$'\t' read -r hash name; do
        curl -sfL --retry 3 "https://snapshot.debian.org/file/${hash}" -o "$name" || return 1
    done
}

cd "$OUT"
missing=""
while IFS=$'\t' read -r src ver; do
    [ -n "$src" ] || continue
    apt-get source -qq --download-only "${src}=${ver}" >/dev/null 2>&1 \
        || snapshot "$src" "$ver" \
        || missing="$missing ${src}=${ver}"
done < DEBIAN-SOURCES.txt

if [ -n "$missing" ]; then
    echo "source packages not found:$missing" >&2
    exit 1
fi
echo "$(wc -l < DEBIAN-SOURCES.txt) source packages, $(du -sh . | cut -f1)"
