# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Subsonic response pipeline: the XML string builder, the orjson encoder and
the song memo.

  * XML: ``_envelope_to_xml`` is held to the ElementTree serializer it
    replaced — copied below verbatim as the reference — byte for byte over
    thousands of randomized envelopes and every envelope a sweep of real
    endpoints produces.  The one deliberate difference: a lone surrogate is
    "?" (ElementTree wrote a ``&#55296;`` reference, which XML forbids — the
    whole document failed to parse).
  * JSON: orjson output means what the stdlib encoder's did (same values, same
    key order, compact), byte for byte when no float is involved; a non-finite
    float is ``null`` and a lone surrogate "?" instead of a failed response;
    what orjson refuses (an int beyond 64 bits) falls back to the stdlib.
  * Song memo: ``_track_to_song`` — cold and warm — equals the pre-memo mapper
    (copied below verbatim) key for key and in order; every way a song's
    inputs can change (each store write path, scan roots, the folder-albums
    flag, folder-album membership, the caller's own stars / ratings / plays /
    bookmarks) shows in the next response; callers can't corrupt it; and the
    memo stays invisible to the cyclic GC.
"""
from __future__ import annotations

import gc
import json
import math
import random
import re
import types
import xml.etree.ElementTree as ET

import pytest

from soniqboom.api import subsonic as S
from soniqboom.core import subsonic_index as sx
from soniqboom.core import subsonic_state
from soniqboom.core.store import TrackStore

from test_subsonic_parity import env  # noqa: F401 — fixture


# ═════════════════════════════════════════════════════════════════════════════
# Reference implementations — the pre-change code, verbatim.
# ═════════════════════════════════════════════════════════════════════════════

_ET = ET                                # the name the reference code uses


def _xml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _xml_walk(parent: _ET.Element, tag: str, value) -> None:
    if value is None:
        return
    if isinstance(value, dict):
        elem = _ET.SubElement(parent, tag)
        for k, v in value.items():
            if k == "_text":
                continue
            if isinstance(v, (dict, list)):
                continue
            if v is None:
                continue
            elem.set(k, _xml_scalar(v))
        if value.get("_text") is not None:
            elem.text = _xml_scalar(value["_text"])
        for k, v in value.items():
            if isinstance(v, (dict, list)):
                _xml_walk(elem, k, v)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                _xml_walk(parent, tag, item)
            else:
                child = _ET.SubElement(parent, tag)
                child.text = _xml_scalar(item)
    else:
        parent.set(tag, _xml_scalar(value))


_XML_ILLEGAL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]|\xef\xbf[\xbe\xbf]")
_XML_C0_DELETE = bytes(range(0x00, 0x09)) + b"\x0b\x0c" + bytes(range(0x0e, 0x20))


def _xml_strip_illegal(xml: bytes) -> bytes:
    xml = xml.translate(None, _XML_C0_DELETE)
    if b"\xef\xbf\xbe" in xml or b"\xef\xbf\xbf" in xml:
        xml = _XML_ILLEGAL.sub(b"", xml)
    return xml


def _ref_envelope_to_xml(envelope: dict) -> bytes:
    body = envelope.get("subsonic-response", {})
    root = _ET.Element("subsonic-response",
                       attrib={"xmlns": "http://subsonic.org/restapi"})
    for k, v in body.items():
        if isinstance(v, (dict, list)):
            _xml_walk(root, k, v)
        elif v is not None:
            root.set(k, _xml_scalar(v))
    try:
        xml = _ET.tostring(root, encoding="utf-8")
    except UnicodeEncodeError:
        xml = _ET.tostring(root, encoding="unicode").encode("utf-8", "replace")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + _xml_strip_illegal(xml)


# The pre-memo song mapper, verbatim, over the (unchanged) module helpers.
_sx = sx
_delivery = S._delivery
_artist_id = S._artist_id
_album_id = S._album_id
_normalise_year = S._normalise_year
_safe_int = S._safe_int
_client_path = S._client_path
_iso = S._iso
_ts_num = S._ts_num


def _ref_track_to_song(t: dict, ctx=None) -> dict:
    aa = _sx.owner_of(t)
    al = (t.get("album") or "").strip()
    genre_list = t.get("genre") or []
    genre = genre_list[0] if genre_list else ""
    suffix, content_type, t_suffix, t_mime = _delivery(t)
    ar_tag = t.get("artist")
    ar_tag = ar_tag.strip() if isinstance(ar_tag, str) else ""
    ar = _sx.norm_owner(ar_tag) or aa
    ids = ctx.artist_ids if ctx is not None else None
    owner_id = ids.get(aa) if ids is not None else None
    if owner_id is None:
        owner_id = _artist_id(aa)
        if ids is not None:
            ids[aa] = owner_id
    if ar == aa:
        artist_id = owner_id
    else:
        artist_id = ids.get(ar) if ids is not None else None
        if artist_id is None:
            artist_id = _artist_id(ar)
            if ids is not None:
                ids[ar] = artist_id
    album_id = None
    dh = t.get("dir_hash")
    fixed = ctx.fixed_album if ctx is not None else None
    if al:
        album_id = _album_id(aa, al)
    elif dh and (fixed is not None or (ctx is not None and ctx.folder_on) or not aa):
        if fixed is not None:
            album_id, al = fixed
        elif ctx is not None and ctx.folder_on:
            e = ctx.folder_entry(t)
            if e is not None:
                album_id, al = e.id, e.name
        if album_id is None:
            album_id = _sx.folder_album_id(dh)
            names = ctx.folder_names if ctx is not None else None
            al = names.get(dh) if names is not None else None
            if al is None:
                al = _sx.folder_album_name(ctx.store if ctx is not None else None, dh)
                if names is not None:
                    names[dh] = al
    aa_disp = aa or _sx.UNKNOWN_ARTIST
    dur = t.get("duration")
    if t.get("start_subsong"):
        dur = _sx.default_duration(t) or dur
    out: dict = {
        "id":         t["id"],
        "parent":     album_id or owner_id,
        "isDir":      False,
        "title":      t.get("title") or "",
        "album":      al,
        "artist":     ar_tag or aa_disp,
        "albumArtist": aa,
        "track":      _safe_int(t.get("track_number")),
        "year":       _normalise_year(t.get("year")),
        "genre":      genre,
        "coverArt":   t["id"],
        "size":       _safe_int(t.get("file_size")),
        "contentType": content_type,
        "suffix":     suffix,
        "duration":   _safe_int(dur, round_=True),
        "bitRate":    _safe_int((t.get("bitrate") or 0) / 1000),
        "path":       _client_path(t, ctx, aa_disp, al),
        "isVideo":    False,
        "type":       "music",
        "artistId":   artist_id,
        "discNumber": _safe_int(t.get("disc_number")),
    }
    if not out["year"]:
        del out["year"]
    out["displayArtist"] = out["artist"]
    out["displayAlbumArtist"] = aa_disp
    out["artists"] = [{"id": artist_id, "name": ar or aa_disp}]
    out["albumArtists"] = [{"id": owner_id, "name": aa_disp}]
    if album_id is not None:
        out["albumId"] = album_id
    created = _iso(t.get("added_at"))
    if created:
        out["created"] = created
    if t_suffix:
        out["transcodedContentType"] = t_mime
        out["transcodedSuffix"] = t_suffix
    if ctx is not None:
        tid = t["id"]
        r = ctx.ratings.get(tid)
        if r:
            out["userRating"] = r
        st = ctx.starred.get(tid)
        if st:
            out["starred"] = _iso(st)
        ps = ctx.plays.get(tid)
        out["playCount"] = _safe_int(ps.get("count")) if isinstance(ps, dict) else 0
        if isinstance(ps, dict) and ps.get("last_played"):
            played = _iso(ps["last_played"])
            if played:
                out["played"] = played
        bm = ctx.marks.get(tid)
        if bm:
            out["bookmarkPosition"] = int(_ts_num(bm.get("position")))
    ch = _safe_int(t.get("channels"))
    if ch:
        out["channelCount"] = ch
    sr = _safe_int(t.get("sample_rate"))
    if sr:
        out["samplingRate"] = sr
    bd = _safe_int(t.get("bit_depth"))
    if bd:
        out["bitDepth"] = bd
    out["mediaType"] = "song"
    out["bpm"] = _safe_int(t.get("bpm"), round_=True)
    out["comment"] = t.get("comment") or ""
    out["displayComposer"] = t.get("composer") or ""
    isrc = t.get("isrc")
    out["isrc"] = [isrc] if isrc and isinstance(isrc, str) else []
    out["genres"] = [{"name": g} for g in genre_list if g]

    def _fin(v, nd):
        if v is None:
            return None
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return None
        return round(fv, nd) if math.isfinite(fv) else None
    rg: dict = {}
    for _k, _src, _nd in (("trackGain", "replaygain_track_gain", 2),
                          ("albumGain", "replaygain_album_gain", 2),
                          ("trackPeak", "replaygain_track_peak", 6),
                          ("albumPeak", "replaygain_album_peak", 6)):
        _v = _fin(t.get(_src), _nd)
        if _v is not None:
            rg[_k] = _v
    out["replayGain"] = rg
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _ordered(obj) -> str:
    """A canonical string that captures values AND key order at every level
    (``==`` on dicts ignores order)."""
    return json.dumps(obj, ensure_ascii=True, allow_nan=True, default=repr)


_SURROGATE_REF = re.compile(rb"&#(\d+);")


def _surrogates_as_question_marks(xml: bytes) -> bytes:
    """The reference's ``&#55296;``-style surrogate references as the "?" the
    builder writes instead (a literal ``&#…;`` in a value is ``&amp;#…;``
    in both outputs, so it never matches)."""
    return _SURROGATE_REF.sub(
        lambda m: b"?" if 0xD800 <= int(m.group(1)) <= 0xDFFF else m.group(0), xml)


class _Str(str):
    def __str__(self):                                   # a str subclass, own __str__
        return "sub<&>" + str.__str__(self)


class _Obj:
    def __str__(self):
        return 'obj "&" <x>'


_PIECES = ["", "plain", "Rock & Roll", "<b>", '"q"', "it's", "a\tb", "l1\nl2", "cr\r",
           "\x00", "\x01\x1f", "\x7f", "￾", "￿", "é", "日本", "\U0001F600",
           " ", "&amp;", "]]>", " ", "&#55296;", "\ud800", "\udfff", "\udce9"]
_KEYS = ["id", "name", "song", "a", "b", "_text", "value", "child", "x_1", "Z",
         "artist", "versions", "entry", "status"]


def _rand_str(rnd: random.Random) -> str:
    return "".join(rnd.choice(_PIECES) for _ in range(rnd.randint(0, 4)))


def _rand_scalar(rnd: random.Random):
    k = rnd.randrange(16)
    if k == 0:
        return rnd.choice([True, False])
    if k == 1:
        return None
    if k == 2:
        return rnd.randint(-10 ** 6, 10 ** 6)
    if k == 3:
        return rnd.choice([0, 2 ** 70, -2 ** 65, 2 ** 63 - 1])
    if k == 4:
        return rnd.choice([float("nan"), float("inf"), float("-inf"), -0.0, 1e16, 1e-5,
                           0.1, 123.456, 1.5e300])
    if k == 5:
        return rnd.uniform(-1e6, 1e6)
    if k == 6:
        return (1, "x&y")                                 # a tuple is a scalar here
    if k == 7:
        return _Str(_rand_str(rnd))
    if k == 8:
        return _Obj()
    return _rand_str(rnd)


def _rand_value(rnd: random.Random, depth: int):
    r = rnd.random()
    if depth >= 4 or r < 0.45:
        return _rand_scalar(rnd)
    if r < 0.75:
        return {rnd.choice(_KEYS): _rand_value(rnd, depth + 1)
                for _ in range(rnd.randint(0, 6))}
    return [_rand_value(rnd, depth + 1) for _ in range(rnd.randint(0, 4))]


def _rand_envelope(rnd: random.Random) -> dict:
    body = {"status": "ok", "version": "1.16.1", "type": "SoniqBoom",
            "serverVersion": "x", "openSubsonic": True}
    for _ in range(rnd.randint(0, 5)):
        body[rnd.choice(_KEYS + ["xmlns"])] = _rand_value(rnd, 0)
    return {"subsonic-response": body}


# ═════════════════════════════════════════════════════════════════════════════
# 1. XML — byte-identical to ElementTree
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("seed", range(20))
def test_xml_builder_matches_elementtree_on_random_envelopes(seed):
    """4,000 random envelopes: nested dicts / lists, every scalar type (bool,
    huge ints, non-finite floats, tuples, str subclasses, objects), strings
    with every escape-relevant / illegal / non-BMP character, ``_text``
    keys (scalar, empty, dict, list), empty containers, lists of None, a
    root ``xmlns`` override.  Identical bytes, save lone surrogates ("?");
    and the builder's output always parses."""
    rnd = random.Random(seed)
    for _ in range(200):
        envlp = _rand_envelope(rnd)
        new = S._envelope_to_xml(envlp)
        ref = _ref_envelope_to_xml(envlp)
        assert new == _surrogates_as_question_marks(ref), envlp
        ET.fromstring(new)                      # well-formed, whatever the input


