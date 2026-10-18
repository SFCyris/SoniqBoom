# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tracks a renderer can't play, marked when they are played
(core/unplayable.py).

A renderer that refuses the same file the same way twice in a row — uade
"score crashed" on a DeliTracker custom module, for one — marks the track
``defect`` "unplayable" with the reason, the renderer and its version; the
track is passed by when a list plays through, its play answers a 422 naming
the reason, and a mark is cleared when the renderer or SoniqBoom changes,
when the file changes, or when a render of it plays.  Transient failures —
a timeout, a render we stopped, a killed renderer, a cut download, a full
disk — never mark.

The renderer here is a stand-in ``uade123`` (a script answering as uade 3.05
does for the real module in the report: its stderr, exit 1, a bare WAV
header), so no module file is needed; one test runs the real uade on a tiny
module made here whose player executes an illegal instruction.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import struct
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

from soniqboom.core import availability, skip_rules, unplayable
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore, _carry_enrichment

UADE_CRASH = ("Illegal instruction: 4afc at 00001320\n"
              "uade warning: Song ended prematurely due to error: score crashed\n"
              "Can not play seven cities of gold.cus\n")

STUB = r'''import math, os, struct, sys, time
d = os.path.dirname(os.path.abspath(__file__))
if "--version" in sys.argv:
    v = open(os.path.join(d, "version")).read().strip() if os.path.exists(os.path.join(d, "version")) else "3.05"
    print("uade123 " + v)
    sys.exit(0)
plan = open(os.path.join(d, "plan")).read().split()
cp = os.path.join(d, "count")
n = int(open(cp).read()) if os.path.exists(cp) else 0
open(cp, "w").write(str(n + 1))
what = plan[min(n, len(plan) - 1)]
hdr = (b"RIFF\xff\xff\xff\xffWAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 2, 44100, 176400, 4, 16)
       + b"data\xff\xff\xff\xff")
out = sys.stdout.buffer
if what == "crash":
    out.write(hdr); out.flush()
    sys.stderr.write({crash!r})
    sys.exit(1)
if what == "crash0":
    out.write(hdr); out.flush()
    sys.stderr.write("uade warning: Song ended prematurely due to error: score crashed\n")
    sys.exit(0)
if what == "died":
    sys.stderr.write("uade warning: score died\nCan not play x\n")
    sys.exit(1)
if what == "kill":
    os.kill(os.getpid(), 9)
if what == "sleep":
    time.sleep(30)
    sys.exit(0)
if what == "ioerr":
    sys.stderr.write("Can not play x: Input/output error\n")
    sys.exit(1)
out.write(hdr)
buf = bytearray()
for i in range(44100 * 2):
    v = int(12000 * math.sin(2 * math.pi * 440 * i / 44100))
    buf += struct.pack("<hh", v, v)
out.write(bytes(buf))
'''


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    """Each test renders into an empty conversion cache (its in-memory
    index is the module's; the files are in each test's own folder)."""
    from collections import OrderedDict
    from soniqboom.core import conversion_cache as cc
    monkeypatch.setattr(cc, "_meta", {})
    monkeypatch.setattr(cc, "_lru", OrderedDict())
    monkeypatch.setattr(cc, "_total_bytes", 0)
    monkeypatch.setattr(cc, "_recent_failures", OrderedDict())


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(skip_rules, "_STALE_OK_S", 0.0)
    monkeypatch.setattr(skip_rules, "_BACKGROUND", False)
    availability.reset_for_tests()
    skip_rules.reset_for_tests()
    unplayable.reset_for_tests()
    yield s
    availability.reset_for_tests()
    skip_rules.reset_for_tests()
    unplayable.reset_for_tests()


