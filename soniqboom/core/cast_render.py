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

import asyncio
import functools
import logging
from pathlib import Path

from soniqboom.core.conversion_cache import get_or_render

log = logging.getLogger(__name__)

# Remote-source schemes the cast render path resolves.  Deliberately limited to
# the schemes ``filesource.parse_remote_path`` actually accepts (it RAISES
# ValueError on http/webdav) — matching the effective handled set keeps the
# tuple honest and lets a webdav/http path degrade to a clean ``None`` miss
# instead of a caught-ValueError traceback.  Same set as cast_stream's inline
# composite-remote branch.  (``core.source_bytes`` lists the broader aspirational
# set, but its resolution funnels through the same smb/ftp-only parse_remote_path.)
_REMOTE_SCHEMES = ("smb://", "ftp://")


# ── Extension sets ─────────────────────────────────────────────────────────
# Mirror the constants in stream.py — keeping a copy here lets us avoid
# importing stream.py at module-load time (circular-import risk through
# the FastAPI router registration in main.py).

_SID_EXTS = {".sid", ".psid"}
_MIDI_EXTS = {".mid", ".midi"}
# AHX needs uade123, NOT openmpt123.  HVL (HivelyTracker) needs the bundled
# hvl2wav (neither uade nor openmpt decodes it).  Both kept separate from the
# openmpt-handled tracker set so the cast dispatcher picks the right renderer.
_UADE_EXTS = {".ahx"}
_HVL_EXTS = {".hvl"}
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
            or e in _HVL_EXTS
            or e in _TRACKER_EXTS
            or e in _GME_EXTS)


def rendered_cache_key(track_id: str, source_ext: str, subsong: int = 0) -> str | None:
    """The conversion-cache key ``prepare_source_for_stream`` will populate for
    this source, or ``None`` for ffmpeg-native sources.

    SINGLE SOURCE OF TRUTH for "what key does cast_render produce" — cast_stream
    calls this to PIN the rendered WAV so the N+1 prewarm's evictor can't unlink
    it mid-stream.  The format_type + key-relevant params below MUST stay in
    lockstep with the ``get_or_render(...)`` calls in ``prepare_source_for_stream``
    (same ext sets, same SID duration, same MIDI soundfont, same subsong).
    """
    e = (source_ext or "").lower()
    if not e.startswith("."):
        e = "." + e
    from soniqboom.core.conversion_cache import _cache_key
    if e in _SID_EXTS:
        from soniqboom.config import settings
        dur = int(getattr(settings, "sid_default_duration", 180))
        return _cache_key(track_id, "sid", subsong=subsong, duration=dur)
    if e in _MIDI_EXTS:
        from soniqboom.config import get_active_soundfont
        sf = get_active_soundfont()
        return _cache_key(track_id, "midi", soundfont_path=str(sf) if sf else "")
    if e in _HVL_EXTS:
        return _cache_key(track_id, "hvl", subsong=subsong)
    if e in _UADE_EXTS:
        return _cache_key(track_id, "uade", subsong=subsong)
    if e in _TRACKER_EXTS:
        return _cache_key(track_id, "tracker", subsong=subsong)
    if e in _GME_EXTS:
        return _cache_key(track_id, "gme", subsong=subsong)
    return None


