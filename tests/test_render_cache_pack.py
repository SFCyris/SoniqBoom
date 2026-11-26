# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Packed render-cache entries (WavPack, ``conversion_cache`` entry format v1).

A finished render is compressed losslessly in the background to
``<key>.sbwv``; its WAV stays as the entry's decoded copy until the cache
needs the room.  A hit on a pack without its decoded copy decodes it back —
whole for ``get_cached``, while it is written for a plain GET
(``stream._serve_packed_hit``).  Covers: byte-identical round trips for every
sample format renders use (and the others WAV allows), layouts that stay
WAV, the accounting (bytes on disk), eviction (decoded copies before
renders), one decode for concurrent hits, damaged and other-version packs (a
miss, dropped), start-up adoption (decoded copy vs a later render), a store
over a packed entry, a pack stopped for a render, the streaming answer with
Range, the peak sidecar of a float render without its WAV, clear / sweeps,
and first-byte / hit-time budgets.  Real ffmpeg (its wavpack codec); the
audio is synthesised here."""
from __future__ import annotations

import array
import asyncio
import hashlib
import math
import os
import struct
import time
from collections import OrderedDict
from pathlib import Path

import pytest

from soniqboom.config import settings
from soniqboom.core import conversion_cache as cc

pytestmark = pytest.mark.skipif(not cc.wavpack_available(),
                                reason="this ffmpeg has no wavpack codec")


@pytest.fixture(autouse=True)
def _fresh_cache_state(monkeypatch):
    """Every test starts with an empty in-memory index (the cache dir itself
    is per-test: conftest)."""
    monkeypatch.setattr(cc, "_meta", {})
    monkeypatch.setattr(cc, "_lru", OrderedDict())
    monkeypatch.setattr(cc, "_total_bytes", 0)
    monkeypatch.setattr(cc, "_pin_refs", {})
    monkeypatch.setattr(cc, "_pending_purge", {})
    monkeypatch.setattr(cc, "_pack_queue", OrderedDict())
    monkeypatch.setattr(cc, "_unpackable", set())
    monkeypatch.setattr(cc, "_unpacks", {})
    monkeypatch.setattr(cc, "_judged", set())
    monkeypatch.setattr(cc, "_drops_pending", {})
    monkeypatch.setattr(cc, "renders_busy", None)
    # (packs run at normal priority here — on a loaded host the background
    # band can take minutes; ``test_packs_run_at_the_lowest_priority``
    # covers the prefix)
    monkeypatch.setattr(cc, "_BG_PREFIX", [])
    monkeypatch.setattr(settings, "conversion_cache_max_bytes", 4 * 1024 ** 3)
    monkeypatch.setattr(settings, "conversion_cache_pack", True)


# ── WAV builders ────────────────────────────────────────────────────────────

def _wav(raw: str, channels: int, rate: int, frames: int, *, specials: bool = False) -> bytes:
    """A canonical 44-byte-header WAV of a few tones (sample format ``raw``,
    as ``conversion_cache._RAW_FORMATS`` names them)."""
    nbytes = {"u8": 1, "s16le": 2, "s24le": 3, "s32le": 4, "f32le": 4}[raw]
    tag = 3 if raw == "f32le" else 1
    vals = []
    for i in range(frames):
        for c in range(channels):
            vals.append(0.6 * math.sin(2 * math.pi * (220 + 110 * c) * i / rate)
                        + 0.25 * math.sin(2 * math.pi * 1733 * i / rate))
    if raw == "f32le":
        a = array.array("f", vals)
        if specials:                                # values a float render can hold
            a[0:8] = array.array("f", [1.75, -1.5, 0.0, -0.0, 1e-42, -1e-40,
                                       float("inf"), float("-inf")])
        pcm = a.tobytes()
    elif raw == "u8":
        pcm = bytes(int(128 + 120 * v) & 0xFF for v in vals)
    elif raw == "s16le":
        pcm = array.array("h", (int(30000 * v) for v in vals)).tobytes()
    elif raw == "s24le":
        pcm = b"".join(int(8_000_000 * v).to_bytes(3, "little", signed=True) for v in vals)
    else:
        pcm = array.array("i", (int(2_000_000_000 * v) for v in vals)).tobytes()
    align = channels * nbytes
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
           + b"fmt " + struct.pack("<IHHIIHH", 16, tag, channels, rate, rate * align,
                                   align, nbytes * 8)
           + b"data" + struct.pack("<I", len(pcm)))
    return hdr + pcm


async def _stored(tmp_path: Path, key: str, data: bytes, fmt: str = "tracker") -> Path:
    src = tmp_path / f"{key}.render.wav"
    src.write_bytes(data)
    return await cc.store_cached(key, fmt, src)


def _drop_hot(key: str) -> None:
    with cc._state_lock:
        p = cc._drop_hot_locked(cc._meta[key])
    assert p is not None
    p.unlink()


class _Req:
    method = "GET"

    def __init__(self, rng=None):
        self.headers = {"range": rng} if rng else {}


async def _body(resp) -> bytes:
    return b"".join([c async for c in resp.body_iterator])


# ── round trips ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,channels,rate,specials", [
    ("s16le", 2, 44100, False),           # most renders
    ("s16le", 1, 44100, False),           # C64 SID
    ("f32le", 2, 48000, True),            # trackers (libopenmpt float, past full scale)
    ("s24le", 2, 48000, False),
    ("s32le", 1, 96000, False),
    ("u8", 1, 22050, False),
])
async def test_a_pack_decodes_to_the_same_file_byte_for_byte(tmp_path, raw, channels, rate,
                                                              specials):
    data = _wav(raw, channels, rate, rate // 2, specials=specials)
    key = f"rt{raw}{channels}__sub0"
    dest = await _stored(tmp_path, key, data)
    mtime = dest.stat().st_mtime_ns
    assert key in cc._pack_queue                     # queued for the packer at store
    assert await cc.pack_entry(key)
    pack = dest.with_suffix(cc.PACK_SUFFIX)
    info = cc.read_pack_info(pack)
    assert info is not None and info.raw == raw and info.channels == channels
    assert info.md5 == hashlib.md5(data[44:]).hexdigest()   # the audio as read, decoded again
    assert pack.stat().st_size < len(data) or raw == "u8"
    assert dest.read_bytes() == data                 # the WAV stays (decoded copy)
    _drop_hot(key)
    assert not dest.exists()
    got = await cc.get_cached(key)
    assert got == dest and got.read_bytes() == data
    assert got.stat().st_mtime_ns == mtime           # the original's mtime, given back


@pytest.mark.parametrize("variant", ["trailer", "streaming", "adpcm", "float64"])
async def test_layouts_a_pack_cant_restore_bit_for_bit_stay_wav(tmp_path, variant):
    data = bytearray(_wav("s16le", 2, 44100, 4410))
    if variant == "trailer":
        data += b"LIST" + struct.pack("<I", 4) + b"INFO"
    elif variant == "streaming":
        data[40:44] = b"\xff\xff\xff\xff"
    elif variant == "adpcm":
        data[20:22] = struct.pack("<H", 2)
    else:
        data[20:22] = struct.pack("<H", 3)          # 64-bit float: not a WavPack format
        data[34:36] = struct.pack("<H", 64)
        data[32:34] = struct.pack("<H", 16)
    key = f"un{variant}__sub0"
    dest = await _stored(tmp_path, key, bytes(data))
    assert not await cc.pack_entry(key)
    assert key in cc._unpackable and key not in cc._pack_queue
    assert not dest.with_suffix(cc.PACK_SUFFIX).exists()
    assert dest.read_bytes() == bytes(data)
    assert not list(dest.parent.glob(".*"))         # no temp left


# ── accounting and eviction ─────────────────────────────────────────────────

async def test_the_budget_counts_the_bytes_on_disk(tmp_path):
    from soniqboom.api import stream
    data = _wav("s16le", 2, 44100, 44100)
    key = "acct__sub0"
    dest = await _stored(tmp_path, key, data)
    assert cc._total_bytes == len(data)
    assert await cc.pack_entry(key)
    pb = dest.with_suffix(cc.PACK_SUFFIX).stat().st_size
    assert cc._total_bytes == len(data) + pb == cc._meta[key]["size_bytes"]
    st = await cc.cache_stats()
    assert (st["packed_count"], st["packed_bytes"], st["decoded_count"]) == (1, pb, 1)
    assert st["wav_bytes"] == len(data) and st["by_type_bytes"]["tracker"] == len(data) + pb
    _drop_hot(key)
    assert cc._total_bytes == pb
    # what a HEAD / render-status reads stays the WAV's size
    assert cc.cached_wav_size(key) == len(data) == stream._peek_cached_size(key)
    await cc.get_cached(key)
    assert cc._total_bytes == len(data) + pb


async def test_eviction_drops_decoded_copies_before_any_render(tmp_path, monkeypatch):
    data = _wav("s16le", 2, 44100, 44100)            # 176 444 bytes
    keys = [f"ev{i}__sub0" for i in range(5)]
    for k in keys:
        await _stored(tmp_path, k, data)
        assert await cc.pack_entry(k)
    pb = cc._meta[keys[0]]["packed_bytes"]
    old = time.time() - 1000
    with cc._state_lock:
        for k in keys:
            cc._lru[k] = old                         # none used lately
    cc.pin(keys[1])
    # room for every pack and two decoded copies; the newest copy is within
    # the protected floor (10 % of the budget)
    budget = 5 * pb + 2 * len(data) + 1000
    monkeypatch.setattr(settings, "conversion_cache_max_bytes", budget)
    monkeypatch.setattr(cc, "_HOT_FLOOR_FRACTION", (len(data) + 10) / budget)
    cc._maybe_evict()
    assert set(cc._meta) == set(keys)                # no render removed
    hot = {k for k in keys if cc._hot(cc._meta[k])}
    assert hot == {keys[1], keys[4]}                 # the pinned one, the newest one
    for k in keys:
        assert Path(cc._meta[k]["path"]).exists() == (k in hot)
    assert cc._total_bytes <= budget
    # tighter still: renders go, oldest unpinned first (every decoded copy
    # that may go is gone already)
    monkeypatch.setattr(settings, "conversion_cache_max_bytes", 3 * pb + len(data) + 100)
    cc._maybe_evict()
    assert set(cc._meta) == {keys[1], keys[3], keys[4]}
    assert not any(Path(cc._meta[k]["path"]).exists() for k in (keys[3], keys[4]))
    assert cc._total_bytes == 3 * pb + len(data)
    cc.unpin(keys[1])


async def test_a_copy_used_lately_is_not_dropped(tmp_path, monkeypatch):
    data = _wav("s16le", 2, 44100, 44100)
    for k in ("rec0__sub0", "rec1__sub0"):
        await _stored(tmp_path, k, data)
        assert await cc.pack_entry(k)
    monkeypatch.setattr(cc, "_HOT_FLOOR_FRACTION", 0.0)
    monkeypatch.setattr(settings, "conversion_cache_max_bytes", 1)
    with cc._state_lock:
        cc._lru["rec0__sub0"] = time.time() - 1000
    with cc._state_lock:
        dropped = cc._drop_hot_copies_locked(1, None)
    assert [k for k, _p in dropped] == ["rec0__sub0"]    # rec1 was just stored
    cc._unlink_dropped(dropped)
    assert not cc._drops_pending


# ── decoding on a hit ───────────────────────────────────────────────────────

async def test_concurrent_hits_share_one_decode(tmp_path, monkeypatch):
    from soniqboom.core import forksafe
    data = _wav("f32le", 2, 48000, 48000)
    key = "conc__sub0"
    dest = await _stored(tmp_path, key, data)
    assert await cc.pack_entry(key)
    _drop_hot(key)
    spawned = []
    real = forksafe.spawn

    async def counting(*a, **kw):
        spawned.append(a)
        return await real(*a, **kw)
    monkeypatch.setattr(forksafe, "spawn", counting)
    got = await asyncio.gather(*[cc.get_cached(key) for _ in range(6)])
    assert got == [dest] * 6 and dest.read_bytes() == data
    assert len(spawned) == 1


async def test_is_cache_ready_never_decodes(tmp_path):
    key = "ready__sub0"
    await _stored(tmp_path, key, _wav("s16le", 2, 44100, 4410))
    assert await cc.pack_entry(key)
    _drop_hot(key)
    assert await cc.is_cache_ready(key)
    assert not cc._unpacks and not cc._hot(cc._meta[key])


async def test_open_cached_audio_decodes_and_pins(tmp_path):
    data = _wav("s16le", 1, 44100, 4410)
    key = "open__sub0"
    await _stored(tmp_path, key, data)
    assert await cc.pack_entry(key)
    _drop_hot(key)
    async with cc.open_cached_audio(key) as wav:
        assert wav.read_bytes() == data
        assert cc._pin_refs.get(key) == 1
    assert cc._pin_refs.get(key) is None
    async with cc.open_cached_audio("nothing__sub0") as wav:
        assert wav is None


async def test_a_shorter_sid_render_kept_packed_is_decoded_for_its_play(tmp_path):
    data = _wav("s16le", 1, 44100, 44100)
    key = cc._cache_key("sidt", "sid", 0, duration=60)
    await _stored(tmp_path, key, data, fmt="sid")
    assert await cc.pack_entry(key)
    _drop_hot(key)
    got = await cc.find_shorter_sid_entry("sidt", 0, 180)
    assert got is not None and got[1] == 60 and got[0].read_bytes() == data


@pytest.mark.parametrize("damage", ["audio", "header", "version"])
async def test_a_damaged_or_other_version_pack_is_a_miss_and_is_dropped(tmp_path, damage):
    data = _wav("s16le", 2, 44100, 44100)
    key = f"dmg{damage}__sub0"
    dest = await _stored(tmp_path, key, data)
    assert await cc.pack_entry(key)
    _drop_hot(key)
    pack = dest.with_suffix(cc.PACK_SUFFIX)
    b = bytearray(pack.read_bytes())
    if damage == "audio":
        mid = cc._PACK_HEAD_LEN + 44 + (len(b) - cc._PACK_HEAD_LEN - 44) // 2
        for i in range(mid, mid + 32):
            b[i] ^= 0x5A
    elif damage == "header":
        b[20] ^= 0x01                                 # the header CRC no longer matches
    else:
        # a pack another version wrote (a newer layout): its own valid CRC
        b[4:6] = struct.pack("<H", cc.PACK_VERSION + 1)
        fields = bytes(b[:cc._PACK_HEAD.size])
        b[cc._PACK_HEAD.size:cc._PACK_HEAD_LEN] = (
            __import__("zlib").crc32(fields).to_bytes(4, "little"))
    pack.write_bytes(bytes(b))
    assert await cc.get_cached(key) is None          # a miss: the caller renders again
    assert key not in cc._meta
    assert not pack.exists() and not dest.exists()
    assert not list(dest.parent.glob(".*"))


# ── stores, packs and readers racing ────────────────────────────────────────

async def test_a_store_over_a_packed_entry_replaces_it(tmp_path):
    key = "repl__sub0"
    dest = await _stored(tmp_path, key, _wav("s16le", 2, 44100, 4410))
    assert await cc.pack_entry(key)
    new = _wav("s16le", 2, 44100, 8820)
    await _stored(tmp_path, key, new)
    assert not dest.with_suffix(cc.PACK_SUFFIX).exists()
    e = cc._meta[key]
    assert not e.get("packed") and e["size_bytes"] == len(new) == cc._total_bytes
    assert dest.read_bytes() == new and key in cc._pack_queue


async def test_a_pack_of_a_render_being_replaced_is_discarded(tmp_path, monkeypatch):
    key = "race__sub0"
    data = _wav("s16le", 2, 44100, 44100)
    dest = await _stored(tmp_path, key, data)
    real = cc._finish_pack_file

    def replaced(tmp, header):                       # a store starts as the pack ends
        with cc._state_lock:
            cc._meta[key]["stale"] = True
        return real(tmp, header)
    monkeypatch.setattr(cc, "_finish_pack_file", replaced)
    assert not await cc.pack_entry(key)
    assert not dest.with_suffix(cc.PACK_SUFFIX).exists()
    assert not list(dest.parent.glob(".*"))


async def test_a_pack_stopped_for_a_render_leaves_the_entry_as_it_was(tmp_path, monkeypatch):
    key = "stop__sub0"
    data = _wav("f32le", 2, 48000, 96000)
    dest = await _stored(tmp_path, key, data)
    assert not await cc.pack_entry(key, preempt=lambda: True)
    # …and one stopped part-way through its encode
    calls = []

    def later() -> bool:
        calls.append(1)
        return len(calls) > 1
    monkeypatch.setattr(cc, "_PACK_LEVEL", 3)        # (slow enough to be caught running)
    assert not await cc.pack_entry(key, preempt=later)
    e = cc._meta[key]
    assert not e.get("packed") and cc._hot(e) and e["size_bytes"] == len(data)
    assert dest.read_bytes() == data and key not in cc._unpackable
    assert not dest.with_suffix(cc.PACK_SUFFIX).exists()
    assert not list(dest.parent.glob(".*"))


async def test_a_pinned_purge_removes_the_pack_at_unpin(tmp_path):
    key = "pp__sub0"
    dest = await _stored(tmp_path, key, _wav("s16le", 2, 44100, 4410))
    assert await cc.pack_entry(key)
    cc.pin(key)
    cc._purge_entry(key)
    assert dest.exists() and dest.with_suffix(cc.PACK_SUFFIX).exists()
    cc.unpin(key)
    assert not dest.exists() and not dest.with_suffix(cc.PACK_SUFFIX).exists()


async def test_a_silent_render_found_at_pack_time_is_removed(tmp_path):
    key = "quiet__sub0"
    hdr = _wav("s16le", 2, 44100, 44100)[:44]
    dest = await _stored(tmp_path, key, hdr + b"\0" * (44100 * 4))
    assert not await cc.pack_entry(key)
    assert key not in cc._meta and not dest.exists()


# ── the web stream's answer from a decode ───────────────────────────────────

async def test_a_get_is_answered_from_the_decode_while_it_is_written(tmp_path):
    from soniqboom.api import stream
    data = _wav("f32le", 2, 48000, 48000 * 4)        # 1.5 MB
    key = "strm__sub0"
    dest = await _stored(tmp_path, key, data)
    assert await cc.pack_entry(key)
    _drop_hot(key)
    resp = await stream._serve_packed_hit(_Req(), key, headers={"X-Rendered": "t"},
                                          background=None)
    assert resp is not None and resp.status_code == 200
    assert resp.headers["content-length"] == str(len(data))
    assert resp.headers["x-cache-store"] == "packed" and resp.headers["x-cache"] == "hit"
    assert await _body(resp) == data
    assert cc._pin_refs.get(key) == 1                # held while it is answered
    await resp.background()
    assert cc._pin_refs.get(key) is None             # released by the response's cleanup
    for _ in range(100):
        if not cc._unpacks:
            break
        await asyncio.sleep(0.02)
    assert cc._hot(cc._meta[key]) and dest.read_bytes() == data
    # decoded now: the caller serves the file as before
    assert await stream._serve_packed_hit(_Req(), key, headers={}, background=None) is None


async def test_a_range_is_answered_from_the_decode(tmp_path):
    from soniqboom.api import stream
    data = _wav("s16le", 2, 44100, 44100 * 6)
    key = "rng__sub0"
    await _stored(tmp_path, key, data)
    assert await cc.pack_entry(key)
    _drop_hot(key)
    lo, hi = 500_001, 500_000 + 65_536
    resp = await stream._serve_packed_hit(_Req(f"bytes={lo}-{hi}"), key, headers={},
                                          background=None)
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes {lo}-{hi}/{len(data)}"
    assert await _body(resp) == data[lo:hi + 1]
    await resp.background()
    # another codec / a time offset needs the whole file: not answered here
    xf = stream._Xform("mp3", 0, 0.0)
    for _ in range(100):
        if not cc._unpacks:
            break
        await asyncio.sleep(0.02)
    _drop_hot(key)
    assert await stream._serve_packed_hit(_Req(), key, headers={}, background=None,
                                          xf=xf) is None


async def test_the_peak_of_a_packed_float_render_is_known_without_its_wav(tmp_path):
    from soniqboom.api import stream
    data = bytearray(_wav("f32le", 2, 48000, 4800))
    data[44:48] = struct.pack("<f", 1.5)             # an overloaded render
    key = "peak__sub0"
    dest = await _stored(tmp_path, key, bytes(data))
    assert await cc.pack_entry(key)                  # measures <wav>.peak first
    assert dest.with_suffix(".peak").exists()
    _drop_hot(key)
    with stream._PEAKS_LOCK:
        stream._PEAKS.clear()
    assert stream.render_peak(dest) == pytest.approx(1.5)
    assert stream.render_gain(dest) == pytest.approx(1 / 1.5)


# ── start-up, clear, sweeps ─────────────────────────────────────────────────

async def test_startup_adopts_packs_and_tells_a_decoded_copy_from_a_later_render(
        tmp_path, monkeypatch):
    data = _wav("s16le", 2, 44100, 44100)
    keys = {"only": "aa1__sub0__r3__amauto", "copy": "aa2__sub0__r3__amauto", "later": "aa3__sub0__r3__amauto",
            "plain": "aa4__sub0__r3__amauto"}
    for k in keys.values():
        await _stored(tmp_path, k, data)
    for name in ("only", "copy", "later"):
        assert await cc.pack_entry(keys[name])
    wav = {n: Path(cc._meta[k]["path"]) for n, k in keys.items()}
    wav["only"].unlink()                                         # pack only
    later = _wav("s16le", 2, 44100, 22050)
    wav["later"].write_bytes(later)                              # another version re-rendered it
    t = wav["later"].with_suffix(cc.PACK_SUFFIX).stat().st_mtime + 5
    os.utime(wav["later"], (t, t))
    (wav["plain"].parent / ".x.wav.1.ab.unpacking").write_bytes(b"x")
    (wav["plain"].parent / ".x.sbwv.1.ab.packing").write_bytes(b"x")
    # a fresh process
    monkeypatch.setattr(cc, "_meta", {})
    monkeypatch.setattr(cc, "_lru", OrderedDict())
    monkeypatch.setattr(cc, "_total_bytes", 0)
    monkeypatch.setattr(cc, "_pack_queue", OrderedDict())
    assert cc.warmup_from_disk() == 4
    m = cc._meta
    assert m[keys["only"]]["packed"] and not cc._hot(m[keys["only"]])
    assert m[keys["only"]]["wav_bytes"] is None                  # header not read yet
    assert m[keys["copy"]]["packed"] and cc._hot(m[keys["copy"]])
    assert not m[keys["later"]].get("packed")
    assert not wav["later"].with_suffix(cc.PACK_SUFFIX).exists()  # the later render won
    assert set(cc._pack_queue) == {keys["later"], keys["plain"]}
    assert not list(wav["plain"].parent.glob(".*"))               # unfinished writes swept
    assert cc._total_bytes == sum(e["size_bytes"] for e in m.values())
    assert cc._check_adopted_packs() == 0
    assert m[keys["only"]]["wav_bytes"] == len(data)
    got = await cc.get_cached(keys["only"])
    assert got.read_bytes() == data and await cc.get_cached(keys["later"]) == wav["later"]


async def test_startup_drops_a_pack_its_decoded_copy_disagrees_with(tmp_path, monkeypatch):
    data = _wav("s16le", 2, 44100, 44100)
    key = "bb1__sub0__r3__amauto"                            # (the current tracker revision)
    dest = await _stored(tmp_path, key, data)
    assert await cc.pack_entry(key)
    mt = dest.stat().st_mtime_ns
    other = _wav("s16le", 2, 44100, 40000)
    dest.write_bytes(other)                          # same age, another (whole) render
    os.utime(dest, ns=(mt, mt))
    monkeypatch.setattr(cc, "_meta", {})
    monkeypatch.setattr(cc, "_lru", OrderedDict())
    monkeypatch.setattr(cc, "_total_bytes", 0)
    monkeypatch.setattr(cc, "_pack_queue", OrderedDict())
    cc.warmup_from_disk()                            # (pairs: the header is read here)
    assert cc._check_adopted_packs() == 0
    assert not cc._meta[key].get("packed") and not dest.with_suffix(cc.PACK_SUFFIX).exists()
    assert cc._total_bytes == len(other) and key in cc._pack_queue


async def test_clear_removes_packs_and_peak_sidecars(tmp_path):
    key = "clr__sub0"
    dest = await _stored(tmp_path, key, _wav("f32le", 2, 48000, 4800))
    assert await cc.pack_entry(key)
    assert dest.with_suffix(".peak").exists()
    out = await cc.clear_cache()
    assert out["deleted_files"] >= 3
    assert not list(dest.parent.iterdir())


def test_a_revision_sweep_removes_old_packs(tmp_path):
    base = tmp_path / "conv"
    d = base / "tracker" / "xx"
    d.mkdir(parents=True)
    old = d / f"xx1__sub0__r1{cc.PACK_SUFFIX}"
    cur = d / f"xx2__sub0__r{cc._RENDER_REVS['tracker']}{cc.PACK_SUFFIX}"
    old.write_bytes(b"x")
    cur.write_bytes(b"x")
    cc._sweep_unrevised_renders(base)
    assert not old.exists() and cur.exists()


async def test_code_before_packs_never_reads_a_pack_as_a_wav(tmp_path):
    """Older versions adopt ``*.wav`` (and sweep ``.wav`` / ``.vu``) only: a
    pack-only entry is invisible to them — a miss, rendered again — and its
    file isn't a RIFF file a WAV reader could take for audio.  (The real
    2.2.0 code against a packed cache was run in the handover's scratch
    check: it adopted the WAVs, missed the pack-only entry, re-rendered it.)"""
    key = "old__sub0"
    dest = await _stored(tmp_path, key, _wav("s16le", 2, 44100, 4410))
    assert await cc.pack_entry(key)
    _drop_hot(key)
    base = cc.get_conversion_cache_dir()
    assert [f.stem for f in base.rglob("*.wav")] == []           # what 2.2.0 adopts
    assert [f for f in base.rglob("*") if f.suffix in (".wav", ".vu")] == []
    assert dest.with_suffix(cc.PACK_SUFFIX).read_bytes()[:4] == b"SBWV"


async def test_packs_run_at_the_lowest_priority(tmp_path, monkeypatch):
    import sys as _sys
    monkeypatch.setattr(cc, "_BG_PREFIX", None)
    pre = cc._background_prefix()
    if _sys.platform == "darwin":
        assert pre == ["/usr/sbin/taskpolicy", "-b"]
    else:
        assert any(p.endswith("nice") for p in pre) or pre == []
    key = "prio__sub0"
    data = _wav("s16le", 2, 44100, 4410)
    dest = await _stored(tmp_path, key, data)
    assert await cc.pack_entry(key)
    _drop_hot(key)
    assert (await cc.get_cached(key)).read_bytes() == data


# ── budgets ─────────────────────────────────────────────────────────────────

async def test_budget_a_hit_on_a_decoded_entry_costs_what_a_wav_hit_does(tmp_path):
    data = _wav("s16le", 2, 44100, 4410)
    await _stored(tmp_path, "plain__sub0", data)
    await _stored(tmp_path, "hot__sub0", data)
    assert await cc.pack_entry("hot__sub0")

    async def median(key):
        ts = []
        for _ in range(200):
            t = time.perf_counter()
            assert await cc.get_cached(key) is not None
            ts.append(time.perf_counter() - t)
        ts.sort()
        return ts[len(ts) // 2]
    plain, hot = await median("plain__sub0"), await median("hot__sub0")
    assert hot < 0.005 and hot < plain * 3 + 0.0005, (plain, hot)


async def test_budget_first_bytes_of_a_packed_hit(tmp_path):
    from soniqboom.api import stream
    data = _wav("s16le", 2, 44100, 44100 * 30)       # 30 s, 5.3 MB
    key = "ttfb__sub0"
    await _stored(tmp_path, key, data)
    assert await cc.pack_entry(key)
    best = 9.0
    for _ in range(3):
        _drop_hot(key)
        t = time.perf_counter()
        resp = await stream._serve_packed_hit(_Req("bytes=0-65535"), key, headers={},
                                              background=None)
        it = resp.body_iterator
        first = await it.__anext__()
        best = min(best, time.perf_counter() - t)
        rest = b"".join([c async for c in it])
        assert first + rest == data[:65536]
        await resp.background()
        for _ in range(200):
            if not cc._unpacks:
                break
            await asyncio.sleep(0.01)
    # measured ~35 ms on the dev box (an ffmpeg start); a whole decode of a
    # 3-minute render is 80-130 ms
    assert best < 0.5, best


# ── the QA round's regressions ──────────────────────────────────────────────

async def test_startup_never_drops_a_wav_before_its_pack_is_checked(tmp_path, monkeypatch):
    """A WAV beside a pack of another version (written by a newer release)
    stays the entry at start-up — the pack's header is read before the WAV
    could be dropped on the strength of it — and a pair whose header could
    not be read yet keeps its WAV through any eviction."""
    import zlib
    data = _wav("s16le", 2, 44100, 44100)
    key = "uc1__sub0__r3__amauto"
    dest = await _stored(tmp_path, key, data)
    assert await cc.pack_entry(key)
    pack = dest.with_suffix(cc.PACK_SUFFIX)
    b = bytearray(pack.read_bytes())
    b[4:6] = struct.pack("<H", cc.PACK_VERSION + 1)
    b[cc._PACK_HEAD.size:cc._PACK_HEAD_LEN] = zlib.crc32(bytes(b[:cc._PACK_HEAD.size])).to_bytes(4, "little")
    pack.write_bytes(bytes(b))
    monkeypatch.setattr(cc, "_meta", {})
    monkeypatch.setattr(cc, "_lru", OrderedDict())
    monkeypatch.setattr(cc, "_total_bytes", 0)
    monkeypatch.setattr(cc, "_pack_queue", OrderedDict())
    cc.warmup_from_disk()
    e = cc._meta[key]
    assert not e.get("packed") and not pack.exists() and dest.read_bytes() == data
    assert cc._total_bytes == len(data) and key in cc._pack_queue
    # an unchecked pair is never a decoded copy eviction may drop
    assert await cc.pack_entry(key)
    with cc._state_lock:
        cc._meta[key]["unchecked"] = True
        cc._lru[key] = time.time() - 1000
    monkeypatch.setattr(cc, "_HOT_FLOOR_FRACTION", 0.0)
    with cc._state_lock:
        assert cc._drop_hot_copies_locked(1, None) == []


async def test_a_late_unlink_never_removes_a_fresh_decode(tmp_path):
    data = _wav("s16le", 2, 44100, 44100 * 3)
    key = "late__sub0"
    dest = await _stored(tmp_path, key, data)
    assert await cc.pack_entry(key)
    with cc._state_lock:                                 # eviction drops the copy…
        p = cc._drop_hot_locked(cc._meta[key], key)
    got = asyncio.ensure_future(cc.get_cached(key))     # …a hit decodes it again…
    await asyncio.sleep(0.4)
    await asyncio.to_thread(cc._unlink_dropped, [(key, p)])   # …then the unlink lands
    assert await got == dest
    assert dest.read_bytes() == data and cc._hot(cc._meta[key])


async def test_a_decoding_hit_is_pinned_and_recent(tmp_path, monkeypatch):
    data = _wav("f32le", 2, 48000, 48000 * 3)
    for k in ("pin0__sub0", "pin1__sub0"):
        await _stored(tmp_path, k, data)
        assert await cc.pack_entry(k)
        _drop_hot(k)
    with cc._state_lock:
        cc._lru.move_to_end("pin0__sub0", last=False)    # the decoding one is oldest
        cc._lru["pin0__sub0"] = time.time() - 1000
    got = asyncio.ensure_future(cc.get_cached("pin0__sub0"))
    for _ in range(100):
        if cc._pin_refs.get("pin0__sub0"):
            break
        await asyncio.sleep(0.005)
    assert cc._pin_refs.get("pin0__sub0") == 1
    assert next(reversed(cc._lru)) == "pin0__sub0"      # moved to the recent end
    pb0 = cc._meta["pin0__sub0"]["packed_bytes"]
    monkeypatch.setattr(settings, "conversion_cache_max_bytes", pb0 + 100)
    cc._maybe_evict()                                    # an eviction during the decode
    path = await got
    assert path is not None and path.read_bytes() == data
    assert "pin1__sub0" not in cc._meta and cc._pin_refs.get("pin0__sub0") is None


async def test_migration_drops_old_decoded_copies_before_renders(tmp_path, monkeypatch):
    """Start-up migration packs oldest first: their decoded copies go before
    any render is evicted (the floor protects the newest entries, packed or
    not)."""
    data = _wav("s16le", 2, 44100, 44100)
    keys = [f"mig{i}__sub0" for i in range(6)]
    for k in keys:
        await _stored(tmp_path, k, data)
    with cc._state_lock:
        for i, k in enumerate(keys):
            cc._lru[k] = time.time() - 10_000 + i
            cc._lru.move_to_end(k)
    for k in keys[:3]:                                   # the packer's order: oldest first
        assert await cc.pack_entry(k)
    pb = sum(cc._meta[k]["packed_bytes"] for k in keys[:3])
    budget = 6 * len(data) + pb - 2 * len(data)          # two WAVs too many
    monkeypatch.setattr(settings, "conversion_cache_max_bytes", budget)
    monkeypatch.setattr(cc, "_HOT_FLOOR_FRACTION", 0.34)  # the newest two renders' worth
    cc._maybe_evict()
    assert set(cc._meta) == set(keys)                    # no render lost
    assert [cc._hot(cc._meta[k]) for k in keys] == [False, False, True, True, True, True]


async def test_no_wavpack_codec_backs_off(tmp_path, monkeypatch):
    key = "nocodec__sub0"
    await _stored(tmp_path, key, _wav("s16le", 2, 44100, 4410))
    monkeypatch.setattr(cc, "wavpack_available", lambda: False)
    assert await cc._pack(key, None) == "nocodec"
    assert not cc._meta[key].get("packed")


async def test_a_pack_that_times_out_is_not_tried_again(tmp_path, monkeypatch):
    key = "slow__sub0"
    dest = await _stored(tmp_path, key, _wav("f32le", 2, 48000, 48000 * 4))
    monkeypatch.setattr(cc, "_PACK_TIMEOUT_S", 0.0)
    assert await cc._pack(key, None) == "skip"
    assert key in cc._unpackable and not dest.with_suffix(cc.PACK_SUFFIX).exists()
    assert not list(dest.parent.glob(".*"))


async def test_a_read_error_keeps_the_pack(tmp_path, monkeypatch):
    """A share that didn't answer is not a damaged pack: the hit is a miss
    now, the pack and the entry stay for the next one."""
    data = _wav("s16le", 2, 44100, 44100)
    key = "eio__sub0"
    dest = await _stored(tmp_path, key, data)
    assert await cc.pack_entry(key)
    _drop_hot(key)
    real = cc.read_pack_info

    def flaky(path):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(cc, "read_pack_info", flaky)
    assert await cc.get_cached(key) is None
    assert key in cc._meta and dest.with_suffix(cc.PACK_SUFFIX).exists()
    monkeypatch.setattr(cc, "read_pack_info", real)
    assert (await cc.get_cached(key)).read_bytes() == data


async def test_a_purge_of_an_entry_spares_the_render_that_replaced_it(tmp_path):
    key = "rep__sub0"
    await _stored(tmp_path, key, _wav("s16le", 2, 44100, 4410))
    old = cc._meta[key]
    await _stored(tmp_path, key, _wav("s16le", 2, 44100, 8820))
    cc._purge_entry(key, old)
    assert key in cc._meta


async def test_a_failed_store_leaves_the_old_entry_usable(tmp_path):
    key = "fail__sub0"
    data = _wav("s16le", 2, 44100, 4410)
    await _stored(tmp_path, key, data)
    with pytest.raises(OSError):
        await cc.store_cached(key, "tracker", tmp_path / "missing.wav")
    assert not cc._meta[key].get("stale")
    assert await cc.pack_entry(key)


async def test_the_packed_answer_carries_the_decoded_files_validators(tmp_path):
    from soniqboom.api import stream
    data = _wav("s16le", 2, 44100, 44100)
    key = "etag__sub0"
    dest = await _stored(tmp_path, key, data)
    assert await cc.pack_entry(key)
    _drop_hot(key)
    resp = await stream._serve_packed_hit(_Req(), key, headers={}, background=None)
    body = await _body(resp)
    await resp.background()
    for _ in range(100):
        if cc._hot(cc._meta[key]):
            break
        await asyncio.sleep(0.02)
    etag, lm = stream._file_validators(dest.stat())
    assert (resp.headers["etag"], resp.headers["last-modified"]) == (etag, lm)
    assert body == data


async def test_a_decode_failing_part_way_ends_the_answer_short(tmp_path):
    from soniqboom.api import stream
    data = _wav("s16le", 2, 44100, 44100 * 20)
    key = "midfail__sub0"
    dest = await _stored(tmp_path, key, data)
    assert await cc.pack_entry(key)
    _drop_hot(key)
    pack = dest.with_suffix(cc.PACK_SUFFIX)
    b = bytearray(pack.read_bytes())
    at = len(b) * 3 // 4
    for i in range(at, at + 64):
        b[i] ^= 0x5A
    pack.write_bytes(bytes(b))
    resp = await stream._serve_packed_hit(_Req(), key, headers={}, background=None)
    assert resp.headers["content-length"] == str(len(data))
    body = await _body(resp)
    await resp.background()
    assert 0 < len(body) < len(data)                    # short, never padded with silence
    assert body == data[:len(body)]
    assert key not in cc._meta and not pack.exists()    # the damaged pack is gone


async def test_the_live_route_answers_a_packed_hit_from_the_decode(tmp_path):
    """``_serve_live`` (trackers, libgme, uade, SNDH, PSF, MIDI, AdLib) — the
    route most renders take — sends a packed hit through the decode, never
    the renderer."""
    from soniqboom.api import stream
    data = _wav("f32le", 2, 48000, 48000)
    key = stream._ck("livetid", "tracker", subsong=0)
    await _stored(tmp_path, key, data)
    assert await cc.pack_entry(key)
    _drop_hot(key)

    async def never():
        raise AssertionError("rendered")
    resp = await stream._serve_live(_Req(), track_id="livetid", format_type="tracker",
                                    subsong=0, key=key, render_fn=never, web_session=True,
                                    background=None, xf=None, rendered="openmpt123")
    assert resp.headers["x-cache-store"] == "packed"
    assert resp.headers["x-rendered"] == "openmpt123"
    assert await _body(resp) == data
    await resp.background()


async def test_renders_busy_follows_the_render_slots():
    from soniqboom.api import stream
    assert cc.renders_busy is None or True               # (the fixture unhooked it)
    assert stream._renders_busy() is False
    async with stream._render_sem:
        assert stream._renders_busy() is True
    async with stream._bg_render_sem.slot(stream.PRIO_VU):
        assert stream._renders_busy() is True
    assert stream._renders_busy() is False


async def test_the_packer_picks_the_oldest_due_entry_and_stops_cleanly(tmp_path, monkeypatch):
    data = _wav("s16le", 2, 44100, 4410)
    for k in ("q0__sub0", "q1__sub0", "q2__sub0"):
        await _stored(tmp_path, k, data)
    with cc._state_lock:
        cc._pack_queue["q0__sub0"] = time.time() + 999   # not due yet
        cc._pack_queue["q1__sub0"] = 0.0
        cc._pack_queue["q2__sub0"] = 0.0
        cc._lru["q2__sub0"] = time.time() - 50           # older than q1
    cc.pin("q2__sub0")
    assert cc._next_pack_key() == "q1__sub0"             # q2 is in use
    cc.unpin("q2__sub0")
    assert cc._next_pack_key() == "q2__sub0"
    monkeypatch.setattr(cc, "_packer_task", None)
    cc.start_packer()
    t = cc._packer_task
    for _ in range(300):
        if cc._meta["q1__sub0"].get("packed") and cc._meta["q2__sub0"].get("packed"):
            break
        await asyncio.sleep(0.05)
    cc.stop_packer()
    await asyncio.sleep(0)
    assert t.cancelled() or t.done()
    assert cc._meta["q1__sub0"].get("packed") and cc._meta["q2__sub0"].get("packed")
    assert not cc._meta["q0__sub0"].get("packed")