@pytest.fixture
def uade(tmp_path, monkeypatch):
    """A stand-in uade123: ``uade.plan(...)`` says what each run does
    ("crash", "died", "kill", "sleep", "ioerr", "ok" — the last one repeats);
    ``uade.runs()`` how many renders ran."""
    from soniqboom.config import settings
    d = tmp_path / "bin"
    d.mkdir()
    (d / "uade123.py").write_text(STUB.replace("{crash!r}", repr(UADE_CRASH)))
    exe = d / "uade123"
    # (a shell launcher: the interpreter's path may hold a space, which a
    # ``#!`` line can't)
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/uade123.py" "$@"\n')
    exe.chmod(0o755)
    monkeypatch.setattr(settings, "uade123_path", str(exe))

    def plan(*steps):
        (d / "plan").write_text(" ".join(steps))
        (d / "count").unlink(missing_ok=True)

    def runs():
        p = d / "count"
        return int(p.read_text()) if p.exists() else 0

    return types.SimpleNamespace(plan=plan, runs=runs,
                                 version=lambda v: (d / "version").write_text(v), dir=d)


def _module(tmp_path, name="seven cities of gold.cus", data=b"\x00\x00\x03\xf3" + bytes(15868)):
    f = tmp_path / "music" / name
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(data)
    return f


def _track(store, f, tid="t1", **kw):
    d = {"id": tid, "title": "seven cities of gold", "artist": "", "album": "", "genre": [],
         "duration": 0.0, "format": "Amiga Custom", "path": str(f), "added_at": 1,
         "file_size": f.stat().st_size}
    d.update(kw)
    store.upsert_track(d)
    return d


async def _play(tid, f, *, bare=True, remote=False, subsong=0):
    """A play of ``tid``: the uade render through the conversion cache, as
    ``stream_track`` runs it (its play context set first)."""
    from soniqboom.api import stream
    from soniqboom.core.conversion_cache import get_or_render
    unplayable.set_play(tid, f, bare=bare, remote=remote)
    return await get_or_render(track_id=tid, format_type="uade", subsong=subsong,
                               render_fn=lambda: stream._render_uade(f, subsong))


# ── classification ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind, rc, err, kw, want", [
    # what marks: the renderer names the file's fault …
    ("uade", 1, UADE_CRASH, {}, ("uade123", "stops on this file: score crashed", True)),
    ("uade", 0, UADE_CRASH, {}, ("uade123", "stops on this file: score crashed", True)),
    ("uade", 0, UADE_CRASH, {"audio": True, "tiny": True},
     ("uade123", "stops on this file: score crashed", True)),
    ("uade", 1, "Can not play garbage.mod\n", {}, ("uade123", "refuses this file", False)),
    ("uade", 1, "uade warning: score died\nCan not play x\n", {},
     ("uade123", "stops on this file: score died", False)),
    ("SID", 1, "Warning, non UTF-8 encoding.\nsidplayfp: Compute!'s Sidplayer MUS data is not supported yet\n",
     {}, ("sidplayfp", "refuses this file: Compute!'s Sidplayer MUS data is not supported yet", True)),
    ("SNDH", 1, "psgplay: error: bad.sndh: invalid SNDH header\n", {},
     ("psgplay", "refuses this file: invalid SNDH header", True)),
    ("adlib", 0, "adplay: unknown filetype -- bad.d00\n", {}, ("adplay", "doesn't recognise this file", True)),
    # … or fails without a word (marked only once the pipeline is proven)
    ("tracker", 0, "", {}, ("openmpt123", "produces no audio from this file", False)),
    ("tracker", -11, "", {}, ("openmpt123", "crashes on this file (SIGSEGV)", False)),
    # what never marks
    ("uade", 0, UADE_CRASH, {"audio": True}, None),              # it played (in part)
    ("uade", -9, UADE_CRASH, {}, None),                          # we killed it
    ("uade", -15, "", {}, None),
    ("uade", -13, "", {}, None),                                 # broken pipe (listener left)
    ("uade", 1, "Unknown format: x.cus\n", {}, None),            # uade's answer to an unreadable file
    ("uade", 1, "Can not play x: Input/output error\n", {}, None),
    ("uade", 1, "uade123: unrecognized option `--ntsc'\n", {}, None),   # our bug, not the file
    ("SID", 1, "sidplayfp: SIDTUNE ERROR: Could not open file for binary input\n", {}, None),
    ("tracker", 1, "No space left on device\n", {}, None),
    ("tracker", 1, "dyld: Symbol not found: _foo\n", {}, None),
    ("MED", 127, "", {}, None),                                   # zxtune that can't start
    ("uade", 1, "uadecore: Can not load score (x).\nCan not play x\n", {}, None),  # broken install
    ("uade", 1, "load: file not found: smpl.x\nuade warning: score died\n", {}, None),
    ("GME-ffmpeg", 1, "Invalid data found when processing input", {}, None),  # ffmpeg, not libgme
    ("Transcode", 1, "Invalid data found when processing input", {}, None),
])
def test_classify(kind, rc, err, kw, want):
    assert unplayable.classify(kind, rc, err, **kw) == want


