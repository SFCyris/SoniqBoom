# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The library's files: the merger (when it merges, where, what a merge that
dies half-way leaves), the shutdown snapshot, the fast reader / writer, the
.bak rotation and the cross-process lock."""
from __future__ import annotations

import io
import json
import math
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from soniqboom.core import merger, persistence


def _lib(d: Path, tracks=("t1", "t2"), **extra) -> dict:
    state = {"tracks": {t: {"id": t, "path": f"/m/{t}.mp3", "title": t} for t in tracks}, **extra}
    (d / "library.json").write_text(json.dumps(state))
    return state


def _aof(d: Path, *records) -> None:
    with open(d / "library.aof", "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _play(tid: str, ts: int) -> dict:
    return {"op": "record_play", "ts": ts, "id": tid}


def _hist(tid: str, ts: int) -> dict:
    return {"op": "push_history", "ts": ts, "data": {"track_id": tid, "ts": ts}}


def _boot(d: Path) -> dict:
    state = persistence.load_snapshot(d)
    persistence.replay_aof(state, d)
    return state


def _count(state: dict, tid: str) -> int:
    return (state.get("play_stats", {}).get(tid) or {}).get("count", 0)


# ── Plays counted once, whatever stops where ────────────────────────────────

@pytest.fixture()
def store():
    from soniqboom.core import store as store_mod
    old = store_mod._store
    store_mod._store = None
    yield
    store_mod._store = old


@pytest.mark.parametrize("then", ["next start", "merger's final merge"])
def test_a_shutdown_snapshot_does_not_count_the_plays_twice(tmp_path, store, then):
    """The QA report: plays since the last merge counted twice after a stop
    that wrote the full snapshot (AOF large / flush incomplete) — the snapshot
    held them AND the AOF still did, replayed (or merged) on top of it."""
    from soniqboom.core.aof import AOFWriter
    from soniqboom.core.store import get_store
    _lib(tmp_path)
    persistence.init_persistence(tmp_path)
    st = get_store()
    w = AOFWriter(tmp_path / "library.aof")
    st._aof_append = w.append
    st.record_play("t1")
    st.record_play("t1")
    st.push_history({"track_id": "t1", "ts": 5})
    w.flush_sync()
    persistence.write_snapshot_sync(tmp_path, consume_aof=True)
    assert (tmp_path / "library.aof").stat().st_size == 0
    if then == "merger's final merge":
        assert merger._do_merge(tmp_path) == 0
    state = _boot(tmp_path)
    assert _count(state, "t1") == 2
    assert len(state["history"]) == 1


def test_the_command_line_import_snapshot_leaves_the_aof_alone(tmp_path, store):
    """``write_snapshot_sync`` without ``consume_aof`` (the import: a store
    that isn't this AOF's) keeps today's behaviour — the AOF stays."""
    from soniqboom.core.store import get_store
    _lib(tmp_path)
    _aof(tmp_path, _play("t1", 10))
    before = (tmp_path / "library.aof").read_bytes()
    persistence.populate_store({"tracks": {"x": {"id": "x", "path": "/x"}}})
    get_store()
    persistence.write_snapshot_sync(tmp_path)
    assert (tmp_path / "library.aof").read_bytes() == before
    assert persistence.AOF_MARK not in json.loads((tmp_path / "library.json").read_text())


class _Died(BaseException):
    """Stands in for a SIGKILL between two steps of a write."""


def test_a_merge_that_dies_before_dropping_the_aof_applies_nothing_twice(tmp_path, monkeypatch):
    _lib(tmp_path)
    _aof(tmp_path, _play("t1", 10), _play("t1", 11), _hist("t1", 11))
    real_drop = persistence.drop_aof_prefix

    def die(*a, **k):
        raise _Died
    monkeypatch.setattr(persistence, "drop_aof_prefix", die)
    with pytest.raises(_Died):
        merger._do_merge(tmp_path)
    monkeypatch.setattr(persistence, "drop_aof_prefix", real_drop)
    # The snapshot was replaced (it holds the two plays) — the AOF still has them.
    assert _count(json.loads((tmp_path / "library.json").read_text()), "t1") == 2
    assert (tmp_path / "library.aof").stat().st_size > 0
    # A start now: the prefix the snapshot holds is skipped.
    assert _count(_boot(tmp_path), "t1") == 2
    # More plays arrive; the next merge drops the held prefix, applies only the new.
    _aof(tmp_path, _play("t1", 12), _play("t2", 12))
    assert merger._do_merge(tmp_path) == 2
    assert (tmp_path / "library.aof").stat().st_size == 0
    state = _boot(tmp_path)
    assert _count(state, "t1") == 3 and _count(state, "t2") == 1
    assert len(state["history"]) == 1


def test_a_merge_with_only_held_entries_left_just_drops_them(tmp_path, monkeypatch):
    _lib(tmp_path)
    _aof(tmp_path, _play("t1", 10))
    real_drop = persistence.drop_aof_prefix
    monkeypatch.setattr(persistence, "drop_aof_prefix", lambda *a: (_ for _ in ()).throw(_Died()))
    with pytest.raises(_Died):
        merger._do_merge(tmp_path)
    monkeypatch.setattr(persistence, "drop_aof_prefix", real_drop)
    snap = (tmp_path / "library.json").read_bytes()
    assert merger._do_merge(tmp_path) == 0
    assert (tmp_path / "library.aof").stat().st_size == 0
    assert (tmp_path / "library.json").read_bytes() == snap        # not rewritten
    assert _count(_boot(tmp_path), "t1") == 1


