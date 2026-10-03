# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Subsonic round-4 review fixes (GitHub #14 follow-ups).

Same harness as tests/test_subsonic_parity.py: the real ``/rest`` router over
ASGI against a hand-built ``TrackStore`` with per-user state in a tmp file.
Covers the ``text/xml`` error envelopes binary endpoints send, big folder /
group albums (snapshot memo, off-loop JSON, time-sliced mapping), sonic
similarity, random picks that skip offline sources, the play-queue / jukebox
shapes, word-wise artist / album search, transcode links bound to the account,
cover-art buckets and placeholders, radio-station logos and the smaller spec
fixes.
"""
from __future__ import annotations

import asyncio
import gc
import json
import types
import weakref
import xml.etree.ElementTree as ET

import pytest

from soniqboom.api import subsonic
from soniqboom.core import subsonic_index as sx
from soniqboom.core import subsonic_state
from soniqboom.core.store import TrackStore

from test_subsonic_parity import _tr, env, radio_favs, users  # noqa: F401 — fixtures


def _add(env, **kw):
    kw.setdefault("store", env.store)
    env.store.upsert_track(_tr(**kw))


# ── r4-sub-1: XML envelopes are text/xml (binary endpoints' errors) ──────────

@pytest.mark.parametrize("method, params", [
    ("stream.view", {"id": "nope"}),
    ("download.view", {"id": "nope"}),
    ("getCoverArt.view", {"id": "al:zzz"}),
    ("getSong.view", {"id": "nope"}),
    ("ping.view", {}),
])
def test_xml_envelopes_are_text_xml(env, method, params):
    """The spec: an error from stream / download / getCoverArt has a content
    type starting with "text/xml" — DSub treats anything else as the song's
    bytes and caches the error document as a finished download."""
    r = env.client.get(f"/rest/{method}", params=params)
    assert r.headers["content-type"].startswith("text/xml"), r.headers["content-type"]
    assert "charset=utf-8" in r.headers["content-type"]
    root = ET.fromstring(r.content)
    assert root.get("status") == ("ok" if method == "ping.view" else "failed")
    # JSON stays JSON.
    j = env.client.get(f"/rest/{method}", params={**params, "f": "json"})
    assert j.headers["content-type"].startswith("application/json")


def test_head_probe_uses_text_xml(env):
    r = env.client.head("/rest/getBogus.view")
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/xml")
    assert env.client.head("/rest/ping.view").headers["content-type"].startswith("text/xml")


def test_big_xml_listing_still_gzipped_off_loop(env, monkeypatch):
    monkeypatch.setattr(subsonic, "_GZIP_OFFLOAD_MIN_BYTES", 500)
    for i in range(20):
        _add(env, tid=f"gx{i}", title=f"Gx {i}", artist="Gzip", added=5000 + i)
    r = env.client.get("/rest/getRandomSongs.view", params={"size": 20},
                       headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-type"].startswith("text/xml")
    assert r.headers.get("content-encoding") == "gzip"
    assert ET.fromstring(r.content).get("status") == "ok"


# ── r4-sub-2 / r4-sub-18: big folder / group albums ──────────────────────────

ROOT = "/music/mods"
COLL = ROOT + "/modarchive_2007"


def _big_group(env, per_bucket=12, letters=("A", "B", "C", "D")):
    """Mixed letter buckets under one ancestor: two named owners and many
    owner-less tunes per bucket (the owner-less share is the big one)."""
    env.store._aof = lambda *a, **k: None
    env.store.upsert_scan_dir(ROOT)
    n = 0
    for L in letters:
        d = f"{COLL}/{L}"
        for owner in ("Alpha", "Beta"):
            _add(env, tid=f"{L}-{owner}", title=f"{owner} {L}", artist=owner, dir_path=d,
                 added=1000 + n, fmt="MOD")
            n += 1
        for k in range(per_bucket):
            _add(env, tid=f"{L}-none{k:03d}", title=f"none {L} {k:03d}", dir_path=d,
                 added=1000 + n, fmt="MOD")
            n += 1
    env.folder_albums(True)
    return sx._path_hash(COLL)


def _old_walk(store, cat, anc, okey):
    e = cat.by_id[f"fa:{anc}~g{okey}"]
    out = []
    for dh in e.dir_hashes:
        out.extend(sx._dir_album_less(store, dh, okey))
    out.sort(key=sx._track_order)
    return out


def test_group_share_memo_matches_the_member_walk(env, monkeypatch):
    anc = _big_group(env)
    cat = subsonic._catalogue(env.store)
    want = [t["id"] for t in _old_walk(env.store, cat, anc, "0")]
    assert len(want) == 48
    # The owner-less share comes from the snapshot's owner-less list — no
    # member-folder walk at all, cold or warm.

    def _boom(*a, **k):
        raise AssertionError("member folder walked")
    real_walk = sx._dir_album_less
    monkeypatch.setattr(sx, "_dir_album_less", _boom)
    got = [t["id"] for t in sx.group_album_tracks(env.store, cat, anc, "0")]
    assert got == want
    assert cat.memo[("grp_tracks", anc, "0")] == tuple(want)
    monkeypatch.setattr(sx, "_dir_album_less", real_walk)
    # A named owner's share: walked once, then served from the memo.
    okey = sx.owner_key("alpha")
    first = [t["id"] for t in sx.group_album_tracks(env.store, cat, anc, okey)]
    assert first == ["A-Alpha", "B-Alpha", "C-Alpha", "D-Alpha"]
    monkeypatch.setattr(sx, "_dir_album_less", _boom)
    assert [t["id"] for t in sx.group_album_tracks(env.store, cat, anc, okey)] == first


def test_big_album_output_unchanged_and_memoised(env, monkeypatch):
    anc = _big_group(env)
    gid = sx.group_album_id(anc, "")
    # Reference bytes: memo and off-loop JSON disabled.
    monkeypatch.setattr(subsonic, "_ALBUM_MEMO_MIN", 10 ** 9)
    monkeypatch.setattr(subsonic, "_JSON_OFFLOAD_MIN_ITEMS", 10 ** 9)
    ref = {}
    for m, extra in (("getAlbum.view", {}), ("getMusicDirectory.view", {}),
                     ("getAlbum.view", {"f": "xml"}),
                     ("getAlbum.view", {"f": "jsonp", "callback": "cb"})):
        ref[(m, tuple(extra.items()))] = env.get(m, id=gid, **extra).content
    body = json.loads(ref[("getAlbum.view", ())])["subsonic-response"]["album"]
    assert body["songCount"] == 48 and len(body["song"]) == 48
    # Now memoised + spliced JSON off the loop: byte-identical.
    monkeypatch.setattr(subsonic, "_ALBUM_MEMO_MIN", 10)
    monkeypatch.setattr(subsonic, "_JSON_OFFLOAD_MIN_ITEMS", 10)
    spliced = []
    real = subsonic._json_spliced

    def _spy(*a, **k):
        spliced.append(a[1:])
        return real(*a, **k)
    monkeypatch.setattr(subsonic, "_json_spliced", _spy)
    for (m, extra), want in ref.items():
        assert env.get(m, id=gid, **dict(extra)).content == want, m
    assert ("album", "song") in spliced and ("directory", "child") in spliced
    cat = subsonic._catalogue(env.store)
    hdr, tids = cat.memo[("album_tracks", gid)]
    assert len(tids) == 48 and "song" not in hdr          # the caller's copy got the songs
    # A hit never walks the folders again.

    def _boom(*a, **k):
        raise AssertionError("walked")
    real_gat, real_fe = sx.group_album_tracks, sx.folder_entry_from_tracks
    monkeypatch.setattr(sx, "group_album_tracks", _boom)
    monkeypatch.setattr(sx, "folder_entry_from_tracks", _boom)
    assert env.get("getAlbum.view", id=gid).content == ref[("getAlbum.view", ())]
    monkeypatch.setattr(sx, "group_album_tracks", real_gat)
    monkeypatch.setattr(sx, "folder_entry_from_tracks", real_fe)
    # A library change is a new snapshot: the new track is listed.
    _add(env, tid="A-none999", title="late", dir_path=f"{COLL}/A", added=9999, fmt="MOD")
    again = env.ok("getAlbum.view", id=gid)["album"]
    assert again["songCount"] == 49 and "A-none999" in {s["id"] for s in again["song"]}


def test_small_albums_are_not_memoised(env):
    anc = _big_group(env, per_bucket=2)
    env.ok("getAlbum.view", id=sx.group_album_id(anc, ""))
    cat = subsonic._catalogue(env.store)
    assert not any(isinstance(k, tuple) and k[0] == "album_tracks" for k in cat.memo)


def test_long_loops_yield_on_a_timer(env, monkeypatch):
    """``_songs_with_tunes`` and ``_prepare_folder_view`` yield after a time
    slice with the catalogue build's sub-ms timer, never ``sleep(0)``."""
    env.store.upsert_scan_dir("/music")
    rh = env.store.store_hash_lookup("/music")
    for i in range(300):
        _add(env, tid=f"y{i:03d}", title=f"Y {i}", artist="Yield", added=3000 + i,
             dir_path="/music/yield")
        env.store.upsert_track(dict(env.store.get_track(f"y{i:03d}"), scan_root_hash=rh))
    monkeypatch.setattr(sx, "_SLICE_SEC", 0.0)            # every check yields
    delays = []
    real = asyncio.sleep

    async def _spy(d=0, *a, **k):
        delays.append(d)
        return await real(0)
    monkeypatch.setattr(subsonic.asyncio, "sleep", _spy)
    tracks = [env.store.get_track(f"y{i:03d}") for i in range(300)]
    ctx = subsonic._song_ctx(env.store, env.user)
    songs = asyncio.run(subsonic._songs_with_tunes(tracks, ctx))
    assert [s["id"] for s in songs] == [f"y{i:03d}" for i in range(300)]
    assert delays and set(delays) == {sx._YIELD_SEC}
    assert len(delays) == 300 // subsonic._MAP_CHECK
    delays.clear()
    cat = subsonic._catalogue(env.store)
    asyncio.run(subsonic._prepare_folder_view(env.store, cat, rh))
    assert set(delays) == {sx._YIELD_SEC}
    assert ("folder_view", rh) in cat.memo
    assert cat.memo[("folder_view", rh)][0] == subsonic._folder_view(
        env.store, types.SimpleNamespace(memo={}, track_entry=cat.track_entry), rh)[0]


