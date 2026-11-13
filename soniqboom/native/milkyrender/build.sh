#!/bin/sh
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build milkyrender (DSIK v1 "DSM\x10" modules → WAV) against a pinned
# MilkyPlay, MilkyTracker's playback library.
#
#   [LICENSE_DIR=dir] build.sh DEST_DIR [MILKYTRACKER_SOURCE_DIR]
#
# Installs DEST_DIR/milkyrender (and, with LICENSE_DIR, the licence file).
# Without MILKYTRACKER_SOURCE_DIR, MilkyTracker is fetched at
# MILKYTRACKER_COMMIT (no submodules).  Only the MilkyPlay files listed in
# MILKYPLAY_FILES are copied out of src/milkyplay and compiled (all
# BSD-3-Clause), with three of SoniqBoom's in place of MilkyPlay's
# (README.md, "Licences"): milkyrender.cpp (it also stands in for
# AudioDriverManager.cpp), ResamplerFactory.cpp and Limiter.h.  In the copy
# of XModule.cpp only the DSIK v1 loader is registered and IT-compressed
# samples are not decoded.  Needs a C++20 compiler, git and awk (no cmake, no
# SDL).  Licences: soniqboom/native/milkyrender/README.md.
set -eu

MILKYTRACKER_COMMIT="${MILKYTRACKER_COMMIT:-e3ceb05105e8c5bdc907d6451245bf5a1926b1f9}"
dest="${1:?usage: build.sh DEST_DIR [MILKYTRACKER_SOURCE_DIR]}"
here="$(cd "$(dirname "$0")" && pwd)"
cxx="${CXX:-c++}"

MILKYPLAY_FILES="
AudioDriverBase.cpp AudioDriver_NULL.cpp AudioDriver_WAVWriter.cpp
ChannelMixer.cpp LittleEndian.cpp LoaderDSM.cpp MasterMixer.cpp PlayerBase.cpp
PlayerFAR.cpp PlayerGeneric.cpp PlayerIT.cpp PlayerSTD.cpp XMFile.cpp XModule.cpp
AudioDriverBase.h AudioDriverManager.h AudioDriver_NULL.h AudioDriver_WAVWriter.h
ChannelMixer.h LittleEndian.h Loaders.h MasterMixer.h MilkyPlay.h
MilkyPlayCommon.h MilkyPlayResults.h MilkyPlayTypes.h Mixable.h PlayerBase.h
PlayerFAR.h PlayerGeneric.h PlayerIT.h PlayerSTD.h ResamplerFactory.h
ResamplerFast.h ResamplerMacros.h XMFile.h XModule.h
"

work="$(mktemp -d "${TMPDIR:-/tmp}/milkyrender.XXXXXX")"
trap 'rm -rf "$work"' EXIT

if [ "${2:-}" ]; then
  src="$2"
else
  src="$work/milkytracker"
  git init --quiet "$src"
  git -C "$src" remote add origin https://github.com/milkytracker/MilkyTracker.git
  if ! git -C "$src" fetch --quiet --depth 1 origin "$MILKYTRACKER_COMMIT"; then
    git -C "$src" fetch --quiet origin
  fi
  git -C "$src" checkout --quiet "$MILKYTRACKER_COMMIT"
fi

mp="$src/src/milkyplay"
b="$work/milkyplay"
mkdir -p "$b"
for f in $MILKYPLAY_FILES; do
  cp "$mp/$f" "$b/$f"
done
cp "$here/ResamplerFactory.cpp" "$here/Limiter.h" "$b/"

# XModule.cpp: register the DSIK v1 loader alone, and leave out the
# IT-compressed sample decoder (an IT-packed sample loads as a failure).
awk '
/^class ITSampleLoader : public XModule::SampleLoader/ { skip = 1 }
/^\/\/ MDL sample loading helper class/ { skip = 0 }
/ITSampleLoader sampleLoader\(f, \(flags & ST_PACKING_IT215\)\);/ {
  print "\t\treturn false;"; blk = 1; next }
blk { if ($0 ~ /^[ \t]*return true;/) blk = 0; next }
/registerLoader\(new Loader/ && !/registerLoader\(new LoaderDSMv1\(\)/ { next }
!skip' "$mp/XModule.cpp" > "$b/XModule.cpp"
if grep -q "ITSampleLoader" "$b/XModule.cpp" \
    || [ "$(grep -c "registerLoader(new " "$b/XModule.cpp")" != 1 ] \
    || ! grep -q "registerLoader(new LoaderDSMv1()" "$b/XModule.cpp"; then
  echo "build.sh: XModule.cpp at $MILKYTRACKER_COMMIT is not laid out as expected" >&2
  exit 1
fi

short="$(echo "$MILKYTRACKER_COMMIT" | cut -c1-12)"
"$cxx" -O2 -std=c++20 -w -DMILKYPLAY_COMMIT="\"$short\"" -I"$b" \
  "$here/milkyrender.cpp" "$b"/*.cpp -o "$work/milkyrender"

mkdir -p "$dest"
install -m 0755 "$work/milkyrender" "$dest/milkyrender"
# LICENSE_DIR: MilkyPlay's licence (the BSD-3-Clause notice every MilkyPlay
# source file carries; MilkyTracker's COPYING names MilkyPlay New-BSD).
if [ "${LICENSE_DIR:-}" ]; then
  mkdir -p "$LICENSE_DIR"
  {
    echo "MilkyPlay (src/milkyplay of https://github.com/milkytracker/MilkyTracker, commit $MILKYTRACKER_COMMIT)"
    echo
    awk 'NR == 1, /\*\// { print }' "$mp/XModule.h"
  } > "$LICENSE_DIR/milkyplay-LICENSE"
fi
"$dest/milkyrender" --version
