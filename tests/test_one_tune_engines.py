# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GoatTracker 2 (gt2reloc + sidplayfp), FamiTracker (Furnace), SunVox
(sunvoxrender) and MGSDRV (kssrender with libkss's MGSDRV driver): the
headers the scan reads, the routing (the one-tune render route — a
GoatTracker ``.sng`` / FamiTracker ``.ftm`` by content, never uade or
libopenmpt), the Modland catalogue, the refusal rules and — where the
renderers are built (``SB_GT2RELOC`` / ``SB_SUNVOXRENDER`` / ``SB_FURNACE`` /
``SB_KSSRENDER``, else on PATH; kssrender must have MGSDRV) — real renders and
plays of the reference files (``internal/testdata/{goattracker,famitracker,
sunvox,mgsdrv}``)."""
from __future__ import annotations

import array
import asyncio
import os
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from soniqboom.core import metadata as md

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "internal/testdata"
GOAT, FAMI, SUNVOX, MGS = (DATA / d for d in ("goattracker", "famitracker", "sunvox", "mgsdrv"))
RJP_SNG = DATA / "uade/Richard Joseph/cannon fodder 2-morbase.sng"


def _bin(env: str, name: str) -> "str | None":
    p = os.environ.get(env) or shutil.which(name)
    return p if p and os.path.exists(p) else None


def _kss_has_mgsdrv(p: "str | None") -> bool:
    """A kssrender built with MGSDRV (1.2 on: ``-i`` measures; a build without
    the driver plays an MGS song as 3 s of silence)."""
    if not p or not (MGS / "SC-OPEN.MGS").is_file():
        return False
    try:
        r = subprocess.run([p, "-i", "-l", "2", "-m", "180", str(MGS / "SC-OPEN.MGS")],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and b"seconds=" in r.stdout and b"seconds=3.0" not in r.stdout


GT2RELOC = _bin("SB_GT2RELOC", "gt2reloc")
SIDPLAYFP = _bin("SB_SIDPLAYFP", "sidplayfp")
FURNACE = _bin("SB_FURNACE", "furnace-headless") or _bin("SB_FURNACE", "furnace")
SUNVOXRENDER = _bin("SB_SUNVOXRENDER", "sunvoxrender")
KSSRENDER = _bin("SB_KSSRENDER", "kssrender")
KSS_MGS = _kss_has_mgsdrv(KSSRENDER)

needs_goat = pytest.mark.skipif(not (GT2RELOC and SIDPLAYFP and GOAT.is_dir()),
                                reason="gt2reloc / sidplayfp / internal/testdata/goattracker absent")
needs_furnace = pytest.mark.skipif(not (FURNACE and FAMI.is_dir()),
                                   reason="furnace / internal/testdata/famitracker absent")
needs_sunvox = pytest.mark.skipif(not (SUNVOXRENDER and SUNVOX.is_dir()),
                                  reason="sunvoxrender / internal/testdata/sunvox absent")
needs_mgs = pytest.mark.skipif(not KSS_MGS, reason="kssrender with MGSDRV / test data absent")


@pytest.fixture
def renderers(monkeypatch):
    from soniqboom.config import settings
    for attr, val in (("gt2reloc_path", GT2RELOC), ("sidplayfp_path", SIDPLAYFP),
                      ("furnace_path", FURNACE), ("sunvoxrender_path", SUNVOXRENDER),
                      ("kssrender_path", KSSRENDER)):
        monkeypatch.setattr(settings, attr, val or "")
    monkeypatch.setattr(settings, "sid_default_duration", 180)


def _stats(path: Path) -> "tuple[float, int, int]":
    """(seconds, peak, channels) of a 16-bit WAV."""
    with wave.open(str(path)) as w:
        n, rate, ch = w.getnframes(), w.getframerate(), w.getnchannels()
        a = array.array("h")
        a.frombytes(w.readframes(n))
    return n / rate, max(map(abs, a), default=0), ch


def _rms(path: Path, start_s: float, end_s: float) -> float:
    with wave.open(str(path)) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        w.setpos(int(start_s * rate))
        a = array.array("h")
        a.frombytes(w.readframes(int((end_s - start_s) * rate)))
    return (sum(x * x for x in a) / max(1, len(a))) ** 0.5


# ── Headers ──────────────────────────────────────────────────────────────────

def _gts(ident=b"GTS5", title=b"Song", author=b"Me", copy=b"2003 Us", tunes=3):
    pad = lambda b: b.ljust(32, b"\0")          # noqa: E731
    return ident + pad(title) + pad(author) + pad(copy) + bytes([tunes]) + b"\0" * 64


import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("render_selftest", ROOT / "scripts/render_selftest.py")
_SELFTEST = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_SELFTEST)


def _one_note_song() -> bytes:
    """A GTS5 song: one note of one instrument on all three channels."""
    pad = lambda b: b.ljust(32, b"\0")          # noqa: E731
    out = b"GTS5" + pad(b"x") + pad(b"") + pad(b"") + bytes([1])
    out += bytes([2, 0, 0xFF, 0]) * 3
    out += bytes([1, 0x0A, 0x9A, 1, 0, 0, 0, 0, 2, 0x09]) + b"t".ljust(16, b"\0")
    out += bytes([2, 0x21, 0xFF, 0x00, 0x00, 0, 0, 0])
    return out + bytes([1, 2, 0x90, 1, 0, 0, 0xFF, 0, 0, 0])


def test_a_goattracker_song_without_notes_is_found_by_its_patterns():
    """``goattracker_has_notes`` walks every GoatTracker layout to the
    patterns: each reference song (GTS!, GTS2, GTS5) and the synthesized
    ones have a note; an empty song, one cut before its patterns and a
    song whose patterns only rest don't."""
    for f in sorted(GOAT.glob("*.sng")):
        assert md.goattracker_has_notes(f.read_bytes()), f.name
    assert md.goattracker_has_notes(_SELFTEST.goat_tune())
    assert md.goattracker_has_notes(_one_note_song())
    assert not md.goattracker_has_notes(b"GTS5")
    assert not md.goattracker_has_notes(_SELFTEST.goat_tune()[:147])     # (before the patterns)
    assert not md.goattracker_has_notes(b"\x00junk" * 40)
    rests = _one_note_song()[:-8] + bytes([0xBD, 1, 0, 0, 0xFF, 0, 0, 0])
    assert not md.goattracker_has_notes(rests)


def test_the_selftest_songs_are_read_as_their_formats():
    """The build gate's synthesized GoatTracker song and FamiTracker module
    are what the scan reads as those formats."""
    g = md.goattracker_header(_SELFTEST.goat_tune())
    assert g == {"title": "SoniqBoom selftest", "author": "", "copyright": "", "tunes": 1}
    f = md.famitracker_header(_SELFTEST.ftm_tune())
    assert f["title"] == "SoniqBoom selftest" and f["tracks"] == 1


def test_goattracker_header():
    h = md.goattracker_header(_gts())
    assert h == {"title": "Song", "author": "Me", "copyright": "2003 Us", "tunes": 3}
    for ident in (b"GTS!", b"GTS2", b"GTS3", b"GTS4"):
        assert md.goattracker_header(_gts(ident=ident))["title"] == "Song"
    assert md.goattracker_header(_gts(ident=b"GTI5")) is None      # an instrument file
    assert md.goattracker_header(_gts()[:90]) is None
    assert md.goattracker_header(_gts(tunes=0))["tunes"] == 1


def _ftm(title=b"Tune", author=b"Ann", copy=b"2011", tracks=2, header_ver=3):
    def block(bid, ver, body):
        return bid.ljust(16, b"\0") + struct.pack("<II", ver, len(body)) + body
    info = title.ljust(32, b"\0") + author.ljust(32, b"\0") + copy.ljust(32, b"\0")
    return (b"FamiTracker Module" + struct.pack("<I", 0x440)
            + block(b"PARAMS", 6, b"\0" * 13) + block(b"INFO", 1, info)
            + block(b"HEADER", header_ver, bytes([tracks - 1]) + b"A\0B\0")
            + b"END")


def test_famitracker_header():
    assert md.famitracker_header(_ftm()) == {"title": "Tune", "author": "Ann",
                                             "copyright": "2011", "tracks": 2}
    assert md.famitracker_header(_ftm(header_ver=1))["tracks"] == 1
    assert md.famitracker_header(_ftm()[:60])["title"] == ""      # (INFO cut off)
    assert md.famitracker_header(b"FTMN" + b"\0" * 100) is None


def test_mgs_title():
    assert md.mgs_title(b"MGS314\r\nHello  \tWorld\r\n\x1a\0\0") == "Hello World"
    assert md.mgs_title("MGS300\r\n日本\r\n\x1a".encode("cp932")) == "日本"
    assert md.mgs_title(b"MGS300\r\n\x1a") == ""
    assert md.mgs_title(b"KSCC....") == ""


# ── The scan: by content ─────────────────────────────────────────────────────

def _no_renderers(monkeypatch):
    monkeypatch.setattr(md, "_find_atari_binary", lambda *a: None)


def test_extract_by_content_without_renderers(tmp_path, monkeypatch):
    """Without the renderers: a GoatTracker song is listed at the SID
    duration (it loops), a FamiTracker module at 0 (its render is the
    length), a SunVox project / MGSDRV song at 0 (measured by its renderer);
    junk under these names is not listed."""
    _no_renderers(monkeypatch)
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 200)
    (tmp_path / "a.sng").write_bytes(_gts())
    (tmp_path / "b.ftm").write_bytes(_ftm())
    (tmp_path / "c.sunvox").write_bytes(b"SVOX\0\0\0\0VERS")
    (tmp_path / "d.mgs").write_bytes(b"MGS314\r\nA title\r\n\x1a" + b"\0" * 64)
    g = md.extract(tmp_path / "a.sng", "a").model_dump()
    assert (g["format"], g["title"], g["artist"], g["duration"], g["year"]) == (
        "GoatTracker", "Song", "Me", 200.0, 2003)
    assert g["genre"] == ["Chiptune", "C64"] and g["comment"] == "2003 Us"
    f = md.extract(tmp_path / "b.ftm", "b").model_dump()
    assert (f["format"], f["title"], f["artist"], f["duration"]) == ("FamiTracker", "Tune", "Ann", 0.0)
    s = md.extract(tmp_path / "c.sunvox", "c").model_dump()
    assert (s["format"], s["title"], s["duration"]) == ("SunVox", "c", 0.0)
    m = md.extract(tmp_path / "d.mgs", "d").model_dump()
    assert (m["format"], m["title"], m["duration"]) == ("MGSDRV", "A title", 0.0)
    for name in ("x.sunvox", "x.mgs", "x.ftm"):
        (tmp_path / name).write_bytes(b"\x01\x02junk" * 40)
        with pytest.raises(ValueError):
            md.extract(tmp_path / name, "j")
    for name in ("a.sng", "b.ftm", "c.sunvox", "d.mgs"):
        assert md.is_supported_music_name(name)
        assert md._wants_scene_md5(Path(name).suffix, name, {})