async def materialize_source(
    track_path: str, track_id: str, *, lane: str = "stream",
) -> Path | None:
    """Resolve any track-path shape to a LOCAL filesystem ``Path`` the renderers
    (and ffmpeg) can read, or ``None`` on a miss.

    Handles all four shapes::

        /local/file.mod                          → returned as-is
        /local/archive.zip::inner.mod            → stable extracted member
        ftp://host/share:/dir/file.mod           → fetched into the remote-cache
        ftp://host/share:/dir/archive.zip::x.mod → OUTER fetched, member extracted

    The ``::`` archive tail is partitioned **first**, so a composite
    remote-archive path fetches only the OUTER container (a cache hit reuses the
    copy playback already downloaded — no re-fetch) and THEN extracts the
    member.  This is the fix for the "remote scheme checked before the archive
    member" bug in the cast render paths: a resolver that tested the remote
    scheme first would hand the whole ``…archive.zip::member`` string to the
    remote fetch (no such remote file exists) or read the raw container without
    extracting the member.  It mirrors ``core.source_bytes.read_source_bytes``
    but returns a PATH — renderers need a filesystem path, not bytes.

    Local archive members go through ``_get_or_extract_zip_member`` — the same
    stable, mtime-gated, ``_zip_pin``-able extraction cache the foreground
    stream uses — so there's no temp-file churn or double extraction, and a
    caller may pin the returned member by ``track_id``.

    ``lane`` selects the remote-cache I/O pool for the OUTER fetch: foreground
    callers use the default ``"stream"`` (playback-priority); background prewarm
    passes ``lane="scan"`` so bulk pulls don't starve playback.  Blocking
    network / disk I/O runs in an executor.  Never raises — returns ``None`` on
    any miss so callers degrade gracefully.
    """
    try:
        outer, sep, member = track_path.partition("::")
        if outer.startswith(_REMOTE_SCHEMES):
            # Mirror ONLY the OUTER remote file (the module itself, or the
            # archive that contains it) into the local remote-cache first.
            from soniqboom.core.filesource import get_source, parse_remote_path
            from soniqboom.core.remote_cache import get_cache
            scan_root, remote_path = parse_remote_path(outer)
            source = get_source(scan_root) if remote_path else None
            if source is None:
                return None
            loop = asyncio.get_running_loop()
            local_outer = await loop.run_in_executor(
                None,
                functools.partial(
                    get_cache().fetch, scan_root, remote_path, source, lane=lane,
                ),
            )
            if not local_outer:
                return None
            outer = str(local_outer)
        if sep:
            # Archive member (local outer, or the remote outer just fetched) —
            # extract via the shared, pinnable on-disk cache.
            from soniqboom.api.stream import _get_or_extract_zip_member
            return await _get_or_extract_zip_member(f"{outer}::{member}", track_id)
        p = Path(outer)
        return p if p.exists() else None
    except Exception:
        # Non-fatal: the caller degrades (renderer FileNotFoundError → 410,
        # prewarm → skip).  Keep the traceback at warning so a genuine
        # remote/archive fetch failure stays diagnosable.
        log.warning("cast: materialize_source failed for %s", track_path, exc_info=True)
        return None


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

    Raises ``FileNotFoundError`` if the source doesn't exist on disk (the
    caller maps it to 410); the underlying ``_render_*`` helpers raise
    ``HTTPException(501, "<binary> not installed")`` when the required renderer
    binary is missing, which cast_stream re-raises unchanged.
    """
    # Strip ``outer.zip::inner.mod`` to the inner filename for the
    # extension test, but feed the renderer the FULL path — the
    # rendering helpers know how to extract from ZIP themselves.
    visible_path = track_path.split("::")[-1] if "::" in track_path else track_path
    src_ext = Path(visible_path).suffix.lower()
    path_obj = Path(track_path)

    # Archive- or remote-contained rendered sources: resolve to the real LOCAL
    # file the renderers (sidplayfp / fluidsynth / openmpt123 / uade123 /
    # hvl2wav) can read — they take a filesystem path and can't read a
    # ``zip::member`` virtual path OR a ``ftp://…`` remote URL.  ``materialize_
    # source`` partitions the ``::`` archive tail FIRST, so a COMPOSITE
    # remote-archive path (``ftp://…album.zip::inner.mod``) fetches only the
    # OUTER container then extracts the member — the "remote scheme checked
    # before the archive member" bug this call closes.  Without it, a COLD cast
    # render of any remote / remote-zip / zip-contained tracker/SID/HVL fails
    # (both the audio render AND the render-time VU sidecar).  Local archive
    # members reuse the same stable extraction cache the foreground stream uses
    # ⇒ no temp churn and no double-extraction.  cast_stream normally
    # pre-resolves to a local path, so for that caller this is a no-op; it keeps
    # the function correct in isolation for any other caller.
    if is_rendered_format(src_ext) and (
            "::" in track_path or track_path.startswith(_REMOTE_SCHEMES)):
        resolved = await materialize_source(track_path, track_id)
        if resolved is not None:
            path_obj = resolved

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

    if src_ext in _HVL_EXTS:
        # HivelyTracker → bundled hvl2wav (neither uade123 nor openmpt123
        # decodes HVL).  Checked BEFORE uade + tracker (the ext also appears
        # in the broader scanner tracker set).
        from soniqboom.api.stream import _render_hvl
        cached_path, _hit = await get_or_render(
            track_id=track_id, format_type="hvl", subsong=subsong,
            render_fn=lambda: _render_hvl(path_obj, subsong=subsong),
        )
        return cached_path, "wav"

    if src_ext in _UADE_EXTS:
        # AHX → uade123.  Must be checked BEFORE the tracker branch — the
        # extension also appears in the broader tracker set used by the
        # library scanner, but openmpt123 can't decode it.
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
