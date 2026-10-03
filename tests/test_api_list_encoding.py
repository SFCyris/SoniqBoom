# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The REST track lists encode with orjson and keep the old JSON contract.

The list endpoints (folder pages, ``/tracks/meta/batch``, ``/tracks/{id}``, the
smart views, ``/library/by-dir``, ``/search/filter``) used to build a Pydantic
model per row and/or run FastAPI's ``jsonable_encoder`` + ``json.dumps`` on the
event loop.  They now shape rows with ``tracks.public_track`` and encode with
``tracks.json_bytes``.  Pinned here, each against a REFERENCE app that runs the
old handlers through FastAPI's real serialization on the same store:

  * the same JSON — same keys, same value TYPES (``180`` vs ``180.0`` counts),
    model defaults for fields an older stored row lacks, model coercions
    (``"1999"`` → 1999), and the same rows rejected (404 / skipped);
  * no ``embedding`` and no stray stored key in any row (``/meta/batch`` used
    to ship the embedding: the one intended shape change);
  * a non-finite float → ``null`` (what the ``response_model`` routes already
    emitted) and a lone surrogate → U+FFFD, where the old path 500'd the whole
    response;
  * the aggregate endpoints' gzip memo: a gzip client gets the memoised gzip
    (compressed once per body), never a stale one after the body changes, and
    the app's gzip middleware passes it through without re-compressing.
"""
from __future__ import annotations

import copy
import gzip
import json
import math

import httpx
import orjson
import pytest
from fastapi import FastAPI, HTTPException, Query

from soniqboom.api import fstree, library, search, smart
from soniqboom.api import tracks as tracks_api
from soniqboom.core import store as store_mod
from soniqboom.core.data import (
    ft_search, get_track, get_tracks_batch, path_hash, tracks_by_dir,
    tracks_by_scan_root,
)
from soniqboom.core.store import TrackStore
from soniqboom.models.track import Track, TrackMeta

FIELDS = set(TrackMeta.model_fields)


# ── rows ──────────────────────────────────────────────────────────────────────

def _row(tid: str, d: str, root: str, **kw) -> dict:
    """A stored row as the scanner writes it: ``Track.model_dump()`` (no zero
    embedding), then whatever the case overrides."""
    row = Track(id=tid, path=f"{d}/{tid}.mod", title=f"Title {tid}", artist="Artist",
                album="Album", genre=["Chip", "Demo"], duration=95.5, format="ProTracker",
                added_at=100 + len(tid), mtime=1.7e9, file_size=4096, bitrate=320_000,
                instruments=["bass", "snare"], hvsc_lengths=[61.5, 12.25],
                dir_hash=path_hash(d), scan_root_hash=path_hash(root)).model_dump()
    row.pop("embedding")
    row.update(kw)
    return row


def _minimal(tid: str, d: str, root: str) -> dict:
    """A row persisted before most fields existed: the model fills the rest."""
    return {"id": tid, "path": f"{d}/{tid}.sid", "title": "Old row", "genre": ["Chip"],
            "added_at": 7, "dir_hash": path_hash(d), "scan_root_hash": path_hash(root)}


# Rows the OLD pipeline could encode: every class of difference between a stored
# row and its model dump, plus rows the model rejects.
def _clean_rows(d: str, root: str) -> list[dict]:
    return [
        _row("plain", d, root),
        _minimal("minimal", d, root),
        _row("embedded", d, root, embedding=[0.25, 0.5, 0.75]),
        _row("stray", d, root, waveform_legacy=[1, 2, 3], _internal="x"),
        _row("nones", d, root, year=None, bitrate=None, instruments=None,
             hvsc_lengths=None, is_lossless=None),
        _row("unicode", d, root, title="Pokémon ★ 日本語", artist="Motörhead",
             album="Ünïcödé \U0001F600"),
        _row("yearstr", d, root, year="1999"),             # model: str → int
        _row("durint", d, root, duration=180),             # model: int → 180.0
        _row("losslessint", d, root, is_lossless=1),       # model: 1 → True
        _row("lenints", d, root, hvsc_lengths=[120, 30.5]),  # model: [120.0, 30.5]
        _row("titlenone", d, root, title=None),            # model rejects
        _row("addedfloat", d, root, added_at=1.5),         # model rejects (int_from_float)
        _row("badinstr", d, root, instruments=[1, "x"]),   # model rejects (string_type)
        _without(_row("nopath", d, root), "path"),         # model rejects (required field)
        _row("dupprim", d, root, duplicate_group_id="gd", is_duplicate_primary=True),
        _row("dupalt", d, root, duplicate_group_id="gd", is_duplicate_primary=False,
             format="MP3"),                                # hidden by "hide duplicates"
    ]


def _without(row: dict, key: str) -> dict:
    row.pop(key)
    return row


def _poison_rows(d: str, root: str) -> list[dict]:
    """Rows that made the old ``jsonable_encoder`` / ``JSONResponse`` path 500."""
    return [
        _row("nandur", d, root, duration=float("nan")),
        _row("infgain", d, root, replaygain_track_gain=float("inf")),
        _row("surrogate", d, root, path=f"{d}/caf\udce9.mod", title="caf\udce9"),
    ]


REJECTED = {"titlenone", "addedfloat", "badinstr", "nopath"}


def _strict_eq(a, b) -> bool:
    """JSON-value equality that also tells ``180`` from ``180.0``."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_strict_eq(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_strict_eq(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _same_value(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return _strict_eq(a, b)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def lib(tmp_path, monkeypatch):
    """A store (installed as THE store) over a real scan root on disk, with the
    browse / smart / aggregate caches emptied."""
    root = str(tmp_path.resolve())
    d = f"{root}/sub"
    (tmp_path / "sub").mkdir()
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    s.upsert_scan_dir(root)
    for name in ("_TRACKS_META_CACHE", "_SCAN_ROOT_FULL_CACHE", "_STORE_RECURSIVE_CACHE",
                 "_DEDUP_MEMO", "_DIRECT_MEMO", "_BYID_MEMO"):
        monkeypatch.setattr(fstree, name, {})
    monkeypatch.setattr(smart, "_MOST_PLAYED_MEMO", {"seq": -1, "ranked": []})
    monkeypatch.setattr(smart, "_TOP_RATED_MEMO", {"seq": -1, "ranked": []})
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})

    class Lib:
        store = s

        def add(self, rows):
            for i, r in enumerate(rows):
                s.upsert_track(copy.deepcopy(r))
                if i % 2 == 0:                    # half played: "unplayed" lists the rest
                    s.record_play(r["id"])
                s.set_rating(r["id"], 1 + i % 5)
                s.push_history({"track_id": r["id"], "title": r.get("title") or "",
                                "artist": "", "ts": 1000})
    lib = Lib()
    lib.root, lib.dir = root, d
    return lib