def test_a_face_the_music_ftm_is_still_libopenmpts(tmp_path):
    """An FTMN module with its samples is Face The Music (libopenmpt), where
    libopenmpt here reads the format; without them it is refused."""
    ftmn = b"FTMN" + b"\0" * 9 + b"\x01" + b"\0" * 200
    (tmp_path / "x.ftm").write_bytes(ftmn)
    if ".ftm" in md.OPENMPT_EXTRA_EXTS:
        assert md.extract(tmp_path / "x.ftm", "x").model_dump()["format"] == "Face The Music"
    else:
        with pytest.raises(ValueError, match="libopenmpt"):
            md.extract(tmp_path / "x.ftm", "x")
    (tmp_path / "y.ftm").write_bytes(b"FTMN" + b"\0" * 200)
    with pytest.raises(ValueError, match="without its samples" if ".ftm" in md.OPENMPT_EXTRA_EXTS
                       else "libopenmpt"):
        md.extract(tmp_path / "y.ftm", "y")


def test_files_this_install_cant_play_are_skipped_without_an_error(tmp_path, monkeypatch):
    """Without uade an Amiga ``.sng``, with a libopenmpt that doesn't read
    Face The Music an FTMN ``.ftm``, is no scan error: ``NotPlayableHere``,
    whose text the scanner skips quietly (``_not_playable_here``) — as it
    skips a suffix the install doesn't play.  A GoatTracker song and a
    FamiTracker module are still read, and a broken file is still an error."""
    from soniqboom.core import scanner, uade_formats
    monkeypatch.setattr(md, "_UADE_SUFFIX_EXTS", set())
    monkeypatch.setattr(md, "OPENMPT_EXTRA_EXTS", frozenset())
    monkeypatch.setattr(uade_formats, "classify", lambda name: None)
    (tmp_path / "amiga.sng").write_bytes(b"\x00\x01junk" * 40)
    (tmp_path / "ftmn.ftm").write_bytes(b"FTMN" + b"\0" * 9 + b"\x01" + b"\0" * 200)
    (tmp_path / "goat.sng").write_bytes(_gts())
    (tmp_path / "fami.ftm").write_bytes(_ftm())
    (tmp_path / "nosamples.ftm").write_bytes(b"FTMN" + b"\0" * 200)
    (tmp_path / "junk.ftm").write_bytes(b"\x01\x02junk" * 40)
    (tmp_path / "junk.sunvox").write_bytes(b"\x01\x02junk" * 40)
    for name in ("amiga.sng", "ftmn.ftm", "nosamples.ftm", "junk.ftm"):
        with pytest.raises(md.NotPlayableHere):
            md.extract(tmp_path / name, "x")
        _p, res, _sm, _lg = scanner._extract_one(tmp_path / name)
        assert isinstance(res, str) and scanner._not_playable_here(res), res
    for name in ("goat.sng", "fami.ftm"):
        _p, res, _sm, _lg = scanner._extract_one(tmp_path / name)
        assert not isinstance(res, str) and not scanner._not_playable_here(res)
    _p, res, _sm, _lg = scanner._extract_one(tmp_path / "junk.sunvox")
    assert isinstance(res, str) and not scanner._not_playable_here(res)
    # Where libopenmpt reads Face The Music, a broken .ftm is an error again.
    monkeypatch.setattr(md, "OPENMPT_EXTRA_EXTS", frozenset({".ftm"}))
    for name in ("nosamples.ftm", "junk.ftm"):
        _p, res, _sm, _lg = scanner._extract_one(tmp_path / name)
        assert isinstance(res, str) and not scanner._not_playable_here(res), name


