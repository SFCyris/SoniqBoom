# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every tune playable on its own (opportunity review v2.2.0, feature 7).

* A tracker module with several libopenmpt subsongs lists each as a tune
  (``subsongs``, ``tune_lengths``); tune N renders subsong N alone — tune 0
  its first, where openmpt123 alone plays them all one after another.
* A GoatTracker song's subtunes and a FamiTracker module's tracks are tunes
  (keyed per tune on the PSF route: ``sub{N}``).
* HES / KSS files, which state no count, get a tune list by playing their
  song slots (on play / Track Info): the slots that sound, dense tune
  numbers, the slot handed to the renderer (``stream.gme_slot``).
* Records scanned before all this learn it from the file on play / Track
  Info (``stream.tune_list_backfill``).
* The tune wire mapping (``stream.tune_index`` / ``sid_wire_tune``, Subsonic
  ``wire_tune``, utils.js ``subsongWireToTune`` — the JS side in
  tests/js/every_tune.test.mjs) is the identity for these families.

Real renderers where installed (openmpt123, gt2reloc + sidplayfp, Furnace,
kssrender, libgme); the files are internal/testdata's, plus a KSS file built
here from a few bytes of our own Z80 code (no multi-tune HES or KSS file is
in the test data)."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import struct
import types
import wave
from pathlib import Path

import pytest

from soniqboom.api import stream
from soniqboom.core import metadata as md
from soniqboom.core import store as S
from soniqboom.core import subsonic_index as sx

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "internal/testdata"
MULTI_MOD = DATA / "nostalgicplayer/chiptracker/premiere - elev.1.mod"           # 2 subsongs
MULTI_DMF = DATA / "nostalgicplayer/softwarevisions/Catalog Software Visions 1995.dmf"
ONE_MOD = DATA / "tracker/actionplatform.mod"
DOJO = DATA / "goattracker/dojo.sng"                                              # 4 subtunes
ONE_GOAT = DATA / "goattracker/sanction.sng"
FTM = DATA / "famitracker/short_boss_theme.ftm"
OPENMPT = shutil.which("openmpt123")
GT2RELOC, SIDPLAYFP = shutil.which("gt2reloc"), shutil.which("sidplayfp")
FURNACE = shutil.which("furnace-headless") or shutil.which("furnace")
KSSRENDER = shutil.which("kssrender")


def _libopenmpt() -> bool:
    from soniqboom.core import openmpt_vu
    return openmpt_vu.is_available()


needs_modules = pytest.mark.skipif(not (MULTI_MOD.is_file() and ONE_MOD.is_file()
                                        and _libopenmpt()),
                                   reason="libopenmpt / internal/testdata modules absent")


def _wav_seconds(p: Path) -> float:
    return stream._wav_audio_seconds(p)


# ── Tracker modules ─────────────────────────────────────────────────────────

@needs_modules
def test_a_multi_subsong_module_lists_each_subsong_as_a_tune():
    from soniqboom.core import openmpt_vu
    data = MULTI_MOD.read_bytes()
    assert openmpt_vu.subsong_lengths(data) == [38.4, 2.64]
    assert openmpt_vu.subsong_lengths(ONE_MOD.read_bytes()) is None
    assert openmpt_vu.plan(data, 0)[::2] == (pytest.approx(38.4), 2)
    assert openmpt_vu.plan(data, 1)[::2] == (pytest.approx(2.64), 2)
    # one subsong: tune 0 plays as openmpt123 does (subsong -1), unchanged
    assert openmpt_vu.plan(ONE_MOD.read_bytes(), 0)[::2] == (pytest.approx(96.0), 1)


@needs_modules
def test_the_scan_records_the_tunes_and_the_first_tunes_length(monkeypatch):
    monkeypatch.setattr(md, "_LIBOPENMPT_IN_PROCESS", True)
    d = md._extract_tracker(MULTI_MOD, "x")
    assert (d["subsongs"], d["tune_lengths"], d["duration"]) == (2, [38.4, 2.64], 38.4)
    one = md._extract_tracker(ONE_MOD, "y")
    assert "subsongs" not in one and "tune_lengths" not in one and abs(one["duration"] - 96.0) < 0.01


