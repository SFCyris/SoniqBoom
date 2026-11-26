# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loudness matching across engines (opportunity review v2.2.0, feature 2).

A render is measured (EBU R128, ffmpeg ``ebur128``) when it lands in the
conversion cache; a local file without ReplayGain by an idle background pass
or as the player's next track.  The result is stored fill-only on the track
(``loudness`` / ``loudness_source`` "analysed"), journaled, never over
ReplayGain tags, measured once per tune, carried over a rescan of the same
file and dropped for a changed one; render-status hands it to the player and
Subsonic fills ``replayGain`` from it.  ffmpeg is required (it is a hard
dependency of the server); real renderers are used where installed."""
from __future__ import annotations

import asyncio
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from soniqboom.core import conversion_cache as cc
from soniqboom.core import loudness as L
from soniqboom.core import merger as M, persistence as P, store as S
from soniqboom.core.aof import AOFWriter

REPO = Path(__file__).resolve().parent.parent
FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg absent")


def _sine(path: Path, amp: float, secs: float = 6.0, freq: int = 997, rate: int = 44100,
          fmt: str = "pcm_s16le") -> Path:
    """A stereo sine WAV at linear amplitude ``amp``."""
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"aevalsrc={amp}*sin(2*PI*{freq}*t)|{amp}*sin(2*PI*{freq}*t):s={rate}:d={secs}",
                    "-c:a", fmt, str(path)], check=True)
    return path


@pytest.fixture(autouse=True)
def _fresh():
    L.reset_for_tests()
    yield
    L.reset_for_tests()


# ── Measurement ─────────────────────────────────────────────────────────────

@needs_ffmpeg
def test_measure_reads_integrated_loudness_and_true_peak(tmp_path):
    """Two sines 12 dB apart measure 12 LU apart; the peak is the amplitude;
    silence measures nothing (gated out)."""
    a = L.measure(_sine(tmp_path / "a.wav", 0.5))
    b = L.measure(_sine(tmp_path / "b.wav", 0.5 / 3.981))      # −12 dB
    assert a and b
    assert abs((a["i"] - b["i"]) - 12.0) < 0.2
    assert abs(a["tp"] - 0.5) < 0.02 and abs(a["sp"] - 0.5) < 0.01
    silent = _sine(tmp_path / "s.wav", 0.0)
    assert L.measure(silent) is None
    assert L.measure(tmp_path / "missing.wav") is None


@needs_ffmpeg
def test_a_float_render_above_full_scale_keeps_its_peaks(tmp_path):
    """A 32-bit float render (libopenmpt) may peak past full scale: the sample
    peak is stored, and a transcode — made turned down by it — is reported
    with the gain raised and the peak lowered by as much."""
    hot = _sine(tmp_path / "hot.wav", 1.6, fmt="pcm_f32le", rate=48000)
    m = L.measure(hot)
    assert m and m["sp"] > 1.5 and m["tp"] >= m["sp"] - 0.01
    e = L.entry_of(m)
    assert len(e) == 3 and e[2] > 1.5
    g, tp = L.served_gain_peak(e)
    assert abs(g - (L.gain_db(e) + 20 * math.log10(e[2]))) < 1e-9
    assert abs(tp - e[1] / e[2]) < 1e-9
    assert L.entry_of({"i": -14.04, "tp": 0.91, "sp": 0.9}) == [-14.0, 0.91]


def test_parse_summary_takes_the_last_summary_block():
    text = ("[Parsed_ebur128_0 @ 0x1] Summary:\n\n  Integrated loudness:\n    I:  -70.0 LUFS\n"
            "\n  True peak:\n    Peak:  -inf dBFS\n"
            "[Parsed_ebur128_0 @ 0x2] Summary:\n\n  Integrated loudness:\n    I:  -14.3 LUFS\n"
            "    Threshold: -24.5 LUFS\n\n  Loudness range:\n    LRA:  3.0 LU\n\n"
            "  Sample peak:\n    Peak:   -2.0 dBFS\n\n  True peak:\n    Peak:   -1.0 dBFS\n")
    m = L.parse_summary(text)
    assert m["i"] == -14.3
    assert abs(m["tp"] - 10 ** (-1 / 20)) < 1e-9 and abs(m["sp"] - 10 ** (-2 / 20)) < 1e-9
    assert L.parse_summary(text[: text.index("[Parsed_ebur128_0 @ 0x2]")]) is None   # silence
    assert L.parse_summary("") is None and L.parse_summary("no summary") is None


def test_the_command_measures_true_and_sample_peak_niced():
    cmd = L.measure_args("/x.flac", pre_filters=("volume=-6dB",), ffmpeg="ffmpeg")
    af = cmd[cmd.index("-af") + 1]
    assert af == "volume=-6dB,ebur128=peak=true+sample:framelog=quiet"
    assert cmd[-3:] == ["null", "-", ] or cmd[-2:] == ["null", "-"]
    if shutil.which("nice"):
        assert cmd[:3] == [shutil.which("nice"), "-n", "19"]


def test_tune_keys_of_render_cache_keys():
    assert L.key_tune("abc__sub3__dur180") == ("abc", 3)
    assert L.key_tune("abc__sub0__r2") == ("abc", 0)
    assert L.key_tune("abc__r1__vgt180") == ("abc", 0)          # PSF route: one tune
    assert L.key_tune("abc__sfdeadbeef") == ("abc", 0)          # MIDI
    assert L.key_tune("") is None


def test_bare_and_tune_keys_follow_the_render_tune_index():
    sid = {"format": "SID", "default_subsong": 3}
    gme = {"format": "NSF", "default_subsong": 3}
    assert L.bare_key(sid) == "0"           # wire 0 = the header's default tune
    assert L.bare_key(gme) == "3"           # a bare play renders the probed default
    assert L.bare_key({"format": "NSF"}) == "0"


# ── The store: fill-only, journaled, once per tune ──────────────────────────

def _track(tid: str, path: str, fmt: str = "SID", **kw) -> dict:
    d = {"id": tid, "path": path, "title": tid, "artist": "x", "album": "", "format": fmt,
         "duration": 60.0, "added_at": 1, "file_size": 1000, "mtime": 5}
    d.update(kw)
    return d


class _Run:
    """One server run on ``d``: a fresh store loaded from its files, journaled."""

    def __init__(self, d, monkeypatch):
        self.d = d
        monkeypatch.setattr(S, "_store", S.TrackStore())
        self.store = S.get_store()
        P.init_persistence(d)
        self.w = AOFWriter(d / "library.aof")
        self.store._aof_append = self.w.append

    def stop(self):
        self.w.flush_sync()
        M._do_merge(self.d)
        self.w.stop()
        self.w.seal()


async def _drain(timeout: float = 30.0) -> None:
    """Wait for the measuring queue to empty, then write what is pending."""
    t0 = asyncio.get_running_loop().time()
    while (L._QUEUED or (L._WORKER is not None and not L._WORKER.done() and L._QUEUE)):
        if asyncio.get_running_loop().time() - t0 > timeout:
            raise AssertionError("loudness queue did not drain")
        await asyncio.sleep(0.05)
    L.flush()


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    from soniqboom import config
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setattr(cc, "get_conversion_cache_dir", lambda: d)
    monkeypatch.setattr(config.settings, "conversion_cache_max_bytes", 10 ** 10, raising=False)
    return d


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_cached_render_is_measured_once_journaled_and_survives_a_restart(
        tmp_path, monkeypatch, cache_dir):
    """Cold (tune 0, the first render) and another input (tune 2, quieter):
    both measured and journaled; a second render of tune 0 (a SID re-render
    differs slightly — sidplayfp's random power-on state) is NOT measured
    again; after a restart the values are there."""
    import soniqboom.api.stream  # noqa: F401 — registers the cache hook
    assert cc.on_stored is L.note_render_stored
    data = tmp_path / "data"
    data.mkdir()
    r = _Run(data, monkeypatch)
    r.store.upsert_tracks_batch([_track("sid1", "/m/a.sid", subsongs=3)])
    loud = _sine(tmp_path / "loud.wav", 0.5)
    quiet = _sine(tmp_path / "quiet.wav", 0.125)
    await cc.store_cached("sid1__sub0__dur180", "sid", loud)
    await cc.store_cached("sid1__sub2__dur180", "sid", quiet)
    await _drain()
    m = r.store.get_track("sid1")["loudness"]
    assert set(m) == {"0", "2"} and r.store.get_track("sid1")["loudness_source"] == "analysed"
    assert abs((m["0"][0] - m["2"][0]) - 12.04) < 0.3
    first = list(m["0"])
    # the same tune rendered again, louder: kept as first measured
    again = _sine(tmp_path / "again.wav", 0.9)
    await cc.store_cached("sid1__sub0__dur180", "sid", again)
    await _drain()
    assert r.store.get_track("sid1")["loudness"]["0"] == first
    r.stop()
    r = _Run(data, monkeypatch)
    assert r.store.get_track("sid1")["loudness"] == m


@needs_ffmpeg
@pytest.mark.asyncio
async def test_tags_win_transcodes_are_not_measured(tmp_path, monkeypatch, cache_dir):
    """A file with ReplayGain tags is never measured nor overwritten; a
    transcode entry is never measured (its source's loudness is)."""
    import soniqboom.api.stream  # noqa: F401
    monkeypatch.setattr(S, "_store", S.TrackStore())
    st = S.get_store()
    st.upsert_tracks_batch([_track("tg", "/m/t.flac", "FLAC", replaygain_track_gain=-3.2),
                            _track("nt", "/m/n.flac", "FLAC")])
    w = _sine(tmp_path / "w.wav", 0.5)
    await cc.store_cached("tg__sub0__r2", "tracker", w)
    await cc.store_cached("nt__cflac__ar0", "transcoded", _sine(tmp_path / "x.wav", 0.5))
    await _drain()
    assert st.get_track("tg").get("loudness") is None
    assert st.get_track("nt").get("loudness") is None
    assert L.record("tg", 0, [-12.0, 0.5]) is False
    L.flush()
    assert st.get_track("tg").get("loudness") is None and st.get_track("tg")["replaygain_track_gain"] == -3.2


def test_rescan_of_the_same_file_keeps_it_a_changed_file_loses_it():
    old = _track("a", "/m/a.sid", subsongs=3, loudness={"0": [-14.0, 0.9], "2": [-20.0, 0.5]},
                 loudness_source="analysed")
    same = _track("a", "/m/a.sid", subsongs=3)
    S._carry_enrichment(old, same)
    assert same["loudness"] == old["loudness"] and same["loudness_source"] == "analysed"
    changed = _track("a", "/m/a.sid", subsongs=3, file_size=2000)
    S._carry_enrichment(old, changed)
    assert "loudness" not in changed
    tagged = _track("a", "/m/a.sid", subsongs=3, replaygain_track_gain=-2.0)
    S._carry_enrichment(old, tagged)
    assert "loudness" not in tagged
    fewer = _track("a", "/m/a.mod", "ProTracker", subsongs=2,
                   loudness=None)
    old_mod = _track("a", "/m/a.mod", "ProTracker", subsongs=3,
                     loudness={"0": [-14.0, 0.9], "2": [-20.0, 0.5]})
    S._carry_enrichment(old_mod, fewer)
    assert fewer["loudness"] == {"0": [-14.0, 0.9]}      # (tune 2 is gone)


def test_a_moved_file_takes_its_loudness_along():
    from soniqboom.core import track_moves as tm
    old = _track("a", "/m/a.flac", "FLAC", loudness={"0": [-9.1, 0.99]}, loudness_source="analysed")
    new = _track("b", "/n/a.flac", "FLAC")
    out = tm.carried_fields(old, new)
    assert out["loudness"] == {"0": [-9.1, 0.99]} and out["loudness_source"] == "analysed"


def test_store_writes_of_loudness_leave_the_library_caches_warm(monkeypatch):
    """``loudness`` is seq / browse / enrichment exempt: the lists, legends,
    skip rules and enrichment passes are not re-cooled by a measurement."""
    monkeypatch.setattr(S, "_store", S.TrackStore())
    st = S.get_store()
    st.upsert_tracks_batch([_track("a", "/m/a.flac", "FLAC")])
    seq, bseq = st._mutation_seq, st._browse_seq
    rev = st._track_rev.get("a", 0)
    assert st.update_track_fields("a", {"loudness": {"0": [-10.0, 0.9]}, "loudness_source": "analysed"})
    assert (st._mutation_seq, st._browse_seq) == (seq, bseq)
    assert st._track_rev["a"] == rev + 1          # (the Subsonic song memo refreshes)


# ── Native files ────────────────────────────────────────────────────────────

def test_native_candidates_are_local_untagged_unmeasured_files():
    ok = _track("a", "/m/a.flac", "FLAC")
    assert L.native_candidate(ok)
    assert not L.native_candidate({**ok, "replaygain_album_gain": -1.0})
    assert not L.native_candidate({**ok, "loudness": {"0": [-9.0, 0.9]}})
    assert not L.native_candidate({**ok, "path": "smb://nas/m/a.flac"})
    assert not L.native_candidate({**ok, "path": "/m/x.zip::a.flac"})
    assert not L.native_candidate({**ok, "absent_since": 5})
    assert not L.native_candidate(_track("s", "/m/a.sid"))
    assert not L.native_candidate(_track("d", "/m/a.dsf", "DSF (Dreamcast)"))
    assert L.native_candidate(_track("d", "/m/a.dsf", "DSF"))
    assert L._native_filters("/m/a.dsf") == ("highpass=f=20", "lowpass=f=40000", "volume=-6dB")
    assert L._native_filters("/m/a.flac") == ()


@needs_ffmpeg
@pytest.mark.asyncio
async def test_the_native_pass_measures_while_idle_and_waits_for_renders(tmp_path, monkeypatch):
    """Two untagged local files (different levels) are measured by one sweep;
    while a render holds a slot the pass waits; a tagged file is skipped."""
    from soniqboom.api import stream
    monkeypatch.setattr(S, "_store", S.TrackStore())
    st = S.get_store()
    a = _sine(tmp_path / "a.wav", 0.5)
    b = _sine(tmp_path / "b.wav", 0.05)
    c = _sine(tmp_path / "c.wav", 0.5)
    st.upsert_tracks_batch([_track("a", str(a), "WAV"), _track("b", str(b), "WAV"),
                            _track("c", str(c), "WAV", replaygain_track_gain=-1.0)])
    monkeypatch.setattr(L, "_PASS_IDLE_POLL_S", 0.05)
    await stream._render_sem.acquire()                 # a render runs
    try:
        task = asyncio.ensure_future(L.native_pass(start_delay=0, once=True))
        await asyncio.sleep(0.4)
        assert not task.done() and st.get_track("a").get("loudness") is None
    finally:
        stream._render_sem.release()
    await asyncio.wait_for(task, 60)
    la, lb = st.get_track("a")["loudness"]["0"], st.get_track("b")["loudness"]["0"]
    assert abs((la[0] - lb[0]) - 20.0) < 0.3
    assert st.get_track("c").get("loudness") is None


@needs_ffmpeg
@pytest.mark.asyncio
async def test_the_players_next_native_track_is_measured_from_its_prewarm(tmp_path, monkeypatch):
    """``POST /prewarm?priority=next`` of an untagged local file queues its
    measurement (the prewarm itself skips it — native)."""
    from soniqboom.api import stream
    monkeypatch.setattr(S, "_store", S.TrackStore())
    st = S.get_store()
    f = _sine(tmp_path / "n.wav", 0.3)
    st.upsert_tracks_batch([_track("n", str(f), "WAV")])

    async def _auth(*a, **k):
        return None
    monkeypatch.setattr(stream, "_require_stream_auth", _auth)
    out = await stream.prewarm("n", subsong=None, priority="next", file_path=None, pw=None,
                               hover=False, target_format=None, max_bitrate_kbps=0,
                               sb_session=None, u=None, p=None, s=None, t=None, request=None)
    assert out["status"] == "skipped"
    await _drain()
    assert st.get_track("n")["loudness"]["0"][0] < -10


# ── What reads it ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_render_status_reports_the_tunes_loudness(monkeypatch):
    from soniqboom.api import stream
    monkeypatch.setattr(S, "_store", S.TrackStore())
    st = S.get_store()
    st.upsert_tracks_batch([_track("r", "/m/r.sid", subsongs=3,
                                   loudness={"0": [-14.0, 0.9], "2": [-21.5, 0.4]}),
                            _track("t", "/m/t.flac", "FLAC", replaygain_track_gain=-2.0,
                                   loudness={"0": [-14.0, 0.9]})])

    async def _auth(*a, **k):
        return None
    monkeypatch.setattr(stream, "_require_stream_auth", _auth)

    class _Req:
        def __init__(self, qs):
            self.query_params = qs
    a = await stream.render_status("r", subsong=None, request=_Req({}))
    b = await stream.render_status("r", subsong=2, request=_Req({"subsong": "2"}))
    c = await stream.render_status("r", subsong=1, request=_Req({"subsong": "1"}))
    d = await stream.render_status("t", subsong=None, request=_Req({}))
    assert a["loudness"] == [-14.0, 0.9] and b["loudness"] == [-21.5, 0.4]
    assert "loudness" not in c and "loudness" not in d          # (unmeasured; tagged)


def test_subsonic_replay_gain_from_the_measured_loudness(monkeypatch):
    """Tags first; else the default tune's measurement for the bare id and
    each tune's own for ``<id>~<n>`` (none: no track gain, never the
    default tune's); a float render above full scale is reported as its
    transcode carries it."""
    from soniqboom.api import subsonic as sub
    tagged = _track("t", "/m/t.flac", "FLAC", replaygain_track_gain=-2.5, replaygain_track_peak=0.8,
                    loudness={"0": [-9.0, 0.99]})
    sid = _track("s", "/m/s.sid", subsongs=3, loudness={"0": [-14.0, 0.9], "2": [-21.5, 0.4]})
    mod = _track("m", "/m/m.mod", "ProTracker", loudness={"0": [-12.0, 1.4, 1.25]})
    song = sub._track_to_song(tagged)
    assert song["replayGain"] == {"trackGain": -2.5, "trackPeak": 0.8}
    song = sub._track_to_song(sid)
    assert song["replayGain"] == {"trackGain": -4.0, "trackPeak": 0.9}
    t2 = sub._tune_child(song, sid, 2, None)
    t1 = sub._tune_child(song, sid, 1, None)
    assert t2["replayGain"] == {"trackGain": 3.5, "trackPeak": 0.4}
    assert t1["replayGain"] == {}
    assert song["replayGain"] == {"trackGain": -4.0, "trackPeak": 0.9}   # (not shared)
    g = sub._track_to_song(mod)["replayGain"]
    assert abs(g["trackGain"] - round(-6.0 + 20 * math.log10(1.25), 2)) < 1e-9
    assert abs(g["trackPeak"] - round(1.4 / 1.25, 6)) < 1e-9


def test_listing_rows_with_a_measurement_or_tune_slots_stay_on_the_fast_path():
    """``loudness`` (str → list of floats) and ``tune_slots`` (ints) are typed
    by the list endpoints' fast path: a well-formed row is served as it is
    (no per-row model), a malformed one is shaped by the model."""
    from soniqboom.api import tracks as T
    from soniqboom.models.track import TrackMeta
    row = TrackMeta(id="a", path="/m/a.kss", loudness={"0": [-14.2, 0.91], "2": [-9.0, 1.2, 1.1]},
                    loudness_source="analysed", tune_slots=[0, 2, 5]).model_dump()
    assert T.public_track(row) is row
    plain = TrackMeta(id="b", path="/m/b.flac").model_dump()
    assert T.public_track(plain) is plain
    bad = {**row, "loudness": {"0": [-14, 1]}}                    # ints: the model re-types
    assert T.public_track(bad)["loudness"] == {"0": [-14.0, 1.0]}
    assert T.public_track({**row, "tune_slots": [0, "x"]}) is None  # rejected, as the model does



@needs_ffmpeg
@pytest.mark.asyncio
async def test_other_render_settings_measure_again_the_same_ones_never(
        tmp_path, monkeypatch, cache_dir):
    """A SID rendered with another chip model (its key names ``m8580``) or a
    MIDI file with another soundfont is measured again and replaces the old
    measurement; the same settings again are not (a SID's power-on state)."""
    import soniqboom.api.stream  # noqa: F401
    monkeypatch.setattr(S, "_store", S.TrackStore())
    st = S.get_store()
    st.upsert_tracks_batch([_track("v", "/m/v.sid", subsongs=3)])
    assert L.variant_of("v__sub0__dur180") == "" == L.variant_of("v__sub2__dur90")
    assert L.variant_of("v__sub0__dur180__m8580") != ""
    await cc.store_cached("v__sub0__dur180", "sid", _sine(tmp_path / "a.wav", 0.5))
    await cc.store_cached("v__sub2__dur180", "sid", _sine(tmp_path / "b.wav", 0.5))
    await _drain()
    first = dict(st.get_track("v")["loudness"])
    await cc.store_cached("v__sub0__dur60", "sid", _sine(tmp_path / "c.wav", 0.1))   # (a length only)
    await _drain()
    assert st.get_track("v")["loudness"] == first
    await cc.store_cached("v__sub0__dur180__m8580", "sid", _sine(tmp_path / "d.wav", 0.125))
    await _drain()
    rec = st.get_track("v")
    assert set(rec["loudness"]) == {"0"} and rec["loudness"]["0"][0] < first["0"][0] - 11
    assert rec["loudness_v"] == L.variant_of("v__sub0__dur180__m8580")


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_render_cached_before_is_measured_when_it_is_read(tmp_path, monkeypatch, cache_dir):
    import soniqboom.api.stream  # noqa: F401
    assert cc.on_hit is L.note_render_stored
    monkeypatch.setattr(S, "_store", S.TrackStore())
    st = S.get_store()
    st.upsert_tracks_batch([_track("h", "/m/h.mod", "ProTracker")])
    monkeypatch.setattr(cc, "on_stored", None)         # (cached by a version without it)
    await cc.store_cached("h__sub0__r3", "tracker", _sine(tmp_path / "h.wav", 0.5))
    await _drain()
    assert st.get_track("h").get("loudness") is None
    assert await cc.get_cached("h__sub0__r3") is not None
    await _drain()
    assert st.get_track("h")["loudness"]["0"][0] > -10


@pytest.mark.asyncio
async def test_a_missing_file_is_not_tried_again_at_once(tmp_path, monkeypatch):
    from soniqboom.api import stream
    monkeypatch.setattr(S, "_store", S.TrackStore())
    st = S.get_store()
    st.upsert_tracks_batch([_track("gone", str(tmp_path / "gone.flac"), "FLAC")])
    assert await L._sweep_once(stream) == 1
    assert await L._sweep_once(stream) == 0                 # (retried after 6 h)
    until = L._FAILED[("gone", "0", (5, 1000))]
    import time as _t
    assert 5 * 3600 < until - _t.monotonic() <= 6 * 3600


def test_files_on_an_os_mounted_network_share_are_never_candidates(monkeypatch):
    import types
    from soniqboom.core import forksafe
    monkeypatch.setattr(L.os.path, "exists", lambda p: False if p == "/proc/self/mounts"
                        else __import__("os").path.lexists(p))
    monkeypatch.setattr(L.shutil, "which", lambda n: "/sbin/mount" if n == "mount" else None)
    out = (b"/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
           b"//guest@nas/Music on /Volumes/Music (smbfs, nodev, nosuid, mounted by me)\n"
           b"nas:/export on /mnt/my share (nfs, asynchronous)\n")
    monkeypatch.setattr(forksafe, "run", lambda *a, **k: types.SimpleNamespace(stdout=out))
    monkeypatch.setattr(L, "_NET_MOUNTS", None)        # (restored after the test)
    mounts = L.net_mounts(refresh=True)
    assert set(mounts) == {"/mnt/my share", "/Volumes/Music"}
    assert not L.native_candidate(_track("n", "/Volumes/Music/a/b.flac", "FLAC"))
    assert not L.native_candidate(_track("n", "/mnt/my share/b.flac", "FLAC"))
    assert L.native_candidate(_track("n", "/Volumes/Musical/b.flac", "FLAC"))
    assert L.native_candidate(_track("n", "/Users/me/Music/b.flac", "FLAC"))
