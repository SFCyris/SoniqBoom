# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loudness matching across engines — EBU R128 measured by ffmpeg ``ebur128``.

A SID render, a tracker render, an SPC rip and a mastered FLAC come out of
their engines at very different levels.  Files with ReplayGain tags are
levelled by them; everything else gets its loudness MEASURED here and stored
on its track record, from where the web player (``utils.js levelingGain``)
and the Subsonic API (``replayGain``) level it the same way ReplayGain does.

What is measured
----------------
* **Renders** — the moment a render lands in the conversion cache
  (``conversion_cache.on_stored`` → ``note_render_stored``), or one cached
  before is read (``on_hit``): the cached WAV
  IS what plays (the tune's played length, a looping tune's loops and fade,
  the 512 s uade cap), so its integrated loudness is the tune's.  One tune
  of a multi-tune file at a time, keyed like the render (``tune_key``).
* **Native files** without ReplayGain tags (MP3, FLAC, Ogg, AAC/ALAC, DSD …)
  — by an idle background pass (``native_pass``) over the local library,
  and for the track the web player plays next (``request_native``, from its
  look-ahead prewarm).  Remote shares — also one the OS mounted as a folder
  (``net_mounts``) — and online collections are never read for it.  A
  failed attempt is retried after a week, a file that wasn't there after
  6 hours.

Every measurement runs behind the background render gate
(``stream._bg_render_sem``) — a render that was played or prepared at
``PRIO_AHEAD``, a cold duration-probe render and the native pass at
``PRIO_VU`` (the lowest: loudness is a refinement; the next track, further
look-ahead, the on-screen duration probes and the VU sidecars all go
first) — and the native pass only while no render runs at all.  ffmpeg runs
at ``nice 19`` where ``nice`` exists.

What is stored
--------------
``loudness`` (an optional track field — old code ignores it): ``{tune key:
[I, TP]}`` or ``[I, TP, SP]`` — I the integrated loudness in LUFS (0.1 LU,
ebur128's summary precision), TP the true peak and SP (only when above full
scale, a 32-bit float render) the sample peak, both linear (1.0 = full
scale).  ``loudness_source`` = ``"analysed"``; ``loudness_v`` the render
settings measured under (``variant_of``).  Written fill-only, batched,
through ``store.update_track_fields_batch`` — so journaled in the AOF like
every field write — and never for a track whose file carries ReplayGain
tags (tags win).  A tune is measured ONCE per render settings: sidplayfp's
random power-on state makes two renders of a SID differ slightly, so a
re-render under the same settings (an evicted cache entry) is not measured
again; a change of them (chip model, soundfont, …) is.  A changed file loses it (its re-extract is a new record);
an unchanged one keeps it across a rescan (``store._carry_loudness``) and a
move (``track_moves._LEARNED_FIELDS``).

