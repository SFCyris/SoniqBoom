"""Online covers for library tracks that have no cover of their own.

A track without an embedded picture and without a folder image is looked up
by its tags — the album at MusicBrainz (cover from the Cover Art Archive),
then at Discogs when a token is set; a track without an album tag by artist
and title — and the cover found is kept in the art cache under ``online/``.
It is served by the art endpoints as a fallback only: the track record and
the per-track art cache are never touched, so a cover the track gains later
(tag or folder image) takes over at once.

Rules:

* a cover is shown only on an exact match of the tagged names — the artist
  and the album (or title) must be equal after folding case, accents and
  punctuation; several different albums of that name are told apart by the
  track's year, and without a single answer nothing is shown;
* chip / tracker formats and the tracks of an online collection are never
  looked up; neither are tracks whose artist is a placeholder;
* a lookup happens only when a cover is asked for (a list row, the player),
  one at a time, paced for MusicBrainz;
* an answer is remembered — a cover until the art cache is cleared, "none"
  for ``_MISS_TTL`` — a failed connection is not, and pauses the lookups;
* the ``online_cover_lookup`` setting switches all of it off; offline mode
  stops the lookups and keeps serving what is stored.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

log = logging.getLogger(__name__)

SETTING = "online_cover_lookup"

_MB = "https://musicbrainz.org/ws/2"
_CAA = "https://coverartarchive.org"
_DISCOGS = "https://api.discogs.com/database/search"
_VA_ARID = "89ad4ac3-39f7-470e-963a-56509c546377"     # MusicBrainz "Various Artists"

_VER = 1
_MISS_TTL = 30 * 24 * 3600      # "none" is asked again after this
_SOFT_TTL = 3600                # a lookup cut short (image host trouble, a bug): this process only
_PAUSE = 120.0                  # after a failed connection
_BUSY_WAITS = (2.0, 5.0)        # MusicBrainz "busy" (503) or "too fast" (429): asked again after these
_TIMEOUT = 12.0                 # per read
_DEADLINE = 30.0                # one request, all told
_PAGES = 5                      # of a MusicBrainz release search (100 releases each)
_MAX_BYTES = 12 * 1024 * 1024
_MAX_PIXELS = 40_000_000
_MIN_SIDE = 100
_SIZES = (("full", 1200, 88), ("lg", 550, 85), ("sm", 200, 85))
_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")           # what a cover is decoded from
_MAX_PENDING = 3000
_MAX_WAITERS = 200
_MAX_MEMO = 50_000

_transport = None               # tests: an httpx transport


class Unreachable(Exception):
    """A lookup service could not be asked — nothing is remembered."""


class _Soft(Exception):
    """An image host answered with a server error."""


# ── Who may be looked up ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Subject:
    kind: str               # "album" | "song"
    artist: str             # as tagged
    name: str               # the album, or the title
    year: int | None
    various: bool
    key: str                # where the answer is kept


_VA_NAMES = frozenset({"various", "various artists", "va", "v.a.", "v/a"})
_NO_ALBUM = frozenset({
    "unknown", "unknown album", "<unknown>", "[unknown]", "<unknown album>",
    "n/a", "none", "no album", "untitled",
})
# A guest credit after the artist: "… (feat. X)", "… ft. X", "… featuring X",
# "… feat X".  A capitalised bare "Feat" / "Ft" is part of a name ("Little Feat
# & …") and stays.
_FEAT = re.compile(r"\s+(?:[\(\[]\s*(?:feat\.?|ft\.?|featuring)|feat\.|ft\.|featuring|(?-i:feat|ft))\s.*$", re.I)
# Titles too common to name one song ("Intro", "Track 03", "Untitled").
_GENERIC_TITLE = re.compile(
    r"(?:intro(?:duction)?|outro|interlude|prelude|skit|untitled|unknown|"
    r"(?:bonus|hidden|audio)?track\d*)", re.I)
_QUALIFIER = re.compile(
    r"\s*[\(\[]([^\(\)\[\]]*\b(?:dis[ck]|cd|remaster(?:ed)?|deluxe|edition|bonus|"
    r"expanded|anniversary|reissue)\b[^\(\)\[\]]*)[\)\]]\s*$", re.I)
_OTHER_RECORDING = re.compile(r"\b(?:live|remix(?:es|ed)?|instrumentals?|acoustic|demos?|karaoke)\b", re.I)
_DISC_TAIL = re.compile(r"[\s,:;\-–—]+(?:dis[ck]|cd)\s*\d+\s*$", re.I)


def _n(value) -> str:
    """Comparison form: case, accents, punctuation and spacing folded away;
    ``&`` reads as "and"."""
    s = unicodedata.normalize("NFKD", str(value or "").replace("&", " and ").casefold())
    return "".join(ch for ch in s if ch.isalnum())


def _na(value) -> str:
    """``_n`` for an artist: a leading "The" does not count."""
    return _n(re.sub(r"^\s*the\s+(?=\S)", "", str(value or ""), flags=re.I))


def _plain_title(album: str) -> str:
    """``album`` without a trailing disc / edition qualifier ("… (Disc 2)",
    "… [Deluxe Edition]", "… CD1"); "" when there is none to drop.  A
    qualifier that names another recording ("(Live Edition)") stays."""
    out = album
    for _ in range(3):
        m = _QUALIFIER.search(out)
        if m and not _OTHER_RECORDING.search(m.group(1)):
            out = out[:m.start()]
            continue
        m = _DISC_TAIL.search(out)
        if m:
            out = out[:m.start()]
            continue
        break
    out = out.strip()
    return out if out and out != album.strip() else ""


def _g(track, name):
    if isinstance(track, dict):
        return track.get(name)
    return getattr(track, name, None)


def _text(value) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def _usable_artist(name: str) -> bool:
    if not name:
        return False
    from soniqboom.core.artistinfo import _is_placeholder_artist
    return not _is_placeholder_artist(name) and name.casefold() not in _VA_NAMES


def subject(track) -> Subject | None:
    """What to look ``track`` up by, or None when it is not looked up at all
    (see the module rules).  ``track`` is a track object or dict; nothing but
    its fields is read."""
    path = str(_g(track, "path") or "")
    if not path or _g(track, "sid_md5") or path.startswith(("http://", "https://")):
        return None
    try:
        from soniqboom.core import catalogue, retro
        if retro.is_retro_format(_g(track, "format")) or catalogue.is_catalogue_path(path):
            return None
    except Exception:
        return None
    artist = _FEAT.sub("", _text(_g(track, "artist"))).strip()
    album_artist = _text(_g(track, "album_artist"))
    album = _text(_g(track, "album"))
    title = _text(_g(track, "title"))
    year = _g(track, "year")
    year = year if isinstance(year, int) and not isinstance(year, bool) and 1000 <= year <= 2999 else None
    if album.casefold() in _NO_ALBUM:
        album = ""
    if album and not _g(track, "album_source"):
        if album_artist.casefold() in _VA_NAMES:
            n = _n(album)
            if not n:
                return None
            key = f"a|~va|{n}" + (f"|y{year}" if year else "")
            return Subject("album", "Various Artists", album, year, True, key)
        who = album_artist if _usable_artist(album_artist) else artist
        if not _usable_artist(who):
            return None
        na, nb = _na(who), _n(album)
        if not na or not nb:
            return None
        return Subject("album", who, album, year, False, f"a|{na}|{nb}")
    if not _usable_artist(artist) or not title:
        return None
    na, nt = _na(artist), _n(title)
    if not na or not nt or _GENERIC_TITLE.fullmatch(nt):
        return None
    return Subject("song", artist, title, year, False, f"t|{na}|{nt}")


# ── Asking the services ──────────────────────────────────────────────────────

def _offline() -> bool:
    from soniqboom.core import netpolicy
    return netpolicy.offline()


async def _http_get(url: str, *, headers: dict | None = None, mb: bool = False) -> tuple[int, bytes, str]:
    """GET ``url`` → ``(status, body, content type)``.  ``Unreachable`` when it
    could not be asked (offline mode, no connection, a timeout — ``_TIMEOUT``
    per read, ``_DEADLINE`` in all — or a redirect to somewhere that is not a
    public web address).  A body over ``_MAX_BYTES`` reads as status 413."""
    from soniqboom.core import artistinfo
    if _offline():
        raise Unreachable("offline mode")
    hdrs = {"User-Agent": artistinfo._UA}
    if headers:
        hdrs.update(headers)

    async def ask() -> tuple[int, bytes, str]:
        import httpx
        kw = {"transport": _transport} if _transport is not None else {}
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=hdrs, follow_redirects=True,
                                     event_hooks={"response": [_check_redirect]}, **kw) as cx:
            async with cx.stream("GET", url) as r:
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > _MAX_BYTES:
                        return 413, b"", ""
                return r.status_code, bytes(body), r.headers.get("content-type", "")

    try:
        if mb:
            await artistinfo.mb_pace()
        return await asyncio.wait_for(ask(), _DEADLINE)
    except Exception as exc:
        raise Unreachable(f"{type(exc).__name__}: {exc}") from None


async def _check_redirect(response) -> None:
    """Refuse a redirect that leaves the public web (another scheme, this
    machine, an address on a private network)."""
    if not response.has_redirect_location:
        return
    target = response.url.join(response.headers.get("location", ""))
    host = (target.host or "").strip("[]").lower()
    ok = target.scheme in ("http", "https") and bool(host) and host != "localhost" \
        and not host.endswith((".localhost", ".local", ".internal"))
    if ok:
        try:
            import ipaddress
            ok = ipaddress.ip_address(host).is_global
        except ValueError:
            pass                                    # a name
    if not ok:
        raise ValueError(f"redirect to {target.scheme}://{host} refused")


async def _json(url: str, *, headers: dict | None = None, mb: bool = False,
                denied_ok: bool = False) -> dict | None:
    """The JSON object at ``url``; None for a definite "nothing" (400 / 404,
    and 401 / 403 with ``denied_ok``); ``Unreachable`` for anything else.
    MusicBrainz (``mb``) answering "busy" is asked again after each of
    ``_BUSY_WAITS`` first — it says so now and then while it is reachable."""
    waits = iter(_BUSY_WAITS if mb else ())
    while True:
        status, body, _ctype = await _http_get(url, headers=headers, mb=mb)
        wait = next(waits, None) if status in (429, 503) else None
        if wait is None:
            break
        log.debug("online cover: status %d from MusicBrainz, asking again in %.0f s", status, wait)
        await asyncio.sleep(wait)
    if status == 200:
        try:
            d = json.loads(body)
        except ValueError:
            raise Unreachable("unreadable answer") from None
        return d if isinstance(d, dict) else None
    if status in (400, 404) or (denied_ok and status in (401, 403)):
        return None
    raise Unreachable(f"status {status}")


async def _image(url: str) -> dict | None:
    """The picture at ``url`` as ``{"full", "lg", "sm"}`` JPEG bytes; None when
    there is none or it is not a usable picture; ``_Soft`` on a server error."""
    status, body, ctype = await _http_get(url)
    if status == 200:
        if not ctype.lower().startswith("image/") or not 256 <= len(body) <= _MAX_BYTES:
            return None
        return await asyncio.to_thread(_normalise, body)
    if status in (400, 401, 403, 404, 410, 413):
        return None
    raise _Soft(f"status {status}")


def _normalise(data: bytes) -> dict | None:
    """Decode ``data`` and re-encode it as the three stored JPEGs; None when it
    is not a picture, smaller than ``_MIN_SIDE`` or absurdly large."""
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data), formats=_FORMATS) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data), formats=_FORMATS) as img:
            w, h = img.size
            if min(w, h) < _MIN_SIDE or w * h > _MAX_PIXELS:
                return None
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
                img = img.convert("RGBA")           # see-through parts on white
                white = Image.new("RGBA", img.size, (255, 255, 255, 255))
                img = Image.alpha_composite(white, img)
            img = img.convert("RGB")
            out = {}
            for name, side, quality in _SIZES:
                im = img.copy()
                im.thumbnail((side, side), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=quality, optimize=True)
                out[name] = buf.getvalue()
            return out
    except Exception:
        return None


def _q(value: str) -> str:
    """``value`` inside a quoted search phrase."""
    return re.sub(r'["\\]', " ", value).strip()


@dataclass
class _Group:
    """One album a search answered with: a MusicBrainz release group, or a
    Discogs master."""
    id: str
    rank: tuple = (0, 0)
    years: set = field(default_factory=set)
    releases: list = field(default_factory=list)      # (date, release id)
    images: list = field(default_factory=list)        # Discogs picture URLs


def _year_of(date) -> int | None:
    m = re.match(r"(\d{4})", str(date or ""))
    return int(m.group(1)) if m and m.group(1) != "0000" else None


def _credit_matches(credit, sub: Subject) -> bool:
    """Is ``sub``'s artist the one a MusicBrainz artist credit names?"""
    if not isinstance(credit, list):
        return False
    parts = [c for c in credit if isinstance(c, dict)]
    if sub.various:
        return any((c.get("artist") or {}).get("id") == _VA_ARID for c in parts)
    who = _na(sub.artist)
    names = ["".join(f"{c.get('name') or ''}{c.get('joinphrase') or ''}" for c in parts)]
    for c in parts:
        names.append(c.get("name") or "")
        names.append((c.get("artist") or {}).get("name") or "")
    return any(n and _na(n) == who for n in names)


