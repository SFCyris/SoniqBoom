# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Playback that starts while the render runs, for every slow renderer.

Covers:
  * ``_LiveWav`` — the canonical growing WAV (exact / streaming header,
    audibility, trim-to-promise, failure cleanup);
  * ``_tail_render`` — following a renderer that writes its own WAV file
    (trailing chunk excluded, format mismatch falls back, failures and empty
    renders are errors that drop the live entry);
  * live MIDI / AdLib / SNDH / PSF / libgme renders through the stream
    endpoint — who gets the growing file (web UI; any client for an exact
    length), byte-identical results, silent tunes still a 422;
  * SID for every client (Subsonic / DLNA-style GETs, exact Content-Length,
    HEAD promising it, a silent tune's 422 on the first play, normalisation);
  * live encodes of a growing render (Subsonic ``format=``) and the cast
    pipeline's stdin feed.

Everything runs in-process against a throw-away conversion cache (``tmp_path``)
and fake stores — never a live server or real data.  Real renderers and the
local test fixtures are used when present, else those tests skip.
"""
from __future__ import annotations

import asyncio
import shutil
import struct
import sys
import time
from pathlib import Path

import pytest

from soniqboom.api import stream
from soniqboom.core import conversion_cache

from test_render_r1_stream import _T, _app, _client, _pcm


@pytest.fixture(autouse=True)
def _clean_module_state():
    """Leave the stream / cache module registries as each test found them."""
    regs = [stream._UADE_LIVE, stream._LIVE_FINISHED, stream._SID_START_SONG,
            stream._DEFAULT_TUNE, conversion_cache._silent_keys,
            conversion_cache._recent_failures]
    before = [set(r) for r in regs]
    yield
    for r, keys in zip(regs, before):
        for k in [k for k in list(r) if k not in keys]:
            r.pop(k, None)

REPO = Path(__file__).resolve().parent.parent
TD = REPO / "internal/testdata"
FS = REPO / "internal/format-samples"
SID = TD / "sid/SX-64_Demo.sid"
SNDH = TD / "atari/sndh/madmax1.sndh"
ADLIB = TD / "adlib/ALLOYRUN.A2M"
GSF = TD / "psf/gsf/01 Main Menu.minigsf"
SPC = TD / "game/sbtk-01.spc"
NSF = TD / "gme/8bp028-b1-nullsleep-axel_f.nsf"
KSS_SILENT = FS / "KSS/- unknown/arcus 1.kss"
SOUNDFONT = next(iter(sorted((REPO / "soundfonts").glob("*.sf2"))), REPO / "soundfonts/none.sf2")

CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
SAFARI = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")


def _needs(binary: str, *files: Path):
    missing = [str(f) for f in files if not f.exists()]
    return pytest.mark.skipif(
        not shutil.which(binary) or bool(missing),
        reason=f"{binary} or a local fixture is missing: {missing}")


def _square(frames: int, channels: int = 2) -> bytes:
    """Audible 16-bit PCM: a ±8000 square wave."""
    one = b"\x40\x1f" * channels
    neg = b"\xc0\xe0" * channels
    return ((one * 50 + neg * 50) * (frames // 100 + 1))[:frames * 2 * channels]


async def _noop(*a, **k):
    return None


async def _settle_sid():
    """Wait until every progressive SID render of a test has settled (its
    finaliser released the pool slot and dropped its registry entries)."""
    for _ in range(400):
        if stream._SID_PROG_ACTIVE[0] == 0 and not stream._SID_PROG_DONE:
            return
        await asyncio.sleep(0.05)


# ── _LiveWav ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_livewav_exact_header_audible_and_trim_to_promise():
    key = "lw-exact__sub0"
    lw = stream._LiveWav(key, expected_seconds=2.0, exact=True)
    try:
        live = stream._UADE_LIVE[key]
        assert live["exact"] is True
        assert live["expected_size"] == 44 + 2 * 44100 * 4
        head = Path(live["path"]).read_bytes()[:44]
        assert int.from_bytes(head[40:44], "little") == 2 * 44100 * 4  # exact from byte 0
        lw.feed(b"\x00" * 4410 * 4)
        assert live["audible"] is False and live["bytes"] == 4410 * 4  # silence isn't sound
        lw.feed(_square(44100 * 2 - 4410 + 200))       # 200 frames past the promise
        assert live["audible"] is True
        out = lw.finish()
        data = out.read_bytes()
        assert len(data) == 44 + 2 * 44100 * 4          # cut to exactly the promise
        assert int.from_bytes(data[40:44], "little") == len(data) - 44
    finally:
        lw.close()
    assert live["complete"].is_set() and live["clean_exit"] is True
    assert out.exists()
    out.unlink()
    stream._UADE_LIVE.pop(key, None)


@pytest.mark.asyncio
async def test_livewav_unknown_length_streams_then_patches_and_failure_drops():
    key = "lw-unk__sub0"
    lw = stream._LiveWav(key)
    live = stream._UADE_LIVE[key]
    assert live["expected_size"] == 0 and live["exact"] is False
    assert Path(live["path"]).read_bytes()[40:44] == b"\xff\xff\xff\xff"   # read to the end
    lw.feed(_square(1000))
    out = lw.finish()
    lw.close()
    data = out.read_bytes()
    assert int.from_bytes(data[40:44], "little") == len(data) - 44 == 4000
    out.unlink()
    stream._UADE_LIVE.pop(key, None)

    # A render that didn't finish: entry dropped, file gone, listeners told.
    key = "lw-fail__sub0"
    lw = stream._LiveWav(key, expected_seconds=5.0)
    live = stream._UADE_LIVE[key]
    lw.feed(_square(500))
    lw.close()
    assert key not in stream._UADE_LIVE
    assert live["complete"].is_set() and live["clean_exit"] is False
    assert not Path(live["path"]).exists()
    # "exact" needs a length
    lw = stream._LiveWav("lw-x__sub0", exact=True)
    assert stream._UADE_LIVE["lw-x__sub0"]["exact"] is False
    lw.close()


# ── _tail_render ────────────────────────────────────────────────────────────

_FAKE_RENDERER = r'''
import struct, sys, time
out, frames, chans, rc, empty = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5] == "1"
def hdr(n):
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE" + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, chans, 44100, 44100 * 2 * chans, 2 * chans, 16)
            + b"data" + struct.pack("<I", n))
one, neg = b"\x40\x1f" * chans, b"\xc0\xe0" * chans
pcm = ((one * 50 + neg * 50) * (frames // 100 + 1))[:frames * 2 * chans]
with open(out, "wb") as f:
    f.write(hdr(0)); f.flush()
    if not empty:
        step = len(pcm) // 10
        for i in range(10):
            f.write(pcm[i * step:(i + 1) * step if i < 9 else len(pcm)]); f.flush()
            time.sleep(0.06)
            if rc and i == 4:
                sys.exit(rc)
        f.write(b"LIST" + struct.pack("<I", 120) + b"x" * 120)     # a trailing tag chunk
        f.seek(0); f.write(hdr(len(pcm)))
sys.exit(0)
'''


def _fake_cmd(tmp_path: Path, out: Path, frames: int, chans: int = 2, rc: int = 0,
              empty: bool = False) -> list[str]:
    script = tmp_path / "fake_renderer.py"
    script.write_text(_FAKE_RENDERER)
    return [sys.executable, str(script), str(out), str(frames), str(chans), str(rc),
            "1" if empty else "0"]


async def _live_of(key: str, task) -> dict:
    """The live entry ``_tail_render`` registers for ``key``."""
    for _ in range(500):
        live = stream._UADE_LIVE.get(key)
        if live is not None or task.done():
            return live
        await asyncio.sleep(0.005)
    raise AssertionError("no live entry registered")


@pytest.mark.asyncio
async def test_tail_render_streams_audible_pcm_and_drops_the_trailer(tmp_path):
    key = "tail-ok__sub0"
    src = tmp_path / "r.wav"
    frames = 44100
    task = asyncio.ensure_future(stream._tail_render(
        _fake_cmd(tmp_path, src, frames), src, key, kind="FAKE", timeout=30))
    live = await _live_of(key, task)
    heard_before_end = False
    while not task.done():
        if live["audible"] and not live["complete"].is_set():
            heard_before_end = True
        await asyncio.sleep(0.01)
    out = await task
    try:
        assert heard_before_end, "the growing file never turned audible while rendering"
        data = out.read_bytes()
        assert _pcm(data) == _square(frames)              # no "LIST" trailer as audio
        assert len(data) == 44 + frames * 4
        assert not src.exists()
        assert live["clean_exit"] is True
    finally:
        out.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_tail_render_failures_and_format_mismatch(tmp_path):
    # A renderer that dies part-way: its error, the entry dropped, files gone.
    key = "tail-rc__sub0"
    src = tmp_path / "rc.wav"
    task = asyncio.ensure_future(stream._tail_render(
        _fake_cmd(tmp_path, src, 44100, rc=3), src, key, kind="FAKE", timeout=30))
    live = await _live_of(key, task)
    with pytest.raises(stream.HTTPException) as exc:
        await task
    assert exc.value.status_code == 502
    assert key not in stream._UADE_LIVE and live["clean_exit"] is False
    assert not src.exists() and not Path(live["path"]).exists()

    # Exit 0 without audio: a 422.
    src = tmp_path / "empty.wav"
    with pytest.raises(stream.HTTPException) as exc:
        await stream._tail_render(_fake_cmd(tmp_path, src, 0, empty=True), src,
                                  "tail-empty__sub0", kind="FAKE", timeout=30)
    assert exc.value.status_code == 422
    assert "tail-empty__sub0" not in stream._UADE_LIVE

    # Mono output where stereo was declared: never offered live; the
    # renderer's own (valid) file is the render.
    key = "tail-mono__sub0"
    src = tmp_path / "mono.wav"
    task = asyncio.ensure_future(stream._tail_render(
        _fake_cmd(tmp_path, src, 22050, chans=1), src, key, kind="FAKE", timeout=30))
    live = await _live_of(key, task)
    out = await task
    assert out == src and live["audible"] is False
    assert key not in stream._UADE_LIVE
    assert not Path(live["path"]).exists()
    src.unlink()


@pytest.mark.asyncio
async def test_a_live_wav_that_cannot_be_made_leaves_nothing_behind(monkeypatch, tmp_path):
    """m1/m2: a failure while the live WAV is set up deletes its temp and
    registers nothing; ``_tail_render`` then also deletes the renderer's."""
    # an absurd length is no length (and never an OverflowError)
    lw = stream._LiveWav("lw-inf__sub0", expected_seconds=float("inf"), exact=True)
    try:
        assert lw.exp_frames == 0 and stream._UADE_LIVE["lw-inf__sub0"]["exact"] is False
    finally:
        lw.close()
    made = []
    real_ntf = stream.tempfile.NamedTemporaryFile

    def ntf(*a, **k):
        f = real_ntf(*a, **k)
        made.append(Path(f.name))
        return f
    monkeypatch.setattr(stream.tempfile, "NamedTemporaryFile", ntf)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(stream, "_streaming_wav_header", boom)
    src = tmp_path / "never.wav"
    src.write_bytes(b"x")
    with pytest.raises(OSError):
        await stream._tail_render(["true"], src, "lw-broken__sub0", kind="FAKE", timeout=5)
    assert "lw-broken__sub0" not in stream._UADE_LIVE
    assert made and not any(p.exists() for p in made)
    assert not src.exists()


def test_live_wav_minimum_audible_length():
    """m3: with ``min_audible_seconds`` sound counts only once that much audio
    is in — an AdLib default subsong that is a split-second stub never gets a
    listener."""
    lw = stream._LiveWav("lw-min__sub0", min_audible_seconds=0.1)
    try:
        live = stream._UADE_LIVE["lw-min__sub0"]
        lw.feed(_square(2205))                 # 0.05 s of sound
        assert live["audible"] is False
        lw.feed(_square(2205))                 # 0.1 s now
        assert live["audible"] is True
    finally:
        lw.close()


# ── SID helpers ─────────────────────────────────────────────────────────────

def test_normalize_sid_wav(tmp_path):
    p = tmp_path / "s.wav"
    want = 10 * 88200
    p.write_bytes(stream._synth_wav_header(44100, 1, 16, want + 264) + b"\x01" * (want + 264))
    stream._normalize_sid_wav(p, want)                   # sidplayfp's overshoot cut
    d = p.read_bytes()
    assert len(d) == 44 + want and d[:44] == stream._synth_wav_header(44100, 1, 16, want)
    p.write_bytes(stream._synth_wav_header(44100, 1, 16, want - 100) + b"\x01" * (want - 100))
    stream._normalize_sid_wav(p, want)                   # a shortfall padded
    d = p.read_bytes()
    assert len(d) == 44 + want and d[-100:] == b"\x00" * 100
    big = want - 3 * 88200
    p.write_bytes(stream._synth_wav_header(44100, 1, 16, big) + b"\x01" * big)
    stream._normalize_sid_wav(p, want)                   # seconds off: left alone
    assert p.stat().st_size == 44 + big
    p.write_bytes(stream._build_wav_header(44100, 2, 100, 16) + b"\x01" * 400)
    stream._normalize_sid_wav(p, 400)                    # not sidplayfp's layout
    assert p.stat().st_size == 444


def _silent_psid(path: Path) -> Path:
    """A PSID whose init and play routines are a bare RTS: it plays nothing."""
    hdr = b"PSID" + struct.pack(">HHHHHHHI", 2, 0x7C, 0, 0x1000, 0x1001, 1, 1, 0)
    hdr += b"Silent".ljust(32, b"\0") + b"QA".ljust(32, b"\0") + b"2026".ljust(32, b"\0")
    hdr += struct.pack(">HBBH", 0x0014, 0, 0, 0)
    path.write_bytes(hdr + struct.pack("<H", 0x1000) + b"\x60\x60")
    return path


def _psf_with_tags(path: Path, tags: str) -> Path:
    path.write_bytes(b"PSF\x01" + struct.pack("<III", 0, 0, 0) + b"[TAG]" + tags.encode())
    return path


def test_psf_render_seconds_is_the_length_tag_without_fade(tmp_path):
    assert stream._psf_render_seconds(_psf_with_tags(
        tmp_path / "a.minipsf", "title=x\nlength=2:02.5\nfade=8\n")) == 122.5
    assert stream._psf_render_seconds(_psf_with_tags(tmp_path / "b.minipsf", "fade=0\n")) == 0.0
    # m1: no length that isn't one — not finite, or over the hour cap
    assert stream._psf_render_seconds(_psf_with_tags(tmp_path / "i.minipsf", "length=inf\n")) == 0.0
    assert stream._psf_render_seconds(_psf_with_tags(tmp_path / "n.minipsf", "length=nan\n")) == 0.0
    assert stream._psf_render_seconds(_psf_with_tags(
        tmp_path / "h.minipsf", "length=99:00:00\n")) == 0.0
    (tmp_path / "c.minipsf").write_bytes(b"not a psf")
    assert stream._psf_render_seconds(tmp_path / "c.minipsf") == 0.0


# ── live renders through the stream endpoint ────────────────────────────────

def _sid_track(tid: str, path: Path, dur: float = 0.0) -> _T:
    t = _T(tid, path, dur, fmt="SID")
    t.genre = ["Chiptune", "C64"]
    return t


@_needs("psgplay", SNDH)
@pytest.mark.asyncio
async def test_sndh_streams_while_rendering_to_every_client_with_its_exact_length(
        monkeypatch, tmp_path):
    ref = await stream._render_sndh(SNDH)
    ref_bytes = ref.read_bytes()
    ref.unlink()
    total = len(ref_bytes)
    tracks = {f"sn{i}": _T(f"sn{i}", SNDH, 0.0, fmt="SNDH") for i in range(3)}
    for t in tracks.values():
        t.genre = ["Chiptune"]
    app, _ = _app(monkeypatch, tmp_path, tracks)
    monkeypatch.setattr(stream, "_backfill_rendered_duration", _noop)
    async with _client(app) as c:
        r = await c.get("/api/stream/sn0", cookies={"sb_session": "x"},
                        headers={"User-Agent": CHROME})
        assert r.headers["x-cache"] == "miss-progressive"
        assert r.headers["content-length"] == str(total) and r.content == ref_bytes
        # a Subsonic / DLNA-style request (no session) gets it too: exact length
        r = await c.get("/api/stream/sn1?u=a&p=b")
        assert r.status_code == 200 and r.headers["x-cache"] == "miss-progressive"
        assert r.headers["content-length"] == str(total) and _pcm(r.content) == _pcm(ref_bytes)
        # Safari's range probe: two bytes against the final size, no waiting
        r = await c.get("/api/stream/sn2", cookies={"sb_session": "x"},
                        headers={"User-Agent": SAFARI, "Range": "bytes=0-1"})
        assert r.status_code == 206 and r.content == b"RI"
        assert r.headers["content-range"] == f"bytes 0-1/{total}"
        key = stream._ck("sn2", "sndh", subsong=0)
        for _ in range(200):                # its render finishes and is cached
            if await conversion_cache.get_cached(key) is not None:
                break
            await asyncio.sleep(0.05)
        r = await c.get("/api/stream/sn2", headers={"Range": f"bytes={total - 100}-"})
        assert r.status_code == 206 and r.headers["x-cache"] == "hit"
        assert r.content == ref_bytes[-100:]


@_needs("fluidsynth", SOUNDFONT)
@pytest.mark.asyncio
async def test_midi_streams_to_the_web_ui_but_not_to_a_client_needing_a_length(
        monkeypatch, tmp_path):
    from soniqboom.config import settings
    import mido
    mid = tmp_path / "t.mid"
    m = mido.MidiFile(ticks_per_beat=480)
    tr = mido.MidiTrack()
    m.tracks.append(tr)
    for b in range(60):
        tr.append(mido.Message("note_on", note=48 + b % 24, velocity=90, time=0))
        tr.append(mido.Message("note_off", note=48 + b % 24, velocity=0, time=480))
    m.save(mid)
    monkeypatch.setattr(settings, "soundfont_path", str(SOUNDFONT))
    tracks = {f"mi{i}": _T(f"mi{i}", mid, 30.0, fmt="MIDI") for i in range(3)}
    app, _ = _app(monkeypatch, tmp_path, tracks)
    async with _client(app) as c:
        r = await c.get("/api/stream/mi0", cookies={"sb_session": "x"},
                        headers={"User-Agent": CHROME})
        assert r.status_code == 200
        assert r.headers["x-cache"] == "miss-progressive"
        assert r.headers["x-render-length"] == "unknown"
        assert "content-length" not in r.headers               # chunked
        assert r.content[:4] == b"RIFF" and r.content[40:44] == b"\xff\xff\xff\xff"
        for _ in range(100):                 # the cache takes the file just after
            st = (await c.get("/api/stream/mi0/render-status")).json()
            if st["state"] == "complete":
                break
            await asyncio.sleep(0.05)
        assert st["state"] == "complete" and st["provisional"] is False
        cached = await conversion_cache.get_cached(stream._ck("mi0", "midi", 0, str(SOUNDFONT)))
        assert cached is not None and _pcm(cached.read_bytes()) == r.content[44:]
        assert abs(st["duration_seconds"] - (cached.stat().st_size - 44) / 176400) < 1e-6
        # a Subsonic-style client waits for the finished file (exact length)
        r = await c.get("/api/stream/mi1?u=a&p=b")
        assert r.headers["x-cache"] == "miss" and "content-length" in r.headers
        # Safari, unknown length: waits too, then gets real byte ranges
        r = await c.get("/api/stream/mi2", cookies={"sb_session": "x"},
                        headers={"User-Agent": SAFARI, "Range": "bytes=0-1"})
        assert r.status_code == 206 and r.headers["x-cache"] == "miss"


@_needs("adplay", ADLIB)
@pytest.mark.asyncio
async def test_adlib_live_render_matches_the_blocking_one(monkeypatch, tmp_path):
    ref = await stream._render_adlib(ADLIB)
    ref_pcm = _pcm(ref.read_bytes())
    ref.unlink()
    app, _ = _app(monkeypatch, tmp_path, {"ad0": _T("ad0", ADLIB, 180.0, fmt="AdLib")})
    monkeypatch.setattr(stream, "_backfill_rendered_duration", _noop)
    async with _client(app) as c:
        r = await c.get("/api/stream/ad0", cookies={"sb_session": "x"},
                        headers={"User-Agent": CHROME})
    assert r.headers["x-cache"] == "miss-progressive"   # the 180 s placeholder: no length
    assert r.headers["x-render-length"] == "unknown"
    assert r.content[44:] == ref_pcm


@pytest.mark.asyncio
async def test_adlib_empty_first_subsong_is_never_streamed_and_falls_back_to_the_probe(
        monkeypatch, tmp_path):
    """An unpinned AdLib play renders subsong 0 live; when it turns out empty
    (Westwood .adl) no listener is attached to it — the probe of the next
    subsongs runs as before and the first audible one is what plays."""
    calls = []

    async def fake_one(binary, path, subsong, live_key=None, *, expected_seconds=0.0,
                       min_audible_seconds=0.0):
        calls.append((subsong, live_key is not None, min_audible_seconds))
        frames = 44100 if subsong == 2 else 4410            # 0.1 s stubs, then music
        pcm = _square(frames) if subsong == 2 else b"\x00" * frames * 4
        src = tmp_path / f"s{subsong}.wav"
        src.write_bytes(stream._build_wav_header(44100, 2, frames, 16) + pcm)
        if live_key:
            return await stream._tail_render(
                [sys.executable, "-c", "import time; time.sleep(0.2)"], src, live_key,
                kind="FAKE", timeout=10, require_audio=False,
                expected_seconds=expected_seconds, min_audible_seconds=min_audible_seconds)
        return src

    monkeypatch.setattr(stream, "_render_adlib_one", fake_one)
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "adplay")
    adl = tmp_path / "dune.adl"
    adl.write_bytes(b"\x00" * 64)
    app, _ = _app(monkeypatch, tmp_path, {"wa0": _T("wa0", adl, 180.0, fmt="AdLib")})
    monkeypatch.setattr(stream, "_backfill_rendered_duration", _noop)
    async with _client(app) as c:
        r = await c.get("/api/stream/wa0", cookies={"sb_session": "x"},
                        headers={"User-Agent": CHROME})
    assert r.status_code == 200 and r.headers["x-cache"] == "miss"     # never streamed live
    assert _pcm(r.content) == _square(44100)                           # subsong 2's music
    assert calls[0] == (0, True, stream._ADLIB_MIN_AUDIO_S)
    assert (1, False, 0.0) in calls and (2, False, 0.0) in calls


