# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A 2SID / 3SID tune's per-voice meter and Voices tab (2.3.1).

* The SID sidecar lookup takes only the tune's meter as it plays now: the
  stereo render's key (variant "st") with 3 voices per chip.  The 3-voice
  sidecar of the mono render keyed before 2.3.0 is skipped (and kept); one
  under the tune's own key with the wrong count (a one-chip pass) is removed,
  so the voice pass can write the right one.
* The SID scope job counts every voice (3 per chip) and shows a voice as soon
  as its render has started, not once the last of 9 has.
* The per-voice meter's pass and the scopes share one set of renders
  (``voices.sid_voice_renders``), pinned while the meter is built.
* A multi-SID tune's pass starts at once (no head start for the browser,
  whose WASM core renders one chip).

No renderer needed: VUMR files are written directly and the renders faked."""
import asyncio
import struct
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from soniqboom.api import stream as s
from soniqboom.api import voices
from soniqboom.core import conversion_cache as cc
from soniqboom.core import openmpt_vu
from soniqboom.core.openmpt_vu import VUResult

T2 = "5d67d86e-d2f6-5006-85b0-d22c66343fb1"     # a 2SID tune
T1 = "24a357db-3700-513b-9733-104d21f8f4ef"     # a one-chip SID


def _vumr(channels: int) -> bytes:
    return openmpt_vu.serialize_vumr(VUResult(
        channels=channels, sample_rate=30, frames=2, mono=bytes(2 * channels),
        pan=bytes(channels)))


def _put(key: str, channels: int, *, meta: bool) -> Path:
    wav = cc._cache_path(key, "sid")
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.with_suffix(".vu").write_bytes(_vumr(channels))
    if meta:
        wav.write_bytes(b"RIFF")
        with cc._state_lock:
            cc._meta[key] = {"path": str(wav), "format_type": "sid"}
    return wav.with_suffix(".vu")


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "get_conversion_cache_dir", lambda: tmp_path)
    added = []
    yield added
    with cc._state_lock:
        for k in list(cc._meta):
            if k.startswith((T1, T2)):
                cc._meta.pop(k, None)


def test_the_sid_lookup_takes_the_tunes_own_meter(cache):
    mono = cc._cache_key(T2, "sid", 0, duration=180)
    st = cc._cache_key(T2, "sid", 0, duration=180, variant="st")
    assert st.split("__")[-1] == "st" and mono != st
    old = _put(mono, 3, meta=False)                  # the mono render's meter, before 2.3.0
    wrong = _put(st, 3, meta=True)                   # a one-chip pass under the tune's own key
    assert cc.get_vu_sidecar_path(T2, 0, sid_voices=6) is None
    assert old.exists(), "another render's sidecar is left to its key"
    assert not wrong.exists(), "a wrong count under the tune's own key is removed"
    right = _put(st, 6, meta=True)
    assert cc.get_vu_sidecar_path(T2, 0, sid_voices=6) == right
    # the cached WAV evicted: found by its path, still checked
    with cc._state_lock:
        cc._meta.pop(st, None)
    assert cc.get_vu_sidecar_path(T2, 0, sid_voices=6) == right
    right.write_bytes(_vumr(3))
    assert cc.get_vu_sidecar_path(T2, 0, sid_voices=6) is None and not right.exists()


def test_a_one_chip_sid_and_the_old_callers_are_unchanged(cache):
    mono = cc._cache_key(T1, "sid", 0, duration=180)
    vu = _put(mono, 3, meta=True)
    assert cc.get_vu_sidecar_path(T1, 0, sid_voices=3) == vu
    assert cc.get_vu_sidecar_path(T1, 0) == vu                # (no count asked: any sidecar)
    # a one-chip tune never takes a stereo render's sidecar
    st = _put(cc._cache_key(T1, "sid", 0, duration=120, variant="st"), 6, meta=False)
    vu.unlink()
    with cc._state_lock:
        cc._meta.pop(mono, None)
    assert cc.get_vu_sidecar_path(T1, 0, sid_voices=3) is None and st.exists()


def _scope_wav(job, name, frames):
    w = job.work / name
    w.write_bytes(b"H" * 44 + b"".join(struct.pack("<h", 100 + i) for i in range(2 * frames)))
    return (w, 44, 2, 2)


def test_a_scope_shows_each_voice_as_its_render_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(voices, "_render_dir", lambda: str(tmp_path))
    sc = voices._Job(("t", 0, "scope", 0), "scope", 22050, 9)
    sc.measure = True
    sc.files = [None] * 9
    assert sc.frames_ready() == 0                    # nothing started
    sc.files[0] = _scope_wav(sc, "v0.wav", 40)
    sc.files[4] = _scope_wav(sc, "v4.wav", 25)
    assert sc.frames_ready() == 40                   # the furthest started voice
    raw = sc.read(30, 4)
    assert len(raw) == 9 * 4 * 2
    v = [raw[i * 8:(i + 1) * 8] for i in range(9)]
    assert v[0] == struct.pack("<hhhh", 160, 162, 164, 166)
    assert v[4] == bytes(8)                          # behind at frame 30: silent there
    assert v[8] == bytes(8)                          # not started: silent
    # a mix is still its one render's frames, and none while it hasn't started
    mix = voices._Job(("t", 0, "mix", 1), "mix", 44100, 2)
    mix.measure = True
    mix.files = [None]
    assert mix.frames_ready() == 0
    sc.close(); mix.close()


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(voices, "_render_dir", lambda: str(tmp_path))
    voices._JOBS.clear()
    renders = []

    async def fake_sid_job(job, src, track, subsong, mask):
        renders.append((job.key, s.sid_chips(track)))
        job.dur = int(src["duration"])
        n = 3 * s.sid_chips(track)
        job.measure = True
        job.files = [_scope_wav(job, f"v{i}.wav", 10) for i in range(n)]
        job.total = 10
        await asyncio.sleep(0.05)
        job.done = True
    monkeypatch.setattr(voices, "_sid_job", fake_sid_job)
    yield renders
    for j in list(voices._JOBS.values()):
        j.close()
    voices._JOBS.clear()


def test_the_scope_job_has_every_voice_and_the_meter_shares_it(jobs):
    async def run():
        three = SimpleNamespace(channels=3, format="SID")
        src = {"family": "sid", "src": Path("t.sid"), "duration": 180}
        tab = await voices._start_job(three, "t", 0, "scope", 0, src)
        assert tab.channels == 9                      # (was 3: voices 4–9 read as flat)
        got = await voices.sid_voice_renders(three, "t", 0, Path("t.sid"), 180)
        assert got is tab and got.pins == 1 and got.done and len(got.files) == 9
        # pinned: the sweeper leaves it however long it idles, and LRU skips it
        got.last = time.monotonic() - 3600
        voices._sweep_jobs()
        assert voices._JOBS.get(("t", 0, "scope", 0)) is got and got.work.exists()
        got.pins -= 1
        voices._sweep_jobs()
        assert ("t", 0, "scope", 0) not in voices._JOBS
        # another length's renders are not this meter's
        await voices._start_job(three, "u", 0, "scope", 0, dict(src, duration=120))
        await asyncio.sleep(0.1)
        assert await voices.sid_voice_renders(three, "u", 0, Path("t.sid"), 180) is None
    asyncio.run(run())
    assert jobs == [(("t", 0, "scope", 0), 3), (("u", 0, "scope", 0), 3)]   # rendered once each


def test_the_meter_pass_builds_from_the_shared_renders(jobs, tmp_path, monkeypatch):
    built = []
    monkeypatch.setattr(s, "_find_renderer", lambda *a: "/bin/true")
    from soniqboom.core import sid_vu
    monkeypatch.setattr(sid_vu, "build_vu", lambda wavs, dur: built.append((len(wavs), dur)) or
                        VUResult(channels=len(wavs), sample_rate=30, frames=1,
                                 mono=bytes(len(wavs)), pan=bytes(len(wavs))))
    anchor = tmp_path / "sid" / "t_" / "t__sub0__dur180__st.wav"
    asyncio.run(s._sid_vu_worker("t__sub0__dur180__st", anchor, Path("t.sid"), 0, 180, chips=2))
    assert built == [(6, 180.0)]
    assert openmpt_vu.parse_and_validate_vumr(anchor.with_suffix(".vu").read_bytes())[0] == 6
    assert voices._JOBS[("t", 0, "scope", 0)].pins == 0    # (unpinned once built)


def test_a_multi_sid_pass_starts_at_once(monkeypatch, tmp_path):
    started = {}

    async def worker(full_key, cached_wav, sid_path, subsong, dur, chips=1):
        started[chips] = time.monotonic()
    monkeypatch.setattr(s, "_sid_vu_worker", worker)
    monkeypatch.setattr(s, "_SID_VU_SERVER_DELAY", 0.4)
    wav = tmp_path / "x.wav"
    for chips in (1, 2, 3):
        t0 = time.monotonic()
        asyncio.run(s._sid_vu_worker_delayed(f"k{chips}", wav, Path("t.sid"), 0, 180, chips))
        started[chips] -= t0
    assert started[1] >= 0.4                          # one chip: the browser's head start
    assert started[2] < 0.2 and started[3] < 0.2


def test_the_packed_cache_hit_asks_for_the_tunes_chips():
    import inspect
    src = inspect.getsource(s)
    i = src.index("_packed = await _serve_packed_hit(",          # (the SID branch's)
                  src.index('_voices.note_voice_source("sid"'))
    j = src.index("return _packed", i)
    assert "_spawn_sid_vu(" in src[i:j] and "chips=_chips" in src[i:j]


# ── after the review: fidelity keys, unreadable files, the length race, a
#    pass without a result, partial scope chunks, the light engine ──────────

def test_the_stereo_key_is_told_apart_with_every_fidelity_part(cache):
    st = "x" * 8 + "__sub0__dur180__m6581f__nf__fc0.5__db__fo3__st"
    mono = "x" * 8 + "__sub0__dur180__m6581f__nf__fc0.5__db__fo3"
    for key, voices, fits in ((st, 9, True), (st, 3, False), (mono, 3, True), (mono, 6, False)):
        vu = _put(key, voices if fits else (3 if voices > 3 else 9), meta=False)
        assert cc._sid_sidecar_fits(key, vu, voices) is fits
        vu.unlink(missing_ok=True)


def test_an_unreadable_sidecar_is_skipped_not_removed(cache):
    st = cc._cache_key(T2, "sid", 0, duration=180, variant="st")
    vu = _put(st, 6, meta=True)
    vu.write_bytes(b"VUM")                               # (cut short — being replaced, say)
    assert cc.get_vu_sidecar_path(T2, 0, sid_voices=6) is None
    assert vu.exists()


def test_the_meter_never_takes_renders_of_another_length(jobs, monkeypatch):
    async def slow_tab_job(job, src, track, subsong, mask):
        await asyncio.sleep(0.2)                         # (its length known only once it runs)
        job.dur = 120
        job.files = [_scope_wav(job, f"v{i}.wav", 10) for i in range(9)]
        job.measure = True
        await asyncio.sleep(0.2)
        job.done = True
    monkeypatch.setattr(voices, "_sid_job", slow_tab_job)

    async def run():
        three = SimpleNamespace(channels=3, format="SID")
        tab = await voices._start_job(three, "r", 0, "scope", 0,
                                      {"family": "sid", "src": Path("t.sid"), "duration": 120})
        assert tab.dur is None
        assert await voices.sid_voice_renders(three, "r", 0, Path("t.sid"), 180) is None
        assert tab.pins == 0
    asyncio.run(run())


def test_a_pass_without_a_result_is_not_run_again_and_get_vu_says_so(jobs, tmp_path, monkeypatch):
    from soniqboom.api import tracks as tracks_api
    from soniqboom.core import sid_vu
    monkeypatch.setattr(s, "_find_renderer", lambda *a: "/bin/true")
    monkeypatch.setattr(sid_vu, "build_vu", lambda wavs, dur: None)      # every voice silent
    monkeypatch.setattr(s, "_SID_VU_NO_RESULT", {})
    key = f"{T2}__sub0__dur180__st"
    anchor = tmp_path / "sid" / "5d" / f"{key}.wav"
    asyncio.run(s._sid_vu_worker(key, anchor, Path("t.sid"), 0, 180, chips=2))
    assert not anchor.with_suffix(".vu").exists() and s._sid_vu_no_result(key)
    spawned = []
    monkeypatch.setattr(s.asyncio, "create_task", lambda *a, **k: spawned.append(a))
    s._spawn_sid_vu(key, anchor, Path("t.sid"), 0, 180, 2)
    assert spawned == []                                  # (not again within the hour)

    tr = SimpleNamespace(id=T2, format="SID", channels=2, path="t.sid")

    async def get_track(tid):
        return tr
    monkeypatch.setattr(tracks_api, "get_track", get_track)
    monkeypatch.setattr(cc, "get_vu_sidecar_path", lambda *a, **k: None)
    monkeypatch.setattr(s, "tune_index", lambda *a, **k: 0)
    asked = []
    monkeypatch.setattr(s, "ensure_sid_vu_sidecar", lambda *a, **k: asked.append((a, k)))
    voices._SOURCES[(T2, 0)] = {"family": "sid", "cache_key": key, "src": Path("t.sid"),
                                "pin": None, "duration": 180}
    try:
        res = asyncio.run(tracks_api.get_vu_sidecar(T2, subsong=0, start=True, request=None))
        assert res.status_code == 204 and res.headers.get("X-VU-Unavailable") == "skipped"
        assert asked == []
        s.forget_sid_vu_no_result(T2)                     # (a changed file: may run again)
        res = asyncio.run(tracks_api.get_vu_sidecar(T2, subsong=0, start=True, request=None))
        assert res.status_code == 204 and res.headers.get("X-VU-Unavailable") is None
        assert asked and asked[0][1] == {"chips": 2}
        voices._SOURCES[(T2, 0)]["duration"] = s._SID_VU_MAX_DURATION + 1
        res = asyncio.run(tracks_api.get_vu_sidecar(T2, subsong=0, start=True, request=None))
        assert res.headers.get("X-VU-Unavailable") == "skipped"   # (longer than a pass takes)
    finally:
        voices._SOURCES.pop((T2, 0), None)


def test_a_scope_range_is_covered_only_once_every_voice_reached_it(tmp_path, monkeypatch):
    monkeypatch.setattr(voices, "_render_dir", lambda: str(tmp_path))
    sc = voices._Job(("c", 0, "scope", 0), "scope", 22050, 3)
    sc.measure = True
    sc.files = [_scope_wav(sc, "a.wav", 40), None, _scope_wav(sc, "c.wav", 40)]
    assert not sc.covered(10)
    sc.files[1] = _scope_wav(sc, "b.wav", 20)
    assert sc.covered(20) and not sc.covered(21)
    sc.close()


def test_the_light_engine_draws_and_never_plays(monkeypatch):
    three = SimpleNamespace(channels=3)
    scope = voices._sid_voice_cmds("sidplayfp", Path("t.sid"), three, 0, 180, 1, "scope", 0,
                                   [f"v{i}.wav" for i in range(9)], light=True)
    assert len(scope) == 9 and all(c[1] == "--sidlite" for c in scope)
    (mix,) = voices._sid_voice_cmds("sidplayfp", Path("t.sid"), three, 0, 180, 1, "mix", 0b11,
                                    ["o.wav"], light=True)
    assert "--sidlite" not in mix                         # (heard: reSIDfp)
    assert "--sidlite" not in s._sid_render_cmd("sidplayfp", Path("t.sid"), 0, 180, "o.wav",
                                                light=True)
    assert "--sidlite" not in voices._sid_voice_cmds("sidplayfp", Path("t.sid"), three, 0, 180, 1,
                                                     "scope", 0, ["v.wav"] * 9)[0]


def test_sidlite_is_used_only_where_it_renders(tmp_path, monkeypatch):
    import shutil as _sh
    monkeypatch.setattr(s, "_SIDLITE_OK", {})
    monkeypatch.setattr(s, "_rtmp", lambda: str(tmp_path))
    fake = tmp_path / "sidplayfp-old"
    fake.write_text("#!/bin/sh\necho 'unknown option' >&2\nexit 1\n")
    fake.chmod(0o755)
    assert s.sidlite_ok(str(fake)) is False and s._SIDLITE_OK[str(fake)] is False
    real = _sh.which("sidplayfp")
    if real is None:
        pytest.skip("no sidplayfp here")
    assert s.sidlite_ok(real) is True
    assert not list(tmp_path.glob("sidlite-*"))           # (its probe files are gone)
