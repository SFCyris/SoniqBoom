"""XPK-SQSH unpacking of Amiga modules, and clear errors for renders that
produce nothing (a PSF rip without its libraries, a renderer that exits 0
without audio)."""
import base64
import hashlib
import shutil
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from soniqboom.core import xpk

# ``V0yager/AHX.Warriors.ahx`` from Aminet's AHXSONGS.LHA: an AHX module
# packed with XPK-SQSH (528 bytes → 2514).
WARRIORS_XPK = base64.b64decode(
    "WFBLRgAAAghTUVNIAAAJ0lRIWAAJtgAFAABACwQAAQAAfQAAAWE4WAHaCdIJ0lQGPAsPcprZ9gWA"
    "cCavOnj0sTEXOVcHVfwritaKA7ZPdPygPsoAAHxAN8ctDzBFKKg8wLzCWAW37IFZBoBAAD5RMECk"
    "4gLwAviBfgU/cDviAHALRBZp4dAEsJjQR0BfQC+AH9wF/QHfoFP/i/0evuF8sIjzF//wL/8C//Au"
    "Qtf1JvziBjzzAv0iN+gL+kTEKfOC4g84n/oL/oHfoNYw8yD/QLxx6Ee9P1kBf+En/hd/4XekAcif"
    "nAf9Av7BPe0mL2R79AC/+Af/hd+s3MUJyShHtQChS+IC6AAg+EFxZ8II+gdiT4hH6GsjifIC8F8S"
    "e4mkD4gp84KBYN0FljCuAF6AL8QEfcAv/hf/4X/eF//gB/4Xf+F3/hdal/0TW/orqesvv84xI0/8"
    "L/ScXklB/yTRwArJufktQS0h7gF9FkP6C/En/hf2A+/rbv8wC9gAGCdD/lZV/8X/0lmf/c//+5//"
    "9133ufsAdgRg0AXH4XJ3+wrCwf5oEEoTId8UGQeCegDo4MzjQUMB10aAJqfoAHQDueCHPPCHDzyC"
    "R9IV+oNwxw5wG7QyhHG7p2ABwBCPAH/+APwsyTOmWFsHBzAX6ltrwCen/3/hi1yEL+cPAScpB4J/"
    "ZmUCwAUAAA8PAAAAAAAA"
)
WARRIORS_MD5 = "d9f5c2c6e70d75a2d0f143784b933b59"


def test_sqsh_unpacks_to_the_module():
    assert xpk.xpk_method(WARRIORS_XPK) == "SQSH"
    out = xpk.unpack(WARRIORS_XPK)
    assert len(out) == int.from_bytes(WARRIORS_XPK[12:16], "big") == 2514
    assert out[:4] == b"THX\0"
    assert hashlib.md5(out).hexdigest() == WARRIORS_MD5


def test_unpacked_data_is_returned_unchanged():
    assert xpk.xpk_method(b"THX\0rest") is None
    assert xpk.unpack(b"THX\0rest") == b"THX\0rest"


def test_other_xpk_methods_are_refused_by_name():
    nuke = WARRIORS_XPK[:8] + b"NUKE" + WARRIORS_XPK[12:]
    with pytest.raises(xpk.XpkUnsupported) as exc:
        xpk.unpack(nuke)
    assert exc.value.method == "NUKE"


@pytest.mark.parametrize("damage", [
    lambda b: b[:200],                                       # truncated
    lambda b: b[:100] + bytes([b[100] ^ 0x55]) + b[101:],    # a flipped byte
    lambda b: b[:12] + (99999).to_bytes(4, "big") + b[16:],  # a wrong unpacked size
    lambda b: b[:4] + (4).to_bytes(4, "big") + b[8:],        # an impossible packed size
    lambda b: b[:38] + bytes([b[38] ^ 1]) + b[39:],          # only the chunk checksum
])
def test_damaged_sqsh_raises_xpk_error(damage):
    with pytest.raises(xpk.XpkError):
        xpk.unpack(damage(WARRIORS_XPK))


