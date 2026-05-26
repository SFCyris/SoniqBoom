# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Codec negotiation + DLNA / Cast / AirPlay protocol-info helpers.

Three jobs:

1. Map ``(source_codec, renderer_caps)`` → ``(deliver_codec, transcode?)``
   so we never blindly tax a Sonos with an MP3 transcode when it can
   decode the source FLAC natively.

2. Generate the DLNA ``contentFeatures.dlna.org`` header per codec.
   Cheap TVs use this string to decide whether to even open the GET;
   getting it wrong is the difference between "plays instantly" and
   "renderer refuses with no error".

3. Provide the canonical content-type / extension / DLNA profile-name
   triple so the rest of the cast stack stops re-deriving these
   independently.

Why this lives in core (not api/cast.py): the same negotiation runs
in three places — the /cast/{token} byte-server, the DLNA controller,
and the Cast controller.  Centralising it keeps them in lockstep.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


# ── Codec table ─────────────────────────────────────────────────────────────
# (codec) → (content_type, url_ext, dlna_profile)
#
# DLNA profile names are normative — the renderer's GetProtocolInfo
# Sink line declares the EXACT strings it understands.  Empty string
# = no PN advertised (renderers like Sonos accept this).

@dataclass(frozen=True)
class CodecSpec:
    codec:        str    # canonical lowercase: 'mp3' / 'flac' / 'wav' / 'aac' / 'opus' / 'ogg'
    content_type: str    # Content-Type: header
    url_ext:      str    # path extension (without dot)
    dlna_profile: str    # DLNA.ORG_PN= value, or "" if none / wildcard


# Order matters: when we pick the "best codec for renderer" we walk
# this list and take the first match.  Lossless first, then lossy by
# fidelity, then the universally-decodable fallback.
CODECS: dict[str, CodecSpec] = {
    # WAV/LPCM: DLNA-strict renderers want the L16 mime with rate +
    # channels, not "audio/wav".  We standardise on 44.1/2 here; the
    # DLNA controller layer can override the per-stream Content-Type
    # when the negotiated rate / channel count differs.
    #
    # Profile name: "LPCM" is the generic 7.4.34 entry that covers
    # 16-bit / 2-channel up to 48 kHz — strict consumers (Sonos, LG,
    # Samsung) accept it; we hand back empty for anything outside that
    # envelope at runtime via ``content_features``.
    "wav":  CodecSpec("wav",  "audio/L16;rate=44100;channels=2",
                              "wav",  "LPCM"),
    "flac": CodecSpec("flac", "audio/flac",        "flac", ""),
    "alac": CodecSpec("alac", "audio/mp4",         "m4a",  ""),
    # AAC: the DLNA profile name must match the container we actually
    # emit, not the codec.  cast_pipe ships ADTS-framed AAC for DLNA /
    # Cast (every frame self-syncs) and fragmented MP4 for AirPlay.
    # "AAC_ISO" is the ISO-MP4 container profile and was wrong for the
    # ADTS path — Sonos / Samsung firmware rejects the stream because
    # the declared profile mismatches the bytes on the wire.  Leaving
    # the profile empty lets renderers infer from the mime; we still
    # emit DLNA.ORG_OP + DLNA.ORG_FLAGS via content_features() so the
    # protocol stays well-formed.  (DLNA Guidelines 7.4.34 + audio
    # codec annex A.5 ADTS vs A.6 ISO-MP4.)
    "aac":  CodecSpec("aac",  "audio/aac",         "aac",  ""),
    "opus": CodecSpec("opus", "audio/ogg",         "opus", ""),
    "ogg":  CodecSpec("ogg",  "audio/ogg",         "ogg",  ""),
    "mp3":  CodecSpec("mp3",  "audio/mpeg",        "mp3",  "MP3"),
}