def test_a_libopenmpt_upgrade_relists_archives(monkeypatch):
    """``.ftm`` is supported everywhere now (FamiTracker), so the archive
    listing fingerprint names libopenmpt's optional formats itself: an
    upgrade that adds Face The Music still re-lists archives."""
    from soniqboom.core import scanner
    monkeypatch.setattr(md, "OPENMPT_EXTRA_EXTS", frozenset())
    before = scanner._listing_fingerprint()
    monkeypatch.setattr(md, "OPENMPT_EXTRA_EXTS", frozenset({".ftm"}))
    assert scanner._listing_fingerprint() != before


def test_a_goattracker_song_with_an_amiga_name(tmp_path):
    """A GoatTracker song named in Amiga prefix form: inside an archive
    (listed ``sng.NAME.sng``) it is read and extracted for play as a ``.sng``
    — never handed to uade as a ZoundMonitor module; loose, the scan says to
    rename it.  An Amiga module with that name stays uade's."""
    import zipfile
    from soniqboom.api import stream
    from soniqboom.core import scanner, uade_formats
    if uade_formats.classify("sng.tune") is None:
        pytest.skip("uade's name tokens aren't installed here")
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("sng.tune", _SELFTEST.goat_tune())
        f.writestr("sng.amiga", b"\x00\x01junk" * 40)
    meta = scanner._extract_from_zip(f"{z}::sng.tune.sng", "t")
    assert meta.format == "GoatTracker" and meta.title == "SoniqBoom selftest"
    assert stream._member_is_goattracker(f"{z}::sng.tune.sng")
    assert not stream._member_is_goattracker(f"{z}::sng.amiga.sng")
    assert stream._render_ident(f"{z}::sng.tune.sng", meta)[0] == stream._GOAT_ROUTE
    (tmp_path / "sng.loose").write_bytes(_SELFTEST.goat_tune())
    with pytest.raises(ValueError, match="rename it loose.sng"):
        md.extract(tmp_path / "sng.loose", "l")


@needs_goat
@pytest.mark.asyncio
async def test_an_archived_goattracker_song_with_an_amiga_name_plays(tmp_path, renderers,
                                                                     monkeypatch):
    import zipfile
    from soniqboom.api import stream
    from soniqboom.config import settings
    from soniqboom.core import uade_formats
    if uade_formats.classify("sng.tune") is None:
        pytest.skip("uade's name tokens aren't installed here")
    monkeypatch.setattr(stream, "_zip_extract_dir", lambda: tmp_path / "x")
    (tmp_path / "x").mkdir()
    monkeypatch.setattr(settings, "sid_default_duration", 10)
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("sng.tune", _SELFTEST.goat_tune())
    path = await stream._get_or_extract_zip_member(f"{z}::sng.tune.sng", "tid")
    assert path is not None and path.suffix == ".sng"
    out = await stream._render_psf(path)
    try:
        secs, peak, _ch = _stats(out)
        assert secs == pytest.approx(10.0, abs=0.01) and peak > 1000
    finally:
        out.unlink(missing_ok=True)


