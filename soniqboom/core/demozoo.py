# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Demozoo scene-group enrichment.

Demozoo (demozoo.org) is the demoscene's canonical who-made-what database.  Its
nightly SQL dump lets us map a retro track's composer (the Modland-derived
``artist``) to the scene GROUP(s) that composer belonged to — a similarity axis
the retro scorer uses (same collective ≈ similar), and a nice track-info detail.

We join on the composer NAME, not file hashes: Demozoo is a curated metadata DB,
not a file index, so most of a random rip library would never hash-match.  The
author name is matched (real name AND the parenthetical scene handle, e.g.
"Thomas Mogensen (DRAX)") against Demozoo's nick + nick-variant spellings.  A
name that resolves to MORE than one scener is skipped — refuse over guess.

Mirrors ``scene_metadata.py`` (Modland): download an index once → build
``<data_dir>/scene/demozoo.sqlite`` → join the store on demand → apply on the
event-loop thread.  Measured on a 262K-track library: ~36% of unique authors
match a scener, ~28% resolve to groups, and coverage by TRACK is far higher
because prolific scene composers (DRAX, 4-Mat, Goto80 …) all match.
"""
from __future__ import annotations

import collections
import gzip
import logging
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

DUMP_URL = "https://data.demozoo.org/demozoo-export.sql.gz"
_TABLES = {
    "public.demoscene_releaser", "public.demoscene_nick",
    "public.demoscene_nickvariant", "public.demoscene_membership",
}
# Productions tables — used to disambiguate a SHARED handle by matching the
# actual track we're looking at against each candidate scener's productions
# (certain evidence, not a prolificness guess: production count rewards prolific
# CODERS who share a handle and is biased away from obscure musicians whose
# module rips were never catalogued).  A shared handle resolves ONLY when the
# current track's title matches ONE candidate's production; otherwise it refuses.
_PROD_AUTHOR = "public.productions_production_author_nicks"   # M2M production↔nick
_PROD_TABLE = "public.productions_production"                 # production→title

_STOP = frozenset(
    "a an and at by de el for from in la le of on or the to with".split())


def _title_toks(s: object) -> frozenset:
    """Distinctive tokens of a module / production title for cross-matching —
    drops stop-words and <2-char tokens so short/common titles can't false-match."""
    if not isinstance(s, str):
        return frozenset()
    return frozenset(
        t for t in re.sub(r"[^a-z0-9]+", " ", s.lower()).split()
        if len(t) >= 2 and t not in _STOP)


def _toks_match(track: frozenset, prod: frozenset) -> bool:
    """True when a track title and a production title share enough DISTINCTIVE
    tokens to be the same work — tolerant of abbreviations/suffixes (e.g.
    "War in the Middle Earth -Rm" vs "War In The Middle Earth-Remix"): at least
    2 shared tokens AND a majority of the SHORTER title's tokens, so a ripper's
    extra tags on the track side ("… Part Two Final Mix") don't drop a real
    match below the bar."""
    if len(track) < 2 or len(prod) < 2:
        return False
    shared = len(track & prod)
    return shared >= 2 and shared >= (min(len(track), len(prod)) + 1) // 2


def _year_toks(s: object) -> frozenset:
    """Title tokens for the YEAR gate — like ``_title_toks`` but keeps single
    DIGITS.  ``_title_toks`` drops <2-char tokens, so "Last Ninja 2" collapses
    into "Last Ninja" and a sequel's release date would bleed onto the
    original.  The year overwrite demands exact-title identity (see
    ``_production_year``), so the sequel number must survive tokenisation."""
    if not isinstance(s, str):
        return frozenset()
    return frozenset(
        t for t in re.sub(r"[^a-z0-9]+", " ", s.lower()).split()
        if (len(t) >= 2 or t.isdigit()) and t not in _STOP)
_status: dict = {
    "refreshing": False, "applying": False, "error": None,
    "built_at": None, "names": 0, "last_apply": None,
}


def _db_path() -> Path:
    from soniqboom.config import get_data_dir
    d = get_data_dir() / "scene"
    d.mkdir(parents=True, exist_ok=True)
    return d / "demozoo.sqlite"


def status() -> dict:
    db = _db_path()
    exists = db.exists()
    names = _status["names"]
    if exists and not names:
        # ``_status["names"]`` is per-PROCESS (only set by a ``refresh_index``
        # run in THIS process).  After a restart the on-disk index still holds
        # its entries, so read the real count rather than reporting "0" — the
        # Settings › Metadata tab shows this, and 0 would look like "not built".
        try:
            con = sqlite3.connect(db)
            try:
                names = con.execute("SELECT COUNT(*) FROM scener").fetchone()[0]
            finally:
                con.close()
        except Exception:                                   # noqa: BLE001
            names = _status["names"]
    return {**_status, "names": names, "exists": exists,
            "size": db.stat().st_size if exists else 0}


def _norm(s: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()) if isinstance(s, str) else ""


def _unesc(v: str) -> str | None:
    if v == r"\N":
        return None
    return (v.replace(r"\t", "\t").replace(r"\n", "\n")
             .replace(r"\r", "\r").replace(r"\\", "\\"))