def test_the_uade_failure_of_the_report_is_a_refusal_with_its_old_answer():
    from soniqboom.api import stream
    e = stream._renderer_failure("uade", "uade123", 1, UADE_CRASH)
    assert isinstance(e, unplayable.RenderRefusal)
    assert (e.status_code, e.detail) == (502, "uade renderer exited with status 1")
    assert e.signature == ("uade123", "stops on this file: score crashed") and not e.verified
    plain = stream._renderer_failure("uade", "uade123", -9, UADE_CRASH)
    assert type(plain) is HTTPException


# ── verification and the mark ───────────────────────────────────────────────

async def test_a_crash_seen_twice_marks_the_track(tmp_path, store, uade):
    f = _module(tmp_path)
    _track(store, f)
    uade.plan("crash")
    with pytest.raises(unplayable.RenderRefusal) as exc:
        await _play("t1", f)
    e = exc.value
    assert uade.runs() == 2                              # the play + one verification
    assert e.verified and e.status_code == 422 and e.headers == {"X-SB-Defect": "unplayable"}
    assert e.detail == ("“seven cities of gold” can’t be played here — the Amiga player (uade) "
                        "stops on this file: score crashed.")
    t = store.get_track("t1")
    assert t["defect"] == "unplayable"
    assert t["defect_detail"] == "The Amiga player (uade) stops on this file: score crashed"
    assert t["defect_renderer"] == "uade123 3.05" and t["defect_at"] > 0
    ev = t["defect_evidence"]
    assert ev["renderer"] == "uade123" and ev["size"] == f.stat().st_size and ev["md5"]
    assert "uade123 3.05" in ev["fp"]
    # passed by when a list plays through (shuffles, radio, Subsonic's lists)
    assert "t1" in skip_rules.skip_ids(store)
    assert skip_rules.entry_skipped("t1", 0, store)
    # what a client reads: the public fields, never the evidence
    from soniqboom.api.tracks import public_track
    row = public_track(store.get_track("t1"))
    assert row["defect"] == "unplayable" and "defect_evidence" not in row
    # a click renders it again — one attempt, answered from the mark
    with pytest.raises(unplayable.RenderRefusal) as again:
        await _play("t1", f)
    assert again.value.verified and uade.runs() == 3


async def test_a_one_off_failure_never_marks(tmp_path, store, uade):
    f = _module(tmp_path)
    _track(store, f)
    uade.plan("crash", "ok")
    path, hit = await _play("t1", f)
    assert not hit and path.exists() and uade.runs() == 2
    assert store.get_track("t1").get("defect") is None
    assert "t1" not in skip_rules.skip_ids(store)


async def test_two_different_failures_never_mark(tmp_path, store, uade):
    f = _module(tmp_path)
    _track(store, f)
    uade.plan("crash", "died")
    with pytest.raises(unplayable.RenderRefusal) as exc:
        await _play("t1", f)
    assert not exc.value.verified and exc.value.why == "stops on this file: score died"
    assert store.get_track("t1").get("defect") is None


@pytest.mark.parametrize("step", ["kill", "ioerr"])
async def test_a_killed_renderer_or_an_io_error_never_marks(tmp_path, store, uade, step):
    f = _module(tmp_path)
    _track(store, f)
    uade.plan(step)
    with pytest.raises(HTTPException) as exc:
        await _play("t1", f)
    assert not isinstance(exc.value, unplayable.RenderRefusal)
    assert uade.runs() == 1                              # nothing to verify
    assert store.get_track("t1").get("defect") is None


