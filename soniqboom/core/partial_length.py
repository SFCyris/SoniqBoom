# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Full lengths of network-share files whose scan read only their front.

The remote scan extracts most formats from a front window of the file
(``metadata.HEADER_BUDGET`` × ``HEADER_BUDGET_MULTIPLIERS``, then the whole
file — ``scanner._process_one``).  Formats that state their length in a
header (FLAC STREAMINFO) read the real length from that window (MP4 /
M4A / AAC are always read whole).  A WAV's own reader caps its data chunk at
what the file holds — a window's length — so the scan measures it again
from the window's chunk headers and the listing size
(``metadata.wav_window_length``).  Two more do not, and mutagen measures the
window instead of the file:

* MP3 without a Xing / Info / VBRI frame count (CBR from most encoders other
  than LAME): the length is estimated as (bytes − first frame offset) × 8 /
  bitrate over the bytes mutagen is handed — a 512 KB window at 320 kbps
  reads as ~13 s, less when an embedded cover fills part of the window.
* Ogg Vorbis / Opus: the length is the granule position of the LAST page —
  the window's last page, somewhere near its end.

``mp3_length`` (from the window and the listing size) and ``ogg_length``
(from the window and a small read of the file's end, ``OGG_TAIL``) give the
file's own length; the scan applies them as it stores the track.

Tracks stored before the scan did that are corrected once per remote scan
root by ``run_backfill`` (a rescan skips unchanged files, so it would never
re-read them): candidates are told apart by their stored values
(``backfill_kind``), then each is re-measured with one or two small range
reads on the ``scan`` lane — an MP3's 10-byte ID3v2 header, an Ogg file's
first page and its last ``OGG_TAIL`` bytes.  A root is marked done
(``BACKFILL_ROOTS_KEY``) once every candidate was read; a root that is
offline, has no connected source, or had a file that could not be reached
stays pending and is retried after its next completed scan (or the next
start) — reading only the files not read yet in this process, and giving a
file up after ``_MAX_UNREACHABLE_RUNS`` failed runs at least
``_UNREACHABLE_INTERVAL_S`` apart (as ``repair``'s one-time backfills do).
Writes go through ``store.update_track_fields_batch``, so the duplicate
groups and every duration-derived view follow.

Reads run ``_concurrency(source)`` at once: an FTP share's scan-lane budget
(``filesource.ftp_lane_budgets``, capped at ``_MAX_CONCURRENCY`` and at the
live pool size minus the stream budget), ``_CONCURRENCY`` for other
sources.  A run keeps an in-memory status (``get_status`` — served by
``GET /api/admin/length-backfill/status``) and logs its progress every
``_LOG_EVERY_N`` files checked and at least every ``_LOG_EVERY_S`` seconds."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import struct
import time

log = logging.getLogger(__name__)

MP3_EXT = ".mp3"
OGG_EXTS = frozenset({".ogg", ".opus"})
PARTIAL_EXTS = frozenset({MP3_EXT}) | OGG_EXTS

# Bytes read from the end of an Ogg file for its last page (an Ogg page is at
# most 65 307 bytes, so the tail always holds the last one whole).
OGG_TAIL = 64 * 1024
# Bytes read from the front of an Ogg file for its first page (the codec
# identification header: 58 bytes for Vorbis, 47 for Opus).
OGG_HEAD = 4 * 1024
# The 10-byte ID3v2 header (its syncsafe size gives where the audio starts).
ID3_HEADER = 10

# Persisted: ``scan_root_hash`` of every remote scan root whose stored
# lengths were checked (``run_backfill``).
BACKFILL_ROOTS_KEY = "partial_length_backfill_roots"
# Persisted: ``{track id: [failed runs, time of the last counted one]}`` for
# candidates that could not be read (cleared when their root is done).
BACKFILL_ATTEMPTS_KEY = "partial_length_backfill_attempts"
_MAX_UNREACHABLE_RUNS = 3
_UNREACHABLE_INTERVAL_S = 3600.0

# A new length is written only when it is clearly longer than the stored one
# (both margins) and below this ceiling.
_MIN_GAIN_S = 1.0
_MIN_GAIN_RATIO = 1.05
_MAX_LENGTH_S = 48 * 3600.0
# Tracks re-measured per write batch.
_CHUNK = 50
# Reads at once on a source without a scan-lane budget (``_concurrency``),
# and the most on any source.
_CONCURRENCY = 2
_MAX_CONCURRENCY = 6
# A progress line is logged every this many files checked, and at least
# this often (seconds) while a run is going.
_LOG_EVERY_N = 500
_LOG_EVERY_S = 60.0
# Consecutive transient read failures after which a root is given up for
# this run (the share went away mid-run): its other candidates wait too.
# (``_max_fails_in_a_row``: one more per read at once beyond ``_CONCURRENCY``.)
_MAX_FAILS_IN_A_ROW = 5


# ── MP3 ──────────────────────────────────────────────────────────────────────

def mp3_audio_start(head: bytes) -> int:
    """Offset of the first byte after the ID3v2 tag(s) at the front of an MP3
    whose first bytes are ``head`` (0 without a tag).  Tags stacked back to
    back are stepped over as far as ``head`` reaches; a stacked tag whose
    header lies past ``head`` is not seen."""
    from soniqboom.core.tag_window import _id3_size
    off = 0
    while True:
        size = _id3_size(head[off:off + ID3_HEADER])
        if size is None:
            return off
        off += size


def mp3_window_capped(duration: float, bitrate: int, audio_bytes: int) -> bool:
    """Is ``duration`` (at ``bitrate`` bps) mutagen's size-based estimate over
    a window holding ``audio_bytes`` bytes after the ID3v2 tag — not a length
    from a Xing / VBRI frame count, nor a whole file's?  mutagen's estimate is
    exactly (window − first frame offset) × 8 / bitrate, and the first frame
    sits at the tag's end or a little after it (padding / junk), so the bytes
    the duration stands for must lie within ``[audio_bytes − slack,
    audio_bytes × 1.01 + 1 KB]``, slack = max(32 KB, 2 %).  The lower bound
    keeps a short file with non-audio bytes behind its audio (a trailing APE
    / ID3 tag with a cover) from passing as capped."""
    if not bitrate or bitrate <= 0 or not duration or duration <= 0 or audio_bytes <= 0:
        return False
    seen = duration * bitrate / 8
    slack = max(32 * 1024, audio_bytes * 0.02)
    return audio_bytes - slack <= seen <= audio_bytes * 1.01 + 1024


def mp3_full_length(file_size: int, audio_start: int, bitrate: int) -> float | None:
    """(``file_size`` − ``audio_start``) × 8 / ``bitrate`` — the length
    mutagen gives a whole CBR file without a frame count.  A trailing ID3v1
    / APE tag is counted as audio, as mutagen counts it (128 bytes of ID3v1
    is 3 ms at 320 kbps).  None without a bitrate or audio."""
    if not bitrate or bitrate <= 0 or file_size <= audio_start:
        return None
    return (file_size - audio_start) * 8 / bitrate


def mp3_length(head: bytes, file_size: int, duration: float,
               bitrate: int) -> float | None:
    """The length of an MP3 of ``file_size`` bytes whose front ``head`` was
    extracted to ``duration`` s at ``bitrate`` bps — None when ``duration``
    is not mutagen's estimate over ``head`` (``mp3_window_capped``: e.g. a
    Xing / VBRI frame count gave it), when ``head`` is the whole file or ends
    inside the ID3v2 tag, or when the result is not longer."""
    if not file_size or file_size <= len(head):
        return None
    start = mp3_audio_start(head)
    if start >= len(head) or not mp3_window_capped(duration, bitrate, len(head) - start):
        return None
    full = mp3_full_length(file_size, start, bitrate)
    return full if full and full > duration else None


# ── Ogg Vorbis / Opus ───────────────────────────────────────────────────────

def ogg_stream_head(head: bytes) -> tuple[int, str, int, int] | None:
    """``(serial, codec, sample rate, pre-skip)`` from the first page of an
    Ogg file (``head`` = its first bytes): codec "vorbis" (pre-skip 0) or
    "opus" (always 48 kHz).  None for anything else (Ogg FLAC, Speex, …) or
    a first page ``head`` doesn't hold whole."""
    from mutagen.ogg import OggPage
    try:
        page = OggPage(io.BytesIO(head))
    except Exception:                                   # noqa: BLE001
        return None
    pkt = page.packets[0] if page.packets else b""
    if pkt.startswith(b"\x01vorbis") and len(pkt) >= 16:
        return page.serial, "vorbis", struct.unpack("<I", pkt[12:16])[0], 0
    if pkt.startswith(b"OpusHead") and len(pkt) >= 12:
        return page.serial, "opus", 48000, struct.unpack("<H", pkt[10:12])[0]
    return None


def ogg_last_granule(tail: bytes, serial: int) -> int | None:
    """Granule position of the last whole page of stream ``serial`` in
    ``tail`` (a file's last bytes) on which a packet ends — the page
    mutagen reads a length from.  None when there is none (a chained file
    ending in another stream, or no page in ``tail``)."""
    from mutagen.ogg import OggPage
    end = len(tail)
    while end > 0:
        i = tail.rfind(b"OggS", 0, end)
        if i < 0:
            return None
        try:
            page = OggPage(io.BytesIO(tail[i:]))
        except Exception:                               # noqa: BLE001
            page = None                                 # "OggS" inside audio data
        if page is not None and page.serial == serial and page.position >= 0:
            return page.position
        end = i
    return None


def ogg_length(head: bytes, tail: bytes) -> float | None:
    """The length of an Ogg Vorbis / Opus file from its first bytes ``head``
    and last bytes ``tail``: last granule / sample rate (Vorbis), (last
    granule − pre-skip) / 48000 (Opus) — mutagen's own formulas.  None when
    either end can't be read as that."""
    info = ogg_stream_head(head)
    if info is None:
        return None
    serial, _codec, rate, pre_skip = info
    granule = ogg_last_granule(tail, serial)
    if granule is None or rate <= 0:
        return None
    secs = (granule - pre_skip) / rate
    return secs if secs > 0 else None


# ── Backfill of stored tracks ───────────────────────────────────────────────

def _windows(ext: str) -> list[int]:
    """Every front-window size a partial read of ``ext`` can have had."""
    from soniqboom.core.metadata import HEADER_BUDGET, HEADER_BUDGET_MULTIPLIERS
    base = HEADER_BUDGET.get(ext)
    return [base * m for m in HEADER_BUDGET_MULTIPLIERS] if base else []


def backfill_kind(t: dict) -> str | None:
    """"mp3" / "ogg" when stored track ``t`` may hold a length its scan's
    front window capped, else None.

    Only plain files of a network share (not archive members — read whole —
    and not user-edited lengths) with the ``PARTIAL_EXTS`` extensions, a
    known size and a length, and big enough that the scan read a window of
    them (more than twice ``HEADER_BUDGET``).  Then, with ``seen`` = length
    × bitrate / 8 (the audio bytes the stored length stands for) and ``cap``
    the largest window (``_windows``):

    * MP3: ``seen`` ≤ ``cap`` × 1.02 while the file is over 1.1 × ``seen``
      (``_measure`` then checks it against each window, the tag size known).
    * Ogg: the same with ``cap`` × 1.5 (a Vorbis bitrate is the nominal one,
      not the real average); without a bitrate every such file qualifies —
      its end read decides."""
    from soniqboom.core.filesource import is_member_path, is_remote_path
    path = str(t.get("path") or "")
    # (the ``::`` archive tail is partitioned off before the scheme test)
    if not path or is_member_path(path) or not is_remote_path(path):
        return None
    ext = os.path.splitext(path.lower())[1]
    if ext not in PARTIAL_EXTS or "duration" in (t.get("user_edited") or ()):
        return None
    size, dur = t.get("file_size"), t.get("duration")
    if (not isinstance(size, int) or size <= 0
            or type(dur) not in (int, float) or dur <= 0):
        return None
    windows = _windows(ext)
    if not windows or size <= windows[0] * 2:
        return None                                     # the scan read it whole
    cap = max(windows)
    rate = t.get("bitrate")
    rate = rate if isinstance(rate, int) and rate > 0 else 0
    seen = dur * rate / 8
    if ext == MP3_EXT:
        if not rate:
            return None
        return "mp3" if seen <= cap * 1.02 and size > seen * 1.1 else None
    if rate and (seen > cap * 1.5 or size <= seen * 1.1):
        return None
    return "ogg"


def _measure(source, sub: str, kind: str, t: dict) -> float | None:
    """Re-measure stored track ``t`` (``backfill_kind`` ``kind``, at
    ``sub`` on ``source``) with small range reads.  Returns its length, or
    None when it can't be told: an MP3 whose stored length is not mutagen's
    estimate over any front window once the real tag size is known
    (``mp3_window_capped``), an Ogg file that isn't plain Vorbis / Opus.
    Network errors propagate.  Blocking."""
    size = int(t["file_size"])
    if kind == "mp3":
        rate = int(t["bitrate"])
        start = mp3_audio_start(source.read_partial(sub, ID3_HEADER, lane="scan"))
        dur = float(t["duration"])
        if not any(w > start and mp3_window_capped(dur, rate, w - start)
                   for w in _windows(MP3_EXT)):
            return None
        return mp3_full_length(size, start, rate)
    head = source.read_partial(sub, OGG_HEAD, lane="scan")
    if ogg_stream_head(head) is None:
        return None
    tail = source.read_at(sub, max(0, size - OGG_TAIL), OGG_TAIL, lane="scan")
    return ogg_length(head, tail)


def _done_roots(store) -> list[str]:
    v = store.get_config(BACKFILL_ROOTS_KEY)
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def backfill_done(root_hash: str, store=None) -> bool:
    """Were the stored lengths of remote scan root ``root_hash`` checked?"""
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    return root_hash in _done_roots(store)


def mark_done(root_hash: str, store=None) -> None:
    """Record that remote scan root ``root_hash`` needs no (more) length
    backfill, and drop the failed-read counts of its tracks.  Idempotent."""
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    done = _done_roots(store)
    if root_hash not in done:
        store.set_config(BACKFILL_ROOTS_KEY, [*done, root_hash])
    got = store.get_config(BACKFILL_ATTEMPTS_KEY)
    if isinstance(got, dict) and got:
        tracks = store._tracks
        keep = {k: v for k, v in got.items()
                if (t := tracks.get(k)) is not None and t.get("scan_root_hash") != root_hash}
        if keep != got:
            store.set_config(BACKFILL_ATTEMPTS_KEY, keep)


def _unmark(root_hash: str, store) -> None:
    done = _done_roots(store)
    if root_hash in done:
        store.set_config(BACKFILL_ROOTS_KEY, [h for h in done if h != root_hash])


# Roots whose scan kept a window length because a file's end couldn't be read
# (``note_pending``) — never marked done while listed here.
_scan_pending: set[str] = set()
# Ids read in this process (fixed, found right, or not measurable) or given
# up — a retry of a pending root reads only the others.
_settled: set[str] = set()


def note_pending(root_hash: str, store=None) -> None:
    """A scan of remote root ``root_hash`` stored a window-capped length (an
    Ogg file whose end couldn't be read): the root's backfill is due again."""
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    _scan_pending.add(root_hash)
    _unmark(root_hash, store)


def after_remote_scan(scan_root: str, plan: dict) -> None:
    """Called after a completed scan of remote root ``scan_root`` (its
    ``plan``) that listed files (the share is reachable).  A root whose scan
    kept a window length (``note_pending``) is un-marked and its backfill
    started.  Otherwise, while the root is not done: when none of its stored
    tracks is a candidate (``backfill_kind`` — an in-memory sweep) it is
    marked done, else the backfill is started (``schedule_backfill``).  One
    config read when done."""
    import hashlib
    from soniqboom.core.store import get_store
    store = get_store()
    root_hash = hashlib.sha256(scan_root.encode()).hexdigest()[:16]
    if root_hash in _scan_pending:
        _scan_pending.discard(root_hash)
        _unmark(root_hash, store)
        schedule_backfill()
        return
    if root_hash in _done_roots(store):
        return
    get = store._tracks.get
    if any((t := get(tid)) is not None and backfill_kind(t)
           for tid in store.get_track_ids_for_scan_root(root_hash)):
        schedule_backfill()
    else:
        mark_done(root_hash, store)


def _attempts(store) -> dict[str, list]:
    got = store.get_config(BACKFILL_ATTEMPTS_KEY)
    out: dict[str, list] = {}
    if isinstance(got, dict):
        for k, v in got.items():
            if isinstance(v, (list, tuple)) and len(v) == 2:
                out[k] = [int(v[0]), float(v[1])]
    return out


def _count_unreachable(store, ids: set[str]) -> set[str]:
    """Count this run's failed reads (``ids``) — a run counts for an id only
    ``_UNREACHABLE_INTERVAL_S`` after its last counted one — and settle the
    ids that reached ``_MAX_UNREACHABLE_RUNS``.  Returns the ids still
    pending."""
    before = _attempts(store)
    runs = dict(before)
    now = time.time()
    pending = set()
    for i in ids:
        n, last = runs.get(i, [0, 0.0])
        if now - last >= _UNREACHABLE_INTERVAL_S:
            n, last = n + 1, now
        runs[i] = [n, last]
        if n < _MAX_UNREACHABLE_RUNS:
            pending.add(i)
        else:
            _settled.add(i)
    if runs != before:
        store.set_config(BACKFILL_ATTEMPTS_KEY, runs)
    return pending


async def _wait_for_scans() -> None:
    """Return once no scan runs — polling every 5 s, with ``waiting_for_scan``
    set in the status meanwhile and one log line when it starts waiting."""
    from soniqboom.core import scanner
    if not scanner.is_scanning():
        return
    _status["waiting_for_scan"] = True
    log.info("Length backfill%s: waiting for the running scan to finish",
             f" of {_status['root']}" if _status.get("root") else "")
    try:
        while scanner.is_scanning():
            await asyncio.sleep(5)
    finally:
        _status["waiting_for_scan"] = False


def _concurrency(source) -> int:
    """Reads the backfill runs at once on ``source``.  An FTP share: its
    scan-lane budget (``filesource.ftp_lane_budgets``), at most
    ``_MAX_CONCURRENCY`` and at most the live pool size minus the stream
    budget (a pool clamped to a detected server cap keeps the stream lane's
    slots), at least 1.  Any other source: ``_CONCURRENCY``."""
    from soniqboom.core.filesource import FTPFileSource, ftp_lane_budgets
    if not isinstance(source, FTPFileSource):
        return _CONCURRENCY
    try:
        scan, stream, _browse = ftp_lane_budgets(source._host, int(source._port))
        live = int(source._pool._max_size)
    except Exception:                                   # noqa: BLE001
        return _CONCURRENCY
    return max(1, min(_MAX_CONCURRENCY, scan, live - stream))


def _max_fails_in_a_row(concurrency: int) -> int:
    """Consecutive transient read failures after which the rest of a root
    waits for the next run: ``_MAX_FAILS_IN_A_ROW`` at ``_CONCURRENCY``
    reads at once, one more per extra read (reads in flight together fail
    together in one burst)."""
    return _MAX_FAILS_IN_A_ROW + max(0, concurrency - _CONCURRENCY)


def display_root(root: str) -> str:
    """``root`` without the user name / password of its URL (``ftp://u:p@h/x``
    → ``ftp://h/x``) — for logs and the status."""
    from urllib.parse import urlsplit, urlunsplit
    try:
        parts = urlsplit(root)
    except ValueError:
        return root
    if "@" not in parts.netloc:
        return root
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def _label(root: str) -> str:
    """The folder alias of ``root`` (Settings → Library), else
    ``display_root``."""
    try:
        from soniqboom.config import settings
        alias = (settings.folder_aliases or {}).get(root)
    except Exception:                                   # noqa: BLE001
        alias = None
    return str(alias) if alias else display_root(root)


# ── Status / progress ───────────────────────────────────────────────────────

_COUNT_KEYS = ("total", "checked", "fixed", "skipped", "unreachable")


def _idle_status() -> dict:
    return {
        "running": False, "started_at": None, "waiting_for_scan": False,
        "root": None, "label": None,
        "current": dict.fromkeys(_COUNT_KEYS, 0),
        "overall": dict.fromkeys(_COUNT_KEYS, 0),
        "roots_done": 0, "roots_pending": 0,   # (done = read, found done or deferred)
        "last": None,
    }


# The running (or last) run's status (``get_status``).  ``current`` counts the
# root being read (the last one read once it ended; ``fixed`` grows at each
# ``_CHUNK`` write, so it trails ``checked`` by up to a batch), ``overall``
# every root of the run (``total`` = the candidates of the roots read so far
# plus the estimate of those to come).
# ``last`` = the summary of the last finished run that read or deferred a
# root: ``{finished_at, seconds, total, checked, fixed, skipped, unreachable,
# roots, deferred_roots, label}`` (``label`` when it covered one root).
_status: dict = _idle_status()


def get_status() -> dict:
    """A copy of the backfill status (see ``_status``)."""
    out = dict(_status)
    out["current"] = dict(_status["current"])
    out["overall"] = dict(_status["overall"])
    out["last"] = dict(_status["last"]) if _status["last"] else None
    return out


def _progress_line() -> str:
    """The progress log line: the root being read, else the whole run."""
    if not _status.get("root"):
        tot = _status["overall"]
        return (f"Length backfill: {tot['checked']:,} of {tot['total']:,} checked so far"
                + (", waiting for the running scan" if _status["waiting_for_scan"] else ""))
    cur = _status["current"]
    return (f"Length backfill of {_status['root']}: {cur['checked']:,} of "
            f"{cur['total']:,} checked, {cur['fixed']:,} fixed, "
            f"{cur['skipped']:,} skipped, {cur['unreachable']:,} unreachable"
            + (" (waiting for the running scan)" if _status["waiting_for_scan"] else ""))


class _Progress:
    """Counts a run's progress into ``_status`` (``current`` for the root
    being read, and ``overall``) and logs ``_progress_line`` every
    ``_LOG_EVERY_N`` files checked of a root (``start_root`` restarts the
    count); ``heartbeat`` (one task per run) logs it when nothing was logged
    for ``_LOG_EVERY_S`` seconds — also while the run waits for a scan."""

    def __init__(self) -> None:
        self.logged_at = time.monotonic()
        self.logged_checked = 0

    def start_root(self) -> None:
        self.logged_at = time.monotonic()
        self.logged_checked = 0

    def add(self, key: str, n: int = 1) -> None:
        if n:
            _status["current"][key] += n
            _status["overall"][key] += n
            if key == "checked":
                checked = _status["current"]["checked"]
                if checked - self.logged_checked >= _LOG_EVERY_N:
                    self.log()

    def log(self) -> None:
        self.logged_at = time.monotonic()
        self.logged_checked = _status["current"]["checked"]
        log.info("%s", _progress_line())

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(max(0.05, self.logged_at + _LOG_EVERY_S - time.monotonic()))
            if time.monotonic() - self.logged_at >= _LOG_EVERY_S:
                self.log()


def _unchanged(cur: "dict | None", snap: dict) -> bool:
    """Is stored row ``cur`` still the one ``snap`` was taken of (same file,
    same length, length not hand-edited)?"""
    return (cur is not None
            and all(cur.get(k) == snap.get(k) for k in ("path", "duration", "file_size", "mtime"))
            and "duration" not in (cur.get("user_edited") or ()))


async def _backfill_root(store, source, candidates: list[tuple[str, str]],
                         progress: "_Progress | None" = None,
                         concurrency: int | None = None) -> dict:
    """Re-measure ``candidates`` (``(track id, kind)``) of one root on
    ``source``, ``concurrency`` (default ``_concurrency(source)``) at once;
    write the lengths that are clearly longer (re-checked against the row
    right before each batch write).  Counts each candidate checked and each
    fixed / skipped / unreachable one into ``progress`` as it goes.  Returns
    the root's counts, with the ids that could not be read under
    ``"unreachable_ids"``."""
    from soniqboom.core.filesource import is_permanent_fetch_error, parse_remote_path
    loop = asyncio.get_running_loop()
    conc = concurrency or _concurrency(source)
    sem = asyncio.Semaphore(conc)
    counts: dict = {"fixed": 0, "skipped": 0, "unreachable": 0}
    unreachable: set[str] = set()
    fails_in_a_row = [0]
    prog = progress or _Progress()
    max_fails = _max_fails_in_a_row(conc)

    def skip() -> None:
        counts["skipped"] += 1
        prog.add("skipped")

    def miss(tid: str) -> None:
        unreachable.add(tid)
        prog.add("unreachable")

    async def one(tid: str, kind: str) -> "tuple[str, dict, float] | None":
        try:
            return await _one(tid, kind)
        finally:
            prog.add("checked")

    async def _one(tid: str, kind: str) -> "tuple[str, dict, float] | None":
        t = store._tracks.get(tid)
        if t is None or backfill_kind(t) != kind:
            skip()                                      # changed meanwhile
            return None
        try:
            sub = parse_remote_path(t["path"])[1]
        except ValueError:
            skip()
            return None
        snap = dict(t)
        async with sem:
            if fails_in_a_row[0] >= max_fails:
                miss(tid)                               # (the share went away)
                return None
            try:
                secs = await loop.run_in_executor(None, _measure, source, sub, kind, snap)
            except Exception as exc:                    # noqa: BLE001
                if is_permanent_fetch_error(exc):
                    skip()                              # (gone / refused: every time)
                    _settled.add(tid)
                    return None
                fails_in_a_row[0] += 1
                miss(tid)
                log.debug("Length backfill: reading %s failed: %s",
                          display_root(snap["path"]), exc)
                return None
        fails_in_a_row[0] = 0
        _settled.add(tid)
        old = float(snap["duration"])
        if (secs is None or secs > _MAX_LENGTH_S
                or secs < old + _MIN_GAIN_S or secs < old * _MIN_GAIN_RATIO):
            skip()                                      # (already right / can't tell)
            return None
        return tid, snap, secs

    for i in range(0, len(candidates), _CHUNK):
        await _wait_for_scans()
        got = await asyncio.gather(*(one(tid, kind) for tid, kind in candidates[i:i + _CHUNK]))
        # (no await between this check and the write)
        items = []
        for g in got:
            if g is None:
                continue
            tid, snap, secs = g
            if _unchanged(store._tracks.get(tid), snap):
                items.append((tid, {"duration": round(secs, 3)}))
            else:
                skip()                                  # changed while it was read
        if items:
            n = store.update_track_fields_batch(items) or 0
            counts["fixed"] += n
            prog.add("fixed", n)
    counts["unreachable"] = len(unreachable)
    counts["unreachable_ids"] = unreachable
    return counts


_CANDIDATE_SLICE = 5_000      # tracks looked at between yields (~6 ms)


async def _candidates(store, root_hash: str) -> list[tuple[str, str]]:
    """``(track id, kind)`` of root ``root_hash``'s tracks that are
    candidates (``backfill_kind``) and not read yet in this process, by
    track id — looked at ``_CANDIDATE_SLICE`` at a time, yielding to the loop
    between slices (a share of 300K tracks took 375 ms in one go), and only
    the candidates sorted."""
    get = store._tracks.get
    ids = list(store.get_track_ids_for_scan_root(root_hash))
    out = []
    for i in range(0, len(ids), _CANDIDATE_SLICE):
        for tid in ids[i:i + _CANDIDATE_SLICE]:
            t = get(tid)
            kind = backfill_kind(t) if t is not None and tid not in _settled else None
            if kind:
                out.append((tid, kind))
        await asyncio.sleep(0)
    out.sort()
    return out


async def run_backfill(store=None) -> dict:
    """Correct the stored lengths of every remote scan root not done yet
    (``BACKFILL_ROOTS_KEY``), reading the candidates not read yet in this
    process (``_settled``).  A root that is marked offline, has no connected
    source or no real range reads is deferred unread.  A root with a
    candidate that could not be read (a transient error — a refused /
    missing file counts as skipped) stays pending until that file was tried
    in ``_MAX_UNREACHABLE_RUNS`` runs (``_count_unreachable``); a root a scan
    flagged meanwhile (``note_pending``) stays pending too; every other root
    is marked done.  Waits while a scan runs.  Keeps ``_status`` up to date
    (``last`` replaced at the end when a root was read or deferred) and logs
    each root's start, its progress (``_Progress``) and its result.  Returns
    ``{"fixed", "skipped", "unreachable", "deferred_roots", "seconds"}``."""
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    from soniqboom.core.filesource import get_source, is_remote_path
    from soniqboom.core.tag_window import supports_ranges
    t0 = time.perf_counter()
    total = {"fixed": 0, "skipped": 0, "unreachable": 0, "deferred_roots": 0}
    done = set(_done_roots(store))
    roots = [d for d in store.list_scan_dirs()
             if is_remote_path(d.get("path") or "") and not d.get("catalogue")
             and d.get("path_hash") not in done]
    # (an estimate for ``overall.total``: each root's count is replaced by
    # its real one when it is read, and dropped when it is deferred)
    last = _status["last"]
    _status.update(_idle_status(), running=True, started_at=time.time(),
                   roots_pending=len(roots), last=last)
    estimate: dict[str, int] = {}
    read_roots: list[str] = []
    prog = _Progress()
    beat = asyncio.ensure_future(prog.heartbeat())
    try:
        for d in roots:
            estimate[d["path_hash"]] = n = len(await _candidates(store, d["path_hash"]))
            _status["overall"]["total"] += n
        for d in roots:
            await _wait_for_scans()
            root, root_hash = d["path"], d["path_hash"]
            shown = display_root(root)
            _status["roots_pending"] -= 1
            _status["overall"]["total"] -= estimate.get(root_hash, 0)
            if backfill_done(root_hash, store):
                _status["roots_done"] += 1
                continue
            candidates = await _candidates(store, root_hash)
            if not candidates:
                if root_hash not in _scan_pending:
                    mark_done(root_hash, store)
                _status["roots_done"] += 1
                continue
            source = get_source(root) if d.get("status") != "unavailable" else None
            if source is None or not supports_ranges(source):
                total["deferred_roots"] += 1
                _status["roots_done"] += 1
                read_roots.append(_label(root))
                log.info("Length backfill: %s is not connected — its %d track(s) are "
                         "checked once it is", shown, len(candidates))
                continue
            conc = _concurrency(source)
            _status["overall"]["total"] += len(candidates)
            _status.update(root=shown, label=_label(root),
                           current=dict.fromkeys(_COUNT_KEYS, 0))
            _status["current"]["total"] = len(candidates)
            read_roots.append(_status["label"])
            log.info("Length backfill: checking %d track(s) on %s (%d at once)",
                     len(candidates), shown, conc)
            prog.start_root()
            counts = await _backfill_root(store, source, candidates, prog, conc)
            pending = _count_unreachable(store, counts.pop("unreachable_ids"))
            for k, v in counts.items():
                total[k] += v
            if pending or root_hash in _scan_pending:
                total["deferred_roots"] += 1
            else:
                mark_done(root_hash, store)
            _status.update(root=None, label=None, roots_done=_status["roots_done"] + 1)
            log.info("Length backfill of %s: %d of %d track(s) fixed, %d skipped, "
                     "%d unreachable%s", shown, counts["fixed"], len(candidates),
                     counts["skipped"], counts["unreachable"],
                     " — retried after its next scan" if pending else
                     " — given up" if counts["unreachable"] else "")
    finally:
        beat.cancel()
        total["seconds"] = round(time.perf_counter() - t0, 2)
        if read_roots:
            _status["last"] = {
                "finished_at": time.time(), "seconds": total["seconds"],
                **_status["overall"],
                "roots": len(read_roots), "deferred_roots": total["deferred_roots"],
                "label": read_roots[0] if len(read_roots) == 1 else None,
            }
        _status.update(running=False, waiting_for_scan=False, root=None, label=None)
        await asyncio.gather(beat, return_exceptions=True)
    return total


_task: "asyncio.Task | None" = None
_again = False


def schedule_backfill() -> None:
    """Run ``run_backfill`` in the background — once more after the running
    one when called while it runs.  No-op without a running loop."""
    global _task, _again
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _task is not None and not _task.done() and _task.get_loop() is loop:
        _again = True
        return

    async def _run() -> None:
        global _again
        while True:
            _again = False
            try:
                await run_backfill()
            except Exception:                           # noqa: BLE001
                log.exception("Length backfill failed")
            if not _again:
                return
    _task = loop.create_task(_run(), name="partial-length-backfill")
