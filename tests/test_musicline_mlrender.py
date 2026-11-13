# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Musicline Editor modules: uade123 renders a tune; one it renders only as
silence goes to mlrender (soniqboom/native/mlrender) when that is installed
(``stream._musicline_route``).  The modules here are synthesized
(``scripts/render_selftest.ml_tune``); uade's verdict is faked unless a test
says otherwise, and the real-binary tests skip without a built mlrender
(``SB_MLRENDER`` or PATH)."""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from soniqboom.core import musicline

ROOT = Path(__file__).resolve().parents[1]


def _selftest():
    spec = importlib.util.spec_from_file_location("render_selftest",
                                                  ROOT / "scripts/render_selftest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RS = _selftest()


def _tail_silence_tune() -> bytes:
    """``ml_tune`` with six more plays of a part whose instrument has volume
    0 before the END: 2.56 s of music, then digital silence."""
    d = RS.ml_tune()
    i = d.find(b"TUNE")
    n = struct.unpack(">I", d[i + 4:i + 8])[0]
    tune = d[i + 8:i + 8 + n]
    chdata = b"".join(struct.pack(">H", w) for w in [0x0110] + [0x0210] * 6 + [0x0060])
    tune = tune[:40] + struct.pack(">IIII", len(chdata), 0, 0, 0) + chdata
    rows = b"".join(b"\x01" + bytes([note, 2]) + b"\x00" * 7
                    for note in (0x19, 0x1B, 0x1D, 0x1E, 0x20, 0x22, 0x24, 0x25)) + b"\xff"
    part2 = b"PART" + struct.pack(">I", 2 + len(rows)) + struct.pack(">H", 2) + rows
    k = d.find(b"INST")
    m = struct.unpack(">I", d[k + 4:k + 8])[0]
    inst2 = bytearray(d[k:k + 8 + m])
    struct.pack_into(">H", inst2, 8 + 58, 0)                  # volume 0
    return (d[:i] + b"TUNE" + struct.pack(">I", len(tune)) + tune + d[i + 8 + n:k]
            + part2 + d[k:k + 8 + m] + bytes(inst2) + d[k + 8 + m:])


_INFO_LINE = re.compile(r"^subsong (\d+) (\d+\.\d+) (end|loop|silence|max) ?(.*)$")


def parse_info(text: str) -> dict:
    """``mlrender -i`` output → ``{"subsongs": n, "tunes": {i: {...}}}``."""
    count, tunes = 0, {}
    for line in text.splitlines():
        if line.startswith("subsongs "):
            count = int(line.split()[1])
        m = _INFO_LINE.match(line)
        if m:
            tunes[int(m.group(1))] = {"seconds": float(m.group(2)), "end": m.group(3),
                                      "name": m.group(4).strip()}
    return {"subsongs": count, "tunes": tunes}


@pytest.fixture(autouse=True)
def _fresh_verdicts():
    musicline.forget_all()
    yield
    musicline.forget_all()


# ── core.musicline ───────────────────────────────────────────────────────────

def test_digest_is_the_content_hash_of_a_musicline_module_only(tmp_path):
    import hashlib
    ml = tmp_path / "a.ml"
    ml.write_bytes(RS.ml_tune())
    assert musicline.digest(ml) == hashlib.sha1(ml.read_bytes()).hexdigest()
    other = tmp_path / "b.ml"
    other.write_bytes(b"MLEDxxxx" + b"\0" * 64)               # not "MLEDMODL"
    assert musicline.digest(other) is None
    assert musicline.digest(tmp_path / "missing.ml") is None


def test_verdicts_are_kept_per_content_tune_and_base_and_undecided_is_not():
    musicline.remember("aa", 0, 0, False)
    musicline.remember("aa", 1, 0, True)
    musicline.remember("bb", 0, 0, None)
    assert musicline.uade_verdict("aa", 0) is False
    assert musicline.uade_verdict("aa", 1) is True
    assert musicline.uade_verdict("aa", 1, base=1) is None
    assert musicline.uade_verdict("bb", 0) is None


def test_render_command_and_the_info_listing_parser():
    cmd = musicline.render_command("/x/mlrender", "/m/a.ml", "/t/o.wav", 3)
    assert cmd == ["/x/mlrender", "-s", "3", "-r", "44100", "-m", "600", "-f", "8000",
                   "-p", "0.3", "/m/a.ml", "/t/o.wav"]
    info = parse_info("subsongs 2\nsubsong 0 266.038 silence Xenex-Remix\n"
                                "subsong 1 11.860 loop The Beat\n")
    assert info == {"subsongs": 2, "tunes": {
        0: {"seconds": 266.038, "end": "silence", "name": "Xenex-Remix"},
        1: {"seconds": 11.86, "end": "loop", "name": "The Beat"}}}


# ── routing (stream._musicline_route) ────────────────────────────────────────

def _fake_tools(monkeypatch, stream, verdicts, *, mlrender="/opt/mlrender"):
    """mlrender at ``mlrender`` (None: not installed); uade's probe answers
    ``verdicts[path name]`` and records each call."""
    real = stream._find_renderer
    calls = []

    def find(conf, name):
        if name == "mlrender":
            return mlrender
        return real(conf, name)

    async def probe(src, index, base=0):
        calls.append((Path(src).name, index, base))
        return verdicts[Path(src).name]
    monkeypatch.setattr(stream, "_find_renderer", find)
    monkeypatch.setattr(stream, "_uade_pcm_verdict", probe)
    return calls


@pytest.mark.asyncio
async def test_a_tune_uade_renders_silent_goes_to_mlrender_once_probed(tmp_path, monkeypatch):
    from soniqboom.api import stream
    silent, plays = tmp_path / "silent.ml", tmp_path / "plays.ml"
    silent.write_bytes(RS.ml_tune())
    plays.write_bytes(RS.ml_tune(loop=True))                  # other content
    calls = _fake_tools(monkeypatch, stream, {"silent.ml": False, "plays.ml": True})
    assert await stream._musicline_route(silent, 0) == ("/opt/mlrender", False)
    assert await stream._musicline_route(silent, 0) == ("/opt/mlrender", False)   # warm
    assert await stream._musicline_route(plays, 0) == (None, True)                 # cold, other file
    assert calls == [("silent.ml", 0, 0), ("plays.ml", 0, 0)]
    # Another tune of the same file is probed on its own.
    assert await stream._musicline_route(silent, 1) == ("/opt/mlrender", False)
    assert calls[-1] == ("silent.ml", 1, 0)


@pytest.mark.asyncio
async def test_no_probe_without_mlrender_or_for_other_modules(tmp_path, monkeypatch):
    from soniqboom.api import stream
    ml = tmp_path / "a.ml"
    ml.write_bytes(RS.ml_tune())
    mod = tmp_path / "mod.tune"
    mod.write_bytes(b"M.K." + b"\0" * 2000)
    calls = _fake_tools(monkeypatch, stream, {"a.ml": False, "mod.tune": False})
    assert await stream._musicline_route(mod, 0) == (None, None)
    calls = _fake_tools(monkeypatch, stream, {"a.ml": False}, mlrender=None)
    assert await stream._musicline_route(ml, 0) == (None, None)
    assert calls == []


@pytest.mark.asyncio
async def test_an_undecided_probe_keeps_uade_and_is_asked_again(tmp_path, monkeypatch):
    from soniqboom.api import stream
    ml = tmp_path / "a.ml"
    ml.write_bytes(RS.ml_tune())
    calls = _fake_tools(monkeypatch, stream, {"a.ml": None})
    assert await stream._musicline_route(ml, 0) == (None, None)
    assert await stream._musicline_route(ml, 0) == (None, None)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_slot_held_probes_without_taking_a_render_slot(tmp_path, monkeypatch):
    from soniqboom.api import stream
    ml = tmp_path / "a.ml"
    ml.write_bytes(RS.ml_tune())
    _fake_tools(monkeypatch, stream, {"a.ml": False})

    class _NoSlot:
        async def __aenter__(self):
            raise AssertionError("took a render slot")

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(stream, "_render_sem", _NoSlot())
    assert await stream._musicline_route(ml, 0, slot_held=True) == ("/opt/mlrender", False)


@pytest.mark.asyncio
async def test_render_uade_hands_a_silent_tune_to_mlrender_and_keeps_the_rest(
        tmp_path, monkeypatch):
    from soniqboom.api import stream
    silent, plays = tmp_path / "silent.ml", tmp_path / "plays.ml"
    silent.write_bytes(RS.ml_tune())
    plays.write_bytes(RS.ml_tune(loop=True))
    _fake_tools(monkeypatch, stream, {"silent.ml": False, "plays.ml": True})
    seen = []

    async def fake_ml(binary, src, subsong=0, *, live_key=None, expected_seconds=0.0):
        seen.append(("mlrender", binary, Path(src).name, subsong, live_key, expected_seconds))
        return tmp_path / "ml.wav"

    async def fake_uade(path, *args, **kwargs):
        seen.append(("uade", Path(path).name, args, kwargs.get("subsong")))
        return tmp_path / "uade.wav"
    monkeypatch.setattr(stream, "_render_mlrender", fake_ml)
    monkeypatch.setattr(stream, "_render_uade_file", fake_uade)
    assert await stream._render_uade(silent, subsong=1, live_key="k",
                                     expected_seconds=12.5) == tmp_path / "ml.wav"
    assert await stream._render_uade(plays, subsong=0) == tmp_path / "uade.wav"
    assert seen == [("mlrender", "/opt/mlrender", "silent.ml", 1, "k", 12.5),
                    ("uade", "plays.ml", (), 0)]


@pytest.mark.asyncio
async def test_an_mlrender_play_proves_mlrender_and_a_uade_play_uade123(tmp_path, monkeypatch):
    from soniqboom.api import stream
    from soniqboom.core import unplayable
    silent, plays = tmp_path / "silent.ml", tmp_path / "plays.ml"
    silent.write_bytes(RS.ml_tune())
    plays.write_bytes(RS.ml_tune(loop=True))
    _fake_tools(monkeypatch, stream, {"silent.ml": False, "plays.ml": True})
    ran = []

    async def fake_await(cmd, out, *, timeout, kind):
        ran.append((Path(cmd[0]).name, kind))

    async def fake_uade(path, *args, **kwargs):
        ran.append(("uade123", "uade"))
        return tmp_path / "uade.wav"
    monkeypatch.setattr(stream, "_await_renderer", fake_await)
    monkeypatch.setattr(stream, "_render_uade_file", fake_uade)
    unplayable.reset_for_tests()
    try:
        out = await unplayable.render_checked(lambda: stream._render_uade(silent, subsong=0),
                                              "uade")
        Path(out).unlink(missing_ok=True)
        unplayable.rendered_ok("uade")
        assert ran == [("mlrender", "Musicline")]
        assert set(unplayable._proven) == {"mlrender"}
        # the next uade render in the same task is uade123's (no stale credit)
        await unplayable.render_checked(lambda: stream._render_uade(plays, subsong=0), "uade")
        unplayable.rendered_ok("uade")
        assert ran[-1] == ("uade123", "uade")
        assert set(unplayable._proven) == {"mlrender", "uade123"}
    finally:
        unplayable.reset_for_tests()


@pytest.mark.asyncio
async def test_the_tune_probe_judges_mlrender_for_a_silent_tune(tmp_path, monkeypatch):
    from soniqboom.api import stream
    silent, plays = tmp_path / "silent.ml", tmp_path / "plays.ml"
    silent.write_bytes(RS.ml_tune())
    plays.write_bytes(RS.ml_tune(loop=True))
    calls = _fake_tools(monkeypatch, stream, {"silent.ml": False, "plays.ml": True})
    probed = []

    async def fake_pcm(cmd, **kw):
        probed.append(cmd)
        return True
    monkeypatch.setattr(stream, "_pcm_probe", fake_pcm)
    assert await stream._tune_audible_run("uade", silent, 0) is True
    assert probed and probed[0][0] == "/opt/mlrender" and probed[0][-1] == "-"
    probed.clear()
    # uade plays the other file: its probe's verdict is the answer (one uade run).
    assert await stream._tune_audible_run("uade", plays, 0) is True
    assert probed == [] and calls.count(("plays.ml", 0, 0)) == 1


@pytest.mark.asyncio
async def test_no_uade_voice_pass_for_a_tune_mlrender_renders(tmp_path, monkeypatch):
    from soniqboom.api import stream
    silent = tmp_path / "silent.ml"
    silent.write_bytes(RS.ml_tune())
    _fake_tools(monkeypatch, stream, {"silent.ml": False})
    called = []

    async def fake_dump(*a, **kw):
        called.append(a)
        return None
    monkeypatch.setattr(stream, "_uade_vu_dump_file", fake_dump)
    assert await stream._uade_vu_dump("/usr/bin/uade123", silent, 0, tmp_path / "x.wav") is None
    assert called == []


def test_failures_name_the_musicline_player():
    from soniqboom.core import unplayable
    c = unplayable.classify("Musicline", 1, "mlrender: can't load this Musicline Editor "
                                            "module: /a/b.ml\n")
    assert c == ("mlrender", "can't load this file", True)
    # A write error is never the file's fault.
    assert unplayable.classify("Musicline", 1, "mlrender: write error on /t/x.wav") is None
    assert "mlrender" in unplayable.NAMES


def test_admin_reads_the_mlrender_version():
    from soniqboom.api import admin
    assert admin.renderer_version("mlrender", "mlrender 1.0 (musicline_playback 57b3e19d2f31)\n"
                                  )[0] == "1.0"
    assert ("mlrender", "mlrender_path") in admin._PROBE_RENDERER_SPECS


def test_the_build_fetches_only_the_port_at_the_dockerfile_pin():
    build = (ROOT / "soniqboom/native/mlrender/build.sh").read_text()
    docker = (ROOT / "Dockerfile").read_text()
    collector = (ROOT / "scripts/collect_third_party_sources.sh").read_text()
    pin = re.search(r"^ARG DEVILBOX_COMMIT=(\w+)$", docker, re.M).group(1)
    assert f'DEVILBOX_COMMIT="${{DEVILBOX_COMMIT:-{pin}}}"' in build
    assert 'DEVILBOX_COMMIT="$DEVILBOX_COMMIT"' in docker
    for text, var in ((build, "port"), (collector, "ML_PORT")):
        # The sparse patterns: DEViLBOX's licence files and the port's replayer only.
        patterns = re.search(r"printf '%s\\n' (.*?)> \"\$\w+/\.git/info/sparse-checkout\"",
                             text, re.S).group(1)
        got = re.findall(r'"?(/[^\s"\\]+)"?', patterns)
        assert got == ["/LICENSE", "/THIRD_PARTY_NOTICES.md", f"/${var}/*.cpp", f"/${var}/*.h",
                       f"/${var}/player/mline_backend.cpp", f"/${var}/player/mline_backend.h"]
        assert "core.sparseCheckout true" in text and "--filter=blob:none" in text
    assert "MAX_INTERNAL_TICK = 2400" in build


# ── the real binary ──────────────────────────────────────────────────────────

def _mlrender():
    p = os.environ.get("SB_MLRENDER") or shutil.which("mlrender")
    return p if p and os.path.exists(p) else None


def _wav_seconds(p: Path) -> float:
    b = p.read_bytes()
    j = b.find(b"data")
    return (len(b) - j - 8) / (44100 * 4)


needs_binary = pytest.mark.skipif(_mlrender() is None,
                                  reason="mlrender not built here (soniqboom/native/mlrender/build.sh)")


@needs_binary
@pytest.mark.asyncio
async def test_a_real_render_is_audible_and_as_long_as_its_listing(tmp_path):
    from soniqboom.api import stream
    from soniqboom.core.silence import wav_layout
    binary = _mlrender()
    out_v = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout
    assert re.match(r"mlrender 1\.1 \(musicline_playback [0-9a-f]{12}\)", out_v)
    for name, data, seconds, end in (("end.ml", RS.ml_tune(), 2.56, "end"),
                                     ("loop.ml", RS.ml_tune(loop=True), 10.583, "loop"),
                                     ("tail.ml", _tail_silence_tune(), 3.06, "silence")):
        src = tmp_path / name
        src.write_bytes(data)
        info = parse_info(subprocess.run(
            [binary, "-i", "-p", str(musicline.SEPARATION), str(src)],
            capture_output=True, text=True, timeout=30).stdout)
        assert info["subsongs"] == 1 and info["tunes"][0]["end"] == end, name
        out = await stream._render_mlrender(binary, src, 0)
        try:
            assert wav_layout(out)[:3] == (1, 2, 16)
            assert stream._wav_is_audible(out)
            assert abs(_wav_seconds(out) - seconds) < 0.01, (name, _wav_seconds(out))
            assert abs(_wav_seconds(out) - info["tunes"][0]["seconds"]) < 0.001, name
        finally:
            out.unlink(missing_ok=True)


@needs_binary
@pytest.mark.asyncio
async def test_a_live_render_is_the_same_file_and_the_silent_tail_is_trimmed(tmp_path):
    import hashlib
    from soniqboom.api import stream
    binary = _mlrender()
    src = tmp_path / "tail.ml"
    src.write_bytes(_tail_silence_tune())
    a = await stream._render_mlrender(binary, src, 0)
    b = await stream._render_mlrender(binary, src, 0, live_key="test-ml-live",
                                      expected_seconds=3.06)
    try:
        da, db = a.read_bytes(), b.read_bytes()
        ja, jb = da.find(b"data"), db.find(b"data")
        assert hashlib.md5(da[ja + 8:]).hexdigest() == hashlib.md5(db[jb + 8:]).hexdigest()
        pcm = da[ja + 8:]
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        last = max(i for i, v in enumerate(samples) if v)
        # Music to 2.56 s, then 0.5 s of the silence is kept.
        assert abs(last / 2 / 44100 - 2.56) < 0.01
        assert abs(len(samples) / 2 / 44100 - 3.06) < 0.01
    finally:
        a.unlink(missing_ok=True)
        b.unlink(missing_ok=True)
    # No subsong 1, and a tempo the replayer can't hold: failures, no audio.
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await stream._render_mlrender(binary, src, 1)
    assert exc.value.status_code in (422, 502)
    slow = bytearray(RS.ml_tune())
    i = slow.find(b"TUNE")
    struct.pack_into(">H", slow, i + 8 + 32, 20)             # tempo 20
    (tmp_path / "slow.ml").write_bytes(bytes(slow))
    r = subprocess.run([binary, "-f", "0", str(tmp_path / "slow.ml"), str(tmp_path / "s.wav")],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "can't play subsong 0" in r.stderr
    struct.pack_into(">H", slow, i + 8 + 32, 32)             # the slowest it plays
    (tmp_path / "slow.ml").write_bytes(bytes(slow))
    r = subprocess.run([binary, "-f", "0", str(tmp_path / "slow.ml"), "-"], capture_output=True)
    assert r.returncode == 0 and r.stdout[:4] == b"RIFF"
    assert r.stdout[40:44] == b"\xff\xff\xff\xff"             # a stream: size unknown
    assert abs((len(r.stdout) - 44) / (44100 * 4) - 10.0) < 0.01
    assert subprocess.run([binary, "-r", "96000", str(src), "x.wav"]).returncode == 2


def _chunk_at(data: bytes, cid: bytes) -> int:
    return data.find(cid)


@needs_binary
def test_a_module_the_loader_would_overrun_is_refused_with_its_reason(tmp_path):
    binary = _mlrender()
    good = RS.ml_tune()

    def refused(data: bytes) -> str:
        src = tmp_path / "bad.ml"
        src.write_bytes(data)
        r = subprocess.run([binary, "-m", "2", str(src), str(tmp_path / "o.wav")],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 1, r.stderr
        lines = r.stderr.strip().splitlines()
        assert len(lines) == 1 and lines[0].startswith(
            "mlrender: damaged Musicline Editor module: "), r.stderr
        return lines[0].split(": ", 2)[2]

    # An unpacked sample of more than 128 KB that the file cuts short.
    j = _chunk_at(good, b"SMPL")
    big = 200_000
    huge = good[:j] + b"SMPL" + struct.pack(">I", 50 + big) + struct.pack(">IBB", big, 0, 0) \
        + good[j + 14:j + 64] + bytes(1000)
    assert refused(huge) == "sample 1's data runs past the end"
    # A tune of 9 channels (the replayer holds 8).
    d = bytearray(good)
    d[_chunk_at(good, b"TUNE") + 8 + 39] = 9
    assert refused(bytes(d)) == "tune 0 has 9 channels (at most 8)"
    # Part 2000 (parts are numbered to 1023).
    d = bytearray(good)
    struct.pack_into(">H", d, _chunk_at(good, b"PART") + 8, 2000)
    assert refused(bytes(d)) == "part number 2000 (at most 1023)"
    # A part whose rows run past the end of the file.
    i = _chunk_at(good, b"PART")
    assert refused(good[:i + 8 + 20]) == "PART chunk runs past the end"
    # 300 instruments (the replayer holds 255).
    i, j = _chunk_at(good, b"INST"), _chunk_at(good, b"SMPL")
    assert refused(good[:j] + good[i:j] * 299 + good[j:]) == "more than 255 instruments"
    # A packed sample whose packed data ends early: 256 samples from 20 bytes.
    j = _chunk_at(good, b"SMPL")
    head = good[j + 14:j + 64]
    packed = b"SMPL" + struct.pack(">I", 50 + 20) + struct.pack(">IBB", 256, 0x7F, 0) \
        + head + bytes(20)
    assert refused(good[:j] + packed) == "sample 1's data runs past the end"


@needs_binary
@pytest.mark.parametrize("cut", [1, 200, 256])
def test_an_unpacked_sample_cut_short_plays_and_says_so(tmp_path, cut):
    good = RS.ml_tune()
    src = tmp_path / "cut.ml"
    src.write_bytes(good[:-cut])
    r = subprocess.run([_mlrender(), "-m", "3", "-f", "0", str(src), "-"], capture_output=True,
                       timeout=30)
    err = r.stderr.decode()
    assert r.returncode == 0, err
    assert f"mlrender: warning: truncated sample data ({cut} bytes missing)" in err.splitlines()
    pcm = r.stdout[44:]
    assert len(pcm) == round(2.56 * 44100) * 4                  # the song itself is whole
    # The wave's first half (0x60) is there unless the cut takes all of it.
    assert (pcm.count(0) < len(pcm)) == (cut < 256)
    src.write_bytes(good)
    r = subprocess.run([_mlrender(), "-m", "3", "-f", "0", str(src), "-"], capture_output=True,
                       timeout=30)
    assert r.returncode == 0 and b"warning" not in r.stderr


@needs_binary
def test_no_such_subsong_is_its_own_exit_status(tmp_path):
    src = tmp_path / "one.ml"
    src.write_bytes(RS.ml_tune())
    for args in (["-i", "-s", "1", str(src)], ["-s", "5", str(src), str(tmp_path / "o.wav")]):
        r = subprocess.run([_mlrender(), *args], capture_output=True, text=True, timeout=30)
        n = args[args.index("-s") + 1]
        assert r.returncode == 3 and r.stderr.strip() == f"mlrender: no subsong {n} (the module has 1)"


@needs_binary
def test_an_instrument_reading_past_its_sample_plays_silence(tmp_path):
    # The instrument plays 128 words of a 16-byte wave: the replayer would read
    # past the sample; that channel plays nothing instead.
    good = RS.ml_tune()
    j = _chunk_at(good, b"SMPL")
    head = bytearray(good[j + 14:j + 64])
    struct.pack_into(">H", head, 38, 8)
    struct.pack_into(">H", head, 44, 8)
    short = good[:j] + b"SMPL" + struct.pack(">I", 50 + 16) + struct.pack(">IBB", 16, 0, 0) \
        + bytes(head) + bytes([0x60] * 8 + [0xA0] * 8)
    src = tmp_path / "short.ml"
    src.write_bytes(short)
    r = subprocess.run([_mlrender(), "-m", "3", "-f", "0", str(src), "-"], capture_output=True,
                       timeout=30)
    assert r.returncode == 0 and r.stdout[:4] == b"RIFF"
    pcm = r.stdout[44:]
    assert len(pcm) == round(2.56 * 44100) * 4 and pcm.count(0) == len(pcm)
    # The same tune on its 256-byte wave is audible (the control).
    src.write_bytes(good)
    r = subprocess.run([_mlrender(), "-m", "3", "-f", "0", str(src), "-"], capture_output=True,
                       timeout=30)
    assert r.returncode == 0 and r.stdout[44:].count(0) < len(r.stdout) - 44


@needs_binary
@pytest.mark.asyncio
async def test_the_tune_probe_hears_mlrender(tmp_path, monkeypatch):
    from soniqboom.api import stream
    src = tmp_path / "silent.ml"
    src.write_bytes(RS.ml_tune())
    _fake_tools(monkeypatch, stream, {"silent.ml": False}, mlrender=_mlrender())
    assert await stream._tune_audible_run("uade", src, 0) is True


def test_a_damaged_musicline_module_is_a_diagnosed_refusal_with_its_reason():
    """mlrender's "damaged Musicline Editor module: …" line becomes a
    diagnosed refusal naming the damage; its "no subsong N" (exit 3) never
    counts against the file."""
    from soniqboom.core import unplayable as _u
    err = "mlrender: damaged Musicline Editor module: sample 3's data runs past the end\n"
    got = _u.classify("Musicline", 1, err)
    assert got is not None and got[0] == "mlrender" and got[2] is True
    assert "sample 3's data runs past the end" in got[1]
    assert _u.ml_damage("nothing here") is None
    assert _u.classify("Musicline", 3, "mlrender: no subsong 4 (the module has 2)\n") is None