def test_a_shutdown_snapshot_that_dies_before_dropping_the_aof(tmp_path, store, monkeypatch):
    from soniqboom.core.store import get_store
    _lib(tmp_path)
    _aof(tmp_path, _play("t1", 10))
    persistence.init_persistence(tmp_path)
    assert get_store().get_play_stats("t1")["count"] == 1
    monkeypatch.setattr(persistence, "drop_aof_prefix", lambda *a: (_ for _ in ()).throw(_Died()))
    with pytest.raises(_Died):
        persistence.write_snapshot_sync(tmp_path, consume_aof=True)
    assert _count(_boot(tmp_path), "t1") == 1


def test_a_mark_that_does_not_match_the_aof_skips_nothing(tmp_path):
    """After the drop the AOF starts with other bytes: everything replays."""
    state = _lib(tmp_path)
    _aof(tmp_path, _play("t1", 10))
    data = (tmp_path / "library.aof").read_bytes()
    state[persistence.AOF_MARK] = persistence.aof_mark(data[:-1] + b" ")   # same size, other bytes
    (tmp_path / "library.json").write_text(json.dumps(state))
    assert _count(_boot(tmp_path), "t1") == 1
    state[persistence.AOF_MARK] = persistence.aof_mark(data + b"x")         # longer than the AOF
    (tmp_path / "library.json").write_text(json.dumps(state))
    assert _count(_boot(tmp_path), "t1") == 1
    assert persistence.aof_merged_prefix({persistence.AOF_MARK: {"size": True, "digest": "x"}}, data) == 0
    assert persistence.aof_merged_prefix({persistence.AOF_MARK: "junk"}, data) == 0


def test_an_old_snapshot_and_aof_replay_as_before(tmp_path):
    """Files from a server before the mark: no mark → the whole AOF replays."""
    _lib(tmp_path, play_stats={"t1": {"count": 3, "last_played": 5}})
    _aof(tmp_path, _play("t1", 10), {"op": "set_rating", "ts": 1, "id": "t2", "rating": 4})
    state = _boot(tmp_path)
    assert _count(state, "t1") == 4 and state["ratings"] == {"t2": 4}


def test_the_new_snapshot_reads_with_the_old_reader(tmp_path):
    """The mark is one more top-level key: the stdlib reader of an older
    server reads the file, and ``populate_store`` ignores the key."""
    _lib(tmp_path)
    _aof(tmp_path, _play("t1", 10))
    assert merger._do_merge(tmp_path) == 1
    with open(tmp_path / "library.json", "r") as f:          # the old reader
        old = json.load(f)
    assert old["play_stats"]["t1"]["count"] == 1 and persistence.AOF_MARK in old
    assert set(old) - {"tracks", "play_stats", persistence.AOF_MARK} == set()


# ── When a periodic merge runs ──────────────────────────────────────────────

def test_merge_due_on_size_or_age_only(tmp_path):
    aof = tmp_path / "library.aof"
    now = [1000.0]
    due = merger._MergeDue(aof, max_bytes=100, max_age=60, clock=lambda: now[0])
    assert not due()                                   # no AOF
    aof.write_bytes(b"x" * 10)
    assert not due()                                   # small, just seen
    now[0] += 59
    assert not due()
    now[0] += 1
    assert due()                                       # its oldest change waited 60 s
    due.merged()
    assert not due()                                   # what's left is dated afresh
    aof.write_bytes(b"x" * 100)
    assert due()                                       # big enough
    aof.write_bytes(b"")
    assert not due() and due.pending_since is None


def test_merge_limits_come_from_the_settings(monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "merger_max_aof_mb", 2.0, raising=False)
    monkeypatch.setattr(settings, "merger_max_age", 30.0, raising=False)
    assert merger._merge_limits(None, None) == (2 * 1024 * 1024, 30.0)
    assert merger._merge_limits(1, 5) == (1024 * 1024, 5.0)
    assert merger._merge_limits("junk", -3) == (32 * 1024 * 1024, 0.0)


def test_the_bundled_merger_merges_when_due_not_at_every_check(tmp_path, monkeypatch):
    """A play every 20 ms, a check every 20 ms: the old merger rewrote the
    library at every check that found the AOF non-empty; now only once the
    oldest change waited ``max_age`` (here 0.3 s)."""
    import asyncio
    _lib(tmp_path)
    merges = []

    def count_merge(d):
        merges.append(time.monotonic())
        return merger._do_merge(d)
    monkeypatch.setattr(merger, "_do_merge_in_child", count_merge)
    monkeypatch.setattr(merger, "_is_bundled", lambda: True)

    async def go():
        task = merger.start_merger(tmp_path, interval=0.02, max_aof_mb=32, max_age=0.3)
        for i in range(50):                             # ~1 s of plays
            _aof(tmp_path, _play("t1", 100 + i))
            await asyncio.sleep(0.02)
        await merger.stop_merger(task, final=True)
    asyncio.run(go())
    # ~1 s / 0.3 s → about 3 periodic merges (+ the final one), not ~50.
    assert 2 <= len(merges) <= 6, merges
    assert _count(_boot(tmp_path), "t1") == 50