def test_xml_builder_edge_cases_are_byte_identical():
    cases = [
        {},                                                     # bare root: self-closing
        {"ping": {}},                                           # empty element
        {"a": {"b": None, "c": [], "d": {}}},                   # nothing produced inside
        {"lyrics": {"artist": "x", "_text": ""}},               # empty text: self-closing
        {"lyrics": {"artist": "x", "_text": "a & <b>\r\n\t"}},  # text escaping (no \r ref)
        {"lyrics": {"_text": {"k": 1}}},                        # dict under _text → child
        {"lyrics": {"_text": [1, None, ""]}},                   # list under _text
        {"x": {"_text": 0}},                                    # falsy-but-present text
        {"x": {"_text": False}},
        {"versions": [1, None, "", True, 2.5, [3, [4]], {"k": "v"}]},
        {"xmlns": "override", "_text": "root text is an attribute"},
        {"a": {"v": 'q"&<>\r\n\t\'', "n": float("nan"), "i": 2 ** 80, "f": 1e16}},
        {"a": [{"id": i, "title": f"T{i}"} for i in range(5)]},
        {"tags": {"v": "\x00\x08\x0b\x0c\x0e\x1f ok ￾￿"}},
    ]
    for payload in cases:
        envlp = S._envelope(payload)
        assert S._envelope_to_xml(envlp) == _ref_envelope_to_xml(envlp), payload


