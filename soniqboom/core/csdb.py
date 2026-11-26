# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CSDb (The C64 Scene Database) data for SID tunes, shown in Track Info.

CSDb's web service (https://csdb.dk/webservice/) answers one entry per
request, by CSDb's own id: ``?type=sid&id=<n>&depth=<d>`` gives a SID's
HVSC path, name and author and, under ``UsedIn``, the releases that use it —
name, type, date, release party, competition placing, rating, screenshot
and, from depth 3, the groups that released it.  It has no search by HVSC
path (an unknown parameter answers with the newest SID id), so the tune's
CSDb id comes from what SoniqBoom holds already:

* the Demozoo index (Settings → Scene): a Demozoo music production that
  links both a DeepSID track (its HVSC path) and a CSDb SID (``CsdbMusic``)
  names the CSDb id of that HVSC path;
* else the CSDb SID link of the Demozoo production the Scene tab matched for
  the track (passed in as ``hint``).

Only a confident answer is shown: the CSDb SID's HVSC path must be the
track's (from the HVSC documents by checksum, or its place in an HVSC tree),
or — for a tune HVSC does not know — its name must be the track's title and
its author must name the track's composer.  Anything else, and the section
simply does not appear.

Requests: only when Track Info opens for a SID (``info_for``), one per tune
not known yet, with the User-Agent "SoniqBoom/<version>"; an answer is kept
in the art cache under ``csdb/`` (``_HIT_TTL`` / ``_MISS_TTL``); a failed
request pauses all CSDb requests for ``_PAUSE``, and no more than
``_HOURLY`` requests go out in any hour.  One screenshot per tune is
fetched when the panel shows it (``screenshot``), re-encoded as PNG and kept.
The ``csdb_lookup`` setting (config store, default on) switches it all off;
offline mode makes no request.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

SETTING = "csdb_lookup"
_WS = "https://csdb.dk/webservice/"
_SITE = "https://csdb.dk"
_VER = 1
_HIT_TTL = 14 * 24 * 3600
_MISS_TTL = 7 * 24 * 3600
_PAUSE = 600.0
_TIMEOUT = 15.0
_DEADLINE = 40.0
_MAX_BYTES = 8 * 1024 * 1024        # a depth-3 answer of a much-used tune is large
_MAX_SHOT = 2 * 1024 * 1024
_MAX_RELEASES = 40
_SHOT_RE = re.compile(r"^https://csdb\.dk/gfx/releases/\d+/\d+\.(?:png|gif|jpg|jpeg)$", re.I)

_HOURLY = 120                       # requests to CSDb in any hour, at most (a client
                                    # stepping through ids can't make the server flood it)

_transport = None                   # tests: an httpx transport
_paused_until = 0.0
_sent: "deque[float]" = deque()     # when the requests of the last hour went out
_inflight: dict[int, asyncio.Future] = {}
_memo: dict[int, dict] = {}
_shown: dict[int, str] = {}         # release id → screenshot URL, of tunes Track Info showed
_shot_inflight: dict[int, asyncio.Future] = {}


class Unreachable(Exception):
    """CSDb could not be asked — nothing is remembered."""


# ── Settings ─────────────────────────────────────────────────────────────────

def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().casefold() not in ("", "0", "false", "off", "no")
    return True if v is None else bool(v)


async def setting_value() -> bool:
    """The ``csdb_lookup`` setting (on unless switched off); raises when the
    settings cannot be read."""
    from soniqboom.core.data import get_config
    return _truthy(await get_config(SETTING, True))


async def enabled() -> bool:
    try:
        return await setting_value()
    except Exception:                                   # noqa: BLE001
        return False


def _offline() -> bool:
    from soniqboom.core import netpolicy
    return netpolicy.offline()


def paused() -> bool:
    return time.monotonic() < _paused_until


def _user_agent() -> str:
    try:
        from soniqboom import __version__
    except Exception:                                   # noqa: BLE001
        __version__ = "?"
    return f"SoniqBoom/{__version__}"


# ── Which tune ───────────────────────────────────────────────────────────────

def _g(track, name):
    if isinstance(track, dict):
        return track.get(name)
    return getattr(track, name, None)


def is_sid(track) -> bool:
    """A C64 SID tune (PSID / RSID) — CSDb's own kind of tune."""
    return str(_g(track, "format") or "") == "SID"


