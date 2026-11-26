# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Waveforms and folder-hash lookups survive a restart (opportunity review
v2.2.0, defect 3).

They were kept in memory only: the background merger folds the AOF into
``library.json`` and the stop skips the full snapshot while the AOF is lean
(``main._snapshot_skippable``), so the next start lost every waveform and
lookup written since the last full snapshot — playlist rows without
waveforms, Subsonic folder albums named "Folder 02ab9b".  Every write is now
journaled (``store_waveforms`` / ``forget_waveforms`` / ``clear_waveforms`` /
``store_hash_lookups``), replayed and merged like the other records; a lookup
that is new moves the Subsonic catalogue's sequence, so its lists and
getAlbum read the same, current name.  (The review's repro r1 is the first
test.)"""
from __future__ import annotations

import pytest

from soniqboom.core import merger as M, persistence as P, store as S
from soniqboom.core.aof import AOFWriter


def _track(i: int, parent: str) -> dict:
    import hashlib
    import uuid
    path = f"{parent}/t{i}.mod"
    return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, path)), "path": path, "title": f"t{i}",
            "artist": "Rob Hubbard", "album": "", "format": "ProTracker", "duration": 60.0,
            "added_at": 1, "scan_root_hash": hashlib.sha256(b"/REOL").hexdigest()[:16],
            "dir_hash": hashlib.sha256(parent.encode()).hexdigest()[:16]}


class _Run:
    """One server run on ``d``: a fresh store loaded from the files, with its
    journal."""

    def __init__(self, d, monkeypatch):
        self.d = d
        monkeypatch.setattr(S, "_store", S.TrackStore())
        self.store = S.get_store()
        P.init_persistence(d)
        self.w = AOFWriter(d / "library.aof")
        self.store._aof_append = self.w.append

    def stop(self, merge: bool = True):
        """The ordinary stop of a lean run: the merger's merge, then no full
        snapshot (``main._snapshot_skippable``)."""
        self.w.flush_sync()
        if merge:
            M._do_merge(self.d)
        self.w.stop()
        import soniqboom.main as m
        assert m._snapshot_skippable(True, False, (self.d / "library.aof").stat().st_size, 32.0)
        self.w.seal()


def test_waveforms_and_lookups_survive_a_lean_restart(tmp_path, monkeypatch):
    """Repro r1: rating (journaled before), waveform and hash lookup."""
    from soniqboom.core.subsonic_index import _FOLDER_NAMES, folder_album_name
    r = _Run(tmp_path, monkeypatch)
    t = _track(1, "/REOL/Hubbard")
    r.store.upsert_tracks_batch([t])
    h = r.store.store_hash_lookups_batch(["/REOL/Hubbard"])["/REOL/Hubbard"]
    r.store.store_waveform(t["id"], [0.1, 0.5, 0.9])
    r.store.store_waveforms_batch({"x": [0.2], "y": [0.3]})
    r.store.set_rating(t["id"], 4)
    r.stop()
    r = _Run(tmp_path, monkeypatch)
    assert r.store.resolve_hash(h) == "/REOL/Hubbard"
    assert r.store.get_waveform(t["id"]) == [0.1, 0.5, 0.9]
    assert r.store.get_waveform("y") == [0.3] and r.store.get_rating(t["id"]) == 4
    _FOLDER_NAMES.pop(h, None)
    assert folder_album_name(r.store, h) == "Hubbard"


def test_forgotten_and_cleared_waveforms_stay_gone(tmp_path, monkeypatch):
    """Both removal ops, merged (cold) and replayed from the AOF (warm, other
    inputs): what a changed file's purge forgot / Settings cleared is not
    back after a restart."""
    r = _Run(tmp_path, monkeypatch)
    r.store.store_waveforms_batch({"a": [1.0], "b": [2.0], "c": [3.0]})
    r.stop()
    r = _Run(tmp_path, monkeypatch)
    assert r.store.forget_waveforms(["a", "zz"]) == 1
    r.stop(merge=True)
    r = _Run(tmp_path, monkeypatch)
    assert set(r.store.waveforms_view()) == {"b", "c"}
    assert r.store.clear_waveforms() == 2
    r.store.store_waveform("d", [4.0])
    r.stop(merge=False)                     # (left in the AOF: replayed at the start)
    r = _Run(tmp_path, monkeypatch)
    assert r.store.waveforms_view() == {"d": [4.0]}


def test_replay_and_merge_agree_with_the_live_store(tmp_path, monkeypatch):
    """The AOF replayed at a start and the merger's fold give what the live
    store held — and a record that fails is rolled back, not half applied."""
    r = _Run(tmp_path, monkeypatch)
    r.store.store_waveforms_batch({"a": [1.0], "b": [2.0]})
    r.store.forget_waveforms(["b"])
    r.store.store_hash_lookup("/x/y")
    r.store.store_hash_lookups_batch(["/x/z", "/x/y"])
    live = {k: dict(v) for k, v in r.store.to_snapshot().items()
            if k in ("waveforms", "hash_lookups")}
    r.w.flush_sync()
    state: dict = {}
    P.replay_aof(state, tmp_path)
    assert {k: state.get(k) for k in live} == live
    merged: dict = {}
    for e in M._parse_aof((tmp_path / "library.aof").read_bytes()):
        M._apply_entry(merged, e[1])
    assert {k: merged.get(k) for k in live} == live
    # undo covers the new ops (a failing apply restores the slot)
    st = {"waveforms": {"a": [1.0]}}
    undo = P._entry_undo(st, {"op": "clear_waveforms"})
    M._apply_entry(st, {"op": "clear_waveforms"})
    P._entry_restore(st, undo)
    assert st["waveforms"] == {"a": [1.0]}
    r.stop(merge=False)


def test_only_a_new_lookup_is_journaled_and_moves_the_catalogue(monkeypatch):
    st = S.TrackStore()
    recs: list = []
    st._aof_append = lambda op, **kw: recs.append(op)
    seq = st._catalog_seq
    st.store_hash_lookups_batch(["/a", "/b"])
    assert recs == ["store_hash_lookups"] and st._catalog_seq == seq + 1
    st.store_hash_lookups_batch(["/a", "/b"])               # (known: nothing written)
    st.store_hash_lookup("/a")
    assert recs == ["store_hash_lookups"] and st._catalog_seq == seq + 1
    st.store_hash_lookup("/c")
    assert recs == ["store_hash_lookups"] * 2


@pytest.mark.asyncio
async def test_the_async_batch_hashes_off_the_loop_and_journals_on_it(monkeypatch):
    import threading
    from soniqboom.core import data
    st = S.TrackStore()
    monkeypatch.setattr(S, "_store", st)
    where: list = []
    st._aof_append = lambda op, **kw: where.append((op, threading.current_thread().name))
    got = await data.store_hash_lookups_batch(["/m/one", "/m/two"])
    assert got == {"/m/one": data.path_hash("/m/one"), "/m/two": data.path_hash("/m/two")}
    assert where == [("store_hash_lookups", threading.current_thread().name)]
    assert st.resolve_hash(got["/m/two"]) == "/m/two"


def test_subsonic_lists_and_get_album_read_the_same_current_name(monkeypatch):
    """A folder album built while its hash was unknown is "Folder xxxxxx";
    once the lookup is stored, the catalogue (search3 / album lists) is
    rebuilt and names it like getAlbum does."""
    from soniqboom.api import subsonic
    from soniqboom.core import subsonic_index as sx
    st = S.TrackStore()
    st._aof_append = None
    monkeypatch.setattr(S, "_store", st)
    monkeypatch.setattr(subsonic, "_ALBUM_LIST_CACHE", {"store": None, "seq": None,
                                                        "built_at": 0.0, "folder_on": None,
                                                        "cat": None, "gen": 0,
                                                        "build_sec": 0.0, "task": None})
    monkeypatch.setattr(subsonic, "_CACHE_DEBOUNCE_SEC", 0.0)
    st.upsert_scan_dir("/REOL")
    st.store_hash_lookup("/REOL")
    parent = "/REOL/A Tribute to Jochen Hippel"
    st.upsert_tracks_batch([_track(i, parent) for i in range(3)])
    dh = _track(0, parent)["dir_hash"]
    sx._FOLDER_NAMES.pop(dh, None)

    def names() -> set:
        cat = subsonic._catalogue(st)
        return {e.name for e in cat.entries if e.kind == "folder"}, cat
    before, cat = names()
    assert before == {f"Folder {dh[:6]}"}
    st.store_hash_lookups_batch([parent])                   # (a scan stores it)
    after, cat = names()
    assert after == {"A Tribute to Jochen Hippel"}
    fid = next(e.id for e in cat.entries if e.kind == "folder")
    album, tracks = subsonic._album_and_tracks(st, fid)
    assert album["name"] == "A Tribute to Jochen Hippel" and len(tracks) == 3
