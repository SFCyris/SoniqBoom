# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""UADE song-database enrichment (audacious-uade-tools).

audacious-uade-tools (https://github.com/mvtiaine/audacious-uade-tools, by
Matti Tiainen) publishes the song database of the audacious-uade player as
plain TSV files keyed by the first 48 bits of a module's MD5:

    tsv/pretty/md5/metadata.tsv     hash, authors, publishers, album, year
    tsv/pretty/md5/songlengths.tsv  hash, first subsong, length+end per subsong

(~380K modules with metadata, ~477K with lengths, compiled from ~400 scene
sources).  The DATABASE is licensed CC BY-NC-SA 4.0, so it is never shipped
with SoniqBoom: an admin downloads it (Admin → Metadata), like the Modland and
Demozoo indexes, and the UI names its source and licence.

Join key: the track's ``file_md5`` (cached at scan time for chip / tracker
formats), first 12 hex digits.  Exact matches only.  What a match fills — only
EMPTY fields, never a field the user edited:

  * ``artist``   ← the authors ("A & B"), also over a ripper placeholder ("<?>");
  * ``label``    ← the publishers ("A, B");
  * ``album``    ← the album (game / production), also over a GUESSED album
                   (``album_source`` "folder" / "modland-filename"), stamped
                   ``album_source="songdb"``.  A production name the database
                   gives to more than one group ("Megademo") carries the group
                   ("Megademo (The Silents)") — the library groups albums by
                   name; with no group to tell them apart it is not used;
  * ``year``     ← the year, stamped ``year_source="songdb"``;
  * ``duration`` ← the length of a one-tune module played through uade (a
                   render-only format, no length until first played) when
                   the database's length ended where our renderer ends it
                   (the player's own song end); the first play re-measures it.

Artist and label have no provenance field of their own, so the names of the
fields this pass filled are kept in ``songdb_fields``.  With the provenance a
rescan keeps the fills while the file is unchanged (``store._carry_enrichment``),
a refreshed index updates or withdraws them, and Reset clears exactly them
(song lengths stay: they describe the file, and a play re-measures them).

The patch of each track is computed at WRITE time from the track as it is then
(``folder_album._guard_item`` accepts a callable), so an edit or rescan landing
while the join ran is never overwritten.

Storage: ``<data_dir>/scene/songdb.sqlite`` (~35 MB); the pass runs from the
Admin Apply button and, while auto-apply is on, after every scan
(``scanner._ensure_scene_autoapply_runner``: Modland → this → Demozoo).
"""
from __future__ import annotations

import functools
import logging
import re
import sqlite3
import time
import unicodedata
from pathlib import Path

from soniqboom.core import folder_album as fa

log = logging.getLogger(__name__)

SOURCE = "songdb"
BASE_URL = ("https://raw.githubusercontent.com/mvtiaine/audacious-uade-tools/"
            "master/tsv/pretty/md5/")
FILES = ("metadata.tsv", "songlengths.tsv")
AUTO_APPLY_PREF = "songdb_auto_apply"

# A download is accepted only when it looks like the real database: the
# repository may move or reformat its files at any time, and an index built
# from a truncated or changed file must not replace a good one (nor withdraw
# the enrichment of every track).  The real files have ~380K / ~477K rows, and
# every row must have the documented layout (``_meta_row_ok``,
# ``_lengths_row_ok``) — a shifted or re-typed column fails the check.
_MIN_META_ROWS = 100_000
_MIN_LENGTH_ROWS = 100_000
_MAX_BAD_LINE_RATIO = 0.01
_MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
_DOWNLOAD_TIMEOUT_S = 300
_LOOKUP_CHUNK = 900              # SQLite's host-parameter limit is 999 on older builds

_KEY_RE = re.compile(r"^[0-9a-f]{12}$")
# Authors the database uses for "nobody known".
_NO_AUTHOR = frozenset({"unknown", "unknown artist", "anonymous"})
# Song-end kinds (audacious-uade ``songend``) whose length is the length our
# own uade123 render has: only "p" (the player ended the song) — measured on
# the local test modules, 15 of 15 within 0.02 s.  A loop end ("l") is the
# loop point while our render runs on to uade's subsong timeout (+218..+490 s),
# a silence end differs by the silence tail (+0.9 / +3.7 s), and errors,
# timeouts and "no sound" are no length at all.  (A stored length is promised
# to a listener up front — ``stream._uade_expected_seconds`` — so a wrong one
# would cut the first play short.)
_GOOD_ENDS = frozenset({"p"})
# Our uade123 render stops every tune at uade's default subsong timeout
# (~512 s; ``_render_uade`` passes no ``-w``), so a longer database length is
# never the length we play.
_MAX_LENGTH_MS = 511_000

_status: dict = {
    "refreshing": False,
    "applying": False,
    "error": None,
    "built_at": None,
    "meta_rows": 0,
    "length_rows": 0,
    "last_apply": None,     # {"matched", "updated", "albums", "at"}
    "last_reset": None,     # {"cleared", "at"}
}


# ── Paths / status ───────────────────────────────────────────────────────────

def _db_path() -> Path:
    from soniqboom.config import get_data_dir
    d = get_data_dir() / "scene"
    d.mkdir(parents=True, exist_ok=True)
    return d / "songdb.sqlite"


def has_index() -> bool:
    """True when a built (non-empty) index file exists (no sqlite open)."""
    try:
        return _db_path().stat().st_size > 0
    except OSError:
        return False


def _connect_ro(db: Path) -> sqlite3.Connection:
    """Read-only connection — never creates an empty index file."""
    return sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)


def auto_apply_enabled() -> bool:
    """Whether a completed scan re-runs the apply.  Default on; Reset turns it
    off (so a reset holds across scans), Apply turns it back on."""
    from soniqboom.config import load_prefs
    try:
        return bool(load_prefs().get(AUTO_APPLY_PREF, True))
    except Exception:                                   # noqa: BLE001
        return True


def set_auto_apply(enabled: bool) -> None:
    from soniqboom.config import load_prefs, save_prefs
    try:
        prefs = load_prefs()
        prefs[AUTO_APPLY_PREF] = bool(enabled)
        save_prefs(prefs)
    except Exception:                                   # noqa: BLE001
        log.warning("could not persist %s", AUTO_APPLY_PREF, exc_info=True)


def _index_info() -> dict:
    """``built_at`` / row counts recorded in the index file itself (they
    survive a restart; ``_status`` is per process)."""
    if not has_index():
        return {}
    try:
        con = _connect_ro(_db_path())
        try:
            return dict(con.execute("SELECT k, v FROM info").fetchall())
        finally:
            con.close()
    except Exception:                                   # noqa: BLE001
        return {}


def status() -> dict:
    out = dict(_status)
    exists = has_index()
    if exists and not out["meta_rows"]:
        info = _index_info()
        try:
            out["meta_rows"] = int(info.get("meta_rows") or 0)
            out["length_rows"] = int(info.get("length_rows") or 0)
            out["built_at"] = float(info.get("built_at") or 0) or None
        except (TypeError, ValueError):
            pass
    out["exists"] = exists
    out["auto_apply"] = auto_apply_enabled()
    try:
        out["size"] = _db_path().stat().st_size if exists else 0
    except OSError:
        out["size"] = 0
    return out


# ── Index build ──────────────────────────────────────────────────────────────

def _download(name: str, dest: Path) -> None:
    """Stream ``BASE_URL + name`` to ``dest`` (3 attempts, size-capped)."""
    import httpx
    url = BASE_URL + name
    last: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=_DOWNLOAD_TIMEOUT_S, follow_redirects=True) as client:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    got = 0
                    with open(dest, "wb") as fh:
                        for chunk in resp.iter_bytes(1 << 16):
                            got += len(chunk)
                            if got > _MAX_DOWNLOAD_BYTES:
                                raise RuntimeError(f"{name} is larger than expected")
                            fh.write(chunk)
            return
        except Exception as exc:                        # noqa: BLE001
            last = exc
            dest.unlink(missing_ok=True)
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"download of {name} failed: {last}")


def _split(v: str) -> list[str]:
    """``~``-separated names → unique, trimmed, in order."""
    out: list[str] = []
    for p in (v or "").split("~"):
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


def _year(v: str) -> int | None:
    v = (v or "").strip()
    if len(v) == 4 and v.isdigit() and 1950 <= int(v) <= 2100:
        return int(v)
    return None


def _meta_row_ok(cols: list[str]) -> bool:
    """``hash, authors, publishers, album, year`` — at most five columns and a
    year cell that is empty or a year (a shifted or reformatted file fails)."""
    return len(cols) <= 5 and (len(cols) < 5 or not cols[4].strip()
                               or _year(cols[4]) is not None)


_SUBSONGS_RE = re.compile(r"^\d+,[a-z+]+(?:,!)?(?: \d+,[a-z+]+(?:,!)?)*$")


def _lengths_row_ok(cols: list[str]) -> bool:
    """``hash, first subsong, "len,end[,!] …"`` — exactly three columns."""
    return (len(cols) == 3 and cols[1].strip().isdigit()
            and _SUBSONGS_RE.match(cols[2].strip()) is not None)


def _iter_tsv(path: Path, min_cols: int, tally: list[int], valid=None):
    """Yield the rows of a pretty TSV whose ``hash`` is valid (and that pass
    ``valid``); count them in ``tally[0]`` and the malformed lines in
    ``tally[1]``.  Streams the file."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line:
                continue
            cols = line.split("\t")
            if (len(cols) < min_cols or not _KEY_RE.match(cols[0])
                    or (valid is not None and not valid(cols))):
                tally[1] += 1
                continue
            tally[0] += 1
            yield cols


def _check(name: str, tally: list[int], minimum: int) -> None:
    rows, bad = tally
    if rows < minimum:
        raise RuntimeError(f"{name} has only {rows:,} entries — not the song database "
                           f"(the upstream files may have moved or changed format)")
    if bad > _MAX_BAD_LINE_RATIO * (rows + bad):
        raise RuntimeError(f"{name}: {bad:,} of {rows + bad:,} lines are not in the "
                           f"expected format")


# Words that don't tell one publisher from another ("Thalion" / "Thalion
# Software", "ORIGIN Systems", "Virgin Interactive Entertainment").
_PUBLISHER_NOISE = frozenset({
    "the", "software", "soft", "systems", "system", "entertainment", "interactive",
    "productions", "production", "prods", "prod", "games", "studios", "studio",
    "ltd", "inc", "co", "company", "corp", "corporation", "gmbh", "limited",
    "llc", "plc", "international", "graphics", "design", "designs", "soundsystem",
    "dj", "team", "crew",
})
_PUBLISHER_PARTS_RE = re.compile(r"\s*(?:&|\+|/|\band\b)\s*", re.IGNORECASE)


def _publisher_key(name: str) -> str:
    """A publisher name reduced to what identifies it: case, accents,
    punctuation, spacing and the ``_PUBLISHER_NOISE`` words ignored
    ("Team 17" = "Team17", "Titus Software" = "Titus") — unless nothing
    but digits would be left."""
    folded = unicodedata.normalize("NFKD", name.casefold())
    words = re.findall(r"[^\W_]+", "".join(c for c in folded if not unicodedata.combining(c)))
    kept = [w for w in words if w not in _PUBLISHER_NOISE]
    if not kept:
        kept = words                # all filler ("Interactive Design"): the whole name
    elif not any(c.isalpha() for w in kept for c in w):
        # "Team 17", "System 3 Software": a filler word is part of the name —
        # drop only a leading "the" and trailing filler words
        kept = list(words)
        while len(kept) > 1 and kept[0] == "the":
            kept.pop(0)
        while len(kept) > 1 and kept[-1] in _PUBLISHER_NOISE:
            kept.pop()
    return "".join(kept)


def _publisher_keys(name: str) -> list[str]:
    """The key of a publisher and, for a joint credit ("Mahoney & Kaktus"),
    those of its parts — a production credited to either part is the same."""
    keys = [_publisher_key(name)]
    parts = [x for x in _PUBLISHER_PARTS_RE.split(name) if x.strip()]
    if len(parts) > 1:
        keys += [_publisher_key(x) for x in parts]
    return [k for k in keys if k]


def _album_qualifiers(rows) -> dict[str, dict[str, str]]:
    """Album names (lower-cased) that name more than one production, each
    mapped to ``{publisher (lower-cased): the publisher its production is
    shown with}``.  ``rows`` is an iterable of metadata rows (streamed).

    The rows of one album name are grouped by shared publishers, compared by
    ``_publisher_keys`` (rows naming "Sensible Software" and "Sensible
    Software~Virgin Interactive" are one production, as are rows naming
    "Team 17" and "Team17"); a name whose rows fall into two or more groups with
    no publisher in common ("Megademo" by The Silents, by Anarchy, …) names
    several productions.  Each group is shown with its most frequent
    publisher spelling (ties: alphabetical).  Rows without a publisher can't
    be placed and get no album.  Memory is per distinct (album, publisher)
    pair, not per row."""
    parent: dict[tuple[str, str], tuple[str, str]] = {}
    counts: dict[tuple[str, str], int] = {}        # (album, publisher lower) → rows
    names: dict[tuple[str, str], str] = {}         # spelling as first seen for this album
    n_rows: dict[str, int] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for cols in rows:
        album = cols[3].strip().lower() if len(cols) > 3 else ""
        pubs = _split(cols[2]) if len(cols) > 2 else []
        if not (album and pubs):
            continue
        n_rows[album] = n_rows.get(album, 0) + 1
        nodes = [(album, k) for p in pubs for k in _publisher_keys(p)]
        if not nodes:
            continue
        root = find(nodes[0])
        for n in nodes[1:]:
            r = find(n)
            if r != root:
                parent[r] = root
        for p in pubs:
            k = (album, p.lower())
            counts[k] = counts.get(k, 0) + 1
            names.setdefault(k, p)
    comps: dict[str, dict[tuple[str, str], list[str]]] = {}
    for (album, low) in counts:
        if n_rows[album] < 2:
            continue
        keys = _publisher_keys(names[(album, low)])
        if keys:
            comps.setdefault(album, {}).setdefault(find((album, keys[0])), []).append(low)
    out: dict[str, dict[str, str]] = {}
    for album, groups in comps.items():
        if len(groups) < 2:
            continue
        m: dict[str, str] = {}
        for lows in groups.values():
            top = min(lows, key=lambda p: (-counts[(album, p)], p))
            for p in lows:
                m[p] = names[(album, top)]
        out[album] = m
    return out


def build_index(meta_tsv: Path, lengths_tsv: Path, dest: Path) -> tuple[int, int]:
    """Build the sqlite index at ``dest`` from the two pretty TSVs; returns
    ``(meta_rows, length_rows)``.  Raises when the input doesn't look like the
    song database (the caller discards ``dest`` then).  The display values are
    computed here once (joined names, a qualified album name, a validated
    year).  Both files are streamed (two passes over the metadata)."""
    tally = [0, 0]
    qualify = _album_qualifiers(_iter_tsv(meta_tsv, 2, tally, _meta_row_ok))
    _check("metadata.tsv", tally, _MIN_META_ROWS)

    dest.unlink(missing_ok=True)
    con = sqlite3.connect(dest)
    try:
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, artist TEXT, "
                    "label TEXT, album TEXT, year INTEGER) WITHOUT ROWID")
        con.execute("CREATE TABLE lengths (key TEXT PRIMARY KEY, minsub INTEGER, "
                    "subsongs TEXT) WITHOUT ROWID")
        con.execute("CREATE TABLE info (k TEXT PRIMARY KEY, v TEXT)")
        batch: list[tuple] = []
        for cols in _iter_tsv(meta_tsv, 2, [0, 0], _meta_row_ok):
            cols = cols + [""] * (5 - len(cols))
            authors = [a for a in _split(cols[1]) if a.lower() not in _NO_AUTHOR]
            publishers = _split(cols[2])
            label = ", ".join(publishers)
            album = cols[3].strip()
            q = qualify.get(album.lower()) if album else None
            if q is not None:
                # a production name several unrelated groups used
                top = q.get(publishers[0].lower()) if publishers else None
                album = f"{album} ({top})" if top else ""
            batch.append((cols[0], " & ".join(authors) or None, label or None,
                          album or None, _year(cols[4])))
            if len(batch) >= 20000:
                con.executemany("INSERT OR REPLACE INTO meta VALUES (?,?,?,?,?)", batch)
                batch.clear()
        con.executemany("INSERT OR REPLACE INTO meta VALUES (?,?,?,?,?)", batch)
        batch.clear()
        tally = [0, 0]
        for cols in _iter_tsv(lengths_tsv, 3, tally, _lengths_row_ok):
            batch.append((cols[0], int(cols[1]) if cols[1].strip().isdigit() else 0,
                          cols[2].strip()))
            if len(batch) >= 20000:
                con.executemany("INSERT OR REPLACE INTO lengths VALUES (?,?,?)", batch)
                batch.clear()
        con.executemany("INSERT OR REPLACE INTO lengths VALUES (?,?,?)", batch)
        _check("songlengths.tsv", tally, _MIN_LENGTH_ROWS)
        n_meta = con.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
        n_len = con.execute("SELECT COUNT(*) FROM lengths").fetchone()[0]
        con.executemany("INSERT INTO info VALUES (?,?)", [
            ("built_at", str(time.time())), ("meta_rows", str(n_meta)),
            ("length_rows", str(n_len)), ("source", BASE_URL)])
        con.commit()
    finally:
        con.close()
    return n_meta, n_len


