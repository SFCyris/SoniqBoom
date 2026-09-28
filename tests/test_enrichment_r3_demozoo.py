# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-3: the Demozoo join memoises its index lookups across applies.

The post-scan runner re-joins the whole library after every scan; the title
search (``LIKE`` scans) and the per-artist handle queries only depend on the
index, so a second join over the same index must produce the same batch
without re-running them — and a rebuilt index must drop the memo."""
from __future__ import annotations

import os
import sqlite3

from soniqboom.core import demozoo

from test_demozoo_scene import _write_dump


class _Store:
    def __init__(self, tracks):
        self._tracks = tracks

    def all_tracks(self):
        return self._tracks


_TRACKS = [
    # unique handle → canonical year
    {"id": "t1", "format": "SID", "artist": "Moby", "title": "Ocean Loader 2", "year": 1999},
    {"id": "t2", "format": "SID", "artist": "Moby", "title": "Ocean Loader 2 (remix)"},
    # shared handle, disambiguated by the title
    {"id": "t3", "format": "SID", "artist": "zap", "title": "Zap Tune Deluxe"},
    # no artist: title-first lookup, corroborated by an in-module music credit
    {"id": "t4", "format": "ProTracker", "artist": "", "title": "Zap Tune Deluxe",
     "instruments": ["music by Zap"]},
]


def _setup(tmp_path, monkeypatch):
    dbp = tmp_path / "demozoo.sqlite"
    monkeypatch.setattr(demozoo, "_db_path", lambda: dbp)
    assert not demozoo.refresh_index(
        dump_path=_write_dump(tmp_path, with_supertype=True)).get("error")
    import soniqboom.core.store as store_mod
    tracks = [dict(t) for t in _TRACKS]
    monkeypatch.setattr(store_mod, "get_store", lambda: _Store(tracks))
    stmts: list[str] = []
    real_connect = sqlite3.connect

    def connect(*a, **k):
        con = real_connect(*a, **k)
        con.set_trace_callback(stmts.append)
        return con
    monkeypatch.setattr(demozoo.sqlite3, "connect", connect)
    return dbp, stmts


def test_second_join_reuses_the_index_lookups(tmp_path, monkeypatch):
    dbp, stmts = _setup(tmp_path, monkeypatch)
    first = demozoo.collect_updates()
    assert any("LIKE" in s for s in stmts)                  # title search ran
    assert any("FROM scener WHERE name" in s for s in stmts)
    by_id = dict(first[1])
    assert by_id["t1"]["year"] == 1987
    assert by_id["t4"]["composer"] == "Zap"
    stmts.clear()
    second = demozoo.collect_updates()
    assert second == first
    assert not any("LIKE" in s for s in stmts)
    assert not any("WHERE name = ?" in s or "WHERE name =" in s for s in stmts)


def test_a_rebuilt_index_drops_the_memo(tmp_path, monkeypatch):
    dbp, stmts = _setup(tmp_path, monkeypatch)
    first = demozoo.collect_updates()
    st = dbp.stat()
    os.utime(dbp, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))   # "rebuilt"
    stmts.clear()
    assert demozoo.collect_updates() == first
    assert any("LIKE" in s for s in stmts)


def test_lookup_by_title_returns_copies(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    kw = dict(author_hints=("zap",), credit_hints={"zap": "Zap"})
    a = demozoo.lookup_by_title("Zap Tune Deluxe", **kw)
    assert a and a["_persist"] == "Zap"
    a["groups"].append("mutated")
    a["_persist"] = "x"
    b = demozoo.lookup_by_title("zap-tune  DELUXE", **kw)   # same tokens → memo hit
    assert b["_persist"] == "Zap" and "mutated" not in b["groups"]