def _parse_dump(path: Path):
    """Stream the pg_dump gzip → ``(unique, ambig, prod_years)``.

    ``unique``: ``{normalised_name: (releaser_id, real_name, groups_csv)}`` for
    names that resolve to EXACTLY ONE scener — the confidence gate.  A scener
    with NO group membership is still kept (``groups_csv`` is ""): a solo
    demoscene musician's identity + Demozoo id/url is valuable even without a
    collective.

    ``ambig``: ``[(name, releaser_id, real_name, groups_csv, prod_title_toks)]``
    — for names SHARED by several sceners, one row per candidate × production
    (space-joined normalised title tokens), so a lookup can match the track it's
    looking at against a candidate's actual production and pick the right
    namesake with certainty (never a popularity guess).

    ``prod_years``: ``[(releaser_id, year_toks, year|None)]`` for EVERY
    non-group scener's MUSIC productions — the evidence for the retro
    release-year backfill (a track's Demozoo year replaces the rip/tag year,
    which for scene music is routinely the rip date, not the composition
    date).  Tokens come from ``_year_toks`` (digits kept, so sequels stay
    distinct) and UNDATED productions are included with ``year None`` — they
    act as vetoes in ``_production_year``, never as evidence.
    """
    rows: dict[str, list[dict]] = collections.defaultdict(list)
    prod_nicks: dict[str, list[str]] = {}       # production_id → [nick_id]
    prod_title: dict[str, str | None] = {}      # production_id → title
    prod_super: dict[str, str | None] = {}      # production_id → supertype
    prod_yr: dict[str, int] = {}                # production_id → release year
    cur: str | None = None
    cols: list[str] | None = None
    idx: dict[str, int] = {}
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            if cur is None:
                if line.startswith("COPY "):
                    m = re.match(r"COPY (public\.\w+) \(([^)]*)\) FROM stdin;", line)
                    if m and (m.group(1) in _TABLES
                              or m.group(1) in (_PROD_AUTHOR, _PROD_TABLE)):
                        cur = m.group(1)
                        cols = [c.strip() for c in m.group(2).split(",")]
                        idx = {c: i for i, c in enumerate(cols)}
                continue
            if line.startswith("\\."):
                cur = cols = None
                idx = {}
                continue
            parts = line.rstrip("\n").split("\t")
            if not cols or len(parts) != len(cols):
                continue
            if cur == _PROD_AUTHOR:
                # M2M authorship (millions of rows) — keep only production→nick
                # links, never full row dicts.
                pi, ni = idx.get("production_id"), idx.get("nick_id")
                if pi is not None and ni is not None:
                    prod_nicks.setdefault(parts[pi], []).append(parts[ni])
            elif cur == _PROD_TABLE:
                ii, ti, si = idx.get("id"), idx.get("title"), idx.get("supertype")
                if ii is not None and ti is not None:
                    prod_title[parts[ii]] = _unesc(parts[ti])
                    # supertype ("music"/"production"/"graphics") gates the
                    # shared-handle title match to MUSIC below — a music track
                    # must not resolve a namesake via a co-authored demo/intro.
                    if si is not None:
                        prod_super[parts[ii]] = _unesc(parts[si])
                    # Release date → year, for the retro year backfill.  The
                    # dump column is ``release_date_date``; accept a plain
                    # ``release_date`` too in case the export schema shifts.
                    di = idx.get("release_date_date")
                    if di is None:
                        di = idx.get("release_date")
                    if di is not None:
                        y = _year_of(_unesc(parts[di]))
                        if y:
                            prod_yr[parts[ii]] = y
            else:
                rows[cur].append({c: _unesc(v) for c, v in zip(cols, parts)})

    rel = {r["id"]: r for r in rows["public.demoscene_releaser"]}
    nick_rel = {n["id"]: n["releaser_id"] for n in rows["public.demoscene_nick"]}

    def _isgroup(r: dict) -> bool:
        return (r or {}).get("is_group") in ("t", "true", True, "1")

    # Productions each releaser authored, as title token-sets — the evidence for
    # matching the current track against a shared handle's candidates.  Dedupe by
    # releaser per production so a re-credit under a second same-releaser nick
    # doesn't add a duplicate.
    rel_prod_toks: dict[str, list[frozenset]] = collections.defaultdict(list)
    # (releaser_id, year_toks_str, year|None).  UNDATED productions are kept
    # (year None) as VETO rows: the year gate must refuse when a same-title
    # sibling exists whose date Demozoo doesn't know — otherwise a dated remix/
    # re-release silently supplies its year for the undated original (QA C1,
    # observed live).  Group releasers are skipped: the year lookup only ever
    # queries by the resolved PERSON's id, so group-credited rows are dead
    # weight.
    prod_years: list[tuple[str, str, int | None]] = []
    for pid, nicks in prod_nicks.items():
        # Only MUSIC productions are evidence for a music track's authorship —
        # a coder namesake's co-authored demo title must not resolve the handle.
        # Guarded on the column being present so an older/variant dump without a
        # supertype column degrades to the previous all-supertypes behaviour
        # rather than dropping every production.
        if prod_super and prod_super.get(pid, "music") != "music":
            continue
        toks = _title_toks(prod_title.get(pid))
        ytoks = _year_toks(prod_title.get(pid))
        if not toks and not ytoks:
            continue
        yr = prod_yr.get(pid)
        ytoks_str = " ".join(sorted(ytoks)) if ytoks else None
        seen: set[str] = set()
        for n in nicks:
            rid = nick_rel.get(n)
            if rid and rid not in seen:
                seen.add(rid)
                if toks:
                    rel_prod_toks[rid].append(toks)
                if ytoks_str is not None and not _isgroup(rel.get(rid, {})):
                    prod_years.append((rid, ytoks_str, yr))
    if rows["public.demoscene_nick"] and not prod_nicks:
        log.warning("Demozoo dump: no production-authorship rows parsed — "
                    "shared-handle title disambiguation will be unavailable "
                    "(productions table columns may have changed)")

    rel_names: dict[str, set[str]] = collections.defaultdict(set)
    for n in rows["public.demoscene_nick"]:
        rid = n.get("releaser_id")          # guard: a NULL releaser_id would
        if not rid:                          # inject a phantom None candidate and
            continue                         # wrongly mark a unique handle shared
        for nm in (n.get("name"), n.get("abbreviation")):
            k = _norm(nm)
            if len(k) >= 2:
                rel_names[rid].add(k)
    for v in rows["public.demoscene_nickvariant"]:
        rid = nick_rel.get(v["nick_id"])
        if rid:
            k = _norm(v.get("name"))
            if len(k) >= 2:
                rel_names[rid].add(k)
    for rid, r in rel.items():
        k = _norm(r.get("name"))
        if len(k) >= 2:
            rel_names[rid].add(k)

    name_sceners: dict[str, set[str]] = collections.defaultdict(set)
    for rid, names in rel_names.items():
        if not _isgroup(rel.get(rid, {})):
            for k in names:
                name_sceners[k].add(rid)

    scener_groups: dict[str, set[str]] = collections.defaultdict(set)
    for m in rows["public.demoscene_membership"]:
        g = rel.get(m["group_id"])
        if g and _isgroup(g):
            scener_groups[m["member_id"]].add(g.get("name"))

    def _meta(rid: str) -> tuple[str, str]:
        r = rel.get(rid, {})
        # Prefer the RECORDED real name (first_name + surname); else fall back to
        # the scener's canonical, properly-cased Demozoo handle (NOT the lower-
        # cased index key) — e.g. releaser.name="Purple Motion".
        real = " ".join(p for p in (r.get("first_name"), r.get("surname")) if p).strip()
        groups = sorted(g for g in scener_groups.get(rid, ()) if g)
        return real or (r.get("name") or ""), " • ".join(groups[:4])

    # Unique names resolve outright.  A SHARED handle emits one row per candidate
    # × production (title-token string) so a lookup can match the actual track
    # and pick the RIGHT namesake — certain evidence, never a popularity guess.
    unique: dict[str, tuple[int, str, str]] = {}
    ambig: list[tuple[str, int, str, str, str]] = []
    for name, sceners in name_sceners.items():
        if len(sceners) == 1:
            rid = next(iter(sceners))
            try:
                rid_int = int(rid)
            except (TypeError, ValueError):
                continue
            real, groups = _meta(rid)
            unique[name] = (rid_int, real or name, groups)
        else:
            for rid in sceners:
                try:
                    rid_int = int(rid)
                except (TypeError, ValueError):
                    continue
                real, groups = _meta(rid)
                for toks in rel_prod_toks.get(rid, ()):
                    ambig.append(
                        (name, rid_int, real or name, groups, " ".join(sorted(toks))))
    return unique, ambig, prod_years


