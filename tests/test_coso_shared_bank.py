# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hippel COSO songs played with a shared instrument file (Cryptoburners
"pack2", "pack4" and "pack5" with "pack3"): the bank is a companion, not a
track; staging appends its instruments to a song without its own; a tune
table cut short keeps the tunes left and marks the song partial."""
from __future__ import annotations

import shutil
import struct

import pytest

from soniqboom.core import rip_repairs, uade_formats as uf, xpk

SONG2 = "cryptoburners - pack2.hipc"
SONG4 = "cryptoburners - pack4.hipc"
BANK = "cryptoburners - pack3.hipc"


def _coso(offs, tunes, instr, body: bytes) -> bytes:
    head = b"COSO" + struct.pack(">7I", *offs) + b"TFMX" + struct.pack(">6H", 1, 1, 1, 1, 64, 0)
    head += struct.pack(">HH", tunes, instr)
    return head + body


def _song(tunes=3, instr=2, cut=0) -> bytes:
    """A COSO song whose instrument table would start at its end; ``cut``
    bytes are cut off its tune table."""
    pre = bytes(range(60)) * 2                     # (sequences / patterns stand-in)
    tunes_at = 52 + len(pre)
    table = b"".join(struct.pack(">HHH", i, i + 1, 6) for i in range(tunes)) + b"\xaa" * 6
    info_at = tunes_at + len(table)
    data = _coso((0x40, 0x50, 0x60, 0x70, tunes_at, info_at, 0), tunes, instr, pre + table)
    return data[:len(data) - cut] if cut else data


def _bank(instr=3, data_off=0, sample_len=8) -> bytes:
    info_at = 52
    info = b"".join(struct.pack(">IHHH", i * sample_len, sample_len // 2, 0, sample_len // 2)
                    for i in range(instr))
    samples = bytes((i * 7) & 0xFF for i in range(instr * sample_len))
    return _coso((0x40, 0x50, 0, 0, 0, info_at, data_off), 0, instr, info + samples)


# ── names ────────────────────────────────────────────────────────────────────

def test_the_bank_is_a_companion_and_the_songs_are_tracks():
    assert uf.is_companion_half(f"Hippel COSO/Jochen Hippel/{BANK}")
    assert uf.is_companion_half(BANK.upper())
    for song in (SONG2, SONG4, "cryptoburners - pack5.hipc"):
        assert not uf.is_companion_half(song)
        assert uf.coso_bank_for(f"Hippel COSO/Jochen Hippel/{song}") == BANK
    assert uf.coso_bank_for("return of medusa (1).hipc") is None


def test_a_songs_companions_include_its_bank():
    assert BANK in uf.companion_sibling_names(SONG2)
    assert BANK not in uf.companion_sibling_names("return of medusa (1).hipc")


# ── assembly ─────────────────────────────────────────────────────────────────

def _parse(out: bytes):
    offs = struct.unpack(">7I", out[4:32])
    tunes, instr = struct.unpack(">HH", out[48:52])
    return offs, tunes, instr


def test_a_whole_song_gets_the_banks_instruments_after_its_tune_table():
    song, bank = _song(tunes=3, instr=2), _bank(instr=3)
    assert rip_repairs.coso_needs_bank(song)
    out = rip_repairs.coso_with_bank(song, bank)
    offs, tunes, instr = _parse(out)
    assert out[:len(song)] == song[:24] + out[24:32] + song[32:]   # (only the two offsets change)
    assert (tunes, instr) == (3, 2) and offs[5] == len(song)
    assert out[offs[5]:offs[6]] == bank[52:52 + 2 * 10]           # its 2 of the bank's 3 entries
    assert out[offs[6]:] == bank[52 + 3 * 10:]                     # the samples after the bank's table
    assert rip_repairs.coso_tunes(song) == (3, 3)


def test_a_cut_tune_table_keeps_the_whole_tunes_left():
    song = _song(tunes=4, instr=2, cut=6 + 3 * 6 - 2)    # padding and 3 tunes gone, 2 bytes of tune 2 left
    assert rip_repairs.coso_tunes(song) == (1, 4)
    out = rip_repairs.coso_with_bank(song, _bank(instr=2))
    offs, tunes, instr = _parse(out)
    assert tunes == 1
    assert offs[5] == offs[4] + 6 + 6 and out[offs[4] + 6:offs[5]] == bytes(6)   # one tune + padding
    assert out[offs[4]:offs[4] + 6] == song[offs[4]:offs[4] + 6]


def test_the_samples_follow_a_stated_data_offset():
    bank = _bank(instr=2, data_off=52 + 2 * 10 + 4)
    bank = bank[:72] + b"\x00" * 4 + bank[72:]
    out = rip_repairs.coso_with_bank(_song(instr=2), bank)
    offs, _, _ = _parse(out)
    assert out[offs[6]:] == bank[76:]


@pytest.mark.parametrize("why, song, bank", [
    ("a song with instruments of its own", _coso((0x40, 0x50, 0x60, 0x70, 172, 172, 172), 1, 1, bytes(140)), _bank()),
    ("a bank that is a song itself", _song(), _song()),
    ("a bank with fewer instruments", _song(instr=3), _bank(instr=2)),
    ("a sample past the bank's end", _song(instr=2), _bank(instr=2)[:-1]),
    ("no COSO at all", _song(), b"FORM" + bytes(80)),
])
def test_an_unfitting_pair_is_left_as_it_is(why, song, bank):
    assert rip_repairs.coso_with_bank(song, bank) is None, why


def test_a_song_whose_tune_table_is_gone_is_left_as_it_is():
    song = _song(tunes=2, cut=6 + 2 * 6)
    assert rip_repairs.coso_tunes(song) == (0, 2)
    assert rip_repairs.coso_with_bank(song, _bank()) is None


# ── staging and the scan's mark ──────────────────────────────────────────────

def test_staging_attaches_the_bank_beside_the_song(tmp_path):
    (tmp_path / SONG4).write_bytes(_song(tunes=3, instr=2))
    (tmp_path / BANK.upper()).write_bytes(_bank(instr=3))        # (matched ignoring case)
    staged = xpk.stage_for_uade(tmp_path / SONG4)
    try:
        assert staged is not None and staged.name == SONG4
        assert staged.read_bytes() == rip_repairs.coso_with_bank(_song(tunes=3, instr=2), _bank(instr=3))
        assert (tmp_path / SONG4).read_bytes() == _song(tunes=3, instr=2)   # (never written)
    finally:
        if staged is not None:
            shutil.rmtree(staged.parent, ignore_errors=True)
    assert rip_repairs.known_defect(tmp_path / SONG4) is None    # (all three tunes are there)


def test_without_the_bank_nothing_is_staged_and_the_song_is_held_back(tmp_path):
    (tmp_path / SONG4).write_bytes(_song())
    assert rip_repairs.coso_bank_attached(tmp_path / SONG4) is None
    assert xpk.stage_for_uade(tmp_path / SONG4) is None
    assert rip_repairs.coso_bank_missing(tmp_path / SONG4, SONG4) == BANK
    (tmp_path / BANK).write_bytes(_bank())
    staged = xpk.stage_for_uade(tmp_path / SONG4)
    try:
        assert rip_repairs.coso_bank_missing(staged, SONG4) is None
    finally:
        shutil.rmtree(staged.parent, ignore_errors=True)


def test_a_song_with_instruments_of_its_own_is_left_alone(tmp_path, caplog):
    own = _coso((0x40, 0x50, 0x60, 0x70, 172, 172, 172), 1, 1, bytes(140))
    (tmp_path / SONG4).write_bytes(own)
    (tmp_path / BANK).write_bytes(_bank())
    with caplog.at_level("WARNING"):
        assert rip_repairs.coso_bank_attached(tmp_path / SONG4) is None
    assert not caplog.records                                     # (no warning for it)
    assert rip_repairs.coso_bank_missing(tmp_path / SONG4, SONG4) is None


def test_a_known_damaged_rip_is_cut_first_then_given_the_bank(tmp_path, monkeypatch):
    import hashlib
    song = _song(tunes=4, instr=2, cut=6 + 3 * 6)
    rip = song + b"FRAGMENT OF ANOTHER FILE" * 20                  # (pack5: pack2 + junk)
    entry = rip_repairs._Entry("pack5", len(rip), 0, hashlib.sha256(rip).hexdigest(),
                               hashlib.sha256(song).hexdigest(), truncate=len(song))
    monkeypatch.setattr(rip_repairs, "_BY_SHA", {entry.sha256: entry})
    monkeypatch.setattr(rip_repairs, "_SIZES", frozenset({len(rip)}))
    name = "cryptoburners - pack5.hipc"
    (tmp_path / name).write_bytes(rip)
    (tmp_path / BANK).write_bytes(_bank(instr=2))
    staged = xpk.stage_for_uade(tmp_path / name)
    try:
        assert staged.read_bytes() == rip_repairs.coso_with_bank(song, _bank(instr=2))
        assert _parse(staged.read_bytes())[1] == 1
    finally:
        shutil.rmtree(staged.parent, ignore_errors=True)
    assert rip_repairs.known_defect(tmp_path / name) == (
        "partial", "only 1 of its 4 tunes is left in the file")
    assert rip_repairs.coso_song_tunes(tmp_path / name) == (1, 4)
    (tmp_path / BANK).unlink()
    staged = xpk.stage_for_uade(tmp_path / name)                   # (cut, but no bank)
    try:
        assert rip_repairs.coso_bank_missing(staged, name) == BANK
    finally:
        shutil.rmtree(staged.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_a_play_without_the_bank_is_a_503_and_renders_nothing(tmp_path):
    from fastapi import HTTPException
    from soniqboom.api import stream
    (tmp_path / SONG2).write_bytes(_song(tunes=4, cut=6 + 3 * 6))
    with pytest.raises(HTTPException) as exc:
        async with stream._xpk_unpacked(tmp_path / SONG2):
            pass
    assert exc.value.status_code == 503 and BANK in exc.value.detail
    (tmp_path / BANK).write_bytes(_bank())
    async with stream._xpk_unpacked(tmp_path / SONG2) as src:
        assert not rip_repairs.coso_needs_bank(src.read_bytes())


@pytest.mark.asyncio
async def test_a_remote_song_is_fetched_with_its_bank_and_an_older_folder_is_made_again(
        monkeypatch, tmp_path):
    from soniqboom.api import stream
    from soniqboom.core import remote_cache
    from tests.test_hippel_staging import _Cache, _Source
    d = "/Hippel COSO/Jochen Hippel"
    files = {f"{d}/{SONG4}": _song(), f"{d}/{BANK}": _bank(), f"{d}/other.hipc": b"x"}
    src = _Source(files)
    cache = _Cache(src, tmp_path / "rc")
    monkeypatch.setattr(remote_cache, "get_cache", lambda: cache)
    zx = tmp_path / "zx"
    zx.mkdir()
    monkeypatch.setattr(stream, "_zip_extract_dir", lambda: zx)
    monkeypatch.setattr(stream, "_register_adlib_extract", lambda *a, **k: None)
    monkeypatch.setattr(stream, "_zip_evict_until_under_budget", lambda *a, **k: None)
    # A folder made before songs brought their bank: no bank, the old marker.
    real = uf.coso_bank_for
    monkeypatch.setattr(uf, "coso_bank_for", lambda n: None)
    old = await stream._materialize_loose_remote_uade("p4", "share", f"{d}/{SONG4}", src)
    monkeypatch.setattr(uf, "coso_bank_for", real)
    assert old is not None and not (old.parent / BANK).exists()
    got = await stream._materialize_loose_remote_uade("p4", "share", f"{d}/{SONG4}", src)
    assert (got.parent / BANK).read_bytes() == _bank()
    assert (got.parent / ".loose_marker").read_text().split("|")[0].endswith(":c2:b1")
    assert f"{d}/other.hipc" not in src.read
    staged = xpk.stage_for_uade(got)
    try:
        assert not rip_repairs.coso_needs_bank(staged.read_bytes())
    finally:
        shutil.rmtree(staged.parent, ignore_errors=True)


def test_a_played_bank_song_is_read_again_after_the_rules_change():
    from soniqboom.core import modland_catalog as mc
    for rel, again in ((f"Hippel COSO/Jochen Hippel/{SONG2}", True),
                       ("Hippel COSO/Jochen Hippel/return of medusa (1).hipc", False)):
        fresh = mc.build_track(rel, 4838, added_at=1, siblings={}, use_filename=False,
                               names=True, dir_hash="d", root_h="r")
        old = dict(fresh, mtime=123.0, subsongs=4, duration=393.6)
        got = mc._rederive_merged(old, fresh)
        assert mc.is_merged(got)
        assert bool(got.get(mc.REREAD)) is again and mc._needs_merge(got) is again


def test_a_song_of_another_name_is_not_given_the_bank(tmp_path):
    (tmp_path / "other.hipc").write_bytes(_song())
    (tmp_path / BANK).write_bytes(_bank())
    assert rip_repairs.coso_bank_attached(tmp_path / "other.hipc") is None
    assert rip_repairs.known_defect(tmp_path / "other.hipc") is None


def test_a_cut_tune_table_is_marked_partial(tmp_path):
    (tmp_path / SONG2).write_bytes(_song(tunes=4, cut=6 + 3 * 6))
    assert rip_repairs.known_defect(tmp_path / SONG2) == (
        "partial", "only 1 of its 4 tunes is left in the file")
    (tmp_path / SONG2).write_bytes(_song(tunes=4, cut=6 + 2 * 6))
    assert rip_repairs.known_defect(tmp_path / SONG2) == (
        "partial", "only 2 of its 4 tunes are left in the file")