# ── r4-sub-7: the /cast/ byte server is never gzipped ────────────────────────

def test_cast_byte_server_is_not_gzipped(tmp_path):
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.testclient import TestClient
    from soniqboom.main import _SelectiveGZipMiddleware
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF" + b"\0" * 60_000)          # highly compressible
    inner = FastAPI()

    @inner.get("/cast/{token}/{name}")
    async def _cast(token: str, name: str):
        return FileResponse(wav, media_type="audio/wav")

    @inner.get("/api/cast/status")
    async def _status():
        return JSONResponse({"devices": ["x" * 40] * 100})

    @inner.get("/api/stream/{tid}")
    async def _stream(tid: str):
        return FileResponse(wav, media_type="audio/wav")

    c = TestClient(_SelectiveGZipMiddleware(inner, minimum_size=1000))
    gz = {"Accept-Encoding": "gzip"}
    r = c.get("/cast/tok/a.wav", headers={**gz, "Range": "bytes=0-999"})
    assert r.status_code == 206
    assert "content-encoding" not in r.headers and r.headers["content-length"] == "1000"
    full = c.get("/cast/tok/a.wav", headers=gz)
    assert "content-encoding" not in full.headers and len(full.content) == 60_004
    # The JSON control API keeps its compression.
    assert c.get("/api/cast/status", headers=gz).headers.get("content-encoding") == "gzip"


# ── r4-sub-8: play queue — the required element, a re-mapped current ────────

def test_play_queue_without_a_saved_queue_has_the_element(env):
    for m, key in (("getPlayQueue.view", "playQueue"),
                   ("getPlayQueueByIndex.view", "playQueueByIndex")):
        pq = env.ok(m)[key]
        assert pq == {"username": "alice", "changed": "1970-01-01T00:00:00Z", "changedBy": ""}
        doc = ET.fromstring(env.get(m, f="xml").content)
        el = doc.find(f"{{*}}{key}")
        assert el is not None and el.get("username") == "alice" and not list(el)


@pytest.mark.parametrize("deleted, cur, idx", [("q2", "q3", 1), ("q3", "q2", 1)])
def test_play_queue_current_song_deleted(env, deleted, cur, idx):
    saved_cur = "q2" if deleted == "q2" else "q3"
    env.ok("savePlayQueue.view", id=["q1", "q2", "q3"], current=saved_cur, position=42000)
    env.store.delete_track(deleted)
    pq = env.ok("getPlayQueue.view")["playQueue"]
    left = [i for i in ("q1", "q2", "q3") if i != deleted]
    assert [e["id"] for e in pq["entry"]] == left
    # The first survivor after the deleted current (else the last entry) is
    # current, and the deleted song's position no longer applies.
    assert pq["current"] == cur and pq["position"] == 0
    bi = env.ok("getPlayQueueByIndex.view")["playQueueByIndex"]
    assert bi["currentIndex"] == idx and bi["position"] == 0
    assert bi["entry"][bi["currentIndex"]]["id"] == pq["current"]


