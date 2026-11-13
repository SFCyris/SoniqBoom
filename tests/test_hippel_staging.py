"""Amiga / Atari ST modules uade plays only once they are staged: PowerPacker
("PP20") packed modules unpacked, Atari ST COSO ``.soc`` files named for
uade's Hippel ST player, and YM-ST modules given the replay routine they
name inside themselves (``xpk.stage_for_uade``)."""
from __future__ import annotations

import asyncio
import shutil

import pytest

from soniqboom.core import uade_formats as uf
from soniqboom.core import xpk


# ── a PowerPacker 2.0 stream built by hand ───────────────────────────────────

def _pp20(fields, dest: bytes, widths=(9, 10, 11, 11)) -> bytes:
    """A PP20 file whose stream holds ``fields`` — (bit count, value) in the
    order the decoder reads them — and that unpacks to ``len(dest)`` bytes.
    The decoder reads the stream from its end, each byte's bits low first."""
    bits: list[int] = []
    for n, v in fields:
        bits += [(v >> (n - 1 - i)) & 1 for i in range(n)]
    skip = (-len(bits)) % 8
    bits = [0] * skip + bits
    stream = bytearray(len(bits) // 8)
    for i, b in enumerate(bits):
        stream[len(stream) - 1 - i // 8] |= b << (i % 8)
    n = len(dest)
    return (b"PP20" + bytes(widths) + bytes(stream)
            + bytes([(n >> 16) & 255, (n >> 8) & 255, n & 255, skip]))


# "ABCABCQ": four literals (written from the end: Q, C, B, A), then a copy of
# three bytes from two places further on.
_ABCABCQ = [(1, 0), (2, 3), (2, 0),
            (8, ord("Q")), (8, ord("C")), (8, ord("B")), (8, ord("A")),
            (2, 1), (10, 2)]


def test_a_powerpacker_file_unpacks():
    data = _pp20(_ABCABCQ, b"ABCABCQ")
    assert xpk.xpk_method(data) == "PP20"
    assert xpk.unpack(data) == b"ABCABCQ"


def test_a_long_copy_reads_its_length_in_three_bit_steps():
    # 1 literal "x", then a copy (code 3, 7-bit offset 0) of 5 + 7 + 5 = 17
    # bytes of the "x" just written → 18 x's.
    fields = [(1, 0), (2, 0), (8, ord("x")), (2, 3), (1, 0), (7, 0),
              (3, 7), (3, 5)]
    assert xpk.unpack(_pp20(fields, b"x" * 18)) == b"x" * 18


@pytest.mark.parametrize("cut", ["copy past the end", "truncated", "zero length"])
def test_damaged_powerpacker_data_is_an_xpk_error(cut):
    if cut == "copy past the end":
        data = _pp20(_ABCABCQ[:-1] + [(10, 900)], b"ABCABCQ")
    elif cut == "truncated":
        good = _pp20(_ABCABCQ, b"ABCABCQ")
        data = good[:8] + good[10:]
    else:
        data = b"PP20" + bytes((9, 10, 11, 11)) + b"\0\0\0\0" + b"\0\0\0\0"
    with pytest.raises(xpk.XpkError):
        xpk.unpack(data)


def test_a_packed_module_is_staged_unpacked_with_its_companions(tmp_path):
    (tmp_path / "HIP.tune.pp.hip").write_bytes(_pp20(_ABCABCQ, b"ABCABCQ"))
    (tmp_path / "smp.set").write_bytes(b"S")
    out = xpk.stage_for_uade(tmp_path / "HIP.tune.pp.hip")
    try:
        assert out.parent != tmp_path and out.name == "HIP.tune.pp.hip"
        assert out.read_bytes() == b"ABCABCQ"
        assert (out.parent / "smp.set").read_bytes() == b"S"
    finally:
        shutil.rmtree(out.parent)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["HIP.tune.pp.hip", "smp.set"]


def test_a_plain_module_needs_no_staging(tmp_path):
    (tmp_path / "mdat.song").write_bytes(b"\0" * 64)
    assert xpk.stage_for_uade(tmp_path / "mdat.song") is None


# ── YM-ST replay routines ────────────────────────────────────────────────────

def _ymst(replay: str) -> bytes:
    return (b"YMST\x80\x00Y_" + b"\0" * 24 + b"Hippel 2 (Sid)\0Jochen Hippel\0"
            + replay.encode() + b"\0NuNuA\0" + b"\x4e\x75" * 40)


def test_a_ymst_module_names_its_replay_routine():
    assert uf.embedded_companions(_ymst("YM.MMME_Sid_replay")) == ("YM.MMME_Sid_replay",)
    assert uf.embedded_companions(b"YMST" + b"\0" * 100) == ()
    assert uf.embedded_companions(b"MMD0" + b"YM.MCoder_replay") == ()   # not YM-ST


def test_replay_routines_are_never_tracks():
    for name in ("ym.mcoder_replay", "ym.coso tfmx replay", "YM.MMME_Sid_replay",
                 "'ym.coso mmme_replay"):
        assert uf.is_companion_half(name), name
        assert uf.classify(name) is None
    assert not uf.is_companion_half("spaceball-mcoder.ymst")
    # Modland's other YM-ST folders store a routine as "<tune>-replay.ymst"
    for name in ("shadowdancer-replay.ymst", "ivanhoe - replay.ymst",
                 "no buddies land replay.ymst", "YMST/HotLine/nobuddiesland-replay.ymst"):
        assert uf.is_companion_half(name), name
    for name in ("instant replay theme.ymst", "replayer.ymst", "game-replay.mod",
                 "darksideofthespoon-tlb.ymst"):
        assert not uf.is_companion_half(name), name


def test_a_replay_spelled_otherwise_is_linked_under_the_name_the_module_asks_for(tmp_path):
    (tmp_path / "hippel2-sid.ymst").write_bytes(_ymst("YM.MMME_Sid_replay"))
    (tmp_path / "ym.mmme sid_replay").write_bytes(b"R")
    (tmp_path / "ym.mcoder_replay").write_bytes(b"other")
    out = xpk.stage_for_uade(tmp_path / "hippel2-sid.ymst")
    try:
        assert out.name == "hippel2-sid.ymst" and out.is_symlink()
        assert (out.parent / "YM.MMME_Sid_replay").read_bytes() == b"R"
        assert not (out.parent / "ym.mcoder_replay").exists()
    finally:
        shutil.rmtree(out.parent)


def test_a_replay_with_a_stray_quote_is_found(tmp_path):
    (tmp_path / "ohhcrickey-sheep.ymst").write_bytes(_ymst("YM.COSO_MMME_replay"))
    (tmp_path / "'ym.coso mmme_replay").write_bytes(b"R")
    (tmp_path / "ym.coso tfmx replay").write_bytes(b"other")
    out = xpk.stage_for_uade(tmp_path / "ohhcrickey-sheep.ymst")
    try:
        assert (out.parent / "YM.COSO_MMME_replay").read_bytes() == b"R"
    finally:
        shutil.rmtree(out.parent)


def test_a_replay_beside_under_its_own_name_needs_no_staging(tmp_path):
    (tmp_path / "x.ymst").write_bytes(_ymst("YM.MCoder_replay"))
    (tmp_path / "YM.MCoder_replay").write_bytes(b"R")
    assert xpk.stage_for_uade(tmp_path / "x.ymst") is None


# ── Atari ST COSO (.soc) ─────────────────────────────────────────────────────

@pytest.fixture
def _alt_player(tmp_path, monkeypatch):
    players = tmp_path / "uade" / "players"
    players.mkdir(parents=True)
    (players / "Jochen_Hippel_ST").write_bytes(b"player")
    conf = tmp_path / "uade" / "eagleplayer.conf"
    conf.write_text("Jochen_Hippel_ST\tprefixes=hst\n")
    monkeypatch.setattr(uf, "conf_path", lambda: conf)
    monkeypatch.setattr(uf, "_PICK_CACHE", {})
    return players


@pytest.mark.parametrize("own,other,want", [
    (500, 11000, "tune.hst"),       # Atari ST COSO: the CoSo player plays it silent
    (0, 9000, "tune.hst"),          # … or refuses it
    (5100, 511, "tune.soc"),        # Amiga COSO: the ST player only drones
    (3000, 3000, "tune.hst"),       # a tie goes to the ST player
])
def test_a_soc_module_is_named_for_the_player_that_plays_it(tmp_path, monkeypatch,
                                                            _alt_player, own, other, want):
    calls = []

    def fake(binary, path, extra):
        calls.append(tuple(extra))
        return other if extra else own
    monkeypatch.setattr(uf, "_distinct_values", fake)
    f = tmp_path / "tune.soc"
    f.write_bytes(b"COSO" + b"\0" * 60)
    assert uf.play_name(f, "/bin/uade123") == want
    assert calls == [(), ("-P", str(_alt_player / "Jochen_Hippel_ST"))]
    assert uf.play_name(f, "/bin/uade123") == want and len(calls) == 2   # remembered


def test_other_modules_keep_their_name(tmp_path, _alt_player, monkeypatch):
    monkeypatch.setattr(uf, "_distinct_values", lambda *a: pytest.fail("rendered"))
    assert uf.play_name(tmp_path / "a.hipc", "/bin/uade123") == "a.hipc"
    assert uf.play_name("thalionb.soc") == "thalionb.hst"      # no uade: the ST player


def test_a_soc_module_is_staged_under_its_play_name(tmp_path, _alt_player, monkeypatch):
    monkeypatch.setattr(uf, "_distinct_values", lambda b, p, extra: 9 if extra else 1)
    (tmp_path / "thalionb.soc").write_bytes(b"COSO" + b"\0" * 60)
    (tmp_path / "smp.set").write_bytes(b"S")
    out = xpk.stage_for_uade(tmp_path / "thalionb.soc", "/bin/uade123")
    try:
        assert out.name == "thalionb.hst" and out.read_bytes()[:4] == b"COSO"
        assert (out.parent / "smp.set").exists()
    finally:
        shutil.rmtree(out.parent)


# ── a remote YM-ST module brings its replay routine ──────────────────────────

class _Entry:
    def __init__(self, name, path, size=1):
        self.name, self.path, self.is_dir, self.size = name, path, False, size


class _Source:
    def __init__(self, files):
        self.files = files
        self.read = []

    def stat(self, p):
        class S:
            size, mtime = len(self.files[p]), 1
        return S()

    def list_dir(self, d):
        return [_Entry(p.rsplit("/", 1)[1], p) for p in self.files if p.rsplit("/", 1)[0] == d]

    def read_file(self, p, lane="stream"):
        self.read.append(p)
        return self.files[p]


class _Cache:
    def __init__(self, src, d):
        self.src, self.dir = src, d
        d.mkdir()

    def fetch(self, scan_root, remote_path, source, lane="stream"):
        out = self.dir / f"{id(source)}{remote_path.replace('/', '_')}"
        out.write_bytes(source.read_file(remote_path, lane=lane))
        return out


@pytest.mark.asyncio
async def test_a_remote_ymst_module_is_fetched_with_the_replay_it_names(monkeypatch, tmp_path):
    from soniqboom.api import stream
    from soniqboom.core import remote_cache
    files = {"/YMST/JH/hippel2-sid.ymst": _ymst("YM.MMME_Sid_replay"),
             "/YMST/JH/ym.mmme sid_replay": b"R1",
             "/YMST/JH/ym.mcoder_replay": b"R2",
             "/YMST/JH/other.ymst": _ymst("YM.MCoder_replay")}
    src = _Source(files)
    cache = _Cache(src, tmp_path / "rc")
    monkeypatch.setattr(remote_cache, "get_cache", lambda: cache)
    zx = tmp_path / "zx"
    zx.mkdir()
    monkeypatch.setattr(stream, "_zip_extract_dir", lambda: zx)
    monkeypatch.setattr(stream, "_register_adlib_extract", lambda *a, **k: None)
    monkeypatch.setattr(stream, "_zip_evict_until_under_budget", lambda *a, **k: None)
    got = await stream._materialize_loose_remote_uade(
        "ymst1", "share", "/YMST/JH/hippel2-sid.ymst", src)
    assert got is not None and got.read_bytes() == files["/YMST/JH/hippel2-sid.ymst"]
    assert (got.parent / "ym.mmme sid_replay").read_bytes() == b"R1"
    assert "/YMST/JH/ym.mcoder_replay" not in src.read      # only the one it names
    out = xpk.stage_for_uade(got)
    try:
        assert (out.parent / "YM.MMME_Sid_replay").read_bytes() == b"R1"
    finally:
        shutil.rmtree(out.parent)


@pytest.mark.asyncio
async def test_uade_gets_the_staged_copy_and_it_is_removed(tmp_path, monkeypatch):
    from soniqboom.api import stream
    (tmp_path / "HIP.x.pp").write_bytes(_pp20(_ABCABCQ, b"ABCABCQ"))
    async with stream._xpk_unpacked(tmp_path / "HIP.x.pp") as src:
        assert src.parent != tmp_path and src.read_bytes() == b"ABCABCQ"
        staged = src.parent
    assert not staged.exists()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_companion_that_failed_to_download_is_fetched_again_next_play(monkeypatch, tmp_path):
    from soniqboom.api import stream
    from soniqboom.core import remote_cache
    files = {"/YMST/JH/crazycomets.ymst": _ymst("YM.LCD_replay"),
             "/YMST/JH/ym.lcd_replay": b"R"}
    src = _Source(files)
    cache = _Cache(src, tmp_path / "rc")
    broken = {"on": True}
    real_fetch = cache.fetch

    def fetch(scan_root, remote_path, source, lane="stream"):
        if broken["on"] and remote_path.endswith("_replay"):
            raise OSError("426 transfer aborted")
        return real_fetch(scan_root, remote_path, source, lane=lane)
    cache.fetch = fetch
    monkeypatch.setattr(remote_cache, "get_cache", lambda: cache)
    zx = tmp_path / "zx"
    zx.mkdir()
    monkeypatch.setattr(stream, "_zip_extract_dir", lambda: zx)
    monkeypatch.setattr(stream, "_register_adlib_extract", lambda *a, **k: None)
    monkeypatch.setattr(stream, "_zip_evict_until_under_budget", lambda *a, **k: None)
    first = await stream._materialize_loose_remote_uade(
        "cc1", "share", "/YMST/JH/crazycomets.ymst", src)
    assert first is not None and not (first.parent / "ym.lcd_replay").exists()
    marker = first.parent / ".loose_marker"
    assert "|partial|" in marker.read_text()
    marker.write_text(marker.read_text().rpartition("|partial|")[0] + "|partial|0")
    broken["on"] = False
    again = await stream._materialize_loose_remote_uade(
        "cc1", "share", "/YMST/JH/crazycomets.ymst", src)
    assert (again.parent / "ym.lcd_replay").read_bytes() == b"R"


# ── sample folders, short PumaTracker files, a second player ─────────────────

def _smus(*instruments: str) -> bytes:
    import struct
    chunks = b""
    for i, n in enumerate(instruments):
        body = bytes([i, 0, 0, 0]) + n.encode() + b"\0"
        chunks += b"INS1" + struct.pack(">I", len(body)) + body + (b"\0" if len(body) & 1 else b"")
    return b"FORM" + struct.pack(">I", 4 + len(chunks)) + b"SMUS" + chunks


def test_a_sonix_score_wants_its_instruments_folder():
    assert uf.companion_dirs("a.smus", _smus("Synth4", "Bass")) == [
        ("Instruments", 0, ("synth4", "bass"))]
    assert uf.companion_dirs("x.mod", b"M.K.") == []


@pytest.mark.skipif(not uf.player_map(), reason="uade conf missing")
def test_a_zoundmonitor_tune_wants_the_shared_samples_folder(tmp_path):
    assert uf.companion_dirs("universe.sng") == [("Samples", 2, None)]
    fmt = tmp_path / "Zoundmonitor"
    (fmt / "Samples").mkdir(parents=True)
    (fmt / "Samples" / "funbass").write_bytes(b"S")
    (fmt / "Sffen").mkdir()
    (fmt / "Sffen" / "universe.sng").write_bytes(b"\0" * 64)
    out = xpk.stage_for_uade(fmt / "Sffen" / "universe.sng")
    try:
        assert (out.parent / "Samples" / "funbass").read_bytes() == b"S"
    finally:
        shutil.rmtree(out.parent)


def _puma(file_len: int) -> bytes:
    import struct
    head = bytearray(0x50)
    struct.pack_into(">I", head, 0x14, 0x40)          # sample 1 at 0x40 …
    struct.pack_into(">H", head, 0x3C, 0x20)          # … 0x20 words long → ends at 0x80
    return bytes(head) + bytes(max(0, file_len - 0x50))


@pytest.mark.skipif(not uf.player_map(), reason="uade conf missing")
def test_a_pumatracker_module_cut_short_is_padded(tmp_path):
    assert uf.puma_padded("cyf.puma", _puma(0x78)) == _puma(0x78) + bytes(8)
    assert uf.puma_padded("cyf.puma", _puma(0x80)) is None          # whole
    assert uf.puma_padded("cyf.mod", _puma(0x78)) is None           # not Puma
    (tmp_path / "cyf.puma").write_bytes(_puma(0x78))
    out = xpk.stage_for_uade(tmp_path / "cyf.puma")
    try:
        assert len(out.read_bytes()) == 0x80
    finally:
        shutil.rmtree(out.parent)


@pytest.mark.parametrize("own,other,want", [
    (False, True, True),      # the listed player refuses it, the other plays it
    (True, True, False),      # the listed player plays it: kept
    (False, False, False),    # neither: no change (the refusal stays honest)
])
def test_a_jo_module_gets_the_player_that_accepts_it(tmp_path, monkeypatch, own, other, want):
    players = tmp_path / "uade" / "players"
    players.mkdir(parents=True)
    (players / "JesperOlsen_EP").write_bytes(b"p")
    conf = tmp_path / "uade" / "eagleplayer.conf"
    conf.write_text("JesperOlsen\tprefixes=jo\n")
    monkeypatch.setattr(uf, "conf_path", lambda: conf)
    monkeypatch.setattr(uf, "_ARGS_CACHE", {})
    monkeypatch.setattr(uf, "_accepts", lambda b, p, extra: other if extra else own)
    f = tmp_path / "georgglaxo ingame.jo"
    f.write_bytes(b"\0" * 32)
    args = uf.player_args(f, "/bin/uade123")
    assert args == (["-P", str(players / "JesperOlsen_EP")] if want else [])
    assert uf.player_args(tmp_path / "x.mod", "/bin/uade123") == []


def test_the_catalogue_gate_lets_a_play_read_its_sample_folder():
    from soniqboom.core import catalogue
    g = catalogue._Grant(root="r", rel="Zoundmonitor/Sffen/universe.sng",
                         dirs=catalogue._grant_dirs("Zoundmonitor/Sffen/universe.sng"),
                         expiry=1e18) if hasattr(catalogue, "_Grant") else None
    if g is None:
        pytest.skip("no _Grant")
    ok = lambda rel: catalogue._allows(g, "r", rel, 0.0)      # noqa: E731
    assert ok("Zoundmonitor/Samples/funbass")
    assert ok("Zoundmonitor/Sffen/Instruments/x.instr")
    assert not ok("Zoundmonitor/Other/x.sng")
    assert not ok("Elsewhere/Samples/x")


@pytest.mark.asyncio
@pytest.mark.skipif(not uf.player_map(), reason="uade conf missing")
async def test_a_remote_tune_is_fetched_with_its_sample_folder(monkeypatch, tmp_path):
    from soniqboom.api import stream
    from soniqboom.core import remote_cache

    class Src(_Source):
        def list_dir(self, d):
            d = d.rstrip("/")
            out, seen = [], set()
            for p in self.files:
                head, _, rest = p.partition(d + "/")
                if head or not rest:
                    continue
                first = rest.split("/", 1)[0]
                if "/" in rest:
                    if first not in seen:
                        seen.add(first)
                        e = _Entry(first, f"{d}/{first}")
                        e.is_dir = True
                        out.append(e)
                else:
                    out.append(_Entry(first, p))
            return out
    files = {"/Zoundmonitor/Sffen/universe.sng": b"\0" * 64,
             "/Zoundmonitor/Samples/funbass": b"FB",
             "/Zoundmonitor/Samples/koto": b"KO",
             "/IFF-SMUS/A/Sphere/Sphere.smus": _smus("Synth4"),
             "/IFF-SMUS/A/Sphere/Instruments/Synth4.instr": b"I",
             "/IFF-SMUS/A/Sphere/Instruments/Synth4.ss": b"SS",
             "/IFF-SMUS/A/Sphere/Instruments/Other.instr": b"no"}
    src = Src(files)
    cache = _Cache(src, tmp_path / "rc")
    monkeypatch.setattr(remote_cache, "get_cache", lambda: cache)
    zx = tmp_path / "zx"
    zx.mkdir()
    monkeypatch.setattr(stream, "_zip_extract_dir", lambda: zx)
    monkeypatch.setattr(stream, "_register_adlib_extract", lambda *a, **k: None)
    monkeypatch.setattr(stream, "_zip_evict_until_under_budget", lambda *a, **k: None)
    zm = await stream._materialize_loose_remote_uade(
        "zm1", "share", "/Zoundmonitor/Sffen/universe.sng", src)
    assert (zm.parent / "Samples" / "funbass").read_bytes() == b"FB"
    assert (zm.parent / "Samples" / "koto").exists()
    # A share whose root is the format's folder: Samples sits in the root.
    root = Src({"/Sffen/universe.sng": b"\0" * 64, "/Samples/koto": b"KO"})
    root.list_dir = (lambda d, _l=root.list_dir: _l("" if d == "/" else d))
    zr = await stream._materialize_loose_remote_uade("zm2", "share", "/Sffen/universe.sng", root)
    assert (zr.parent / "Samples" / "koto").read_bytes() == b"KO"
    sx = await stream._materialize_loose_remote_uade(
        "sx1", "share", "/IFF-SMUS/A/Sphere/Sphere.smus", src)
    assert (sx.parent / "Instruments" / "Synth4.instr").read_bytes() == b"I"
    assert (sx.parent / "Instruments" / "Synth4.ss").read_bytes() == b"SS"
    assert not (sx.parent / "Instruments" / "Other.instr").exists()


@pytest.mark.asyncio
async def test_a_cancelled_play_leaves_no_staged_folder(tmp_path, monkeypatch):
    import tempfile
    from soniqboom.api import stream
    started = __import__("threading").Event()
    release = __import__("threading").Event()
    real = xpk.stage_for_uade
    made = []

    def slow(path, binary=None):
        out = real(path, binary)
        made.append(out)
        started.set()
        release.wait(5)
        return out
    monkeypatch.setattr(xpk, "stage_for_uade", slow)
    (tmp_path / "HIP.x.pp").write_bytes(_pp20(_ABCABCQ, b"ABCABCQ"))

    async def play():
        async with stream._xpk_unpacked(tmp_path / "HIP.x.pp"):
            await asyncio.sleep(10)
    task = asyncio.ensure_future(play())
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if made and not made[0].parent.exists():
            break
    assert made and not made[0].parent.exists()
    del tempfile


@pytest.mark.asyncio
async def test_a_partial_folder_is_kept_for_an_hour_then_made_again(monkeypatch, tmp_path):
    from soniqboom.api import stream
    from soniqboom.core import remote_cache
    files = {"/Y/a.ymst": _ymst("YM.LCD_replay"), "/Y/ym.lcd_replay": b"R"}
    src = _Source(files)
    cache = _Cache(src, tmp_path / "rc")
    real_fetch = cache.fetch
    cache.fetch = lambda r, p, s, lane="stream": (_ for _ in ()).throw(OSError("x")) \
        if p.endswith("_replay") else real_fetch(r, p, s, lane=lane)
    monkeypatch.setattr(remote_cache, "get_cache", lambda: cache)
    zx = tmp_path / "zx"
    zx.mkdir()
    monkeypatch.setattr(stream, "_zip_extract_dir", lambda: zx)
    monkeypatch.setattr(stream, "_register_adlib_extract", lambda *a, **k: None)
    monkeypatch.setattr(stream, "_zip_evict_until_under_budget", lambda *a, **k: None)
    await stream._materialize_loose_remote_uade("p1", "share", "/Y/a.ymst", src)
    n = len(src.read)
    await stream._materialize_loose_remote_uade("p1", "share", "/Y/a.ymst", src)
    assert len(src.read) == n                     # within the hour: not fetched again
    marker = zx / "p1.uade" / ".loose_marker"
    head, _, _ = marker.read_text().rpartition("|partial|")
    marker.write_text(f"{head}|partial|{0}")      # an hour (and more) ago
    cache.fetch = real_fetch
    again = await stream._materialize_loose_remote_uade("p1", "share", "/Y/a.ymst", src)
    assert (again.parent / "ym.lcd_replay").read_bytes() == b"R"
