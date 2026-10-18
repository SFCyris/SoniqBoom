# sidwasm — WebAssembly SID engine

Source for `soniqboom/frontend/js/vendor/sidwasm.js` and
`soniqboom/frontend/js/vendor/sidwasm.wasm`, the in-browser C64 SID renderer
used by `frontend/js/vu-sid-worker.js` (per-voice VU and audio rendering).

The engine is libsidplayfp 2.16.1 with the reSIDfp emulation, two small
patches, and a C wrapper (`sidwasm.cpp`), compiled to WebAssembly with
Emscripten.

## Licence

GPL-2.0-or-later. The full licence text is in [`COPYING`](COPYING) (copied
from the libsidplayfp release).

libsidplayfp copyright holders, as listed in the release's `AUTHORS`:

- Copyright (c) 2000 Simon White
- Copyright (c) 2007-2010 Antti Lankila
- Copyright (c) 2010-2026 Leandro Nini

reSIDfp (`src/builders/residfp-builder/residfp`):

- Copyright 2011-2025 Leandro Nini
- Copyright 2007-2010 Antti Lankila
- Copyright 2004 Dag Lem

Contributed or derived work credited in `AUTHORS`: Dag Lem (reSID),
Michael Schwendt (SidTune, MD5), Simon White, Antti Lankila, Leandro Nini,
LaLa (stilview), André Fachat (reloc65), Jarno Paananen, Thibaut Varene,
LouD; VIC-II, CIA and CPU-port emulation based on VICE; CIA SDR and interrupt
handling based on the Denise emulator.

`sidwasm.cpp` and the patches in `patches/` are distributed under the same
licence (GPL-2.0-or-later). The patches were written for SoniqBoom on
2026-10-07; each patch file starts with a note of the change, and each file
they modify carries a dated modification notice.

`sidwasm.js` and `sidwasm.wasm` also contain the Emscripten runtime (MIT /
University of Illinois-NCSA) with musl libc (MIT) and LLVM libc++, libc++abi
and compiler-rt (Apache-2.0 WITH LLVM-exception), compiled in by Emscripten.

## Contents

| File | Description |
|---|---|
| `sidwasm.cpp` | C API exported to JavaScript (`sid_load`, `sid_render`, `sid_render_vu`, `sid_render_audio_vu`, `sid_mute`, `sid_set_model`, `sid_set_sampling`, `sid_set_power_delay`, `sid_reg`, `sid_is_playing`, `sid_cur_song`, `sid_songs`, `sid_time_ms`, `sid_error`). The header comment documents each function. |
| `patches/0001-residfp-per-voice-vu-tap.patch` | Adds the globals `sb_vu_tap` (off by default) and `sb_vu_peak[3]` to reSIDfp. While the tap is on, `SID::clock()` records the maximum of `\|voice[i].output()\|` (before the filter) for each of the three voices, over all SID chips of the tune. |
| `patches/0002-residfp-sequential-filter-tables.patch` | Builds the 6581/8580 filter lookup tables one after another instead of in parallel `std::thread`s. |
| `banner.js` | Copyright, licence and source notice placed at the top of `sidwasm.js`. |
| `build.sh` | Reproducible build script (below). |
| `COPYING` | GNU General Public License, version 2. |

## Upstream source

- URL: <https://github.com/libsidplayfp/libsidplayfp/releases/download/v2.16.1/libsidplayfp-2.16.1.tar.gz>
- SHA-256: `ace0f73c2ef8645ab069ce1b298b10e31e36af7b5996109983b2b67ad60ff3ca`

The release tarball contains the pre-generated `src/psiddrv.bin`,
`src/sidtune/sidplayer1.bin`, `src/sidtune/sidplayer2.bin` and the reSID
`wave*.h` tables; the build uses them as shipped.

## Toolchain

Docker image `emscripten/emsdk:6.0.11`, pinned by digest:

```
emscripten/emsdk:6.0.11@sha256:cdefec943f04fd4b2b2fe23b0a1a346be9fc560ef5784a83faa27dd351381372
```

Compiler and linker flags:

- library and wrapper: `-O2 -msimd128`
- library configure: `--disable-shared --enable-static --disable-hardsid
  --disable-tests --with-simd=none --without-gcrypt --without-exsid
  --without-usbsid`, built with empty `PTHREAD_CFLAGS` / `PTHREAD_LIBS`
- link: `-sMODULARIZE=1 -sEXPORT_NAME=createSidModule
  -sENVIRONMENT=web,worker,node -sALLOW_MEMORY_GROWTH=1 --extern-pre-js banner.js`, the exported
  functions listed above plus `_malloc` and `_free`, and the runtime methods
  `ccall`, `cwrap`, `getValue`, `setValue`, `UTF8ToString`, `HEAPU8`,
  `HEAP16`, `HEAPF32`

The output uses WebAssembly SIMD (fixed-width, 128-bit). The shipped files
were built on linux/arm64 and have these SHA-256 hashes:

```
4de12ef789362deae564bf79a4b471645f23982ece90809cd65a676dac1d5b9c  sidwasm.js
ab792b1e059cee6fa2327f6727c53b6a1212b0f4519365d4aaa32d74fcfd794a  sidwasm.wasm
```

## Rebuilding

Requires Docker. From the repository root:

```
bash soniqboom/native/sidwasm/build.sh
```

This downloads the libsidplayfp tarball inside the container, checks its
SHA-256, applies `patches/*.patch`, builds the library and the wrapper, and
writes `sidwasm.js` and `sidwasm.wasm` to `soniqboom/frontend/js/vendor/`.
An output directory can be given as the first argument. Two environment
variables are read: `SIDWASM_ENVIRONMENT` (the `-sENVIRONMENT` value, default
`web,worker,node`) and `SIDWASM_JOBS` (parallel make jobs in the container,
default 4). The script prints the Emscripten version, the build platform, the
libsidplayfp version and SHA-256, the environment value and the SHA-256 of both
outputs.

After replacing the vendor files, bump `WASM_V` in
`soniqboom/frontend/js/vu-sid-worker.js` and `SHELL_VERSION` in
`soniqboom/frontend/sw.js`.
