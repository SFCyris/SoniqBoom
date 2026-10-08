#!/usr/bin/env bash
# Build the SoniqBoom WebAssembly SID engine (sidwasm.js + sidwasm.wasm)
# from libsidplayfp 2.16.1 + reSIDfp, the patches in ./patches and
# ./sidwasm.cpp, inside a pinned Emscripten Docker image.
#
# Usage:
#   bash soniqboom/native/sidwasm/build.sh [OUT_DIR]
#
# OUT_DIR defaults to soniqboom/frontend/js/vendor.
#
# Environment:
#   SIDWASM_ENVIRONMENT  Emscripten -sENVIRONMENT value (default: web,worker,node;
#                        node is used by tests/js/subsong_map.test.mjs)
#   SIDWASM_JOBS         parallel make jobs inside the container (default: 4)
#
# Requires: docker. The sources are passed to the container as a tar stream
# on stdin and the results come back as a tar stream on stdout, so no bind
# mounts are used.

set -euo pipefail

EMSDK_IMAGE="emscripten/emsdk:6.0.11@sha256:cdefec943f04fd4b2b2fe23b0a1a346be9fc560ef5784a83faa27dd351381372"

LIBSIDPLAYFP_VERSION="2.16.1"
LIBSIDPLAYFP_URL="https://github.com/libsidplayfp/libsidplayfp/releases/download/v2.16.1/libsidplayfp-2.16.1.tar.gz"
LIBSIDPLAYFP_SHA256="ace0f73c2ef8645ab069ce1b298b10e31e36af7b5996109983b2b67ad60ff3ca"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"
OUT_DIR="${1:-${REPO_ROOT}/soniqboom/frontend/js/vendor}"
ENVIRONMENT="${SIDWASM_ENVIRONMENT:-web,worker,node}"
JOBS="${SIDWASM_JOBS:-4}"

command -v docker >/dev/null || { echo "build.sh: docker not found" >&2; exit 1; }
mkdir -p "${OUT_DIR}"

# Script executed inside the container. Inputs arrive in /work/in.
read -r -d '' INNER <<'INNER_EOF' || true
set -euo pipefail
exec 3>&1 1>&2                      # keep stdout for the result tar only
mkdir -p /work && cd /work
mkdir -p in && tar -xf - -C in

curl -fsSL -o libsidplayfp.tar.gz "$LIBSIDPLAYFP_URL"
echo "$LIBSIDPLAYFP_SHA256  libsidplayfp.tar.gz" | sha256sum -c -
tar -xzf libsidplayfp.tar.gz
cd "libsidplayfp-$LIBSIDPLAYFP_VERSION"

for p in /work/in/patches/*.patch; do
    echo "applying $(basename "$p")"
    patch -p1 --no-backup-if-mismatch < "$p"
done

# psiddrv.bin, sidtune/sidplayer{1,2}.bin and the reSID wave*.h tables ship
# pre-generated in the release tarball (newer than their sources), so make
# does not need xa65 or perl to regenerate them.

FLAGS="-O2 -msimd128"
emconfigure ./configure \
    --host=wasm32-unknown-emscripten \
    --prefix=/work/prefix \
    --disable-shared --enable-static \
    --disable-hardsid --disable-tests \
    --with-simd=none \
    --without-gcrypt --without-exsid --without-usbsid \
    CFLAGS="$FLAGS" CXXFLAGS="$FLAGS"

# No threads: the patched reSIDfp no longer uses std::thread, so drop the
# -pthread flags that configure's AX_PTHREAD check adds.
emmake make -j"$JOBS" PTHREAD_CFLAGS= PTHREAD_LIBS= PTHREAD_CC=emcc
emmake make install PTHREAD_CFLAGS= PTHREAD_LIBS= PTHREAD_CC=emcc

mkdir -p /work && cd /work
mkdir -p out
em++ $FLAGS \
    -I/work/prefix/include \
    /work/in/sidwasm.cpp \
    --extern-pre-js /work/in/banner.js \
    /work/prefix/lib/libsidplayfp.a \
    -o out/sidwasm.js \
    -sMODULARIZE=1 \
    -sEXPORT_NAME=createSidModule \
    -sENVIRONMENT="$SIDWASM_ENVIRONMENT" \
    -sALLOW_MEMORY_GROWTH=1 \
    -sEXPORTED_FUNCTIONS=_sid_load,_sid_render,_sid_render_vu,_sid_render_audio_vu,_sid_mute,_sid_set_model,_sid_set_sampling,_sid_set_power_delay,_sid_reg,_sid_is_playing,_sid_cur_song,_sid_songs,_sid_time_ms,_sid_error,_malloc,_free \
    -sEXPORTED_RUNTIME_METHODS=ccall,cwrap,getValue,setValue,UTF8ToString,HEAPU8,HEAP16,HEAPF32

emcc --version | sed -n 1p > out/BUILDINFO
echo "platform $(uname -m)" >> out/BUILDINFO
echo "libsidplayfp $LIBSIDPLAYFP_VERSION sha256 $LIBSIDPLAYFP_SHA256" >> out/BUILDINFO
echo "environment $SIDWASM_ENVIRONMENT" >> out/BUILDINFO
(cd out && sha256sum sidwasm.js sidwasm.wasm >> BUILDINFO)

tar -cf - -C out sidwasm.js sidwasm.wasm BUILDINFO >&3
INNER_EOF

echo "build.sh: building in ${EMSDK_IMAGE}" >&2
TMP_TAR="$(mktemp "${TMPDIR:-/tmp}/sidwasm-out.XXXXXX")"
trap 'rm -f "${TMP_TAR}"' EXIT

COPYFILE_DISABLE=1 tar -cf - -C "${HERE}" sidwasm.cpp banner.js patches \
| docker run --rm -i \
    -e LIBSIDPLAYFP_VERSION="${LIBSIDPLAYFP_VERSION}" \
    -e LIBSIDPLAYFP_URL="${LIBSIDPLAYFP_URL}" \
    -e LIBSIDPLAYFP_SHA256="${LIBSIDPLAYFP_SHA256}" \
    -e SIDWASM_ENVIRONMENT="${ENVIRONMENT}" \
    -e JOBS="${JOBS}" \
    "${EMSDK_IMAGE}" \
    bash -c "${INNER}" > "${TMP_TAR}"

tar -xf "${TMP_TAR}" -C "${OUT_DIR}" sidwasm.js sidwasm.wasm
tar -xOf "${TMP_TAR}" BUILDINFO >&2
echo "build.sh: wrote ${OUT_DIR}/sidwasm.js and ${OUT_DIR}/sidwasm.wasm" >&2
