# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The voices of a chip tune: per-voice VU meters for libgme chiptunes and
AdLib / OPL tunes, and the voice mixer's renders (mute / solo, per-voice
scopes) for those and C64 SID.

Per-voice VU (``request_chip_vu``)
----------------------------------
A libgme or AdLib play registers its render here (``note_chip_vu_wanted``,
from ``stream.stream_track``).  The pass that builds the sidecar starts only
when ``GET /api/tracks/{id}/vu`` asks for it — the web now-playing meter, for
the track that is playing, with the meter on — once the track was first
served ``_MIN_PLAYED_S`` ago and its render is cached; then it waits
``_START_DELAY_S`` (the next-track prewarm goes first) and runs in the
background priority gate at ``PRIO_VU``, the lowest: never a playback slot.

* libgme (``core.gme_voices``): one render per voice with only that voice
  heard, on the cached render's timeline, in slices of ``_GME_SLICE_S`` of
  audio per slot hold, so a queued prewarm waits at most one slice.
* AdLib / OPL (``native/oplvoices``): one pass of AdPlug's player through
  the same Nuked OPL3 core adplay renders with, reading each channel's share
  of the mix after every sample.

The sidecar goes beside the cached WAV (``<key>.vu``), with the voice names
(``docs/vu-cache-format.md``).  A pass is dropped when the meter stopped
asking for ``_POLL_FRESH_S`` (the listener moved on); one that ran without a
result is remembered (``skipped``) for ``_SKIP_TTL_S``.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger(__name__)

_MIN_PLAYED_S = 3.0
_START_DELAY_S = 4.0
# The web player's re-ask ladder for these meters is the Amiga one
# (frontend/js/vu/vumr.js AMIGA_VU_RETRY_MS: every rung ≤ 15 s), so a pass
# counts as wanted while the meter asked within this window.
_POLL_FRESH_S = 20.0
_SKIP_TTL_S = 3600.0
_WANTED_MAX = 64
# Longest tune given meters: a Mega Drive VGM renders at ~17x real time per
# voice, eight voices — a 15-minute tune costs ~7 minutes of one core.
_MAX_TUNE_S = 900.0
_GME_SLICE_S = 20.0
# A pass under way stops only when the meter has not asked for this long (the
# player's ladder ends ~164 s after its first ask; a long Mega Drive VGM needs
# minutes): the sidecar is then there for the next play.
_RUNNING_FRESH_S = 600.0
_OPL_TIMEOUT_S = 300.0

# poll key ("<family>:<id>:<subsong>") → entry dict
_WANTED: "OrderedDict[str, dict]" = OrderedDict()
_LAST_POLL: "OrderedDict[str, float]" = OrderedDict()
_SKIPPED: "OrderedDict[str, float]" = OrderedDict()
_INFLIGHT: set = set()


async def _owned(fn, *args):
    """``fn(*args)`` in a worker thread, for work on a libgme emulator (or a
    file) the caller frees afterwards: a cancel waits for the thread to
    return before it propagates — an emulator is never deleted under a
    running ``gme_play`` (a crash: "pure virtual function called")."""
    fut = asyncio.get_running_loop().run_in_executor(None, fn, *args)
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError as cancel:
        while not fut.done():
            try:
                await asyncio.shield(fut)
            except asyncio.CancelledError:
                continue
            except BaseException:                       # noqa: BLE001
                break
        raise cancel


def _poll_key(family: str, track_id: str, subsong: int) -> str:
    return f"{family}:{track_id}:{int(subsong)}"


def _chip_key(track_id: str, subsong: int) -> "str | None":
    """The poll key of a registered chip render of this tune (gme / adlib /
    imf), or None."""
    for fam in ("gme", "adlib", "imf"):
        k = _poll_key(fam, track_id, subsong)
        if k in _WANTED or k in _SKIPPED:
            return k
    return None


def note_chip_vu_wanted(family: str, track_id: str, subsong: int, cache_key: str,
                        src_path, zip_pin_id: "str | None" = None) -> None:
    """Register a libgme (``family`` "gme") or AdLib ("adlib" / "imf")
    render served for ``track_id`` tune ``subsong`` under ``cache_key`` —
    the per-voice pass may be asked for later.  A dict write."""
    note_voice_source(family, track_id, subsong, cache_key, src_path, zip_pin_id)
    key = _poll_key(family, track_id, subsong)
    prev = _WANTED.get(key)
    _WANTED[key] = {
        "family": family, "track_id": track_id, "subsong": int(subsong),
        "cache_key": cache_key, "src": Path(src_path), "pin": zip_pin_id,
        "served_at": prev["served_at"] if prev else time.monotonic(),
    }
    _WANTED.move_to_end(key)
    while len(_WANTED) > _WANTED_MAX:
        _WANTED.popitem(last=False)


def _still_wanted(key: str, window: float = _POLL_FRESH_S) -> bool:
    t = _LAST_POLL.get(key)
    return t is not None and time.monotonic() - t <= window


def _note_skipped(key: str) -> None:
    _SKIPPED[key] = time.monotonic()
    _SKIPPED.move_to_end(key)
    while len(_SKIPPED) > 256:
        _SKIPPED.popitem(last=False)


def chip_vu_skipped_reason(track_id: str, subsong: int) -> "str | None":
    """``"skipped"`` when this tune's per-voice pass ran without a sidecar
    within ``_SKIP_TTL_S``; ``"unsupported"`` when this server can't make
    one for a registered render (libgme without the voice API, no
    oplvoices, a render libgme didn't make); else None."""
    for fam in ("gme", "adlib", "imf"):
        key = _poll_key(fam, track_id, subsong)
        t = _SKIPPED.get(key)
        if t is not None:
            if time.monotonic() - t < _SKIP_TTL_S:
                return "skipped"
            _SKIPPED.pop(key, None)
        ent = _WANTED.get(key)
        if ent is not None and not _family_supported(ent):
            return "unsupported"
    return None


