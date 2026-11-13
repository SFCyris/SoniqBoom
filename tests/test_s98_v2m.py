# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S98 register dumps and Farbrausch V2 songs, played by zxtune123: the scan's
length and tags (``metadata.s98_seconds`` / ``s98_tags`` / ``v2m_seconds``),
their routing (the PSF render route, never uade), the Modland catalogue's
kinds — and, where the reference files (``internal/testdata/zxtune``) and
zxtune123 are there, real renders whose length is the scanned one."""
from __future__ import annotations

import array
import shutil
import struct
import wave
from pathlib import Path

import pytest

from soniqboom.core import metadata as md

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "internal/testdata/zxtune"
UNSUPPORTED = ROOT / "internal/testdata/unsupported"


# ── Synthesized files ────────────────────────────────────────────────────────

def _s98(version: bytes, dump: bytes, *, num=0, den=0, tag: bytes = b"", loop=0,
         devices=()) -> bytes:
    """An S98 file: header, (v3) device list, the tag block, then ``dump``."""
    devs = b"".join(struct.pack("<4I", *d) for d in devices)
    head_len = 0x20 + len(devs)
    tag_at = head_len if tag else 0
    dump_at = head_len + len(tag)
    head = (b"S98" + version + struct.pack("<5I", num, den, 0, tag_at, dump_at)
            + struct.pack("<II", loop and dump_at + loop, len(devices)))
    return head + devs + tag + dump


def _v2m(timediv: int, maxtime: int, events) -> bytes:
    """A V2M header: ``events`` = ``(tick delta, µs per beat)``; the deltas as
    three byte columns, then each event's tempo as 32 bits, then num / den /
    ticks-per-quarter columns."""
    n = len(events)
    cols = bytes(e[0] & 0xFF for e in events) + bytes(e[0] >> 8 & 0xFF for e in events) \
        + bytes(e[0] >> 16 & 0xFF for e in events)
    tempos = b"".join(struct.pack("<I", e[1]) for e in events)
    return struct.pack("<3I", timediv, maxtime, n) + cols + tempos + b"\4" * n + b"\4" * n \
        + b"\x08" * n + b"\0" * 64


def test_s98_seconds_counts_one_and_n_syncs_to_the_end_mark():
    # FF = 1 sync; FE + 7-bit LE count (0x81 0x01 = 129) = 131 syncs; a register
    # write (dev 0 port 0) takes no time; FD ends it — what follows is ignored.
    dump = b"\xff" + b"\xfe\x81\x01" + b"\x00\x07\x38" + b"\xfd" + b"\xff" * 50
    raw = _s98(b"3", dump, num=10, den=1000, devices=[(4, 7987200, 0, 0)])
    assert md.s98_seconds(raw) == pytest.approx(132 * 0.01)
    # timer 0 / 0 = 10 / 1000 s per sync; no end mark: the data's end
    assert md.s98_seconds(_s98(b"3", b"\xff" * 300)) == pytest.approx(3.0)
    # version 1 has no TIMER INFO2: its second field is not the denominator
    assert md.s98_seconds(_s98(b"1", b"\xff" * 100, num=5, den=7)) == pytest.approx(0.5)


def test_s98_seconds_of_what_is_no_s98_file():
    assert md.s98_seconds(b"") == 0.0
    assert md.s98_seconds(b"S98x" + b"\0" * 40) == 0.0
    assert md.s98_seconds(b"PSF\x01" + b"\0" * 40) == 0.0
    assert md.s98_seconds(_s98(b"3", b"\x00\x01")) == 0.0          # a cut write, no wait
    assert md.s98_seconds(_s98(b"3", b"\xfe\x80\x80")) > 0          # a cut count: no crash
    bad = bytearray(_s98(b"3", b"\xff" * 10))
    bad[0x14:0x18] = b"\0\0\0\0"                                    # a dump inside the header
    assert md.s98_seconds(bytes(bad)) == 0.0


def test_s98_tags_v3_block_in_utf8_or_shift_jis():
    utf = _s98(b"3", b"\xff\xfd", tag="[S98]﻿Title=Opening\nARTIST=Crystal King\n"
               "game=Relics\nyear=1986\n\0".encode("utf-8"))
    assert md.s98_tags(utf) == {"title": "Opening", "artist": "Crystal King",
                                "game": "Relics", "year": "1986"}
    sjis = _s98(b"3", b"\xff\xfd", tag=b"[S98]title=" + "起動時BGM".encode("cp932")
                + b"\nartist=ZUN\n\0")
    assert md.s98_tags(sjis) == {"title": "起動時BGM", "artist": "ZUN"}
    # version 1: the tag offset holds the title
    v1 = _s98(b"1", b"\xff\xfd", tag="SILF OF WIND I".encode("cp932") + b"\0" * 8)
    assert md.s98_tags(v1) == {"title": "SILF OF WIND I"}
    assert md.s98_tags(_s98(b"3", b"\xff\xfd")) == {}               # no tag block
    inside = bytearray(_s98(b"1", b"\xff\xfd"))
    inside[0x10:0x14] = b"\x10\0\0\0"                                # tag offset inside the header
    assert md.s98_tags(bytes(inside)) == {}
    ctl = _s98(b"1", b"\xff\xfd", tag=b"\x07Title\x1b here\0")
    assert md.s98_tags(ctl) == {"title": "Title here"}


def test_v2m_seconds_follows_the_tempo_map():
    # 96 ticks a beat, 120 BPM (500000 µs a beat): 960 ticks = 5 s
    assert md.v2m_seconds(_v2m(96, 960, [(0, 500000)])) == pytest.approx(5.0)
    # the tempo doubles 480 ticks in: 2.5 s + 1.25 s
    assert md.v2m_seconds(_v2m(96, 960, [(0, 500000), (480, 250000)])) == pytest.approx(3.75)
    # ticks before the first tempo event count at its tempo
    assert md.v2m_seconds(_v2m(96, 960, [(480, 500000)])) == pytest.approx(5.0)
    # a tempo event past the song's end changes nothing
    assert md.v2m_seconds(_v2m(96, 960, [(0, 500000), (5000, 1)])) == pytest.approx(5.0)
    assert md.v2m_seconds(b"") == 0.0
    assert md.v2m_seconds(struct.pack("<3I", 0, 960, 1) + b"\0" * 20) == 0.0
    assert md.v2m_seconds(struct.pack("<3I", 96, 960, 9) + b"\0" * 20) == 0.0     # cut


def test_extract_reads_length_and_tags(tmp_path):
    s98 = tmp_path / "relics.S98"
    s98.write_bytes(_s98(b"3", b"\xff" * 200 + b"\xfd", num=1, den=100,
                         tag=b"[S98]title=Opening\nartist=Crystal King\ngame=Relics\n"
                             b"year=1986\ncomment=rip\n\0"))
    d = md.extract(s98, "s").model_dump()
    assert (d["format"], d["title"], d["artist"], d["album"], d["album_source"], d["year"],
            d["comment"], d["duration"], d["genre"]) == (
        "S98", "Opening", "Crystal King", "Relics", "tag", 1986, "rip", 2.0,
        ["Chiptune", "Game Rip"])
    v2 = tmp_path / "female.v2m"
    v2.write_bytes(_v2m(96, 960, [(0, 500000)]))
    d = md.extract(v2, "v").model_dump()
    assert (d["format"], d["title"], d["duration"], d["genre"]) == (
        "V2M", "female", 5.0, ["Chiptune"])
    bad = tmp_path / "broken.v2m"
    bad.write_bytes(b"junk")
    assert md.extract(bad, "b").model_dump()["duration"] == 0      # (backfilled at play)


# ── Routing ──────────────────────────────────────────────────────────────────

def test_listed_and_routed_to_zxtune123_never_uade():
    from soniqboom.api import stream
    from soniqboom.core import uade_formats, retro
    for name in ("a.s98", "B.S98", "c.v2m", "D.V2M"):
        assert md.is_supported_music_name(name)
        assert uade_formats.classify(name) is None
    for ext in (".s98", ".v2m"):
        assert ext in stream._PSF_STREAM_EXTS and stream._is_rendered_ext(ext)
    assert stream.renderer_key_for({"id": "a", "path": "/m/x.s98", "format": "S98"}) == "zxtune123"
    assert stream.renderer_key_for({"id": "b", "path": "/m/y.v2m", "format": "V2M"}) == "zxtune123"
    assert stream._zxtune_kind(Path("x.S98")) == "S98"
    assert stream._zxtune_kind(Path("x.minipsf")) == "PSF"
    assert (retro.chip_family("S98"), retro.chip_family("V2M")) == ("s98", "v2")
    assert retro.coarse_family("S98") == retro.coarse_family("V2M") == "chiptune"


def test_render_plan_is_the_scanned_length(tmp_path):
    from soniqboom.api import stream
    s98 = tmp_path / "a.s98"
    s98.write_bytes(_s98(b"3", b"\xff" * 300))
    v2 = tmp_path / "b.v2m"
    v2.write_bytes(_v2m(96, 960, [(0, 500000), (480, 250000)]))
    assert stream._psf_render_plan(s98) == (pytest.approx(3.0), 0.0)
    assert stream._psf_render_plan(v2) == (pytest.approx(3.75), 0.0)
    assert stream._psf_render_plan(tmp_path / "missing.s98") == (0.0, 0.0)


@pytest.mark.parametrize("rel, kind", [
    ("S98/Zun/Touhou 05/th5-00.s98", 1),
    ("V2/KB/fr-011 picknick in the meadows.v2m", 1),
    ("FamiTracker/80something/Square eyes.ftm", 1),       # (Furnace, not Face The Music)
    ("MDX/Ayako Mori/Ghosts'N Goblins (ARCADE)/Map Scroll.mdx", 1),     # (mdxrender)
    ("PMD/M. Kajihara/For Graunseed/calm milly.m", 2),
    ("SunVox/Asdn/out of time.sunvox", 1),               # (sunvoxrender)
    ("GoatTracker 2/Cadaver/dojo.sng", 1),                # (gt2reloc, not uade's ZoundMonitor)
    ("MGSDRV/Ain/Mario-Net - Theme.MGS", 1),              # (kssrender with MGSDRV)
    ("MoonBlaster/Anbert Smedes/Forever [50Hz].MBM", 2),
    ("MuSICA/Convex/Funky Fingers.BGM", 2),
    ("OPLLDRV/Gake/Sakura Sakura.OPX", 2),
    ("Super Nintendo Sound Format/Glenn Stafford/Blackthorne/blackthorne-0000.minisnsf", 2),
])
def test_modland_lists_only_what_plays(rel, kind):
    from soniqboom.core import modland_catalog as mc
    assert (mc.KIND_MUSIC, mc.KIND_OTHER)[kind - 1] == mc._classify(rel)


def test_modland_family_genres():
    from soniqboom.core import modland_catalog as mc
    assert mc.family_genre("S98/Zun/x/th5-00.s98") == ["Chiptune", "Game Rip"]
    assert mc.family_genre("V2/KB/x.v2m") == ["Chiptune"]


# ── The reference files ──────────────────────────────────────────────────────

needs_data = pytest.mark.skipif(not DATA.is_dir(), reason="internal/testdata/zxtune absent")
# file → (the length zxtune123 renders, measured 2026-11-11, zxtune123 r5112)
_EXPECTED = {
    "s98/relics98amd981.s98": 181.13,       # v3, three OPNA, no loop
    "s98/silpheed-01.s98": 102.23,          # v1, loop point (played once)
    "s98/th5-00.s98": 10.80,                # v3, one OPNA, Shift-JIS tags
    "v2m/female.v2m": 126.55,
    "v2m/fr-011 picknick in the meadows.v2m": 219.72,
    "v2m/the scene is dead.v2m": 229.29,
    "v2m/agony remix.v2m": 294.40,          # four tempo events
}


@needs_data
@pytest.mark.parametrize("rel, secs", sorted(_EXPECTED.items()))
def test_reference_lengths(rel, secs):
    d = md.extract(DATA / rel, "r").model_dump()
    assert d["duration"] == pytest.approx(secs, abs=0.01)
    assert d["format"] == ("S98" if rel.endswith(".s98") else "V2M")


@needs_data
def test_reference_tags():
    d = md.extract(DATA / "s98/relics98amd981.s98", "r").model_dump()
    assert (d["title"], d["artist"], d["album"], d["year"]) == (
        "Opening Theme", "Crystal King", "RELICS  AMD-98 Version", 1986)
    d = md.extract(DATA / "s98/th5-00.s98", "r").model_dump()
    assert (d["title"], d["artist"], d["album"], d["year"]) == ("起動時BGM", "ZUN", "東方怪綺談", 1998)
    assert md.extract(DATA / "s98/silpheed-01.s98", "r").model_dump()["title"] == "SILF OF WIND I"


@pytest.mark.skipif(not UNSUPPORTED.is_dir(), reason="internal/testdata/unsupported absent")
def test_formats_no_engine_plays_are_not_scanned():
    """PMD, SNSF, the MSX drivers other than MGSDRV (MuSICA, MoonBlaster,
    OPLLDRV): never listed.  (GoatTracker, FamiTracker, SunVox and MGSDRV
    play since 2.1.0 — tests/test_one_tune_engines.py.)"""
    names = sorted(f.name for f in UNSUPPORTED.iterdir())
    assert names, "internal/testdata/unsupported is empty"
    for name in names:
        assert not md.is_supported_music_name(name), name


# ── Real renders ─────────────────────────────────────────────────────────────

def _zxtune() -> "str | None":
    from soniqboom.config import settings
    p = settings.zxtune123_path
    return p if p and Path(p).is_file() else shutil.which("zxtune123")


needs_zxtune = pytest.mark.skipif(_zxtune() is None or not DATA.is_dir(),
                                  reason="zxtune123 or internal/testdata/zxtune absent")


def _stats(path: Path) -> "tuple[float, int]":
    with wave.open(str(path)) as w:
        n, rate = w.getnframes(), w.getframerate()
        a = array.array("h")
        a.frombytes(w.readframes(n))
    return n / rate, max(map(abs, a), default=0)


@needs_zxtune
@pytest.mark.asyncio
@pytest.mark.parametrize("rel", ["s98/th5-00.s98", "s98/silpheed-01.s98", "v2m/female.v2m"])
async def test_render_is_the_scanned_length_with_audio(rel):
    from soniqboom.api import stream
    out = await stream._render_psf(DATA / rel)
    try:
        secs, peak = _stats(out)
    finally:
        out.unlink(missing_ok=True)
    assert secs == pytest.approx(_EXPECTED[rel], abs=0.05)
    assert peak > 1000


@needs_zxtune
@pytest.mark.asyncio
async def test_the_stream_endpoint_plays_them_cold_then_from_the_cache(monkeypatch, tmp_path):
    """Two different files cold (each rendered by zxtune123), then the first
    again warm (a cache hit)."""
    import asyncio
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        from test_render_r1_stream import _T, _app, _client
    finally:
        sys.path.pop(0)
    from soniqboom.core import unplayable
    monkeypatch.setattr(unplayable, "_proven", {})
    tracks = {"s98": _T("s98", DATA / "s98/th5-00.s98", 10.8, fmt="S98"),
              "v2m": _T("v2m", DATA / "v2m/female.v2m", 126.55, fmt="V2M")}
    app, _fs = _app(monkeypatch, tmp_path, tracks)
    got = []
    async with _client(app) as c:
        for tid in ("s98", "v2m", "s98"):
            r = await c.get(f"/api/stream/{tid}", cookies={"sb_session": "x"})
            assert r.status_code == 200, r.text
            got.append((tid, r.headers.get("x-rendered"), r.headers.get("x-cache"), r.content))
            await asyncio.sleep(0.2)
    assert [g[1] for g in got] == ["zxtune"] * 3
    assert got[0][2].startswith("miss") and got[1][2].startswith("miss") and got[2][2] == "hit"

    def _pcm(wav: bytes) -> bytes:
        i = wav.find(b"data")
        size = int.from_bytes(wav[i + 4:i + 8], "little")
        return wav[i + 8:i + 8 + size] if size != 0xFFFFFFFF else wav[i + 8:]
    s98_pcm, v2m_pcm, warm = (_pcm(g[3]) for g in got)
    assert len(s98_pcm) / (44100 * 4) == pytest.approx(10.80, abs=0.05)
    assert len(v2m_pcm) / (44100 * 4) == pytest.approx(126.55, abs=0.05)
    assert warm == s98_pcm
    assert sorted(p.stem for p in (tmp_path / "conv" / "psf").rglob("*.wav")) == [
        "s98__r1", "v2m__r1"]
    assert "zxtune123" in unplayable._proven
