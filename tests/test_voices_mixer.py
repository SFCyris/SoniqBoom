# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-voice meters of libgme / AdLib tunes and the voice mixer
(api/voices.py), the VUMR voice names (core/openmpt_vu.py), and the
oplvoices tool (soniqboom/native/oplvoices) when it is built."""
import asyncio
import os
import shutil
import struct
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TD = ROOT / "internal" / "testdata"
A2M = TD / "adlib" / "ALLOYRUN.A2M"

from soniqboom.api import voices                         # noqa: E402
from soniqboom.core import openmpt_vu                     # noqa: E402
from soniqboom.core.openmpt_vu import VUResult            # noqa: E402


# ── VUMR voice names ────────────────────────────────────────────────────────

def _vu(names=()):
    return VUResult(channels=3, sample_rate=30, frames=4, mono=bytes(range(12)),
                    pan=bytes([0, 1, 2]), names=tuple(names))


def test_names_round_trip_and_the_levels_are_unchanged():
    plain = openmpt_vu.serialize_vumr(_vu())
    named = openmpt_vu.serialize_vumr(_vu(["Square 1", "Wave", "Noise"]))
    assert plain[6] == 0 and named[6] == openmpt_vu.VUMR_FLAG_NAMES
    assert named[:6] == plain[:6] and named[7:len(plain)] == plain[7:]
    assert openmpt_vu.vumr_names(named) == ["Square 1", "Wave", "Noise"]
    assert openmpt_vu.vumr_names(plain) == []


def test_a_long_or_wide_name_is_cut_at_a_character():
    raw = openmpt_vu.serialize_vumr(_vu(["é" * 40, "x", "y"]))
    got = openmpt_vu.vumr_names(raw)
    assert got[0] == "é" * 16 and len(got[0].encode()) == 32


def test_validation_takes_names_only_when_asked():
    named = openmpt_vu.serialize_vumr(_vu(["a", "b", "c"]))
    with pytest.raises(ValueError):
        openmpt_vu.parse_and_validate_vumr(named)            # an upload: exact size
    assert openmpt_vu.parse_and_validate_vumr(named, allow_names=True) == (3, 30, 4)
    bad = named[:-1]
    with pytest.raises(ValueError):
        openmpt_vu.parse_and_validate_vumr(bad, allow_names=True)
    with pytest.raises(ValueError):
        openmpt_vu.serialize_vumr(_vu(["only one"]))


# ── the sidecar of a libgme / AdLib render ─────────────────────────────────

def test_the_sidecar_is_found_beside_the_libgme_render_and_after_eviction(tmp_path, monkeypatch):
    from soniqboom.core import conversion_cache as cc
    monkeypatch.setattr(cc, "get_conversion_cache_dir", lambda: tmp_path)
    for fmt in ("gme", "adlib"):
        tid = f"ab{fmt}0000-1111-2222-3333-444444444444"
        key = cc._cache_key(tid, fmt, 2)
        wav = cc._cache_path(key, fmt)
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"RIFF")
        wav.with_suffix(".vu").write_bytes(b"VUMR")
        with cc._state_lock:
            cc._meta[key] = {"path": str(wav), "format_type": fmt}
        try:
            assert cc.get_vu_sidecar_path(tid, 2) == wav.with_suffix(".vu")
            assert cc.get_vu_sidecar_path(tid, 1) is None
        finally:
            with cc._state_lock:
                cc._meta.pop(key, None)
        # the WAV evicted: the sidecar stays and is still found
        wav.unlink()
        assert cc.get_vu_sidecar_path(tid, 2) == wav.with_suffix(".vu")


# ── asking for the pass ─────────────────────────────────────────────────────

@pytest.fixture
def clean_registry():
    for d in (voices._WANTED, voices._LAST_POLL, voices._SKIPPED, voices._SOURCES):
        d.clear()
    voices._INFLIGHT.clear()
    yield
    for d in (voices._WANTED, voices._LAST_POLL, voices._SKIPPED, voices._SOURCES):
        d.clear()
    voices._INFLIGHT.clear()


def test_the_pass_starts_only_for_a_served_tune_after_it_has_played(clean_registry, monkeypatch):
    started = []
    monkeypatch.setattr(voices, "_run_chip_vu", lambda key, ent: started.append(key) or asyncio.sleep(0))
    monkeypatch.setattr(voices, "_family_supported", lambda ent: True)

    async def run():
        assert voices.request_chip_vu("t1", 0) is False            # never served
        voices.note_chip_vu_wanted("gme", "t1", 0, "t1__sub0__r3", "/x/a.nsf")
        assert voices.request_chip_vu("t1", 0) is True              # wanted, too early
        assert started == []
        voices._WANTED["gme:t1:0"]["served_at"] -= voices._MIN_PLAYED_S + 1
        assert voices.request_chip_vu("t1", 0) is True
        assert started == ["gme:t1:0"]
        assert voices.request_chip_vu("t1", 0) is True              # in flight: once
        assert started == ["gme:t1:0"]
        assert voices._still_wanted("gme:t1:0")
        assert ("t1", 0) in voices._SOURCES                         # the mixer's source too
    asyncio.run(run())


def test_a_pass_without_result_says_skipped_then_tries_again_later(clean_registry):
    voices.note_chip_vu_wanted("adlib", "t2", 0, "t2__sub0__r2", "/x/a.rad")
    voices._note_skipped("adlib:t2:0")
    assert voices.chip_vu_skipped_reason("t2", 0) == "skipped"
    assert voices.request_chip_vu("t2", 0) is False
    voices._SKIPPED["adlib:t2:0"] -= voices._SKIP_TTL_S + 1
    assert voices.chip_vu_skipped_reason("t2", 0) in (None, "unsupported")


def test_a_kss_kssrender_plays_has_no_libgme_voices(clean_registry):
    from soniqboom.core.conversion_cache import _cache_key
    voices.note_chip_vu_wanted("gme", "t3", 0, _cache_key("t3", "gme", 0, variant="kss1"), "/x/a.kss")
    assert voices.chip_vu_skipped_reason("t3", 0) == "unsupported"
    assert voices.request_chip_vu("t3", 0) is False


# ── the mixer's temp renders ────────────────────────────────────────────────

def test_job_reads_mix_frames_and_half_rate_scope_voices(tmp_path, monkeypatch):
    monkeypatch.setattr(voices, "_render_dir", lambda: str(tmp_path))
    mix = voices._Job(("t", 0, "mix", 1), "mix", 44100, 2)
    p = mix.work / "m.pcm"
    p.write_bytes(b"".join(struct.pack("<hh", i, -i) for i in range(100)))
    mix.files.append((p, 0, 4, 1))
    mix.measure = True
    assert mix.frames_ready() == 100
    assert mix.read(10, 2) == struct.pack("<hhhh", 10, -10, 11, -11)
    assert mix.read(99, 3)[4:] == bytes(8)                    # past the end: silence
    # a SID scope: a mono 44.1 kHz WAV with a header, read at half the rate
    sc = voices._Job(("t", 0, "scope", 0), "scope", 22050, 1)
    w = sc.work / "v.wav"
    w.write_bytes(b"H" * 44 + b"".join(struct.pack("<h", i) for i in range(64)))
    sc.files.append((w, 44, 2, 2))
    sc.measure = True
    assert sc.frames_ready() == 32
    assert sc.read(3, 4) == struct.pack("<hhhh", 6, 8, 10, 12)
    mix.close()
    sc.close()
    assert not mix.work.exists() and not sc.work.exists()


def test_wav_layout_of_a_growing_file(tmp_path):
    w = tmp_path / "a.wav"
    w.write_bytes(b"RIFF\xff\xff\xff\xffWAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 44100, 88200, 2, 16)
                  + b"data" + struct.pack("<I", 0xFFFFFFFF) + b"\0\0" * 10)
    assert voices._wav_layout(w) == (44, 1, 44100)
    (tmp_path / "b.wav").write_bytes(b"RIFF")
    assert voices._wav_layout(tmp_path / "b.wav") is None


def test_opl_channels_from_the_sidecar_names():
    assert voices._opl_channels(["OPL 1", "OPL 7 BD", "OPL 18", "Square 1"]) == [0, 6, 17, -1]


# ── oplvoices (built: on PATH, or OPLVOICES=path) ───────────────────────────

def _oplvoices() -> "str | None":
    return os.environ.get("OPLVOICES") or shutil.which("oplvoices")


@pytest.mark.skipif(not _oplvoices() or not shutil.which("adplay") or not A2M.is_file(),
                    reason="oplvoices / adplay / the A2M fixture not present")
def test_oplvoices_is_adplay_sample_for_sample_and_its_channels_add_up(tmp_path):
    tool = _oplvoices()
    ref = tmp_path / "ref.wav"
    subprocess.run(["adplay", "-q", "-O", "disk", "-e", "nuked", "--stereo", "-d", str(ref),
                    "-f", "49716", str(A2M)], check=True, capture_output=True, timeout=120)
    mine = tmp_path / "mine.wav"
    r = subprocess.run([tool, "-w", str(mine), str(A2M)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0
    assert mine.read_bytes() == ref.read_bytes()
    # 20 s: the solo renders of every channel add up to the mix exactly
    full = tmp_path / "f.wav"
    subprocess.run([tool, "-t", "20", "-w", str(full), str(A2M)], check=True, capture_output=True)
    import array
    f = array.array("h"); f.frombytes(full.read_bytes()[44:])
    acc = [0] * len(f)
    for c in range(18):
        s = tmp_path / f"s{c}.wav"
        subprocess.run([tool, "-t", "20", "-m", str(1 << c), "-w", str(s), str(A2M)],
                       check=True, capture_output=True)
        a = array.array("h"); a.frombytes(s.read_bytes()[44:])
        for i, x in enumerate(a):
            acc[i] += x
    assert list(acc) == list(f)
    # the meters: the channels that play, named, at 30 Hz
    vu = tmp_path / "a.vu"
    r = subprocess.run([tool, "-v", str(vu), str(A2M)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "channels=0,1,2,3,4,5,6,7,8" in r.stdout
    raw = vu.read_bytes()
    ch, rate, frames = openmpt_vu.parse_and_validate_vumr(raw, allow_names=True, max_frames=10**6)
    assert (ch, rate) == (9, 30)
    secs = float(r.stdout.split("seconds=")[1].split()[0])
    assert abs(frames - secs * 30) <= 1.5                   # one frame per 1/30 s, a last partial one
    assert openmpt_vu.vumr_names(raw)[0] == "OPL 1"
    cols = [[raw[16 + ch + i * ch + c] for i in range(frames)] for c in range(ch)]
    assert len({tuple(c) for c in cols}) >= 5               # the channels differ


@pytest.mark.skipif(not _oplvoices() or not A2M.is_file(), reason="oplvoices / fixture not present")
def test_oplvoices_usage_and_a_file_adplug_cannot_read(tmp_path):
    tool = _oplvoices()
    assert subprocess.run([tool], capture_output=True).returncode == 2
    junk = tmp_path / "x.rad"
    junk.write_bytes(b"not a tune")
    assert subprocess.run([tool, "-w", str(tmp_path / "o.wav"), str(junk)],
                          capture_output=True).returncode == 1


# ── a render stopped mid-slice ──────────────────────────────────────────────

def test_a_cancel_waits_for_the_worker_thread():
    """``_owned``: the cancel propagates only once the blocking call returned
    (what it works on is freed after it)."""
    done = []

    def slow():
        time.sleep(0.4)
        done.append(1)

    async def run():
        t = asyncio.ensure_future(voices._owned(slow))
        await asyncio.sleep(0.05)
        t.cancel()
        t0 = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await t
        return time.monotonic() - t0
    waited = asyncio.run(run())
    assert done == [1] and waited >= 0.3


VGZ = TD / "game" / "09.vgz"


@pytest.mark.skipif(not VGZ.is_file(), reason="fixture 09.vgz not present")
def test_stopping_a_libgme_mix_render_mid_slice_does_not_crash(tmp_path, monkeypatch):
    """A mix render stopped while libgme renders a slice (the listener moved
    on, a newer mask): it used to delete the emulators under the running
    thread — the server aborted ("pure virtual function called")."""
    from soniqboom.core import conversion_cache, gme_render, gme_voices
    if not gme_voices.is_available():
        pytest.skip("libgme not available")
    data = VGZ.read_bytes()
    monkeypatch.setattr(voices, "_render_dir", lambda: str(tmp_path))
    monkeypatch.setattr(voices, "gme_inputs", lambda p, s, raw=None: (data, 180.0, None))
    monkeypatch.setattr(voices, "_wav_frames", lambda w: 44100 * 120)

    async def cached(key):
        return tmp_path / "ref.wav"
    monkeypatch.setattr(conversion_cache, "get_cached", cached)
    src = {"family": "gme", "src": VGZ, "cache_key": "k", "pin": None}

    async def run():
        for delay in (0.2, 0.5, 0.9, 1.4):
            job = voices._Job(("t", 0, "mix", 0xFE), "mix", 44100, 2)
            job.task = asyncio.ensure_future(voices._gme_job(job, src, 0, 0xFE, "t"))
            await asyncio.sleep(delay)
            job.close()                         # cancels the task, mid-slice
            with pytest.raises(asyncio.CancelledError):
                await job.task
            assert not job.done
        sc = voices._Job(("t", 0, "scope", 0), "scope", 22050, 0)
        sc.task = asyncio.ensure_future(voices._gme_job(sc, src, 0, 0, "t"))
        await asyncio.sleep(0.7)
        sc.close()
        with pytest.raises(asyncio.CancelledError):
            await sc.task
    asyncio.run(run())