def _oplvoices_binary() -> "str | None":
    from soniqboom.api import stream as _s
    return _s._find_renderer(getattr(_s.settings, "oplvoices_path", "") or "", "oplvoices")


def _family_supported(ent: dict) -> bool:
    if ent["family"] == "gme":
        from soniqboom.core import gme_voices
        from soniqboom.core.conversion_cache import _cache_key
        # (a KSS that kssrender plays has a variant key: not libgme's render)
        return (gme_voices.is_available()
                and ent["cache_key"] == _cache_key(ent["track_id"], "gme", ent["subsong"]))
    return _oplvoices_binary() is not None


def request_chip_vu(track_id: str, subsong: int) -> bool:
    """Start the per-voice pass of a libgme / AdLib render this server served
    (GET /vu calls it on a sidecar miss).  False when none was served for
    the tune.  Every call marks the tune as still wanted."""
    key = None
    for fam in ("gme", "adlib", "imf"):
        k = _poll_key(fam, track_id, subsong)
        if k in _WANTED:
            key = k
            break
    if (key is None or chip_vu_skipped_reason(track_id, subsong) is not None
            or not _family_supported(_WANTED[key])):
        return False
    now = time.monotonic()
    _LAST_POLL[key] = now
    _LAST_POLL.move_to_end(key)
    while len(_LAST_POLL) > _WANTED_MAX:
        _LAST_POLL.popitem(last=False)
    ent = _WANTED[key]
    if now - ent["served_at"] < _MIN_PLAYED_S or key in _INFLIGHT:
        return True
    _INFLIGHT.add(key)
    from soniqboom.api import stream as _s
    _s._bg_keep(asyncio.ensure_future(_run_chip_vu(key, dict(ent))))
    return True


# ── libgme ─────────────────────────────────────────────────────────────────

def gme_inputs(path: Path, subsong: int,
               raw: "bytes | None" = None) -> "tuple[bytes, float, float | None] | None":
    """``(data, duration, fade_at)`` exactly as ``stream._render_gme`` hands
    them to libgme for this tune, or None when libgme doesn't make its
    render (a KSS kssrender plays, a VGM for chips libgme lacks, no libgme).
    Keep in step with ``_render_gme``.  Blocking (reads the file)."""
    from soniqboom.api import stream as _s
    from soniqboom.core import gme_render
    ext = path.suffix.lower()
    if not gme_render.is_available():
        return None
    if ext == ".kss" and _s._gme_variant(ext):
        return None
    data = path.read_bytes() if raw is None else raw
    if ext in (".vgm", ".vgz") and gme_render.vgm_bytes_need_other_engine(data):
        return None
    duration = float(_s.settings.sid_default_duration)
    if ext == ".gym":
        data = gme_render.unpack_gym(data)
        secs = gme_render.gym_seconds(data)
        if secs:
            duration = float(min(3600, int(secs) + 1 + 8))
        return data, duration, None
    duration, fade_at = gme_render.render_plan(data, subsong, duration)
    return data, duration, fade_at


async def _gme_inputs_async(path: Path, subsong: int):
    """``gme_inputs`` with only the file read on the playback I/O pool (its
    mount turn); the plan (GYM inflation, libgme's lengths) in a worker."""
    from soniqboom.api import stream as _s
    raw = await _s._pb_at(path, path.read_bytes)
    return await asyncio.to_thread(gme_inputs, path, subsong, raw)


def _wav_frames(wav: Path) -> int:
    """Frames of the 44.1 kHz 16-bit stereo cached render ``wav``."""
    from soniqboom.api import stream as _s
    return int(round(_s._wav_audio_seconds(wav) * 44100))


async def _gme_vu(ent: dict, wav: Path, key: str) -> "object | None":
    from soniqboom.api import stream as _s
    from soniqboom.core import gme_voices
    if not gme_voices.is_available():
        return None
    try:
        inputs = await _gme_inputs_async(ent["src"], ent["subsong"])
    except (OSError, ValueError):
        return None
    if inputs is None:
        return None
    data, duration, fade_at = inputs
    ref_frames = await asyncio.to_thread(_wav_frames, wav)
    if ref_frames <= 0 or ref_frames > _MAX_TUNE_S * 44100:
        return None
    async with _s._bg_render_sem.slot(_s.PRIO_VU):
        lead = await _owned(gme_voices.lead_in_frames, data, ent["subsong"])
    if lead is None:
        return None
    builder = await _owned(functools.partial(
        gme_voices.VuBuilder, data, ent["subsong"], ref_frames=ref_frames,
        fade_at_ms=gme_voices.fade_ms(duration, fade_at), lead_in=lead))
    try:
        while not builder.done:
            async with _s._bg_render_sem.slot(_s.PRIO_VU):
                if not _still_wanted(key, _RUNNING_FRESH_S):
                    return None
                await _owned(builder.step, _GME_SLICE_S)
        return builder.result()
    finally:
        builder.close()


# ── AdLib / OPL ───────────────────────────────────────────────────────────

def _adlib_rate() -> int:
    """The rate adplay renders at for the reference (``stream._adlib_cmd``):
    the OPL chip's own, resampled by ffmpeg — or 44.1 kHz straight when there
    is no ffmpeg / interpreter for the pipeline."""
    from soniqboom.api import stream as _s
    from soniqboom.core.merger import _is_bundled
    if not shutil.which(_s.settings.ffmpeg_path or "ffmpeg") or _is_bundled():
        return 44100
    return _s._OPL_NATIVE_RATE