# Renderer-side defaults when GetProtocolInfo isn't available or
# returns garbage.  Sourced from published spec docs:
#
#  Chromecast Audio:  https://developers.google.com/cast/docs/media
#  AirPlay 2:         https://en.wikipedia.org/wiki/AirPlay
#  Generic DLNA:      MP3 mandatory; FLAC / WAV widely supported
#
# Note: ALAC is the Apple-native lossless format; HomePod / Apple TV
# prefer it.  Sonos and Chromecast prefer FLAC.  When negotiating
# we'll honour explicit renderer caps first, then fall back to
# protocol-default.

DEFAULT_CAPS = {
    "cast":    {"mp3", "flac", "ogg", "opus", "aac", "wav"},
    "airplay": {"alac", "aac", "mp3", "wav"},
    "dlna":    {"mp3", "wav"},  # safe minimum; probe widens this
}


# ── Negotiation ────────────────────────────────────────────────────────────

def negotiate_codec(
    *,
    source_codec: str,
    renderer_caps: set[str] | None,
    protocol: str = "dlna",
    user_pref: str = "auto",
    source_bit_depth: int | None = None,
) -> tuple[str, bool]:
    """Pick the codec we'll deliver and whether we need to transcode.

    Algorithm:

      1. ``user_pref == "force-mp3"``      → MP3 (universal fallback).
      2. Source codec ∈ caps               → native (no transcode).
      3. ``"flac"`` ∈ caps and source is
         losslessly-representable          → FLAC (lossless transcode).
      4. ``"alac"`` ∈ caps (AirPlay)       → ALAC (Apple-native).
      5. ``"mp3"``  ∈ caps                 → MP3 320 (universal lossy).
      6. else                              → MP3 anyway, with a warn
                                              that the renderer didn't
                                              advertise it (most do
                                              implicitly).

    Returns (delivered_codec, transcode_needed).
    """
    src = (source_codec or "").lower().strip()
    if not src:
        src = "mp3"
    caps = renderer_caps or DEFAULT_CAPS.get(protocol.lower(), {"mp3"})

    if user_pref == "force-mp3":
        return ("mp3", src != "mp3")

    # 2. Native — same codec source-side and renderer-supported.
    if src in caps:
        return (src, False)

    # Source-format aliasing:
    #   .m4a (ALAC) → 'alac' if source is ALAC, 'aac' if AAC
    #   For now treat 'm4a' as 'alac' so AirPlay renderers get it right;
    #   the byte-server probes again and chooses the right path.
    if src == "m4a" and "alac" in caps:
        return ("alac", False)

    # 3. Lossless preference.  DSD always transcodes (no consumer
    # renderer decodes raw .dsf/.dff/.wsd), but FLAC is the correct
    # *target* for DSD when the renderer can decode it — FLAC carries
    # 24-bit / 96 kHz PCM losslessly, which is what we get out of the
    # DSD → PCM filter chain.  High-bit-depth FLAC also covers any
    # other lossless source we couldn't deliver natively (ALAC on a
    # non-Apple renderer, WavPack, etc.).
    if "flac" in caps:
        return ("flac", True)

    # 4. AirPlay strong preference for ALAC over MP3.
    if protocol.lower() == "airplay" and "alac" in caps:
        return ("alac", True)

    # 5. Universal lossy.
    if "mp3" in caps:
        return ("mp3", True)

    log.warning(
        "Renderer %s declared no decoder we can produce; defaulting to mp3 anyway.",
        protocol,
    )
    return ("mp3", True)


# ── DLNA contentFeatures.dlna.org builders ────────────────────────────────

# DLNA.ORG_OP — Operation parameters
#   bit 0: Range-supported via byte-range  (0=no, 1=yes)
#   bit 1: TimeSeekRange supported          (0=no, 1=yes)
# Almost every renderer wants OP=01 (byte-range).
_DLNA_OP_RANGE_ONLY = "01"

# DLNA.ORG_FLAGS — 32-bit hex right-padded to 24 nibbles (long form).
#
# We declare DLNA.ORG_OP=01 (byte-range YES, time-range NO), so the
# matching flags are:
#
#   0x00100000  BACKGROUND_TRANSFER_MODE   — background download OK
#   0x00200000  CONNECTION_STALLING_ALLOWED — buffering tolerated
#   0x01000000  DLNA_V1_5_FLAG             — DLNA 1.5 client present
#
# Total: 0x01300000.  The previous default (0x01700000) included
# 0x00400000 (PLAY_CONTAINER) which is for playlist/container URIs
# — not single audio files — and tripped strict Samsung firmware.
_DLNA_FLAGS_DEFAULT = "01300000000000000000000000000000"


