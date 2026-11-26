# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Conversion cache for rendered audio (SID, MIDI, tracker modules).

Caches the WAV output of sidplayfp / fluidsynth / openmpt123 so repeat
playback of the same track serves instantly from disk rather than
re-rendering every time.

Directory layout:
  <cache_dir>/sid/<key[:2]>/<key>.wav
  <cache_dir>/midi/<key[:2]>/<key>.wav
  <cache_dir>/tracker/<key[:2]>/<key>.wav

LRU metadata is kept in-memory.  WAV files live on disk.
Eviction runs after every cache write: while total > max, pop the
oldest entry and delete its file.

Packed entries (cache entry format v1, ``PACK_SUFFIX``)
------------------------------------------------------
A finished render is compressed in the background, losslessly, to
``<key>.sbwv`` beside its WAV (``pack_entry``; the worker ``start_packer``:
one at a time, only while no render runs, stopped when one starts, its
ffmpeg at the lowest CPU / I/O priority — its own reads of the WAV for the
silence check and a float render's peak are ordinary Python): our header
(magic ``SBWV``, ``PACK_VERSION``, the original WAV's size / mtime / MD5 and
its header bytes verbatim) followed by a WavPack stream that ffmpeg's own
wavpack codec writes and reads.  The decoded file is byte for byte the WAV
it was made from, 32-bit float renders included (verified by decoding it
before it is kept).  Once packed, the WAV stays as the entry's decoded copy
("hot") until the cache needs the room: eviction drops the decoded copies of
packed entries (oldest first; the newest ``_HOT_FLOOR_FRACTION`` of the
budget, pinned ones and ones used in the last ``_HOT_RECENT_S`` kept) before
it removes renders.  ``get_cached`` of a packed entry without its decoded
copy decodes it back to ``<key>.wav`` first, so every consumer keeps reading
a plain WAV (``open_cached_audio`` pins it for the reader); ``unpacking``
hands the web stream the decode while it is written (first bytes in
~35 ms).  The budget counts the bytes on disk (pack + decoded copy).  Code
that predates packed entries adopts only ``*.wav``: it sees a packed-only
entry as a miss and renders it again (it neither counts nor clears the
packs); a reader that finds another version of the header drops the file (a
miss, never garbage).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import itertools
import logging
import os
import re
import secrets
import shutil
import struct
import sys
import threading
import time
import weakref
import zlib
from collections import OrderedDict
from pathlib import Path

from fastapi import HTTPException

from soniqboom.config import get_conversion_cache_dir, settings
from soniqboom.core import render_options as _render_options
# Aliased: this module already defines a public ``async def cache_stats()``
# (imported by main.py / admin.py).  Without the alias that function shadows
# this telemetry module at module scope, so ``cache_stats.miss`` resolves to
# the function → AttributeError on every cold render (MIDI/SID/MOD/GME).
from soniqboom.core import cache_stats as _cstats
from soniqboom.core import unplayable as _unplayable

log = logging.getLogger(__name__)

# ── Thundering-herd prevention ──────────────────────────────────────────────
_inflight: dict[str, asyncio.Event] = {}
# Per-key asyncio.Lock so the get_cached → _inflight check → render → store
# sequence is atomic for any one cache key.  Without this lock two coroutines
# (e.g. a live stream + a prewarm) arriving cold for the same key both saw
# ``None`` from get_cached, both saw the key missing from _inflight, both
# registered their own event, both ran ffmpeg, and both ``os.replace`` raced
# onto the same destination — double-counting ``_total_bytes`` and corrupting
# a FileResponse that was mid-reading the older write.
#
# ``WeakValueDictionary`` so an entry disappears automatically once no caller
# still holds the lock — without this the dict grew once per distinct cache
# key for the lifetime of the process (Perf #7).
_keyed_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
    weakref.WeakValueDictionary()
)
_keyed_locks_lock = threading.Lock()


def _lock_for(key: str) -> asyncio.Lock:
    """Get (or lazily create) the asyncio.Lock for ``key``.  Mutex-protected
    creation so two coroutines colliding here don't end up with two locks.

    Callers must keep a local reference to the returned lock for the duration
    of their critical section — the underlying dict is weak-valued, so
    dropping the reference is what allows GC to reclaim the entry.
    """
    with _keyed_locks_lock:
        lock = _keyed_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _keyed_locks[key] = lock
    return lock

# ── Who still wants a render ────────────────────────────────────────────────
# Every caller waiting for a render's result, by cache key: a callable each
# that says whether that caller still wants it.  ``get_or_render`` registers
# its caller for as long as it waits — wanted throughout unless the caller's
# context says otherwise (``render_interest``: a prewarm that is retired stops
# wanting its render; ``NO_INTEREST``: bookkeeping that only follows a render
# someone else asked for).  A render no caller wants (``wanted`` False) and
# no listener reads may be stopped to free its render slot
# (``stream._preempt_abandoned_render``); its waiters then see it cancelled.
render_interest: "contextvars.ContextVar" = contextvars.ContextVar(
    "soniqboom_render_interest", default=None)
_interest: "dict[str, dict[int, object]]" = {}
_interest_seq = itertools.count()
# The cache key the render running in this task fills: set inside
# ``get_or_render``'s detached render task, so a live render can tell that
# cancelling the task it runs in stops exactly that render.
rendering_key: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "soniqboom_rendering_key", default=None)
# A list a caller sets to learn which entries its ``get_or_render`` calls
# produced: ``(key, format_type, path)`` appended per call (the next-track
# prewarm: the render a transcode is then made from).
render_results: "contextvars.ContextVar[list | None]" = contextvars.ContextVar(
    "soniqboom_render_results", default=None)


def _note_result(key: str, format_type: str, path: Path) -> None:
    out = render_results.get()
    if out is not None:
        out.append((key, format_type, path))


def _always_wanted() -> bool:
    return True


def NO_INTEREST() -> bool:       # noqa: N802 — a constant callable
    return False


class RenderStopped(RuntimeError):
    """The render a caller that no longer wants it was waiting for was
    stopped (``get_or_render`` doesn't render it again for such a caller)."""


def register_interest(key: str, cb=None) -> int:
    """Count a waiter on ``key``'s render (``cb``: whether it still wants it;
    default the context's ``render_interest``, else always).  Returns the
    token for ``drop_interest``."""
    if cb is None:
        cb = render_interest.get() or _always_wanted
    tok = next(_interest_seq)
    _interest.setdefault(key, {})[tok] = cb
    return tok


# Called (on the loop) when a caller stops waiting for a render — the stream
# module's ``_poke_slot_waiters``: a play waiting for a slot looks again.
on_interest_dropped = None


def drop_interest(key: str, tok: int) -> None:
    waiters = _interest.get(key)
    if waiters is not None:
        waiters.pop(tok, None)
        if not waiters:
            _interest.pop(key, None)
    hook = on_interest_dropped
    if hook is not None:
        try:
            hook()
        except Exception:                               # noqa: BLE001
            pass


@contextlib.contextmanager
def interest(key: str, cb=None):
    """``register_interest`` for the duration of a ``with`` block."""
    tok = register_interest(key, cb)
    try:
        yield
    finally:
        drop_interest(key, tok)


class PrewarmWant:
    """A prewarm's ``render_interest``: it wants its render until retired
    (cancelled, expired, or its render was stopped for a play — ``retire``).
    A play that needs a render slot doesn't count it (``wanted(by_play=True)``):
    speculative work never keeps a slot from a listener."""

    prewarm = True
    __slots__ = ("alive",)

    def __init__(self) -> None:
        self.alive = True

    def __call__(self) -> bool:
        return self.alive

    def retire(self) -> None:
        self.alive = False


def wanted(key: str, *, by_play: bool = False) -> bool:
    """Does any caller still want ``key``'s render?  ``by_play``: as a play
    waiting for a slot sees it — prewarms (``PrewarmWant``) don't count."""
    for cb in list((_interest.get(key) or {}).values()):
        if by_play and getattr(cb, "prewarm", False):
            continue
        try:
            if cb():
                return True
        except Exception:                               # noqa: BLE001
            return True          # can't tell: keep it
    return False


def retire_prewarm_wants(key: str) -> int:
    """Retire every prewarm waiting for ``key``'s render (its render was
    stopped for a play): none of them renders it again.  Returns how many."""
    n = 0
    for cb in list((_interest.get(key) or {}).values()):
        if getattr(cb, "prewarm", False) and callable(getattr(cb, "retire", None)):
            cb.retire()
            n += 1
    return n


# ── Eviction-pin set — keys protected from LRU eviction ─────────────────────
# Populated/cleared by the stream endpoint at playback start/end so a
# speculative N+2 prewarm can't evict the currently-playing track and force
# a re-render under the user's nose.  Pins are advisory: the eviction loop
# skips pinned keys when picking victims, but if every entry is pinned and
# we're over budget we still evict the oldest (correctness wins over polish).
#
# Pins are *reference-counted* — every ``pin(key)`` call increments a counter
# and every matching ``unpin(key)`` decrements it.  The key only loses its
# pinned status once the count reaches zero.  This replaces the previous
# FIFO-ageing model which could silently age out a still-playing track when
# enough new prewarms arrived (Perf #9): an active streaming response now
# always wins over LRU/eviction regardless of how many concurrent readers
# arrive afterwards.  Pending purges (``_purge_entry``) are deferred while
# the count is non-zero so an in-flight ``FileResponse`` can't race against
# an ``unlink`` of the file it's still reading from disk.
_pin_refs: dict[str, int] = {}
# Cache keys whose unlink is deferred until the refcount hits zero.  Maps
# ``key → on-disk path`` so ``unpin`` can perform the unlink without
# needing the now-removed ``_meta`` entry.
_pending_purge: dict[str, str] = {}

# ── In-memory LRU state ────────────────────────────────────────────────────
# key → {path, size_bytes, format_type, created_at} — ``path`` is always the
# entry's WAV path (``<key>.wav``), whether or not that file exists now;
# ``size_bytes`` is what the entry holds on disk.  A packed entry also has
# ``packed`` (True: ``<key>.sbwv`` holds it), ``packed_bytes``, ``hot``
# (its decoded WAV exists), ``wav_bytes`` (the WAV's size, None until the
# pack's header was read) and ``wav_mtime_ns``; ``stale`` marks an entry a
# new store is replacing (a pack or decode of it in progress is dropped).
_meta: dict[str, dict] = {}

# Expose live entry count to the cache-stats telemetry (cascade viz).
_cstats.register_size("conversion", lambda: len(_meta))
# OrderedDict gives us O(1) eviction (popitem from the front) and O(1)
# touch-on-access (move_to_end) instead of the previous ``min(_lru, key=...)``
# which scanned every entry per eviction.
_lru: "OrderedDict[str, float]" = OrderedDict()
_total_bytes: int = 0
# Mutex covering _meta / _lru / _total_bytes.  RLock so nested acquisition
# is safe — e.g. ``purge_sid_entries_for`` snapshots victim keys under the
# lock, then calls ``_purge_entry`` (which also acquires the lock) in a
# loop.  Worker-thread paths (``_maybe_evict``, ``_purge_entry`` via
# ``asyncio.to_thread``) and loop-thread paths (``find_shorter_sid_entry``)
# both honour it.
_state_lock = threading.RLock()


# ── Key / path helpers ──────────────────────────────────────────────────────

# Render revision per format type: bumped when that renderer's output for
# the same tune changes, so older cached renders are left to eviction.
# gme 2: libgme tunes that state a length play it (+ the fade) instead of the
# 180 s default (GitHub #38); 3: SAP tunes play their TIME (+ the fade only
# when marked LOOP — ``gme_render.tune_fade``) — every libgme render is swept
# once (re-rendered on its next play).
# tracker 2: our own float WAV with libopenmpt's end fade, and modules ending
# in a song-end loop stop where it first repeats (a 65:56 render of a 5:02
# tune was played — and its length written back — while it stayed cached);
# rev 1 was a 16-bit 44.1 kHz render that never shipped; 3: each subsong of a
# module with several is a tune of its own — tune 0 (``sub0``) is its first
# subsong, where it was every subsong played one after another.
# adlib / imf 1: rendered at the OPL chip's own 49,716 Hz and resampled
# (``stream._adlib_cmd``), not interpolated to 44.1 kHz by the emulator; 2:
# without the header sizes adplay writes after the audio into a pipe (a click
# at the end — ``stream.ADPLAY_TAIL_FILTER``).
# uade 1: modules uade only plays staged (``xpk.stage_for_uade``: PowerPacker
# unpacked, an Atari ST ``.soc`` for the Hippel ST player, a YM-ST replay
# routine, a sample folder, a second player) — an older render of one played
# the wrong player (a silent ``.soc``) or nothing usable; 2: Hippel COSO songs
# played with their shared instrument file (``rip_repairs.coso_with_bank``) —
# an older render played whatever followed the song as its instruments.
_RENDER_REVS = {"sndh": 2, "gme": 3, "psf": 1, "tracker": 3, "adlib": 2, "imf": 2,
                "uade": 2}


def _cache_key(
    track_id: str,
    format_type: str,
    subsong: int = 0,
    soundfont_path: str | None = None,
    duration: int | None = None,
    codec: str | None = None,
    target_rate: int | None = None,
    bitrate: int | None = None,
    variant: str | None = None,
    tuned: bool = False,
) -> str:
    """Build the cache key.  ``duration`` overrides the SID default so a
    HVSC-supplied per-tune length yields a distinct key — without this
    override, a SID rendered once at the 5 min default would forever
    serve from cache even after HVSC delivered the real 3:27.

    For the ``transcoded`` format type, ``codec`` is the output codec
    (typically ``settings.transcode_format`` — flac/mp3/ogg) and
    ``target_rate`` is an optional resample target.  Distinct codec or
    rate yields distinct entries so a user changing transcode_format from
    flac to mp3 doesn't accidentally serve a stale flac WAV.  ``bitrate``
    (a client's bitrate cap, kbps) and ``variant`` (a tag for a transcode of
    a RENDERED WAV — which subsong / length it came from) also yield
    distinct entries; both append nothing when unset, so existing keys are
    unchanged.  For the subsong-carrying render types ``variant`` marks
    another render of the same tune index (uade's subsong base).  ``tuned``
    (``psf`` only): a multi-tune file the PSF route plays tune by tune
    (GoatTracker, FamiTracker — ``stream._psf_tuned``) is keyed per tune,
    ``sub{N}`` as the subsong-carrying types are."""
    parts = [track_id]
    if format_type == "sid":
        parts.append(f"sub{subsong}")
        dur = int(duration if duration is not None else settings.sid_default_duration)
        parts.append(f"dur{dur}")
        # SID fidelity settings shape the rendered audio — key on any
        # NON-default value so changing the chip model / filter doesn't
        # serve stale WAVs.  At defaults NO parts are appended, keeping
        # every pre-existing cache entry valid.
        _model = (getattr(settings, "sid_model", "auto") or "auto").lower()
        if _model in ("6581", "8580"):
            parts.append(
                f"m{_model}{'f' if getattr(settings, 'sid_model_force', False) else ''}")
        if not getattr(settings, "sid_filter", True):
            parts.append("nf")
        _curve = float(getattr(settings, "sid_filter_curve", -1.0))
        if 0.0 <= _curve <= 1.0:
            parts.append(f"fc{_curve:g}")
        if getattr(settings, "sid_digiboost", False):
            parts.append("db")
        # A fade over the end of the length (``render_options.sid_fade_s``),
        # and a tune for two or three SID chips rendered in stereo
        # (``variant`` "st", ``stream._sid_key_variant``): other renders than
        # the mono, unfaded one the key named before 2.3.0.
        parts.extend(_render_options.sid_key_parts())
        if variant:
            parts.append(variant)
    elif format_type == "midi":
        sf_hash = hashlib.sha256((soundfont_path or "").encode()).hexdigest()[:8]
        parts.append(f"sf{sf_hash}")
    elif format_type in ("tracker", "uade", "hvl", "sndh", "sc68", "gme",
                         "adlib", "imf", "ym"):
        # All carry sub-songs (HVL/AHX/SNDH/SC68 especially) — without
        # ``sub{N}`` in the key, different subsongs of the same module
        # collide and the first render wins (QA 2026-07-02: sndh/sc68
        # originally fell through to the bare-track_id key and served ONE
        # cached render for every subsong).
        # NOTE: "gme" already keyed sub{N} before (identical output — no
        # invalidation); "adlib"/"imf" previously used the BARE track_id, so
        # Westwood .adl / id-IMF subsongs collided the same way — including
        # them here fixes that at the cost of a one-time lazy re-render of
        # existing AdLib cache entries.
        parts.append(f"sub{subsong}")
        if format_type in _RENDER_REVS:
            # Renders made before a renderer change are not reused: SNDH and
            # libgme tunes played every tune at the 180 s default before they
            # honoured the length the file states (GitHub #38).
            parts.append(f"r{_RENDER_REVS[format_type]}")
        if variant:
            # A distinct render of the same tune index — uade modules whose
            # subsongs are numbered from 1 (``b1``): an older key for that
            # index holds the wrong tune.  Unset for everything else, so
            # existing keys are unchanged.
            parts.append(f"v{variant}")
        # Render options in force (``core/render_options``): a tracker's Amiga
        # resampler and loop count / fade, a libgme / kssrender tune's loop
        # count / fade.  Nothing at "off" / the default endings.
        if format_type == "tracker":
            parts.extend(_render_options.tracker_key_parts())
        elif format_type == "gme":
            parts.extend(_render_options.chip_key_parts())
        elif format_type == "uade":
            # Paula's stereo since 2.3.0 (``render_options.UADE_STEREO_ARGS``)
            parts.append(_render_options.UADE_KEY_PART)
    elif format_type in _RENDER_REVS:
        # A render without subsongs (PSF: one tune per rip) under a revision:
        # PSF rips stopped at their length tag before their fade was played.
        if tuned:
            parts.append(f"sub{subsong}")
        parts.append(f"r{_RENDER_REVS[format_type]}")
        if variant:
            # Another renderer's render of the rip (a Dreamcast DSF through
            # dsfrender — ``core/dsf_render``); unset, keys are unchanged.
            parts.append(f"v{variant}")
    elif format_type == "transcoded":
        parts.append(f"c{codec or 'flac'}")
        # ar=0 means "preserve source sample rate" — distinct from a literal
        # 96 kHz target (used for DSD) so we never reuse a DSD-downsampled
        # 96 kHz FLAC when serving a native-rate ALAC source.
        parts.append(f"ar{int(target_rate or 0)}")
        if bitrate:
            parts.append(f"br{int(bitrate)}")
        if variant:
            parts.append(f"v{variant}")
    return "__".join(parts)