def test_play_queue_current_survives_keeps_position(env):
    env.ok("savePlayQueueByIndex.view", id=["q1", "q2", "q1"], currentIndex=2, position=1500)
    env.store.delete_track("q2")
    pq = env.ok("getPlayQueue.view")["playQueue"]
    assert pq["current"] == "q1" and pq["position"] == 1500
    bi = env.ok("getPlayQueueByIndex.view")["playQueueByIndex"]
    assert bi["currentIndex"] == 1 and bi["position"] == 1500
    # Every entry gone: no current, position 0, no entries.
    env.store.delete_track("q1")
    pq = env.ok("getPlayQueue.view")["playQueue"]
    assert "current" not in pq and pq["position"] == 0 and not pq.get("entry")


# ── r4-sub-9: jukeboxControl queues file ids and keeps its index aligned ─────

@pytest.fixture()
def jukebox(env, monkeypatch):
    from soniqboom.api import multiroom
    from soniqboom.core import jukebox as jb_mod
    jb = jb_mod._Jukebox()
    monkeypatch.setattr(jb_mod, "get_jukebox", lambda: jb)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(multiroom, "notify_jukebox_room", _noop)
    return jb


def _tunes(env, tid="sid1", subsongs=3):
    _add(env, tid=tid, title="Trap", artist="Ben Daglish", dir_path="/music/C64/Daglish",
         added=700, fmt="SID")
    env.store.upsert_track(dict(env.store.get_track(tid), subsongs=subsongs))


def test_jukebox_queues_file_ids_and_skips_unresolvable(env, jukebox):
    _tunes(env)
    env.ok("jukeboxControl.view", action="set", id=["sid1~1", "h1", "bogus"])
    assert jukebox.queue_ids() == ["sid1", "h1"]
    assert jukebox.current_id() == "sid1"
    pl = env.ok("jukeboxControl.view", action="get")["jukeboxPlaylist"]
    assert [e["id"] for e in pl["entry"]] == ["sid1", "h1"] and pl["currentIndex"] == 0
    # Only unresolvable ids: an error, and the queue is left alone.
    assert env.fail("jukeboxControl.view", action="add", id=["nope"])["code"] == 70
    assert jukebox.queue_ids() == ["sid1", "h1"]


def test_jukebox_index_tracks_the_listed_entries(env, jukebox):
    env.ok("jukeboxControl.view", action="set", id=["q1", "q2", "q3"])
    env.ok("jukeboxControl.view", action="skip", index=2)
    env.store.delete_track("q1")                      # deleted after it was queued
    pl = env.ok("jukeboxControl.view", action="get")["jukeboxPlaylist"]
    assert [e["id"] for e in pl["entry"]] == ["q2", "q3"]
    assert pl["entry"][pl["currentIndex"]]["id"] == jukebox.current_id() == "q3"
    st = env.ok("jukeboxControl.view", action="status")["jukeboxStatus"]
    assert st["currentIndex"] == 1
    # A client index is into the listed entries: skip 0 plays q2.
    env.ok("jukeboxControl.view", action="skip", index=0)
    assert jukebox.current_id() == "q2"
    env.ok("jukeboxControl.view", action="remove", index=1)          # removes q3
    assert jukebox.queue_ids() == ["q1", "q2"]
    env.store.delete_track("q2")
    assert env.ok("jukeboxControl.view", action="get")["jukeboxPlaylist"]["currentIndex"] == -1


# ── r4-sub-4: getTranscodeDecision honours codecProfiles / maxAudioChannels ──

def _decide(env, media_id, info):
    r = env.client.post("/rest/getTranscodeDecision.view",
                        params={"mediaId": media_id, "mediaType": "song", "f": "json"},
                        json=info)
    body = r.json()["subsonic-response"]
    assert body["status"] == "ok", body
    return body["transcodeDecision"]


def _hires(env, tid="hf", ch=6, sr=96000, bd=24, br=3_000_000):
    _add(env, tid=tid, title="Hi-res", artist="Audiophile", added=800, fmt="FLAC")
    t = dict(env.store.get_track(tid), path=f"/music/misc/{tid}.flac", channels=ch,
             sample_rate=sr, bit_depth=bd, bitrate=br)
    env.store.upsert_track(t)


_FLAC_DP = {"containers": ["flac"], "audioCodecs": ["flac"], "protocols": ["http"]}
_TX = [{"container": "flac", "audioCodec": "flac", "protocol": "http"},
       {"container": "mp3", "audioCodec": "mp3", "protocol": "http", "maxAudioChannels": 2}]


def test_codec_profile_limits_block_direct_play(env):
    _hires(env)
    info = {"directPlayProfiles": [{**_FLAC_DP, "maxAudioChannels": 2}],
            "transcodingProfiles": _TX,
            "codecProfiles": [{"type": "AudioCodec", "name": "flac", "limitations": [
                {"name": "audioSamplerate", "comparison": "LessThanEqual", "values": ["48000"],
                 "required": True},
                {"name": "audioBitdepth", "comparison": "LessThanEqual", "values": ["16"],
                 "required": True}]},
                {"type": "AudioCodec", "name": "mp3", "limitations": [
                    {"name": "audioSamplerate", "comparison": "LessThanEqual",
                     "values": ["48000"], "required": True}]}]}
    d = _decide(env, "hf", info)
    assert d["canDirectPlay"] is False and d["canTranscode"] is True
    assert d["transcodeReason"] == ["audio channels not supported",
                                    "audio samplerate not supported",
                                    "audio bitdepth not supported"]
    ts = d["transcodeStream"]
    # Not flac again (the limits are the source codec's); mp3, which downmixes.
    assert ts["codec"] == "mp3" and ts["audioChannels"] == 2 and ts["audioSamplerate"] == 48000
    claims = subsonic._verify_token(d["transcodeParams"])
    assert claims["tc"] == "mp3" and claims["tsr"] == 48000


def test_source_within_limits_still_direct_plays(env):
    _hires(env, ch=2, sr=44100, bd=16, br=900_000)
    info = {"directPlayProfiles": [{**_FLAC_DP, "maxAudioChannels": 2}],
            "transcodingProfiles": _TX,
            "codecProfiles": [{"type": "AudioCodec", "name": "flac", "limitations": [
                {"name": "audioSamplerate", "comparison": "LessThanEqual", "values": ["48000"],
                 "required": True},
                {"name": "audioChannels", "comparison": "Equals", "values": ["1", "2"],
                 "required": True},
                {"name": "audioBitdepth", "comparison": "NotEquals", "values": ["32"],
                 "required": True}]}]}
    d = _decide(env, "hf", info)
    assert d["canDirectPlay"] is True and "transcodeReason" not in d


