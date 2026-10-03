# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Render-then-transcode bridge for the cast pipeline.

The cast pipeline (cast_pipe.render_stream) feeds ffmpeg with the
source file directly.  That works for every codec ffmpeg can demux —
MP3 / FLAC / WAV / OGG / OPUS / AAC / ALAC / AIFF / WavPack / Musepack
/ DSD — but NOT for the rendered formats SoniqBoom supports:

  • SID (.sid, .psid, .rsid) — requires sidplayfp
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
from soniqboom.core.filesource import is_remote_path

log = logging.getLogger(__name__)


# ── Extension sets ─────────────────────────────────────────────────────────
# Mirror the constants in stream.py — keeping a copy here lets us avoid
# importing stream.py at module-load time (circular-import risk through
# the FastAPI router registration in main.py).

from soniqboom.core.metadata import SID_EXTS as _SID_EXTS   # .sid / .psid / .rsid
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
    # ``subsong`` 0 is the bare play: the file's default tune, which for a
    # multi-tune Amiga / libgme file may not be its first (``stream.tune_index``).
    # SID maps the wire itself.
    if e not in _SID_EXTS:
        from soniqboom.api.stream import tune_index
        from soniqboom.core.store import get_store
        subsong = tune_index(track_id, get_store().get_track(track_id), subsong or None)
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
        # The subsong base ``prepare_source_for_stream`` resolved (and
        # remembered) for this track — part of the key for tune N > 0 of a
        # module numbered from 1.
        from soniqboom.api.stream import uade_cache_key_known
        return uade_cache_key_known(track_id, subsong)
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
        if is_remote_path(outer):
            # Mirror ONLY the OUTER remote file (the module itself, or the
            # archive that contains it) into the local remote-cache first.
            from soniqboom.core.filesource import get_source, parse_remote_path
            from soniqboom.core.remote_cache import get_cache
            scan_root, remote_path = parse_remote_path(outer)
            if not remote_path:
                return None
            # No source (not connected, or its sign-in refused): what the
            # remote cache holds still plays.
            source = get_source(scan_root)
            loop = asyncio.get_running_loop()
            if sep:
                # Only the member (+ companions) when the archive isn't cached.
                from soniqboom.api.stream import _archive_companion_filter
                from soniqboom.core.remote_zip import archive_for_member
                fetch = functools.partial(
                    archive_for_member, scan_root, remote_path, member, source,
                    lane=lane, companion=_archive_companion_filter(member))
            elif source is not None:
                fetch = functools.partial(
                    get_cache().fetch, scan_root, remote_path, source, lane=lane)
            else:
                fetch = functools.partial(get_cache().get_cached, scan_root, remote_path)
            local_outer = await loop.run_in_executor(None, fetch)
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


async def _render_input(track_id: str, track_path: str,
                        subsong: int) -> "tuple[Path, str, int]":
    """``(path, ext, subsong)`` a render of this source takes: the LOCAL file
    (an archive member or a remote source materialized), its extension, and
    the tune — the index every renderer but SID takes (a bare play's being
    the file's default tune), the wire for SID."""
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
            "::" in track_path or is_remote_path(track_path)):
        resolved = await materialize_source(track_path, track_id)
        if resolved is not None:
            path_obj = resolved

    # ``subsong`` 0 is the bare play: the file's default tune — for a
    # multi-tune Amiga / libgme file its first tune that isn't empty, probed
    # once (``stream.ensure_default_tune``, the probe a web play runs).  The
    # renderers below except SID take the tune index.
    if src_ext not in _SID_EXTS and is_rendered_format(src_ext):
        from soniqboom.api.stream import _bare_play_track, _probe_family, tune_index
        from soniqboom.core.data import get_track
        _track = await get_track(track_id)
        if not subsong and path_obj.exists():
            _track = await _bare_play_track(track_id, _track, path_obj,
                                            _probe_family(src_ext, False, path_obj))
        subsong = tune_index(track_id, _track, subsong or None)
    return path_obj, src_ext, subsong


async def prepare_live_source(
    *,
    track_id: str,
    track_path: str,
    subsong: int = 0,
):
    """A WAV byte stream of a rendered source's render AS IT RENDERS, for the
    cast / DLNA / AirPlay transcode pipeline (``cast_pipe.render_stream``'s
    ``src_feed``): ffmpeg then starts on the render's first audio instead of
    its end.  For the formats whose render is slow enough to wait on and can
    be read while it grows — SID (sidplayfp), MIDI (fluidsynth), AHX
    (uade123) and the libgme chiptunes — under the same cache keys
    ``prepare_source_for_stream`` renders them to (the render is cached as
    usual).  None when there is nothing live to offer: another format, the
    render is already cached, or it ended before it was audible (failed,
    silent, or simply short) — the caller then uses
    ``prepare_source_for_stream``, which serves the cached file or attaches
    to the render and raises its error.  A SID that plays only silence
    raises the cache's 422."""
    visible_path = track_path.split("::")[-1] if "::" in track_path else track_path
    ext = Path(visible_path).suffix.lower()
    if not (ext in _SID_EXTS or ext in _MIDI_EXTS or ext in _UADE_EXTS
            or ext in _GME_EXTS):
        return None
    path_obj, src_ext, subsong = await _render_input(track_id, track_path, subsong)
    if not path_obj.exists():
        return None
    from soniqboom.api import stream as _st
    from soniqboom.core.conversion_cache import _cache_key, get_cached
    if src_ext in _SID_EXTS:
        if not _st._is_c64_sid(path_obj):
            return None
        from soniqboom.config import settings
        target_dur = int(getattr(settings, "sid_default_duration", 180))
        key = _cache_key(track_id, "sid", subsong=subsong, duration=target_dur)
        if await get_cached(key) is not None:
            return None
        return await _st.sid_wav_feed(path_obj, subsong, target_dur, key)
    if src_ext in _MIDI_EXTS:
        from soniqboom.config import get_active_soundfont
        sf = get_active_soundfont()
        sfp = str(sf) if sf else ""
        key = _cache_key(track_id, "midi", soundfont_path=sfp)
        return await _st.live_wav_feed(
            track_id, format_type="midi", subsong=0, key=key,
            render_fn=lambda: _st._render_midi(path_obj, live_key=key),
            cache_kw={"soundfont_path": sfp})
    if src_ext in _UADE_EXTS:
        base = await _st._uade_resolve_base(track_id, None, path_obj, subsong)
        key = _st.uade_cache_key(track_id, subsong, base)
        return await _st.live_wav_feed(
            track_id, format_type="uade", subsong=subsong, key=key,
            render_fn=lambda: _st._render_uade(path_obj, subsong=subsong, live_key=key,
                                               subsong_base=base),
            cache_kw={"variant": _st.uade_cache_variant(subsong, base)})
    key = _cache_key(track_id, "gme", subsong=subsong)
    return await _st.live_wav_feed(
        track_id, format_type="gme", subsong=subsong, key=key,
        render_fn=lambda: _st._render_gme(path_obj, subsong=subsong, live_key=key))


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
    path_obj, src_ext, subsong = await _render_input(track_id, track_path, subsong)

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
        from soniqboom.api.stream import (
            _render_uade, _uade_resolve_base, uade_cache_variant,
        )
        base = await _uade_resolve_base(track_id, None, path_obj, subsong)
        cached_path, _hit = await get_or_render(
            track_id=track_id, format_type="uade", subsong=subsong,
            render_fn=lambda: _render_uade(path_obj, subsong=subsong, subsong_base=base),
            variant=uade_cache_variant(subsong, base),
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