def _norm_path(p: str | None) -> str:
    """An HVSC path compared: one leading slash, ``/`` separators, case folded."""
    s = str(p or "").strip().replace("\\", "/")
    if not s:
        return ""
    return "/" + s.lstrip("/").casefold()


def hvsc_path(track) -> str | None:
    """``/MUSICIANS/…`` — the tune's HVSC path: Songlengths' for its checksum,
    else its place in the online HVSC collection, Modland's HVSC mirror or
    the local HVSC tree."""
    try:
        from soniqboom.core import catalogue
        from soniqboom.core.hvsc import get_hvsc
        h = get_hvsc()
        md5 = str(_g(track, "sid_md5") or "")
        if md5:
            p = h.path_for_md5(md5)
            if p:
                return "/" + p.lstrip("/")
        path = str(_g(track, "path") or "")
        if path and catalogue.is_catalogue_path(path):
            croot = catalogue.root_of(path)
            if croot and catalogue.key_of(croot) == "hvsc":
                return "/" + path[len(croot):].lstrip(":/")
            i = path.find(":/HVSC/")
            if i >= 0:
                return path[i + len(":/HVSC"):]
        key = h.stil_key_for(path) if path else None
        return key or None
    except Exception:                                   # noqa: BLE001
        log.debug("csdb: hvsc path failed", exc_info=True)
        return None


_index: tuple | None = None         # (index file stamp, {hvsc path → {csdb ids}})


def _demozoo_map() -> dict[str, set]:
    """HVSC path → CSDb SID ids, from the Demozoo index: the music
    productions that link a DeepSID track and a CSDb SID.  Rebuilt when the
    index file changes; {} without one."""
    global _index
    try:
        from soniqboom.core import demozoo
        db = demozoo._db_path()
        st = db.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except Exception:                                   # noqa: BLE001
        return {}
    if _index is not None and _index[0] == stamp:
        return _index[1]
    out: dict[str, set] = {}
    try:
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                               "AND name='prod_link'").fetchone():
                _index = (stamp, out)
                return out
            paths: dict[int, set] = {}
            ids: dict[int, set] = {}
            for pid, cls, param in con.execute(
                    "SELECT prod_id, cls, param FROM prod_link "
                    "WHERE cls IN ('DeepSIDTrack', 'CsdbMusic')"):
                if cls == "DeepSIDTrack":
                    n = _norm_path(param)
                    if n.endswith(".sid"):
                        paths.setdefault(pid, set()).add(n)
                elif str(param).strip().isdigit():
                    ids.setdefault(pid, set()).add(int(str(param).strip()))
        finally:
            con.close()
        for pid, ps in paths.items():
            for p in ps:
                for i in ids.get(pid, ()):
                    out.setdefault(p, set()).add(i)
    except sqlite3.Error:
        log.debug("csdb: Demozoo index unreadable", exc_info=True)
    _index = (stamp, out)
    return out


def known_id(path: str | None) -> int | None:
    """The one CSDb SID id the Demozoo index names for an HVSC path."""
    if not path:
        return None
    ids = _demozoo_map().get(_norm_path(path)) or set()
    return next(iter(ids)) if len(ids) == 1 else None


# ── Asking CSDb ──────────────────────────────────────────────────────────────

def _spend() -> bool:
    """Take one request from the hourly budget (``_HOURLY``)."""
    now = time.monotonic()
    while _sent and now - _sent[0] > 3600:
        _sent.popleft()
    if len(_sent) >= _HOURLY:
        return False
    _sent.append(now)
    return True


async def _get(url: str, limit: int) -> tuple[int, bytes, str]:
    if _offline():
        raise Unreachable("offline mode")
    if not _spend():
        raise Unreachable("the hourly number of CSDb requests is used up")
    from soniqboom.core.online_cover import _check_redirect

    async def ask():
        import httpx
        kw = {"transport": _transport} if _transport is not None else {}
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _user_agent()},
                                     follow_redirects=True,
                                     event_hooks={"response": [_check_redirect]}, **kw) as cx:
            async with cx.stream("GET", url) as r:
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > limit:
                        return 413, b"", ""
                return r.status_code, bytes(body), r.headers.get("content-type", "")

    try:
        return await asyncio.wait_for(ask(), _DEADLINE)
    except Exception as exc:                            # noqa: BLE001
        raise Unreachable(f"{type(exc).__name__}: {exc}") from None