def _new_app() -> FastAPI:
    app = FastAPI()
    for mod in (tracks_api, search, smart, library, fstree):
        app.include_router(mod.router, prefix="/api")
    return app


# ── the OLD handlers (verbatim logic), served by FastAPI's own serialization ──

async def _old_enrich_tracks(track_ids, stats=None, ratings=None):
    tracks = await get_tracks_batch(track_ids)
    result = []
    for tid, t in zip(track_ids, tracks):
        if t is None:
            continue
        d = t.model_dump()
        d.pop("embedding", None)
        if stats and tid in stats:
            d["play_count"] = stats[tid].get("count", 0)
            d["last_played"] = stats[tid].get("last_played")
        if ratings and tid in ratings:
            d["rating"] = ratings[tid]
        result.append(d)
    return result


def _old_public_meta(store, tid):
    t = store.get_track(tid)
    if not t:
        return None
    try:
        return TrackMeta(**{k: v for k, v in t.items()
                            if k in FIELDS and k != "embedding"}).model_dump()
    except Exception:
        return None


def _ref_app(monkeypatch) -> FastAPI:
    ref = FastAPI()

    @ref.get("/api/tracks/{track_id}", response_model=TrackMeta)
    async def read_track(track_id: str):
        track = await get_track(track_id)
        if not track:
            raise HTTPException(404, "Track not found")
        return track

    @ref.post("/api/tracks/meta/batch")
    async def batch_tracks(body: dict):
        out = []
        for tid in body.get("ids", [])[:5000]:
            t = await get_track(tid)
            if t:
                out.append(t)
        return out

    @ref.get("/api/library/by-dir")
    async def by_dir(path: str, recursive: bool = False, limit: int = Query(1000, ge=1, le=5000)):
        if recursive:
            return await tracks_by_scan_root(path, limit=limit)
        return await tracks_by_dir(path, limit=limit)

    @ref.get("/api/search/filter", response_model=list[TrackMeta])
    async def filter_tracks(artist: str | None = None, genre: str | None = None,
                            scene_group: str | None = None,
                            limit: int = Query(200, ge=1, le=2000), offset: int = Query(0, ge=0)):
        if scene_group:
            st = store_mod.get_store()
            return st.filter_tracks(artist=artist, genre=genre, scene_group=scene_group,
                                    limit=limit, offset=offset,
                                    filter_duplicates=bool(st.get_config("filter_duplicates", False)))
        parts = []
        if artist:
            parts.append(f"@artist_tag:{{{search._esc_tag(artist)}}}")
        if genre:
            parts.append(f"@genre:{{{search._esc_tag(genre)}}}")
        return await ft_search(" ".join(parts) if parts else "*", limit=limit, offset=offset)

    # Handlers whose body is unchanged: the plain function under the old
    # ``@router.get`` registration (jsonable_encoder + JSONResponse).
    ref.get("/api/smart/recently-added")(smart.recently_added)
    ref.get("/api/smart/unplayed")(smart.unplayed)
    ref.get("/api/smart/history")(smart.listening_history)
    ref.get("/api/fstree/tracks-with-meta")(fstree.tracks_with_meta)

    # Handlers that now shape through public_track: run them with the old shaper.
    @ref.get("/api/smart/most-played")
    async def most_played(limit: int = Query(100, ge=1, le=500)):
        with monkeypatch.context() as m:
            m.setattr(smart, "_enrich_tracks", _old_enrich_tracks)
            return await smart.most_played(limit=limit)

    @ref.get("/api/smart/top-rated")
    async def top_rated(limit: int = Query(100, ge=1, le=500)):
        with monkeypatch.context() as m:
            m.setattr(smart, "_enrich_tracks", _old_enrich_tracks)
            return await smart.top_rated(limit=limit)

    @ref.get("/api/smart/duplicates")
    async def duplicates(limit: int = Query(100, ge=1, le=500)):
        with monkeypatch.context() as m:
            m.setattr(smart, "_public_meta", _old_public_meta)
            return await smart.list_duplicate_groups(limit=limit)

    @ref.get("/api/smart/duplicates/{group_id}")
    async def duplicate_group(group_id: str):
        with monkeypatch.context() as m:
            m.setattr(smart, "_public_meta", _old_public_meta)
            return await smart.get_duplicate_group(group_id)

    ref.get("/api/smart/radio")(smart.instant_mix)

    return ref


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                             base_url="http://t", headers={"Accept-Encoding": "identity"})


