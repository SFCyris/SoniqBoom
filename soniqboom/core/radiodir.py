# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
radiodir.py — internet-radio directory layer.

Three station sources feed the Stations UI:

  * A curated **scene pack** (demoscene / chiptune / SID / game-music
    radios) with stream URLs taken from each station's official site.
  * The community **Radio Browser** directory (api.radio-browser.info,
    public-domain data, no API key) for the world-wide country browser.
  * The server-local **favorites** list (seeded with Nectarine).

Radio Browser etiquette implemented here: a descriptive User-Agent on
every request, results cached on disk so the public mirror isn't hammered
(countries 7 days, per-country station lists 24 h), and play-click
reporting via ``/json/url/{uuid}`` (see :func:`report_click`).

A station that fails every stream is treated as a temporary outage — it is
never blacklisted, so it stays listed and the listener can retry.

All state lives under ``<data_dir>/radio/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import httpx

from soniqboom.config import get_data_dir

log = logging.getLogger("soniqboom.radiodir")

USER_AGENT = "SoniqBoom/1.3 (self-hosted music server; internet-radio)"

# Mirror candidates, walked in order on failure.  The project asks clients
# to discover mirrors via DNS (all.api.radio-browser.info); as of 2026 the
# fleet is a single German host, so a static walk over the known names plus
# the catch-all alias covers both today and a future re-grown fleet.
_RB_HOSTS = (
    "de2.api.radio-browser.info",
    "de1.api.radio-browser.info",
    "all.api.radio-browser.info",
)

_COUNTRIES_TTL = 7 * 86400
_COUNTRY_STATIONS_TTL = 86400
_COUNTRY_STATIONS_CAP = 5000   # safety cap per country (US ≈ 8k raw entries)

# ── Disk layout ───────────────────────────────────────────────────────────────

def _dir() -> Path:
    d = get_data_dir() / "radio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(name: str):
    try:
        return json.loads((_dir() / name).read_text())
    except Exception:
        return None


def _write_json(name: str, data) -> None:
    tmp = _dir() / f"{name}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(_dir() / name)


def _fresh(name: str, ttl: float) -> bool:
    p = _dir() / name
    try:
        return (time.time() - p.stat().st_mtime) < ttl
    except OSError:
        return False


# ── Curated scene pack ────────────────────────────────────────────────────────
# Stream URLs from each station's official site / tune-in playlists (probed
# June 2026).  Streams are listed best-quality-first; the frontend may
# reorder by codec support and steps down the list when playback can't
# keep up.  Favicons are deliberately omitted unless the station publishes
# one at a stable URL — the UI falls back to a glyph.