def refresh_index(dump_path: Path | None = None) -> dict:
    """Download the Demozoo dump (~192 MB) and (re)build the local sqlite index.

    Blocking (download + parse) — callers ``run_in_executor`` it.  ``dump_path``
    lets tests pass an already-downloaded dump instead of fetching.
    """
    if _status["refreshing"]:
        return status()
    _status.update(refreshing=True, error=None)
    tmp: Path | None = None
    try:
        if dump_path is None:
            tmp = _db_path().with_suffix(".download.gz")
            # Stream with a socket timeout so a STALLED connection fails fast
            # instead of hanging this executor thread forever — a hung thread
            # blocks the server's shutdown ("waiting for background tasks") and
            # leaves it unresponsive.  ``urlretrieve`` has no timeout knob.
            req = urllib.request.Request(
                DUMP_URL, headers={"User-Agent": "SoniqBoom (+demozoo index)"})
            with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as out:  # noqa: S310 — fixed HTTPS host
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
            src = tmp
        else:
            src = dump_path
        unique, ambig, prod_years = _parse_dump(src)
        db = _db_path()
        con = sqlite3.connect(db)
        try:
            # ``scener`` keeps the Demozoo releaser id (→ /sceners/{id}/ link) +
            # real name for UNIQUE names; ``ambig_prod`` holds shared-handle
            # candidates + their production title-tokens for track-title
            # disambiguation; ``prod_year`` holds every scener's dated MUSIC
            # productions for the release-year backfill.  Drop the legacy
            # group-only table.
            con.execute("DROP TABLE IF EXISTS name_group")
            con.execute("CREATE TABLE IF NOT EXISTS scener "
                        "(name TEXT PRIMARY KEY, releaser_id INTEGER, "
                        " real_name TEXT, groups TEXT)")
            con.execute("DELETE FROM scener")
            con.executemany("INSERT OR REPLACE INTO scener VALUES (?,?,?,?)",
                            [(k, v[0], v[1], v[2]) for k, v in unique.items()])
            con.execute("DROP TABLE IF EXISTS ambig_prod")
            con.execute("CREATE TABLE ambig_prod "
                        "(name TEXT, releaser_id INTEGER, real_name TEXT, "
                        " groups TEXT, ptoks TEXT)")
            con.execute("CREATE INDEX ix_ambig_name ON ambig_prod(name)")
            con.executemany("INSERT INTO ambig_prod VALUES (?,?,?,?,?)", ambig)
            con.execute("DROP TABLE IF EXISTS prod_year")
            con.execute("CREATE TABLE prod_year "
                        "(releaser_id INTEGER, ptoks TEXT, year INTEGER)")
            con.execute("CREATE INDEX ix_prod_year_rid ON prod_year(releaser_id)")
            _py_rows = []
            for rid, ptoks, yr in prod_years:
                try:
                    # yr None is a deliberate VETO row (undated production).
                    _py_rows.append((int(rid), ptoks,
                                     int(yr) if yr is not None else None))
                except (TypeError, ValueError):
                    continue
            con.executemany("INSERT INTO prod_year VALUES (?,?,?)", _py_rows)
            con.commit()
        finally:
            con.close()
        _status.update(built_at=int(time.time()), names=len(unique))
        log.info("Demozoo index built: %d unique names + %d shared-handle "
                 "production rows + %d dated music productions",
                 len(unique), len(ambig), len(prod_years))
    except Exception as exc:                            # noqa: BLE001
        _status["error"] = f"refresh failed: {exc}"
        log.warning("Demozoo refresh failed: %s", exc)
    finally:
        _status["refreshing"] = False
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return status()