def test_the_unpacked_copy_sits_in_its_own_folder_with_its_companions(tmp_path):
    (tmp_path / "mdat.song").write_bytes(b"packed")
    (tmp_path / "smpl.song").write_bytes(b"samples")
    (tmp_path / "other.mod").write_bytes(b"x")
    out = xpk.write_unpacked(tmp_path / "mdat.song", b"unpacked", ["smpl.song", "SMP.song"])
    try:
        assert out.parent != tmp_path and out.name == "mdat.song"
        assert out.read_bytes() == b"unpacked"
        assert sorted(p.name for p in out.parent.iterdir()) == ["mdat.song", "smpl.song"]
        assert (out.parent / "smpl.song").read_bytes() == b"samples"
        assert (tmp_path / "mdat.song").read_bytes() == b"packed"   # never written to
    finally:
        shutil.rmtree(out.parent)


async def test_uade_gets_an_unpacked_copy_removed_afterwards(tmp_path):
    from soniqboom.api import stream
    packed = tmp_path / "AHX.Warriors.ahx"
    packed.write_bytes(WARRIORS_XPK)
    async with stream._xpk_unpacked(packed) as src:
        assert src != packed and hashlib.md5(src.read_bytes()).hexdigest() == WARRIORS_MD5
        tmp_dir = src.parent
    assert not tmp_dir.exists()
    plain = tmp_path / "plain.ahx"
    plain.write_bytes(b"THX\0" + bytes(100))
    async with stream._xpk_unpacked(plain) as src:
        assert src == plain


@pytest.mark.parametrize("data, needle", [
    (WARRIORS_XPK[:8] + b"NUKE" + WARRIORS_XPK[12:], "XPK-NUKE"),
    (WARRIORS_XPK[:300], "damaged"),
])
async def test_a_module_that_cannot_be_unpacked_says_why(tmp_path, data, needle):
    from soniqboom.api import stream
    f = tmp_path / "x.ahx"
    f.write_bytes(data)
    with pytest.raises(HTTPException) as exc:
        async with stream._xpk_unpacked(f):
            pass
    assert exc.value.status_code == 422 and needle in exc.value.detail


@pytest.mark.skipif(not shutil.which("uade123"), reason="uade123 not installed")
async def test_a_packed_ahx_renders(tmp_path):
    from soniqboom.api import stream
    packed = tmp_path / "AHX.Warriors.ahx"
    packed.write_bytes(WARRIORS_XPK)
    wav = await stream._render_uade(packed, with_vu=False)
    try:
        assert stream._render_has_audio(wav)
        assert wav.stat().st_size > 44100 * 4 * 10          # well over 10 s of audio
    finally:
        wav.unlink(missing_ok=True)


def test_a_packed_ahx_is_scanned_with_its_module_name(tmp_path):
    from soniqboom.core import metadata
    packed = tmp_path / "AHX.Warriors.ahx"
    packed.write_bytes(WARRIORS_XPK)
    meta = metadata.extract(packed, "t1")
    assert (meta.title, meta.format) == ("Warriors", "AHX")


def test_uade_saying_a_module_is_packed_is_a_clear_422():
    from soniqboom.api import stream
    exc = stream._renderer_failure(
        "uade", "uade123", 1,
        "uade: The file is XYZ packed. Please depack first. You may use …")
    assert exc.status_code == 422 and "unpack it first" in exc.detail


# ── PSF libraries ─────────────────────────────────────────────────────────────

def _minipsf(tags: str) -> bytes:
    return b"PSF\x01" + bytes(12) + b"[TAG]" + tags.encode()