def _cache_path(cache_key: str, format_type: str) -> Path:
    base = get_conversion_cache_dir() / format_type
    shard = cache_key[:2]
    return base / shard / f"{cache_key}.wav"


def sid_warm_eligible() -> bool:
    """True only when ALL SID fidelity settings are at their defaults.

    This is the exact condition under which ``_cache_key`` appends NO fidelity
    suffix for ``format_type == "sid"`` — and it is also the only case where a
    browser (WASM libsidplayfp/reSIDfp) render can MATCH the server render,
    because the WASM core has no chip-model / filter / curve / digiboost setter.
    Used to gate client SID audio cache-warming (``POST /tracks/{id}/sid-audio``):
    non-default fidelity → warming is refused so a client can never poison a
    slot with audio the server would have rendered differently."""
    _model = (getattr(settings, "sid_model", "auto") or "auto").lower()
    if _model in ("6581", "8580"):
        return False
    if not getattr(settings, "sid_filter", True):
        return False
    _curve = float(getattr(settings, "sid_filter_curve", -1.0))
    if 0.0 <= _curve <= 1.0:
        return False
    if getattr(settings, "sid_digiboost", False):
        return False
    if _render_options.sid_key_parts():
        return False           # (the WASM core renders no fade)
    return True


def _find_orphan_sidecar(track_id: str, subsong: int,
                         uade_variant: str | None = None,
                         sid_voices: int = 0) -> Path | None:
    """Find a VU sidecar whose WAV is absent — evicted, never cached, or written
    ahead of the audio.

    ``get_vu_sidecar_path`` scans ``_meta``, so it can only ever see sidecars
    whose audio is still cached.  Sidecars now outlive their WAV (eviction keeps
    them) and can even predate one (the VU pass runs from the source file), so a
    ``_meta`` miss must fall back to the deterministic on-disk layout:
    ``<fmt>/<shard>/<track_id>__sub<N>[__…].vu``.

    Matching mirrors ``get_vu_sidecar_path``: tracker/uade keys are exactly
    ``<id>__sub<N>``; SID keys carry a ``__dur<D>[+fidelity]`` tail, so the
    trailing ``__`` in the glob keeps ``sub1`` from matching ``sub10``.
    """
    base = get_conversion_cache_dir()
    exact = f"{track_id}__sub{int(subsong)}"
    shard = track_id[:2]
    if sid_voices:
        # A C64 SID tune: only its own render keys (``sid/<shard>/<id>__sub<N>__…``),
        # and only a sidecar that is its meter as it plays now.
        try:
            for cand in sorted((base / "sid" / shard).glob(f"{exact}__*.vu")):
                if _sid_sidecar_fits(cand.stem, cand, sid_voices):
                    return cand
        except OSError:
            pass
        return None
    # A tracker render's own key carries its render revision (``_RENDER_REVS``).
    p = base / "tracker" / shard / f"{_cache_key(track_id, 'tracker', int(subsong))}.vu"
    if not uade_variant and p.exists():
        return p
    # A uade render's key carries its render revision too (and its variant):
    # only that key's sidecar is this render's — the plain ``__sub<N>`` one (or
    # the glob below) could be another tune's, or an older render's.
    p = base / "uade" / shard / (
        f"{_cache_key(track_id, 'uade', int(subsong), variant=uade_variant)}.vu")
    if uade_variant or p.exists():
        return p if p.exists() else None
    for fmt in FORMAT_TYPES:
        if fmt == "uade":
            continue
        d = base / fmt / shard
        if not d.is_dir():
            continue
        p = d / f"{exact}.vu"                      # tracker (older keys)
        if p.exists():
            return p
        try:
            for cand in sorted(d.glob(f"{exact}__*.vu")):   # sid
                return cand
        except OSError:
            continue
    return None