_PRIMARY = {"album": 3, "ep": 2, "single": 1}


def _release_groups(answer, sub: Subject, name: str) -> list[_Group]:
    """The albums in a MusicBrainz release search ``answer`` that are ``name``
    by ``sub``'s artist — only the best kind found (an album before an EP
    before a single; a plain one before a compilation / live / remix one),
    official releases when there are any."""
    want = _n(name)
    rels = []
    for rel in (answer or {}).get("releases") or []:
        if not isinstance(rel, dict) or not rel.get("id"):
            continue
        rg = rel.get("release-group")
        if not isinstance(rg, dict) or not rg.get("id"):
            continue
        if _n(rel.get("title")) != want or not _credit_matches(rel.get("artist-credit"), sub):
            continue
        rels.append(rel)
    official = [r for r in rels if str(r.get("status") or "").casefold() == "official"]
    if official:
        rels = official
    groups: dict[str, _Group] = {}
    for rel in rels:
        rg = rel["release-group"]
        g = groups.get(rg["id"])
        if g is None:
            rank = (_PRIMARY.get(str(rg.get("primary-type") or "").casefold(), 0),
                    0 if rg.get("secondary-types") else 1)
            g = groups[rg["id"]] = _Group(str(rg["id"]), rank)
        y = _year_of(rel.get("date"))
        if y:
            g.years.add(y)
        g.releases.append((str(rel.get("date") or "9999"), str(rel["id"])))
    if not groups:
        return []
    best = max(g.rank for g in groups.values())
    return [g for g in groups.values() if g.rank == best]


