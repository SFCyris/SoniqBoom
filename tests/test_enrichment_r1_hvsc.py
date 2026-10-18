# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The HVSC re-apply writes the store on the event-loop thread (the store has
no lock), never from an executor worker."""
from __future__ import annotations

import threading

from soniqboom.core import folder_album as fa
from soniqboom.core import hvsc as hvsc_mod
from soniqboom.core import hvsc_apply
from soniqboom.core.store import TrackStore


class _FakeHvsc:
    def is_configured(self):
        return True

    def reload(self):
        pass

    def lookup_durations_by_md5(self, md5):
        return [123.0, 45.0]

    def stil_key_for(self, path):
        return None

    def lookup_stil_by_relpath(self, key):
        return None


async def test_hvsc_apply_writes_on_the_loop_thread(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(hvsc_mod, "get_hvsc", lambda: _FakeHvsc())
    refreshed = []

    async def _refresh(ids):                # the real one touches the data dir
        refreshed.extend(ids)
    monkeypatch.setattr(fa, "refresh_album_caches", _refresh)

    async def no_purge(ids, keep_duration=None):
        return 0
    monkeypatch.setattr("soniqboom.core.conversion_cache.purge_sid_entries_for", no_purge)
    s.upsert_tracks_batch([{"id": f"s{i}", "path": f"/m/{i}.sid", "title": "t",
                            "format": "SID", "duration": 180.0, "sid_md5": "a" * 32}
                           for i in range(5)])
    threads = []
    real = s.update_track_fields_batch
    monkeypatch.setattr(s, "update_track_fields_batch",
                        lambda items: threads.append(threading.current_thread()) or real(items))
    res = await hvsc_apply.apply_hvsc_to_library()
    assert res["updated"] == 5
    assert threads and all(t is threading.main_thread() for t in threads)
    assert s.get_track("s0")["duration"] == 123.0
    assert s.get_track("s0")["subsongs"] == 2
    assert s.verify_indexes()["index_ok"] is True
    assert sorted(refreshed) == [f"s{i}" for i in range(5)]   # the patched rows


async def test_the_join_reconciles_the_sid_render_cache(monkeypatch, tmp_path, tmp_data_dir):
    """The real conversion-cache purge (not stubbed): a SID's renders at its
    HVSC lengths stay, other lengths go; a SID HVSC doesn't know keeps the
    renders at the lengths it already had; one with none loses them all —
    in the scoped join as in the full one."""
    from collections import OrderedDict

    from soniqboom.core import conversion_cache as cc

    class _Hv(_FakeHvsc):
        def lookup_durations_by_md5(self, md5):
            return [111.0, 60.0] if md5 == "a" * 32 else []
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(hvsc_mod, "get_hvsc", lambda: _Hv())

    async def _refresh(ids):
        pass
    monkeypatch.setattr(fa, "refresh_album_caches", _refresh)
    s.upsert_tracks_batch([
        {"id": "A", "path": "/m/a.sid", "title": "a", "format": "SID", "sid_md5": "a" * 32,
         "duration": 111.0, "hvsc_lengths": [111.0, 60.0], "subsongs": 2},
        {"id": "B", "path": "/m/b.sid", "title": "b", "format": "SID", "sid_md5": "b" * 32,
         "duration": 200.0, "hvsc_lengths": [200.0, 90.0], "subsongs": 2},
        {"id": "C", "path": "/m/c.sid", "title": "c", "format": "SID", "sid_md5": "c" * 32},
        # (no tune lengths stored: its own duration is the length it keeps)
        {"id": "D", "path": "/m/d.sid", "title": "d", "format": "SID", "sid_md5": "d" * 32,
         "duration": 150.0},
        {"id": "X", "path": "/m/x.mp3", "title": "x", "format": "MP3", "duration": 5.0}])
    keys = ["A__sub0__dur111", "A__sub1__dur60", "A__sub0__dur180",
            "B__sub0__dur200", "B__sub1__dur90", "B__sub0__dur300",
            "C__sub0__dur120", "D__sub0__dur150", "D__sub0__dur151", "X__sub0__dur999"]

    def fill():
        meta, lru = {}, OrderedDict()
        for k in keys:
            p = tmp_path / f"{k}.wav"
            p.write_bytes(b"RIFF")
            meta[k] = {"path": str(p), "size_bytes": 4, "format_type": "sid", "created_at": 0}
            lru[k] = 0.0
        monkeypatch.setattr(cc, "_meta", meta)
        monkeypatch.setattr(cc, "_lru", lru)
        monkeypatch.setattr(cc, "_total_bytes", 4 * len(keys))
    want = ["A__sub0__dur111", "A__sub1__dur60", "B__sub0__dur200", "B__sub1__dur90",
            "D__sub0__dur150", "X__sub0__dur999"]
    for ids in (None, {"A", "B", "C", "D"}):
        fill()
        res = await hvsc_apply.apply_hvsc_to_library(ids=ids)
        assert sorted(cc._meta) == want, (ids, sorted(cc._meta))
        assert "Purged 4 stale render" in res["message"]
        assert not (tmp_path / "A__sub0__dur180.wav").exists()
        assert (tmp_path / "B__sub1__dur90.wav").exists()
