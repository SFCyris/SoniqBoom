# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Early Digital Sound Interface Kit modules (DSIK v1: a file starting with
``DSM\\x10``) through ``milkyrender`` (MilkyPlay — soniqboom/native/
milkyrender).

A ``.dsm`` file in the later RIFF ``DSMF`` layout is no DSIK v1 module: it
plays through libopenmpt as every tracker module does.  Without milkyrender
a DSIK v1 module is refused at play (``api.stream._DSM_EARLY_DETAIL``) and
scanned with no length.

* ``is_dsik_v1`` — the content test (the first four bytes);
* ``find`` — the binary: ``settings.milkyrender_path``, else ``milkyrender``
  on PATH;
* ``render_cmd`` — the command a play renders with: to the song's natural
  end (``-l 1``) and a ``FADE_MS`` fade where it jumps back, at most
  ``MAX_S``, 16-bit stereo at ``RATE``;
* ``length_seconds`` — what ``milkyrender --length`` prints for the same
  options: the exact length of that render (``fade_ms=0``: the song's own,
  as the library stores it);
* ``header`` — the title, channel / pattern counts and instrument names the
  module's header states.
"""
from __future__ import annotations

import re
import logging
import shutil
import struct
from pathlib import Path

log = logging.getLogger(__name__)

MAGIC = b"DSM\x10"
# The mix rate (MilkyPlay's timer steps rate / 250 frames: 192 at 48 kHz).
RATE = 48000
# The fade a song that jumps back at its end ends with (as libopenmpt ends
# one: ``api.stream._OPENMPT_END_FADE_S``).
FADE_MS = 100
MAX_S = 3600
LENGTH_TIMEOUT_S = 10
_DSI_MAGIC = b"DSI\x10"


def is_dsik_v1(head: bytes) -> bool:
    """Does a file starting with ``head`` hold a DSIK v1 module?"""
    return bytes(head[:4]) == MAGIC


def find() -> "str | None":
    """The milkyrender binary: the configured path when it is a file, else
    the one on PATH, else None."""
    try:
        from soniqboom.config import settings
        configured = getattr(settings, "milkyrender_path", "") or ""
    except Exception:                                   # noqa: BLE001
        configured = ""
    if configured and Path(configured).is_file():
        return configured
    return shutil.which("milkyrender")


def _options(fade_ms: int) -> list:
    return ["-r", str(RATE), "-m", str(MAX_S), "-f", str(int(fade_ms)), "-l", "1"]


def render_cmd(binary: str, src, out) -> list:
    """The command that renders module ``src`` to WAV ``out``."""
    return [binary, *_options(FADE_MS), str(src), str(out)]


_TRUNCATED_RE = re.compile(r"warning: truncated sample data \((\d+) bytes? missing\)")


def length_seconds(binary: str, src, *, fade_ms: int = FADE_MS) -> "float | None":
    """Seconds of audio ``render_cmd`` writes for ``src`` (with ``fade_ms``
    in place of ``FADE_MS``), from ``milkyrender --length`` — no audio is
    rendered.  None when milkyrender refuses the file, fails or takes longer
    than ``LENGTH_TIMEOUT_S``."""
    return length_info(binary, src, fade_ms=fade_ms)[0]


def length_info(binary: str, src, *, fade_ms: int = FADE_MS) -> "tuple[float | None, int]":
    """``(seconds, missing)``: ``length_seconds``' answer and the number of
    sample-data bytes the module is cut short by (milkyrender's "warning:
    truncated sample data (N bytes missing)" line; 0 without one — such a
    module plays, the missing part silent)."""
    from soniqboom.core import forksafe
    cmd = [binary, *_options(fade_ms), "--length", str(src)]
    try:
        r = forksafe.run(cmd, capture_output=True, text=True, timeout=LENGTH_TIMEOUT_S)
    except Exception as exc:                            # noqa: BLE001 — timeout, missing binary
        log.debug("milkyrender --length failed for %s: %s", src, exc)
        return None, 0
    if r.returncode != 0:
        log.debug("milkyrender --length refused %s: %s", src, (r.stderr or "").strip()[:200])
        return None, 0
    m = _TRUNCATED_RE.search(r.stderr or "")
    missing = int(m.group(1)) if m else 0
    lines = (r.stdout or "").strip().splitlines()
    try:
        secs = float(lines[-1]) if lines else 0.0
    except ValueError:
        return None, missing
    return (secs if secs > 0 else None), missing


def header(raw: bytes) -> "dict | None":
    """What a DSIK v1 module's header states — ``{"name": bytes,
    "channels": int, "patterns": int, "instruments": [bytes, …]}`` (the
    title field and the instrument names as stored, NUL-padded) — or None
    when ``raw`` is not one or its fixed header is cut short.  Instrument
    blocks the file doesn't hold whole are left out.  Pure."""
    if not is_dsik_v1(raw) or len(raw) < 176:
        return None
    n_ins, n_pat = raw[39], raw[40]
    names = []
    if len(raw) >= 176 + 2 * n_ins:
        for i in range(n_ins):
            off = struct.unpack_from("<H", raw, 176 + 2 * i)[0] * 16
            if off and off + 34 <= len(raw) and raw[off:off + 4] == _DSI_MAGIC:
                names.append(bytes(raw[off + 4:off + 34]))
    return {"name": bytes(raw[4:32]), "channels": raw[37], "patterns": n_pat,
            "instruments": names}