def test_non_required_and_malformed_limits_never_block(env):
    _hires(env)
    info = {"directPlayProfiles": [_FLAC_DP], "transcodingProfiles": _TX,
            "codecProfiles": [{"type": "AudioCodec", "name": "flac", "limitations": [
                {"name": "audioSamplerate", "comparison": "LessThanEqual", "values": ["48000"],
                 "required": False},
                {"name": "audioBitdepth", "comparison": "LessThanEqual", "values": ["sixteen"],
                 "required": True},
                {"name": "audioChannels", "comparison": "LessThanEqual", "values": [],
                 "required": True},
                {"name": "audioProfile", "comparison": "Equals", "values": ["LC"],
                 "required": True},
                "junk"]},
                "junk", {"type": "Video", "name": "flac", "limitations": [
                    {"name": "audioSamplerate", "comparison": "LessThanEqual",
                     "values": ["8000"], "required": True}]}]}
    d = _decide(env, "hf", info)
    assert d["canDirectPlay"] is True, d


def test_direct_play_channel_limit_alone(env):
    _hires(env, sr=48000, bd=16, br=2_000_000)
    d = _decide(env, "hf", {"directPlayProfiles": [{**_FLAC_DP, "maxAudioChannels": 2}],
                            "transcodingProfiles": _TX})
    assert d["transcodeReason"] == ["audio channels not supported"]
    assert d["transcodeStream"]["codec"] == "mp3" and d["transcodeStream"]["audioChannels"] == 2
    # Without the channel cap the same file direct-plays.
    assert _decide(env, "hf", {"directPlayProfiles": [_FLAC_DP],
                               "transcodingProfiles": _TX})["canDirectPlay"] is True


def test_target_bitrate_limit_caps_the_encode(env):
    _hires(env, ch=2, sr=44100, bd=16, br=900_000)
    info = {"directPlayProfiles": [{"audioCodecs": ["mp3"]}],
            "transcodingProfiles": [{"audioCodec": "mp3"}],
            "codecProfiles": [{"name": "mp3", "limitations": [
                {"name": "audioBitrate", "comparison": "LessThanEqual", "values": ["160000"],
                 "required": True},
                {"name": "audioSamplerate", "comparison": "LessThanEqual", "values": ["48000"],
                 "required": True}]}]}
    d = _decide(env, "hf", info)
    assert d["transcodeReason"] == ["audio codec not supported"]
    ts = d["transcodeStream"]
    assert ts["codec"] == "mp3" and ts["audioBitrate"] == 160000
    # A sample-rate cap above the source's rate never resamples (up).
    assert "audioSamplerate" not in ts
    assert subsonic._verify_token(d["transcodeParams"])["tbr"] == 160


# ── r4-sub-5: random picks skip songs whose source is offline ────────────────

LIVE_ROOT, DEAD_ROOT, FTP_ROOT = "/music/live", "/music/dead", "ftp://nas.example/Music/Demo"


def _rooted(env, root, n, *, album="", artist="Rooted", genre=("Chip",), status="ok"):
    env.store.upsert_scan_dir(root, status=status)
    rh = env.store.store_hash_lookup(root)
    for i in range(n):
        tid = f"{rh[:4]}-{i:03d}"
        _add(env, tid=tid, title=f"{root} {i}", artist=artist, album=album,
              genre=list(genre), added=4000 + i, dir_path=f"{root}/d")
        env.store.upsert_track(dict(env.store.get_track(tid), scan_root_hash=rh))
    return rh


def test_random_songs_skip_offline_roots(env):
    live = _rooted(env, LIVE_ROOT, 30, album="Live LP", artist="L")
    dead = _rooted(env, DEAD_ROOT, 200, album="Dead LP", artist="D", status="unavailable")
    ftp = _rooted(env, FTP_ROOT, 100, album="Ftp LP", artist="F")    # no connected share
    assert subsonic._dead_root_hashes(env.store) == {dead, ftp}
    roots = lambda songs: {env.store.get_track(s["id"])["scan_root_hash"] for s in songs}  # noqa: E731
    for _ in range(5):
        songs = env.ok("getRandomSongs.view", size=40)["randomSongs"]["song"]
        assert len(songs) == 30 and roots(songs) == {live}
        g = env.ok("getRandomSongs.view", size=10, genre="Chip")["randomSongs"]["song"]
        assert len(g) == 10 and roots(g) == {live}
        albums = env.ok("getAlbumList2.view", type="random", size=500)["albumList2"]["album"]
        names = {a["name"] for a in albums}
        assert "Live LP" in names and not names & {"Dead LP", "Ftp LP"}
    # A folder asked for by name is served even while offline.
    fid = next(x["id"] for x in subsonic._music_folders(env.store) if x["hash"] == dead)
    assert roots(env.ok("getRandomSongs.view", size=5, musicFolderId=fid)
                 ["randomSongs"]["song"]) == {dead}
    # Listings still show the offline songs and albums.
    names = {a["name"] for a in env.ok("getAlbumList2.view", type="newest", size=500)
             ["albumList2"]["album"]}
    assert {"Dead LP", "Ftp LP"} <= names
    assert env.ok("search3.view", query="Dead LP", songCount=5)["searchResult3"]["album"]
    # Everything offline: still an answer.
    env.store.set_scan_dir_status(LIVE_ROOT, "unavailable")
    assert len(env.ok("getRandomSongs.view", size=10)["randomSongs"]["song"]) == 10
    assert len(env.ok("getRandomSongs.view", size=10, genre="Chip")["randomSongs"]["song"]) == 10
    assert env.ok("getAlbumList2.view", type="random", size=5)["albumList2"]["album"]


def test_random_songs_filtered_both_branches(env, monkeypatch):
    """Mostly reachable: an oversampled draw; mostly offline: the reachable
    roots' share by set intersection.  Both answer reachable songs only."""
    import random
    live = _rooted(env, LIVE_ROOT, 300, genre=("Chip",))
    dead = _rooted(env, DEAD_ROOT, 40, genre=("Chip",), status="unavailable")
    draws = []
    real = random.sample
    monkeypatch.setattr(random, "sample", lambda pop, k: draws.append(k) or real(pop, k))
    roots = lambda songs: {env.store.get_track(s["id"])["scan_root_hash"] for s in songs}  # noqa: E731
    for _ in range(10):
        g = env.ok("getRandomSongs.view", size=10, genre="Chip")["randomSongs"]["song"]
        assert len(g) == 10 and roots(g) == {live}
    assert 30 in draws                                   # the oversampled draw (3 × size)
    # Now mostly offline: no oversampling, the exact path.
    env.store.set_scan_dir_status(LIVE_ROOT, "unavailable")
    env.store.set_scan_dir_status(DEAD_ROOT, "ok")
    draws.clear()
    g = env.ok("getRandomSongs.view", size=10, genre="Chip")["randomSongs"]["song"]
    assert len(g) == 10 and roots(g) == {dead} and draws == [10]
    # A filter matching nothing at all: an empty list, not an error.
    assert env.ok("getRandomSongs.view", size=10, genre="Nope")["randomSongs"].get("song", []) == []