async def test_a_timeout_never_marks(tmp_path, store, uade, monkeypatch):
    from soniqboom.api import stream
    monkeypatch.setattr(stream, "_UADE_DEFAULT_TIMEOUT_S", 0.5)
    f = _module(tmp_path)
    _track(store, f)
    uade.plan("sleep")
    with pytest.raises(HTTPException) as exc:
        await _play("t1", f)
    assert exc.value.status_code == 504 and uade.runs() == 1
    assert store.get_track("t1").get("defect") is None


async def test_a_render_stopped_mid_way_never_marks(tmp_path, store, uade):
    """A skip / a listener who left / the render-slot stop: the render task
    is cancelled — the renderer is killed, nothing is marked."""
    from soniqboom.api import stream
    f = _module(tmp_path)
    _track(store, f)
    uade.plan("sleep")
    unplayable.set_play("t1", f, bare=True, remote=False)
    task = asyncio.ensure_future(unplayable.render_checked(lambda: stream._render_uade(f, 0), "uade"))
    for _ in range(200):
        if uade.runs():
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert uade.runs() == 1
    assert store.get_track("t1").get("defect") is None


async def test_a_copy_that_is_not_the_listed_file_never_marks(tmp_path, store, uade):
    """A local file of another size than the library knows is not the file
    to blame; a fetched copy is checked against it only loosely (an online
    collection's index rounds sizes; the remote cache refuses a transfer
    shorter than its server states)."""
    f = _module(tmp_path)
    n = f.stat().st_size
    _track(store, f, file_size=n + 512)                      # local: exactly
    uade.plan("crash")
    with pytest.raises(unplayable.RenderRefusal) as exc:
        await _play("t1", f)
    assert not exc.value.verified and uade.runs() == 2
    assert store.get_track("t1").get("defect") is None
    _track(store, f, file_size=n * 2)                        # fetched: far off
    uade.plan("crash")
    with pytest.raises(unplayable.RenderRefusal) as exc:
        await _play("t1", f, remote=True)
    assert not exc.value.verified and store.get_track("t1").get("defect") is None
    _track(store, f, file_size=n + 1000)                     # fetched: an index's rounding
    uade.plan("crash")
    with pytest.raises(unplayable.RenderRefusal) as ok:
        await _play("t1", f, remote=True)
    assert ok.value.verified and store.get_track("t1")["defect"] == "unplayable"


async def test_a_full_disk_never_marks(tmp_path, store, uade, monkeypatch):
    monkeypatch.setattr(unplayable, "_disk_ok", lambda: False)
    f = _module(tmp_path)
    _track(store, f)
    uade.plan("crash")
    with pytest.raises(unplayable.RenderRefusal) as exc:
        await _play("t1", f)
    assert not exc.value.verified
    assert store.get_track("t1").get("defect") is None


async def test_another_tune_than_the_default_never_marks(tmp_path, store, uade):
    f = _module(tmp_path)
    _track(store, f, subsongs=4)
    uade.plan("crash")
    with pytest.raises(unplayable.RenderRefusal) as exc:
        await _play("t1", f, bare=False, subsong=3)
    assert not exc.value.verified and uade.runs() == 1
    assert store.get_track("t1").get("defect") is None


async def test_a_silent_failure_marks_only_once_the_pipeline_played_something(store, tmp_path):
    f = _module(tmp_path, "song.mod", b"M.K." * 600)
    _track(store, f, format="MOD")
    calls = []

    async def render():
        calls.append(1)
        raise unplayable.no_audio("tracker")

    unplayable.set_play("t1", f, bare=True, remote=False)
    with pytest.raises(unplayable.RenderRefusal) as exc:
        await unplayable.render_checked(render, "tracker")
    assert not exc.value.verified and len(calls) == 1      # (not even re-run)
    assert store.get_track("t1").get("defect") is None
    unplayable.rendered_ok("tracker")                        # another module played
    unplayable.set_play("t1", f, bare=True, remote=False)
    with pytest.raises(unplayable.RenderRefusal) as exc:
        await unplayable.render_checked(render, "tracker")
    assert exc.value.verified and len(calls) == 3
    assert store.get_track("t1")["defect_detail"] == (
        "The module player (libopenmpt) produces no audio from this file")