async def _both(monkeypatch, method: str, url: str, **kw):
    async with _client(_ref_app(monkeypatch)) as old_c, _client(_new_app()) as new_c:
        old = await old_c.request(method, url, **kw)
        new = await new_c.request(method, url, **kw)
    return old, new


def _assert_public_rows(rows):
    """Every row: exactly the TrackMeta field set — no embedding, no stray key."""
    for r in rows:
        assert set(r) == FIELDS, (r.get("id"), set(r) ^ FIELDS)


# ── public_track: the model's output, without the model ─────────────────────

def test_public_track_equals_the_model_dump_for_every_row_class(lib):
    odd = [_without(_row("noid", lib.dir, lib.root), "id"),          # model rejects
           _row("hugeint", lib.dir, lib.root, duration=10 ** 400),   # model rejects
           _row("bigint", lib.dir, lib.root, file_size=2 ** 70),     # kept as is
           _row("tuplegenre", lib.dir, lib.root, genre=("a", "b")),  # model: → list
           _row("boolyear", lib.dir, lib.root, year=True),           # model: → 1
           _row("emptylists", lib.dir, lib.root, genre=[], instruments=[])]
    for row in _clean_rows(lib.dir, lib.root) + _poison_rows(lib.dir, lib.root) + odd:
        try:
            want = TrackMeta(**{k: v for k, v in row.items() if k in FIELDS}).model_dump()
        except Exception:
            want = None
        got = tracks_api.public_track(row)
        name = row.get("id", "noid")
        if want is None:
            assert got is None, name
            assert name in REJECTED | {"noid", "hugeint"}
            continue
        assert got is not None and set(got) == FIELDS, name
        for k in FIELDS:                 # same value AND same type, field by field
            assert _same_value(got[k], want[k]), (name, k, got[k], want[k])


def test_public_track_hands_back_a_canonical_row_untouched_and_never_mutates(lib):
    canonical = _row("plain", lib.dir, lib.root)
    assert tracks_api.public_track(canonical) is canonical      # zero-copy fast path
    for row in (_row("durint", lib.dir, lib.root, duration=180),
                _row("stray", lib.dir, lib.root, embedding=[0.5], extra=1),
                _minimal("minimal", lib.dir, lib.root),
                _row("yearstr", lib.dir, lib.root, year="1999")):
        before = copy.deepcopy(row)
        out = tracks_api.public_track(row)
        assert out is not row and row == before                 # the stored row is read-only
    assert tracks_api.public_track(None) is None and tracks_api.public_track({}) is None


def test_every_trackmeta_field_has_a_fast_path_type():
    """A field whose annotation ``_meta_types`` can't read would send EVERY row
    through the model (correct, ~5x slower) — fail loudly instead."""
    assert not [n for n, (kept, _f, _e) in tracks_api._META_SPEC.items() if not kept]
    assert set(tracks_api._STR_LISTS) | set(tracks_api._FLOAT_LISTS) == {
        n for n, f in TrackMeta.model_fields.items() if "list" in repr(f.annotation)}


