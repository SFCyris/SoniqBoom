# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A render stored into the conversion cache across filesystems never leaves
a partial file under the entry's name, and the start adopts only whole WAVs
(``conversion_cache._move_into_place`` / ``warmup_from_disk``)."""
from __future__ import annotations

import asyncio
import errno
import os
import shutil
from pathlib import Path

import pytest

from soniqboom.core import conversion_cache as cc


def _wav(n_data: int, *, declared: int | None = None, fill: bytes = b"\x01") -> bytes:
    d = n_data if declared is None else declared
    fmt = (b"fmt " + (16).to_bytes(4, "little")
           + bytes.fromhex("0100020044ac000010b1020004001000"))
    return (b"RIFF" + (4 + len(fmt) + 8 + d).to_bytes(4, "little") + b"WAVE" + fmt
            + b"data" + d.to_bytes(4, "little") + fill * n_data)


@pytest.fixture
def cache_dir(monkeypatch, tmp_path):
    from soniqboom.config import settings
    base = tmp_path / "conv"
    monkeypatch.setattr(settings, "conversion_cache_dir", str(base))
    saved = (dict(cc._meta), cc._lru.copy(), cc._total_bytes)
    cc._meta.clear()
    cc._lru.clear()
    yield base
    with cc._state_lock:
        cc._meta.clear()
        cc._meta.update(saved[0])
        cc._lru.clear()
        cc._lru.update(saved[1])
        cc._total_bytes = saved[2]


def _exdev_for(monkeypatch, name: str) -> None:
    """``os.replace`` of a file called ``name`` fails as across filesystems."""
    real = os.replace

    def _replace(src, dst, *a, **k):
        if os.fspath(src).endswith(name):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "replace", _replace)


def test_cross_filesystem_store_is_whole_and_removes_the_temp(cache_dir, tmp_path, monkeypatch):
    src = tmp_path / "tmp" / "tmpRENDER.wav"
    src.parent.mkdir()
    data = _wav(300_000)
    src.write_bytes(data)
    _exdev_for(monkeypatch, "tmpRENDER.wav")
    dest = asyncio.run(cc.store_cached("xfs__sid__r9__k", "sid", src))
    assert dest.read_bytes() == data
    assert not src.exists()
    assert [p.name for p in dest.parent.iterdir()] == [dest.name]   # no temp copy left
    assert cc._meta["xfs__sid__r9__k"]["size_bytes"] == len(data)


def test_cross_filesystem_copy_cut_off_leaves_no_entry_file(cache_dir, tmp_path, monkeypatch):
    """The copy fails part-way (as a process killed mid-copy stops it): the
    entry's own name never holds the partial bytes."""
    src = tmp_path / "tmp" / "tmpRENDER.wav"
    src.parent.mkdir()
    src.write_bytes(_wav(2_000_000))
    _exdev_for(monkeypatch, "tmpRENDER.wav")

    def _cut_copy(s, d, *, follow_symlinks=True):
        with open(s, "rb") as fi, open(d, "wb") as fo:
            fo.write(fi.read(500_000))
        raise OSError(errno.EIO, "copy cut off")
    monkeypatch.setattr(shutil, "copyfile", _cut_copy)
    with pytest.raises(OSError):
        asyncio.run(cc.store_cached("cut__sid__r9__k", "sid", src))
    dest = cc._cache_path("cut__sid__r9__k", "sid")
    assert not dest.exists()
    assert "cut__sid__r9__k" not in cc._meta


def test_same_filesystem_store_is_a_rename(cache_dir, tmp_path):
    src = tmp_path / "tmpSAME.wav"
    data = _wav(1000)
    src.write_bytes(data)
    ino = src.stat().st_ino
    dest = asyncio.run(cc.store_cached("same__sid__r9__k", "sid", src))
    assert dest.read_bytes() == data and dest.stat().st_ino == ino and not src.exists()


def test_start_adopts_only_whole_wavs_and_sweeps_unfinished_writes(cache_dir):
    def put(fmt: str, name: str, body: bytes) -> Path:
        p = cache_dir / fmt / name[:2] / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        return p
    whole = put("sid", "wh__sid__r9__k.wav", _wav(1000))
    torn = put("sid", "to__sid__r9__k.wav", _wav(1000)[:44 + 400])
    head_only = put("sid", "ho__sid__r9__k.wav", _wav(1000)[:20])
    empty = put("sid", "em__sid__r9__k.wav", b"")
    ff = (0xFFFFFFFF).to_bytes(4, "little")
    streaming = put("midi", "st__midi.wav",                  # header written while streaming
                    b"RIFF" + ff + _wav(500)[8:36] + b"data" + ff + b"\x01" * 500)
    mp3 = put("transcoded", "mp__cmp3__ar0__br192.wav", b"ID3\x04" + b"\xff\xfb" * 400)
    padded = put("sid", "pa__sid__r9__k.wav", _wav(1000) + b"LIST" + (4).to_bytes(4, "little") + b"INFO")
    copying = put("sid", ".cp__sid__r9__k.wav.123.abcd1234.copying", _wav(1000)[:300])
    cast_partial = put("transcoded", "ca__cwav.wav.partial", _wav(1000)[:300])
    upload = put("sid", "tmpab12cd.wav.upload", _wav(1000)[:300])          # a SID upload cut off
    # playable layouts some writers produce: an odd-sized chunk without its
    # pad byte before the audio, a RIFF size larger than the file
    odd = (b"RIFF" + (0).to_bytes(4, "little") + b"WAVE" + b"LIST" + (3).to_bytes(4, "little")
           + b"abc" + _wav(500)[12:])
    nopad = put("sid", "np__sid__r9__k.wav", odd)
    big_riff = bytearray(_wav(800))
    big_riff[4:8] = (10 ** 7).to_bytes(4, "little")
    riffbig = put("sid", "rb__sid__r9__k.wav", bytes(big_riff))
    n = cc.warmup_from_disk()
    adopted = set(cc._meta)
    assert adopted == {"wh__sid__r9__k", "st__midi", "mp__cmp3__ar0__br192", "pa__sid__r9__k",
                       "np__sid__r9__k", "rb__sid__r9__k"}, adopted
    assert n == 6
    for p in (torn, head_only, empty, copying, cast_partial, upload):
        assert not p.exists(), p
    for p in (whole, streaming, mp3, padded, nopad, riffbig):
        assert p.exists(), p
    assert cc._total_bytes >= sum(p.stat().st_size for p in (whole, streaming, mp3, padded))


@pytest.mark.parametrize("cut", [12, 30, 44, 45, 1043])
def test_wav_is_whole_cut_points(tmp_path, cut):
    body = _wav(1000)
    p = tmp_path / "x.wav"
    p.write_bytes(body[:cut])
    assert cc._wav_is_whole(p, cut) is False
    p.write_bytes(body)
    assert cc._wav_is_whole(p, len(body)) is True
