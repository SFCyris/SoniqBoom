# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Track CRUD endpoints."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi import APIRouter, Cookie, HTTPException, Query, Request, Response

from soniqboom.core import forksafe
from soniqboom.core.filesource import is_remote_path

# Dedicated thread-pool for ``_compute_waveform`` — that helper spawns a
# 60s-timeout ffmpeg subprocess per call and ties up its worker the whole
# time.  Letting it share the default executor with AOF flush + art reads
# led to flush starvation under 5 concurrent users (Perf #1).  Sized
# small so a flood of waveform requests can't drown the rest of the app.
_WAVEFORM_POOL = ThreadPoolExecutor(
    max_workers=max(2, min(4, (os.cpu_count() or 4) // 2)),
    thread_name_prefix="sb-waveform",
)


# Waveform decodes are full-file ffmpeg runs: cap how many run at once so a
# burst (track skipping, several listeners) can't crowd out the renders and
# streams that playback is waiting on.
_WAVEFORM_DECODE_SEM = asyncio.Semaphore(2)


async def _compute_waveform_safe(path: str, points: int = 200):
    """Compute a track's waveform WITHOUT forking ffmpeg from a worker thread.

    ``scanner._compute_waveform`` decodes via a blocking ``subprocess.run``;
    offloaded to ``_WAVEFORM_POOL`` that fork runs on a non-main thread, which
    SEGFAULTS on macOS once the process has initialised Core Foundation (e.g.
    after the stations relay's outbound networking) — the worker dies and every
    waveform comes back all-zero (blank).  Decode ffmpeg on the EVENT LOOP via
    ``forksafe.spawn`` instead (posix_spawn, no fork) — the pattern the whole
    streaming path uses — then crunch the PCM in the pool (numpy doesn't fork).
    """
    async with _WAVEFORM_DECODE_SEM:
        return await _compute_waveform_unbounded(path, points)


# Decodes up to this many bytes of PCM (~47 s at 22.05 kHz mono f32le) are
# reduced from the whole buffer, exactly as before; a longer decode switches
# to per-block statistics as it streams in, so a long track never holds its
# whole decode in memory (a 30-minute file was +353 MB of peak RSS).
_WAVEFORM_EXACT_BYTES = 4 * 1024 * 1024
# Samples per block statistic in streaming mode.  A streamed decode has over
# 5,000 samples per waveform point, so a block boundary moves at most ~10 %
# of a point's samples into its neighbour — invisible at 200 points.
_WAVEFORM_BLOCK = 512
# Decoded bytes handed to the reducer per worker-thread hop.
_WAVEFORM_FEED_BYTES = 256 * 1024
# A decode still running after this long is killed (blank waveform).
_WAVEFORM_DECODE_TIMEOUT_S = 60.0


class _WaveformAccumulator:
    """Reduce mono f32le PCM to a ``{"peaks", "rms"}`` waveform as it arrives.

    Up to ``_WAVEFORM_EXACT_BYTES`` the bytes are kept and ``result`` is
    ``scanner._pcm_to_waveform`` of the whole buffer (identical output to the
    buffered decode).  Past that, every ``_WAVEFORM_BLOCK`` samples collapse
    to (max |x|, sum x²) and ``result`` bins the blocks by their start sample
    with the same bin width and tail truncation as ``_pcm_to_waveform``,
    then normalises the same way.  Memory: the exact-mode buffer, then ~16
    bytes per block (~1.2 MB for 60 minutes).  numpy when available, else a
    pure-Python path with the same output.  Not thread-safe: feed it from one
    caller at a time (CPU work — run ``feed`` / ``result`` off the loop)."""

    def __init__(self, points: int = 200):
        self.points = points
        self._buf = bytearray()
        self._streaming = False
        self._carry = b""
        # Per-block max |x| and sum of squares: numpy arrays (one per feed),
        # or flat ``array('d')`` on the pure-Python path (8 bytes a value).
        self._pk: list = []
        self._ss: list = []
        self._pk_d = None
        self._ss_d = None
        self._n = 0                  # samples reduced into full blocks

    def feed(self, data: bytes) -> None:
        if not self._streaming:
            self._buf += data
            if len(self._buf) < _WAVEFORM_EXACT_BYTES:
                return
            self._streaming = True
            data, self._buf = self._buf, bytearray()      # no copy of the buffer
        if self._carry:
            data = self._carry + data
        whole = len(data) // (_WAVEFORM_BLOCK * 4) * (_WAVEFORM_BLOCK * 4)
        if whole:
            self._reduce(memoryview(data)[:whole])
        self._carry = bytes(data[whole:])

    @staticmethod
    def _samples(mv):
        import sys
        if sys.byteorder == "little":
            return mv.cast("f")
        import array
        a = array.array("f")
        a.frombytes(mv)
        a.byteswap()                                  # f32le on the wire
        return a

    def _reduce(self, mv) -> None:
        try:
            import numpy as _np
        except ImportError:
            _np = None
        if _np is not None:
            a = _np.frombuffer(mv, dtype="<f4").reshape(-1, _WAVEFORM_BLOCK)
            self._pk.append(_np.abs(a).max(axis=1).astype(_np.float64))
            self._ss.append(_np.einsum("ij,ij->i", a, a, dtype=_np.float64))
            self._n += a.size
            return
        import array
        import operator
        s = self._samples(mv)
        if self._pk_d is None:
            self._pk_d, self._ss_d = array.array("d"), array.array("d")
        pk, ss, B = self._pk_d, self._ss_d, _WAVEFORM_BLOCK
        for j in range(0, len(s), B):
            blk = s[j:j + B]
            pk.append(max(max(blk), -min(blk)))
            ss.append(sum(map(operator.mul, blk, blk)))
        self._n += len(s)

    def result(self):
        points = self.points
        if not self._streaming:
            from soniqboom.core.scanner import _pcm_to_waveform
            try:
                return _pcm_to_waveform(bytes(self._buf), points)
            except Exception:                         # noqa: BLE001 — a few samples only
                return [0.0] * points
        tail = self._carry[: len(self._carry) // 4 * 4]
        tail_n = len(tail) // 4
        tail_pk = tail_ss = 0.0
        if tail_n:
            import operator
            t = self._samples(memoryview(tail))
            tail_pk = float(max(max(t), -min(t)))
            tail_ss = float(sum(map(operator.mul, t, t)))
        n = self._n + tail_n
        cs = max(1, n // points)
        usable = cs * points
        B = _WAVEFORM_BLOCK
        try:
            import numpy as _np
        except ImportError:
            _np = None
        if _np is not None:
            pk = _np.concatenate(self._pk + [_np.array([tail_pk])]) if tail_n else \
                _np.concatenate(self._pk)
            ss = _np.concatenate(self._ss + [_np.array([tail_ss])]) if tail_n else \
                _np.concatenate(self._ss)
            cnt = _np.full(len(pk), B, dtype=_np.float64)
            if tail_n:
                cnt[-1] = tail_n
            starts = _np.arange(len(pk), dtype=_np.int64) * B
            keep = starts < usable
            bins = _np.minimum(starts[keep] // cs, points - 1)
            peaks = _np.zeros(points)
            _np.maximum.at(peaks, bins, pk[keep])
            sums = _np.bincount(bins, weights=ss[keep], minlength=points)
            cnts = _np.bincount(bins, weights=cnt[keep], minlength=points)
            rms = _np.sqrt(_np.divide(sums, cnts, out=_np.zeros(points), where=cnts > 0))
            rms_peak, peak_peak = float(rms.max()), float(peaks.max())
            if rms_peak > 0:
                rms = rms / rms_peak
            if peak_peak > 0:
                peaks = peaks / peak_peak
            return {"peaks": peaks.tolist(), "rms": rms.tolist()}
        import math
        peaks = [0.0] * points
        sums = [0.0] * points
        cnts = [0] * points

        def _add(j: int, p: float, q: float, c: int) -> None:
            start = j * B
            if start >= usable:
                return
            b = min(start // cs, points - 1)
            if p > peaks[b]:
                peaks[b] = p
            sums[b] += q
            cnts[b] += c

        full = len(self._pk_d) if self._pk_d is not None else 0
        for j in range(min(full, -(-usable // B))):
            _add(j, self._pk_d[j], self._ss_d[j], B)
        if tail_n:
            _add(full, tail_pk, tail_ss, tail_n)
        rms = [math.sqrt(sums[i] / cnts[i]) if cnts[i] else 0.0 for i in range(points)]
        rms_peak, peak_peak = max(rms), max(peaks)
        if rms_peak > 0:
            rms = [v / rms_peak for v in rms]
        if peak_peak > 0:
            peaks = [v / peak_peak for v in peaks]
        return {"peaks": [float(v) for v in peaks], "rms": rms}


async def _compute_waveform_unbounded(path: str, points: int = 200):
    """ffmpeg-decode ``path`` to mono 22.05 kHz f32le and reduce it to a
    waveform as it streams (``_WaveformAccumulator``), the reduction off the
    loop in ``_WAVEFORM_POOL``.  A spawn failure, a decode running longer
    than ``_WAVEFORM_DECODE_TIMEOUT_S`` (ffmpeg is killed) or any other error
    gives the blank ``[0.0] * points`` (never stored)."""
    from soniqboom.config import settings
    try:
        proc = await forksafe.spawn(
            settings.ffmpeg_path, "-i", path,
            "-ac", "1", "-ar", "22050", "-f", "f32le", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:               # noqa: BLE001 — ffmpeg missing / spawn failure
        return [0.0] * points
    loop = asyncio.get_running_loop()
    acc = _WaveformAccumulator(points)

    async def _drain() -> None:
        pending = bytearray()
        while True:
            chunk = await proc.stdout.read(262144)
            if chunk:
                pending += chunk
            if len(pending) >= _WAVEFORM_FEED_BYTES or (not chunk and pending):
                data, pending = bytes(pending), bytearray()
                await loop.run_in_executor(_WAVEFORM_POOL, acc.feed, data)
            if not chunk:
                break
        await proc.wait()

    try:
        await asyncio.wait_for(_drain(), timeout=_WAVEFORM_DECODE_TIMEOUT_S)
    except (Exception, asyncio.CancelledError) as exc:   # noqa: BLE001 — timeout / error / cancel
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        if isinstance(exc, asyncio.CancelledError):
            raise
        return [0.0] * points
    return await loop.run_in_executor(_WAVEFORM_POOL, acc.result)


async def _resolve_zip_member_to_local(path_str: str):
    """Extract a ZIP member to a local temp file ffmpeg can read.

    Handles both a local archive (``/path/a.zip::member``) and a remote one
    (``ftp://host/scanroot:/a.zip::member`` — fetch the archive to the cache
    first, then read the member).  Returns a ``Path`` the CALLER must unlink,
    or ``None`` if it can't be resolved (the caller then degrades to 404).
    Mirrors the extraction already used for converted formats in
    ``_waveform_from_conversion_cache``.
    """
    from pathlib import Path as _Path
    import tempfile
    loop = asyncio.get_event_loop()
    try:
        if is_remote_path(path_str):
            from soniqboom.core import archive as _archive
            from soniqboom.core.filesource import get_source, parse_remote_path
            from soniqboom.core.remote_cache import get_cache
            scan_root, remote_path = parse_remote_path(path_str)
            # (no source: the cached subset / archive still answers)
            source = get_source(scan_root)
            if "::" not in remote_path:
                return None
            arc_rel, member_name = remote_path.split("::", 1)
            from soniqboom.core.remote_zip import archive_for_member
            local_archive = await loop.run_in_executor(
                None, archive_for_member, scan_root, arc_rel, member_name, source)
            data = await loop.run_in_executor(
                None, _archive.read_member, local_archive, member_name)
        else:
            from soniqboom.core.scanner import _read_from_zip_path
            data, member_name = await loop.run_in_executor(
                None, _read_from_zip_path, path_str)
        tmp = tempfile.NamedTemporaryFile(suffix=_Path(member_name).suffix, delete=False)
        try:
            tmp.write(data)
            tmp.close()
        except Exception:
            # Don't orphan the just-created (delete=False) temp if the write
            # fails mid-stream (e.g. ENOSPC) — unlink before degrading.
            tmp.close()
            _Path(tmp.name).unlink(missing_ok=True)
            raise
        return _Path(tmp.name)
    except Exception:                       # noqa: BLE001 — degrade, never 500
        return None

import orjson

from soniqboom.core.data import (
    delete_track, get_track, track_count,
    set_rating, get_rating, get_ratings_batch, get_all_ratings,
    record_play, get_play_stats, get_play_stats_batch, get_all_play_stats,
    ft_search, ft_search_dicts,
)
from soniqboom.core.metadata import extract_lyrics
from soniqboom.models.track import TrackMeta

router = APIRouter(prefix="/tracks", tags=["tracks"])


# ── JSON list encoding (shared by every track-list endpoint) ─────────────────
#
# A route that returns plain Python data goes through FastAPI's
# ``jsonable_encoder`` (a recursive Python walk over every value) and then
# stdlib ``json.dumps`` — on the event loop: ~140 ms for a 2000-row folder page,
# ~430 ms for a 5000-id ``/meta/batch``.  Rows that a ``response_model`` (or a
# ``Track(**d)`` per row) shaped first paid a Pydantic model construction per
# row on top.  These helpers serialize the rows with orjson instead (a few ms)
# and keep the TrackMeta contract the models gave: every field present (a field
# missing from an older stored row → its model default), only TrackMeta fields
# (``embedding`` and any stray key never leak), values of the declared types.
# The OpenAPI schemas of those routes are the price.

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _json_safe(obj):
    """``obj`` with every lone surrogate replaced by U+FFFD and every
    non-finite float by None (what orjson writes for one).

    A filename that is not valid UTF-8 decodes (``os.fsdecode``) with its bad
    bytes as lone surrogates, and undecodable tag bytes can end up the same
    way.  No UTF-8 encoder accepts those: orjson refuses the whole payload, and
    the old ``JSONResponse`` path raised on ``.encode("utf-8")`` — one such
    file 500'd its entire folder listing.  Degrade the characters instead (the
    Subsonic XML encoder does the same)."""
    if isinstance(obj, str):
        if obj.isascii() or not _LONE_SURROGATE.search(obj):
            return obj
        return _LONE_SURROGATE.sub("\ufffd", obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {_json_safe(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def json_bytes(obj) -> bytes:
    """``obj`` as JSON bytes — orjson, with a fallback for what it refuses.

    For the JSON-native data these routes return, the same JSON values as the
    ``JSONResponse`` path, except that a non-finite float (mutagen reports
    ``nan`` for a truncated stream's length) is ``null`` — exactly what the
    ``response_model`` routes already emitted — instead of a 500 for the whole
    response.  orjson refuses lone surrogates (see ``_json_safe``), types it
    doesn't know (a ``set``, a ``Path``…), non-str keys and ints beyond 64
    bits: see ``_json_bytes_slow``.  Anything still unencodable raises, as it
    did before."""
    try:
        return orjson.dumps(obj)
    except orjson.JSONEncodeError:
        return _json_bytes_slow(obj)


def _json_bytes_slow(obj) -> bytes:
    """What :func:`json_bytes` makes of a payload orjson refused — localised.

    A list (or a dict with str keys) is encoded part by part, each part by
    orjson first, so only the parts it refuses — typically the one row with an
    undecodable filename in a 2000-row page — take the slow path; the rest stay
    orjson-fast (one bad row: ~4 ms, not the ~190 ms of re-encoding the whole
    page through ``jsonable_encoder``; every row bad: ~55 ms).  orjson's compact
    output composes, so the bytes are the same as encoding the scrubbed payload
    in one go.  A part that is not a container goes through ``jsonable_encoder``
    (the conversions FastAPI applied before) + ``_json_safe``, then orjson
    (stdlib ``json`` for an int beyond 64 bits)."""
    if isinstance(obj, (list, tuple)):
        return b"[" + b",".join(map(_json_bytes_part, obj)) + b"]"
    if isinstance(obj, dict) and all(type(k) is str for k in obj):
        return b"{" + b",".join(orjson.dumps(_json_safe(k)) + b":" + _json_bytes_part(v)
                                for k, v in obj.items()) + b"}"
    from fastapi.encoders import jsonable_encoder
    safe = _json_safe(jsonable_encoder(obj))
    try:
        return orjson.dumps(safe, option=orjson.OPT_NON_STR_KEYS)
    except orjson.JSONEncodeError:              # an int beyond 64 bits
        import json
        return json.dumps(safe, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")


def _json_bytes_part(obj) -> bytes:
    try:
        return orjson.dumps(obj)
    except orjson.JSONEncodeError:
        return _json_bytes_slow(obj)


def json_response(obj, status_code: int = 200, headers: dict | None = None) -> Response:
    """``obj`` as an ``application/json`` response encoded by :func:`json_bytes`."""
    return Response(content=json_bytes(obj), status_code=status_code,
                    headers=headers, media_type="application/json")


def json_route(router_: APIRouter, path: str, *, methods: tuple[str, ...] = ("GET",),
               **route_kw):
    """``@router.get(path)``, but the handler's result is encoded by
    :func:`json_bytes` instead of ``jsonable_encoder`` + ``json.dumps``.

    The decorated function itself is returned UNCHANGED — still a coroutine
    returning plain Python data — so code and tests that call it directly keep
    getting the dicts / lists; only the registered endpoint (a wrapper with the
    same signature, which FastAPI reads through ``__wrapped__``) encodes.  A
    ``Response`` the handler returns passes through as is; ``HTTPException``
    propagates as usual.  ``response_model=None``: nothing is re-validated, so
    the handler must already return the shape it promises.  Async handlers
    only, and headers set on an injected ``response: Response`` parameter are
    NOT applied (FastAPI merges those only into a response it builds) — return
    a ``Response`` for that."""
    import functools
    import inspect
    status_code = route_kw.get("status_code") or 200

    def decorate(fn):
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"json_route needs an async handler, not {fn.__qualname__}")

        @functools.wraps(fn)
        async def endpoint(*args, **kwargs):
            out = await fn(*args, **kwargs)
            return out if isinstance(out, Response) else json_response(out, status_code)
        router_.add_api_route(path, endpoint, methods=list(methods),
                              response_model=None, **route_kw)
        return fn
    return decorate


# TrackMeta-shaped rows without building a TrackMeta per row.  Stored rows are
# ``Track.model_dump()`` output plus diffed field writes, so nearly every row
# already holds exactly the values the model would give back: those are served
# as they are (the store's own dict when it has precisely the TrackMeta keys —
# callers must copy before adding keys).  A row whose values the model would
# CHANGE or REJECT (a "1999" year, a float in an int field, a None title…) is
# shaped by the model itself, so it comes out exactly as the old per-row
# ``TrackMeta(**d)`` made it — coerced, or rejected → ``None``.
_META_NAMES: tuple[str, ...] = tuple(TrackMeta.model_fields)
_META_KEYS = frozenset(_META_NAMES)
_META_REQUIRED = frozenset(n for n, f in TrackMeta.model_fields.items() if f.is_required())
_NONE_T = type(None)


def _meta_types(annotation) -> "tuple[frozenset, frozenset, frozenset | None]":
    """``(kept, to_float, elements)`` for a TrackMeta field annotation: the
    Python types the model passes through unchanged, the types it turns into a
    float (an int in a float field — same JSON number, re-typed to match), and
    for a list field the element types it keeps.  An annotation not handled
    here gets empty sets: any value present takes the model path (correct, just
    slower — a test pins that no current field does)."""
    import types
    import typing
    args = (typing.get_args(annotation)
            if typing.get_origin(annotation) in (typing.Union, types.UnionType)
            else (annotation,))
    kept: set = set()
    to_float: set = set()
    elements = None
    for a in args:
        if a is _NONE_T:
            kept.add(_NONE_T)
        elif a in (str, int, bool):
            kept.add(a)
        elif a is float:
            kept.add(float)
            to_float.add(int)
        elif typing.get_origin(a) is list and typing.get_args(a) in ((str,), (float,)):
            kept.add(list)
            elements = frozenset(typing.get_args(a))
        else:
            return frozenset(), frozenset(), None
    return frozenset(kept), frozenset(to_float), elements


_META_SPEC = {n: _meta_types(f.annotation) for n, f in TrackMeta.model_fields.items()}
_FLOAT_ONLY = frozenset({float})
_STR_LISTS = tuple(n for n, (_k, _f, el) in _META_SPEC.items() if el == {str})
_FLOAT_LISTS = tuple(n for n, (_k, _f, el) in _META_SPEC.items() if el == _FLOAT_ONLY)


class _Absent:
    """Type of the marker a field missing from a stored row reads back as."""


_ABSENT = (_Absent(),) * len(_META_NAMES)     # per-field ``dict.get`` default
# value-type signature (one type per field, in _META_NAMES order; ``_Absent`` for
# a missing one) → ``(fields to re-type as float, fields missing)``, or None when
# the model must shape the row.  A library has a handful of distinct signatures;
# the bound only guards against pathological churn.
_SIG_PLANS: dict[tuple, "tuple[tuple[str, ...], tuple[str, ...]] | None"] = {}
_SIG_PLANS_MAX = 4096
_NO_PLAN = object()


def _sig_plan(sig: tuple):
    to_fix: list[str] = []
    missing: list[str] = []
    plan = None
    for name, t in zip(_META_NAMES, sig):
        kept, to_float, _el = _META_SPEC[name]
        if t is _Absent:
            if name in _META_REQUIRED:
                break                       # no id / path: the model rejects the row
            missing.append(name)
        elif t in to_float:
            to_fix.append(name)
        elif t not in kept:
            break
    else:
        plan = (tuple(to_fix), tuple(missing))
    if len(_SIG_PLANS) >= _SIG_PLANS_MAX:
        _SIG_PLANS.clear()
    _SIG_PLANS[sig] = plan
    return plan


def _model_shaped(d: dict) -> dict | None:
    try:
        return TrackMeta(**{k: v for k, v in d.items() if k in _META_KEYS}).model_dump()
    except Exception:                       # ValidationError: the row was always rejected
        return None


def public_track(d: dict | None) -> dict | None:
    """Stored track dict → what ``TrackMeta(**d).model_dump()`` returns, or
    ``None`` where that raised (or ``d`` is empty / None).

    Equal values, without the per-row model construction for the rows that
    need none (~3 µs a row instead of ~15 µs + the encoder walk).
    ``embedding`` and non-TrackMeta keys are dropped; missing fields take their
    model defaults (a row persisted before a newer field existed).  May return
    ``d`` itself — treat the result as read-only."""
    if not d:
        return None
    sig = tuple(map(type, map(d.get, _META_NAMES, _ABSENT)))
    plan = _SIG_PLANS.get(sig, _NO_PLAN)
    if plan is _NO_PLAN:
        plan = _sig_plan(sig)
    if plan is None:
        return _model_shaped(d)
    try:
        for name in _STR_LISTS:
            if v := d.get(name):
                "".join(v)                  # TypeError on a non-str element
    except TypeError:
        return _model_shaped(d)
    for name in _FLOAT_LISTS:
        if (v := d.get(name)) and not set(map(type, v)) <= _FLOAT_ONLY:
            return _model_shaped(d)
    to_fix, missing = plan
    if missing or len(d) != len(_META_NAMES):    # → a field to fill, or a stray key
        fields = TrackMeta.model_fields
        out = {name: d[name] if name in d
               else fields[name].get_default(call_default_factory=True)
               for name in _META_NAMES}
    elif to_fix:
        out = dict(d)
    else:
        return d
    try:
        for name in to_fix:
            out[name] = float(out[name])
    except OverflowError:                   # an int too big for a float: model rejects it
        return _model_shaped(d)
    return out


def public_tracks(rows) -> list[dict]:
    """:func:`public_track` over ``rows``, skipping the ones it rejects."""
    return [t for t in map(public_track, rows) if t is not None]


# ── Tag editing ───────────────────────────────────────────────────────────────

from fastapi import Depends as _Depends
from pydantic import BaseModel as _BaseModel


class _TagUpdate(_BaseModel):
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    genre: str | None = None
    year: int | None = None
    track_number: int | None = None
    game: str | None = None             # the file's GAME tag (TXXX:GAME / GAME / ----:GAME)


from soniqboom.api.users import (
    require_user as _require_user, require_edit as _require_edit,
    require_admin as _require_admin,
)


@router.put("/{track_id}/tags")
async def update_tags(track_id: str, body: _TagUpdate, user=_Depends(_require_edit)):
    """Write tags into the local audio file AND mirror them into the library.

    Local files only — remote-share tracks (smb/ftp/webdav) and zip members
    are refused.  Requires a signed-in non-read-only account.
    """
    if user.role == "readonly":
        raise HTTPException(403, "Your account is read-only — tag editing needs an 'edit' or admin account.")

    from soniqboom.core.store import get_store
    t = get_store().get_track(track_id)
    if not t:
        raise HTTPException(404, "Track not found")
    path = t.get("path") or ""
    if is_remote_path(path):
        raise HTTPException(422, "Tags can only be edited on local files (this track lives on a network share).")
    if "::" in path:
        raise HTTPException(422, "Tags can't be edited on files inside archives.")

    from soniqboom.core.tagwriter import write_tags
    try:
        applied = await asyncio.to_thread(write_tags, path, body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception:
        raise HTTPException(500, "Could not write tags to the file.")

    store_updates: dict = dict(applied)
    if "genre" in store_updates:
        store_updates["genre"] = [store_updates["genre"]]
    if "year" in store_updates:
        # A hand-edited year is authoritative: mark its provenance so the
        # Demozoo year backfill (demozoo.collect_updates) never overwrites a
        # deliberate user correction on a later apply.
        store_updates["year_source"] = "user"
    if "game" in store_updates:
        store_updates["game_source"] = None     # the file's own GAME tag now
    get_store().update_track_fields(track_id, store_updates)
    await _refresh_rows(track_id)
    # The edited artist/title/album change what LRCLib would return, so drop any
    # cached lyrics for this track — the next LYRICS open re-resolves with the
    # corrected tags instead of serving a stale (possibly mismatched) result.
    _forget_lyrics(track_id)
    return {"id": track_id, "applied": applied}


async def _refresh_rows(track_id: str) -> None:
    """An edited track's cached folder-browse row (and the album aggregation
    cache) is refreshed like an enrichment pass's — else the Folders view
    shows the old values until its music folder changes."""
    try:
        from soniqboom.core.folder_album import refresh_album_caches
        await refresh_album_caches([track_id])
    except Exception:                                   # noqa: BLE001
        logging.getLogger(__name__).debug("browse-row refresh after an edit failed",
                                          exc_info=True)


class _YearUpdate(_BaseModel):
    year: int | None = None      # the corrected year (None = "no year")
    revert: bool = False         # restore the file/rip year, dropping the stamp


@router.put("/{track_id}/year")
async def update_year(track_id: str, body: _YearUpdate, user=_Depends(_require_edit)):
    """Store-only year correction — the ONLY year-edit path for retro formats.

    Modules / SID / chip formats can't be tag-written (mutagen doesn't support
    the containers), so the file-writing tag editor refuses them.  That left a
    wrong Demozoo release-year backfill uncorrectable.  This edits the LIBRARY
    record only (no file write), so it works for any format, remote share, or
    archive member:

      * ``revert: true`` restores the preserved original (``year_file``) and
        clears the provenance — the manual counterpart to the backfill's own
        stale-stamp revert;
      * otherwise ``year`` is stamped with ``year_source="user"``, which the
        Demozoo backfill treats as authoritative and never overwrites (and
        which now survives a rescan, see store.upsert_tracks_batch).

    The original file/rip year is preserved once in ``year_file`` so a user
    stamp is itself revertible.
    """
    from soniqboom.core.store import get_store
    store = get_store()
    t = store.get_track(track_id)          # the raw store dict (has .get)
    if not t:
        raise HTTPException(404, "Track not found")
    if body.revert:
        # Only a stamped year can be reverted — refuse on an unstamped track so
        # a stray revert (a direct API / mobile-shell caller) can't blank a
        # real file year that was never overridden.
        if t.get("year_source") not in ("demozoo", "user"):
            raise HTTPException(409, "Nothing to revert — this year isn't a "
                                     "Demozoo or manual override.")
        # "The file/rip year is right, not Demozoo's" — a deliberate user
        # choice, so it's stamped ``user`` (sticky): the backfill and the
        # display-time scene overwrite both leave a user year alone, so this
        # survives the next apply.  ``year_file`` is KEPT as the anchor so a
        # later edit/revert can still recover the true original.
        updates: dict = {"year": t.get("year_file"), "year_source": "user"}
    else:
        y = body.year
        if y is not None and not (1000 <= int(y) <= 2100):
            raise HTTPException(422, "Year must be between 1000 and 2100.")
        updates = {"year": (int(y) if y is not None else None),
                   "year_source": "user"}
        # Preserve whatever the file/rip carried, ONCE — a second user edit (or
        # editing over a demozoo stamp) must keep the true original, not stamp
        # our own prior value as the "file" year.
        # (a song-database year only filled a missing one — not the file's)
        if (t.get("year_source") not in ("user", "demozoo", "songdb")
                and t.get("year") is not None):
            updates["year_file"] = t.get("year")
    get_store().update_track_fields(track_id, updates)
    await _refresh_rows(track_id)
    return {"id": track_id, "applied": updates}


class _MetaUpdate(_BaseModel):
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    genre: str | None = None          # comma-separated; stored as a list
    composer: str | None = None
    comment: str | None = None
    label: str | None = None
    game: str | None = None
    year: int | None = None


_META_TEXT_FIELDS = ("title", "artist", "album", "album_artist", "composer", "comment",
                     "label", "game")


@router.put("/{track_id}/meta")
async def update_meta(track_id: str, body: _MetaUpdate, user=_Depends(_require_edit)):
    """Store-only metadata edit — the metadata editor for formats that can't be
    tag-written (modules, SID, chip, archive members, remote shares).

    Only the fields the client actually sends are touched (partial update).
    Each hand-set field is recorded in ``user_edited`` so a rescan's fresh file
    extract doesn't revert it (see store._carry_enrichment); ``year`` rides its
    own provenance (year_source=user + year_file preserved once), exactly like
    PUT /{id}/year, so the two stay consistent.  No file is written, so this
    works for any format/source.
    """
    from soniqboom.core.store import get_store
    store = get_store()
    t = store.get_track(track_id)
    if not t:
        raise HTTPException(404, "Track not found")
    fields = body.model_dump(exclude_unset=True)     # only what the client sent
    updates: dict = {}
    edited = set(x for x in (t.get("user_edited") or []) if isinstance(x, str))
    for k, v in fields.items():
        if k == "year":
            if v is not None and not (1000 <= int(v) <= 2100):
                raise HTTPException(422, "Year must be between 1000 and 2100.")
            updates["year"] = int(v) if v is not None else None
            updates["year_source"] = "user"
            if (t.get("year_source") not in ("user", "demozoo", "songdb")
                    and t.get("year") is not None):
                updates["year_file"] = t.get("year")
            # year is tracked by year_source, not user_edited
        elif k == "genre":
            updates["genre"] = [g.strip() for g in (v or "").split(",") if g.strip()]
            edited.add("genre")
        elif k in _META_TEXT_FIELDS:
            updates[k] = "" if v is None else str(v)
            edited.add(k)
            if k == "album":
                updates["album_source"] = None   # the listener's own album, not a derived one
            elif k == "game":
                updates["game_source"] = None    # the listener's own game
    if not updates:
        return {"id": track_id, "applied": {}}
    if any(k != "year" for k in updates if k not in ("year_source", "year_file")):
        updates["user_edited"] = sorted(edited)
    # A field the user set is no longer the song database's fill.
    sf = t.get("songdb_fields") or []
    if any(f in edited for f in sf):
        updates["songdb_fields"] = [f for f in sf if f not in edited] or None
    before = {k: t.get(k) for k in ("game", "game_source", "game_aliases")}
    store.update_track_fields(track_id, updates)
    await _refresh_rows(track_id)
    # The game (and its other names) a retro album implies
    # (``store.game_follow``) came with it.
    cur = store.get_track(track_id) or {}
    applied = dict(updates)
    for k, v in before.items():
        if k not in applied and cur.get(k) != v:
            applied[k] = cur.get(k)
    # Edited artist/title/album change the LRCLib match — drop cached lyrics.
    _forget_lyrics(track_id)
    return {"id": track_id, "applied": applied}

# ── Shared httpx client for LRCLib requests ──────────────────────────────────

_lrclib_client: httpx.AsyncClient | None = None


def _get_lrclib_client() -> httpx.AsyncClient:
    global _lrclib_client
    if _lrclib_client is None:
        # Cap connection use so a track-change storm (5 users + 3 rooms
        # all switching together) doesn't open 100 concurrent connections
        # to LRClib — Perf #1 flagged the missing limits.
        _lrclib_client = httpx.AsyncClient(
            timeout=8.0,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
            ),
        )
    return _lrclib_client


# ── LRCLib fuzzy-fallback helpers ─────────────────────────────────────────────
# LRCLib's exact ``/api/get`` 404s when a store/label appends release noise to
# the title or album — "(No Narration)", "(24-bit HD audio)", "(Deluxe
# Edition)", "(Remastered 2021)", "[Bonus Track]" — or when the tagged duration
# differs from LRCLib's by more than its tolerance.  When that happens we clean
# the title and fall back to the fuzzy ``/api/search``, then pick the candidate
# closest in duration so a noisy tag still resolves WITHOUT attaching a
# different recording's words (confidence-gated: refuse over guess).
import re as _re

# A TRAILING parenthetical / bracket group + its inner text (group 1).
_TRAILING_GROUP_RE = _re.compile(r"\s*[\(\[]([^\(\)\[\]]*)[\)\]]\s*$")
# Inner text that marks the group as release NOISE (safe to strip) rather than a
# meaningful subtitle.  Only noise groups are peeled — so "(24-bit HD audio)",
# "(No Narration)", "(Deluxe Edition)", "(Remastered 2021)", "(Live)" go, but
# "(How Does It Feel)" or "(Pt. 2)" stay (stripping them would let a fuzzy match
# collide with a *different* same-artist song — dangerous once write-back is on).
_QUALIFIER_NOISE_RE = _re.compile(
    r"(?i)(remaster|deluxe|expanded|\bedition\b|bonus|reissue|anniversary|"
    r"\bmono\b|\bstereo\b|\bversion\b|\bmix\b|remix|\bedit\b|\blive\b|acoustic|"
    r"instrumental|\bdemo\b|\d+\s*-?\s*bit|\d+\s*k?hz|hd\s*audio|hi-?res|"
    r"narration|explicit|\bclean\b|\bradio\b|\bsingle\b|original|feat\.?|"
    r"featuring|ft\.?|no\s*vocals?)"
)

# Duration windows (seconds) for accepting a fuzzy /api/search candidate.
_LRCLIB_DUR_MAX_S = 30.0   # nothing within this of the track → refuse the match

# Per-request timeouts (seconds).  The exact /api/get is quick; the full-text
# /api/search is heavier and slower (5–9 s under load), so it gets a longer
# budget.  Both degrade cleanly to "no lyrics" on timeout.
_LRCLIB_GET_TIMEOUT_S = 6.0
_LRCLIB_SEARCH_TIMEOUT_S = 12.0

# Resolved-lyrics cache (in-memory).  LRCLib is slow and flaky, so once a
# track's lyrics resolve we keep them for the process lifetime: the LYRICS tab
# re-opens instantly and — crucially — the lyrics survive LRCLib later going
# down (the "used to have lyrics, now nothing" report).  Bounded to cap memory.
_lyrics_cache: dict[str, dict] = {}
_LYRICS_CACHE_MAX = 4000

# Negative cache: track_id → monotonic expiry.  A CLEAN miss (every provider
# answered and none had the song) is remembered for a day, so re-opening the
# tab — or a Subsonic client asking on every track change — doesn't re-query
# both providers (~1 s) each time.  A provider ERROR (timeout, 5xx, 429) is
# never remembered, so a transient outage still self-heals on the next open.
_lyrics_miss: dict[str, float] = {}
_LYRICS_MISS_TTL_S = float(os.environ.get("SONIQBOOM_LYRICS_MISS_TTL_S", "86400"))
_LYRICS_MISS_MAX = 20000
_MISS = object()        # provider answer: "no lyrics for this song" (not an error)


def _remember_lyrics_miss(track_id: str) -> None:
    if len(_lyrics_miss) >= _LYRICS_MISS_MAX:
        now = time.monotonic()
        for k in [k for k, exp in _lyrics_miss.items() if exp <= now]:
            _lyrics_miss.pop(k, None)
        if len(_lyrics_miss) >= _LYRICS_MISS_MAX:
            _lyrics_miss.clear()           # simple bound — cheap, rare
    _lyrics_miss[track_id] = time.monotonic() + _LYRICS_MISS_TTL_S


def _forget_lyrics(track_id: str) -> None:
    """Drop cached lyrics — found or not — for a track whose tags changed."""
    _lyrics_cache.pop(track_id, None)
    _lyrics_miss.pop(track_id, None)


def _remember_lyrics(track_id: str, result: dict) -> dict:
    """Cache a resolved lyrics payload if it actually has lyrics; return it."""
    if result.get("lyrics"):
        if len(_lyrics_cache) >= _LYRICS_CACHE_MAX:
            _lyrics_cache.clear()          # simple bound — cheap, rare
        _lyrics_cache[track_id] = result
    return result


def lyrics_cache_size() -> int:
    """Number of tracks with cached resolved lyrics (for the admin panel;
    remembered misses are not counted)."""
    return len(_lyrics_cache)


def clear_lyrics_cache() -> int:
    """Empty the resolved-lyrics cache (and the remembered misses); return how
    many resolved entries were dropped.  The next LYRICS open re-resolves
    online (used by Admin → System → Cache)."""
    n = len(_lyrics_cache)
    _lyrics_cache.clear()
    _lyrics_miss.clear()
    return n


async def _maybe_writeback_lyrics(track, lyrics_text: str) -> None:
    """If the ``lyrics_writeback`` setting is on and this is a LOCAL file with no
    embedded lyrics, embed the freshly-fetched lyrics into it — in the
    background, off the response path so the LYRICS tab never waits on a tag
    write.  A no-op when the toggle is off, for remote/zip paths, or when the
    file already has lyrics (``write_lyrics`` re-checks and never overwrites)."""
    try:
        from soniqboom.core.data import get_config
        if not await get_config("lyrics_writeback", False):
            return
        path_str = getattr(track, "path", "") or ""
        if not path_str or is_remote_path(path_str):
            return                              # only real local files
        if "!" in path_str or "::" in path_str:
            return                              # zip-virtual member — not a writable file
        p = Path(path_str)

        async def _bg() -> None:
            log = logging.getLogger(__name__)
            try:
                from soniqboom.core.metadata import write_lyrics
                wrote = await asyncio.to_thread(write_lyrics, p, lyrics_text)
                if wrote:
                    log.info("Lyrics writeback: embedded fetched lyrics into %s", p.name)
            except Exception:
                log.debug("Lyrics writeback failed for %s", p, exc_info=True)

        asyncio.create_task(_bg())
    except Exception:
        pass


def _strip_release_qualifiers(name: str) -> str:
    """Strip trailing release-NOISE qualifiers from a title/album so a fuzzy
    lyrics lookup matches the canonical release — but only groups that look like
    noise (see ``_QUALIFIER_NOISE_RE``); a meaningful subtitle is kept.  Never
    strips to empty."""
    s = (name or "").strip()
    while True:
        m = _TRAILING_GROUP_RE.search(s)
        if not m or not _QUALIFIER_NOISE_RE.search(m.group(1)):
            break                             # no trailing group, or it's a real subtitle
        stripped = s[:m.start()].strip()
        if not stripped:
            break                             # would empty the title — keep as-is
        s = stripped
    return s


def _norm_title(name: str) -> str:
    """Normalise a title for equality comparison: drop trailing qualifiers,
    lowercase, strip punctuation, collapse whitespace.  So "Shaggathon (Album
    Version)" == "Shaggathon", but "Angel" != "Angel of Death"."""
    s = _strip_release_qualifiers(name).lower()
    s = _re.sub(r"[^\w\s]", " ", s)
    return _re.sub(r"\s+", " ", s).strip()


# A trailing "feat./ft./featuring/with …" credit — stripped so a primary-artist
# comparison treats "Artist feat. X" == "Artist" WITHOUT the substring looseness
# that made "Sia" match "Basia".
_FEAT_RE = _re.compile(r"\s*[\(\[]?\s*\b(feat\.?|featuring|ft\.?|with)\b.*$", _re.IGNORECASE)


def _artist_core(name: str) -> str:
    """Primary artist, lowercased, trailing feat.-clause removed."""
    return _FEAT_RE.sub("", (name or "").strip().lower()).strip()


def _lrclib_lyrics_payload(rec: dict) -> dict | None:
    """Shape an LRCLib record into the endpoint's response, synced over plain.
    Returns None for an instrumental / lyric-less record."""
    if not isinstance(rec, dict):
        return None
    synced = (rec.get("syncedLyrics") or "").strip()
    if synced:
        return {"lyrics": synced, "synced": True, "source": "LRCLib.net"}
    plain = (rec.get("plainLyrics") or "").strip()
    if plain:
        return {"lyrics": plain, "synced": False, "source": "LRCLib.net"}
    return None


def _lrclib_best_match(results, artist: str, title: str, duration: float | None) -> dict | None:
    """Pick the /api/search candidate most likely to be THIS recording: same
    artist, same (cleaned) title, has lyrics, and — when we know the track
    duration — closest in length within a tolerance.  Refuse if everything is
    wildly off so we never show another song's lyrics (refuse over guess)."""
    if not isinstance(results, list):
        return None
    wa = _artist_core(artist)
    wt = _norm_title(title)
    wt_raw = _strip_release_qualifiers(title).strip().lower()

    def _artist_ok(r: dict) -> bool:
        ra = _artist_core(r.get("artistName") or "")
        if not wa or not ra:
            return True                       # can't compare → trust the search's artist filter
        return wa == ra                       # exact primary artist (feat.-clauses stripped)

    def _title_ok(r: dict) -> bool:
        rt = _norm_title(r.get("trackName") or "")
        if not wt:
            # A title that normalises to empty (punctuation-only names — "!!!",
            # "+/-") must NOT become a wildcard matching any same-artist song.
            # Fall back to a raw comparison so "!!!" only matches "!!!".
            rt_raw = _strip_release_qualifiers(r.get("trackName") or "").strip().lower()
            return bool(wt_raw) and wt_raw == rt_raw
        if not rt:
            return False                      # candidate has no comparable title → refuse
        return wt == rt                       # same core title (± qualifiers)

    cands = [
        r for r in results
        if isinstance(r, dict)
        and (r.get("syncedLyrics") or r.get("plainLyrics"))
        and _artist_ok(r)
        and _title_ok(r)
    ]
    if not cands:
        return None

    if duration and duration > 0:
        def _delta(r):
            try:
                return abs(float(r.get("duration") or 0) - float(duration))
            except (TypeError, ValueError):
                return float("inf")
        # Closest duration first; a synced result breaks a tie.
        cands.sort(key=lambda r: (_delta(r), 0 if (r.get("syncedLyrics") or "").strip() else 1))
        best = cands[0]
        if _delta(best) > _LRCLIB_DUR_MAX_S:
            return None                        # nothing close enough — refuse
        return best

    # No duration to disambiguate — prefer a synced candidate, else the first.
    cands.sort(key=lambda r: 0 if (r.get("syncedLyrics") or "").strip() else 1)
    return cands[0]


_ALLOWED_SORT_KEYS = {
    "added", "year", "duration", "bpm",
    "title", "artist", "album_artist", "album", "format",
}


@router.get("")
async def list_tracks(
    limit: int = Query(50, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    format: str | None = Query(
        None,
        description=(
            "Optional format filter (e.g. 'MIDI', 'ProTracker').  Drives the "
            "library Galaxy view's windowed per-format browse.  Matched "
            "case-insensitively against the store's format index."
        ),
    ),
    sort: str | None = Query(
        None,
        description=(
            "Sort key: added (default, newest first), year, duration, bpm, "
            "title, artist, album, format."
        ),
    ),
    order: str | None = Query(
        None,
        description=(
            "Sort direction: asc or desc.  Defaults to desc for 'added' and "
            "asc for every other key."
        ),
    ),
):
    """Return all tracks (paginated), sorted by added_at desc by default.

    The All Tracks windowed view passes ``sort=<col>&order=<asc|desc>`` to
    drive the per-column lexical / numeric sort indexes maintained by the
    in-memory store: pages are slices of those sorted indexes (O(limit)).
    With duplicates hidden, a page at offset >= 5,000 slices a primary-only
    order memoised until the next track write (one O(N) build, ~125 ms at
    263K tracks, then ~1 ms per 500-row page); shallower pages walk and filter.
    """
    # Defensive whitelist — silently ignore unknown sort keys so a stale
    # frontend can't 400 the page; we fall back to the default sort instead.
    sort_by = sort if sort in _ALLOWED_SORT_KEYS else None
    sort_order = order if order in ("asc", "desc") else None
    if format:
        # ft_search parses @format:{value} → store.filter_tracks(format_=value),
        # matched case-insensitively.  _esc_tag keeps odd format names (spaces,
        # slashes) from breaking the tag-query parse.
        from soniqboom.api.search import _esc_tag
        query = f"@format:{{{_esc_tag(format)}}}"
    else:
        query = "*"
    # Hot path: return the store's dicts serialized straight to JSON bytes with
    # orjson, skipping the per-row TrackMeta construction + Pydantic re-encode
    # (~12 ms/page on a 2000-track page).  The dicts are already the TrackMeta
    # field set (``_meta_dict`` strips ``embedding``), so the response bytes are
    # identical to the response_model path — we just trade this endpoint's
    # OpenAPI schema for the speed.  See data.ft_search_dicts.  (``json_bytes``:
    # a lone surrogate in one path no longer 500s the page.)
    dicts = await ft_search_dicts(
        query, limit=limit, offset=offset,
        sort_by=sort_by, sort_order=sort_order,
    )
    return Response(content=json_bytes(dicts), media_type="application/json")


@router.get("/count")
async def count_tracks():
    """Library size.

    ``count`` is the raw number of indexed tracks.  ``visible`` is how many rows
    the All-Tracks LIST actually serves: with the "hide duplicates" setting on,
    ``GET /api/tracks`` returns duplicate-group primaries only, so a list sized
    from ``count`` would end in rows that never load.  Size lists from
    ``visible``.
    """
    from soniqboom.core.store import get_store
    store = get_store()
    total = await track_count()
    hide_dups = bool(store.get_config("filter_duplicates", False))
    return {"count": total,
            "visible": store.primary_track_count() if hide_dups else total}


@router.get("/shuffled")
async def shuffled_tracks(
    seed: int = Query(..., description="Shuffle seed — same seed ⇒ same order."),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    q: str | None = Query(None, description="Search-box query (same syntax as /api/search)."),
    artist: str | None = None,
    album_artist: str | None = None,
    album: str | None = None,
    genre: str | None = None,
    scene_group: str | None = None,
    format: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    untagged: str | None = Query(None, pattern="^(artist|album_artist|album|genre)$"),
):
    """One page of a seeded SHUFFLE ORDER over every track matching the filter.

    The play-queue counterpart of ``GET /api/tracks`` / ``/api/search/filter``:
    same predicates (resolved by the store's single ``_candidate_ids`` resolver,
    honouring the "hide duplicates" setting exactly like those lists do), but
    ordered as a pseudo-random permutation of the WHOLE result set instead of a
    sorted page.  The client pages through it sequentially, so shuffle covers
    every matching track — not just the rows its list happens to have loaded —
    with no repeats until the set is exhausted.

    Response: ``{"total", "seed", "offset", "tracks": [TrackMeta…]}``.
    """
    from soniqboom.core import shuffle_order
    from soniqboom.core.data import _parse_tag_query
    from soniqboom.core.store import get_store

    store = get_store()
    seed = shuffle_order.normalize_seed(seed)
    hide_dups = bool(store.get_config("filter_duplicates", False))

    preds: dict = {}
    if q:
        # Identical parse path to /api/search so "shuffle these results" means
        # exactly the tracks the search would list (uncapped).
        from soniqboom.api.search import _parse_advanced_query
        advanced = _parse_advanced_query(q)
        tagq = advanced or q.replace("-", "\\-").replace(":", "\\:").replace("/", "\\/")
        preds.update(_parse_tag_query(tagq))
    for name, val in (("artist", artist), ("album_artist", album_artist),
                      ("album", album), ("genre", genre),
                      ("scene_group", scene_group), ("format_", format),
                      ("year_min", year_min), ("year_max", year_max),
                      ("untagged", untagged)):
        if val is not None and val != "":
            preds[name] = val

    # No store version in the key — see ``shuffle_order``: a seed belongs to one
    # shuffle session, the order is mutation-stable, and ids deleted since are
    # skipped below.
    cache_key = ("tracks", hide_dups, seed,
                 tuple(sorted((k, str(v)) for k, v in preds.items())))
    order = shuffle_order.peek(cache_key)
    if order is None:
        # The store is not thread-safe, so the candidate ids are resolved here on
        # the loop (index intersections; ``untagged`` and the dedup filter are
        # O(N) passes — tens of ms on a 260 K library).  The hash+sort of that
        # SNAPSHOT (~150 ms, once per seed+filter) runs in a worker thread so the
        # loop keeps getting slices; later pages are O(limit).
        ids_snapshot = store.filter_track_ids(filter_duplicates=hide_dups, **preds)
        order = await asyncio.to_thread(
            shuffle_order.cached_order, cache_key, lambda: ids_snapshot, seed)

    total = len(order)
    page_ids = order[offset: offset + limit]
    tracks = [store._meta_dict(tid) for tid in page_ids if tid in store._tracks]
    return Response(
        content=json_bytes({"total": total, "seed": seed, "offset": offset,
                            "tracks": tracks}),
        media_type="application/json",
    )


# ── Ratings (batch endpoints — must be before /{track_id} to avoid capture) ──

@router.get("/meta/ratings")
async def all_ratings():
    """Return all ratings as {track_id: rating}."""
    return await get_all_ratings()


@router.post("/meta/ratings/batch")
async def batch_ratings(body: dict):
    """Return ratings for a list of track IDs."""
    ids = body.get("ids", [])
    return await get_ratings_batch(ids)


@router.get("/meta/playstats")
async def all_play_stats_endpoint():
    """Return all play stats as {track_id: {count, last_played}}."""
    return await get_all_play_stats()


@router.post("/meta/playstats/batch")
async def batch_play_stats(body: dict):
    """Return play stats for a list of track IDs."""
    ids = body.get("ids", [])
    return await get_play_stats_batch(ids)


@json_route(router, "/meta/batch", methods=("POST",))
async def batch_tracks(body: dict):
    """Return full track objects for a list of IDs in ONE request.

    Hydrates client-side id lists (e.g. the History Smart view's play-log
    entries) without an N+1 storm of ``GET /api/tracks/{id}`` round-trips —
    each id is an in-memory lookup, so N of them in a single request is
    cheap.  Unknown ids are skipped; order follows the request.  Capped to
    keep a pathological request bounded.

    Rows are TrackMeta-shaped (``public_track``), encoded with orjson: 5000 ids
    took ~430 ms through ``Track(**d)`` + ``jsonable_encoder`` per row, now
    ~25 ms.  They used to carry the ``embedding`` vector (every client dropped
    it).  A row ``TrackMeta`` rejects is still skipped; one the old ``Track``
    rejected only for a malformed embedding is now served.
    """
    ids = body.get("ids", [])
    if not isinstance(ids, list):
        raise HTTPException(422, "ids must be a list")
    from soniqboom.core.store import get_store
    # A non-string id can't name a track — skipped like an unknown one (an
    # unhashable one used to 500 the whole batch).
    return public_tracks(get_store().get_tracks_batch(
        [tid for tid in ids[:5000] if isinstance(tid, str)]))


@json_route(router, "/{track_id}")
async def read_track(track_id: str):
    """One track, TrackMeta-shaped (the old ``response_model=TrackMeta``
    output); 404 when unknown or when ``TrackMeta`` rejects its stored row, as
    the old ``get_track`` → ``Track(**d)`` did (that one also 404'd a row whose
    only fault was a malformed embedding, which is now served)."""
    from soniqboom.core.store import get_store
    track = public_track(get_store().get_track(track_id))
    if track is None:
        raise HTTPException(404, "Track not found")
    return track


@router.delete("/{track_id}")
async def remove_track(track_id: str, _user=_Depends(_require_edit)):
    removed = await delete_track(track_id)
    if not removed:
        raise HTTPException(404, "Track not found")
    _forget_lyrics(track_id)
    return {"deleted": track_id}


@router.get("/{track_id}/extended")
async def get_track_extended(track_id: str):
    """Return extended metadata for tracker/SID/MIDI files."""
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    if not track.subsongs:
        # A libgme rip scanned before tune counts were read (plain local file).
        p = str(track.path or "")
        if p and "::" not in p and not is_remote_path(p):
            from soniqboom.api import stream as _stream
            track = await _stream._gme_backfill_tune_count(track_id, track, Path(p))
    default_track = await _default_track(track_id, track)
    default_tune, default_pending = await _default_tune(track_id, track, default_track)

    result = {
        "format": track.format,
        "instruments": track.instruments or [],
        "channels": track.channels,
        "patterns": track.patterns,
        "subsongs": track.subsongs,
        # The file's own start song (1-based: the PSID start song / SNDH
        # ``!#`` — what wire 0 plays), or ``None`` (tune 1).  It drives the
        # wire ↔ tune mapping: ``stream.sid_wire_tune`` (utils.js
        # ``subsongWireToTune``).
        "default_track": default_track,
        # The tune (1-based) a play of the bare id plays: the start song
        # above for SID / SNDH; for a multi-tune Amiga module, console rip
        # or SC68 disk the first tune that isn't empty (probed once — here
        # too, when the file is at hand); else tune 1.  ``None`` while it
        # isn't known yet (a play finds it out).  Never moves a wire.
        "default_tune": default_tune,
        # True while that tune is still being found out (a probe running past
        # the short wait below): ask again shortly for ``default_tune``.
        "default_tune_pending": default_pending,
        # Per-tune lengths (seconds, in tune order: index = tune - 1), when
        # an HVSC Songlengths DB is configured; else None — the picker then
        # shows tune numbers without times (graceful degrade).
        "hvsc_lengths": getattr(track, "hvsc_lengths", None),
        # HVSC STIL commentary blob (raw text) for SID files, when configured;
        # else None.  The client parses the ``(#N)`` subtune markers into
        # per-tune titles for the picker and shows the file-level comment as a
        # "STIL" panel.  We ship the raw text (not a parsed structure) so the
        # backend stays agnostic to STIL's freeform layout.
        "stil": getattr(track, "stil", None),
        # SID chip model ("6581" / "8580" / "6581/8580"), read from the PSID
        # header at scan time; None when the header didn't specify one.  Drives
        # a chip badge in the Track-Info header.
        "sid_model": getattr(track, "sid_model", None),
        # Known playback defect flagged at scan ("partial" | "corrupt") + human
        # context — drives the health badge in the info panel + listings.
        "defect": getattr(track, "defect", None),
        "defect_detail": getattr(track, "defect_detail", None),
    }
    return result


_DEFAULT_TUNE_WAIT_S = 5.0     # bound of each background step (extract, probe)
_DEFAULT_TUNE_REPLY_S = 1.5    # how long /extended waits before answering "pending"
_DEFAULT_TUNE_LOOKUPS: "dict[str, asyncio.Task]" = {}


async def _default_tune(track_id: str, track, default_track: "int | None") -> "tuple[int | None, bool]":
    """``(tune, pending)``: the 1-based tune a bare play of a multi-tune file
    plays (see /extended), and whether it is still being found out.  SID /
    SNDH: their start song.  uade / libgme / sc68: the first tune that isn't
    empty (``stream.ensure_default_tune``) — probed on the file itself when
    it is a plain local file, on its extracted / fetched copy when that is
    local already (an archive member, a cached remote file), else taken from
    a probe a play is running.  That lookup runs as one background task per
    track: the reply waits ``_DEFAULT_TUNE_REPLY_S`` for it, then answers
    ``(None, True)`` while it goes on (its result lands in the probe memo).
    Other multi-tune files: tune 1.  ``(None, False)`` for single-tune files
    and when nothing here can find it out."""
    if not (isinstance(track.subsongs, int) and track.subsongs > 1):
        return None, False
    from soniqboom.api import stream as _stream
    if _stream._header_tuned(track):
        return (default_track or 1), False
    decided = _stream._default_tune_decided(track_id, track)
    if decided is not None:
        return decided + 1, False
    # An undecided probe's pick is what a bare play plays now — still
    # provisional while a probe goes on (the reply says so: ask again).
    pick = _stream.default_tune_known(track_id, track)
    if pick is not None:
        return pick + 1, _stream.default_probe_running(track_id)
    task = _DEFAULT_TUNE_LOOKUPS.get(track_id)
    if task is None or task.done():
        task = asyncio.ensure_future(_find_default_tune(track_id, track))
        _DEFAULT_TUNE_LOOKUPS[track_id] = task

        def _forget(t, tid=track_id):
            if _DEFAULT_TUNE_LOOKUPS.get(tid) is t:
                _DEFAULT_TUNE_LOOKUPS.pop(tid, None)
        task.add_done_callback(_forget)
    try:
        got = await asyncio.wait_for(asyncio.shield(task), _DEFAULT_TUNE_REPLY_S)
    except asyncio.TimeoutError:
        got = None
    except Exception:
        return None, False
    # Not decided yet: report the pick so far (if any) and keep the client
    # asking while the lookup or a probe still runs.
    if _stream._default_tune_decided(track_id, track) is not None:
        return _stream._default_tune_decided(track_id, track) + 1, False
    running = (not task.done()) or _stream.default_probe_running(track_id)
    if got is None:
        pick = _stream.default_tune_known(track_id, track)
        got = pick + 1 if pick is not None else None
    return got, running


async def _find_default_tune(track_id: str, track) -> "int | None":
    """The background half of ``_default_tune`` (1-based, or None).  Never
    raises; each step is bounded by ``_DEFAULT_TUNE_WAIT_S``."""
    from soniqboom.api import stream as _stream
    p = str(track.path or "")
    pin = None
    try:
        ext, uade_named = _stream._render_ident(p, track)
        if _stream._probe_family(ext, uade_named) is None and ext not in _stream._SID_EXTS:
            return 1                              # tracker / HVL: tune 1 plays
        local = bool(p) and "::" not in p and not is_remote_path(p)
        if local:
            path = Path(p)
        elif _stream.default_probe_running(track_id):
            got = await _stream.await_default_probe(track_id, _DEFAULT_TUNE_WAIT_S)
            return got + 1 if got is not None else None
        elif (("::" in p and not is_remote_path(p))
              or (is_remote_path(p) and _stream._remote_bytes_local(p))):
            # An archive member (extracted like a play extracts it) or a
            # remote file already in the local cache.
            path, ext, uade_named, pin = await asyncio.wait_for(
                _stream._resolve_play_source(track_id, track, lane="scan"),
                timeout=_DEFAULT_TUNE_WAIT_S)
        else:
            return None                       # not fetched here just for this
        fam = _stream._probe_family(ext, uade_named, path)
        if fam is None:
            return 1
        got = await asyncio.wait_for(
            _stream.ensure_default_tune(track_id, track, path, fam, background=True),
            timeout=_DEFAULT_TUNE_WAIT_S)
        return got + 1 if got is not None else None
    except Exception:
        return None
    finally:
        if pin:
            try:
                _stream._zip_unpin(pin)
            except Exception:
                pass


async def _default_track(track_id: str, track) -> int | None:
    """The 1-based start song of a multi-tune SID / SNDH (see /extended):
    the one the scan recorded, else read from a plain local file's header
    (SID: 18 bytes; SNDH: ``psgplay -i``) — the renderers read the same.
    None for other formats (their wires are plain tune numbers), single-tune
    files and sources that aren't local (the picker then assumes tune 1)."""
    if not (isinstance(track.subsongs, int) and track.subsongs > 1):
        return None
    from soniqboom.api import stream as _stream
    if not _stream._header_tuned(track):
        return None
    p = str(track.path or "")
    local = bool(p) and "::" not in p and not is_remote_path(p)
    rec = _stream._recorded_start_song(track)
    if rec is not None:
        return rec
    fam = str(track.format or "").split("/")[0].strip().upper()
    if fam == "SID":
        got = _stream._SID_START_SONG.get(track_id)
        if got is None and local:
            got = await _stream.sid_start_song(track_id, track, Path(p))
        return got
    if fam == "SNDH" and local:
        try:
            start, _times, count = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _stream._sndh_info, Path(p)),
                timeout=10.0)
        except Exception:
            return None
        start = _stream.sid_wire_tune(0, start, count)
        _stream._persist_start_song(track_id, start, count)
        return start
    return None


# Format names (metadata.FORMAT_NAMES values) whose files libopenmpt can
# parse into a pattern grid.  AHX/HivelyTracker are uade/hvl2wav territory
# and SID/MIDI have no pattern grid — deliberately absent.
_PATTERN_FORMAT_NAMES = frozenset({
    "ProTracker", "ScreamTracker 3", "ScreamTracker 2", "FastTracker 2",
    "Impulse Tracker", "MultiTracker", "OctaMED", "Composer 669",
    "DigiBooster Pro", "UltraTracker", "Farandole", "ASYLUM/DMP",
    "General DigiMusic", "Imago Orpheus", "Oktalyzer", "SoundFX",
    "Grave Composer", "DSIK",
})


def _read_module_bytes(path_str: str) -> bytes | None:
    """Raw module bytes for a local / archive-virtual / remote / composite
    remote-archive path.  Thin alias over the shared canonical resolver
    (``core.source_bytes.read_source_bytes``) so the SID-bytes endpoint, the
    VU backfill, and the core services (hvsc_apply / repair / art_backfill)
    all share ONE ``::``-before-remote implementation.  Blocking — run in an
    executor.  Never raises; returns None on any miss."""
    from soniqboom.core.source_bytes import read_source_bytes
    return read_source_bytes(path_str)


# Extracted pattern payloads, LRU keyed by (track_id, mtime).  The row→time
# map costs one libopenmpt seek per row, which grows with order count
# (~3.7 s for an 82-order S3M) — re-opening the same track's info modal
# shouldn't re-pay it.  The mtime is part of the key so an in-place edit +
# rescan (same path → same track_id, new mtime) invalidates the entry
# instead of serving a stale grid; nothing else clears this cache.  Tiny
# (≤16 payloads).
_PATTERNS_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_PATTERNS_CACHE_MAX = 16
# Coalesce concurrent cache-misses for the SAME (track_id, mtime): the
# extraction is a multi-second libopenmpt seek, so a second request that lands
# mid-flight awaits the first computation instead of re-paying it.
_PATTERNS_INFLIGHT: "dict[tuple, asyncio.Future]" = {}


@router.get("/{track_id}/patterns")
async def get_patterns(track_id: str):
    """Return the tracker pattern grid, order list, row→time map, song
    message and initial tempo for a module — extracted in-process via
    libopenmpt (see ``core/tracker_patterns.py`` for the payload
    contract).  Drives the Track-Info "Patterns" and "Song message"
    sections.  Returns ``{"available": False}`` for non-tracker files,
    unreachable sources, or hosts without libopenmpt."""
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    if (track.format or "") not in _PATTERN_FORMAT_NAMES:
        return {"id": track_id, "available": False, "channels": 0,
                "order": [], "patterns": []}
    cache_key = (track_id, getattr(track, "mtime", None))
    cached = _PATTERNS_CACHE.get(cache_key)
    if cached is not None:
        _PATTERNS_CACHE.move_to_end(cache_key)
        return cached
    # No await between the cache-miss above and this in-flight check, so the two
    # lookups are atomic under asyncio: a concurrent request either sees the
    # cache populated (owner fully done) or joins this future (owner still in
    # the executor) — never recomputes redundantly.
    inflight = _PATTERNS_INFLIGHT.get(cache_key)
    if inflight is not None:
        return await inflight
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    _PATTERNS_INFLIGHT[cache_key] = fut
    try:
        data = await loop.run_in_executor(None, _read_module_bytes, track.path)
        if not data:
            payload = {"id": track_id, "available": False, "channels": 0,
                       "order": [], "patterns": []}
        else:
            from soniqboom.core.tracker_patterns import extract_patterns
            payload = await loop.run_in_executor(None, extract_patterns, data)
            payload["id"] = track_id
            if payload.get("available"):
                _PATTERNS_CACHE[cache_key] = payload
                while len(_PATTERNS_CACHE) > _PATTERNS_CACHE_MAX:
                    _PATTERNS_CACHE.popitem(last=False)
        fut.set_result(payload)
    except Exception as exc:  # propagate to the owner AND any joined waiters
        fut.set_exception(exc)
    finally:
        # Pop and set_result happen with no await between them, so no coroutine
        # can observe a populated cache with a stale in-flight entry.
        _PATTERNS_INFLIGHT.pop(cache_key, None)
    return await fut


@router.get("/{track_id}/vu")
async def get_vu_sidecar(track_id: str, subsong: int | None = Query(None, ge=0, le=1024),
                         start: bool = Query(True), request: Request = None):
    """Return the binary VUMR sidecar for a rendered tracker module.

    ``start=false``: a miss never starts the Amiga per-voice pass (the
    player asks that way when a track loads, and starts the pass once it
    has played a few seconds).

    ``subsong`` (0-based wire index) selects the tune: a multi-subsong
    UADE/tracker file renders a distinct sidecar per tune, so the meters
    match the tune actually playing.  The frontend passes the current
    subsong when the playing track carries one (mirroring the ``?subsong=``
    it threads onto the stream URL); plain playback omits it → the tune a
    bare play renders (``stream.tune_index``).

    Tracker / chip-format renders produce a per-channel VU sidecar
    alongside the audio cache (see ``docs/vu-cache-format.md``).  The
    frontend fetches this once on track-load and drives the per-channel
    VU bars from it — random-access by frame index against
    ``audio.currentTime``.

    Lazy backfill
    -------------
    When the sidecar doesn't exist yet but the track IS a tracker
    format AND the source file is reachable, we run an in-process VU
    extraction pass on the source file directly.  Result is cached
    alongside the existing audio WAV.  This covers the "v1.3.0 just
    shipped, my 60 K-file library has audio caches but no .vu yet"
    case without forcing the user to wait for natural cache eviction.

    First-call latency: typically < 0.5 s for a sub-5-minute module
    (libopenmpt advances the mixer state at ~1500× real-time per the
    bench in core/openmpt_vu.py).  Cached on disk forever after — a
    given track's sidecar is generated once per (track, subsong)
    pair.

    Returns:
      * 200 ``application/octet-stream`` with the VUMR binary +
        immutable cache headers, when a sidecar exists or was
        just generated.
      * 404 when the track isn't a tracker format, the source file
        can't be reached, or libopenmpt isn't available on this
        host.  The frontend falls back to its FFT-spectrum
        visualiser with the honest label.  For an Amiga track whose
        meters can't come at all it carries ``X-VU-Unavailable``
        (``off``: the setting is off; ``unsupported``: this uade build
        can't dump voices; ``skipped``: this tune's pass ran without a
        result — longer than the pass allows, or uade failed on it), so
        the player stops asking.
    """
    from fastapi.responses import Response
    from soniqboom.core.conversion_cache import (
        get_vu_sidecar_path, _cache_path,
    )

    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")

    from soniqboom.api import stream as _stream
    # The tune index renders (and their sidecars) are keyed by: the tune
    # asked for, or the one a bare play renders.
    subsong = _stream.tune_index(
        track_id, track,
        _stream.explicit_wire(subsong if isinstance(subsong, int) else None, request))
    sidecar = get_vu_sidecar_path(
        track_id, subsong,
        uade_variant=_stream.uade_cache_variant(
            subsong, _stream._uade_base_known(track_id, track) or 0))
    if sidecar is None:
        # Amiga (uade) meters come from a second full uade pass, started
        # only here — i.e. only when the web now-playing meter asks for the
        # track that is actually playing.  It runs in the background; this
        # request 404s and the player's poll picks the sidecar up later.
        if start is not False:
            _stream.request_uade_vu(track_id, subsong)
        # Lazy-backfill path.  Only attempt for known tracker formats
        # (we don't want to spin up libopenmpt against a 10 GB FLAC).
        sidecar = await _try_backfill_vu_sidecar(track, track_id, subsong)

    if sidecar is None:
        # An Amiga track whose meters can't come (the setting is off, this
        # uade build can't dump voices, or this tune's pass already ran
        # without a result): say so, so the player stops polling.  Every
        # other miss — a pass pending or running, other formats — stays a
        # plain 404 and the player's retry ladder keeps asking.
        reason = _stream.uade_vu_unavailable_reason()
        if reason is not None and (
                _stream.is_uade_routed(getattr(track, "path", "") or "", track)
                or _stream._ck(track_id, "uade", subsong=subsong) in _stream._UADE_VU_WANTED):
            raise HTTPException(404, "Per-voice Amiga meters unavailable",
                                headers={"X-VU-Unavailable": reason})
        skipped = _stream.uade_vu_skipped_reason(track_id, subsong)
        if skipped is not None:
            raise HTTPException(404, "No per-voice meters for this tune",
                                headers={"X-VU-Unavailable": skipped})
        raise HTTPException(404, "No VU sidecar (not a tracker render or libopenmpt unavailable)")
    try:
        data = sidecar.read_bytes()
    except OSError:
        raise HTTPException(404, "VU sidecar unreadable")
    import hashlib
    etag = f'"{hashlib.sha256(data).hexdigest()[:16]}"'
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag":          etag,
            "X-VU-Version":  "1",
        },
    )


def _is_sid_format(track) -> bool:
    """True if *track* is a C64 SID (format primary name ``SID``) — the gate
    for both the raw-bytes and VU-upload endpoints.  Amiga SidMon ``*.sid``
    carries a uade format name, not ``SID``."""
    return (str(track.format or "").split("/")[0].strip() == "SID")


@router.get("/{track_id}/sid")
async def get_sid_bytes(track_id: str, _user=_Depends(_require_user)):
    """Serve the raw C64 SID container bytes for the client-side WASM VU worker.

    The worker (``frontend/js/vu-sid-worker.js``) renders the tune in-browser to
    produce the per-voice VU sidecar, offloading the server's 3-pass sidplayfp
    render.  This is the ONLY route that returns SID source bytes (normal
    playback always transcodes to WAV), so it is tightly gated: any signed-in
    user (read), SID-format tracks only, and a PSID/RSID magic re-check on the
    resolved bytes (415 for an Amiga SidMon ``*.sid``).  Bytes are immutable and
    tiny (a few KB) → a plain immutable-cached Response, no range needed."""
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    if not _is_sid_format(track):
        raise HTTPException(415, "Not a C64 SID track")
    # Bound a mislabelled/replaced file BEFORE reading it all into RAM.  For a
    # plain local path we can stat cheaply; remote/zip sources fall through to
    # the post-read length check (those members are tiny + fetched anyway).
    _pstr = str(track.path)
    if "::" not in _pstr and not is_remote_path(_pstr):
        try:
            if Path(_pstr).stat().st_size > 1024 * 1024:   # SIDs are < 64 KB
                raise HTTPException(415, "File too large to be a SID")
        except OSError:
            pass
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _read_module_bytes, track.path)
    if not data:
        raise HTTPException(404, "SID source unreachable")
    if len(data) > 1024 * 1024:                 # remote/zip belt-and-braces
        raise HTTPException(415, "File too large to be a SID")
    if data[:4] not in (b"PSID", b"RSID"):
        raise HTTPException(415, "Not a C64 SID (missing PSID/RSID magic)")
    if len(data) >= 0x12:
        # The worker renders the tune each wire selects from this header; the
        # server's O(1) paths (render length / key, the upload gates) need the
        # same start song.
        from soniqboom.api import stream as _stream
        _count = int.from_bytes(data[0x0E:0x10], "big")
        if _count > 0 and _stream._recorded_start_song(track) is None:
            _start = _stream.sid_wire_tune(0, int.from_bytes(data[0x10:0x12], "big"),
                                           _count)
            _stream._note_sid_start_song(track_id, _start)
            _stream._persist_start_song(track_id, _start, _count)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def _client_tune_mismatch(track_id: str, subsong: int, tune: "int | None") -> bool:
    """A browser-rendered SID for wire ``subsong`` > 0 must say which 1-based
    tune it rendered, and it must be the tune the server renders for that
    wire (``stream.sid_wire_tune`` with the start song the server knows —
    tune ``subsong + 1`` unless the file's default isn't tune 1).  A page
    still running the older worker — which read the wire index as the tune
    number — renders another tune and must never store it in this slot."""
    if subsong <= 0:
        return False
    from soniqboom.api import stream as _stream
    return tune != _stream.sid_wire_tune(
        subsong, _stream.sid_start_song_known(track_id, None))


@router.post("/{track_id}/vu")
async def upload_vu_sidecar(
    track_id: str,
    request: Request,
    subsong: int = Query(0, ge=0, le=1024),
    content_hash: str = Query(..., min_length=64, max_length=64),
    tune: int | None = Query(None, ge=1, le=1025),
    user=_Depends(_require_edit),
):
    """Persist a client-rendered VUMR sidecar into the shared SID cache slot.

    The browser worker computes the per-voice VU (offloading the server's
    sidplayfp render) and uploads it here; we drop it beside the cached SID WAV
    so the existing ``GET /vu`` serves it to every later play, any client.

    Trust model: the VU sidecar drives only the cosmetic per-voice meter, and
    the uploader is a ``require_edit`` user, so this is low-stakes.  The real
    containment (all reject BEFORE any write) is: (1) a 256 KB hard ceiling
    stream-read — no global body limit exists and a chunked upload has no
    Content-Length; (2) strict VUMR structural validation incl. exact length;
    (3) SID must be 3-channel; (4) the write target is SERVER-derived from the
    ``sid``-format cache slot (never a client-supplied path), so an upload can
    only land beside a genuine C64-SID render — 425 when that slot isn't cached
    yet (client retries).  ``content_hash`` is a transit-integrity check only
    (the same client computes body+hash, so it is NOT an anti-forgery gate).
    Idempotent + server-prefers: skip if a ``.vu`` already exists.  For a
    subsong > 0, ``tune`` must name the 1-based tune the browser rendered,
    the one the server renders for that wire — 409 otherwise (see
    ``_client_tune_mismatch``)."""
    import hashlib
    from soniqboom.core.openmpt_vu import (
        parse_and_validate_vumr, write_sidecar_bytes,
    )
    from soniqboom.core.conversion_cache import get_sid_wav_path_for_upload

    if _client_tune_mismatch(track_id, subsong, tune):
        raise HTTPException(409, "client SID renderer is out of date — reload the page")

    # (1) size cap — a chunked upload carries no Content-Length, so the header
    # check alone is bypassable; stream-read with a HARD ceiling and abort the
    # instant we cross it, before the whole body is ever in memory.
    _MAX = 256 * 1024
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > _MAX:
        raise HTTPException(413, "VU sidecar too large")
    _buf = bytearray()
    async for _chunk in request.stream():
        _buf += _chunk
        if len(_buf) > _MAX:
            raise HTTPException(413, "VU sidecar too large")
    raw = bytes(_buf)

    # (2)+(3) structural validation.
    try:
        channels, _rate, _frames = parse_and_validate_vumr(raw)
    except ValueError as exc:
        raise HTTPException(422, f"invalid VUMR: {exc}")
    if channels != 3:
        raise HTTPException(422, "SID VU sidecar must have 3 channels")

    # (4) content-hash integrity.
    if hashlib.sha256(raw).hexdigest() != content_hash.lower():
        raise HTTPException(422, "content_hash mismatch")

    # track existence + SID gate (belt-and-braces; the slot scan also gates).
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    if not _is_sid_format(track):
        raise HTTPException(415, "Not a C64 SID track")

    # (5) SERVER-derived destination slot.
    wav_path = get_sid_wav_path_for_upload(track_id, subsong)
    if wav_path is None:
        raise HTTPException(425, "SID WAV not cached yet — retry")
    vu_path = wav_path.with_suffix(".vu")

    # Idempotent, server-wins: never overwrite an existing sidecar.
    if vu_path.exists():
        return Response(status_code=204)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, write_sidecar_bytes, vu_path, raw)
    return Response(status_code=204)


# Bound concurrent client SID-WAV uploads (A2): each body is spooled to disk, but
# the semaphore + the Content-Length gate stop N concurrent ~50 MB uploads from
# hammering a low-power box's RAM / disk / CPU all at once.
_SID_UPLOAD_SEM = asyncio.Semaphore(2)


def _validate_sid_wav(header: bytes, total_len: int, expect_data_bytes: int) -> str | None:
    """Validate a client-rendered SID WAV from its 44-byte canonical header + the
    total streamed byte count (the body is spooled to disk, never held in RAM).
    Canonical RIFF/WAVE, mono / 44100 / 16-bit PCM, and a data chunk within ~1 s
    of the expected length (rejects a truncated WAV under a full-length key).
    The WASM worker writes exactly this canonical layout (vu-sid-worker.js
    buildWav), so a non-canonical upload is refused."""
    import struct
    if len(header) < 44:
        return "shorter than a WAV header"
    if header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
        return "not a RIFF/WAVE"
    if header[12:16] != b"fmt ":
        return "no fmt chunk at offset 12"
    audio_format, channels, rate = struct.unpack("<HHI", header[20:28])
    bits = struct.unpack("<H", header[34:36])[0]
    if audio_format != 1:
        return "not PCM"
    if channels != 1:
        return f"not mono ({channels} channels)"
    if rate != 44100:
        return f"sample rate {rate} != 44100"
    if bits != 16:
        return f"{bits}-bit != 16-bit"
    if header[36:40] != b"data":
        return "no data chunk at offset 36"
    data_bytes = struct.unpack("<I", header[40:44])[0]
    if abs(data_bytes - expect_data_bytes) > 88200 + 4096:
        return f"data length {data_bytes} not within 1 s of expected {expect_data_bytes}"
    if total_len < 44 + data_bytes - 4096:
        return "body truncated (fewer bytes than the declared data chunk)"
    return None


@router.post("/{track_id}/sid-audio")
async def upload_sid_audio(
    track_id: str,
    request: Request,
    subsong: int = Query(0, ge=0, le=1024),
    duration: int = Query(..., ge=1, le=3600),
    wav_sha256: str = Query(..., min_length=64, max_length=64),
    tune: int | None = Query(None, ge=1, le=1025),
    user=_Depends(_require_admin),
):
    """Cache-warm: persist a CLIENT-rendered SID WAV into the exact conversion-
    cache slot the SERVER render would occupy, so every later play (any client,
    cast, offline, Subsonic) streams it via the normal cached-file path with
    ZERO ``sidplayfp`` render.  This is what lets a low-power box never re-render
    a SID once a capable browser has played it once.

    Companion to ``POST /vu`` (the VU sidecar upload); same trust tier and the
    same "reject BEFORE any write" containment:
      (1) ``_require_admin`` — the stored WAV is served verbatim to EVERY later
          consumer (anonymous stream, cast, offline SW, Subsonic), and SID PCM is
          non-deterministic so no hash can prove a client sent the real render;
          admin-only bounds that audio-injection capability to the operator.
          (Non-admin plays still get a warm cache via the server's own render.)
      (2) ``_is_sid_format`` gate → 415 (Amiga SidMon ``*.sid`` renders under uade).
      (3) ``target_dur`` is RE-DERIVED server-side, IDENTICAL to ``render_status``
          / the SID stream branch; a client ``duration`` that disagrees → 409, so
          the warmed slot always matches the key ``/stream`` later requests (a
          mismatch would be invisible — the server would simply re-render).
      (4) ``sid_warm_eligible()``: only when ALL SID fidelity settings are default,
          so the key carries no fidelity suffix AND the WASM render (which has no
          chip-model/filter/curve/digiboost setter) actually matches the server
          → 409 otherwise.  Blocks cross-chip-model cache poisoning.
      (5) streamed body with a HARD ceiling derived from ``target_dur``; WAV
          structural validation (mono/44100/16-bit, data length within ~1 s).
      (6) ``wav_sha256`` = transit-integrity only (the same client computes
          body+hash; NOT anti-forgery — SID PCM is non-deterministic so no hash
          can prove fidelity; the authenticated ADMIN user IS the trust boundary).
      (7) per-key lock + ``get_cached`` re-check → idempotent, server-wins skip.
      (8) subsong > 0: ``tune`` must name the rendered 1-based tune, the one
          the server renders for that wire → 409 otherwise
          (``_client_tune_mismatch``).
    """
    from soniqboom.core.conversion_cache import (
        _cache_key, get_cached, store_cached, _lock_for,
        get_conversion_cache_dir, sid_warm_eligible,
    )

    if _client_tune_mismatch(track_id, subsong, tune):
        raise HTTPException(409, "client SID renderer is out of date — reload the page")

    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    if not _is_sid_format(track):
        raise HTTPException(415, "Not a C64 SID track")

    # (3) RE-derive target_dur exactly like render_status / the SID stream branch.
    from soniqboom.api import stream as _stream
    target_dur = _stream._sid_target_seconds(track, subsong)
    if int(duration) != target_dur:
        raise HTTPException(409, f"duration mismatch — server target is {target_dur}s")

    # (4) fidelity gate — refuse warming a slot the WASM can't reproduce.
    if not sid_warm_eligible():
        raise HTTPException(409, "server SID fidelity is non-default — cannot cache-warm")

    # (5) Bound RAM + concurrency (A2): require Content-Length (reject chunked so
    # we can't be forced to buffer an unbounded body), cap the size, gate
    # concurrent uploads, and SPOOL the streamed body straight to a temp file so
    # the ~50 MB WAV never sits in RAM.  mono/44100/16-bit = 88200 bytes/s.
    _EXPECT = target_dur * 88200
    _MAX = min(_EXPECT + 2 * 88200 + 4096, 64 * 1024 * 1024)
    clen = request.headers.get("content-length")
    if not (clen and clen.isdigit()):
        raise HTTPException(411, "Content-Length required")
    if int(clen) > _MAX:
        raise HTTPException(413, "SID WAV too large")

    full_key = _cache_key(track_id, "sid", subsong, duration=target_dur)
    async with _SID_UPLOAD_SEM:
        import hashlib
        import tempfile
        cdir = get_conversion_cache_dir() / "sid"
        cdir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".wav", dir=str(cdir))
        digest = hashlib.sha256()
        header = bytearray()
        total = 0
        try:
            with os.fdopen(fd, "wb") as fh:
                async for _chunk in request.stream():
                    total += len(_chunk)
                    if total > _MAX:
                        raise HTTPException(413, "SID WAV too large")
                    digest.update(_chunk)
                    if len(header) < 44:
                        header += _chunk[: 44 - len(header)]
                    fh.write(_chunk)
            if digest.hexdigest() != wav_sha256.lower():
                raise HTTPException(422, "wav_sha256 mismatch")
            err = _validate_sid_wav(bytes(header), total, _EXPECT)
            if err:
                raise HTTPException(422, f"invalid SID WAV: {err}")
            # (7) per-key lock: idempotent, server-wins — the spooled temp file is
            # moved into the keyed slot, or dropped if a render beat us to it.
            async with _lock_for(full_key):
                if await get_cached(full_key) is not None:
                    return Response(status_code=204)      # already warmed — first-wins
                # A silent render is never cached, whoever rendered it (422;
                # the file is dropped).
                from soniqboom.core.conversion_cache import refuse_silent
                await refuse_silent(full_key, "sid", Path(tmp))
                await store_cached(full_key, "sid", Path(tmp))
                tmp = None                                # moved into the cache
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return Response(status_code=204)


# Tracker-only formats — gating the lazy backfill so we don't try to
# open a FLAC with libopenmpt.  Matches stream.py's _TRACKER_EXTS but
# imported lazily to avoid a cycle.
_VU_BACKFILL_EXTS = {
    ".mod", ".s3m", ".xm", ".it", ".mptm", ".med", ".oct",
    ".669", ".dbm", ".dsm", ".far", ".gdm", ".imf", ".mtm",
    ".okt", ".sfx", ".stm", ".ult", ".wow",
}


async def _try_backfill_vu_sidecar(track, track_id: str, subsong: int = 0):
    """One-shot VU extraction against a track's source file.

    ``subsong`` (0-based wire index) selects the tune to render so a
    multi-subsong module backfills the meters for the tune being played,
    not always tune 0.  Written to a per-subsong slot so tune 0's and
    tune 3's backfills never overwrite each other.

    Returns the path to the freshly-written sidecar, or None if any
    step fails.  Side effect: writes the sidecar next to the cached
    audio WAV when one exists, else next to the source file under a
    pre-determined conversion-cache path.
    """
    import os
    from pathlib import Path
    from soniqboom.core import openmpt_vu
    from soniqboom.core.conversion_cache import _cache_path

    log = logging.getLogger(__name__)
    if not openmpt_vu.is_available():
        log.debug("VU backfill skipped — libopenmpt unavailable")
        return None
    # Tracker path can be a ZIP-virtual like ``foo.zip::inner.xm``
    # where ``Path().suffix`` walks the LAST component (".xm" — good).
    # For raw nested virtuals like ``a.zip::b.zip::c.xm`` it's the
    # same — Path() ignores the ``::`` separator and treats the whole
    # thing as one name; suffix is still ``.xm``.
    ext = (Path(track.path).suffix or "").lower()
    if ext not in _VU_BACKFILL_EXTS:
        log.debug("VU backfill skipped — ext %r not in tracker set", ext)
        return None
    # Resolve raw module bytes via the shared resolver, the SAME helper the
    # SID-bytes endpoint uses.  It partitions the ``::`` archive tail FIRST,
    # so a composite remote-archive path (``ftp://…foo.zip::inner.mod``)
    # fetches the OUTER ``.zip`` into the local remote-cache — a cache HIT
    # returns the copy playback already downloaded, no re-fetch — and THEN
    # extracts the member before handing bytes to libopenmpt.
    #
    # The previous hand-rolled block here checked ``startswith("ftp://")``
    # BEFORE the ``"::" in path`` case, so a remote-zip module never reached
    # the archive extractor: it fed either a bogus ``…zip::member`` remote path
    # (fetch fails) or the raw ZIP container (unparseable) to libopenmpt →
    # None → 404 → FFT fallback.  ``subsong`` is intentionally NOT applied here;
    # the bytes are subsong-agnostic and the tune is selected downstream in
    # ``extract_vu``.
    path_str = track.path
    src_bytes = await asyncio.get_event_loop().run_in_executor(
        None, _read_module_bytes, path_str,
    )
    if not src_bytes:
        log.debug("VU backfill found no bytes for %s", path_str)
        return None

    loop = asyncio.get_event_loop()
    # extract_vu takes -1 = "libopenmpt default subsong" (what _render_tracker
    # uses for subsong 0 — it only passes --subsong for N>0), and an explicit
    # index for N>0.  Mirror that so the backfilled sidecar matches the streamed
    # render's sidecar frame-for-frame.
    vu_subsong = subsong if subsong > 0 else -1
    result = await loop.run_in_executor(
        None, lambda: openmpt_vu.extract_vu(src_bytes, subsong=vu_subsong),
    )
    if result is None:
        log.warning("VU backfill: extract_vu returned None for %s", path_str)
        return None
    if result.frames == 0:
        log.warning("VU backfill: 0 frames for %s", path_str)
        return None

    # Pick a destination path.  Prefer next to the existing cached
    # WAV (so eviction is uniform); fall back to a freshly-keyed
    # cache slot if no audio cache exists for this track yet.
    try:
        from soniqboom.core.conversion_cache import _meta, _state_lock
        candidate: Path | None = None
        with _state_lock:
            for cache_key, entry in _meta.items():
                # This subsong's cached WAV specifically — write the .vu next to
                # it so eviction stays uniform.  Exact match (not startswith) so
                # subsong 3's backfill doesn't land on subsong 0's WAV.
                if cache_key == f"{track_id}__sub{subsong}" and entry.get("format_type") == "tracker":
                    candidate = Path(entry["path"]).with_suffix(".vu")
                    break
        if candidate is None:
            # No cached WAV for this subsong yet — synthesize a sidecar-only slot
            # keyed per-subsong (the ``__novubackfill`` suffix keeps it from
            # colliding with a real render slot).  Lives in the same cache dir so
            # it gets evicted alongside other tracker assets.
            base = _cache_path(f"{track_id}__sub{subsong}__novubackfill", "tracker")
            candidate = base.with_suffix(".vu")
        await loop.run_in_executor(
            None, openmpt_vu.write_sidecar, candidate, result,
        )
        log.info(
            "VU sidecar backfilled for %s: %d ch × %d frames @ %d Hz → %s",
            track_id, result.channels, result.frames, result.sample_rate, candidate,
        )
        return candidate
    except Exception:
        log.warning("VU backfill write failed for %s", track_id, exc_info=True)
        return None


@router.get("/{track_id}/chapters")
async def get_chapters(track_id: str):
    """Return chapter markers for podcasts / audiobooks / long tracks.

    Reads MP4 ``chpl`` atoms and ID3 ``CHAP`` frames from the file.
    Empty list if the file has no chapters."""
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    path_str = track.path
    if is_remote_path(path_str):
        # Remote / WebDAV path — only check the locally cached copy.
        from soniqboom.core.filesource import parse_remote_path
        from soniqboom.core.remote_cache import get_cache
        scan_root, remote_path = parse_remote_path(path_str)
        if remote_path:
            cached = get_cache().get_cached(scan_root, remote_path)
            path = cached if cached and cached.exists() else None
        else:
            path = None
    else:
        path = Path(path_str)
    if not path or not path.exists():
        return {"id": track_id, "chapters": []}
    from soniqboom.core.chapters import extract_chapters
    loop = asyncio.get_event_loop()
    chapters = await loop.run_in_executor(None, extract_chapters, path)
    return {"id": track_id, "chapters": chapters}


# ── Online lyrics providers (responsiveness-ordered fallback chain) ──────────
# LRCLib is the richer source (synced + fuzzy + duration-gated); lyrics.ovh is
# the fallback (plain-only, exact artist+title, no key).  Both are probed at
# startup — and lazily re-probed — and the FASTER one is tried FIRST, so when
# LRCLib is degraded (it 502'd during testing) the quicker source answers
# without the user waiting on the slow one.  Every result still passes the same
# confidence gate downstream, so the order changes only latency, never which
# lyrics get attached.
_LYRICS_OVH_TIMEOUT_S = 8.0
_PROBE_TIMEOUT_S = 5.0
_PROBE_INTERVAL_S = float(os.environ.get("SONIQBOOM_LYRICS_PROBE_INTERVAL_S", "1800"))  # 30 min


async def _provider_lrclib(client, artist, title, album, duration):
    """LRCLib: exact ``/api/get`` first, then the duration-gated fuzzy
    ``/api/search``.  Each call is guarded so a slow/failed exact still lets the
    fuzzy run.  Returns the lyrics payload; ``_MISS`` when both lookups
    answered cleanly (200 / 404) without a match; None when either failed
    (network error, timeout, 5xx, 429) → the resolver tries the next provider
    and the miss is not remembered."""
    log = logging.getLogger(__name__)
    clean = True
    try:
        params = {"artist_name": artist, "track_name": title}
        if album:
            params["album_name"] = album
        if duration:
            params["duration"] = str(int(duration))
        resp = await client.get("https://lrclib.net/api/get", params=params,
                                timeout=_LRCLIB_GET_TIMEOUT_S)
        if resp.status_code == 200:
            hit = _lrclib_lyrics_payload(resp.json())
            if hit:
                return hit
        elif resp.status_code != 404:
            clean = False
    except Exception:
        clean = False
        log.debug("LRCLib exact lookup failed", exc_info=True)
    try:
        resp = await client.get(
            "https://lrclib.net/api/search",
            params={"artist_name": artist, "track_name": _strip_release_qualifiers(title)},
            timeout=_LRCLIB_SEARCH_TIMEOUT_S,
        )
        if resp.status_code == 200:
            cand = _lrclib_best_match(resp.json(), artist, title, duration)
            if cand:
                hit = _lrclib_lyrics_payload(cand)
                if hit:
                    return hit
        elif resp.status_code != 404:
            clean = False
    except Exception:
        clean = False
        log.debug("LRCLib fuzzy lookup failed", exc_info=True)
    return _MISS if clean else None


async def _provider_lyrics_ovh(client, artist, title, album, duration):
    """lyrics.ovh: one exact ``/v1/{artist}/{title}`` lookup (plain only, no
    key).  Uses the primary artist (feat.-clause dropped) + de-noised title so a
    store-tagged "(Remastered)" / "feat. X" still matches its canonical entry."""
    a = _FEAT_RE.sub("", artist).strip()
    t = _strip_release_qualifiers(title)
    if not a or not t:
        return _MISS
    url = (
        "https://api.lyrics.ovh/v1/"
        f"{urllib.parse.quote(a, safe='')}/{urllib.parse.quote(t, safe='')}"
    )
    resp = await client.get(url, timeout=_LYRICS_OVH_TIMEOUT_S)
    if resp.status_code == 200:
        ly = ((resp.json() or {}).get("lyrics") or "").strip()
        if ly:
            return {"lyrics": ly, "synced": False, "source": "lyrics.ovh"}
        return _MISS
    return _MISS if resp.status_code == 404 else None


# name, provider fn, and a representative "is it up + how fast" probe URL.
_LYRICS_PROVIDERS = [
    {"name": "LRCLib", "fn": _provider_lrclib,
     "probe": "https://lrclib.net/api/get?artist_name=Coldplay&track_name=Yellow"},
    {"name": "lyrics.ovh", "fn": _provider_lyrics_ovh,
     "probe": "https://api.lyrics.ovh/v1/Coldplay/Yellow"},
]
_lyrics_order = list(range(len(_LYRICS_PROVIDERS)))   # provider indices, primary first
_last_probe_ts = 0.0
_probe_in_flight = False


async def probe_lyrics_providers() -> None:
    """Measure each provider's response latency and rank them fastest-first, so
    the faster source is tried first.  A provider that errors/times out sorts
    LAST (used only as a fallback).  Called at startup and lazily re-run every
    ``_PROBE_INTERVAL_S`` — provider health drifts (LRCLib was fast, then 5–13 s
    within a day)."""
    global _lyrics_order, _last_probe_ts, _probe_in_flight
    _probe_in_flight = True
    _last_probe_ts = time.monotonic()
    client = _get_lrclib_client()
    log = logging.getLogger(__name__)

    async def _latency(url: str) -> float:
        t = time.monotonic()
        try:
            await client.get(url, timeout=_PROBE_TIMEOUT_S)
            return time.monotonic() - t          # any HTTP response = "up"
        except Exception:
            return float("inf")                  # down → sort last

    try:
        lats = await asyncio.gather(*(_latency(p["probe"]) for p in _LYRICS_PROVIDERS))
        _lyrics_order = sorted(range(len(_LYRICS_PROVIDERS)), key=lambda i: lats[i])
        log.info(
            "Lyrics providers ranked by responsiveness: %s",
            ", ".join(
                f"{_LYRICS_PROVIDERS[i]['name']}="
                + ("down" if lats[i] == float("inf") else f"{lats[i]:.2f}s")
                for i in _lyrics_order
            ),
        )
    except Exception:
        log.debug("lyrics provider probe failed", exc_info=True)
    finally:
        _probe_in_flight = False


async def _resolve_online_lyrics(artist, title, album, duration):
    """Try the online providers fastest-first; return ``(hit, clean)`` — the
    first confident hit (or None), and whether EVERY provider answered with a
    clean "not found" (only then is a miss worth remembering).
    Kicks a non-blocking background re-probe when the ranking is stale."""
    if not _probe_in_flight and (time.monotonic() - _last_probe_ts) > _PROBE_INTERVAL_S:
        asyncio.create_task(probe_lyrics_providers())
    client = _get_lrclib_client()
    log = logging.getLogger(__name__)
    clean = True
    for i in list(_lyrics_order):
        prov = _LYRICS_PROVIDERS[i]
        try:
            hit = await prov["fn"](client, artist, title, album, duration)
            if hit is _MISS:
                continue
            if hit:
                return hit, False
            clean = False
        except Exception:
            clean = False
            log.debug("lyrics provider %s failed", prov["name"], exc_info=True)
    return None, clean


@router.get("/{track_id}/lyrics")
async def get_lyrics(track_id: str):
    """Return lyrics: embedded tags first, then the online providers
    (LRCLib + lyrics.ovh) tried fastest-first (see ``probe_lyrics_providers``)."""
    # 0. Serve a previously-resolved result instantly (and independently of
    # LRCLib's current health) — see ``_lyrics_cache``.
    cached = _lyrics_cache.get(track_id)
    if cached is not None:
        return cached
    if _lyrics_miss.get(track_id, 0.0) > time.monotonic():
        return {"lyrics": None, "synced": False, "source": None}

    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")

    # 1. Try embedded lyrics (sync-safe via executor)
    loop = asyncio.get_event_loop()
    path_str = track.path
    # For remote tracks, try the locally cached copy
    if is_remote_path(path_str):
        from soniqboom.core.filesource import parse_remote_path
        from soniqboom.core.remote_cache import get_cache
        scan_root, remote_path = parse_remote_path(path_str)
        if remote_path:
            cached = get_cache().get_cached(scan_root, remote_path)
            path = cached if cached and cached.exists() else None
        else:
            path = None
    else:
        path = Path(path_str)
        if not path.exists():
            path = None
    embedded = None
    if path:
        embedded = await loop.run_in_executor(None, extract_lyrics, path)
    if embedded:
        # Detect LRC synced format: lines starting with [mm:ss.xx]
        import re
        is_synced = bool(re.search(r'^\[\d{1,2}:\d{2}[.\:]\d{2,3}\]', embedded, re.MULTILINE))
        return _remember_lyrics(track_id, {"lyrics": embedded, "synced": is_synced, "source": "Embedded tags"})

    # Chip / tracker / Amiga formats: an online "artist + title" lookup for a
    # tune only ever misses (~1 s per call, spent sending tune titles to third
    # parties) — same rule as the Subsonic path.  MIDI keeps the online chain
    # (karaoke files), and every format keeps the embedded-tag read above.
    from soniqboom.core.retro import chip_family, is_retro_format
    fmt = getattr(track, "format", None)
    if is_retro_format(fmt) and chip_family(fmt) != "midi":
        if path is not None:
            # The file's own tags were readable and hold none: remember the
            # miss (a remote file not in the local cache yet might still
            # carry tags, so that case is not remembered).
            _remember_lyrics_miss(track_id)
        return {"lyrics": None, "synced": False, "source": None}

    # 2. Online fallback chain — LRCLib + lyrics.ovh, tried fastest-first.  Each
    # provider applies the same confidence gate, so the ranking only affects
    # latency (and, when the faster source is lyrics.ovh, plain-vs-synced).
    artist = track.artist or track.album_artist or ""
    title  = track.title or ""
    album  = track.album or ""
    if not (artist and title):
        return {"lyrics": None, "source": None}

    online, clean = await _resolve_online_lyrics(artist, title, album, track.duration)
    if online:
        await _maybe_writeback_lyrics(track, online["lyrics"])
        return _remember_lyrics(track_id, online)
    if clean and path is not None:
        # Only a full answer is remembered: every provider said "not found"
        # AND the file's own tags were readable (a remote track whose file
        # isn't in the local cache yet may still carry embedded lyrics).
        _remember_lyrics_miss(track_id)

    return {"lyrics": None, "synced": False, "source": None}


def _sid_target_duration(track, subsong: int = 0) -> int:
    """Per-tune SID render length — the stream path's own
    (``stream._sid_target_seconds``): the HVSC length of the tune the wire
    plays, then the stored duration, then the global default; clamp 5..3600.

    The waveform MUST use this same value (and the same cache key) as the
    audio render — otherwise it renders the SID at ``sid_default_duration``
    (e.g. 300 s) while the tune is ~54 s, producing 54 s of audio + 246 s
    of trailing silence.  The 200 waveform bars then spread across 300 s,
    so the real signal lands in only the leftmost ~18 % of the seek bar.
    """
    from soniqboom.api import stream as _stream
    return _stream._sid_target_seconds(track, subsong)


_WAVEFORM_PENDING = object()      # "the audio isn't rendered yet — ask again"
def _source_unreachable(path_str: str) -> bool:
    """True when a track's source can't be reached right now: its network
    share isn't connected, or its local scan root is marked ``unavailable``
    (an ejected drive / dropped mount).  Registry and stored state only —
    never touches the filesystem, so a stalled mount can't block the loop."""
    try:
        outer = (path_str or "").split("::", 1)[0]
        if is_remote_path(outer):
            from soniqboom.core.filesource import get_source, parse_remote_path
            scan_root, _rp = parse_remote_path(path_str)
            return bool(scan_root) and get_source(scan_root) is None
        from soniqboom.core.store import get_store
        best = None
        for sd in get_store().list_scan_dirs():
            p = sd.get("path") or ""
            if p and (outer == p or outer.startswith(p.rstrip("/") + "/")):
                if best is None or len(p) > len(best.get("path") or ""):
                    best = sd
        return best is not None and best.get("status") == "unavailable"
    except Exception:
        return False


async def _waveform_from_conversion_cache(track_id: str, path_str: str, ext: str,
                                          *, sid_duration: int | None = None,
                                          dreamcast: bool = False,
                                          subsong: int = 0,
                                          uade_base: int = 0,
                                          uade_named: bool = False,
                                          appear_wait: float = 5.0,
                                          finish_wait: float = 120.0):
    """WAV path of a rendered format's cached render, for the waveform.

    NEVER starts a render.  The waveform request is fired just before the
    audio request and is aborted on every track change; when it owned the
    shared render, an abort or a differently-prepared source (an archive
    member without its Amiga name / companion files) failed the render the
    audio request was waiting on — the "first play fails, retry works" bug.
    Now it only ATTACHES: cached → path; a render in flight → wait for it;
    nothing yet → wait ``appear_wait`` s for playback to start one, else
    return ``_WAVEFORM_PENDING`` (the client asks again later).  A track whose
    source is offline can't render at all, so that case returns None at once
    (no pending, no 5 s hold).  Progressive SID streams count as renders in
    flight: the waveform is ready the moment the finished render is cached.

    ``sid_duration`` (SID only) MUST match the stream path's per-tune length
    so the waveform reads the already-rendered stream WAV.  ``subsong`` picks
    that tune's render (the stream path keys every tune separately);
    ``uade_base`` is an Amiga module's subsong base, part of its key;
    ``uade_named`` is ``stream._render_ident``'s verdict (a verified Amiga
    module with a tracker extension renders through uade, not openmpt).
    """
    from soniqboom.api.stream import (
        _SID_EXTS, _MIDI_EXTS, _TRACKER_EXTS, _UADE_EXTS, _HVL_EXTS,
        _ADLIB_EXTS, _GME_EXTS_STREAM,
        _SNDH_EXTS, _YM_EXTS, _SC68_EXTS, _PSF_STREAM_EXTS, _UADE_LIVE,
        _SID_PROG_DONE,
    )
    from soniqboom.core.conversion_cache import (
        _cache_key, get_cached, inflight_event,
    )

    if ext in _SID_EXTS:
        cands = [("sid", None, sid_duration), ("uade", None, None)]
    elif ext in _MIDI_EXTS:
        from soniqboom.config import get_active_soundfont
        sf = get_active_soundfont()
        cands = [("midi", str(sf) if sf else "", None)]
    elif ext in _HVL_EXTS:
        cands = [("hvl", None, None)]
    elif ext in _UADE_EXTS or uade_named:
        cands = [("uade", None, None)]
    elif ext == ".imf":
        cands = [("imf", None, None)]
    elif ext in _ADLIB_EXTS:
        cands = [("adlib", None, None)]
    elif ext in _GME_EXTS_STREAM:
        cands = [("gme", None, None)]
    elif ext in _PSF_STREAM_EXTS or dreamcast:
        cands = [("psf", None, None)]
    elif ext in _SNDH_EXTS:
        cands = [("sndh", None, None)]
    elif ext in _YM_EXTS:
        cands = [("ym", None, None)]
    elif ext in _SC68_EXTS:
        cands = [("sc68", None, None)]
    elif ext not in _TRACKER_EXTS:
        # Exotic-Amiga prefix/suffix names (mdat.song, song.fc13).
        cands = [("uade", None, None)]
    else:
        cands = [("tracker", None, None)]
    # MIDI / PSF / YM render one tune only — always keyed as subsong 0.
    from soniqboom.api.stream import uade_cache_key
    keys = [uade_cache_key(track_id, subsong, uade_base) if fmt == "uade" else
            _cache_key(track_id, fmt, subsong if fmt not in ("midi", "psf", "ym") else 0,
                       sf, duration=dur) for fmt, sf, dur in cands]

    async def _hit():
        for k in keys:
            p = await get_cached(k)
            if p is not None:
                return p
        return None

    def _running():
        for k in keys:
            ev = inflight_event(k)
            if ev is not None:
                return ev
            live = _UADE_LIVE.get(k)
            if live is not None and not live["complete"].is_set():
                return live["complete"]
            prog = _SID_PROG_DONE.get(k)
            if prog is not None and not prog.is_set():
                return prog
        return None

    loop = asyncio.get_event_loop()
    hit = await _hit()
    if hit is not None:
        return hit
    ev = _running()
    if ev is None and _source_unreachable(path_str):
        return None        # nothing can render it now: blank, not pending
    deadline = loop.time() + appear_wait
    while ev is None and loop.time() < deadline:
        await asyncio.sleep(0.1)
        hit = await _hit()
        if hit is not None:
            return hit
        ev = _running()
    if ev is None:
        return _WAVEFORM_PENDING
    # Follow the render to the cache — also across a hand-over: a SID
    # prewarm retired for a progressive play of the same tune ends its event
    # with nothing stored while the progressive render (a different event)
    # carries on.  Out of time with a render still running → pending (the
    # player asks again); nothing running and nothing cached → the render
    # failed: None (blank waveform).
    finish_by = loop.time() + finish_wait
    settle_by = None                 # a finished live uade render being stored
    while True:
        remaining = finish_by - loop.time()
        if remaining <= 0:
            return _WAVEFORM_PENDING
        try:
            await asyncio.wait_for(ev.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            return _WAVEFORM_PENDING
        hit = await _hit()
        if hit is not None:
            return hit
        ev2 = _running()
        if ev2 is None:
            return await _hit()      # None → the render failed: blank waveform
        if ev2 is not ev:
            ev, settle_by = ev2, None
            continue
        # The same (live uade) render finished a moment before the cache
        # took the file: give the store up to 5 s.
        if settle_by is None:
            settle_by = loop.time() + 5.0
        elif loop.time() >= settle_by:
            return _WAVEFORM_PENDING
        await asyncio.sleep(0.1)


def _normalise_waveform(result):
    """Normalise ``_compute_waveform_safe`` output to ``(stored, response)``.

    ``_compute_waveform_safe`` returns a ``{"peaks", "rms"}`` dict (numpy
    and pure-Python paths alike), or a flat ``[0.0] * points`` list for an
    empty decode or an ffmpeg spawn/timeout failure.  The store layer only
    accepts a flat list, so we keep one array for storage.

    User observation (2026-05-23) on a high-dynamic-range DSF: storing
    RMS produced a waveform display where the loud transients dominated
    visually and quieter passages rendered as 1-pixel bars indistinct
    from the seek-track background — read as "blocks with gaps".  PEAKS
    are visually more uniform (less compressed by averaging) and match
    user expectation of a waveform display.  Store peaks, falling back to
    the rms array; a bare list (the blank/failed case) passes through
    unchanged.  The API response carries the full dict when available.
    """
    if isinstance(result, dict):
        stored = result.get("peaks") or result.get("rms") or []
        return stored, result
    return result, result


def _waveform_is_blank(stored) -> bool:
    """Return True if ``stored`` is empty or all-zero.

    Used as the gate before persisting a freshly-computed waveform.
    ``_compute_waveform`` returns ``[0.0] * points`` when ffmpeg's decode
    produces no audio bytes — most commonly because the source path
    couldn't be opened (remote URL ffmpeg doesn't speak, in-flight
    ``.partial`` not yet promoted to the final cache name, malformed
    file).  Storing that blank result would lock the waveform endpoint
    into the cache fast-path forever and the user would never see the
    real waveform after the transcode finished.  Skipping the store on a
    blank result lets the next call (e.g. the one app.js fires when
    ``transcode-ready`` lands) recompute from the now-available cached
    WAV and store a real waveform.
    """
    if not stored:
        return True
    try:
        return all(float(v) == 0.0 for v in stored)
    except (TypeError, ValueError):
        return False


@router.get("/{track_id}/waveform")
async def get_track_waveform(track_id: str, response: Response,
                             subsong: int | None = Query(default=None, ge=0),
                             request: Request = None):
    """Return waveform amplitude data, computing on-demand if not cached.

    For converted formats (SID, MIDI, tracker modules) the waveform is
    computed from the conversion-cache WAV rather than the raw source file.
    ``subsong`` (rendered formats) reads that tune's render; without it, the
    render a bare play makes (a multi-tune file's default tune).  The stored
    per-track waveform belongs to that bare play, so another tune's is
    computed from its render and not stored.
    """
    import asyncio
    from pathlib import Path as _Path
    from soniqboom.core.data import get_waveform, get_track, store_waveform
    from soniqboom.api.stream import (
        _SID_EXTS, _MIDI_EXTS, _TRACKER_EXTS, _UADE_EXTS, _HVL_EXTS,
        _ADLIB_EXTS, _GME_EXTS_STREAM,
    )

    # ``no-store`` on every response so the browser never serves a stale
    # body when the frontend re-fetches after ``transcode-ready``.  The
    # initial fetch on a fresh DSF/SACD track returns the silent-padded
    # reading taken off the in-flight WAV; the transcode-ready refresh
    # is supposed to return the real one once the full conversion lands.
    # Without this header (or the frontend's matching ``cache: no-cache``
    # on its fetch) Chrome happily caches the first body under the URL
    # key and reuses it for the refresh — manifests as "the waveform
    # updates sometimes but not always", because Chrome's disk-cache
    # eviction is LRU+size-bound so what gets reused varies per session.
    response.headers["Cache-Control"] = "no-store"

    # Fast path: already cached — but treat a blank (all-zero / empty)
    # cached entry as a miss so we recompute against a now-available
    # source.  Tracks that were waveform-computed against an unreachable
    # source (remote URL ffmpeg couldn't open, in-flight WAV not yet
    # promoted) wrote zeros into the cache under the pre-fix code; this
    # makes the next call self-heal instead of forever-serving the
    # poisoned zeros, no manual ``/api/admin/cache/waveforms`` clear
    # required.
    from soniqboom.api.stream import explicit_wire, tune_index
    # (A direct call passes the Query default — a bare play.)
    wire = explicit_wire(subsong if isinstance(subsong, int) else None, request)
    waveform = await get_waveform(track_id) if wire is None else None
    if waveform is not None and not _waveform_is_blank(waveform):
        return {"waveform": waveform}

    track = await get_track(track_id)
    if track is None:
        raise HTTPException(404, "Track not found")
    # The stored waveform is the bare play's: it serves (and is written by)
    # a request for the same tune too.
    bare = wire is None or tune_index(track_id, track, None) == wire
    if wire is not None and bare:
        waveform = await get_waveform(track_id)
        if waveform is not None and not _waveform_is_blank(waveform):
            return {"waveform": waveform}
    subsong = wire or 0                   # SID: the wire (wire 0 = its default)

    path_str = track.path
    # Route the scrubber waveform render exactly like playback: an AdLib
    # extension wins over a uade name-token collision, and an archive routing
    # suffix (``STAR.AMD.star``) is stripped so AMUSIC ``.amd`` files get their
    # AdLib waveform instead of a failed uade render — including library rows
    # scanned before ``.amd`` was recognized (mirrors ``stream._render_ident``).
    from soniqboom.api.stream import _render_ident
    ext, _uade_named = _render_ident(path_str, track)

    loop = asyncio.get_event_loop()

    # ── Converted formats: compute waveform from conversion-cache WAV ────
    from soniqboom.api.stream import (
        _SNDH_EXTS, _YM_EXTS, _SC68_EXTS, _PSF_STREAM_EXTS,
    )
    # ``.dsf`` is ambiguous (Sony DSD vs Dreamcast rip) — the scanner already
    # content-sniffed it, so trust the STORE's format field here (no file
    # access needed; works for remote paths too).
    _dreamcast = ext == ".dsf" and (
        getattr(track, "format", "") or "").startswith("DSF")
    # ``_uade_named``: an Amiga name (mdat.song) — but never a uade name
    # token over an extension another engine owns (``P10.mp3`` is an MP3 and
    # takes the native path below), exactly as playback routes it.
    if (ext in _SID_EXTS or ext in _MIDI_EXTS or ext in _TRACKER_EXTS
            or ext in _UADE_EXTS or ext in _HVL_EXTS
            or ext in _ADLIB_EXTS or ext in _GME_EXTS_STREAM
            or ext in _SNDH_EXTS or ext in _YM_EXTS or ext in _SC68_EXTS
            or ext in _PSF_STREAM_EXTS or _dreamcast
            or _uade_named):
        # SID: pass the per-tune duration so the waveform reuses the stream's
        # render (see _sid_target_duration).  Other converted formats render
        # full-length by nature and need no duration hint.
        _sid_dur = _sid_target_duration(track, subsong) if ext in _SID_EXTS else None
        from soniqboom.api.stream import _uade_base_known
        wav_path = await _waveform_from_conversion_cache(
            track_id, path_str, ext, sid_duration=_sid_dur, dreamcast=_dreamcast,
            subsong=tune_index(track_id, track, wire),
            uade_base=_uade_base_known(track_id, track) or 0,
            uade_named=_uade_named)
        if wav_path is _WAVEFORM_PENDING:
            # The audio isn't rendered yet (and nothing is rendering it): say
            # so instead of rendering here — the player asks again once the
            # track is playing.
            return {"waveform": None, "pending": True}
        if wav_path is None:
            # The render failed (unrenderable file): a blank waveform, never
            # an error toast.
            return {"waveform": None}
        result = await _compute_waveform_safe(str(wav_path))
        stored, response = _normalise_waveform(result)
        if bare and not _waveform_is_blank(stored):
            await store_waveform(track_id, stored)
        return {"waveform": response}

    # ── Transcoded formats (DSD / ALAC / AIFF / WavPack / Musepack) ──────
    # These also have a cached FLAC the stream endpoint produces.  Using
    # that instead of the raw source means ffmpeg decodes a ~10 MB FLAC
    # instead of a ~60 MB DSD or ~50 MB ALAC, and it shares one render
    # with the stream path (thundering-herd guard prevents duplicate work).
    # Perception payoff: the waveform appears within ~1 s of the audio
    # starting, instead of ~5–10 s in the old code path.
    from soniqboom.api.stream import _DSD_EXTS, _inflight_cache_key
    _TRANSCODED_WAVEFORM_EXTS = _DSD_EXTS | {
        ".m4a", ".aac", ".aiff", ".aif", ".wv", ".mpc",
    }
    if ext in _TRANSCODED_WAVEFORM_EXTS:
        # Prefer the final cached WAV when it exists — fastest path
        # (file already on disk, no ffmpeg invocation needed beyond the
        # 8 kHz mono downsample inside _compute_waveform).
        from soniqboom.core.conversion_cache import get_cached
        from soniqboom.api.stream import (
            _DSD_OUTPUT_RATE, _INFLIGHT_TRANSCODES,
        )
        import logging
        _log = logging.getLogger("soniqboom.waveform-dbg")
        target_rate = _DSD_OUTPUT_RATE if ext in _DSD_EXTS else None
        cache_key = _inflight_cache_key(track_id, target_rate)
        cached_path = await get_cached(cache_key)
        _log.debug("waveform %s ext=%s cache=%s",
                  track_id[:8], ext,
                  "HIT" if cached_path else "MISS")

        # Cache MISS recovery.  Two sub-cases:
        #
        # 1. An in-flight pump is ALREADY rendering this track — await it.
        # 2. No pump yet — wait briefly for one to appear, then await it.
        #
        # Sub-case 2 was the killer the diagnostic logs exposed: the
        # frontend's ``trackchange`` listener fires _fetchWaveform BEFORE
        # the audio element issues its first range GET, so /waveform
        # arrives at the backend a tiny moment ahead of /stream — and
        # /stream is what triggers ``_get_or_start_inflight_wav`` to
        # create the pump.  Without the appear-wait below, our
        # ``_INFLIGHT_TRANSCODES.get(track_id)`` reads ``None``, we skip
        # the await, fall through to ``_compute_waveform(ftp://...)``,
        # ffmpeg can't decode that pseudo-URL, returns zeros, user sees
        # blank.  Polling for the inflight to appear (cheap dict lookup
        # every 100 ms for up to 2 s) gives the streaming side a chance
        # to spawn the pump first; once it's there we join it.
        if cached_path is None:
            # Wait window for one of three exit conditions:
            #   (a) ``get_cached(cache_key)`` flips HIT — some other
            #       concurrent request finished the pump before we did.
            #   (b) ``_INFLIGHT_TRANSCODES[track_id]`` appears — the
            #       streaming-side audio request landed and spawned the
            #       pump; we'll join it.
            #   (c) Wait ceiling exceeded — fall through to blank.
            #
            # 8 s ceiling: prior 2 s missed the cases where the browser
            # delayed its first audio range GET (HTTP/2 prioritisation,
            # connection pool exhaustion under rapid track-skip, etc.).
            # The audio request usually arrives within 50-500 ms, but
            # observed worst case in the diagnostic was ~2.5 s — 8 s
            # gives a comfortable margin without hanging on the genuinely-
            # not-played case for too long.  Re-checks BOTH the cache
            # (covers a concurrent fetch that finished while we slept)
            # and the inflight registry every 100 ms so we exit as soon
            # as either condition is met.
            _log.debug("waveform %s waiting (cache+inflight)...",
                      track_id[:8])
            inflight = None
            loop = asyncio.get_event_loop()
            deadline = loop.time() + 8.0
            while loop.time() < deadline:
                cached_path = await get_cached(cache_key)
                if cached_path is not None:
                    _log.debug("waveform %s cache flipped to HIT during wait",
                              track_id[:8])
                    break
                inflight = _INFLIGHT_TRANSCODES.get(track_id)
                if inflight is not None:
                    break
                await asyncio.sleep(0.1)

            if cached_path is None:
                # The inflight dict is inserted into _INFLIGHT_TRANSCODES
                # BEFORE its ``pump_task`` key is populated — stream.py
                # holds _INFLIGHT_LOCK only long enough to claim the slot,
                # then drops it for the slow ffprobe + WAV header pre-write
                # (200-500 ms), THEN re-acquires the lock to add
                # ``pump_task`` + ``wav_path`` + the events.  If we look
                # up ``pump_task`` during that window we get ``None`` and
                # silently fall through to blank.  Wait for
                # ``setup_ready`` to fire (the same coordination event
                # other inflight subscribers use, ``stream.py:1245``) so
                # we read the dict only after it's fully populated.
                if inflight is not None:
                    setup_ready = inflight.get("setup_ready")
                    if setup_ready is not None and not setup_ready.is_set():
                        _log.debug("waveform %s awaiting inflight setup...",
                                  track_id[:8])
                        try:
                            await asyncio.wait_for(
                                setup_ready.wait(), timeout=10.0,
                            )
                        except asyncio.TimeoutError:
                            _log.debug(
                                "waveform %s setup_ready timed out",
                                track_id[:8],
                            )
                        # Re-read in case the inflight was replaced.
                        inflight = (
                            _INFLIGHT_TRANSCODES.get(track_id) or inflight
                        )

                pump_task = inflight.get("pump_task") if inflight else None
                if pump_task is not None and not pump_task.done():
                    try:
                        # 120 s ceiling — enough for any reasonable
                        # DSD/ALAC pass, short enough that a wedged pump
                        # fails the request rather than hanging the
                        # worker forever.
                        _log.debug("waveform %s awaiting inflight pump...",
                                  track_id[:8])
                        await asyncio.wait_for(pump_task, timeout=120.0)
                        _log.debug("waveform %s pump finished", track_id[:8])
                    except asyncio.TimeoutError:
                        _log.debug("waveform %s pump timed out after 120s",
                                     track_id[:8])
                    except Exception as exc:
                        _log.debug("waveform %s pump errored: %s: %s",
                                  track_id[:8], type(exc).__name__, exc)
                elif inflight is None:
                    _log.debug("waveform %s no inflight after 8s wait",
                              track_id[:8])
                else:
                    # inflight exists but pump_task still missing or
                    # already done — log so we can spot it.
                    _log.debug(
                        "waveform %s inflight present but no live pump "
                        "(keys=%s, done=%s)",
                        track_id[:8],
                        sorted(inflight.keys()),
                        pump_task.done() if pump_task else "N/A",
                    )

                # Final cache re-check — covers both the post-pump path
                # and the race where the pump completed between our last
                # in-loop check and the pump_task await.
                cached_path = await get_cached(cache_key)
                _log.debug("waveform %s post-wait cache=%s",
                          track_id[:8],
                          "HIT" if cached_path else "STILL MISS")
        # ``_compute_waveform`` runs its own ``ffmpeg -ac 1 -ar 8000 -f f32le``
        # which handles every source format ffmpeg can demux — DSD via the
        # built-in dsf / iff (DFF) / wsd demuxers, ALAC inside .m4a, AIFF,
        # WavPack, Musepack.  Going straight to source means the waveform
        # appears in ~3 s on a typical 5-min DSD instead of waiting the full
        # transcode (~30–50 s) — the single biggest perception polish
        # remaining after the cold-start fix.
        if cached_path:
            src_for_waveform = str(cached_path)
        elif '::' in path_str:
            # Transcoded-format member inside a LOCAL or REMOTE archive with no
            # cached transcode yet.  ffmpeg can't read the ``archive.zip::member``
            # virtual path — nor our composite ``ftp://host/scan:/…zip::member``
            # form — directly, and the plain-remote branch below would hand the
            # whole composite string to get_cache().fetch (no such remote file →
            # HTTP 502).  Partition the ``::`` tail FIRST and extract the member
            # to a local temp, exactly like the plain-audio branch does further
            # down.
            local = await _resolve_zip_member_to_local(path_str)
            if local is None:
                raise HTTPException(404, "Waveform not available for this format")
            try:
                result = await _compute_waveform_safe(str(local))
                stored, response = _normalise_waveform(result)
                if not _waveform_is_blank(stored):
                    await store_waveform(track_id, stored)
                return {"waveform": response}
            finally:
                try:
                    local.unlink()
                except Exception:
                    pass
        elif is_remote_path(path_str):
            # Remote transcoded source (e.g. a .m4a/.aac on an FTP/SMB share)
            # with no cached transcode yet: ffmpeg can't open our internal
            # ``ftp://host/scanroot:/rel`` pseudo-URL — it returns all-zeros and
            # the waveform shows blank.  Fetch a local copy first (same as the
            # remote branch below) instead of handing ffmpeg the pseudo-URL.
            from soniqboom.core.filesource import get_source, parse_remote_path
            from soniqboom.core.remote_cache import get_cache
            scan_root, remote_path = parse_remote_path(path_str)
            source = get_source(scan_root) if remote_path else None
            if source is None:
                raise HTTPException(503, "Network share unavailable")
            try:
                # In a worker thread: a download on the event loop stalled
                # every other request for its whole duration.
                src_for_waveform = str(await asyncio.get_running_loop().run_in_executor(
                    None, get_cache().fetch, scan_root, remote_path, source))
            except Exception as exc:        # noqa: BLE001 — surface fetch failure
                raise HTTPException(502, f"Could not fetch remote file: {exc}")
        else:
            src_for_waveform = path_str
        result = await _compute_waveform_safe(src_for_waveform)
        stored, response = _normalise_waveform(result)
        # Cache-poisoning guard: when ``cached_path`` is None (the
        # in-flight pump hasn't promoted .partial yet) and ``path_str``
        # is a remote URL ffmpeg can't read directly (e.g. our internal
        # ``ftp://host/scan:/relative`` form), ``_compute_waveform``
        # returns all-zeros — storing that locks the fast-path forever.
        # Pump-completion reordering (api/stream.py) now closes the race
        # on the happy path; this guard is the belt-and-braces fallback.
        blank = _waveform_is_blank(stored)
        if not blank:
            await store_waveform(track_id, stored)
        _log.debug(
            "waveform %s computed: len=%d shape=%s first5=%s blank=%s",
            track_id[:8], len(stored) if stored else 0,
            type(response).__name__,
            (stored[:5] if stored else []), blank,
        )
        return {"waveform": response}

    # ── Plain audio inside a ZIP: extract the member, then compute ───────
    # ffmpeg can't read the ``archive.zip::member`` virtual path directly, so
    # pull the member out to a local temp file first (works for local and
    # remote archives), exactly like the converted-format path already does.
    if '::' in path_str:
        local = await _resolve_zip_member_to_local(path_str)
        if local is None:
            raise HTTPException(404, "Waveform not available for this format")
        try:
            result = await _compute_waveform_safe(str(local))
            stored, response = _normalise_waveform(result)
            if not _waveform_is_blank(stored):
                await store_waveform(track_id, stored)
            return {"waveform": response}
        finally:
            try:
                local.unlink()
            except Exception:
                pass

    # ── Remote files: compute from cached local copy ─────────────────────
    if is_remote_path(path_str):
        from soniqboom.core.filesource import get_source, parse_remote_path
        from soniqboom.core.remote_cache import get_cache
        scan_root, remote_path = parse_remote_path(path_str)
        if not remote_path:
            raise HTTPException(400, "Remote path is malformed")
        source = get_source(scan_root)
        if source is None:
            raise HTTPException(503, "Network share unavailable")
        try:
            local_path = await asyncio.get_running_loop().run_in_executor(
                None, get_cache().fetch, scan_root, remote_path, source)
        except Exception as exc:
            raise HTTPException(502, f"Could not fetch remote file: {exc}")
        result = await _compute_waveform_safe(str(local_path))
        stored, response = _normalise_waveform(result)
        if not _waveform_is_blank(stored):
            await store_waveform(track_id, stored)
        return {"waveform": response}

    # ── Standard local files: compute directly from source ───────────────
    result = await _compute_waveform_safe(track.path)
    stored, response = _normalise_waveform(result)
    if not _waveform_is_blank(stored):
        await store_waveform(track_id, stored)
    return {"waveform": response}


# ── Ratings ──────────────────────────────────────────────────────────────────

@router.put("/{track_id}/rating")
async def update_rating(track_id: str, body: dict, _user=_Depends(_require_edit)):
    """Set or remove a track rating (0-5). Pass {"rating": 0} to remove."""
    rating = body.get("rating", 0)
    if not isinstance(rating, int) or rating < 0 or rating > 5:
        raise HTTPException(400, "Rating must be 0-5")
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    await set_rating(track_id, rating)
    return {"id": track_id, "rating": rating}


@router.get("/{track_id}/rating")
async def read_rating(track_id: str):
    return {"id": track_id, "rating": await get_rating(track_id)}


# ── Play stats (per-track endpoints) ─────────────────────────────────────────

@router.post("/{track_id}/played")
async def mark_played(track_id: str, sb_session: str | None = Cookie(default=None)):
    """Record a play event for the track (increments count, sets last_played).

    Also pushes the event to the listening history log (smart.py), shows the
    signed-in listener in Subsonic ``getNowPlaying`` (player "SoniqBoom
    Web"), and forwards the play to last.fm / ListenBrainz if that user has
    scrobble tokens configured.
    """
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    stats = await record_play(track_id)

    user = None
    if sb_session:
        try:
            from soniqboom.core.users import get_user_store
            user = get_user_store().lookup_session(sb_session)
        except Exception:
            user = None
    if user is not None:
        # The web / mobile player only reports a play once it crossed the
        # record threshold (never on a preload), so this is a real listen.
        try:
            from soniqboom.api.subsonic import _note_now_playing
            _note_now_playing(user, track_id, "SoniqBoom Web", from_scrobble=True)
        except Exception:
            pass

    # Push to listening history (non-blocking, fire-and-forget)
    try:
        from soniqboom.api.smart import push_history
        await push_history(track_id, title=track.title or "", artist=track.artist or "")
    except Exception:
        pass  # history is best-effort, don't fail the play recording

    # External scrobble (last.fm / ListenBrainz) for the signed-in user —
    # queued + retried on network failure inside core.scrobble.
    try:
        from soniqboom.core.scrobble import submit_play
        from soniqboom.core.store import get_store
        if user is not None:
            full_track = get_store().get_track(track_id)
            if full_track:
                await submit_play(user, full_track)
    except Exception:
        pass

    return {"id": track_id, **stats}


@router.get("/{track_id}/stats")
async def read_play_stats(track_id: str):
    stats = await get_play_stats(track_id)
    return {"id": track_id, **stats}
