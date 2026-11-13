# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The formats of NostalgicPlayer's list SoniqBoom plays since 2.1.0, on the
real files in ``internal/testdata/nostalgicplayer`` (downloaded from
nostalgicplayer.dk and modland.com; ``MANIFEST.md``) — skipped where that
folder, or the engine a test needs, is absent.

* libopenmpt formats listed when the installed libopenmpt reads them
  (``metadata._OPENMPT_OPTIONAL``) and other names it reads (``.ptm``,
  OctaMED's / ChipTracker's other suffixes); a ``.dtm`` / ``.psm`` / ``.xmf``
  libopenmpt can't open is not listed.
* ffmpeg audio: WMA (1, 2, Pro, Lossless), MPEG layer 1 / 2, ``.oga``, IFF
  8SVX (also Fibonacci-packed) / 16SV.
* uade's formats under other names (``uade_formats._SUFFIX_ALIASES``), a
  TFMX ``X.tfx`` with its ``X.sam`` staged as ``mdat.X`` + ``smpl.X``, a
  Kefrens Sound Machine ``.ksm`` (Ken Silverman's AdLib suffix too).
* C64 Sidplayer songs (sidplayfp, with the ``.str`` of a stereo song).
* xmprender (libxmp) modules and the packed modules ``core.prowizard``
  converts for it (xmprender: ``SB_XMPRENDER``, else on PATH).
"""
from __future__ import annotations

import array
import asyncio
import math
import os
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from soniqboom.api import stream
from soniqboom.config import settings
from soniqboom.core import metadata as md, uade_formats as uf, xpk

ROOT = Path(__file__).resolve().parents[1]
NP = ROOT / "internal/testdata/nostalgicplayer"
XMPRENDER = os.environ.get("SB_XMPRENDER") or shutil.which("xmprender")
UADE = shutil.which("uade123")
SIDPLAYFP = shutil.which("sidplayfp")
FFMPEG = shutil.which("ffmpeg")

needs_np = pytest.mark.skipif(not NP.is_dir(), reason="internal/testdata/nostalgicplayer absent")
needs_xmp = pytest.mark.skipif(not XMPRENDER, reason="xmprender not found (SB_XMPRENDER)")
needs_uade = pytest.mark.skipif(not UADE or not uf.player_map(), reason="uade not installed")
needs_sid = pytest.mark.skipif(not SIDPLAYFP, reason="sidplayfp not found")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not found")


def _files(folder: str, *exts: str) -> list[Path]:
    d = NP / folder
    out = sorted(p for p in d.iterdir() if p.is_file() and (not exts or p.suffix.lower() in exts)) \
        if d.is_dir() else []
    if not out:
        pytest.skip(f"no test files in {folder}")
    return out


def _wav(path: Path) -> "tuple[float, float, array.array, int]":
    """(seconds, peak dBFS, samples, channels) of a 16-bit WAV."""
    with wave.open(str(path)) as w:
        ch, rate, n = w.getnchannels(), w.getframerate(), w.getnframes()
        a = array.array("h")
        a.frombytes(w.readframes(n))
    m = max((abs(v) for v in a), default=0)
    return n / rate, (20 * math.log10(m / 32768) if m else -999.0), a, ch


@pytest.fixture
def in_worker(monkeypatch):
    """Extraction as a scan worker runs it: libopenmpt in-process."""
    monkeypatch.setattr(md, "_LIBOPENMPT_IN_PROCESS", True)


@pytest.fixture
def xmp_path(monkeypatch):
    if XMPRENDER:
        monkeypatch.setattr(settings, "xmprender_path", XMPRENDER)


# ── 1. libopenmpt ─────────────────────────────────────────────────────────────

_OMPT_FOLDERS = {
    "astroideaxmf": "Astroidea XMF", "digitalsymphony": "Digital Symphony",
    "digitaltracker": "Digital Tracker", "softwarevisions": "Software Visions",
    "digitrakker": "Digitrakker", "epicgamesumx": "Unreal Music", "dsmicompact": "DSMI Compact",
    "epicmegagamesmasi": "Epic MegaGames MASI", "epicmegagamesmasi16": "Epic MegaGames MASI",
    "galaxymusicsystem40": "Galaxy Music System", "galaxymusicsystem50": "Galaxy Music System",
    "mo3": "MO3", "oggmod": "OggMod", "openmptmptm": "OpenMPT MPTM",
    "realtracker": "Real Tracker", "screamtrackermusicinterfacekit": "STMIK",
    "polytracker": "Poly Tracker", "protrackeriff": "ProTracker IFF",
}


@needs_np
@pytest.mark.parametrize("folder,fmt", sorted(_OMPT_FOLDERS.items()))
def test_libopenmpt_formats_are_read_with_their_own_names(folder, fmt, in_worker):
    if not md.OPENMPT_EXTRA_EXTS - {".ftm"}:
        pytest.skip("libopenmpt not loadable here")
    for f in _files(folder):
        assert md.is_supported_music_name(f.name), f.name
        m = md.extract(f, "x").model_dump()
        assert m["format"] == fmt, (f.name, m["format"])
        assert m["duration"] > 10 and m["channels"] and m["title"], (f.name, m)
        # libopenmpt plays it: the tracker route, never uade
        assert f.suffix.lower() in stream._TRACKER_EXTS
        assert stream._render_ident(str(f), m) == (f.suffix.lower(), False)


@needs_np
def test_sfx2_and_the_titles_libopenmpt_reads(in_worker):
    """A SoundFX 2.0 module (saved as ``.sfx``) and a title only libopenmpt
    reads (Digital Symphony keeps its own): the module's, not the file's."""
    m = md.extract(NP / "digitalsymphony/A Track.dsym", "x").model_dump()
    assert m["title"] == "a track!"
    assert md.FORMAT_NAMES[".sfx2"] == "SoundFX"


@pytest.mark.parametrize("in_process", [True, False])
def test_a_file_libopenmpt_cant_open_under_a_shared_suffix_is_not_listed(tmp_path, monkeypatch,
                                                                         in_process):
    """DeFy AdLib Tracker ``.dtm``, ZX Spectrum ``.psm``, an Extensible Music
    Format ``.xmf``, a ``.dmf`` of none of its formats: libopenmpt opens none —
    skipped like an unplayed suffix, no scan error, no stub; in a scan worker
    (libopenmpt in-process) and in the server (``openmpt123 --info``, which
    exits 0 on a file it can't load) alike."""
    monkeypatch.setattr(md, "_LIBOPENMPT_IN_PROCESS", in_process)
    if not in_process and not shutil.which("openmpt123"):
        pytest.skip("openmpt123 not found")
    import zlib
    for name, body in (("defy.dtm", b"DeFy DTM \x00" + bytes(500)),
                       ("spec.psm", b"\x00\x01PSM" + bytes(400)),
                       ("x.xmf", b"XMF_2.00" + bytes(300)),
                       ("song.dmf", zlib.compress(b"not a module" + bytes(2000)))):
        if f".{name.rsplit('.', 1)[1]}" not in md.OPENMPT_EXTRA_EXTS:
            continue
        p = tmp_path / name
        p.write_bytes(body)
        with pytest.raises(md.NotPlayableHere):
            md.extract(p, "x")


@needs_np
def test_other_names_of_octamed_and_chiptracker_modules(tmp_path, in_worker):
    """``.md0``–``.md3`` / ``.mmd3`` / ``.omed`` / ``.ocss`` (OctaMED) and
    ``.chp`` / ``.chip`` (ChipTracker): libopenmpt by content."""
    src = _files("octamedsoundstudio")[0]
    for ext in (".md0", ".md3", ".mmd3", ".omed", ".ocss"):
        p = tmp_path / f"song{ext}"
        shutil.copyfile(src, p)
        assert md.is_supported_music_name(p.name)
        m = md.extract(p, "x").model_dump()
        assert m["format"] == "OctaMED" and m["duration"] > 60, (ext, m)
    chip = _files("chiptracker")[0]
    for ext in (".chp", ".chip"):
        p = tmp_path / f"song{ext}"
        shutil.copyfile(chip, p)
        m = md.extract(p, "x").model_dump()
        assert m["format"] == "ChipTracker" and m["duration"] > 10, (ext, m)


def test_ptm_is_libopenmpts_under_either_name():
    """``song.ptm`` and the ``ptm.song`` prefix name (archive.py maps it to
    ``.mod``) are both libopenmpt's — uade's ProTracker 4 token claims neither."""
    assert ".ptm" in md._TRACKER_EXTS and ".ptm" not in md._UADE_SUFFIX_EXTS
    assert ".ptm" in stream._TRACKER_EXTS
    assert uf.classify("song.ptm") is None and uf.classify("ptm.song") is None


# ── 2. ffmpeg audio ───────────────────────────────────────────────────────────

@needs_np
def test_wma_formats_named_by_their_codec():
    want = {"windowsmediaaudio1": "WMA", "windowsmediaaudio2": "WMA",
            "windowsmediaaudiolossless": "WMA Lossless", "windowsmediaaudioprofessional": "WMA Pro"}
    for folder, fmt in want.items():
        for f in _files(folder, ".wma"):
            m = md.extract(f, "x").model_dump()
            assert m["format"] == fmt and m["duration"] > 10, (f.name, m)
            assert m["is_lossless"] == (fmt == "WMA Lossless")
    pro = {md.extract(f, "x").channels for f in _files("windowsmediaaudioprofessional", ".wma")}
    assert pro == {6, 8}                     # (the 6- and 8-channel mixes)


def _ffprobe_seconds(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


@needs_np
@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe not found")
def test_mpeg_layer_1_and_2_and_iff_samples_measure_as_ffprobe_does():
    """The scan's lengths: MPEG layer 1 / 2 (mutagen, else ffprobe) and IFF
    8SVX / 16SV (the BODY's samples — two per byte Fibonacci-packed) agree
    with ffprobe's to 3 %."""
    files = (_files("mpeg10") + _files("mpeg20") + _files("iff8svx_pcm")
             + _files("iff8svx_fibonacci") + _files("iff16sv_pcm"))
    for f in files:
        m = md.extract(f, "x").model_dump()
        ref = _ffprobe_seconds(f)
        assert ref > 0 and abs(m["duration"] - ref) <= max(0.03 * ref, 0.06), (f.name, m["duration"], ref)
        layer = {".mp1": "MP1", ".mp2": "MP2", ".m2a": "MP2"}.get(f.suffix.lower())
        if layer:
            assert m["format"] == layer
    st = md.extract(NP / "iff8svx_fibonacci/Lille.Stereo.8svx", "x").model_dump()
    assert st["channels"] == 2 and st["format"] == "IFF 8SVX"
    assert md.extract(NP / "iff16sv_pcm/CLAPS.16sv", "x").title == "CLAPS"   # (NAME "CLAPS.iff")


@needs_np
@needs_ffmpeg
def test_ffmpeg_decodes_every_new_audio_file(tmp_path):
    """What a play transcodes: ffmpeg decodes each to audible PCM."""
    for folder in ("windowsmediaaudio1", "windowsmediaaudio2", "windowsmediaaudiolossless",
                   "windowsmediaaudioprofessional", "mpeg10", "mpeg20", "iff8svx_pcm",
                   "iff8svx_fibonacci", "iff16sv_pcm"):
        f = _files(folder)[0]
        out = tmp_path / f"{folder}.wav"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(f), "-t", "10", "-ac", "2",
                        "-ar", "44100", "-c:a", "pcm_s16le", str(out)], check=True)
        _secs, db, _a, _ch = _wav(out)
        assert db > -40, (folder, db)


@needs_np
def test_oga_reads_as_ogg_vorbis(tmp_path):
    p = tmp_path / "x.oga"
    shutil.copyfile(_files("oggvorbis")[0], p)
    assert md.is_supported_music_name(p.name)
    m = md.extract(p, "x").model_dump()
    assert m["format"] == "Ogg Vorbis" and m["duration"] > 1


# ── 3. uade's formats under other names ───────────────────────────────────────

_ALIASES = {
    "channelplayer1": ".chan", "channelplayer2": ".chan", "channelplayer3": ".chan",
    "sidmon10": ".sid1", "sidmon20": ".sid2", "moduleprotector": ".mpro",
    "wantonpacker": ".wn", "startrekkerpacker": ".stpk", "fuzzacpacker": ".fuz",
}


@needs_np
@needs_uade
@pytest.mark.parametrize("folder,as_ext", sorted(_ALIASES.items()))
def test_other_suffixes_play_under_uades_token(folder, as_ext, tmp_path):
    f = _files(folder)[0]
    assert md.is_supported_music_name(f.name)
    assert uf.classify(f.name) is not None
    staged = xpk.stage_for_uade(f, UADE)
    try:
        assert staged is not None and staged.name == f.stem + as_ext
        out = tmp_path / "u.wav"
        subprocess.run([UADE, "-t", "6", *uf.player_args(staged, UADE), "-e", "wav", "-f", str(out),
                        str(staged)], capture_output=True, cwd=str(staged.parent), timeout=60)
        _secs, db, _a, _ch = _wav(out)
        assert db > -30, (f.name, db)
    finally:
        if staged is not None:
            shutil.rmtree(staged.parent, ignore_errors=True)


@needs_np
@needs_uade
@pytest.mark.parametrize("folder", ["tfmx15", "tfmx7voices", "tfmxprofessional"])
def test_a_tfx_song_plays_as_mdat_with_its_sam_as_smpl(folder, tmp_path):
    f = [p for p in _files(folder, ".tfx") if "Effect" not in p.name][0]
    assert md.is_supported_music_name(f.name) and not md.is_supported_music_name(f.stem + ".sam")
    assert uf.expected_companions(f.name) == (f.stem + ".sam",)
    staged = xpk.stage_for_uade(f, UADE)
    try:
        assert staged.name == "mdat." + f.stem
        assert (staged.parent / ("smpl." + f.stem)).resolve() == (f.parent / (f.stem + ".sam")).resolve()
        out = tmp_path / "t.wav"
        subprocess.run([UADE, "-t", "8", *uf.player_args(staged, UADE), "-e", "wav", "-f", str(out),
                        str(staged)], capture_output=True, cwd=str(staged.parent), timeout=60)
        _secs, db, _a, _ch = _wav(out)
        assert db > -30, (f.name, db)
    finally:
        shutil.rmtree(staged.parent, ignore_errors=True)


@needs_np
@needs_uade
def test_kefrens_ksm_is_uades_and_kens_ksm_stays_adlib(tmp_path):
    for f in _files("kefrenssoundmachine", ".ksm"):
        assert md.is_kefrens_module(f.read_bytes()[:16])
        m = md.extract(f, "x").model_dump()
        assert m["format"] != md.FORMAT_NAMES[".ksm"], m
        assert stream._render_ident(str(f), m) == (".ksm", True)
    ken = tmp_path / "song.ksm"
    ken.write_bytes(bytes(range(256)) * 8)          # (no "M." … "a" Kefrens header)
    near = bytearray((NP / "kefrenssoundmachine/Dig-It-Twice.ksm").read_bytes()[:64])
    near[15] = ord("b")                             # ("M." without the "a" at 15)
    assert md.is_kefrens_module(bytes(near[:16])) is False
    assert not md.is_kefrens_module(ken.read_bytes()[:16])
    assert stream._render_ident(str(ken), {"format": "Ken's AdLib"}) == (".ksm", False)


def test_alias_registration_keeps_owned_and_common_suffixes():
    """An alias claims a suffix only for a token the conf lists, never an
    owned or common one; ``.is`` (common) stays unlisted, ``.is10`` plays."""
    if not uf.player_map():
        pytest.skip("uade not installed")
    tokens = uf.new_suffix_tokens()
    for alias in uf._SUFFIX_ALIASES:
        assert alias not in uf._SUFFIX_OWNED_ELSEWHERE
        if uf._SUFFIX_ALIASES[alias] in uf.player_map():
            assert alias in tokens, alias
    assert not md.is_supported_music_name("song.is") and md.is_supported_music_name("song.is10")
    assert uf.play_name("x/Cherry.is10") == "Cherry.is"
    assert uf.play_name("x/Song.TFX") == "mdat.Song"


# ── 4. C64 Sidplayer ──────────────────────────────────────────────────────────

@needs_np
def test_c64_sidplayer_songs_are_told_from_ufo_by_content(tmp_path):
    for f in _files("sidplay", ".mus") + _files("sidplay-stereo", ".mus"):
        assert md.is_c64_sidplayer(f.read_bytes())
        m = md.extract(f, "x").model_dump()
        assert m["format"] == md.C64_SIDPLAYER_FORMAT
        assert stream._render_ident(str(f), m) == (stream._SIDPLAYER_ROUTE, False)
    ufo = tmp_path / "x.mus"
    ufo.write_bytes(b"FORM\x00\x00\x10\x00SMUS" + bytes(4096))
    assert not md.is_c64_sidplayer(ufo.read_bytes())
    assert stream._render_ident(str(ufo), {"format": "UFO"})[0] == ".mus"
    assert not md.is_supported_music_name("song.str")


@needs_np
@needs_sid
def test_a_stereo_sidplayer_song_plays_both_sids_and_a_lone_mus_one(tmp_path, monkeypatch):
    """With its ``.str`` beside it the song's two SIDs differ left and right;
    the same ``.mus`` alone plays one SID in both channels."""
    monkeypatch.setattr(md, "SIDPLAYER_MAX_S", 8)          # (a short render: the cap)
    src = NP / "sidplay-stereo/andante.mus"
    if not src.is_file():
        pytest.skip("andante.mus absent")

    def render(folder: Path) -> "tuple[float, float, float]":
        out = asyncio.run(stream._render_psf(folder / "andante.mus"))
        try:
            secs, db, a, ch = _wav(out)
        finally:
            out.unlink()
        diff = sum(abs(a[i] - a[i + 1]) for i in range(0, len(a) - 1, 2)) / max(1, len(a) // 2)
        return secs, db, diff

    both = tmp_path / "both"
    both.mkdir()
    shutil.copyfile(src, both / "andante.mus")
    shutil.copyfile(src.with_suffix(".str"), both / "andante.STR")     # (any case)
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copyfile(src, alone / "andante.mus")
    s1, db1, d1 = render(both)
    s2, db2, d2 = render(alone)
    assert 7.5 <= s1 <= 9.5 and db1 > -30 and d1 > 5, (s1, db1, d1)   # (measured ~50)
    assert db2 > -30 and d2 < 1, (db2, d2)


# ── 5 / 6. xmprender: libxmp modules, converted packed modules ────────────────

_XMP_FOLDERS = {
    "archimedestracker": "Archimedes Tracker", "coconizer": "Coconizer", "flextrax": "FlexTrax",
    "funktracker": "Funktracker", "liquidtracker": "Liquid Tracker", "liquidtrackerno": "Liquid Tracker",
    "megatracker": "Megatracker", "titanicsplayer": "Titanics Player", "stim": "STIM",
    "thedarkdemon": "The Dark Demon", "eurekapacker": "Eureka Packer",
}


@needs_np
@needs_xmp
@pytest.mark.parametrize("folder,fmt", sorted(_XMP_FOLDERS.items()))
def test_libxmp_modules_scan_with_the_length_they_render(folder, fmt, xmp_path):
    f = _files(folder)[0]
    m = md.extract(f, "x").model_dump()
    assert m["format"] == fmt and m["duration"] > 5 and m["channels"], m
    assert stream._render_ident(str(f), m) == (f.suffix.lower(), False)
    assert stream._own_engine(f) == ("xmp", "xmprender", fmt)
    out = asyncio.run(stream._render_psf(f))
    try:
        secs, db, _a, ch = _wav(out)
    finally:
        out.unlink()
    assert abs(secs - m["duration"]) < 0.05 and db > -30 and ch == 2, (secs, m["duration"], db)


@needs_np
@needs_xmp
def test_a_packed_module_converts_and_renders(xmp_path):
    f = NP / "benreplay/Iris - Noise From Heaven 1 - Pep.ben"
    if not f.is_file():
        pytest.skip("Ben Replay test file absent")
    m = md.extract(f, "x").model_dump()
    assert m["format"] == "Ben Replay" and m["duration"] > 20 and m["instruments"], m
    out = asyncio.run(stream._render_psf(f))
    try:
        secs, db, _a, _ch = _wav(out)
    finally:
        out.unlink()
    assert abs(secs - m["duration"]) < 0.05 and db > -30, (secs, db)
    assert not list(Path(stream._rtmp()).glob("tmp*.mod"))     # (the converted module removed)


_PW_FOLDERS = ("benreplay", "binarypacker", "devilsreplay", "gnoipacker", "gnuprotectedmodule",
               "gnuplayer", "hcdprotector", "newtronpacker10", "newtronpacker20", "perfectsong1",
               "perfectsong2", "modulepatterncompressor", "noisetrackercompressed", "theplayer22a",
               "theplayer30a")


@needs_np
@needs_xmp
@pytest.mark.parametrize("folder", _PW_FOLDERS)
def test_every_converted_packed_module_loads_in_xmprender(folder, xmp_path):
    """libxmp loads every converted test module and measures its length (a
    converted module it refused — The Player's out-of-range finetune — scanned
    as 0 s)."""
    for f in _files(folder):
        m = md.extract(f, "x").model_dump()
        assert m["format"] == md.PW_EXT_NAMES[f.suffix.lower()] and m["duration"] > 5, (f.name, m)


def test_files_under_these_suffixes_that_arent_modules_are_skipped_quietly(tmp_path, xmp_path):
    """A ``.dev`` / ``.gnu`` / ``.flx`` that is no module — text, a FLIC
    animation — is skipped like an unplayed suffix (no stub, no error)."""
    for name, body in (("notes.dev", b"just some text\n" * 50), ("x.gnu", bytes(5000)),
                       ("anim.flx", b"\x00\x10\x00\x00\x12\xaf" + bytes(2000))):
        p = tmp_path / name
        p.write_bytes(body)
        if name.endswith(".flx") and not XMPRENDER:
            continue
        with pytest.raises(md.NotPlayableHere):
            md.extract(p, "x")


def test_every_new_suffix_is_scanned_and_companions_are_not():
    for name in ("a.xmf", "a.dsym", "a.mdl", "a.j2b", "a.mo3", "a.mptm", "a.wma", "a.mp2", "a.m2a",
                 "a.mp1", "a.oga", "a.8svx", "a.16sv", "a.ptm", "a.md2", "a.chp", "a.musx",
                 "a.coco", "a.fnk", "a.liq", "a.mgt", "a.tip", "a.sti", "a.tdd", "a.eureka",
                 "a.ben", "a.nw2", "a.p22a", "a.mus"):
        ext = "." + name.rsplit(".", 1)[1]
        if ext in md._OPENMPT_OPTIONAL and ext not in md.OPENMPT_EXTRA_EXTS:
            continue
        assert md.is_supported_music_name(name), name
    for name in ("a.str", "a.sam"):
        assert not md.is_supported_music_name(name), name


@needs_np
@needs_sid
@pytest.mark.parametrize("rel,end", [("sidplay/hells angel.mus", 210.2),
                                     ("sidplay-stereo/andante.mus", 200.0)])
def test_a_sidplayer_song_plays_to_its_end(rel, end):
    """The render stops 3 s after the song falls silent — not at the SID
    duration setting (180 s; these end at about 210 / 200 s), nor at the
    30-minute cap for a stereo song whose second SID leaves a constant
    level — and the play stores that length."""
    src = NP / rel
    if not src.is_file():
        pytest.skip(f"{rel} absent")
    out = asyncio.run(stream._render_psf(src))
    try:
        secs, db, _a, _ch = _wav(out)
    finally:
        out.unlink()
    assert end < secs < end + 8 and db > -30, (secs, db)
    assert md.extract(src, "x").duration == 0.0          # (measured by the first play)


def test_silence_end_counts_only_after_audio():
    import array as _array
    rate, ch = 1000, 2
    quiet = _array.array("h", [0] * (rate * ch * 5)).tobytes()
    loud = _array.array("h", [3000, -3000] * (rate * 2)).tobytes()
    st = {"heard": False, "quiet": 0}
    assert stream._silence_end(quiet, st, ch, rate, 3.0) is None      # (silence before music)
    cut = stream._silence_end(loud + quiet, st, ch, rate, 3.0)
    assert cut is not None and len(loud) < cut <= len(loud) + 4 * rate * ch * 2
    # a constant level after the music (a second SID's DC offset: 69 / 39) is silence too
    dc = _array.array("h", [69, 39] * (rate * 5)).tobytes()
    st = {"heard": False, "quiet": 0}
    assert stream._silence_end(loud + dc, st, ch, rate, 3.0) is not None


def test_xmprender_measures_and_renders_one_pass():
    """``xmp_info`` and the render use the same arguments — one pass (-l 1)
    of the first sequence, the 30-minute cap."""
    args = md.xmp_render_args()
    assert args[args.index("-l") + 1] == "1" and args[args.index("-m") + 1] == str(md.XMP_MAX_S)


def test_packed_modules_arent_listed_without_xmprender(tmp_path, monkeypatch):
    f = NP / "benreplay/Iris - Noise From Heaven 1 - Pep.ben"
    if not f.is_file():
        pytest.skip("Ben Replay test file absent")
    monkeypatch.setattr(md, "_find_atari_binary", lambda *a: None)
    with pytest.raises(md.NotPlayableHere):
        md.extract(f, "x")


def test_a_file_that_isnt_an_iff_sample_isnt_listed(tmp_path):
    for name in ("junk.8svx", "junk.16sv"):
        p = tmp_path / name
        p.write_bytes(b"RIFF" + bytes(200))
        with pytest.raises(ValueError):
            md.extract(p, "x")


def test_a_cast_sidplayer_song_uses_the_play_cache_key():
    from types import SimpleNamespace
    from soniqboom.api.cast_stream import _ext_for
    from soniqboom.core.cast_render import rendered_cache_key
    t = SimpleNamespace(id="abc", format=md.C64_SIDPLAYER_FORMAT, path="/x/a.mus")
    ext = _ext_for("/x/a.mus", t)
    assert ext == stream._SIDPLAYER_ROUTE
    assert rendered_cache_key("abc", ext) == f"abc__r1__v{stream._own_variant('sidplayer')}"


def test_modland_lists_x_tracker_dsmi_and_deflemask_dmf():
    """Modland's ``.dmf`` folders are all listed: X-Tracker and DSMI Compact
    (libopenmpt), DefleMask (Furnace — ``tests/test_deflemask.py``)."""
    if ".dmf" not in md.OPENMPT_EXTRA_EXTS:
        pytest.skip("libopenmpt here doesn't read .dmf")
    from soniqboom.core import modland_catalog as mc
    assert mc._classify("Deflemask/DevEd/7_grand_cop.dmf") == mc.KIND_MUSIC
    assert mc._classify("X-Tracker/Knuddel/one.dmf") == mc.KIND_MUSIC
    assert mc._classify("DSMI Compact/Ariel Gross/tronic-gameover.dmf") == mc.KIND_MUSIC
