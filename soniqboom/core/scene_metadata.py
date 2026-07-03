"""Scene-metadata enrichment from Modland's public MD5 index.

Modland (ftp.modland.com) hosts 500k+ scene modules in a strict
``Format/Author[/coop-Partner][/Collection]/file`` tree and publishes a
nightly, no-auth index of every file's MD5:

    https://ftp.modland.com/pub/documents/allmods_md5.zip
    → allmods_md5.txt: ``<md5><space>Format/Author/.../file`` per line

Joining a library module's ``file_md5`` (cached at scan time) against that
index yields the scene AUTHOR (and format cross-check) with byte-exact
confidence — the same shape as the HVSC Songlengths join for SID.  Per the
house rule (confidence-gated enrichment), ONLY exact-MD5 matches are ever
applied; filename fuzzy matching is deliberately not attempted.

Storage: a local sqlite DB (stdlib, ~40 MB, indexed lookups) under
``<data_dir>/scene/modland.sqlite`` — no RAM cost at runtime.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

MODLAND_MD5_URL = "https://ftp.modland.com/pub/documents/allmods_md5.zip"
_DOWNLOAD_TIMEOUT_S = 300

_status: dict = {
    "index_rows": 0,
    "index_built_at": 0.0,
    "refreshing": False,
    "applying": False,
    "last_apply": None,     # {"matched": n, "updated": n, "at": ts}
    "error": None,
}


def _db_path() -> Path:
    from soniqboom.config import get_data_dir
    d = get_data_dir() / "scene"
    d.mkdir(parents=True, exist_ok=True)
    return d / "modland.sqlite"


def status() -> dict:
    out = dict(_status)
    if not out["index_rows"]:
        try:
            con = sqlite3.connect(_db_path())
            out["index_rows"] = con.execute(
                "SELECT COUNT(*) FROM mods").fetchone()[0]
            con.close()
        except Exception:
            pass
    return out


def refresh_index() -> dict:
    """Download the nightly index and (re)build the sqlite DB.  Blocking —
    callers run it in an executor.  Returns the updated status dict."""
    import httpx
    if _status["refreshing"]:
        # Re-entrancy guard (QA): two concurrent refreshes clobber the same
        # .building/.zip/sqlite paths — reject the second.
        return {**status(), "error": "refresh already running"}
    _status.update(refreshing=True, error=None)
    tmp = _db_path().with_suffix(".building")
    try:
        log.info("Modland index: downloading %s", MODLAND_MD5_URL)
        blob = None
        last_exc: Exception | None = None
        for _attempt in range(3):          # ftp.modland.com can be flaky
            try:
                with httpx.Client(timeout=_DOWNLOAD_TIMEOUT_S,
                                  follow_redirects=True) as client:
                    resp = client.get(MODLAND_MD5_URL)
                    resp.raise_for_status()
                    blob = resp.content
                break
            except Exception as exc:
                last_exc = exc
                time.sleep(2 * (_attempt + 1))
        if blob is None:
            raise RuntimeError(f"download failed after 3 attempts: {last_exc}")
        zpath = _db_path().with_suffix(".zip")
        zpath.write_bytes(blob)
        rows = 0
        tmp.unlink(missing_ok=True)
        con = sqlite3.connect(tmp)
        con.execute("CREATE TABLE mods (md5 TEXT PRIMARY KEY, path TEXT)")
        with zipfile.ZipFile(zpath) as zf:
            name = zf.namelist()[0]
            with zf.open(name) as fh:
                batch = []
                for raw in fh:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    md5, sep, path = line.partition(" ")
                    if sep and len(md5) == 32:
                        batch.append((md5.lower(), path))
                        rows += 1
                        if len(batch) >= 20000:
                            con.executemany(
                                "INSERT OR REPLACE INTO mods VALUES (?,?)",
                                batch)
                            batch.clear()
                if batch:
                    con.executemany(
                        "INSERT OR REPLACE INTO mods VALUES (?,?)", batch)
        con.commit()
        con.close()
        zpath.unlink(missing_ok=True)
        tmp.replace(_db_path())
        _status.update(index_rows=rows, index_built_at=time.time())
        log.info("Modland index built: %d rows", rows)
    except Exception as exc:
        _status["error"] = f"index refresh failed: {exc}"
        log.warning("Modland index refresh failed: %s", exc)
        tmp.unlink(missing_ok=True)
    finally:
        _status["refreshing"] = False
    return status()


def _parse_modland_path(p: str) -> tuple[str, str | None]:
    """(format_dir, author) from ``Format/Author[/coop-Partner]/.../file``.

    ``- unknown`` authors return None; ``coop-X`` subdirs append the partner.
    """
    parts = p.split("/")
    if len(parts) < 3:
        return (parts[0] if parts else "", None)
    fmt_dir, author = parts[0], parts[1]
    if author.strip().lower() in ("- unknown", "-unknown", "unknown"):
        return fmt_dir, None
    for seg in parts[2:-1]:
        if seg.startswith("coop-"):
            author = f"{author} & {seg[5:]}"
    return fmt_dir, author


def collect_updates() -> tuple[int, list[tuple[str, dict]]]:
    """Join every ``file_md5``-carrying track against the index; return
    ``(matched, batch)`` WITHOUT touching the store.

    Blocking (sqlite lookups) — run in an executor.  The store WRITE must
    happen on the event-loop thread (QA MAJOR-2: the store has no lock;
    every other batch writer mutates only on the loop thread, so a
    cross-thread write racing an active scan could corrupt indexes).
    """
    db = _db_path()
    if not db.exists():
        raise RuntimeError("no Modland index — refresh it first")
    from soniqboom.core.store import get_store
    con = sqlite3.connect(db)
    store = get_store()
    matched = 0
    batch: list[tuple[str, dict]] = []
    try:
        for t in store.all_tracks():          # list[dict] (shallow refs)
            md5 = t.get("file_md5")
            if not md5:
                continue
            row = con.execute(
                "SELECT path FROM mods WHERE md5 = ?", (md5.lower(),)
            ).fetchone()
            if not row:
                continue
            matched += 1
            updates: dict = {}
            # Scene provenance — always stored on an exact match; the
            # track-info modal shows it as "Scene origin".
            if t.get("scene_path") != row[0]:
                updates["scene_path"] = row[0]
            _fmt_dir, author = _parse_modland_path(row[0])
            # Some Modland families nest a SUBFORMAT dir where the author
            # normally sits ("Ad Lib/Adlib Tracker 2/song.a2m") — a match
            # whose "author" merely repeats the format name is a tree
            # artefact, not a credit.
            fmt_name = (t.get("format") or "").strip().lower()
            if (author
                    and not (t.get("artist") or "").strip()
                    and author.strip().lower() not in (
                        fmt_name, _fmt_dir.strip().lower())):
                updates["artist"] = author
            if updates:
                batch.append((t["id"], updates))
    finally:
        con.close()
    return matched, batch


async def apply_to_library() -> dict:
    """Async apply: sqlite JOIN in an executor, store WRITE on the loop
    thread (mirrors ``smart._do_dup_recompute``'s pattern)."""
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
            "matched": matched, "updated": updated, "at": time.time()}
        log.info("Modland enrichment: %d matched, %d tracks updated",
                 matched, updated)
    except Exception as exc:
        _status["error"] = f"apply failed: {exc}"
        log.warning("Modland enrichment failed: %s", exc)
    finally:
        _status["applying"] = False
    return status()