def test_trackmeta_has_nothing_the_fast_path_would_bypass():
    """``public_track`` re-implements plain field typing only.  A validator,
    serializer, constraint, alias or config transform added to TrackMeta would
    be silently skipped for fast-path rows — teach ``public_track`` (or route
    those rows to the model) before relaxing this."""
    deco = TrackMeta.__pydantic_decorators__
    assert not (deco.validators or deco.field_validators or deco.root_validators
                or deco.field_serializers or deco.model_serializers
                or deco.model_validators or deco.computed_fields)
    assert not dict(TrackMeta.model_config)
    for name, f in TrackMeta.model_fields.items():
        assert not f.metadata and f.alias is None and f.validation_alias is None \
            and f.serialization_alias is None and not f.exclude, name
    assert tracks_api._META_REQUIRED == {"id", "path"}


def test_json_bytes_fallbacks():
    nan_row = {"duration": float("nan"), "gain": float("-inf"), "t": "Pokémon"}
    assert json.loads(tracks_api.json_bytes(nan_row)) == {"duration": None, "gain": None,
                                                          "t": "Pokémon"}
    body = tracks_api.json_bytes([{"path": "/a/caf\udce9.mod", "ok": "日本\U0001F600"}])
    assert json.loads(body) == [{"path": "/a/caf\ufffd.mod", "ok": "日本\U0001F600"}]
    from pathlib import Path
    odd = {"tags": {"x"}, "where": Path("/m/x.mod"), 3: "int key", "s": "\ud800"}
    assert json.loads(tracks_api.json_bytes(odd)) == {
        "tags": ["x"], "where": "/m/x.mod", "3": "int key", "s": "\ufffd"}
    # One bad row in a page: only its bad VALUE takes the jsonable_encoder path
    # (the rest stays orjson), and the bytes equal encoding the scrubbed page.
    page = {"total": 3, "tracks": [{"id": f"t{i}", "path": f"/m/{i}.mod", "d": 1.5}
                                   for i in range(3)]}
    page["tracks"][1]["path"] = "/m/caf\udce9.mod"
    import fastapi.encoders as enc
    seen = []
    real = enc.jsonable_encoder
    enc.jsonable_encoder = lambda o, *a, **k: seen.append(o) or real(o, *a, **k)
    try:
        body = tracks_api.json_bytes(page)
    finally:
        enc.jsonable_encoder = real
    assert seen == ["/m/caf\udce9.mod"]
    assert body == orjson.dumps(tracks_api._json_safe(page))
    assert json.loads(body)["tracks"][1]["path"] == "/m/caf\ufffd.mod"
    # orjson can't encode an int beyond 64 bits at all: stdlib json, still
    # with the surrogate scrubbed and the non-finite float as null.
    big = {"big": 2 ** 70, "nan": float("nan"), "s": "caf\udce9"}
    assert json.loads(tracks_api.json_bytes(big)) == {"big": 2 ** 70, "nan": None,
                                                      "s": "caf\ufffd"}


# ── endpoints: new vs the old pipeline on the same store ────────────────────

async def test_read_track_matches_the_response_model_route(lib, monkeypatch):
    lib.add(_clean_rows(lib.dir, lib.root) + _poison_rows(lib.dir, lib.root))
    for row in _clean_rows(lib.dir, lib.root) + _poison_rows(lib.dir, lib.root) + [{"id": "nope"}]:
        old, new = await _both(monkeypatch, "GET", f"/api/tracks/{row['id']}")
        if row["id"] == "surrogate":
            assert old.status_code == 500                      # UnicodeEncodeError before
            assert new.status_code == 200 and new.json()["path"].endswith("caf\ufffd.mod")
            continue
        assert new.status_code == old.status_code, row["id"]
        assert new.headers["content-type"] == old.headers["content-type"] == "application/json"
        if old.status_code == 200:
            assert _strict_eq(new.json(), old.json()), row["id"]
            _assert_public_rows([new.json()])
        else:
            assert new.json() == old.json()                    # {"detail": "Track not found"}
    assert (await _both(monkeypatch, "GET", "/api/tracks/nandur"))[1].json()["duration"] is None


async def test_meta_batch_matches_minus_the_embedding(lib, monkeypatch):
    rows = _clean_rows(lib.dir, lib.root)
    lib.add(rows)
    ids = [r["id"] for r in rows] + ["unknown", "plain"]      # unknown skipped, dup kept
    old, new = await _both(monkeypatch, "POST", "/api/tracks/meta/batch", json={"ids": ids})
    assert old.status_code == new.status_code == 200
    old_rows, new_rows = old.json(), new.json()
    assert "embedding" in old_rows[0]                          # what the old route shipped
    for r in old_rows:
        r.pop("embedding")
    assert _strict_eq(new_rows, old_rows)
    _assert_public_rows(new_rows)
    assert [r["id"] for r in new_rows] == [i for i in ids if i in {r["id"] for r in rows}
                                           and i not in REJECTED]


