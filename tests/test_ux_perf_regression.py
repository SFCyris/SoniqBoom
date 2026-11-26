# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Performance-regression guards for the UX-critical paths that had none
(from a v2.2.0 vs 2.3.0-dev comparison at 500K tracks, 2026-11-25):

* the startup load (``populate_store`` + ``warmup_scan_root_caches``),
* ``/api/tracks`` paging, the search box's preview (``limit=8&fields=list``),
* folder listing (``/api/fstree/tracks-with-meta`` flat and recursive,
  ``/api/fstree/children`` of a 4,000-folder parent),
* Track Info (``/api/tracks/{id}``, ``/extended``, ``/chapters``),
* the sidebar counts and Artists right after a write,
* first play (SID, MOD) to the first byte, cold and warm, and a seek
  (Range) on a cached render,
* Subsonic ``getArtists`` (first and warm) and ``stream?format=mp3``.

The startup's browse rows and the Artists fold are also guarded as RATIOS
(against the Pydantic model, against ``aggregate_artists``), so the +2.5 s
and +40 ms of 2.3.0-dev would fail however fast the machine.

Each budget is the v2.2.0 time measured at 500K tracks on the dev Mac (in the
comment, with 2.3.0-dev's) scaled to this module's library size, times a
margin, and best-of-3 — so a parallel full run (``-n``) passes and a change
of order (a per-row Pydantic pass back on a hot path, a full rebuild on every
write, a render that waits for its end) fails.  The startup load is checked
as a RATIO of linear work (per-track cost at 20K vs 100K) plus a generous
absolute bound, so a slow CI box does not flake.

In-process only: bare routers, a throw-away TrackStore, a tmp folder tree,
the heap frozen after the load as the server does.
"""
from __future__ import annotations

import asyncio
import gc
import hashlib
import shutil
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import fstree, library, search, stream, subsonic
from soniqboom.api import tracks as tracks_api
from soniqboom.api import users as users_api
from soniqboom.core import availability
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore
from soniqboom.models.track import Track

N = 100_000
import soniqboom
REPO = Path(soniqboom.__file__).resolve().parent.parent
SID = REPO / "internal/testdata/sid/SX-64_Demo.sid"
MOD = REPO / "internal/testdata/tracker/actionplatform.mod"


def ph(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


_TMPL = None


def _row(path: str, root: str, k: int, **kw) -> dict:
    global _TMPL
    if _TMPL is None:
        _TMPL = Track(id="x", path="/x", embedding=[]).model_dump()
        _TMPL.pop("embedding", None)
    d = dict(_TMPL)
    d.update(id=ph(path), path=path, dir_hash=ph(path.rsplit("/", 1)[0]),
             scan_root_hash=ph(root), added_at=1_600_000_000 + k, mtime=1.7e9,
             is_duplicate_primary=True, **kw)
    return d


def _rows(base: Path, n: int) -> tuple[dict, dict]:
    """``n`` rows: 24% HVSC-style SID (80 a folder), 26% trackers (10 a
    folder), 50% albums (12 a folder, 800 artists); scene credits on 2%."""
    hv, ml, mn = str(base / "HVSC"), str(base / "Modland"), str(base / "Main")
    rows = {}
    k = 0
    for m in range(int(n * 0.24) // 80):
        folder = f"{hv}/MUSICIANS/H/Hubbard{m}_Rob"
        for j in range(80):
            r = _row(f"{folder}/tune_{j}.sid", hv, k, title=f"Tune {j}", format="SID",
                     artist=f"Rob Hubbard{m}" if m % 3 else f"Hubbard{m}_Rob", duration=120.0,
                     year=1984 + j % 20)
            if k % 50 == 0:
                r.update(scene_party=f"Party {k % 30}", scene_compo="C64 Music", scene_place=1 + k % 8)
            rows[r["id"]] = r
            k += 1
    for a in range(int(n * 0.26) // 10):
        fdir = ("Protracker", "Fasttracker 2", "Impulsetracker")[a % 3]
        for j in range(10):
            r = _row(f"{ml}/{fdir}/Jester {a}/song_{j}.mod", ml, k, title=f"Song {j}",
                     artist=f"Jester {a}", format="ProTracker", duration=200.0)
            rows[r["id"]] = r
            k += 1
    al = 0
    while k < n:
        ar = al % 800
        for j in range(12):
            if k >= n:
                break
            r = _row(f"{mn}/Band {ar}/Album {al}/{j + 1:02d} - Love {j}.flac", mn, k,
                     title=f"Love {j} night", artist=f"Band {ar}", album_artist=f"Band {ar}",
                     album=f"Album {al}", genre=[("Rock", "Pop", "Jazz")[al % 3]],
                     composer=f"Writer {al % 500}" if j % 3 == 0 else "", format="FLAC",
                     duration=210.0, year=1970 + al % 50, track_number=j + 1)
            rows[r["id"]] = r
            k += 1
        al += 1
    scan_dirs = {r: {"path": r, "track_count": 0, "added_at": 0, "last_scanned": 0,
                     "network_share_id": None, "status": "ok", "path_hash": ph(r)}
                 for r in (hv, ml, mn)}
    return rows, scan_dirs


def _mkdirs(rows: dict) -> None:
    for d in {r["path"].rsplit("/", 1)[0] for r in rows.values()}:
        Path(d).mkdir(parents=True, exist_ok=True)


def _load(rows, scan_dirs) -> TrackStore:
    from soniqboom.core import persistence
    s = TrackStore()
    store_mod._store = s
    persistence.populate_store({"db_version": persistence.DB_VERSION, "tracks": rows,
                                "scan_dirs": scan_dirs, "config": {}})
    return s


def _best(fn, tries=3) -> float:
    return min(fn() for _ in range(tries))


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5)),
                             base_url="http://t", timeout=120)


async def _ms(client, url, **params) -> float:
    t = time.perf_counter()
    r = await client.get(url, params=params)
    el = (time.perf_counter() - t) * 1000
    assert r.status_code == 200, (url, r.status_code, r.text[:200])
    return el


async def _best_ms(client, url, tries=3, **params) -> float:
    return min([await _ms(client, url, **params) for _ in range(tries)])


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    base = tmp_path_factory.mktemp("lib")
    rows, scan_dirs = _rows(base, N)
    _mkdirs(rows)
    saved = store_mod._store

    async def load():
        s = _load(rows, scan_dirs)
        fstree.warmup_scan_root_caches(None)
        if getattr(s, "_sx_task", None) is not None:
            await s._sx_task
        await s.composer_index_async()
        return s
    s = asyncio.run(load())
    gc.collect()
    gc.freeze()                                  # (as the server does after its load)
    yield s, base
    gc.unfreeze()
    store_mod._store = saved


@pytest.fixture
def app(big, monkeypatch):
    s, base = big
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})
    availability.reset_for_tests()
    a = FastAPI()
    for m in (tracks_api, library, search, fstree):
        a.include_router(m.router, prefix="/api")
    a.include_router(subsonic.router)
    a.dependency_overrides[users_api.current_user] = lambda: None
    a.dependency_overrides[users_api.optional_listener_uid] = lambda: None
    yield a, s, base
    availability.reset_for_tests()


# ── startup ──────────────────────────────────────────────────────────────────

def test_the_startup_load_stays_linear_and_within_budget(tmp_path):
    """populate_store + the browse warmup per track: 20K vs 100K (a quadratic
    step shows as a ratio well above 1), and 100K within an absolute bound.
    500K on the dev Mac: v2.2.0 12.6 s + 18.5 s; 2.3.0-dev 15.0 s + 23.1 s
    (≈ 30 µs + 37 µs a track at 100K)."""
    def per_track_us(n):
        rows, sd = _rows(tmp_path / f"s{n}", n)
        _mkdirs(rows)
        saved = store_mod._store
        try:
            t = time.perf_counter()
            _load(rows, sd)
            fstree.warmup_scan_root_caches(None)
            return (time.perf_counter() - t) / n * 1e6
        finally:
            store_mod._store = saved
            gc.collect()
    small = _best(lambda: per_track_us(20_000), tries=2)
    large = per_track_us(N)
    assert large < 3 * small, f"per-track load cost grows with size: {small:.0f} → {large:.0f} µs"
    assert large * N / 1e6 < 60, f"100K tracks load in {large * N / 1e6:.1f} s"


def test_the_browse_rows_are_shaped_without_the_model(big):
    """The startup warmup shapes every track into a browse row
    (``fstree._shape_row``); 2.3.0-dev built a Pydantic ``TrackMeta`` per row
    (+2.5 s at 500K).  The defaults merge must stay well under the model's
    cost (≈ 0.2 of it), measured back to back on the same rows."""
    from soniqboom.models.track import TrackMeta
    s, _b = big
    rows = [s._tracks[t] for t in list(s._tracks)[: 20_000]]
    # (half of them as an older library stored them: fields missing)
    rows = [r if i % 2 else {k: v for k, v in r.items() if k not in ("loudness", "bpm", "isrc")}
            for i, r in enumerate(rows)]
    fields = TrackMeta.model_fields

    def model():
        t = time.perf_counter()
        for d in rows:
            TrackMeta(**{k: v for k, v in d.items() if k in fields}).model_dump()
        return time.perf_counter() - t

    def fast():
        t = time.perf_counter()
        for d in rows:
            fstree._shape_row(d)
        return time.perf_counter() - t
    m, f = _best(model), _best(fast)
    assert f < 0.5 * m, f"shaping {f * 1e3:.0f} ms vs the model's {m * 1e3:.0f} ms"


# ── lists, search, folders, Track Info ───────────────────────────────────────
# (v2.2.0 / 2.3.0-dev medians at 500K in the comments; budgets ≥ 10× and
# ≥ 50 ms: a listener notices ~100 ms on a click)

@pytest.mark.parametrize("params", [
    {"limit": 200},                                   # 1.1 / 1.2 ms
    {"limit": 200, "offset": N // 2},                 # 1.1 / 1.1 ms
    {"limit": 200, "sort": "artist"},                 # 1.0 / 1.1 ms
    {"limit": 200, "offset": N // 2, "sort": "title"},  # 1.2 / 1.3 ms
])
async def test_tracks_paging(app, params):
    a, _s, _b = app
    async with _client(a) as c:
        await c.get("/api/tracks", params=params)
        assert await _best_ms(c, "/api/tracks", **params) < 50


async def test_search_preview_while_typing(app):
    """Every key a new prefix: the preview's set is built cold each time
    (v2.2.0 12 ms, 2.3.0-dev 14 ms median at 500K)."""
    a, _s, _b = app
    async with _client(a) as c:
        times = []
        for q in ("hubb", "hubba", "hubbar", "hubbard", "love n", "love ni", "love nig"):
            times.append(await _ms(c, "/api/search/page", q=q, limit=8, fields="list"))
    times.sort()
    assert times[len(times) // 2] < 100, times             # (median of the keys)
    assert times[-2] < 250, times                          # (no key waits long)


async def test_folder_listings(app):
    a, s, base = app
    hv = f"{base}/HVSC/MUSICIANS/H/Hubbard7_Rob"
    al = f"{base}/Main/Band 3/Album 3"
    async with _client(a) as c:
        # cold, then warm (v2.2.0 0.6 / 0.6 ms, 2.3.0-dev 0.8 / 0.7 ms)
        assert await _ms(c, "/api/fstree/tracks-with-meta", path=hv, recursive="false") < 100
        assert await _best_ms(c, "/api/fstree/tracks-with-meta", path=hv, recursive="false") < 30
        assert await _ms(c, "/api/fstree/tracks-with-meta", path=al, recursive="false") < 100
        # a recursive branch, first page (13.6 / 14.4 ms cold at 13K rows)
        rec = f"{base}/Modland/Protracker"
        assert await _ms(c, "/api/fstree/tracks-with-meta", path=rec, recursive="true", limit=200) < 200
        assert await _best_ms(c, "/api/fstree/tracks-with-meta", path=rec, recursive="true", limit=200) < 30
        # the subfolders of a parent of 800 (v2.2.0 474 ms, 2.3.0-dev 254 ms for 4,000)
        assert await _best_ms(c, "/api/fstree/children", path=f"{base}/Main") < 150


async def test_track_info_open(app):
    """The three requests Track Info makes (v2.2.0 1.3 ms, 2.3.0-dev 0.9 ms
    together, serial)."""
    a, s, _b = app
    ids = list(s._tracks)[:: N // 12][:12]
    async with _client(a) as c:
        worst = 0.0
        for tid in ids:
            t = time.perf_counter()
            for url in (f"/api/tracks/{tid}", f"/api/tracks/{tid}/extended",
                        f"/api/tracks/{tid}/chapters"):
                r = await c.get(url)
                assert r.status_code == 200, (url, r.text[:200])
            worst = max(worst, (time.perf_counter() - t) * 1000)
    assert worst < 100, worst


# ── after a write ────────────────────────────────────────────────────────────

async def test_counts_and_artists_after_a_write(app):
    """The sidebar re-asks counts (and an open Artists view its list) after
    every library write.  500K: v2.2.0 178 / 110 ms, 2.3.0-dev 218 / 150 ms
    (scaled to 100K ≈ 36 / 22 ms) — budget 150 ms, best of 3 writes."""
    a, s, _b = app
    mn = [i for i, t in s._tracks.items() if "/Main/" in t["path"]]
    async with _client(a) as c:
        await c.get("/api/library/counts"); await c.get("/api/library/artists")
        best = {"counts": 1e9, "artists": 1e9}
        for k in range(3):
            for name in best:
                s.update_track_fields(mn[100 + 2 * k + (name == "artists")],
                                      {"artist": f"Guard Write {k} {name}"})
                best[name] = min(best[name], await _ms(c, f"/api/library/{name}"))
    assert best["counts"] < 150 and best["artists"] < 150, best


async def test_the_artists_fold_and_count_after_a_write(app):
    """What the Artists view and the counts add to ``aggregate_artists``
    after a write — the scene musicians' fold — measured back to back with
    it (a loaded machine slows them all), best of 3 writes:

    * the counts COUNT the folded rows (``folded_artist_count``: no list, no
      sort); 2.3.0-dev built and re-sorted the whole fold for that number
      (+38 ms at 500K) — the count costs well under the fold (0.6 of it at
      500K, 0.4 here);
    * the fold itself (9 ms for 35K rows at 500K, was 12) stays a fraction
      of the aggregation it follows (0.4 here)."""
    from soniqboom.core import composers
    _a, s, _b = app
    mn = [i for i, t in s._tracks.items() if "/Main/" in t["path"]]
    agg = fold = count = 1e9
    for k in range(3):
        s.update_track_fields(mn[300 + k], {"artist": f"Ratio Write {k}"})
        await s.composer_index_async()            # (as the endpoints do first)
        assert composers.index_for(s).by_artist   # (musicians to fold)
        s.aggregate_facet("composer")             # (the counts read it anyway)
        t = time.perf_counter()
        rows = s.aggregate_artists()
        agg = min(agg, time.perf_counter() - t)
        t = time.perf_counter()
        composers.fold_artist_rows(s, rows)
        fold = min(fold, time.perf_counter() - t)
        t = time.perf_counter()
        composers.folded_artist_count(s, rows)
        count = min(count, time.perf_counter() - t)
    msg = f"aggregate {agg * 1e3:.1f} ms, fold {fold * 1e3:.1f} ms, count {count * 1e3:.1f} ms"
    assert count < 0.8 * fold, msg
    assert fold < 1.0 * agg, msg


# ── Subsonic ────────────────────────────────────────────────────────────────

async def test_subsonic_get_artists(app, monkeypatch):
    """First (built) and warm (memo).  500K: 1.76 s / 2.9 ms (v2.2.0),
    1.91 s / 2.9 ms (2.3.0-dev) — the first scales with the library:
    budget 1.5 s at 100K, warm 50 ms."""
    import types
    a, _s, _b = app
    user = types.SimpleNamespace(id="u1", username="alice", role="admin", enabled=True)
    monkeypatch.setattr(subsonic, "_require_user", lambda *a_, **k: user)
    subsonic._song_memo_clear()
    p = {"f": "json", "v": "1.16.1", "c": "t"}
    async with _client(a) as c:
        first = await _ms(c, "/rest/getArtists.view", **p)
        warm = await _best_ms(c, "/rest/getArtists.view", **p)
    assert first < 1500 and warm < 50, (first, warm)


# ── first play, seek ────────────────────────────────────────────────────────

async def _ttfb(app, path, query="", headers=()):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": query.encode(), "root_path": "",
             "headers": [(b"host", b"t")] + [(k.encode(), v.encode()) for k, v in headers],
             "client": ("127.0.0.1", 5), "server": ("t", 80), "state": {}}
    st = {"sent": False, "first": None, "status": None, "n": 0}
    done = asyncio.Event()

    async def receive():
        if not st["sent"]:
            st["sent"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            st["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            if msg.get("body") and st["first"] is None:
                st["first"] = time.perf_counter()
            st["n"] += len(msg.get("body", b""))
            if not msg.get("more_body"):
                done.set()
    t0 = time.perf_counter()
    await app(scope, receive, send)
    done.set()
    return st["status"], ((st["first"] or time.perf_counter()) - t0) * 1000, st["n"]


@pytest.fixture
def playable(tmp_path, monkeypatch):
    if not (SID.exists() and MOD.exists() and shutil.which("sidplayfp")
            and shutil.which("openmpt123") and shutil.which("ffmpeg")):
        pytest.skip("renderers or the local test tunes are not available")
    from soniqboom.config import settings
    from soniqboom.core import metadata
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    root = tmp_path / "play"
    root.mkdir()
    sid, mod = SID.read_bytes(), MOD.read_bytes()
    rows = {}
    for i in range(3):          # (each its own content: its own render)
        (root / f"s{i}.sid").write_bytes(sid[:0x16] + f"Guard {i}".encode().ljust(32, b"\0") + sid[0x36:])
        (root / f"m{i}.mod").write_bytes(f"guard {i}".encode().ljust(20, b"\0") + mod[20:])
    for f in sorted(root.iterdir()):
        d = metadata.extract(f, f.stem).model_dump()
        d.pop("embedding", None)
        d.update(dir_hash=ph(str(root)), scan_root_hash=ph(str(root)), is_duplicate_primary=True,
                 cover_art=None)
        rows[f.stem] = d
    s = TrackStore()
    s.bulk_load(rows, {}, {}, {}, {}, {}, {str(root): {"path": str(root), "status": "ok",
                                                      "path_hash": ph(str(root))}}, {}, {})
    s.rebuild_indexes()
    monkeypatch.setattr(store_mod, "_store", s)

    async def _noauth(*a, **k):
        return None
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)
    a = FastAPI()
    a.include_router(stream.router, prefix="/api")
    return a


@pytest.mark.parametrize("kind,cold_budget", [
    # first byte of a never-played tune — v2.2.0 241 / 120 ms, 2.3.0-dev 225 / 26 ms
    pytest.param("s", 1000, marks=pytest.mark.slow),   # (three full SID renders)
    ("m", 600),
])
async def test_first_play_and_seek(playable, kind, cold_budget):
    cold, warm, seek = [], [], []
    for i in range(3):
        status, ms, n = await _ttfb(playable, f"/api/stream/{kind}{i}")
        assert status == 200
        cold.append(ms)
        await asyncio.sleep(0.3)
        status, ms, n = await _ttfb(playable, f"/api/stream/{kind}{i}")
        warm.append(ms)
        status, ms, _ = await _ttfb(playable, f"/api/stream/{kind}{i}",
                                    headers=[("range", f"bytes={n // 2}-")])
        assert status == 206
        seek.append(ms)
    # warm / seek on the cached render: 2-3 ms / ~1 ms both versions
    assert min(cold) < cold_budget, cold
    assert min(warm) < 50 and min(seek) < 50, (warm, seek)