def test_xml_lone_surrogate_degrades_instead_of_breaking_the_document():
    """ElementTree wrote ``path="caf&#56553;.mod"`` — a reference to a
    surrogate, which every strict XML parser rejects, so ONE song with an
    undecodable file name made the whole listing unreadable."""
    envlp = S._envelope({"song": {"id": "x", "path": "caf\udce9.mod", "title": "ok"}})
    ref = _ref_envelope_to_xml(envlp)
    with pytest.raises(ET.ParseError):
        ET.fromstring(ref)                                      # the old failure
    body = S._envelope_to_xml(envlp)
    song = ET.fromstring(body).find("{*}song")
    assert song.get("path") == "caf?.mod" and song.get("title") == "ok"


def test_xml_endpoint_sweep_matches_elementtree(env, monkeypatch):
    """Every envelope a sweep of real endpoints serialises — songs with
    per-user fields, albums, artists, folders, lyrics, errors — byte for byte
    against the ElementTree reference."""
    seen = []
    real = S._envelope_to_xml

    def _spy(envlp):
        out = real(envlp)
        assert out == _ref_envelope_to_xml(envlp)
        seen.append(len(out))
        return out
    monkeypatch.setattr(S, "_envelope_to_xml", _spy)
    S._ALBUM_LIST_CACHE.update(cat=None)
    env.folder_albums(True)
    env.store.set_rating("q1", 4)
    env.store.record_play("q2")
    alb = env.ok("getAlbumList2.view", type="newest", size=50)["albumList2"]["album"]
    calls = [("ping.view", {}), ("getLicense.view", {}), ("getMusicFolders.view", {}),
             ("getOpenSubsonicExtensions.view", {}), ("getIndexes.view", {}),
             ("getArtists.view", {}), ("getGenres.view", {}),
             ("getAlbumList.view", {"type": "alphabeticalByName", "size": 50}),
             ("getAlbumList2.view", {"type": "newest", "size": 50}),
             ("getRandomSongs.view", {"size": 50}), ("search3.view", {"query": "a"}),
             ("getStarred2.view", {}), ("getPlaylists.view", {}), ("getPlayQueue.view", {}),
             ("getNowPlaying.view", {}), ("getScanStatus.view", {}),
             ("getSong.view", {"id": "q1"}), ("getSong.view", {"id": "nope"}),
             ("getTopSongs.view", {"artist": "Queen"}), ("getBookmarks.view", {}),
             ("getLyrics.view", {"artist": "Queen", "title": "Innuendo"}),
             ("getUser.view", {"username": "alice"})]
    calls += [("getAlbum.view", {"id": a["id"]}) for a in alb]
    calls += [("getMusicDirectory.view", {"id": a["id"]}) for a in alb]
    for a in env.ok("getArtists.view")["artists"]["index"]:
        for ar in a["artist"]:
            calls.append(("getArtist.view", {"id": ar["id"]}))
            calls.append(("getMusicDirectory.view", {"id": ar["id"]}))
    for method, params in calls:
        r = env.client.get(f"/rest/{method}", params={**params, "f": "xml"})
        assert r.headers["content-type"].startswith("text/xml")
        ET.fromstring(r.content)
    assert len(seen) >= len(calls)