async def test_meta_batch_no_longer_500s_on_a_nan_or_a_surrogate(lib, monkeypatch):
    lib.add(_clean_rows(lib.dir, lib.root)[:1] + _poison_rows(lib.dir, lib.root))
    for tid in ("nandur", "infgain", "surrogate"):
        old, new = await _both(monkeypatch, "POST", "/api/tracks/meta/batch",
                               json={"ids": ["plain", tid]})
        assert old.status_code == 500, tid
        assert new.status_code == 200 and [r["id"] for r in new.json()] == ["plain", tid]
    by_id = {r["id"]: r for r in (await _both(
        monkeypatch, "POST", "/api/tracks/meta/batch",
        json={"ids": ["nandur", "infgain", "surrogate"]}))[1].json()}
    assert by_id["nandur"]["duration"] is None
    assert by_id["infgain"]["replaygain_track_gain"] is None
    assert by_id["surrogate"]["title"] == "caf\ufffd"
    # Malformed ids are skipped like unknown ones (an unhashable one 500'd the batch).
    new = (await _both(monkeypatch, "POST", "/api/tracks/meta/batch",
                       json={"ids": [["x"], 5, "plain"]}))[1]
    assert new.status_code == 200 and [r["id"] for r in new.json()] == ["plain"]
    assert (await _both(monkeypatch, "POST", "/api/tracks/meta/batch",
                        json={"ids": "plain"}))[1].status_code == 422


@pytest.mark.parametrize("url", [
    "/api/smart/most-played?limit=500",
    "/api/smart/top-rated?limit=500",
    "/api/smart/recently-added?limit=500",
    "/api/smart/unplayed?limit=500",
    "/api/smart/history?limit=200",
    "/api/library/by-dir?path={dir}",
    "/api/library/by-dir?path={root}&recursive=true",
    "/api/search/filter?limit=2000",
    "/api/search/filter?genre=Chip&limit=5&offset=2",
    "/api/search/filter?artist=Artist&scene_group=Ate%20Bit",
    "/api/fstree/tracks-with-meta?path={dir}&filter_duplicates=false",
    "/api/fstree/tracks-with-meta?path={dir}&limit=4&offset=3&filter_duplicates=false",
    "/api/fstree/tracks-with-meta?path={root}&recursive=true&limit=50&filter_duplicates=false",
    "/api/fstree/tracks-with-meta?path={root}&recursive=true&limit=50&shuffle_seed=7"
    "&filter_duplicates=false",
])
async def test_list_endpoints_match_the_old_pipeline(lib, monkeypatch, url):
    rows = _clean_rows(lib.dir, lib.root)
    if "/by-dir" in url:
        # The old by-dir 500'd on a row the model rejects (no try/except in
        # data.tracks_by_dir) — compared without those; see the test below.
        rows = [r for r in rows if r["id"] not in REJECTED]
    for r in rows[:6]:
        r["scene_group"] = "Ate Bit"
    lib.add(rows)
    url = url.format(dir=lib.dir, root=lib.root)
    old, new = await _both(monkeypatch, "GET", url)
    assert old.status_code == new.status_code == 200, (old.text[:300], new.text[:300])
    assert new.headers["content-type"] == old.headers["content-type"]
    assert _strict_eq(new.json(), old.json())
    body = new.json()
    listed = body["tracks"] if isinstance(body, dict) else body
    assert listed and not any("embedding" in r for r in listed if isinstance(r, dict))
    if "/smart/history" not in url:
        assert not {r["id"] for r in listed} & REJECTED or "recently-added" in url \
            or "unplayed" in url                        # those two serve raw rows, as before


async def test_by_dir_skips_a_rejected_row_instead_of_500ing(lib, monkeypatch):
    lib.add(_clean_rows(lib.dir, lib.root))
    for url in (f"/api/library/by-dir?path={lib.dir}",
                f"/api/library/by-dir?path={lib.root}&recursive=true"):
        old, new = await _both(monkeypatch, "GET", url)
        assert old.status_code == 500                         # pydantic ValidationError
        assert new.status_code == 200
        ids = {r["id"] for r in new.json()}
        assert ids == {r["id"] for r in _clean_rows(lib.dir, lib.root)} - REJECTED
        _assert_public_rows(new.json())