def test_psf_libraries_missing_beside_the_rip_are_named(tmp_path):
    from soniqboom.api import stream
    rip = tmp_path / "12 - kiss in the dark.minipsf"
    rip.write_bytes(_minipsf("_lib=driver.psflib\n_lib2=Vab_m5.psflib\ngame=Metal Slug X\n"))
    assert stream._psf_missing_libs(rip) == ["driver.psflib", "Vab_m5.psflib"]
    (tmp_path / "DRIVER.PSFLIB").write_bytes(b"PSF\x01")         # any case
    assert stream._psf_missing_libs(rip) == ["Vab_m5.psflib"]
    (tmp_path / "Vab_m5.psflib").write_bytes(b"PSF\x01")
    assert stream._psf_missing_libs(rip) == []


def test_a_psf_without_libraries_needs_none(tmp_path):
    from soniqboom.api import stream
    rip = tmp_path / "song.psf"
    rip.write_bytes(_minipsf("title=Song\n"))
    assert stream._psf_missing_libs(rip) == []
    (tmp_path / "junk.psf").write_bytes(b"not a psf")
    assert stream._psf_missing_libs(tmp_path / "junk.psf") == []


async def test_a_psf_rip_without_its_libraries_is_a_clear_422(tmp_path, monkeypatch):
    from soniqboom.api import stream
    monkeypatch.setattr(stream, "_find_renderer", lambda *_: sys.executable)
    rip = tmp_path / "song.minipsf"
    rip.write_bytes(_minipsf("_lib=driver.psflib\n"))
    with pytest.raises(HTTPException) as exc:
        await stream._render_psf(rip)
    assert exc.value.status_code == 422
    assert "driver.psflib" in exc.value.detail and "missing" in exc.value.detail


# ── a renderer that exits 0 without audio ─────────────────────────────────────

def _wav(frames: bytes, extra: bytes = b"") -> bytes:
    fmt = b"fmt " + (16).to_bytes(4, "little") + bytes(16)
    data = b"data" + len(frames).to_bytes(4, "little") + frames
    body = b"WAVE" + fmt + extra + data
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def test_render_output_without_frames_holds_no_audio(tmp_path):
    from soniqboom.api import stream
    cases = {
        "header.wav": (_wav(b""), False),
        "list.wav": (_wav(b"", b"LIST" + (36).to_bytes(4, "little") + bytes(36)), False),
        "audio.wav": (_wav(b"\x01\x00" * 100), True),
        "tiny.mp3": (b"ID3" + bytes(10), False),
        "some.mp3": (b"ID3" + bytes(4000), True),
    }
    for name, (data, has) in cases.items():
        (tmp_path / name).write_bytes(data)
        assert stream._render_has_audio(tmp_path / name) is has, name
    assert stream._render_has_audio(tmp_path / "missing.wav") is False


async def test_a_renderer_that_exits_0_without_audio_is_a_clear_422(tmp_path):
    from soniqboom.api import stream
    out = tmp_path / "out.wav"
    with pytest.raises(HTTPException) as exc:
        await stream._await_renderer([sys.executable, "-c", "pass"], out, timeout=30, kind="PSF")
    assert exc.value.status_code == 422
    assert exc.value.detail == "The renderer finished but produced no audio for this file."
    write = f"open({str(out)!r}, 'wb').write({_wav(b'\x01\x00' * 64)!r})"
    await stream._await_renderer([sys.executable, "-c", write], out, timeout=30, kind="PSF")
    assert out.exists()


# ── more XPK coverage: stored chunks, real multi-chunk streams, scan / probe ──

def _sqsh(chunks: list[bytes]) -> bytes:
    """An XPK-SQSH file of stored (type 0) chunks — what a packer writes for
    data that doesn't compress; each chunk carries its real checksum."""
    body = b""
    for data in chunks:
        padded = data + bytes(-len(data) % 4)
        s = 0
        for i in range(0, len(padded), 4):
            s ^= int.from_bytes(padded[i:i + 4], "big")
        csum = (s ^ (s >> 16)) & 0xFFFF
        body += (bytes([0, 0]) + csum.to_bytes(2, "big") + len(data).to_bytes(2, "big")
                 + len(data).to_bytes(2, "big") + data)
    out_len = sum(len(d) for d in chunks)
    head = b"XPKF" + (36 + len(body) - 8).to_bytes(4, "big") + b"SQSH" + out_len.to_bytes(4, "big")
    return head + bytes(20) + body


