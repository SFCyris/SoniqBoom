# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# SoniqBoom — self-hosted music server.  https://github.com/SFCyris/SoniqBoom
# One image, every format: chiptune · tracker · SID · MIDI · lossless · DSD.
#
# Multi-stage: a `builder` stage carries the full toolchain and compiles/fetches
# the renderers that aren't in Debian's apt archive (psgplay, sc68, uade123;
# zxtune123 is a prebuilt fetch) AND pre-builds the vendored StSound (.ym) /
# HivelyTracker (.hvl) engines.  The final runtime stage copies only the results,
# so it ships NO compiler — smaller, immutable, and quick to start.  Multi-arch:
#   docker buildx build --platform linux/amd64,linux/arm64 -t <ref> --push .

# ═══════════════════════════════ Stage 1: builder ═══════════════════════════════
FROM python:3.12-slim-bookworm AS builder
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git ca-certificates curl \
        autoconf automake libtool pkg-config zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
# /out/bin  → binaries copied onto the final image's PATH
# /out/sc68 → sc68 `make install` DESTDIR (staged /usr/local tree). Pre-create
#             both so a best-effort renderer failure still leaves a copyable dir.
# Pre-create every /out subtree the runtime stage COPYs from — including
# sc68's share dir — so a SLIM build (which skips sc68/uade) still has empty
# dirs to copy and the runtime COPYs don't fail on a missing source path.
RUN mkdir -p /out/bin /out/lib /out/share \
    /out/sc68/usr/local/bin /out/sc68/usr/local/lib /out/sc68/usr/local/share

# SLIM=1 skips the two heavy source-compiled renderers (uade123's Amiga
# "exotica" family and sc68's native .sc68 disks) for a smaller, faster-building
# image aimed at low-RAM boxes that only want the mainstream + common retro
# formats.  Everything else — SID, MIDI, trackers, AHX, YM/SNDH, console
# chiptune, PSF, AdLib, all modern codecs — still ships.
ARG SLIM=0