async def test_list_endpoints_survive_rows_that_500d_the_old_encoder(lib, monkeypatch):
    lib.add(_clean_rows(lib.dir, lib.root) + _poison_rows(lib.dir, lib.root))
    for url in ("/api/smart/recently-added?limit=500", "/api/smart/unplayed?limit=500",
                "/api/smart/most-played?limit=500", "/api/smart/top-rated?limit=500",
                "/api/smart/history?limit=200", f"/api/library/by-dir?path={lib.dir}",
                "/api/search/filter?limit=2000",
                f"/api/fstree/tracks-with-meta?path={lib.dir}&filter_duplicates=false",
                f"/api/fstree/tracks-with-meta?path={lib.root}&recursive=true&limit=50"
                "&filter_duplicates=false"):
        old, new = await _both(monkeypatch, "GET", url)
        assert old.status_code == 500, url
        assert new.status_code == 200, url
        body = new.json()
        listed = {r.get("id", r.get("track_id")): r
                  for r in (body["tracks"] if isinstance(body, dict) else body)}
        present = {"nandur", "infgain", "surrogate"} & set(listed)
        assert present, url                                   # the view lists poison rows
        if "history" in url:
            assert listed["surrogate"]["title"] == "caf\ufffd"
            continue
        if "nandur" in present:
            assert listed["nandur"]["duration"] is None
        if "infgain" in present:
            assert listed["infgain"]["replaygain_track_gain"] is None
        if "surrogate" in present:
            assert listed["surrogate"]["path"].endswith("/caf\ufffd.mod")


async def test_duplicate_groups_match_the_old_shaping(lib, monkeypatch):
    rows = _clean_rows(lib.dir, lib.root)
    for i, r in enumerate(rows):
        r["duplicate_group_id"] = f"g{i % 3}"
        r["is_duplicate_primary"] = i < 3
        r["format_score"] = 10 * i
    lib.add(rows)
    monkeypatch.setattr(smart, "_dup_seq", smart._seq_of(lib.store))   # no recompute
    old, new = await _both(monkeypatch, "GET", "/api/smart/duplicates")
    assert old.status_code == new.status_code == 200
    assert len(new.json()) == 3 and _strict_eq(new.json(), old.json())
    for g in new.json():
        _assert_public_rows(g["tracks"])


async def test_route_contract_is_unchanged(lib, monkeypatch):
    """Query validation, HEAD handling and 404s are the route's, as before."""
    lib.add(_clean_rows(lib.dir, lib.root)[:2])
    for method, url in (("GET", "/api/search/filter?limit=0"),
                        ("GET", "/api/library/by-dir?path=/x&limit=99999"),
                        ("GET", "/api/smart/most-played?limit=0"),
                        ("GET", "/api/fstree/tracks-with-meta"),
                        ("GET", "/api/fstree/tracks-with-meta?path=/definitely/not/here"),
                        ("HEAD", "/api/smart/recently-added"),
                        ("HEAD", "/api/tracks/plain"),
                        ("HEAD", "/api/library/by-dir?path=/x")):
        old, new = await _both(monkeypatch, method, url)
        assert new.status_code == old.status_code, (method, url, old.status_code,
                                                    new.status_code)
        assert new.status_code in (404, 405, 422)


async def test_direct_callers_still_get_python_data(lib):
    """``json_route`` registers a wrapper; the module function is unchanged."""
    lib.add(_clean_rows(lib.dir, lib.root))
    page = await fstree.tracks_with_meta(path=lib.dir, recursive=False, offset=0, limit=2,
                                         filter_duplicates=False, shuffle_seed=None)
    valid = len(_clean_rows(lib.dir, lib.root)) - len(REJECTED)
    assert isinstance(page, dict) and page["total"] == valid and len(page["tracks"]) == 2
    rows = await search.filter_tracks(artist="Artist", album_artist=None, album=None,
                                      genre=None, scene_group=None, format=None,
                                      year_min=None, year_max=None, limit=200, offset=0)
    assert isinstance(rows, list) and {r["id"] for r in rows}.isdisjoint(REJECTED)


async def test_hide_duplicates_setting_reaches_the_same_rows(lib, monkeypatch):
    """With "hide duplicates" on, /search/filter drops the non-primary copy (the
    FT branch and the scene_group branch alike) and by-dir still does not —
    exactly as before."""
    rows = [r for r in _clean_rows(lib.dir, lib.root) if r["id"] not in REJECTED]
    for r in rows:
        r["scene_group"] = "Ate Bit"
    lib.add(rows)
    lib.store.set_config("filter_duplicates", True)
    for url, alt_listed in (("/api/search/filter?limit=2000", False),
                            ("/api/search/filter?scene_group=Ate%20Bit", False),
                            (f"/api/library/by-dir?path={lib.dir}", True),
                            (f"/api/library/by-dir?path={lib.root}&recursive=true", True)):
        old, new = await _both(monkeypatch, "GET", url)
        assert old.status_code == new.status_code == 200
        assert _strict_eq(new.json(), old.json()), url
        assert ("dupalt" in {r["id"] for r in new.json()}) is alt_listed, url


