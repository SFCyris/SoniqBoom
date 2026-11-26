# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Game box covers from IGDB, with the user's own Twitch credentials.

Optional and off until a Twitch application's client id and secret are saved
(Settings → Metadata → IGDB): IGDB (owned by Twitch) answers only with an app
access token from Twitch's client-credentials flow, and allows four requests
a second.  With them, a retro track that has a game name — the one
SoniqBoom already resolves from the file's header, Modland, Demozoo, the song
database, the No-Intro / Redump / TOSEC / MAME game lists or the archive name
(``track.game``, plus ``game_aliases``) — gets that game's box cover when it
is played or its Track Info is opened, never for a list row.

Rules:

* only an exact match counts: the game's name (or one of IGDB's alternative
  names for it) equal to the track's game after ``game_titles.title_key``,
  on one of the platforms the track's format belongs to
  (``game_titles.platforms_for``); several different games left → none;
* the answer is kept in the art cache under ``igdb/`` per game and platform
  — a cover until the art cache is cleared, "none" for ``_MISS_TTL`` — and
  shared by every track of that game (list rows then show it too);
* never written into a music folder or a track record;
* offline mode, a connection failure (a pause of ``_PAUSE``) or credentials
  Twitch refuses stop the lookups; the generated cover stands in.

The pictures are padded to a square on a blurred copy of themselves, so they
sit in the app's square cover frames without being cut.
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
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_GAMES_URL = "https://api.igdb.com/v4/games"
_IMAGE_URL = "https://images.igdb.com/igdb/image/upload/t_1080p/{}.jpg"

_VER = 1
_MISS_TTL = 30 * 24 * 3600
_SOFT_TTL = 3600
_PAUSE = 120.0
_AUTH_PAUSE = 3600.0             # Twitch refused the credentials
_MIN_GAP = 0.3                  # between two IGDB requests (IGDB allows 4 a second)
_TIMEOUT = 12.0
_DEADLINE = 30.0
_MAX_BYTES = 12 * 1024 * 1024
_MAX_PIXELS = 40_000_000
_MIN_SIDE = 90
_SIZES = (("full", 1200, 88), ("lg", 550, 85), ("sm", 200, 85))
_MAX_PENDING = 500
_HOURLY = 300                   # IGDB lookups (games) in any hour, at most
_MAX_WAITERS = 200
_MAX_MEMO = 20_000
_IMAGE_ID = re.compile(r"^[a-z0-9]{4,64}$")

_transport = None               # tests: an httpx transport


class Unreachable(Exception):
    """IGDB or Twitch could not be asked — nothing is remembered."""


class AuthRefused(Exception):
    """Twitch refused the client id / secret."""