@_needs("zxtune123", GSF)
@pytest.mark.asyncio
async def test_psf_promises_its_length_tag_and_lands_on_it(monkeypatch, tmp_path):
    secs = stream._psf_render_seconds(GSF)
    assert secs > 1
    app, _ = _app(monkeypatch, tmp_path, {"ps0": _T("ps0", GSF, secs + 10, fmt="GSF")})
    monkeypatch.setattr(stream, "_backfill_rendered_duration", _noop)
    async with _client(app) as c:
        r = await c.get("/api/stream/ps0", cookies={"sb_session": "x"},
                        headers={"User-Agent": CHROME})
        assert r.headers["x-cache"] == "miss-progressive"
        promised = 44 + int(round(secs * 44100)) * 4
        assert r.headers["content-length"] == str(promised)
        for _ in range(100):
            if (await c.get("/api/stream/ps0/render-status")).json()["state"] == "complete":
                break
            await asyncio.sleep(0.1)
        cached = await conversion_cache.get_cached(stream._ck("ps0", "psf", subsong=0))
        assert cached is not None and cached.stat().st_size == promised
        assert cached.read_bytes() == r.content


@_needs("ffmpeg", SPC)
@pytest.mark.asyncio
async def test_libgme_live_render_and_a_live_mp3_encode(monkeypatch, tmp_path):
    from soniqboom.core import gme_render
    if not gme_render.is_available():
        pytest.skip("libgme missing")
    tracks = {"gm0": _T("gm0", SPC, 180.0, fmt="SPC"), "gm1": _T("gm1", SPC, 180.0, fmt="SPC")}
    app, _ = _app(monkeypatch, tmp_path, tracks)
    monkeypatch.setattr(stream, "_backfill_rendered_duration", _noop)
    async with _client(app) as c:
        r = await c.get("/api/stream/gm0", cookies={"sb_session": "x"},
                        headers={"User-Agent": CHROME})
        assert r.headers["x-cache"] == "miss-progressive"
        ref = gme_render.render_wav(SPC.read_bytes(), 0, 180)
        assert _pcm(ref) == r.content[44:]
        # Subsonic format=mp3 of a cold render: a live encode of the growing file
        r = await c.get("/api/stream/gm1?u=a&p=b&format=mp3")
        assert r.status_code == 200 and r.headers["x-cache"] == "miss-progressive"
        assert r.headers["content-type"] == "audio/mpeg"
        assert r.content[:3] == b"ID3" or r.content[:2] == b"\xff\xfb"
        assert len(r.content) > 100_000