async def test_a_render_that_plays_clears_the_mark_and_brings_back_the_old_defect(
        tmp_path, store, uade):
    f = _module(tmp_path)
    _track(store, f, defect="partial", defect_detail="2 instruments missing")
    uade.plan("crash")
    with pytest.raises(unplayable.RenderRefusal):
        await _play("t1", f)
    assert store.get_track("t1")["defect"] == "unplayable"
    uade.plan("ok")                                       # (say, a fixed uade)
    await _play("t1", f)
    t = store.get_track("t1")
    assert (t["defect"], t["defect_detail"]) == ("partial", "2 instruments missing")
    assert t.get("defect_evidence") is None and t.get("defect_renderer") is None
    assert "t1" not in skip_rules.skip_ids(store)


async def test_a_user_report_is_kept_beside_the_mark(tmp_path, store, uade):
    f = _module(tmp_path)
    _track(store, f, health="glitched", health_note="wrong drums", health_by="Ed")
    uade.plan("crash")
    with pytest.raises(unplayable.RenderRefusal):
        await _play("t1", f)
    t = store.get_track("t1")
    assert t["defect"] == "unplayable"
    assert (t["health"], t["health_note"], t["health_by"]) == ("glitched", "wrong drums", "Ed")
    uade.plan("ok")
    await _play("t1", f)
    t = store.get_track("t1")
    assert t.get("defect") is None and t["health"] == "glitched"


async def test_a_new_renderer_version_retries_the_mark(tmp_path, store, uade):
    f = _module(tmp_path)
    _track(store, f)
    uade.plan("crash")
    with pytest.raises(unplayable.RenderRefusal):
        await _play("t1", f)
    assert await unplayable.sweep_stale(store) == 0       # same uade: kept
    assert store.get_track("t1")["defect"] == "unplayable"
    uade.version("3.06")                                   # an upgrade
    assert await unplayable.sweep_stale(store) == 1
    assert store.get_track("t1").get("defect") is None
    assert "t1" not in skip_rules.skip_ids(store)
    # and a play under the new version verifies (and marks) afresh
    uade.plan("crash")
    with pytest.raises(unplayable.RenderRefusal) as exc:
        await _play("t1", f)
    assert exc.value.verified and uade.runs() == 2
    assert store.get_track("t1")["defect_renderer"] == "uade123 3.06"


async def test_a_soniqboom_release_retries_the_mark(tmp_path, store, uade, monkeypatch):
    f = _module(tmp_path)
    _track(store, f)
    uade.plan("crash")
    with pytest.raises(unplayable.RenderRefusal):
        await _play("t1", f)
    import soniqboom
    monkeypatch.setattr(soniqboom, "__version__", soniqboom.__version__ + ".1")
    assert await unplayable.sweep_stale(store) == 1


# ── the mark across rescans ──────────────────────────────────────────────────

def _marked(**kw):
    d = {"id": "t1", "path": "/m/x.cus", "file_size": 100, "file_md5": "aa",
         "defect": "unplayable", "defect_detail": "The Amiga player (uade) stops on this file: x",
         "defect_renderer": "uade123 3.05", "defect_at": 1.0,
         "defect_evidence": {"renderer": "uade123", "fmt": "uade", "fp": "f", "why": "x",
                             "size": 100, "md5": "aa", "prev": None}}
    d.update(kw)
    return d


def test_a_rescan_of_the_same_file_keeps_the_mark_a_changed_one_drops_it():
    new = {"id": "t1", "path": "/m/x.cus", "file_size": 100, "file_md5": "aa"}
    _carry_enrichment(_marked(), new)
    assert new["defect"] == "unplayable" and new["defect_evidence"]["md5"] == "aa"
    changed = {"id": "t1", "path": "/m/x.cus", "file_size": 100, "file_md5": "bb"}
    _carry_enrichment(_marked(), changed)
    assert changed.get("defect") is None
    # an online collection's index row (no checksum): by its size
    row = {"id": "t1", "path": "/m/x.cus", "file_size": 100}
    _carry_enrichment(_marked(), row)
    assert row["defect"] == "unplayable"
    resized = {"id": "t1", "path": "/m/x.cus", "file_size": 101}
    _carry_enrichment(_marked(), resized)
    assert resized.get("defect") is None
    # a defect the fresh read found itself wins
    other = {"id": "t1", "path": "/m/x.cus", "file_size": 100, "file_md5": "aa", "defect": "corrupt"}
    _carry_enrichment(_marked(), other)
    assert other["defect"] == "corrupt"