def test_random_songs_unchanged_when_everything_is_reachable(env, monkeypatch):
    _rooted(env, LIVE_ROOT, 30)
    assert subsonic._dead_root_hashes(env.store) == frozenset()
    keys = subsonic._random_track_keys(env.store, None, frozenset())
    assert len(keys) == len(env.store._tracks)                  # the whole library


# ── r4-sub-6: the album-order memo dies with its catalogue snapshot ──────────

def test_album_order_memo_dies_with_its_snapshot(env):
    assert not hasattr(subsonic, "_ALBUM_ORDER")
    aid = sx.album_id("queen", "a night at the opera")
    cat1 = subsonic._catalogue(env.store)
    order, pos = subsonic._album_order(env.store, cat1, aid)
    assert [k[0] for k in order] == ["q1", "q2", "q3"] and pos[("q2", 0)] == 1
    assert subsonic._album_order(env.store, cat1, aid)[0] is order        # memo hit
    ref = weakref.ref(cat1)
    _add(env, tid="new1", title="New", artist="Someone", added=9000)
    cat2 = subsonic._catalogue(env.store)
    assert cat2 is not cat1
    # Another album on the new snapshot: nothing may keep the old one alive.
    subsonic._album_order(env.store, cat2, sx.album_id("various artists", "hits 1990"))
    del cat1, order, pos
    gc.collect()
    assert ref() is None
    assert aid not in cat2.memo.get("album_order", {})


# ── r4-sub-10: word-wise artist / album search ───────────────────────────────

def test_search_artists_and_albums_word_wise(env):
    _add(env, tid="sw1", title="Imperial March", artist="John Williams",
         album="Star Wars Trilogy (d2) The Empire Strikes Back", added=7000)

    def _res(q):
        r = env.ok("search3.view", query=q, songCount=0)["searchResult3"]
        return ({a["name"] for a in r.get("artist", [])},
                {a["name"] for a in r.get("album", [])})
    for q in ("hubbard rob", '"rob hubbard"', "hubbard*", "ROB  hubbard"):
        assert "Rob Hubbard" in _res(q)[0], q
    assert _res("star wars empire")[1] == {"Star Wars Trilogy (d2) The Empire Strikes Back"}
    assert _res("empire wars")[1] == {"Star Wars Trilogy (d2) The Empire Strikes Back"}
    assert _res("star wars zebra") == (set(), set())
    everything = _res("")
    assert "Queen" in everything[0] and "A Night at the Opera" in everything[1]
    assert _res("genre:rock") == (set(), set())                 # operator-only


# ── r4-sub-11: transcode links are bound to the account, not the address ────

