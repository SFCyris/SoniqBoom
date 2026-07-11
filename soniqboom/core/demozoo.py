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
                names = con.execute("SELECT COUNT(*) FROM name_group").fetchone()[0]
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


def _parse_dump(path: Path) -> dict[str, tuple[str, str]]:
    """Stream the pg_dump gzip → ``{normalised_name: (scener, groups_csv)}``.

    Only names that resolve to EXACTLY ONE scener who has ≥1 group membership
    are kept — the confidence gate baked into the index.
    """
    rows: dict[str, list[dict]] = collections.defaultdict(list)
    cur: str | None = None
    cols: list[str] | None = None
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            if cur is None:
                if line.startswith("COPY "):
                    m = re.match(r"COPY (public\.\w+) \(([^)]*)\) FROM stdin;", line)
                    if m and m.group(1) in _TABLES:
                        cur = m.group(1)
                        cols = [c.strip() for c in m.group(2).split(",")]
                continue
            if line.startswith("\\."):
                cur = cols = None
                continue
            parts = line.rstrip("\n").split("\t")
            if cols and len(parts) == len(cols):
                rows[cur].append({c: _unesc(v) for c, v in zip(cols, parts)})

    rel = {r["id"]: r for r in rows["public.demoscene_releaser"]}
    nick_rel = {n["id"]: n["releaser_id"] for n in rows["public.demoscene_nick"]}

    def _isgroup(r: dict) -> bool:
        return (r or {}).get("is_group") in ("t", "true", True, "1")

    rel_names: dict[str, set[str]] = collections.defaultdict(set)
    for n in rows["public.demoscene_nick"]:
        for nm in (n.get("name"), n.get("abbreviation")):
            k = _norm(nm)
            if len(k) >= 2:
                rel_names[n["releaser_id"]].add(k)
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

    out: dict[str, tuple[str, str]] = {}
    for name, sceners in name_sceners.items():
        if len(sceners) != 1:                       # ambiguous → refuse over guess
            continue
        rid = next(iter(sceners))
        groups = sorted(g for g in scener_groups.get(rid, ()) if g)
        if groups:
            out[name] = (rel[rid].get("name") or name, " • ".join(groups[:4]))
    return out


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
            urllib.request.urlretrieve(DUMP_URL, tmp)   # noqa: S310 — fixed HTTPS host
            src = tmp
        else:
            src = dump_path
        mapping = _parse_dump(src)
        db = _db_path()
        con = sqlite3.connect(db)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS name_group "
                        "(name TEXT PRIMARY KEY, scener TEXT, groups TEXT)")
            con.execute("DELETE FROM name_group")
            con.executemany("INSERT OR REPLACE INTO name_group VALUES (?,?,?)",
                            [(k, v[0], v[1]) for k, v in mapping.items()])
            con.commit()
        finally:
            con.close()
        _status.update(built_at=int(time.time()), names=len(mapping))
        log.info("Demozoo index built: %d unambiguous name→group entries", len(mapping))
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


def collect_updates() -> tuple[int, list[tuple[str, dict]]]:
    """Join every retro track's ``artist`` against the index; return
    ``(matched, batch)`` WITHOUT touching the store (run in an executor).
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
    try:
        for t in store.all_tracks():
            if not is_retro_format(t.get("format")):
                continue
            a = (t.get("artist") or "").strip()
            if not a:
                continue
            for v in _name_variants(a):
                row = con.execute(
                    "SELECT groups FROM name_group WHERE name = ?", (v,)
                ).fetchone()
                if row:
                    matched += 1
                    if t.get("scene_group") != row[0]:
                        batch.append((t["id"], {"scene_group": row[0]}))
                    break
    finally:
        con.close()
    return matched, batch


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
        updated = get_store().update_track_fields_batch(batch) if batch else 0
        _status["last_apply"] = {
            "matched": matched, "updated": updated, "at": int(time.time()),
        }
        return {**status(), "matched": matched, "updated": updated}
    except Exception as exc:                            # noqa: BLE001
        _status["error"] = str(exc)
        return {**status(), "error": str(exc)}
    finally:
        _status["applying"] = False
