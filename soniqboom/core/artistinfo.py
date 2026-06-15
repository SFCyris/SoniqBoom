# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Artist metadata enrichment — bios + portraits, resolved music-first.

The artist is identified through MusicBrainz (keyless, music-specific), using
the album or track title as context so a name like "Ghost" resolves to the
band on the record being played — never to Wikipedia's article on ghosts.
MusicBrainz then links to the artist's Wikidata entity, whose English-Wikipedia
sitelink yields the bio and portrait.

Resolution order:
  1. MusicBrainz release search  ``release:"<album>" AND artist:"<name>"``
  2. MusicBrainz recording search ``recording:"<track>" AND artist:"<name>"``
  3. MusicBrainz artist search    ``artist:"<name>"`` (exact-name best score)
  4. artist MBID → url-rels → Wikidata QID (or direct Wikipedia rel)
  5. Wikidata sitelinks → enwiki title → Wikipedia REST summary (bio + image)
  6. No wiki link? Fall back to MusicBrainz's own fields (type / country /
     disambiguation) as a one-line description.

Results are cached to disk per artist (positive 30 d, negative 3 d).  A cache
record carries a version marker so older direct-Wikipedia entries (which could
be the wrong entity) are discarded and re-resolved.  MusicBrainz asks for
~1 request/second — a module-wide throttle enforces that.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

from soniqboom import __version__
from soniqboom.config import get_data_dir

log = logging.getLogger(__name__)

_VER = 2                       # cache-record version — bump to re-resolve all
_UA = f"SoniqBoom/{__version__} (https://github.com/SFCyris/SoniqBoom)"
_TTL = 30 * 24 * 3600          # 30 days
_NEG_TTL = 3 * 24 * 3600       # re-try unknown artists after 3 days
_MB = "https://musicbrainz.org/ws/2"
_WD = "https://www.wikidata.org/wiki/Special:EntityData"
_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"

_mem: dict[str, dict] = {}     # name(lower) → record, process-lifetime cache
_inflight: dict[str, asyncio.Lock] = {}

# MusicBrainz politeness: ≥1.1 s between requests, process-wide.
_mb_gate = asyncio.Lock()
_mb_last = 0.0


class _Unreachable(Exception):
    """Network-level failure — don't negative-cache, just skip this time."""


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s[:120] or "_"


def _cache_dir() -> Path:
    d = get_data_dir() / "cache" / "artistinfo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_cache(name: str) -> dict | None:
    rec = _mem.get(name.lower())
    if rec is None:
        p = _cache_dir() / f"{_slug(name)}.json"
        try:
            rec = json.loads(p.read_text())
            _mem[name.lower()] = rec
        except Exception:
            return None
    if not rec or rec.get("v") != _VER:
        return None             # pre-v2 records may be the wrong entity
    age = time.time() - rec.get("fetched_at", 0)
    ttl = _TTL if rec.get("found") else _NEG_TTL
    if age > ttl:
        return None
    return rec


def _write_cache(name: str, rec: dict) -> None:
    rec["v"] = _VER
    _mem[name.lower()] = rec
    try:
        (_cache_dir() / f"{_slug(name)}.json").write_text(json.dumps(rec))
    except Exception as exc:
        log.debug("artistinfo cache write failed for %s: %s", name, exc)


async def _get_json(url: str, *, mb: bool = False) -> dict | None:
    """GET → JSON. ``mb=True`` routes through the MusicBrainz rate gate.
    Returns None on HTTP error status; raises _Unreachable on network failure."""
    import httpx
    global _mb_last
    try:
        if mb:
            async with _mb_gate:
                wait = 1.1 - (time.monotonic() - _mb_last)
                if wait > 0:
                    await asyncio.sleep(wait)
                _mb_last = time.monotonic()
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": _UA}) as cx:
            r = await cx.get(url, follow_redirects=True)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as exc:
        log.debug("artistinfo fetch failed %s: %s", url, exc)
        raise _Unreachable(str(exc))


def _lucene(value: str) -> str:
    """Make a value safe inside a quoted Lucene term."""
    return value.replace('"', " ").strip()


def _pick_credit(entities: list[dict], name: str) -> dict | None:
    """From release/recording search results, find the artist credit whose
    name matches ``name`` (casefold)."""
    want = name.casefold()
    for ent in entities or []:
        for credit in ent.get("artist-credit", []):
            art = credit.get("artist") or {}
            if (art.get("name") or "").casefold() == want:
                return art
    return None