The tune key is the tune index the render is keyed by (``stream.tune_index``):
the WIRE for SID / SNDH (wire 0 = the file's default tune), the 0-based tune
index for every other multi-tune family (a bare play: the file's default
tune), "0" for a one-tune file.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import math
import os
import re
import shutil
import subprocess
import time
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger(__name__)

# ReplayGain 2.0's reference loudness: the gain of a track is REF_LUFS − I.
REF_LUFS = -18.0
FIELD = "loudness"
SOURCE_FIELD = "loudness_source"
VARIANT_FIELD = "loudness_v"        # the render settings measured under (``variant_of``)
SOURCE = "analysed"
# ebur128 reports a gated-out (silent) programme as -70.0 LUFS.
_SILENT_LUFS = -69.95
# Integrated loudness believed between these (a render of noise can't pass 0).
_MIN_LUFS, _MAX_LUFS = -69.9, 6.0
# The ReplayGain tag fields whose presence means "the file is levelled by its
# own tags" — never measured, never overridden.
TAG_FIELDS = ("replaygain_track_gain", "replaygain_album_gain")

# Cache format types whose entries are renders of the tune itself (a
# transcode — of a render or a native file — is not measured: its loudness
# is its source's).
_RENDER_TYPES = frozenset(("sid", "midi", "tracker", "uade", "hvl", "sndh", "sc68",
                           "gme", "adlib", "imf", "ym", "psf"))


# ── Measurement (blocking) ───────────────────────────────────────────────────

def _ffmpeg() -> str:
    try:
        from soniqboom.config import settings
        return settings.ffmpeg_path or "ffmpeg"
    except Exception:                                   # noqa: BLE001
        return "ffmpeg"


_NICE: "str | None | bool" = False


def _nice() -> "str | None":
    """The ``nice`` binary (None where there is none) — found once."""
    global _NICE
    if _NICE is False:
        _NICE = shutil.which("nice")
    return _NICE or None


def measure_args(path: "Path | str", *, pre_filters: "tuple[str, ...]" = (),
                 max_seconds: "float | None" = None, ffmpeg: "str | None" = None,
                 niced: bool = True) -> list[str]:
    """The ffmpeg command that measures ``path``: decode it (the first
    ``max_seconds`` only, when given), run ``pre_filters`` (the decode chain
    the server serves the format with — DSD's), then ``ebur128`` with the
    true and sample peaks, its summary only (no per-frame log)."""
    chain = ",".join((*pre_filters, "ebur128=peak=true+sample:framelog=quiet"))
    cmd = [ffmpeg or _ffmpeg(), "-hide_banner", "-nostdin", "-nostats", "-vn", "-sn", "-dn",
           "-i", str(path)]
    if max_seconds and max_seconds > 0:
        cmd += ["-t", f"{float(max_seconds):.3f}"]
    cmd += ["-af", chain, "-f", "null", "-"]
    nice = _nice() if niced else None
    return [nice, "-n", "19", *cmd] if nice else cmd


_SECTION_RE = re.compile(r"^\s*(Integrated loudness|Loudness range|True peak|Sample peak):\s*$")
_VALUE_RE = re.compile(r"^\s*(I|Peak):\s*(-?inf|-?\d+(?:\.\d+)?)\s*(LUFS|dBFS)\s*$")


def parse_summary(text: str) -> "dict | None":
    """``{"i": LUFS, "tp": linear, "sp": linear}`` from the ``Summary:`` block
    ffmpeg's ebur128 prints at its end (its LAST one); None when there is
    none, or the programme is silent (gated out: -70 LUFS) or unreadable."""
    if not text:
        return None
    pos = text.rfind("Summary:")
    if pos < 0:
        return None
    out: dict = {}
    section = None
    for line in text[pos:].splitlines()[1:]:
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1)
            continue
        m = _VALUE_RE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        val = -math.inf if raw.endswith("inf") else float(raw)
        if key == "I" and section == "Integrated loudness":
            out["i"] = val
        elif key == "Peak" and section == "True peak":
            out["tp"] = 0.0 if val == -math.inf else 10 ** (val / 20.0)
        elif key == "Peak" and section == "Sample peak":
            out["sp"] = 0.0 if val == -math.inf else 10 ** (val / 20.0)
    i = out.get("i")
    if i is None or not math.isfinite(i) or i <= _SILENT_LUFS or not _MIN_LUFS <= i <= _MAX_LUFS:
        return None
    tp = out.get("tp")
    if not tp or not math.isfinite(tp) or tp <= 0:
        return None
    sp = out.get("sp")
    if sp is None or not math.isfinite(sp) or sp <= 0:
        sp = tp
    return {"i": i, "tp": tp, "sp": sp}


def measure(path: "Path | str", *, pre_filters: "tuple[str, ...]" = (),
            max_seconds: "float | None" = None, timeout: float = 600.0) -> "dict | None":
    """Measure ``path`` (``measure_args``) and return ``parse_summary``'s
    result — None when ffmpeg isn't there, fails, times out or finds only
    silence.  Blocking: run it in a worker thread."""
    from soniqboom.core import forksafe
    cmd = measure_args(path, pre_filters=pre_filters, max_seconds=max_seconds)
    try:
        r = forksafe.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.debug("loudness: ffmpeg could not measure %s: %s", path, exc)
        return None
    err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else (r.stderr or "")
    if r.returncode != 0:
        log.debug("loudness: ffmpeg exited %s on %s: %s", r.returncode, path, err[-300:])
        return None
    return parse_summary(err)


def entry_of(m: dict) -> list:
    """The stored form of a measurement: ``[I, TP]``, with SP appended when
    the sample peak is above full scale (a float render the server turns
    down by it — ``stream.render_gain``)."""
    out = [round(float(m["i"]), 1), round(float(m["tp"]), 4)]
    sp = float(m.get("sp") or 0.0)
    if sp > 1.0:
        out.append(round(sp, 4))
    return out


def valid_entry(e) -> bool:
    """Is ``e`` a stored entry (``entry_of``'s shape, finite numbers)?"""
    return (isinstance(e, (list, tuple)) and len(e) in (2, 3)
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) for v in e)
            and _MIN_LUFS <= e[0] <= _MAX_LUFS and e[1] > 0)


