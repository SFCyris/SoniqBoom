# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What auto-play passes by, besides the tracks that can't play right now.

Two rules, one contract — the one "Tracks on unavailable drives" already
follows (``core/availability.py``): a track the queue moves on to by itself
(the next track, Play all, a shuffle deal, radio) is passed by; a track the
listener clicks still plays.

* **Reported problems** (GitHub #36): a track marked *broken* by a user with
  edit rights (``health == "broken"``), a file no engine can decode
  (``defect == "corrupt"``) and one its renderer refused the same way twice
  when it was played (``defect == "unplayable"``, ``core/unplayable.py`` —
  a click renders it again).  *Glitched* marks and *partial* defects are only
  shown, never skipped.  The mark lives on the track (``health``,
  ``health_note``, ``health_by``, ``health_at``, ``health_renderer``), is
  carried over rescans (``store._carry_enrichment``) and is set on every copy
  with the same file checksum (``set_mark``).  A registry in the store config
  (``REG_KEY``) keeps every mark by the file's checksum and by its track id,
  so a file that went away and came back unchanged — or a copy added later —
  gets its mark again (``restore``); a changed file doesn't.
* **Short tunes** (GitHub #37): the server-wide setting "Skip short tunes"
  (store config ``skip_short_tunes``: 0 = off, else seconds) passes by a tune
  of a retro format (``is_tune_file`` — not ordinary audio) whose own length
  is known to be shorter — sound effects, jingles.  Known (``known_len``):
  the tune's entry in HVSC's song lengths (``hvsc_lengths``, SID) or in the
  lengths the file states (``tune_lengths``: SNDH, NSFe / AY), else —
  for the tune a plain play plays — the file's stored length, unless it is a
  placeholder (``_placeholder``); a tune of unknown length is never skipped.
  A file is passed by when its DEFAULT tune is short (the bare play); the
  other tunes of a multi-tune file are separate entries (the web tune
  picker, Subsonic's per-tune songs — ``subsonic_index.listed_tunes``), each
  judged on its own.

``skip_ids`` is the set of files auto-play passes by; ``availability``
adds it to the tracks that can't play right now, so every shuffle, radio,
similar-tracks and paged queue honours both.
"""
from __future__ import annotations

import threading
import time
from typing import Any

SHORT_KEY = "skip_short_tunes"
SHORT_CHOICES = (0, 5, 10, 20, 30)

STATES = ("broken", "glitched")
# Defects auto-play passes by: a file no engine decodes (found at scan) and
# one its renderer refused twice when played (``core/unplayable.py``).
_UNPLAYABLE_DEFECTS = ("corrupt", "unplayable")
# The fields ``cant_play`` reads.
CANT_PLAY_FIELDS = frozenset(("health", "defect"))
HEALTH_FIELDS = ("health", "health_note", "health_by", "health_at", "health_renderer")
REG_KEY = "health_marks"
NOTE_MAX = 200

_lock = threading.Lock()
_memo: dict[str, Any] = {"hard": None, "seqs": None, "at": 0.0, "ids": frozenset()}
_gen = 0                 # bumped by every mark and setting change (``marks_gen``, the memo)
_BOOT = __import__("uuid").uuid4().hex[:8]


def bump() -> None:
    """A mark or the setting changed: the availability token moves, so open
    pages re-read their rows and re-check the queue's."""
    global _gen
    with _lock:
        _gen += 1


def token_part(store=None) -> str:
    """What the availability token (``availability.token``) adds for these
    rules: the setting and, while it is on, the lengths that mean "unknown"
    (open pages re-read their lists when either changes).  A mark changes no
    list — ``marks_gen`` tells clients to re-check the rows they hold."""
    secs = short_secs(store)
    if not secs:
        return "0"
    return f"{secs}/" + ",".join(f"{v:g}" for v in placeholder_seconds(store))


def marks_gen() -> str:
    """Changes with every mark (``GET /api/library/availability`` ``marks``):
    a client holding rows (its queue) re-reads their marks."""
    return f"{_BOOT}.{_gen}"


def normalize_short(value: Any) -> int:
    """A ``skip_short_tunes`` value → one of ``SHORT_CHOICES`` (0 when it
    isn't one)."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 0
    return v if v in SHORT_CHOICES else 0


def short_secs(store=None) -> int:
    """The setting: tunes shorter than this many seconds are skipped (0 =
    off)."""
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    try:
        return normalize_short(store.get_config(SHORT_KEY, 0))
    except Exception:                                   # noqa: BLE001
        return 0


LENGTH_FIELDS = ("hvsc_lengths", "tune_lengths")
_MAX_LEN_S = 3600.0
_tune_exts: "frozenset | None" = None
# Ordinary audio: never a tune, whatever its tags say (an MP3 remix tagged
# "Amiga" / "Module").
_AUDIO_EXTS = frozenset({".mp3", ".flac", ".m4a", ".mp4", ".m4b", ".aac", ".alac", ".ogg",
                         ".oga", ".opus", ".wav", ".wave", ".aif", ".aiff", ".aifc", ".wma",
                         ".ape", ".wv", ".mpc", ".dff", ".wsd", ".caf", ".mka", ".webm"})
# libgme formats whose stated lengths (``tune_lengths``) are stored with the
# renderer's fixed fade added (``gme_render.FADE_S``).
_GME_FADED_EXTS = frozenset({".spc", ".vgm", ".vgz", ".nsfe", ".ay"})
_GME_FADE_S = 8.0


def _exts() -> frozenset:
    global _tune_exts
    if _tune_exts is None:
        from soniqboom.core import metadata as m
        _tune_exts = frozenset(
            m.SID_EXTS | m._MIDI_EXTS | m._TRACKER_EXTS | m._GME_EXTS | m._ADLIB_EXTS
            | m._ATARI_EXTS | m._PSF_EXTS | m._UADE_SUFFIX_EXTS | {".imf", ".hvl", ".ahx"})
    return _tune_exts


def _suffix(t: dict) -> str:
    tail = str(t.get("path") or "").rsplit("::", 1)[-1]
    dot = tail.rfind(".")
    return tail[dot:].lower() if dot > tail.rfind("/") else ""


def is_tune_file(t: dict) -> bool:
    """Is ``t`` a retro tune SoniqBoom renders (SID, tracker, chip, AdLib,
    Atari, PSF, MIDI, Amiga) — not ordinary audio, whose short tracks
    (an album's interlude) are never skipped?"""
    ext = _suffix(t)
    if ext in _exts():
        return True
    if ext in _AUDIO_EXTS:
        return False
    g = t.get("genre")
    if isinstance(g, list) and "Amiga" in g and "Module" in g:
        return True                       # a uade module named by prefix (mod.foo)
    return str(t.get("format") or "").upper() == "DSF (DREAMCAST)"


def _fade(t: dict, stated: bool = False) -> float:
    """The fade-out included in a stored length of ``t``: its ``fade_s`` (a
    PSF rip's), and for a length the file states (``stated``: its
    ``tune_lengths``) a libgme rip's fixed fade.  (A libgme file's
    ``duration`` may be its render's length since its first play — no fade
    in it when the tune ended by itself; its stated length is in
    ``tune_lengths``.)"""
    f = t.get("fade_s")
    if type(f) in (int, float):
        return max(0.0, float(f))
    return _GME_FADE_S if stated and _suffix(t) in _GME_FADED_EXTS else 0.0


def _bare_index(t: dict) -> int:
    """0-based tune a plain play of ``t`` plays: its start song, else the
    first tune that isn't empty a probe found (``default_subsong``)."""
    cnt = t.get("subsongs")
    for f in ("start_subsong", "default_subsong"):
        v = t.get(f)
        if type(v) is int and type(cnt) is int and 0 < v < cnt:
            return v
    return 0


# Every default render length ("SID / chiptune default duration") in use so
# far — a track stored while an earlier one was the default still holds it,
# and it means "unknown" as much as the current one does.
PLACEHOLDER_KEY = "render_default_lengths"
_LAST_DEFAULT_KEY = "render_default_last"


def _current_default() -> "float | None":
    try:
        from soniqboom.config import settings
        return float(settings.sid_default_duration)
    except Exception:                                   # noqa: BLE001
        return None


def note_default_length(store=None, previous: "float | None" = None) -> None:
    """Remember the default render length in use now — and ``previous`` (the
    one a settings change replaced), and the one the last run used (a hand
    edit of the conf while the server was stopped) — as placeholders."""
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    cur = _current_default()
    seen = [float(v) for v in (store.get_config(PLACEHOLDER_KEY) or [])
            if type(v) in (int, float)]
    last = store.get_config(_LAST_DEFAULT_KEY)
    add = [v for v in (cur, previous, last) if type(v) in (int, float)]
    new = sorted({*seen, *(float(v) for v in add)})
    if new != sorted(seen):
        store.set_config(PLACEHOLDER_KEY, new)
        bump()                                          # (what counts as known changed)
    if cur is not None and last != cur:
        store.set_config(_LAST_DEFAULT_KEY, cur)


def placeholder_seconds(store=None) -> "list[float]":
    """The lengths that stand for "unknown" (``_placeholder``): the 180 s
    defaults and every default render length in use so far — also sent to
    the web player."""
    out = {180.0}
    cur = _current_default()
    if cur is not None:
        out.add(cur)
    try:
        if store is None:
            from soniqboom.core.store import get_store
            store = get_store()
        for v in store.get_config(PLACEHOLDER_KEY) or []:
            if type(v) in (int, float):
                out.add(float(v))
    except Exception:                                   # noqa: BLE001
        pass
    return sorted(out)


def _placeholder(d: float) -> bool:
    """A stored length that stands for "unknown" (``placeholder_seconds``)."""
    return any(abs(d - p) < 0.01 for p in placeholder_seconds())


def known_len(t: dict, wire: int, ph: "tuple | None" = None) -> float:
    """The known own length (seconds) of the tune wire ``wire`` of ``t``
    plays, 0 when unknown (see the module docstring); ``ph``: the
    placeholder lengths, when a caller loops."""
    # (before the fade-out the stored lengths include — ``_fade``)
    k = _bare_index(t) if wire == 0 else _wire_tune(t, wire)
    for f in LENGTH_FIELDS:
        lens = t.get(f)
        if isinstance(lens, list) and 0 <= k < len(lens):
            v = lens[k]
            if type(v) in (int, float) and 0 < v <= _MAX_LEN_S:
                v = float(v) - (_fade(t, stated=True) if f == "tune_lengths" else 0.0)
                return v if v > 0 else 0.0
    if wire == 0:
        d = t.get("duration")
        if (type(d) in (int, float) and 0 < d <= _MAX_LEN_S
                and not (any(abs(d - p) < 0.01 for p in ph) if ph is not None else _placeholder(d))
                and is_tune_file(t)):
            v = float(d) - _fade(t)
            return v if v > 0 else 0.0
    return 0.0


def _wire_tune(t: dict, wire: int) -> int:
    global _wt
    if _wt is None:
        from soniqboom.core.subsonic_index import wire_tune
        _wt = wire_tune
    return _wt(t, wire)


_wt = None


def tune_is_short(t: dict, wire: int, secs: int) -> bool:
    """Is the tune that wire ``wire`` of ``t`` plays known to be shorter than
    ``secs``?  False when the setting is off or the length is unknown."""
    if not secs or not isinstance(t, dict):
        return False
    return 0 < known_len(t, wire) < secs


def cant_play(t: dict) -> bool:
    """Is ``t`` a track that can't be played — reported broken
    (``health == "broken"``), undecodable (``defect == "corrupt"``) or
    refused twice by its renderer (``defect == "unplayable"``)?  What
    auto-play always passes by and what Settings → "Tracks that can't be
    played: Hide" leaves out of the lists (``core/availability``; the store
    keeps these ids as ``TrackStore._cant_play_ids``).  Glitched and partial
    tracks play: not these."""
    return t.get("health") == "broken" or t.get("defect") in _UNPLAYABLE_DEFECTS


def file_skipped(t: dict, secs: int) -> bool:
    """Does auto-play pass the file ``t`` (its bare play) by?"""
    if not isinstance(t, dict):
        return False
    if cant_play(t):
        return True
    return tune_is_short(t, 0, secs)


def entry_skipped(tid: str, wire: int = 0, store=None) -> bool:
    """Does auto-play pass the queue entry (``tid``, tune ``wire``; 0 = the
    file's bare play) by?  For queues SoniqBoom deals itself outside the web
    player — the cast queue (``cast_session``): reported broken, undecodable,
    or (with "Skip short tunes" on) that tune known to be short."""
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    t = store.get_track(tid)
    if not isinstance(t, dict):
        return False
    if cant_play(t):
        return True
    return tune_is_short(t, int(wire or 0), short_secs(store))


# After library writes (a scan writes all the time) the set is rebuilt in a
# worker thread, at most this often, while the previous one is served; a mark
# or a change of the setting rebuilds it at once.
_STALE_OK_S = 2.0
_BACKGROUND = True            # (tests: rebuild in the caller)


def _default_short(t: dict, secs: int, ph: tuple) -> bool:
    """``tune_is_short(t, 0, secs)`` for the library-wide pass (``ph``: the
    placeholder lengths), with the cheap test first: most tracks state no
    tune lengths and last longer."""
    if t.get("hvsc_lengths") or t.get("tune_lengths"):
        return tune_is_short(t, 0, secs)
    d = t.get("duration")
    if not (type(d) in (int, float) and 0 < d < secs + 60):
        return False                      # (the common case: longer, or no length)
    for p in ph:
        if abs(d - p) < 0.01:
            return False
    return is_tune_file(t) and 0 < d - _fade(t) < secs


def skip_ids(store=None) -> frozenset:
    """The ids of the files auto-play passes by (see the module docstring).
    Rebuilt (one pass over the library, ~30-40 ms per 100k tracks) at once
    after a mark or a change of the setting; after library writes at most
    every ``_STALE_OK_S``, in a worker thread, serving the previous set
    meanwhile.  Empty when nothing is skipped."""
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    secs = short_secs(store)
    seqs = (getattr(store, "_mutation_seq", None), getattr(store, "_duration_seq", None))
    hard = (id(store), secs, _gen)
    now = time.monotonic()
    with _lock:
        if _memo.get("hard") == hard:
            if _memo.get("seqs") == seqs or now - _memo.get("at", 0.0) < _STALE_OK_S:
                return _memo["ids"]
            if _BACKGROUND:
                # Library writes only: serve the set as it was while a worker
                # thread rebuilds it (one at a time).
                if not _memo.get("building"):
                    _memo["building"] = True
                    threading.Thread(target=_rebuild_bg, args=(store, secs, seqs, hard),
                                     name="skip-rules", daemon=True).start()
                return _memo["ids"]
    return _rebuild(store, secs, seqs, hard)


def _scan(store, secs: int) -> frozenset:
    # (``list(dict.items())`` copies under the GIL: safe from a worker thread
    # while the loop writes; a track dict read mid-write is judged next time.)
    ph = tuple(placeholder_seconds(store))
    out = []
    for tid, t in list((getattr(store, "_tracks", {}) or {}).items()):
        if cant_play(t) or (secs and _default_short(t, secs, ph)):
            out.append(tid)
    return frozenset(out)


def _rebuild(store, secs: int, seqs: tuple, hard: tuple) -> frozenset:
    ids = _scan(store, secs)
    with _lock:
        _memo.update(hard=hard, seqs=seqs, at=time.monotonic(), ids=ids, building=False)
    return ids


def _rebuild_bg(store, secs: int, seqs: tuple, hard: tuple) -> None:
    try:
        ids = _scan(store, secs)
    except Exception:                                   # noqa: BLE001 — the next call retries
        with _lock:
            _memo["building"] = False
        return
    with _lock:
        if _memo.get("hard") == hard:                   # (a mark / the setting won meanwhile)
            _memo.update(seqs=seqs, at=time.monotonic(), ids=ids)
        _memo["building"] = False


def coverage(tracks) -> dict:
    """How many tunes of the retro files in ``tracks`` (track dicts) have a
    known length (``known_len``) — what the setting can act on:
    ``{"tunes": <tunes>, "known": <with a known length>, "short": {secs:
    <known tunes shorter than secs>}}``.  Pure."""
    tunes = known = 0
    short = {s: 0 for s in SHORT_CHOICES if s}
    ph = tuple(placeholder_seconds())
    for t in tracks:
        if not is_tune_file(t):
            continue
        cnt = t.get("subsongs")
        n = cnt if isinstance(cnt, int) and cnt > 0 else 1
        tunes += n
        for w in range(n):
            v = known_len(t, w, ph)
            if v > 0:
                known += 1
                for s in short:
                    if v < s:
                        short[s] += 1
    return {"tunes": tunes, "known": known, "short": short}


# ── Marks ────────────────────────────────────────────────────────────────────

def mark_fields(state: str | None, note: str | None, by: str | None,
                renderer: str | None) -> dict:
    """The track fields a mark writes (all ``None`` to clear one)."""
    if state not in STATES:
        return {f: None for f in HEALTH_FIELDS}
    note = " ".join((note or "").split())[:NOTE_MAX] or None
    return {"health": state, "health_note": note, "health_by": by or None,
            "health_at": round(time.time(), 3), "health_renderer": renderer or None}


def copies(store, tid: str) -> list[str]:
    """``tid`` and the other tracks holding the same file: the members of
    its duplicate group with the same ``file_md5`` (``sid_md5`` for a
    SID)."""
    t = store.get_track(tid)
    if not t:
        return []
    out = [tid]
    gid = t.get("duplicate_group_id")
    if not gid:
        return out
    # (A duplicate group is title / artist / length — only a copy whose
    # bytes hash the same is the same file.)
    for key in ("file_md5", "sid_md5"):
        md5 = t.get(key)
        if md5:
            break
    else:
        return out
    for m in list(getattr(store, "_tag_dup_group", {}).get(gid) or ()):
        if m != tid:
            o = store.get_track(m)
            if o and o.get(key) == md5:
                out.append(m)
    return out


def reg_keys(t: dict) -> list[str]:
    """The registry keys of track ``t``: its file checksum (when the scan
    recorded one) and its id."""
    out = []
    for f, tag in (("file_md5", "md5"), ("sid_md5", "sid")):
        v = t.get(f)
        if v:
            out.append(f"{tag}:{v}")
    if t.get("id"):
        out.append(f"id:{t['id']}")
    return out


def set_mark(store, tid: str, state: str | None, note: str | None = None,
             by: str | None = None, renderer: str | None = None) -> list[str]:
    """Mark ``tid`` (and its identical copies) broken / glitched, or clear the
    mark (``state`` None) — on the tracks and in the registry.  Returns the
    ids written."""
    ids = copies(store, tid)
    fields = mark_fields(state, note, by, renderer)
    reg = dict(store.get_config(REG_KEY) or {})
    for i in ids:
        t = store.get_track(i) or {"id": i}
        f = fields
        if fields["health"] and t.get("health") == fields["health"]:
            # The same report with another note: it stays the report it was —
            # who made it, when, and with which renderer.
            f = {**fields, **{k: t.get(k) for k in ("health_by", "health_at", "health_renderer")
                               if t.get(k) is not None}}
        for k in reg_keys(t):
            if f["health"]:
                # (with the checksums the mark was made on — ``restore``)
                reg[k] = {**f, "file_md5": t.get("file_md5"), "sid_md5": t.get("sid_md5")}
            else:
                reg.pop(k, None)
        store.update_track_fields(i, f)
    store.set_config(REG_KEY, reg)
    bump()
    return ids


def restore(reg: "dict | None", t: dict) -> bool:
    """Give a track that is (re)added without a mark the one the registry
    ``reg`` holds for its checksum or id.  True when it set one."""
    if not reg or not isinstance(t, dict) or t.get("health"):
        return False
    for k in reg_keys(t):
        f = reg.get(k)
        if (isinstance(f, dict) and f.get("health") in STATES
                and f.get("file_md5") == t.get("file_md5") and f.get("sid_md5") == t.get("sid_md5")):
            for name in HEALTH_FIELDS:
                if f.get(name) is not None:
                    t[name] = f[name]
            return True
    return False


def reset_for_tests() -> None:
    with _lock:
        _memo.update(hard=None, seqs=None, at=0.0, ids=frozenset(), building=False)
