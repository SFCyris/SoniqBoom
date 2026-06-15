# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# SoniqBoom — self-hosted music server.  https://github.com/SFCyris/SoniqBoom
# One image, every format: chiptune · tracker · SID · MIDI · lossless · DSD.
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="SoniqBoom" \
      org.opencontainers.image.description="Self-hosted music server — chiptune, tracker, SID, MIDI, lossless and DSD." \
      org.opencontainers.image.source="https://github.com/SFCyris/SoniqBoom" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

ENV DEBIAN_FRONTEND=noninteractive

# ── System renderers + ffmpeg ────────────────────────────────────────────────
# These let SoniqBoom play the formats other servers forget.  A missing renderer
# only disables its own format (SoniqBoom degrades gracefully and tells the user
# which package to add), so the optional players never fail the build.
#   ffmpeg            transcoding + DSD/ALAC/etc.
#   fluidsynth (+gm)  MIDI, with a General-MIDI SoundFont so MIDI plays out of box
#   sidplayfp         C64 SID
#   openmpt123        tracker modules (MOD/XM/IT/S3M/…)
#   adplay            AdLib / OPL2 FM (DOS-era IMF/ROL/CMF/…)
#   libgme0           console chiptunes (NSF/SPC/GBS/VGM/AY/KSS/…)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fluidsynth fluid-soundfont-gm \
        sidplayfp \
        openmpt123 \
        adplay \
        libgme0 \
        curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

# uade (Amiga AHX) isn't in every Debian mirror — best effort, never fail the build.
RUN apt-get update \
    && (apt-get install -y --no-install-recommends uade123 \
        || echo "NOTE: uade123 unavailable on this mirror — AHX (.ahx) playback disabled.") \
    && rm -rf /var/lib/apt/lists/*

# ── App ──────────────────────────────────────────────────────────────────────
WORKDIR /app
COPY . /app
# Editable install keeps the bundled frontend assets resolvable from the source
# tree (no dependence on package_data wiring) and is the smallest reliable path.
RUN pip install --no-cache-dir -e .

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