# ── Reading what is stored ──────────────────────────────────────────────────

def _field(t, name: str):
    if t is None:
        return None
    if isinstance(t, dict):
        return t.get(name)
    return getattr(t, name, None)


def has_tag_gain(t) -> bool:
    """Does the track's file carry ReplayGain (a track or album gain)?"""
    for f in TAG_FIELDS:
        v = _field(t, f)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            return True
    return False


def tune_entry(t, key) -> "list | None":
    """The stored measurement of tune ``key`` (the render's tune index) of
    track ``t``, or None."""
    m = _field(t, FIELD)
    if not isinstance(m, dict):
        return None
    e = m.get(str(int(key)) if isinstance(key, int) else str(key))
    return list(e) if valid_entry(e) else None


def gain_db(entry) -> float:
    """The ReplayGain-style gain (dB) of a stored entry: ``REF_LUFS − I``."""
    return REF_LUFS - float(entry[0])


def served_gain_peak(entry) -> "tuple[float, float]":
    """``(gain dB, peak)`` of the audio a transcode of the render carries: a
    float render above full scale is transcoded turned down by its sample
    peak (``stream.render_gain`` = 1 / SP), which raises the gain a client
    must apply by as much and lowers the peak it sees."""
    g, tp = gain_db(entry), float(entry[1])
    sp = float(entry[2]) if len(entry) > 2 else 0.0
    if sp > 1.0:
        g += 20.0 * math.log10(sp)
        tp /= sp
    return g, tp


# ── Tune keys ───────────────────────────────────────────────────────────────

def header_tuned(t) -> bool:
    """SID / SNDH: their renders (and tune keys) are keyed by the wire."""
    return str(_field(t, "format") or "").split("/")[0].strip().upper() in ("SID", "SNDH")


def bare_key(t) -> str:
    """The tune key of a BARE play (no tune named — a Subsonic bare id): 0
    for SID / SNDH (wire 0, the file's default tune), else the file's
    decided default tune (``default_subsong``), else 0."""
    if header_tuned(t):
        return "0"
    z = _field(t, "default_subsong")
    return str(z) if isinstance(z, int) and not isinstance(z, bool) and z >= 0 else "0"


def subsonic_replay_gain(t, wire: "int | None" = None) -> "tuple[float, float] | None":
    """``(trackGain, trackPeak)`` for a Subsonic song (``wire`` None: the bare
    id; else the tune ``<id>~<wire>``) of a track without ReplayGain tags,
    from its measured loudness (``served_gain_peak`` — what a transcode
    carries), rounded as the tags are; None when nothing is measured."""
    if has_tag_gain(t):
        return None
    e = tune_entry(t, bare_key(t) if not wire else str(int(wire)))
    if e is None:
        return None
    g, tp = served_gain_peak(e)
    return round(g, 2), round(tp, 6)


def key_tune(cache_key: str) -> "tuple[str, int] | None":
    """``(track id, tune)`` of a render's cache key — ``<id>__sub<N>__…`` is
    tune N, a key without a ``sub`` segment (PSF-route rips, MIDI) tune 0;
    None for a key that isn't a track's."""
    if not cache_key:
        return None
    tid, sep, rest = cache_key.partition("__")
    if not tid:
        return None
    if not sep:
        return tid, 0
    head = rest.split("__", 1)[0]
    if head.startswith("sub"):
        try:
            return tid, max(0, int(head[3:]))
        except ValueError:
            return tid, 0
    return tid, 0


# ── Where results go ────────────────────────────────────────────────────────
# A result for a track the store doesn't hold (gone meanwhile) is kept in
# memory only, bounded — the next play's render-status still reports it.
_MEMO: "OrderedDict[tuple, list]" = OrderedDict()
_MEMO_MAX = 4096
# (track id, tune key, file version) whose measurement found nothing to keep
# (silence, an unreadable file) → monotonic time before which it isn't tried
# again.  Bounded (memory is no constraint: ~150 B an entry).
_FAILED: "OrderedDict[tuple, float]" = OrderedDict()
_FAILED_MAX = 500_000
_FAILED_RETRY_S = 7 * 24 * 3600.0       # silence / a file ffmpeg can't read
_MISSING_RETRY_S = 6 * 3600.0           # a file that wasn't there
# Pending store writes (track id → {"v": render variant or None, "add": {tune
# key: entry}, "replace": whether they replace the measurements stored under
# other render settings}), flushed in batches.
_PENDING: dict[str, dict] = {}
_FLUSH_S = 2.0
_flush_handle: "asyncio.TimerHandle | None" = None


