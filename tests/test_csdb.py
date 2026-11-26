# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CSDb data for SIDs in Track Info (``core/csdb.py``, ``GET /api/scene/csdb``):
parsing real web-service answers (``tests/data/csdb``, captured 2026-11-25
from https://csdb.dk/webservice/ — user comments and trivia cut out), how a
tune's CSDb id is found (the Demozoo index, else the Scene tab's hint) and
checked, the gating (setting off, not a SID, no id known → no request), the
caching (cold / warm with different tunes), the pause after a failure, the
User-Agent, and the screenshot.  No test reaches the network: an httpx mock
transport answers."""
from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from soniqboom.api import artist as ART
from soniqboom.config import settings
from soniqboom.core import csdb as C

DATA = Path(__file__).parent / "data" / "csdb"
SID1 = (DATA / "sid-1-depth2.xml").read_bytes()
SID20901 = (DATA / "sid-20901-depth3.xml").read_bytes()
UNKNOWN = (DATA / "unknown-parameter.xml").read_bytes()


def sid(tid="s1", **kw):
    d = dict(id=tid, path=f"/hvsc/MUSICIANS/M/Moon/{tid}.sid", format="SID", title="Best",
             artist="Moon", sid_md5=None)
    d.update(kw)
    return SimpleNamespace(**d)


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_parse_a_depth_3_answer():
    s = C.parse_sid(SID20901)
    assert (s["id"], s["name"], s["author"], s["hvsc_path"]) == (
        20901, "Best", "Anders Elmén (Moon)", "/MUSICIANS/M/Moon/Best.sid")
    assert s["released"] == "1990 Flash Inc."
    rels = {r["id"]: r for r in s["releases"]}
    assert list(rels) == [11589, 653, 4212, 9103, 253480]
    m = rels[11589]
    assert (m["name"], m["type"], m["year"], m["groups"], m["event"], m["compo"], m["place"], m["rating"]) \
        == ("Mist II", "C64 Demo", 1990, ["Vision"], "Censor Halloween Party 1990", "C64 Demo", 2, 8.5)
    assert m["shot"] == "https://csdb.dk/gfx/releases/11000/11589.png"
    # a group CSDb printed in full only once is named by its id elsewhere
    assert rels[4212]["groups"] == ["Flash Inc."]
    assert rels[653]["rating"] is None and rels[253480]["year"] is None


def test_parse_a_depth_2_answer_has_no_groups():
    s = C.parse_sid(SID1)
    assert s["hvsc_path"] == "/DEMOS/0-9/128_Byte_Blues_BASIC.sid"
    r = s["releases"][0]
    assert (r["name"], r["place"], r["compo"], r["groups"]) == ("128 Byte Blues", 9, "C64 Music", [])


def test_parse_refuses_what_is_no_sid_answer():
    assert C.parse_sid(UNKNOWN) is None                     # (an unknown parameter: the newest id)
    assert C.parse_sid(b"<not xml") is None
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><CSDbData><SID><ID>1</ID></SID></CSDbData>'
    assert C.parse_sid(bomb) is None


def test_public_payload_links_and_the_best_rated_screenshot():
    p = C._public(C.parse_sid(SID20901))
    assert p["url"] == "https://csdb.dk/sid/?id=20901"
    assert p["releases"][0]["url"] == "https://csdb.dk/release/?id=11589"
    assert p["top_rating"] == 8.5
    assert p["screenshot"] == "/api/scene/csdb/shot/11589.png" and p["screenshot_release"] == 11589
    assert "shot" not in p["releases"][0]                   # (the csdb.dk URL stays on the server)


# ── Confidence ───────────────────────────────────────────────────────────────

def test_confident_by_hvsc_path_else_by_title_and_composer():
    s = C.parse_sid(SID20901)
    assert C.confident(s, sid(), "/MUSICIANS/M/Moon/Best.sid")
    assert C.confident(s, sid(), "musicians/m/moon/best.sid")          # (case, slash)
    assert not C.confident(s, sid(), "/MUSICIANS/M/Moon/Other.sid")
    assert C.confident(s, sid(), None)                      # no HVSC path: title + composer
    assert not C.confident(s, sid(title="Best Of"), None)
    assert not C.confident(s, sid(artist="JCH"), None)
    assert not C.confident(s, sid(artist=""), None)
    assert not C.confident(None, sid(), "/MUSICIANS/M/Moon/Best.sid")


# ── info_for: ids, gating, caching ───────────────────────────────────────────

def _demozoo_db(path: Path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE music_prod (id INTEGER PRIMARY KEY, title TEXT)")
    con.execute("CREATE TABLE prod_link (prod_id INTEGER, cls TEXT, param TEXT, download INTEGER)")
    con.executemany("INSERT INTO prod_link VALUES (?, ?, ?, 0)", rows)
    con.commit()
    con.close()


@pytest.fixture
def cw(monkeypatch, tmp_path):
    """CSDb with a Demozoo index naming two tunes, the HVSC path of a track
    from its ``hvsc`` attribute, and CSDb answered by a mock transport."""
    C._reset_for_tests()
    monkeypatch.setattr(settings, "art_cache_dir", str(tmp_path / "art"))
    db = tmp_path / "demozoo.sqlite"
    _demozoo_db(db, [
        (10, "DeepSIDTrack", "/MUSICIANS/M/Moon/Best.sid"), (10, "CsdbMusic", "20901"),
        (11, "DeepSIDTrack", "DEMOS/0-9/128_Byte_Blues_BASIC.sid"), (11, "CsdbMusic", "1"),
        (12, "DeepSIDTrack", "/MUSICIANS/X/Two_Ids.sid"), (12, "CsdbMusic", "5"),
        (13, "DeepSIDTrack", "/MUSICIANS/X/Two_Ids.sid"), (13, "CsdbMusic", "6"),
        (14, "CsdbMusic", "777"),                            # no DeepSID link: not by path
    ])
    from soniqboom.core import demozoo
    monkeypatch.setattr(demozoo, "_db_path", lambda: db)
    monkeypatch.setattr(C, "hvsc_path", lambda t: getattr(t, "hvsc", None))
    monkeypatch.setattr(C, "_offline", lambda: False)
    st = SimpleNamespace(setting=True, seen=[], answers={20901: SID20901, 1: SID1}, fail=False)

    async def get_config(key, default=None):
        assert key == C.SETTING
        return st.setting
    from soniqboom.core import data
    monkeypatch.setattr(data, "get_config", get_config)

    def handle(req: httpx.Request) -> httpx.Response:
        st.seen.append(req)
        if st.fail:
            return httpx.Response(503)
        if req.url.host == "csdb.dk" and req.url.path == "/webservice/":
            n = int(req.url.params["id"])
            return httpx.Response(200, content=st.answers.get(n, UNKNOWN),
                                  headers={"content-type": "text/xml"})
        if req.url.path.startswith("/gfx/releases/"):
            buf = io.BytesIO()
            Image.new("P", (320, 200), 6).save(buf, "GIF" if req.url.path.endswith(".gif") else "PNG")
            return httpx.Response(200, content=buf.getvalue(), headers={"content-type": "image/png"})
        return httpx.Response(404)
    monkeypatch.setattr(C, "_transport", httpx.MockTransport(handle))
    yield st
    C._reset_for_tests()


async def test_known_by_the_demozoo_index_cold_then_warm_different_tunes(cw):
    a = sid("a", hvsc="/MUSICIANS/M/Moon/Best.sid")
    b = sid("b", title="128 Byte Blues", artist="Leonard J. Paul", hvsc="/DEMOS/0-9/128_Byte_Blues_BASIC.sid")
    ra = await C.info_for(a)                                # cold
    assert ra["found"] is True and ra["id"] == 20901 and len(ra["releases"]) == 5
    assert [str(r.url) for r in cw.seen] == ["https://csdb.dk/webservice/?type=sid&id=20901&depth=3"]
    ua = cw.seen[0].headers["user-agent"]
    assert ua.startswith("SoniqBoom/") and " " not in ua                # nothing personal
    assert set(cw.seen[0].headers) <= {"host", "accept", "accept-encoding", "connection", "user-agent"}
    rb = await C.info_for(b)                                # another tune, cold
    assert rb["found"] is True and rb["id"] == 1 and len(cw.seen) == 2
    assert (await C.info_for(a))["id"] == 20901 and len(cw.seen) == 2   # warm: memory
    C._memo.clear()
    assert (await C.info_for(b))["id"] == 1 and len(cw.seen) == 2       # warm: the kept file


@pytest.mark.parametrize("case", ["off", "not_sid", "unknown", "ambiguous_index"])
async def test_no_request_when_there_is_nothing_to_ask(cw, case):
    t = sid(hvsc="/MUSICIANS/M/Moon/Best.sid")
    if case == "off":
        cw.setting = False
    elif case == "not_sid":
        t.format = "ProTracker"
    elif case == "unknown":
        t.hvsc = "/MUSICIANS/Q/Nobody/None.sid"
    else:
        t.hvsc = "/MUSICIANS/X/Two_Ids.sid"                 # two CSDb ids: neither is trusted
    r = await C.info_for(t)
    assert r["found"] is False and r["reason"] == {"off": "off", "not_sid": "not_sid"}.get(case, "unknown")
    assert cw.seen == []


async def test_a_hint_is_used_only_when_the_index_names_none_and_must_match(cw):
    t = sid("t", hvsc=None)                                 # HVSC doesn't know it: title + composer
    r = await C.info_for(t, 20901)
    assert r["found"] is True and r["id"] == 20901
    wrong = sid("w", title="Something Else", hvsc=None)
    assert (await C.info_for(wrong, 20901)) == {"found": False, "reason": "no_match"}
    assert len(cw.seen) == 1                                # (the same tune: kept)
    # the index wins over a hint
    k = sid("k", hvsc="/MUSICIANS/M/Moon/Best.sid")
    assert (await C.info_for(k, 1))["id"] == 20901


async def test_an_id_csdb_does_not_know_is_no_match(cw):
    t = sid("t", hvsc=None)
    assert (await C.info_for(t, 424242)) == {"found": False, "reason": "no_match"}
    assert len(cw.seen) == 1


async def test_a_failure_pauses_every_csdb_request(cw):
    cw.fail = True
    a = sid("a", hvsc="/MUSICIANS/M/Moon/Best.sid")
    assert (await C.info_for(a)) == {"found": False, "reason": "unavailable"}
    assert len(cw.seen) == 1 and C.paused()
    b = sid("b", title="128 Byte Blues", artist="Leonard J. Paul", hvsc="/DEMOS/0-9/128_Byte_Blues_BASIC.sid")
    assert (await C.info_for(b))["reason"] == "unavailable"
    assert len(cw.seen) == 1                                # (paused: not asked)


async def test_an_error_page_is_no_answer_and_pauses(cw, tmp_path):
    cw.answers[20901] = b"<html><body>Down for maintenance</body></html>"
    a = sid("a", hvsc="/MUSICIANS/M/Moon/Best.sid")
    assert (await C.info_for(a))["reason"] == "unavailable"
    assert C.paused() and not list((tmp_path / "art").rglob("sid-*.json"))


def test_an_entity_in_another_encoding_is_refused():
    body = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
            '<CSDbData><SID><ID>1</ID><Name>&a;</Name></SID></CSDbData>').encode("utf-16")
    assert C.parse_sid(body) is None


def test_group_ratings_come_with_the_release():
    rels = {r["id"]: r for r in C.parse_sid(SID20901)["releases"]}
    assert rels[11589]["group_rating"] == {"name": "Vision", "rating": 8.43}
    assert C._public(C.parse_sid(SID20901))["releases"][0]["group_rating"]["rating"] == 8.43


async def test_offline_mode_asks_nothing(cw, monkeypatch):
    monkeypatch.setattr(C, "_offline", lambda: True)
    assert (await C.info_for(sid(hvsc="/MUSICIANS/M/Moon/Best.sid")))["reason"] == "unavailable"
    assert cw.seen == []


async def test_the_endpoint_and_the_screenshot(cw, monkeypatch):
    t = sid("s1", hvsc="/MUSICIANS/M/Moon/Best.sid")

    async def get_track(tid):
        return t if tid == "s1" else None
    from soniqboom.core import data
    monkeypatch.setattr(data, "get_track", get_track)
    r = await ART.scene_csdb(track_id="s1", sid=None)
    assert r["found"] is True and r["screenshot"] == "/api/scene/csdb/shot/11589.png"
    assert (await ART.scene_csdb(track_id="nope", sid=None))["found"] is False
    res = await ART.scene_csdb_shot(11589)
    assert res.media_type == "image/png" and res.body[:8] == b"\x89PNG\r\n\x1a\n"
    assert res.headers["Cache-Control"] == "public, max-age=604800"
    assert str(cw.seen[-1].url) == "https://csdb.dk/gfx/releases/11000/11589.png"
    n = len(cw.seen)
    assert (await ART.scene_csdb_shot(11589)).body == res.body and len(cw.seen) == n   # kept
    # a release no shown tune lists is never fetched
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        await ART.scene_csdb_shot(99999)
    assert e.value.status_code == 404 and len(cw.seen) == n


def test_hvsc_path_of_an_online_hvsc_tune(monkeypatch):
    from soniqboom.core import catalogue, hvsc
    monkeypatch.setattr(hvsc, "get_hvsc", lambda: SimpleNamespace(path_for_md5=lambda m: None,
                                                                  stil_key_for=lambda p: None))
    monkeypatch.setattr(catalogue, "is_catalogue_path", lambda p: True)
    monkeypatch.setattr(catalogue, "root_of", lambda p: "https://6581.nl/C64Music")
    monkeypatch.setattr(catalogue, "key_of", lambda r: "hvsc")
    t = sid(path="https://6581.nl/C64Music/MUSICIANS/M/Moon/Best.sid")
    assert C.hvsc_path(t) == "/MUSICIANS/M/Moon/Best.sid"
    monkeypatch.setattr(hvsc, "get_hvsc", lambda: SimpleNamespace(
        path_for_md5=lambda m: "/MUSICIANS/H/Hubbard_Rob/Commando.sid", stil_key_for=lambda p: None))
    assert C.hvsc_path(sid(sid_md5="ab" * 16)) == "/MUSICIANS/H/Hubbard_Rob/Commando.sid"


async def test_an_hourly_budget_caps_what_a_client_can_make_the_server_ask(cw, monkeypatch):
    monkeypatch.setattr(C, "_HOURLY", 3)
    for n in range(1, 6):                                   # five unknown ids, one tune
        await C.info_for(sid(f"t{n}", hvsc=None), 1000 + n)
    assert len(cw.seen) == 3                                # the rest refused before asking
