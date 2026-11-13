# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Musicline Editor modules ("MLEDMODL"): which engine renders a tune.

uade123's MusiclineEditor player renders a Musicline Editor tune; a tune it
doesn't play (no second of sound: only silence, or nothing) is rendered by
``mlrender`` (soniqboom/native/mlrender — the musicline_playback replayer)
instead, when it is installed.  What uade does with a tune is learnt once per
file content, tune and subsong base (``remember`` / ``uade_verdict``: a probe
render in ``api.stream._musicline_route``) and kept in memory for this server
run.  A tune whose probe can't decide (it timed out) is probed again on its
next play; meanwhile its uade render, when it fails or comes out silent, is
rendered by mlrender instead (``api.stream._musicline_after_uade``).  A
file's content hash is taken once per path, size and modification time
(``digest``).
``render_command`` is the mlrender command line of such a render.
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict

MAGIC = b"MLEDMODL"
# mlrender reads modules up to 64 MB; larger files are never Musicline here.
_MAX_BYTES = 64 * 1024 * 1024

# (sha1 of the file, tune index, subsong base) → does uade render it audibly?
_VERDICTS: "OrderedDict[tuple[str, int, int], bool]" = OrderedDict()
_VERDICTS_MAX = 2048
_lock = threading.Lock()


# path → (size, mtime_ns, sha1 or None) of the last ``digest`` of it.
_DIGESTS: "OrderedDict[str, tuple[int, int, str | None]]" = OrderedDict()
_DIGESTS_MAX = 4096


def digest(path) -> "str | None":
    """The sha1 of the file at ``path`` when it is a Musicline Editor module
    (its first 8 bytes are ``MLEDMODL``), else None — also when it can't be
    read.  Read once per path, size and modification time (a stat answers
    the next call).  Blocking."""
    key = str(path)
    try:
        st = os.stat(key)
    except OSError:
        return None
    sig = (st.st_size, st.st_mtime_ns)
    with _lock:
        hit = _DIGESTS.get(key)
    if hit is not None and hit[:2] == sig:
        return hit[2]
    try:
        sha = _digest_read(key)
    except OSError:
        return None                     # (not remembered: read again next time)
    with _lock:
        _DIGESTS[key] = (*sig, sha)
        _DIGESTS.move_to_end(key)
        while len(_DIGESTS) > _DIGESTS_MAX:
            _DIGESTS.popitem(last=False)
    return sha


def known_digest(path) -> "str | None":
    """The sha1 the last ``digest`` of ``path`` found (None: not a Musicline
    module, or not read in this run).  Reads nothing."""
    with _lock:
        hit = _DIGESTS.get(str(path))
    return hit[2] if hit is not None else None


# (sha1, tune index, subsong base) of the tunes mlrender rendered in this run
# when uade's render of them failed (their probe undecided): ``mlrender_plays``.
_ML_MADE: "OrderedDict[tuple[str, int, int], bool]" = OrderedDict()


def note_mlrender_made(sha1: str, index: int, base: int = 0) -> None:
    """Remember that mlrender made the render of tune ``index`` (subsong
    ``base``) of the module with content ``sha1`` — uade's render of it
    failed — without a verdict on uade (its probe is asked again)."""
    key = (sha1, int(index), int(base))
    with _lock:
        _ML_MADE[key] = True
        _ML_MADE.move_to_end(key)
        while len(_ML_MADE) > _VERDICTS_MAX:
            _ML_MADE.popitem(last=False)


def mlrender_plays(path, index: int, base: int = 0) -> bool:
    """Has this run learnt that mlrender renders tune ``index`` of the
    module at ``path`` — uade doesn't play it (``uade_verdict`` False on
    ``known_digest``), or, with no verdict, mlrender made its render when
    uade's failed (``note_mlrender_made``)?  Reads nothing."""
    sha = known_digest(path)
    if sha is None:
        return False
    verdict = uade_verdict(sha, index, base)
    if verdict is not None:
        return not verdict
    with _lock:
        return (sha, int(index), int(base)) in _ML_MADE


def _digest_read(path: str) -> "str | None":
    with open(path, "rb") as fh:
        if fh.read(len(MAGIC)) != MAGIC:
            return None
        h = hashlib.sha1(MAGIC)
        total = len(MAGIC)
        while True:
            block = fh.read(1 << 20)
            if not block:
                break
            total += len(block)
            if total > _MAX_BYTES:
                return None
            h.update(block)
        return h.hexdigest()


def uade_verdict(sha1: str, index: int, base: int = 0) -> "bool | None":
    """Whether uade renders tune ``index`` of the module with content
    ``sha1`` audibly, when learnt in this server run (None: not yet)."""
    with _lock:
        return _VERDICTS.get((sha1, int(index), int(base)))


def remember(sha1: str, index: int, base: int, plays: "bool | None") -> None:
    """Keep what a uade probe of the tune found (None — undecided — is not
    kept)."""
    if plays is None:
        return
    key = (sha1, int(index), int(base))
    with _lock:
        _VERDICTS[key] = bool(plays)
        _VERDICTS.move_to_end(key)
        while len(_VERDICTS) > _VERDICTS_MAX:
            _VERDICTS.popitem(last=False)


def forget_all() -> None:
    """Drop every verdict, remembered digest and mlrender render (tests)."""
    with _lock:
        _VERDICTS.clear()
        _DIGESTS.clear()
        _ML_MADE.clear()


# What a SoniqBoom render of a Musicline tune through mlrender uses: 44.1 kHz,
# at most 600 s, an 8 s fade once the song loops, and the stereo image of
# uade's panning 0.7 (uade.conf) — left and right each 0.65 / 0.35.
RATE = 44100
MAX_S = 600
FADE_MS = 8000
SEPARATION = 0.3


def render_command(binary: str, path, out, subsong: int = 0, *, max_s: int = MAX_S,
                   fade_ms: int = FADE_MS, separation: float = SEPARATION,
                   rate: int = RATE) -> list[str]:
    """``mlrender`` argv rendering tune ``subsong`` (0-based) of ``path`` to
    the WAV ``out`` ("-": standard output)."""
    return [str(binary), "-s", str(max(0, min(int(subsong), 255))), "-r", str(int(rate)),
            "-m", str(max(1, min(int(max_s), 3600))), "-f", str(max(0, int(fade_ms))),
            "-p", f"{float(separation):g}", str(path), str(out)]