# platform (``game_titles.platforms_for``) → (IGDB platform ids, IGDB platform
# names / abbreviations, folded).  A game counts when one of its platforms
# matches either; the names keep a match working should an id be off.
_PLATFORMS: dict[str, tuple[frozenset, frozenset]] = {
    "c64": (frozenset({15}), frozenset({"commodorec64128max", "c64", "commodore64", "c64c128"})),
    "amiga": (frozenset({16}), frozenset({"amiga", "amigacd32", "cd32"})),
    "atarist": (frozenset({63}), frozenset({"ataristste", "atarist", "atariste"})),
    "dos": (frozenset({13}), frozenset({"dos", "pcdos", "msdos"})),
    "nes": (frozenset({18, 99}), frozenset({"nintendoentertainmentsystem", "nes", "familycomputer",
                                           "famicom", "nesfamicom"})),
    "snes": (frozenset({19, 58}), frozenset({"supernintendoentertainmentsystem", "snes",
                                            "superfamicom", "sfam", "snessuperfamicom"})),
    "gb": (frozenset({33, 22}), frozenset({"gameboy", "gb", "gameboycolor", "gbc"})),
    "gba": (frozenset({24}), frozenset({"gameboyadvance", "gba"})),
    "nds": (frozenset({20}), frozenset({"nintendods", "nds"})),
    "n64": (frozenset({4}), frozenset({"nintendo64", "n64"})),
    "psx": (frozenset({7}), frozenset({"playstation", "ps1", "psx", "ps"})),
    "ps2": (frozenset({8}), frozenset({"playstation2", "ps2"})),
    "saturn": (frozenset({32}), frozenset({"segasaturn", "saturn"})),
    "dreamcast": (frozenset({23}), frozenset({"dreamcast", "dc", "segadreamcast"})),
    "megadrive": (frozenset({29}), frozenset({"segamegadrivegenesis", "genesismegadrive",
                                             "megadrive", "genesis", "segagenesis"})),
    "sms": (frozenset({64}), frozenset({"segamastersystemmarkiii", "segamastersystem",
                                       "mastersystem", "sms"})),
    "gamegear": (frozenset({35}), frozenset({"segagamegear", "gamegear", "gg"})),
    "pce": (frozenset({86}), frozenset({"turbografx16pcengine", "turbografx16", "pcengine",
                                       "tg16", "pce", "pcenginecd", "turbografxcd"})),
    "msx": (frozenset({27, 53}), frozenset({"msx", "msx2"})),
    "zx": (frozenset({26}), frozenset({"zxspectrum", "zxs"})),
    "cpc": (frozenset({25}), frozenset({"amstradcpc", "acpc", "cpc"})),
    "atari8": (frozenset({65}), frozenset({"atari8bit", "atari8"})),
    "x68000": (frozenset({121}), frozenset({"sharpx68000", "x68000"})),
    "arcade": (frozenset({52}), frozenset({"arcade"})),
    "neogeo": (frozenset({80, 79}), frozenset({"neogeoaes", "neogeomvs", "neogeo"})),
}


def _fold(value) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


# ── Settings ─────────────────────────────────────────────────────────────────

def credentials() -> tuple[str, str]:
    try:
        from soniqboom.config import settings
        return (str(getattr(settings, "igdb_client_id", "") or "").strip(),
                str(getattr(settings, "igdb_client_secret", "") or "").strip())
    except Exception:                                   # noqa: BLE001
        return "", ""


def configured() -> bool:
    cid, secret = credentials()
    return bool(cid and secret)


def credentials_ok(client_id: str, secret: str) -> bool:
    """Do these look like a Twitch client id and secret (letters and digits)?"""
    return bool(re.fullmatch(r"[A-Za-z0-9]{8,64}", client_id or "")
                and re.fullmatch(r"[A-Za-z0-9]{8,64}", secret or ""))


def _offline() -> bool:
    from soniqboom.core import netpolicy
    return netpolicy.offline()


# ── Who is looked up ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Subject:
    game: str
    names: frozenset            # title_key of the game and of its aliases
    platforms: tuple
    key: str


def _g(track, name):
    if isinstance(track, dict):
        return track.get(name)
    return getattr(track, name, None)


def subject(track) -> Subject | None:
    """What ``track`` is looked up by — its game on its platforms — or None."""
    try:
        from soniqboom.core import game_titles, retro
        if track is None or not retro.is_retro_format(_g(track, "format")):
            return None
        game = " ".join(str(_g(track, "game") or "").split())
        if not game:
            return None
        plats, _strict = game_titles.platforms_for(_g(track, "format"))
        plats = tuple(p for p in plats if p in _PLATFORMS)
        if not plats:
            return None
        key = game_titles.title_key(game)
        if not key:
            return None
        names = {key}
        for alias in _g(track, "game_aliases") or ():
            k = game_titles.title_key(str(alias or ""))
            if k:
                names.add(k)
        return Subject(game, frozenset(names), plats, f"{'+'.join(sorted(plats))}|{key}")
    except Exception:                                   # noqa: BLE001
        log.debug("igdb subject failed", exc_info=True)
        return None


# ── Asking IGDB ──────────────────────────────────────────────────────────────