def test_furnace_runs_without_xdg_config_home(monkeypatch, tmp_path):
    """Furnace puts its config folder in XDG_CONFIG_HOME before HOME: every
    run with a temp HOME drops the variable."""
    from soniqboom.api import admin
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    env = admin._furnace_env(str(tmp_path / "home"))
    assert env["HOME"] == str(tmp_path / "home") and "XDG_CONFIG_HOME" not in env
    src = (ROOT / "soniqboom/api/stream.py").read_text()
    body = src.split("async def _render_famitracker")[1].split("\nasync def ")[0]
    assert 'k != "XDG_CONFIG_HOME"' in body
    assert 'k != "XDG_CONFIG_HOME"' in (ROOT / "scripts/render_selftest.py").read_text()
    assert "env -u XDG_CONFIG_HOME" in (ROOT / "soniqboom/native/furnace/build.sh").read_text()
    assert "env -u XDG_CONFIG_HOME" in (ROOT / "install.sh").read_text()


def test_sunvox_is_a_tracker_in_the_galaxy():
    from soniqboom.core import retro
    assert retro.coarse_family("SunVox") == "trackers"
    assert retro.coarse_family("GoatTracker") == "chiptune"
    assert retro.coarse_family("FamiTracker") == "chiptune"
    assert retro.coarse_family("MGSDRV") == "chiptune"


@pytest.mark.skipif(not RJP_SNG.is_file() or not shutil.which("uade123"),
                    reason="uade123 or the Richard Joseph test module absent")
def test_an_amiga_sng_is_still_uades():
    t = md.extract(RJP_SNG, "rj").model_dump()
    assert t["format"] != "GoatTracker" and t["genre"] == ["Amiga", "Module"]


@pytest.mark.skipif(not GOAT.is_dir(), reason="internal/testdata/goattracker absent")
@pytest.mark.parametrize("name, title, author, year", [
    ("dojo.sng", "Dojo", "Cadaver", 2002),                       # GTS2 (Modland)
    ("sanction.sng", "On a sanction from CIA", "Cadaver", 2005),  # GTS5
    ("nintendo-style.sng", "Nintendo-style", "Cadaver", 2002),   # GTS! (1.x)
])
def test_reference_goattracker_scan(name, title, author, year, monkeypatch):
    _no_renderers(monkeypatch)
    t = md.extract(GOAT / name, "g").model_dump()
    assert (t["format"], t["title"], t["artist"]) == ("GoatTracker", title, author)
    assert t.get("year") == year


@pytest.mark.skipif(not FAMI.is_dir(), reason="internal/testdata/famitracker absent")
def test_reference_famitracker_scan(monkeypatch):
    _no_renderers(monkeypatch)
    t = md.extract(FAMI / "Square eyes.ftm", "f").model_dump()
    assert (t["format"], t["title"], t["artist"], t["duration"]) == (
        "FamiTracker", "Shellac", "Square eyes", 0.0)


@needs_sunvox
@pytest.mark.parametrize("name, title, secs", [
    ("out of time.sunvox", "adsn - out of time", 491.25),
    ("tiny tune.sunvox", "Tiny Tune. by NightRadio 2008", 16.64),
])
def test_reference_sunvox_scan(name, title, secs, renderers):
    t = md.extract(SUNVOX / name, "s").model_dump()
    assert (t["format"], t["title"], t["duration"]) == ("SunVox", title, secs)


@needs_mgs
@pytest.mark.parametrize("name, title, secs", [
    ("SC-OPEN.MGS", "Falcom SORCERIAN Opening", 64.1),
    ("Mario-Net - Theme.MGS", "The thme of MARIO-NET No.2 By (YMD)A / Ain", 133.1),
])
def test_reference_mgs_scan(name, title, secs, renderers):
    t = md.extract(MGS / name, "m").model_dump()
    assert (t["format"], t["duration"]) == ("MGSDRV", secs)
    assert t["title"].startswith(title.split(" ")[0])


# ── Routing ─────────────────────────────────────────────────────────────────

def test_routing_names_them_by_format():
    from soniqboom.api import stream
    goat = {"path": "/m/a.sng", "format": "GoatTracker"}
    fami = {"path": "/m/b.ftm", "format": "FamiTracker"}
    assert stream._render_ident(goat["path"], goat) == (stream._GOAT_ROUTE, False)
    assert stream._render_ident(fami["path"], fami) == (stream._FAMI_ROUTE, False)
    assert stream._render_ident("/m/c.sunvox", {"format": "SunVox"}) == (".sunvox", False)
    assert stream._render_ident("/m/d.mgs", {"format": "MGSDRV"}) == (".mgs", False)
    for e in (stream._GOAT_ROUTE, stream._FAMI_ROUTE, ".sunvox", ".mgs"):
        assert e in stream._PSF_STREAM_EXTS
    # Face The Music stays a tracker module, an Amiga .sng uade's
    ext, _ = stream._render_ident("/m/x.ftm", {"format": "Face The Music"})
    assert ext == ".ftm" and ext not in stream._PSF_STREAM_EXTS
    ext, _ = stream._render_ident("/m/x.sng", {"format": "Richard Joseph", "genre": ["Amiga", "Module"]})
    assert ext == ".sng" and ext not in stream._PSF_STREAM_EXTS
    keys = {stream.renderer_key_for(t) for t in (
        {"id": "1", **goat}, {"id": "2", **fami},
        {"id": "3", "path": "/m/c.sunvox", "format": "SunVox"},
        {"id": "4", "path": "/m/d.mgs", "format": "MGSDRV"})}
    assert keys == {"gt2reloc", "furnace", "sunvoxrender", "kssrender"}