SCENE_PACK: list[dict] = [
    {
        "sid": "scene:scenesat",
        "name": "SceneSat Radio",
        "homepage": "https://scenesat.com/",
        "tags": "demoscene, amiga, c64",
        "streams": [
            {"url": "http://oscar.scenesat.com:8000/scenesatmax", "codec": "MP3", "bitrate": 320},
            {"url": "http://oscar.scenesat.com:8000/scenesathi", "codec": "AAC+", "bitrate": 128},
            {"url": "http://oscar.scenesat.com:8000/scenesat", "codec": "MP3", "bitrate": 128},
            {"url": "http://oscar.scenesat.com:8000/scenesatmed", "codec": "AAC+", "bitrate": 48},
        ],
    },
    {
        "sid": "scene:nectarine",
        "name": "Nectarine Demoscene Radio",
        "homepage": "https://scenestream.net/",
        "tags": "demoscene, tracker, chiptune",
        "streams": [
            {"url": "http://nectarine.from-de.com/necta192", "codec": "MP3", "bitrate": 192},
            {"url": "http://necta.burn.net:8000/nectarine", "codec": "MP3", "bitrate": 192},
            {"url": "https://scenestream.io/necta128.ogg", "codec": "OGG", "bitrate": 128},
            {"url": "https://scenestream.io/necta64.mp3", "codec": "MP3", "bitrate": 64},
        ],
    },
    {
        "sid": "scene:slayradio",
        "name": "SLAY Radio",
        "homepage": "https://www.slayradio.org/",
        "tags": "c64, sid remixes",
        "streams": [
            {"url": "http://relay4.slayradio.org:8000/", "codec": "MP3", "bitrate": 128},
            {"url": "http://relay1.slayradio.org:8000/", "codec": "MP3", "bitrate": 128},
        ],
    },
    {
        "sid": "scene:kohina",
        "name": "Kohina",
        "homepage": "https://www.kohina.com/",
        "tags": "chiptune, oldschool game music",
        "streams": [
            {"url": "https://player.kohina.com/icecast/stream.aac", "codec": "AAC", "bitrate": 0},
            {"url": "https://kohina.duckdns.org/icecast/stream.ogg", "codec": "OGG", "bitrate": 0},
            {"url": "http://kohina.duckdns.org:8000/stream.ogg", "codec": "OGG", "bitrate": 0},
        ],
    },
    {
        "sid": "scene:paralax",
        "name": "Radio PARALAX",
        "homepage": "https://www.radio-paralax.de/",
        "tags": "game music, demoscene, chiptune remixes",
        "streams": [
            {"url": "https://ssl.radio-paralax.de:8443/;", "codec": "MP3", "bitrate": 192},
            {"url": "http://radio-paralax.de:8000/;", "codec": "MP3", "bitrate": 192},
        ],
    },
    {
        "sid": "scene:cvgm",
        "name": "CVGM.net",
        "homepage": "https://www.cvgm.net/",
        "tags": "video game music, demoscene, requests",
        "streams": [
            {"url": "https://slacker.cvgm.net/cvgm192", "codec": "MP3", "bitrate": 192},
            {"url": "http://stream.cvgm.net:8080/;", "codec": "MP3", "bitrate": 192},
        ],
    },
    {
        "sid": "scene:rainwave-chiptune",
        "name": "Rainwave Chiptunes",
        "homepage": "https://rainwave.cc/chiptune/",
        "tags": "chiptune, game music",
        "streams": [{"url": "https://relay.rainwave.cc/chiptune.mp3", "codec": "MP3", "bitrate": 0}],
    },
    {
        "sid": "scene:rainwave-game",
        "name": "Rainwave Game Music",
        "homepage": "https://rainwave.cc/game/",
        "tags": "game music",
        "streams": [{"url": "https://relay.rainwave.cc/game.mp3", "codec": "MP3", "bitrate": 0}],
    },
    {
        "sid": "scene:rainwave-ocremix",
        "name": "OverClocked ReMix Radio",
        "homepage": "https://rainwave.cc/ocremix/",
        "tags": "game music remixes",
        "streams": [{"url": "https://relay.rainwave.cc/ocremix.mp3", "codec": "MP3", "bitrate": 0}],
    },
    {
        "sid": "scene:keygenfm",
        "name": "Keygen-FM",
        "homepage": "https://keygen.fm/",
        "tags": "keygen music, chiptune",
        "streams": [{"url": "http://stream.keygen-fm.ru:8042/live.ogg", "codec": "OGG", "bitrate": 0}],
    },
    {
        "sid": "scene:cgm-uk",
        "name": "CGM UK DemoScene",
        "homepage": "http://www.lmp.d2g.com/",
        "tags": "demoscene",
        "streams": [
            {"url": "http://www.lmp.d2g.com:8040/;", "codec": "MP3", "bitrate": 256},
            {"url": "http://www.lmp.d2g.com:8020/;", "codec": "AAC+", "bitrate": 64},
        ],
    },
]

# ── Continent mapping (ISO-3166-1 alpha-2 → continent) ───────────────────────

_CONTINENTS: dict[str, str] = {}
for _cont, _codes in {
    "Africa": (
        "DZ AO BJ BW BF BI CM CV CF TD KM CG CD CI DJ EG GQ ER SZ ET GA GM GH"
        " GN GW KE LS LR LY MG MW ML MR MU MA MZ NA NE NG RW ST SN SC SL SO ZA"
        " SS SD TZ TG TN UG EH ZM ZW YT RE SH"
    ),
    "Asia": (
        "AF AM AZ BH BD BT BN KH CN CY GE IN ID IR IQ IL JP JO KZ KW KG LA LB"
        " MY MV MN MM NP KP OM PK PS PH QA SA SG KR LK SY TW TJ TH TL TR TM AE"
        " UZ VN YE HK MO"
    ),
    "Europe": (
        "AL AD AT BY BE BA BG HR CZ DK EE FI FR DE GR HU IS IE IT LV LI LT LU"
        " MT MD MC ME NL MK NO PL PT RO RU SM RS SK SI ES SE CH UA GB VA XK FO"
        " GI GG IM JE AX SJ"
    ),
    "North America": (
        "AG BS BB BZ CA CR CU DM DO SV GD GT HT HN JM MX NI PA KN LC VC TT US"
        " PR GL BM AW CW SX BQ KY TC VG VI MQ GP MF BL PM AI MS"
    ),
    "South America": "AR BO BR CL CO EC GY PY PE SR UY VE FK GF",
    "Oceania": (
        "AU FJ KI MH FM NR NZ PW PG WS SB TO TV VU NC PF GU MP AS CK NU TK WF"
        " PN NF"
    ),
}.items():
    for _c in _codes.split():
        _CONTINENTS[_c] = _cont