def _t(el, name: str) -> str:
    v = el.findtext(name) if el is not None else None
    return " ".join(v.split()) if v else ""


def _int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def parse_sid(xml: bytes) -> dict | None:
    """A CSDb web-service SID answer → ``{id, name, author, released,
    hvsc_path, releases: [...]}``; None when it holds no SID.  Each release:
    ``{id, name, type, year, groups, event, compo, place, rating, shot}``
    (``groups``: names of the groups and sceners that released it — a group
    printed in full elsewhere in the answer is named from there)."""
    import xml.etree.ElementTree as ET
    from defusedxml import DefusedXmlException
    from defusedxml.ElementTree import fromstring
    try:
        root = fromstring(xml, forbid_dtd=True)     # (no DTDs or entities: nothing to expand)
    except (ET.ParseError, DefusedXmlException, ValueError):
        return None
    sid = root.find("SID")
    if sid is None or _int(sid.findtext("ID")) is None:
        return None
    names: dict[str, str] = {}              # group / handle id → name, from anywhere
    g_rating: dict[str, float] = {}         # group id → its CSDb rating
    for g in root.iter("Group"):
        gid, nm = g.findtext("ID"), _t(g, "Name")
        if gid and nm:
            names.setdefault(f"g{gid.strip()}", nm)
        try:
            gr = float(g.findtext("Rating") or 0)
        except ValueError:
            gr = 0.0
        if gid and gr > 0:
            g_rating.setdefault(gid.strip(), round(gr, 2))
    for h in root.iter("Handle"):
        hid, nm = h.findtext("ID"), _t(h, "Handle")
        if hid and nm:
            names.setdefault(f"h{hid.strip()}", nm)
    releases = []
    used = sid.find("UsedIn")
    for rel in (list(used) if used is not None else []):
        if rel.tag != "Release" or _int(rel.findtext("ID")) is None:
            continue
        by = []
        group_rating = None
        rb = rel.find("ReleasedBy")
        for g in (list(rb) if rb is not None else []):
            gid = (g.findtext("ID") or "").strip()
            ref = names.get(("g" if g.tag == "Group" else "h") + gid)
            nm = _t(g, "Name") or _t(g, "Handle") or ref or ""
            if nm and nm not in by:
                by.append(nm)
            if g.tag == "Group" and group_rating is None and gid in g_rating:
                group_rating = {"name": nm, "rating": g_rating[gid]}
        ach = rel.find("Achievement")
        rating = None
        try:
            rating = round(float(rel.findtext("Rating")), 2) if rel.findtext("Rating") else None
        except ValueError:
            rating = None
        shot = _t(rel, "ScreenShot")
        releases.append({
            "id": _int(rel.findtext("ID")),
            "name": _t(rel, "Name"),
            "type": _t(rel, "Type"),
            "year": _int(rel.findtext("ReleaseYear")),
            "groups": by[:6],
            "event": _t(rel.find("ReleasedAt/Event"), "Name"),
            "compo": _t(ach, "Compo") if ach is not None else "",
            "place": _int(ach.findtext("Place")) if ach is not None else None,
            "rating": rating if rating and rating > 0 else None,
            "group_rating": group_rating,
            "shot": shot if _SHOT_RE.match(shot) else "",
        })
        if len(releases) >= _MAX_RELEASES:
            break
    return {
        "id": _int(sid.findtext("ID")),
        "name": _t(sid, "Name"),
        "author": _t(sid, "Author"),
        "released": _t(sid, "Released"),
        "hvsc_path": _t(sid, "HVSCPath"),
        "releases": releases,
    }


def _dir() -> Path:
    from soniqboom.config import get_art_cache_dir
    return get_art_cache_dir() / "csdb"


def _read(sid_id: int) -> dict | None:
    try:
        rec = json.loads((_dir() / f"sid-{sid_id}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("v") == _VER else None


def _write(sid_id: int, rec: dict) -> None:
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"sid-{sid_id}.json"
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec), "utf-8")
    os.replace(tmp, p)