def memo_entry(track_id: str, key) -> "list | None":
    """A measurement known in this run but not (yet) on the record."""
    pend = _PENDING.get(track_id)
    if pend and str(key) in pend["add"]:
        return list(pend["add"][str(key)])
    got = _MEMO.get((track_id, str(key)))
    return list(got) if got else None


def entry_for(track_id: str, t, key) -> "list | None":
    """The tune's latest measurement: one made in this run and not written
    yet, else the record's."""
    return memo_entry(track_id, key) or tune_entry(t, key)


def _version(t) -> tuple:
    return (_field(t, "mtime"), _field(t, "file_size"))


def _note_failed(track_id: str, key, t, *, retry_s: "float | None" = None) -> None:
    """Remember a measurement that found nothing (silence, ffmpeg refused the
    file) — retried after ``_FAILED_RETRY_S`` — or a file that wasn't there
    (``retry_s``: an unplugged drive, retried sooner)."""
    k = (track_id, str(key), _version(t))
    _FAILED[k] = time.monotonic() + (_FAILED_RETRY_S if retry_s is None else retry_s)
    _FAILED.move_to_end(k)
    while len(_FAILED) > _FAILED_MAX:
        _FAILED.popitem(last=False)


def _failed(track_id: str, key, t) -> bool:
    until = _FAILED.get((track_id, str(key), _version(t)))
    return until is not None and time.monotonic() < until


def _store():
    from soniqboom.core.store import get_store
    return get_store()


def variant_of(cache_key: str) -> str:
    """The render settings a render's cache key names — every segment after
    the track id but the tune (``sub<N>``) and a SID's length (``dur<N>``):
    the SID chip model / filter, the MIDI soundfont, a renderer's revision or
    variant … — as 8 hex digits ("" when it names none).  A tune measured
    under other settings is measured again: a soundfont or chip-model change
    can move a tune's loudness by several dB, while a re-render under the
    SAME settings (a SID's random power-on state) is not measured again."""
    parts = [p for p in (cache_key or "").split("__")[1:]
             if not (p.startswith("sub") or p.startswith("dur"))]
    if not parts:
        return ""
    import hashlib
    return hashlib.blake2b("__".join(parts).encode(), digest_size=4).hexdigest()


def _stored_variant(t) -> str:
    v = _field(t, VARIANT_FIELD)
    return v if isinstance(v, str) else ""


def _current_entry(track_id: str, t, key, variant: "str | None") -> "list | None":
    """The measurement of tune ``key`` that holds under render settings
    ``variant`` (None: whatever they were) — pending, stored, or (a track the
    store doesn't hold) in memory — else None."""
    k = str(key)
    pend = _PENDING.get(track_id)
    if pend is not None:
        if variant is not None and pend["v"] not in (None, variant):
            return None                       # (pending under other settings)
        if k in pend["add"]:
            return list(pend["add"][k])
        if pend["replace"]:
            return None                       # (the stored ones are other settings')
    e = tune_entry(t, k)
    if e is not None:
        if variant is not None and _stored_variant(t) != variant:
            return None
        return e
    got = _MEMO.get((track_id, k))
    return list(got) if got else None


def wanted(track_id: str, key, t=None, variant: "str | None" = None) -> bool:
    """Should tune ``key`` of ``track_id`` be measured: a record without
    ReplayGain tags, nothing measured for that tune yet — for a render
    (``variant``: ``variant_of`` its key) nothing under these render
    settings — and no recent failed attempt on this file version."""
    if t is None:
        t = _store().get_track(track_id)
    if t is None or has_tag_gain(t):
        return False
    if _current_entry(track_id, t, key, variant) is not None:
        return False
    return not _failed(track_id, key, t)