def _name_variants(a: str) -> set[str]:
    """Normalised match candidates from an author string: the whole name, the
    parenthetical scene handle, and the name with the parenthetical removed."""
    out = {_norm(a)}
    m = re.search(r"\(([^)]+)\)", a)
    if m:
        out.add(_norm(m.group(1)))
    out.add(_norm(re.sub(r"\([^)]*\)", "", a)))
    return {v for v in out if len(v) >= 2}


def _production_year(prod_rows: list[tuple[frozenset, int | None]],
                     ttoks: frozenset) -> int | None:
    """Demozoo canonical release YEAR for the track whose ``_year_toks`` are
    *ttoks*, from one scener's music productions (dated AND undated).

    The gate for overwriting a library field is deliberately much stricter
    than the fuzzy ``_toks_match`` used for identity disambiguation:

    * **exact title identity** — the production's token set must EQUAL the
      track's.  Fuzzy subset/permutation matching let "Title Remix" supply its
      year for "Title" whenever the original was undated (QA C1, observed
      live: the non-remix "War in the Middle Earth" was stamped with the 1994
      Remix date).  Order is still ignored (sets), so "Plastic World" ↔
      "World of plastic" keeps matching; sequels stay distinct because
      ``_year_toks`` keeps digit tokens.
    * **undated veto** — a same-title production whose date Demozoo doesn't
      know means we cannot know which printing this track is: refuse.
    * **single-year consensus** — several same-title dated productions must
      agree; two printings a year apart ⇒ refuse.

    A wrong year is far worse than a missing one — refuse over guess.
    """
    if not ttoks:
        return None
    years: set[int] = set()
    for ptoks, year in prod_rows:
        if ptoks == ttoks:
            if year is None:
                return None                     # undated same-title sibling
            years.add(int(year))
            if len(years) > 1:
                return None
    return years.pop() if len(years) == 1 else None


