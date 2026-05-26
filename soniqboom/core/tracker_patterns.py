# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tracker pattern data extractor.

Reads the full pattern grid + order list from a tracker module so the
frontend can render the OpenMPT/XMPlay-style "pattern view" alongside
playback.  Uses ``pyopenmpt`` (Python binding to libopenmpt) when the
operator has installed it; otherwise returns an empty payload so the
UI quietly falls back to the (already-implemented) channel VU display.

Channel VU itself uses the in-browser Web Audio analyser, so it works
even without libopenmpt — this module only enriches the *pattern grid*
visualisation, not the per-channel level meters.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def is_available() -> bool:
    try:
        import pyopenmpt  # noqa: F401
        return True
    except ImportError:
        return False


def extract_patterns(path: Path, *, max_rows: int = 64, max_channels: int = 32) -> dict:
    """Return a JSON-friendly dict::

        {
          "channels": int,
          "rows":     int,    # per-pattern (variable across patterns)
          "order":    [int],  # pattern order list
          "patterns": [        # one dict per pattern referenced in order
            {"index": int, "rows": [[note_str, ...], ...]},
            ...
          ],
        }

    Returns ``{"available": False}`` if libopenmpt isn't installed.
    Empty / unreadable files return an empty grid.
    """
    if not is_available():
        return {"available": False, "channels": 0, "order": [], "patterns": []}
    try:
        import pyopenmpt as _opm
        mod = _opm.Module(str(path))
        n_channels = min(mod.num_channels, max_channels)
        order = list(mod.order_list)
        # Dedupe — same pattern may appear at multiple order positions; we
        # only want the row data once per unique pattern index.
        unique = sorted(set(order))
        patterns = []
        for pi in unique:
            n_rows = min(mod.pattern_num_rows(pi), max_rows)
            rows = []
            for r in range(n_rows):
                row = []
                for c in range(n_channels):
                    cell = mod.pattern_row(pi, r, c) or ""
                    row.append(str(cell)[:8])   # trim to a compact cell
                rows.append(row)
            patterns.append({"index": pi, "rows": rows})
        return {
            "available": True,
            "channels": n_channels,
            "order":    order,
            "patterns": patterns,
        }
    except Exception:
        log.exception("pattern extract failed for %s", path)
        return {"available": False, "channels": 0, "order": [], "patterns": []}