@pytest.mark.skipif(not (OPENMPT and MULTI_MOD.is_file()), reason="openmpt123 absent")
def test_the_scan_in_the_servers_own_threads_reads_them_with_openmpt123(monkeypatch):
    """Without libopenmpt in-process (a folder click's refresh in the server)
    the CLI answers: ``--info --subsong N`` per subsong."""
    monkeypatch.setattr(md, "_LIBOPENMPT_IN_PROCESS", False)
    d = md._extract_tracker(MULTI_MOD, "x")
    assert (d["subsongs"], d["tune_lengths"], d["duration"]) == (2, [38.4, 2.64], 38.4)


@pytest.mark.skipif(not (OPENMPT and MULTI_MOD.is_file() and _libopenmpt()),
                    reason="openmpt123 / libopenmpt absent")
@pytest.mark.asyncio
async def test_each_subsong_renders_alone_and_a_one_subsong_module_is_unchanged():
    """Cold renders of three different inputs: tune 0 and tune 1 of the
    two-subsong module are each only that subsong (+ libopenmpt's 0.1 s end
    fade), not 41.04 s of both; the one-subsong module still plays its whole
    96 s; another two-subsong module (a DMF) likewise."""
    outs = []
    try:
        for path, tune, want in ((MULTI_MOD, 0, 38.5), (MULTI_MOD, 1, 2.74), (ONE_MOD, 0, 96.1),
                                 (MULTI_DMF, 1, 14.68)):
            if not path.is_file():
                continue
            out = await stream._render_tracker(path, subsong=tune)
            outs.append(out)
            assert abs(_wav_seconds(out) - want) < 0.1, (path.name, tune, _wav_seconds(out))
    finally:
        for o in outs:
            Path(o).unlink(missing_ok=True)
            Path(o).with_suffix(".vu").unlink(missing_ok=True)


def test_tracker_renders_made_before_are_swept_once():
    """Tune 0 of a module with several subsongs held all of them: every older
    tracker render is left (``_RENDER_REVS`` 3, swept at start)."""
    from soniqboom.core import conversion_cache as cc
    assert cc._cache_key("t", "tracker", 0) == "t__sub0__r3__amauto"   # (+ the Amiga resampler default)
    assert "tracker3" in cc._render_rev_marker()


# ── Records scanned before: learnt on play / Track Info ────────────────────

def _rec(tid, path, fmt, **kw):
    d = {"id": tid, "path": str(path), "title": tid, "format": fmt, "duration": 41.04,
         "mtime": 1, "file_size": os.path.getsize(path) if Path(path).is_file() else 1,
         "added_at": 1}
    d.update(kw)
    return d


@needs_modules
@pytest.mark.asyncio
async def test_an_old_module_record_learns_its_tunes_from_the_file(monkeypatch):
    monkeypatch.setattr(S, "_store", S.TrackStore())
    st = S.get_store()
    st.upsert_tracks_batch([_rec("m2", MULTI_MOD, "ProTracker"),
                            _rec("m1", ONE_MOD, "ProTracker", duration=96.0)])
    stream._TUNE_LIST_READ.clear()
    got = await stream.tune_list_backfill("m2", st.get_track("m2"), MULTI_MOD)
    rec = st.get_track("m2")
    assert (rec["subsongs"], rec["tune_lengths"], rec["duration"]) == (2, [38.4, 2.64], 38.4)
    assert got["subsongs"] == 2
    seq = st._mutation_seq
    await stream.tune_list_backfill("m1", st.get_track("m1"), ONE_MOD)
    assert st.get_track("m1").get("subsongs") is None and st._mutation_seq == seq
    assert ("m1", 1, os.path.getsize(ONE_MOD), "tracker") in stream._TUNE_LIST_READ