def _vumr_channels(path: Path) -> int:
    """The voice count a VUMR sidecar's header states (byte 5), 0 when the
    file can't be read or isn't one.  Blocking (6 bytes)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(6)
    except OSError:
        return 0
    return head[5] if len(head) == 6 and head[:4] == b"VUMR" else 0


def _sid_sidecar_fits(cache_key: str, sidecar: Path, sid_voices: int) -> bool:
    """Is ``sidecar`` (of the SID render ``cache_key``) the meter of the tune
    as it plays now: ``sid_voices`` voices (3 per SID chip)?

    A 2SID / 3SID tune renders in stereo under its own key (variant "st",
    ``stream._sid_key_variant``) since 2.3.0, and its meter has 6 / 9 voices.
    The same ``<id>__sub<N>__`` prefix also names the mono render keyed before
    — whose 3-voice sidecar (a browser upload, an older server pass) is another
    render's, never this tune's meter: skipped, and left to its key.  A sidecar
    under the tune's OWN key that still has the wrong count (a pass that ran
    for one chip only — the packed-cache-hit path before 2.3.1) is removed:
    the voice pass writes a sidecar only where none exists, so leaving it
    would keep the 3-voice meter for good."""
    stereo_key = "st" in cache_key.split("__")[2:]
    if stereo_key != (sid_voices > 3):
        return False
    have = _vumr_channels(sidecar)
    if have == sid_voices:
        return True
    if not have:
        return False                    # (unreadable just now, or being replaced: left alone)
    try:
        sidecar.unlink()
        log.info("VU sidecar %s has the wrong voice count for its tune (%d wanted) — removed",
                 sidecar.name, sid_voices)
    except OSError:
        pass
    return False


def get_vu_sidecar_path(track_id: str, subsong: int = 0,
                        uade_variant: str | None = None,
                        sid_voices: int = 0) -> Path | None:
    """Return the on-disk path to the VU sidecar for *track_id* at
    *subsong* (0-based wire index), or None if that subsong hasn't been
    rendered yet.

    The tracker/uade cache key is exactly ``"<track_id>__sub<N>"`` plus, for
    a tracker, its render revision (see ``_cache_key``), so we match by
    EQUALITY on the whole key.  Equality (not ``startswith``)
    is deliberate: a prefix match on ``sub1`` would also catch ``sub10``,
    ``sub12`` … and a subsong-agnostic match would serve tune 0's meters
    while tune 3 plays.  Multi-subsong UADE files (AHX/HVL/TFMX) and the
    rare multi-song tracker each get their own per-tune sidecar, written
    during that subsong's render (stream.py ``_extract_vu_sidecar``).

    A miss (subsong never rendered) returns None → the endpoint attempts a
    lazy backfill for that subsong, else 404 → the frontend falls back to
    its FFT-spectrum visualiser.

    ``uade_variant`` (a uade module whose tunes are numbered from 1, see
    ``_cache_key``): the uade key is ``<track_id>__sub<N>__v<variant>``.

    ``sid_voices`` (a C64 SID tune: 3 per SID chip): only a SID sidecar with
    that many voices, of a render keyed for the tune as it plays now, is its
    meter (``_sid_sidecar_fits``).  0 — any sidecar of the tune, as before.
    """
    want = f"{track_id}__sub{int(subsong)}"
    want_sid_prefix = want + "__"      # SID keys append __dur{D}[+fidelity parts]
    want_uade = _cache_key(track_id, "uade", int(subsong), variant=uade_variant)
    want_tracker = _cache_key(track_id, "tracker", int(subsong))
    sid_found: "list[tuple[str, Path]]" = []
    with _state_lock:
        for cache_key, entry in _meta.items():
            fmt = entry.get("format_type")
            if sid_voices and fmt != "sid":
                continue
            if fmt in ("tracker", "uade"):
                # tracker renders get VU from libopenmpt; uade renders from the
                # Paula --write-audio dump (uade_vu.py).  Their keys are exactly
                # "<track_id>__sub<N>" (uade: plus the variant; tracker: plus
                # its render revision) → equality.
                if cache_key != (want_uade if fmt == "uade" else want_tracker):
                    continue
            elif fmt == "sid":
                # SID renders get VU from 3 sidplayfp voice-isolation passes
                # (sid_vu.py).  Their key carries a "__dur<D>[+fidelity]" suffix,
                # so match on the "<track_id>__sub<N>__" prefix — the trailing
                # "__" keeps sub1 from also matching sub10/sub12.
                if not cache_key.startswith(want_sid_prefix):
                    continue
            elif fmt in ("gme", "adlib", "imf"):
                # libgme / AdLib renders get VU from api/voices.py (their
                # libgme render's key only — a KSS kssrender played is not).
                if cache_key != _cache_key(track_id, fmt, int(subsong)):
                    continue
            else:
                continue
            wav_path = Path(entry["path"])
            sidecar  = wav_path.with_suffix(".vu")
            if sid_voices:
                sid_found.append((cache_key, sidecar))   # (checked below, off the lock)
                continue
            if sidecar.exists():
                return sidecar
    for cache_key, sidecar in sid_found:
        if sidecar.exists() and _sid_sidecar_fits(cache_key, sidecar, sid_voices):
            return sidecar
    # No cached WAV to hang the sidecar off — but one may still exist on disk:
    # eviction now KEEPS sidecars, and the VU pass can write one before (or
    # without) the audio ever being cached.  Look it up by its deterministic path.
    return _find_orphan_sidecar(track_id, subsong, uade_variant, sid_voices)


def get_sid_wav_path_for_upload(track_id: str, subsong: int = 0) -> Path | None:
    """Return the cached SID WAV anchor for *track_id*/*subsong* whose ``.vu``
    a client may write, or None when no SID WAV is cached yet.

    Companion to :func:`get_vu_sidecar_path` for the client-VU upload path
    (``POST /api/tracks/{id}/vu``): identical ``sid``-format prefix scan, but
    returns the anchor WAV *without* requiring the sidecar to already exist —
    the caller derives ``wav_path.with_suffix(".vu")`` as the write target.

    Restricting to ``format_type == "sid"`` entries is the security gate: an
    Amiga SidMon ``*.sid`` renders under ``uade`` (never ``sid``), so an upload
    can only ever land beside a genuine C64-SID render, never poison another
    format's slot.  Returns None (→ caller replies 425) when the WAV isn't
    cached, so a client can't create arbitrary files.
    """
    want_sid_prefix = f"{track_id}__sub{int(subsong)}__"
    with _state_lock:
        for cache_key, entry in _meta.items():
            if entry.get("format_type") != "sid":
                continue
            if not cache_key.startswith(want_sid_prefix):
                continue
            return Path(entry["path"])
    return None


# A copy into the cache in progress (``_move_into_place``): never adopted, and
# removed by ``warmup_from_disk`` (a process that died part-way left it).
_COPY_TMP_SUFFIX = ".copying"


def _move_into_place(src: Path, dest: Path) -> None:
    """Move ``src`` to ``dest`` so that ``dest`` only ever names a complete
    file: a rename when they share a filesystem; otherwise (the temp folder on
    another volume — the Docker layout) a copy into a temp name in ``dest``'s
    folder, fsync'd, then renamed over ``dest`` (the folder fsync'd too), and
    ``src`` removed.  A process that ends part-way leaves the temp copy, never
    a truncated ``dest``."""
    try:
        os.replace(str(src), str(dest))
        return
    except OSError:
        pass
    import secrets
    from soniqboom.core import durable
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{secrets.token_hex(4)}{_COPY_TMP_SUFFIX}")
    try:
        shutil.copyfile(str(src), str(tmp))
        fd = os.open(str(tmp), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(dest))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    durable.fsync_dir(dest.parent)
    try:
        src.unlink()
    except OSError:
        pass


def _wav_data_chunk(head: bytes, pad: bool) -> "tuple[int, int] | None":
    """``(offset of the audio, declared size)`` of the ``data`` chunk in the
    RIFF header bytes ``head``, chunks walked with (``pad``) or without the
    pad byte after an odd-sized one; None when not found in ``head``."""
    pos = 12
    while pos + 8 <= len(head):
        cid = head[pos:pos + 4]
        clen = int.from_bytes(head[pos + 4:pos + 8], "little")
        if cid == b"data":
            return pos + 8, clen
        pos += 8 + clen + (clen & 1 if pad else 0)
    return None


def _wav_is_whole(path: Path, size: int) -> bool:
    """False for an empty file and for a RIFF/WAVE file whose ``data`` chunk
    declares more audio than the file holds (a copy or write cut off
    part-way).  A ``data`` size of ``0xFFFFFFFF`` (written while streaming),
    a file that is not a RIFF/WAVE (a cached transcode in another codec) and
    one whose ``data`` chunk isn't in its first 4 KB can't be checked: True.
    The RIFF size itself isn't trusted (some writers get it wrong)."""
    if size <= 0:
        return False
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    if head[:4] != b"RIFF":
        return True
    if len(head) < 12:
        return False
    if head[8:12] != b"WAVE":
        return True
    found = _wav_data_chunk(head, True) or _wav_data_chunk(head, False)
    if found is None:
        # The whole file was read and holds no audio chunk → cut off; else a
        # big chunk comes first (cover art) — not told without reading on.
        return size > len(head)
    off, dlen = found
    return dlen == 0xFFFFFFFF or off + dlen <= size


# ── Packed entries (WavPack, cache entry format v1) ─────────────────────────
# See the module docstring.  ``<key>.sbwv`` = header (``_PACK_HEAD`` + its
# CRC-32) + the WAV's bytes before its audio + its bytes after it (none for a
# render: only WAVs whose ``data`` chunk ends the file are packed) + the
# WavPack stream of the audio.  Any change to that layout takes a new
# ``PACK_VERSION``: a reader drops a file of a version it doesn't know.
PACK_SUFFIX = ".sbwv"
PACK_VERSION = 1
_PACK_MAGIC = b"SBWV"
# magic, version, header length, offset of the WavPack stream, WAV size, data
# offset (the WAV header bytes kept), data length, trailer length, sample
# format code (``_RAW_FORMATS``), reserved, channels, rate, MD5 of the audio
# bytes, the WAV's mtime (ns) — a decoded copy is given it back.
_PACK_HEAD = struct.Struct("<4sHHIQIQIBBHI16sq")
_PACK_HEAD_LEN = _PACK_HEAD.size + 4
_PACK_TMP_SUFFIX = ".packing"
_UNPACK_TMP_SUFFIX = ".unpacking"
_RAW_FORMATS = {1: "u8", 2: "s16le", 3: "s24le", 4: "s32le", 5: "f32le"}
_RAW_CODES = {v: k for k, v in _RAW_FORMATS.items()}
_RAW_BYTES = {"u8": 1, "s16le": 2, "s24le": 3, "s32le": 4, "f32le": 4}
# Cached transcodes (FLAC / MP3 / Ogg, the in-flight WAVs of real recordings)
# are not packed: only renders.
_UNPACKED_TYPES = frozenset({"transcoded"})
# A render is packed no sooner than this after it was stored (its first
# minutes of reads — seeks, VU, waveform, transcodes — find it untouched).
_PACK_DELAY_S = 30.0
# ffmpeg's wavpack ``compression_level``: 1 is WavPack's default mode — level
# 3 ("very high") saved 2 % more on 22 real renders, encoded 5-10× slower and
# decoded no faster.
_PACK_LEVEL = 1
# Under pressure, decoded copies of packed entries go first — except the
# newest ones up to this fraction of the budget, and any used in the last
# ``_HOT_RECENT_S`` (a reader may be about to open the path it was given).
_HOT_FLOOR_FRACTION = 0.10
_HOT_RECENT_S = 120.0
_PACK_TIMEOUT_S = 900.0
# How often a running pack asks whether a render started (``_run_bg``).
_PREEMPT_POLL_S = 0.025
_UNPACKABLE_MAX = 100_000

# Called by the packer to ask whether a render is running (``api.stream``
# sets it: a foreground or background render slot in use) — a pack waits,
# and one running is stopped, while it says True.
renders_busy = None
# Called (blocking) with a float render's WAV path before it is packed —
# ``api.stream`` sets ``render_peak``, whose ``<wav>.peak`` sidecar must be
# measured while the WAV is there.
pack_prepare_hook = None


class PackInfo:
    """A pack's header (``read_pack_info``)."""

    __slots__ = ("wv_offset", "wav_size", "data_off", "data_len", "raw", "channels",
                 "rate", "md5", "mtime_ns", "prefix", "suffix")

    def __init__(self, *, wv_offset: int, wav_size: int, data_off: int, data_len: int,
                 raw: str, channels: int, rate: int, md5: str, mtime_ns: int,
                 prefix: bytes = b"", suffix: bytes = b"") -> None:
        self.wv_offset, self.wav_size = int(wv_offset), int(wav_size)
        self.data_off, self.data_len = int(data_off), int(data_len)
        self.raw, self.channels, self.rate = raw, int(channels), int(rate)
        self.md5, self.mtime_ns = md5, int(mtime_ns)
        self.prefix, self.suffix = prefix, suffix


class _PackError(RuntimeError):
    pass


class _PackTimeout(_PackError):
    """A pack step ran past ``_PACK_TIMEOUT_S`` (the entry is not tried again
    this run)."""


def _pack_header(info: PackInfo) -> bytes:
    fields = _PACK_HEAD.pack(
        _PACK_MAGIC, PACK_VERSION, _PACK_HEAD_LEN, info.wv_offset, info.wav_size,
        info.data_off, info.data_len, len(info.suffix), _RAW_CODES[info.raw], 0,
        info.channels, info.rate, bytes.fromhex(info.md5) if info.md5 else b"\0" * 16,
        info.mtime_ns)
    return fields + zlib.crc32(fields).to_bytes(4, "little")


def read_pack_info(path: "Path | str") -> "PackInfo | None":
    """The header of the pack at ``path`` with the WAV bytes it keeps, or
    None when it is missing, damaged (short, a CRC mismatch) or of another
    ``PACK_VERSION`` — what a reader drops.  Any other read error (a share
    that didn't answer) raises ``OSError``: the pack may be fine (blocking)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_PACK_HEAD_LEN)
            if len(head) < _PACK_HEAD_LEN:
                return None
            fields = head[:_PACK_HEAD.size]
            if zlib.crc32(fields) != int.from_bytes(head[_PACK_HEAD.size:], "little"):
                return None
            (magic, version, head_len, wv_off, wav_size, data_off, data_len, suffix_len,
             code, _res, channels, rate, md5, mtime_ns) = _PACK_HEAD.unpack(fields)
            raw = _RAW_FORMATS.get(code)
            if (magic != _PACK_MAGIC or version != PACK_VERSION or head_len != _PACK_HEAD_LEN
                    or raw is None or channels < 1 or rate < 1
                    or data_off + data_len + suffix_len != wav_size
                    or wv_off != head_len + data_off + suffix_len):
                return None
            prefix = fh.read(data_off)
            suffix = fh.read(suffix_len)
            if len(prefix) != data_off or len(suffix) != suffix_len:
                return None
    except FileNotFoundError:
        return None
    return PackInfo(wv_offset=wv_off, wav_size=wav_size, data_off=data_off,
                    data_len=data_len, raw=raw, channels=channels, rate=rate,
                    md5=md5.hex(), mtime_ns=mtime_ns, prefix=prefix, suffix=suffix)


def _pack_layout(path: Path) -> "tuple[int, int, str, int, int] | None":
    """``(data offset, data length, raw format, channels, rate)`` of a WAV
    whose audio a pack stores bit for bit — integer PCM of 8/16/24/32 bits or
    32-bit float, its ``data`` chunk ending the file exactly (no trailer, no
    streaming size) — else None: it stays a plain WAV.  Blocking."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos, fmt = 12, None
    while pos + 8 <= len(head):
        cid = head[pos:pos + 4]
        clen = int.from_bytes(head[pos + 4:pos + 8], "little")
        body = pos + 8
        if cid == b"fmt " and clen >= 16 and body + 16 <= len(head):
            tag, ch, rate, _bps, align, bits = struct.unpack_from("<HHIIHH", head, body)
            if tag == 0xFFFE and clen >= 40 and body + 26 <= len(head):
                tag = int.from_bytes(head[body + 24:body + 26], "little")
            fmt = (tag, ch, rate, align, bits)
        elif cid == b"data":
            if fmt is None:
                return None
            tag, ch, rate, align, bits = fmt
            raw = {(1, 8): "u8", (1, 16): "s16le", (1, 24): "s24le",
                   (1, 32): "s32le", (3, 32): "f32le"}.get((tag, bits))
            if (raw is None or not 1 <= ch <= 8 or not 1000 <= rate <= 768_000
                    or align != ch * bits // 8 or clen <= 0
                    or body + clen != size or clen % align):
                return None
            return body, clen, raw, ch, rate
        pos = body + clen + (clen & 1)
    return None


def _ffmpeg() -> str:
    return settings.ffmpeg_path or "ffmpeg"


_WV_CODEC_OK: "dict[str, bool]" = {}


def wavpack_available() -> bool:
    """Does the ffmpeg in use have what packs need — the wavpack encoder and
    decoder, the ``wv`` muxer and demuxer, the ``md5`` muxer?  Probed once
    per binary (a changed ``ffmpeg_path`` is probed again; blocking)."""
    ff = _ffmpeg()
    got = _WV_CODEC_OK.get(ff)
    if got is None:
        import subprocess
        from soniqboom.core import forksafe
        codec = re.compile(rb"^\s*A\S*\s+wavpack\s", re.M)
        try:
            out = {k: forksafe.run([ff, "-hide_banner", "-" + k], capture_output=True,
                                   timeout=20).stdout or b""
                   for k in ("encoders", "decoders", "muxers", "demuxers")}
            got = (bool(codec.search(out["encoders"])) and bool(codec.search(out["decoders"]))
                   and bool(re.search(rb"^\s*D?E\s+wv\s", out["muxers"], re.M))
                   and bool(re.search(rb"^\s*DE?\s+wv\s", out["demuxers"], re.M))
                   and bool(re.search(rb"^\s*D?E\s+md5\s", out["muxers"], re.M)))
        except (OSError, subprocess.SubprocessError):
            got = False
        if not got:
            log.info("Conversion cache: %s has no wavpack codec — renders stay "
                     "uncompressed", ff)
        _WV_CODEC_OK[ff] = got
    return got


_BG_PREFIX: "list[str] | None" = None


def _background_prefix() -> "list[str]":
    """The command prefix that runs a pack's ffmpeg at the lowest priority:
    macOS ``taskpolicy -b`` (background: efficiency cores, throttled I/O),
    elsewhere ``ionice -c 3`` (idle I/O) and ``nice -n 19`` — each only when
    it runs here.  Probed once (blocking)."""
    global _BG_PREFIX
    if _BG_PREFIX is not None:
        return _BG_PREFIX
    from soniqboom.core import forksafe
    true = shutil.which("true") or "/usr/bin/true"

    def _works(pre: "list[str]") -> bool:
        try:
            return forksafe.run(pre + [true], capture_output=True, timeout=10).returncode == 0
        except Exception:                               # noqa: BLE001
            return False
    pre: "list[str]" = []
    if sys.platform == "darwin" and os.path.exists("/usr/sbin/taskpolicy") \
            and _works(["/usr/sbin/taskpolicy", "-b"]):
        pre = ["/usr/sbin/taskpolicy", "-b"]
    else:
        ion = shutil.which("ionice")
        if ion and _works([ion, "-c", "3"]):
            pre += [ion, "-c", "3"]
        nic = shutil.which("nice")
        if nic and _works([nic, "-n", "19"]):
            pre += [nic, "-n", "19"]
    _BG_PREFIX = pre
    return pre


def _hot(entry: dict) -> bool:
    """Is the entry's WAV on disk?  (Every entry but a packed one whose
    decoded copy was dropped.)"""
    return entry.get("hot", True)


def _wav_size_of(entry: dict) -> int:
    wb = entry.get("wav_bytes")
    if wb is not None:
        return int(wb)
    return 0 if entry.get("packed") else int(entry.get("size_bytes", 0) or 0)


def _entry_size(entry: dict) -> int:
    """The bytes ``entry`` holds on disk: its pack and/or its WAV."""
    n = int(entry.get("packed_bytes") or 0) if entry.get("packed") else 0
    return n + (_wav_size_of(entry) if _hot(entry) else 0)


def _resize_locked(entry: dict) -> None:
    """(``_state_lock`` held) Re-count ``entry`` after its files changed."""
    global _total_bytes
    new = _entry_size(entry)
    _total_bytes = max(0, _total_bytes + new - int(entry.get("size_bytes", 0) or 0))
    entry["size_bytes"] = new


# key → set once the decoded copy dropped for it has been unlinked: a decode
# of that key finishing meanwhile waits for it (``_commit_unpack``), so the
# late unlink never removes the new copy.  Under ``_state_lock``.
_drops_pending: "dict[str, threading.Event]" = {}


def _drop_hot_locked(entry: dict, key: "str | None" = None) -> "Path | None":
    """(``_state_lock`` held) Forget a packed entry's decoded WAV; returns
    the file to unlink outside the lock — then call ``_dropped(key)``.
    Without ``key`` the file is already gone (nothing to unlink)."""
    if not entry.get("packed") or not _hot(entry):
        return None
    entry["hot"] = False
    _resize_locked(entry)
    if key is not None:
        _drops_pending[key] = threading.Event()
    return Path(entry["path"])


def _unlink_dropped(drops: "list[tuple[str, Path]]") -> None:
    """Unlink decoded copies ``_drop_hot_locked`` dropped, each then marked
    done (a decode of its key waiting in ``_commit_unpack`` goes on)."""
    for key, p in drops:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        with _state_lock:
            ev = _drops_pending.pop(key, None)
        if ev is not None:
            ev.set()


def cached_wav_size(cache_key: str) -> "int | None":
    """Size of the WAV a finished entry serves, without touching its LRU
    position or the disk (a packed one's decoded size); None when not cached
    or not known yet (a packed entry whose header the background pass after
    start-up hasn't read)."""
    with _state_lock:
        e = _meta.get(cache_key)
        if not e:
            return None
        n = _wav_size_of(e)
    return n or None


def packed_wav_stat(wav_path: "Path | str") -> "tuple[int, int] | None":
    """``(size, mtime_ns)`` the WAV at ``wav_path`` has when decoded, for a
    packed entry whose decoded copy isn't on disk now — what a sidecar tied to
    the WAV (``<wav>.peak``) is judged against; None otherwise.  A pack whose
    header start-up hasn't read yet is read here (blocking: a thread)."""
    p = Path(wav_path)
    with _state_lock:
        e = _meta.get(p.stem)
        if not e or not e.get("packed") or _hot(e) or e.get("path") != str(p):
            return None
        if e.get("wav_bytes") is not None and e.get("wav_mtime_ns") is not None:
            return int(e["wav_bytes"]), int(e["wav_mtime_ns"])
    try:
        info = read_pack_info(p.with_suffix(PACK_SUFFIX))
    except OSError:
        return None
    if info is None:
        return None
    _note_pack_info(e, info)
    return info.wav_size, info.mtime_ns


def is_packed(cache_key: str) -> bool:
    """Is ``cache_key`` cached as a pack (with or without its decoded copy)?"""
    with _state_lock:
        e = _meta.get(cache_key)
        return bool(e and e.get("packed"))


# ── Decoding a packed entry back to its WAV ─────────────────────────────────

class _Unpack:
    """A packed entry being decoded back to ``<key>.wav`` (``_run_unpack``):
    into ``tmp`` (``ready`` once it holds the WAV's header and ffmpeg runs),
    ``data`` pulsed as it grows, ``done`` at the end — ``ok`` when the entry's
    decoded copy is in place."""

    __slots__ = ("key", "entry", "tmp", "size", "ready", "done", "data", "ok", "live", "task")

    def __init__(self, key: str, entry: dict) -> None:
        self.key, self.entry = key, entry
        self.tmp: "Path | None" = None
        self.size = 0
        self.ok = False
        self.ready, self.done, self.data = asyncio.Event(), asyncio.Event(), asyncio.Event()
        # The ``inflight`` a growing-file response of ``tmp`` reads: a decode
        # that failed part-way ends the body short (never silence).
        self.live = {"no_pad_on_failure": True, "clean_exit": None,
                     "bytes_per_sec": 176_400}
        self.task: "asyncio.Task | None" = None

    def pulse(self) -> None:
        self.data.set()
        self.data.clear()


_unpacks: "dict[str, _Unpack]" = {}
_unpack_tasks: "set[asyncio.Task]" = set()


def _start_unpack(cache_key: str) -> "_Unpack | None":
    """The decode of packed entry ``cache_key`` — the one under way, else a
    new one — or None when the entry isn't a pack without its decoded copy.
    Event loop only."""
    u = _unpacks.get(cache_key)
    if u is not None and not u.done.is_set():
        t = u.task
        try:
            same_loop = t is not None and not t.done() and t.get_loop() is asyncio.get_running_loop()
        except RuntimeError:
            same_loop = False
        if same_loop:
            return u
    with _state_lock:
        entry = _meta.get(cache_key)
        if (entry is None or not entry.get("packed") or _hot(entry)
                or entry.get("stale")):
            return None
    u = _Unpack(cache_key, entry)
    _unpacks[cache_key] = u
    u.task = asyncio.ensure_future(_run_unpack(u))
    _unpack_tasks.add(u.task)
    u.task.add_done_callback(_unpack_tasks.discard)
    return u


def _open_new_file(tmp: Path, head: bytes) -> int:
    """A new file ``tmp`` holding ``head``, open for appending (blocking)."""
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
    try:
        view = memoryview(head)
        while view:
            n = os.write(fd, view)
            view = view[n:]
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    return fd


def _note_pack_info(entry: dict, info: PackInfo) -> None:
    with _state_lock:
        if entry.get("wav_bytes") is None:
            entry["wav_bytes"] = info.wav_size
        if entry.get("wav_mtime_ns") is None:
            entry["wav_mtime_ns"] = info.mtime_ns


def _commit_unpack(u: _Unpack, tmp: Path, dest: Path, info: PackInfo) -> bool:
    """Put the finished decode ``tmp`` in place as the entry's WAV — with the
    original's mtime, so sidecars tied to it stay valid and start-up tells it
    from a later render — unless the entry went or is being replaced
    meanwhile.  A decoded copy of the key dropped a moment ago and not yet
    unlinked is waited for first (its unlink would remove this one).  The
    rename runs under ``_state_lock`` (a same-folder rename): the check that
    no new render replaced the entry and the rename are one step (blocking)."""
    try:
        os.utime(tmp, ns=(time.time_ns(), info.mtime_ns))
    except OSError:
        pass
    ok = False
    deadline = time.monotonic() + 10.0
    while True:
        with _state_lock:
            ev = _drops_pending.get(u.key)
            if ev is None:
                cur = _meta.get(u.key)
                if (cur is u.entry and not cur.get("stale") and cur.get("packed")
                        and not _hot(cur)):
                    try:
                        os.replace(str(tmp), str(dest))
                        ok = True
                    except OSError:
                        ok = False
                    if ok:
                        cur["hot"] = True
                        cur["wav_bytes"] = info.wav_size
                        cur["wav_mtime_ns"] = info.mtime_ns
                        _resize_locked(cur)
                break
        if time.monotonic() > deadline:
            break
        ev.wait(0.2)
    if not ok:
        tmp.unlink(missing_ok=True)
    return ok


async def _run_unpack(u: _Unpack) -> None:
    """Decode ``u``'s pack into a temp file beside its WAV (header bytes
    first, then ffmpeg's output, with block CRCs checked and any error
    fatal), then move it into place.  A damaged pack is dropped with its
    entry (the next play renders the tune again)."""
    from soniqboom.core import forksafe, playback_io
    import subprocess
    dest = Path(u.entry["path"])
    pack = dest.with_suffix(PACK_SUFFIX)
    mk = playback_io.mount_key(dest)
    fd, proc, damaged = -1, None, False
    try:
        info = await playback_io.run(mk, read_pack_info, pack)
        if info is None:
            damaged = True
            raise _PackError("its header is unreadable")
        _note_pack_info(u.entry, info)
        u.size = info.wav_size
        u.live["bytes_per_sec"] = max(1, info.rate * info.channels * _RAW_BYTES[info.raw])
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{secrets.token_hex(4)}"
                             f"{_UNPACK_TMP_SUFFIX}")
        fd = await playback_io.run(mk, _open_new_file, tmp, info.prefix)
        u.tmp = tmp
        proc = await forksafe.spawn(
            _ffmpeg(), "-v", "error", "-nostdin", "-threads", "0",
            "-err_detect", "crccheck", "-xerror",
            "-skip_initial_bytes", str(info.wv_offset), "-f", "wv", "-i", str(pack),
            "-f", info.raw, "-c:a", "pcm_" + info.raw, "pipe:1",
            stdin=subprocess.DEVNULL, stdout=fd, stderr=asyncio.subprocess.PIPE)
        u.ready.set()
        err_f = asyncio.ensure_future(proc.stderr.read())
        wait_f = asyncio.ensure_future(proc.wait())
        seen = len(info.prefix)
        while True:
            finished, _ = await asyncio.wait({wait_f}, timeout=0.01)
            size = (await playback_io.run(mk, os.fstat, fd)).st_size
            if size > seen:
                seen = size
                u.pulse()
            if finished:
                break
        rc = wait_f.result()
        err = await err_f
        if rc != 0:
            damaged = b"Invalid data" in err
            raise _PackError(f"ffmpeg exited {rc}: {err[-300:].decode('utf-8', 'replace')}")
        if info.suffix:
            await playback_io.run(mk, os.write, fd, info.suffix)
        size = (await playback_io.run(mk, os.fstat, fd)).st_size
        if size != info.wav_size:
            damaged = True
            raise _PackError(f"decoded {size} bytes, the WAV had {info.wav_size}")
        fd_, fd = fd, -1
        await playback_io.run(mk, os.close, fd_)
        u.ok = await playback_io.run(mk, _commit_unpack, u, tmp, dest, info)
    except asyncio.CancelledError:
        raise
    except Exception as exc:                            # noqa: BLE001
        # (a read error that isn't damage — a share that didn't answer — keeps
        # the pack: the next hit tries again)
        log.warning("Conversion cache: decoding the packed render %s failed: %s",
                    u.key, exc)
        if damaged:
            await asyncio.to_thread(_purge_entry, u.key, u.entry)
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if not u.ok and u.tmp is not None:
            with contextlib.suppress(OSError):
                u.tmp.unlink(missing_ok=True)
        u.live["clean_exit"] = u.ok
        u.ready.set()
        u.done.set()
        u.pulse()
        if _unpacks.get(u.key) is u:
            _unpacks.pop(u.key, None)
    if u.ok:
        await asyncio.to_thread(_maybe_evict, u.key)


async def unpacking(cache_key: str) -> "_Unpack | None":
    """For a streaming reader of the render ``cache_key``: the decode of its
    pack while it is written (started here; ``tmp`` grows to ``size`` bytes,
    ``data`` pulses, ``done`` ends it, ``live`` is the growing-file
    response's ``inflight``) — or None when the entry isn't a pack without its
    decoded copy, or the decode has already ended (read the cache as usual:
    ``get_cached``).  Counts as a hit."""
    u = _start_unpack(cache_key)
    if u is None:
        return None
    with _state_lock:
        if cache_key in _meta:
            _lru[cache_key] = time.time()
            _lru.move_to_end(cache_key)
    _cstats.hit("conversion")
    await u.ready.wait()
    if u.done.is_set() or u.tmp is None:
        return None
    return u


@contextlib.asynccontextmanager
async def open_cached_audio(cache_key: str):
    """``async with open_cached_audio(key) as wav:`` — the finished render of
    ``key`` as a plain WAV path (a packed entry decoded first), pinned for the
    block (never evicted, its decoded copy never dropped meanwhile); None
    when it isn't cached.  For code that reads a cached render's audio
    (loudness, waveform, VU, a transcode)."""
    hold = PinHold(cache_key)
    try:
        yield await get_cached(cache_key)
    finally:
        hold.release()


# ── Packing ──────────────────────────────────────────────────────────────────

# key → when it may be packed (wall clock): renders stored this run and those
# adopted at start-up still as WAV.  Under ``_state_lock``.
_pack_queue: "OrderedDict[str, float]" = OrderedDict()
# Keys whose WAV can't be packed (another layout, not bit-exact): not tried
# again this run.
_unpackable: "set[str]" = set()
_packer_task: "asyncio.Task | None" = None


def _queue_pack(cache_key: str, delay: float = _PACK_DELAY_S) -> None:
    with _state_lock:
        if cache_key in _unpackable:
            return
        _pack_queue[cache_key] = time.time() + delay
        _pack_queue.move_to_end(cache_key)


def _mark_unpackable(cache_key: str) -> None:
    with _state_lock:
        if len(_unpackable) >= _UNPACKABLE_MAX:
            _unpackable.clear()
        _unpackable.add(cache_key)
        _pack_queue.pop(cache_key, None)


def _busy() -> bool:
    """Is a render (or a decode for a listener) running?  Packs wait."""
    if _inflight or _unpacks:
        return True
    hook = renders_busy
    if hook is not None:
        try:
            return bool(hook())
        except Exception:                               # noqa: BLE001
            return True
    return False


async def _run_bg(cmd: "list[str]", *, stdin, stdout, preempt) -> "tuple[int | None, bytes, bytes]":
    """Run ``cmd`` (a pack step); ``(returncode, stdout, stderr)`` —
    returncode None when ``preempt()`` asked it to stop (checked before it
    starts and every ``_PREEMPT_POLL_S``): it was killed.  Past
    ``_PACK_TIMEOUT_S`` it is killed and ``_PackTimeout`` raised."""
    from soniqboom.core import forksafe
    if preempt is not None and preempt():
        return None, b"", b""                # a render started: not now
    proc = await forksafe.spawn(*cmd, stdin=stdin, stdout=stdout,
                                stderr=asyncio.subprocess.PIPE)
    comm = asyncio.ensure_future(proc.communicate())
    deadline = time.monotonic() + _PACK_TIMEOUT_S
    try:
        while True:
            finished, _ = await asyncio.wait({comm}, timeout=_PREEMPT_POLL_S)
            if finished:
                out, err = comm.result()
                return proc.returncode, out or b"", err or b""
            late = time.monotonic() > deadline
            if late or (preempt is not None and preempt()):
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await comm
                if late:
                    raise _PackTimeout(f"still running after {_PACK_TIMEOUT_S:.0f} s")
                return None, b"", b""
    except BaseException:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
        raise


def _pack_prepare(cache_key: str, wav: Path):
    """What packing ``wav`` needs: ``(layout, stat, WAV header bytes)``;
    "silent" for a silent render; None when it can't be packed (blocking)."""
    lay = _pack_layout(wav)
    if lay is None:
        return None
    try:
        st = wav.stat()
        with open(wav, "rb") as fh:
            prefix = fh.read(lay[0])
    except OSError:
        return None
    if len(prefix) != lay[0]:
        return None
    # A render cached before silent ones were refused may be silence: it is
    # judged here (a packed entry's first hit isn't judged again this run).
    if cache_key not in _judged:
        from soniqboom.core.silence import wav_audible
        try:
            if wav_audible(wav) is False:
                return "silent"
        except Exception:                               # noqa: BLE001
            pass
    if lay[2] == "f32le" and pack_prepare_hook is not None:
        try:
            pack_prepare_hook(wav)
        except Exception:                               # noqa: BLE001
            log.debug("pack: peak of %s not measured", wav, exc_info=True)
    return lay, st, prefix


def _finish_pack_file(tmp: Path, header: bytes) -> int:
    """Write the pack's final header and flush it to disk (the WAV may be
    dropped later on the strength of it; blocking)."""
    fd = os.open(str(tmp), os.O_WRONLY)
    try:
        os.pwrite(fd, header, 0)
        os.fsync(fd)
    finally:
        os.close(fd)
    return tmp.stat().st_size


def _commit_pack(cache_key: str, entry: dict, tmp: Path, wav: Path, st) -> bool:
    """Put the verified pack ``tmp`` in place for ``entry`` — unless the entry
    went, is being replaced, or its WAV changed since it was read.  The file
    work runs outside ``_state_lock`` (only this packer writes ``<key>.sbwv``;
    a store replacing the entry marks it ``stale`` first and removes the old
    pack, so a pack renamed in for a replaced entry is removed here; blocking)."""
    dest = wav.with_suffix(PACK_SUFFIX)
    try:
        now = os.stat(wav)
        if (now.st_size, now.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            return False
        with _state_lock:
            cur = _meta.get(cache_key)
            if cur is not entry or cur.get("stale") or cur.get("packed"):
                return False
        os.replace(str(tmp), str(dest))
        packed_bytes = dest.stat().st_size
    except OSError:
        return False
    with _state_lock:
        cur = _meta.get(cache_key)
        ok = cur is entry and not cur.get("stale") and not cur.get("packed")
        if ok:
            cur.update(packed=True, packed_bytes=packed_bytes, hot=True,
                       wav_bytes=st.st_size, wav_mtime_ns=st.st_mtime_ns)
            _resize_locked(cur)
            if len(_judged) >= _JUDGED_MAX:
                _judged.clear()
            _judged.add(cache_key)
        _pack_queue.pop(cache_key, None)
        replaced = cur is not entry or bool(cur and cur.get("stale"))
    if not ok and replaced:
        with contextlib.suppress(OSError):
            dest.unlink(missing_ok=True)
    return ok


async def _pack(cache_key: str, preempt) -> str:
    """Pack one entry: "packed", "retry" (stopped for a render), "later"
    (a file error — a full disk, say), "nocodec" (this ffmpeg can't) or
    "skip" (nothing to do, or it can't be packed)."""
    with _state_lock:
        entry = _meta.get(cache_key)
        if (entry is None or entry.get("packed") or entry.get("stale")
                or not _hot(entry) or entry.get("format_type") in _UNPACKED_TYPES
                or cache_key in _unpackable):
            _pack_queue.pop(cache_key, None)
            return "skip"
        wav = Path(entry["path"])
    if not getattr(settings, "conversion_cache_pack", True):
        return "skip"
    if not await asyncio.to_thread(wavpack_available):
        return "nocodec"
    prep = await asyncio.to_thread(_pack_prepare, cache_key, wav)
    if prep == "silent":
        await asyncio.to_thread(_purge_entry, cache_key)
        with contextlib.suppress(OSError):
            wav.with_suffix(".vu").unlink(missing_ok=True)
        log.info("Cached render %s is silent — removed, it renders again", cache_key)
        return "skip"
    if prep is None:
        _mark_unpackable(cache_key)
        return "skip"
    (data_off, data_len, raw, channels, rate), st, prefix = prep
    info = PackInfo(wv_offset=_PACK_HEAD_LEN + data_off, wav_size=st.st_size,
                    data_off=data_off, data_len=data_len, raw=raw, channels=channels,
                    rate=rate, md5="", mtime_ns=st.st_mtime_ns, prefix=prefix)
    tmp = wav.with_name(f".{wav.stem}{PACK_SUFFIX}.{os.getpid()}.{secrets.token_hex(4)}"
                        f"{_PACK_TMP_SUFFIX}")
    pre = await asyncio.to_thread(_background_prefix)
    ff = _ffmpeg()
    in_fd = out_fd = -1
    t0 = time.monotonic()
    try:
        out_fd = await asyncio.to_thread(_open_new_file, tmp, _pack_header(info) + prefix)

        def _open_audio() -> int:
            fd = os.open(str(wav), os.O_RDONLY)
            os.lseek(fd, data_off, os.SEEK_SET)
            return fd
        in_fd = await asyncio.to_thread(_open_audio)
        # One read of the audio: the WavPack stream (into the pack after our
        # header) and the MD5 of the audio as read (on stderr).
        rc, _out, err = await _run_bg(
            [*pre, ff, "-v", "error", "-nostdin", "-threads", "1",
             "-f", raw, "-ar", str(rate), "-ac", str(channels), "-i", "pipe:0",
             "-map", "0:a", "-c:a", "wavpack", "-compression_level", str(_PACK_LEVEL),
             "-f", "wv", "pipe:1",
             "-map", "0:a", "-c:a", "pcm_" + raw, "-f", "md5", "pipe:2"],
            stdin=in_fd, stdout=out_fd, preempt=preempt)
        fds, in_fd, out_fd = (in_fd, out_fd), -1, -1
        await asyncio.to_thread(_close_all, fds)
        if rc is None:
            return "retry"
        m = re.search(rb"MD5=([0-9a-f]{32})", err)
        if rc != 0 or m is None:
            raise _PackError(f"encode exited {rc}: {err[-300:].decode('utf-8', 'replace')}")
        want = m.group(1).decode()
        # The proof: the pack decodes (block CRCs checked) to the same audio.
        rc, out, err = await _run_bg(
            [*pre, ff, "-v", "error", "-nostdin", "-threads", "1",
             "-err_detect", "crccheck", "-xerror",
             "-skip_initial_bytes", str(info.wv_offset), "-f", "wv", "-i", str(tmp),
             "-f", "md5", "-c:a", "pcm_" + raw, "pipe:1"],
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            preempt=preempt)
        if rc is None:
            return "retry"
        m = re.search(rb"MD5=([0-9a-f]{32})", out)
        if rc != 0 or m is None or m.group(1).decode() != want:
            log.warning("Conversion cache: the pack of %s did not decode to the same "
                        "audio — kept as WAV", cache_key)
            _mark_unpackable(cache_key)
            return "skip"
        info.md5 = want
        await asyncio.to_thread(_finish_pack_file, tmp, _pack_header(info))
        ok = await asyncio.to_thread(_commit_pack, cache_key, entry, tmp, wav, st)
        if not ok:
            return "skip"
        log.debug("Conversion cache: packed %s (%d → %s bytes, %.2f s)", cache_key,
                  st.st_size, cached_size(cache_key), time.monotonic() - t0)
        await asyncio.to_thread(_maybe_evict)
        return "packed"
    except _PackError as exc:
        log.warning("Conversion cache: packing %s failed: %s", cache_key, exc)
        _mark_unpackable(cache_key)
        return "skip"
    except OSError as exc:
        log.warning("Conversion cache: packing %s failed: %s", cache_key, exc)
        return "later"
    finally:
        left = tuple(fd for fd in (in_fd, out_fd) if fd >= 0)
        if left:
            _close_all(left)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _close_all(fds) -> None:
    """Close each descriptor once, whatever the others do (blocking)."""
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


def cached_size(cache_key: str) -> "int | None":
    """The bytes ``cache_key`` holds on disk now (None: not cached)."""
    with _state_lock:
        e = _meta.get(cache_key)
        return int(e.get("size_bytes", 0) or 0) if e else None


async def pack_entry(cache_key: str, *, preempt=None) -> bool:
    """Pack the cached render ``cache_key`` now (see the module docstring):
    True when it is packed.  ffmpeg runs at the lowest priority
    (``_background_prefix``); ``preempt()``, asked before each step and every
    ``_PREEMPT_POLL_S`` while one runs, stops it (the entry stays as it was)."""
    return await _pack(cache_key, preempt) == "packed"


def _next_pack_key() -> "str | None":
    """The queued entry to pack next: of those due, the one nearest the
    eviction end of the LRU (packing it saves it from eviction), skipping
    entries a reader holds."""
    now = time.time()
    best, best_t = None, None
    with _state_lock:
        for k, due in list(_pack_queue.items()):
            if k not in _meta:
                _pack_queue.pop(k, None)
                continue
            if due > now or _pin_refs.get(k, 0) > 0:
                continue
            t = _lru.get(k, 0.0)
            if best_t is None or t < best_t:
                best, best_t = k, t
    return best


def _check_adopted_packs() -> int:
    """Read the header of every pack start-up adopted without reading it:
    its WAV's size and mtime are filled in; a damaged pack, one of another
    version, or one whose decoded copy doesn't match it is dropped (its WAV,
    when there is one, stays as an unpacked entry).  Blocking; returns how
    many were dropped."""
    with _state_lock:
        todo = [(k, e) for k, e in _meta.items() if e.get("packed") and e.get("unchecked")]
    dropped = 0
    for key, entry in todo:
        pack = Path(entry["path"]).with_suffix(PACK_SUFFIX)
        try:
            info = read_pack_info(pack)
        except OSError:
            continue                    # (not read now: stays unchecked, tried again)
        drop_pack = remove_entry = False
        with _state_lock:
            if _meta.get(key) is not entry:
                continue
            entry.pop("unchecked", None)
            if info is None:
                if _hot(entry):
                    drop_pack = True
                else:
                    remove_entry = True
            elif _hot(entry) and int(entry.get("wav_bytes") or -1) != info.wav_size:
                drop_pack = True        # the WAV is another render: it wins
            else:
                entry["wav_bytes"] = info.wav_size
                entry["wav_mtime_ns"] = info.mtime_ns
            if drop_pack:
                entry["packed"] = False
                entry.pop("packed_bytes", None)
                entry["wav_mtime_ns"] = None
                _resize_locked(entry)
        if remove_entry:
            _purge_entry(key, entry)
        if drop_pack or remove_entry:
            dropped += 1
            with contextlib.suppress(OSError):
                pack.unlink(missing_ok=True)
            if drop_pack:
                _queue_pack(key, 0.0)
    if dropped:
        log.info("Conversion cache: dropped %d unreadable or outdated pack(s)", dropped)
    return dropped


def _any_unchecked() -> bool:
    with _state_lock:
        return any(e.get("unchecked") for e in _meta.values())


async def _packer_loop() -> None:
    """The packer: one entry at a time, only while no render runs, stopped
    (and tried again later) when one starts.  Packs start-up adopted are
    checked first (again every 10 min while a read of one failed)."""
    await asyncio.sleep(1.0)
    next_check = 0.0
    while True:
        key = None
        try:
            if next_check is not None and time.monotonic() >= next_check:
                await asyncio.to_thread(_check_adopted_packs)
                next_check = (time.monotonic() + 600.0
                              if await asyncio.to_thread(_any_unchecked) else None)
            if not getattr(settings, "conversion_cache_pack", True):
                await asyncio.sleep(30.0)
                continue
            key = _next_pack_key()
            if key is None:
                await asyncio.sleep(2.0)
                continue
            if _busy():
                await asyncio.sleep(1.0)
                continue
            got = await _pack(key, _busy)
            if got == "retry":
                _queue_pack(key, 10.0)
            elif got == "later":
                _queue_pack(key, 600.0)
            elif got == "nocodec":
                await asyncio.sleep(300.0)      # (a changed ffmpeg_path is probed again)
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("Conversion cache packer (%s)", key)
            if key is not None:
                _mark_unpackable(key)
            await asyncio.sleep(5.0)


def start_packer() -> None:
    """Start the background packer on the running loop (start-up, after
    ``warmup_from_disk``; again after an in-process restart).  Idempotent."""
    global _packer_task
    loop = asyncio.get_running_loop()
    t = _packer_task
    if t is not None and not t.done():
        try:
            if t.get_loop() is loop:
                return
        except RuntimeError:
            pass
    _packer_task = loop.create_task(_packer_loop())


def stop_packer() -> None:
    """Stop the background packer (shutdown): a pack in progress is killed
    and its temp file removed; the entry stays as it was."""
    global _packer_task
    t, _packer_task = _packer_task, None
    if t is not None and not t.done():
        t.cancel()


# ── Core cache operations ───────────────────────────────────────────────────

async def get_cached(cache_key: str) -> Path | None:
    """Look up a cache entry; return its disk path (a complete WAV) or None.

    On a hit, updates the LRU timestamp.  A packed entry without its decoded
    copy is decoded back to its WAV first (``_run_unpack``; callers arriving
    meanwhile wait for the same decode).
    Self-heals if the metadata exists but the file is missing on disk.
    """
    from soniqboom.core import playback_io
    for _round in range(3):
        with _state_lock:
            entry = _meta.get(cache_key)
            if not entry:
                _lru.pop(cache_key, None)
                return None
            path = Path(entry["path"])
            fmt = entry.get("format_type") or ""
            hot = _hot(entry)
            packed = bool(entry.get("packed"))
        if not hot:
            u = _start_unpack(cache_key)
            if u is None:
                continue                # decoded meanwhile, or being replaced: look again
            # A hit: the entry moves to the recent end and is pinned while it
            # decodes (an old entry sits at the eviction end), so neither it
            # nor its fresh copy is evicted before this caller has it.
            with _state_lock:
                if cache_key in _meta:
                    _lru[cache_key] = time.time()
                    _lru.move_to_end(cache_key)
            hold = PinHold(cache_key)
            try:
                await u.done.wait()
            finally:
                hold.release()
            if u.ok:
                _note_hit(cache_key, fmt, path)
                return path             # (its audio was judged when it was packed)
            continue                    # failed / dropped / replaced: look again
        # The file check runs on the playback threads, never on the loop: the
        # cache may sit on a NAS data dir (``core.playback_io``).
        exists_now = await playback_io.run(playback_io.mount_key(path), path.exists)
        if exists_now:
            with _state_lock:
                if cache_key in _meta:
                    _lru[cache_key] = time.time()
                    _lru.move_to_end(cache_key)

        if not exists_now:
            if packed:
                # The decoded copy went (the pack is the entry): decode again.
                with _state_lock:
                    if _meta.get(cache_key) is entry:
                        _drop_hot_locked(entry)
                continue
            # Off-loop purge so a slow filesystem doesn't block the request.
            await asyncio.to_thread(_purge_entry, cache_key)
            return None
        if await _silent_leftover(cache_key, fmt, path):
            return None
        _note_hit(cache_key, fmt, path)
        return path
    return None


def _note_hit(cache_key: str, fmt: str, path: Path) -> None:
    """``on_hit`` for a hit (``core/loudness`` measures a render cached
    before it existed).  Never raises."""
    if on_hit is not None:
        try:
            on_hit(cache_key, fmt, path)
        except Exception:                                   # noqa: BLE001
            log.debug("on_hit hook failed for %s", cache_key, exc_info=True)


# Called on the event loop as ``on_hit(cache_key, format_type, path)`` for a
# cache hit (``core/loudness`` measures a render cached before it existed).
# Must not raise or block.
on_hit = None


# Renders cached before silent ones were refused (``refuse_silent``) — by an
# earlier version — can still be silent.  Each render's first hit in a run is
# judged once (``core.silence.wav_audible``: music costs one read of its first
# audible block): a silent one is dropped, so the tune renders again — with
# this version's renderer it may well be audible; a render that is silent
# again is refused then (``refuse_silent`` → the 422).  ``_judged`` holds the
# keys found audible (or that can't be judged) this run.
_judged: set[str] = set()
_JUDGED_MAX = 200_000


async def _silent_leftover(cache_key: str, format_type: str, path: Path) -> bool:
    """Is the cached render ``cache_key`` silent?  Then it is dropped (with
    any VU sidecar beside it): the caller renders it again."""
    if format_type in _SILENCE_EXEMPT or cache_key in _judged:
        return False
    from soniqboom.core import playback_io
    from soniqboom.core.silence import wav_audible
    try:
        audible = await playback_io.run(playback_io.mount_key(path), wav_audible, path)
    except Exception:
        audible = None
    if audible is not False:
        if len(_judged) >= _JUDGED_MAX:
            _judged.clear()
        _judged.add(cache_key)
        return False
    await asyncio.to_thread(_purge_entry, cache_key)
    try:
        path.with_suffix(".vu").unlink(missing_ok=True)
    except OSError:
        pass
    log.info("Cached render %s is silent — removed, it renders again", cache_key)
    return True


async def store_cached(
    cache_key: str,
    format_type: str,
    source_path: Path,
    *,
    cold: bool = False,
) -> Path:
    """Move a rendered temp WAV into the cache and register it.

    Placed by a rename when the temp file and the cache share a filesystem,
    else copied in under a temp name beside the entry and renamed once
    complete (``_move_into_place``) — the entry's name never holds a partial
    copy.  Triggers LRU eviction if over quota.

    ``cold``: a speculative render nobody has played yet (a duration probe)
    — a NEW entry goes in at the eviction end of the LRU, so browsing never
    pushes out renders people actually played.  Its first real read
    (``get_cached``) moves it to the recent end like any other entry.

    Sidecar handling
    ----------------
    Tracker renders may produce a ``<source>.vu`` per-channel VU sidecar
    next to the WAV (see ``soniqboom/core/openmpt_vu.py``).  If present,
    we move it alongside so the cached WAV always has its matching VU
    sidecar — the frontend looks for ``<wav>.vu`` to drive per-channel
    meters.  Sidecar bytes don't count towards the cache LRU budget
    because they're trivially small (~10–100 KB) compared to the WAV.
    """
    global _total_bytes

    dest = _cache_path(cache_key, format_type)
    src_sidecar  = source_path.with_suffix(".vu")
    dest_sidecar = dest.with_suffix(".vu")
    with _state_lock:
        # This render replaces the key's entry: a pack or a decode of the old
        # one in progress must not land over it (``_commit_pack`` /
        # ``_commit_unpack``), and an unlink deferred for a pinned reader of
        # the old file must not remove the new one (``unpin``).
        old = _meta.get(cache_key)
        if old is not None:
            old["stale"] = True
        pending = _pending_purge.pop(cache_key, None)
        _unpackable.discard(cache_key)

    def _place_and_size() -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _move_into_place(source_path, dest)
        # The old render's pack (if any) is not this render.
        dest.with_suffix(PACK_SUFFIX).unlink(missing_ok=True)
        # Move the sidecar too if it exists — non-fatal if it doesn't.
        if src_sidecar.exists():
            try:
                _move_into_place(src_sidecar, dest_sidecar)
            except Exception:
                pass
        return dest.stat().st_size

    try:
        size_bytes = await asyncio.to_thread(_place_and_size)
    except BaseException:
        # Nothing replaced: the old entry (if any) stays what it was.
        with _state_lock:
            if old is not None and _meta.get(cache_key) is old:
                old.pop("stale", None)
            if pending is not None:
                _pending_purge.setdefault(cache_key, pending)
        raise
    now = time.time()
    _clear_render_failure(cache_key)
    _silent_keys.pop(cache_key, None)

    with _state_lock:
        # Idempotent accounting: if this key already had an entry (e.g. two
        # concurrent cold renders of the same tune both finishing, or a
        # progressive render racing a blocking one), the new WAV replaced the
        # old file at ``dest`` via os.replace above — so drop the previous
        # entry's bytes from the running total before adding the new size.
        # Without this, a second store of the same key permanently inflates
        # ``_total_bytes`` (only one file exists on disk) and drives
        # ``_maybe_evict`` to prematurely evict legitimate entries.
        prev = _meta.get(cache_key)
        if prev:
            # Clamp at 0 (mirrors _purge_entry / _maybe_evict) so a drifted
            # counter can never go negative.
            _total_bytes = max(0, _total_bytes - int(prev.get("size_bytes", 0) or 0))
        _meta[cache_key] = {
            "path": str(dest),
            "size_bytes": size_bytes,
            "wav_bytes": size_bytes,
            "format_type": format_type,
            "created_at": now,
        }
        _lru[cache_key] = now
        _lru.move_to_end(cache_key, last=not (cold and not prev))
        _total_bytes += size_bytes
    if format_type not in _UNPACKED_TYPES:
        _queue_pack(cache_key)

    # Eviction may unlink several files; do it off the event loop.
    await asyncio.to_thread(_maybe_evict, cache_key)
    if on_stored is not None:
        try:
            on_stored(cache_key, format_type, dest, cold=cold)
        except Exception:                                   # noqa: BLE001
            log.debug("on_stored hook failed for %s", cache_key, exc_info=True)
    return dest


# Called on the event loop as ``on_stored(cache_key, format_type, path,
# cold=…)`` once ``store_cached`` has placed an entry (``core/loudness``
# measures a render there).  Must not raise or block.
on_stored = None


async def register_existing(
    cache_key: str,
    format_type: str,
    dest_path: Path,
) -> Path:
    """Register a file already placed at ``dest_path`` as a cache entry.

    The stream-as-render path (cast_pipe) writes to a ``.partial`` file
    and atomically renames it to the final cache path itself — at that
    point the bytes are on disk but the in-memory ``_meta``/``_lru``
    index doesn't know.  Without registration, the next ``get_cached``
    for the same key returns ``None`` until the warmup-from-disk pass at
    the next server restart — every "second play" therefore re-runs the
    full ffmpeg transcode instead of range-serving the warm WAV.

    This helper repairs that gap.  Idempotent: re-registering an
    existing entry just refreshes the LRU timestamp.
    """
    global _total_bytes

    def _stat() -> int:
        try:
            return dest_path.stat().st_size
        except OSError:
            return 0
    size_bytes = await asyncio.to_thread(_stat)
    if size_bytes <= 0:
        # Disk file vanished before we could register it.  Caller will
        # treat this as a cache miss next read.
        return dest_path

    now = time.time()
    with _state_lock:
        old = _meta.get(cache_key)
        if old:
            # Idempotent re-register — only touch LRU.
            _lru[cache_key] = now
            _lru.move_to_end(cache_key)
            return dest_path
        _meta[cache_key] = {
            "path":        str(dest_path),
            "size_bytes":  size_bytes,
            "wav_bytes":   size_bytes,
            "format_type": format_type,
            "created_at":  now,
        }
        _lru[cache_key] = now
        _lru.move_to_end(cache_key)
        _total_bytes += size_bytes
    # Run eviction off the event loop — bounded by the entry count above
    # the LRU budget, so a one-off register won't stall callers.
    await asyncio.to_thread(_maybe_evict, cache_key)
    return dest_path


def _unlink_cached(path_str: str) -> None:
    """Unlink an evicted cached WAV — deliberately KEEPING its ``<wav>.vu``
    per-channel VU sidecar.

    The sidecar is ~26 KB against a ~26 MB WAV, and it is far more expensive to
    regenerate: a SID sidecar costs a 3-pass sidplayfp render (~40-90 s
    wall-clock), where re-rendering the audio itself costs ~30 s.  Destroying
    the expensive-but-tiny artifact to reclaim the space of the cheap-but-huge
    one is backwards — it made a returning listener watch the FFT fallback for
    ~90 s ("came back to the track and the VU meter was gone", measured on a
    Firefox client whose in-browser render can't upload in time).

    Keeping it means a re-render after eviction serves the real per-voice meter
    immediately.  ``get_vu_sidecar_path`` locates these now-WAV-less sidecars by
    their deterministic path (``_find_orphan_sidecar``), and ``clear_cache``
    removes them on an explicit wipe, so they can't accumulate unbounded.
    A tracker render's ``<wav>.peak`` (``stream.render_peak`` — cheap to
    measure again) goes with the WAV, and so does the entry's pack.
    """
    p0 = Path(path_str)
    for p in (p0, p0.with_suffix(".peak"), p0.with_suffix(PACK_SUFFIX)):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def _purge_entry(cache_key: str, expect: "dict | None" = None) -> None:
    """Remove a single cache entry from metadata (``expect``: only when it is
    still that entry — not a render that replaced it meanwhile).

    If the entry is currently pinned (``_pin_refs[key] > 0``) the file unlink
    is deferred — the entry stays out of ``_meta`` / ``_lru`` so future
    callers won't find it, but the disk file lives until the last in-flight
    reader releases its pin.  ``unpin`` then completes the purge.
    """
    global _total_bytes
    deferred = False
    with _state_lock:
        if expect is not None and _meta.get(cache_key) is not expect:
            return
        entry = _meta.pop(cache_key, None)
        _lru.pop(cache_key, None)
        _pack_queue.pop(cache_key, None)
        if entry:
            size = entry.get("size_bytes", 0)
            if size > 0:
                _total_bytes = max(0, _total_bytes - size)
        if entry and _pin_refs.get(cache_key, 0) > 0:
            # Defer the unlink — an in-flight FileResponse is still reading
            # this file.  ``unpin`` will do the unlink once the refcount
            # drops to zero.
            _pending_purge[cache_key] = entry["path"]
            deferred = True
    if entry and not deferred:
        _unlink_cached(entry["path"])


def _maybe_evict(keep: "str | None" = None) -> None:
    """Evict oldest entries until total size is within the configured limit.

    ``keep``: the entry just stored (``store_cached`` / ``register_existing``)
    is never a victim of its own store — the caller is about to serve it.  A
    render larger than the whole budget (a 66-minute module at a 1 GB limit)
    was otherwise unlinked the moment it was stored, and its play failed
    with a missing file; now the cache holds it over budget until a later
    store evicts it (or, while a reader pins it, defers its unlink).

    Eviction is O(1) per item now that ``_lru`` is an ``OrderedDict`` —
    ``popitem(last=False)`` removes the least-recently-used key without
    scanning every entry, which was an O(N) cost per eviction previously.
    (The decoded-copy pass below is one O(N) walk per call over budget:
    0.4 ms at 3,000 entries.)

    Honours ``_pinned`` — a pinned key (typically the currently-playing
    track) is skipped while looking for an eviction victim, so a
    speculative N+2 prewarm can't evict the user's current playback and
    force a re-render.  If every entry is pinned and we're still over
    budget we fall back to oldest-first (correctness over politeness).

    Serialises under ``_state_lock`` because two concurrent stream requests
    can both schedule this via ``asyncio.to_thread`` — without the lock,
    two threads doing ``_lru.popitem(last=False)`` would race on the same
    OrderedDict and could ``KeyError`` or double-evict.

    Packed entries: before any render is removed, the decoded copies of
    packed entries are dropped, oldest first, except those
    ``_drop_hot_copies_locked`` keeps (the newest ones, in use, just used) —
    the pack stays, a later play decodes it again.
    """
    global _total_bytes
    max_bytes = settings.conversion_cache_max_bytes
    victims: list[tuple[str, dict]] = []
    dropped: "list[tuple[str, Path]]" = []
    with _state_lock:
        if _total_bytes > max_bytes:
            dropped = _drop_hot_copies_locked(max_bytes, keep)
        while _total_bytes > max_bytes and _lru:
            evict_key: str | None = None
            # First pass: pick the oldest unpinned key.  Refcount==0 means
            # no in-flight reader is currently using this entry.
            for k in _lru:
                if k != keep and _pin_refs.get(k, 0) == 0:
                    evict_key = k
                    break
            # Fallback: every entry is pinned — evict the oldest anyway so
            # we don't blow past the disk budget indefinitely.  The unlink
            # below will be deferred for any key still actually pinned.
            if evict_key is None:
                evict_key = next((k for k in _lru if k != keep), None)
                if evict_key is None:
                    break                   # only ``keep`` is left: it stays
                log.warning("ConvCache over budget but all entries pinned — evicting %s anyway",
                            evict_key)
            _lru.pop(evict_key, None)
            entry = _meta.pop(evict_key, None)
            if entry:
                size = entry.get("size_bytes", 0)
                _total_bytes = max(0, _total_bytes - size)
                victims.append((evict_key, entry))
                log.debug("ConvCache evicted %s (%d bytes)", evict_key, size)
        if not _lru:
            _total_bytes = 0
        # Mark any still-pinned victims for deferred unlink — only the
        # unpinned ones get unlinked outside the lock below.
        immediate_unlinks: list[dict] = []
        for k, entry in victims:
            if _pin_refs.get(k, 0) > 0:
                _pending_purge[k] = entry["path"]
            else:
                immediate_unlinks.append(entry)
    # Unlink outside the lock so a slow disk doesn't block other callers.
    _unlink_dropped(dropped)
    for entry in immediate_unlinks:
        _unlink_cached(entry["path"])


def _drop_hot_copies_locked(max_bytes: int, keep: "str | None") -> "list[tuple[str, Path]]":
    """(``_state_lock`` held) Drop decoded copies of packed entries, oldest
    first, until the cache fits ``max_bytes``.  Kept: the entries at the
    recent end of the LRU whose WAVs (packed or not) add up to
    ``_HOT_FLOOR_FRACTION`` of it, one used in the last ``_HOT_RECENT_S``, a
    pinned one, ``keep``, one being decoded, and one whose pack start-up
    hasn't checked yet (its WAV may be the only good copy).  Returns
    ``(key, file)`` to unlink with ``_unlink_dropped``.  One pass over the
    LRU."""
    floor = int(max_bytes * _HOT_FLOOR_FRACTION)
    cands: "list[str]" = []
    acc = 0
    protecting = True
    for k in reversed(_lru):            # newest first
        e = _meta.get(k)
        if e is None or not _hot(e):
            continue
        if protecting:
            wb = _wav_size_of(e)
            if acc + wb <= floor:
                acc += wb
                continue
            protecting = False
        if e.get("packed") and not e.get("unchecked"):
            cands.append(k)
    now = time.time()
    out: "list[tuple[str, Path]]" = []
    for k in reversed(cands):           # oldest first
        if _total_bytes <= max_bytes:
            break
        if (k == keep or now - _lru.get(k, 0.0) < _HOT_RECENT_S
                or _pin_refs.get(k, 0) > 0 or k in _unpacks or k in _drops_pending):
            continue
        p = _drop_hot_locked(_meta[k], k)
        if p is not None:
            out.append((k, p))
    return out


def pin(cache_key: str) -> int:
    """Acquire a pin on ``cache_key`` — increments the refcount.

    Every call must be paired with exactly one ``unpin(cache_key)`` once the
    caller no longer needs the entry held immobile (typically the streaming
    response close handler).  While the refcount is non-zero the LRU eviction
    loop will skip this key, and ``_purge_entry`` defers the actual file
    unlink so an in-flight ``FileResponse`` doesn't lose its open file out
    from under it.

    Returns the new refcount (mostly useful for assertions in tests).
    """
    with _state_lock:
        n = _pin_refs.get(cache_key, 0) + 1
        _pin_refs[cache_key] = n
        return n


def unpin(cache_key: str) -> int:
    """Release one pin on ``cache_key``.

    Matched with ``pin`` 1:1.  When the refcount falls to zero any deferred
    file unlink (queued by ``_purge_entry`` / ``_maybe_evict`` while the
    entry was pinned) is performed.

    Returns the new refcount.
    """
    pending_path: str | None = None
    with _state_lock:
        n = _pin_refs.get(cache_key, 0) - 1
        if n <= 0:
            _pin_refs.pop(cache_key, None)
            pending_path = _pending_purge.pop(cache_key, None)
            n = 0
        else:
            _pin_refs[cache_key] = n
    if pending_path:
        _unlink_cached(pending_path)        # the WAV, its peak sidecar, its pack
    return n


class PinHold:
    """One pin on ``key``, taken at construction and released exactly once by
    ``release()`` — from any thread, however many of the holder's cleanup
    paths call it (a response body's ``finally``, its background task, the
    error path of the code building it).  Usable as a context manager that
    releases on an exception only (``with PinHold(k) as hold: …`` hands the
    pin on to a response that releases it)."""

    __slots__ = ("key", "_done", "_lock")

    def __init__(self, key: str) -> None:
        self.key = key
        self._done = False
        self._lock = threading.Lock()
        pin(key)

    @property
    def released(self) -> bool:
        return self._done

    def release(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        try:
            unpin(self.key)
        except Exception:                               # noqa: BLE001
            pass

    def __enter__(self) -> "PinHold":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.release()
        return False


# ── Recent render failures ──────────────────────────────────────────────────
# key → (monotonic time, HTTP status, detail) of a render that just failed, so
# /render-status can say "failed" (and why) for a while instead of "idle" —
# the player then reports the server's reason instead of probing the stream
# (which would only run the failing render again).  Cleared when a render of
# the key starts or is stored; expires after _FAILURE_TTL_S; bounded.  Never a
# negative cache: a new play of the tune always renders again.
_recent_failures: "OrderedDict[str, tuple[float, int, str]]" = OrderedDict()
_FAILURE_TTL_S = 60.0
_FAILURE_MAX = 256


def note_render_failure(cache_key: str, status: int, detail: str) -> None:
    """Remember that the render of ``cache_key`` just failed (see above)."""
    _recent_failures[cache_key] = (time.monotonic(), int(status), str(detail or ""))
    _recent_failures.move_to_end(cache_key)
    while len(_recent_failures) > _FAILURE_MAX:
        _recent_failures.popitem(last=False)


def _clear_render_failure(cache_key: str) -> None:
    _recent_failures.pop(cache_key, None)
    _recent_refusals.pop(cache_key, None)


# key → the verified refusal (core/unplayable.py: its renderer refused the
# file twice) a render of it just ended with — what a request that waited
# for that render answers, and what /render-status names.  Kept like
# ``_recent_failures`` (same lifetime, cleared with it).
_recent_refusals: "OrderedDict[str, BaseException]" = OrderedDict()


def recent_refusal(track_id: str, subsong: int | None = None) -> "BaseException | None":
    """The verified refusal a render of ``track_id`` (``subsong``) ended with
    within ``_FAILURE_TTL_S`` (``recent_failure`` still lists it), else None."""
    now = time.monotonic()
    for k, exc in list(_recent_refusals.items()):
        hit = _recent_failures.get(k)
        if hit is None or now - hit[0] >= _FAILURE_TTL_S:
            _recent_refusals.pop(k, None)
        elif key_matches(k, track_id, subsong):
            return exc
    return None


def recent_failure(track_id: str, subsong: int | None = None) -> dict | None:
    """``{"status", "detail"}`` of a render of ``track_id`` (its ``subsong``
    when given — see ``key_matches``) that failed within ``_FAILURE_TTL_S``,
    else None.  Expired entries are dropped on the way."""
    now = time.monotonic()
    found = None
    for k, (t, status, detail) in list(_recent_failures.items()):
        if now - t >= _FAILURE_TTL_S:
            _recent_failures.pop(k, None)
        elif found is None and key_matches(k, track_id, subsong):
            found = {"status": status, "detail": detail}
    return found


# ── Silent renders ──────────────────────────────────────────────────────────
# A render that finished cleanly but holds nothing audible (``core.silence``)
# is never cached: the caller gets a 422 saying so instead of minutes of
# silence.  Every emulator / synth render is judged; a transcode of a real
# recording is what it is.  ``known_silent`` remembers such a key for a while:
# ``get_or_render`` then answers the 422 without rendering the silence again,
# and the progressive paths (which can judge a render only after streaming it)
# don't stream it again.
SILENT_RENDER_DETAIL = ("This tune plays only silence — the renderer produced "
                        "nothing audible for it.")
_SILENCE_EXEMPT = frozenset({"transcoded"})
_silent_keys: "OrderedDict[str, float]" = OrderedDict()
_SILENT_TTL_S = 600.0
_SILENT_MAX = 512


def note_silent(cache_key: str) -> None:
    """Remember that a render of ``cache_key`` came out silent."""
    _silent_keys[cache_key] = time.monotonic()
    _silent_keys.move_to_end(cache_key)
    while len(_silent_keys) > _SILENT_MAX:
        _silent_keys.popitem(last=False)


def known_silent(cache_key: str) -> bool:
    """Did a render of ``cache_key`` come out silent within ``_SILENT_TTL_S``?"""
    t = _silent_keys.get(cache_key)
    if t is None:
        return False
    if time.monotonic() - t >= _SILENT_TTL_S:
        _silent_keys.pop(cache_key, None)
        return False
    return True


async def refuse_silent(cache_key: str, format_type: str, wav_path: Path) -> None:
    """Raise a 422 (and drop ``wav_path`` with any VU sidecar beside it) when
    the render about to be cached under ``cache_key`` is silent.  A render
    that can't be judged (another sample format) passes.  The read runs in a
    thread and stops at the first audible block."""
    if format_type in _SILENCE_EXEMPT:
        return
    from soniqboom.core.silence import wav_audible
    try:
        audible = await asyncio.to_thread(wav_audible, Path(wav_path))
    except Exception:
        audible = None
    if audible is not False:
        return
    for p in (Path(wav_path), Path(wav_path).with_suffix(".vu")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    note_silent(cache_key)
    log.info("Render of %s is silent — not cached", cache_key)
    raise HTTPException(422, SILENT_RENDER_DETAIL)


def _note_failure_from(cache_key: str, exc: BaseException) -> None:
    """Record ``exc`` as the render failure of ``cache_key`` — an HTTP error
    with its status and detail, anything else as a 502.  A cancellation (a
    client that left, a retired prewarm) is not a failure."""
    if isinstance(exc, asyncio.CancelledError):
        return
    if getattr(exc, "verified", False):
        _recent_refusals[cache_key] = exc
        _recent_refusals.move_to_end(cache_key)
        while len(_recent_refusals) > _FAILURE_MAX:
            _recent_refusals.popitem(last=False)
    else:
        _recent_refusals.pop(cache_key, None)
    if isinstance(exc, HTTPException):
        note_render_failure(cache_key, exc.status_code, exc.detail)
    else:
        note_render_failure(cache_key, 502, "The render failed")


# ── Main entry point ────────────────────────────────────────────────────────

# How often a waiter whose render was cancelled (not failed) goes round again.
_WAITER_REENTRIES = 2


async def get_or_render(
    track_id: str,
    format_type: str,
    subsong: int,
    render_fn,
    soundfont_path: str | None = None,
    duration: int | None = None,
    codec: str | None = None,
    target_rate: int | None = None,
    bitrate: int | None = None,
    variant: str | None = None,
    *,
    tuned: bool = False,
    cold: bool = False,
    interest=None,
) -> tuple[Path, bool]:
    """Look up cache; on miss, render and store.  Returns (path, cache_hit).

    Handles thundering-herd: if another coroutine is already rendering the
    same key, waits for it to finish then serves from cache.

    ``duration`` is forwarded into the cache key for SID (so HVSC-supplied
    per-tune lengths produce distinct cache entries instead of colliding
    with the global-default render).  ``codec`` and ``target_rate`` are
    used by the ``transcoded`` format type so a DSD-as-96 kHz-FLAC entry
    doesn't collide with an ALAC-as-source-rate-FLAC entry.  ``bitrate`` and
    ``variant`` likewise separate transcodes (see ``_cache_key``).  ``cold``
    registers a new render at the eviction end of the LRU (``store_cached``).
    ``interest``: whether this caller still wants the render, asked while it
    waits (``register_interest``; default the context's ``render_interest``,
    else always) — a render nobody wants may be stopped for another.
    """
    key = _cache_key(
        track_id, format_type, subsong, soundfont_path,
        duration=duration, codec=codec, target_rate=target_rate,
        bitrate=bitrate, variant=variant, tuned=tuned,
    )

    # Fast path: cache hit without acquiring the per-key lock.
    cached = await get_cached(key)
    if cached:
        _cstats.hit("conversion")
        _note_result(key, format_type, cached)
        return cached, True
    # A render of this key came out silent a moment ago: say so again
    # rather than render the silence again (``_SILENT_TTL_S``).
    if known_silent(key):
        note_render_failure(key, 422, SILENT_RENDER_DETAIL)
        raise HTTPException(422, SILENT_RENDER_DETAIL)

    # Slow path: serialise cold callers for this key.  Inside the lock the
    # cache-check + inflight-register sequence is atomic, so two coroutines
    # arriving cold can't both decide "no one else is rendering" and both
    # kick off ffmpeg.
    cb = interest if interest is not None else (render_interest.get() or _always_wanted)
    tok = register_interest(key, cb)
    try:
        got = await _get_or_render_slow(key, format_type, render_fn, cold, cb)
    finally:
        drop_interest(key, tok)
    _note_result(key, format_type, got[0])
    return got


async def _get_or_render_slow(key: str, format_type: str, render_fn,
                              cold: bool, wants=_always_wanted) -> tuple[Path, bool]:
    lock = _lock_for(key)
    reentries = 0
    while True:
        is_renderer = False
        async with lock:
            # Re-check now we hold the lock — the cache may have filled while
            # we awaited acquisition.
            cached = await get_cached(key)
            if cached:
                _cstats.hit("conversion")
                return cached, True
            event = _inflight.get(key)
            if event is None:
                event = asyncio.Event()
                _inflight[key] = event
                _clear_render_failure(key)
                is_renderer = True
                # Cold path — this caller will actually render.  Count one miss
                # per logical render (the waiters below served from cache are
                # separate requests counted as hits when they wake).
                _cstats.miss("conversion")
        if is_renderer:
            break
        # Another coroutine is already rendering — wait for its event and
        # serve from cache when it completes.
        await event.wait()
        cached = await get_cached(key)
        if cached:
            _cstats.hit("conversion")
            return cached, True
        # Event set with nothing stored.  A FAILED render recorded why
        # (``_recent_failures``, cleared when it started): surface it, don't
        # retry.  No record means it was cancelled (a prewarm retired for a
        # progressive play of the same tune) — go round again: attach to the
        # render that replaced it, or render ourselves.  Bounded.
        if key in _recent_failures or reentries >= _WAITER_REENTRIES:
            refused = _recent_refusals.get(key)
            if refused is not None:
                # Its renderer refused the file twice: the same answer the
                # request that rendered it got.
                raise _unplayable.RenderRefusal(
                    refused.status_code, refused.detail, renderer=refused.renderer,
                    why=refused.why, diagnosed=refused.diagnosed,
                    headers=dict(refused.headers or {}), verified=True)
            raise HTTPException(
                502, "Cache fill failed in a concurrent render — try again",
            )
        # Stopped because nobody wanted it (``stream._preempt_abandoned_render``):
        # it renders again only when someone wants it now — this caller, or
        # another request waiting on the key (a play that arrived while it
        # was being stopped holds ``interest`` on it); bookkeeping alone never
        # starts it again.
        try:
            still_wanted = bool(wants()) or wanted(key)
        except Exception:                               # noqa: BLE001
            still_wanted = True
        if not still_wanted:
            raise RenderStopped(f"the render of {key} was stopped")
        reentries += 1

    # Run the render + store in a DETACHED, shield-protected task so a client
    # disconnect (which cancels THIS caller) doesn't abort a mostly-done render.
    # It finishes in the background and fills the cache, so the next play — the
    # returning listener, or a Subsonic/DLNA/cast retry — is instant instead of
    # re-rendering from scratch (the blocking analogue of the progressive path's
    # detach-and-finish).  The inflight Event + slot are released by the TASK, so
    # waiters wake correctly whether or not the original caller stuck around; SID
    # renders are bounded by ``stream._sid_blocking_sem`` so detached ones can't
    # pile up unbounded, and other formats' renders are short.
    async def _render_and_store() -> Path:
        rendering_key.set(key)          # (this task's own context)
        try:
            # (a refusal is rendered once more and, the same again, marks the
            # track it was for — core/unplayable.py)
            tmp_path = await _unplayable.render_checked(render_fn, format_type)
            await refuse_silent(key, format_type, tmp_path)
            stored = await store_cached(key, format_type, tmp_path, cold=cold)
            _unplayable.rendered_ok(format_type)
            return stored
        except BaseException as exc:
            _note_failure_from(key, exc)
            raise
        finally:
            event.set()
            _inflight.pop(key, None)

    task = asyncio.ensure_future(_render_and_store())
    _detached_renders.add(task)

    def _reap(t: asyncio.Task) -> None:
        _detached_renders.discard(t)
        if not t.cancelled():
            t.exception()          # retrieve so a caller-cancelled failure doesn't warn

    task.add_done_callback(_reap)
    # shield: a cancelled caller re-raises CancelledError but leaves the task
    # running to completion (and to fill the cache).
    dest = await asyncio.shield(task)
    return dest, False


# ── SID progressive playback helpers ─────────────────────────────────────────

# Background-render tasks keyed by cache key.  Weak-valued so finished tasks
# vanish automatically once asyncio releases its final reference — the
# previous strong-valued dict relied on the ``finally`` block popping the
# key, but a task cancelled / collected before reaching the ``finally``
# block (e.g. during shutdown) would leave a permanent dict entry behind.
# Callers in ``start_background_render`` keep their own strong reference to
# the new task in module state (``_bg_render_strong``) until its done-callback
# clears the entry — without that, Python 3.10+ may GC a pending task whose
# only reference was the weak-valued dict.
_bg_renders: "weakref.WeakValueDictionary[str, asyncio.Task]" = (
    weakref.WeakValueDictionary()
)
# Strong-ref set for live tasks — populated alongside ``_bg_renders`` and
# pruned via ``add_done_callback``.  This way the WeakValueDictionary frees
# entries immediately on task completion (no orphan keys) while the asyncio
# task itself stays alive for the duration of its execution.
_bg_render_strong: set[asyncio.Task] = set()

# Strong refs to detached get_or_render tasks (see get_or_render): a client
# disconnect cancels the caller but the shielded render task must survive to
# finish + cache, so it needs a reference the GC won't collect mid-run.
_detached_renders: set[asyncio.Task] = set()


async def find_shorter_sid_entry(
    track_id: str, subsong: int, target_dur: int, tail: str = "",
) -> tuple[Path, int] | None:
    """Find any cached SID entry for this track with a shorter duration.

    ``tail``: what the wanted key has after its ``dur<D>`` part (fidelity,
    fade, stereo — ``_cache_key``); only an entry with the same tail is the
    same render cut shorter (a plain mono render is no shorter version of a
    2SID tune's stereo one)."""
    prefix = f"{track_id}__sub{subsong}__dur"
    # Snapshot under the state lock — otherwise an in-flight
    # ``_maybe_evict`` on a worker thread can mutate ``_meta`` mid-iteration
    # and raise ``RuntimeError: dictionary changed size during iteration``.
    with _state_lock:
        candidates = [
            (key, entry) for key, entry in _meta.items()
            if key.startswith(prefix)
        ]
    best: tuple[Path, int] | None = None
    best_key = None
    for key, entry in candidates:
        rest = key[len(prefix):]
        if tail:
            if not rest.endswith(tail):
                continue
            rest = rest[:-len(tail)]
        try:
            dur = int(rest)
        except (ValueError, IndexError):
            continue
        if dur >= target_dur:
            continue
        # path.exists() is a sync syscall; the cache lives locally so this
        # is microseconds, but if it ever moves to a slow share this is the
        # spot to switch to asyncio.to_thread.  (A packed entry counts: it is
        # decoded below.)
        path = Path(entry["path"])
        if entry.get("packed") or path.exists():
            if best is None or dur > best[1]:
                best = (path, dur)
                best_key = key

    if best and best_key is not None:
        if is_packed(best_key):
            got = await get_cached(best_key)        # (its decoded copy; LRU touched)
            return (got, best[1]) if got is not None else None
        with _state_lock:
            if best_key in _lru:
                _lru[best_key] = time.time()
                _lru.move_to_end(best_key)
    return best


async def start_background_render(
    cache_key: str, format_type: str, render_fn,
) -> None:
    """Fire-and-forget: render in background and store when done."""
    if cache_key in _bg_renders or cache_key in _inflight:
        return

    async def _do():
        event = asyncio.Event()
        _inflight[cache_key] = event
        _clear_render_failure(cache_key)
        try:
            tmp_path = await _unplayable.render_checked(render_fn, format_type)
            await refuse_silent(cache_key, format_type, tmp_path)
            await store_cached(cache_key, format_type, tmp_path)
            _unplayable.rendered_ok(format_type)
            log.info("Background render complete: %s", cache_key)
        except Exception as exc:
            _note_failure_from(cache_key, exc)
            log.error("Background render failed for %s: %s", cache_key, exc)
        finally:
            event.set()
            _inflight.pop(cache_key, None)

    task = asyncio.create_task(_do())
    # Strong-ref the live task so the WeakValueDictionary entry survives
    # until the task is actually done; the done-callback prunes both.
    _bg_render_strong.add(task)
    _bg_renders[cache_key] = task
    task.add_done_callback(_bg_render_strong.discard)


def inflight_event(cache_key: str) -> "asyncio.Event | None":
    """The Event a render in progress for ``cache_key`` sets when it finishes
    (stored or failed), or ``None`` when nothing is rendering that key.

    Lets a caller ATTACH to a render someone else started without ever
    starting one itself (the waveform endpoint: it must never own a render —
    it is aborted on every track change, and a render it owned could be
    prepared differently from the audio request's)."""
    return _inflight.get(cache_key)


def key_matches(key: str, track_id: str, subsong: int | None = None) -> bool:
    """Does cache key ``key`` belong to ``track_id`` (and, when given, to its
    ``subsong``)?

    Keys start with ``<track_id>__`` (see ``_cache_key``).  Formats that carry
    subsongs key them as the next segment (``<id>__sub<N>`` — uade/tracker/…
    exactly, SID with a ``__dur…`` tail), so ``sub1`` must not match ``sub10``;
    keys whose next segment is not ``sub…`` (MIDI, transcodes, PSF) have no
    subsong and match any.  ``subsong=None`` matches every key of the track."""
    if key == track_id:
        return True
    prefix = f"{track_id}__"
    if not key.startswith(prefix):
        return False
    if subsong is None:
        return True
    rest = key[len(prefix):]
    if not rest.startswith("sub"):
        return True
    want = f"sub{int(subsong)}"
    return rest == want or rest.startswith(want + "__")


def render_state(track_id: str, subsong: int | None = None) -> str:
    """Coarse render state of any cached render of ``track_id`` (of one
    ``subsong`` when given — see ``key_matches``):
    ``"rendering"`` (a render or background render is under way),
    ``"complete"`` (a rendered WAV is cached) or ``"idle"``.

    The in-flight maps are tiny and the metadata map is a few thousand
    entries, so a prefix scan per status poll is cheap."""
    for k in list(_inflight) + list(_bg_renders.keys()):
        if key_matches(k, track_id, subsong):
            return "rendering"
    with _state_lock:
        for k in _meta:
            if key_matches(k, track_id, subsong):
                return "complete"
    return "idle"


async def is_cache_ready(cache_key: str) -> bool:
    if cache_key in _bg_renders or cache_key in _inflight:
        return False
    if is_packed(cache_key):
        return True                  # (a status check never decodes a pack)
    cached = await get_cached(cache_key)
    return cached is not None


# Every render format-type the cache can hold — THE single list, used by
# warmup adoption, clear_cache, cache_stats and the dir pre-create in
# config.py (QA M2/M3/M4 2026-07-02: four divergent hand-kept lists meant
# new formats couldn't be cleared, mis-reported in stats, and orphaned WAVs
# on full clears).
FORMAT_TYPES = ("sid", "midi", "tracker", "uade", "hvl", "gme",
                "adlib", "imf", "sndh", "ym", "sc68", "psf", "transcoded")


# ── Admin / stats ───────────────────────────────────────────────────────────

async def cache_stats() -> dict:
    with _state_lock:
        by_type: dict[str, int] = dict.fromkeys(FORMAT_TYPES, 0)
        by_type_bytes: dict[str, int] = dict.fromkeys(by_type, 0)
        packed = packed_bytes = hot = hot_bytes = wav_bytes = 0
        for entry in _meta.values():
            ft = entry.get("format_type", "")
            if ft in by_type:
                by_type[ft] += 1
                by_type_bytes[ft] += int(entry.get("size_bytes", 0) or 0)
            wav_bytes += _wav_size_of(entry)
            if entry.get("packed"):
                packed += 1
                packed_bytes += int(entry.get("packed_bytes") or 0)
                if _hot(entry):
                    hot += 1
                    hot_bytes += _wav_size_of(entry)
        return {
            "total_bytes": _total_bytes,
            "entry_count": len(_meta),
            "max_bytes": settings.conversion_cache_max_bytes,
            "pinned_count": len(_pin_refs),
            "by_type_bytes": by_type_bytes,
            "by_type": by_type,
            # Packed (WavPack) entries: how many, their packs' bytes, how many
            # of them also hold their decoded WAV and its bytes; ``wav_bytes``:
            # what every entry would take as plain WAV (unknown packed sizes
            # count 0 until read); ``pack_queue``: renders waiting to be packed.
            "packed_count": packed,
            "packed_bytes": packed_bytes,
            "decoded_count": hot,
            "decoded_bytes": hot_bytes,
            "wav_bytes": wav_bytes,
            "pack_queue": len(_pack_queue),
        }


def rendered_transcode_variant(source_key: str) -> str:
    """The ``variant`` of a transcode made from the rendered WAV cached under
    ``source_key`` (see ``stream._serve_rendered``): a render's transcodes are
    keyed on the render's own key, so subsongs / SID lengths never share one."""
    return hashlib.sha1(source_key.encode("utf-8")).hexdigest()[:10]


# Renders whose ``sub<N>`` (N > 0) used to hold the wrong tune: SID / SNDH /
# SC68 renderers read the 0-based wire index as their 1-based track number, so
# ``sub1`` held tune 1 (the tune ``sub0`` plays by default) instead of tune 2;
# and for a file whose default tune isn't tune 1, wire ``start-1`` now plays
# tune 1 (``stream.sid_wire_tune``).  Their keys are unchanged by the fix, so a
# cache directory written before it is swept once — the WAVs, their VU
# sidecars and the transcodes made from them — and then carries this marker.
_SUBSONG_BASE_FORMATS = ("sid", "sndh", "sc68")
_SUBSONG_BASE_MARKER = ".subsong-wire-index-v3"


def _stale_subsong_key(key: str) -> bool:
    """Is ``key`` (a cache file's stem) ``<track_id>__sub<N>[__…]`` with N > 0?"""
    parts = key.split("__")
    if len(parts) < 2 or not parts[1].startswith("sub"):
        return False
    try:
        return int(parts[1][3:]) > 0
    except ValueError:
        return False


def _sweep_stale_subsong_renders(base: Path) -> int:
    """One-time sweep of the pre-fix multi-tune renders (see
    ``_SUBSONG_BASE_MARKER``) — before warmup adopts anything, so none of them
    is ever served.  Idempotent; returns the number of files removed.  The
    marker is written only after a complete sweep, so an interrupted one
    simply runs again on the next start."""
    marker = base / _SUBSONG_BASE_MARKER
    if marker.exists():
        return 0
    removed = 0
    stale_variants: set[str] = set()
    for fmt in _SUBSONG_BASE_FORMATS:
        sub = base / fmt
        if not sub.is_dir():
            continue
        for f in sub.rglob("*"):
            if f.suffix not in (".wav", ".vu", PACK_SUFFIX) or not _stale_subsong_key(f.stem):
                continue
            if f.suffix in (".wav", PACK_SUFFIX):
                stale_variants.add(rendered_transcode_variant(f.stem))
            try:
                f.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    tdir = base / "transcoded"
    if stale_variants and tdir.is_dir():
        for f in tdir.rglob("*.wav"):
            _, sep, tail = f.stem.rpartition("__v")
            if sep and tail in stale_variants:
                try:
                    f.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
    try:
        marker.write_text("sub<N> = wire index N (stream.sid_wire_tune)\n")
    except OSError:
        log.warning("Conversion cache: could not write %s", marker)
    if removed:
        log.info("Conversion cache: removed %d pre-fix multi-tune SID/SNDH/SC68 "
                 "render file(s)", removed)
    return removed


# Render revisions (``_RENDER_REVS``) are swept once per revision: an older
# render is never adopted (its key would still match its tune — see
# ``key_matches`` — and report the tune as rendered), nor its transcodes.
def _render_rev_marker() -> str:
    return ".render-revs-" + "-".join(f"{k}{v}" for k, v in sorted(_RENDER_REVS.items()))


def _sweep_unrevised_renders(base: Path) -> int:
    """Remove ``_RENDER_REVS`` renders made before their current revision
    (and the transcodes made from them).  Idempotent; the marker is written
    after a complete sweep."""
    marker = base / _render_rev_marker()
    if marker.exists():
        return 0
    removed = 0
    stale_variants: set[str] = set()
    for fmt, rev in _RENDER_REVS.items():
        sub = base / fmt
        if not sub.is_dir():
            continue
        tag = f"__r{rev}"
        for f in sub.rglob("*"):
            if f.suffix not in (".wav", ".vu", PACK_SUFFIX):
                continue
            stem = f.stem
            if stem.endswith(".partial"):
                stem = stem[: -len(".partial")]
            if tag in stem:
                continue
            if f.suffix in (".wav", PACK_SUFFIX):
                stale_variants.add(rendered_transcode_variant(stem))
            try:
                f.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    tdir = base / "transcoded"
    if stale_variants and tdir.is_dir():
        for f in tdir.rglob("*.wav"):
            _, sep, tail = f.stem.rpartition("__v")
            if sep and tail in stale_variants:
                try:
                    f.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
    try:
        marker.write_text("renders older than these revisions removed\n")
    except OSError:
        log.warning("Conversion cache: could not write %s", marker)
    if removed:
        log.info("Conversion cache: removed %d render file(s) older than their "
                 "renderer's current revision", removed)
    return removed


def _sweep_unfinished_writes(base: Path) -> int:
    """Remove what a process that ended part-way left in the cache: copies
    in progress (``_move_into_place``'s ``.copying`` temps), the cast pipe's
    ``<key>.wav.partial`` (``cast_pipe.render_stream``), a SID upload's
    temp (``tracks``' SID WAV upload) and a pack or a decode of one in
    progress (``_PACK_TMP_SUFFIX`` / ``_UNPACK_TMP_SUFFIX``).  Run at the
    start, before anything writes the cache."""
    removed = 0
    for fmt in FORMAT_TYPES:
        sub = base / fmt
        if not sub.is_dir():
            continue
        for pat in (f"*{_COPY_TMP_SUFFIX}", "*.wav.partial", "*.wav.upload",
                    f"*{_PACK_TMP_SUFFIX}", f"*{_UNPACK_TMP_SUFFIX}"):
            for f in sub.rglob(pat):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    if removed:
        log.info("Conversion cache: removed %d unfinished write(s) a previous run left", removed)
    return removed


def warmup_from_disk() -> int:
    """Rebuild ``_meta`` / ``_lru`` / ``_total_bytes`` from on-disk WAVs and
    packs.

    The cache state is in-memory; without this, every server restart leaves
    previously-rendered WAVs orphaned on disk, so the next play re-renders
    even though the file already exists.  Run once at startup.  Renders from
    before the multi-tune index fix are swept first (once per cache dir —
    ``_sweep_stale_subsong_renders``), and so are unfinished writes
    (``_sweep_unfinished_writes``); a WAV shorter than its header declares
    is removed, not adopted (``_wav_is_whole``).

    A pack without its WAV (``<key>.sbwv``) is adopted by its size alone —
    its header is read after start-up (``_check_adopted_packs``).  A WAV
    beside its pack is the entry's decoded copy when it is no newer than the
    pack (a decoded copy carries its original's mtime) and the pack's header
    (read here: the WAV may later be dropped on the strength of it) matches
    it; a newer WAV is a later render (another version's, say) and wins, and
    so does a WAV whose pack is damaged, of another version or of another
    size: the pack is removed.  WAVs not packed yet are queued for the
    packer.

    Returns the number of entries adopted."""
    global _total_bytes
    base = get_conversion_cache_dir()
    if not base.exists():
        return 0
    _sweep_stale_subsong_renders(base)
    _sweep_unrevised_renders(base)
    _sweep_unfinished_writes(base)
    adopted = 0
    with _state_lock:
        for fmt in FORMAT_TYPES:
            sub = base / fmt
            if not sub.exists():
                continue
            packs: "dict[str, tuple[Path, os.stat_result]]" = {}
            for pk in sub.rglob("*" + PACK_SUFFIX):
                try:
                    packs[pk.stem] = (pk, pk.stat())
                except OSError:
                    continue
            for wav in sub.rglob("*.wav"):
                # ``foo.partial.wav`` is a half-written render left behind by
                # a crashed ffmpeg / sidplayfp.  Adopting it as a real cache
                # entry would serve a truncated/corrupt stream to the next
                # caller.  Unlink and skip — the next playback will trigger
                # a fresh render.
                if wav.name.endswith(".partial.wav"):
                    try:
                        wav.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                key = wav.stem
                if key in _meta:
                    packs.pop(key, None)
                    continue  # already adopted (idempotent)
                try:
                    st = wav.stat()
                except OSError:
                    continue
                # Shorter than its header says: a write or copy that ended
                # part-way (a process killed mid-store, a power loss) —
                # removed, the next play renders it again.
                if not _wav_is_whole(wav, st.st_size):
                    log.warning("Conversion cache: removed %s — %d bytes, less than "
                                "its WAV header declares", wav.name, st.st_size)
                    try:
                        wav.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                entry = {
                    "path": str(wav),
                    "size_bytes": st.st_size,
                    "wav_bytes": st.st_size,
                    "format_type": fmt,
                    # mtime is the closest proxy to "last-touched"; the LRU
                    # entry below uses the same value so subsequent eviction
                    # decisions are sensible from the first request.
                    "created_at": st.st_mtime,
                }
                seen = st.st_mtime
                pk = packs.pop(key, None)
                info = None
                if pk is not None and st.st_mtime_ns <= pk[1].st_mtime_ns:
                    # A decoded copy or the pack's own source — the pack's
                    # header read now (the WAV may be dropped on the strength
                    # of it): one with another version or size is dropped.
                    try:
                        info = read_pack_info(pk[0])
                        if info is None or info.wav_size != st.st_size:
                            info = None
                        else:
                            entry.update(packed=True, packed_bytes=pk[1].st_size, hot=True,
                                         wav_mtime_ns=info.mtime_ns)
                    except OSError:     # not read now: kept, checked later
                        info = False
                        entry.update(packed=True, packed_bytes=pk[1].st_size, hot=True,
                                     wav_mtime_ns=st.st_mtime_ns, unchecked=True)
                    if entry.get("packed"):
                        entry["size_bytes"] = st.st_size + pk[1].st_size
                        seen = max(seen, pk[1].st_mtime)
                if pk is not None and not entry.get("packed"):
                    # a later render (newer than its pack), or a pack that
                    # doesn't match the WAV: the WAV wins
                    try:
                        pk[0].unlink(missing_ok=True)
                    except OSError:
                        pass
                _meta[key] = entry
                _lru[key] = seen
                _total_bytes += entry["size_bytes"]
                if not entry.get("packed") and fmt not in _UNPACKED_TYPES:
                    _pack_queue[key] = 0.0
                adopted += 1
            for key, (pk, pst) in packs.items():
                if key in _meta:
                    continue
                _meta[key] = {
                    "path": str(pk.with_suffix(".wav")),
                    "size_bytes": pst.st_size,
                    "format_type": fmt,
                    "created_at": pst.st_mtime,
                    "packed": True, "packed_bytes": pst.st_size, "hot": False,
                    "wav_bytes": None, "wav_mtime_ns": None, "unchecked": True,
                }
                _lru[key] = pst.st_mtime
                _total_bytes += pst.st_size
                adopted += 1
        # Order _lru by mtime so the oldest entries are first (LRU candidates).
        if adopted:
            ordered = sorted(_lru.items(), key=lambda kv: kv[1])
            _lru.clear()
            for k, t in ordered:
                _lru[k] = t
    return adopted


async def purge_sid_entries_for(
    track_ids: list[str],
    keep_duration: dict[str, int | list[int] | set[int]] | None = None,
) -> int:
    """Remove SID cache entries for the given track_ids.

    ``keep_duration`` maps each track id to a single duration or an iterable of
    valid durations — entries whose embedded ``__dur{N}`` matches any value are
    preserved.  Useful after HVSC rescan: pass the post-rescan length so the
    correct-duration WAV survives while stale renders (e.g. the pre-HVSC global
    default) are evicted.  Multi-subsong tracks pass the set of all
    ``hvsc_lengths`` so every still-relevant subsong render survives.
    """
    if not track_ids:
        return 0
    raw = keep_duration or {}
    keep: dict[str, set[int]] = {}
    for tid, v in raw.items():
        if isinstance(v, (int, float)):
            keep[tid] = {int(v)}
        else:
            keep[tid] = {int(x) for x in v if isinstance(x, (int, float))}
    # One pass over the cache keys, each parsed once (``<tid>__sub…``) —
    # not every key against every id.
    ids = set(track_ids)
    victims: list[str] = []
    with _state_lock:
        for key in list(_meta.keys()):
            tid, sep, _rest = key.partition("__sub")
            if not sep or tid not in ids:
                continue
            # Preserve entries whose embedded duration is still valid.
            if tid in keep:
                try:
                    dur_str = key.rsplit("__dur", 1)[-1]
                    if int(dur_str) in keep[tid]:
                        continue  # keep this entry
                except (ValueError, IndexError):
                    pass
            victims.append(key)
    for key in victims:
        await asyncio.to_thread(_purge_entry, key)
    return len(victims)


async def purge_tracks(track_ids) -> int:
    """Everything cached FROM these tracks' files — every render and
    transcode (all formats, tunes, variants), their VU sidecars (eviction
    keeps those) and recent failure / silence notes: the files changed at
    their source (an online collection's re-uploaded file).  Returns the
    entries purged."""
    ids = {str(t) for t in (track_ids or ()) if t}
    if not ids:
        return 0
    with _state_lock:
        victims = [k for k in list(_meta) if k.split("__", 1)[0] in ids]
    for key in victims:
        await asyncio.to_thread(_purge_entry, key)
    for k in [k for k in list(_recent_failures) if k.split("__", 1)[0] in ids]:
        _recent_failures.pop(k, None)
    for k in [k for k in list(_silent_keys) if k.split("__", 1)[0] in ids]:
        _silent_keys.pop(k, None)

    def _sidecars() -> None:
        # One listing per format and id prefix folder, not a stat and a glob
        # per id and format (a local scan purges every file it found changed).
        base = get_conversion_cache_dir()
        by_dir: dict[str, set] = {}
        for tid in ids:
            by_dir.setdefault(tid[:2], set()).add(tid)
        for fmt in FORMAT_TYPES:
            for pre, tids in by_dir.items():
                d = base / fmt / pre
                try:
                    names = os.listdir(d)
                except OSError:
                    continue
                for name in names:
                    # ``<id>.vu`` or ``<id>__<variant>.vu``
                    if name.endswith(".vu") and name[:-3].split("__", 1)[0] in tids:
                        try:
                            (d / name).unlink(missing_ok=True)
                        except OSError:
                            pass
    await asyncio.to_thread(_sidecars)
    return len(victims)


async def purge_entries_for(track_ids, format_type: str) -> int:
    """Remove every cached ``format_type`` render of the given tracks (all
    their tunes and variants) — renders a renderer change made wrong.
    Returns how many entries went (their transcodes included)."""
    ids = set(track_ids or ())
    if not ids:
        return 0
    victims: list[str] = []
    with _state_lock:
        for key, entry in list(_meta.items()):
            if entry.get("format_type") != format_type:
                continue
            tid, sep, _rest = key.partition("__sub")
            if sep and tid in ids:
                victims.append(key)
        # …and the transcodes made from those renders (a Subsonic client's
        # ``format=mp3``), keyed on the render's key.
        variants = {rendered_transcode_variant(k) for k in victims}
        if variants:
            for key, entry in list(_meta.items()):
                if entry.get("format_type") != "transcoded":
                    continue
                _, sep, tail = key.rpartition("__v")
                if sep and tail in variants:
                    victims.append(key)
    for key in victims:
        await asyncio.to_thread(_purge_entry, key)
    return len(victims)


async def clear_cache(types: list[str] | None = None) -> dict:
    """Wipe cached conversion files + in-memory metadata.

    When ``types`` is None (default), wipe everything.  Otherwise wipe only
    the named format types (any of ``sid`` / ``midi`` / ``tracker`` /
    ``transcoded``) — useful for the Settings UI "Clear transcode cache"
    button so a user can reclaim DSD/ALAC disk without losing their
    sidplayfp / fluidsynth / openmpt renders (which are expensive to
    regenerate from scratch)."""
    global _total_bytes

    all_types = set(FORMAT_TYPES)
    selected = set(types) if types else all_types
    selected &= all_types
    if not selected:
        return {"deleted_files": 0, "types": []}

    cache_dir = get_conversion_cache_dir()

    def _purge_disk() -> int:
        # Sweep the VU sidecars too, not just the WAVs.  Eviction deliberately
        # KEEPS ``.vu`` files (tiny, expensive to regenerate) and the VU pass can
        # write one with no WAV at all, so an explicit user-requested wipe is the
        # only thing that reaps them — globbing "*.wav" alone left them orphaned
        # on disk forever (301 such strays were measured before this fix).
        n = 0
        for sub in selected:
            d = cache_dir / sub
            if d.exists():
                for pat in ("*.wav", "*.vu", "*" + PACK_SUFFIX, "*.peak"):
                    for f in d.rglob(pat):
                        try:
                            f.unlink(missing_ok=True)
                            n += 1
                        except OSError:
                            pass
        return n

    deleted = await asyncio.to_thread(_purge_disk)
    with _state_lock:
        if selected == all_types:
            _meta.clear()
            _lru.clear()
            _pack_queue.clear()
            _total_bytes = 0
        else:
            # Surgical purge: only remove meta entries whose format_type
            # matches the selection.  Recompute _total_bytes to match.
            for key in list(_meta.keys()):
                if _meta[key].get("format_type") in selected:
                    _meta.pop(key, None)
                    _lru.pop(key, None)
                    _pack_queue.pop(key, None)
            _total_bytes = sum(int(e.get("size_bytes", 0) or 0)
                               for e in _meta.values())
    log.info("Conversion cache cleared (%s): %d files",
             ",".join(sorted(selected)), deleted)
    return {"deleted_files": deleted, "types": sorted(selected)}