def _fresh(rec: dict | None) -> bool:
    if not rec:
        return False
    ttl = _HIT_TTL if rec.get("sid") else _MISS_TTL
    try:
        return time.time() - float(rec.get("at") or 0) < ttl
    except (TypeError, ValueError):
        return False


async def _fetch(sid_id: int) -> dict:
    """CSDb's answer for SID ``sid_id`` → the kept record ``{v, at, sid}``
    (``sid`` None: CSDb has no such SID).  ``Unreachable`` on failure."""
    global _paused_until
    try:
        status, body, _ = await _get(f"{_WS}?type=sid&id={sid_id}&depth=3", _MAX_BYTES)
        if status == 413:               # too much for depth 3: the releases without groups
            status, body, _ = await _get(f"{_WS}?type=sid&id={sid_id}&depth=2", _MAX_BYTES)
        if status != 200:
            raise Unreachable(f"status {status}")
    except Unreachable as exc:
        if not _offline():
            _paused_until = time.monotonic() + _PAUSE
            log.info("CSDb requests paused for %d s: %s", int(_PAUSE), exc)
        raise
    sid = await asyncio.to_thread(parse_sid, body)
    if sid is None and b"<CSDbData" not in body[:4096]:
        # (an error or maintenance page: nothing is remembered, CSDb is paused)
        _paused_until = time.monotonic() + _PAUSE
        raise Unreachable("not a CSDb answer")
    if sid is not None and sid.get("id") != sid_id:
        sid = None                      # (an id CSDb does not know: it answers another entry)
    rec = {"v": _VER, "at": int(time.time()), "sid": sid}
    try:
        await asyncio.to_thread(_write, sid_id, rec)
    except OSError as exc:
        log.debug("csdb: could not store %s: %s", sid_id, exc)
    return rec


async def sid_record(sid_id: int, *, ask: bool = True) -> dict | None:
    """The kept (or, with ``ask``, newly fetched) record of SID ``sid_id``;
    None when it is not known and cannot be asked now."""
    rec = _memo.get(sid_id)
    if rec is None:
        rec = await asyncio.to_thread(_read, sid_id)
        if rec is not None:
            _memo[sid_id] = rec
    if _fresh(rec):
        return rec
    if not ask or _offline() or paused():
        return rec                      # (an old answer is better than none)
    fut = _inflight.get(sid_id)
    if fut is not None:
        return await asyncio.shield(fut)
    fut = asyncio.get_running_loop().create_future()
    _inflight[sid_id] = fut
    try:
        new = await _fetch(sid_id)
        _memo[sid_id] = new
        fut.set_result(new)
        return new
    except Unreachable:
        return rec
    except Exception:                                   # noqa: BLE001
        log.warning("csdb: SID %s failed", sid_id, exc_info=True)
        return rec
    finally:
        _inflight.pop(sid_id, None)
        if not fut.done():
            fut.set_result(rec)             # (also when cancelled: the others asking don't hang)


# ── Confidence ───────────────────────────────────────────────────────────────

def _fold(value) -> str:
    s = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(ch for ch in s if ch.isalnum())


def confident(sid: dict | None, track, track_hvsc: str | None) -> bool:
    """Is CSDb's ``sid`` this track's tune?  The same HVSC path when the
    track has one; for a tune HVSC does not know, the title equal to CSDb's
    name and the composer named in CSDb's author."""
    if not sid:
        return False
    if track_hvsc:
        return bool(sid.get("hvsc_path")) and _norm_path(sid["hvsc_path"]) == _norm_path(track_hvsc)
    title, artist = _fold(_g(track, "title")), _fold(_g(track, "artist"))
    if not title or title != _fold(sid.get("name")):
        return False
    return bool(artist) and artist in _fold(sid.get("author"))


# ── For Track Info ───────────────────────────────────────────────────────────

