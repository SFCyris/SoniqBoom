# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sharp X68000 MDX songs through mdxrender (soniqboom/native/mdxrender:
portable_mdx — MXDRV + X68Sound): the header the scan reads
(``metadata.mdx_header``), the routing (the one-tune render route, never
zxtune123 or uade), the PDX companion, the refusal rules — and, where the
binary is built (``SB_MDXRENDER`` or on PATH), real renders of a synthesized
song and of the reference files (``internal/testdata/mdx``)."""
from __future__ import annotations

import array
import importlib.util
import os
import shutil
import wave
import zipfile
from pathlib import Path

import pytest

from soniqboom.core import metadata as md

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "internal/testdata/mdx"


def _selftest():
    spec = importlib.util.spec_from_file_location(
        "render_selftest", ROOT / "scripts/render_selftest.py")
    rs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rs)
    return rs


def _binary():
    p = os.environ.get("SB_MDXRENDER") or shutil.which("mdxrender")
    return p if p and os.path.exists(p) else None


needs_binary = pytest.mark.skipif(_binary() is None,
                                  reason="mdxrender not built here (soniqboom/native/mdxrender/build.sh)")
needs_data = pytest.mark.skipif(not DATA.is_dir(), reason="internal/testdata/mdx absent")


@pytest.fixture
def binary(monkeypatch):
    from soniqboom.config import settings
    b = _binary()
    monkeypatch.setattr(settings, "mdxrender_path", b)
    return b


def _with_pdx(raw: bytes, name: bytes) -> bytes:
    """The self-test song naming PDX file ``name``."""
    i = raw.index(b"\r\n\x1a") + 3
    return raw[:i] + name + raw[i:]          # (the name before the existing NUL)


def _stats(path: Path) -> "tuple[float, int, bytes]":
    with wave.open(str(path)) as w:
        n, rate = w.getnframes(), w.getframerate()
        pcm = w.readframes(n)
    a = array.array("h")
    a.frombytes(pcm)
    return n / rate, max(map(abs, a), default=0), pcm


# ── The header ───────────────────────────────────────────────────────────────

def test_header_title_and_pdx_name():
    tune = _selftest().mdx_tune()
    assert md.mdx_header(tune) == ("SoniqBoom self-test", "")
    assert md.mdx_header(_with_pdx(tune, b"drums")) == ("SoniqBoom self-test", "drums.PDX")
    assert md.mdx_header(_with_pdx(tune, b"a:\\pdx\\Drums.pdx"))[1] == "Drums.pdx"
    sjis = "魔界村  マップ\tスクロール".encode("cp932") + b"\r\n\x1a\0" + b"\0" * 40
    assert md.mdx_header(sjis) == ("魔界村 マップ スクロール", "")
    assert md.mdx_header(b"no marker here") is None
    assert md.mdx_header(b"title\r\n\x1a" + b"x" * 600) is None          # no NUL
    assert md.mdx_header(b"lf only\n\x1a\0") == ("lf only", "")           # (as portable_mdx)
    # a Shift-JIS name whose second byte is "\\" (表 = 95 5C) is not split there
    sj = b"t\r\n\x1a" + "表示".encode("cp932") + b"\0"
    assert md.mdx_header(sj) == ("t", "表示.PDX")


def test_extract_without_the_renderer(tmp_path, monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "mdxrender_path", str(tmp_path / "none"))
    monkeypatch.setattr(md, "find_mdx_renderer", lambda: None)
    f = tmp_path / "Song.MDX"
    f.write_bytes(_selftest().mdx_tune())
    d = md.extract(f, "m").model_dump()
    assert (d["format"], d["title"], d["duration"], d["genre"]) == (
        "MDX", "SoniqBoom self-test", 0, ["Chiptune"])
    junk = tmp_path / "junk.mdx"                       # listed; its play is refused
    junk.write_bytes(b"\0" * 300)
    d = md.extract(junk, "j").model_dump()
    assert (d["format"], d["title"], d["duration"]) == ("MDX", "junk", 0)
    assert md.is_supported_music_name("a.MDX") and not md.is_supported_music_name("a.pdx")


# ── Routing ──────────────────────────────────────────────────────────────────

def test_routed_to_mdxrender():
    from soniqboom.api import stream
    from soniqboom.core import retro, uade_formats, unplayable
    assert ".mdx" in stream._PSF_STREAM_EXTS and stream._is_rendered_ext(".mdx")
    assert stream.renderer_key_for({"id": "a", "path": "/m/x.mdx", "format": "MDX"}) == "mdxrender"
    assert stream._route_renderer(Path("x.MDX")) == "mdxrender"
    assert stream._route_renderer(Path("x.minipsf")) == "zxtune123"
    assert stream._zxtune_kind(Path("x.mdx")) == "MDX"
    assert stream._ADLIB_COMPANION_GLOBS[".mdx"] == ("*.pdx",)
    assert uade_formats.classify("x.mdx") is None
    assert unplayable.KIND_KEYS["MDX"] == "mdxrender"
    assert retro.chip_family("MDX") == "x68000" and retro.coarse_family("MDX") == "chiptune"


def test_refusal_rules():
    from soniqboom.core import unplayable
    # a missing PDX: the companion, never the file
    assert unplayable.classify("MDX", 3, "mdxrender: PDX not found: drums.PDX\n") is None
    assert unplayable.classify("MDX", 1, "mdxrender: not an MDX file this build can play: x")[1] \
        == "doesn't recognise this file as an MDX song"
    assert unplayable.classify("MDX", -11, "")[1] == "crashes on this file (SIGSEGV)"
    # the machine, never the file
    assert unplayable.classify("MDX", 1, "mdxrender: out of memory\n") is None
    assert unplayable.classify("MDX", 1, "mdxrender: read error: /m/a.PDX\n") is None


@pytest.mark.parametrize("rel, kind", [
    ("MDX/Glo/GLOTECNO.MDX", 1), ("MDX/Glo/GLOTECNO.PDX", 2),
    ("MDX/Gamuo/Dream Come True/mirai.mdx", 1), ("MDX/Gamuo/Dream Come True/mirai.pdx", 2),
])
def test_modland_lists_songs_not_sample_files(rel, kind):
    from soniqboom.core import modland_catalog as mc
    assert mc._classify(rel) == (mc.KIND_MUSIC, mc.KIND_OTHER)[kind - 1]
    if kind == 1:
        assert mc.family_genre(rel) == ["Chiptune"]


def test_the_route_plan_leaves_the_length_to_the_render(tmp_path):
    from soniqboom.api import stream
    f = tmp_path / "a.mdx"
    f.write_bytes(_selftest().mdx_tune())
    assert stream._psf_render_plan(f) == (0.0, 0.0)          # (no second -i per play)


@pytest.mark.asyncio
async def test_no_renderer_is_a_501(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "mdxrender_path", "")
    monkeypatch.setattr(stream, "_find_renderer", lambda *_a: None)
    f = tmp_path / "a.mdx"
    f.write_bytes(_selftest().mdx_tune())
    with pytest.raises(HTTPException) as exc:
        await stream._render_psf(f)
    assert exc.value.status_code == 501 and "mdxrender" in exc.value.detail


# ── The binary ───────────────────────────────────────────────────────────────

@needs_binary
def test_selftest_song_renders_audible(tmp_path):
    rs = _selftest()
    src = tmp_path / "t.mdx"
    src.write_bytes(rs.mdx_tune())
    info = md.mdx_info(src, _binary())
    assert info == {"seconds": "7.520", "pdx": "", "pdx_found": "0"}
    out = tmp_path / "o.wav"
    assert os.system(f'"{_binary()}" -m 3 -f 0 "{src}" "{out}"') == 0
    secs, peak, _ = _stats(out)
    assert secs == pytest.approx(3.0) and peak > 3000


@needs_binary
def test_usage_and_missing_pdx_exit_codes(tmp_path):
    import subprocess
    src = tmp_path / "t.mdx"
    src.write_bytes(_with_pdx(_selftest().mdx_tune(), b"kit"))
    run = lambda *a: subprocess.run([_binary(), *map(str, a)], capture_output=True, text=True)
    r = run(src, tmp_path / "o.wav")
    assert r.returncode == 3 and "PDX not found: kit.PDX" in r.stderr
    assert run("-i", src).stdout.splitlines() == ["seconds=7.520", "pdx=kit.PDX", "pdx_found=0"]
    (tmp_path / "KIT.pdx").write_bytes(b"\0" * 2048)                 # any letter case
    assert "pdx_found=1" in run("-i", src).stdout
    assert run(src, tmp_path / "o.wav").returncode == 0
    for bad in (["-r", "22050"], ["-l", "0"], ["-m", "0"], ["-f", "-1"], ["-x", "1"]):
        assert run(*bad, src, tmp_path / "o.wav").returncode == 2, bad
    assert run("--version").stdout.startswith("mdxrender ")
    junk = tmp_path / "junk.mdx"
    junk.write_bytes(os.urandom(4000).replace(b"\x1a", b"\0"))
    assert run(junk, tmp_path / "o.wav").returncode == 1


# file → (title, the length mdxrender measures with 2 loops + fade, PDX)
_REFERENCE = {
    "194301c.mdx": ("♪1943 空中戦 ROUND 1,2,7,10,13,16 (C)CAPCOM 1987 / ChaMa", 81.174, ""),
    "GLOTECNO.MDX": ("懐かしのテクノポップス・グロテクノ by Glo.", 163.457, "GLOTECNO.PDX"),
    "mirai.mdx": ("『未来予想図II』(Dreams Come True) By ガムオ '91/7/16", 433.965, "mirai.PDX"),
    "Map Scroll.mdx": ("♪魔界村 マップスクロール (C)CAPCOM 1985 / ChaMa++", 7.027, ""),
    "alex00.mdx": ("アレックスキッド -THE LOST STARS- [ ラウンド スタート ] (c)SEGA 1986 by YURAYSAN",
                   5.204, ""),
}


@needs_data
@needs_binary
@pytest.mark.parametrize("name", sorted(_REFERENCE))
def test_reference_scan(name, binary):
    title, secs, pdx = _REFERENCE[name]
    info = md.mdx_info(DATA / name, binary)
    assert float(info["seconds"]) == pytest.approx(secs, abs=0.001)
    assert info["pdx"] == pdx and info["pdx_found"] == ("1" if pdx else "0")
    d = md.extract(DATA / name, "r").model_dump()
    assert d["title"] == title
    assert d["duration"] == pytest.approx(min(secs, 180.0), abs=0.01)    # the SID-duration cap


@needs_data
@needs_binary
@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["GLOTECNO.MDX", "194301c.mdx", "mirai.mdx"])
async def test_reference_renders(name, binary):
    from soniqboom.api import stream
    out = await stream._render_psf(DATA / name)
    try:
        secs, peak, pcm = _stats(out)
    finally:
        out.unlink(missing_ok=True)
    assert secs == pytest.approx(min(_REFERENCE[name][1], 180.0), abs=0.001)
    assert peak > 5000
    tail = array.array("h")
    tail.frombytes(pcm[-44100 * 4:])                        # the last second: faded out
    assert max(map(abs, tail)) < peak // 8


@needs_data
@needs_binary
def test_the_pdx_samples_are_played(tmp_path, binary):
    for f in ("GLOTECNO.MDX", "GLOTECNO.PDX"):
        shutil.copy(DATA / f, tmp_path / f)
    real = tmp_path / "real.wav"
    assert os.system(f'"{binary}" -m 20 "{tmp_path / "GLOTECNO.MDX"}" "{real}"') == 0
    (tmp_path / "GLOTECNO.PDX").write_bytes(
        (DATA / "GLOTECNO.PDX").read_bytes()[:1024] + b"\0" * 30207)  # its sample table only
    silent = tmp_path / "silent.wav"
    assert os.system(f'"{binary}" -m 20 "{tmp_path / "GLOTECNO.MDX"}" "{silent}"') == 0
    assert _stats(real)[2] != _stats(silent)[2]


def _named(raw: bytes, name: bytes) -> bytes:
    """An MDX song ``raw`` naming PDX file ``name`` instead of its own."""
    i = raw.index(b"\x1a") + 1
    return raw[:i] + name + raw[raw.index(b"\0", i):]


@needs_data
@needs_binary
@pytest.mark.asyncio
async def test_a_shift_jis_pdx_name_finds_its_file(tmp_path, binary):
    """The name is Shift-JIS in the song; on disk it's in UTF-8 — or kept in
    Shift-JIS bytes — and 表 (95 5C) is never split at its "\\"."""
    from soniqboom.api import stream
    song = _named((DATA / "GLOTECNO.MDX").read_bytes(), "表示".encode("cp932"))
    (tmp_path / "sj.mdx").write_bytes(song)
    assert md.mdx_info(tmp_path / "sj.mdx", binary)["pdx"] == "表示.PDX"
    assert md.mdx_info(tmp_path / "sj.mdx", binary)["pdx_found"] == "0"
    (tmp_path / "表示.PDX").write_bytes((DATA / "GLOTECNO.PDX").read_bytes())
    assert md.mdx_info(tmp_path / "sj.mdx", binary)["pdx_found"] == "1"
    out = await stream._render_psf(tmp_path / "sj.mdx")
    try:
        assert _stats(out)[0] == pytest.approx(163.457, abs=0.001)
    finally:
        out.unlink(missing_ok=True)
    d = md.extract(tmp_path / "sj.mdx", "s").model_dump()
    assert d["title"] == _REFERENCE["GLOTECNO.MDX"][0] and d["duration"] == pytest.approx(163.46)


@needs_data
@needs_binary
def test_big_and_unreadable_pdx_files(tmp_path, binary):
    import subprocess
    shutil.copy(DATA / "GLOTECNO.MDX", tmp_path / "GLOTECNO.MDX")
    pdx = (DATA / "GLOTECNO.PDX").read_bytes()
    run = lambda: subprocess.run([binary, "-m", "10", str(tmp_path / "GLOTECNO.MDX"),
                                  str(tmp_path / "o.wav")], capture_output=True, text=True)
    (tmp_path / "GLOTECNO.PDX").write_bytes(pdx + bytes(20 << 20))   # 20 MB: plays
    r = run()
    assert r.returncode == 0, r.stderr
    assert _stats(tmp_path / "o.wav")[0] == pytest.approx(10.0)
    (tmp_path / "GLOTECNO.PDX").write_bytes(bytes(70 << 20))          # over 64 MB
    r = run()
    assert r.returncode == 1 and "read error" in r.stderr


@needs_data
@needs_binary
@pytest.mark.asyncio
async def test_missing_pdx_is_a_422_naming_it(tmp_path, binary):
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.core.unplayable import RenderRefusal
    shutil.copy(DATA / "GLOTECNO.MDX", tmp_path / "GLOTECNO.MDX")
    with pytest.raises(HTTPException) as exc:
        await stream._render_psf(tmp_path / "GLOTECNO.MDX")
    assert exc.value.status_code == 422 and "GLOTECNO.PDX" in exc.value.detail
    assert not isinstance(exc.value, RenderRefusal)                    # (never marks the track)


@needs_data
@needs_binary
@pytest.mark.asyncio
async def test_archived_song_plays_with_its_pdx(tmp_path, monkeypatch, binary):
    """An MDX song in a ZIP gets its PDX extracted beside it (the companion
    globs), so it renders with its samples."""
    from soniqboom.api import stream
    monkeypatch.setattr(stream, "_zip_extract_dir",
                        lambda: (tmp_path / "zx").mkdir(exist_ok=True) or tmp_path / "zx")
    arc = tmp_path / "glo.zip"
    with zipfile.ZipFile(arc, "w") as z:
        z.write(DATA / "GLOTECNO.MDX", "glo/GLOTECNO.MDX")
        z.write(DATA / "GLOTECNO.PDX", "glo/glotecno.pdx")
    local = await stream._resolve_adlib_local_path("arc1", f"{arc}::glo/GLOTECNO.MDX")
    assert local is not None and (local.parent / "glotecno.pdx").is_file()
    out = await stream._render_psf(local)
    try:
        assert _stats(out)[0] == pytest.approx(163.457, abs=0.001)
    finally:
        out.unlink(missing_ok=True)


@needs_data
@needs_binary
@pytest.mark.asyncio
async def test_the_stream_endpoint_plays_them_cold_then_from_the_cache(
        monkeypatch, tmp_path, binary):
    """Two different songs cold (each rendered by mdxrender, the X-Rendered
    header naming it), then the first again warm (a cache hit)."""
    import asyncio
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        from test_render_r1_stream import _T, _app, _client
    finally:
        sys.path.pop(0)
    from soniqboom.core import unplayable
    monkeypatch.setattr(unplayable, "_proven", {})
    for f in ("GLOTECNO.MDX", "GLOTECNO.PDX", "194301c.mdx"):
        shutil.copy(DATA / f, tmp_path / f)
    tracks = {"glo": _T("glo", tmp_path / "GLOTECNO.MDX", 163.46, fmt="MDX", genre=["Chiptune"]),
              "c43": _T("c43", tmp_path / "194301c.mdx", 81.17, fmt="MDX", genre=["Chiptune"])}
    app, _fs = _app(monkeypatch, tmp_path, tracks)
    got = []
    async with _client(app) as c:
        for tid in ("glo", "c43", "glo"):
            r = await c.get(f"/api/stream/{tid}", cookies={"sb_session": "x"})
            assert r.status_code == 200, r.text
            got.append((r.headers.get("x-rendered"), r.headers.get("x-cache"), r.content))
            await asyncio.sleep(0.2)
    assert [g[0] for g in got] == ["mdxrender"] * 3
    assert got[0][1].startswith("miss") and got[1][1].startswith("miss") and got[2][1] == "hit"

    def _pcm(wav: bytes) -> bytes:
        i = wav.find(b"data")
        size = int.from_bytes(wav[i + 4:i + 8], "little")
        return wav[i + 8:i + 8 + size] if size != 0xFFFFFFFF else wav[i + 8:]
    glo, c43, warm = (_pcm(g[2]) for g in got)
    assert len(glo) == 163457 * 44100 // 1000 * 4 and len(c43) == 81174 * 44100 // 1000 * 4
    assert warm == glo
    assert "mdxrender" in unplayable._proven and "zxtune123" not in unplayable._proven


# ── A song on a remote share / an online collection: only its own PDX ───────

class _Entry:
    def __init__(self, name, path):
        self.name, self.path, self.is_dir = name, path, False


class _Share:
    """A remote source: ``files`` path → bytes; ``fetched`` what was read."""

    def __init__(self, files: dict):
        self.files, self.fetched = files, []

    def stat(self, p):
        class S:
            size, mtime = len(self.files[p]), 1
        return S()

    def list_dir(self, d):
        return [_Entry(p.rsplit("/", 1)[1], p) for p in self.files if p.rsplit("/", 1)[0] == d]


class _Cache:
    def __init__(self, share, where):
        self.share, self.dir = share, where
        self.dir.mkdir(parents=True)

    def fetch(self, scan_root, remote_path, source, lane="stream"):
        self.share.fetched.append(remote_path)
        out = self.dir / remote_path.replace("/", "_")
        out.write_bytes(self.share.files[remote_path])
        return out


@pytest.fixture
def share_env(tmp_path, monkeypatch):
    from soniqboom.api import stream
    from soniqboom.core import remote_cache
    zx = tmp_path / "zx"
    zx.mkdir()
    monkeypatch.setattr(stream, "_zip_extract_dir", lambda: zx)
    monkeypatch.setattr(stream, "_register_adlib_extract", lambda *a, **k: None)
    monkeypatch.setattr(stream, "_zip_evict_until_under_budget", lambda *a, **k: None)

    def make(files):
        share = _Share(files)
        cache = _Cache(share, tmp_path / f"c{len(made)}")
        made.append(cache)
        monkeypatch.setattr(remote_cache, "get_cache", lambda: cache)
        return share
    made: list = []
    return make


@pytest.mark.asyncio
async def test_a_remote_song_fetches_only_the_pdx_it_names(share_env):
    from soniqboom.api import stream
    tune = _with_pdx(_selftest().mdx_tune(), b"Kit")           # names "Kit.PDX"
    share = share_env({"/m/a/song.mdx": tune, "/m/a/KIT.pdx": b"K" * 64,
                       **{f"/m/a/other{i}.pdx": b"O" * 64 for i in range(40)}})
    got = await stream._materialize_loose_remote_adlib(
        "r1", "share", "/m/a/song.mdx", share, stream._ADLIB_COMPANION_GLOBS[".mdx"])
    assert got is not None and got.read_bytes() == tune
    assert sorted(p.name for p in got.parent.iterdir() if p.name != ".loose_marker") == [
        "KIT.pdx", "song.mdx"]
    assert sorted(share.fetched) == ["/m/a/KIT.pdx", "/m/a/song.mdx"]   # (once each)


@pytest.mark.asyncio
async def test_a_remote_song_without_a_pdx_fetches_none(share_env):
    from soniqboom.api import stream
    share = share_env({"/m/a/song.mdx": _selftest().mdx_tune(),
                       **{f"/m/a/other{i}.pdx": b"O" * 64 for i in range(5)}})
    got = await stream._materialize_loose_remote_adlib(
        "r2", "share", "/m/a/song.mdx", share, stream._ADLIB_COMPANION_GLOBS[".mdx"])
    assert got is None                                          # the plain fetch serves it
    assert share.fetched == ["/m/a/song.mdx"]
    # …and a song whose PDX isn't there: none of the others either
    share = share_env({"/m/b/song.mdx": _with_pdx(_selftest().mdx_tune(), b"gone"),
                       "/m/b/other.pdx": b"O"})
    assert await stream._materialize_loose_remote_adlib(
        "r3", "share", "/m/b/song.mdx", share, stream._ADLIB_COMPANION_GLOBS[".mdx"]) is None
    assert share.fetched == ["/m/b/song.mdx"]


@pytest.mark.asyncio
async def test_other_formats_still_fetch_every_matching_companion(share_env):
    from soniqboom.api import stream
    share = share_env({"/m/a/x.minipsf": b"PSF\x01" + b"\0" * 20,
                       "/m/a/one.psflib": b"1", "/m/a/two.psflib": b"2"})
    got = await stream._materialize_loose_remote_adlib(
        "r4", "share", "/m/a/x.minipsf", share, stream._ADLIB_COMPANION_GLOBS[".minipsf"])
    assert {p.name for p in got.parent.iterdir()} >= {"one.psflib", "two.psflib"}


# ── The SID-duration cap is part of the cache key ───────────────────────────

def test_the_cache_variant_follows_the_sid_duration(monkeypatch):
    from soniqboom.api import stream
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 180)
    assert stream._route_variant(Path("a.MDX")) == "mdx180"
    monkeypatch.setattr(settings, "sid_default_duration", 300)
    assert stream._route_variant(Path("a.mdx")) == "mdx300"
    monkeypatch.setattr(settings, "sid_default_duration", 99999)
    assert stream._route_variant(Path("a.mdx")) == "mdx3600"
    assert stream._route_variant(Path("a.s98")) is None              # (zxtune123's key)


@needs_data
@needs_binary
@pytest.mark.asyncio
async def test_a_changed_sid_duration_renders_again_and_corrects_the_length(
        monkeypatch, tmp_path, binary):
    """mirai (433.965 s measured) at a 180 s cap, then 240 s: the second play
    is a new render of 240 s (not the cached 180 s one), and the stored
    length follows it; back at 180 s the first render is a cache hit again."""
    import asyncio
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        from test_render_r1_stream import _T, _app, _client
    finally:
        sys.path.pop(0)
    from soniqboom.config import settings
    for f in ("mirai.mdx", "mirai.pdx"):
        shutil.copy(DATA / f, tmp_path / f)
    tracks = {"mi": _T("mi", tmp_path / "mirai.mdx", 180.0, fmt="MDX", genre=["Chiptune"])}
    app, fs = _app(monkeypatch, tmp_path, tracks)
    got = []
    async with _client(app) as c:
        for cap in (180, 240, 180):
            monkeypatch.setattr(settings, "sid_default_duration", cap)
            r = await c.get("/api/stream/mi", cookies={"sb_session": "x"})
            assert r.status_code == 200, r.text
            i = r.content.find(b"data")
            got.append((r.headers.get("x-cache"), (len(r.content) - i - 8) / (44100 * 4)))
            await asyncio.sleep(0.3)
    assert got[0][0].startswith("miss") and got[0][1] == pytest.approx(180.0)
    assert got[1][0].startswith("miss") and got[1][1] == pytest.approx(240.0)
    assert got[2][0] == "hit" and got[2][1] == pytest.approx(180.0)
    # the 240 s render corrected the stored length (the scan's was 180 s)
    assert ("mi", {"duration": 240.0}) in fs.updates
    assert sorted(p.stem for p in (tmp_path / "conv" / "psf").rglob("*.wav")) == [
        "mi__r1__vmdx180", "mi__r1__vmdx240"]