async def test_radio_and_one_duplicate_group_match(lib, monkeypatch):
    import random
    rows = _clean_rows(lib.dir, lib.root)
    for i, r in enumerate(rows):
        r["duplicate_group_id"] = "g1" if i < 4 else None
        r["is_duplicate_primary"] = i != 1
    lib.add(rows)
    monkeypatch.setattr(smart, "_dup_seq", smart._seq_of(lib.store))
    seeded = random.Random
    monkeypatch.setattr(random, "Random", lambda *a: seeded(1234))   # radio's jitter
    for url in ("/api/smart/radio?seed=plain&limit=20", "/api/smart/duplicates/g1",
                "/api/smart/duplicates/nope", "/api/smart/radio?seed=nope"):
        old, new = await _both(monkeypatch, "GET", url)
        assert new.status_code == old.status_code, url
        assert _strict_eq(new.json(), old.json()), url
    old, new = await _both(monkeypatch, "GET", "/api/smart/radio?seed=plain&limit=20")
    assert len(new.json()) > 3 and not any("embedding" in r for r in new.json())


async def test_shaped_rows_never_write_back_into_the_store(lib, monkeypatch):
    """The fast path may hand out the store's own dicts: adding play_count /
    rating (most-played, top-rated) or sorting duplicate members must work on
    copies — the stored rows stay exactly as they were."""
    rows = _clean_rows(lib.dir, lib.root)
    for i, r in enumerate(rows):
        r["duplicate_group_id"] = f"g{i % 2}"
    lib.add(rows)
    monkeypatch.setattr(smart, "_dup_seq", smart._seq_of(lib.store))
    before = copy.deepcopy(lib.store._tracks)
    async with _client(_new_app()) as c:
        for url in ("/api/smart/most-played?limit=500", "/api/smart/top-rated?limit=500",
                    "/api/smart/duplicates", "/api/smart/duplicates/g0",
                    f"/api/library/by-dir?path={lib.dir}", "/api/search/filter",
                    "/api/tracks/plain"):
            r = await c.get(url)
            assert r.status_code == 200, url
        assert (await c.post("/api/tracks/meta/batch", json={"ids": ["plain"]})).status_code == 200
        played = (await c.get("/api/smart/most-played?limit=500")).json()
    assert played and all("play_count" in r and "rating" not in r for r in played)
    assert lib.store._tracks == before
    assert not any("play_count" in t or "rating" in t for t in lib.store._tracks.values())


async def test_raw_row_lists_survive_a_surrogate_too(lib):
    """/api/tracks, /api/search and /api/tracks/shuffled already used orjson —
    and 500'd on a lone surrogate (orjson refuses it); they share json_bytes now."""
    lib.add(_clean_rows(lib.dir, lib.root)[:3] + _poison_rows(lib.dir, lib.root))
    async with _client(_new_app()) as c:
        for url in ("/api/tracks?limit=100", "/api/search?q=caf",
                    "/api/tracks/shuffled?seed=3&limit=100"):
            r = await c.get(url)
            assert r.status_code == 200, url
            body = r.json()
            listed = {t["id"]: t for t in (body["tracks"] if isinstance(body, dict) else body)}
            assert "surrogate" in listed, url
            assert listed["surrogate"]["path"].endswith("/caf\ufffd.mod")
    clean = [t for t in lib.store.filter_tracks(limit=100)
             if t["id"] in {"plain", "minimal", "embedded", "nandur"}]
    assert len(clean) == 4
    assert tracks_api.json_bytes(clean) == orjson.dumps(clean)     # fast path: same bytes


def test_json_route_contract():
    from fastapi import APIRouter
    r = APIRouter()
    with pytest.raises(TypeError):
        @tracks_api.json_route(r, "/sync")
        def handler():                      # noqa: ANN202 — a sync handler is refused
            return []

    @tracks_api.json_route(r, "/made", methods=("POST",), status_code=201)
    async def made():
        return {"ok": True}

    app = FastAPI()
    app.include_router(r)
    from fastapi.testclient import TestClient
    resp = TestClient(app).post("/made")
    assert resp.status_code == 201 and resp.json() == {"ok": True}


# ── the aggregate endpoints' gzip memo ────────────────────────────────────────

def _gzip_app():
    from soniqboom.main import _SelectiveGZipMiddleware      # the app's real middleware
    return _SelectiveGZipMiddleware(_new_app(), minimum_size=1000)


async def _raw(c, url, headers):
    async with c.stream("GET", url, headers=headers) as r:
        return r, b"".join([chunk async for chunk in r.aiter_raw()])


def _many_artists(lib, n=120, prefix="Artist"):
    lib.add([_row(f"{prefix.lower()}{i}", lib.dir, lib.root, artist=f"{prefix} {i:03d}",
                  album=f"{prefix} album {i:03d}")
             for i in range(n)])