def record(track_id: str, key, entry: list, variant: "str | None" = None) -> bool:
    """Store a measurement of tune ``key``: fill-only — a tune measured once
    keeps its value, and nothing is stored on a record with ReplayGain tags —
    except that a render's measurement under other render settings
    (``variant``, see ``variant_of``) replaces the track's measurements made
    under the old ones.  Batched: the write lands within ``_FLUSH_S``
    (``flush``).  Event loop only."""
    global _flush_handle
    if not valid_entry(entry):
        return False
    k = str(key)
    t = _store().get_track(track_id)
    if t is None:
        _MEMO[(track_id, k)] = list(entry)
        _MEMO.move_to_end((track_id, k))
        while len(_MEMO) > _MEMO_MAX:
            _MEMO.popitem(last=False)
        return True
    if has_tag_gain(t):
        return False
    pend = _PENDING.get(track_id)
    if pend is not None and variant is not None and pend["v"] not in (None, variant):
        _PENDING.pop(track_id, None)          # (measured under settings since changed)
        pend = None
    if _current_entry(track_id, t, k, variant) is not None:
        return False
    if pend is None:
        stale = (variant is not None and bool(_field(t, FIELD))
                 and _stored_variant(t) != variant)
        pend = _PENDING[track_id] = {"v": variant, "add": {}, "replace": stale}
    pend["add"][k] = list(entry)
    if _flush_handle is None:
        try:
            _flush_handle = asyncio.get_running_loop().call_later(_FLUSH_S, flush)
        except RuntimeError:
            flush()
    return True


def flush() -> int:
    """Write the pending measurements in one batched store update
    (``update_track_fields_batch``: one AOF record).  Returns the number of
    tracks written."""
    global _flush_handle
    _flush_handle = None
    if not _PENDING:
        return 0
    items = list(_PENDING.items())
    _PENDING.clear()
    store = _store()
    writes = []
    for tid, pend in items:
        t = store.get_track(tid)
        if t is None or has_tag_gain(t):
            continue
        cur = t.get(FIELD) if isinstance(t.get(FIELD), dict) else {}
        merged = {} if pend["replace"] else dict(cur)
        for k, e in pend["add"].items():
            if pend["replace"] or not valid_entry(merged.get(k)):
                merged[k] = e
        fields = {FIELD: merged, SOURCE_FIELD: SOURCE}
        if pend["v"] is not None:
            fields[VARIANT_FIELD] = pend["v"] or None
        if merged != cur or fields.get(VARIANT_FIELD, t.get(VARIANT_FIELD)) != t.get(VARIANT_FIELD):
            writes.append((tid, fields))
    if not writes:
        return 0
    try:
        return store.update_track_fields_batch(writes)
    except Exception:                                   # noqa: BLE001
        log.warning("loudness: store write failed", exc_info=True)
        return 0


# ── The measuring queue ─────────────────────────────────────────────────────
# One worker measures one file at a time, the most urgent job first (lowest
# gate priority; FIFO within one), each under a ``_bg_render_sem`` slot of
# its priority.  Jobs are deduplicated per (track, tune).
_QUEUE: list = []                         # heap of (prio, seq, job dict)
_QUEUED: dict[tuple, dict] = {}           # (track id, tune key) → its job
_SEQ = itertools.count()
_QUEUE_MAX = 5000
_WORKER: "asyncio.Task | None" = None
_WAKE: "asyncio.Event | None" = None
# Wall-clock cap per job; a 512 s uade render takes ~2.5 s at ~200× realtime.
_JOB_TIMEOUT_S = 900.0


def _enqueue(job: dict, prio: int) -> bool:
    k = (job["track_id"], str(job["key"]))
    old = _QUEUED.get(k)
    if old is not None:
        if prio < old["prio"]:
            old["prio"] = prio            # (a more urgent ask: re-pushed below)
            heapq.heappush(_QUEUE, (prio, next(_SEQ), old))
        return False
    if len(_QUEUED) >= _QUEUE_MAX:
        return False
    job["prio"] = prio
    _QUEUED[k] = job
    heapq.heappush(_QUEUE, (prio, next(_SEQ), job))
    _ensure_worker()
    return True


def _ensure_worker() -> None:
    global _WORKER, _WAKE
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _WAKE is None or getattr(_WAKE, "_sb_loop", None) is not loop:
        _WAKE = asyncio.Event()
        _WAKE._sb_loop = loop             # type: ignore[attr-defined]
    _WAKE.set()
    if _WORKER is None or _WORKER.done() or _WORKER.get_loop() is not loop:
        _WORKER = loop.create_task(_worker(), name="loudness_worker")


def _pop() -> "dict | None":
    while _QUEUE:
        prio, _seq, job = heapq.heappop(_QUEUE)
        k = (job["track_id"], str(job["key"]))
        if _QUEUED.get(k) is not job or job.get("done") or prio != job["prio"]:
            continue                      # (superseded entry of a re-prioritised job)
        return job
    return None


