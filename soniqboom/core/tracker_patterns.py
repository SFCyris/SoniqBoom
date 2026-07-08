# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tracker pattern-grid / song-message / tempo extractor.

Reads the pattern grid, order list, song message, initial tempo and a
row→time map from a tracker module via the same in-process libopenmpt
``ctypes`` binding that :mod:`soniqboom.core.openmpt_vu` uses (this
module binds the handful of extra C functions it needs on top of that
loader).  Powers the Track-Info "Patterns" and "Song message" sections.

Historical note: this module previously imported a third-party
``pyopenmpt`` binding that was never installed on any deployment, so
``extract_patterns`` always returned ``{"available": False}``.  The
ctypes rewrite needs nothing beyond the libopenmpt shared library that
the installer already provides for VU extraction.

Payload contract (JSON-friendly)::

    {
      "available":  True,
      "channels":   int,        # capped at max_channels
      "channels_total": int,    # real module voice count
      "order":      [int],      # pattern index per order position (-1 = marker)
      "patterns":   [{"index": int, "rows": [[cell, ...], ...]}],
      "truncated":  [int],      # pattern indices dropped by the cell budget
      "order_times": [float],   # start-of-order seconds, len == len(order)
      "row_times":  [[float]] | None,   # per-row seconds per order (None when
                                        # the module exceeds _ROW_TIME_BUDGET)
      "duration":   float,      # module duration in seconds
      "message":    str,        # song message, "" when absent (UTF-8, decoded
                                # from the native charset by libopenmpt itself)
      "tempo":      float,      # initial tempo (≈BPM for most formats)
      "tracker":    str,        # tracking software, when the file records it
    }