# ═════════════════════════════════════════════════════════════════════════════
# 2. JSON — orjson, same meaning, never a failed listing over one value
# ═════════════════════════════════════════════════════════════════════════════

def _reject_constant(tok):
    raise AssertionError(f"non-finite JSON constant in body: {tok!r}")


def _pairs(raw: bytes):
    """Parsed with key order kept, strict (no NaN / Infinity tokens)."""
    return json.loads(raw, object_pairs_hook=list, parse_constant=_reject_constant)


def _stdlib(obj) -> bytes:
    """What Starlette's JSONResponse rendered (it raised on non-finite)."""
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, indent=None,
                      separators=(",", ":")).encode("utf-8")


def _rand_json(rnd: random.Random, depth: int, *, floats: bool):
    r = rnd.random()
    if depth >= 4 or r < 0.5:
        k = rnd.randrange(7 if floats else 6)
        return [lambda: rnd.choice([True, False, None]),
                lambda: rnd.randint(-2 ** 63, 2 ** 64 - 1),
                lambda: "".join(rnd.choice([p for p in _PIECES if not
                                            any(0xD800 <= ord(c) <= 0xDFFF for c in p)])
                                for _ in range(rnd.randint(0, 4))),
                lambda: rnd.randint(-5, 5),
                lambda: "",
                lambda: rnd.choice(["Rock & Roll", "日本", "\U0001F600"]),
                lambda: rnd.uniform(-1e9, 1e9)][k]()
    if r < 0.75:
        return {rnd.choice(_KEYS) + str(rnd.randrange(3)): _rand_json(rnd, depth + 1, floats=floats)
                for _ in range(rnd.randint(0, 6))}
    return [_rand_json(rnd, depth + 1, floats=floats) for _ in range(rnd.randint(0, 4))]


@pytest.mark.parametrize("seed", range(10))
def test_json_matches_the_stdlib_encoder(seed):
    """Without floats: the very bytes JSONResponse produced (string escapes,
    key order, compact separators).  With floats: the same values in the same
    order (a float may be spelled differently — ``1e-05`` / ``0.00001``)."""
    rnd = random.Random(seed)
    for _ in range(100):
        p = {"x": _rand_json(rnd, 0, floats=False)}
        assert S._ok(p, fmt="json").body == _stdlib(S._envelope(p))
        q = {"x": _rand_json(rnd, 0, floats=True)}
        assert _pairs(S._ok(q, fmt="json").body) == _pairs(_stdlib(S._envelope(q)))


def test_json_response_shape_unchanged():
    r = S._ok({"a": 1}, fmt="json")
    assert r.headers["content-type"] == "application/json"
    assert r.body == b'{"subsonic-response":{"status":"ok","version":"1.16.1",' \
                     b'"type":"SoniqBoom","serverVersion":"' + S.__version__.encode() + \
                     b'","openSubsonic":true,"a":1}}'
    e = S._err(70, "Song not found.", fmt="json")
    assert e.status_code == 200 and e.headers["content-type"] == "application/json"
    assert _pairs(e.body)[0][1][0] == ("status", "failed")


def test_json_nonfinite_float_is_null_not_a_failed_response():
    """The decision: a non-finite float a mapper didn't sanitise is ``null``.
    The stdlib encoder (allow_nan=False) raised — and @_wrap answered the
    WHOLE request with a code-0 error envelope over one value."""
    payload = {"jukeboxStatus": {"gain": float("nan"), "position": 3,
                                 "rates": [float("inf"), -float("inf"), 1.5]}}
    with pytest.raises(ValueError):
        _stdlib(S._envelope(payload))                   # what the old encoder did
    body = json.loads(S._ok(payload, fmt="json").body, parse_constant=_reject_constant)
    st = body["subsonic-response"]
    assert st["status"] == "ok"
    assert st["jukeboxStatus"] == {"gain": None, "position": 3, "rates": [None, None, 1.5]}


def test_json_lone_surrogate_and_huge_int_fall_back_cleanly():
    """orjson refuses both; the stdlib fallback keeps the int, writes the
    surrogate as "?" (it used to raise UnicodeEncodeError → error envelope)
    and still nulls a non-finite float in the same body."""
    payload = {"song": {"id": "x", "path": "caf\udce9.mod", "big": 2 ** 80,
                        "gain": float("nan"), "title": "日本 & \U0001F600"}}
    with pytest.raises(UnicodeEncodeError):
        json.dumps(S._envelope(payload), ensure_ascii=False).encode("utf-8")
    song = json.loads(S._ok(payload, fmt="json").body,
                      parse_constant=_reject_constant)["subsonic-response"]["song"]
    assert song == {"id": "x", "path": "caf?.mod", "big": 2 ** 80, "gain": None,
                    "title": "日本 & \U0001F600"}
    # The spliced (per item) encoder agrees, item by item.
    big = {"album": {"id": "al:x", "song": [payload["song"], {"id": "y", "n": 1.5}]}}
    assert json.loads(S._json_spliced(big, "album", "song")) == json.loads(S._ok(big, fmt="json").body)