async def _worker() -> None:
    from soniqboom.api import stream
    while True:
        job = _pop()
        if job is None:
            _WAKE.clear()
            try:
                await asyncio.wait_for(_WAKE.wait(), timeout=300.0)
            except asyncio.TimeoutError:
                if not _QUEUE:
                    return                # (idle: the next job starts a worker again)
            continue
        try:
            async with stream._bg_render_sem.slot(job["prio"]):
                await _run_job(job)
        except asyncio.CancelledError:
            raise
        except Exception:                                   # noqa: BLE001
            log.debug("loudness: job failed", exc_info=True)
        finally:
            job["done"] = True
            k = (job["track_id"], str(job["key"]))
            if _QUEUED.get(k) is job:
                _QUEUED.pop(k, None)


async def _run_job(job: dict) -> None:
    from soniqboom.core import conversion_cache as cc
    tid, key = job["track_id"], job["key"]
    t = _store().get_track(tid)
    if t is not None and not wanted(tid, key, t, job.get("variant")):
        return
    pin_key = job.get("pin")
    if pin_key:
        cc.pin(pin_key)
    try:
        path = Path(job["path"])
        if job.get("native") and on_network_mount(str(path),
                                                  await asyncio.to_thread(net_mounts)):
            return                            # (an OS-mounted share: never read for this)
        if not await asyncio.to_thread(path.is_file):
            _note_failed(tid, key, t, retry_s=_MISSING_RETRY_S)
            return
        m = await asyncio.wait_for(
            asyncio.to_thread(measure, path, pre_filters=tuple(job.get("filters") or ()),
                              timeout=_JOB_TIMEOUT_S),
            timeout=_JOB_TIMEOUT_S + 30)
    except asyncio.TimeoutError:
        m = None
    finally:
        if pin_key:
            cc.unpin(pin_key)
    if m is None:
        _note_failed(tid, key, t)
        return
    record(tid, key, entry_of(m), job.get("variant"))
    log.debug("loudness: %s tune %s = %.1f LUFS, TP %.3f", tid, key, m["i"], m["tp"])


# ── Renders ─────────────────────────────────────────────────────────────────

def note_render_stored(cache_key: str, format_type: str, path: "Path | str", *,
                       cold: bool = False) -> None:
    """``conversion_cache.on_stored`` / ``on_hit``: a render of a tune has
    just been cached at ``path``, or a cached one was read (a render cached
    before this version, or one whose earlier measurement was lost) —
    measure it, unless its track has ReplayGain tags or the tune is measured
    already under these render settings (``variant_of``).  A render that was
    played or prepared for a play goes at ``PRIO_AHEAD``; a cold one (a
    duration probe of a row on screen) at ``PRIO_VU``.  Never raises; O(1)
    on the event loop."""
    try:
        if format_type not in _RENDER_TYPES:
            return
        kt = key_tune(cache_key)
        if kt is None:
            return
        tid, key = kt
        variant = variant_of(cache_key)
        if not wanted(tid, key, None, variant):
            return
        from soniqboom.api import stream
        _enqueue({"track_id": tid, "key": key, "path": str(path), "pin": cache_key,
                  "variant": variant},
                 stream.PRIO_VU if cold else stream.PRIO_AHEAD)
    except Exception:                                   # noqa: BLE001
        log.debug("loudness: render hook failed", exc_info=True)


# ── Native files ────────────────────────────────────────────────────────────

def _native_filters(path: str) -> tuple:
    """The decode chain the server serves a native format with, which the
    measurement must hear too: DSD's (``stream._ffmpeg_encode_args``)."""
    from soniqboom.api import stream
    if Path(path).suffix.lower() in stream._DSD_EXTS:
        return ("highpass=f=20", "lowpass=f=40000", "volume=-6dB")
    return ()


# Native (not rendered) audio — what ffmpeg decodes, the browser plays or the
# server converts (``metadata.SUPPORTED_EXTENSIONS``' mainstream part).  A
# ``.dsf`` is DSD audio unless the scan named it a Dreamcast rip.
_NATIVE_EXTS = frozenset((
    ".mp3", ".flac", ".m4a", ".aac", ".mp4", ".ogg", ".oga", ".opus", ".aiff", ".aif",
    ".wav", ".wv", ".mpc", ".wma", ".mp1", ".mp2", ".m2a", ".ape", ".tta",
    ".dsf", ".dff", ".wsd", ".ac3", ".eac3", ".dts", ".thd", ".mlp", ".truehd",
))