def _choose(groups: list[_Group], year: int | None, *, need_year: bool = False):
    """``(the one album, were there several)``.  Several: the one — and only
    one — with a release in ``year``, else none.  ``need_year``: a single
    album too must have a release in ``year`` (when the track has one)."""
    if not groups:
        return None, False
    if len(groups) == 1:
        g = groups[0]
        if need_year and year is not None and year not in g.years:
            return None, False
        return g, False
    if year is not None:
        hit = [g for g in groups if year in g.years]
        if len(hit) == 1:
            return hit[0], True
    return None, True


def _discogs_groups(answer, sub: Subject, name: str) -> list[_Group]:
    """The albums in a Discogs release search ``answer`` titled exactly
    "<``sub``'s artist> - <``name``>", one per master release."""
    who, want = _na(sub.artist), _n(name)
    groups: dict[str, _Group] = {}
    for r in (answer or {}).get("results") or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        artist, sep, release = str(r.get("title") or "").partition(" - ")
        if not sep:
            continue
        artist = re.sub(r"\s*\(\d+\)\s*$", "", artist.strip().rstrip("*")).rstrip("*")
        if _na(artist) != who or _n(release) != want:
            continue
        gid = f"master/{r['master_id']}" if r.get("master_id") else f"release/{r['id']}"
        g = groups.setdefault(gid, _Group(gid))
        y = _year_of(r.get("year"))
        if y:
            g.years.add(y)
        url = str(r.get("cover_image") or "")
        if _discogs_image_ok(url) and url not in g.images:
            g.images.append(url)
    return list(groups.values())