def test_cache_variants(monkeypatch):
    from soniqboom.api import stream
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 180)
    got = {e: stream._route_variant(Path(f"x{e}")) for e in (".sng", ".mgs", ".sunvox", ".ftm")}
    assert got == {".sng": "gt180", ".mgs": "mgs180", ".sunvox": "sv1", ".ftm": "ft1"}
    monkeypatch.setattr(settings, "sid_default_duration", 240)
    assert stream._route_variant(Path("x.sng")) == "gt240"
    assert stream._route_variant(Path("x.mgs")) == "mgs240"
    assert asyncio.run(stream._psf_variants_known(stream._GOAT_ROUTE, "/m/x.sng", True)) == ("gt240",)
    assert {stream._route_renderer(Path(f"x{e}")) for e in (".sng", ".mgs", ".sunvox", ".ftm")} == {
        "gt2reloc", "kssrender", "sunvoxrender", "furnace"}
    assert stream._psf_render_plan(Path("x.sunvox")) == (0.0, 0.0)


def test_gt2reloc_workdir_fits_its_name_buffer():
    from soniqboom.api import stream
    d = stream._goat_workdir()
    try:
        assert len(str(d / "s.sng").encode()) <= 59 and d.is_dir() and not any(d.iterdir())
    finally:
        shutil.rmtree(d)


@pytest.mark.asyncio
@pytest.mark.parametrize("ext, setting, binary", [
    (".sng", "gt2reloc_path", "gt2reloc"), (".ftm", "furnace_path", "furnace"),
    (".sunvox", "sunvoxrender_path", "sunvoxrender"), (".mgs", "kssrender_path", "kssrender"),
])
async def test_no_renderer_is_a_501(tmp_path, monkeypatch, ext, setting, binary):
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.config import settings
    monkeypatch.setattr(settings, setting, "")
    monkeypatch.setattr(stream, "_find_renderer", lambda p, name: None)
    f = tmp_path / f"x{ext}"
    f.write_bytes(b"x")
    with pytest.raises(HTTPException) as e:
        await stream._render_psf(f)
    assert e.value.status_code == 501 and binary in e.value.detail


def test_a_goattracker_render_follows_the_sid_fidelity_settings(monkeypatch):
    from soniqboom.api import stream
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 180)
    for attr, val in (("sid_model", "auto"), ("sid_model_force", False), ("sid_filter", True),
                      ("sid_filter_curve", -1.0), ("sid_digiboost", False)):
        monkeypatch.setattr(settings, attr, val)
    assert stream._route_variant(Path("x.sng")) == "gt180"
    monkeypatch.setattr(settings, "sid_model", "8580")
    monkeypatch.setattr(settings, "sid_filter", False)
    assert stream._route_variant(Path("x.sng")) == "gt180m8580nf"
    assert stream._route_variant(Path("x.mgs")) == "mgs180"      # (not a SID render)