# YM (.ym) + HVL (.hvl): pre-build the vendored engines the app would otherwise
# compile on first play — same compile commands as api/stream.py's _build_*.
# Only the native/ sources are needed, so this layer caches independently of app code.
COPY soniqboom/native /src/native
RUN set -eux; \
    ( cc  -O2 -w /src/native/hvl/hvl2wav.c /src/native/hvl/replay.c -o /out/bin/hvl2wav -lm ) \
      || echo "NOTE: hvl2wav pre-build failed — .hvl falls back to a runtime build (needs a compiler)." ; \
    ( c++ -O2 -w -o /out/bin/ym2wav \
          /src/native/stsound/Ym2Wav/Ym2Wav.cpp \
          /src/native/stsound/StSoundLibrary/*.cpp \
          /src/native/stsound/StSoundLibrary/LZH/*.cpp \
          -I /src/native/stsound/StSoundLibrary -I /src/native/stsound/StSoundLibrary/LZH ) \
      || echo "NOTE: ym2wav pre-build failed — .ym falls back to a runtime build (needs a compiler)."

# psgplay — Atari ST SNDH (.sndh).  Plain C, pinned commit; the post-checkout
# submodule sync is a no-op if the pin has no submodules.
ARG PSGPLAY_COMMIT=869992cbbb8488b519149d8c0dd7afafb78aae5e
RUN set -eux; \
    ( git clone https://github.com/frno7/psgplay.git /tmp/psgplay \
      && git -C /tmp/psgplay checkout "$PSGPLAY_COMMIT" \
      && git -C /tmp/psgplay submodule update --init --recursive \
      && make -C /tmp/psgplay -j"$(nproc)" psgplay \
      && install -m0755 /tmp/psgplay/psgplay /out/bin/psgplay ) \
      || echo "NOTE: psgplay build failed — Atari ST SNDH (.sndh) disabled." ; \
    rm -rf /tmp/psgplay

# zxtune123 — PSF console-music family (PSF/PSF2/USF/GSF/2SF/SSF/DSF/NCSF).
# Official prebuilt; fetch the arch that matches the build — BOTH x86_64 and
# arm64 are published on storage.zxtune.ru (the arm64 build needs glibc ≤ 2.35,
# and bookworm ships 2.36, so it runs).
RUN set -eux; \
    case "$(uname -m)" in \
      x86_64)        ZXARCH=x86_64 ;; \
      aarch64|arm64) ZXARCH=arm64  ;; \
      *)             ZXARCH="" ;; \
    esac; \
    if [ -n "$ZXARCH" ]; then \
      ( mkdir -p /tmp/zx \
        && curl -sfL --max-time 300 \
             "https://storage.zxtune.ru/builds/public/r5100/linux/${ZXARCH}/zxtune_r5100_linux_${ZXARCH}.tar.gz" \
             -o /tmp/zx.tar.gz \
        && tar xzf /tmp/zx.tar.gz -C /tmp/zx \
        && install -m0755 "$(find /tmp/zx -name zxtune123 -type f | head -1)" /out/bin/zxtune123 ) \
        || echo "NOTE: zxtune123 fetch failed — PSF family (.psf/.usf/.gsf/…) disabled." ; \
      rm -rf /tmp/zx /tmp/zx.tar.gz ; \
    else \
      echo "NOTE: no zxtune123 prebuilt for $(uname -m) — PSF family disabled." ; \
    fi

# sc68 — Atari ST .sc68.  Built from the 2.2.1 release tarball (autotools) and
# staged into /out/sc68 via DESTDIR (binary + its shared libs).  Three fixes make
# this 2002-era tree build on modern Debian:
#   1. automake's fresh config.guess/config.sub + explicit --build (the ancient
#      config.guess/sub don't recognise aarch64/x86_64);
#   2. hardcode the six `#if @HAVE_*@` knobs in config_platform68.h.in that this
#      release's configure leaves UNSUBSTITUTED (bare `#if` → compile error);
#   3. -fcommon / implicit-decl CFLAGS for gcc-12, and drop the Makefiles' -O3
#      to -O1 (emu68 has -O3 aggressive-loop UB; -O3 also crashes gcc under qemu
#      cross-emulation).  On a native amd64 host this builds cleanly.
# The `make || make || make -j1` retry absorbs qemu-user's NON-DETERMINISTIC
# "cc1: internal compiler error: Segmentation fault" — building the amd64 image
# on an Apple-Silicon host (buildx + qemu) faults on a random translation unit;
# make resumes from cached objects, so a retry re-compiles only the crashed file,
# and the -j1 last resort removes emulation concurrency pressure.  No-op natively.
ARG SC68_URL=https://downloads.sourceforge.net/project/sc68/sc68/2.2.1/sc68-2.2.1.tar.gz
RUN set -eux; \
    if [ "$SLIM" = "1" ]; then echo "SLIM build — skipping sc68 (.sc68 disabled)"; exit 0; fi; \
    ( curl -sfL --max-time 300 "$SC68_URL" -o /tmp/sc68.tar.gz \
      && tar xzf /tmp/sc68.tar.gz -C /tmp \
      && cd /tmp/sc68-2.2.1 \
      && cp /usr/share/automake-*/config.guess ./config.guess \
      && cp /usr/share/automake-*/config.sub   ./config.sub \
      && sed -i -e 's/#if @HAVE_VSPRINTF@/#if 1/'  -e 's/#if @HAVE_VSNPRINTF@/#if 1/' \
                -e 's/#if @HAVE_GETENV@/#if 1/'     -e 's/#if @HAVE_ZLIB_H@/#if 1/' \
                -e 's/#if @HAVE_READLINE_READLINE_H@/#if 0/' \
                -e 's/#if @HAVE_READLINE_HISTORY_H@/#if 0/' config_platform68.h.in \
      && CFLAGS="-fcommon -Wno-implicit-function-declaration" \
         ./configure --prefix=/usr/local --build="$(gcc -dumpmachine)" \
      && find . -name Makefile -exec sed -i 's/-O3/-O1/g' {} + \
      && { make -j"$(nproc)" || make -j"$(nproc)" || make -j1; } \
      && make install DESTDIR=/out/sc68 ) \
      || echo "NOTE: sc68 build failed — Atari ST .sc68 disabled (SNDH/YM unaffected)." ; \
    rm -rf /tmp/sc68.tar.gz /tmp/sc68-2.2.1