def test_json_spliced_is_byte_identical_to_ok():
    songs = [{"id": str(i), "title": f"T{i} \"&\" é", "n": i, "f": i / 7, "l": [i, None]}
             for i in range(300)]
    p = {"album": {"id": "al:x", "name": "n", "song": songs}}
    assert S._json_spliced(p, "album", "song") == S._ok(p, fmt="json").body


def test_jsonp_wrapper_still_applies(env):
    r = env.client.get("/rest/ping.view", params={"f": "jsonp", "callback": "cb"})
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.content.startswith(b"cb({") and r.content.endswith(b"});")
    assert json.loads(r.content[3:-2])["subsonic-response"]["status"] == "ok"
    plain = env.client.get("/rest/ping.view", params={"f": "jsonp"})
    assert plain.headers["content-type"] == "application/json"


def test_ok_async_offloads_exactly_from_the_thresholds(monkeypatch):
    """Below ``_OFFLOAD_MIN_ITEMS`` / ``_JSON_OFFLOAD_MIN_ITEMS`` a body is
    encoded on the calling (event-loop) thread; from the threshold on, in a
    worker — XML whole, JSON per item (``_json_spliced``).  Same bytes either
    way."""
    import asyncio
    import threading
    where = []
    real_xml, real_spliced = S._envelope_to_xml, S._json_spliced
    monkeypatch.setattr(S, "_envelope_to_xml", lambda e: where.append(
        ("xml", threading.current_thread() is threading.main_thread())) or real_xml(e))
    monkeypatch.setattr(S, "_json_spliced", lambda *a: where.append(
        ("json", threading.current_thread() is threading.main_thread())) or real_spliced(*a))
    p = {"album": {"id": "al:x", "song": [{"id": str(i)} for i in range(3)]}}
    bodies = set()
    for fmt, limit in (("xml", S._OFFLOAD_MIN_ITEMS), ("json", S._JSON_OFFLOAD_MIN_ITEMS)):
        for n in (limit - 1, limit):
            r = asyncio.run(S._ok_async(p, fmt=fmt, n_hint=n, splice=("album", "song")))
            bodies.add((fmt, r.body))
    assert where == [("xml", True), ("xml", False), ("json", False)]
    assert len(bodies) == 2                                    # one body per format


# ═════════════════════════════════════════════════════════════════════════════
# 3. Song memo
# ═════════════════════════════════════════════════════════════════════════════

ROOT = "/music"
MIXED = "/music/C64/Mixed"


def _t(st: TrackStore, tid: str, d: str = "/music/misc", **kw) -> dict:
    t = {"id": tid, "title": f"Title {tid}", "artist": "Artist", "album_artist": "",
         "album": "Album", "genre": ["Rock"], "year": 1990, "added_at": 1_700_000_000,
         "duration": 100.4, "format": "MP3", "path": f"{d}/{tid}.mp3",
         "dir_hash": st.store_hash_lookup(d), "track_number": 1}
    t.update(kw)
    return t


@pytest.fixture()
def lib(monkeypatch, tmp_path):
    """A real TrackStore with every song shape the mapper branches on."""
    subsonic_state.reset_state(tmp_path / "state.json")
    monkeypatch.setattr(S, "_CACHE_DEBOUNCE_SEC", 0.0)
    st = TrackStore()
    st._aof = lambda *a, **k: None
    st.upsert_scan_dir(ROOT)
    nan = float("nan")
    rows = [
        _t(st, "tag1", genre=["Rock", "Pop", ""], isrc="USX", comment="c\nd",
           composer="Comp", channels=2, sample_rate=44100, bit_depth=24, bpm=120.6,
           replaygain_track_gain=-7.123, replaygain_album_gain=nan,
           replaygain_track_peak=0.9876543, file_size=1234, bitrate=320_000),
        _t(st, "comp1", artist="Guest", album_artist="Various Artists", album="Hits"),
        _t(st, "ph1", artist="<?>", album_artist="Real Owner", year=1994),
        _t(st, "noalb1", album="", artist="Composer A", d="/music/C64/A"),
        _t(st, "anon1", album="", artist="", d="/music/mods/unsorted", format="MOD",
           path="/music/mods/unsorted/anon1.mod"),
        _t(st, "mixA", album="", artist="Mix One", d=MIXED, format="SID"),
        _t(st, "mixB", album="", artist="Mix Two", d=MIXED, format="SID"),
        _t(st, "tune1", album="", artist="Composer A", d="/music/C64/A", format="SID",
           subsongs=5, start_subsong=2, hvsc_lengths=[10, 20, 30.6, 40, 50]),
        _t(st, "alac1", format="ALAC", path="/music/misc/alac1.m4a", year=19940101),
        _t(st, "amiga1", format="", path="/music/amiga/mdat.song", album=""),
        _t(st, "outside1", path="/elsewhere/x/outside1.flac", format="FLAC", added_at=nan),
        _t(st, "odd1", artist="", title="", genre=[], duration=nan, track_number=None,
           year=0, album_artist="  "),
    ]
    for r in rows:
        st.upsert_track(r)
    st.set_rating("tag1", 5)
    st.record_play("tag1", at=1_700_000_100)
    st._play_stats["comp1"] = {"count": 2}
    st.set_config("subsonic_folder_albums", True)
    monkeypatch.setattr(S, "get_store", lambda: st)
    S._ALBUM_LIST_CACHE.update(cat=None)
    yield st
    subsonic_state.reset_state()


_USER = types.SimpleNamespace(id="u1", username="alice")


def _user_state(uid="u1"):
    import asyncio
    state = subsonic_state.get_state()
    asyncio.run(state.star(uid, "song", ["tag1", "noalb1"]))
    asyncio.run(state.set_bookmark(uid, "comp1", position=12_345))