# ── GoatTracker / FamiTracker ───────────────────────────────────────────────

@pytest.mark.skipif(not DOJO.is_file(), reason="internal/testdata/goattracker absent")
def test_goattracker_and_famitracker_scans_count_their_tunes():
    assert md._extract_goattracker(DOJO, "g")["subsongs"] == 4
    assert "subsongs" not in md._extract_goattracker(ONE_GOAT, "g1")
    if FTM.is_file():
        assert "subsongs" not in md._extract_famitracker(FTM, "f")   # one track
        # (a module whose HEADER block counts 3 tracks — the count byte only)
        raw = bytearray(FTM.read_bytes())
        pos = 22
        while pos + 24 <= len(raw):
            bid = bytes(raw[pos:pos + 16]).split(b"\0", 1)[0]
            ver, size = struct.unpack("<II", raw[pos + 16:pos + 24])
            if bid == b"HEADER":
                raw[pos + 24] = 2
                break
            pos += 24 + size
        assert md.famitracker_header(bytes(raw))["tracks"] == 3


def test_goattracker_and_famitracker_tunes_are_keyed_per_tune():
    from soniqboom.core import conversion_cache as cc
    multi = {"format": "GoatTracker", "subsongs": 4}
    one = {"format": "GoatTracker"}
    assert stream._psf_tuned(".goattracker", multi) and stream._psf_tuned(".famitracker",
                                                                          {"subsongs": 2})
    assert not stream._psf_tuned(".goattracker", one) and not stream._psf_tuned(".mdx", multi)
    k0 = stream.chip_family_key("psf", "g", ".goattracker", 0, "gt180", track=multi)
    k2 = stream.chip_family_key("psf", "g", ".goattracker", 2, "gt180", track=multi)
    assert (k0, k2) == ("g__sub0__r1__vgt180", "g__sub2__r1__vgt180")
    # a one-tune song keeps its key (no re-render)
    assert stream.chip_family_key("psf", "g", ".goattracker", 0, "gt180", track=one) == \
        "g__r1__vgt180"
    assert cc.key_matches(k2, "g", 2) and not cc.key_matches(k2, "g", 0)
    assert not cc.key_matches(k0, "g", 2)


@pytest.mark.skipif(not (GT2RELOC and SIDPLAYFP and DOJO.is_file()),
                    reason="gt2reloc / sidplayfp / goattracker test data absent")
@pytest.mark.asyncio
async def test_each_goattracker_subtune_renders_its_own_music(monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 12)
    monkeypatch.setattr(settings, "gt2reloc_path", GT2RELOC)
    monkeypatch.setattr(settings, "sidplayfp_path", SIDPLAYFP)
    digests = []
    for tune in (0, 1, 3):
        out = await stream._render_goattracker(DOJO, subsong=tune)
        try:
            with wave.open(str(out)) as w:
                frames = w.readframes(w.getframerate() * 8)
            digests.append(hashlib.md5(frames).hexdigest())
            assert max(abs(v) for v in struct.unpack(f"<{len(frames) // 2}h", frames)) > 500
        finally:
            Path(out).unlink(missing_ok=True)
    assert len(set(digests)) == 3, "every subtune is other music"


@pytest.mark.asyncio
async def test_famitracker_plays_the_track_asked_for(monkeypatch, tmp_path):
    """Furnace is handed ``-subsong N`` for tune N > 0 (none for tune 0)."""
    seen = []

    async def fake_tail(cmd, tmp, live_key, **kw):
        seen.append(cmd)
        return tmp
    monkeypatch.setattr(stream, "_tail_render", fake_tail)
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "/x/furnace")
    for tune in (0, 2):
        out = await stream._render_famitracker(tmp_path / "a.ftm", subsong=tune)
        Path(out).unlink(missing_ok=True)
    assert "-subsong" not in seen[0]
    assert seen[1][seen[1].index("-subsong") + 1] == "2"