async def _resolve_mbid(name: str, album: str | None, track: str | None) -> dict | None:
    """Identify the artist via MusicBrainz; returns the artist stub dict."""
    q_name = _lucene(name)
    if album:
        q = quote(f'release:"{_lucene(album)}" AND artist:"{q_name}"', safe="")
        d = await _get_json(f"{_MB}/release/?query={q}&fmt=json&limit=3", mb=True)
        art = _pick_credit((d or {}).get("releases"), name)
        if art:
            return art
    if track:
        q = quote(f'recording:"{_lucene(track)}" AND artist:"{q_name}"', safe="")
        d = await _get_json(f"{_MB}/recording/?query={q}&fmt=json&limit=3", mb=True)
        art = _pick_credit((d or {}).get("recordings"), name)
        if art:
            return art
    q = quote(f'artist:"{q_name}"', safe="")
    d = await _get_json(f"{_MB}/artist/?query={q}&fmt=json&limit=5", mb=True)
    want = name.casefold()
    best = None
    for art in (d or {}).get("artists", []):
        if (art.get("name") or "").casefold() == want:
            if best is None or int(art.get("score", 0)) > int(best.get("score", 0)):
                best = art
    if best is None:
        arts = (d or {}).get("artists", [])
        if arts and int(arts[0].get("score", 0)) >= 95:
            best = arts[0]
    return best


async def _wiki_title_for(mbid: str) -> str | None:
    """Artist MBID → enwiki article title, via url-rels (Wikidata preferred)."""
    d = await _get_json(f"{_MB}/artist/{mbid}?inc=url-rels&fmt=json", mb=True)
    qid = None
    for rel in (d or {}).get("relations", []):
        res = (rel.get("url") or {}).get("resource", "")
        if rel.get("type") == "wikipedia" and "en.wikipedia.org/wiki/" in res:
            return res.rsplit("/wiki/", 1)[1]
        if rel.get("type") == "wikidata":
            m = re.search(r"(Q\d+)", res)
            if m:
                qid = m.group(1)
    if not qid:
        return None
    wd = await _get_json(f"{_WD}/{qid}.json")
    try:
        return wd["entities"][qid]["sitelinks"]["enwiki"]["title"]
    except Exception:
        return None


async def _wiki_summary(title: str) -> dict | None:
    d = await _get_json(_SUMMARY + quote(title.replace(" ", "_"), safe=""))
    if d and d.get("type") != "disambiguation" and d.get("extract"):
        return d
    return None


def _mb_only_record(name: str, art: dict) -> dict:
    """Describe the artist from MusicBrainz's own fields when no wiki exists."""
    bits = []
    if art.get("disambiguation"):
        bits.append(art["disambiguation"])
    kind = (art.get("type") or "").lower()
    country = art.get("country") or (art.get("area") or {}).get("name")
    if kind and country:
        bits.append(f"{kind} from {country}")
    elif kind or country:
        bits.append(kind or country)
    span = art.get("life-span") or {}
    if span.get("begin"):
        bits.append(f"active since {span['begin'][:4]}")
    if not bits:
        return {"name": name, "found": False, "fetched_at": time.time()}
    return {
        "name": name, "found": True, "title": art.get("name") or name,
        "bio": f"{art.get('name') or name} — " + ", ".join(bits) + ".",
        "image": None,
        "url": f"https://musicbrainz.org/artist/{art['id']}",
        "source": "MusicBrainz",
        "fetched_at": time.time(),
    }


async def get_artist_info(name: str, album: str | None = None, track: str | None = None) -> dict:
    """Return ``{name, found, bio, image, url, source}`` for an artist.

    ``album`` / ``track`` give MusicBrainz context so ambiguous names resolve
    to the musician on this record. Cached to disk; concurrent requests for
    one artist share a single resolution.
    """
    name = (name or "").strip()
    if not name:
        return {"name": "", "found": False}

    cached = _read_cache(name)
    if cached is not None:
        return cached

    lock = _inflight.setdefault(name.lower(), asyncio.Lock())
    try:
        return await _fetch_locked(name, album, track, lock)
    finally:
        if not lock.locked():
            _inflight.pop(name.lower(), None)


async def _fetch_locked(name: str, album: str | None, track: str | None,
                        lock: asyncio.Lock) -> dict:
    async with lock:
        cached = _read_cache(name)          # double-check after acquiring
        if cached is not None:
            return cached
        try:
            art = await _resolve_mbid(name, album, track)
            if not art:
                rec = {"name": name, "found": False, "fetched_at": time.time()}
                _write_cache(name, rec)
                return rec
            title = await _wiki_title_for(art["id"])
            data = await _wiki_summary(title) if title else None
            if data:
                rec = {
                    "name": name, "found": True,
                    "title": data.get("title"),
                    "bio": data.get("extract"),
                    "image": (data.get("thumbnail") or {}).get("source"),
                    "url": (data.get("content_urls", {}).get("desktop", {}) or {}).get("page"),
                    "source": "Wikipedia",
                    "fetched_at": time.time(),
                }
            else:
                rec = _mb_only_record(name, art)
            _write_cache(name, rec)
            return rec
        except _Unreachable:
            # Network trouble — report not-found for now WITHOUT caching, so
            # the next panel open retries.
            return {"name": name, "found": False}
