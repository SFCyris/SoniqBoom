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