@pytest.mark.skipif(not (FURNACE and FTM.is_file()), reason="Furnace / famitracker data absent")
@pytest.mark.asyncio
async def test_famitracker_tune_0_still_renders(monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "furnace_path", FURNACE)
    out = await stream._render_famitracker(FTM, subsong=0)
    try:
        assert abs(_wav_seconds(out) - 21.9) < 0.5
    finally:
        Path(out).unlink(missing_ok=True)


# ── HES / KSS: slots that sound ─────────────────────────────────────────────

def _kss(audible=(0, 2, 5), limits=None) -> bytes:
    """A KSS (KSCC) file of our own Z80 code: song slots in ``audible`` play a
    steady PSG tone — for ``limits[slot]`` frames only (60 a second), when
    given: a sound effect — and every other slot stays silent."""
    def jr(at: int, to: int) -> int:
        return (to - (at + 2)) & 0xFF
    init = [0x32, 0x00, 0xC0, 0xAF, 0x32, 0x01, 0xC0, 0xC9]        # song → C000, frames = 0
    play = [0x3A, 0x00, 0xC0, 0x5F, 0x16, 0x00,                     # DE = song
            0x21, 0x01, 0xC0, 0x7E, 0x3C, 0x28, 0x01, 0x77,         # frames += 1 (to 255)
            0x21, 0x00, 0x42, 0x19, 0x7E, 0xB7]                     # limit = 4200[song]
    base = 0x4000 + len(init)
    jz_at = len(play)
    play += [0x28, 0x00, 0x47, 0x3A, 0x01, 0xC0, 0xB8]              # JR Z tone; frames ≥ limit?
    jnc_at = len(play)
    play += [0x30, 0x00]                                            # JR NC silence
    tone = len(play)
    play += [0x21, 0x00, 0x41, 0x19, 0x46, 0x78, 0xB7, 0xC8,        # B = 4100[song] or RET
             0x3E, 0x00, 0xD3, 0xA0, 0x78, 0xD3, 0xA1, 0x3E, 0x01, 0xD3, 0xA0, 0x3E, 0x01,
             0xD3, 0xA1, 0x3E, 0x07, 0xD3, 0xA0, 0x3E, 0x3E, 0xD3, 0xA1, 0x3E, 0x08, 0xD3,
             0xA0, 0x3E, 0x0C, 0xD3, 0xA1, 0xC9]                    # PSG tone A at volume 12
    silence = len(play)
    play += [0x3E, 0x08, 0xD3, 0xA0, 0xAF, 0xD3, 0xA1, 0xC9]        # volume A = 0
    play[jz_at + 1] = jr(jz_at, tone)
    play[jnc_at + 1] = jr(jnc_at, silence)
    code = bytes(init + play).ljust(0x100, b"\0")
    tones, lims = bytearray(256), bytearray(256)
    for i, slot in enumerate(audible):
        tones[slot] = 0x40 + 0x30 * i
    for slot, frames in (limits or {}).items():
        lims[slot] = frames
    body = code + bytes(tones) + bytes(lims)
    return b"KSCC" + struct.pack("<HHHH", 0x4000, len(body), 0x4000, base) + bytes(4) + body


def _libgme() -> bool:
    from soniqboom.core import gme_render
    return gme_render.is_available()