def _discogs_image_ok(url: str) -> bool:
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return (u.scheme == "https" and (host == "discogs.com" or host.endswith(".discogs.com"))
            and "spacer.gif" not in u.path)


@dataclass
class _Found:
    source: str             # "musicbrainz" | "discogs"
    ref: str
    images: dict


class _Run:
    """One lookup's bookkeeping."""
    def __init__(self) -> None:
        self.soft = False       # an image host failed: the "none" is not final
        self.several = False    # the answer depended on the track's year

    async def image(self, url: str) -> dict | None:
        """The picture at ``url``, or None.  Trouble at the image host — a
        server error, no connection, a timeout — makes this lookup's "none"
        a short-lived one; a host that could not be reached is left alone for
        ``_PAUSE`` (the other lookups go on)."""
        host = (urlsplit(url).hostname or "").lower()
        if time.monotonic() < _host_down.get(host, 0.0):
            self.soft = True
            return None
        try:
            return await _image(url)
        except _Soft as exc:
            log.debug("online cover: %s for %s", exc, url)
            self.soft = True
            return None
        except Unreachable as exc:
            if _offline():
                raise
            log.info("online cover: %s not reached (%s), left alone for %d s", host, exc, int(_PAUSE))
            if len(_host_down) >= 64:
                _host_down.clear()
            _host_down[host] = time.monotonic() + _PAUSE
            self.soft = True
            return None