@_needs("ffmpeg", KSS_SILENT)
@pytest.mark.asyncio
async def test_a_silent_live_render_is_still_the_422(monkeypatch, tmp_path):
    from soniqboom.core import gme_render
    if not gme_render.is_available():
        pytest.skip("libgme missing")
    app, _ = _app(monkeypatch, tmp_path, {"ks0": _T("ks0", KSS_SILENT, 180.0, fmt="KSS")})
    async with _client(app) as c:
        r = await c.get("/api/stream/ks0", cookies={"sb_session": "x"},
                        headers={"User-Agent": CHROME})
        assert r.status_code == 422 and "only silence" in r.text
        r = await c.get("/api/stream/ks0", cookies={"sb_session": "x"})
        assert r.status_code == 422


def test_gme_render_pcm_streams_what_render_wav_returns():
    from soniqboom.core import gme_render
    if not gme_render.is_available() or not NSF.exists():
        pytest.skip("libgme or the NSF fixture is missing")
    data = NSF.read_bytes()
    parts: list[bytes] = []
    n = gme_render.render_pcm(data, 0, 20, parts.append)
    assert n == sum(map(len, parts)) > 0 and len(parts) > 10
    assert b"".join(parts) == _pcm(gme_render.render_wav(data, 0, 20))
    few: list[bytes] = []
    gme_render.render_pcm(data, 0, 20, lambda c: few.append(c) or len(few) < 3)
    assert len(few) == 3                                 # sink False stops it
    assert gme_render.render_pcm(b"junk", 0, 5, few.append) == 0