def test_the_bundled_apps_merge_runs_in_a_child_process(tmp_path):
    """The merge itself (the parse + encode that held the GIL) runs in a
    child forked from the forkserver; its log lines come back."""
    _lib(tmp_path)
    _aof(tmp_path, _play("t1", 10), {"op": "set_rating", "ts": 1, "id": "t1", "rating": 3})
    with open(tmp_path / "library.aof", "a") as f:
        f.write("{corrupt\n")
    res = merger._run_merge_child(tmp_path)
    assert res is not None, "no child ran the merge"
    n, records = res
    assert n == 2
    assert any("Skipping corrupt AOF line" in msg for _name, _lvl, msg in records)
    state = _boot(tmp_path)
    assert _count(state, "t1") == 1 and state["ratings"] == {"t1": 3}
    assert (tmp_path / "library.aof").stat().st_size == 0


def test_the_bundled_merge_falls_back_to_the_server_without_a_child(tmp_path, monkeypatch):
    _lib(tmp_path)
    _aof(tmp_path, _play("t1", 10))
    monkeypatch.setattr(merger, "_run_merge_child", lambda d: None)
    assert merger._do_merge_in_child(tmp_path) == 1
    assert _count(_boot(tmp_path), "t1") == 1
    assert merger._do_merge_in_child(tmp_path) == 0          # empty AOF: no child, no work


# ── Reading and writing library.json ────────────────────────────────────────

_TRICKY = [
    {"a": 1, "b": 1.5, "c": -0.0, "d": 1e16, "e": 0.1, "f": 5e-324, "g": 1.7976931348623157e308},
    {"big": 2 ** 64 - 1, "bigger": 2 ** 64, "neg": -(2 ** 63), "negger": -(2 ** 63) - 1, "huge": 10 ** 30},
    {"nan": float("nan"), "inf": float("inf"), "ninf": -float("inf")},
    {"s": "é ü 😀   \x00 \x1f \"q\" \\ /", "lone": "\udc80\udcff", "k\udc80": 1},
    {"hexid": "1234567890123456789012345abc", "path": "/a/12345678901234567890.mp3"},
    {"nested": [[], {}, [1, [2, [3]]], {"x": None, "y": True, "z": False}]},
    {"list_of_big": [1, 12345678901234567890123, 3]},
]


@pytest.mark.parametrize("doc", _TRICKY)
def test_the_fast_reader_gives_what_the_stdlib_gives(doc, tmp_path):
    raw = json.dumps(doc).encode()
    want = json.loads(raw)
    got = persistence.loads_json(raw)
    assert repr(got) == repr(want)                     # same values AND types (int vs float)
    (tmp_path / "f.json").write_bytes(raw)
    assert repr(persistence._try_load_json(tmp_path / "f.json")) == repr(want)


def test_the_fast_reader_spots_long_integer_tokens():
    lt = persistence._long_int_token
    assert lt(b'{"a": 12345678901234567890}') and lt(b'[-1234567890123456789]')
    assert lt(b'{"a":1,"b":\n 1234567890123456789}')
    assert not lt(b'{"a": "x1234567890123456789012"}')     # inside a string
    assert not lt(b'{"a": 0.12345678901234567890}')        # a fraction
    assert not lt(b'{"a": 123456789012345678}')            # 18 digits fit
    assert lt(b'12345678901234567890')                     # a bare number


def test_trailing_garbage_is_still_trimmed(tmp_path):
    p = tmp_path / "library.json"
    p.write_bytes(json.dumps({"tracks": {"a": {"id": "a"}}}).encode() + b"\x00\x00garbage")
    assert persistence._try_load_json(p) == {"tracks": {"a": {"id": "a"}}}
    p.write_bytes(b"\xff\xfe not json")
    assert persistence._try_load_json(p) is None             # not UTF-8: no crash


def test_the_snapshot_bytes_are_what_json_dump_wrote(tmp_path):
    """Written with ``json.dumps`` (one C call) — byte for byte the old
    ``json.dump(state, f)`` output, NaN and all."""
    state = {"tracks": {"t": {"id": "t", "x": _TRICKY}}, "nan": math.nan}
    buf = io.StringIO()
    json.dump(state, buf)
    assert persistence.write_library_file(tmp_path, state, "test")
    assert (tmp_path / "library.json").read_text(encoding="utf-8") == buf.getvalue()


def test_the_backup_is_a_hard_link_rotation(tmp_path):
    _lib(tmp_path, tracks=("old",))
    old_bytes = (tmp_path / "library.json").read_bytes()
    old_inode = (tmp_path / "library.json").stat().st_ino
    assert persistence.write_library_file(tmp_path, {"tracks": {"new": {}}}, "test")
    bak = tmp_path / "library.json.bak"
    assert bak.read_bytes() == old_bytes and bak.stat().st_ino == old_inode   # no copy made
    assert (tmp_path / "library.json").stat().st_ino != old_inode
    assert json.loads((tmp_path / "library.json").read_text()) == {"tracks": {"new": {}}}
    assert not (tmp_path / "library.json.bak.new").exists()
    assert not (tmp_path / "library.json.new").exists()


