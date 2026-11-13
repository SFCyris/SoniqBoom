# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sega Dreamcast / Naomi DSF rips through ``dsfrender``
(soniqboom/native/dsfrender: Highly Theoretical + psflib).

A ``.minidsf``, or a ``.dsf`` that is a Dreamcast rip ('PSF\\x12' —
``api.stream._dsf_is_dreamcast``; a Sony DSD ``.dsf`` never comes here),
plays through dsfrender when it is installed (``renderers.dsfrender_path``,
else on PATH), else through zxtune123 as every other PSF-family rip
(``api.stream._render_psf``).  dsfrender loads each section into the 8 MB of
sound RAM a Naomi board has.

It renders as long as the zxtune123 path does: the rip's ``length`` tag plus
its ``fade`` tag, the last ``fade`` seconds fading out
(``metadata.psf_play_plan``, which the scan's duration also comes from);
without a length, zxtune123's default 180 s, its ``fade`` tag (if any)
fading out inside them.  Its renders are cached apart from zxtune123's
(``cache_variant_for``).

A rip dsfrender fails on (an error exit, no audio, silence) is rendered by
zxtune123 when that is installed (``api.stream._render_psf`` /
``_serve_psf``); the file is then routed to zxtune123 for the rest of
this server run (``note_failed``: kept in memory, per path, size and
modification time).
"""
from __future__ import annotations

import math
import os
import shutil
import threading
from collections import OrderedDict
from pathlib import Path

from soniqboom.config import settings

DSF_EXTS = frozenset({".dsf", ".minidsf"})
DEFAULT_SECONDS = 180.0       # zxtune123's default length for a rip stating none
KIND = "DSF"                  # the render kind (``core/unplayable.KIND_KEYS``)
RENDERER = "dsfrender"
CACHE_VARIANT = "dsf1"


def binary() -> "str | None":
    """The dsfrender binary: ``renderers.dsfrender_path`` when it is a file,
    else the one on PATH; None when there is none."""
    configured = getattr(settings, "dsfrender_path", "") or ""
    if configured and Path(configured).is_file():
        return configured
    return shutil.which(RENDERER)


# path → (size, mtime_ns) of a rip dsfrender failed on in this run: zxtune123
# renders it (``note_failed``).  Bounded.
_FAILED: "OrderedDict[str, tuple[int, int]]" = OrderedDict()
_FAILED_MAX = 4096
_failed_lock = threading.Lock()


def _signature(path) -> "tuple[int, int] | None":
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def note_failed(path) -> None:
    """Remember that dsfrender failed on the rip at ``path`` as it is now
    (its size and modification time): ``binary_for`` routes it to zxtune123
    from now on in this run.  Blocking (a stat)."""
    sig = _signature(path)
    if sig is None:
        return
    key = str(path)
    with _failed_lock:
        _FAILED[key] = sig
        _FAILED.move_to_end(key)
        while len(_FAILED) > _FAILED_MAX:
            _FAILED.popitem(last=False)


def failed_before(path) -> bool:
    """Did dsfrender fail on the rip at ``path``, unchanged since, in this
    run (``note_failed``)?  Blocking (a stat when it did)."""
    with _failed_lock:
        sig = _FAILED.get(str(path))
    return sig is not None and _signature(path) == sig


def forget_failures() -> None:
    """Drop every ``note_failed`` record (tests)."""
    with _failed_lock:
        _FAILED.clear()


def binary_for(path: Path) -> "str | None":
    """dsfrender, when it is installed and ``path`` is a Dreamcast rip it
    plays (a ``.minidsf`` or ``.dsf`` starting 'PSF\\x12', on which it hasn't
    failed in this run — ``note_failed``); else None.  The one routing
    decision: the cache variant (``cache_variant_for``) and the renderer a
    play names follow it.
    Blocking (reads the file's first 4 bytes)."""
    ext = Path(path).suffix.lower()
    if ext not in DSF_EXTS:
        return None
    found = binary()
    if not found:
        return None
    from soniqboom.api.stream import _dsf_is_dreamcast
    if not _dsf_is_dreamcast(Path(path)):
        return None                    # (no Dreamcast header: zxtune123 reads it)
    if failed_before(path):
        return None                    # (zxtune123 renders it instead)
    return found


def cache_variant_for(path: Path) -> "str | None":
    """The cache variant of a ``psf`` render of the file at ``path``:
    ``CACHE_VARIANT`` when dsfrender renders it (``binary_for``: another
    render than zxtune123's, which a render cached before is not reused
    for), else None.  Blocking."""
    return CACHE_VARIANT if binary_for(path) else None


def cache_variants(ext: str) -> "tuple[str | None, ...]":
    """Every cache variant a ``psf`` render of a file with suffix ``ext`` may
    be stored under — dsfrender's, then zxtune123's, for a Dreamcast suffix —
    for a lookup that reads no file.  Pure."""
    return (CACHE_VARIANT, None) if ext.lower() in DSF_EXTS else (None,)


def render_plan(tags: dict) -> "tuple[float, float]":
    """``(seconds, fade)`` a Dreamcast rip renders for, from its ``[TAG]``
    block (``tags``: lower-case keys): ``metadata.psf_play_plan``'s length +
    fade when it states a length; else ``DEFAULT_SECONDS`` with its fade tag
    (when it is a positive time, at most ``metadata._PSF_MAX_FADE_S``) faded
    out inside them (zxtune123 also fades a lone fade tag within its 180 s).
    Pure."""
    from soniqboom.core.metadata import _PSF_MAX_FADE_S, _psf_parse_length, psf_play_plan
    seconds, fade = psf_play_plan(tags)
    if seconds > 0:
        return seconds, fade
    fade = _psf_parse_length(tags["fade"]) if "fade" in tags else 0.0
    if not (math.isfinite(fade) and fade > 0):
        fade = 0.0
    return DEFAULT_SECONDS, min(fade, _PSF_MAX_FADE_S)


def file_plan(path: Path) -> "tuple[float, float]":
    """``render_plan`` of the rip at ``path`` (its own tag block).
    Blocking (header + tag block)."""
    from soniqboom.api.stream import _psf_tag_lines
    tags: dict[str, str] = {}
    for line in _psf_tag_lines(Path(path)):
        key, sep, val = line.partition("=")
        if sep:
            tags.setdefault(key.strip().lower(), val.strip())
    return render_plan(tags)


def command(binary_path: str, path: Path, out: Path, seconds: float, fade: float) -> list[str]:
    """The dsfrender command line rendering ``path`` to ``out`` for
    ``seconds`` (fade included), its last ``fade`` seconds fading out.
    ``repr`` keeps the numbers exact (dsfrender reads them back as the same
    doubles)."""
    return [binary_path, "-r", "44100", "-m", repr(float(seconds)),
            "-f", repr(float(fade) * 1000), str(path), str(out)]


async def render(binary_path: str, path: Path, *, live_key: "str | None" = None,
                 expected_seconds: float = 0.0) -> Path:
    """Render the Dreamcast rip ``path`` with dsfrender to a temp WAV (44.1 kHz
    stereo) for ``file_plan``'s length and fade.  ``live_key``: as a live
    render (``api.stream._tail_render``), played while it renders;
    ``expected_seconds`` is then the length it will have (the plan's, when
    not given).  A failed render, or one without audio, removes the temp file
    and raises the usual renderer errors (``api.stream._await_renderer`` /
    ``_tail_render``, kind ``KIND``).  The render names dsfrender as its
    renderer (``unplayable.renderer_ran``): one stored and audible proves
    dsfrender, not zxtune123."""
    import tempfile

    from soniqboom.api import stream as _s
    from soniqboom.core import unplayable as _unplayable
    seconds, fade = await _s._pb(file_plan, Path(path))
    tmp = tempfile.NamedTemporaryFile(dir=_s._rtmp(), suffix=".wav", delete=False)
    tmp.close()
    out = Path(tmp.name)
    cmd = command(binary_path, Path(path), out, seconds, fade)
    timeout = max(600.0, seconds * 2 + 60)
    _unplayable.renderer_ran(RENDERER, "psf")
    if live_key:
        return await _s._tail_render(cmd, out, live_key, kind=KIND, timeout=timeout,
                                     expected_seconds=expected_seconds or seconds)
    await _s._await_renderer(cmd, out, timeout=timeout, kind=KIND)
    return out