@pytest.mark.asyncio
async def test_an_old_kssrender_is_a_501_for_mgsdrv(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.config import settings
    old = tmp_path / "kssrender"
    old.write_text("#!/bin/sh\necho 'kssrender 1.1 (libkss 2ed958823237)'\n")
    old.chmod(0o755)
    monkeypatch.setattr(settings, "kssrender_path", str(old))
    (tmp_path / "x.mgs").write_bytes(b"MGS314\r\nT\r\n\x1a" + b"\0" * 40)
    with pytest.raises(HTTPException) as e:
        await stream._render_psf(tmp_path / "x.mgs")
    assert e.value.status_code == 501 and "1.2" in e.value.detail
    assert stream._kss_plays_mgs(str(old)) is False


def test_sunvoxrender_runs_in_the_render_temp_folder():
    from soniqboom.core import metadata as md2, render_tmp
    assert md2.sunvox_env()["TMPDIR"] == render_tmp.tmp_dir()


def test_stale_gt2reloc_folders_are_swept(tmp_path):
    import time as _time
    from soniqboom.api import stream
    old, new = tmp_path / "sbgtOLD", tmp_path / "sbgtNEW"
    old.mkdir()
    new.mkdir()
    (old / "s.sng").write_bytes(b"x")
    past = _time.time() - 7200
    os.utime(old, (past, past))
    stream._sweep_goat_workdirs(str(tmp_path))
    assert not old.exists() and new.exists()


def test_lhasa_symlinks_are_never_members(tmp_path, monkeypatch):
    """The lha (lhasa) fallback extracts an archive's symlinks too: those are
    no member (one would read the file it points at)."""
    from soniqboom.core import archive
    secret = tmp_path / "secret.key"
    secret.write_bytes(b"KEY")

    def fake_lha(cmd, **kw):
        where = cmd[1].split("=", 1)[1]
        (Path(where) / "tune.mod").write_bytes(b"M" * 1084)
        os.symlink(secret, Path(where) / "x.mp3")
        os.symlink("../" * 12 + str(secret).lstrip("/"), Path(where) / "y.mod")

    monkeypatch.setattr(archive, "_LHA_BIN", "lha")
    monkeypatch.setattr(archive.forksafe, "run", fake_lha)
    a = archive._LhaCliArchive(str(tmp_path / "a.lha"))
    try:
        assert sorted(a.namelist()) == ["tune.mod"]
        with pytest.raises(KeyError):
            a.read("x.mp3")
    finally:
        a.close()


def test_the_health_check_finds_furnace_headless(monkeypatch, tmp_path):
    from soniqboom.api import admin
    fake = tmp_path / "furnace-headless"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(admin.shutil, "which",
                        lambda n: str(fake) if n == "furnace-headless" else None)
    assert admin.which_renderer("furnace") == str(fake)
    monkeypatch.setattr(admin.shutil, "which",
                        lambda n: "/usr/bin/furnace" if n == "furnace" else None)
    assert admin.which_renderer("furnace") == "/usr/bin/furnace"


def test_cast_routes_the_one_tune_formats_like_a_play(monkeypatch):
    from soniqboom.api import cast_stream, stream
    from soniqboom.config import settings
    from soniqboom.core import cast_render
    from soniqboom.core.conversion_cache import _cache_key
    monkeypatch.setattr(settings, "sid_default_duration", 180)
    for attr, val in (("sid_model", "auto"), ("sid_filter", True), ("sid_filter_curve", -1.0),
                      ("sid_digiboost", False)):
        monkeypatch.setattr(settings, attr, val)
    route = cast_render.one_tune_route
    assert route("a", "/m/a.ftm", {"format": "FamiTracker"}) == ".famitracker"
    assert route("a", "/m/a.ftm", {"format": "Face The Music"}) is None
    assert route("a", "/m/a.sng", {"format": "GoatTracker"}) == ".goattracker"
    assert route("a", "/m/a.sng", {"format": "Richard Joseph", "genre": ["Amiga", "Module"]}) is None
    assert route("a", "/m/a.sunvox", {"format": "SunVox"}) == ".sunvox"
    assert route("a", "/m/a.mgs", {"format": "MGSDRV"}) == ".mgs"
    for e, variant in ((".famitracker", "ft1"), (".goattracker", "gt180"), (".sunvox", "sv1"),
                       (".mgs", "mgs180"), (".mdx", "mdx180")):
        assert cast_render.is_rendered_format(e)
        assert cast_render.rendered_cache_key("t1", e) == _cache_key("t1", "psf", subsong=0,
                                                                     variant=variant)
        assert stream._route_variant(Path("x" + {".famitracker": ".ftm",
                                                 ".goattracker": ".sng"}.get(e, e))) == variant

    class _Rec:
        id, format, genre = "t2", "FamiTracker", ["Chiptune"]
    assert cast_stream._ext_for("/m/a.ftm", _Rec()) == ".famitracker"


def test_modland_lists_amiga_sng_and_ftmn_only_where_they_play(monkeypatch):
    """A host without uade (a slim image) or with a libopenmpt that doesn't
    read Face The Music lists neither format's Modland files; GoatTracker's
    and FamiTracker's folders are listed everywhere."""
    from soniqboom.core import modland_catalog as mc
    monkeypatch.setattr(md, "_UADE_SUFFIX_EXTS", set())
    monkeypatch.setattr(md, "OPENMPT_EXTRA_EXTS", frozenset())
    assert mc._classify("Richard Joseph/Cannon Fodder/x.sng") != mc.KIND_MUSIC
    assert mc._classify("ZoundMonitor/Someone/x.sng") != mc.KIND_MUSIC
    assert mc._classify("Face The Music/Someone/x.ftm") != mc.KIND_MUSIC
    assert mc._classify("GoatTracker 2/Cadaver/x.sng") == mc.KIND_MUSIC
    assert mc._classify("FamiTracker/Someone/x.ftm") == mc.KIND_MUSIC


# ── Refusals ────────────────────────────────────────────────────────────────

def test_refusal_rules():
    from soniqboom.core import unplayable as u
    assert u.classify("FamiTracker", 1, "[ERROR] could not open file! (incomplete file)") == (
        "furnace", "can't load this file (incomplete file)", True)
    assert u.classify("FamiTracker", 1, "[ERROR] could not open file! (Permission denied)") is None
    assert u.classify("SunVox", 1, "sunvoxrender: not a SunVox project this library can "
                                   "load: x")[0] == "sunvoxrender"
    assert u.classify("SunVox", 1, "sunvoxrender: the project is empty (no pattern plays): x")[1] \
        == "finds nothing to play in this project"
    assert u.classify("MGSDRV", 1, "kssrender: not a KSS file this build can play: x")[0] == "kssrender"
    assert u.KIND_KEYS["GoatTracker"] == "sidplayfp"              # (the render step that can fail)
    assert u.classify("SunVox", 1, "sunvoxrender: can't write /tmp/x.wav") is None
    for key in ("gt2reloc", "furnace", "sunvoxrender", "kssrender"):
        assert key in u.NAMES


def test_renderer_versions_read_through_furnaces_log():
    from soniqboom.api import admin
    furnace = ("\x1b[1;34m[debug]\x1b[m config path: /h/.config/furnace\n" * 6
               + "\x1b[1;32m[info]\x1b[m creating default config.\n"
               + "Furnace version 0.6.8.3.\n\nlicensed under the GNU General Public License\n")
    assert admin.renderer_version("furnace", furnace)[0] == "0.6.8.3"
    assert admin.renderer_version("sunvoxrender", "sunvoxrender 1.0 (SunVox Library 2.1.4d)\n")[0] \
        == "1.0"
    assert admin.renderer_version("kssrender", "kssrender 1.2 (libkss 2ed958823237)\n")[0] == "1.2"


# ── The Modland catalogue ───────────────────────────────────────────────────

@pytest.mark.parametrize("rel, fmt, genre", [
    ("GoatTracker 2/Cadaver/on a sanction from cia.sng", "GoatTracker", ["Chiptune", "C64"]),
    ("GoatTracker/Cadaver/nintendo-style.sng", "GoatTracker", ["Chiptune", "C64"]),
    ("FamiTracker/Aleksa9/short_boss_theme.ftm", "FamiTracker", ["Chiptune"]),
    ("SunVox/NightRadio/tiny tune.sunvox", "SunVox", ["Tracker"]),
    ("MGSDRV/Pa/Star.MGS", "MGSDRV", ["Chiptune"]),
])
def test_modland_lists_and_names_them(rel, fmt, genre):
    from soniqboom.core import modland_catalog as mc
    assert mc._classify(rel) == mc.KIND_MUSIC
    assert mc.format_for(rel) == fmt
    assert mc.family_genre(rel) == genre


def test_modland_still_skips_the_unplayed_msx_and_pc98_formats():
    from soniqboom.core import modland_catalog as mc
    for rel in ("MuSICA/Convex/Funky Fingers.BGM", "PMD/M. Kajihara/For Graunseed/calm milly.m",
                "MoonBlaster/Anbert Smedes/Forever [50Hz].MBM"):
        assert mc._classify(rel) != mc.KIND_MUSIC, rel


# ── Real renders ─────────────────────────────────────────────────────────────

@needs_goat
@pytest.mark.asyncio
async def test_goattracker_renders_the_sid_duration_with_a_fade(renderers):
    from soniqboom.api import stream
    out = await stream._render_psf(GOAT / "sanction.sng")
    try:
        secs, peak, ch = _stats(out)
        assert secs == pytest.approx(180.0, abs=0.01) and ch == 2 and peak > 1000
        assert _rms(out, 30, 40) > 300 and _rms(out, 179, 180) < _rms(out, 160, 170) / 3
    finally:
        out.unlink(missing_ok=True)


@needs_goat
@pytest.mark.asyncio
async def test_a_goattracker_1x_song_renders(renderers):
    from soniqboom.api import stream
    out = await stream._render_psf(GOAT / "nintendo-style.sng")
    try:
        assert _stats(out)[1] > 1000
    finally:
        out.unlink(missing_ok=True)


@needs_goat
@pytest.mark.asyncio
async def test_a_song_gt2reloc_cant_convert_is_a_refusal(tmp_path, renderers):
    from soniqboom.api import stream
    from soniqboom.core import unplayable
    for body in (b"GTS5", b"GTS5" + bytes(range(40)), (GOAT / "sanction.sng").read_bytes()[:150]):
        bad = tmp_path / "bad.sng"
        bad.write_bytes(body)
        with pytest.raises(unplayable.RenderRefusal) as e:
            await stream._render_psf(bad)
        assert e.value.status_code == 422 and e.value.renderer == "gt2reloc"


@needs_goat
@pytest.mark.asyncio
async def test_a_one_instrument_song_renders(tmp_path, renderers, monkeypatch):
    """A real song with one plain instrument (gt2reloc packs it to the same
    3 instrument bytes as an empty song — the empty-song check reads the
    patterns instead) and one with a single note both play."""
    from soniqboom.api import stream
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 10)
    for song, body in (("tune.sng", _SELFTEST.goat_tune()), ("note.sng", _one_note_song())):
        (tmp_path / song).write_bytes(body)
        out = await stream._render_psf(tmp_path / song)
        try:
            secs, peak, ch = _stats(out)
            assert secs == pytest.approx(10.0, abs=0.01) and peak > 1000, song
        finally:
            out.unlink(missing_ok=True)