def _adlib_source(path: Path) -> "tuple[Path, Path | None] | None":
    """What adplay renders for ``path`` (``_render_adlib``'s DRO v1 → v2
    rewrite) and the temp file to remove; None for an Imago Orpheus
    ``.imf`` (a tracker module).  Blocking."""
    from soniqboom.api import stream as _s
    with open(path, "rb") as fh:
        head = fh.read(64)
    if path.suffix.lower() == ".imf" and len(head) >= 64 and head[60:64] == b"IM10":
        return None
    if head[:8] != b"DBRAWOPL" or _s._dro_is_v2(head):
        return path, None
    v2 = _s._dro_v1_to_v2(path.read_bytes())
    fd, name = tempfile.mkstemp(dir=str(_s._rtmp()), suffix=".dro")
    with os.fdopen(fd, "wb") as fh:
        fh.write(v2)
    return Path(name), Path(name)


async def _run_tool(cmd: "list[str]", timeout: float) -> "tuple[int, str]":
    from soniqboom.api import stream as _s
    import asyncio.subprocess as asp
    proc = await _s._spawn_render(cmd, stdout=asp.PIPE, stderr=asp.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        _s._kill_render_proc(proc)
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except BaseException:                           # noqa: BLE001
            pass
        raise
    return proc.returncode, (out or b"").decode("utf-8", "replace")


def _tool_seconds(text: str) -> float:
    m = re.search(r"^seconds=([0-9.]+)", text, re.M)
    return float(m.group(1)) if m else 0.0


async def _opl_vu(ent: dict, wav: Path, key: str) -> "bytes | None":
    """The VUMR the oplvoices pass writes for this tune (the subsong whose
    length matches the cached render: ``_render_adlib`` plays the first
    audible one of a file whose first is empty)."""
    from soniqboom.api import stream as _s
    from soniqboom.core import openmpt_vu
    binary = _oplvoices_binary()
    if not binary:
        return None
    try:
        src = await _s._pb_at(ent["src"], _adlib_source, ent["src"])
    except (OSError, ValueError):
        return None
    if src is None:
        return None
    render_path, tmp_src = src
    work = Path(tempfile.mkdtemp(prefix="sboplvu", dir=str(_s._rtmp())))
    try:
        want_s = await asyncio.to_thread(_s._wav_audio_seconds, wav)
        if not (0 < want_s <= _MAX_TUNE_S):
            return None
        rate = _adlib_rate()
        subsong = ent["subsong"]
        tries = [subsong] + (list(range(1, _s._ADLIB_MAX_SUBSONG_PROBE + 1)) if subsong == 0 else [])
        for ss in tries:
            out = work / f"s{ss}.vu"
            cmd = [binary, "-r", str(rate), "-t", str(int(_MAX_TUNE_S) + 10), "-v", str(out)]
            if ss > 0:
                cmd += ["-s", str(ss)]
            cmd.append(str(render_path))
            async with _s._bg_render_sem.slot(_s.PRIO_VU):
                if not _still_wanted(key):
                    return None
                code, text = await _run_tool(cmd, _OPL_TIMEOUT_S)
            got = _tool_seconds(text)
            # (the resampled reference is within a frame of the native length)
            if abs(got - want_s) > 0.05:
                continue
            if code != 0 or not out.is_file():
                return None
            raw = out.read_bytes()
            openmpt_vu.parse_and_validate_vumr(raw, allow_names=True, max_frames=240 * 3600)
            _note_adlib_subsong(ent["cache_key"], subsong, ss)
            return raw
        return None
    except (OSError, ValueError, asyncio.TimeoutError):
        log.debug("oplvoices VU pass failed for %s", key, exc_info=True)
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if tmp_src is not None:
            tmp_src.unlink(missing_ok=True)


# ── the pass ────────────────────────────────────────────────────────────────

async def _run_chip_vu(key: str, ent: dict) -> None:
    from soniqboom.api import stream as _s
    from soniqboom.core import openmpt_vu
    from soniqboom.core.conversion_cache import (
        get_cached, inflight_event, pin as _pin, unpin as _unpin,
    )
    cache_key = ent["cache_key"]
    pinned = attempted = wrote = False
    if ent.get("pin"):
        _s._zip_pin(ent["pin"])
    try:
        ev = inflight_event(cache_key)
        if ev is not None:
            await ev.wait()
        wav = await get_cached(cache_key)
        if wav is None:
            return                          # not cached (yet): a later ask tries again
        vu_path = wav.with_suffix(".vu")

        def _state() -> str:
            if vu_path.exists():
                return "done"
            return "go" if Path(ent["src"]).exists() else "gone"
        st = await _s._pb_at(ent["src"], _state)
        if st != "go":
            attempted = st == "gone"        # (a source gone: say "skipped", not "loading")
            return
        _pin(cache_key)
        pinned = True
        if _START_DELAY_S > 0:
            await asyncio.sleep(_START_DELAY_S)
        if not _still_wanted(key):
            return
        attempted = True
        t0 = time.monotonic()
        if ent["family"] == "gme":
            result = await _gme_vu(ent, wav, key)
            if result is not None:
                await asyncio.to_thread(openmpt_vu.write_sidecar, vu_path, result)
                wrote = True
        else:
            raw = await _opl_vu(ent, wav, key)
            if raw is not None:
                await asyncio.to_thread(openmpt_vu.write_sidecar_bytes, vu_path, raw)
                wrote = True
        if wrote:
            log.info("chip VU: wrote the per-voice sidecar of %s (%.1f s)",
                     cache_key, time.monotonic() - t0)
        elif not _still_wanted(key):
            attempted = False               # dropped (the listener left): not a verdict
    except Exception:                                   # noqa: BLE001
        log.debug("chip VU pass failed for %s", key, exc_info=True)
    finally:
        if pinned:
            _unpin(cache_key)
        if ent.get("pin"):
            try:
                _s._zip_unpin(ent["pin"])
            except Exception:                           # noqa: BLE001
                pass
        if attempted:
            _WANTED.pop(key, None)
            if not wrote:
                _note_skipped(key)
        _INFLIGHT.discard(key)


# ── The voice mixer ─────────────────────────────────────────────────────────
#
# Mute / solo and per-voice scopes in the player (frontend/js/voices/).  The
# cached render stays the reference: muting plays ANOTHER render of the tune —
# the same engine with the muted voices switched off inside the emulation —
# which the browser plays in place of the reference from the same position
# while a voice is muted.  Scopes are renders of each voice on its own.  Both
# are made on ask, for the tune being played (``note_voice_source``), as
# temp files of this server's render folder, kept while the browser reads
# them (``_JOB_IDLE_S``), never cached:
#
#   family  mix (mute / solo)                       scope (each voice alone)
#   gme     libgme, gme_mute_voices (exact on the   libgme, one render per voice
#           reference timeline: core/gme_voices)
#   adlib   oplvoices -m (the reference mix without  oplvoices -c (each channel's
#           the muted channels' terms), resampled    share of the mix)
#           as the reference is
#   sid     sidplayfp -u<n> (the SID with those     sidplayfp, two voices muted
#           voices muted: its filter and mixer      (the per-voice meter's renders)
#           act on the voices still heard)
#
# They run in the background gate: a mix at PRIO_NEXT (it is being listened
# to), scopes at PRIO_VU (cosmetic) — never a playback slot.

MIX_RATE = 44100
_JOB_IDLE_S = 45.0
_JOBS_MAX = 8
_SLICE_S = 10.0
_WAIT_S = 8.0

# (track_id, subsong) → {"family", "cache_key", "src", "pin", "duration"}
_SOURCES: "OrderedDict[tuple, dict]" = OrderedDict()
_SOURCES_MAX = 64
_JOBS: "OrderedDict[tuple, _Job]" = OrderedDict()


def note_voice_source(family: str, track_id: str, subsong: int, cache_key: str,
                      src_path, zip_pin_id: "str | None" = None, *,
                      duration: "float | None" = None) -> None:
    """Remember what a play of ``track_id`` tune ``subsong`` rendered (the
    voice mixer renders from the same source).  A dict write."""
    k = (track_id, int(subsong))
    _SOURCES[k] = {"family": "adlib" if family == "imf" else family,
                   "cache_key": cache_key, "src": Path(src_path), "pin": zip_pin_id,
                   "duration": duration}
    _SOURCES.move_to_end(k)
    while len(_SOURCES) > _SOURCES_MAX:
        _SOURCES.popitem(last=False)


class _Job:
    """One render of the voice mixer, written to temp files as it renders.
    ``files``: (path, data offset, bytes per file frame, file frames per
    job frame); ``mix``: one file of interleaved PCM at ``rate``; ``scope``:
    one file per voice, read as mono (a file's first channel) at ``rate``."""

    def __init__(self, key: tuple, kind: str, rate: int, channels: int):
        self.key, self.kind, self.rate, self.channels = key, kind, rate, channels
        self.work = Path(tempfile.mkdtemp(prefix="sbvoices", dir=_render_dir()))
        self.files: "list[tuple[Path, int, int, int]]" = []
        self.ready = 0                # frames rendered (in-process writers)
        self.measure = False          # ready from the file sizes (subprocess writers)
        self.total: "int | None" = None
        self.done = False
        self.error: "str | None" = None
        self.last = time.monotonic()
        self.task: "asyncio.Future | None" = None

    def frames_ready(self) -> int:
        if not self.measure:
            return self.ready
        if any(f is None for f in self.files):
            return 0                    # (a render not started yet)
        n = None
        for path, off, fb, dec in self.files:
            try:
                k = max(0, (path.stat().st_size - off) // fb) // dec
            except OSError:
                k = 0
            n = k if n is None else min(n, k)
        n = n or 0
        return n if self.total is None else min(n, self.total)

    def read(self, start: int, count: int) -> bytes:
        """Frames [start, start + count): a mix interleaved, a scope one
        voice after another (planar).  Blocking."""
        import array
        out = []
        for ent in self.files:
            if ent is None:
                out.append(bytes(count * 2 * (self.channels if self.kind == "mix" else 1)))
                continue
            path, off, fb, dec = ent
            span = count * fb * dec
            try:
                with open(path, "rb") as fh:
                    fh.seek(off + start * fb * dec)
                    raw = fh.read(span).ljust(span, b"\0")
            except OSError:
                raw = bytes(span)
            if self.kind == "scope" and (dec > 1 or fb > 2):
                a = array.array("h")
                a.frombytes(raw)        # (little-endian PCM in and out: a slice keeps the bytes)
                raw = a[0::dec * (fb // 2)].tobytes()
            out.append(raw)
        return b"".join(out)

    def close(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        shutil.rmtree(self.work, ignore_errors=True)


def _render_dir() -> str:
    from soniqboom.core import render_tmp
    return str(render_tmp.tmp_dir())


def _sweep_jobs() -> None:
    now = time.monotonic()
    for k, job in list(_JOBS.items()):
        if now - job.last > _JOB_IDLE_S:
            _JOBS.pop(k, None)
            job.close()
    while len(_JOBS) > _JOBS_MAX:
        _, job = _JOBS.popitem(last=False)
        job.close()


_SWEEPER: "asyncio.Future | None" = None


def _ensure_sweeper() -> None:
    """Remove idle renders (and their temp files) while any exist: the
    browser stops asking when the listener moves on, and nothing else would
    come by to sweep."""
    global _SWEEPER
    if _SWEEPER is not None and not _SWEEPER.done():
        return

    async def run():
        while _JOBS:
            await asyncio.sleep(_JOB_IDLE_S / 3)
            _sweep_jobs()
    from soniqboom.api import stream as _s
    _SWEEPER = asyncio.ensure_future(run())
    _s._bg_keep(_SWEEPER)


def _voice_family(track) -> "str | None":
    """Which voice mixer plays this track: "gme", "adlib", "sid" or None —
    as playback routes it (``stream._render_ident``)."""
    from soniqboom.api import stream as _s
    if str(getattr(track, "format", "") or "").split("/")[0].strip() == "SID":
        return "sid"
    try:
        ext, uade_named = _s._render_ident(getattr(track, "path", "") or "", track)
    except Exception:                                   # noqa: BLE001
        return None
    if uade_named:
        return None
    if ext in _s._GME_EXTS_STREAM:
        return "gme"
    if ext in _s._ADLIB_EXTS or ext == ".imf":
        return "adlib"
    return None


def _sidecar(track_id: str, subsong: int) -> "Path | None":
    from soniqboom.core.conversion_cache import get_vu_sidecar_path
    return get_vu_sidecar_path(track_id, subsong)


def _sidecar_names(track_id: str, subsong: int) -> "list[str] | None":
    from soniqboom.core import openmpt_vu
    p = _sidecar(track_id, subsong)
    if p is None:
        return None
    try:
        return openmpt_vu.vumr_names(p.read_bytes()) or None
    except OSError:
        return None


_SID_VOICES = ["Voice 1", "Voice 2", "Voice 3"]


def _sid_voice_names(track) -> "list[str]":
    """The voices the mixer offers for a SID tune: 3 per SID chip
    (``stream.sid_chips``) — sidplayfp's ``-u1``..``-u9``."""
    from soniqboom.api import stream as _s
    chips = _s.sid_chips(track)
    if chips == 1:
        return list(_SID_VOICES)
    return [f"SID {c + 1} Voice {v + 1}" for c in range(chips) for v in range(3)]


def _sid_voice_cmds(binary: str, sid_path, track, subsong: int, dur: int, start: int,
                    kind: str, mask: int, outs: "list[str]") -> "list[list[str]]":
    """The sidplayfp commands of a SID voice job (``outs``: one WAV each).

    A mix (``kind == "mix"``) is ONE render with the voices not in ``mask``
    (bit v = voice v + 1) muted, otherwise the reference render's command
    (``stream._sid_render_cmd``): a 2SID / 3SID tune in stereo
    (``sid_stereo``) and the SID fade (``-fob``), so with every voice on it
    equals it.  A scope is one mono pass per voice, the others muted
    (``sid_vu.voice_mutes``)."""
    from soniqboom.api import stream as _s
    from soniqboom.core import sid_vu
    chips = _s.sid_chips(track)
    if kind == "mix":
        n = 3 * chips
        mute = tuple(v + 1 for v in range(n) if not mask & (1 << v))
        return [_s._sid_render_cmd(binary, sid_path, subsong, dur, outs[0], mute=mute,
                                   start=start, stereo=_s.sid_stereo(track), isolate=False)]
    return [_s._sid_render_cmd(binary, sid_path, subsong, dur, o, mute=m, start=start)
            for o, m in zip(outs, sid_vu.voice_mutes(chips))]


def _opl_channels(names: "list[str]") -> "list[int]":
    """The OPL channel (0-17) of each voice of an AdLib sidecar ("OPL <n> …")."""
    out = []
    for nm in names:
        m = re.match(r"OPL\s*(\d+)", nm or "")
        out.append(int(m.group(1)) - 1 if m else -1)
    return out


async def voices_info(track, track_id: str, subsong: int) -> dict:
    """What the voice mixer offers for this tune (``GET …/voices``)."""
    fam = _voice_family(track)
    info = {"family": fam, "voices": [], "mute": False, "scope": False,
            "rate": MIX_RATE, "scope_rate": MIX_RATE // 2, "pending": False,
            "playing": (track_id, int(subsong)) in _SOURCES, "reason": ""}
    if fam is None:
        info["reason"] = "unsupported"
        return info
    src = _SOURCES.get((track_id, int(subsong)))
    if src is None:
        info["reason"] = "not-playing"       # (the mixer renders from the play's source)
        return info
    if fam == "sid":
        from soniqboom.api import stream as _s
        if not _s._find_renderer(_s.settings.sidplayfp_path, "sidplayfp"):
            info["reason"] = "unsupported"
            return info
        info.update(voices=_sid_voice_names(track), mute=True, scope=True)
        return info
    if src["family"] == "gme" and not _family_supported(
            {"family": "gme", "cache_key": src["cache_key"], "track_id": track_id,
             "subsong": int(subsong)}):
        info["reason"] = "unsupported"
        return info
    if fam == "adlib" and _oplvoices_binary() is None:
        info["reason"] = "unsupported"
        return info
    names = await asyncio.to_thread(_sidecar_names, track_id, int(subsong))
    if not names:
        # The voices are the per-voice meter's: ask for its pass (it runs
        # only while asked, like the meter's own asks).
        reason = chip_vu_skipped_reason(track_id, int(subsong))
        if reason:
            info["reason"] = reason
        else:
            info["pending"] = request_chip_vu(track_id, int(subsong))
            if not info["pending"]:
                info["reason"] = chip_vu_skipped_reason(track_id, int(subsong)) or "unsupported"
        return info
    if fam == "adlib":
        info["scope_rate"] = _adlib_rate() // 2
    info.update(voices=names, mute=True, scope=True)
    return info


def _wav_layout(path: Path) -> "tuple[int, int, int] | None":
    """(data offset, channels, rate) of a WAV being written, once its header
    is there.  Blocking."""
    import struct
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if len(head) < 44 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos, ch, rate = 12, 0, 0
    while pos + 8 <= len(head):
        cid, size = head[pos:pos + 4], struct.unpack_from("<I", head, pos + 4)[0]
        if cid == b"fmt " and pos + 24 <= len(head):
            ch, rate = struct.unpack_from("<HI", head, pos + 10)
        if cid == b"data":
            return (pos + 8, ch, rate) if ch else None
        pos += 8 + size + (size & 1)
    return None


async def _start_job(track, track_id: str, subsong: int, kind: str, mask: int,
                     src: dict) -> _Job:
    key = (track_id, int(subsong), kind, int(mask) if kind == "mix" else 0)
    job = _JOBS.get(key)
    if job is not None:
        _JOBS.move_to_end(key)
        job.last = time.monotonic()
        return job
    _sweep_jobs()
    if kind == "mix":
        # A listener toggling voices: keep the last other mix of this tune
        # (toggling back is instant), stop the older ones.
        mine = [k for k in _JOBS if k[:3] == key[:3]]
        for k in mine[:-1]:
            _JOBS.pop(k).close()
    fam = src["family"]
    if fam == "gme":
        job = _Job(key, kind, MIX_RATE if kind == "mix" else MIX_RATE // 2,
                   2 if kind == "mix" else 0)
        job.task = asyncio.ensure_future(_gme_job(job, src, int(subsong), int(mask), track_id))
    elif fam == "adlib":
        rate = _adlib_rate()
        job = _Job(key, kind, MIX_RATE if kind == "mix" else rate // 2,
                   2 if kind == "mix" else 0)
        job.task = asyncio.ensure_future(_adlib_job(job, src, int(subsong), int(mask), track_id))
    else:
        job = _Job(key, kind, MIX_RATE if kind == "mix" else MIX_RATE // 2,
                   0 if kind == "mix" else 3)
        job.task = asyncio.ensure_future(_sid_job(job, src, track, int(subsong), int(mask)))
    _JOBS[key] = job
    _ensure_sweeper()
    return job


def _job_failed(job: _Job, exc: "BaseException | None", what: str) -> None:
    if isinstance(exc, asyncio.CancelledError):
        return
    job.error = what
    log.info("voice mixer: %s (%s)", what, job.key, exc_info=exc is not None)


async def _gme_job(job: _Job, src: dict, subsong: int, mask: int, track_id: str) -> None:
    from soniqboom.api import stream as _s
    from soniqboom.core import gme_voices
    from soniqboom.core.conversion_cache import get_cached
    renders: list = []
    fhs: list = []
    try:
        inputs = await _gme_inputs_async(src["src"], subsong)
        if inputs is None or not gme_voices.is_available():
            raise ValueError("libgme does not make this render")
        data, duration, fade_at = inputs
        wav = await get_cached(src["cache_key"])
        async with _s._bg_render_sem.slot(_s.PRIO_NEXT if job.kind == "mix" else _s.PRIO_VU):
            total = (await asyncio.to_thread(_wav_frames, wav) if wav is not None
                     else await _owned(gme_voices.reference_frames, data,
                                       subsong, duration, fade_at))
            lead = await _owned(gme_voices.lead_in_frames, data, subsong)
        if lead is None or total <= 0:
            raise ValueError("no timeline")
        fade = gme_voices.fade_ms(duration, fade_at)
        names = await asyncio.to_thread(_sidecar_names, track_id, subsong) or \
            await asyncio.to_thread(gme_voices.voice_names, data) or []
        n = len(names)
        if job.kind == "mix":
            masks = [mask]
            job.total = total
        else:
            masks = [1 << v for v in range(n)]
            job.channels = n
            job.total = total // 2
        for i, m in enumerate(masks):
            renders.append(await _owned(functools.partial(
                gme_voices.VoiceRender, data, subsong, m, fade_at_ms=fade, lead_in=lead)))
            p = job.work / f"v{i}.pcm"
            fhs.append(open(p, "wb"))
            job.files.append((p, 0, 4 if job.kind == "mix" else 2, 1))
        done = 0
        prio = _s.PRIO_NEXT if job.kind == "mix" else _s.PRIO_VU
        slice_s = _SLICE_S if job.kind == "mix" else 2.0
        while done < total:
            k = min(int(slice_s * MIX_RATE), total - done)
            k -= k % 2
            k = k or (total - done)
            async with _s._bg_render_sem.slot(prio):
                await _owned(_gme_slice, job.kind, renders, fhs, k)
            done += k
            job.ready = done if job.kind == "mix" else done // 2
        job.done = True
    except BaseException as exc:                        # noqa: BLE001
        _job_failed(job, exc, "the libgme render failed")
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        for fh in fhs:
            fh.close()
        for r in renders:
            r.close()


def _gme_slice(kind: str, renders: list, fhs: list, frames: int) -> None:
    import array
    import sys
    for r, fh in zip(renders, fhs):
        chunk = r.read(frames)
        if kind == "mix":
            fh.write(chunk)
        else:
            a = array.array("h")
            a.frombytes(chunk)
            if sys.byteorder == "big":
                a.byteswap()
            # Every second frame of the louder side (a voice panned hard to
            # one side is silent on the other).
            left, right = a[0::4], a[1::4]
            pk = lambda x: max(max(x), -min(x)) if x else 0      # noqa: E731
            side = right if pk(right) > pk(left) else left
            if sys.byteorder == "big":
                side.byteswap()
            fh.write(side.tobytes())
        fh.flush()


async def _adlib_job(job: _Job, src: dict, subsong: int, mask: int, track_id: str) -> None:
    from soniqboom.api import stream as _s
    import asyncio.subprocess as asp
    binary = _oplvoices_binary()
    tmp_src = None
    proc = None
    try:
        if not binary:
            raise ValueError("no oplvoices")
        got = await _s._pb_at(src["src"], _adlib_source, src["src"])
        if got is None:
            raise ValueError("not an AdLib tune")
        render_path, tmp_src = got
        names = await asyncio.to_thread(_sidecar_names, track_id, subsong) or []
        chans = _opl_channels(names)
        if not chans or min(chans) < 0:
            raise ValueError("no voices")
        rate = _adlib_rate()
        if _s._SOXR_OK is None:
            await asyncio.to_thread(_s._ffmpeg_has_soxr)
        # The subsong the reference rendered (its probe): the meter pass found it.
        ss = await _adlib_subsong(src, subsong)
        base = [binary, "-r", str(rate), "-t", str(int(_MAX_TUNE_S) + 10)]
        if ss > 0:
            base += ["-s", str(ss)]
        prio = _s.PRIO_NEXT if job.kind == "mix" else _s.PRIO_VU
        if job.kind == "mix":
            chmask = 0
            for v, c in enumerate(chans):
                if mask & (1 << v):
                    chmask |= 1 << c
            out = job.work / "mix.pcm"
            if rate == MIX_RATE:
                cmd = base + ["-m", str(chmask), "-w", str(out), str(render_path)]
                off = 44
                new_session = False
            else:
                import shlex
                ff = shutil.which(_s.settings.ffmpeg_path or "ffmpeg")
                tool = " ".join(shlex.quote(a) for a in base + ["-m", str(chmask), "-w", "-",
                                                                 str(render_path)])
                resample = " ".join(shlex.quote(a) for a in [
                    ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-f", "wav", "-ignore_length", "1", "-i", "pipe:0",
                    "-af", _s._opl_resample_filter(), "-c:a", "pcm_s16le",
                    "-fflags", "+bitexact", "-flags:a", "+bitexact", "-f", "s16le", str(out)])
                cmd = ["/bin/sh", "-c", f"{tool} 2>/dev/null | exec {resample}"]
                off = 0
                new_session = True
            job.files.append((out, off, 4, 1))
            job.measure = True
            async with _s._bg_render_sem.slot(prio):
                proc = await _s._spawn_render(cmd, new_session=new_session,
                                              stdout=asp.DEVNULL, stderr=asp.DEVNULL)
                await asyncio.wait_for(proc.wait(), _OPL_TIMEOUT_S)
            if proc.returncode != 0:
                raise ValueError(f"oplvoices exited {proc.returncode}")
            job.total = job.frames_ready()
            job.done = True
            return
        # scope: every channel's share, every second sample, the voices' channels
        job.channels = len(chans)
        fhs = []
        for v in range(len(chans)):
            p = job.work / f"v{v}.pcm"
            fhs.append(open(p, "wb"))
            job.files.append((p, 0, 2, 1))
        frame = 18 * 2
        try:
            async with _s._bg_render_sem.slot(prio):
                proc = await _s._spawn_render(base + ["-c", "-", str(render_path)],
                                              stdout=asp.PIPE, stderr=asp.DEVNULL)
                pend = b""
                frames = 0
                while True:
                    blob = await proc.stdout.read(frame * 2 * 4096)
                    if not blob:
                        break
                    pend += blob
                    use = len(pend) - len(pend) % (frame * 2)
                    if use:
                        frames += await _owned(_opl_scope_slice, pend[:use], chans, fhs)
                        pend = pend[use:]
                        job.ready = frames
                await asyncio.wait_for(proc.wait(), 30)
        finally:
            for fh in fhs:
                fh.close()
        job.total = job.ready
        job.done = True
    except BaseException as exc:                        # noqa: BLE001
        if proc is not None and proc.returncode is None:
            _s._kill_render_proc(proc)
        _job_failed(job, exc, "the AdLib voice render failed")
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        if tmp_src is not None:
            tmp_src.unlink(missing_ok=True)


def _opl_scope_slice(blob: bytes, chans: "list[int]", fhs: list) -> int:
    import array
    import sys
    a = array.array("h")
    a.frombytes(blob)
    if sys.byteorder == "big":
        a.byteswap()
    n = 0
    for c, fh in zip(chans, fhs):
        col = a[c::36]                  # channel c of every second frame
        if sys.byteorder == "big":
            col.byteswap()
        fh.write(col.tobytes())
        fh.flush()
        n = len(col)
    return n


_ADLIB_SUBSONG: "OrderedDict[tuple, int]" = OrderedDict()


def _note_adlib_subsong(cache_key: str, subsong: int, played: int) -> None:
    _ADLIB_SUBSONG[(cache_key, subsong)] = played
    _ADLIB_SUBSONG.move_to_end((cache_key, subsong))
    while len(_ADLIB_SUBSONG) > 64:
        _ADLIB_SUBSONG.popitem(last=False)


async def _adlib_subsong(src: dict, subsong: int) -> int:
    """The subsong the reference render of this tune played: ``subsong``, or
    for an unpinned play of a file whose first subsong is empty the one
    ``_render_adlib``'s probe chose — the one whose length is the cached
    render's (found by the meter pass, else here)."""
    from soniqboom.api import stream as _s
    from soniqboom.core.conversion_cache import get_cached
    if subsong != 0:
        return subsong
    k = (src["cache_key"], subsong)
    if k in _ADLIB_SUBSONG:
        return _ADLIB_SUBSONG[k]
    wav = await get_cached(src["cache_key"])
    binary = _oplvoices_binary()
    if wav is None or not binary:
        return 0
    want = await asyncio.to_thread(_s._wav_audio_seconds, wav)
    got = await _s._pb_at(src["src"], _adlib_source, src["src"])
    if got is None:
        return 0
    render_path, tmp = got
    try:
        for ss in range(0, _s._ADLIB_MAX_SUBSONG_PROBE + 1):
            cmd = [binary, "-r", str(_adlib_rate()), "-t", str(int(_MAX_TUNE_S) + 10),
                   "-w", os.devnull]
            if ss:
                cmd += ["-s", str(ss)]
            async with _s._bg_render_sem.slot(_s.PRIO_NEXT):
                code, text = await _run_tool(cmd + [str(render_path)], _OPL_TIMEOUT_S)
            if abs(_tool_seconds(text) - want) <= 0.05:
                _note_adlib_subsong(src["cache_key"], subsong, ss)
                return ss
        return 0
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


async def _sid_job(job: _Job, src: dict, track, subsong: int, mask: int) -> None:
    from soniqboom.api import stream as _s
    from soniqboom.core import sid_vu
    binary = _s._find_renderer(_s.settings.sidplayfp_path, "sidplayfp")
    tasks: list = []
    try:
        if not binary:
            raise ValueError("no sidplayfp")
        dur = int(src.get("duration") or _s._sid_target_seconds(track, subsong))
        start = await _s._psid_start_song(src["src"]) if subsong > 0 else 1
        n = 1 if job.kind == "mix" else len(sid_vu.voice_mutes(_s.sid_chips(track)))
        prio = _s.PRIO_NEXT if job.kind == "mix" else _s.PRIO_VU
        outs = [job.work / f"v{i}.wav" for i in range(n)]
        cmds = _sid_voice_cmds(binary, src["src"], track, subsong, dur, start, job.kind,
                               mask, [str(o) for o in outs])

        async def one(cmd, out):
            async with _s._bg_render_sem.slot(prio):
                await _s._await_renderer(cmd, out, timeout=dur + 60, kind="SID-voices",
                                         require_audio=False)
        tasks = [asyncio.ensure_future(one(c, o)) for c, o in zip(cmds, outs)]
        # Each WAV joins once sidplayfp has written its header — the renders
        # may wait for background slots one after another; until all three
        # have started, a scope has no frames (``frames_ready``).
        job.files = [None] * len(outs)
        job.measure = True
        job.total = dur * MIX_RATE if job.kind == "mix" else dur * MIX_RATE // 2
        while None in job.files:
            for i, o in enumerate(outs):
                if job.files[i] is None:
                    lay = await asyncio.to_thread(_wav_layout, o)
                    if lay:
                        if job.kind == "mix":
                            job.channels = lay[1]
                            job.files[i] = (o, lay[0], 2 * lay[1], 1)
                        else:
                            # (a scope: the first channel of every second frame)
                            job.files[i] = (o, lay[0], 2 * lay[1], 2)
                    elif tasks[i].done():
                        raise ValueError("sidplayfp wrote no audio")
            if None in job.files:
                await asyncio.sleep(0.1)
        await asyncio.gather(*tasks)
        job.done = True
    except BaseException as exc:                        # noqa: BLE001
        _job_failed(job, exc, "the SID voice render failed")
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        # (a stopped job: its sidplayfp runs end with it — _await_renderer kills them)
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# ── HTTP ────────────────────────────────────────────────────────────────────

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response  # noqa: E402

from soniqboom.api.users import require_user as _require_user  # noqa: E402

router = APIRouter(prefix="/tracks", tags=["voices"])


async def _resolve(track_id: str, subsong: "int | None", request: "Request | None"):
    from soniqboom.api import stream as _s
    from soniqboom.api.tracks import _as_played
    from soniqboom.core.data import get_track
    track_id = _as_played(track_id)
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    sub = _s.tune_index(track_id, track,
                        _s.explicit_wire(subsong if isinstance(subsong, int) else None, request))
    return track_id, track, sub


@router.get("/{track_id}/voices")
async def get_voices(track_id: str, subsong: "int | None" = Query(None, ge=0, le=1024),
                     request: Request = None, _user=Depends(_require_user)):
    """What the voice mixer offers for a tune: ``family`` ("gme", "adlib",
    "sid" or null), its ``voices`` (names, in the per-voice meter's order),
    whether ``mute`` / ``scope`` work, the PCM ``rate`` and ``scope_rate``,
    ``pending`` (the voices are being read — ask again) and ``reason`` when
    nothing is offered ("unsupported", "skipped", "not-playing": play the
    tune first)."""
    track_id, track, sub = await _resolve(track_id, subsong, request)
    out = await voices_info(track, track_id, sub)
    return Response(content=json.dumps(out), media_type="application/json",
                    headers={"Cache-Control": "no-store"})


@router.get("/{track_id}/voices/pcm")
async def get_voice_pcm(track_id: str,
                        kind: str = Query("mix", pattern="^(mix|scope)$"),
                        mask: int = Query(0, ge=0, le=0xFFFFFFFF),
                        start: int = Query(0, ge=0, le=MIX_RATE * 3600),
                        count: int = Query(MIX_RATE, ge=1, le=MIX_RATE * 20),
                        subsong: "int | None" = Query(None, ge=0, le=1024),
                        request: Request = None, _user=Depends(_require_user)):
    """PCM of the voice mixer's renders of a tune being played: ``kind=mix``
    — the tune with only the voices in ``mask`` (bit v = voice v) heard,
    16-bit little-endian, interleaved, at ``X-Voices-Rate`` with
    ``X-Voices-Channels``; ``kind=scope`` — each voice alone, mono 16-bit at
    the scope rate, one voice after another.  Frames ``start`` …
    ``start + count`` of the render (fewer at its end, or what is rendered
    so far after a wait).  202 while the render hasn't reached ``start``
    (``X-Voices-Ready``: frames rendered), 204 past its end, 409 for a tune
    this server isn't playing, 422 when the render failed."""
    track_id, track, sub = await _resolve(track_id, subsong, request)
    src = _SOURCES.get((track_id, sub))
    if src is None or _voice_family(track) is None:
        raise HTTPException(409, "Play this tune first")
    job = await _start_job(track, track_id, sub, kind, mask, src)
    deadline = time.monotonic() + _WAIT_S
    while True:
        job.last = time.monotonic()
        if job.error:
            if _JOBS.get(job.key) is job:
                _JOBS.pop(job.key, None)
                job.close()
            raise HTTPException(422, job.error)
        ready = job.frames_ready()
        if ready >= start + count or job.done:
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.05)
    ready = job.frames_ready()
    hdr = {"Cache-Control": "no-store", "X-Voices-Rate": str(job.rate),
           "X-Voices-Channels": str(job.channels), "X-Voices-Ready": str(ready),
           "X-Voices-Done": "1" if job.done else "0"}
    if job.total is not None:
        hdr["X-Voices-Total"] = str(job.total)
    if ready <= start:
        return Response(status_code=204 if job.done else 202, headers=hdr)
    n = min(count, ready - start)
    data = await asyncio.to_thread(job.read, start, n)
    hdr.update({"X-Voices-Start": str(start), "X-Voices-Frames": str(n)})
    return Response(content=data, media_type="application/octet-stream", headers=hdr)