def test_transcode_link_works_after_an_address_change(env, monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.responses import Response
    from soniqboom.api import stream as stream_mod
    served = []

    async def _fake(**kw):
        served.append(kw["track_id"])
        return Response(b"ogg", media_type="audio/ogg")
    monkeypatch.setattr(stream_mod, "stream_track", _fake)
    d = _decide(env, "q1", {"directPlayProfiles": [{"audioCodecs": ["flac"]}],
                            "transcodingProfiles": [{"audioCodec": "ogg"}]})
    tp = d["transcodeParams"]
    assert subsonic._verify_token(tp)["uid"] == "u1"
    params = {"mediaId": "q1", "mediaType": "song", "transcodeParams": tp}
    for host in ("192.168.1.20", "2001:db8::20", "10.1.2.3"):   # Wi-Fi, IPv6, cellular
        r = TestClient(env.client.app, client=(host, 5000)).get(
            "/rest/getTranscodeStream.view", params=params)
        assert r.status_code == 200 and r.content == b"ogg", host
    assert served == ["q1"] * 3
    # Another account presenting the link: refused.
    env.user = types.SimpleNamespace(id="u2", username="mallory", role="edit", enabled=True)
    r = env.client.get("/rest/getTranscodeStream.view", params=params)
    assert r.status_code == 403 and len(served) == 3
    # A link minted before links carried the account still works.
    legacy = subsonic._sign_token({k: v for k, v in subsonic._verify_token(tp).items()
                                   if k != "uid"})
    r = env.client.get("/rest/getTranscodeStream.view",
                       params={**params, "transcodeParams": legacy})
    assert r.status_code == 200


# ── r4-sub-12 / r4-sub-20: cover-art sizes and the placeholder ───────────────

@pytest.mark.parametrize("size, want", [
    (1, "sm"), (200, "sm"), (201, "lg"), (300, "lg"), (550, "lg"), (551, "full"),
    (600, "full"), (1200, "full"), (None, "full"), (0, "full"), (-5, "full")])
def test_cover_bucket_is_never_smaller_than_asked(size, want):
    assert subsonic._cover_bucket(size) == want


def test_cover_art_sizes_serve_the_bucket_at_least_that_big(env, monkeypatch):
    from soniqboom.core import art_cache
    from soniqboom.core import metadata
    resizes = []

    def _resize(data, size, quality=85):
        resizes.append(size)
        return b"\xff\xd8thumb%d" % size

    async def _store(tid, data, bucket):
        return None
    monkeypatch.setattr(metadata, "resize_cover", _resize)
    monkeypatch.setattr(art_cache, "store_art", _store)
    for size, px in ((200, 200), (201, 550), (300, 550), (550, 550)):
        resizes.clear()
        r = env.client.get("/rest/getCoverArt.view", params={"id": "q2", "size": size})
        assert r.content == b"\xff\xd8thumb%d" % px and resizes == [px], size
    for params in ({"size": 551}, {"size": 600}, {}):
        resizes.clear()
        r = env.client.get("/rest/getCoverArt.view", params={"id": "q2", **params})
        assert r.content.startswith(b"\xff\xd8JPEG") and resizes == [], params


def _png_size(content: bytes) -> tuple[int, int]:
    import io
    from PIL import Image
    return Image.open(io.BytesIO(content)).size


def test_placeholder_is_the_512_icon_unless_a_small_tile_was_asked(env, monkeypatch):
    miss = "fa:" + "0" * 16
    small = env.client.get("/rest/getCoverArt.view", params={"id": miss, "size": 100})
    assert small.headers["content-type"] == "image/png" and _png_size(small.content) == (192, 192)
    assert _png_size(env.client.get("/rest/getCoverArt.view",
                                    params={"id": miss, "size": 192}).content) == (192, 192)
    for params in ({"size": 1000}, {"size": 193}, {}):
        r = env.client.get("/rest/getCoverArt.view", params={"id": miss, **params})
        assert _png_size(r.content) == (512, 512), params
    # getAvatar keeps the small icon.
    r = env.client.get("/rest/getAvatar.view", params={"username": "alice"})
    assert _png_size(r.content) == (192, 192)
    # No 512 icon bundled: the 192 one; no icon at all: the built-in 1×1.
    real = subsonic._icon_bytes
    monkeypatch.setattr(subsonic, "_icon_bytes",
                        lambda n: None if n == "icon-512.png" else real(n))
    assert _png_size(env.client.get("/rest/getCoverArt.view",
                                    params={"id": miss}).content) == (192, 192)
    monkeypatch.setattr(subsonic, "_icon_bytes", lambda n: None)
    assert env.client.get("/rest/getCoverArt.view",
                          params={"id": miss}).content == subsonic._FALLBACK_PNG


# ── r4-sub-13: settingsRole for every account ────────────────────────────────

def test_every_account_may_change_its_own_settings(env, users):
    env.user = users.get_by_username("bob")                        # an edit account
    me = env.ok("getUser.view")["user"]
    assert me["settingsRole"] is True and me["adminRole"] is False
    users.create(username="rita", password="ritapass12", role="readonly")
    env.user = users.get_by_username("rita")
    me = env.ok("getUser.view", username="rita")["user"]
    assert me["settingsRole"] is True and me["adminRole"] is False and me["streamRole"] is True


# ── r4-sub-15: getArtistInfo clamps count ────────────────────────────────────

def test_artist_info_count_is_clamped_not_an_error(env, monkeypatch):
    seen = []
    real = subsonic._scene_similar
    monkeypatch.setattr(subsonic, "_scene_similar",
                        lambda store, low, count: seen.append(count) or real(store, low, count))
    aid = sx.artist_id("Rob Hubbard")
    for m in ("getArtistInfo.view", "getArtistInfo2.view"):
        env.ok(m, id=aid, count=200)
    assert env.fail("getArtistInfo2.view", id=aid, count=-1)["code"] == 10
    assert all(c <= 100 for c in seen)


# ── r4-sub-16: p together with t + s is error 43 ─────────────────────────────

def test_password_and_token_together_are_43(env, users, monkeypatch):
    import hashlib
    from test_subsonic_parity import _REAL_REQUIRE_USER
    monkeypatch.setattr(subsonic, "_require_user", _REAL_REQUIRE_USER)
    admin = users.get_by_username("admin")
    salt = "c19b2d"
    tok = hashlib.md5((admin.subsonic_password + salt).encode()).hexdigest()
    fails, auths = [], []
    monkeypatch.setattr(users, "note_failed_attempt", lambda *a, **k: fails.append(a))
    real_auth = users.authenticate
    monkeypatch.setattr(users, "authenticate", lambda *a, **k: auths.append(a) or real_auth(*a))
    both = {"u": "admin", "p": "adminpass1", "t": tok, "s": salt}
    assert env.fail("ping.view", **both)["code"] == 43
    assert env.fail("ping.view", **{**both, "p": "wrong-password"})["code"] == 43
    assert env.fail("ping.view", **{**both, "t": "0" * 32})["code"] == 43
    assert fails == [] and auths == []                  # nothing verified, nothing counted
    env.ok("ping.view", u="admin", p="adminpass1")
    env.ok("ping.view", u="admin", t=tok, s=salt)
    # Account changes (main password only) refuse the mix the same way.
    assert env.fail("changePassword.view", username="admin", password="newpass123",
                    **both)["code"] == 43


# ── r4-sub-17: radio station logos ───────────────────────────────────────────

def _logo_png(px=64, color=(255, 0, 0, 128)) -> bytes:
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGBA", (px, px), color).save(b, format="PNG")
    return b.getvalue()


def _logo_ico(px=48) -> bytes:
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGBA", (px, px), (0, 0, 255, 255)).save(b, format="ICO", sizes=[(px, px)])
    return b.getvalue()


@pytest.fixture()
def logo_net(radio_favs, monkeypatch):
    """Station favicons served by a mock transport; ``*.example`` hosts pass
    the SSRF guard, IP literals go through the real one."""
    import httpx
    from soniqboom.api import stations as stations_mod
    from soniqboom.core import ssrf_proxy
    routes: dict[str, httpx.Response] = {}
    hits: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        resp = routes.get(str(request.url))
        return resp if resp is not None else httpx.Response(404)
    real_client = httpx.AsyncClient

    def _client(**kw):
        kw.pop("mounts", None)
        return real_client(transport=httpx.MockTransport(_handler), **kw)
    monkeypatch.setattr(httpx, "AsyncClient", _client)
    real_guard = stations_mod._assert_public_url

    async def _guard(url):
        from urllib.parse import urlsplit
        if (urlsplit(url).hostname or "").endswith(".example"):
            return None
        await real_guard(url)
    monkeypatch.setattr(stations_mod, "_assert_public_url", _guard)
    monkeypatch.setattr(ssrf_proxy, "proxy_url", lambda: None)
    subsonic._LOGO_TASKS.clear()
    favs = radio_favs.get_favorites()
    favs[0]["favicon"] = "https://a.example/logo.png"
    favs[1]["favicon"] = "https://b.example/favicon.ico"
    favs.append({"sid": "rb:d", "name": "D", "favicon": "javascript:alert(1)",
                 "streams": [{"url": "http://d.example/s"}]})
    radio_favs._write_json(radio_favs._FAVS_FILE, favs)
    return types.SimpleNamespace(routes=routes, hits=hits, radiodir=radio_favs)


def _set_logo(net, sid, url):
    favs = net.radiodir.get_favorites()
    for f in favs:
        if f["sid"] == sid:
            f["favicon"] = url
    net.radiodir._write_json(net.radiodir._FAVS_FILE, favs)
    return subsonic._station_cover_id(sid)


def test_station_listing_sends_cover_art_only_for_a_logo(env, logo_net):
    rows = {r["id"]: r for r in env.ok("getInternetRadioStations.view")
            ["internetRadioStations"]["internetRadioStation"]}
    assert rows["scene:a"]["coverArt"] == subsonic._station_cover_id("scene:a")
    assert rows["scene:a"]["coverArt"].startswith("rs:") and len(rows["scene:a"]["coverArt"]) == 19
    assert rows["rb:b"]["coverArt"] != rows["scene:a"]["coverArt"]
    assert "coverArt" not in rows["rb:d"]                    # not an http(s) URL
    doc = ET.fromstring(env.get("getInternetRadioStations.view", f="xml").content)
    els = {e.get("id"): e for e in doc.findall(".//{*}internetRadioStation")}
    assert els["scene:a"].get("coverArt") == rows["scene:a"]["coverArt"]
    assert env.fail("getCoverArt.view", id="rs:zz")["code"] == 70


def test_station_logo_fetched_once_and_served(env, logo_net):
    import httpx
    logo_net.routes["https://a.example/logo.png"] = httpx.Response(
        200, content=_logo_png(64), headers={"content-type": "image/png"})
    logo_net.routes["https://b.example/favicon.ico"] = httpx.Response(
        200, content=_logo_ico(48), headers={"content-type": "image/x-icon"})
    cid_a = subsonic._station_cover_id("scene:a")
    r = env.client.get("/rest/getCoverArt.view", params={"id": cid_a, "size": 300})   # cold
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert _png_size(r.content) == (64, 64)                        # never upscaled
    assert logo_net.hits == ["https://a.example/logo.png"]
    etag = r.headers["etag"]
    again = env.client.get("/rest/getCoverArt.view", params={"id": cid_a, "size": 300})
    assert again.content == r.content and len(logo_net.hits) == 1  # warm: from disk
    assert env.client.get("/rest/getCoverArt.view", params={"id": cid_a, "size": 300},
                          headers={"If-None-Match": etag}).status_code == 304
    # A different station, an ICO logo, the small rendition.
    cid_b = subsonic._station_cover_id("rb:b")
    r = env.client.get("/rest/getCoverArt.view", params={"id": cid_b, "size": 100})
    assert r.headers["content-type"] == "image/png" and _png_size(r.content) == (48, 48)
    assert logo_net.hits[-1] == "https://b.example/favicon.ico"
    # A big logo is stored at ≤ 512 px, ≤ 200 px for small tiles.
    url = "https://a.example/big.png"
    logo_net.routes[url] = httpx.Response(200, content=_logo_png(1000),
                                          headers={"content-type": "image/png"})
    cid = _set_logo(logo_net, "scene:a", url)
    assert _png_size(env.client.get("/rest/getCoverArt.view",
                                    params={"id": cid, "size": 600}).content) == (512, 512)
    assert _png_size(env.client.get("/rest/getCoverArt.view",
                                    params={"id": cid, "size": 150}).content) == (200, 200)


@pytest.mark.parametrize("target", ["http://127.0.0.1/logo.png",
                                    "http://169.254.169.254/latest/meta-data/",
                                    "http://[::1]/x.png"])
def test_station_logo_redirect_to_a_private_address_is_refused(env, logo_net, target):
    import httpx
    logo_net.routes["https://a.example/logo.png"] = httpx.Response(
        302, headers={"location": target})
    logo_net.routes[target] = httpx.Response(200, content=_logo_png(),
                                             headers={"content-type": "image/png"})
    r = env.client.get("/rest/getCoverArt.view",
                       params={"id": subsonic._station_cover_id("scene:a")})
    assert r.content == subsonic._icon_bytes("icon-512.png")      # the placeholder
    assert "max-age=300" in r.headers["cache-control"]
    assert logo_net.hits == ["https://a.example/logo.png"]         # target never fetched


@pytest.mark.parametrize("resp", [
    dict(content=b"<html>nope</html>" * 20, headers={"content-type": "text/html"}),
    dict(content=b"not an image at all" * 20, headers={"content-type": "image/png"}),
    dict(content=b"\0" * (1024 * 1024 + 1), headers={"content-type": "image/png"}),
])
def test_station_logo_non_image_or_oversize_is_the_placeholder(env, logo_net, resp):
    import httpx
    logo_net.routes["https://a.example/logo.png"] = httpx.Response(200, **resp)
    cid = subsonic._station_cover_id("scene:a")
    r = env.client.get("/rest/getCoverArt.view", params={"id": cid})
    assert r.content == subsonic._icon_bytes("icon-512.png")
    # The failure is remembered: no refetch on the next request.
    env.client.get("/rest/getCoverArt.view", params={"id": cid})
    assert len(logo_net.hits) == 1


def test_station_logo_for_an_unknown_station_is_the_placeholder(env, logo_net):
    r = env.client.get("/rest/getCoverArt.view",
                       params={"id": subsonic._station_cover_id("rb:gone"), "size": 100})
    assert r.content == subsonic._icon_bytes("icon-192.png") and logo_net.hits == []


# ── r4-sub-19: an unknown year is omitted ────────────────────────────────────

def test_unknown_year_is_omitted_everywhere(env):
    for i in range(2):
        _add(env, tid=f"ny{i}", title=f"Undated {i}", artist="Nodate", album="Undated LP",
             added=8000 + i)
        env.store.upsert_track(dict(env.store.get_track(f"ny{i}"), year=None))
    env.folder_albums(True)
    _add(env, tid="nyf", title="Loose", artist="Nodate", dir_path="/music/nodate", added=8100)
    env.store.upsert_track(dict(env.store.get_track("nyf"), year=0))
    al = sx.album_id("nodate", "undated lp")
    fa = "fa:" + env.store.store_hash_lookup("/music/nodate")
    lst = {a["id"]: a for a in env.ok("getAlbumList2.view", type="newest", size=500)
           ["albumList2"]["album"]}
    assert "year" not in lst[al] and "year" not in lst[fa]
    assert lst[sx.album_id("queen", "a night at the opera")]["year"] == 1990
    for aid in (al, fa):
        alb = env.ok("getAlbum.view", id=aid)["album"]
        assert "year" not in alb and all("year" not in s for s in alb["song"])
        doc = ET.fromstring(env.get("getAlbum.view", id=aid, f="xml").content)
        assert all(el.get("year") is None for el in doc.iter()
                   if el.tag.endswith(("album", "song")))
    assert "year" not in env.ok("getSong.view", id="ny0")["song"]
    assert env.ok("getSong.view", id="q1")["song"]["year"] == 1990
    doc = ET.fromstring(env.get("getAlbumList2.view", type="newest", size=500, f="xml").content)
    years = {el.get("id"): el.get("year") for el in doc.findall(".//{*}album")}
    assert years[al] is None and years[fa] is None
    assert years[sx.album_id("queen", "a night at the opera")] == "1990"


# ── r4-sub-22: one accent fold for names and songs ───────────────────────────

def test_name_fold_matches_song_search_fold():
    from soniqboom.core.store import fold_token
    for s in ("が", "й", "Öörni", "Ødegaard", "Beyoncé", "Łódź", "straße"):
        assert subsonic._fold(s) == fold_token(s.lower()), s
    assert subsonic._fold("Öörni") == "oorni" and subsonic._fold("Ødegaard") == "odegaard"
    assert subsonic._fold("が") != subsonic._fold("か")
    assert subsonic._fold("й") != subsonic._fold("и")
    assert not hasattr(subsonic, "_FOLD_EXTRA")


# ── r4-sub-14: one artist's same-name folder albums are told apart ───────────

def test_same_name_folder_albums_get_a_qualifier(env):
    env.store._aof = lambda *a, **k: None
    for root in ("/music/C64Music", "/music/ym", "/music/uade"):
        env.store.upsert_scan_dir(root)
    dirs = {"/music/C64Music/MUSICIANS/W/Whittaker_David": "SID",
            "/music/ym/cta-ym/whittaker,_david": "YM",
            "/music/uade/David Whittaker": "David Whittaker"}
    for n, (d, fmt) in enumerate(dirs.items()):
        for k in range(2):
            _add(env, tid=f"dw{n}{k}", title=f"Tune {n}{k}", artist="David Whittaker",
                 dir_path=d, added=6000 + 10 * n + k, fmt=fmt)
    # Same family twice: the parent folder tells them apart.
    for n, d in enumerate(("/music/C64Music/GAMES/Jeroen_Tel", "/music/ym/cta-ym/Tel_Jeroen")):
        _add(env, tid=f"jt{n}", title=f"JT {n}", artist="Jeroen Tel", dir_path=d,
             added=6100 + n, fmt="SID")
    env.folder_albums(True)
    ids = {d: "fa:" + env.store.store_hash_lookup(d) for d in dirs}
    lst = {a["id"]: a["name"] for a in env.ok("getAlbumList2.view", type="alphabeticalByName",
                                               size=500)["albumList2"]["album"]}
    names = [lst[ids[d]] for d in dirs]
    assert names == ["David Whittaker (C64)", "David Whittaker (Atari ST)",
                     "David Whittaker (Amiga)"]
    jt = sorted(n for i, n in lst.items() if n.startswith("Jeroen Tel"))
    assert jt == ["Jeroen Tel (C64, GAMES)", "Jeroen Tel (C64, cta-ym)"]
    # Every surface agrees on the name.
    for d, want in zip(dirs, names):
        assert env.ok("getAlbum.view", id=ids[d])["album"]["name"] == want
        assert env.ok("getMusicDirectory.view", id=ids[d])["directory"]["name"] == want
        songs = env.ok("getAlbum.view", id=ids[d])["album"]["song"]
        assert {s["album"] for s in songs} == {want}
    art = env.ok("getArtist.view", id=sx.artist_id("David Whittaker"))["artist"]["album"]
    assert sorted(a["name"] for a in art) == sorted(names)
    # A unique name is left alone.
    assert "Queen" not in {n for n in lst.values() if "(" in n}


# ── r4-sub-23: the Subsonic prewarm uses the stream module's registry ────────

def test_subsonic_prewarm_shares_the_stream_registry(env, monkeypatch):
    from soniqboom.api import stream as stream_mod
    from soniqboom.core import data as data_mod
    monkeypatch.setattr(data_mod, "get_store", lambda: env.store)
    started = []

    async def _do(track_id, track, subsong, priority=None):
        started.append((track_id, subsong, priority))
        await asyncio.sleep(3600)                        # still running
    monkeypatch.setattr(stream_mod, "_do_prewarm", _do)
    stream_mod._prewarm_tasks.clear()
    stream_mod._prewarm_owner.clear()
    for i in range(stream_mod._PREWARM_CAP + 1):
        _add(env, tid=f"pw{i}", title=f"PW {i}", artist="Warm", fmt="SID",
             dir_path="/music/C64/warm")

    async def _run():
        await subsonic._start_prewarm("pw0", 0, ".sid", "alice")
        await subsonic._start_prewarm("pw0", 0, ".sid", "bob")      # joins the running task
        await asyncio.sleep(0)
        assert len(stream_mod._prewarm_tasks) == 1
        (key, owners), = stream_mod._prewarm_owner.items()
        assert key.startswith("pw0::") and owners == {"alice", "bob"}
        for i in range(1, stream_mod._PREWARM_CAP + 1):
            await subsonic._start_prewarm(f"pw{i}", 0, ".sid", "alice")
        await asyncio.sleep(0)
        assert len(stream_mod._prewarm_tasks) == stream_mod._PREWARM_CAP
        assert not any(k.startswith("pw0::") for k in stream_mod._prewarm_tasks)   # oldest out
        for tk in list(stream_mod._prewarm_tasks.values()):
            tk.cancel()
        await asyncio.sleep(0)
    asyncio.run(_run())
    # the bare id: the file's default tune (None), as the stream plays it
    assert started[0] == ("pw0", None, stream_mod.PRIO_AHEAD)
    stream_mod._prewarm_tasks.clear()
    stream_mod._prewarm_owner.clear()


def test_subsonic_prewarm_setting_holds_back_rendered_formats_only(env, monkeypatch):
    from starlette.responses import Response
    from soniqboom.api import stream as stream_mod
    from soniqboom.core import data as data_mod
    monkeypatch.setattr(data_mod, "get_store", lambda: env.store)

    async def _fake(**kw):
        return Response(b"x", media_type="audio/wav")
    monkeypatch.setattr(stream_mod, "stream_track", _fake)
    started = []

    async def _do(track_id, track, subsong, priority=None):
        started.append(track_id)
    monkeypatch.setattr(stream_mod, "_do_prewarm", _do)
    stream_mod._prewarm_tasks.clear()
    stream_mod._prewarm_owner.clear()
    _add(env, tid="cur", title="Cur", artist="Hifi", fmt="FLAC")
    _add(env, tid="nsid", title="Next SID", artist="Hifi", fmt="SID")
    _add(env, tid="ndsd", title="Next DSD", artist="Hifi", fmt="DSD")
    for tid, suffix in (("cur", "flac"), ("nsid", "sid"), ("ndsd", "dsf")):
        t = env.store.get_track(tid)
        env.store.upsert_track(dict(t, path=f"/music/misc/{tid}.{suffix}"))
    env.store.set_config("render_prewarm", False)
    env.ok("savePlayQueue.view", id=["cur", "nsid", "cur", "ndsd"], current="cur")
    env.client.get("/rest/stream.view", params={"id": "cur"})
    assert started == []                                 # a SID: held back by the setting
    env.ok("savePlayQueueByIndex.view", id=["cur", "nsid", "cur", "ndsd"], currentIndex=2)
    env.client.get("/rest/stream.view", params={"id": "cur"})
    assert started == ["ndsd"]                           # a DSD conversion: still prepared
    stream_mod._prewarm_tasks.clear()
    stream_mod._prewarm_owner.clear()
