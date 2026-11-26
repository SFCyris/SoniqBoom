# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A library emptied on purpose stays empty after a restart (opportunity
review v2.2.0, "Also worth fixing").

A start (``persistence.load_snapshot``) and the merger took an empty
``library.json`` for a bad write and loaded ``library.json.bak`` — the
library from before the removal, one merge older — so the removed folders
came back and the playlists and settings changed since were lost.  The
delete that leaves no track now records ``LIBRARY_EMPTIED`` (journaled; the
next track added removes it): an empty snapshot carrying it is the library
as it is.  One without it — written empty by mistake — still falls back to
the backup, and the stop's snapshot still refuses to write such a state over
a populated file.  (The review's repro r3, both modes, are the first two
tests.)"""
from __future__ import annotations

import uuid

import pytest

from soniqboom.core import merger as M, persistence as P, store as S
from soniqboom.core.aof import AOFWriter


def _track(i: int) -> dict:
    path = f"/music/old/t{i}.flac"
    return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, path)), "path": path, "title": f"t{i}",
            "artist": "a", "album": "b", "format": "FLAC", "duration": 200.0,
            "added_at": 1, "scan_root_hash": "rh", "dir_hash": "dh"}


@pytest.fixture
def run(tmp_path, monkeypatch):
    """``start()`` → (store, writer) of a fresh run on ``tmp_path``."""
    def start():
        monkeypatch.setattr(S, "_store", S.TrackStore())
        st = S.get_store()
        P.init_persistence(tmp_path)
        w = AOFWriter(tmp_path / "library.aof")
        st._aof_append = w.append
        return st, w
    return tmp_path, start


def _day1(d, st, w):
    st.upsert_scan_dir("/music/old")
    st.upsert_tracks_batch([_track(i) for i in range(3)])
    w.flush_sync(); M._do_merge(d)
    st.create_playlist("p-old", "Old mix", [_track(0)["id"]], owner_user_id="u1")
    w.flush_sync(); M._do_merge(d)


def _day2(d, st, w):
    """Every folder removed with its tracks; a new playlist and a setting in
    the same merge window; a periodic merge while empty."""
    st.delete_track_ids([_track(i)["id"] for i in range(3)])
    st.delete_scan_dir("/music/old")
    st.create_playlist("p-new", "Made after the removal", [], owner_user_id="u1")
    st.set_config("some_setting", "changed")
    w.flush_sync(); M._do_merge(d)


def _stop(w):
    w.stop()
    w.seal()


def _assert_empty_as_left(st):
    assert st.track_count() == 0 and sorted(st._scan_dirs) == []
    assert sorted(st._playlists) == ["p-new", "p-old"]
    assert st.get_config("some_setting") == "changed"


def test_restart_keeps_the_library_emptied_on_purpose(run):
    d, start = run
    st, w = start()
    _day1(d, st, w)
    _day2(d, st, w)
    assert st.get_config(P.LIBRARY_EMPTIED)
    _stop(w)
    st, w = start()
    _assert_empty_as_left(st)
    _stop(w)


def test_the_next_merge_keeps_it_empty_too(run):
    d, start = run
    st, w = start()
    _day1(d, st, w)
    _day2(d, st, w)
    st.record_play("x", uid="u1")                           # (the server keeps running)
    w.flush_sync(); M._do_merge(d)
    disk = P._try_load_json(d / "library.json")
    assert not disk.get("tracks") and sorted(disk["playlists"]) == ["p-new", "p-old"]
    assert disk["config"]["some_setting"] == "changed"
    _stop(w)
    st, w = start()
    _assert_empty_as_left(st)
    _stop(w)


def test_an_empty_snapshot_written_by_mistake_still_falls_back(run):
    """The protection stays: an empty ``library.json`` without the mark (a
    run that loaded nothing and wrote what it had) loads the ``.bak``; the
    merger reads the ``.bak`` too."""
    d, start = run
    st, w = start()
    _day1(d, st, w)
    _stop(w)
    P._write_library_text(d, P.encode_state({"db_version": P.DB_VERSION, "tracks": {},
                                             "config": {}}), "Test")   # (.bak: the library)
    st, w = start()
    assert st.track_count() == 3 and "p-old" in st._playlists
    st.set_rating(_track(1)["id"], 5)
    w.flush_sync()
    M._do_merge(d)
    assert len(P._try_load_json(d / "library.json")["tracks"]) == 3
    _stop(w)


def test_the_stop_snapshot_writes_an_empty_library_only_when_emptied_on_purpose(run):
    d, start = run
    st, w = start()
    _day1(d, st, w)
    w.flush_sync(); M._do_merge(d)
    # an empty store without the mark (a load that failed): refused
    held = S._store
    S._store = S.TrackStore()
    assert P.write_snapshot_sync(d, consume_aof=False) is False
    assert len(P._try_load_json(d / "library.json")["tracks"]) == 3
    S._store = held
    # emptied by its own deletes: written
    st.delete_track_ids([_track(i)["id"] for i in range(3)])
    assert P.write_snapshot_sync(d, consume_aof=True, aof_writer=w) is True
    disk = P._try_load_json(d / "library.json")
    assert not disk["tracks"] and disk["config"][P.LIBRARY_EMPTIED]
    w.seal()


def test_the_mark_goes_with_the_next_track_added(run):
    d, start = run
    st, w = start()
    _day1(d, st, w)
    st.delete_track(_track(0)["id"])
    assert not st.get_config(P.LIBRARY_EMPTIED)             # (tracks are left)
    st.delete_track_ids([_track(1)["id"], _track(2)["id"]])
    assert st.get_config(P.LIBRARY_EMPTIED)
    st.upsert_track(_track(7))
    assert st.get_config(P.LIBRARY_EMPTIED) is None
    w.flush_sync(); M._do_merge(d)
    _stop(w)
    st, w = start()
    assert st.track_count() == 1 and st.get_config(P.LIBRARY_EMPTIED) is None
    _stop(w)
