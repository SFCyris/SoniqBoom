# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub #40 — CrowdSec banned a listener after a burst of 404s.

Scrolling past tracks without cover art made the web app ask for each one's
thumbnail with ``fallback=404`` and the server answered 404; CrowdSec's
``http-probing`` bans a client after 10 distinct 404 paths.  "Nothing here
for this track" is a state, not an error:

* no cover art → ``204 No Content`` (an IMG fires ``error`` for it exactly
  as for the 404, so the format icon still shows), same caching headers;
* the web app asks for covers at ``/api/art/{id}/{size}.jpg`` — a static
  file extension, which CrowdSec's scenarios skip (``http-crawl-non_statics``
  counts successful requests too);
* no per-voice meters / no waveform / no now-playing cover → 204 with the
  reason in a header;
* an unknown track or size is still a 404.
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import art

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "soniqboom" / "frontend"


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.fixture()
def art_app(monkeypatch):
    """The art router over one track with art (``has``) and one without."""
    async def get_art(tid, size):
        return b"\xff\xd8THUMB" if tid == "has" else None

    async def full_art(tid):
        return (b"\xff\xd8FULL", "image/jpeg") if tid == "has" else (None, None)

    async def get_track(tid):
        return None

    monkeypatch.setattr(art.art_cache, "get_art", get_art)
    monkeypatch.setattr(art, "_resolve_full_art", full_art)
    monkeypatch.setattr(art, "_art_cached_mtime", lambda *a: 1.0)
    monkeypatch.setattr(art, "get_track", get_track)
    app = FastAPI()
    app.include_router(art.router, prefix="/api")
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "/api/art/none/sm.jpg?fallback=none",
    "/api/art/none/lg.jpg?fallback=none",
    "/api/art/none/full.jpg?fallback=none",
    "/api/art/none?size=sm&fallback=none",
    "/api/art/none?size=sm&fallback=404",          # the old name: same answer
    "/api/art/none/sm.jpg?fallback=404",
])
async def test_no_art_is_a_cacheable_204(art_app, url):
    async with _client(art_app) as c:
        r = await c.get(url)
    assert r.status_code == 204
    assert r.content == b""
    assert r.headers["x-soniqboom-art"] == "none"
    assert r.headers["cache-control"] == "public, max-age=60, must-revalidate"
    assert r.headers.get("etag")


@pytest.mark.asyncio
async def test_no_art_revalidates_with_a_304(art_app):
    async with _client(art_app) as c:
        first = await c.get("/api/art/none/sm.jpg?fallback=none")
        again = await c.get("/api/art/none/sm.jpg?fallback=none",
                            headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


@pytest.mark.asyncio
async def test_the_jpg_path_serves_the_cover_and_the_placeholder(art_app):
    async with _client(art_app) as c:
        has = await c.get("/api/art/has/sm.jpg?fallback=none")
        old = await c.get("/api/art/has?size=sm&fallback=none")
        ph = await c.get("/api/art/none/sm.jpg")              # default: placeholder
    assert has.status_code == 200 and has.content == b"\xff\xd8THUMB"
    assert old.content == has.content
    assert ph.status_code == 200 and ph.headers["x-soniqboom-art"] == "placeholder"


@pytest.mark.asyncio
async def test_an_unknown_size_is_still_an_error(art_app):
    async with _client(art_app) as c:
        r = await c.get("/api/art/has/huge.jpg")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_missing_now_playing_cover_is_a_204(monkeypatch):
    from soniqboom.api import stations
    from soniqboom.core import nowplaying_art
    monkeypatch.setattr(nowplaying_art, "read_cover", lambda slug: None)
    app = FastAPI()
    app.include_router(stations.router, prefix="/api")
    async with _client(app) as c:
        r = await c.get("/api/stations/nowplaying-art/x")
    assert r.status_code == 204


# ── VU and waveform: "nothing here" is a 204 with the reason ─────────────────

class _T:
    def __init__(self, tid, path, fmt="MP3"):
        self.id, self.path, self.duration = tid, str(path), 0.0
        self.format, self.title, self.genre = fmt, "t", []


@pytest.mark.asyncio
async def test_waveform_unavailable_is_a_204(monkeypatch):
    """A track inside an archive whose member can't be extracted."""
    from starlette.responses import Response
    from soniqboom.api import tracks as tracks_api
    from soniqboom.core import data as data_mod
    tr = _T("wz", "/music/a.zip::song.mp3")

    async def get_track(tid):
        return tr if tid == "wz" else None

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(data_mod, "get_track", get_track)
    monkeypatch.setattr(data_mod, "get_waveform", nothing)
    monkeypatch.setattr(tracks_api, "_resolve_zip_member_to_local", nothing)
    res = await tracks_api.get_track_waveform("wz", Response(), subsong=0)
    assert res.status_code == 204 and res.body == b""
    assert res.headers["x-waveform-unavailable"] == "format"


@pytest.mark.asyncio
async def test_unreadable_vu_sidecar_is_a_204(monkeypatch, tmp_path):
    from soniqboom.api import tracks as tracks_api
    tr = _T("vx", tmp_path / "m.mod", fmt="MOD")

    async def get_track(tid):
        return tr if tid == "vx" else None
    monkeypatch.setattr(tracks_api, "get_track", get_track)
    gone = tmp_path / "vx.vumr"                          # a sidecar that can't be read
    monkeypatch.setattr("soniqboom.core.conversion_cache.get_vu_sidecar_path",
                        lambda *a, **k: gone)
    res = await tracks_api.get_vu_sidecar("vx", subsong=None, start=True, request=None)
    assert res.status_code == 204 and res.body == b""


# ── The web app ───────────────────────────────────────────────────────────────

def _frontend_js() -> dict[str, str]:
    return {str(p.relative_to(FRONTEND)): p.read_text(encoding="utf-8")
            for p in (FRONTEND / "js").rglob("*.js")}


def test_every_art_image_uses_the_static_path():
    """No ``fallback=404`` left, and every no-art IMG asks at ``…/{size}.jpg``."""
    srcs = _frontend_js()
    leftovers = [f for f, s in srcs.items() if "fallback=404" in s]
    assert leftovers == []
    builders = [(f, m.group(0)) for f, s in srcs.items()
                for m in re.finditer(r"/api/art/\$\{[^}]+\}[^`'\"\s]*fallback=none", s)]
    assert len(builders) >= 16                     # desktop, phone, multi-room
    assert all(re.search(r"\}/(sm|lg)\.jpg\?fallback=none", b) for _, b in builders), builders


def test_stored_cover_urls_go_through_the_static_path():
    """``cover_art`` (``/api/art/{id}`` from the scanner) is rewritten to the
    ``.jpg`` form wherever an IMG uses it."""
    srcs = _frontend_js()
    raw = [(f, m.group(0)) for f, s in srcs.items()
           for m in re.finditer(r"(?<!staticArtUrl\()\b(?:track|t)\.cover_art\s*(\|\||;)", s)]
    assert raw == []
    assert all("/api/art/${" not in m.group(0)
               for s in srcs.values() for m in re.finditer(r"/api/art/\$\{[^}]+\}\?size=", s))


def test_vu_and_waveform_readers_treat_204_as_nothing_here():
    s = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    assert "if (!res.ok || res.status === 204) {\n      const reason = res.headers.get('X-VU-Unavailable');" in s
    assert "if (!res.ok || res.status === 204) {          // 204: no waveform for this track" in s


def test_art_refresh_matches_the_static_path():
    s = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    assert "u.pathname !== want && !u.pathname.startsWith(`${want}/`)" in s