def _map_all(st, ctx_factory):
    ctx = ctx_factory()
    return {tid: S._track_to_song(t, ctx) for tid, t in st._tracks.items()}


def _ref_all(st, ctx_factory):
    ctx = ctx_factory()
    return {tid: _ref_track_to_song(t, ctx) for tid, t in st._tracks.items()}


class _Calls:
    """Counts ``_song_static`` calls per track id (a memo miss builds one)."""

    def __init__(self, monkeypatch):
        self.ids: list[str] = []
        real = S._song_static

        def _spy(t, ctx):
            self.ids.append(t["id"])
            return real(t, ctx)
        monkeypatch.setattr(S, "_song_static", _spy)

    def take(self) -> list[str]:
        out, self.ids = self.ids, []
        return out


@pytest.mark.parametrize("folder_on", [True, False])
def test_memo_cold_and_warm_equal_the_pre_memo_mapper(lib, monkeypatch, folder_on):
    """Every song shape, folder albums on and off, with per-user fields: the
    cold (memo empty) and warm (memo hit) results equal the old mapper's, key
    order included — and the warm pass really hits."""
    lib.set_config("subsonic_folder_albums", folder_on)
    _user_state()
    calls = _Calls(monkeypatch)
    S._song_memo_clear()
    mk = lambda: S._song_ctx(lib, _USER)                      # noqa: E731
    want = _ref_all(lib, mk)
    cold = _map_all(lib, mk)
    assert sorted(calls.take()) == sorted(lib._tracks)        # all built
    warm = _map_all(lib, mk)
    assert calls.take() == []                                 # none rebuilt
    for tid in want:
        assert _ordered(cold[tid]) == _ordered(want[tid]), tid
        assert _ordered(warm[tid]) == _ordered(want[tid]), tid
    # The shapes really differ (the comparison isn't vacuous).
    assert want["noalb1"].get("albumId", "").startswith("fa:") == folder_on
    assert want["tune1"]["duration"] == 31                     # the default tune's own length
    assert want["tag1"]["userRating"] == 5 and "starred" in want["tag1"]
    assert want["comp1"]["bookmarkPosition"] == 12_345 and want["comp1"]["playCount"] == 2


def test_memo_without_context_fixed_album_and_stub_store(lib, monkeypatch):
    """No context, a fixed (fa:) album and a stub store never use the memo
    and still map exactly as before."""
    calls = _Calls(monkeypatch)
    S._song_memo_clear()
    for t in lib._tracks.values():
        assert _ordered(S._track_to_song(t)) == _ordered(_ref_track_to_song(t))
    fixed = S._song_ctx(lib, _USER, album=("fa:0123456789abcdef", "Folder X"))
    ref_fixed = S._song_ctx(lib, _USER, album=("fa:0123456789abcdef", "Folder X"))
    for t in lib._tracks.values():
        assert _ordered(S._track_to_song(t, fixed)) == _ordered(_ref_track_to_song(t, ref_fixed))
    assert len(calls.take()) == 2 * len(lib._tracks)          # every call built afresh
    stub = types.SimpleNamespace(get_track=lib.get_track, _ratings={}, _play_stats={},
                                 get_config=lambda k, d=None: False)
    sctx = S._song_ctx(stub, _USER)
    assert sctx.revs is None
    # Shapes no store indexes (a non-str artist, a None title / genre list).
    weird = dict(lib.get_track("tag1"), id="weird", artist=42, title=None, genre=None,
                 track_number="x", year="1994")
    for ctx_ in (None, sctx):
        assert _ordered(S._track_to_song(weird, ctx_)) == _ordered(_ref_track_to_song(weird, ctx_))
    calls.take()
    for t in lib._tracks.values():
        assert _ordered(S._track_to_song(t, sctx)) == \
            _ordered(_ref_track_to_song(t, S._song_ctx(stub, _USER)))
    assert not S._SONG_META                                   # nothing memoised
    assert len(calls.take()) == len(lib._tracks)


def _song(st, tid, user=_USER):
    return S._track_to_song(st.get_track(tid), S._song_ctx(st, user))


@pytest.mark.parametrize("write, field, value, out_key, expect", [
    # (how the track changes, field, new value, song key, expected song value)
    ("update", "title", "Retitled", "title", "Retitled"),
    ("update", "duration", 321.4, "duration", 321),             # duration-only: _duration_seq
    ("update", "file_size", 999_999, "size", 999_999),          # seq-exempt: bumps NO seq
    ("update", "bitrate", 128_000, "bitRate", 128),
    ("update", "replaygain_track_gain", -1.5, "replayGain", {"trackGain": -1.5}),
    ("batch_update", "comment", "new comment", "comment", "new comment"),
    ("upsert", "album", "Other Album", "album", "Other Album"),
    ("batch_upsert", "year", 2001, "year", 2001),
    ("reupsert_same_object", "title", "Mutated", "title", "Mutated"),
    ("rebatch_same_object", "title", "Mutated again", "title", "Mutated again"),
])
def test_every_track_write_path_shows_in_the_next_response(lib, monkeypatch, write, field,
                                                           value, out_key, expect):
    """Warm the memo, change one track through each store write path, and the
    next response shows it — rebuilding THAT track only.  ``file_size`` is the
    inversion case: it bumps no store sequence at all, so a memo keyed on
    ``_mutation_seq`` / ``_catalog_seq`` / ``_duration_seq`` would serve the
    stale size."""
    calls = _Calls(monkeypatch)
    S._song_memo_clear()
    _map_all(lib, lambda: S._song_ctx(lib, _USER))
    _song(lib, "comp1")
    calls.take()
    seqs = (lib._mutation_seq, lib._catalog_seq, lib._duration_seq)
    if write == "update":
        lib.update_track_fields("tag1", {field: value})
    elif write == "batch_update":
        lib.update_track_fields_batch([("tag1", {field: value})])
    elif write == "upsert":
        lib.upsert_track({**lib.get_track("tag1"), field: value})
    elif write == "batch_upsert":
        lib.upsert_tracks_batch([{**lib.get_track("tag1"), field: value}])
    elif write == "reupsert_same_object":
        t = lib.get_track("tag1")
        t[field] = value
        lib.upsert_track(t)
    elif write == "rebatch_same_object":
        t = lib.get_track("tag1")
        t[field] = value
        lib.upsert_tracks_batch([t])
    if field == "file_size":
        assert (lib._mutation_seq, lib._catalog_seq, lib._duration_seq) == seqs
    got = _song(lib, "tag1")[out_key]
    if isinstance(expect, dict):
        assert got.items() >= expect.items()
    else:
        assert got == expect
    assert calls.take() == ["tag1"]                         # only the changed track
    _song(lib, "comp1")
    assert calls.take() == []                               # the rest stay warm