# Network filesystems an OS mount puts a share under (a library folder on an
# SMB / NFS / AFP / WebDAV mount): its files are not read for a measurement —
# that would download the whole share.  The mount table is read once per
# sweep (Linux ``/proc/self/mounts``; macOS ``mount``'s list, which comes from
# the kernel's table — no share is contacted).
_NET_FS = frozenset(("smbfs", "cifs", "smb3", "smb", "nfs", "nfs4", "afpfs", "webdav",
                     "davfs", "fuse.davfs", "sshfs", "fuse.sshfs", "fuse.rclone", "9p",
                     "ceph", "glusterfs", "fuse.glusterfs", "fuse.s3fs", "ftpfs",
                     "fuse.curlftpfs"))
_NET_MOUNTS: "tuple[str, ...] | None" = None
_NET_MOUNTS_AT = 0.0
_NET_MOUNTS_TTL_S = 600.0


def _read_net_mounts() -> "tuple[str, ...]":
    """Mount points of network filesystems (see ``_NET_FS``), longest first.
    Blocking; () when the table can't be read."""
    out = []
    try:
        if os.path.exists("/proc/self/mounts"):
            with open("/proc/self/mounts", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 3 and parts[2].lower() in _NET_FS:
                        out.append(parts[1].replace("\\040", " "))
        elif shutil.which("mount"):
            from soniqboom.core import forksafe
            r = forksafe.run([shutil.which("mount")], capture_output=True, timeout=10)
            for line in (r.stdout or b"").decode("utf-8", "replace").splitlines():
                m = re.match(r"^.+? on (.+) \(([^,)]+)", line)
                if m and m.group(2).strip().lower() in _NET_FS:
                    out.append(m.group(1))
    except Exception:                                   # noqa: BLE001
        log.debug("loudness: mount table unreadable", exc_info=True)
    return tuple(sorted({m.rstrip("/") or "/" for m in out}, key=len, reverse=True))


def net_mounts(refresh: bool = False) -> "tuple[str, ...]":
    """``_read_net_mounts``, re-read at most every ``_NET_MOUNTS_TTL_S``.
    Blocking on a refresh."""
    global _NET_MOUNTS, _NET_MOUNTS_AT
    if refresh or _NET_MOUNTS is None or time.monotonic() - _NET_MOUNTS_AT > _NET_MOUNTS_TTL_S:
        _NET_MOUNTS = _read_net_mounts()
        _NET_MOUNTS_AT = time.monotonic()
    return _NET_MOUNTS


def on_network_mount(p: str, mounts: "tuple[str, ...] | None" = None) -> bool:
    """Is the path ``p`` on a network filesystem (``net_mounts``)?"""
    for m in (mounts if mounts is not None else (_NET_MOUNTS or ())):
        if p == m or p.startswith(m + "/"):
            return True
    return False


def native_candidate(t) -> bool:
    """Is ``t`` a native (not rendered) LOCAL file without ReplayGain tags and
    without a measurement — one the native pass measures?  O(1), reads no
    file: a remote share (also one mounted by the OS — ``on_network_mount``,
    as last read), an online-collection tune, an archive member, a rendered
    format, a file in a missing folder are never candidates."""
    if not isinstance(t, dict):
        return False
    p = t.get("path") or ""
    if not p or "::" in p or "://" in p or t.get("absent_since") or on_network_mount(p):
        return False
    ext = os.path.splitext(p)[1].lower()
    if ext not in _NATIVE_EXTS:
        return False
    if ext == ".dsf" and "dreamcast" in str(t.get("format") or "").lower():
        return False
    if has_tag_gain(t) or tune_entry(t, 0) is not None:
        return False
    from soniqboom.core import catalogue
    return not catalogue.is_catalogue_path(p)


def request_native(track_id: str, t=None, *, prio: "int | None" = None) -> bool:
    """Measure a native file now (the web player's next track — its
    look-ahead prewarm), behind the gate at ``prio`` (default
    ``PRIO_AHEAD``).  False when it isn't a candidate or is queued already."""
    try:
        if not isinstance(t, dict):           # (none, or a Track model: the store's dict)
            t = _store().get_track(track_id)
        if not native_candidate(t) or _failed(track_id, 0, t) or memo_entry(track_id, 0):
            return False
        from soniqboom.api import stream
        return _enqueue({"track_id": track_id, "key": 0, "path": t["path"], "native": True,
                         "filters": _native_filters(t["path"])},
                        stream.PRIO_AHEAD if prio is None else prio)
    except Exception:                                   # noqa: BLE001
        log.debug("loudness: native request failed", exc_info=True)
        return False


def _render_idle() -> bool:
    """No render running or waiting at all (foreground or background): the
    native pass measures only then."""
    from soniqboom.api import stream
    sem = stream._render_sem
    try:
        return sem.free() >= stream._RENDER_SLOTS and not sem.locked()
    except Exception:                                   # noqa: BLE001
        return False


def pass_enabled() -> bool:
    """conf ``loudness_scan`` (default on): the idle native pass runs."""
    try:
        from soniqboom.config import settings
        return bool(getattr(settings, "loudness_scan", True))
    except Exception:                                   # noqa: BLE001
        return True


# The native pass: a sweep over the library's track ids in slices (a loop
# turn after each), measuring the candidates one at a time; when a sweep
# found nothing it rests ``_PASS_REST_S`` and sweeps again (new files).
_PASS_START_DELAY_S = 180.0
_PASS_REST_S = 900.0
_PASS_SLICE = 1000                  # track ids looked at per loop turn
_PASS_IDLE_POLL_S = 5.0             # wait while a render runs
_PASS_DUTY = 1.0                    # rest as long as each measurement took (≤ 50 % of a core)
PASS_STATS = {"measured": 0, "failed": 0, "sweeps": 0, "running": False}


async def native_pass(*, start_delay: float = _PASS_START_DELAY_S,
                      rest: float = _PASS_REST_S, once: bool = False) -> None:
    """The idle background pass over local native files without ReplayGain
    (``native_candidate``): each measured behind ``_bg_render_sem`` at
    ``PRIO_VU``, only while no render runs (``_render_idle``), resting after
    each as long as it took.  ``once``: one sweep (tests)."""
    from soniqboom.api import stream
    await asyncio.sleep(start_delay)
    PASS_STATS["running"] = True
    try:
        while True:
            if not pass_enabled():
                if once:
                    return
                await asyncio.sleep(rest)
                continue
            found = await _sweep_once(stream)
            PASS_STATS["sweeps"] += 1
            flush()
            if once:
                return
            await asyncio.sleep(rest if not found else 1.0)
    finally:
        PASS_STATS["running"] = False


async def _sweep_once(stream) -> int:
    await asyncio.to_thread(net_mounts, True)
    store = _store()
    ids = list(store._tracks.keys())
    found = 0
    for i in range(0, len(ids), _PASS_SLICE):
        batch = []
        for tid in ids[i:i + _PASS_SLICE]:
            t = store._tracks.get(tid)
            if t is not None and native_candidate(t) and not _failed(tid, 0, t) \
                    and not memo_entry(tid, 0) and (tid, "0") not in _QUEUED:
                batch.append(tid)
        await asyncio.sleep(0)
        for tid in batch:
            while not _render_idle():
                await asyncio.sleep(_PASS_IDLE_POLL_S)
            t = store._tracks.get(tid)
            if t is None or not native_candidate(t) or _failed(tid, 0, t):
                continue
            found += 1
            t0 = time.monotonic()
            async with stream._bg_render_sem.slot(stream.PRIO_VU):
                job = {"track_id": tid, "key": 0, "path": t["path"], "native": True,
                       "filters": _native_filters(t["path"])}
                before = PASS_STATS["measured"]
                await _run_job(job)
                if memo_entry(tid, 0) is not None:
                    PASS_STATS["measured"] = before + 1
                else:
                    PASS_STATS["failed"] += 1
            await asyncio.sleep(max(0.05, (time.monotonic() - t0) * _PASS_DUTY))
    return found


def start_native_pass() -> "asyncio.Task | None":
    """Start the native pass on the running loop (main.py's startup)."""
    try:
        return asyncio.get_running_loop().create_task(native_pass(), name="loudness_native_pass")
    except RuntimeError:
        return None


def stop() -> None:
    """Shutdown: write what is pending and stop the measuring worker."""
    flush()
    if _WORKER is not None and not _WORKER.done():
        _WORKER.cancel()


def reset_for_tests() -> None:
    """Forget every in-memory state (tests)."""
    global _WORKER, _WAKE, _flush_handle
    _MEMO.clear()
    _FAILED.clear()
    _PENDING.clear()
    _QUEUE.clear()
    _QUEUED.clear()
    if _flush_handle is not None:
        _flush_handle.cancel()
    _flush_handle = None
    if _WORKER is not None and not _WORKER.done():
        _WORKER.cancel()
    _WORKER = None
    _WAKE = None