def collect_updates() -> tuple[int, list[tuple[str, dict]]]:
    """Join every retro track's ``artist`` against the index; return
    ``(matched, batch)`` WITHOUT touching the store (run in an executor).

    Stamps two things per resolved track:
      * ``scene_group`` — the composer's collective(s), as before;
      * ``year`` — the Demozoo canonical release year, under the strict
        ``_production_year`` gate (exact title identity, undated-sibling veto,
        single-year consensus).  Scene rips routinely carry the RIP year (or
        none) in the tag, so the canonical year replaces it; the original is
        preserved once in ``year_file`` and the overwrite is marked
        ``year_source: "demozoo"`` (idempotent across re-applies).

    Wrong stamps get an EXIT: a track carrying ``year_source == "demozoo"``
    whose year the current gate no longer endorses (gate tightened, index
    changed, or the composer no longer resolves) is REVERTED to its preserved
    ``year_file``.  A hand-edited year (``year_source == "user"``) is never
    touched.

    Resolution covers both UNIQUE handles (``scener`` table — collected across
    ALL name variants and REFUSED when variants disagree, mirroring
    ``lookup_scener``) and SHARED handles (disambiguated by this track's title
    against ``ambig_prod``; no title match ⇒ skip).
    """
    db = _db_path()
    if not db.exists():
        raise RuntimeError("no Demozoo index — refresh it first")
    from soniqboom.core.store import get_store
    from soniqboom.core.retro import is_retro_format
    con = sqlite3.connect(db)
    store = get_store()
    matched = 0
    batch: list[tuple[str, dict]] = []
    # Prolific composers appear thousands of times — cache their production
    # rows (parsed once per releaser) instead of re-querying per track.
    prod_cache: dict[int, list[tuple[frozenset, int | None]]] = {}

    def _revert(t: dict) -> dict:
        """Withdraw a stale demozoo stamp: restore the preserved original
        (which may be None — the track simply had no year)."""
        return {"year": t.get("year_file"), "year_source": None, "year_file": None}

    try:
        if not _has_scener_table(con):
            raise RuntimeError(
                "Demozoo index is in the legacy format — refresh it to rebuild")
        has_ambig = _has_ambig_table(con)
        has_years = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prod_year'"
        ).fetchone() is not None
        for t in store.all_tracks():
            if not is_retro_format(t.get("format")):
                continue
            stamped = t.get("year_source") == "demozoo"
            a = (t.get("artist") or "").strip()
            if not a:
                if stamped:
                    batch.append((t["id"], _revert(t)))
                continue
            ttoks = _title_toks(t.get("title"))
            rid = None
            groups = None
            # 1) Unique handle — collect across ALL variants and refuse when
            #    they point at DIFFERENT sceners (same gate as lookup_scener;
            #    taking the first hit was order-nondeterministic, QA M1).
            hits: dict[int, str] = {}
            for v in _name_variants(a):
                row = con.execute(
                    "SELECT releaser_id, groups FROM scener WHERE name = ?", (v,)
                ).fetchone()
                if row and row[0] is not None:
                    hits[int(row[0])] = row[1]
            if len(hits) == 1:
                rid, groups = next(iter(hits.items()))
            # 2) Shared handle — this track's title must pick ONE candidate.
            elif not hits and has_ambig and len(ttoks) >= 2:
                winners: dict[int, str] = {}
                for v in _name_variants(a):
                    for arid, agroups, ptoks in con.execute(
                        "SELECT releaser_id, groups, ptoks FROM ambig_prod "
                        "WHERE name = ?", (v,),
                    ):
                        if arid is not None and _toks_match(
                                ttoks, frozenset((ptoks or "").split())):
                            winners[int(arid)] = agroups
                if len(winners) == 1:
                    rid, groups = next(iter(winners.items()))
            if rid is None:
                # Unresolvable (or variant-ambiguous) — withdraw any stamp we
                # can no longer stand behind.
                if stamped:
                    batch.append((t["id"], _revert(t)))
                continue
            matched += 1
            updates: dict = {}
            # Group-less sceners are indexed now — only stamp a scene_group
            # when there IS one.
            if groups and t.get("scene_group") != groups:
                updates["scene_group"] = groups
            # A user's hand-edited year (tag editor stamps year_source="user")
            # outranks the canonical Demozoo date — never clobber a deliberate
            # correction.
            if has_years and t.get("year_source") != "user":
                ytoks = _year_toks(t.get("title"))
                y = None
                if ytoks:
                    rows = prod_cache.get(rid)
                    if rows is None:
                        rows = [(frozenset((p or "").split()), yy) for p, yy in
                                con.execute("SELECT ptoks, year FROM prod_year "
                                            "WHERE releaser_id = ?", (rid,))]
                        prod_cache[rid] = rows
                    y = _production_year(rows, ttoks=ytoks)
                if y and t.get("year") != y:
                    updates["year"] = y
                    updates["year_source"] = "demozoo"
                    # Preserve the tag/rip year ONCE — a re-apply after an index
                    # update must not clobber the true original with our own
                    # earlier overwrite.
                    if not stamped and t.get("year") is not None:
                        updates["year_file"] = t.get("year")
                elif y is None and stamped:
                    # The tightened gate no longer endorses this stamp — revert.
                    updates.update(_revert(t))
            if updates:
                batch.append((t["id"], updates))
    finally:
        con.close()
    return matched, batch


def _has_scener_table(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scener'"
    ).fetchone()
    return row is not None


# ── Artist-panel lookup (Demozoo-first identity for retro/scene music) ────────

# Live per-scener enrichment is cached this long — a scener's id is stable and
# their links/groups change rarely, so 30 days keeps the API essentially untouched.
_SCENER_TTL = 30 * 86400


def _scener_card(rid: object, real_name: object, groups: object) -> dict:
    return {
        "releaser_id": int(rid),
        "real_name": real_name or "",
        "groups": [g for g in (groups or "").split(" • ") if g],
        "url": f"https://demozoo.org/sceners/{int(rid)}/",
    }


def _has_ambig_table(con: sqlite3.Connection) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ambig_prod'"
    ).fetchone() is not None


def lookup_scener(name: str, track_title: str | None = None) -> dict | None:
    """Offline Demozoo identity for a (retro) artist string, or ``None``.

    A UNIQUE handle resolves outright (and refuses if different name-variants of
    the artist string point at DIFFERENT sceners).  A SHARED handle resolves
    ONLY on evidence: ``track_title`` must match ONE candidate scener's actual
    Demozoo production — so "Skaven" on the module "War in the Middle Earth -Rm"
    resolves to Peter Hajba #356 (who released "War In The Middle Earth-Remix"),
    not a namesake.  No track match ⇒ refuse (refuse over guess).  Reads the
    local sqlite only — fast, offline, never raises.
    Returns ``{releaser_id, real_name, groups[], url}``.
    """
    db = _db_path()
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(db)
        try:
            if not _has_scener_table(con):
                return None                     # legacy index — needs a refresh
            variants = _name_variants(name)
            # 1) Unique names.
            hits: dict[int, tuple] = {}
            for v in variants:
                row = con.execute(
                    "SELECT releaser_id, real_name, groups FROM scener WHERE name = ?",
                    (v,),
                ).fetchone()
                if row and row[0] is not None:
                    hits[int(row[0])] = row
            if len(hits) == 1:
                rid, row = next(iter(hits.items()))
                return _scener_card(rid, row[1], row[2])
            if len(hits) > 1:                   # variants disagree → refuse
                return None
            # 2) Shared handle — pick the candidate whose production matches THIS
            #    track's title.  No title / no match / >1 match ⇒ refuse.
            ttoks = _title_toks(track_title)
            if len(ttoks) < 2 or not _has_ambig_table(con):
                return None
            winners: dict[int, tuple] = {}
            for v in variants:
                for rid, real, groups, ptoks in con.execute(
                    "SELECT releaser_id, real_name, groups, ptoks "
                    "FROM ambig_prod WHERE name = ?", (v,),
                ):
                    if rid is not None and _toks_match(
                            ttoks, frozenset((ptoks or "").split())):
                        winners[int(rid)] = (real, groups)
            if len(winners) != 1:              # 0 = no evidence, >1 = still ambiguous
                return None
            rid, (real, groups) = next(iter(winners.items()))
            return _scener_card(rid, real, groups)
        finally:
            con.close()
    except sqlite3.Error:
        return None