def test_the_write_counter_is_load_bearing(lib):
    """Falsifying companion: with the per-track write counter ignored (a
    memo validated by identity alone), an in-place field write is served
    stale — so the tests above really exercise the counter."""
    S._song_memo_clear()
    ctx = S._song_ctx(lib, _USER)
    ctx.revs = {}                                           # every lookup: rev 0
    t = lib.get_track("tag1")
    assert S._track_to_song(t, ctx)["size"] == 1234
    lib.update_track_fields("tag1", {"file_size": 4321})
    ctx2 = S._song_ctx(lib, _USER)
    ctx2.revs = {}
    assert S._track_to_song(t, ctx2)["size"] == 1234        # stale without the counter
    assert _song(lib, "tag1")["size"] == 4321               # the real context: fresh


def test_delete_and_readd_and_bulk_load_rebuild(lib):
    S._song_memo_clear()
    assert _song(lib, "comp1")["title"] == "Title comp1"
    lib.delete_track("comp1")
    lib.upsert_track(_t(lib, "comp1", title="Back again"))
    assert _song(lib, "comp1")["title"] == "Back again"
    # A snapshot reload: new dicts under the same ids, write counters untouched.
    assert _song(lib, "tag1")["title"] == "Title tag1"
    tracks = {tid: dict(t, title=f"Reloaded {tid}") for tid, t in lib._tracks.items()}
    lib.bulk_load(tracks, {}, dict(lib._ratings), dict(lib._play_stats), {}, [],
                  dict(lib._scan_dirs), dict(lib._hash_lookups), dict(lib._config))
    lib.rebuild_indexes()
    assert _song(lib, "tag1")["title"] == "Reloaded tag1"


def test_scan_roots_and_folder_flag_change_rebuild(lib):
    S._song_memo_clear()
    assert _song(lib, "tag1")["path"] == "misc/tag1.mp3"          # below /music
    lib.upsert_scan_dir("/music/misc")                            # a deeper root
    assert _song(lib, "tag1")["path"] == "tag1.mp3"
    lib.delete_scan_dir("/music/misc")
    assert _song(lib, "tag1")["path"] == "misc/tag1.mp3"
    on = _song(lib, "noalb1")
    assert on["albumId"].startswith("fa:")
    lib.set_config("subsonic_folder_albums", False)
    off = _song(lib, "noalb1")
    assert "albumId" not in off and off["parent"] == off["artistId"]


def test_folder_album_membership_change_shows_in_cached_songs(lib):
    """Folder-album ids / names depend on the OTHER tracks of the folder: a
    single-owner folder is ``fa:<dir>``; once no owner holds half of it, each
    owner gets a share ``fa:<dir>~<owner key>``.  The memoised song of the
    untouched track must follow — its own write counter never moved."""
    S._song_memo_clear()
    d = "/music/C64/Solo"
    lib.upsert_track(_t(lib, "solo1", album="", artist="Solo Artist", d=d, format="SID"))
    first = _song(lib, "solo1")
    rev = lib._track_rev["solo1"]
    assert first["albumId"] == sx.folder_album_id(lib.store_hash_lookup(d))
    # Two tracks by two other artists: nobody holds half — a mixed folder.
    for k in range(2):
        lib.upsert_track(_t(lib, f"intruder{k}", album="", artist=f"Intruder {k}", d=d,
                            format="SID"))
    S._catalogue(lib)                                             # the next snapshot
    again = _song(lib, "solo1")
    assert lib._track_rev["solo1"] == rev                         # the track itself: untouched
    want = _ref_track_to_song(lib.get_track("solo1"), S._song_ctx(lib, _USER))
    assert again["albumId"] == want["albumId"] != first["albumId"]
    assert _ordered(again) == _ordered(want)


def test_per_user_fields_are_never_cached(lib):
    import asyncio
    S._song_memo_clear()
    bob = types.SimpleNamespace(id="u2", username="bob")
    state = subsonic_state.get_state()
    # The FIRST (cold) caller has a star and a bookmark: nobody else may see them.
    asyncio.run(state.star("u1", "song", ["tag1"]))
    asyncio.run(state.set_bookmark("u1", "tag1", position=555))
    cold = _song(lib, "tag1")
    assert cold["starred"] and cold["bookmarkPosition"] == 555
    warm_bob = _song(lib, "tag1", bob)
    assert "starred" not in warm_bob and "bookmarkPosition" not in warm_bob
    asyncio.run(state.unstar("u1", "song", ["tag1"]))
    asyncio.run(state.delete_bookmark("u1", "tag1"))
    assert "starred" not in _song(lib, "tag1") and "bookmarkPosition" not in _song(lib, "tag1")
    a0 = _song(lib, "comp1")
    assert "starred" not in a0 and a0["playCount"] == 2 and "userRating" not in a0
    asyncio.run(subsonic_state.get_state().star("u1", "song", ["comp1"]))
    asyncio.run(subsonic_state.get_state().set_bookmark("u1", "comp1", position=777))
    lib.set_rating("comp1", 3)
    lib.record_play("comp1", at=1_700_000_999)
    a1 = _song(lib, "comp1")
    assert a1["starred"] and a1["bookmarkPosition"] == 777 and a1["userRating"] == 3
    assert a1["playCount"] == 3 and a1["played"] == "2023-11-14T22:29:59Z"
    b1 = _song(lib, "comp1", bob)                                 # another user, same memo
    assert "starred" not in b1 and "bookmarkPosition" not in b1
    assert b1["userRating"] == 3 and b1["playCount"] == 3        # library-wide ones