def test_stored_chunks_of_odd_sizes_unpack_in_order():
    parts = [bytes(range(7)), b"\xff" * 13, b"abc", bytes(range(200, 256)) * 3]
    assert xpk.unpack(_sqsh(parts)) == b"".join(parts)


def test_a_stored_chunk_checksum_ignores_the_bytes_after_it():
    # The three bytes after a chunk (here the next chunk's header) are
    # counted as zero; a 5-byte chunk leaves 3 of them in the last word.
    assert xpk.unpack(_sqsh([b"\x11\x22\x33\x44\x55", b"\x99" * 9])) == b"\x11\x22\x33\x44\x55" + b"\x99" * 9


_XPK_SAMPLES = Path(__file__).resolve().parent.parent / "internal" / "format-samples" / "xpk"


@pytest.mark.skipif(not (_XPK_SAMPLES / "PRU2.PDX-Perihelion").exists(), reason="libxmp vector not present")
def test_libxmp_s_packed_module_unpacks_as_libxmp_does():
    out = xpk.unpack((_XPK_SAMPLES / "PRU2.PDX-Perihelion").read_bytes())
    assert hashlib.md5(out).hexdigest() == "70e931a4c9835cd60493ae3d22b0cea6"


@pytest.mark.skipif(not (_XPK_SAMPLES / "test_C1_sqsh.xpkf").exists(), reason="ancient vector not present")
def test_a_multi_chunk_stream_from_another_packer_unpacks_exactly():
    packed = (_XPK_SAMPLES / "test_C1_sqsh.xpkf").read_bytes()
    assert xpk.unpack(packed) == (_XPK_SAMPLES / "test_C1.raw").read_bytes()
    assert xpk.unpack_file(_XPK_SAMPLES / "test_C1_sqsh.xpkf") == (_XPK_SAMPLES / "test_C1.raw").read_bytes()


def test_unpack_file_reads_plain_files_whole(tmp_path):
    f = tmp_path / "plain.mod"
    f.write_bytes(b"M.K." * 100)
    assert xpk.unpack_file(f) == b"M.K." * 100
    g = tmp_path / "huge.ahx"
    g.write_bytes(b"XPKF" + (0x7FFFFFFF).to_bytes(4, "big") + b"SQSH" + bytes(8))
    with pytest.raises(xpk.XpkError):
        xpk.unpack_file(g)


async def test_a_packed_module_is_unpacked_beside_its_companion(tmp_path):
    from soniqboom.api import stream
    (tmp_path / "mdat.tune").write_bytes(_sqsh([b"TFMX-SONG " + bytes(40)]))
    (tmp_path / "SMPL.tune").write_bytes(b"samples")          # companions match in any case
    async with stream._xpk_unpacked(tmp_path / "mdat.tune") as src:
        assert src.read_bytes() == b"TFMX-SONG " + bytes(40)
        assert (src.parent / "SMPL.tune").read_bytes() == b"samples"


@pytest.mark.skipif(not shutil.which("uade123"), reason="uade123 not installed")
def test_the_scan_probe_reads_a_packed_module(tmp_path):
    import tempfile as _tf
    from soniqboom.core import metadata
    f = tmp_path / "warriors.ahx"
    f.write_bytes(_sqsh([xpk.unpack(WARRIORS_XPK)]))
    before = {p.name for p in Path(_tf.gettempdir()).glob("sb-xpk-*")}
    info = metadata.uade_get_info(f)
    assert info["_ok"] and info.get("modulename") == "Warriors"
    assert {p.name for p in Path(_tf.gettempdir()).glob("sb-xpk-*")} == before