def refresh_index() -> dict:
    """Download the two TSVs and (re)build the index.  Blocking — callers run
    it in an executor.  A failed or implausible download keeps the current
    index.  Returns the updated status."""
    if _status["refreshing"]:
        return {**status(), "error": "refresh already running"}
    _status.update(refreshing=True, error=None)
    db = _db_path()
    tmp = {n: db.with_name(f"songdb-{n}.part") for n in FILES}
    building = db.with_suffix(".building")
    try:
        for name, path in tmp.items():
            log.info("Song database: downloading %s", BASE_URL + name)
            _download(name, path)
        n_meta, n_len = build_index(tmp["metadata.tsv"], tmp["songlengths.tsv"], building)
        building.replace(db)
        _status.update(meta_rows=n_meta, length_rows=n_len, built_at=time.time())
        log.info("Song database index built: %d metadata, %d song-length entries",
                 n_meta, n_len)
    except Exception as exc:                            # noqa: BLE001
        _status["error"] = f"index refresh failed: {exc}"
        log.warning("Song database refresh failed: %s", exc)
        building.unlink(missing_ok=True)
    finally:
        for path in tmp.values():
            path.unlink(missing_ok=True)
        _status["refreshing"] = False
    return status()


# ── Join ─────────────────────────────────────────────────────────────────────

