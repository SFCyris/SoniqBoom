# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub #38 — every SNDH tune played 3:00 and every AHX tune 8:31.

* SNDH: the SNDH archive states lengths in FRMS tags (frames at the replay
  timer's rate); only the older TIME tag was read, so every tune fell back
  to the 180 s default.
* libgme (SPC / VGM / NSFe …): the length a file states was never asked for —
  every tune rendered 180 s.
* AHX: uade's player loops for ever, so a render ran to uade's ~512 s
  subsong timeout; the bundled HivelyTracker replayer finds the song end.
"""
from __future__ import annotations

import asyncio
import shutil
import struct
from pathlib import Path

import pytest

from soniqboom.core import metadata

ROOT = Path(__file__).resolve().parent.parent
TD = ROOT / "internal" / "testdata"
SNDH_FRMS = TD / "atari" / "sndh" / "Jess__Buzzmosphere_FRMS.sndh"      # FRMS 18804, TC 70
SNDH_TIME = TD / "atari" / "sndh" / "gwEm__Future_Gone_Wrong_TIME.sndh"  # TIME 1 206
SNDH_NONE = TD / "atari" / "sndh" / "ACC__ACC_Song.sndh"                 # no length
SPC_124 = TD / "game" / "mmgg-02.spc"                                    # ID666 124 s
SPC_8 = TD / "game" / "sbtk-01.spc"                                      # ID666 8 s
VGZ_LOOP = TD / "game" / "09.vgz"                                        # intro 1.5 + loop 70.7
NSF = TD / "gme" / "8bp028-b1-nullsleep-axel_f.nsf"                      # no length
AHX = TD / "tracker" / "agony end.ahx"                                   # song end 104.9 s


def _wav_seconds(p: Path) -> float:
    b = Path(p).read_bytes()
    i, ch, rate, bits = 12, 2, 44100, 16
    while i + 8 <= len(b):
        cid, sz = b[i:i + 4], struct.unpack("<I", b[i + 4:i + 8])[0]
        if cid == b"fmt ":
            ch, rate = struct.unpack("<HI", b[i + 10:i + 16])
            bits = struct.unpack("<H", b[i + 22:i + 24])[0]
        if cid == b"data":
            return (len(b) - (i + 8)) / (rate * ch * bits / 8)
        i += 8 + sz
    return 0.0


# ── SNDH: FRMS / TIME ───────────────────────────────────────────────────────

OLD_STYLE = ("tag field TITL Quiz\ntag field ## 3\ntag field TC 200\n"
             "tag field FRMS 3071\ntag field FRMS 2559\ntag field FRMS 1151\n")
NEW_STYLE = ("tag field TITL Quiz\ntag field ## 3\ntag field TC 200\n"
             "tag field FRMS 1  3071  15.355  00:15\n"
             "tag field FRMS 2  2559  12.795  00:13\n"
             "tag field FRMS 3  1151   5.755  00:06\n")


def test_frms_lengths_in_both_psgplay_output_styles():
    for out in (OLD_STYLE, NEW_STYLE):
        info = metadata.parse_sndh_info(out)
        assert info["lengths"] == {1: 15.0, 2: 13.0, 3: 6.0}   # 15.355 / 12.795 / 5.755 s
        assert info["count"] == 3 and info["title"] == "Quiz"


def test_frms_rate_is_the_first_timer_else_50_hz():
    assert metadata.parse_sndh_info("tag field FRMS 18804\ntag field TC 70\n")["lengths"] == {1: 269.0}
    assert metadata.parse_sndh_info("tag field !V 50\ntag field TC 200\ntag field FRMS 500\n")["lengths"] == {1: 10.0}
    assert metadata.parse_sndh_info("tag field FRMS 500\n")["lengths"] == {1: 10.0}


def test_time_tags_old_and_new_and_frms_preferred():
    assert metadata.parse_sndh_info("tag field TIME 1 95\ntag field TIME 2 0\n")["lengths"] == {1: 95.0}
    assert metadata.parse_sndh_info("tag field TIME 1 261 04:21\n")["lengths"] == {1: 261.0}
    both = "tag field TC 50\ntag field TIME 1 99\ntag field FRMS 1 1000 20.0 00:20\n"
    assert metadata.parse_sndh_info(both)["lengths"] == {1: 20.0}
    # FRMS 0 = unknown: the TIME length still counts.
    assert metadata.parse_sndh_info("tag field TIME 1 42\ntag field FRMS 0\n")["lengths"] == {1: 42.0}
    assert metadata.parse_sndh_info("tag field TITL x\n")["lengths"] == {}


def test_scan_reads_the_default_tracks_frms_length(tmp_path, monkeypatch):
    out = ("tag field TITL T\ntag field ## 2\ntag field !# 2\ntag field TC 50\n"
           "tag field FRMS 1 1000 20 00:20\ntag field FRMS 2 6000 120 02:00\n")

    class R:
        stdout = out.encode()
    monkeypatch.setattr(metadata, "_find_atari_binary", lambda *_a: "/bin/psgplay")
    monkeypatch.setattr(metadata.forksafe, "run", lambda *a, **kw: R())
    p = tmp_path / "t.sndh"
    p.write_bytes(b"SNDH")
    d = metadata._extract_sndh(p, "t")
    assert d["duration"] == 120.0 and d["subsongs"] == 2 and d["start_subsong"] == 1


@pytest.mark.asyncio
async def test_render_passes_the_fractional_length(monkeypatch):
    from soniqboom.api import stream
    seen = []
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "psgplay")
    monkeypatch.setattr(stream, "_sndh_info", lambda p: (1, {1: 269.0}, 1))

    async def fake_await(cmd, tmp, *, timeout, kind):
        seen.append(cmd)
    monkeypatch.setattr(stream, "_await_renderer", fake_await)
    out = await stream._render_sndh(Path("/x.sndh"))
    out.unlink(missing_ok=True)
    assert "--length=269" in seen[0]


_psgplay = pytest.mark.skipif(not shutil.which("psgplay") or not SNDH_FRMS.exists()
                              or not SNDH_TIME.exists(),
                              reason="psgplay or the SNDH fixtures are not available")


@_psgplay
@pytest.mark.asyncio
async def test_real_files_scan_and_render_their_own_lengths():
    from soniqboom.api import stream
    for path, want in ((SNDH_FRMS, 269.0), (SNDH_TIME, 206.0)):   # FRMS 268.63 s → psgplay plays 269
        assert metadata._extract_sndh(path, "t")["duration"] == want
        wav = await stream._render_sndh(path)
        try:
            assert abs(_wav_seconds(wav) - want) < 0.05, path.name
        finally:
            wav.unlink(missing_ok=True)
    if SNDH_NONE.exists():
        assert metadata._extract_sndh(SNDH_NONE, "t")["duration"] == 180.0


# ── libgme: the length a file states ───────────────────────────────────────

def _gme():
    from soniqboom.core import gme_render
    if not gme_render.is_available():
        pytest.skip("libgme missing")
    return gme_render


def test_libgme_stated_lengths():
    g = _gme()
    for path in (SPC_124, SPC_8, VGZ_LOOP, NSF):
        if not path.exists():
            pytest.skip(f"{path.name} missing")
    assert g.play_seconds(SPC_124.read_bytes()) == 124.0
    assert g.render_plan(SPC_124.read_bytes(), 0, 180) == (132.0, 124.0)
    assert g.render_plan(SPC_8.read_bytes(), 0, 180) == (16.0, 8.0)
    secs = g.play_seconds(VGZ_LOOP.read_bytes())        # intro + two loops
    assert secs is not None and 140 < secs < 146
    assert g.play_seconds(NSF.read_bytes()) is None
    assert g.render_plan(NSF.read_bytes(), 0, 180) == (180.0, None)
    assert g.play_seconds(b"junk") is None


@pytest.mark.asyncio
async def test_libgme_scan_and_render_agree():
    _gme()
    if not SPC_124.exists():
        pytest.skip("SPC fixture missing")
    from soniqboom.api import stream
    assert metadata._extract_gme(SPC_124, "t")["duration"] == 132.0
    wav = await stream._render_gme(SPC_124)
    try:
        assert abs(_wav_seconds(wav) - 132.0) < 0.2
    finally:
        wav.unlink(missing_ok=True)


def test_old_sndh_and_libgme_renders_are_not_reused():
    from soniqboom.core.conversion_cache import _cache_key
    assert _cache_key("t", "sndh", subsong=1) == "t__sub1__r2"
    assert _cache_key("t", "gme", subsong=0) == "t__sub0__r2"
    assert _cache_key("t", "psf", subsong=0) == "t__r1"      # PSF: the fade (no subsongs)
    # other render keys are unchanged
    assert _cache_key("t", "uade", subsong=2) == "t__sub2"
    assert _cache_key("t", "tracker", subsong=0) == "t__sub0"


# ── AHX: stop uade at the song end ──────────────────────────────────────────

def _hvl_ready():
    if not AHX.exists():
        pytest.skip("AHX fixture missing")
    if not (shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
            or shutil.which("hvl2wav")):
        pytest.skip("no compiler for hvl2wav")


@pytest.mark.asyncio
async def test_ahx_song_end_from_the_hively_replayer(tmp_path):
    _hvl_ready()
    from soniqboom.api import stream
    # 5245 frames — at uade's PAL rate (49.92 Hz), not hvl2wav's 50 Hz
    assert await stream._ahx_play_seconds(AHX, 0) == 105.07
    other = tmp_path / "x.mod"
    other.write_bytes(b"M.K." * 400)
    assert await stream._ahx_play_seconds(other, 0) is None


@pytest.mark.asyncio
async def test_ahx_render_passes_the_whole_second_timeout(monkeypatch):
    _hvl_ready()
    from soniqboom.api import stream
    seen = []

    async def spawn(*cmd, **kw):
        seen.append(list(cmd))
        raise RuntimeError("stop here")
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "uade123")
    monkeypatch.setattr(stream.forksafe, "spawn_pipe_stdout", spawn)
    with pytest.raises(RuntimeError):
        await stream._render_uade(AHX)
    cmd = seen[0]
    assert cmd[cmd.index("-w") + 1] == "106"


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("uade123"), reason="uade123 missing")
async def test_ahx_render_ends_at_the_song_end():
    _hvl_ready()
    from soniqboom.api import stream
    wav = await stream._render_uade(AHX)
    try:
        assert abs(_wav_seconds(wav) - 105.07) < 0.02      # trimmed to the song end
    finally:
        wav.unlink(missing_ok=True)


def test_hvl_length_without_the_option_writes_nothing_beside_the_module(tmp_path):
    from soniqboom.api import stream
    fake = tmp_path / "old-hvl2wav"
    # An hvl2wav from before ``-l``: converts to its -o name and prints no length.
    fake.write_text('#!/bin/sh\nfor a in "$@"; do case "$a" in -o*) : > "${a#-o}";; esac; done\n'
                    'echo "Converting tune..."\n')
    fake.chmod(0o755)
    mod = tmp_path / "mods" / "song.ahx"
    mod.parent.mkdir()
    mod.write_bytes(b"THX" + b"\0" * 64)
    assert stream._hvl_length(str(fake), mod, 0) is None
    assert sorted(p.name for p in mod.parent.iterdir()) == ["song.ahx"]


# ── Existing libraries: corrected once ──────────────────────────────────────

@pytest.mark.asyncio
async def test_stored_defaults_are_corrected_once(monkeypatch, tmp_path):
    _gme()
    for path in (SNDH_FRMS, SPC_124, NSF, AHX):
        if not path.exists():
            pytest.skip(f"{path.name} missing")
    if not shutil.which("psgplay"):
        pytest.skip("psgplay missing")
    _hvl_ready()
    from soniqboom.config import settings
    from soniqboom.core import conversion_cache, repair
    from soniqboom.core.store import TrackStore
    monkeypatch.setattr(settings, "sid_default_duration", 180)
    s = TrackStore()
    purged = []

    async def purge(ids, fmt):
        purged.append((sorted(ids), fmt))
        return len(ids)
    monkeypatch.setattr(conversion_cache, "purge_entries_for", purge)
    s.upsert_tracks_batch([
        {"id": "sn", "path": str(SNDH_FRMS), "format": "SNDH", "title": "a", "duration": 180.0},
        {"id": "sp", "path": str(SPC_124), "format": "SPC", "title": "b", "duration": 180.0},
        {"id": "ns", "path": str(NSF), "format": "NSF", "title": "c", "duration": 180.0},
        {"id": "ax", "path": str(AHX), "format": "AHX", "title": "d", "duration": 511.98},
        {"id": "mp", "path": "/m/x.mp3", "format": "MP3", "title": "e", "duration": 180.0},
    ])
    r = await repair.fix_render_lengths_once(s)
    dur = {t: s.get_track(t)["duration"] for t in ("sn", "sp", "ns", "ax", "mp")}
    assert dur == {"sn": 269.0, "sp": 132.0, "ns": 180.0, "ax": 105.07, "mp": 180.0}
    assert r["checked"] == 3 and r["updated"] == 3      # a plain NSF states no length
    assert purged == [(["ax"], "uade")]
    assert s.get_config(repair.RENDER_LENGTHS_KEY) == repair.RENDER_LENGTHS_VERSION
    assert (await repair.fix_render_lengths_once(s)) == {"skipped": True}


def test_hvl_length_follows_the_subsong(tmp_path):
    _hvl_ready()
    from soniqboom.api import stream
    import asyncio as _a
    binary = _a.run(stream._hvl2wav_with_length())
    seen = []
    real = stream.forksafe.run

    def spy(cmd, **kw):
        seen.append(cmd)
        return real(cmd, **kw)
    import unittest.mock as um
    with um.patch.object(stream.forksafe, "run", spy):
        stream._hvl_length(binary, AHX, 3)
    assert "-s3" in seen[-1]


@pytest.mark.asyncio
async def test_purge_drops_the_renders_and_their_transcodes(monkeypatch, tmp_path):
    from soniqboom.config import settings
    from soniqboom.core import conversion_cache as cc
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(cc, "_meta", {})
    monkeypatch.setattr(cc, "_lru", __import__("collections").OrderedDict())
    keys = []
    for key, fmt in (("ax__sub0", "uade"), ("ax__sub2", "uade"), ("ot__sub0", "uade"),
                     ("ax__sub0__r2", "gme")):
        p = cc._cache_path(key, fmt)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RIFF")
        cc._meta[key] = {"path": str(p), "format_type": fmt, "size": 4}
        keys.append(key)
    tkey = f"ax__cmp3__ar0__v{cc.rendered_transcode_variant('ax__sub0')}"
    other_t = f"ot__cmp3__ar0__v{cc.rendered_transcode_variant('ot__sub0')}"
    for k in (tkey, other_t):
        p = cc._cache_path(k, "transcoded")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RIFF")
        cc._meta[k] = {"path": str(p), "format_type": "transcoded", "size": 4}
    n = await cc.purge_entries_for(["ax"], "uade")
    assert n == 3
    assert sorted(cc._meta) == sorted(["ot__sub0", "ax__sub0__r2", other_t])


def test_renders_before_the_revision_are_swept_once(tmp_path, monkeypatch):
    from soniqboom.core import conversion_cache as cc
    base = tmp_path
    files = {"sndh": ["t__sub0.wav", "t__sub0__r2.wav"], "gme": ["g__sub1.wav", "g__sub1__r2.wav"],
             "uade": ["u__sub0.wav"], "psf": ["p.wav", "p__r1.wav"]}
    for fmt, names in files.items():
        d = base / fmt / "xx"
        d.mkdir(parents=True)
        for n in names:
            (d / n).write_bytes(b"RIFF")
    td = base / "transcoded" / "xx"
    td.mkdir(parents=True)
    stale_t = td / f"t__cmp3__ar0__v{cc.rendered_transcode_variant('t__sub0')}.wav"
    keep_t = td / f"t__cmp3__ar0__v{cc.rendered_transcode_variant('t__sub0__r2')}.wav"
    stale_t.write_bytes(b"RIFF")
    keep_t.write_bytes(b"RIFF")
    assert cc._sweep_unrevised_renders(base) == 4
    left = sorted(str(p.relative_to(base)) for p in base.rglob("*.wav"))
    assert left == sorted(["sndh/xx/t__sub0__r2.wav", "gme/xx/g__sub1__r2.wav",
                           "uade/xx/u__sub0.wav", "psf/xx/p__r1.wav",
                           str(keep_t.relative_to(base))])
    assert cc._sweep_unrevised_renders(base) == 0          # once
    # Another renderer's later revision sweeps only its own older renders.
    monkeypatch.setattr(cc, "_RENDER_REVS", {**cc._RENDER_REVS, "gme": 3})
    assert cc._sweep_unrevised_renders(base) == 1
    assert (base / "psf/xx/p__r1.wav").exists() and (base / "sndh/xx/t__sub0__r2.wav").exists()


def test_vu_pass_ends_just_after_the_render():
    from soniqboom.api import stream
    cmd = stream._uade_vu_cmd("uade123", Path("/m/x.ahx"), 0, 0, "/tmp/d", 105.07)
    assert cmd[cmd.index("-w") + 1] == "107"
    assert "-w" not in stream._uade_vu_cmd("uade123", Path("/m/x.ahx"), 0, 0, "/tmp/d")


def test_a_vgm_for_zxtune_keeps_the_default(tmp_path, monkeypatch):
    _gme()
    from soniqboom.core import gme_render
    p = tmp_path / "x.vgm"
    p.write_bytes(b"Vgm " + b"\0" * 252)
    monkeypatch.setattr(gme_render, "vgm_needs_other_engine", lambda path: True)
    monkeypatch.setattr(gme_render, "render_plan", lambda *a: (99.0, 91.0))
    assert metadata._gme_stated_duration(p, 180.0) == 180.0
    monkeypatch.setattr(gme_render, "vgm_needs_other_engine", lambda path: False)
    assert metadata._gme_stated_duration(p, 180.0) == 99.0


def _store_with(tracks):
    from soniqboom.core.store import TrackStore
    s = TrackStore()
    s.upsert_tracks_batch(tracks)
    return s


@pytest.mark.asyncio
async def test_pass_keeps_what_it_cannot_measure_and_reruns_without_tools(monkeypatch, tmp_path):
    from soniqboom.core import repair, conversion_cache, metadata as md
    from soniqboom.api import stream

    async def no_purge(ids, fmt):
        return 0
    monkeypatch.setattr(conversion_cache, "purge_entries_for", no_purge)
    looping = tmp_path / "loop.ahx"
    looping.write_bytes(b"THX" + b"\0" * 64)

    async def hvl():
        return "/bin/hvl2wav"
    monkeypatch.setattr(stream, "_hvl2wav_with_length", hvl)
    monkeypatch.setattr(stream, "_hvl_length", lambda *a, **k: None)     # never ends
    s = _store_with([{"id": "ax", "path": str(looping), "format": "AHX", "title": "x",
                      "duration": 511.98},
                     {"id": "gone", "path": str(tmp_path / "missing.ahx"), "format": "AHX",
                      "title": "y", "duration": 511.98},
                     {"id": "sn", "path": str(tmp_path / "x.sndh"), "format": "SNDH",
                      "title": "z", "duration": 180.0}])
    (tmp_path / "x.sndh").write_bytes(b"SNDH")
    monkeypatch.setattr(md, "_find_atari_binary", lambda *a: None)      # no psgplay
    r = await repair.fix_render_lengths_once(s)
    assert {t: s.get_track(t)["duration"] for t in ("ax", "gone", "sn")} == \
        {"ax": 511.98, "gone": 511.98, "sn": 180.0}
    assert r["updated"] == 0
    assert s.get_config(repair.RENDER_LENGTHS_KEY) is None              # runs again later


@pytest.mark.asyncio
async def test_pass_rereads_a_played_catalogue_tune_it_has_no_copy_of(monkeypatch):
    from soniqboom.core import repair, catalogue
    from soniqboom.core.modland_catalog import REREAD
    import soniqboom.core.source_bytes as sb
    path = "ftp://ftp.modland.com/pub/modules:/SNDH/gwEm/x.sndh"
    monkeypatch.setattr(catalogue, "is_catalogue_path", lambda p: p == path)
    monkeypatch.setattr(sb, "read_source_bytes", lambda p, lane="stream": None)
    s = _store_with([{"id": "c", "path": path, "format": "SNDH", "title": "x",
                      "duration": 180.0, "mtime": 5.0}])
    await repair.fix_render_lengths_once(s)
    assert s.get_track("c").get(REREAD) is True and s.get_track("c")["duration"] == 180.0


@pytest.mark.asyncio
async def test_pass_resumes_after_the_chunk_it_was_on(monkeypatch, tmp_path):
    from soniqboom.core import repair
    seen = []

    def stated(kind, data, tune, default, hvl, ext=""):
        seen.append(kind)
        return 99.0
    monkeypatch.setattr(repair, "_stated_length", stated)
    monkeypatch.setattr(repair, "_RENDER_LENGTHS_CHUNK", 1)
    import soniqboom.core.source_bytes as sb
    monkeypatch.setattr(sb, "read_source_bytes", lambda p, lane="stream": b"x")
    s = _store_with([{"id": f"t{i}", "path": f"/m/{i}.spc", "format": "SPC", "title": "x",
                      "duration": 180.0} for i in range(3)])
    # A previous pass died inside the chunk at index 1: resume after it.
    s.set_config(repair.RENDER_LENGTHS_PROGRESS_KEY,
                 {"version": repair.RENDER_LENGTHS_VERSION, "next": 2})
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 180)
    await repair.fix_render_lengths_once(s)
    assert seen == ["gme"]
    assert s.get_track("t2")["duration"] == 99.0 and s.get_track("t0")["duration"] == 180.0
    assert s.get_config(repair.RENDER_LENGTHS_PROGRESS_KEY) is None