# ── SID for every client ────────────────────────────────────────────────────

@_needs("sidplayfp", SID)
@pytest.mark.asyncio
async def test_sid_streams_progressively_to_subsonic_and_head_promises_the_length(
        monkeypatch, tmp_path):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 20)
    monkeypatch.setattr(stream, "_spawn_sid_vu", lambda *a, **k: None)
    monkeypatch.setattr(stream, "ensure_sid_vu_sidecar", lambda *a, **k: None)
    tracks = {"sd0": _sid_track("sd0", SID), "sd1": _sid_track("sd1", SID)}
    app, _ = _app(monkeypatch, tmp_path, tracks)
    total = 44 + 20 * 88200
    try:
        async with _client(app) as c:
            h = await c.head("/api/stream/sd0?u=a&p=b")
            assert h.headers.get("content-length") == str(total)    # known for free
            t0 = time.monotonic()
            r = await c.get("/api/stream/sd0?u=a&p=b")
            assert r.status_code == 200 and r.headers["x-cache"] == "miss-progressive"
            assert r.headers["content-length"] == str(total) and len(r.content) == total
            await _settle_sid()
            key = stream._ck("sd0", "sid", 0, duration=20)
            cached = await conversion_cache.get_cached(key)
            # the cached file is exactly the resource that was streamed
            assert cached is not None and cached.read_bytes() == r.content
            h = await c.head("/api/stream/sd0?u=a&p=b")
            assert h.headers.get("content-length") == str(total)
            # a DLNA-style Range GET on another cold tune: 206 against the total
            r = await c.get("/api/stream/sd1?u=a&p=b", headers={"Range": "bytes=1000-1999"})
            assert r.status_code == 206 and len(r.content) == 1000
            assert r.headers["content-range"] == f"bytes 1000-1999/{total}"
            assert time.monotonic() - t0 < 30
    finally:
        await _settle_sid()