async def fetch_scener_details(releaser_id: int) -> dict | None:
    """Best-effort LIVE Demozoo enrichment for a KNOWN releaser id — external
    links, current groups, aliases — cached to disk for ``_SCENER_TTL``.

    We already hold the exact id (from the offline match), so there is NO
    name-matching risk here.  Returns ``None`` on any failure so the caller
    keeps the offline-only card.
    """
    import asyncio
    import json
    try:
        rid = int(releaser_id)
    except (TypeError, ValueError):
        return None
    cache = _db_path().parent / f"scener_{rid}.json"
    try:
        if cache.exists() and (time.time() - cache.stat().st_mtime) < _SCENER_TTL:
            return json.loads(cache.read_text("utf-8"))
    except Exception:                                    # noqa: BLE001
        pass
    url = f"https://demozoo.org/api/v1/releasers/{rid}/"

    def _get() -> dict:
        req = urllib.request.Request(
            url, headers={"User-Agent": "SoniqBoom (+demozoo artist enrichment)"})
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 — fixed HTTPS host
            return json.load(r)

    try:
        data = await asyncio.get_running_loop().run_in_executor(None, _get)
        # Parse INSIDE the try — a 200 with a non-dict body (``null``, a list)
        # or a wrong-shaped member_of/nicks/external_links must yield None (the
        # documented no-raise contract), NOT raise past artist_card and discard
        # the confident offline identity into the MusicBrainz mislink.
        if not isinstance(data, dict):
            return None
        groups = [(m.get("group") or {}).get("name")
                  for m in (data.get("member_of") or []) if isinstance(m, dict)]
        out = {
            "name": data.get("name"),
            "aliases": [n.get("name") for n in (data.get("nicks") or [])
                        if isinstance(n, dict) and n.get("name")],
            "groups": [g for g in groups if g],
            "links": [{"class": lk.get("link_class"), "url": lk.get("url")}
                      for lk in (data.get("external_links") or [])
                      if isinstance(lk, dict) and lk.get("url")],
            "demozoo_url": data.get("demozoo_url") or f"https://demozoo.org/sceners/{rid}/",
        }
    except Exception as exc:                             # noqa: BLE001 — never raise; caller keeps the offline card
        log.debug("Demozoo scener %s fetch/parse failed: %s", rid, exc)
        return None
    # Only cache a response that actually carries enrichment — a thin/degraded
    # 200 (name null, no groups/links) must NOT be pinned for the 30-day TTL;
    # returning it uncached lets a later panel open retry once the API recovers.
    if out.get("name") or out.get("groups") or out.get("links"):
        try:
            cache.write_text(json.dumps(out), "utf-8")
        except Exception:                                # noqa: BLE001
            pass
    return out


# ── SCENE-tab enrichment (discography + release details) ──────────────────────
# The retro-track SCENE tab surfaces a composer's Demozoo discography and — when
# the current track matches ONE of their productions — that production's release
# details.  Sourced LIVE (the ``releasers/{id}/productions/`` + ``productions/
# {id}/`` API) rather than the offline dump so no giant per-scener production
# table has to be built into the local sqlite (and no 190 MB reindex is needed
# to get it); every response is disk-cached for ``_SCENER_TTL``, exactly like
# ``fetch_scener_details``, so a scener/production is fetched at most once a month
# and only when a user actually opens that track's info panel.
_API_BASE = "https://demozoo.org/api/v1/"
_PROD_PAGE_CAP = 3          # follow at most this many discography pages (cold)


def _year_of(release_date: object) -> int | None:
    """First plausible 4-digit year in a Demozoo date ("1997-08-05", "2017", or
    ``None``) → ``int``, else ``None`` (dates are free-form / partial upstream)."""
    if not isinstance(release_date, str):
        return None
    m = re.search(r"\b(\d{4})\b", release_date)
    if m:
        y = int(m.group(1))
        if 1975 <= y <= 2100:
            return y
    return None