def test_skip_rules_pass_a_marked_track_by(store):
    store.upsert_track(_marked())
    store.upsert_track({"id": "t2", "path": "/m/y.cus", "title": "y"})
    assert skip_rules.skip_ids(store) == {"t1"}
    assert skip_rules.file_skipped(store.get_track("t1"), 0)


# ── what clients get ─────────────────────────────────────────────────────────

def test_subsonic_gets_the_reason_for_a_marked_track():
    from soniqboom.api import subsonic
    e = unplayable.RenderRefusal(422, "“x” can’t be played here — the Amiga player (uade) stops "
                                      "on this file: score crashed.", renderer="uade123",
                                 why="stops on this file: score crashed", diagnosed=True,
                                 headers={"X-SB-Defect": "unplayable"}, verified=True)
    err = subsonic._stream_http_error(e)
    assert err.message == e.detail and err.http_status == 422
    plain = subsonic._stream_http_error(HTTPException(502, "uade renderer exited with status 1"))
    assert plain.message == "The server could not prepare this song."


async def test_render_status_names_the_mark(tmp_path, store, uade, monkeypatch):
    from soniqboom.api import stream
    f = _module(tmp_path)
    _track(store, f)
    uade.plan("crash")
    with pytest.raises(unplayable.RenderRefusal):
        await _play("t1", f)

    async def _ok(*a, **k):
        return None
    monkeypatch.setattr(stream, "_require_stream_auth", _ok)
    req = types.SimpleNamespace(query_params={}, headers={}, client=None)
    out = await stream.render_status("t1", subsong=None, sb_session=None, u=None, p=None,
                                     s=None, t=None, request=req)
    assert out["state"] == "failed" and out["error_status"] == 422
    assert out["error"].startswith("“seven cities of gold” can’t be played here")
    assert out["defect"] == "unplayable" and out["defect_renderer"] == "uade123 3.05"


async def test_a_marked_track_is_not_prewarmed(tmp_path, store, uade, monkeypatch):
    from soniqboom.api import stream
    f = _module(tmp_path)
    _track(store, f, defect="unplayable", defect_detail="x")

    async def _ok(*a, **k):
        return None
    monkeypatch.setattr(stream, "_require_stream_auth", _ok)
    req = types.SimpleNamespace(query_params={}, headers={}, client=None, cookies={})
    out = await stream.prewarm("t1", subsong=None, priority="next", file_path=None, pw=None,
                               hover=False, sb_session=None, u=None, p=None, s=None, t=None,
                               request=req)
    assert out == {"status": "skipped", "reason": "renderer refused it"}
    assert uade.runs() == 0


# ── the real uade ────────────────────────────────────────────────────────────

def _crashing_custom_module() -> bytes:
    """A DeliTracker custom module (an Amiga hunk executable with the
    DELIRIUM tag list) whose InitPlayer is the 68000 ILLEGAL instruction —
    made here, for this test."""
    def L(*xs):
        return b"".join(struct.pack(">I", x) for x in xs)
    code = bytearray(bytes.fromhex("70ff4e75") + b"DELIRIUM" + L(0x14) + bytes.fromhex("4afc4e75"))
    rts = 0x14 + 7 * 8
    tags = [(0x80004455, 1), (0x80004459, rts), (0x8000445E, 0x10), (0x80004463, rts),
            (0x80004464, rts), (0x80004465, rts), (0, 0)]
    for t, v in tags:
        code += L(t, v)
    code += bytes.fromhex("4e750000")
    n = len(code) // 4
    relocs = [0x0C] + [0x14 + 8 * i + 4 for i in range(1, 6)]
    return (L(0x3F3, 0, 1, 0, 0, 0x40000000 | n, 0x3E9, n) + bytes(code)
            + L(0x3EC, len(relocs), 0, *relocs, 0) + L(0x3F2))