@_needs("sidplayfp", SID)
@pytest.mark.asyncio
async def test_a_silent_sid_is_the_422_on_its_first_play_for_every_client(
        monkeypatch, tmp_path):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 10)
    monkeypatch.setattr(stream, "_spawn_sid_vu", lambda *a, **k: None)
    monkeypatch.setattr(stream, "ensure_sid_vu_sidecar", lambda *a, **k: None)
    sil = _silent_psid(tmp_path / "silent.sid")
    tracks = {"ss0": _sid_track("ss0", sil), "ss1": _sid_track("ss1", sil)}
    app, _ = _app(monkeypatch, tmp_path, tracks)
    try:
        async with _client(app) as c:
            r = await c.get("/api/stream/ss0", cookies={"sb_session": "x"})
            assert r.status_code == 422 and "only silence" in r.text
            st = (await c.get("/api/stream/ss0/render-status")).json()
            assert st["state"] == "failed" and st["error_status"] == 422
            r = await c.get("/api/stream/ss1?u=a&p=b")
            assert r.status_code == 422
        assert await conversion_cache.get_cached(stream._ck("ss0", "sid", 0, duration=10)) is None
    finally:
        await _settle_sid()


@_needs("sidplayfp", SID)
@pytest.mark.asyncio
async def test_sid_mp3_for_subsonic_encodes_the_growing_render(monkeypatch, tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg missing")
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 15)
    monkeypatch.setattr(stream, "_spawn_sid_vu", lambda *a, **k: None)
    monkeypatch.setattr(stream, "ensure_sid_vu_sidecar", lambda *a, **k: None)
    app, _ = _app(monkeypatch, tmp_path, {"sm0": _sid_track("sm0", SID)})
    try:
        async with _client(app) as c:
            r = await c.get("/api/stream/sm0?u=a&p=b&format=mp3")
            assert r.status_code == 200 and r.headers["x-cache"] == "miss-progressive"
            assert r.headers["content-type"] == "audio/mpeg" and len(r.content) > 50_000
            await _settle_sid()
            # the render was cached; the transcode cache fills from it
            key = stream._ck("sm0", "sid", 0, duration=15)
            assert await conversion_cache.get_cached(key) is not None
            for _ in range(100):
                r2 = await c.get("/api/stream/sm0?u=a&p=b&format=mp3")
                if r2.headers.get("x-transcode-cache") == "hit":
                    break
                await asyncio.sleep(0.1)
            assert r2.headers.get("x-transcode-cache") == "hit"
    finally:
        await _settle_sid()