@pytest.mark.skipif(not _libgme(), reason="libgme absent")
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["libgme", "kssrender"])
async def test_a_kss_files_tune_list_is_the_slots_that_sound(engine, tmp_path, monkeypatch):
    """Cold probe of two different files per engine: slots {0, 2, 5} and
    {3} — a tune list of 3 (slots kept, dense tune numbers, default tune 0)
    and of one tune at slot 3 (no count, the slot kept)."""
    if engine == "kssrender" and not KSSRENDER:
        pytest.skip("kssrender absent")
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 20)
    monkeypatch.setattr(settings, "kssrender_path", KSSRENDER if engine == "kssrender" else "")
    if engine == "libgme":
        monkeypatch.setattr(stream, "_find_renderer",
                            lambda conf, name: None if name == "kssrender" else shutil.which(name))
    a, b = tmp_path / "a.kss", tmp_path / "b.kss"
    a.write_bytes(_kss((0, 2, 5)))
    b.write_bytes(_kss((3,)))
    fa = await stream._hes_kss_tune_list("a", {}, a)
    fb = await stream._hes_kss_tune_list("b", {}, b)
    assert fa["subsongs"] == 3 and fa["tune_slots"] == [0, 2, 5] and fa["default_subsong"] == 0
    assert "subsongs" not in fb and fb["tune_slots"] == [3]
    # a slot that sounds for 0.2 s (a sound effect) is no tune, a 2 s one is
    c = tmp_path / "c.kss"
    c.write_bytes(_kss((0, 1, 4), limits={1: 12, 4: 120}))
    fc = await stream._hes_kss_tune_list("c", {}, c)
    assert fc["tune_slots"] == [0, 4], fc
    if engine == "kssrender":
        assert fa["tune_lengths"] == [20.0, 20.0, 20.0]      # (the cap: a tone never ends)
    else:
        assert "tune_lengths" not in fa
    rec = {"tune_slots": fa["tune_slots"]}
    assert [stream.gme_slot(rec, i) for i in range(4)] == [0, 2, 5, 3]
    assert stream.gme_slot({}, 4) == 4


@pytest.mark.skipif(not _libgme(), reason="libgme absent")
@pytest.mark.asyncio
async def test_a_kss_play_learns_the_list_in_the_background_and_renders_the_slot(
        tmp_path, monkeypatch):
    from soniqboom.config import settings
    from soniqboom.core import gme_render
    monkeypatch.setattr(settings, "sid_default_duration", 20)
    monkeypatch.setattr(stream, "_find_renderer",
                        lambda conf, name: None if name == "kssrender" else shutil.which(name))
    monkeypatch.setattr(S, "_store", S.TrackStore())
    st = S.get_store()
    f = tmp_path / "c.kss"
    f.write_bytes(_kss((1, 4)))
    st.upsert_tracks_batch([_rec("k", f, "KSS", duration=180.0)])
    stream._TUNE_LIST_READ.clear()
    stream._start_tune_list_backfill("k", st.get_track("k"), f, ".kss")
    for _ in range(200):
        if st.get_track("k").get("tune_slots"):
            break
        await asyncio.sleep(0.02)
    rec = st.get_track("k")
    assert rec["subsongs"] == 2 and rec["tune_slots"] == [1, 4] and rec["default_subsong"] == 0
    # tune index 1 (wire 1) renders song slot 4 — which sounds
    assert gme_render.tune_audible(f.read_bytes(), stream.gme_slot(rec, 1))
    assert not gme_render.tune_audible(f.read_bytes(), 1 + 1)


# ── The three wire mappings agree for these families ───────────────────────

@pytest.mark.parametrize("fmt,n", [("ProTracker", 2), ("GoatTracker", 4), ("FamiTracker", 3),
                                   ("KSS", 3), ("HES", 5)])
def test_the_wire_is_the_tune_index_in_every_mapping(fmt, n):
    """Server (``tune_index``; these families never take ``sid_wire_tune``'s
    start-song swap), Subsonic (``wire_tune``, its tune ids and listings) —
    and utils.js ``subsongWireToTune`` in tests/js/every_tune.test.mjs."""
    rec = {"id": "w", "format": fmt, "subsongs": n, "tune_lengths": [float(10 + i) for i in range(n)]}
    assert not stream._header_tuned(rec)
    for w in range(n):
        assert stream.tune_index("w", rec, w) == w
        assert sx.wire_tune(rec, w) == w
        assert sx.wire_length(rec, w) == 10.0 + w
    assert stream.tune_index("w", rec, None) == 0
    assert sx.listed_tunes(rec) == list(range(1, n))
    from soniqboom.api import subsonic as sub
    song = sub._track_to_song({**rec, "path": "/m/w", "title": "W", "artist": "a",
                               "album": "", "duration": 10.0, "added_at": 1})
    kids = sub._with_tunes(rec, song, None)
    assert [k["id"] for k in kids] == ["w"] + [f"w~{i}" for i in range(1, n)]
    assert kids[-1]["title"].endswith(f"(Tune {n}/{n})") and kids[-1]["duration"] == 10 + n - 1


