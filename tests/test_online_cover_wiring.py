# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where an online cover (``core/online_cover.py``) is served and what starts
its lookup: ``/api/art`` (the answer and its ETag before and after the cover,
the track's own cover taking over), the Subsonic and cast cover paths, and the
settings that govern it (the switch, Use Folder Art, the art cache clear, a
saved Discogs token)."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from soniqboom.api import admin, art as A
from soniqboom.config import settings
from soniqboom.core import cast_session, online_cover as OC

from test_online_cover import (  # noqa: F401  (``w`` is a fixture)
    BLUE, CAA, GREEN, RED, colour, front, jpeg, rel, settle, side, tr, w,
)
from test_subsonic_parity import env  # noqa: F401  (fixture)
from test_subsonic_r3 import _add


class _Req:
    def __init__(self, inm=None):
        self.headers = {"if-none-match": inm} if inm else {}


OWN = b"\xff\xd8own-cover"


@pytest.fixture
def lib(w, monkeypatch):
    """A library of tracks without cached art.  ``own[tid]``: the cover the
    file gives when it is read; ``unreadable``: files that cannot be read (no
    "no art" mark); ``absent`` / ``persisted``: the "no art" marks; ``cached``:
    the stamp of a cached cover of the track's own."""
    L = SimpleNamespace(w=w, tracks={}, own={}, unreadable=set(), absent=set(), persisted=set(),
                        resolved=[], cached={})

    async def get_track(tid):
        return L.tracks.get(tid)
    monkeypatch.setattr(A, "get_track", get_track)

    async def no_thumb(*_a, **_k):
        return None
    monkeypatch.setattr(A.art_cache, "get_art", no_thumb)
    monkeypatch.setattr(A, "_art_cached_mtime", lambda tid, size: L.cached.get(tid))

    async def source_mtime(_p):
        return 100.0
    monkeypatch.setattr(A, "_source_mtime_for_async", source_mtime)

    async def resolve(tid):
        L.resolved.append(tid)
        if tid in L.own:
            L.cached[tid] = 77.0                # (the real one caches what it found)
            return L.own[tid], "image/jpeg"
        if tid not in L.unreadable:
            L.absent.add(tid)                   # (as the real one: a clean read without a cover)
        return None, None
    monkeypatch.setattr(A, "_resolve_full_art", resolve)
    monkeypatch.setattr(A, "get_store", lambda: SimpleNamespace(is_art_absent=lambda tid: tid in L.absent))
    monkeypatch.setattr(A, "_is_art_absent_persisted", lambda tid, m: tid in L.persisted and m == 100.0)
    monkeypatch.setattr(A, "resize_cover", lambda data, px: data)

    async def stored(*_a, **_k):
        return None
    monkeypatch.setattr(A.art_cache, "store_art", stored)

    async def no_backfill(*_a, **_k):
        return None
    monkeypatch.setattr(A, "_fill_remote_art_later", no_backfill)
    A._bg_resolved.clear()

    def add(tid="t1", **kw):
        L.tracks[tid] = tr(tid, **kw)
        return L.tracks[tid]
    L.add = add
    w.mb.append(('release:"Jazz"', {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg(BLUE, (1600, 1600))
    w.mb.append(('release:"Innuendo"', {"releases": [rel("r2", "Innuendo", rg="g2", date="1991")]}))
    w.pics[front("release-group", "g2")] = jpeg(GREEN, (1600, 1600))
    return L


async def get(tid, inm=None, size="sm", fallback="none"):
    return await A.cover_art(tid, _Req(inm), size=size, fallback=fallback)


# ── /api/art ─────────────────────────────────────────────────────────────────

async def test_no_cover_then_the_online_cover_under_another_etag(lib):
    lib.add("t1")
    first = await get("t1")
    assert first.status_code == 204 and first.headers["X-SoniqBoom-Art"] == "none"
    base = first.headers["ETag"]
    await settle()
    assert lib.w.sent == [{"event": "art_ready", "track_id": "t1"}]
    # The list asks again with the tag of its empty answer: that one is stale now.
    second = await get("t1", base)
    assert second.status_code == 200 and second.media_type == "image/jpeg"
    assert second.headers["X-SoniqBoom-Art"] == "online"
    assert second.headers["Cache-Control"] == "no-cache"
    tag = second.headers["ETag"]
    assert tag != base and tag == OC.etag(base, await OC.state(lib.tracks["t1"]))
    assert side(second.body) == 200 and colour(second.body) == BLUE
    # Holding the online cover: current.
    third = await get("t1", tag)
    assert third.status_code == 304 and third.headers["ETag"] == tag and third.body == b""
    assert third.headers["Cache-Control"] == "no-cache"        # still checked at the next load


@pytest.mark.parametrize("size, px", [("sm", 200), ("lg", 550), ("full", 1200)])
async def test_each_size_gets_its_own_picture(lib, size, px):
    lib.add("t1", album="Innuendo")
    assert (await get("t1", size=size)).status_code == 204
    await settle()
    r = await get("t1", size=size)
    assert r.status_code == 200 and side(r.body) == px and colour(r.body) == GREEN
    assert r.headers["X-SoniqBoom-Art"] == "online"


async def test_the_placeholder_answer_gives_way_to_the_online_cover_too(lib):
    lib.add("t1")
    first = await get("t1", fallback="placeholder")
    assert first.status_code == 200 and first.headers["X-SoniqBoom-Art"] == "placeholder"
    await settle()
    r = await get("t1", fallback="placeholder")
    assert r.headers["X-SoniqBoom-Art"] == "online" and colour(r.body) == BLUE


async def test_the_tracks_own_cover_takes_over(lib):
    """A cover the track gains without its file changing (Use Folder Art
    switched on): the client still holds the online cover's tag."""
    lib.add("t1")
    await get("t1")
    await settle()
    tag = (await get("t1")).headers["ETag"]
    assert (await get("t1", tag)).status_code == 304
    lib.own["t1"] = OWN
    lib.absent.clear()                          # (what the setting change does)
    r = await get("t1", tag)
    assert r.status_code == 200 and r.body == OWN
    assert "X-SoniqBoom-Art" not in r.headers and r.headers["ETag"] != tag
    # From here on its own cover is cached (the tag follows the cached file).
    cached = await get("t1", r.headers["ETag"])
    assert cached.body == OWN and (await get("t1", cached.headers["ETag"])).status_code == 304
    assert (await get("t1", tag)).body == OWN          # the online cover's tag: never current again


async def test_the_stored_no_art_mark_counts_as_known_missing(lib):
    """After a restart the in-memory mark is gone; the stored one (valid for
    the file as it is) keeps the online cover's answer current."""
    lib.add("t1")
    await get("t1")
    await settle()
    tag = (await get("t1")).headers["ETag"]
    lib.absent.clear()
    lib.persisted.add("t1")
    n = len(lib.resolved)
    assert (await get("t1", tag)).status_code == 304 and len(lib.resolved) == n


async def test_a_file_that_cannot_be_read_starts_no_lookup(lib):
    lib.add("t1")
    lib.unreadable.add("t1")
    r = await get("t1")
    await settle()
    assert r.status_code == 204 and lib.w.calls == [] and lib.w.sent == []
    assert (await get("t1", r.headers["ETag"])).status_code == 304
    await settle()
    assert lib.w.calls == []
    lib.unreadable.clear()                      # readable again, and without a cover
    assert (await get("t1")).status_code == 204
    await settle()
    assert lib.w.sent == [{"event": "art_ready", "track_id": "t1"}]


async def test_a_revalidated_empty_answer_starts_the_lookup(lib):
    """The list got its empty answer while lookups were paused; it only
    revalidates from then on (304), and the cover must still arrive."""
    lib.add("t1")
    OC._paused_until = time.monotonic() + 600
    first = await get("t1")
    await settle()
    assert first.status_code == 204 and lib.w.calls == []
    OC._paused_until = 0.0
    r = await get("t1", first.headers["ETag"])
    assert r.status_code == 304 and r.headers["ETag"] == first.headers["ETag"]
    await settle()
    assert lib.w.sent == [{"event": "art_ready", "track_id": "t1"}]
    assert (await get("t1", first.headers["ETag"])).status_code == 200


async def test_none_found_keeps_the_empty_answer_and_asks_once(lib):
    lib.add("t1", album="Hot Space")
    first = await get("t1")
    await settle()
    n = len(lib.w.calls)
    assert n == 1 and lib.w.sent == []
    again = await get("t1")
    assert again.status_code == 204 and again.headers["ETag"] == first.headers["ETag"]
    assert (await get("t1", first.headers["ETag"])).status_code == 304
    await settle()
    assert len(lib.w.calls) == n


async def test_the_empty_answer_is_asked_again_at_every_use_while_the_lookup_runs(lib):
    """The row got "no cover" a moment before the cover was found; the player
    bar and the album grid ask the same URL next and must not be given the
    kept empty answer."""
    lib.add("t1")
    first = await get("t1")                     # the lookup is queued here
    assert first.status_code == 204 and first.headers["Cache-Control"] == "no-cache"
    again = await get("t1", first.headers["ETag"])          # still under way
    assert again.status_code == 304 and again.headers["Cache-Control"] == "no-cache"
    lib.add("t5", album="Innuendo")
    big = await get("t5", size="full")          # the full-size answer follows the same rule
    assert big.status_code == 204 and big.headers["Cache-Control"] == "no-cache"
    await settle()
    found = await get("t1", first.headers["ETag"])
    assert found.status_code == 200 and found.headers["X-SoniqBoom-Art"] == "online"
    assert found.headers["Cache-Control"] == "no-cache"


async def test_an_empty_answer_that_stays_is_kept_for_the_minute(lib):
    minute = "public, max-age=60, must-revalidate"
    lib.add("t1", album="Hot Space")            # nothing is found for this one
    lib.add("t2", format="ProTracker")          # never looked up
    first = await get("t1")
    assert first.headers["Cache-Control"] == "no-cache"
    plain = await get("t2")
    assert plain.status_code == 204 and plain.headers["Cache-Control"] == minute
    await settle()
    assert (await get("t1")).headers["Cache-Control"] == minute
    # The kept answer of the lookup's time goes back to the usual rule.
    r = await get("t1", first.headers["ETag"])
    assert r.status_code == 304 and r.headers["Cache-Control"] == minute
    # Paused: nothing is under way, so nothing is about to change.
    lib.add("t3", album="Innuendo")
    OC._paused_until = time.monotonic() + 600
    assert (await get("t3")).headers["Cache-Control"] == minute
    # A 304 for a file that could not be read carries no rule of its own.
    lib.add("t4", album="Innuendo")
    lib.unreadable.add("t4")
    held = await get("t4")
    assert "Cache-Control" not in (await get("t4", held.headers["ETag"])).headers


async def test_the_placeholder_picture_is_not_kept_for_good_where_a_cover_can_come(lib):
    lib.add("t1")
    lib.add("t2", format="ProTracker")
    lib.add("t3", album="Hot Space")
    under_way = await get("t1", fallback="placeholder")
    assert under_way.headers["X-SoniqBoom-Art"] == "placeholder"
    assert under_way.headers["Cache-Control"] == "no-cache"
    never = await get("t2", fallback="placeholder", size="full")
    assert never.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    await get("t3", fallback="placeholder")
    await settle()
    none_found = await get("t3", fallback="placeholder", size="full")
    assert none_found.headers["X-SoniqBoom-Art"] == "placeholder"
    assert none_found.headers["Cache-Control"] == "max-age=3600"


async def test_switched_off_the_online_cover_goes_and_comes_back(lib):
    lib.add("t1")
    base = (await get("t1")).headers["ETag"]
    await settle()
    tag = (await get("t1")).headers["ETag"]
    lib.w.setting = False
    off = await get("t1", tag)
    assert off.status_code == 204 and off.headers["ETag"] == base
    assert (await get("t1", base)).status_code == 304
    lib.w.setting = True
    on = await get("t1", base)
    assert on.status_code == 200 and on.headers["ETag"] == tag
    assert len(lib.w.queries()) == 1


async def test_a_network_share_track_goes_by_what_the_scan_saw(lib):
    share = "smb://nas.invalid/Music:/Queen"
    lib.add("r1", path=f"{share}/Jazz/01.mp3")                       # no picture in the file
    lib.add("r2", path=f"{share}/Innuendo/01.mp3", album="Innuendo", cover_art="/api/art/r2")
    lib.unreadable.update({"r1", "r2"})         # (a share file's read leaves no mark here)
    a, b = await get("r1"), await get("r2")
    await settle()
    assert a.status_code == b.status_code == 204
    assert lib.w.sent == [{"event": "art_ready", "track_id": "r1"}]
    assert [q.split('"')[1] for q in lib.w.queries()] == ["Jazz"]     # r2's own cover is still to come
    r = await get("r1", a.headers["ETag"])
    assert r.status_code == 200 and colour(r.body) == BLUE
    assert (await get("r1", r.headers["ETag"])).status_code == 304


async def test_a_track_that_is_never_looked_up_keeps_its_plain_answer(lib):
    lib.add("t1", format="ProTracker")
    r = await get("t1")
    await settle()
    assert r.status_code == 204 and lib.w.calls == []
    assert (await get("t1", r.headers["ETag"])).status_code == 304


async def test_a_cached_cover_of_its_own_never_consults_the_online_store(lib, monkeypatch):
    lib.add("t1")
    lib.cached["t1"] = 55.0

    async def thumb(tid, size):
        return OWN
    monkeypatch.setattr(A.art_cache, "get_art", thumb)

    async def never(track):
        raise AssertionError("the online store was consulted")
    monkeypatch.setattr(OC, "state", never)
    r = await get("t1")
    assert r.status_code == 200 and r.body == OWN
    assert (await get("t1", r.headers["ETag"])).status_code == 304


async def test_an_unknown_track_is_plain_no_art(lib):
    r = await get("nope")
    await settle()
    assert r.status_code == 204 and lib.w.calls == []


async def test_the_online_store_failing_costs_only_the_online_cover(lib, monkeypatch):
    lib.add("t1")
    monkeypatch.setattr(OC, "request", lambda st, tid: 1 / 0)
    assert (await get("t1")).status_code == 204
    assert await A.online_cover_for("t1") == (None, True)         # (not known: it may still come)


# ── The other cover paths: ``online_cover_for`` ──────────────────────────────

async def test_for_the_other_endpoints_pending_then_the_cover(lib):
    lib.add("t1")
    lib.add("t2", album="Innuendo")
    assert await A.online_cover_for("t1") == (None, False)        # its own cover was never resolved
    await A._resolve_full_art("t1")
    assert await A.online_cover_for("t1") == (None, True)
    await settle()
    data, pending = await A.online_cover_for("t1")
    assert pending is False and side(data) == 1200 and colour(data) == BLUE
    await A._resolve_full_art("t2")
    assert await A.online_cover_for("t2", "sm") == (None, True)
    await settle()
    data, pending = await A.online_cover_for("t2", "sm")
    assert side(data) == 200 and colour(data) == GREEN
    assert await A.online_cover_for("nope") == (None, False)


async def test_subsonic_serves_the_online_cover_and_a_short_lived_placeholder_meanwhile(lib, monkeypatch):
    from soniqboom.api import subsonic
    lib.add("t1")
    lib.add("t3", album="Hot Space")
    store = SimpleNamespace(get_track=lambda tid: {"id": tid, "path": f"/m/{tid}.mp3"},
                            is_art_absent=lambda tid: True)
    monkeypatch.setattr(subsonic, "_source_offline", lambda t: False)
    await A._resolve_full_art("t1")
    await A._resolve_full_art("t3")
    r = await subsonic._online_cover_art(store, "t1", "sm", False)
    assert r.media_type == "image/png" and r.headers["Cache-Control"] == "public, max-age=300"
    await settle()
    r = await subsonic._online_cover_art(store, "t1", "sm", False)
    assert r.media_type == "image/jpeg" and r.headers["Cache-Control"] == "private, max-age=3600"
    assert side(r.body) == 200 and colour(r.body) == BLUE
    assert side((await subsonic._online_cover_art(store, "t1", "full", True)).body) == 1200
    r = await subsonic._online_cover_art(store, "t3", "sm", False)
    assert r.headers["Cache-Control"] == "public, max-age=300"
    await settle()
    r = await subsonic._online_cover_art(store, "t3", "sm", False)      # none found: the day-long one
    assert r.media_type == "image/png" and r.headers["Cache-Control"] == "public, max-age=86400"


def test_subsonic_get_cover_art_falls_back_to_the_online_cover(env, w, monkeypatch):  # noqa: F811
    _add(env, tid="oc1", title="Bicycle Race", artist="Queen", dir_path="/music/queen")
    track = tr("oc1", path="/music/queen/oc1.mp3")

    async def get_track(tid):
        return track if tid == "oc1" else None
    monkeypatch.setattr(A, "get_track", get_track)
    monkeypatch.setattr(A, "get_store", lambda: env.store)
    env.store.mark_art_absent("oc1")
    sub = OC.subject(track)
    OC._write_record(sub.key, {"v": 1, "ok": True, "at": 1700000000, "source": "musicbrainz", "ref": "x"},
                     OC._normalise(jpeg(BLUE, (1600, 1600))))
    small = env.client.get("/rest/getCoverArt.view", params={"id": "oc1", "size": 200})
    assert small.headers["content-type"] == "image/jpeg" and small.headers["cache-control"] == "private, max-age=3600"
    assert side(small.content) == 200 and colour(small.content) == BLUE
    assert side(env.client.get("/rest/getCoverArt.view", params={"id": "oc1"}).content) == 1200
    w.setting = False
    off = env.client.get("/rest/getCoverArt.view", params={"id": "oc1", "size": 200})
    assert off.headers["content-type"] == "image/png" and off.headers["cache-control"].endswith("max-age=86400")


async def test_cast_shows_the_online_cover_and_does_not_remember_a_pending_one(lib, monkeypatch):
    cast_session._art_cache.clear()
    lib.add("t1")
    lib.add("t3", album="Hot Space")
    with pytest.raises(cast_session._ArtPending):
        await cast_session._resolve_and_cap_art("t1")
    OC._pending.clear()                          # (as if that lookup had not finished yet)
    assert await cast_session._airplay_artwork_bytes("t1") is None
    assert "t1" not in cast_session._art_cache
    await settle()
    data = await cast_session._airplay_artwork_bytes("t1")
    assert side(data) == 1200 and colour(data) == BLUE and cast_session._art_cache["t1"] == data
    assert await cast_session._airplay_artwork_bytes("t3") is None     # pending
    await settle()
    assert await cast_session._airplay_artwork_bytes("t3") is None     # none found: remembered
    assert cast_session._art_cache["t3"] is None
    lib.own["t4"] = OWN
    lib.add("t4")
    assert await cast_session._resolve_and_cap_art("t4") == OWN
    cast_session._art_cache.clear()


# ── Settings ─────────────────────────────────────────────────────────────────

@pytest.fixture
def real_setting(monkeypatch):
    """The real stored setting (``w`` fakes it) and what is sent to clients."""
    from soniqboom.api import library
    from soniqboom.core import data
    from soniqboom.core.store import TrackStore
    OC._reset_for_tests()
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(data, "get_store", lambda: s)
    monkeypatch.setattr(admin, "get_store", lambda: s)
    sent = []

    async def broadcast(msg):
        sent.append(msg)
    monkeypatch.setattr(library, "_broadcast", broadcast)
    return sent


async def test_the_switch_is_on_by_default_stored_and_announced_on_a_change_only(real_setting):
    sent = real_setting
    assert await OC.enabled() is True
    assert (await admin.get_settings(_tok="x"))["online_cover_lookup"] is True
    await admin.update_settings({"online_cover_lookup": True}, _tok="x")       # unchanged
    assert sent == []
    cast_session._art_cache["t9"] = b"an online cover"
    await admin.update_settings({"online_cover_lookup": False}, _tok="x")
    assert sent == [{"event": "art_refresh"}]
    assert cast_session._art_cache == {}                           # (a cast cover may have been an online one)
    assert await OC.enabled() is False and await OC.state(tr()) is OC.NOTHING
    assert (await admin.get_settings(_tok="x"))["online_cover_lookup"] is False
    await admin.update_settings({"online_cover_lookup": False}, _tok="x")
    await admin.update_settings({"scan_zips": True}, _tok="x")                # another setting: untouched
    assert sent == [{"event": "art_refresh"}] and await OC.enabled() is False
    await admin.update_settings({"online_cover_lookup": "true"}, _tok="x")
    assert sent == [{"event": "art_refresh"}] * 2 and await OC.enabled() is True
    from soniqboom.core.data import get_config
    await admin.update_settings({"online_cover_lookup": "false"}, _tok="x")    # (text, not a boolean)
    assert await get_config(OC.SETTING, None) is False and len(sent) == 3


def _art_files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


@pytest.fixture
def art_dir(tmp_path, monkeypatch):
    root = tmp_path / "artcache"
    monkeypatch.setattr(settings, "art_cache_dir", str(root))
    (root / "ab").mkdir(parents=True)
    (root / "ab" / "own.sm.jpg").write_bytes(b"own")
    (root / "top.jpg").write_bytes(b"own")
    OC._reset_for_tests()
    sub = OC.subject(tr())
    OC._write_record(sub.key, {"v": 1, "ok": True, "at": 1700000000}, OC._normalise(jpeg()))
    assert len(_art_files(root / "online")) == 4
    OC._memo["a|x|y"] = {"v": 1, "ok": False, "at": 1}
    OC._pending["a|x|y"] = OC._Job("a|x|y", sub)
    yield root
    OC._reset_for_tests()


def test_clearing_the_art_cache_takes_the_online_covers_too(art_dir):
    gen = OC._gen
    count, errors = admin._purge_art_dir()
    assert (count, errors) == (6, []) and _art_files(art_dir) == []
    assert not OC._memo and not OC._pending and OC._gen > gen


def test_a_clear_forgets_before_the_files_go_and_again_after(art_dir, monkeypatch):
    import os
    gen, seen, real = OC._gen, [], os.unlink

    def unlink(p):
        if not seen:
            seen.append((OC._gen > gen, len(OC._pending)))        # already forgotten: nothing is stored into the gap
            OC._memo["late"] = {"v": 1, "ok": True, "at": 1}      # an answer noted while the files go
        real(p)
    monkeypatch.setattr(os, "unlink", unlink)
    admin._purge_art_dir()
    assert seen == [(True, 0)] and not OC._memo


def test_a_use_folder_art_change_keeps_the_online_covers(art_dir):
    gen = OC._gen
    count, _errors = admin._purge_art_dir(keep_online=True)
    assert count == 2 and len(_art_files(art_dir)) == 4
    assert all(p.startswith("online/") for p in _art_files(art_dir))
    assert "a|x|y" in OC._memo and "a|x|y" in OC._pending and OC._gen == gen


async def test_switching_use_folder_art_keeps_the_online_covers(real_setting, art_dir):
    await admin.update_settings({"use_folder_art": False}, _tok="x")
    assert real_setting == [{"event": "art_refresh"}]
    assert len(_art_files(art_dir)) == 4 and (await OC.state(tr())).hit


def test_saving_a_discogs_token_asks_the_nones_again(tmp_path, monkeypatch, w):  # noqa: F811
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from soniqboom import config
    from soniqboom.core import nowplaying_art
    conf = tmp_path / "SoniqBoom.conf"
    conf.write_text(json.dumps({}))
    real = config.update_local_conf
    monkeypatch.setattr(config, "update_local_conf", lambda fn: real(fn, path=conf))
    monkeypatch.setattr(nowplaying_art, "forget_misses", lambda: 0)
    monkeypatch.delenv("SONIQBOOM_DISCOGS_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    miss, hit = OC.subject(tr(album="Hot Space")), OC.subject(tr())
    OC._write_record(miss.key, {"v": 1, "ok": False, "at": int(time.time())}, None)
    OC._write_record(hit.key, {"v": 1, "ok": True, "at": int(time.time())}, OC._normalise(jpeg()))
    app = FastAPI()
    app.include_router(admin.router, prefix="/api")
    app.dependency_overrides[admin._require_token] = lambda: "t"
    c = TestClient(app)
    assert c.put("/api/admin/discogs", json={"token": ""}).status_code == 200      # removing: nothing forgotten
    assert asyncio.run(OC.state(tr(album="Hot Space"))).known
    OC._reset_for_tests()
    assert c.put("/api/admin/discogs", json={"token": "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789wxyz"}).status_code == 200
    assert asyncio.run(OC.state(tr(album="Hot Space"))).known is False
    assert asyncio.run(OC.state(tr())).hit


# ── Review fixes: the track's own cover out of reach, and "may it still come?" ──

async def test_a_stored_online_cover_is_not_shown_for_a_file_that_cannot_be_read(lib):
    lib.add("t1")
    lib.add("t2")                                # the same album; its file cannot be read
    lib.unreadable.add("t2")
    assert (await get("t1")).status_code == 204
    await settle()
    assert (await get("t1")).headers["X-SoniqBoom-Art"] == "online"
    r = await get("t2")
    assert r.status_code == 204 and r.headers["X-SoniqBoom-Art"] == "none"
    assert await A.online_cover_for("t2") == (None, False)
    lib.unreadable.clear()                       # readable again, and without a cover of its own
    assert (await get("t2")).headers["X-SoniqBoom-Art"] == "online"
    assert (await A.online_cover_for("t2"))[0] is not None


async def test_whether_a_cover_may_still_come(lib):
    lib.add("t1")
    lib.add("t3", album="Hot Space")
    lib.add("t5", album="Soft")
    lib.w.mb.append(('release:"Soft"', {"releases": [rel("r5", "Soft", rg="g5")]}))
    lib.w.pics[front("release-group", "g5")] = (500, b"oops", "text/plain")
    lib.w.pics[front("release", "r5")] = (500, b"oops", "text/plain")
    for tid in ("t1", "t3", "t5"):
        await A._resolve_full_art(tid)
    OC._paused_until = time.monotonic() + 100    # the services could not be reached: later, then
    assert await A.online_cover_for("t1") == (None, True) and lib.w.calls == []
    OC._paused_until = 0.0
    lib.w.offline = True                         # offline mode: never
    assert await A.online_cover_for("t1") == (None, False) and lib.w.calls == []
    lib.w.offline = False
    assert await A.online_cover_for("t3") == (None, True)
    assert await A.online_cover_for("t5") == (None, True)
    await settle()
    assert await A.online_cover_for("t3") == (None, False)         # none found
    assert await A.online_cover_for("t5") == (None, True)          # the image host failed: asked again in an hour


async def test_subsonic_and_cast_do_not_remember_a_placeholder_while_lookups_are_paused(lib, monkeypatch):
    from soniqboom.api import subsonic
    cast_session._art_cache.clear()
    lib.add("t1")
    store = SimpleNamespace(get_track=lambda tid: {"id": tid, "path": f"/m/{tid}.mp3"},
                            is_art_absent=lambda tid: True)
    monkeypatch.setattr(subsonic, "_source_offline", lambda t: False)
    await A._resolve_full_art("t1")
    OC._paused_until = time.monotonic() + 100
    r = await subsonic._online_cover_art(store, "t1", "sm", False)
    assert r.media_type == "image/png" and r.headers["Cache-Control"] == "public, max-age=300"
    with pytest.raises(cast_session._ArtPending):
        await cast_session._resolve_and_cap_art("t1")
    assert lib.w.calls == []
    OC._paused_until = 0.0
    lib.w.offline = True
    r = await subsonic._online_cover_art(store, "t1", "sm", False)
    assert r.headers["Cache-Control"] == "public, max-age=86400"
    assert await cast_session._resolve_and_cap_art("t1") is None
    cast_session._art_cache.clear()


async def test_the_settings_page_does_not_show_off_for_a_setting_that_cannot_be_read(real_setting, monkeypatch):
    async def broken():
        raise RuntimeError("no store")
    monkeypatch.setattr(OC, "setting_value", broken)
    with pytest.raises(RuntimeError):
        await admin.get_settings(_tok="x")