# ── the cast pipeline's live feed ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_cast_pipe_encodes_a_live_feed_and_never_caches_a_failed_one(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg missing")
    from soniqboom.core.cast_pipe import render_stream
    frames = 44100 * 3
    wav = stream._build_wav_header(44100, 2, frames, 16) + _square(frames)

    async def feed(fail: bool):
        for i in range(0, len(wav), 65536):
            yield wav[i:i + 65536]
            await asyncio.sleep(0)
            if fail and i > 200_000:
                raise stream._RenderAborted("boom")

    sink = tmp_path / "out.mp3"
    got = b"".join([c async for c in render_stream(
        tmp_path / "live.wav", codec="mp3", src_feed=feed(False), cache_sink=sink)])
    assert len(got) > 20_000 and sink.exists()            # cached on a clean feed

    sink2 = tmp_path / "out2.mp3"
    with pytest.raises(RuntimeError):
        async for _ in render_stream(tmp_path / "live2.wav", codec="mp3",
                                     src_feed=feed(True), cache_sink=sink2):
            pass
    assert not sink2.exists() and not sink2.with_suffix(".mp3.partial").exists()


@pytest.mark.asyncio
async def test_cast_prepare_live_source_feeds_a_libgme_render(monkeypatch, tmp_path):
    from soniqboom.core import gme_render
    if not gme_render.is_available() or not SPC.exists():
        pytest.skip("libgme or the SPC fixture is missing")
    from soniqboom.config import settings
    from soniqboom.core import cast_render
    from soniqboom.core import data as data_mod
    from soniqboom.core import store as store_mod
    from test_render_r1_stream import FakeStore
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    t = _T("cl0", SPC, 180.0, fmt="SPC")
    fs = FakeStore({"cl0": t})
    monkeypatch.setattr(store_mod, "get_store", lambda: fs)
    monkeypatch.setattr(data_mod, "get_store", lambda: fs)

    async def get_track(tid):
        return t if tid == "cl0" else None
    monkeypatch.setattr(data_mod, "get_track", get_track)
    monkeypatch.setattr(stream, "get_track", get_track)
    feed = await cast_render.prepare_live_source(track_id="cl0", track_path=str(SPC))
    assert feed is not None
    data = b"".join([c async for c in feed])
    assert data[:4] == b"RIFF" and _pcm(gme_render.render_wav(SPC.read_bytes(), 0, 180)) == data[44:]
    for _ in range(50):
        cached = await conversion_cache.get_cached(stream._ck("cl0", "gme", subsong=0))
        if cached is not None:
            break
        await asyncio.sleep(0.1)
    assert cached is not None
    # cached now: nothing live to offer — the caller takes the file
    assert await cast_render.prepare_live_source(track_id="cl0", track_path=str(SPC)) is None


# ── growing-file range answers (m4) and the live-render gap guard (M3) ─────

class _Req:
    def __init__(self, rng=None):
        self.headers = {"range": rng} if rng else {}


async def _body(resp) -> bytes:
    return b"".join([c async for c in resp.body_iterator])


