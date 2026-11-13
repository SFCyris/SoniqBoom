# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sawteeth songs in the server: a ``.st`` is one by its "SWTD" / "SWTT"
start (else not listed — the suffix is also uade's SynTracker), read with
``sawteethrender -i`` (title, author, channels, the render's length), routed
by the record's format to the one-tune route (web play, cast), checked by
content inside archives, rendered by ``stream._render_sawteeth`` and its
refusals named.  The renderer: ``SB_SAWTEETHRENDER``, else on PATH, else
built with its build.sh into a temp folder (skipped without a C compiler);
the songs: ``internal/testdata/nostalgicplayer/sawteeth``."""
from __future__ import annotations

import array
import importlib.util
import os
import shutil
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from soniqboom.core import metadata as md

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "internal/testdata/nostalgicplayer/sawteeth"
# name → (title, author, channels, seconds) as sawteethrender reads them
SONGS = {
    "64k.st": ("64k", "Jonas Norberg ©2001 LINEFEED", 5, 68.99),
    "Infinity.st": ("Infinity", "biteoN", 8, 165.76),
    "Second.st": ("Second", "Tigoro", 4, 38.52),
    "Techno Loop.st": ("TechnoLoop", "Spruce / LF © 2000", 3, 15.36),
}


@pytest.fixture(scope="module")
def binary(tmp_path_factory) -> str:
    p = os.environ.get("SB_SAWTEETHRENDER") or shutil.which("sawteethrender")
    if p and os.path.exists(p):
        return p
    if not shutil.which(os.environ.get("CC", "cc")):
        pytest.skip("no C compiler to build sawteethrender")
    dest = tmp_path_factory.mktemp("swbin")
    subprocess.run(["sh", str(ROOT / "soniqboom/native/sawteethrender/build.sh"), str(dest)],
                   check=True, capture_output=True, timeout=300)
    return str(dest / "sawteethrender")


@pytest.fixture
def renderer(binary, monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sawteethrender_path", binary)
    return binary


needs_data = pytest.mark.skipif(not DATA.is_dir(), reason="no Sawteeth test songs")


def _selftest_tune() -> bytes:
    spec = importlib.util.spec_from_file_location("render_selftest",
                                                  ROOT / "scripts/render_selftest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.sawteeth_tune()


# ── Scan ─────────────────────────────────────────────────────────────────────

@needs_data
@pytest.mark.parametrize("name", sorted(SONGS))
def test_scan_reads_the_song(renderer, name):
    title, author, channels, secs = SONGS[name]
    t = md.extract(DATA / name, "s").model_dump()
    assert (t["format"], t["title"], t["artist"], t["channels"]) == (
        "Sawteeth", title, author, channels)
    assert t["duration"] == pytest.approx(secs, abs=0.01)
    assert t["genre"] == ["Chiptune"]


def test_a_st_that_isnt_sawteeth_is_not_listed(tmp_path, renderer):
    for body in (b"MOD stuff" + bytes(600), b"", b"SWT"):
        f = tmp_path / "x.st"
        f.write_bytes(body)
        with pytest.raises(md.NotPlayableHere):
            md.extract(f, "x")


def test_a_broken_sawteeth_song_is_not_listed(tmp_path, renderer):
    f = tmp_path / "x.st"
    f.write_bytes(b"SWTD" + bytes(20))
    with pytest.raises(md.NotPlayableHere):
        md.extract(f, "x")


def test_without_the_renderer_nothing_is_listed(tmp_path, monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sawteethrender_path", "")
    monkeypatch.setattr(md, "_find_atari_binary", lambda s, b: None)
    f = tmp_path / "x.st"
    f.write_bytes(_selftest_tune())
    with pytest.raises(md.NotPlayableHere):
        md.extract(f, "x")


UADE_BP = ROOT / "internal/testdata/uade/BP SoundMon 2/commando - hiscore.bp"


@pytest.mark.skipif(not UADE_BP.is_file(), reason="no uade test module")
def test_a_prefix_named_st_is_uades_or_not_listed(tmp_path, renderer):
    """``st`` is an owned suffix a verified Amiga module may override: a uade
    prefix-named module saved as ``.st`` is still uade's; text or junk under
    such a name isn't listed; a Sawteeth song stays one whatever its name."""
    from soniqboom.api import stream
    from soniqboom.core import uade_formats as uf
    if not uf.player_map() or not shutil.which("uade123"):
        pytest.skip("uade not installed")
    (tmp_path / "bp.commando.st").write_bytes(UADE_BP.read_bytes())
    t = md.extract(tmp_path / "bp.commando.st", "a").model_dump()
    assert t["format"] == "SoundMon 2.0"
    assert stream._render_ident(str(tmp_path / "bp.commando.st"), t) == (".st", True)
    for name, body in (("bp.readme.st", b"hello text file\n"), ("bp.junk.st", bytes(range(256)) * 8)):
        (tmp_path / name).write_bytes(body)
        with pytest.raises(md.NotPlayableHere):
            md.extract(tmp_path / name, "b")
    (tmp_path / "bp.song.st").write_bytes(_selftest_tune())
    t = md.extract(tmp_path / "bp.song.st", "c").model_dump()
    assert t["format"] == "Sawteeth"
    assert stream._render_ident(str(tmp_path / "bp.song.st"), t) == (stream._SAWTEETH_ROUTE, False)


def test_the_selftest_song(tmp_path, renderer):
    f = tmp_path / "selftest.st"
    f.write_bytes(_selftest_tune())
    t = md.extract(f, "t").model_dump()
    assert (t["format"], t["title"], t["artist"], t["channels"]) == (
        "Sawteeth", "SoniqBoom self-test", "SoniqBoom", 2)
    assert t["duration"] == pytest.approx(0.96, abs=0.01)


# ── Routing ──────────────────────────────────────────────────────────────────

def test_routing_names_it_by_format():
    from soniqboom.api import stream
    rec = {"id": "1", "path": "/m/a.st", "format": "Sawteeth"}
    assert stream._render_ident(rec["path"], rec) == (stream._SAWTEETH_ROUTE, False)
    assert stream._SAWTEETH_ROUTE in stream._PSF_STREAM_EXTS
    assert ".st" not in stream._PSF_STREAM_EXTS        # (the suffix alone isn't routed)
    assert stream._own_engine(Path("/m/a.st")) == ("sawteeth", "sawteethrender", "Sawteeth")
    assert stream.renderer_key_for(rec) == "sawteethrender"
    assert stream._route_variant(Path("x.st")) == "sw1"
    ext, uade = stream._render_ident("/m/mod.x.st", {"format": "ProTracker",
                                                    "genre": ["Amiga", "Module"]})
    assert ext != stream._SAWTEETH_ROUTE


def test_cast_routes_it_like_a_play():
    from soniqboom.api import cast_stream, stream
    from soniqboom.core import cast_render
    from soniqboom.core.conversion_cache import _cache_key
    assert cast_render.one_tune_route("a", "/m/a.st", {"format": "Sawteeth"}) == ".sawteeth"
    assert cast_render.is_rendered_format(".sawteeth") and cast_render.is_rendered_format(".st")
    assert cast_render.rendered_cache_key("t1", ".sawteeth") == _cache_key(
        "t1", "psf", subsong=0, variant=stream._own_variant("sawteeth"))
    rec = SimpleNamespace(id="t2", format="Sawteeth", genre=["Chiptune"], path="/m/a.st")
    assert cast_stream._ext_for("/m/a.st", rec) == ".sawteeth"


def test_archive_members_are_checked_by_content():
    from soniqboom.core import archive
    assert not archive.rejected_by_bytes("a.st", "a.st", lambda r: _selftest_tune())
    assert not archive.rejected_by_bytes("a.st", "a.st", lambda r: b"SWTT\r\n1200\r\n")
    assert archive.rejected_by_bytes("b.st", "b.st", lambda r: b"M.K." + bytes(100))


def test_archived_prefix_named_st_members_keep_their_names():
    """Members named by a uade prefix that end in ``.st`` are listed as they
    were before Sawteeth (``jam.title.st.jam``; SynTracker's own ``st.title``
    as ``st.title.st``), whatever their bytes; a suffix-named ``.st`` must
    be a Sawteeth song."""
    from soniqboom.core import archive, uade_formats as uf
    if not uf.player_map():
        pytest.skip("uade not installed")
    saw = _selftest_tune()
    got = archive._build_map(["jam.title.st", "st.trip to holland", "mod.title.st", "song.st",
                              "saw.st", "dir/jam.x.st"],
                             lambda r: saw if r == "saw.st" else bytes(64))
    assert got == {"jam.title.st.jam": "jam.title.st", "st.trip to holland.st": "st.trip to holland",
                   "mod.title.st.mod": "mod.title.st", "saw.st": "saw.st",
                   "dir/jam.x.st.jam": "dir/jam.x.st"}


def test_modland_st_files_are_never_sawteeth():
    """Modland has no Sawteeth folder: a ``.st`` there is an Amiga module
    where its folder names the player, else not music — never "Sawteeth"."""
    from soniqboom.core import modland_catalog as mc
    assert mc._classify("Unknown/Someone/x.st") == mc.KIND_OTHER
    assert mc.format_for("Unknown/Someone/x.st") != "Sawteeth"
    assert mc.family_genre("Unknown/Someone/x.st") != ["Chiptune"]


def test_families_and_rules_know_it():
    from soniqboom.core import retro, skip_rules, unplayable
    skip_rules._tune_exts = None
    assert ".st" in skip_rules._exts()
    assert retro.coarse_family("Sawteeth") == "chiptune"
    assert retro.coarse_family("DefleMask") == "chiptune"
    from soniqboom.core import subsonic_index
    assert subsonic_index._FAMILY_LABELS[retro.chip_family("Sawteeth")] == "Sawteeth"
    assert subsonic_index._FAMILY_LABELS[retro.chip_family("DefleMask")] == "DefleMask"
    assert unplayable.KIND_KEYS["Sawteeth"] == "sawteethrender"
    assert unplayable.classify("Sawteeth", 1, "sawteethrender: x.st: not a Sawteeth module") == (
        "sawteethrender", "doesn't recognise this file as a Sawteeth song", True)
    assert unplayable.classify("Sawteeth", 1, "sawteethrender: a damaged Sawteeth module")[0] \
        == "sawteethrender"
    assert unplayable.classify("Sawteeth", 1, "sawteethrender: can't write /x.wav") is None


# ── Per-channel meters ───────────────────────────────────────────────────────

@needs_data
@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["64k.st", "Second.st"])
async def test_the_first_meter_ask_gets_sawteeths_levels(renderer, tmp_path, monkeypatch, name):
    """``/vu``'s backfill runs ``sawteethrender -i -v``: one level frame per
    1/30 s of the render, a column per channel, levels that move."""
    from soniqboom.api import tracks as tracks_api
    from soniqboom.config import settings
    from soniqboom.core import openmpt_vu
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    rec = SimpleNamespace(id="s", path=str(DATA / name), format="Sawteeth", genre=["Chiptune"])
    assert tracks_api._vu_backfill_route(rec)
    sidecar = await tracks_api._try_backfill_vu_sidecar(rec, "s1", 0)
    assert sidecar is not None
    raw = sidecar.read_bytes()
    channels, rate, frames = openmpt_vu.parse_and_validate_vumr(raw)
    secs = SONGS[name][3]
    assert (channels, rate) == (SONGS[name][2], 30)
    assert frames == pytest.approx(secs * 30, abs=1)
    levels = raw[16 + channels:]
    for c in range(channels):
        col = levels[c::channels]
        assert max(col) > 100 and len(set(col)) > 10        # (audible, and it moves)


@pytest.mark.asyncio
async def test_no_renderer_no_sawteeth_meter(tmp_path, monkeypatch):
    from soniqboom.api import tracks as tracks_api
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(md, "_find_atari_binary", lambda s, b: None)
    f = tmp_path / "x.st"
    f.write_bytes(_selftest_tune())
    rec = SimpleNamespace(id="x", path=str(f), format="Sawteeth", genre=["Chiptune"])
    assert await tracks_api._try_backfill_vu_sidecar(rec, "x", 0) is None


# ── Renders ──────────────────────────────────────────────────────────────────

def _stats(path: Path) -> "tuple[float, int, int]":
    with wave.open(str(path)) as w:
        n, rate, ch = w.getnframes(), w.getframerate(), w.getnchannels()
        a = array.array("h")
        a.frombytes(w.readframes(n))
    return n / rate, max(map(abs, a), default=0), ch


@needs_data
@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["64k.st", "Techno Loop.st"])
async def test_render_is_the_scanned_length(renderer, name):
    from soniqboom.api import stream
    out = await stream._render_psf(DATA / name)
    try:
        secs, peak, ch = _stats(out)
        assert secs == pytest.approx(SONGS[name][3], abs=0.01) and ch == 2 and peak > 1000
    finally:
        out.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_a_song_it_cant_load_is_a_refusal(tmp_path, renderer):
    from soniqboom.api import stream
    from soniqboom.core import unplayable
    bad = tmp_path / "bad.st"
    bad.write_bytes(b"SWTD" + bytes(30))
    with pytest.raises(unplayable.RenderRefusal) as e:
        await stream._render_psf(bad)
    assert e.value.renderer == "sawteethrender"


@pytest.mark.asyncio
async def test_no_renderer_is_a_501(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sawteethrender_path", "")
    monkeypatch.setattr(stream, "_find_renderer", lambda p, name: None)
    f = tmp_path / "x.st"
    f.write_bytes(_selftest_tune())
    with pytest.raises(HTTPException) as e:
        await stream._render_psf(f)
    assert e.value.status_code == 501 and "sawteethrender" in e.value.detail


def test_the_build_gate_renders_its_song(binary):
    r = subprocess.run([os.environ.get("PYTHON", str(ROOT / ".venv/bin/python")), "-I",
                        str(ROOT / "scripts/render_selftest.py"), "--only", "sawteethrender",
                        "--sawteethrender", binary], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "sawteethrender (Sawteeth): ok" in r.stdout
