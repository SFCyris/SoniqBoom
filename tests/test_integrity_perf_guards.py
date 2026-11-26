# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Performance guards for the data-integrity work of 2.3.0 (the paths a
listener waits on).  Budgets are several times what was measured on the
development Mac (in the comments), so a loaded machine passes — what they
catch is a change of order: a record replayed through the slow full-copy
path, a quadratic match, a listing check that walks the library.

* Every op the store journals has a cheap undo (``persistence._entry_undo``):
  otherwise AOF replay at a start falls back to copying every slot of the
  library per record (measured: 7,000 such records on a 150,000-track
  library replayed in 20 s instead of 0.08 s).
* The new records (waveforms, folder lookups) replay linearly.
* Matching moved files is linear (it runs off the loop above 5,000 rows).
* Marking a list page's rows offline costs no more with missing-folder
  tracks than with an offline drive."""
from __future__ import annotations

import time
import uuid

import pytest

from soniqboom.core import availability as A, merger as M, persistence as P
from soniqboom.core import store as S, track_moves


def _row(i: int, root: str = "/perf/root") -> dict:
    path = f"{root}/A{i % 300}/B{i % 3000}/{i:06d}.flac"
    return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, path)), "path": path,
            "title": f"T{i}", "artist": f"A{i % 300}", "album": f"B{i % 3000}",
            "format": "FLAC", "duration": 100.0 + i % 200, "file_size": 1_000_000 + i,
            "mtime": 1_700_000_000.0 + i, "added_at": 1, "content_key": f"k{i}",
            "scan_root_hash": "rh", "dir_hash": f"d{i % 3000}"}


def test_every_journaled_op_has_a_cheap_undo():
    st = S.TrackStore()
    recs: list = []
    st._aof_append = lambda op, **kw: recs.append({"op": op, **kw})
    a, b = _row(1), _row(2)
    st.upsert_track(a)
    st.upsert_tracks_batch([b])
    st.update_track_fields(a["id"], {"title": "x"})
    st.update_track_fields_batch([(b["id"], {"title": "y"})])
    st.set_rating(a["id"], 3)
    st.record_play(a["id"], uid="u")
    st.push_history({"track_id": a["id"], "ts": 1}, uid="u")
    st.create_playlist("p", "P", [a["id"]])
    st.update_playlist("p", {"name": "Q"})
    st.delete_playlist("p")
    st.upsert_scan_dir("/perf/root")
    st.delete_scan_dir("/perf/root")
    st.set_config("k", 1)
    st.delete_config("k")
    st.store_waveform(a["id"], [0.5])
    st.store_waveforms_batch({b["id"]: [0.2]})
    st.forget_waveforms([a["id"]])
    st.clear_waveforms()
    st.store_hash_lookups_batch(["/perf/root/A1"])
    st.remap_track_ids({a["id"]: "new"})
    st.move_user_data("u", "v")
    st.delete_user_data("v")
    st.delete_track_ids([a["id"], b["id"]])            # (also: the emptied mark)
    ops = {r["op"] for r in recs}
    assert {"store_waveforms", "forget_waveforms", "clear_waveforms",
            "store_hash_lookups", "delete_tracks", "set_config"} <= ops
    state = {"tracks": {}, "config": {}}
    for r in recs:
        assert P._entry_undo(state, r) is not None, f"{r['op']}: replayed by the slow path"


def test_waveform_and_lookup_records_replay_linearly(tmp_path):
    # measured (dev Mac, 150k tracks): 10,000 old + 7,000 new records 0.084 s
    tracks = {r["id"]: r for r in (_row(i) for i in range(50_000))}
    state = {"db_version": P.DB_VERSION, "tracks": tracks, "waveforms": {},
             "hash_lookups": {}, "config": {}}
    from soniqboom.core.aof import encode_record
    lines = [encode_record({"op": "store_waveforms", "ts": 1.0,
                            "data": {f"t{i}": [0.25] * 200}}) for i in range(2000)]
    lines += [encode_record({"op": "store_hash_lookups", "ts": 1.0,
                             "data": {f"h{i}": f"/v{i}"}}) for i in range(2000)]
    data = ("\n".join(lines) + "\n").encode()
    t0 = time.perf_counter()
    n = P.replay_aof(state, tmp_path, data, quarantine=False)
    took = time.perf_counter() - t0
    assert n == 4000 and len(state["waveforms"]) == 2000 and len(state["hash_lookups"]) == 2000
    assert took < 2.0, f"4,000 records took {took:.2f} s (the slow path: ~10 s here)"


def test_matching_moved_files_is_linear():
    # measured: 40,000 × 40,000 rows ≈ 0.3 s (off the loop above 5,000)
    old = {r["id"]: r for r in (_row(i, "/perf/old") for i in range(40_000))}
    new = {r["id"]: r for r in (_row(i, "/perf/new") for i in range(40_000))}
    t0 = time.perf_counter()
    m = track_moves.match_moves(old, new)
    took = time.perf_counter() - t0
    assert len(m) == 40_000
    assert took < 3.0, f"match_moves took {took:.2f} s"


def test_listing_marks_cost_no_more_with_missing_tracks(monkeypatch):
    st = S.TrackStore()
    st._aof_append = None
    rows = [_row(i) for i in range(20_000)]
    st.upsert_tracks_batch(rows)
    page = [st.get_track(r["id"]) for r in rows[:500]]

    def per_page() -> float:
        A.mark_rows(page, st)
        t0 = time.perf_counter()
        for _ in range(200):
            A.mark_rows(page, st)
        return (time.perf_counter() - t0) / 200
    st._scan_dirs["/perf/root"] = {"path": "/perf/root", "status": "unavailable",
                                   "path_hash": "rh"}
    dead = per_page()                                  # measured: ~160 µs a page
    del st._scan_dirs["/perf/root"]
    st.update_track_fields_batch([(r["id"], {"absent_since": 1}) for r in rows[:2000]])
    assert len(A.snapshot(st).absent) == 2000
    missing = per_page()                               # measured: ~105 µs a page
    assert sum(1 for r in A.mark_rows(page, st) if r.get("offline") == "drive") == 500
    assert missing < max(3 * dead, 0.002), (dead, missing)
