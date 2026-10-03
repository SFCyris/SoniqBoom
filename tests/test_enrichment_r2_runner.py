# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-2 review fixes around the enrichment runner and its neighbours:

* a remote (FTP/SMB) scan that extracted or removed files feeds the same
  post-scan runner as a local scan (Modland → Demozoo → backfill → folder
  pass); a no-change freshness poll only gets the cheap folder pass;
* the Demozoo apply skips its join when neither the library nor the index
  changed (the Admin button always joins);
* saving the uade VU option ON clears the "this build can't dump voices"
  latch;
* uade modules numbered from 1 keep their subsong base;
* the folder-album pass keeps one per-folder memo across chunks and its
  up-front guard pass is bounded;
* ``data`` / ``code`` / ``musicians``… are container words, not albums;
* ``game:`` in the shuffled play order (core/data.py maps ``@game_tag``
  to the store's ``game`` predicate)."""
from __future__ import annotations

import json
import shutil

import pytest

from soniqboom.core import demozoo, scanner, scene_metadata
from soniqboom.core import folder_album as fa
from soniqboom.core.store import TrackStore


# ── r2-enr-2: remote scans feed the scene runner ─────────────────────────────

class _Pool:
    def __init__(self, *a, **k):
        pass

    def shutdown(self, wait=True):
        pass


@pytest.fixture
def remote(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply", lambda: calls.append("runner"))
    monkeypatch.setattr(scanner, "_schedule_folder_album_pass", lambda: calls.append("folder"))
    monkeypatch.setattr(scanner, "ProcessPoolExecutor", _Pool)
    plans: list = []

    async def body(*_a, **_k):
        p = plans.pop(0)
        if isinstance(p, Exception):
            raise p
        return p
    monkeypatch.setattr(scanner, "_remote_scan_body", body)
    return calls, plans


@pytest.mark.parametrize("plan,expect", [
    ({"extract": 3, "new": 3, "ghosts": 0, "mtime_refresh": 0}, "runner"),
    ({"extract": 0, "ghosts": 2}, "runner"),
    ({"extract": 0, "ghosts": 0, "mtime_refresh": 40, "skip": 900}, "folder"),
    (None, "folder"),                                  # body returned nothing
    (RuntimeError("boom"), "folder"),                  # a crashed scan ({} plan)
])
async def test_remote_scan_routes_to_the_runner_only_on_change(remote, plan, expect):
    calls, plans = remote
    plans.append(plan)
    out = await scanner.start_remote_scan("share", "ftp://h/Music/Demo", object())
    assert calls == [expect]
    assert out == (plan if isinstance(plan, dict) else {})
    assert "ftp://h/Music/Demo" not in scanner._current_remote_dirs


async def test_a_changed_remote_scan_marks_the_runner_pending(monkeypatch):
    monkeypatch.setattr(scanner, "ProcessPoolExecutor", _Pool)

    async def body(*_a, **_k):
        return {"extract": 1, "ghosts": 0}
    monkeypatch.setattr(scanner, "_remote_scan_body", body)
    monkeypatch.setattr(scene_metadata, "has_index", lambda: True)
    started = []
    monkeypatch.setattr(scanner, "_ensure_scene_autoapply_runner", lambda: started.append(1))
    monkeypatch.setattr(scanner, "_scene_autoapply_pending", False)
    await scanner.start_remote_scan("share", "ftp://h/x", object())
    assert scanner._scene_autoapply_pending is True and started == [1]


# ── Demozoo: skip an unchanged join ──────────────────────────────────────────

async def test_demozoo_apply_skips_when_nothing_changed(tmp_path, monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    db = tmp_path / "demozoo.sqlite"
    db.write_bytes(b"x")
    monkeypatch.setattr(demozoo, "_db_path", lambda: db)
    monkeypatch.setattr(demozoo, "_last_apply_sig", None)
    demozoo._status["applying"] = False
    s.upsert_track({"id": "t", "path": "/m/t.mod", "artist": "Dalezy", "format": "ProTracker"})
    joins = []

    def collect(tracks=None, matched_ids=None):
        joins.append(1)
        return 1, ([("t", {"scene_group": "Fairlight"})] if len(joins) == 1 else [])
    monkeypatch.setattr(demozoo, "collect_updates", collect)
    res = await demozoo.apply_to_library()
    assert res["updated"] == 1 and s.get_track("t")["scene_group"] == "Fairlight"
    res = await demozoo.apply_to_library()              # only our own write since
    assert res.get("skipped") == "unchanged" and joins == [1]
    await demozoo.apply_to_library(force=True)          # the Admin button
    assert joins == [1, 1]
    s.update_track_fields("t", {"artist": "Jester"})    # a library change
    await demozoo.apply_to_library()
    assert joins == [1, 1, 1]
    db.write_bytes(b"refreshed index")                  # an index refresh
    await demozoo.apply_to_library()
    assert joins == [1, 1, 1, 1]


async def test_demozoo_apply_reruns_after_a_concurrent_mutation(tmp_path, monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    db = tmp_path / "demozoo.sqlite"
    db.write_bytes(b"x")
    monkeypatch.setattr(demozoo, "_db_path", lambda: db)
    monkeypatch.setattr(demozoo, "_last_apply_sig", None)
    demozoo._status["applying"] = False
    s.upsert_track({"id": "t", "path": "/m/t.mod", "artist": "A"})
    joins = []

    def collect(tracks=None, matched_ids=None):
        joins.append([t["id"] for t in tracks])
        s.upsert_track({"id": "u", "path": "/m/u.mod", "artist": "B"})  # a scan meanwhile
        return 0, []
    monkeypatch.setattr(demozoo, "collect_updates", collect)
    await demozoo.apply_to_library()
    # not recorded past the scan's write: the next apply joins it
    assert demozoo._last_apply_sig[0] != s.enrich_cursor()
    await demozoo.apply_to_library()
    assert joins == [["t"], ["u"]]


# ── r2-enr-18 / r2-enr-7: settings save ─────────────────────────────────────

@pytest.fixture
def settings_store(monkeypatch):
    s = TrackStore()
    for target in ("soniqboom.core.store.get_store", "soniqboom.core.data.get_store"):
        monkeypatch.setattr(target, lambda: s)
    return s


async def test_saving_vu_meters_on_resets_the_unsupported_latch(settings_store, monkeypatch):
    from soniqboom.api import admin, stream
    monkeypatch.setattr(stream, "_UADE_VU_UNSUPPORTED", True)
    await admin.update_settings({"uade_vu_meters": False})
    assert stream._UADE_VU_UNSUPPORTED is True           # off: the setting gates it
    await admin.update_settings({"render_prewarm": True})
    assert stream._UADE_VU_UNSUPPORTED is True           # an unrelated save
    await admin.update_settings({"uade_vu_meters": True})
    assert stream._UADE_VU_UNSUPPORTED is False
    assert settings_store.get_config("uade_vu_meters") is True


async def test_folder_albums_setting_shows_the_subsonic_default(settings_store):
    from soniqboom.api import admin, subsonic
    got = await admin.get_settings()
    assert got["subsonic_folder_albums"] is fa.SUBSONIC_FOLDER_ALBUMS_DEFAULT is True
    assert subsonic._folder_albums_on(settings_store) is True
    await admin.update_settings({"subsonic_folder_albums": False})
    assert (await admin.get_settings())["subsonic_folder_albums"] is False
    assert subsonic._folder_albums_on(settings_store) is False


# ── r2-enr-17: uade subsong base ─────────────────────────────────────────────

def test_uade_subsong_base_from_the_real_hippel_fixture(repo_root):
    from soniqboom.core import metadata
    f = repo_root / "internal/testdata/uade/Hippel COSO/dragonflight (town).hipc"
    if not f.exists() or not shutil.which("uade123"):
        pytest.skip("needs the local Hippel fixture and uade123")
    m = metadata.extract(f, "x")
    assert (m.subsongs, m.subsong_base) == (3, 1)          # uade: "min 1 max 3"


@pytest.mark.parametrize("rng,expect", [
    ("cur 1 min 1 max 3", (3, 1)),          # numbered from 1: base kept
    ("cur 0 min 0 max 4", (5, None)),       # numbered from 0: no base
    ("cur 1 min 1 max 1", (None, None)),    # a single tune: no picker
])
def test_uade_subsong_base_parsing(tmp_path, rng, expect):
    from soniqboom.core import metadata
    d = metadata._extract_uade(tmp_path / "mdat.song", "x",
                               {"playername": "TFMX", "subsongs": rng})
    assert (d.get("subsongs"), d.get("subsong_base")) == expect


# ── r2-enr-14: folder pass memo + bounded guard pass ─────────────────────────

def _retro(i, path, **kw):
    t = {"id": f"f{i}", "path": path, "title": f"tune {i}", "artist": "Someone",
         "album": "", "album_source": None, "format": "ProTracker", "genre": []}
    t.update(kw)
    return t


def test_chunked_collect_with_a_shared_memo_equals_one_call():
    tracks = [_retro(i, f"/m/Games/Turrican {i % 7}/t{i}.mod") for i in range(300)]
    tracks += [_retro(1000 + i, f"/m/pack{i % 3}.zip::data/t{i}.mod") for i in range(30)]
    args = dict(roots=frozenset({"/m"}), format_keys=frozenset(), author_keys=frozenset(),
                artist_keys=frozenset())
    whole = fa.collect_folder_updates(tracks, **args)
    rc, dc, state = {}, {}, fa.new_folder_state()
    for i in range(0, len(tracks), 17):
        assert fa.collect_folder_updates(tracks[i:i + 17], **args, retro_cache=rc,
                                         dir_cache=dc, state=state) == []
    chunked = fa.finalize_folder_updates(state)
    assert chunked == whole and len(whole) == 330
    assert len(dc) == 7 + 3                                 # one verdict per folder


async def test_guard_pass_stops_at_the_threshold_and_yields(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    s.upsert_tracks_batch([{"id": f"x{i}", "path": f"/m/{i}", "album": ""}
                           for i in range(60)])
    monkeypatch.setattr(fa, "_BATCH_MODE_THRESHOLD", 10)
    monkeypatch.setattr(fa, "_GUARD_CHUNK", 7)
    guarded = []
    real = fa._guard_item
    monkeypatch.setattr(fa, "_guard_item",
                        lambda *a: guarded.append(a[1]) or real(*a))
    items = [(f"x{i}", {"album": "A", "album_source": fa.SOURCE_FOLDER},
              {"album": "", "album_source": None}) for i in range(60)]
    written: list[str] = []
    updated, albums, bumps = await fa._commit_album_updates(items, written=written)
    assert updated == 60 and albums == 60
    # up-front pass: stops after the chunk that crossed the threshold (2 × 7
    # items), then one check per written item
    assert len(guarded) == 14 + 60
    assert written == [f"x{i}" for i in range(60)]


# ── r2-enr-16: container words ───────────────────────────────────────────────

@pytest.mark.parametrize("name", ["data", "DATA", "code", "Musicians", "artists", "authors"])
def test_container_words_are_generic(name):
    assert fa._generic(fa.clean_folder_name(name))


def test_an_in_archive_data_dir_gives_the_archive_name():
    assert fa.folder_candidate("/x/hjb_mifi.zip::data/a.mod", frozenset({"/x"})) == \
        ("hjb_mifi", "", True)
    assert not fa._generic("Data East")


# ── r2-enr-1: game: in the shuffled play order ───────────────────────────────

async def test_shuffled_order_honours_game(monkeypatch):
    from soniqboom.api import tracks as tracks_api
    from soniqboom.core import shuffle_order
    from soniqboom.core.data import _parse_tag_query
    assert _parse_tag_query("@game_tag:{uridium}") == {"game": "uridium"}
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    s.upsert_tracks_batch([dict(t, path=f"/m/{t['id']}", format="SID", genre=[],
                                added_at=i + 1) for i, t in enumerate([
        {"id": "u2", "title": "Uridium 2", "album": ""},
        {"id": "u2l", "title": "Uridium 2 Loader", "album": ""},
        {"id": "ualb", "title": "Title", "album": "Uridium", "album_source": "folder"},
        {"id": "other", "title": "Paradroid", "album": "Hewson"},
    ])])
    shuffle_order.clear()
    args = dict(artist=None, album_artist=None, album=None, genre=None, scene_group=None,
                format=None, year_min=None, year_max=None, untagged=None)
    body = json.loads((await tracks_api.shuffled_tracks(
        q="game:uridium", seed=7, offset=0, limit=50, **args)).body)
    assert body["total"] == len(s.filter_track_ids(game="uridium")) == 3
    assert {t["id"] for t in body["tracks"]} == {"u2", "u2l", "ualb"}
    body = json.loads((await tracks_api.shuffled_tracks(
        q="game:uridium loader", seed=7, offset=0, limit=50, **args)).body)
    assert [t["id"] for t in body["tracks"]] == ["u2l"]