@pytest.mark.asyncio
async def test_growing_range_suffix_range_and_416(tmp_path):
    p = tmp_path / "g.wav"
    data = stream._build_wav_header(44100, 2, 4410, 16) + _square(4410)
    p.write_bytes(data)
    done = asyncio.Event()
    done.set()
    r = await stream._growing_file_range_response(_Req("bytes=-100"), p, len(data), done,
                                                  "audio/wav")
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes {len(data) - 100}-{len(data) - 1}/{len(data)}"
    assert await _body(r) == data[-100:]
    r = await stream._growing_file_range_response(_Req(f"bytes={len(data) + 5}-"), p,
                                                  len(data), done, "audio/wav")
    assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{len(data)}"
    r = await stream._growing_file_range_response(_Req("bytes=0-"), p, len(data), done,
                                                  "audio/wav")
    assert r.status_code == 206 and await _body(r) == data


@pytest.mark.asyncio
async def test_a_live_render_is_never_padded_with_more_than_a_second(tmp_path):
    p = tmp_path / "short.wav"
    data = stream._build_wav_header(44100, 2, 44100, 16) + _square(44100)   # 1 s
    p.write_bytes(data)
    done = asyncio.Event()
    done.set()
    live = {"no_pad_on_failure": True, "clean_exit": True, "bytes_per_sec": 176400}
    # promised 3 s: ends short (2 s of silence would be padding)
    r = await stream._growing_file_range_response(_Req(), p, 44 + 3 * 176400, done,
                                                  "audio/wav", inflight=live)
    assert r.headers["content-length"] == str(44 + 3 * 176400)
    assert await _body(r) == data
    # promised 1.5 s: within a second — padded to the promise
    r = await stream._growing_file_range_response(_Req(), p, 44 + 264600, done,
                                                  "audio/wav", inflight=live)
    got = await _body(r)
    assert len(got) == 44 + 264600 and got[:len(data)] == data


@pytest.mark.asyncio
async def test_a_feed_carries_a_streaming_header_unless_the_length_is_exact():
    """M3: an encode / cast feed of a live render whose length is only a stored
    guess reads to the end (ffmpeg honours a WAV header's length), an exact
    one keeps its exact header."""
    for exact in (False, True):
        key = f"feed-{exact}__sub0"
        lw = stream._LiveWav(key, expected_seconds=2.0, exact=exact)
        live = stream._UADE_LIVE[key]
        lw.feed(_square(44100))
        feed = await stream._live_feed_of(live)
        first = await feed.__anext__()
        if exact:
            assert int.from_bytes(first[40:44], "little") == 2 * 176400
        else:
            assert first[:44] == stream._streaming_wav_header(44100, 2, 16)
        lw.feed(_square(44100 * 3))        # the render runs past the guess
        lw.finish()
        lw.close()
        rest = b"".join([c async for c in feed])
        got = first + rest
        if exact:
            assert len(got) - 44 == 4 * 176400
        else:
            assert got[44:] == _square(44100) + _square(44100 * 3)   # nothing cut


@pytest.mark.asyncio
async def test_a_live_render_corrects_a_stale_stored_length(monkeypatch, tmp_path):
    """M3: the render is the authority for the length of a live AdLib render
    (a stored one the live start promised but the render disagrees with)."""
    async def fake_one(binary, path, subsong, live_key=None, *, expected_seconds=0.0,
                       min_audible_seconds=0.0):
        src = tmp_path / f"src{subsong}.wav"
        src.write_bytes(stream._build_wav_header(44100, 2, 88200, 16) + _square(88200))
        return await stream._tail_render([sys.executable, "-c", "pass"], src, live_key,
                                         kind="FAKE", timeout=10, require_audio=False,
                                         expected_seconds=expected_seconds)
    monkeypatch.setattr(stream, "_render_adlib_one", fake_one)
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "adplay")
    adl = tmp_path / "tune.rad"
    adl.write_bytes(b"\x00" * 64)
    app, fs = _app(monkeypatch, tmp_path, {"st0": _T("st0", adl, 5.0, fmt="AdLib")})
    async with _client(app) as c:
        r = await c.get("/api/stream/st0", cookies={"sb_session": "x"},
                        headers={"User-Agent": CHROME})
        assert r.status_code == 200
    for _ in range(100):
        if ("st0", {"duration": 2.0}) in fs.updates:
            break
        await asyncio.sleep(0.05)
    assert ("st0", {"duration": 2.0}) in fs.updates


# ── SID: never streamed before sound for clients other than the web UI (M1);
#     one render per tune (m5) ───────────────────────────────────────────────

_FAKE_SIDPLAYFP = r"""
import struct, sys, time
out, secs, sleep_s = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
n = int(secs * 44100)
pcm = ((b"\x40\x1f" * 50 + b"\xc0\xe0" * 50) * (n // 100 + 1))[:n * 2]
with open(out, "wb") as f:
    f.write(b"RIFF" + struct.pack("<I", 36) + b"WAVE" + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 44100, 88200, 2, 16) + b"data" + struct.pack("<I", 0))
    f.write(pcm); f.flush()
    time.sleep(sleep_s)
"""


@pytest.mark.asyncio
async def test_a_silent_sid_streams_to_the_web_ui_only_after_its_wait_but_never_to_others(
        monkeypatch, tmp_path):
    if not shutil.which("sidplayfp"):
        pytest.skip("sidplayfp missing")
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 10)
    monkeypatch.setattr(stream, "_spawn_sid_vu", lambda *a, **k: None)
    monkeypatch.setattr(stream, "ensure_sid_vu_sidecar", lambda *a, **k: None)
    # the web UI's wait for sound, made tiny: it streams before the end
    monkeypatch.setattr(stream, "_SID_PROG_AUDIBLE_WAIT", 0.0)
    sil = _silent_psid(tmp_path / "silent.sid")
    tracks = {f"sw{i}": _sid_track(f"sw{i}", sil) for i in range(3)}
    app, _ = _app(monkeypatch, tmp_path, tracks)
    try:
        async with _client(app) as c:
            r = await c.get("/api/stream/sw0", cookies={"sb_session": "x"})
            assert r.status_code == 200 and r.headers["x-cache"] == "miss-progressive"
            # Subsonic / DLNA: waits for sound or the end — the 422
            r = await c.get("/api/stream/sw1?u=a&p=b")
            assert r.status_code == 422
            # an encode (Subsonic format=, the cast pipeline's feed): the 422 too
            r = await c.get("/api/stream/sw2?u=a&p=b&format=mp3")
            assert r.status_code == 422
    finally:
        await _settle_sid()