def test_the_backup_is_copied_without_hard_links(tmp_path, monkeypatch):
    _lib(tmp_path, tracks=("old",))
    old_bytes = (tmp_path / "library.json").read_bytes()

    def no_link(*a, **k):
        raise OSError(45, "Operation not supported")
    monkeypatch.setattr(persistence.os, "link", no_link)
    assert persistence.write_library_file(tmp_path, {"tracks": {}}, "test")
    assert (tmp_path / "library.json.bak").read_bytes() == old_bytes
    assert (tmp_path / "library.json.bak").stat().st_ino != (tmp_path / "library.json").stat().st_ino


def test_a_failed_write_keeps_the_snapshot(tmp_path):
    _lib(tmp_path)
    before = (tmp_path / "library.json").read_bytes()
    with pytest.raises(TypeError):
        persistence.write_library_file(tmp_path, {"x": object()}, "test")
    assert (tmp_path / "library.json").read_bytes() == before
    assert not (tmp_path / "library.json.new").exists()


def test_the_empty_store_guard_still_holds(tmp_path, store):
    """A store that is empty never overwrites a populated snapshot (nor
    drops the AOF)."""
    _lib(tmp_path)
    _aof(tmp_path, _play("t1", 10))
    before = (tmp_path / "library.json").read_bytes(), (tmp_path / "library.aof").read_bytes()
    persistence.populate_store({})
    persistence.write_snapshot_sync(tmp_path, consume_aof=True)
    assert ((tmp_path / "library.json").read_bytes(), (tmp_path / "library.aof").read_bytes()) == before


# ── Between processes ───────────────────────────────────────────────────────

def test_another_process_holding_the_library_lock_is_waited_for(tmp_path, store):
    """The merger process's merge and the server's load / snapshot exclude
    each other (an flock on library.lock) — the in-process lock alone didn't
    reach the merger process."""
    _lib(tmp_path)
    code = textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        from soniqboom.core.persistence import _library_flock
        with _library_flock(Path({str(tmp_path)!r})):
            print("held", flush=True)
            time.sleep(0.6)
        print("released", flush=True)
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                            cwd=str(Path(__file__).resolve().parents[1]))
    try:
        assert proc.stdout.readline().strip() == "held"
        t0 = time.monotonic()
        persistence.init_persistence(tmp_path)
        waited = time.monotonic() - t0
        assert waited >= 0.3, waited
        t0 = time.monotonic()
        persistence.write_snapshot_sync(tmp_path, consume_aof=True)    # free now
        assert time.monotonic() - t0 < 0.3
    finally:
        proc.wait(5)


def test_the_library_lock_is_reentrant_in_a_process(tmp_path):
    with persistence.library_files_locked(tmp_path):
        with persistence.library_files_locked(tmp_path):
            assert merger._do_merge(tmp_path) == 0
        assert persistence._flocks_held
    assert not persistence._flocks_held


# ── Review fixes: failed flush, two instances, the drop, the age, a stuck child ──

def test_records_a_failed_flush_kept_are_not_counted_twice(tmp_path, store):
    """The stop's AOF flush failed (records stay buffered, already in the
    store) → the full snapshot holds them; written at exit afterwards, the
    next start applied them on top of it.  The snapshot takes the writer:
    flushed into the AOF it consumes when it can, dropped once saved when it
    can't."""
    import fcntl
    from soniqboom.core.aof import AOFWriter
    from soniqboom.core.store import get_store
    for flush_works_later in (True, False):
        d = tmp_path / str(flush_works_later)
        d.mkdir()
        _lib(d)
        persistence.init_persistence(d)
        st = get_store()
        w = AOFWriter(d / "library.aof")
        w._FLOCK_BUDGET_SECONDS = 0.2
        st._aof_append = w.append
        st.record_play("t1")
        w.flush_sync()
        st.record_play("t2")                                  # buffered
        with open(d / "library.aof", "rb+") as holder:        # the stop's flush fails
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            w.stop()
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        assert w.buffer_depth == 1
        if not flush_works_later:
            w._write_sync = lambda data: (_ for _ in ()).throw(OSError("disk full"))
        persistence.write_snapshot_sync(d, consume_aof=True, aof_writer=w)
        w.__dict__.pop("_write_sync", None)
        assert w.buffer_depth == 0
        w._atexit_flush()                                     # the interpreter's exit
        state = _boot(d)
        assert (_count(state, "t1"), _count(state, "t2")) == (1, 1), flush_works_later


