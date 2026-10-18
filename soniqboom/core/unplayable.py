# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tracks a renderer can't play — found when they are played.

A renderer that refuses the SAME file the SAME way twice in a row marks the
track: ``defect`` "unplayable" (the badge "can't play", health.js), with the
reason in ``defect_detail`` ("The Amiga player (uade) stops on this file:
score crashed"), the renderer and version in ``defect_renderer`` and when it
was found in ``defect_at``.  ``defect_evidence`` (never sent to clients:
not a ``TrackMeta`` field) keeps what the mark was made on — the
renderer's key and fingerprint, the reason, the file's size and checksum, the
defect the track had before.  A marked track is passed by when a list plays
through (``skip_rules``); a click renders it again (the dealt → passed by,
picked → tried contract of every skip rule), and a render of it that plays
clears the mark.

**Which outcomes mark** (``classify``) — only a refusal by the renderer
itself, before it produced any audio:

* *diagnosed* — the renderer says what is wrong with the file: uade "score
  crashed" / an illegal instruction / "module check failed" / a cruncher it
  can't unpack; sidplayfp's SIDTUNE loader errors (unsupported or invalid
  data); psgplay's invalid SNDH header; openmpt123's "error loading file"
  (when it prints it — the play renders ``--quiet``).  A uade render that
  crashed ("score crashed", illegal instruction) with less than a second of
  output that held nothing audible counts too.
* *silent* — the renderer exits with an error, crashes (SIGSEGV / SIGBUS /
  SIGILL / SIGFPE) or ends with no audio (a header-only WAV) without naming
  a reason; uade's "score died" (its player gave up — also what a missing
  companion file looks like) and its bare "Can not play" count here too.
  Marked only once that renderer has played some other file in this server
  run (``_proven``, by renderer: a format can be played by more than one):
  a renderer — or a SoniqBoom build — that fails on everything is broken,
  not the files.
* A renderer that refuses ``_BREAKER`` different files in a row with no
  render of it playing in between is taken for broken: those marks are
  taken off again and it marks nothing more until one of its renders plays
  (``_streaks``).