@pytest.mark.asyncio
async def test_a_stalled_sid_render_is_padded_for_the_web_ui_and_aborted_for_others(
        monkeypatch, tmp_path):
    from soniqboom.config import settings
    script = tmp_path / "fake_sidplayfp.py"
    script.write_text(_FAKE_SIDPLAYFP)
    monkeypatch.setattr(settings, "sid_default_duration", 5)
    monkeypatch.setattr(stream, "_spawn_sid_vu", lambda *a, **k: None)
    monkeypatch.setattr(stream, "ensure_sid_vu_sidecar", lambda *a, **k: None)
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: sys.executable)
    monkeypatch.setattr(stream, "_sid_render_cmd",
                        lambda binary, path, subsong, dur, out, mute=(), start=1:
                        [sys.executable, str(script), out, "0.5", "30"])
    monkeypatch.setattr(stream, "_GROWING_READ_TIMEOUT", 0.4)
    sid = _silent_psid(tmp_path / "x.sid")          # content ignored by the fake
    tracks = {f"st{i}": _sid_track(f"st{i}", sid) for i in range(2)}
    app, _ = _app(monkeypatch, tmp_path, tracks)
    total = 44 + 5 * 88200
    try:
        async with _client(app) as c:
            r = await c.get("/api/stream/st0", cookies={"sb_session": "x"})
            assert r.status_code == 200 and len(r.content) == total   # padded
            with pytest.raises(stream._RenderAborted):
                await c.get("/api/stream/st1?u=a&p=b")
        for k in ("st0", "st1"):                     # never cached
            assert await conversion_cache.get_cached(stream._ck(k, "sid", 0, duration=5)) is None
    finally:
        await _settle_sid()


@pytest.mark.asyncio
async def test_a_second_request_attaches_to_the_running_sid_render(monkeypatch, tmp_path):
    """m5: concurrent GETs of one cold tune, and a reconnect after the first
    listener left, share one sidplayfp."""
    if not shutil.which("sidplayfp") or not SID.exists():
        pytest.skip("sidplayfp or the SID fixture is missing")
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(stream, "_spawn_sid_vu", lambda *a, **k: None)
    spawned = []
    real_spawn = stream.forksafe.spawn

    async def counting_spawn(*cmd, **kw):
        if "sidplayfp" in str(cmd[0]):
            spawned.append(cmd)
        return await real_spawn(*cmd, **kw)
    monkeypatch.setattr(stream.forksafe, "spawn", counting_spawn)
    dur = 60
    key = conversion_cache._cache_key("att0", "sid", 0, duration=dur)
    total = 44 + dur * 88200
    try:
        r1 = await stream._serve_sid_progressive(_Req(), SID, 0, dur, key, {}, None, web=False)
        r2 = await stream._serve_sid_progressive(_Req("bytes=1000-"), SID, 0, dur, key, {},
                                                 None, web=False)
        assert r1 is not None and r2 is not None and len(spawned) == 1
        a, b = await asyncio.gather(_body(r1), _body(r2))
        assert len(a) == total and b == a[1000:]
        await _settle_sid()
        # a listener leaves early; the render keeps going (detached) and a
        # reconnect with a Range takes it back
        key2 = conversion_cache._cache_key("att1", "sid", 0, duration=dur)
        r3 = await stream._serve_sid_progressive(_Req(), SID, 0, dur, key2, {}, None, web=False)
        it = r3.body_iterator.__aiter__()
        first = await it.__anext__()
        await it.aclose()
        assert key2 in stream._SID_DETACHED           # still rendering, for the cache
        r4 = await stream._serve_sid_progressive(_Req(f"bytes={total - 5000}-"), SID, 0, dur,
                                                 key2, {}, None, web=False)
        assert r4 is not None and key2 not in stream._SID_DETACHED   # taken back
        tail = await _body(r4)
        assert len(tail) == 5000 and len(spawned) == 2
        await _settle_sid()
        cached = await conversion_cache.get_cached(key2)
        assert cached is not None and cached.stat().st_size == total
        assert first == cached.read_bytes()[:len(first)]
    finally:
        await _settle_sid()


@pytest.mark.asyncio
async def test_sid_head_agrees_with_get_and_changes_nothing(monkeypatch, tmp_path):
    """m9: HEAD's answer for an uncached local C64 SID is GET's: the exact
    length, or GET's error (a tune known to be silent, no sidplayfp) — and a
    HEAD remembers nothing (no start song noted or persisted)."""
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 12)
    persisted = []
    monkeypatch.setattr(stream, "_persist_start_song", lambda *a: persisted.append(a))
    sid = _silent_psid(tmp_path / "h.sid")
    app, _ = _app(monkeypatch, tmp_path, {"hd0": _sid_track("hd0", sid)})
    async with _client(app) as c:
        monkeypatch.setattr(stream, "_find_renderer", lambda *a: "/bin/sidplayfp")
        h = await c.head("/api/stream/hd0?u=a&p=b")
        assert h.status_code == 200 and h.headers["content-length"] == str(44 + 12 * 88200)
        assert persisted == [] and "hd0" not in stream._SID_START_SONG
        conversion_cache.note_silent(stream._ck("hd0", "sid", 0, duration=12))
        assert (await c.head("/api/stream/hd0?u=a&p=b")).status_code == 422
        monkeypatch.setattr(stream, "_find_renderer", lambda *a: None)
        assert (await c.head("/api/stream/hd0?u=a&p=b")).status_code == 501