# uade123 — Amiga "exotica" family (TFMX, Future Composer, David Whittaker, …).
# Not in Debian's apt archive, so built from source: uade 3.05 + its two deps
# (bencodetools, libzakalwe).  WAV output comes from libao inside uade123 itself;
# the resulting binary links only libao + libzakalwe/bencodetools (verified via ldd).
# --without-write-audio drops uade's optional SDL2/GL oscilloscope tool, whose
# install step runs `python3 setup.py install` and crashes on Python 3.12 (distutils
# was removed) — it is NOT the render path, so skipping it also removes the SDL2 dep.
# Best-effort: a failure disables only the uade family.  Artifacts staged to /out:
# uade123 → bin; the two .so + the lib/uade/uadecore m68k engine (uade123 spawns
# it per render — without it, --version works but playback dies) → lib; the
# eagleplayer/score/player data → share/uade.
RUN set -eux; \
    if [ "$SLIM" = "1" ]; then echo "SLIM build — skipping uade123 (Amiga exotica disabled)"; exit 0; fi; \
    ( apt-get update \
      && apt-get install -y --no-install-recommends libao-dev bzip2 \
      && git clone https://gitlab.com/heikkiorsila/bencodetools.git /tmp/bt \
      && ( cd /tmp/bt && (git checkout -q v1.0.1 2>/dev/null || true) \
           && ./configure --prefix=/usr/local --without-python \
           && { make -j"$(nproc)" || make -j"$(nproc)" || make -j1; } && make install ) \
      && git clone https://gitlab.com/hors/libzakalwe.git /tmp/lz \
      && ( cd /tmp/lz && (git checkout -q v1.0.0 2>/dev/null || true) && ./configure \
           && { make install PREFIX=/usr/local CC=cc || make install PREFIX=/usr/local CC=cc \
                || make -j1 install PREFIX=/usr/local CC=cc; } ) \
      && curl -sfL --max-time 300 https://zakalwe.fi/uade/uade3/uade-3.05.tar.bz2 -o /tmp/uade.tar.bz2 \
      && tar xjf /tmp/uade.tar.bz2 -C /tmp \
      && ( cd /tmp/uade-3.05 \
           && ./configure --prefix=/usr/local --libzakalwe-prefix=/usr/local \
              --bencode-tools-prefix=/usr/local --without-uadefs --without-write-audio \
           && { make -j"$(nproc)" || make -j"$(nproc)" || make -j1; } && make install ) \
      && install -m0755 /usr/local/bin/uade123 /out/bin/uade123 \
      && cp -a /usr/local/lib/libzakalwe.so* /usr/local/lib/libbencodetools.so* /out/lib/ \
      && cp -a /usr/local/lib/uade /out/lib/uade \
      && cp -a /usr/local/share/uade /out/share/uade ) \
      || echo "NOTE: uade123 build failed — Amiga exotica (TFMX/FC/…) disabled." ; \
    rm -rf /tmp/bt /tmp/lz /tmp/uade-3.05 /tmp/uade.tar.bz2 /var/lib/apt/lists/*

# ── Gate: every renderer must be present before we build the runtime image ──────
# Each step above is best-effort (|| echo NOTE) so a transient failure prints a
# clear reason instead of a cryptic mid-build COPY error.  This gate turns "no
# gaps if we go live" into a hard guarantee: if ANY expected artifact is missing
# — including the two data files that are easy to forget (uade's uadecore engine,
# sc68's mcoder replay driver) — the build FAILS here listing them, so a silent-
# gap image can never ship.  The usual cause is a qemu cross-emulation cc1
# segfault that outlived the compile retries: build linux/amd64 on a native amd64
# host.  To deliberately ship without a renderer, pass
# --build-arg ALLOW_MISSING_RENDERERS=1.
ARG ALLOW_MISSING_RENDERERS=0
RUN set -eu; \
    req="/out/bin/zxtune123 /out/bin/psgplay /out/bin/ym2wav /out/bin/hvl2wav"; \
    if [ "$SLIM" != "1" ]; then \
      req="$req /out/bin/uade123 /out/lib/uade/uadecore /out/lib/libzakalwe.so \
           /out/lib/libbencodetools.so /out/share/uade/eagleplayer.conf \
           /out/sc68/usr/local/bin/sc68 /out/sc68/usr/local/share/sc68/Replay/mcoder.bin"; \
    fi; \
    missing=""; \
    for f in $req; do [ -e "$f" ] || missing="$missing $f"; done; \
    if [ -n "$missing" ]; then \
      echo "═══ RENDERER GAP — missing artifacts:$missing" >&2; \
      [ "$ALLOW_MISSING_RENDERERS" = "1" ] \
        && echo "    (ALLOW_MISSING_RENDERERS=1 — shipping anyway)" >&2 \
        || { echo "    build linux/amd64 on a native amd64 host, or override with" >&2; \
             echo "    --build-arg ALLOW_MISSING_RENDERERS=1 (or SLIM=1 to drop uade/sc68)." >&2; exit 1; }; \
    fi; \
    echo "All required renderers present in /out (SLIM=${SLIM})."

# ═══════════════════════════════ Stage 2: runtime ═══════════════════════════════
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="SoniqBoom" \
      org.opencontainers.image.description="Self-hosted music server — chiptune, tracker, SID, MIDI, lossless and DSD." \
      org.opencontainers.image.source="https://github.com/SFCyris/SoniqBoom" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

ENV DEBIAN_FRONTEND=noninteractive

# ── Runtime renderers via apt (NO compiler / build tools in the final image) ──
#   ffmpeg            transcoding + DSD/ALAC/etc.
#   fluidsynth (+gm)  MIDI, with a General-MIDI SoundFont
#   sidplayfp         C64 SID          openmpt123  tracker modules
#   adplay            AdLib / OPL2 FM  libgme0     console chiptunes
#   libasound2/zlib1g runtime libs for the copied-in renderers (zxtune/sc68)
#   libao4            runtime lib for the source-built uade123
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fluidsynth fluid-soundfont-gm \
        sidplayfp \
        openmpt123 \
        adplay \
        libgme0 \
        libasound2 zlib1g libao4 \
        curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

# Renderers built in the builder stage: psgplay/zxtune123/ym2wav/hvl2wav/uade123
# onto PATH (the app finds ym2wav/hvl2wav via shutil.which — no runtime compiler);
# uade's libzakalwe/bencodetools .so + its eagleplayer/score data into /usr/local;
# sc68 + its shared libs into /usr/local.  sc68's share/sc68/Replay/*.bin are the
# m68k replay drivers it loads at render time (compiled-in path is
# /usr/local/share/sc68); without them sc68 parses a tune but emits no PCM
# (SC68rsc_open(…,mcoder,…) : not found), so its share tree must ship too.
COPY --from=builder /out/bin/                  /usr/local/bin/
COPY --from=builder /out/lib/                  /usr/local/lib/
COPY --from=builder /out/share/                /usr/local/share/
COPY --from=builder /out/sc68/usr/local/bin/   /usr/local/bin/
COPY --from=builder /out/sc68/usr/local/lib/   /usr/local/lib/
COPY --from=builder /out/sc68/usr/local/share/ /usr/local/share/
RUN ldconfig

# ── App ──────────────────────────────────────────────────────────────────────
WORKDIR /app
COPY . /app
# Editable install keeps the bundled frontend assets resolvable from the source
# tree (no dependence on package_data wiring).  A couple of deps (e.g. lhafile,
# for Amiga LHA archives) build a C extension with no prebuilt wheel, so add a
# compiler JUST for pip and purge it in the SAME layer — the final image keeps
# no toolchain (YM/HVL are already pre-built in the builder stage).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && pip install --no-cache-dir -e . \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

# The bundled soundfonts/ (~260 MB) is excluded from the image; point the app's
# SoundFont lookup at the apt-provided GM SoundFont so MIDI plays out of the box.
# Add richer SoundFonts at runtime via Settings → SoundFonts.
RUN mkdir -p /app/soundfonts \
    && ln -sf /usr/share/sounds/sf2/FluidR3_GM.sf2 /app/soundfonts/FluidR3_GM.sf2

# ── Runtime contract ─────────────────────────────────────────────────────────
# Mount your library read-only at /music; ALL state (index, conversion cache,
# config, logs) lives under /data so one named volume persists everything.
ENV SONIQBOOM_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080

# The server binds 0.0.0.0:8080 by default (config server.host/port), so it's
# reachable from outside the container with no extra flags.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

# tini as PID 1 reaps the renderer subprocesses (ffmpeg, sidplayfp, …) cleanly.
ENTRYPOINT ["tini", "--"]
CMD ["soniqboom"]