async def test_gzip_memo_cold_then_warm_and_passed_through_untouched(lib, monkeypatch):
    _many_artists(lib)
    calls: list[str] = []
    real = gzip.compress
    monkeypatch.setattr(library.gzip, "compress",
                        lambda data, **kw: calls.append(len(data)) or real(data, **kw))
    gz_hdr = {"Accept-Encoding": "gzip, deflate"}
    async with _client(_gzip_app()) as c:
        plain, plain_raw = await _raw(c, "/api/library/artists", {"Accept-Encoding": "identity"})
        assert calls == []                                    # identity: no gzip made
        assert "content-encoding" not in plain.headers
        assert plain.headers["vary"] == "Accept-Encoding"     # the middleware's, as before
        assert len(plain_raw) >= library._GZIP_MIN_BYTES

        cold, cold_raw = await _raw(c, "/api/library/artists", gz_hdr)
        warm, warm_raw = await _raw(c, "/api/library/artists", gz_hdr)
        # Different key → its own entry, compressed once too.
        albums, albums_raw = await _raw(c, "/api/library/albums", gz_hdr)
        albums2, _ = await _raw(c, "/api/library/albums", gz_hdr)
    assert len(calls) == 2                                    # artists once, albums once
    for r in (cold, warm, albums, albums2):
        assert r.status_code == 200
        assert r.headers["content-encoding"] == "gzip"
        assert r.headers["vary"] == "Accept-Encoding"         # not "…, Accept-Encoding" twice
        assert r.headers["content-type"] == "application/json"
        assert r.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert cold_raw == warm_raw                               # the memoised bytes
    assert int(cold.headers["content-length"]) == len(cold_raw)
    # ONE gzip layer: a single decompress yields the exact identity body (a
    # middleware re-compression would leave gzip bytes here).
    assert gzip.decompress(cold_raw) == plain_raw
    assert cold.headers["etag"] == warm.headers["etag"] == plain.headers["etag"]
    assert json.loads(gzip.decompress(albums_raw))           # a different, valid body
    assert gzip.decompress(albums_raw) != plain_raw


async def test_gzip_memo_never_serves_a_stale_body_after_the_data_changes(lib):
    _many_artists(lib)
    gz_hdr = {"Accept-Encoding": "gzip"}
    async with _client(_gzip_app()) as c:
        before, before_raw = await _raw(c, "/api/library/artists", gz_hdr)
        # A retag: the aggregate (and so the body + ETag) changes.
        for i in range(0, 120, 2):
            lib.store.update_track_fields(f"artist{i}", {"artist": f"Renamed {i:03d}"})
        after, after_raw = await _raw(c, "/api/library/artists", gz_hdr)
        plain_after, plain_raw = await _raw(c, "/api/library/artists",
                                            {"Accept-Encoding": "identity"})
    new_names = {a["artist"] for a in json.loads(gzip.decompress(after_raw))}
    assert "Renamed 000" in new_names and "Artist 000" not in new_names
    assert gzip.decompress(after_raw) == plain_raw
    assert gzip.decompress(before_raw) != plain_raw
    assert after.headers["etag"] != before.headers["etag"]
    assert after.headers["etag"] == plain_after.headers["etag"]
    # invalidate_agg_cache() (a scan completing) drops the memo with the body.
    library.invalidate_agg_cache()
    assert library._AGG_ETAGS == {}


async def test_gzip_memo_304_and_small_bodies(lib):
    _many_artists(lib, n=2)                                   # a body under the threshold
    async with _client(_gzip_app()) as c:
        small, small_raw = await _raw(c, "/api/library/artists", {"Accept-Encoding": "gzip"})
        assert len(small_raw) < library._GZIP_MIN_BYTES
        assert "content-encoding" not in small.headers and "vary" not in small.headers
        nm = await c.get("/api/library/artists", headers={
            "Accept-Encoding": "gzip", "If-None-Match": small.headers["etag"]})
        assert nm.status_code == 304 and nm.content == b"" and "vary" not in nm.headers

        _many_artists(lib, n=120, prefix="Big")
        big, big_raw = await _raw(c, "/api/library/artists", {"Accept-Encoding": "gzip"})
        assert big.headers["content-encoding"] == "gzip"
        for etag in (big.headers["etag"], "W/" + big.headers["etag"], "*"):
            nm = await c.get("/api/library/artists", headers={
                "Accept-Encoding": "gzip", "If-None-Match": etag})
            assert nm.status_code == 304 and nm.content == b""
            assert nm.headers["etag"] == big.headers["etag"]
            assert nm.headers["vary"] == "Accept-Encoding"    # what its 200 carries
        stale = await c.get("/api/library/artists", headers={
            "Accept-Encoding": "gzip", "If-None-Match": small.headers["etag"]})
        assert stale.status_code == 200 and json.loads(stale.content)