_host_down: dict[str, float] = {}       # image host → not asked before (monotonic)


def _discogs_token() -> str:
    try:
        from soniqboom.config import settings
        return str(settings.discogs_token or "")
    except Exception:
        return ""


async def _release_search(query: str) -> tuple[dict, bool]:
    """A MusicBrainz release search, all of its pages up to ``_PAGES`` →
    ``(the answer, were there more releases than were read)``."""
    url = f"{_MB}/release?query={query}&fmt=json&limit=100"
    answer = await _json(url, mb=True)
    rels = [r for r in ((answer or {}).get("releases") or [])]
    try:
        total = int((answer or {}).get("count") or 0)
    except (TypeError, ValueError):
        total = 0
    page = 1
    while len(rels) < total and page < _PAGES:
        more = ((await _json(f"{url}&offset={page * 100}", mb=True)) or {}).get("releases") or []
        if not more:
            break
        rels += more
        page += 1
    return {"releases": rels}, len(rels) < total


async def _lookup_album(sub: Subject, run: _Run) -> _Found | None:
    names = [sub.name]
    plain = _plain_title(sub.name)
    if plain and _n(plain) and _n(plain) != _n(sub.name):
        names.append(plain)
    who = f"arid:{_VA_ARID}" if sub.various else f'artist:"{_q(sub.artist)}"'
    for name in names:
        query = quote(f'release:"{_q(name)}" AND {who}', safe="")
        answer, cut = await _release_search(query)
        groups = _release_groups(answer, sub, name)
        if not groups:
            continue
        g, several = _choose(groups, sub.year, need_year=sub.various)
        if cut and g is not None and not several and (sub.year is None or sub.year not in g.years):
            # Not every release was read, so another album of this name may
            # exist: only the track's year can vouch for this one.
            g, several = None, True
        run.several = run.several or several
        if g is None:
            return None                     # which album is unknown: no other source decides it
        urls = [f"{_CAA}/release-group/{g.id}/front-1200"]
        urls += [f"{_CAA}/release/{rid}/front-1200" for _date, rid in sorted(g.releases)[:3]]
        for url in urls:
            images = await run.image(url)
            if images:
                return _Found("musicbrainz", f"release-group/{g.id}", images)
        break
    token = "" if sub.various else _discogs_token()
    if not token:
        return None
    from soniqboom.core.nowplaying_art import _discogs_headers
    for name in names:
        url = (f"{_DISCOGS}?type=release&artist={quote(sub.artist, safe='')}"
               f"&release_title={quote(name, safe='')}&per_page=25")
        answer = await _json(url, headers=_discogs_headers(token), denied_ok=True)
        groups = _discogs_groups(answer, sub, name)
        if not groups:
            continue
        # (MusicBrainz knew several albums of this name: the year decides here too)
        g, several = _choose(groups, sub.year, need_year=run.several)
        run.several = run.several or several
        if g is not None:
            for picture in g.images[:2]:
                images = await run.image(picture)
                if images:
                    return _Found("discogs", g.id, images)
        break
    return None


_BAD_SECONDARY = frozenset({"compilation", "remix", "live", "dj-mix", "mixtape/street",
                            "interview", "soundtrack", "demo", "spokenword", "audiobook"})


