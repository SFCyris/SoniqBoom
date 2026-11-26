# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A local file replaced in place no longer plays its old render, transcode,
VU data or waveform (opportunity review v2.2.0, defect 10).

Only the share scan dropped what was made from a changed file
(``scanner._purge_made_from``); a local scan, the watcher's scoped scan and a
folder click's refresh kept serving the old audio.  Now every local path
purges a file whose size, whole-file checksum or (without checksums)
modification time changed (``track_moves.file_changed``) — and keeps what
was made from a file only touched (same checksum).  (The review's repro m1
is the first test.)"""
from __future__ import annotations

import math
import os
import struct
import time
import wave
from pathlib import Path

import pytest

from soniqboom.core import conversion_cache as cc, scanner


def make_mod(path: Path, title: bytes = b"tune", fill: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = b"square".ljust(22, b"\0") + struct.pack(">HBBHH", 512, 0, 64, 0, 512)
    samples += (b"\0" * 22 + struct.pack(">HBBHH", 0, 0, 0, 0, 1)) * 30
    pat = bytearray(1024)
    pat[0:4] = bytes([0x01, 0xAC, 0x10, fill & 0xFF])
    path.write_bytes(title[:20].ljust(20, b"\0") + samples + bytes([1, 127]) + bytes(128)
                     + b"M.K." + bytes(pat) + bytes(([100] * 16 + [156] * 16) * 32))
    return path


def make_wav(path: Path, freq: float, rate: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / rate)))
                               for i in range(rate)))
    return path


@pytest.fixture
def lib(tmp_path, monkeypatch, tmp_data_dir):
    from soniqboom.core import store as store_mod
    from soniqboom.core.store import TrackStore
    monkeypatch.setattr(store_mod, "_store", TrackStore())
    monkeypatch.setattr(scanner, "_scan_count", 0)
    monkeypatch.setattr(scanner, "_progress", scanner.ScanProgress())

    async def _none(*a, **k):
        return None
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", _none)
    root = tmp_path / "lib"
    root.mkdir()
    return root.resolve(), store_mod._store, tmp_path


async def _made_from(store, tid: str, scratch: Path, kind: str = "tracker") -> tuple:
    """A render, a transcode and a waveform of the file as it is now (what a
    play leaves)."""
    key = cc._cache_key(tid, kind, 0)
    wav = scratch / f"{tid}-render.wav"
    wav.write_bytes(b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little")
                    + bytes(16) + b"data" + (0).to_bytes(4, "little"))
    await cc.store_cached(key, kind, wav)
    tkey = cc._cache_key(tid, "transcoded", codec="mp3")
    mp3 = scratch / f"{tid}-tc.mp3"
    mp3.write_bytes(b"ID3" + bytes(100))
    await cc.store_cached(tkey, "transcoded", mp3)
    store.store_waveform(tid, [0.1, 0.2])
    assert key in cc._meta and tkey in cc._meta
    return key, tkey


def _gone(store, tid, keys) -> bool:
    return all(k not in cc._meta for k in keys) and store.get_waveform(tid) is None


def _kept(store, tid, keys) -> bool:
    return all(k in cc._meta for k in keys) and store.get_waveform(tid) == [0.1, 0.2]


def _later(p: Path, secs: float = 30) -> None:
    t = time.time() + secs
    os.utime(p, (t, t))


@pytest.mark.asyncio
async def test_a_full_scan_drops_what_was_made_from_a_replaced_module(lib):
    """Repro m1: same path, same size, other bytes (another file_md5)."""
    root, store, scratch = lib
    mod = make_mod(root / "tune.mod")
    await scanner._run_scan([str(root)])
    (tid, t), = store._tracks.items()
    keys = await _made_from(store, tid, scratch)
    data = mod.read_bytes()
    mod.write_bytes(data[:-16] + b"\x01" * 16)
    _later(mod)
    await scanner._run_scan([str(root)])
    assert tid in store._tracks and store.get_track(tid)["file_md5"] != t["file_md5"]
    assert _gone(store, tid, keys)


@pytest.mark.asyncio
async def test_a_watcher_scan_drops_what_was_made_from_a_rewritten_wav(lib):
    """Another input on the watcher's path: a WAV (no whole-file checksum —
    the modification time decides)."""
    root, store, scratch = lib
    wav = make_wav(root / "A" / "w.wav", 440)
    make_wav(root / "A" / "keep.wav", 330)
    await scanner._run_scan([str(root)])
    tid = next(i for i, t in store._tracks.items() if t["path"].endswith("w.wav"))
    keep = next(i for i, t in store._tracks.items() if t["path"].endswith("keep.wav"))
    keys = await _made_from(store, tid, scratch, "transcoded")
    keys_keep = await _made_from(store, keep, scratch, "transcoded")
    make_wav(wav, 880)                                      # rewritten in place
    _later(wav)
    await scanner._run_scan([str(root)], scope={str(root): frozenset({str(wav)})})
    assert _gone(store, tid, keys)
    assert _kept(store, keep, keys_keep)                    # (not changed: untouched)


@pytest.mark.asyncio
async def test_a_folder_refresh_drops_what_was_made_from_a_changed_file(lib):
    root, store, scratch = lib
    mod = make_mod(root / "B" / "x.mod", fill=1)
    await scanner._run_scan([str(root)])
    (tid, _t), = store._tracks.items()
    keys = await _made_from(store, tid, scratch)
    make_mod(mod, fill=2)
    _later(mod)
    await scanner.refresh_subtree_under_root(str(root), str(root / "B"))
    assert _gone(store, tid, keys)


@pytest.mark.asyncio
async def test_a_touched_file_keeps_what_was_made_from_it(lib):
    """Same bytes (same checksum), only a new time: nothing is purged."""
    root, store, scratch = lib
    mod = make_mod(root / "C" / "y.mod")
    await scanner._run_scan([str(root)])
    (tid, _t), = store._tracks.items()
    keys = await _made_from(store, tid, scratch)
    _later(mod)
    await scanner._run_scan([str(root)])
    assert _kept(store, tid, keys)


async def test_purge_drops_per_file_memos_of_the_old_content():
    """A file changed in place: the facts read from its OLD content go too —
    a uade module's subsong base, a SID's start song, cast's memo of a chip
    render's cache key (else a rewritten module plays from the old base and a
    cast pins the old render's key while the new one plays)."""
    from soniqboom.api import stream
    from soniqboom.core import cast_render
    changed, kept = "t-changed", "t-kept"
    stream._UADE_BASE_MEMO[changed] = 1
    stream._UADE_BASE_MEMO[kept] = 1
    stream._SID_START_SONG[changed] = 3
    cast_render._CHIP_KEYS[(changed, 0)] = f"{changed}__psf__vOLD"
    cast_render._CHIP_KEYS[(changed, 2)] = f"{changed}__psf__vOLD2"
    cast_render._CHIP_KEYS[(kept, 0)] = f"{kept}__psf__v"
    try:
        await scanner._purge_made_from([changed])
        assert changed not in stream._UADE_BASE_MEMO
        assert changed not in stream._SID_START_SONG
        assert (changed, 0) not in cast_render._CHIP_KEYS
        assert (changed, 2) not in cast_render._CHIP_KEYS
        # (another track's memos stay)
        assert stream._UADE_BASE_MEMO.get(kept) == 1
        assert cast_render._CHIP_KEYS.get((kept, 0)) == f"{kept}__psf__v"
    finally:
        stream._UADE_BASE_MEMO.pop(kept, None)
        cast_render._CHIP_KEYS.pop((kept, 0), None)
