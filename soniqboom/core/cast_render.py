# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Render-then-transcode bridge for the cast pipeline.

The cast pipeline (cast_pipe.render_stream) feeds ffmpeg with the
source file directly.  That works for every codec ffmpeg can demux —
MP3 / FLAC / WAV / OGG / OPUS / AAC / ALAC / AIFF / WavPack / Musepack
/ DSD — but NOT for the rendered formats SoniqBoom supports:

  • SID (.sid, .psid) — requires sidplayfp
  • MIDI (.mid, .midi) — requires FluidSynth + a SoundFont
  • Tracker (.mod, .s3m, .xm, .it, …) — requires openmpt123
  • GME (.nsf, .spc, .gbs, .vgm, …) — requires libgme

For these, ffmpeg sees a binary blob it can't parse and produces zero
bytes; the renderer hits "stream ended early" with no useful error.

This module bridges by:

  1. Detecting the source's extension.
  2. If it's a rendered format, kicking off the right
     ``_render_<sid|midi|tracker|gme>`` helper from stream.py (which
     already handles cache hits, partial-renders, HVSC duration
     lookup, SoundFont selection, subsong propagation).
  3. Returning the **WAV path** that the renderer produced, plus an
     effective source-codec of ``"wav"`` so the downstream cast_pipe
     transcode picks ffmpeg's pcm_s16le decoder.

For non-rendered formats this is a no-op (returns the original path
unchanged) — the function is safe to call on every cast request.
"""
from __future__ import annotations

import logging
from pathlib import Path

from soniqboom.core.conversion_cache import get_or_render

log = logging.getLogger(__name__)


# ── Extension sets ─────────────────────────────────────────────────────────
# Mirror the constants in stream.py — keeping a copy here lets us avoid
# importing stream.py at module-load time (circular-import risk through
# the FastAPI router registration in main.py).

_SID_EXTS = {".sid", ".psid"}
_MIDI_EXTS = {".mid", ".midi"}
# AHX / Hively need uade123, NOT openmpt123 — keep them separate from
# the openmpt-handled tracker set so the cast dispatcher picks the
# right renderer.
_UADE_EXTS = {".ahx", ".hvl"}
_TRACKER_EXTS = {
    ".mod", ".s3m", ".xm", ".it", ".mtm", ".med", ".oct",
    ".669", ".dbm", ".ult", ".stm", ".far",
    ".amf", ".gdm", ".imf", ".okt", ".sfx", ".wow", ".dsm",
}
# Stream-supported libgme containers (matches _GME_EXTS_STREAM in stream.py).
_GME_EXTS = {
    ".nsf", ".nsfe", ".spc", ".gbs", ".vgm", ".vgz",
    ".ay", ".kss", ".sap", ".gym", ".hes",
}


def is_rendered_format(source_ext: str) -> bool:
    """True if ``source_ext`` needs an external renderer before
    ffmpeg can ingest it."""
    e = (source_ext or "").lower()
    if not e.startswith("."):
        e = "." + e
    return (e in _SID_EXTS
            or e in _MIDI_EXTS
            or e in _UADE_EXTS
            or e in _TRACKER_EXTS
            or e in _GME_EXTS)


async def prepare_source_for_stream(
    *,
    track_id: str,
    track_path: str,
    subsong: int = 0,
) -> tuple[Path, str]:
    """Return ``(path, effective_codec)`` for the cast / DLNA / AirPlay
    transcode pipeline.

    For ffmpeg-native sources (MP3, FLAC, DSD, ALAC, …) this returns
    ``(Path(track_path), <ext>)`` unchanged.

    For rendered formats (SID, MIDI, tracker, GME), this runs the right
    renderer (cached via ``conversion_cache.get_or_render`` so a second
    play hits the on-disk cache) and returns the resulting WAV path
    with ``effective_codec = "wav"``.

    ``subsong`` is honoured for SID / tracker / GME; ignored for MIDI
    and other single-track formats.

    Raises ``FileNotFoundError`` if the source doesn't exist on disk,
    ``RuntimeError`` if the required renderer binary is missing.  The
    caller (cast_stream) maps both to user-visible HTTP responses.
    """
    # Strip ``outer.zip::inner.mod`` to the inner filename for the
    # extension test, but feed the renderer the FULL path — the
    # rendering helpers know how to extract from ZIP themselves.
    visible_path = track_path.split("::")[-1] if "::" in track_path else track_path
    src_ext = Path(visible_path).suffix.lower()
    path_obj = Path(track_path)

    if src_ext in _SID_EXTS:
        # Late import — keeps the cast modules independently loadable
        # even if the SID renderer fails to import for any reason
        # (sidplayfp not installed, HVSC unconfigured, etc.).
        from soniqboom.api.stream import _render_sid
        from soniqboom.config import settings
        # We don't have a per-tune target_dur here without an HVSC
        # lookup; let _render_sid honour its own settings default.
        target_dur = int(getattr(settings, "sid_default_duration", 180))
        cached_path, _hit = await get_or_render(
            track_id=track_id, format_type="sid", subsong=subsong,
            duration=target_dur,
            render_fn=lambda: _render_sid(path_obj, subsong=subsong, duration=target_dur),
        )
        return cached_path, "wav"

    if src_ext in _MIDI_EXTS:
        from soniqboom.api.stream import _render_midi
        from soniqboom.config import get_active_soundfont
        sf = get_active_soundfont()
        cached_path, _hit = await get_or_render(
            track_id=track_id, format_type="midi", subsong=0,
            render_fn=lambda: _render_midi(path_obj),
            soundfont_path=str(sf) if sf else "",
        )
        return cached_path, "wav"

    if src_ext in _UADE_EXTS:
        # AHX / Hively → uade123.  Must be checked BEFORE the tracker
        # branch — both extensions also appear in the broader tracker
        # set used by the library scanner, but openmpt123 can't decode
        # them.
        from soniqboom.api.stream import _render_uade
        cached_path, _hit = await get_or_render(
            track_id=track_id, format_type="uade", subsong=subsong,
            render_fn=lambda: _render_uade(path_obj, subsong=subsong),
        )
        return cached_path, "wav"

    if src_ext in _TRACKER_EXTS:
        from soniqboom.api.stream import _render_tracker
        cached_path, _hit = await get_or_render(
            track_id=track_id, format_type="tracker", subsong=subsong,
            render_fn=lambda: _render_tracker(path_obj, subsong=subsong),
        )
        return cached_path, "wav"

    if src_ext in _GME_EXTS:
        from soniqboom.api.stream import _render_gme
        cached_path, _hit = await get_or_render(
            track_id=track_id, format_type="gme", subsong=subsong,
            render_fn=lambda: _render_gme(path_obj, subsong=subsong),
        )
        return cached_path, "wav"

    # Non-rendered source — ffmpeg can handle it directly.
    return path_obj, src_ext.lstrip(".") or "?"