def _song_groups(answer, sub: Subject) -> list[str]:
    """The albums (release-group ids) to take a song's cover from, best first:
    those with an official release of a plain album / single / EP carrying a
    recording titled exactly ``sub.name`` by ``sub``'s artist — an album
    first, the earliest released first."""
    want = _n(sub.name)
    best: dict[str, tuple] = {}
    for rec in (answer or {}).get("recordings") or []:
        if not isinstance(rec, dict) or _n(rec.get("title")) != want:
            continue
        if not _credit_matches(rec.get("artist-credit"), sub):
            continue
        for rel in rec.get("releases") or []:
            if not isinstance(rel, dict) or not rel.get("id"):
                continue
            if str(rel.get("status") or "").casefold() != "official":
                continue
            rg = rel.get("release-group")
            if not isinstance(rg, dict) or not rg.get("id"):
                continue
            if {str(s).casefold() for s in (rg.get("secondary-types") or [])} & _BAD_SECONDARY:
                continue
            score = {"album": 5, "single": 2, "ep": 1}.get(str(rg.get("primary-type") or "").casefold())
            if score is None:
                continue
            rank = (-score, str(rel.get("date") or "9999"))
            gid = str(rg["id"])
            if gid not in best or rank < best[gid]:
                best[gid] = rank
    return [gid for gid, _rank in sorted(best.items(), key=lambda kv: (kv[1], kv[0]))]


async def _lookup_song(sub: Subject, run: _Run) -> _Found | None:
    # Asked for the song's albums first: a much-played song has more concert
    # recordings than one answer holds, and those would crowd the album out.
    base = f'recording:"{_q(sub.name)}" AND artist:"{_q(sub.artist)}" AND status:official'
    for narrow in (" AND primarytype:album", ""):
        query = quote(base + narrow, safe="")
        answer = await _json(f"{_MB}/recording?query={query}&fmt=json&limit=100", mb=True)
        groups = _song_groups(answer, sub)
        if not groups:
            continue
        for gid in groups[:3]:
            images = await run.image(f"{_CAA}/release-group/{gid}/front-1200")
            if images:
                return _Found("musicbrainz", f"release-group/{gid}", images)
        break
    return None


# ── What is remembered ───────────────────────────────────────────────────────

_memo: dict[str, dict | None] = {}      # key → record, or None: nothing on disk


def _base(key: str) -> Path:
    from soniqboom.config import get_art_cache_dir
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return get_art_cache_dir() / "online" / h[:2] / h


def _fresh(rec) -> bool:
    if not isinstance(rec, dict) or rec.get("v") != _VER:
        return False
    if rec.get("ok"):
        return True
    try:
        return time.time() - float(rec.get("at") or 0) < float(rec.get("ttl") or _MISS_TTL)
    except (TypeError, ValueError):
        return False