@needs_furnace
@pytest.mark.asyncio
@pytest.mark.parametrize("name, secs", [("Square eyes.ftm", 145.728),
                                        ("short_boss_theme.ftm", 21.9)])
async def test_famitracker_renders(name, secs, renderers, tmp_path, monkeypatch):
    from soniqboom.api import stream
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out = await stream._render_psf(FAMI / name)
    try:
        got, peak, ch = _stats(out)
        assert got == pytest.approx(secs, abs=0.05) and ch == 2 and peak > 1000
    finally:
        out.unlink(missing_ok=True)
    assert not (tmp_path / "home").exists()             # (Furnace got a HOME of its own)


@pytest.mark.skipif(not FURNACE, reason="furnace absent")
@pytest.mark.asyncio
async def test_the_selftest_famitracker_module_renders(tmp_path, renderers):
    """The build gate's synthesized module plays through the server's
    FamiTracker route: its 64 rows at speed 6 (6.4 s), audible."""
    from soniqboom.api import stream
    (tmp_path / "selftest.ftm").write_bytes(_SELFTEST.ftm_tune())
    out = await stream._render_psf(tmp_path / "selftest.ftm")
    try:
        secs, peak, ch = _stats(out)
        assert ch == 2 and peak > 1000 and 6.0 < secs < 14.0
    finally:
        out.unlink(missing_ok=True)


@needs_furnace
@pytest.mark.asyncio
async def test_a_broken_famitracker_module_is_a_refusal(tmp_path, renderers):
    from soniqboom.api import stream
    from soniqboom.core import unplayable
    bad = tmp_path / "bad.ftm"
    bad.write_bytes((FAMI / "Square eyes.ftm").read_bytes()[:40])
    with pytest.raises(unplayable.RenderRefusal) as e:
        await stream._render_psf(bad)
    assert e.value.renderer == "furnace"


@needs_sunvox
@pytest.mark.asyncio
async def test_sunvox_renders_the_project_length(renderers):
    from soniqboom.api import stream
    out = await stream._render_psf(SUNVOX / "tiny tune.sunvox")
    try:
        secs, peak, ch = _stats(out)
        assert secs == pytest.approx(16.64, abs=0.01) and ch == 2 and peak > 1000
    finally:
        out.unlink(missing_ok=True)


@needs_mgs
@pytest.mark.asyncio
async def test_mgs_renders_its_measured_length(renderers):
    from soniqboom.api import stream
    out = await stream._render_psf(MGS / "SC-OPEN.MGS")
    try:
        secs, peak, ch = _stats(out)
        assert secs == pytest.approx(64.1, abs=0.01) and ch == 2 and peak > 1000
    finally:
        out.unlink(missing_ok=True)


# ── The stream endpoint: cold (two different files), then warm ──────────────

def _helpers():
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        from test_render_r1_stream import _T, _app, _client
    finally:
        sys.path.pop(0)
    return _T, _app, _client