def _public(sid: dict) -> dict:
    rels = []
    shot_id = None
    best = None
    for r in sid.get("releases") or []:
        url = f"{_SITE}/release/?id={r['id']}"
        rels.append({k: r.get(k) for k in ("id", "name", "type", "year", "groups", "event",
                                           "compo", "place", "rating", "group_rating")}
                    | {"url": url})
        if r.get("shot"):
            # the screenshot of the best-rated release, else of the first
            score = (r.get("rating") or 0, -len(rels))
            if best is None or score > best:
                best, shot_id = score, r["id"]
    rated = [r["rating"] for r in rels if r.get("rating")]
    return {
        "id": sid["id"], "name": sid.get("name") or "", "author": sid.get("author") or "",
        "released": sid.get("released") or "", "hvsc_path": sid.get("hvsc_path") or "",
        "url": f"{_SITE}/sid/?id={sid['id']}", "releases": rels,
        "top_rating": max(rated) if rated else None,
        "screenshot": f"/api/scene/csdb/shot/{shot_id}.png" if shot_id else None,
        "screenshot_release": shot_id,
    }


async def info_for(track, hint: int | None = None) -> dict:
    """Track Info's CSDb section for ``track``: ``{"found": True, …_public}``
    or ``{"found": False, "reason": …}`` (``off``, ``not_sid``, ``unknown``
    — no CSDb id known —, ``unavailable``, ``no_match``).  At most one request
    to CSDb, and only for a tune not known yet."""
    if not await enabled():
        return {"found": False, "reason": "off"}
    if track is None or not is_sid(track):
        return {"found": False, "reason": "not_sid"}
    path = await asyncio.to_thread(hvsc_path, track)
    sid_id = await asyncio.to_thread(known_id, path)
    if sid_id is None and isinstance(hint, int) and 0 < hint < 10_000_000:
        sid_id = hint
    if sid_id is None:
        return {"found": False, "reason": "unknown"}
    rec = await sid_record(sid_id)
    if rec is None:
        return {"found": False, "reason": "unavailable"}
    sid = rec.get("sid")
    if not confident(sid, track, path):
        return {"found": False, "reason": "no_match"}
    for r in sid.get("releases") or []:
        if r.get("shot"):
            if len(_shown) > 5000:
                _shown.clear()
            _shown[r["id"]] = r["shot"]
    return {"found": True, **_public(sid)}


# ── The screenshot ───────────────────────────────────────────────────────────

def _shot_path(release_id: int) -> Path:
    return _dir() / "shots" / f"{release_id}.png"


def _reencode(data: bytes) -> bytes | None:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data), formats=("PNG", "GIF", "JPEG")) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data), formats=("PNG", "GIF", "JPEG")) as im:
            if im.width * im.height > 4_000_000 or min(im.size) < 8:
                return None
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "PNG", optimize=True)
            return buf.getvalue()
    except Exception:                                   # noqa: BLE001
        return None


def _shot_url(release_id: int) -> str | None:
    """The screenshot URL of ``release_id`` as CSDb listed it for a tune
    Track Info showed (confidently matched) — no other is fetched."""
    return _shown.get(release_id)


async def screenshot(release_id: int) -> bytes | None:
    """The PNG screenshot of a release a shown tune lists; fetched once."""
    p = _shot_path(release_id)
    try:
        return await asyncio.to_thread(p.read_bytes)
    except OSError:
        pass
    url = _shot_url(release_id)
    if not url or not await enabled() or _offline() or paused():
        return None
    fut = _shot_inflight.get(release_id)
    if fut is not None:
        return await asyncio.shield(fut)
    fut = asyncio.get_running_loop().create_future()
    _shot_inflight[release_id] = fut
    png = None
    try:
        png = await _fetch_shot(url, p)
    finally:
        _shot_inflight.pop(release_id, None)
        if not fut.done():
            fut.set_result(png)
    return png


async def _fetch_shot(url: str, p: Path) -> bytes | None:
    global _paused_until
    try:
        status, body, ctype = await _get(url, _MAX_SHOT)
    except Unreachable as exc:
        _paused_until = time.monotonic() + _PAUSE
        log.info("CSDb requests paused for %d s: %s", int(_PAUSE), exc)
        return None
    if status != 200:
        return None
    png = await asyncio.to_thread(_reencode, body)
    if png:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
            await asyncio.to_thread(tmp.write_bytes, png)
            os.replace(tmp, p)
        except OSError:
            pass
    return png


def forget_all() -> None:
    _memo.clear()
    _shown.clear()


def _reset_for_tests() -> None:
    global _paused_until, _index
    _paused_until = 0.0
    _sent.clear()
    _index = None
    _memo.clear()
    _inflight.clear()
    _shown.clear()
    _shot_inflight.clear()