def test_a_second_instance_waits_for_the_first_to_finish_writing(tmp_path):
    """The app's Restart starts a new instance while the old one is still
    stopping: the new one loads only once the old one released the data
    directory — so the old one's snapshot never drops what the new one wrote."""
    _lib(tmp_path)
    code = textwrap.dedent(f"""
        import time
        from pathlib import Path
        from soniqboom.core import persistence
        assert persistence.claim_library(Path({str(tmp_path)!r}))
        print("held", flush=True)
        time.sleep(0.8)
        persistence.release_library()
        print("released", flush=True)
        time.sleep(5)
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                            cwd=str(Path(__file__).resolve().parents[1]))
    try:
        assert proc.stdout.readline().strip() == "held"
        assert not persistence.claim_library(tmp_path, wait=0.2)     # still held: given up
        assert not persistence.owns_library()
        t0 = time.monotonic()
        assert persistence.claim_library(tmp_path, wait=10)
        assert time.monotonic() - t0 >= 0.3                          # waited for the release
        assert persistence.owns_library() and persistence.claim_library(tmp_path)   # re-entrant
    finally:
        persistence.release_library()
        proc.kill()
        proc.wait(5)
    assert not persistence.owns_library()


def test_the_shutdown_snapshot_consumes_only_for_the_owner():
    """main.shutdown passes ``owns_library()`` as ``consume_aof``."""
    import inspect
    from soniqboom import main
    src = inspect.getsource(main.shutdown)
    assert "write_snapshot_sync, get_data_dir(), owns_library(), _aof_writer" in src
    assert src.index('_step("merger"') < src.index("release_library()")
    assert "claim_library(data_dir)" in inspect.getsource(main.startup)


def test_the_aof_drop_swaps_in_a_new_file_and_the_writer_follows(tmp_path):
    from soniqboom.core.aof import AOFWriter
    _lib(tmp_path)
    aof = tmp_path / "library.aof"
    w = AOFWriter(aof)
    w.append("record_play", id="t1", ts=10)
    w.flush_sync()                                        # the writer holds the file open
    merged = aof.stat().st_size
    w.append("record_play", id="t2", ts=11)
    w.flush_sync()                                        # written "during the merge"
    ino = aof.stat().st_ino
    persistence.drop_aof_prefix(aof, merged)
    assert aof.stat().st_ino != ino                       # a new file
    assert json.loads(aof.read_text())["id"] == "t2"
    w.append("record_play", id="t1", ts=12)
    w.flush_sync()                                        # lands in the NEW file
    assert [json.loads(l)["ts"] for l in aof.read_text().splitlines()] == [11, 12]
    assert not (tmp_path / "library.aof.new").exists()
    w.stop()


def test_a_merge_killed_inside_the_drop_applies_nothing_twice(tmp_path, monkeypatch):
    """The drop's rest is written aside and swapped in: dying before the swap
    leaves the whole AOF (its prefix recognised by the mark)."""
    _lib(tmp_path)
    _aof(tmp_path, *[_play("t1", 1000 + i) for i in range(50)])
    real_write = persistence.write_library_file

    def write_then_append(d, state, who):                 # 10 plays arrive mid-merge
        ok = real_write(d, state, who)
        _aof(tmp_path, *[_play("t1", 5000 + i) for i in range(10)])
        return ok
    monkeypatch.setattr(persistence, "write_library_file", write_then_append)
    real_replace = persistence.os.replace

    def die_on_aof(src, dst):
        if str(dst).endswith("library.aof"):
            raise _Died
        return real_replace(src, dst)
    monkeypatch.setattr(persistence.os, "replace", die_on_aof)
    with pytest.raises(_Died):
        merger._do_merge(tmp_path)
    monkeypatch.undo()
    assert not (tmp_path / "library.aof.new").exists()
    assert _count(_boot(tmp_path), "t1") == 60
    assert merger._do_merge(tmp_path) == 10
    assert _count(_boot(tmp_path), "t1") == 60


def test_the_age_of_an_aof_a_previous_run_left_counts(tmp_path):
    """The oldest change is dated by its record, not by this run's first
    check — the bundled app's stop merges nothing, so an AOF can outlive
    many short sessions."""
    aof = tmp_path / "library.aof"
    _aof(tmp_path, {"op": "record_play", "ts": int(time.time()) - 3600, "id": "t1"})
    assert merger._aof_first_age(aof) >= 3599
    assert merger._MergeDue(aof, max_bytes=1 << 30, max_age=1800)()       # due at once
    aof.write_text(json.dumps({"op": "set_rating", "ts": time.time() - 5, "id": "t", "rating": 1}) + "\n")
    assert not merger._MergeDue(aof, max_bytes=1 << 30, max_age=1800)()
    aof.write_text("garbage\n")
    assert merger._aof_first_age(aof) == 0.0
    # Within a run, a new AOF is dated by observation: a back-dated scrobble at
    # its head doesn't make every check merge.
    now = [0.0]
    due = merger._MergeDue(aof, max_bytes=1 << 30, max_age=60, clock=lambda: now[0])
    aof.write_text("")
    assert not due()                                    # this run's first check: empty
    _aof(tmp_path, {"op": "record_play", "ts": 100, "id": "t1"})
    assert not due()
    now[0] = 60.0
    assert due()


def test_a_stuck_merge_child_is_killed_not_waited_for(tmp_path, monkeypatch):
    """A child that can't finish (here: the library lock held elsewhere)
    is killed after the timeout instead of holding the server's lock."""
    _lib(tmp_path)
    _aof(tmp_path, _play("t1", 10))
    monkeypatch.setattr(merger, "_MERGE_CHILD_TIMEOUT_S", 1.5)
    with persistence._library_flock(tmp_path):            # the child blocks on it
        t0 = time.monotonic()
        assert merger._run_merge_child(tmp_path) == (0, [])
        assert time.monotonic() - t0 < 10
    assert merger._do_merge(tmp_path) == 1                 # nothing half-done
    assert _count(_boot(tmp_path), "t1") == 1


# ── Final-QA fixes: late writes, the snapshot's count, concurrent stops ──────

def _aof_ids(d: Path, key: str = "id") -> list:
    return [json.loads(l).get(key) for l in (d / "library.aof").read_text().splitlines()]


def test_writes_after_the_journal_ends_never_reach_the_aof(tmp_path, store):
    """M4: the old run's last store writes (a scan's tail) landed in its
    writer's buffer after the stop's flush and reached the AOF only at the
    process's exit — after the new instance had loaded it (lost: its snapshot
    dropped them), or in the bundled app after a newer run's records
    (replayed over them).  The stop seals the journal before it releases the
    data directory: a later write is set aside, never appended."""
    from soniqboom.core import store as store_mod
    from soniqboom.core.aof import AOFWriter
    d = tmp_path
    _lib(d)
    store_mod._store = None
    assert persistence.claim_library(d)
    persistence.init_persistence(d)
    a = store_mod.get_store()
    wa = AOFWriter(d / "library.aof")
    a._aof_append = wa.append
    a.update_track_fields("t1", {"title": "A-early"})
    wa.stop()                                       # the stop's aof-flush step
    a.update_track_fields("t1", {"title": "A-flushed-by-seal"})
    assert wa.seal() == 0                           # written, then sealed
    persistence.release_library()
    a.update_track_fields("t1", {"title": "A-late"})        # a pass still ending
    # the new instance loads now
    store_mod._store = None
    assert persistence.claim_library(d)
    persistence.init_persistence(d)
    b = store_mod.get_store()
    assert b.get_track("t1")["title"] == "A-flushed-by-seal"
    wb = AOFWriter(d / "library.aof")
    b._aof_append = wb.append
    b.update_track_fields("t2", {"title": "B-edit"})
    wb.stop()
    wa._atexit_flush()                              # the old process exits only now
    titles = [json.loads(l)["data"]["title"] for l in (d / "library.aof").read_text().splitlines()]
    assert titles == ["A-early", "A-flushed-by-seal", "B-edit"]
    late = list(d.glob("library.aof.late-*"))
    assert len(late) == 1 and "A-late" in late[0].read_text()
    persistence.release_library()
    state = _boot(d)
    assert state["tracks"]["t1"]["title"] == "A-flushed-by-seal"
    assert state["tracks"]["t2"]["title"] == "B-edit"


def test_a_sealed_writer_sets_aside_what_slips_into_its_buffer(tmp_path):
    """A record that reaches the buffer around the seal (another thread) is
    set aside by any later flush — the exit's included — never written."""
    from soniqboom.core.aof import AOFWriter
    (tmp_path / "library.aof").touch()
    w = AOFWriter(tmp_path / "library.aof")
    w.append("record_play", id="A")
    w.seal()
    w._buffer.append(json.dumps({"op": "record_play", "id": "B"}) + "\n")   # raced in
    w.append("record_play", id="C")
    w._atexit_flush()
    assert _aof_ids(tmp_path) == ["A"]
    late = next(tmp_path.glob("library.aof.late-*")).read_text()
    assert '"B"' in late and '"C"' in late and w.buffer_depth == 0


def test_two_concurrent_stops_write_every_record_once(tmp_path):
    """The aof-flush step timed out (2.0 s) while its stop still waited on the
    merger's flock (2.5 s) and the snapshot's stop ran alongside: the AOF got
    [A, B, A, B] and a record appended meanwhile was deleted unwritten."""
    import fcntl
    import threading
    from soniqboom.core.aof import AOFWriter
    (tmp_path / "library.aof").touch()
    w = AOFWriter(tmp_path / "library.aof")
    w.append("record_play", id="A")
    w.append("record_play", id="B")
    holder = open(tmp_path / "library.aof", "rb+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)     # a merge's drop holds the AOF
    t1 = threading.Thread(target=w.stop)
    t1.start()
    time.sleep(0.3)
    t2 = threading.Thread(target=w.stop)
    t2.start()
    time.sleep(0.1)
    w.append("record_play", id="C")                 # the loop keeps writing
    time.sleep(0.5)
    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    holder.close()
    t1.join(10)
    t2.join(10)
    assert _aof_ids(tmp_path) == ["A", "B", "C"] and w.buffer_depth == 0


def test_the_flush_step_outlasts_the_writers_flock_wait():
    """The aof-flush step's timeout used to be 2.0 s against a 2.5 s flock
    wait; the seal step's covers the seal's own retry budget."""
    import inspect
    from soniqboom import main
    src = inspect.getsource(main.shutdown)
    assert "timeout=_AOFWriter._FLOCK_BUDGET_SECONDS + 1.0" in src
    assert "_seal_budget = _AOFWriter._FLOCK_BUDGET_SECONDS * 2" in src
    assert "asyncio.to_thread(_aof_writer.seal, _seal_budget)" in src
    assert "timeout=_seal_budget + 1.0" in src


def test_a_seal_that_times_out_lets_nothing_reach_the_aof_after_the_release(tmp_path):
    """The aof-seal step timed out while its seal waited for the writer's
    lock (a timed-out snapshot still held it): the data directory was
    released, a late write buffered, and both reached the AOF once the lock
    freed.  Intake closes on the loop before the step, the journal ends
    after it whatever it did."""
    import threading
    from soniqboom.core.aof import AOFWriter
    w = AOFWriter(tmp_path / "library.aof")
    w.append("record_play", id="before")
    w.flush_sync()
    w.append("record_play", id="pending")             # buffered when the stop seals
    held, go = threading.Event(), threading.Event()

    def snapshot_still_writing():
        with w.exclusive():
            held.set()
            go.wait(5)
    t = threading.Thread(target=snapshot_still_writing)
    t.start()
    held.wait(5)
    w.close_intake()                                  # main.shutdown, on the loop
    sealer = threading.Thread(target=w.seal, args=(5.0,))
    sealer.start()
    time.sleep(0.3)                                   # … the step times out
    w.end_journal()                                   # … and release_library() follows
    w.append("record_play", id="after-release")
    go.set()
    t.join(5)
    sealer.join(10)
    assert _aof_ids(tmp_path) == ["before"]
    late = next(tmp_path.glob("library.aof.late-*")).read_text()
    assert '"pending"' in late and '"after-release"' in late


def test_the_final_flush_is_retried_before_records_are_set_aside(tmp_path):
    """A merge held the AOF longer than one flock wait: the seal tries again
    within its budget instead of setting the records aside at once."""
    import fcntl
    import threading
    from soniqboom.core.aof import AOFWriter
    (tmp_path / "library.aof").touch()
    w = AOFWriter(tmp_path / "library.aof")
    w._FLOCK_BUDGET_SECONDS = 0.2
    w.append("record_play", id="A")
    holder = open(tmp_path / "library.aof", "rb+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    threading.Timer(0.6, lambda: fcntl.flock(holder.fileno(), fcntl.LOCK_UN)).start()
    try:
        assert w.seal(3.0) == 0
    finally:
        time.sleep(0.1)
        holder.close()
    assert _aof_ids(tmp_path) == ["A"] and not list(tmp_path.glob("library.aof.late-*"))


async def test_a_cancelled_auto_flush_does_not_leave_its_records_for_the_stop(tmp_path):
    """The stop's cancel_flush_task() while the auto-flush awaited its slow
    executor write: the write completed, the task's ``del buffer`` never ran,
    and stop() wrote the same records again."""
    import asyncio
    from soniqboom.core.aof import AOFWriter
    w = AOFWriter(tmp_path / "library.aof", flush_interval=0.05)
    real = w._write_sync

    def slow(data):
        time.sleep(0.2)
        real(data)
    w._write_sync = slow
    await w.start_auto_flush()
    w.append("record_play", id="t1")
    await asyncio.sleep(0.08)                       # the flush is inside run_in_executor
    w.cancel_flush_task()
    await asyncio.to_thread(w.stop)
    assert _aof_ids(tmp_path) == ["t1"]


async def test_writes_during_the_shutdown_snapshot_are_counted_once(tmp_path, store):
    """Writes landing between the count of the buffered records and the
    encode of the store were in the snapshot AND written at exit (a live run:
    4,895 plays counted as 8,531).  The count and the encode run together on
    the loop that makes the writes."""
    import asyncio
    from soniqboom.core.aof import AOFWriter
    from soniqboom.core.store import get_store
    _lib(tmp_path)
    persistence.init_persistence(tmp_path)
    st = get_store()
    w = AOFWriter(tmp_path / "library.aof")
    st._aof_append = w.append
    st.record_play("t1")
    w.flush_sync()
    real_mark = persistence.aof_mark

    def slow_mark(data):                            # a big AOF's hash: the loop runs on
        time.sleep(0.05)
        return real_mark(data)
    persistence.aof_mark = slow_mark
    made = 0
    done = False

    async def plays():
        nonlocal made
        while not done:
            st.record_play("t2")
            made += 1
            await asyncio.sleep(0)
    try:
        task = asyncio.create_task(plays())
        await asyncio.sleep(0.01)
        await asyncio.to_thread(persistence.write_snapshot_sync, tmp_path, True, w,
                                asyncio.get_running_loop())
        done = True
        await task
    finally:
        persistence.aof_mark = real_mark
    w._atexit_flush()                               # the exit writes what came after
    state = _boot(tmp_path)
    assert made > 5
    assert (_count(state, "t1"), _count(state, "t2")) == (1, made)


def test_a_failed_drop_still_keeps_the_held_back_records_out(tmp_path, store, monkeypatch):
    """The snapshot was saved but dropping the AOF prefix failed: the records
    the snapshot holds from the buffer must still not be written at exit."""
    import fcntl
    from soniqboom.core.aof import AOFWriter
    from soniqboom.core.store import get_store
    _lib(tmp_path)
    persistence.init_persistence(tmp_path)
    st = get_store()
    w = AOFWriter(tmp_path / "library.aof")
    w._FLOCK_BUDGET_SECONDS = 0.2
    st._aof_append = w.append
    st.record_play("t1")
    w.flush_sync()
    st.record_play("t2")                            # stays buffered: the flush fails
    with open(tmp_path / "library.aof", "rb+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        w.stop()
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    assert w.buffer_depth == 1
    w._write_sync = lambda data: (_ for _ in ()).throw(OSError("still busy"))

    def broken_drop(path, size):
        raise OSError("disk went away")
    monkeypatch.setattr(persistence, "drop_aof_prefix", broken_drop)
    with pytest.raises(OSError):
        persistence.write_snapshot_sync(tmp_path, consume_aof=True, aof_writer=w)
    assert w.buffer_depth == 0                      # dropped all the same
    w.__dict__.pop("_write_sync", None)
    w._atexit_flush()
    state = _boot(tmp_path)                         # the mark skips the AOF prefix
    assert (_count(state, "t1"), _count(state, "t2")) == (1, 1)


def test_a_deleted_config_key_is_gone_after_replay_and_merge(tmp_path, store):
    from soniqboom.core.aof import AOFWriter
    from soniqboom.core.store import get_store
    _lib(tmp_path)
    persistence.init_persistence(tmp_path)
    st = get_store()
    w = AOFWriter(tmp_path / "library.aof")
    st._aof_append = w.append
    st.set_config("k", {"a": 1})
    st.delete_config("k")
    st.delete_config("never-there")                 # nothing journalled
    w.stop()
    assert "k" not in st._config
    assert _aof_ids(tmp_path, "op") == ["set_config", "delete_config"]
    assert "k" not in _boot(tmp_path).get("config", {})
    merger._do_merge(tmp_path)
    assert "k" not in persistence.load_snapshot(tmp_path).get("config", {})


def test_the_stop_ends_scans_before_the_flush_and_seals_before_the_release():
    import inspect
    from soniqboom import main
    src = inspect.getsource(main.shutdown)
    assert src.index('_step("scans"') < src.index("finalize_dup_pending()") < src.index('_step("aof-flush"')
    assert src.index('_step("aof-flush"') < src.index('_step("merger"') \
        < src.index("_aof_writer.close_intake()") < src.index('_step("aof-seal"') \
        < src.index("_aof_writer.end_journal()") < src.index("release_library()")
    assert "stop_background_writers" in src and "_bg_scan_tasks" in src
    assert "aof-seal" in main._WRITING_STEPS
    start = inspect.getsource(main.startup)
    assert start.index("_aof_append = None") < start.index("init_persistence(data_dir)")


async def test_scans_and_passes_are_stopped_before_the_journal_ends(store, monkeypatch):
    """``scanner.stop_background_writers``: the scan queue, the enrichment
    runner, the passes and the caller's tasks are cancelled and waited for —
    none writes the store after it returns."""
    import asyncio
    from soniqboom.core import art_backfill, folder_album, game_titles, repair, scanner
    from soniqboom.core.store import get_store
    st = get_store()
    st.upsert_track({"id": "t1", "path": "/m/t1.mp3", "title": "x"})
    writes: list = []
    finished: list = []

    async def writer(name):
        try:
            while True:
                st.record_play("t1")
                writes.append(name)
                await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            st.record_play("t1")                    # a finally that still writes
            writes.append(name + "-final")
            finished.append(name)
            raise
    scan = asyncio.create_task(writer("scan"))
    monkeypatch.setattr(scanner, "_scan_task", scan)
    monkeypatch.setattr(scanner, "_scan_queue", [(frozenset({"/m"}), None, None, False)])
    enrich = asyncio.create_task(writer("enrich"))
    monkeypatch.setattr(scanner, "_scene_autoapply_tasks", {enrich})
    fa = asyncio.create_task(writer("folder"))
    monkeypatch.setattr(folder_album, "_tasks", {fa})
    gt = asyncio.create_task(writer("game"))
    monkeypatch.setattr(game_titles, "_bg_tasks", {gt})
    art = asyncio.create_task(writer("art"))
    monkeypatch.setattr(art_backfill, "_tasks", {art})
    rep = asyncio.create_task(writer("repair"))
    monkeypatch.setattr(repair, "_task", rep)
    from soniqboom.api import fstree
    drill = asyncio.create_task(writer("drill"))
    monkeypatch.setattr(fstree, "_DRILL_TASKS", {drill})
    remote = asyncio.create_task(writer("remote"))
    await asyncio.sleep(0.03)
    n = await scanner.stop_background_writers([remote], timeout=2.0)
    assert n == 8 and sorted(finished) == sorted(
        ["scan", "enrich", "folder", "game", "art", "repair", "drill", "remote"])
    assert scanner._scan_queue == []
    before = len(writes)
    await asyncio.sleep(0.05)
    assert len(writes) == before                    # nothing writes any more
    assert await scanner.stop_background_writers() == 0
