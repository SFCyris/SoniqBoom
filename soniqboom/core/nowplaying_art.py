# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Now-playing cover lookup for internet-radio stations.

Radio relays push the ICY ``StreamTitle`` (usually ``"Artist - Title"``).  We
try to find a cover for it, in order:

  0. **The user's own library** — exact, instant, private, no external call.
  1. **Discogs** — only when a token is configured (anonymous search 401s).
  2. **MusicBrainz + the Cover Art Archive** — free, no token; the workhorse.

It is **confidence-gated** (the artist, and ideally the title, must match) so a
wrong cover never replaces the station logo — when nothing confident is found
the caller keeps showing the station image.  Results (hit *and* miss) are
cached to disk so a repeated song is free and we stay well under the
MusicBrainz 1 req/s limit.

This reuses ``artistinfo``'s MusicBrainz infrastructure (the shared rate gate,
User-Agent, and JSON fetch) so radio lookups and artist-bio lookups share one
global MB rate budget.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

from soniqboom.config import get_data_dir
# Reuse the artistinfo MusicBrainz scaffolding (shared rate gate + UA + fetch).
from soniqboom.core.artistinfo import _get_json, _lucene, _UA, _Unreachable

log = logging.getLogger(__name__)

_VER = 1
_TTL = 30 * 86400          # positive cache: 30 days
_NEG_TTL = 1 * 86400       # negative cache: 1 day (radio metadata is messy)
_MB = "https://musicbrainz.org/ws/2"
_CAA = "https://coverartarchive.org/release"

_mem: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}


# ── StreamTitle parsing ─────────────────────────────────────────────────────

# Trailing station tags / qualifiers that pollute the artist/title match.
_TRAIL_JUNK = re.compile(r"\s*[\[(](?:[^\])]*?\b(?:radio|live|fm|stream|station|on air|"
                         r"now playing)\b[^\])]*?)[\])]\s*$", re.I)


def parse_stream_title(raw: str) -> tuple[str | None, str | None]:
    """Best-effort split of an ICY ``StreamTitle`` into (artist, title).

    Stations almost universally send ``Artist - Title``.  We split on the first
    ``" - "``, strip surrounding whitespace, and drop an obvious trailing
    ``[Station]`` / ``(Radio Edit)``-style tag.  Returns (None, None) when there
    is no usable artist/title (e.g. a bare title, an ad, or the station name) —
    the caller then just keeps the station logo."""
    if not raw:
        return None, None
    s = _TRAIL_JUNK.sub("", raw.strip())
    # Normalise a few dash variants to a plain hyphen separator.
    parts = re.split(r"\s[-–—]\s", s, maxsplit=1)
    if len(parts) != 2:
        return None, None
    artist, title = parts[0].strip(), parts[1].strip()
    if not artist or not title or len(artist) > 200 or len(title) > 200:
        return None, None
    return artist, title


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").casefold())


def _key(artist: str, title: str) -> str:
    return f"{_norm(artist)}|{_norm(title)}"