async def fetch_scener_productions(releaser_id: int) -> list[dict] | None:
    """LIVE Demozoo discography for a KNOWN releaser id — their MUSIC
    productions ``[{id, title, year, type, platforms, url}]``, newest first,
    disk-cached for ``_SCENER_TTL``.  ``None`` on failure so the caller shows an
    identity-only card.  Music-only (``supertype == "music"``): the panel is a
    music track's "more by this artist", and the release match below only ever
    needs the composer's music."""
    import asyncio
    import json
    try:
        rid = int(releaser_id)
    except (TypeError, ValueError):
        return None
    cache = _db_path().parent / f"scener_{rid}_prods.json"
    try:
        if cache.exists() and (time.time() - cache.stat().st_mtime) < _SCENER_TTL:
            return json.loads(cache.read_text("utf-8"))
    except Exception:                                    # noqa: BLE001
        pass

    def _get() -> list[dict]:
        out: list[dict] = []
        url: str | None = f"{_API_BASE}releasers/{rid}/productions/"
        pages = 0
        while url and pages < _PROD_PAGE_CAP:
            req = urllib.request.Request(
                url, headers={"User-Agent": "SoniqBoom (+demozoo discography)"})
            with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 — fixed HTTPS host
                data = json.load(r)
            # ``releasers/{id}/productions/`` returns a BARE LIST of the whole
            # discography in one response; the generic ``/productions/`` list
            # endpoint is paginated (``{results, next}``).  Accept either so an
            # upstream shape change can't silently empty the panel.
            if isinstance(data, list):
                results, nxt = data, None
            elif isinstance(data, dict):
                results, nxt = (data.get("results") or []), data.get("next")
            else:
                break
            for p in results:
                if not isinstance(p, dict) or p.get("supertype") != "music":
                    continue
                types = [t.get("name") for t in (p.get("types") or [])
                         if isinstance(t, dict) and t.get("name")]
                plats = [pl.get("name") for pl in (p.get("platforms") or [])
                         if isinstance(pl, dict) and pl.get("name")]
                out.append({
                    "id": p.get("id"),
                    "title": p.get("title") or "",
                    "year": _year_of(p.get("release_date")),
                    "type": types[0] if types else "",
                    "platforms": plats,
                    "url": p.get("demozoo_url") or "",
                })
            # Only follow pagination back into the same trusted API host.
            url = nxt if isinstance(nxt, str) and nxt.startswith(_API_BASE) else None
            pages += 1
        return out

    try:
        prods = await asyncio.get_running_loop().run_in_executor(None, _get)
    except Exception as exc:                             # noqa: BLE001 — never raise
        log.debug("Demozoo productions %s fetch failed: %s", rid, exc)
        return None
    prods.sort(key=lambda p: (p.get("year") or 0), reverse=True)   # newest first
    if prods:
        try:
            cache.write_text(json.dumps(prods), "utf-8")
        except Exception:                                # noqa: BLE001
            pass
    return prods


async def fetch_production_detail(production_id: int) -> dict | None:
    """LIVE Demozoo detail for ONE production — canonical release date/year,
    type, platform, release parties, competition placing(s), and external /
    download links — disk-cached for ``_SCENER_TTL``.  ``None`` on failure."""
    import asyncio
    import json
    try:
        pid = int(production_id)
    except (TypeError, ValueError):
        return None
    cache = _db_path().parent / f"prod_{pid}.json"
    try:
        if cache.exists() and (time.time() - cache.stat().st_mtime) < _SCENER_TTL:
            return json.loads(cache.read_text("utf-8"))
    except Exception:                                    # noqa: BLE001
        pass

    def _get() -> dict:
        req = urllib.request.Request(
            f"{_API_BASE}productions/{pid}/",
            headers={"User-Agent": "SoniqBoom (+demozoo production)"})
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 — fixed HTTPS host
            return json.load(r)

    try:
        data = await asyncio.get_running_loop().run_in_executor(None, _get)
        if not isinstance(data, dict):
            return None
        types = [t.get("name") for t in (data.get("types") or [])
                 if isinstance(t, dict) and t.get("name")]
        plats = [pl.get("name") for pl in (data.get("platforms") or [])
                 if isinstance(pl, dict) and pl.get("name")]
        parties = [{"name": pt.get("name"), "year": _year_of(pt.get("start_date")),
                    "url": pt.get("demozoo_url") or ""}
                   for pt in (data.get("release_parties") or [])
                   if isinstance(pt, dict) and pt.get("name")]
        placings: list[dict] = []
        for pl in (data.get("competition_placings") or []):
            if not isinstance(pl, dict):
                continue
            comp = pl.get("competition") if isinstance(pl.get("competition"), dict) else {}
            party = comp.get("party") if isinstance(comp.get("party"), dict) else {}
            rank = pl.get("ranking") or (str(pl.get("position")) if pl.get("position") else "")
            placings.append({
                "rank": rank,
                "competition": comp.get("name") or "",
                "party": party.get("name") or "",
                "year": _year_of(party.get("start_date")),
                "url": party.get("demozoo_url") or "",
            })

        def _links(key: str) -> list[dict]:
            return [{"class": lk.get("link_class"), "url": lk.get("url")}
                    for lk in (data.get(key) or [])
                    if isinstance(lk, dict) and lk.get("url")]

        out = {
            "id": pid,
            "title": data.get("title") or "",
            "year": _year_of(data.get("release_date")),
            "type": types[0] if types else "",
            "platforms": plats,
            "parties": parties,
            "placings": placings,
            "links": _links("external_links") + _links("download_links"),
            "url": data.get("demozoo_url") or f"https://demozoo.org/productions/{pid}/",
        }
    except Exception as exc:                             # noqa: BLE001 — never raise
        log.debug("Demozoo production %s fetch failed: %s", pid, exc)
        return None
    # Only pin a response that actually carries release detail — a thin/degraded
    # 200 (null release_date, no placings/parties/links) must NOT be cached for
    # the 30-day TTL, or it would suppress the year overwrite (and the whole
    # release block, via the year backfill in scene_card) long after Demozoo
    # recovers.  Mirrors the fetch_scener_details / fetch_scener_productions
    # thin-response guards.
    if out.get("year") or out.get("type") or out.get("platforms") \
            or out.get("placings") or out.get("parties") or out.get("links"):
        try:
            cache.write_text(json.dumps(out), "utf-8")
        except Exception:                                # noqa: BLE001
            pass
    return out