@pytest.mark.skipif(not shutil.which("uade123"), reason="uade123 not installed")
async def test_the_real_uade_on_a_module_whose_player_crashes(tmp_path, store, monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "uade123_path", shutil.which("uade123"))
    f = _module(tmp_path, "crash.cus", _crashing_custom_module())
    _track(store, f, title="crash")
    with pytest.raises(unplayable.RenderRefusal) as exc:
        await _play("t1", f)
    assert exc.value.verified and exc.value.why == "stops on this file: score crashed"
    t = store.get_track("t1")
    assert t["defect"] == "unplayable" and t["defect_renderer"].startswith("uade123 ")


def test_a_first_play_merge_that_lands_after_the_mark_keeps_it(store):
    """An online collection's first-play read (``modland_catalog.merged_track``
    → ``upsert_tracks_batch``) can land after the mark: the read file's
    checksum is the one the mark was made on, so it stays; another file's
    doesn't keep it."""
    store.upsert_track({"id": "t1", "path": "ftp://ftp.modland.com/pub/modules:/x/y.cus",
                        "title": "y", "file_size": 100})
    store.update_track_fields("t1", {k: v for k, v in _marked().items() if k.startswith("defect")})
    store.upsert_tracks_batch([{"id": "t1", "path": "ftp://ftp.modland.com/pub/modules:/x/y.cus",
                                "title": "y", "file_size": 100, "file_md5": "aa", "mtime": 1.0}])
    assert store.get_track("t1")["defect"] == "unplayable"
    store.upsert_tracks_batch([{"id": "t1", "path": "ftp://ftp.modland.com/pub/modules:/x/y.cus",
                                "title": "y", "file_size": 100, "file_md5": "cc", "mtime": 2.0}])
    assert store.get_track("t1").get("defect") is None


# ── review follow-ups ────────────────────────────────────────────────────────

async def test_a_renderer_that_refuses_everything_is_taken_for_broken(tmp_path, store, uade):
    """``_BREAKER`` different files refused in a row with none of its renders
    playing in between: the marks are taken off again and nothing is marked
    until one of its renders plays."""
    files = []
    for i in range(unplayable._BREAKER + 1):
        f = _module(tmp_path, f"m{i}.cus")
        _track(store, f, tid=f"m{i}")
        files.append(f)
    uade.plan("crash")
    for i in range(unplayable._BREAKER - 1):
        with pytest.raises(unplayable.RenderRefusal) as exc:
            await _play(f"m{i}", files[i])
        assert exc.value.verified
    assert all(store.get_track(f"m{i}")["defect"] == "unplayable"
               for i in range(unplayable._BREAKER - 1))
    k = unplayable._BREAKER - 1
    with pytest.raises(unplayable.RenderRefusal) as exc:
        await _play(f"m{k}", files[k])
    assert not exc.value.verified
    assert all(store.get_track(f"m{i}").get("defect") is None for i in range(k + 1))
    with pytest.raises(unplayable.RenderRefusal) as exc:      # tripped: no mark
        await _play(f"m{k + 1}", files[k + 1])
    assert not exc.value.verified and store.get_track(f"m{k + 1}").get("defect") is None
    # one of its renders plays: it marks again
    good = _module(tmp_path, "good.cus")
    _track(store, good, tid="good")
    uade.plan("ok")
    await _play("good", good)
    uade.plan("crash")
    with pytest.raises(unplayable.RenderRefusal) as exc:
        await _play(f"m{k + 1}", files[k + 1])
    assert exc.value.verified


async def test_score_died_marks_only_once_uade_has_played(tmp_path, store, uade):
    f = _module(tmp_path)
    _track(store, f)
    uade.plan("died")
    with pytest.raises(unplayable.RenderRefusal) as exc:
        await _play("t1", f)
    assert not exc.value.verified and uade.runs() == 1
    unplayable.rendered_ok("uade")
    uade.plan("died")
    with pytest.raises(unplayable.RenderRefusal) as exc:
        await _play("t1", f)
    assert exc.value.verified


async def test_a_missing_companion_is_said_not_marked(tmp_path, store, uade):
    """TFMX ``mdat.x`` without its ``smpl.x``: uade's crash is the missing
    half — the 422 says so, the track isn't marked."""
    f = _module(tmp_path, "mdat.lonely")
    _track(store, f, title="lonely")
    for step in ("crash", "crash0"):
        uade.plan(step)
        with pytest.raises(HTTPException) as exc:
            await _play("t1", f)
        assert not isinstance(exc.value, unplayable.RenderRefusal), step
        assert exc.value.status_code == 422 and "companion" in exc.value.detail
    assert store.get_track("t1").get("defect") is None