_HIPPEL = Path(__file__).resolve().parent.parent / "internal" / "testdata" / "uade" / "Hippel" / "dragonflight unicorn.hip"


@pytest.mark.skipif(not (shutil.which("uade123") and _HIPPEL.exists()), reason="uade123 / Hippel sample missing")
async def test_the_subsong_probe_reads_a_packed_module(tmp_path):
    from soniqboom.api import stream
    plain = tmp_path / "dragonflight unicorn.hip"
    plain.write_bytes(_HIPPEL.read_bytes())
    base = await stream._probe_uade_base(plain)
    assert base == 1                                   # Hippel tunes count from 1
    packed = tmp_path / "packed" / "dragonflight unicorn.hip"
    packed.parent.mkdir()
    packed.write_bytes(_sqsh([_HIPPEL.read_bytes()]))
    assert await stream._probe_uade_base(packed) == base


# ── more render-output edge cases ─────────────────────────────────────────────

def test_wav_chunks_of_odd_length_and_long_headers(tmp_path):
    from soniqboom.api import stream
    odd = b"junk" + (3).to_bytes(4, "little") + b"abc\0"          # padded to even
    big = b"LIST" + (5000).to_bytes(4, "little") + bytes(5000)
    cases = {"odd_empty.wav": (_wav(b"", odd), False), "odd_audio.wav": (_wav(b"\x01\x00" * 50, odd), True),
             "big_audio.wav": (_wav(b"\x01\x00" * 50, big), True)}
    for name, (data, has) in cases.items():
        (tmp_path / name).write_bytes(data)
        assert stream._render_has_audio(tmp_path / name) is has, name


def test_psf_library_names_with_numbers_paths_and_backslashes(tmp_path):
    from soniqboom.api import stream
    rip = tmp_path / "song.minipsf"
    rip.write_bytes(_minipsf("_lib=a.psflib\n_lib10=sub\\b.psflib\n_lib2=libs/c.psflib\n"))
    assert stream._psf_missing_libs(rip) == ["a.psflib", "sub\\b.psflib", "libs/c.psflib"]
    (tmp_path / "a.psflib").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "B.PSFLIB").write_bytes(b"x")
    (tmp_path / "c.psflib").write_bytes(b"x")                 # beside the rip counts too
    assert stream._psf_missing_libs(rip) == []


@pytest.mark.skipif(not shutil.which("adplay"), reason="adplay not installed")
@pytest.mark.parametrize("ext, needle", [(".rol", "instrument bank"), (".laa", "corrupt")])
async def test_adlib_keeps_its_own_explanations(tmp_path, ext, needle):
    import random
    from soniqboom.api import stream
    f = tmp_path / f"junk{ext}"
    f.write_bytes(random.Random(7).randbytes(300))
    with pytest.raises(HTTPException) as exc:
        await stream._render_adlib(f)
    assert exc.value.status_code == 422 and needle in exc.value.detail


async def test_the_voice_meter_pass_gets_the_unpacked_module(tmp_path, monkeypatch):
    from soniqboom.api import stream
    seen = []

    async def fake_dump(binary, path, subsong, wav_path, *, base=0):
        seen.append((path.name, path.read_bytes()[:4], path.parent == tmp_path))
        return None
    monkeypatch.setattr(stream, "_uade_vu_dump_file", fake_dump)
    packed = tmp_path / "AHX.Warriors.ahx"
    packed.write_bytes(WARRIORS_XPK)
    await stream._uade_vu_dump("uade123", packed, 0, tmp_path / "x.wav")
    assert seen == [("AHX.Warriors.ahx", b"THX\0", False)]
    bad = tmp_path / "bad.ahx"
    bad.write_bytes(WARRIORS_XPK[:300])
    assert await stream._uade_vu_dump("uade123", bad, 0, tmp_path / "x.wav") is None