**Which never mark**: a timeout, a render we stopped or cancelled (a skip, a
listener who left, the render-slot stop — ``CancelledError`` /
``RenderStopped``; a renderer killed by SIGKILL / SIGTERM / SIGINT /
SIGHUP / SIGPIPE / SIGABRT), a missing or unloadable renderer (501, exit
126 / 127), a broken installation (uade can't load its score or players),
an I/O error (stderr naming a file that can't be opened, read or found, a
full disk, too many open files, no memory, a broken pipe — and less than
``_MIN_FREE_BYTES`` free in the render folder), a usage error (a pipeline
bug), a companion sample file that is missing, a silent tune
(``conversion_cache`` 422 — its content), audio cut short part-way, a
source fetch that failed (it fails before any render), ffmpeg (transcodes,
and libgme's ffmpeg fallback), any other tune than the file's own default
one (a multi-tune file whose tune 4 crashes still plays), and a local file
whose bytes on disk are not the size the library knows.  A copy fetched
from a share or an online collection is checked by the remote cache against
the size its server states (a cut transfer is never kept); against the
library's size only loosely (``_size_ok``: an online collection's index
rounds sizes).

**Verification**: the first refusal re-runs the render once at once
(``render_checked`` — the render is a few milliseconds when it refuses at
load).  Only the same renderer and the same reason both times marks; a
refusal that took longer than ``VERIFY_MAX_S`` is not re-run (and not
marked).  A track already marked on the current fingerprint with that reason
is answered from its mark after one attempt.  A play that waits for a render
someone else started gets that render's verified answer too.

**Fingerprint** (``fingerprint``): the renderer and its version ("uade123
3.05"), the format's render revision (``conversion_cache._RENDER_REVS``),
these rules (``RULES``) and the SoniqBoom version.  ``sweep_stale`` (at
startup and every ``SWEEP_EVERY_S``) clears every mark whose fingerprint is
no longer the current one — a new uade, or a SoniqBoom release, tries the
file again (a renderer whose version can't be read just then keeps its
marks).  A changed file (another checksum; without one, another size or
modification time) loses its mark on rescan (``carry``).

**User marks** (``health`` broken / glitched, ``skip_rules``) are separate
fields and never touched here: both can be on a track.  The row's badge shows
"can't play" first (the server's finding about this renderer), Track Info
keeps the listener's report under Playback, and once the automatic mark
clears the listener's report is the badge again (a report on the track keeps
the Playback choice in Track Info, to clear it).  A scan-time defect —
"corrupt" (never rendered) or "empty" — is never replaced; a "partial" one
is, and it comes back when the mark clears (``defect_evidence.prev``).
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException

log = logging.getLogger(__name__)

DEFECT = "unplayable"
FIELDS = ("defect", "defect_detail", "defect_renderer", "defect_at", "defect_evidence")
PUBLIC_FIELDS = ("defect", "defect_detail", "defect_renderer", "defect_at")
HEADER = "X-SB-Defect"            # on the 422 of a verified refusal
RULES = 1                         # bump when ``classify`` changes: every mark is retried
VERIFY_MAX_S = 20.0
SWEEP_EVERY_S = 3600.0
_MIN_FREE_BYTES = 256 * 1024 * 1024
_MD5_MAX_BYTES = 256 * 1024 * 1024
_LABEL_TTL_S = 60.0
_BREAKER = 4                      # different files refused in a row → the renderer is broken

# A renderer's kind (the ``kind`` its render helpers pass to
# ``stream._renderer_failure``) → its Settings → Renderers key.
KIND_KEYS = {
    "uade": "uade123", "SID": "sidplayfp", "tracker": "openmpt123", "MED": "zxtune123",
    "VGM": "zxtune123", "PSF": "zxtune123", "GME": "gme", "MIDI": "fluidsynth",
    "HVL": "hvl2wav", "YM": "ym2wav", "SNDH": "psgplay", "adlib": "adplay", "sc68": "sc68",
}
# A render kind (``get_or_render``'s ``format_type``) → the renderer that
# usually plays it: what a render that played proves (``_proven``).
FORMAT_KEYS = {
    "uade": "uade123", "sid": "sidplayfp", "tracker": "openmpt123", "gme": "gme",
    "midi": "fluidsynth", "hvl": "hvl2wav", "psf": "zxtune123", "sndh": "psgplay",
    "ym": "ym2wav", "sc68": "sc68", "adlib": "adplay", "imf": "adplay",
}
# What the listener reads for each renderer.
NAMES = {
    "uade123": "the Amiga player (uade)", "sidplayfp": "the C64 player (sidplayfp)",
    "openmpt123": "the module player (libopenmpt)", "zxtune123": "the chiptune player (ZXTune)",
    "gme": "the console chip player (libgme)", "fluidsynth": "the MIDI synth (FluidSynth)",
    "hvl2wav": "the HivelyTracker player (hvl2wav)", "ym2wav": "the YM player (ST-Sound)",
    "psgplay": "the Atari ST player (psgplay)", "adplay": "the AdLib player (AdPlug)",
    "sc68": "the Atari ST player (sc68)",
}

# stderr that is about reaching or writing a file, the machine or how the
# renderer was called — never about the file's content.
_IO_RE = re.compile(
    r"no such file|permission denied|not permitted|no space left|disk full|quota|"
    r"input/output error|i/o error|read error|write error|short read|file not found|"
    r"can not request amiga file|"
    r"could not open|cannot open|can't open|can not open|unable to open|failed to open|"
    r"error opening|opening output|could not create|cannot create|can't create|"
    r"broken pipe|resource temporarily unavailable|too many open files|"
    r"cannot allocate|out of memory|not enough memory|memory allocation|"
    r"interrupted|timed out|timeout|connection|network|"
    r"unknown format|\brom\b|\broms\b|\b(?:kernal|basic|chargen)\b", re.IGNORECASE)
_USAGE_RE = re.compile(
    r"usage:|unrecognized option|unrecognised option|unknown option|invalid option|"
    r"illegal option|invalid parameter|invalid argument|missing argument|"
    r"requires an argument|--help", re.IGNORECASE)
_LOADER_RE = re.compile(
    r"dyld|symbol not found|error while loading shared librar|image not found|"
    r"cannot open shared object|"
    # a uade that can't find its own parts (score, players, config)
    r"can not load score|can't load score|uadecore|eagleplayer\.conf|uade\.conf|"
    r"can not find|could not find .*(?:score|player|uade)", re.IGNORECASE)
# Signals that end a renderer we (or the system) stopped: never the file.
_STOP_SIGNALS = {9, 15, 2, 1, 13, 6}       # KILL, TERM, INT, HUP, PIPE, ABRT
_NOT_RUNNABLE = {126, 127}                  # the shell's "can't execute" / "not found"
_CRASH_SIGNALS = {11: "SIGSEGV", 10: "SIGBUS", 7: "SIGBUS", 4: "SIGILL", 8: "SIGFPE"}


class RenderRefusal(HTTPException):
    """A renderer refused the file (``classify``): the HTTP error a play gets
    (its usual status and text until it is ``verified``), with who refused
    (``renderer``: a Settings → Renderers key), why (``why``: the words after
    the renderer's name — "stops on this file: score crashed") and whether
    the renderer named the reason (``diagnosed``)."""

    def __init__(self, status_code: int, detail: str, *, renderer: str, why: str,
                 diagnosed: bool, headers: "dict | None" = None, verified: bool = False):
        super().__init__(status_code, detail, headers=headers)
        self.renderer = renderer
        self.why = why
        self.diagnosed = diagnosed
        self.verified = verified

    @property
    def signature(self) -> tuple:
        return (self.renderer, self.why)


def _clean(msg: str) -> str:
    """One line of renderer text fit for a listener: the leading ``name:``
    parts (a program, "error", a file name) dropped, no word with a path in
    it, at most 120 characters."""
    parts = " ".join(str(msg or "").split()).split(": ")
    while len(parts) > 1 and (" " not in parts[0] or "." in parts[0] or "/" in parts[0]):
        parts.pop(0)
    msg = " ".join(w for w in ": ".join(parts).split() if "/" not in w and "\\" not in w)
    return msg.strip(" .:;-")[:120]


def _diagnosis(key: str, low: str, text: str) -> "str | None":
    """The reason renderer ``key`` names for the file in its stderr (``low``:
    lower-cased), or None."""
    if key == "uade123":
        if "score crashed" in low:
            return "stops on this file: score crashed"
        if "illegal instruction" in low:
            return "stops on this file: illegal instruction"
        if "score died" in low:
            return "stops on this file: score died"        # (silent class: ``classify``)
        if "module check failed" in low:
            return "doesn't recognise this file as a module it plays"
        if "please depack" in low:
            return "can't play this file: it is packed with a cruncher SoniqBoom can't unpack"
        if "can not play" in low:
            return "refuses this file"
        return None
    if key == "sidplayfp":
        for ln in text.splitlines():
            m = re.search(r"(?:sidtune error:?\s*)?(.*)$", ln.split("sidplayfp:", 1)[-1].strip(),
                          re.IGNORECASE)
            line = (m.group(1) if m else "").strip()
            l2 = line.lower()
            if any(w in l2 for w in ("not supported", "unsupported", "invalid", "corrupt",
                                     "truncated", "could not determine", "exceeds",
                                     "bad address", "bad reloc")):
                return f"refuses this file: {_clean(line)}"
        return None
    if key == "psgplay":
        for ln in text.splitlines():
            if re.search(r"invalid sndh|unsupported|corrupt", ln, re.IGNORECASE):
                return f"refuses this file: {_clean(ln)}"
        return None
    if key == "adplay":
        return "doesn't recognise this file" if "unknown filetype" in low else None
    if key == "openmpt123":
        return "can't load this file" if "error loading" in low or "errors loading" in low else None
    return None


def classify(kind: str, returncode, err_text: str = "", *, audio: bool = False,
             tiny: bool = False) -> "tuple[str, str, bool] | None":
    """How renderer ``kind`` failed on a file: ``(renderer key, why,
    diagnosed)`` when the outcome says the file can't be played by it (see
    the module docstring), None for anything transient, ours or unknown.

    ``returncode``: its exit status (negative: the signal that ended it; 0
    with ``audio`` False: it ended without audio).  ``audio``: it wrote
    audio (then nothing is marked — it plays, at least in part); ``tiny``:
    what it wrote is under a second (only a uade crash counts then).  Pure."""
    key = KIND_KEYS.get(kind)
    if not key:
        return None
    text = err_text or ""
    low = text.lower()
    if _LOADER_RE.search(low) or _USAGE_RE.search(low):
        return None
    # (the emulated Amiga CPU crashing is never an I/O problem — any other
    # reason next to an I/O word is not trusted)
    diag = _diagnosis(key, low, text)
    crash = diag in ("stops on this file: score crashed", "stops on this file: illegal instruction")
    if not crash and _IO_RE.search(low):
        return None
    if audio:
        if tiny and crash:
            return key, diag, True
        return None
    try:
        rc = int(returncode)
    except (TypeError, ValueError):
        rc = 1
    if rc in _NOT_RUNNABLE:
        return None
    if rc < 0:
        sig = -rc
        if sig in _STOP_SIGNALS or sig not in _CRASH_SIGNALS:
            return None
        return key, f"crashes on this file ({_CRASH_SIGNALS[sig]})", False
    if diag is not None:
        # (uade's "score died" and bare "Can not play" name no reason — a
        # missing companion or a broken install says them too: silent class)
        return key, diag, diag not in ("refuses this file", "stops on this file: score died")
    if rc != 0:
        return key, f"stops on this file (exit status {rc})", False
    return key, "produces no audio from this file", False


def refusal(kind: str, status: int, detail: str, returncode, err_text: str = "", *,
            audio: bool = False, tiny: bool = False) -> HTTPException:
    """The HTTP error for a renderer failure (``status`` / ``detail`` as the
    caller had them): a ``RenderRefusal`` when ``classify`` says the file
    can't be played by this renderer, else a plain ``HTTPException``."""
    c = classify(kind, returncode, err_text, audio=audio, tiny=tiny)
    if c is None:
        return HTTPException(status, detail)
    key, why, diagnosed = c
    return RenderRefusal(status, detail, renderer=key, why=why, diagnosed=diagnosed)


def no_audio(kind: str, err_text: str = "") -> HTTPException:
    """The 422 of a render that ended (exit 0) without audio."""
    return refusal(kind, 422, "The renderer finished but produced no audio for this file.",
                   0, err_text)


# ── The play a render is for ─────────────────────────────────────────────────

@dataclass(frozen=True)
class PlayContext:
    """What a render started by a play (or a prewarm) is for: the track, the
    local file the renderer reads, whether it is the file's own default tune
    (``bare``) and whether the file came from a share / an online collection
    (``remote``)."""
    track_id: str
    path: str
    bare: bool
    remote: bool


_play: "contextvars.ContextVar[PlayContext | None]" = contextvars.ContextVar(
    "unplayable_play", default=None)


def set_play(track_id: str, path, *, bare: bool, remote: bool) -> None:
    """Name the play the renders started from here on are for (a context
    variable: the render tasks ``get_or_render`` starts copy it)."""
    _play.set(PlayContext(str(track_id), str(path), bool(bare), bool(remote)))


def current_play() -> "PlayContext | None":
    return _play.get()


# ── Fingerprint ──────────────────────────────────────────────────────────────

_labels: "dict[str, tuple[str, float]]" = {}


async def renderer_label(key: str, *, fresh: bool = False) -> str:
    """"uade123 3.05": the renderer ``key`` as installed now (cached
    ``_LABEL_TTL_S``; ``fresh`` asks it again)."""
    now = time.monotonic()
    hit = _labels.get(key)
    if hit is not None and not fresh and now - hit[1] < _LABEL_TTL_S:
        return hit[0]
    try:
        from soniqboom.api.admin import renderer_label as _rl
        label = (await _rl(key)) or key
    except Exception:                                   # noqa: BLE001
        label = key
    _labels[key] = (label, now)
    return label


async def fingerprint(key: str, format_type: str, *, fresh: bool = False) -> str:
    """What a mark by renderer ``key`` on a ``format_type`` render holds
    while it is current (see the module docstring)."""
    from soniqboom import __version__
    try:
        from soniqboom.core.conversion_cache import _RENDER_REVS
        rev = _RENDER_REVS.get(format_type, 0)
    except Exception:                                   # noqa: BLE001
        rev = 0
    label = await renderer_label(key, fresh=fresh)
    return f"{label}|{format_type}:r{rev}|rules{RULES}|sb{__version__}"


# ── Verification and marks ───────────────────────────────────────────────────

_proven: "dict[str, float]" = {}       # renderer key → when one of its renders last played
# renderer key → the tracks it marked since its last render that played
# (``_BREAKER``); a key in ``_tripped`` marks nothing until one plays.
_streaks: "dict[str, list[str]]" = {}
_tripped: "set[str]" = set()


def _store():
    from soniqboom.core.store import get_store
    return get_store()


def _broadcast(track_id: str) -> None:
    """Tell open pages the track's fields changed (they re-read it)."""
    try:
        from soniqboom.api import library
        task = asyncio.get_running_loop().create_task(library.broadcast_track_updated(track_id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception:                                   # noqa: BLE001
        log.debug("track-updated broadcast failed", exc_info=True)


_tasks: set = set()


def _file_facts(path: str) -> "tuple[int, str | None] | None":
    """``(size, md5)`` of the file a renderer read (md5 None above
    ``_MD5_MAX_BYTES``), or None when it can't be read.  Blocking."""
    try:
        st = os.stat(path)
        if not os.path.isfile(path):
            return None
        if st.st_size > _MD5_MAX_BYTES:
            return st.st_size, None
        h = hashlib.md5()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return st.st_size, h.hexdigest()
    except OSError:
        return None


def _disk_ok() -> bool:
    """Room in the render folder: a full disk fails every render alike."""
    try:
        from soniqboom.core import render_tmp
        d = render_tmp.tmp_dir()
    except Exception:                                   # noqa: BLE001
        import tempfile
        d = tempfile.gettempdir()
    try:
        return shutil.disk_usage(d).free >= _MIN_FREE_BYTES
    except OSError:
        return False


def _size_ok(known, size: int, remote: bool) -> bool:
    """Is a file of ``size`` bytes the one the library lists at ``known``?
    A local file: exactly (when a size is stored).  A copy from a share or an
    online collection: the remote cache already refused any transfer shorter
    than its server states; the library's own size may be an index's rounded
    one (Aminet's "12K"), so only a clear mismatch — more than 6 % or 2 KB —
    counts against it.  Pure."""
    if not (isinstance(known, int) and known > 0):
        return True
    if not remote:
        return known == size
    return abs(known - size) <= max(2048, known * 0.06)


def is_marked(t: "dict | None") -> bool:
    return isinstance(t, dict) and t.get("defect") == DEFECT


def _sentence(t: dict, detail: str) -> str:
    """What a play of a marked track answers: "“seven cities of gold” can’t
    be played here — the Amiga player (uade) stops on this file: score
    crashed."."""
    title = str(t.get("title") or "").strip() or "This file"
    name = f"“{title}”" if title != "This file" else title
    d = detail[:1].lower() + detail[1:] if detail else "the renderer can’t play it"
    return f"{name} can’t be played here — {d.rstrip('.')}."


def _verified(t: dict, exc: RenderRefusal) -> RenderRefusal:
    detail = str(t.get("defect_detail") or "")
    return RenderRefusal(422, _sentence(t, detail), renderer=exc.renderer, why=exc.why,
                         diagnosed=exc.diagnosed, headers={HEADER: DEFECT}, verified=True)


async def render_checked(render_fn, format_type: str):
    """``await render_fn()`` — a render — with a refusal verified: re-run once
    at once, and the track marked when it refuses the same way again (see the
    module docstring).  Returns what the render returned; raises its error
    (a verified one as a 422 naming the reason, ``HEADER`` set)."""
    t0 = time.monotonic()
    try:
        return await render_fn()
    except RenderRefusal as first:
        took = time.monotonic() - t0
        ctx = _play.get()
        if ctx is None or not ctx.bare:
            raise
        return await _verify(first, render_fn, format_type, ctx, took)


async def _verify(first: RenderRefusal, render_fn, format_type: str, ctx: PlayContext,
                  took: float):
    store = _store()
    t = store.get_track(ctx.track_id)
    if not t or t.get("defect") in ("corrupt", "empty"):
        raise first
    fp = await fingerprint(first.renderer, format_type)
    ev = t.get("defect_evidence") if is_marked(t) else None
    if isinstance(ev, dict) and ev.get("fp") == fp and ev.get("why") == first.why:
        raise _verified(t, first)              # (the mark is the earlier evidence)
    if first.renderer in _tripped:
        log.info("Render of %s failed (%s %s) — not marked: %s refused several files in a "
                 "row and none of its renders has played since", ctx.track_id,
                 first.renderer, first.why, first.renderer)
        raise first
    if not first.diagnosed and first.renderer not in _proven:
        log.info("Render of %s failed (%s %s) — not marked: no %s render has played "
                 "in this run yet", ctx.track_id, first.renderer, first.why, first.renderer)
        raise first
    if took > VERIFY_MAX_S:
        log.info("Render of %s failed after %.0f s (%s %s) — not re-run, not marked",
                 ctx.track_id, took, first.renderer, first.why)
        raise first
    log.info("Render of %s refused (%s %s) — rendering it once more to verify",
             ctx.track_id, first.renderer, first.why)
    try:
        out = await render_fn()
    except RenderRefusal as second:
        if second.signature != first.signature:
            log.info("Render of %s failed differently the second time (%s / %s) — not marked",
                     ctx.track_id, first.why, second.why)
            raise
        marked = await mark(ctx, second, format_type, fp)
        raise (marked or second)
    log.info("Render of %s failed once (%s %s), then played — not marked",
             ctx.track_id, first.renderer, first.why)
    return out


async def mark(ctx: PlayContext, exc: RenderRefusal, format_type: str,
               fp: "str | None" = None) -> "RenderRefusal | None":
    """Mark ``ctx``'s track unplayable on ``exc`` (a refusal seen twice).
    Returns the verified 422 to answer, or None when it was not marked (the
    file on disk isn't the one the library knows, the disk is full, the
    track is gone)."""
    store = _store()
    t = store.get_track(ctx.track_id)
    if not t:
        return None
    facts = await asyncio.to_thread(_file_facts, ctx.path)
    if facts is None:
        log.info("Not marking %s: the file it rendered can't be read now", ctx.track_id)
        return None
    size, md5 = facts
    if not _size_ok(t.get("file_size"), size, ctx.remote):
        log.info("Not marking %s: the copy rendered is %d bytes, the library knows %s "
                 "(not the file it lists?)", ctx.track_id, size, t.get("file_size"))
        return None
    if not await asyncio.to_thread(_disk_ok):
        log.info("Not marking %s: the render folder is (nearly) full", ctx.track_id)
        return None
    if fp is None:
        fp = await fingerprint(exc.renderer, format_type)
    label = (await renderer_label(exc.renderer)) or exc.renderer
    old = t.get("defect_evidence") if is_marked(t) else None
    prev = (old.get("prev") if isinstance(old, dict)
            else ([t.get("defect"), t.get("defect_detail")] if t.get("defect") else None))
    name = NAMES.get(exc.renderer, f"the renderer ({exc.renderer})")
    detail = f"{name[:1].upper()}{name[1:]} {exc.why}"
    fields = {
        "defect": DEFECT, "defect_detail": detail, "defect_renderer": label,
        "defect_at": round(time.time(), 3),
        "defect_evidence": {"renderer": exc.renderer, "fmt": format_type, "fp": fp,
                            "why": exc.why, "size": size, "md5": md5, "mtime": t.get("mtime"),
                            "label": label, "prev": prev},
    }
    streak = _streaks.setdefault(exc.renderer, [])
    if ctx.track_id not in streak:
        streak.append(ctx.track_id)
    if len(streak) >= _BREAKER:
        # The renderer refuses everything: it is broken, not the files.
        _tripped.add(exc.renderer)
        undone = [tid for tid in streak[:-1] if clear(tid, f"{exc.renderer} refused "
                                                     f"{len(streak)} files in a row", store)]
        streak.clear()
        log.warning("%s refused %d different files in a row with none of its renders "
                    "playing in between — taken for broken, not the files: %d mark(s) "
                    "taken off, none made until one of its renders plays",
                    exc.renderer, _BREAKER, len(undone))
        return None
    store.update_track_fields(ctx.track_id, fields)
    _bump()
    _broadcast(ctx.track_id)
    log.warning("Marked %s (%s) can't play: %s — %s", ctx.track_id, t.get("title") or "?",
                detail, label)
    return _verified({**t, **fields}, exc)


def _bump() -> None:
    try:
        from soniqboom.core import skip_rules
        skip_rules.bump()
    except Exception:                                   # noqa: BLE001
        pass


def unmark_fields(t: dict) -> dict:
    """The fields that take ``t``'s automatic mark off (the defect it had
    before, if any, comes back)."""
    ev = t.get("defect_evidence") if isinstance(t.get("defect_evidence"), dict) else {}
    prev = ev.get("prev") if isinstance(ev.get("prev"), (list, tuple)) else None
    out = {"defect": None, "defect_detail": None, "defect_renderer": None,
           "defect_at": None, "defect_evidence": None}
    if prev and len(prev) == 2 and prev[0] not in (None, DEFECT):
        out["defect"], out["defect_detail"] = prev[0], prev[1]
    return out


def clear(track_id: str, why: str, store=None, *, tell: bool = True) -> bool:
    """Take the automatic mark off ``track_id`` (``why``: for the log;
    ``tell``: the skip set and open pages follow at once — a sweep tells
    them once, at its end)."""
    store = store or _store()
    t = store.get_track(track_id)
    if not is_marked(t):
        return False
    store.update_track_fields(track_id, unmark_fields(t))
    if tell:
        _bump()
        _broadcast(track_id)
    log.info("Cleared the can't-play mark of %s (%s): %s", track_id,
             t.get("title") or "?", why)
    return True


def rendered_ok(format_type: str) -> None:
    """A ``format_type`` render played (stored, not silent): its renderer is
    proven (and any refusal streak of it ends), and the track of the play it
    was for — when it rendered the file's default tune — loses an automatic
    mark."""
    key = FORMAT_KEYS.get(format_type)
    if key:
        _proven[key] = time.monotonic()
        _streaks.pop(key, None)
        _tripped.discard(key)
    ctx = _play.get()
    if ctx is None or not ctx.bare:
        return
    try:
        store = _store()
        if is_marked(store.get_track(ctx.track_id)):
            clear(ctx.track_id, "it rendered fine", store)
    except Exception:                                   # noqa: BLE001
        log.debug("clearing a mark after a good render failed", exc_info=True)


async def sweep_stale(store=None) -> int:
    """Clear every mark whose fingerprint isn't the current one (a renderer
    upgrade, a SoniqBoom release, new rules).  Returns how many."""
    store = store or _store()

    def _marked():
        return [(tid, t.get("defect_evidence")) for tid, t in list(store._tracks.items())
                if t.get("defect") == DEFECT]
    marked = await asyncio.to_thread(_marked)            # (a pass over the whole library)
    cleared: list = []
    if not marked:
        return 0
    cur: dict = {}
    n = 0
    for tid, ev in marked:
        ev = ev if isinstance(ev, dict) else {}
        k = (ev.get("renderer") or "", ev.get("fmt") or "")
        if k not in cur:
            if not k[0]:
                cur[k] = None
            else:
                label = await renderer_label(k[0], fresh=True)
                # A renderer whose version can't be read just now (its
                # probe failed) keeps its marks: no version is no change.
                cur[k] = ("keep" if label == k[0] and ev.get("label") not in (None, k[0])
                          else await fingerprint(k[0], k[1]))
        if cur[k] == "keep":
            continue
        if cur[k] is None or ev.get("fp") != cur[k]:
            if clear(tid, f"the renderer or SoniqBoom changed ({ev.get('fp')} → {cur[k]})",
                     store, tell=False):
                n += 1
                cleared.append(tid)
    if n:
        _bump()                     # (queues re-check their rows: availability ``marks``)
        if n <= 50:                 # (open lists re-read those rows; more: on their next read)
            for tid in cleared:
                _broadcast(tid)
        log.info("Can't-play marks: %d cleared — their renderer or SoniqBoom changed, "
                 "they are tried again", n)
    return n


async def sweep_loop() -> None:
    """``sweep_stale`` now and every ``SWEEP_EVERY_S``."""
    while True:
        try:
            await sweep_stale()
        except Exception:                               # noqa: BLE001
            log.debug("can't-play sweep failed", exc_info=True)
        await asyncio.sleep(SWEEP_EVERY_S)


def carry(old: dict, new: dict) -> None:
    """A re-read of ``old``'s file (``new``, mutated): keep the automatic mark
    while it is the same file — the checksum the mark was made on, or (no
    checksum read) its size — and the fresh read found no other defect."""
    if not is_marked(old):
        return
    ev = old.get("defect_evidence")
    if not isinstance(ev, dict):
        return
    prev = ev.get("prev") if isinstance(ev.get("prev"), (list, tuple)) else None
    if new.get("defect") and not (prev and new.get("defect") == prev[0]):
        return
    md5, size = ev.get("md5"), ev.get("size")
    if new.get("file_md5"):
        same = bool(md5) and new["file_md5"] == md5
    else:
        # (no checksum read: the size the mark was made on — the copy's or
        # the library's — and no other modification time)
        same = (isinstance(size, int) and size > 0
                and new.get("file_size") in (size, old.get("file_size"))
                and (not new.get("mtime") or not old.get("mtime")
                     or new.get("mtime") == old.get("mtime")))
    if not same:
        return
    for f in FIELDS:
        new[f] = old.get(f)


def reset_for_tests() -> None:
    _proven.clear()
    _labels.clear()
    _streaks.clear()
    _tripped.clear()
