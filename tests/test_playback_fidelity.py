# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Playback fidelity (2.0.0): silent renders an earlier version cached are
dropped on their first hit; a chosen SoundFont that can't be found is never
replaced by another."""
from __future__ import annotations

import struct
import time
from pathlib import Path

import pytest

from soniqboom.core import conversion_cache as cc


def _wav(path: Path, frames: int, sample) -> Path:
    data = b"".join(struct.pack("<hh", sample(i), sample(i)) for i in range(frames))
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
           + struct.pack("<IHHIIHH", 16, 1, 2, 44100, 44100 * 4, 4, 16)
           + b"data" + struct.pack("<I", len(data)))
    path.write_bytes(hdr + data)
    return path


def _adopt(key: str, fmt: str, path: Path) -> None:
    with cc._state_lock:
        cc._meta[key] = {"path": str(path), "size_bytes": path.stat().st_size,
                         "format_type": fmt, "created_at": 0}
        cc._lru[key] = time.time()


@pytest.fixture(autouse=True)
def _clean():
    cc._judged.clear()
    yield
    cc._judged.clear()


@pytest.mark.asyncio
async def test_a_silent_cached_render_is_dropped_on_its_first_hit(tmp_path):
    silent = _wav(tmp_path / "s.wav", 44100, lambda i: 0)
    (tmp_path / "s.vu").write_bytes(b"vu")
    _adopt("silent1__sub0", "uade", silent)
    assert await cc.get_cached("silent1__sub0") is None
    assert not silent.exists() and not (tmp_path / "s.vu").exists()
    assert "silent1__sub0" not in cc._meta
    assert not cc.known_silent("silent1__sub0")     # it renders again


@pytest.mark.asyncio
async def test_an_audible_render_is_judged_once_and_kept(tmp_path, monkeypatch):
    loud = _wav(tmp_path / "a.wav", 44100, lambda i: 4000 if (i // 50) % 2 else -4000)
    _adopt("loud1__sub0", "sid", loud)
    assert await cc.get_cached("loud1__sub0") == loud
    from soniqboom.core import silence
    calls = []
    monkeypatch.setattr(silence, "wav_audible", lambda p: calls.append(p) or True)
    assert await cc.get_cached("loud1__sub0") == loud
    assert calls == []                              # judged once per run


@pytest.mark.asyncio
async def test_a_transcode_of_a_recording_is_never_judged(tmp_path):
    quiet = _wav(tmp_path / "t.wav", 44100, lambda i: 0)
    _adopt("trans1__x", "transcoded", quiet)
    assert await cc.get_cached("trans1__x") == quiet
    assert quiet.exists()


# ── the chosen SoundFont ────────────────────────────────────────────────────

@pytest.fixture
def sf_dir(tmp_path, monkeypatch):
    from soniqboom import config
    d = tmp_path / "soundfonts"
    d.mkdir()
    monkeypatch.setattr(config, "get_soundfonts_dir", lambda: d)
    return d


def test_the_chosen_soundfont_is_used_where_it_is(sf_dir, tmp_path, monkeypatch):
    from soniqboom import config
    (sf_dir / "A.sf2").write_bytes(b"a")
    chosen = tmp_path / "elsewhere" / "Chosen.sf2"
    chosen.parent.mkdir()
    chosen.write_bytes(b"c")
    monkeypatch.setattr(config.settings, "soundfont_path", str(chosen))
    assert config.get_active_soundfont() == chosen and config.missing_soundfont() is None


def test_a_moved_chosen_soundfont_is_found_by_name_in_the_folder(sf_dir, monkeypatch):
    from soniqboom import config
    (sf_dir / "A.sf2").write_bytes(b"a")
    (sf_dir / "Chosen.sf2").write_bytes(b"c")
    monkeypatch.setattr(config.settings, "soundfont_path", "/Volumes/Gone/Chosen.sf2")
    assert config.get_active_soundfont() == sf_dir / "Chosen.sf2"
    assert config.missing_soundfont() is None


def test_a_missing_chosen_soundfont_is_never_replaced_by_another(sf_dir, monkeypatch):
    from soniqboom import config
    (sf_dir / "A.sf2").write_bytes(b"a")
    monkeypatch.setattr(config.settings, "soundfont_path", "/Volumes/Gone/Chosen.sf2")
    assert config.get_active_soundfont() is None
    assert config.missing_soundfont() == "/Volumes/Gone/Chosen.sf2"


def test_with_none_chosen_the_first_in_the_folder_plays(sf_dir, monkeypatch):
    from soniqboom import config
    (sf_dir / "B.sf2").write_bytes(b"b")
    (sf_dir / "A.sf2").write_bytes(b"a")
    monkeypatch.setattr(config.settings, "soundfont_path", "")
    assert config.get_active_soundfont() == sf_dir / "A.sf2"
    assert config.missing_soundfont() is None


@pytest.mark.asyncio
async def test_a_midi_tune_says_which_soundfont_is_missing(sf_dir, tmp_path, monkeypatch):
    from fastapi import HTTPException
    from soniqboom import config
    from soniqboom.api import stream
    (sf_dir / "A.sf2").write_bytes(b"a")
    monkeypatch.setattr(config.settings, "soundfont_path", "/Volumes/Gone/Chosen.sf2")
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "/usr/bin/true")
    mid = tmp_path / "x.mid"
    mid.write_bytes(b"MThd")
    with pytest.raises(HTTPException) as e:
        await stream._render_midi(mid)
    assert e.value.status_code == 503 and "Chosen.sf2" in e.value.detail


# ── overloaded tracker renders ──────────────────────────────────────────────

def _float_wav(path: Path, samples) -> Path:
    data = struct.pack(f"<{len(samples)}f", *samples)
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
           + struct.pack("<IHHIIHH", 16, 3, 2, 48000, 48000 * 8, 8, 32)
           + b"data" + struct.pack("<I", len(data)))
    path.write_bytes(hdr + data)
    return path


def test_the_peak_of_a_float_render_is_measured_once_and_kept(tmp_path, monkeypatch):
    from soniqboom.api import stream
    wav = _float_wav(tmp_path / "r.wav", [0.1, -0.2, 0.5, -1.5] * 1000)
    stream._PEAKS.clear()
    assert stream.render_peak(wav) == pytest.approx(1.5)
    assert (tmp_path / "r.peak").read_text().strip() == "1.500000"
    stream._PEAKS.clear()
    import array
    monkeypatch.setattr(array, "array", None)          # the sidecar answers
    assert stream.render_peak(wav) == pytest.approx(1.5)
    assert stream.render_gain(wav) == pytest.approx(1 / 1.5)


def test_renders_within_full_scale_or_in_16_bit_get_no_gain(tmp_path):
    from soniqboom.api import stream
    stream._PEAKS.clear()
    assert stream.render_gain(_float_wav(tmp_path / "ok.wav", [0.9, -0.95] * 500)) == 1.0
    assert stream.render_peak(_wav(tmp_path / "i.wav", 4410, lambda i: 30000)) is None
    assert stream.render_gain(tmp_path / "i.wav") == 1.0


def test_the_gain_goes_first_in_the_filter_chain():
    from soniqboom.api import stream
    assert stream._gain_args(["-c:a", "flac"], 1.0) == ["-c:a", "flac"]
    assert stream._gain_args(["-c:a", "flac"], 0.5) == ["-af", "volume=0.500000", "-c:a", "flac"]
    assert stream._gain_args(["-af", "lowpass=f=1", "-c:a", "mp3"], 0.5) == \
        ["-af", "volume=0.500000,lowpass=f=1", "-c:a", "mp3"]
    assert stream._gain_variant(1.0) == "" and stream._gain_variant(0.5) == "-g0.50000"


@pytest.mark.asyncio
async def test_an_overloaded_render_transcodes_at_full_scale_without_clipping(tmp_path):
    import shutil
    import subprocess
    from soniqboom.api import stream
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    stream._PEAKS.clear()
    samples = []
    for i in range(48000):                 # 0.5 s stereo, peaks 1.6 / -1.6
        v = 1.6 if i % 100 == 0 else 0.4 * ((i % 7) - 3) / 3
        samples += [v, -v]
    wav = _float_wav(tmp_path / "loud.wav", samples)
    gain = stream.render_gain(wav)
    out = await stream._render_to_transcoded_flac(wav, codec="flac", gain=gain)
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    vals = struct.unpack(f"<{len(pcm) // 4}f", pcm)
    peak = max(abs(v) for v in vals)
    assert 0.99 < peak <= 1.0                       # brought to full scale…
    flat = sum(1 for v in vals if abs(v) >= 0.9999)
    assert flat == 2 * 480                          # …only the peaks reach it, none flattened
    assert vals[2] == pytest.approx(samples[2] / 1.6, abs=1e-4)   # exactly 1/peak


# ── AdLib at the OPL chip's own rate ────────────────────────────────────────

def test_adlib_renders_at_the_chip_rate_into_a_resampler(monkeypatch):
    import shlex
    from soniqboom.api import stream
    monkeypatch.setattr(stream, "_SOXR_OK", False)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")
    cmd = stream._adlib_cmd("/opt/adplay", Path("/m/it's a \"tune\".rad"), 3, "/tmp/o.wav")
    assert cmd[:2] == ["/bin/sh", "-c"]
    head, _, enc = cmd[2].partition(" | exec ")
    play, _, tail_filter = head.partition(" | ")
    assert shlex.split(play) == ["/opt/adplay", "-q", "-O", "disk", "-e", "nuked", "--stereo",
                                 "-s", "3", "-d", "/dev/fd/3", "-f", "49716",
                                 "/m/it's a \"tune\".rad", "3>&1", ">/dev/null", "2>&1"]
    import sys
    assert shlex.split(tail_filter) == [sys.executable, "-I", "-c", stream.ADPLAY_TAIL_FILTER]
    enc = shlex.split(enc)
    assert enc[0] == "/usr/bin/ffmpeg" and enc[-1] == "/tmp/o.wav"
    assert enc[enc.index("-i") + 1] == "pipe:0"
    assert enc[enc.index("-af") + 1].startswith("aresample=44100:filter_size=64")
    monkeypatch.setattr(stream, "_SOXR_OK", True)
    assert "resampler=soxr" in stream._opl_resample_filter()


def test_without_ffmpeg_adlib_renders_at_44100_as_before(monkeypatch):
    from soniqboom.api import stream
    monkeypatch.setattr("shutil.which", lambda name: None)
    cmd = stream._adlib_cmd("/opt/adplay", Path("/m/a.rad"), 0, "/tmp/o.wav")
    assert cmd == ["/opt/adplay", "-q", "-O", "disk", "-e", "nuked", "--stereo",
                   "-d", "/tmp/o.wav", "-f", "44100", "/m/a.rad"]


_ADLIB_SAMPLE = Path(__file__).resolve().parents[1] / "internal/testdata/adlib/ALLOYRUN.A2M"


@pytest.mark.asyncio
async def test_a_real_adlib_render_is_44100_and_as_long_as_the_tune(tmp_path):
    import shutil
    from soniqboom.api import stream
    from soniqboom.core.silence import wav_layout
    if not (shutil.which("adplay") and shutil.which("ffmpeg") and _ADLIB_SAMPLE.exists()):
        pytest.skip("adplay / ffmpeg / the private AdLib sample not here")
    out = await stream._render_adlib_one(shutil.which("adplay"), _ADLIB_SAMPLE, 0)
    try:
        tag, ch, bits, off, length = wav_layout(out)
        assert (tag, ch, bits) == (1, 2, 16)
        assert out.read_bytes()[24:28] == (44100).to_bytes(4, "little")
        assert length / (44100 * 4) == pytest.approx(76.74, abs=0.02)
        assert stream._wav_is_audible(out)
    finally:
        out.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_an_undecodable_adlib_file_still_says_why(tmp_path):
    import shutil
    from fastapi import HTTPException
    from soniqboom.api import stream
    if not (shutil.which("adplay") and shutil.which("ffmpeg")):
        pytest.skip("adplay / ffmpeg not here")
    bad = tmp_path / "junk.rad"
    bad.write_bytes(b"not an adlib tune at all" * 10)
    with pytest.raises(HTTPException) as e:
        await stream._render_adlib(bad)
    assert e.value.status_code == 422


# ── KSS through libkss (kssrender) ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_kss_goes_to_kssrender_when_it_is_installed(tmp_path, monkeypatch):
    from soniqboom.api import stream
    from soniqboom.config import settings
    seen = []

    async def fake_kss(binary, path, subsong, duration, **kw):
        seen.append((binary, path.name, subsong, duration))
        return tmp_path / "out.wav"
    monkeypatch.setattr(stream, "_render_kss", fake_kss)
    monkeypatch.setattr(stream, "_find_renderer",
                        lambda conf, name: "/opt/kssrender" if name == "kssrender" else None)
    monkeypatch.setattr(settings, "sid_default_duration", 150)
    kss = tmp_path / "a.kss"
    kss.write_bytes(b"KSCC" + b"\0" * 12)
    assert await stream._render_gme(kss, 3) == tmp_path / "out.wav"
    assert seen == [("/opt/kssrender", "a.kss", 3, 150)]


@pytest.mark.asyncio
async def test_without_kssrender_kss_stays_with_libgme(tmp_path, monkeypatch):
    from soniqboom.api import stream
    from soniqboom.core import gme_render
    called = []

    async def fake_kss(*a, **k):
        called.append(a)
    async def fake_external(path, out, subsong, duration):
        return out
    monkeypatch.setattr(stream, "_render_kss", fake_kss)
    monkeypatch.setattr(stream, "_find_renderer", lambda conf, name: None)
    monkeypatch.setattr(gme_render, "is_available", lambda: False)
    monkeypatch.setattr(stream, "_render_gme_external", fake_external)
    kss = tmp_path / "a.kss"
    kss.write_bytes(b"KSCC" + b"\0" * 12)
    await stream._render_gme(kss, 0)
    assert called == []


def _kssrender():
    import os
    import shutil
    p = os.environ.get("SB_KSSRENDER") or shutil.which("kssrender")
    return p if p and os.path.exists(p) else None


@pytest.mark.asyncio
async def test_a_real_kss_render_is_audible_stereo(tmp_path):
    import importlib.util
    from soniqboom.api import stream
    from soniqboom.core.silence import wav_layout
    binary = _kssrender()
    if not binary:
        pytest.skip("kssrender not built here (soniqboom/native/kssrender/build.sh)")
    spec = importlib.util.spec_from_file_location(
        "render_selftest", Path(__file__).resolve().parents[1] / "scripts/render_selftest.py")
    rs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rs)
    src = tmp_path / "t.kss"
    src.write_bytes(rs.kss_tune())
    out = await stream._render_kss(binary, src, 0, 4)
    try:
        assert wav_layout(out)[:3] == (1, 2, 16)
        assert out.read_bytes()[24:28] == (44100).to_bytes(4, "little")
        assert stream._wav_is_audible(out)
        assert rs.peak_dbfs(str(out)) > -30
    finally:
        out.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_a_stopped_pipeline_render_leaves_no_process(tmp_path):
    """The AdLib render is a shell pipeline (adplay | ffmpeg): stopping it
    must end both commands, not only the shell (QA 2026-10-28)."""
    import asyncio
    import subprocess
    from soniqboom.api import stream
    mark = "47.0815"                                    # a sleep no one else runs

    def running():
        out = subprocess.run(["ps", "-A", "-o", "command="], capture_output=True, text=True).stdout
        return [l for l in out.splitlines() if l.startswith("/bin/sleep " + mark)]
    pipe = ["/bin/sh", "-c", f"/bin/sleep {mark} 3>&1 >/dev/null 2>&1 | exec /bin/sleep {mark}"]
    for render in (lambda: stream._await_renderer(pipe, tmp_path / "a.wav", timeout=60, kind="t",
                                                   require_audio=False, new_session=True),
                   lambda: stream._tail_render(pipe, tmp_path / "b.wav", None, kind="t",
                                               timeout=60, require_audio=False, new_session=True)):
        task = asyncio.ensure_future(render())
        for _ in range(50):
            if len(running()) == 2:
                break
            await asyncio.sleep(0.05)
        assert len(running()) == 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)
        assert running() == []


def test_the_adplay_tail_filter_leaves_out_only_the_header_sizes():
    """adplay writes its WAV header's sizes after the audio when its output is
    a pipe (in either order): they are dropped — they played as a click — and
    any other ending is passed on (QA 2026-10-28)."""
    import struct
    import subprocess
    import sys
    from soniqboom.api import stream
    audio = bytes(range(256)) * 40
    head = b"RIFF" + bytes(4) + b"WAVEfmt " + bytes(20) + b"data" + bytes(4)   # 44 bytes
    d = len(audio)

    def run(data: bytes) -> bytes:
        return subprocess.run([sys.executable, "-I", "-c", stream.ADPLAY_TAIL_FILTER],
                              input=data, capture_output=True, check=True).stdout
    for tail in (struct.pack("<II", d, d + 36), struct.pack("<II", d + 36, d)):
        assert run(head + audio + tail) == head + audio
    for other in (struct.pack("<II", d, d), b"\x01" * 8, b"abc"):
        assert run(head + audio + other) == head + audio + other
    assert run(b"") == b"" and run(b"RI") == b"RI"


def test_the_build_self_test_runs_the_servers_adlib_command(monkeypatch):
    """scripts/render_selftest.py renders AdLib with the command the server
    uses (it can't import SoniqBoom at that build step): the two are equal."""
    import importlib.util
    from soniqboom.api import stream
    spec = importlib.util.spec_from_file_location(
        "render_selftest", Path(__file__).resolve().parent.parent / "scripts/render_selftest.py")
    st = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(st)
    assert st.ADPLAY_TAIL_FILTER == stream.ADPLAY_TAIL_FILTER
    monkeypatch.setattr(stream.shutil, "which", lambda name: "/opt/ffmpeg")
    for soxr in (True, False):
        monkeypatch.setattr(stream, "_SOXR_OK", soxr)
        server = stream._adlib_cmd("/opt/adplay", Path("/m/a b.imf"), 2, "/t/o.wav")
        assert server == st.adlib_pipeline("/opt/adplay", "/opt/ffmpeg", "/m/a b.imf",
                                           "/t/o.wav", soxr=soxr, subsong=2,
                                           emulator=stream._ADLIB_OPL_EMULATOR)


def test_the_macos_app_renders_adlib_without_the_pipeline(monkeypatch):
    """In the Nuitka app ``sys.executable`` is the app itself — no interpreter
    for the tail filter — so adplay renders alone at 44.1 kHz (QA 2026-10-28)."""
    from soniqboom.api import stream
    from soniqboom.core import merger
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(merger, "_is_bundled", lambda: True)
    cmd = stream._adlib_cmd("/opt/adplay", Path("/m/a.rad"), 0, "/tmp/o.wav")
    assert cmd == ["/opt/adplay", "-q", "-O", "disk", "-e", "nuked", "--stereo",
                   "-d", "/tmp/o.wav", "-f", "44100", "/m/a.rad"]
