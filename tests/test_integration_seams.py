"""Regressions found by the cross-session integration review (2026-10-05):
an interrupted multi-value repair must not strand Subsonic stars on dead ids,
the availability snapshot must follow an offline root whose track set churns
at the same count, and the Years legend must accept legacy string years."""
import asyncio
import copy

import pytest

from soniqboom.core import availability, repair, subsonic_state
from soniqboom.core import store as store_mod
from soniqboom.core import subsonic_index as sx
from soniqboom.core.store import TrackStore
from tests.test_offline_tracks import _get, _row, lib  # noqa: F401  (fixture)


def _t(i, artist, year=None):
    return {"id": f"t{i}", "path": f"/lib/p/{i}.mp3", "title": f"T{i}", "artist": artist,
            "album": "LP", "genre": ["Rock"], "format": "MP3", "added_at": 1000 + i,
            "scan_root_hash": "r", "dir_hash": "d", "year": year}


@pytest.fixture
def mv_store(tmp_path, monkeypatch):
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    s.upsert_tracks_batch([_t(0, "A\x00B")] + [_t(i, f"X{i}\x00Y") for i in range(1, 700)])
    st = subsonic_state.reset_state(tmp_path / "ss.json")
    return s, st


@pytest.mark.asyncio
async def test_a_repair_cut_off_at_a_write_keeps_the_stars(mv_store, monkeypatch):
    s, st = mv_store
    old = sx.artist_id("a\x00b")
    await st.star("u1", "artist", [old])
    real = s.update_track_fields_batch
    calls = {"n": 0}

    def cut(items):
        calls["n"] += 1
        if calls["n"] == 2:
            raise asyncio.CancelledError()            # the stop cancels the startup task
        return real(items)
    monkeypatch.setattr(s, "update_track_fields_batch", cut)
    with pytest.raises(asyncio.CancelledError):
        await repair.repair_multi_values_once(s)
    monkeypatch.setattr(s, "update_track_fields_batch", real)
    await repair.repair_multi_values_once(s)           # the next start
    assert sx.artist_id("a") in st.starred("u1", "artist")
    assert old not in st.starred("u1", "artist")


@pytest.mark.asyncio
async def test_a_repair_cut_off_before_its_move_finishes_it_at_the_next_start(mv_store, monkeypatch):
    s, st = mv_store
    old = sx.artist_id("a\x00b")
    await st.star("u1", "artist", [old])
    real = st.remap_ids

    async def cut(mapping):
        raise asyncio.CancelledError()                # cut off after the chunk was written
    monkeypatch.setattr(st, "remap_ids", cut)
    with pytest.raises(asyncio.CancelledError):
        await repair.repair_multi_values_once(s)
    assert "\x00" not in s.get_track("t0")["artist"], "the chunk was written"
    assert s.get_config(repair.MULTIVALUE_PENDING_REMAP_KEY), "its move is recorded"
    monkeypatch.setattr(st, "remap_ids", real)
    await repair.repair_multi_values_once(s)           # the next start: nothing NUL left
    assert sx.artist_id("a") in st.starred("u1", "artist")
    assert not s.get_config(repair.MULTIVALUE_PENDING_REMAP_KEY)


@pytest.mark.asyncio
async def test_an_offline_root_that_churns_at_the_same_count_is_re_snapshotted(lib):  # noqa: F811
    s = lib.store
    lib.offline()
    lib.mode("hide")
    h0 = availability.hidden(s)
    assert len(h0) == 40
    gone = lib.ids_b[0]
    new = _row(f"{lib.root_b}/moved/new.mod", lib.root_b, 999)
    s.upsert_track(copy.deepcopy(new))
    s.delete_track(gone)
    h1 = availability.hidden(s)
    assert new["id"] in h1 and gone not in h1
    rows = await _get("/api/tracks?limit=500")
    assert not any(r["id"] == new["id"] for r in rows), "listed in Hide mode"


def test_the_years_legend_counts_legacy_string_years():
    s = TrackStore()
    s.upsert_tracks_batch([_t(1, "A", "19990101"), _t(2, "B", "1999"),
                           _t(3, "C", 2001), _t(4, "D", 1999.0), _t(5, "E", None)])
    want = [{"year": 2001, "count": 1}, {"year": 1999, "count": 3}]
    assert s.aggregate_years() == want
    assert s.aggregate_years(primary_only=True) == want
    assert s.aggregate_years(exclude={"t5"}) == want
    assert all(isinstance(r["year"], int) for r in s.aggregate_years())