async def _http(method: str, url: str, *, headers: dict | None = None,
                params: dict | None = None, content: bytes | None = None) -> tuple[int, bytes, str]:
    if _offline():
        raise Unreachable("offline mode")
    from soniqboom.core.online_catalog import user_agent
    from soniqboom.core.online_cover import _check_redirect
    hdrs = {"User-Agent": user_agent()}
    if headers:
        hdrs.update(headers)

    async def ask():
        import httpx
        kw = {"transport": _transport} if _transport is not None else {}
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=hdrs, follow_redirects=True,
                                     event_hooks={"response": [_check_redirect]}, **kw) as cx:
            async with cx.stream(method, url, params=params, content=content) as r:
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > _MAX_BYTES:
                        return 413, b"", ""
                return r.status_code, bytes(body), r.headers.get("content-type", "")

    try:
        return await asyncio.wait_for(ask(), _DEADLINE)
    except Exception as exc:                            # noqa: BLE001
        raise Unreachable(f"{type(exc).__name__}: {exc}") from None


_token: tuple[str, float, str] | None = None     # (token, valid until (monotonic), for client id)
_last_call = 0.0


async def _access_token(client_id: str, secret: str, *, fresh: bool = False) -> str:
    """An app access token from Twitch's client-credentials flow, kept until
    shortly before it ends.  ``AuthRefused`` when Twitch refuses the pair."""
    global _token
    if not fresh and _token and _token[2] == client_id and time.monotonic() < _token[1]:
        return _token[0]
    # (in the form body, never the URL: request URLs are logged)
    from urllib.parse import urlencode
    status, body, _ = await _http("POST", _TOKEN_URL, content=urlencode({
        "client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    if status in (400, 401, 403):
        raise AuthRefused(f"Twitch refused the client id / secret (status {status})")
    if status != 200:
        raise Unreachable(f"Twitch token: status {status}")
    try:
        d = json.loads(body)
        tok = str(d["access_token"])
        life = float(d.get("expires_in") or 3600)
    except (ValueError, KeyError, TypeError):
        raise Unreachable("unreadable Twitch token answer") from None
    _token = (tok, time.monotonic() + max(60.0, life * 0.9), client_id)
    return tok


async def _pace() -> None:
    global _last_call
    wait = _last_call + _MIN_GAP - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)
    _last_call = time.monotonic()


def _query(name: str) -> bytes:
    q = re.sub(r'["\\;]', " ", name).strip()
    return (f'search "{q}"; fields name,alternative_names.name,platforms.name,'
            f'platforms.abbreviation,cover.image_id,version_parent; limit 50;').encode("utf-8")


async def _games(name: str) -> list:
    """IGDB's games for a name search (a list of objects)."""
    cid, secret = credentials()
    if not (cid and secret):
        raise AuthRefused("no credentials")
    for attempt in range(3):
        tok = await _access_token(cid, secret, fresh=attempt > 0)
        await _pace()
        status, body, _ = await _http("POST", _GAMES_URL, content=_query(name), headers={
            "Client-ID": cid, "Authorization": f"Bearer {tok}", "Accept": "application/json"})
        if status == 200:
            try:
                d = json.loads(body)
            except ValueError:
                raise Unreachable("unreadable IGDB answer") from None
            return d if isinstance(d, list) else []
        if status == 401 and attempt == 0:
            continue                                    # the token ended early: a new one
        if status == 429 and attempt < 2:
            await asyncio.sleep(1.0)
            continue
        if status in (400, 404):
            return []
        raise Unreachable(f"IGDB: status {status}")
    raise Unreachable("IGDB: no answer")


def _on_platform(game: dict, platforms: tuple) -> bool:
    ids: set = set()
    names: set = set()
    for p in platforms:
        pi, pn = _PLATFORMS.get(p, (frozenset(), frozenset()))
        ids |= pi
        names |= pn
    for pl in game.get("platforms") or []:
        if isinstance(pl, int):
            if pl in ids:
                return True
            continue
        if not isinstance(pl, dict):
            continue
        if pl.get("id") in ids or _fold(pl.get("name")) in names \
                or _fold(pl.get("abbreviation")) in names:
            return True
    return False


def choose(games: list, sub: Subject) -> tuple[str | None, bool]:
    """``(the cover image id of the one game that is ``sub``'s, were there
    several)``: a game whose name or an alternative name equals one of the
    track's game names (``title_key``) on one of its platforms, with a
    cover.  A version of a game that also matches counts as that game; two
    different games left → none."""
    from soniqboom.core.game_titles import title_key
    hits: dict[int, dict] = {}
    for gm in games or []:
        if not isinstance(gm, dict) or not isinstance(gm.get("id"), int):
            continue
        keys = {title_key(str(gm.get("name") or ""))}
        for alt in gm.get("alternative_names") or []:
            if isinstance(alt, dict):
                keys.add(title_key(str(alt.get("name") or "")))
        if not (keys & sub.names) or not _on_platform(gm, sub.platforms):
            continue
        cover = gm.get("cover")
        image = cover.get("image_id") if isinstance(cover, dict) else None
        if not (isinstance(image, str) and _IMAGE_ID.match(image)):
            continue
        hits[gm["id"]] = gm
    # A version (an edition) whose main game is also a hit is that game.
    left = [gm for gm in hits.values()
            if not (isinstance(gm.get("version_parent"), int) and gm["version_parent"] in hits)]
    images = {gm["cover"]["image_id"] for gm in left}
    if len(images) == 1:
        return images.pop(), False
    return None, len(images) > 1


def _square(data: bytes) -> dict | None:
    """The picture as ``{"full", "lg", "sm"}`` square JPEGs — fitted whole
    onto a blurred, darkened copy of itself; None when it is no usable
    picture."""
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
        with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP")) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP")) as img:
            w, h = img.size
            if min(w, h) < _MIN_SIDE or w * h > _MAX_PIXELS:
                return None
            img = ImageOps.exif_transpose(img).convert("RGB")
            out = {}
            for name, side, quality in _SIZES:
                bg = ImageOps.fit(img, (side, side), Image.BILINEAR)
                bg = bg.filter(ImageFilter.GaussianBlur(max(2, side // 24)))
                bg = ImageEnhance.Brightness(bg).enhance(0.45)
                fg = img.copy()
                fg.thumbnail((side, side), Image.LANCZOS)
                bg.paste(fg, ((side - fg.width) // 2, (side - fg.height) // 2))
                buf = io.BytesIO()
                bg.save(buf, "JPEG", quality=quality, optimize=True)
                out[name] = buf.getvalue()
            return out
    except Exception:                                   # noqa: BLE001
        return None


_looked: "list[float]" = []         # when the lookups of the last hour started


def _spend() -> bool:
    now = time.monotonic()
    _looked[:] = [t for t in _looked if now - t < 3600]
    if len(_looked) >= _HOURLY:
        return False
    _looked.append(now)
    return True


async def _lookup(sub: Subject) -> tuple[dict | None, bool, str]:
    """``(the pictures or None, were there several games, IGDB image id)``."""
    if not _spend():
        raise Unreachable("the hourly number of IGDB lookups is used up")
    games = await _games(sub.game)
    image, several = choose(games, sub)
    if image is None:
        return None, several, ""
    status, body, ctype = await _http("GET", _IMAGE_URL.format(image))
    if status != 200 or not ctype.lower().startswith("image/"):
        if status in (400, 403, 404, 410):
            return None, False, ""
        raise Unreachable(f"IGDB image: status {status}")
    pics = await asyncio.to_thread(_square, body)
    return pics, False, image


# ── What is remembered ───────────────────────────────────────────────────────

_memo: dict[str, dict | None] = {}


def _base(key: str) -> Path:
    from soniqboom.config import get_art_cache_dir
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return get_art_cache_dir() / "igdb" / h[:2] / h


def _read_record(key: str) -> dict | None:
    try:
        rec = json.loads(Path(f"{_base(key)}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("v") == _VER else None


def _fresh(rec) -> bool:
    if not isinstance(rec, dict):
        return False
    if rec.get("ok"):
        return True
    try:
        return time.time() - float(rec.get("at") or 0) < float(rec.get("ttl") or _MISS_TTL)
    except (TypeError, ValueError):
        return False


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


def _write(key: str, rec: dict, pics: dict | None) -> None:
    base = _base(key)
    base.parent.mkdir(parents=True, exist_ok=True)
    for name, data in (pics or {}).items():
        _atomic(Path(f"{base}.{name}.jpg"), data)
    _atomic(Path(f"{base}.json"), json.dumps(rec).encode("utf-8"))


def _memoise(key: str, rec: dict | None) -> None:
    if len(_memo) >= _MAX_MEMO and key not in _memo:
        _memo.clear()
    _memo[key] = rec


async def record(sub: Subject | None) -> dict | None:
    """The valid answer kept for ``sub`` (a cover or a "none"), or None."""
    if sub is None:
        return None
    if sub.key in _memo:
        rec = _memo[sub.key]
    else:
        rec = await asyncio.to_thread(_read_record, sub.key)
        _memoise(sub.key, rec)
    return rec if _fresh(rec) else None


async def _store(key: str, pics: dict | None, *, several=False, soft=False, image="") -> None:
    rec: dict = {"v": _VER, "ok": bool(pics), "at": int(time.time())}
    if pics:
        rec["image"] = image
    elif several:
        rec["amb"] = True
    if soft and not pics:
        rec["ttl"] = _SOFT_TTL
    else:
        try:
            await asyncio.to_thread(_write, key, rec, pics)
        except Exception as exc:                        # noqa: BLE001
            log.warning("IGDB cover: could not store %s: %s", key, exc)
            rec = {"v": _VER, "ok": False, "at": int(time.time()), "ttl": _SOFT_TTL}
    _memoise(key, rec)


async def stored(track) -> tuple[Subject | None, dict | None]:
    """``(track``'s subject, its kept answer)`` — (None, None) when IGDB is not
    set up or the track has no game."""
    if not configured():
        return None, None
    sub = subject(track)
    return sub, await record(sub)


async def image(track, size: str = "full") -> bytes | None:
    """``track``'s stored box cover (``sm`` / ``lg`` / ``full``), or None."""
    sub, rec = await stored(track)
    if not (sub and rec and rec.get("ok")):
        return None
    name = size if size in ("sm", "lg") else "full"
    try:
        return await asyncio.to_thread(Path(f"{_base(sub.key)}.{name}.jpg").read_bytes) or None
    except OSError:
        _memo.pop(sub.key, None)
        return None


def etag(base: str, rec: dict | None) -> str:
    """``base`` (a quoted ETag) marked with a stored box cover."""
    if not (rec and rec.get("ok")) or len(base) < 2 or not base.endswith('"'):
        return base
    return f'{base[:-1]}:b{int(rec.get("at") or 0)}"'


def forget_all() -> None:
    global _gen
    _gen += 1
    _memo.clear()
    _pending.clear()


def forget_misses() -> None:
    """New credentials: every "none" is asked again."""
    global _miss_gen
    _miss_gen += 1
    for key in [k for k, r in list(_memo.items()) if not (r and r.get("ok"))]:
        _memo.pop(key, None)
    try:
        from soniqboom.config import get_art_cache_dir
        for p in (get_art_cache_dir() / "igdb").glob("*/*.json"):
            try:
                rec = json.loads(p.read_text("utf-8"))
                if not (isinstance(rec, dict) and rec.get("ok")):
                    p.unlink()
            except (OSError, ValueError):
                continue
    except Exception:                                   # noqa: BLE001
        log.debug("IGDB forget_misses failed", exc_info=True)


# ── The queue (play / Track Info only) ───────────────────────────────────────

@dataclass
class _Job:
    sub: Subject
    waiters: "OrderedDict[str, None]" = field(default_factory=OrderedDict)


_pending: "OrderedDict[str, _Job]" = OrderedDict()
_current: _Job | None = None
_worker: asyncio.Task | None = None
_paused_until = 0.0
_gen = 0
_miss_gen = 0


def paused() -> bool:
    return time.monotonic() < _paused_until


def pending(sub: Subject | None) -> bool:
    """Is ``sub``'s lookup queued or running?"""
    if sub is None:
        return False
    return sub.key in _pending or (_current is not None and _current.sub.key == sub.key)


async def on_intent(track, track_id: str) -> bool:
    """The track is being played or its Track Info opened: queue its game's
    lookup when nothing is known yet.  True when one is queued or running
    (``art_ready`` follows a cover).  Never for a list row."""
    try:
        if not configured() or _offline() or paused():
            return False
        sub = subject(track)
        if sub is None:
            return False
        if pending(sub):
            job = _current if (_current and _current.sub.key == sub.key) else _pending.get(sub.key)
            if job is not None and track_id and len(job.waiters) < _MAX_WAITERS:
                job.waiters[track_id] = None
            return True
        if await record(sub) is not None:
            return False
        job = _pending.get(sub.key)
        if job is None:
            job = _pending[sub.key] = _Job(sub)
            while len(_pending) > _MAX_PENDING:
                _pending.popitem(last=False)
        if track_id:
            job.waiters[track_id] = None
        _kick()
        return True
    except Exception:                                   # noqa: BLE001
        log.debug("IGDB intent failed", exc_info=True)
        return False


def _kick() -> None:
    global _worker
    w = _worker
    loop = asyncio.get_running_loop()
    if w is not None and not w.done():
        try:
            if w.get_loop() is loop:
                return
        except Exception:                               # noqa: BLE001
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
            if _offline() or not configured():
                _pending.clear()
                return
            await _process(job)
        except AuthRefused as exc:
            _pending.clear()
            _paused_until = time.monotonic() + _AUTH_PAUSE
            log.warning("IGDB covers paused for %d s: %s", int(_AUTH_PAUSE), exc)
        except Unreachable as exc:
            _pending.clear()
            _paused_until = time.monotonic() + _PAUSE
            log.info("IGDB covers paused for %d s: %s", int(_PAUSE), exc)
        except Exception:                               # noqa: BLE001
            log.warning("IGDB lookup failed for %r", job.sub.key, exc_info=True)
            _memoise(job.sub.key, {"v": _VER, "ok": False, "at": int(time.time()), "ttl": _SOFT_TTL})
        finally:
            _current = None


async def _process(job: _Job) -> None:
    gen, miss_gen = _gen, _miss_gen
    pics, several, image_id = await _lookup(job.sub)
    if gen != _gen or (pics is None and miss_gen != _miss_gen):
        return
    await _store(job.sub.key, pics, several=several, image=image_id)
    if pics:
        log.info("IGDB cover for %r (%s)", job.sub.game, "+".join(job.sub.platforms))
        try:
            from soniqboom.api.library import _broadcast
        except Exception:                               # noqa: BLE001
            return
        for tid in list(job.waiters):
            try:
                await _broadcast({"event": "art_ready", "track_id": tid})
            except Exception:                           # noqa: BLE001
                pass


def credentials_changed() -> None:
    """New (or no) credentials: the old token goes, and a pause for refused
    ones ends."""
    global _token, _paused_until
    _token = None
    _paused_until = 0.0


async def test_credentials(client_id: str, secret: str) -> dict:
    """Ask Twitch for a token with this pair: ``{"ok": bool, "error": str}``."""
    if _offline():
        return {"ok": False, "error": "Offline mode is on."}
    try:
        await _access_token(client_id, secret, fresh=True)
        return {"ok": True}
    except AuthRefused:
        return {"ok": False, "error": "Twitch refused this client id and secret."}
    except Unreachable as exc:
        return {"ok": False, "error": f"Twitch could not be reached ({exc})."}


def _reset_for_tests() -> None:
    global _current, _worker, _paused_until, _token, _last_call
    forget_all()
    w, _worker = _worker, None
    if w is not None and not w.done():
        try:
            w.cancel()
        except Exception:                               # noqa: BLE001
            pass
    _current = None
    _paused_until = 0.0
    _token = None
    _last_call = 0.0
    _looked.clear()