def _key(t: dict) -> str | None:
    md5 = t.get("file_md5")
    if isinstance(md5, str) and len(md5) >= 12:
        return md5[:12].lower()
    return None


def _has_ours(t: dict) -> bool:
    return bool(t.get("songdb_fields") or t.get("album_source") == SOURCE
                or t.get("year_source") == SOURCE or t.get("game_by_songdb"))


def _uade_served(t: dict) -> bool:
    """Played through uade — extracted by it (genre Amiga + Module) or an
    ``.ahx`` (tracker-extracted, uade-served): its stored length is promised
    to listeners up front and the first play re-measures it
    (``stream._backfill_rendered_duration`` with ``authoritative``).  Other
    renderers keep a stored length."""
    genre = t.get("genre") or []
    if "Amiga" in genre and "Module" in genre:
        return True
    return (t.get("path") or "").lower().endswith(".ahx")


def default_tune_seconds(lengths: "tuple[int, str] | None", t: dict) -> float | None:
    """Length in seconds of track ``t``'s tune from the database's
    ``(first subsong, "len,end len,end …")`` row — or None.

    Only a one-tune file: SoniqBoom plays uade's own default tune ("cur"),
    which the database doesn't record, while its list starts at the first
    subsong ("min"); with one tune the two are the same.  The track must be
    one tune too (``subsongs`` unset or 1, no other default tune)."""
    if not lengths or not lengths[1]:
        return None
    entries = lengths[1].split(" ")
    try:
        ours = int(t.get("subsongs") or 1)
        start = int(t.get("start_subsong") or 0)
    except (TypeError, ValueError):
        return None
    if len(entries) != 1 or ours != 1 or start:
        return None
    ms, _, rest = entries[0].partition(",")
    end = rest.split(",", 1)[0]
    if end not in _GOOD_ENDS or not ms.isdigit():
        return None
    ms_i = int(ms)
    if not (0 < ms_i <= _MAX_LENGTH_MS):
        return None
    return round(ms_i / 1000.0, 2)