Cells come from ``openmpt_module_format_pattern_row_channel`` at a fixed
width of 13 with padding: ``note(3) sp inst(2) vol(3) sp fx(3)`` — e.g.
``"D#5 0Cv20 ..."`` (the volume column is THREE chars at [6:9]: command
letter + 2 digits, or " .." when empty) — so the frontend slices note[0:3],
inst[4:6], vol[6:9], fx[10:13] at fixed offsets.
"""
from __future__ import annotations

import ctypes
import logging
from pathlib import Path

from soniqboom.core import openmpt_vu as _ovu

log = logging.getLogger(__name__)

# One formatted cell: "nnn iivvv exx" — note(3) sp inst(2) vol(3) sp fx(3)
# (vol is THREE chars at [6:9]; matches the module docstring + frontend slices).
_CELL_WIDTH = 13

# Grid caps.  A pathological 64-channel / 240-pattern IT would serialize to
# tens of MB; the cell budget keeps the payload bounded while covering the
# overwhelmingly common 4–32 channel modules completely.  Patterns are kept
# in order-of-first-appearance until the budget runs out; dropped pattern
# indices are reported in ``truncated`` so the UI can say so.
_MAX_CHANNELS   = 32
_MAX_ROWS       = 200          # IT allows up to 200 rows per pattern
_CELL_BUDGET    = 400_000      # ≈5.6 MB raw / ~10× smaller gzipped
# Full per-row time maps are skipped for monsters (orders × rows); the
# frontend then interpolates between order_times instead.
_ROW_TIME_BUDGET = 20_000
# Refuse absurd inputs outright.
_MAX_FILE_BYTES = 64 * 1024 * 1024


def is_available() -> bool:
    """True iff libopenmpt is loadable on this host."""
    return _ovu.is_available()


def _bind_patterns() -> ctypes.CDLL | None:
    """Bind the pattern/metadata C functions on top of openmpt_vu's loader.

    Idempotent (guarded by ``_sb_pat_bound``); returns None when libopenmpt
    isn't available so callers can degrade to ``available: False``.
    """
    lib = _ovu._load()
    if lib is None or not _ovu._bind():
        return None
    if getattr(lib, "_sb_pat_bound", False):
        return lib

    M   = ctypes.c_void_p
    i32 = ctypes.c_int32

    # The shared loader (openmpt_vu._load) only sanity-checks
    # openmpt_module_create_from_memory, so it accepts libopenmpt versions
    # older than ours.  A couple of the symbols below arrived in 0.7
    # (get_current_tempo2), so a missing symbol on an old library must
    # degrade to "no pattern grid" — NOT raise AttributeError up through
    # the request (which returned 500).  Bind under a guard.
    try:
        for fn in ("openmpt_module_get_num_orders",
                   "openmpt_module_get_num_patterns"):
            f = getattr(lib, fn)
            f.argtypes = [M]
            f.restype  = i32

        lib.openmpt_module_get_order_pattern.argtypes = [M, i32]
        lib.openmpt_module_get_order_pattern.restype  = i32

        lib.openmpt_module_get_pattern_num_rows.argtypes = [M, i32]
        lib.openmpt_module_get_pattern_num_rows.restype  = i32

        # Returns a malloc'd char* that MUST be released via
        # openmpt_free_string — restype stays c_void_p so ctypes doesn't
        # copy-and-lose the pointer.
        lib.openmpt_module_format_pattern_row_channel.argtypes = [
            M, i32, i32, i32, ctypes.c_size_t, ctypes.c_int,
        ]
        lib.openmpt_module_format_pattern_row_channel.restype = ctypes.c_void_p

        lib.openmpt_module_get_metadata.argtypes = [M, ctypes.c_char_p]
        lib.openmpt_module_get_metadata.restype  = ctypes.c_void_p

        lib.openmpt_free_string.argtypes = [ctypes.c_void_p]
        lib.openmpt_free_string.restype  = None

        lib.openmpt_module_get_current_tempo2.argtypes = [M]
        lib.openmpt_module_get_current_tempo2.restype  = ctypes.c_double

        lib.openmpt_module_set_position_order_row.argtypes = [M, i32, i32]
        lib.openmpt_module_set_position_order_row.restype  = ctypes.c_double
    except AttributeError:
        log.info("libopenmpt too old for the pattern grid — degrading to "
                 "no-grid; VU meters still work")
        return None

    lib._sb_pat_bound = True
    return lib


def _take_string(lib, ptr) -> str:
    """Copy a libopenmpt-owned C string and free it."""
    if not ptr:
        return ""
    try:
        raw = ctypes.cast(ptr, ctypes.c_char_p).value or b""
    finally:
        lib.openmpt_free_string(ptr)
    return raw.decode("utf-8", "replace")


def _unavailable() -> dict:
    return {"available": False, "channels": 0, "order": [], "patterns": []}


def extract_patterns(
    source: bytes | Path,
    *,
    max_rows: int = _MAX_ROWS,
    max_channels: int = _MAX_CHANNELS,
) -> dict:
    """Extract the pattern grid + module metadata from *source*.

    *source* is the module's raw bytes (preferred — archive members never
    touch disk) or a filesystem Path.  Returns the payload documented in
    the module docstring, or ``{"available": False, ...}`` when libopenmpt
    is missing, the file doesn't parse, or the input is oversized.
    """
    lib = _bind_patterns()
    if lib is None:
        return _unavailable()

    if isinstance(source, Path):
        try:
            if source.stat().st_size > _MAX_FILE_BYTES:
                return _unavailable()
            data = source.read_bytes()
        except OSError:
            return _unavailable()
    else:
        data = source
    if not data or len(data) > _MAX_FILE_BYTES:
        return _unavailable()

    mod = lib.openmpt_module_create_from_memory2(
        data, len(data), None, None, None, None, None, None, None)
    if not mod:
        return _unavailable()

    try:
        # The player streams subsong 0, so the grid describes subsong 0 too.
        try:
            lib.openmpt_module_select_subsong(mod, 0)
        except Exception:
            pass

        n_channels_total = int(lib.openmpt_module_get_num_channels(mod))
        n_channels = min(n_channels_total, max_channels)
        n_orders   = int(lib.openmpt_module_get_num_orders(mod))
        n_patterns = int(lib.openmpt_module_get_num_patterns(mod))
        if n_channels <= 0 or n_orders <= 0:
            return _unavailable()

        order: list[int] = []
        for o in range(n_orders):
            pi = int(lib.openmpt_module_get_order_pattern(mod, o))
            # "+++" (skip) / "---" (stop) markers land outside the real
            # pattern range — normalise to -1 so the UI can render a
            # neutral chip instead of fetching a phantom pattern.
            order.append(pi if 0 <= pi < n_patterns else -1)

        # Unique patterns in order of first appearance — the reading order
        # a user follows — so the cell budget drops the least-visited tail.
        seen: list[int] = []
        for pi in order:
            if pi >= 0 and pi not in seen:
                seen.append(pi)

        fmt_cell = lib.openmpt_module_format_pattern_row_channel
        patterns: list[dict] = []
        truncated: list[int] = []
        cells_used = 0
        for pi in seen:
            n_rows = min(int(lib.openmpt_module_get_pattern_num_rows(mod, pi)),
                         max_rows)
            if n_rows <= 0:
                patterns.append({"index": pi, "rows": []})
                continue
            if cells_used + n_rows * n_channels > _CELL_BUDGET:
                truncated.append(pi)
                continue
            cells_used += n_rows * n_channels
            rows: list[list[str]] = []
            for r in range(n_rows):
                row: list[str] = []
                for c in range(n_channels):
                    ptr = fmt_cell(mod, pi, r, c, _CELL_WIDTH, 1)
                    row.append(_take_string(lib, ptr))
                rows.append(row)
            patterns.append({"index": pi, "rows": rows})

        # ── Timing ──
        # set_position_order_row returns the approximate song position in
        # seconds for that (order, row) — libopenmpt walks tempo/speed
        # commands itself, so mid-pattern speed changes are accounted for.
        set_pos = lib.openmpt_module_set_position_order_row
        # Clamp to the SAME max_rows the grid uses — an XM pattern can hold
        # 256 rows but the grid renders at most max_rows (200), so an
        # unclamped time map would point the frontend playhead at rows that
        # have no rendered <tr> (playhead silently stalls past row 199).
        rows_per_order = [
            (min(int(lib.openmpt_module_get_pattern_num_rows(mod, pi)), max_rows)
             if pi >= 0 else 0)
            for pi in order
        ]
        total_rows = sum(rows_per_order)
        order_times = [float(set_pos(mod, o, 0)) for o in range(n_orders)]
        row_times: list[list[float]] | None = None
        if 0 < total_rows <= _ROW_TIME_BUDGET:
            row_times = []
            for o, nr in enumerate(rows_per_order):
                row_times.append(
                    [round(float(set_pos(mod, o, r)), 3) for r in range(nr)])
        duration = float(lib.openmpt_module_get_duration_seconds(mod))

        # ── Module metadata ──
        def _meta(key: str) -> str:
            return _take_string(
                lib, lib.openmpt_module_get_metadata(mod, key.encode()))

        # message_raw = the REAL song message only.  The plain "message" key
        # falls back to a concatenation of instrument/sample names when the
        # module has none — which would duplicate the Instruments list the
        # modal already shows (MOD greets live in sample names, and those
        # are already on screen).
        message = _meta("message_raw").rstrip()
        tracker = _meta("tracker").strip()
        tempo   = float(lib.openmpt_module_get_current_tempo2(mod))

        return {
            "available": True,
            "channels": n_channels,
            "channels_total": n_channels_total,
            "order": order,
            "patterns": patterns,
            "truncated": truncated,
            "order_times": [round(t, 3) for t in order_times],
            "row_times": row_times,
            "duration": round(duration, 3),
            "message": message,
            "tempo": round(tempo, 2),
            "tracker": tracker,
        }
    except Exception:
        log.exception("pattern extract failed")
        return _unavailable()
    finally:
        try:
            lib.openmpt_module_destroy(mod)
        except Exception:
            pass