def _pcm(wav: bytes) -> bytes:
    """The audio of a WAV response (a live one states no data size)."""
    i = wav.find(b"data")
    size = int.from_bytes(wav[i + 4:i + 8], "little")
    return wav[i + 8:] if size in (0, 0xFFFFFFFF) else wav[i + 8:i + 8 + size]


def _pcm_seconds(wav: bytes) -> float:
    i = wav.find(b"data")
    return (len(wav) - i - 8) / (44100 * 4)


async def _play_three(monkeypatch, tmp_path, tracks, order):
    """Play ``order`` (track ids) through /api/stream; (X-Rendered, X-Cache,
    seconds) each."""
    _T, _app, _client = _helpers()
    from soniqboom.core import unplayable
    monkeypatch.setattr(unplayable, "_proven", {})
    app, fs = _app(monkeypatch, tmp_path, tracks)
    got = []
    async with _client(app) as c:
        for tid in order:
            r = await c.get(f"/api/stream/{tid}", cookies={"sb_session": "x"})
            assert r.status_code == 200, r.text
            got.append((r.headers.get("x-rendered"), r.headers.get("x-cache"),
                        _pcm_seconds(r.content), r.content))
            await asyncio.sleep(0.3)
    return got, fs


def _expect_cold_cold_warm(got, renderer):
    assert [g[0] for g in got] == [renderer] * 3
    assert got[0][1].startswith("miss") and got[1][1].startswith("miss") and got[2][1] == "hit"
    assert _pcm(got[2][3]) == _pcm(got[0][3])        # the warm play is the first render


@needs_goat
@pytest.mark.asyncio
async def test_goattracker_plays_cold_then_warm_and_follows_the_cap(monkeypatch, tmp_path, renderers):
    _T = _helpers()[0]
    from soniqboom.config import settings
    for f in ("sanction.sng", "unleash.sng"):
        shutil.copy(GOAT / f, tmp_path / f)
    tracks = {t: _T(t, tmp_path / f, 180.0, fmt="GoatTracker", genre=["Chiptune", "C64"])
              for t, f in (("san", "sanction.sng"), ("unl", "unleash.sng"))}
    got, _fs = await _play_three(monkeypatch, tmp_path, tracks, ("san", "unl", "san"))
    _expect_cold_cold_warm(got, "gt2reloc")
    assert all(g[2] == pytest.approx(180.0) for g in got)
    assert _pcm(got[0][3]) != _pcm(got[1][3])
    monkeypatch.setattr(settings, "sid_default_duration", 60)
    got2, fs = await _play_three(monkeypatch, tmp_path, tracks, ("san", "san", "san"))
    assert got2[0][1].startswith("miss") and got2[0][2] == pytest.approx(60.0)
    assert sorted(p.stem for p in (tmp_path / "conv" / "psf").rglob("san*.wav")) == [
        "san__r1__vgt180", "san__r1__vgt60"]


@needs_furnace
@pytest.mark.asyncio
async def test_famitracker_plays_cold_then_warm_and_stores_its_length(monkeypatch, tmp_path, renderers):
    _T = _helpers()[0]
    for f in ("Square eyes.ftm", "short_boss_theme.ftm"):
        shutil.copy(FAMI / f, tmp_path / f)
    tracks = {t: _T(t, tmp_path / f, 0.0, fmt="FamiTracker", genre=["Chiptune"])
              for t, f in (("sq", "Square eyes.ftm"), ("bo", "short_boss_theme.ftm"))}
    got, fs = await _play_three(monkeypatch, tmp_path, tracks, ("sq", "bo", "sq"))
    _expect_cold_cold_warm(got, "furnace")
    assert got[0][2] == pytest.approx(145.728, abs=0.01) and got[1][2] == pytest.approx(21.9, abs=0.01)
    lengths = {tid: f["duration"] for tid, f in fs.updates if "duration" in f}
    assert lengths == {"sq": pytest.approx(145.73, abs=0.01), "bo": pytest.approx(21.9, abs=0.01)}


@needs_sunvox
@pytest.mark.asyncio
async def test_sunvox_plays_cold_then_warm(monkeypatch, tmp_path, renderers):
    _T = _helpers()[0]
    for f in ("tiny tune.sunvox", "out of time.sunvox"):
        shutil.copy(SUNVOX / f, tmp_path / f)
    tracks = {"ti": _T("ti", tmp_path / "tiny tune.sunvox", 16.64, fmt="SunVox", genre=["Tracker"]),
              "oo": _T("oo", tmp_path / "out of time.sunvox", 491.25, fmt="SunVox",
                       genre=["Tracker"])}
    got, _fs = await _play_three(monkeypatch, tmp_path, tracks, ("ti", "oo", "ti"))
    _expect_cold_cold_warm(got, "sunvoxrender")
    assert got[0][2] == pytest.approx(16.64, abs=0.01) and got[1][2] == pytest.approx(491.25, abs=0.01)


@needs_mgs
@pytest.mark.asyncio
async def test_mgs_plays_cold_then_warm(monkeypatch, tmp_path, renderers):
    _T = _helpers()[0]
    for f in ("SC-OPEN.MGS", "Mario-Net - Theme.MGS"):
        shutil.copy(MGS / f, tmp_path / f)
    tracks = {"sc": _T("sc", tmp_path / "SC-OPEN.MGS", 64.1, fmt="MGSDRV", genre=["Chiptune"]),
              "ma": _T("ma", tmp_path / "Mario-Net - Theme.MGS", 133.1, fmt="MGSDRV",
                       genre=["Chiptune"])}
    got, _fs = await _play_three(monkeypatch, tmp_path, tracks, ("sc", "ma", "sc"))
    _expect_cold_cold_warm(got, "kssrender")
    assert got[0][2] == pytest.approx(64.1, abs=0.01) and got[1][2] == pytest.approx(133.1, abs=0.01)