async def scene_card(name: str, track_title: str | None = None) -> dict:
    """Full demoscene enrichment for a retro track's SCENE tab.

    Resolves the composer offline (Demozoo-first identity, same confidence gate
    as ``artist_card`` — including shared-handle disambiguation by ``track_title``)
    then LIVE-enriches with their discography and, when the current track matches
    exactly ONE of their productions by title, that production's release details
    (canonical date/year → the retro year overwrite, type, platform, release
    party, competition placing, links).  Every live call is disk-cached; any
    failure degrades to identity-only.  Never raises.

    Returns ``{found, artist, discography, release}`` (``found: False`` when the
    composer doesn't resolve to a scener — the panel then shows only baseline
    module context).
    """
    import asyncio
    try:
        base = lookup_scener(name, track_title)
        if base is None:
            return {"found": False}
        rid = base["releaser_id"]
        details, prods = await asyncio.gather(
            fetch_scener_details(rid),
            fetch_scener_productions(rid),
        )
        groups = base.get("groups") or []
        aliases: list[str] = []
        links: list[dict] = []
        if details:
            groups = details.get("groups") or groups
            aliases = [a for a in (details.get("aliases") or []) if a]
            links = details.get("links") or []
        artist = {
            "name": name,
            "real_name": base.get("real_name") or "",
            "groups": groups,
            "aliases": aliases,
            "links": links,
            "url": base["url"],
        }
        prods = prods or []
        # Release block — the ONE production whose title matches this track
        # (refuse >1: two matches ⇒ ambiguous, no year overwrite).
        release = None
        ttoks = _title_toks(track_title)
        if len(ttoks) >= 2 and prods:
            winners = [p for p in prods
                       if _toks_match(ttoks, _title_toks(p.get("title")))]
            if len(winners) == 1:
                w = winners[0]
                detail = await fetch_production_detail(w.get("id")) if w.get("id") else None
                if detail:
                    # Backfill year/type/platform/title from the discography row
                    # when the detail response omits them — a thin production
                    # detail (null release_date, no types) must never suppress
                    # the year overwrite when the list row carried the year.
                    release = dict(detail)
                    release["year"] = detail.get("year") or w.get("year")
                    release["type"] = detail.get("type") or w.get("type") or ""
                    release["platforms"] = detail.get("platforms") or w.get("platforms") or []
                    release["title"] = detail.get("title") or w.get("title") or ""
                else:
                    release = {
                        "id": w.get("id"), "title": w.get("title"),
                        "year": w.get("year"), "type": w.get("type"),
                        "platforms": w.get("platforms") or [], "parties": [],
                        "placings": [], "links": [], "url": w.get("url") or "",
                    }
        # Discography — newest-first music, minus the current release (shown in
        # its own block), capped for the panel.
        rel_id = release.get("id") if release else None
        disco = [{"title": p.get("title"), "year": p.get("year"),
                  "type": p.get("type"), "url": p.get("url")}
                 for p in prods if p.get("id") != rel_id and p.get("title")][:15]
        return {"found": True, "artist": artist,
                "discography": disco, "release": release}
    except Exception:                       # noqa: BLE001 — never break the panel
        log.debug("Demozoo scene_card failed for %r", name, exc_info=True)
        return {"found": False}


async def artist_card(name: str, track_title: str | None = None) -> dict | None:
    """Demozoo-first artist card for a (retro) artist, or ``None`` to fall back
    to the MusicBrainz path.

    ``track_title`` is the module we're viewing — it disambiguates a SHARED
    handle by matching the track against the candidate sceners' Demozoo
    productions.  Combines the offline index (identity, groups, ``/sceners/{id}/``
    link) with best-effort live enrichment (external links).  Shaped for the
    existing ``/artist/info`` renderer (``{name, found, bio, image, url,
    source}``) plus ``groups``/``links`` extras the panel renders as chips.
    """
    base = lookup_scener(name, track_title)
    if base is None:
        return None
    details = await fetch_scener_details(base["releaser_id"])
    groups = base["groups"]
    links: list[dict] = []
    if details:
        groups = details.get("groups") or groups
        links = details.get("links") or []
    real = base.get("real_name") or ""
    bits: list[str] = []
    if real and _norm(real) != _norm(name):
        bits.append(real)
    if groups:
        bits.append("Member of " + ", ".join(groups[:5]))
    bio = " — ".join(bits) if bits else f"Demoscene musician “{name}”."
    return {
        "name": name,
        "found": True,
        "title": name,
        "bio": bio,
        "image": None,
        "url": base["url"],
        "source": "Demozoo",
        "groups": groups,
        "links": links,
    }


async def apply_to_library() -> dict:
    """Async apply: sqlite JOIN in an executor, store WRITE on the loop thread
    (mirrors ``scene_metadata.apply_to_library``)."""
    import asyncio
    from soniqboom.core.store import get_store
    if _status["applying"]:
        return {**status(), "error": "apply already running"}
    _status.update(applying=True, error=None)
    try:
        loop = asyncio.get_running_loop()
        matched, batch = await loop.run_in_executor(None, collect_updates)
        updated = 0
        if batch:
            # Batch mode defers the per-item sorted-index maintenance to ONE
            # O(n log n) rebuild on exit — without it a ~20K-item batch spent
            # ~19 s of loop-thread time on incremental bisect.insort against
            # 262K-entry lists, stalling every request mid-apply (QA M3).
            store = get_store()
            store.enter_batch_mode()
            try:
                updated = store.update_track_fields_batch(batch)
            finally:
                store.exit_batch_mode()
        _status["last_apply"] = {
            "matched": matched, "updated": updated, "at": int(time.time()),
        }
        return {**status(), "matched": matched, "updated": updated}
    except Exception as exc:                            # noqa: BLE001
        _status["error"] = str(exc)
        return {**status(), "error": str(exc)}
    finally:
        _status["applying"] = False