def _read_record(key: str) -> dict | None:
    try:
        rec = json.loads(Path(f"{_base(key)}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("v") == _VER else None


def _atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _write_record(key: str, rec: dict, images: dict | None) -> None:
    base = _base(key)
    base.parent.mkdir(parents=True, exist_ok=True)
    for name, data in (images or {}).items():
        _atomic(Path(f"{base}.{name}.jpg"), data)
    _atomic(Path(f"{base}.json"), json.dumps(rec).encode("utf-8"))    # last: it announces the pictures


def _drop_record(key: str) -> None:
    base = _base(key)
    for suffix in (".json", ".full.jpg", ".lg.jpg", ".sm.jpg"):
        try:
            Path(f"{base}{suffix}").unlink()
        except OSError:
            pass


def _read_image(key: str, name: str) -> bytes | None:
    try:
        data = Path(f"{_base(key)}.{name}.jpg").read_bytes()
    except OSError:
        return None
    return data or None


def _memoise(key: str, rec: dict | None) -> None:
    if len(_memo) >= _MAX_MEMO and key not in _memo:
        _memo.clear()
    _memo[key] = rec


async def _load_any(key: str) -> dict | None:
    """The record under ``key``, however old; None when there is none."""
    if key in _memo:
        return _memo[key]
    rec = await asyncio.to_thread(_read_record, key)
    _memoise(key, rec)
    return rec


async def _load(key: str) -> dict | None:
    """The valid record under ``key`` (a cover or a remembered "none"), or
    None when the answer is unknown."""
    rec = await _load_any(key)
    return rec if _fresh(rec) else None


async def _store(key: str, found: _Found | None, *, several: bool = False, soft: bool = False) -> dict:
    rec: dict = {"v": _VER, "ok": bool(found), "at": int(time.time())}
    if found:
        rec.update(source=found.source, ref=found.ref)
    elif several:
        rec["amb"] = True
    if soft and not found:
        rec["ttl"] = _SOFT_TTL                      # (not written: this process only)
    else:
        try:
            await asyncio.to_thread(_write_record, key, rec, found.images if found else None)
        except Exception as exc:
            log.warning("online cover: could not store %s: %s", key, exc)
            if found:
                rec = {"v": _VER, "ok": False, "at": int(time.time()), "ttl": _SOFT_TTL}
    _memoise(key, rec)
    return rec


def forget_all() -> None:
    """Forget every answer held in memory and everything queued — the art
    cache directory was emptied."""
    global _gen
    _gen += 1
    _memo.clear()
    _pending.clear()


def forget_misses() -> int:
    """Drop every remembered "none" (memory and disk) so those are asked
    again — a new source was configured.  Returns how many were dropped.  A
    lookup under way does not store its "none" either (``_miss_gen``)."""
    global _miss_gen
    _miss_gen += 1
    for key in [k for k, r in list(_memo.items()) if not (r and r.get("ok"))]:
        _memo.pop(key, None)
    dropped = 0
    try:
        from soniqboom.config import get_art_cache_dir
        root = get_art_cache_dir() / "online"
        for p in root.glob("*/*.json"):
            try:
                rec = json.loads(p.read_text("utf-8"))
                if isinstance(rec, dict) and rec.get("ok"):
                    continue
                p.unlink()
                dropped += 1
            except (OSError, ValueError):
                continue
    except Exception as exc:
        log.debug("online cover: forget_misses: %s", exc)
    for key in [k for k, r in list(_memo.items()) if not (r and r.get("ok"))]:
        _memo.pop(key, None)                        # (a lookup that ended meanwhile)
    return dropped


# ── The state of one track ───────────────────────────────────────────────────

def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() not in ("", "0", "false", "off", "no")
    return bool(value)


async def setting_value() -> bool:
    """The ``online_cover_lookup`` setting as stored (on unless switched
    off); raises when the settings cannot be read."""
    from soniqboom.core.data import get_config
    value = await get_config(SETTING, True)
    return True if value is None else _truthy(value)


async def enabled() -> bool:
    """Are online covers on?  The setting — off while it cannot be read."""
    try:
        return await setting_value()
    except Exception:
        return False


@dataclass(frozen=True)
class State:
    """What is known about one track's online cover."""
    subject: Subject | None = None
    key: str | None = None          # where its answer is (or will be) kept
    record: dict | None = None      # the answer; None: not known yet

    @property
    def hit(self) -> bool:
        return bool(self.record and self.record.get("ok"))

    @property
    def known(self) -> bool:
        return self.record is not None

    @property
    def open(self) -> bool:
        """Can this track still gain an online cover soon — its answer is not
        known yet, or its "none" is a short-lived one?"""
        if self.subject is None or self.hit:
            return False
        return self.record is None or "ttl" in self.record

    @property
    def stamp(self) -> int:
        """Changes whenever the cover does (for an ETag); 0 without one."""
        try:
            return int(self.record.get("at") or 0) if self.hit else 0
        except (TypeError, ValueError):
            return 0


NOTHING = State()          # no online cover, and none to look up


async def state(track) -> State:
    """``track``'s online-cover state; never raises.  With the setting off,
    or for a track that is not looked up, an empty state (no subject)."""
    try:
        if track is None or not await enabled():
            return NOTHING
        sub = subject(track)
        if sub is None:
            return NOTHING
        rec = await _load_any(sub.key)
        if rec is not None and rec.get("amb") and not sub.various and sub.year is not None:
            # Several albums of this name: this track's answer is its year's
            # (kept and refreshed on its own, whatever the age of the note).
            key = f"{sub.key}|y{sub.year}"
            return State(sub, key, await _load(key))
        return State(sub, sub.key, rec if _fresh(rec) else None)
    except Exception as exc:
        log.debug("online cover: state failed: %s", exc)
        return NOTHING


async def image(st: State, size: str = "full") -> bytes | None:
    """The stored cover of ``st`` as JPEG bytes — ``sm`` / ``lg`` thumbnail or
    the full one; None when there is none."""
    if not st.hit or not st.key:
        return None
    name = size if size in ("sm", "lg") else "full"
    data = await asyncio.to_thread(_read_image, st.key, name)
    if data is None:
        # The record outlived its pictures (a partial clean-up): start over.
        _memo.pop(st.key, None)
        await asyncio.to_thread(_drop_record, st.key)
    return data


def etag(base: str, st: State) -> str:
    """``base`` (a quoted ETag) marked with ``st``'s cover, so the answer for
    "no cover" and the one carrying an online cover never share a tag."""
    if not st.hit or len(base) < 2 or not base.endswith('"'):
        return base
    return f'{base[:-1]}:o{st.stamp}"'


# ── The queue ────────────────────────────────────────────────────────────────

@dataclass
class _Job:
    key: str
    subject: Subject
    waiters: "OrderedDict[str, Subject]" = field(default_factory=OrderedDict)   # track id → its subject


_pending: "OrderedDict[str, _Job]" = OrderedDict()      # newest last; served from the end
_current: _Job | None = None
_worker: asyncio.Task | None = None
_paused_until = 0.0
_gen = 0                # bumped when everything is forgotten
_miss_gen = 0           # bumped when every "none" is forgotten


def paused() -> bool:
    return time.monotonic() < _paused_until


def lookups_off() -> bool:
    """Can no lookup run at all (offline mode)?"""
    return _offline()


def request(st: State, track_id: str) -> bool:
    """Ask for ``st``'s cover to be looked up (no-op when the answer is known,
    in offline mode or while paused).  True when a lookup is now queued or
    running — ``art_ready`` is sent for ``track_id`` when a cover is found."""
    if st.subject is None or st.key is None or st.record is not None:
        return False
    if paused() or _offline():
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _enqueue(st.key, st.subject, track_id)
    _kick(loop)
    return True


def _enqueue(key: str, sub: Subject, track_id: str) -> None:
    job = _current if _current is not None and _current.key == key else _pending.get(key)
    if job is None:
        job = _pending[key] = _Job(key, sub)
        while len(_pending) > _MAX_PENDING:
            _pending.popitem(last=False)
    elif job is not _current:
        _pending.move_to_end(key)
    if track_id and track_id not in job.waiters and len(job.waiters) < _MAX_WAITERS:
        job.waiters[track_id] = sub


def _kick(loop) -> None:
    global _worker
    w = _worker
    if w is not None and not w.done():
        try:
            if w.get_loop() is loop:
                return
        except Exception:
            pass
    _worker = loop.create_task(_run())


async def _run() -> None:
    global _current, _paused_until
    while True:
        try:
            _key, job = _pending.popitem(last=True)
        except KeyError:
            return
        _current = job
        try:
            if _offline() or not await enabled():
                _pending.clear()
                return
            await _process(job)
        except Unreachable as exc:
            _pending.clear()
            _paused_until = time.monotonic() + _PAUSE
            log.info("online cover lookups paused for %d s: %s", int(_PAUSE), exc)
        except Exception:
            log.warning("online cover lookup failed for %r", job.key, exc_info=True)
            _memoise(job.key, {"v": _VER, "ok": False, "at": int(time.time()), "ttl": _SOFT_TTL})
        finally:
            _current = None


async def _process(job: _Job) -> None:
    gen, miss_gen = _gen, _miss_gen
    sub = job.subject
    run = _Run()
    if sub.kind == "album":
        found = await _lookup_album(sub, run)
    else:
        found = await _lookup_song(sub, run)
    if gen != _gen:
        return                                  # the art cache was cleared meanwhile
    if found is None and miss_gen != _miss_gen:
        return                                  # every "none" was dropped meanwhile: asked again
    soft = run.soft and found is None
    served = list(job.waiters)
    if job.key == sub.key and run.several and not sub.various:
        # Several albums of this name: the answer belongs to one year only.
        await _store(sub.key, None, several=True)
        if sub.year is not None:
            await _store(f"{sub.key}|y{sub.year}", found, soft=soft)
        served = []
        for tid, other in job.waiters.items():
            if other.year == sub.year:
                served.append(tid)
            elif other.year is not None:
                _enqueue(f"{sub.key}|y{other.year}", other, tid)
    else:
        await _store(job.key, found, soft=soft)
    if found:
        log.info("online cover: %s %r by %r from %s", sub.kind, sub.name, sub.artist, found.source)
        await _announce(served)


async def _announce(track_ids) -> None:
    try:
        from soniqboom.api.library import _broadcast
    except Exception:
        return
    for tid in track_ids:
        try:
            await _broadcast({"event": "art_ready", "track_id": tid})
        except Exception:
            pass


def _reset_for_tests() -> None:
    global _current, _worker, _paused_until
    forget_all()
    _host_down.clear()
    w, _worker = _worker, None
    if w is not None and not w.done():
        try:
            w.cancel()
        except Exception:
            pass
    _current = None
    _paused_until = 0.0