def _slug(artist: str, title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", f"{artist}-{title}".strip().lower()).strip("-")
    return s[:120] or "_"


# ── Disk cache (mirrors artistinfo's pattern) ───────────────────────────────

def _cache_dir() -> Path:
    d = get_data_dir() / "cache" / "nowplaying"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_cache(key: str) -> dict | None:
    rec = _mem.get(key)
    if rec is None:
        p = _cache_dir() / f"{key.replace('|', '__')}.json"
        try:
            rec = json.loads(p.read_text())
            _mem[key] = rec
        except Exception:
            return None
    if not rec or rec.get("v") != _VER:
        return None
    age = time.time() - rec.get("fetched_at", 0)
    ttl = _TTL if rec.get("found") else _NEG_TTL
    if age > ttl:
        return None
    return rec


def _write_cache(key: str, rec: dict) -> None:
    rec["v"] = _VER
    rec["fetched_at"] = time.time()
    if len(_mem) > 8192:                # bound the process-lifetime mem cache
        _mem.clear()
    _mem[key] = rec
    try:
        (_cache_dir() / f"{key.replace('|', '__')}.json").write_text(json.dumps(rec))
    except Exception as exc:
        log.debug("nowplaying cache write failed for %s: %s", key, exc)


def _cover_path(slug: str) -> Path:
    return _cache_dir() / f"{slug}.img"


def _prune_covers(cap: int = 3000) -> None:
    """Keep the on-disk cover set bounded — radio runs forever, so delete the
    oldest ``.img`` files once we exceed ``cap`` (they re-fetch on demand)."""
    try:
        imgs = sorted(_cache_dir().glob("*.img"), key=lambda p: p.stat().st_mtime)
        for p in imgs[:-cap] if len(imgs) > cap else []:
            p.unlink(missing_ok=True)
    except Exception:
        pass


# ── Image fetch (CAA / Discogs return image bytes, not JSON) ─────────────────

async def _fetch_image(url: str) -> bytes | None:
    """GET an image URL → bytes, or None.  Follows redirects (CAA → archive.org)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": _UA}) as cx:
            r = await cx.get(url, follow_redirects=True)
        if r.status_code != 200:
            return None
        ctype = r.headers.get("content-type", "")
        if not ctype.startswith("image/"):
            return None
        data = r.content
        # Sanity bounds: a real cover is a few KB to a few MB.
        if not data or len(data) < 256 or len(data) > 12 * 1024 * 1024:
            return None
        return data
    except Exception as exc:
        log.debug("nowplaying image fetch failed %s: %s", url, exc)
        return None


# ── Source: local library ───────────────────────────────────────────────────

def _local_match(artist: str, title: str) -> str | None:
    """Return a track_id from the user's library whose artist AND title match
    (normalised), or None.  Exact, instant, private — preferred over any
    external lookup."""
    try:
        from soniqboom.core.store import get_store
        store = get_store()
        na, nt = _norm(artist), _norm(title)
        # Full-text search is in-memory + fast; cap the candidate set.
        for row in store.search(f"{artist} {title}", limit=25):
            if _norm(row.get("artist", "")) == na and _norm(row.get("title", "")) == nt:
                # Only useful if the track actually has art to serve.
                if row.get("cover_art") or row.get("sid_md5") is None:
                    return row.get("id")
        return None
    except Exception:
        return None


# ── Source: MusicBrainz + Cover Art Archive ─────────────────────────────────

# Release-group secondary types that mean "not the canonical album cover".
_BAD_SECONDARY = {"compilation", "remix", "live", "dj-mix", "mixtape/street",
                  "interview", "soundtrack"}


async def _musicbrainz_cover(artist: str, title: str) -> dict | None:
    """Find the *canonical* release for (artist, title) via MusicBrainz and pull
    its front cover from the Cover Art Archive.  Returns
    ``{cover_bytes, album, year, label, source}`` or None.

    Confidence + quality gating (so we don't show a bootleg trance-remix
    compilation cover for "Pink Floyd – Money"):
      * the recording's artist credit AND title must match;
      * the release must NOT be a bootleg/promo/pseudo-release;
      * the release-group must NOT be a compilation / remix / live / dj-mix;
      * prefer Album > Single > EP, Official status, and the EARLIEST date (the
        original release).  If nothing canonical survives, return None and let
        the caller keep the station logo."""
    na, nt = _norm(artist), _norm(title)
    q = quote(f'recording:"{_lucene(title)}" AND artist:"{_lucene(artist)}"', safe="")
    try:
        d = await _get_json(f"{_MB}/recording/?query={q}&fmt=json&limit=15", mb=True)
    except _Unreachable:
        return None
    if not d:
        return None

    scored: list[tuple[int, str, dict]] = []   # (score, date_sort, release)
    for rec in d.get("recordings", []):
        if not any(_norm((c.get("artist") or {}).get("name", "")) == na
                   for c in rec.get("artist-credit", [])):
            continue
        if _norm(rec.get("title", "")) != nt:
            continue
        for rel in rec.get("releases", []):
            if not rel.get("id"):
                continue
            status = (rel.get("status") or "").lower()
            if status and status != "official":
                continue                       # drop bootleg / promo / pseudo
            rg = rel.get("release-group") or {}
            secondary = {s.lower() for s in (rg.get("secondary-types") or [])}
            if secondary & _BAD_SECONDARY:
                continue                       # drop comp / remix / live / dj-mix
            primary = (rg.get("primary-type") or "").lower()
            score = {"album": 5, "single": 2, "ep": 1}.get(primary, 0)
            if status == "official":
                score += 3
            scored.append((score, rel.get("date") or "9999", rel))

    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1]))   # best score, then earliest date
    for _score, date, rel in scored[:6]:
        data = await _fetch_image(f"{_CAA}/{rel['id']}/front-500")
        if data:
            year = date[:4] if re.match(r"^\d{4}", date) else None
            return {
                "cover_bytes": data,
                "album": rel.get("title") or "",
                "year": year,
                "label": "",            # not in the search response; left blank
                "source": "MusicBrainz",
            }
    return None


# ── Source: Discogs (hook — only when a token is configured) ─────────────────

async def _discogs_cover(artist: str, title: str, token: str) -> dict | None:
    """Discogs release search → cover image.  Requires a personal access token
    (anonymous search 401s).  Confidence-gated on the artist.  Returns the same
    shape as :func:`_musicbrainz_cover`, or None."""
    if not token:
        return None
    import httpx
    na = _norm(artist)
    q = quote(f"{artist} {title}")
    url = (f"https://api.discogs.com/database/search?q={q}&type=release"
           f"&token={quote(token)}&per_page=8")
    try:
        async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": _UA}) as cx:
            r = await cx.get(url)
        if r.status_code != 200:
            return None
        results = r.json().get("results", [])
    except Exception as exc:
        log.debug("discogs search failed: %s", exc)
        return None
    for res in results:
        # Discogs result titles are "Artist - Release"; require the artist part.
        rtitle = res.get("title") or ""
        left = _norm(rtitle.split(" - ", 1)[0]) if " - " in rtitle else ""
        if na and na not in left and left not in na:
            continue
        img = res.get("cover_image") or res.get("thumb")
        if not img or "spacer.gif" in img:
            continue
        data = await _fetch_image(img)
        if data:
            year = str(res.get("year")) if res.get("year") else None
            label = ", ".join(res.get("label", [])[:1]) if res.get("label") else ""
            return {"cover_bytes": data, "album": rtitle, "year": year,
                    "label": label, "source": "Discogs"}
    return None


# ── Public entry point ──────────────────────────────────────────────────────

async def lookup(artist: str, title: str) -> dict | None:
    """Resolve a cover + metadata for a now-playing (artist, title).

    Returns ``{cover_url, album, year, label, source}`` or None.  ``cover_url``
    points at a locally-served image (``/api/art/<track_id>`` for a library hit,
    ``/api/stations/nowplaying-art/<slug>`` for a fetched external cover) so the
    browser never hotlinks the third party (no CORS, no IP leak)."""
    if not artist or not title:
        return None
    key = _key(artist, title)
    if not key.strip("|"):
        return None

    cached = _read_cache(key)
    if cached is not None:
        return cached.get("result")

    if len(_locks) > 4096:               # bound the per-key coalescing locks
        _locks.clear()                   # orphaned in-flight locks self-release
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _read_cache(key)         # double-check after acquiring
        if cached is not None:
            return cached.get("result")
        result = await _resolve(artist, title)
        _write_cache(key, {"found": result is not None, "result": result})
        return result


async def _resolve(artist: str, title: str) -> dict | None:
    # 0. Local library — exact, instant, private.
    tid = _local_match(artist, title)
    if tid:
        return {"cover_url": f"/api/art/{tid}?size=lg", "album": "", "year": None,
                "label": "", "source": "library"}

    # 1. Discogs (only if a token is configured), then 2. MusicBrainz/CAA.
    from soniqboom.config import settings
    token = (getattr(settings, "discogs_token", "") or "").strip()
    found = None
    if token:
        try:
            found = await _discogs_cover(artist, title, token)
        except Exception:
            found = None
    if not found:
        try:
            found = await _musicbrainz_cover(artist, title)
        except Exception:
            found = None
    if not found:
        return None

    # Persist the fetched cover bytes and hand back a local URL.
    slug = _slug(artist, title)
    try:
        _cover_path(slug).write_bytes(found["cover_bytes"])
        _prune_covers()
    except Exception as exc:
        log.debug("nowplaying cover write failed for %s: %s", slug, exc)
        return None
    return {
        "cover_url": f"/api/stations/nowplaying-art/{slug}",
        "album": found.get("album") or "",
        "year": found.get("year"),
        "label": found.get("label") or "",
        "source": found.get("source") or "",
    }


def read_cover(slug: str) -> bytes | None:
    """Serve a previously-fetched now-playing cover by slug (for the endpoint)."""
    if not re.fullmatch(r"[a-z0-9-]{1,120}", slug or ""):
        return None
    p = _cover_path(slug)
    try:
        return p.read_bytes() if p.is_file() else None
    except OSError:
        return None