def continent_of(code: str) -> str:
    return _CONTINENTS.get((code or "").upper(), "Other")


# ── Radio Browser client ──────────────────────────────────────────────────────

async def _rb_get(path: str, params: dict | None = None):
    """GET from the first Radio Browser mirror that answers."""
    last_exc: Exception | None = None
    for host in _RB_HOSTS:
        try:
            async with httpx.AsyncClient(
                timeout=20.0, headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            ) as client:
                r = await client.get(f"https://{host}{path}", params=params)
                r.raise_for_status()
                return r.json()
        except Exception as exc:           # noqa: BLE001 — walk the mirrors
            last_exc = exc
            continue
    raise RuntimeError(f"all Radio Browser mirrors failed for {path}: {last_exc}")


async def get_countries(force: bool = False) -> list[dict]:
    """Country list with station counts, grouped client-side by continent."""
    if not force and _fresh("countries.json", _COUNTRIES_TTL):
        cached = _read_json("countries.json")
        if cached:
            return cached
    raw = await _rb_get("/json/countries")
    out = [
        {
            "code": c.get("iso_3166_1", ""),
            "name": c.get("name", ""),
            "count": c.get("stationcount", 0),
            "continent": continent_of(c.get("iso_3166_1", "")),
        }
        for c in raw
        if c.get("iso_3166_1") and c.get("stationcount", 0) > 0
    ]
    _write_json("countries.json", out)
    return out


# Multi-quality grouping: RB lists "Station (MP3)" / "Station (AAC+ mobile)"
# as separate entries.  Strip quality decorations from the name to find the
# group key, so the UI shows one row per station with a quality ladder
# inside.  Two passes: drop any (...)/[...] segment that mentions a codec
# or bitrate (whatever else it says — "mobile", "hi", …), then drop bare
# codec/bitrate tokens left in the open.
_CODEC_TOKEN = re.compile(
    r"(?i)\b(mp3|aac\+?|aacp|ogg|vorbis|opus|flac|hls|\d{2,3}\s?(?:k|kbps|kbit))\b")
_PAREN_SEG = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_BARE_DECOR = re.compile(
    r"(?i)[\- ]*\b(mp3|aac\+?|aacp|ogg|vorbis|opus|flac|hls|\d{2,3}\s?(?:k|kbps|kbit))\b")


def _group_key(name: str) -> str:
    base = _PAREN_SEG.sub(
        lambda m: "" if _CODEC_TOKEN.search(m.group(0)) else m.group(0),
        name or "")
    base = _BARE_DECOR.sub(" ", base)
    base = re.sub(r"\s+", " ", base).strip(" -–—_.").lower()
    return base or (name or "").lower()


def _trim_station(s: dict) -> dict:
    return {
        "uuid": s.get("stationuuid"),
        "name": (s.get("name") or "").strip(),
        "url": s.get("url_resolved") or s.get("url") or "",
        "codec": (s.get("codec") or "").upper(),
        "bitrate": s.get("bitrate") or 0,
        "favicon": s.get("favicon") or "",
        "homepage": s.get("homepage") or "",
        "country": s.get("countrycode") or "",
        "tags": s.get("tags") or "",
        "votes": s.get("votes") or 0,
        "clicks": s.get("clickcount") or 0,
    }