def patch_for(t: dict, *, key: str, meta: tuple | None,
              lengths: "tuple[int, str] | None", withdraw_ok: bool) -> dict | None:
    """The patch the database implies for track ``t`` AS IT IS NOW (called at
    write time), or None.

    ``meta`` is the index row ``(artist, label, album, year)`` for ``key`` and
    ``lengths`` its ``(first subsong, length list)`` (None: not in that table).  A field this
    pass filled earlier follows the index (updated when the value changed,
    withdrawn when the index no longer has one — a missing row only when
    ``withdraw_ok``, i.e. the index is complete); anything else is only ever
    filled when empty.  A field the user edited is never touched."""
    if _key(t) != key:
        return None                             # a different file now
    if meta is None and not withdraw_ok:
        meta_known = False
        artist = label = album = year = None
    else:
        meta_known = True
        artist, label, album, year = meta or (None, None, None, None)
    upd: dict = {}
    ours = [f for f in (t.get("songdb_fields") or []) if isinstance(f, str)]
    new_ours = list(ours)

    for field, want in (("artist", artist), ("label", label)):
        if fa.field_locked(t, field):
            if field in new_ours:
                new_ours.remove(field)          # the user's value now
            continue
        cur = (t.get(field) or "").strip()
        if field in ours:
            if not meta_known:
                continue
            if want and cur != want:
                upd[field] = want
            elif not want:
                upd[field] = ""
                new_ours.remove(field)
        elif want:
            empty = (cur.lower() in fa._ARTIST_PLACEHOLDERS if field == "artist"
                     else not cur)
            if empty:
                upd[field] = want
                new_ours.append(field)

    if not fa.album_edit_locked(t):
        cur = (t.get("album") or "").strip()
        src = t.get("album_source")
        if src == SOURCE:
            if meta_known and album and cur != album:
                upd["album"] = album
            elif meta_known and not album:
                upd.update(fa.header_album_back(t))
        elif album and (not cur or src in (fa.SOURCE_FOLDER, fa.SOURCE_MODLAND_FILENAME)):
            upd.update(album=album, album_source=SOURCE)
    # The song database's game name — recorded whether or not it is the album
    # (a header game or a typed album keeps that), so ``game:`` finds it.
    if meta_known and (t.get("game_by_songdb") or None) != (album or None):
        upd["game_by_songdb"] = album or None

    ysrc = t.get("year_source")
    if ysrc == SOURCE:
        if meta_known and year and t.get("year") != year:
            upd["year"] = year
        elif meta_known and not year:
            upd.update(year=t.get("year_file"), year_source=None, year_file=None)
    elif year and not ysrc and t.get("year") is None:
        upd.update(year=year, year_source=SOURCE)

    try:
        dur = float(t.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0 and _uade_served(t):
        secs = default_tune_seconds(lengths, t)
        if secs:
            upd["duration"] = secs

    if new_ours != ours:
        upd["songdb_fields"] = sorted(new_ours) or None
    return upd or None


def reset_patch(t: dict) -> dict | None:
    """The patch that withdraws what this pass filled on ``t`` (at write time):
    its album, year, artist and label — never a field the user edited."""
    upd: dict = {}
    if t.get("album_source") == SOURCE and not fa.album_edit_locked(t):
        upd.update(fa.header_album_back(t))
    if t.get("year_source") == SOURCE:
        upd.update(year=t.get("year_file"), year_source=None, year_file=None)
    ours = t.get("songdb_fields") or []
    for field in ("artist", "label"):
        if field in ours and not fa.field_locked(t, field):
            upd[field] = ""
    if ours:
        upd["songdb_fields"] = None
    if t.get("game_by_songdb"):
        upd["game_by_songdb"] = None
    return upd or None


def collect(tracks: list[dict], matched_ids: set | None = None
            ) -> tuple[int, list[tuple[str, object, None]]]:
    """Join the snapshot ``tracks`` against the index (blocking — run in an
    executor).  Returns ``(matched, items)``: one write-time patch callable
    per track that has a database row or carries this pass's fills.
    ``matched_ids`` (optional) collects the ids of the matched tracks."""
    if not has_index():
        raise RuntimeError("no song database index — download it first")
    keyed: list[tuple[str, str, bool]] = []
    for t in tracks:
        k = _key(t)
        if k is not None:
            keyed.append((t["id"], k, _has_ours(t)))
        elif _has_ours(t):
            keyed.append((t["id"], "", True))
    wanted = list({k for _tid, k, _o in keyed if k})
    meta: dict[str, tuple] = {}
    lens: dict[str, tuple[int, str]] = {}
    con = _connect_ro(_db_path())
    try:
        for i in range(0, len(wanted), _LOOKUP_CHUNK):
            chunk = wanted[i:i + _LOOKUP_CHUNK]
            marks = ",".join("?" * len(chunk))
            for row in con.execute(
                    f"SELECT key, artist, label, album, year FROM meta WHERE key IN ({marks})",
                    chunk):
                meta[row[0]] = row[1:]
            for row in con.execute(
                    f"SELECT key, minsub, subsongs FROM lengths WHERE key IN ({marks})",
                    chunk):
                lens[row[0]] = (row[1], row[2])
        n_meta = con.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
    finally:
        con.close()
    withdraw_ok = n_meta >= _MIN_META_ROWS
    matched = 0
    items: list[tuple[str, object, None]] = []
    for tid, k, has_ours in keyed:
        m = meta.get(k)
        s = lens.get(k)
        if m is not None or s is not None:
            matched += 1
            if matched_ids is not None:
                matched_ids.add(tid)
        elif not (has_ours and withdraw_ok):
            continue
        if not k:
            # provenance but no md5 any more: nothing to join — leave it
            continue
        items.append((tid, functools.partial(patch_for, key=k, meta=m, lengths=s,
                                              withdraw_ok=withdraw_ok), None))
    return matched, items


# ── Apply / reset ────────────────────────────────────────────────────────────

# The last apply's position in the store's enrichment change log and the index
# signature (``enrich_delta.record``): the next post-scan apply joins only the
# tracks written since, all of them when the index changed (None: all).
_last_auto_sig: tuple | None = None
# The ids of the tracks the index matches, as of the last apply (a delta apply
# updates its own tracks), so the status counts the whole library.
_matched_ids: set[str] = set()


def _index_sig() -> tuple | None:
    try:
        st = _db_path().stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


async def apply_to_library(*, force: bool = False) -> dict:
    """Join in an executor, write on the loop (each patch computed from the
    track as it is at write time).  Without ``force`` (the post-scan runner)
    only the tracks written since the last apply are joined (``enrich_delta``;
    the join is per track) and the join is skipped when there are none; every
    track is joined when the index changed.  Frees albums → the folder-album
    pass (if enabled) runs."""
    import asyncio
    global _last_auto_sig, _matched_ids
    if _status["applying"]:
        return {**status(), "error": "apply already running"}
    from soniqboom.core import enrich_delta
    from soniqboom.core.store import get_store
    store = get_store()
    inputs = (_index_sig(),)
    changed = enrich_delta.changes(store, _last_auto_sig, inputs, force=force)
    if changed is not None and not changed:
        return {**status(), "skipped": "unchanged"}
    _status.update(applying=True, error=None)
    freed = False
    try:
        loop = asyncio.get_running_loop()
        snap = store.enrich_cursor()                    # taken with the snapshot
        tracks = (store.all_tracks() if changed is None
                  else enrich_delta.tracks_of(store, changed))
        joined = len(tracks)
        found: set[str] = set()
        matched, items = await loop.run_in_executor(
            None, lambda: collect(tracks, matched_ids=found))
        tracks = None
        before = {tid: (store.get_track(tid) or {}).get("album_source") for tid, _p, _e in items}
        async with fa._get_lock():
            written: list[str] = []
            updated, albums, own_bumps = await fa._commit_album_updates(items, written=written)
        freed = any(before.get(tid) == SOURCE
                    and (store.get_track(tid) or {}).get("album_source") != SOURCE
                    for tid in written)
        if updated:
            await fa.refresh_album_caches(written)
        _last_auto_sig = enrich_delta.record(store, snap, own_bumps, inputs)
        if changed is None:
            _matched_ids = found
        else:
            _matched_ids -= changed
            _matched_ids |= found
        _status["last_apply"] = {"matched": len(_matched_ids), "updated": updated,
                                 "albums": albums, "at": time.time()}
        log.info("Song database enrichment: %d matched, %d of %d joined track(s) "
                 "updated (%d album changes)", len(_matched_ids), updated, joined, albums)
    except Exception as exc:                            # noqa: BLE001
        _status["error"] = f"apply failed: {exc}"
        log.warning("Song database enrichment failed: %s", exc)
    finally:
        _status["applying"] = False
    if freed and fa.enabled():
        try:
            await fa.apply_folder_albums()              # the freed tracks are in its delta
        except Exception:                               # noqa: BLE001
            log.debug("folder-album pass after song-database withdrawals failed",
                      exc_info=True)
    return status()


async def reset_to_file_state() -> dict:
    """Withdraw every album, year, artist and label this pass filled (song
    lengths stay).  Waits (bounded) for an apply already in flight; the caller
    turns auto-apply off first so no new one starts."""
    import asyncio
    global _last_auto_sig
    for _ in range(600):                                # ~60 s ceiling
        if not _status["applying"]:
            break
        await asyncio.sleep(0.1)
    if _status["applying"]:
        return {**status(), "error": "apply already running"}
    from soniqboom.core.store import get_store
    store = get_store()
    _status.update(applying=True, error=None)
    freed = False
    try:
        items = [(t["id"], reset_patch, None) for t in store.all_tracks() if _has_ours(t)]
        freed = any((store.get_track(tid) or {}).get("album_source") == SOURCE
                    for tid, _p, _e in items)
        async with fa._get_lock():
            written: list[str] = []
            cleared, _albums, _bumps = await fa._commit_album_updates(items, written=written)
        if cleared:
            await fa.refresh_album_caches(written)
        _last_auto_sig = None
        _status["last_reset"] = {"cleared": cleared, "at": time.time()}
        log.info("Song database enrichment reset: %d tracks restored", cleared)
    except Exception as exc:                            # noqa: BLE001
        _status["error"] = f"reset failed: {exc}"
        log.warning("Song database reset failed: %s", exc)
    finally:
        _status["applying"] = False
    if freed and fa.enabled():
        try:
            await fa.apply_folder_albums(force=True)
        except Exception:                               # noqa: BLE001
            log.debug("folder-album pass after the song-database reset failed",
                      exc_info=True)
    return {**status(), "cleared": (_status.get("last_reset") or {}).get("cleared", 0)}