@pytest.mark.skipif(not _libgme(), reason="libgme absent")
@pytest.mark.asyncio
async def test_renders_made_before_a_list_that_moves_tune_0_are_dropped(tmp_path, monkeypatch):
    """A bare play before the list rendered slot 0 as tune 0; a list whose
    tune 0 is another slot drops that render and its measurement."""
    from soniqboom.config import settings
    from soniqboom.core import conversion_cache as cc
    monkeypatch.setattr(settings, "sid_default_duration", 20)
    monkeypatch.setattr(stream, "_find_renderer",
                        lambda conf, name: None if name == "kssrender" else shutil.which(name))
    monkeypatch.setattr(S, "_store", S.TrackStore())
    st = S.get_store()
    f = tmp_path / "d.kss"
    f.write_bytes(_kss((3, 6)))
    st.upsert_tracks_batch([_rec("d", f, "KSS", duration=180.0,
                                 loudness={"0": [-30.0, 0.1]}, loudness_source="analysed")])
    purged = []

    async def fake_purge(ids, fmt):
        purged.append((list(ids), fmt))
        return 1
    monkeypatch.setattr(cc, "purge_entries_for", fake_purge)
    stream._TUNE_LIST_READ.clear()
    stream._start_tune_list_backfill("d", st.get_track("d"), f, ".kss")
    for _ in range(200):
        if st.get_track("d").get("tune_slots"):
            break
        await asyncio.sleep(0.02)
    rec = st.get_track("d")
    assert rec["tune_slots"] == [3, 6] and purged == [(["d"], "gme")]
    assert not rec.get("loudness")


def test_a_kss_files_list_survives_a_re_read_of_the_same_file(tmp_path):
    f = tmp_path / "e.kss"
    f.write_bytes(_kss((0, 2)))
    old = _rec("e", f, "KSS", subsongs=2, tune_slots=[0, 2], tune_lengths=[20.0, 20.0],
               default_subsong=0, loudness={"0": [-12.0, 0.5], "1": [-12.5, 0.5]})
    fresh = _rec("e", f, "KSS")
    S._carry_enrichment(old, fresh)
    assert (fresh["subsongs"], fresh["tune_slots"], fresh["tune_lengths"]) == (2, [0, 2], [20.0, 20.0])
    assert fresh["loudness"] == old["loudness"]
    changed = _rec("e", f, "KSS", file_size=9)
    S._carry_enrichment(old, changed)
    assert "tune_slots" not in changed and "subsongs" not in changed


@pytest.mark.asyncio
async def test_a_goattracker_tunes_own_length_comes_from_its_render(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_store", S.TrackStore())
    st = S.get_store()
    st.upsert_tracks_batch([_rec("gl", DOJO if DOJO.is_file() else tmp_path / "x.sng",
                                 "GoatTracker", subsongs=4, duration=180.0)])
    w = tmp_path / "t.wav"
    import wave as _wave
    with _wave.open(str(w), "wb") as fh:
        fh.setnchannels(2); fh.setsampwidth(2); fh.setframerate(44100)
        fh.writeframes(b"\0\0\0\0" * 44100 * 3)
    assert await stream._backfill_tune_length("gl", 2, w) == 3.0
    assert st.get_track("gl")["tune_lengths"] == [0.0, 0.0, 3.0, 0.0]
    assert sx.wire_length(st.get_track("gl"), 2) == 3.0