def _group_stations(trimmed: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    order: list[str] = []
    for s in trimmed:
        if not s["url"] or not s["uuid"]:
            continue
        key = _group_key(s["name"])
        g = groups.get(key)
        if g is None:
            g = {
                "sid": f"rb:{s['uuid']}",
                "name": s["name"],
                "homepage": s["homepage"],
                "favicon": s["favicon"],
                "country": s["country"],
                "tags": s["tags"],
                "votes": s["votes"],
                "clicks": s["clicks"],
                "streams": [],
            }
            groups[key] = g
            order.append(key)
        g["votes"] = max(g["votes"], s["votes"])
        g["clicks"] = max(g["clicks"], s["clicks"])
        if not g["favicon"] and s["favicon"]:
            g["favicon"] = s["favicon"]
        if not g["homepage"] and s["homepage"]:
            g["homepage"] = s["homepage"]
        g["streams"].append(
            {"url": s["url"], "codec": s["codec"], "bitrate": s["bitrate"], "uuid": s["uuid"]}
        )
    out = []
    for key in order:
        g = groups[key]
        g["streams"].sort(key=lambda x: x["bitrate"], reverse=True)
        out.append(g)
    return out


async def get_country_stations(code: str, force: bool = False) -> list[dict]:
    """All station groups for a country, sorted by votes (desc)."""
    code = (code or "").upper()
    fname = f"country_{code}.json"
    if not force and _fresh(fname, _COUNTRY_STATIONS_TTL):
        cached = _read_json(fname)
        if cached is not None:
            return cached
    raw = await _rb_get(
        f"/json/stations/bycountrycodeexact/{code}",
        params={
            "hidebroken": "true",
            "order": "votes",
            "reverse": "true",
            "limit": str(_COUNTRY_STATIONS_CAP),
        },
    )
    groups = _group_stations([_trim_station(s) for s in raw])
    groups.sort(key=lambda g: (g["votes"], g["clicks"]), reverse=True)
    _write_json(fname, groups)
    return groups


async def search_stations(q: str, limit: int = 30) -> list[dict]:
    """Search stations by name across the scene pack, favorites, and the
    Radio Browser directory.  Local (curated + favorite) matches rank first,
    then directory matches by votes."""
    q = (q or "").strip()
    if not q:
        return []
    ql = q.lower()
    out: list[dict] = []
    seen: set[str] = set()
    for st in SCENE_PACK + get_favorites():
        sid = st.get("sid")
        if not sid or sid in seen:
            continue
        hay = f"{st.get('name', '')} {st.get('tags', '')}".lower()
        if ql in hay:
            out.append(st)
            seen.add(sid)
    try:
        raw = await _rb_get("/json/stations/search", params={
            "name": q, "hidebroken": "true", "order": "votes",
            "reverse": "true", "limit": str(min(limit * 4, 400)),
        })
        rb = _group_stations([_trim_station(s) for s in raw])
        rb.sort(key=lambda g: (g["votes"], g["clicks"]), reverse=True)
        for g in rb:
            if g["sid"] in seen:
                continue
            out.append(g)
            seen.add(g["sid"])
            if len(out) >= limit:
                break
    except Exception as exc:                # noqa: BLE001 — directory optional
        log.debug("Station name search via Radio Browser failed: %s", exc)
    return out[:limit]


async def report_click(uuid: str) -> None:
    """Fire-and-forget play-click report (Radio Browser etiquette)."""
    try:
        await _rb_get(f"/json/url/{uuid}")
    except Exception:                       # noqa: BLE001 — best-effort only
        pass


# ── Favorites ────────────────────────────────────────────────────────────────

_FAVS_FILE = "favorites.json"


def get_favorites() -> list[dict]:
    return _read_json(_FAVS_FILE) or []


def add_favorite(station: dict) -> list[dict]:
    favs = get_favorites()
    if not any(f.get("sid") == station.get("sid") for f in favs):
        favs.append(station)
        _write_json(_FAVS_FILE, favs)
    return favs


def remove_favorite(sid: str) -> list[dict]:
    favs = [f for f in get_favorites() if f.get("sid") != sid]
    _write_json(_FAVS_FILE, favs)
    return favs


def is_favorite(sid: str) -> bool:
    return any(f.get("sid") == sid for f in get_favorites())


# ── Resolution + startup ─────────────────────────────────────────────────────

def _scene_by_sid(sid: str) -> dict | None:
    return next((s for s in SCENE_PACK if s["sid"] == sid), None)


async def resolve_station(sid: str) -> dict | None:
    """Find a station object by sid in scene pack, favorites, country caches,
    or (for rb: sids) a live Radio Browser lookup as the last resort."""
    st = _scene_by_sid(sid)
    if st:
        return st
    for f in get_favorites():
        if f.get("sid") == sid:
            return f
    for p in _dir().glob("country_*.json"):
        try:
            for g in json.loads(p.read_text()):
                if g.get("sid") == sid:
                    return g
        except Exception:
            continue
    if sid.startswith("rb:"):
        uuid = sid[3:]
        try:
            raw = await _rb_get(f"/json/stations/byuuid/{uuid}")
            if raw:
                return _group_stations([_trim_station(raw[0])])[0]
        except Exception:                   # noqa: BLE001
            return None
    return None


async def ensure_ready() -> None:
    """Startup hook: seed favorites with Nectarine on first run and make
    sure the Radio Browser country metadata is on disk (or refreshed)."""
    if _read_json(_FAVS_FILE) is None:
        nectarine = _scene_by_sid("scene:nectarine")
        _write_json(_FAVS_FILE, [nectarine] if nectarine else [])
        log.info("Stations: favorites seeded with Nectarine")
    try:
        countries = await get_countries()
        log.info("Stations: Radio Browser metadata ready (%d countries)", len(countries))
    except Exception as exc:                # noqa: BLE001
        cached = _read_json("countries.json")
        if cached:
            log.warning("Stations: Radio Browser unreachable, serving cached metadata (%d countries): %s",
                        len(cached), exc)
        else:
            log.warning("Stations: Radio Browser unreachable and no cache yet — world browser empty until next retry: %s", exc)