async def test_a_play_waiting_for_the_render_gets_its_verified_answer(tmp_path, store, uade):
    from soniqboom.api import stream
    from soniqboom.core.conversion_cache import get_or_render
    f = _module(tmp_path)
    _track(store, f)
    uade.plan("crash")

    async def slow():
        await asyncio.sleep(0.3)
        return await stream._render_uade(f, 0)
    unplayable.set_play("t1", f, bare=True, remote=False)
    first = asyncio.ensure_future(get_or_render(track_id="t1", format_type="uade", subsong=0,
                                                render_fn=slow))
    await asyncio.sleep(0.05)
    second = asyncio.ensure_future(get_or_render(track_id="t1", format_type="uade", subsong=0,
                                                 render_fn=slow))
    for task in (first, second):
        with pytest.raises(unplayable.RenderRefusal) as exc:
            await task
        assert exc.value.verified and exc.value.status_code == 422
        assert exc.value.headers == {"X-SB-Defect": "unplayable"}
    assert uade.runs() == 2


async def test_render_status_names_only_the_verified_refusal(tmp_path, store, uade, monkeypatch):
    """Another failure of a marked track (a timeout, another tune) is said
    as itself — render-status carries the mark only with its refusal."""
    from soniqboom.api import stream
    from soniqboom.core import conversion_cache as cc
    f = _module(tmp_path)
    _track(store, f)
    uade.plan("crash")
    with pytest.raises(unplayable.RenderRefusal):
        await _play("t1", f)

    async def _ok(*a, **k):
        return None
    monkeypatch.setattr(stream, "_require_stream_auth", _ok)
    req = types.SimpleNamespace(query_params={}, headers={}, client=None)
    key = next(iter(cc._recent_failures))
    cc._note_failure_from(key, HTTPException(504, "uade render timed out after 8s"))
    out = await stream.render_status("t1", subsong=None, sb_session=None, u=None, p=None,
                                     s=None, t=None, request=req)
    assert out["state"] == "failed" and out["error_status"] == 504 and "defect" not in out


def _stream_app(monkeypatch):
    from fastapi import FastAPI
    from soniqboom.api import stream

    async def _ok(*a, **k):
        return None
    monkeypatch.setattr(stream, "_require_stream_auth", _ok)
    monkeypatch.setattr(stream, "_spawn_uade_vu", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(stream.router, prefix="/api")
    return app


async def test_the_stream_endpoint_marks_the_default_tune_only(tmp_path, store, uade, monkeypatch):
    """Through the real ``/api/stream/{id}`` route (its play context):
    a bare play marks, a pinned other tune never does."""
    import httpx
    app = _stream_app(monkeypatch)
    f = _module(tmp_path)
    _track(store, f, subsongs=3)
    uade.plan("crash")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/stream/t1?subsong=2", cookies={"sb_session": "x"})
        assert r.status_code == 502 and "x-sb-defect" not in r.headers
        assert store.get_track("t1").get("defect") is None
        r = await c.get("/api/stream/t1", cookies={"sb_session": "x"})
        assert r.status_code == 422 and r.headers["x-sb-defect"] == "unplayable"
        assert r.json()["detail"].startswith("“seven cities of gold” can’t be played here")
    assert store.get_track("t1")["defect"] == "unplayable"


async def test_a_prewarm_marks_through_its_own_play_context(tmp_path, store, uade, monkeypatch):
    import httpx
    from soniqboom.api import stream
    monkeypatch.setattr(stream, "_prewarm_enabled", lambda: True)
    app = _stream_app(monkeypatch)
    f = _module(tmp_path)
    _track(store, f)
    uade.plan("crash")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/stream/t1/prewarm?priority=next", cookies={"sb_session": "x"})
        assert r.status_code == 200, r.text
    for _ in range(100):
        if store.get_track("t1").get("defect"):
            break
        await asyncio.sleep(0.05)
    assert store.get_track("t1")["defect"] == "unplayable" and uade.runs() == 2