def content_features(codec: str) -> str:
    """Build the contentFeatures.dlna.org header value for ``codec``.

    Header form (per DLNA Guidelines 7.4.1.3):
        DLNA.ORG_PN=<profile>;DLNA.ORG_OP=01;DLNA.ORG_FLAGS=<flags>

    Profile omitted entirely when the codec has no canonical PN
    (FLAC, OGG, OPUS, ALAC) — Sonos / LG accept this; older Samsung
    sets prefer it explicit (LPCM, MP3 always emitted).
    """
    spec = CODECS.get(codec.lower())
    if spec is None:
        return ""
    parts = []
    if spec.dlna_profile:
        parts.append(f"DLNA.ORG_PN={spec.dlna_profile}")
    parts.append(f"DLNA.ORG_OP={_DLNA_OP_RANGE_ONLY}")
    parts.append(f"DLNA.ORG_FLAGS={_DLNA_FLAGS_DEFAULT}")
    return ";".join(parts)


def dlna_response_headers(codec: str, *, transfer_mode: str = "Streaming") -> dict[str, str]:
    """Return the full set of DLNA headers to attach to a byte-stream
    response.  Safe to attach unconditionally — non-DLNA clients
    ignore them.

    ``transfer_mode`` must be one of:

      * ``"Streaming"`` — live audio/video, default for our streams.
      * ``"Interactive"`` — short clips, low-latency.
      * ``"Background"`` — file downloads (sync to disk).
    """
    return {
        "contentFeatures.dlna.org":     content_features(codec),
        "transferMode.dlna.org":        transfer_mode,
        "realTimeInfo.dlna.org":        "DLNA.ORG_TLAG=*",
    }


# ── Renderer-capability probe parsing ──────────────────────────────────────

def parse_sink_protocol_info(sink_csv: str) -> set[str]:
    """Parse a DLNA SinkProtocolInfo CSV into a set of canonical codec
    names we recognise.  Anything we don't recognise is silently
    dropped — we'd rather under-claim caps and transcode unnecessarily
    than over-claim and feed an undecodable stream.

    Sample input (from a Sonos S2 GetProtocolInfo response):
        http-get:*:audio/mpeg:*,http-get:*:audio/flac:*,
        http-get:*:audio/x-wav:*,http-get:*:audio/L16;rate=44100;channels=2:*

    Output: {"mp3", "flac", "wav"}
    """
    if not sink_csv:
        return set()
    caps: set[str] = set()
    mime_to_codec = {
        # canonical
        "audio/mpeg":       "mp3",
        "audio/mp3":        "mp3",
        "audio/flac":       "flac",
        "audio/x-flac":     "flac",
        "audio/wav":        "wav",
        "audio/x-wav":      "wav",
        "audio/wave":       "wav",
        "audio/l16":        "wav",   # LPCM 16-bit — we deliver via WAV
        "audio/l24":        "wav",
        "audio/aac":        "aac",
        "audio/mp4":        "aac",
        "audio/m4a":        "aac",
        "audio/x-m4a":      "aac",
        "audio/ogg":        "ogg",
        "audio/x-ogg":      "ogg",
        "audio/opus":       "opus",
        "audio/vorbis":     "ogg",
    }
    for entry in sink_csv.split(","):
        # Each entry: ``http-get:*:<mime>;<params>:*``
        parts = entry.strip().split(":")
        if len(parts) < 3:
            continue
        mime_full = parts[2].strip().lower()
        # Strip parameters (``;rate=44100;channels=2`` etc.)
        mime = mime_full.split(";", 1)[0].strip()
        codec = mime_to_codec.get(mime)
        if codec:
            caps.add(codec)
    return caps