def test_callers_cannot_corrupt_the_memo(lib):
    S._song_memo_clear()
    s1 = _song(lib, "tag1")
    want = _ordered(s1)
    s1["title"] = "X"
    s1.pop("path")
    s1["artists"][0]["name"] = "X"
    s1["albumArtists"].append({"id": "x"})
    s1["genres"].append({"name": "X"})
    s1["isrc"].append("X")
    s1["replayGain"]["trackGain"] = 99
    tune = S._tune_child(_song(lib, "tune1"), lib.get_track("tune1"), 1, None)
    tune["artists"].clear()
    assert _ordered(_song(lib, "tag1")) == want
    assert _song(lib, "tune1")["artists"]


def test_a_copy_of_a_track_is_mapped_but_never_displaces_the_store_dict(lib, monkeypatch):
    """A smart playlist maps search-result copies: they're mapped from their
    own contents and leave the store dict's memo entry in place."""
    calls = _Calls(monkeypatch)
    S._song_memo_clear()
    live = lib.get_track("tag1")
    _song(lib, "tag1")
    copy = dict(live, title="From a copy")
    ctx = S._song_ctx(lib, _USER)
    assert S._track_to_song(copy, ctx)["title"] == "From a copy"
    assert S._SONG_SRC["tag1"] is live
    calls.take()
    assert _song(lib, "tag1")["title"] == "Title tag1"
    assert calls.take() == []                                    # still a hit


def test_memo_is_bounded(lib, monkeypatch):
    monkeypatch.setattr(S, "_SONG_MEMO_MAX", 4)
    S._song_memo_clear()
    want = _ref_all(lib, lambda: S._song_ctx(lib, _USER))
    for _ in range(2):
        got = _map_all(lib, lambda: S._song_ctx(lib, _USER))
        assert {k: _ordered(v) for k, v in got.items()} == {k: _ordered(v) for k, v in want.items()}
        assert len(S._SONG_META) <= 4
        assert len(S._SONG_META) == len(S._SONG_HEAD) == len(S._SONG_TAIL) == len(S._SONG_SRC)


def test_memo_is_invisible_to_the_cyclic_gc(lib):
    """The memo's pieces hold only strings / numbers / tuples of them, so a
    full collection untracks them: it never walks the memo (with the song's
    lists inside, ~7 objects per entry stayed tracked — ~85 ms per full
    collection at 100k entries)."""
    S._song_memo_clear()
    _map_all(lib, lambda: S._song_ctx(lib, _USER))
    gc.collect()
    gc.collect()                      # a tuple inside a tuple: untracked a pass later
    for m in (S._SONG_META, S._SONG_HEAD, S._SONG_TAIL):
        assert m and not any(gc.is_tracked(v) for v in m.values())


def test_memo_survives_many_stores(lib):
    """Each store gets its own generation; a store's id can't be reused while
    its generation exists (the registry pins it), and the registry is
    bounded."""
    S._song_memo_clear()
    gens = set()
    for _ in range(S._SONG_GENS_MAX + 5):
        st = TrackStore()
        st._aof = lambda *a, **k: None
        st.upsert_track(_t(st, "same", title=f"store {id(st)}"))
        s = S._track_to_song(st.get_track("same"), S._song_ctx(st, _USER))
        assert s["title"] == f"store {id(st)}"
        gens.add(S._song_ctx(st, _USER).memo_gen)
    assert len(gens) == S._SONG_GENS_MAX + 5                  # never a reused generation
    assert len(S._SONG_GENS) <= S._SONG_GENS_MAX
    assert _song(lib, "tag1")["title"] == "Title tag1"


def test_big_listing_end_to_end_is_unchanged_and_warm(env, monkeypatch):
    """A 1,200-song album through the real route, JSON and XML, cold then
    warm: identical bodies, and the warm request builds no song."""
    env.store._aof = lambda *a, **k: None
    for i in range(1200):
        env.store.upsert_track({
            "id": f"big{i:04d}", "title": f"Song {i} & <co>", "artist": "Big Band",
            "album_artist": "Big Band", "album": "Huge", "genre": ["Jazz"], "year": 2000,
            "added_at": 1_700_000_000 + i, "duration": 200.5, "format": "FLAC",
            "path": f"/music/big/{i}.flac", "dir_hash": "", "track_number": i % 20,
            "disc_number": 1 + i // 20})
    calls = _Calls(monkeypatch)
    S._song_memo_clear()
    aid = S._album_id("Big Band", "Huge")
    for fmt in ("json", "xml"):
        cold = env.client.get("/rest/getAlbum.view", params={"id": aid, "f": fmt}).content
        built = len(calls.take())
        warm = env.client.get("/rest/getAlbum.view", params={"id": aid, "f": fmt}).content
        assert cold == warm and calls.take() == []
        assert built == (1200 if fmt == "json" else 0)          # XML pass: already warm
    assert env.ok("getAlbum.view", id=aid)["album"]["songCount"] == 1200
