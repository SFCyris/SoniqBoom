# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Covers for retro tracks: the generated covers (``core/gen_cover.py``) and
the optional IGDB game box covers (``core/igdb_cover.py``) — what is drawn,
determinism and caching (cold and warm, different tracks), where they are
served (``/api/art``, ``/api/art/status``, Subsonic / cast through
``online_cover_for``) with which cache rules, and that an IGDB lookup starts
only on play / Track Info (``POST /api/art/{id}/intent``), never for a list
row, and never without credentials or in offline mode.  No test reaches the
network: IGDB answers come from ``tests/data/igdb`` through an httpx mock
transport."""
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from soniqboom.api import art as A
from soniqboom.config import settings
from soniqboom.core import gen_cover as G
from soniqboom.core import igdb_cover as I

from test_online_cover import settle as settle_online, tr  # noqa: F401
from test_online_cover import w  # noqa: F401  (fixture)
from test_online_cover_wiring import _Req, get, lib  # noqa: F401  (``lib`` is a fixture)

DATA = Path(__file__).parent / "data" / "igdb"


def retro(tid="r1", **kw):
    base = dict(path=f"/m/{tid}.sid", format="SID", artist="Rob Hubbard", album="",
                title="Commando", game="")
    base.update(kw)
    return tr(tid, **base)


def side(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as im:
        return im.size


@pytest.fixture
def gen(monkeypatch, tmp_path):
    """A clean generated-cover cache in ``tmp_path``; ``renders`` counts the
    pictures actually drawn."""
    monkeypatch.setattr(settings, "art_cache_dir", str(tmp_path / "art"))
    G.forget_all()
    G._inflight.clear()
    renders = []
    real = G.render

    def counting(spec, size="lg"):
        renders.append((spec.key, size))
        return real(spec, size)
    monkeypatch.setattr(G, "render", counting)
    monkeypatch.setattr(G, "enabled_now", lambda: True)
    yield SimpleNamespace(renders=renders, root=tmp_path / "art" / "generated")
    G.forget_all()


# ── What is drawn ────────────────────────────────────────────────────────────

def test_only_retro_formats_get_a_generated_cover():
    assert G.spec_for(tr("m1")) is None                         # an MP3
    assert G.spec_for(retro()) is not None
    assert G.spec_for(retro(format="ProTracker")).style == "tracker"
    assert G.spec_for(retro(format="NSF")).style == "cart"
    assert G.spec_for(retro(format="SPC")).style == "cart16"
    assert G.spec_for(retro(format="GBS")).style == "gb"
    assert G.spec_for(retro(format="VGZ")).style == "pcb"
    assert G.spec_for(retro(format="PSF")).style == "disc"
    assert G.spec_for(retro(format="SNDH")).style == "atarist"
    assert G.spec_for(retro(format="AdLib ROL")).style == "dos"
    assert G.spec_for(retro(format="TFMX Pro")).style == "amiga"   # a uade player name


def test_a_spec_is_deterministic_and_a_game_shares_one_cover():
    a = G.spec_for(retro("a", title="Title", game="Turrican II", format="TFMX Pro",
                         artist="Chris Huelsbeck"))
    b = G.spec_for(retro("b", title="Level 3", game="Turrican II", format="TFMX Pro",
                         artist="Chris Huelsbeck"))
    assert a == b                                   # the game's tunes: one cover
    assert a.headline == "Turrican II" and a.composer == "Chris Huelsbeck"
    assert a.tag == "AMIGA - TFMX PRO"
    c = G.spec_for(retro("c", title="Title", game="Turrican", format="TFMX Pro",
                         artist="Chris Huelsbeck"))
    assert c.key != a.key and c.seed != a.seed
    # without a game or album the title is the headline (and part of the key)
    x, y = G.spec_for(retro("x", title="Commando")), G.spec_for(retro("y", title="Delta"))
    assert x.headline == "Commando" and x.key != y.key


def test_text_the_font_lacks_is_left_out_but_still_tells_covers_apart():
    a = G.spec_for(retro("a", format="2SF", title="ドラゴンクエスト", artist="すぎやま"))
    b = G.spec_for(retro("b", format="2SF", title="ファイナル", artist="すぎやま"))
    assert a.headline == "2SF" and a.composer == ""           # nothing printable: the format stands in
    assert a.key != b.key                                       # (keyed by the real names)
    assert G.printable("Mötley Crüe – Æon") == "Motley Crue - AEon"


def test_a_placeholder_artist_is_not_printed():
    assert G.spec_for(retro(artist="Unknown Artist")).composer == ""


def test_render_is_pure_and_sized():
    sp = G.spec_for(retro(game="Commando"))
    for size, px in (("sm", 200), ("lg", 550), ("full", 1200)):
        one, two = G.render(sp, size), G.render(sp, size)
        assert one == two and one[:2] == b"\xff\xd8"            # same bytes, a JPEG
        assert side(one) == (px, px)
    other = G.render(G.spec_for(retro(game="Delta")), "lg")
    assert other != G.render(sp, "lg")


@pytest.mark.parametrize("fmt", ["SID", "ProTracker", "TFMX Pro", "SNDH", "AdLib ROL", "NSF", "SPC",
                                 "GBS", "VGZ", "GYM", "PSF", "USF", "GSF", "AY", "KSS", "SAP", "HES",
                                 "MDX", "MIDI", "V2M", "SSF", "DSF (Dreamcast)", "AHX", "2SF"])
def test_every_style_draws_a_picture_that_is_not_flat(fmt):
    data = G.render(G.spec_for(retro(format=fmt, game="A Long Game Name That Wraps Twice",
                                     artist="Some Composer")), "sm")
    with Image.open(io.BytesIO(data)) as im:
        colours = im.convert("RGB").getcolors(maxcolors=1 << 16)
    assert colours is None or len(colours) > 16                 # (a picture, not one colour)


# ── Caching: cold and warm, different tracks ─────────────────────────────────

async def test_cold_then_warm_with_different_tracks(gen):
    a, b = retro("a", game="Commando"), retro("b", game="Delta", artist="Rob Hubbard")
    pa = await G.cover(a, "lg")                         # cold: drawn and stored
    assert side(pa) == (550, 550) and len(gen.renders) == 1
    assert await G.cover(a, "lg") == pa and len(gen.renders) == 1      # warm: memory
    pb = await G.cover(b, "lg")                         # another track, cold
    assert pb != pa and len(gen.renders) == 2
    assert sorted(p.name.split(".", 1)[1] for p in gen.root.glob("*/*")) == ["lg.jpg", "lg.jpg"]
    G.forget_all()                                      # memory gone: the disk answers
    assert await G.cover(a, "lg") == pa and len(gen.renders) == 2
    # a list thumbnail is drawn (1-2 ms) but never written to disk
    sm = await G.cover(a, "sm")
    assert side(sm) == (200, 200) and len(gen.renders) == 3
    assert not list(gen.root.glob("*/*.sm.jpg"))
    assert await G.cover(a, "sm") == sm and len(gen.renders) == 3


async def test_the_same_picture_asked_at_once_is_drawn_once(gen):
    import asyncio
    t = retro("a", game="Commando")
    out = await asyncio.gather(*(G.cover(t, "full") for _ in range(5)))
    assert len(set(out)) == 1 and out[0]
    assert gen.renders == [(G.spec_for(t).key, "full")]


async def test_the_setting_off_draws_nothing(gen, monkeypatch):
    monkeypatch.setattr(G, "enabled_now", lambda: False)
    assert await G.cover(retro(), "lg") is None and gen.renders == []


# ── /api/art ─────────────────────────────────────────────────────────────────

@pytest.fixture
def rl(lib, gen, monkeypatch):
    """``lib`` (test_online_cover_wiring) with the generated covers on and
    IGDB off."""
    monkeypatch.setattr(settings, "igdb_client_id", "")
    monkeypatch.setattr(settings, "igdb_client_secret", "")
    I._reset_for_tests()
    lib.gen = gen
    yield lib
    I._reset_for_tests()


async def test_a_retro_track_without_a_cover_gets_its_generated_cover(rl):
    rl.tracks["r1"] = retro("r1", game="Commando")
    res = await get("r1", size="sm", fallback="none")
    assert res.status_code == 200 and res.media_type == "image/jpeg"
    assert res.headers["X-SoniqBoom-Art"] == "generated"
    assert res.headers["Cache-Control"] == "public, max-age=60, must-revalidate"
    assert side(res.body) == (200, 200)
    tag = res.headers["ETag"]
    assert ":g" in tag
    again = await get("r1", tag, size="sm")             # the list revalidates: still current
    assert again.status_code == 304 and again.headers["ETag"] == tag
    assert again.headers["Cache-Control"] == "public, max-age=60, must-revalidate"
    # the placeholder form (Subsonic-style callers of /api/art) gets it too
    ph = await get("r1", size="lg", fallback="placeholder")
    assert ph.headers["X-SoniqBoom-Art"] == "generated" and side(ph.body) == (550, 550)


async def test_a_client_holding_the_old_no_cover_answer_gets_the_picture(rl, monkeypatch):
    rl.tracks["r1"] = retro("r1")
    monkeypatch.setattr(G, "enabled_now", lambda: False)
    old = await get("r1")                               # (before the upgrade / setting off)
    assert old.status_code == 204
    monkeypatch.setattr(G, "enabled_now", lambda: True)
    res = await get("r1", old.headers["ETag"])
    assert res.status_code == 200 and res.headers["X-SoniqBoom-Art"] == "generated"
    # and back: switched off, the client holding the picture gets "no cover"
    monkeypatch.setattr(G, "enabled_now", lambda: False)
    off = await get("r1", res.headers["ETag"])
    assert off.status_code == 204


async def test_the_own_cover_takes_over_from_a_held_generated_one(rl, monkeypatch):
    rl.tracks["r1"] = retro("r1")
    gen_tag = (await get("r1")).headers["ETag"]
    rl.cached["r1"] = 99.0                              # its own cover found and cached since

    async def thumb(tid, size):
        return b"\xff\xd8own"
    monkeypatch.setattr(A.art_cache, "get_art", thumb)
    res = await get("r1", gen_tag)
    assert res.status_code == 200 and res.body == b"\xff\xd8own"


async def test_a_mainstream_track_still_answers_no_cover(rl):
    rl.tracks["m1"] = tr("m1", album="", title="Intro", artist="Unknown Artist")
    res = await get("m1")
    assert res.status_code == 204 and res.headers["X-SoniqBoom-Art"] == "none"


async def test_the_tracks_own_cover_comes_first(rl):
    rl.tracks["r1"] = retro("r1")
    rl.own["r1"] = b"\xff\xd8own"
    res = await get("r1")
    assert res.status_code == 200 and res.body == b"\xff\xd8own"
    assert "X-SoniqBoom-Art" not in res.headers


async def test_the_setting_off_brings_back_no_cover(rl, monkeypatch):
    monkeypatch.setattr(G, "enabled_now", lambda: False)
    rl.tracks["r1"] = retro("r1")
    res = await get("r1")
    assert res.status_code == 204


async def test_a_generated_cover_is_asked_again_while_the_own_one_is_on_its_way(rl, monkeypatch):
    rl.tracks["r1"] = retro("r1", path="ftp://nas/m/r1.sid")
    monkeypatch.setattr(A.folder_covers, "pending", lambda t: True)
    res = await get("r1")
    assert res.headers["X-SoniqBoom-Art"] == "generated"
    assert res.headers["Cache-Control"] == "no-cache"


async def test_status_does_not_call_a_retro_row_coverless(rl, monkeypatch):
    rl.tracks["r1"] = retro("r1")
    rl.tracks["m1"] = tr("m1", album="", title="Intro", artist="Unknown Artist")
    monkeypatch.setattr(A, "_no_cover_candidates", lambda ids, use_folder: list(ids))
    monkeypatch.setattr(A, "_use_folder_art_now", lambda: True)
    monkeypatch.setattr(A, "_verify_later", lambda t: None)

    class Body:
        async def json(self):
            return {"ids": ["r1", "m1"]}
    assert (await A.art_status(Body()))["none"] == ["m1"]
    monkeypatch.setattr(G, "enabled_now", lambda: False)
    assert (await A.art_status(Body()))["none"] == ["r1", "m1"]


async def test_subsonic_and_cast_get_the_generated_cover(rl):
    rl.tracks["r1"] = retro("r1", game="Commando")
    await A._resolve_full_art("r1")                     # (marks "no cover of its own")
    data, may_come = await A.online_cover_for("r1", "lg")
    assert side(data) == (550, 550) and may_come is False
    from soniqboom.core import cast_session
    assert side(await cast_session._resolve_and_cap_art("r1"))[0] > 0


# ── IGDB: matching (from the fixture) ────────────────────────────────────────

def _fixture(name):
    return json.loads((DATA / "games-search.json").read_text())[name]


def test_igdb_subject_needs_a_retro_game_on_a_known_platform():
    assert I.subject(retro(game="")) is None
    assert I.subject(tr("m1", game="Turrican II")) is None              # not retro
    sub = I.subject(retro(format="TFMX Pro", game="Turrican II", game_aliases=["Turrican 2"]))
    assert sub.platforms == ("amiga",) and sub.key == "amiga|turrican 2"
    assert "turrican 2" in sub.names


def test_igdb_matches_the_exact_game_on_the_platform():
    sub = I.subject(retro(format="TFMX Pro", game="Turrican II"))
    # "Turrican II" is an alternative name of 2734; 2735 is a version of it
    assert I.choose(_fixture("turrican_ii"), sub) == ("co1abc", False)
    # another name ("Turrican") is not this game
    sub1 = I.subject(retro(format="TFMX Pro", game="Turrican"))
    assert I.choose(_fixture("turrican_ii"), sub1) == ("co1abe", False)
    # not on the track's platform: none
    snes = I.subject(retro(format="SPC", game="Chrono Trigger"))
    assert I.choose(_fixture("other_platform"), snes) == (None, False)


def test_igdb_two_different_games_of_one_name_are_refused():
    sub = I.subject(retro(format="TFMX Pro", game="Outrun"))
    assert I.choose(_fixture("two_games"), sub) == (None, True)


def test_igdb_pictures_are_squared_without_cutting():
    buf = io.BytesIO()
    Image.new("RGB", (264, 374), (200, 30, 30)).save(buf, "JPEG")
    pics = I._square(buf.getvalue())
    assert {k: side(v) for k, v in pics.items()} == {"full": (1200, 1200), "lg": (550, 550), "sm": (200, 200)}
    with Image.open(io.BytesIO(pics["lg"])) as im:
        assert im.getpixel((275, 275))[0] > 150             # the cover in the middle
        assert im.getpixel((5, 275))[0] < 150               # darkened blur at the sides


# ── IGDB: when it is asked ───────────────────────────────────────────────────

def _cover_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (528, 748), (30, 160, 60)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def igdb(rl, monkeypatch):
    """IGDB set up, its services answered by a mock transport."""
    monkeypatch.setattr(settings, "igdb_client_id", "clientid123")
    monkeypatch.setattr(settings, "igdb_client_secret", "secret456789")
    monkeypatch.setattr(I, "_offline", lambda: False)
    monkeypatch.setattr(I, "_MIN_GAP", 0.0)
    n = SimpleNamespace(seen=[], games=_fixture("turrican_ii"))

    def handle(req: httpx.Request) -> httpx.Response:
        n.seen.append(req)
        if req.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 5000000,
                                             "token_type": "bearer"})
        if req.url.host == "api.igdb.com":
            return httpx.Response(200, json=n.games)
        if req.url.host == "images.igdb.com":
            return httpx.Response(200, content=_cover_jpeg(), headers={"content-type": "image/jpeg"})
        return httpx.Response(404)
    monkeypatch.setattr(I, "_transport", httpx.MockTransport(handle))
    rl.igdb = n
    return rl


async def settle_igdb():
    while I._worker is not None and not I._worker.done():
        await I._worker


async def test_a_list_row_never_starts_an_igdb_lookup(igdb):
    igdb.tracks["r1"] = retro("r1", format="TFMX Pro", game="Turrican II")
    for size in ("sm", "lg", "full"):
        res = await get("r1", size=size)
        assert res.headers["X-SoniqBoom-Art"] == "generated"
    await A.online_cover_for("r1", "sm")                # (Subsonic / cast too)
    await settle_igdb()
    assert igdb.igdb.seen == [] and not I._pending


async def test_play_or_track_info_looks_the_game_up_once(igdb):
    t = igdb.tracks["r1"] = retro("r1", format="TFMX Pro", game="Turrican II")
    await A.cover_art("r1", _Req(), size="sm", fallback="none")
    assert (await A.art_intent("r1")) == {"queued": True}
    await settle_igdb()
    hosts = [r.url.host for r in igdb.igdb.seen]
    assert hosts == ["id.twitch.tv", "api.igdb.com", "images.igdb.com"]
    games = igdb.igdb.seen[1]
    assert games.headers["client-id"] == "clientid123" and games.headers["authorization"] == "Bearer tok"
    assert b'search "Turrican II"' in games.content
    assert str(igdb.igdb.seen[2].url).endswith("/t_1080p/co1abc.jpg")
    assert igdb.w.sent[-1] == {"event": "art_ready", "track_id": "r1"}
    # now every list row of the game shows the box cover
    igdb.tracks["r2"] = retro("r2", format="TFMX Pro", game="Turrican II", title="Level 2")
    res = await get("r2")
    assert res.headers["X-SoniqBoom-Art"] == "boxart" and res.headers["Cache-Control"] == "no-cache"
    assert side(res.body) == (200, 200)
    tag = res.headers["ETag"]
    assert ":b" in tag and (await get("r2", tag)).status_code == 304
    # a second intent asks nothing (the answer is known)
    assert (await A.art_intent("r2")) == {"queued": False}
    await settle_igdb()
    assert len(igdb.igdb.seen) == 3
    data, may = await A.online_cover_for("r1", "lg")
    assert side(data) == (550, 550) and may is False
    assert t.game == "Turrican II"


async def test_a_game_igdb_does_not_know_keeps_the_generated_cover(igdb):
    igdb.igdb.games = _fixture("two_games")
    igdb.tracks["r1"] = retro("r1", format="TFMX Pro", game="Outrun")
    await A.art_intent("r1")
    await settle_igdb()
    assert [r.url.host for r in igdb.igdb.seen] == ["id.twitch.tv", "api.igdb.com"]
    assert (await get("r1")).headers["X-SoniqBoom-Art"] == "generated"
    assert (await A.art_intent("r1")) == {"queued": False}     # the "none" is remembered


async def test_box_cover_then_credentials_removed_back_to_the_generated_one(igdb, monkeypatch):
    igdb.tracks["r1"] = retro("r1", format="TFMX Pro", game="Turrican II")
    await A.cover_art("r1", _Req(), size="sm", fallback="none")
    gen_tag = (await get("r1")).headers["ETag"]
    await A.art_intent("r1")
    await settle_igdb()
    box = await get("r1", gen_tag)                      # holding the generated one: the box comes
    assert box.status_code == 200 and box.headers["X-SoniqBoom-Art"] == "boxart"
    monkeypatch.setattr(settings, "igdb_client_secret", "")
    back = await get("r1", box.headers["ETag"])
    assert back.status_code == 200 and back.headers["X-SoniqBoom-Art"] == "generated"


async def test_an_online_collection_tune_shows_its_box_cover(igdb, monkeypatch):
    from soniqboom.core import catalogue
    monkeypatch.setattr(catalogue, "is_catalogue_path", lambda p: str(p).startswith("https://"))
    igdb.tracks["c1"] = retro("c1", format="TFMX Pro", game="Turrican II",
                              path="https://ftp.modland.invalid/pub/modules/TFMX/x/mdat.t2")
    igdb.unreadable.add("c1")                           # (a remote file: no "no art" mark)
    await A.art_intent("c1")
    await settle_igdb()
    res = await get("c1")
    assert res.headers["X-SoniqBoom-Art"] == "boxart"
    data, may = await A.online_cover_for("c1", "sm")
    assert side(data) == (200, 200) and may is False


async def test_the_token_request_keeps_the_secret_out_of_the_url(igdb):
    igdb.tracks["r1"] = retro("r1", format="TFMX Pro", game="Turrican II")
    await A.art_intent("r1")
    await settle_igdb()
    tok = igdb.igdb.seen[0]
    assert "secret" not in str(tok.url) and b"client_secret=secret456789" in tok.content
    # a second game reuses the token
    igdb.tracks["r2"] = retro("r2", format="TFMX Pro", game="Turrican")
    await A.art_intent("r2")
    await settle_igdb()
    assert [r.url.host for r in igdb.igdb.seen].count("id.twitch.tv") == 1


@pytest.mark.parametrize("change", ["no_credentials", "offline", "no_game", "not_retro"])
async def test_no_igdb_request_without_what_it_needs(igdb, monkeypatch, change):
    kw = dict(format="TFMX Pro", game="Turrican II")
    if change == "no_credentials":
        monkeypatch.setattr(settings, "igdb_client_secret", "")
    elif change == "offline":
        monkeypatch.setattr(I, "_offline", lambda: True)
    elif change == "no_game":
        kw["game"] = ""
    else:
        kw["format"] = "MP3"
    igdb.tracks["r1"] = retro("r1", **kw)
    assert (await A.art_intent("r1")) == {"queued": False}
    await settle_igdb()
    assert igdb.igdb.seen == []


async def test_refused_credentials_pause_the_lookups(igdb, monkeypatch):
    def handle(req):
        igdb.igdb.seen.append(req)
        return httpx.Response(400, json={"message": "invalid client"})
    monkeypatch.setattr(I, "_transport", httpx.MockTransport(handle))
    igdb.tracks["r1"] = retro("r1", format="TFMX Pro", game="Turrican II")
    await A.art_intent("r1")
    await settle_igdb()
    assert len(igdb.igdb.seen) == 1 and I.paused()
    igdb.tracks["r2"] = retro("r2", format="SPC", game="Chrono Trigger")
    assert (await A.art_intent("r2")) == {"queued": False}
    I.credentials_changed()                                 # new credentials: asked again
    assert not I.paused()


# ── Settings: the IGDB credentials, the two switches ─────────────────────────

@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from soniqboom import config
    from soniqboom.api import admin, library
    conf = tmp_path / "SoniqBoom.conf"
    conf.write_text(json.dumps({"radio_art_lookup": True}))
    real = config.update_local_conf
    monkeypatch.setattr(config, "update_local_conf", lambda fn: real(fn, path=conf))
    monkeypatch.setattr(settings, "igdb_client_id", "")
    monkeypatch.setattr(settings, "igdb_client_secret", "")
    monkeypatch.delenv("SONIQBOOM_IGDB_CLIENT_ID", raising=False)
    monkeypatch.delenv("SONIQBOOM_IGDB_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(I, "forget_misses", lambda: None)
    sent = []

    async def broadcast(msg):
        sent.append(msg)
    monkeypatch.setattr(library, "_broadcast", broadcast)
    app = FastAPI()
    app.include_router(admin.router, prefix="/api")
    app.dependency_overrides[admin._require_token] = lambda: "t"
    c = TestClient(app)
    c.conf, c.sent = conf, sent
    return c


def test_igdb_credentials_are_saved_encrypted_and_never_sent_back(admin_client):
    c = admin_client
    assert c.get("/api/admin/igdb").json()["set"] is False
    r = c.put("/api/admin/igdb", json={"client_id": "abcd1234efgh", "client_secret": "SeCrEt0987654321"})
    assert r.status_code == 200 and r.json()["set"] is True and r.json()["client_id"] == "abcd1234efgh"
    assert "SeCrEt0987654321" not in r.text and "SeCrEt0987654321" not in c.get("/api/admin/igdb").text
    stored = json.loads(c.conf.read_text())
    assert "SeCrEt0987654321" not in c.conf.read_text() and "igdb_client_secret" not in stored
    from soniqboom.core.credentials import decrypt
    assert decrypt(stored["igdb_client_secret_enc"]) == "SeCrEt0987654321"
    assert I.configured() and c.sent == [{"event": "art_refresh"}]
    assert c.put("/api/admin/igdb", json={"client_id": "x", "client_secret": "y"}).status_code == 400
    r = c.put("/api/admin/igdb", json={"client_id": "", "client_secret": ""})
    assert r.json()["set"] is False and not I.configured()
    assert "igdb_client_id" not in json.loads(c.conf.read_text())


def test_igdb_test_without_credentials_asks_nothing(admin_client, monkeypatch):
    called = []

    async def tc(cid, secret):
        called.append(cid)
        return {"ok": True}
    monkeypatch.setattr(I, "test_credentials", tc)
    assert admin_client.post("/api/admin/igdb/test", json={}).json()["ok"] is False and called == []
    assert admin_client.post("/api/admin/igdb/test", json={"client_id": "abcd1234efgh",
                                                           "client_secret": "SeCrEt0987654321"}).json()["ok"]
    assert called == ["abcd1234efgh"]


async def test_the_switches_default_on_and_a_change_repaints_covers(monkeypatch):
    from soniqboom.api import admin, library
    from soniqboom.core import csdb
    cfg: dict = {}
    sent = []
    from soniqboom.core import data, store

    async def get_config(key, default=None):
        return cfg.get(key, default)

    async def set_config(key, value):
        cfg[key] = value
    monkeypatch.setattr(data, "get_config", get_config)
    monkeypatch.setattr(data, "set_config", set_config)
    monkeypatch.setattr(store, "get_store", lambda: SimpleNamespace(get_config=lambda k, d=None: cfg.get(k, d)))

    async def broadcast(msg):
        sent.append(msg)
    monkeypatch.setattr(library, "_broadcast", broadcast)
    assert G.enabled_now() is True and await csdb.setting_value() is True
    await admin.update_settings({"generated_covers": False, "csdb_lookup": False}, "t")
    assert cfg == {G.SETTING: False, csdb.SETTING: False}
    assert G.enabled_now() is False and await csdb.setting_value() is False
    assert sent == [{"event": "art_refresh"}]
    await admin.update_settings({"generated_covers": False}, "t")      # unchanged: no repaint
    assert sent == [{"event": "art_refresh"}]
