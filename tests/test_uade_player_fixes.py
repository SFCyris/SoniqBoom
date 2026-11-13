"""uade players SoniqBoom adds or corrects (``core.uade_players``), the Delta
Music 2 song length (``core.dm2_length``) and how ``uade_formats.player_args``
and the render / scan paths use them."""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from soniqboom.core import dm2_length, uade_formats as uf, uade_players


# ── synthetic Delta Music 2.0 modules ────────────────────────────────────────

def _dm2(track_lens, *, speed=5, loops=(0, 0, 0, 0), blocks=None, track_blocks=None,
         cut=0) -> bytes:
    """A Delta Music 2.0 module: ``track_lens`` positions per voice (block
    numbers ``track_blocks[v]`` or 0, 1, 2, …), ``blocks`` a list of 16
    ``(effect, arg)`` rows each (default: one empty block per position used),
    no instruments; ``cut`` bytes cut off the end."""
    if track_blocks is None:
        track_blocks = [list(range(n)) for n in track_lens]
    nblocks = max([b + 1 for tb in track_blocks for b in tb] or [1])
    if blocks is None:
        blocks = [[(0, 0)] * 16 for _ in range(nblocks)]
    head = bytearray(0xBCA)
    head[0xBBB] = speed & 0xFF
    head[0xBC6:0xBCA] = b".FNL"
    out = bytes(head) + bytes(64 * 16)                     # arpeggios
    for v in range(4):
        out += struct.pack(">HH", loops[v], 2 * len(track_blocks[v]))
    for v in range(4):
        out += b"".join(bytes([b, 0]) for b in track_blocks[v])
    out += struct.pack(">I", 64 * len(blocks))
    for blk in blocks:
        out += b"".join(bytes([0, 0, e, a]) for e, a in blk)
    out += bytes(256 - 2) + struct.pack(">H", 0)            # instrument offsets, size 0
    out += struct.pack(">I", 0) + bytes(64)                 # waveforms
    out += bytes(64)
    return out[:len(out) - cut] if cut else out


ROW = 6 / 50          # one row at speed 5: six ticks
BLOCK = 16 * ROW


def test_the_song_ends_when_every_voice_has_started_over():
    assert dm2_length.song_seconds_of(_dm2([1, 2, 3, 4])) == round(4 * BLOCK, 2)


def test_a_voice_with_an_empty_track_list_counts_as_done():
    assert dm2_length.song_seconds_of(_dm2([2, 2, 2, 0])) == round(2 * BLOCK, 2)


def test_a_track_loop_position_ends_the_song_where_the_list_wraps():
    # Positions 0, 1, 2 then back to 1: the voice has started over at 3 blocks.
    assert dm2_length.song_seconds_of(_dm2([3, 1, 1, 1], loops=(1, 0, 0, 0))) == round(3 * BLOCK, 2)


def test_a_speed_effect_changes_the_row_length():
    fast = [[(0, 0), (1, 2)] + [(0, 0)] * 14]              # SetSpeed 2 on the second row
    data = _dm2([1, 1, 1, 1], blocks=fast, track_blocks=[[0], [0], [0], [0]])
    # Row 0 runs at speed 5 (six ticks), rows 1-15 at speed 2 (three ticks).
    assert dm2_length.song_seconds_of(data) == round((6 + 15 * 3) / 50, 2)


@pytest.mark.parametrize("data", [
    b"",
    bytes(0x1000),                                       # no .FNL mark
    _dm2([0, 0, 0, 0]),                                  # ends at once
    _dm2([1, 1, 1, 1], track_blocks=[[5], [0], [0], [0]], blocks=[[(0, 0)] * 16]),  # no block 5
    _dm2([1, 1, 1, 1], cut=200),                         # shorter than its header says
    _dm2([1, 1, 1, 1], speed=-1),                        # a negative speed never plays a row
])
def test_no_length_for_what_is_not_a_playable_dm2_song(data):
    assert dm2_length.song_seconds_of(data) is None


def test_song_seconds_reads_a_file_once_and_only_tune_0(tmp_path, monkeypatch):
    monkeypatch.setattr(dm2_length, "_CACHE", {})
    f = tmp_path / "a.dm2"
    f.write_bytes(_dm2([1, 2, 3, 4]))
    assert dm2_length.song_seconds(f) == round(4 * BLOCK, 2)
    assert dm2_length.song_seconds(f, 1) is None
    calls = []
    monkeypatch.setattr(dm2_length, "song_seconds_of", lambda d: calls.append(d) or 0.0)
    assert dm2_length.song_seconds(f) == round(4 * BLOCK, 2) and not calls   # remembered
    f.write_bytes(_dm2([1, 1, 1, 1]) + b"x")                                  # changed: read again
    dm2_length.song_seconds(f)
    assert len(calls) == 1
    assert dm2_length.song_seconds(tmp_path / "missing.dm2") is None


def _sqsh(data: bytes) -> bytes:
    """An XPK-SQSH file of one stored chunk (as in test_xpk_and_render_errors)."""
    padded = data + bytes(-len(data) % 4)
    s = 0
    for i in range(0, len(padded), 4):
        s ^= int.from_bytes(padded[i:i + 4], "big")
    csum = (s ^ (s >> 16)) & 0xFFFF
    body = (bytes([0, 0]) + csum.to_bytes(2, "big") + len(data).to_bytes(2, "big")
            + len(data).to_bytes(2, "big") + data)
    head = b"XPKF" + (36 + len(body) - 8).to_bytes(4, "big") + b"SQSH" + len(data).to_bytes(4, "big")
    return head + bytes(20) + body


def test_a_packed_dm2_module_is_measured_unpacked(tmp_path, monkeypatch):
    monkeypatch.setattr(dm2_length, "_CACHE", {})
    monkeypatch.setattr(uf, "classify", lambda name: ("DeltaMusic2.0", "dm2")
                        if name.endswith(".dm2") else None)
    packed = _sqsh(_dm2([1, 2, 3, 4]))
    (tmp_path / "p.dm2").write_bytes(packed)
    (tmp_path / "p.mod").write_bytes(packed)               # not named for Delta Music 2
    assert dm2_length.song_seconds(tmp_path / "p.dm2") == round(4 * BLOCK, 2)
    assert dm2_length.song_seconds(tmp_path / "p.mod") is None


# ── player_args ──────────────────────────────────────────────────────────────

@pytest.fixture
def fake_uade(tmp_path, monkeypatch):
    """A uade conf with the players this file is about, nothing remembered."""
    conf = tmp_path / "uade" / "eagleplayer.conf"
    (conf.parent / "players").mkdir(parents=True)
    conf.write_text("DeltaMusic2.0\tprefixes=dlm2,dm2\nForgottenWorlds_Game\tprefixes=fw\n"
                    "SUN-Tronic\tprefixes=sun\nJesperOlsen\tprefixes=jo\n")
    monkeypatch.setattr(uf, "conf_path", lambda: conf)
    monkeypatch.setattr(uf, "_map_memo", {"at": 0.0, "map": None})
    monkeypatch.setattr(uf, "_ARGS_CACHE", {})
    monkeypatch.setattr(dm2_length, "_CACHE", {})
    return conf


def test_a_dm2_module_ignores_the_players_song_end(tmp_path, fake_uade):
    f = tmp_path / "song.dm2"
    f.write_bytes(_dm2([1, 2, 3, 4]))
    assert uf.player_args(f, "/bin/uade123") == ["-n"]
    assert uf.player_args(f, None) == []                    # no uade: nothing
    g = tmp_path / "other.dm2"
    g.write_bytes(bytes(0x1000))                            # no song length: uade decides
    assert uf.player_args(g, "/bin/uade123") == []


@pytest.mark.parametrize("own,patched,want", [
    (True, True, True),       # the stock player accepts it too: still the corrected one
    (False, True, True),
    (True, False, False),     # the corrected player refuses it: not a Forgotten Worlds module
])
def test_a_forgotten_worlds_module_plays_with_the_corrected_player(
        tmp_path, fake_uade, monkeypatch, own, patched, want):
    fixed = tmp_path / "data" / "ForgottenWorlds_Game"
    fixed.parent.mkdir()
    fixed.write_bytes(b"p")
    monkeypatch.setattr(uade_players, "forgotten_worlds", lambda: fixed)
    monkeypatch.setattr(uf, "_accepts", lambda b, p, extra: patched if extra else own)
    for name in ("forgotten worlds intro.fw", "fw.intro"):
        f = tmp_path / name
        f.write_bytes(b"\0" * 64)
        assert uf.player_args(f, "/bin/uade123") == (["-P", str(fixed)] if want else [])


def test_no_corrected_player_leaves_forgotten_worlds_alone(tmp_path, fake_uade, monkeypatch):
    monkeypatch.setattr(uade_players, "forgotten_worlds", lambda: None)
    monkeypatch.setattr(uf, "_accepts", lambda b, p, extra: True)
    f = tmp_path / "a.fw"
    f.write_bytes(b"\0" * 64)
    assert uf.player_args(f, "/bin/uade123") == []


@pytest.mark.parametrize("own,other,want", [
    (False, True, True),      # the stock player refuses it, SUNtronic2 plays it
    (True, True, False),      # the stock player plays it: kept
    (False, False, False),    # neither: no change
])
def test_a_sun_module_the_stock_player_refuses_gets_suntronic2(
        tmp_path, fake_uade, monkeypatch, own, other, want):
    monkeypatch.setattr(uf, "_accepts", lambda b, p, extra: other if extra else own)
    bundled = uade_players.bundled("SUNtronic2")
    assert bundled is not None
    for name in ("pseudo oops.sun", "sun.pseudo oops"):
        f = tmp_path / name
        f.write_bytes(b"\0" * 64)
        assert uf.player_args(f, "/bin/uade123") == (["-P", str(bundled)] if want else [])


def test_fw_and_sun_prefixes_still_reach_the_extensions_player(tmp_path, fake_uade, monkeypatch):
    # "sun.x.tme": SUN-Tronic by its prefix (refuses it), neither SUNtronic2
    # nor the corrected FW player plays it, The Musical Enlightenment does.
    fake_uade.write_text(fake_uade.read_text() + "TheMusicalEnlightenment\tprefixes=tme\n")
    tme = fake_uade.parent / "players" / "TheMusicalEnlightenment"
    tme.write_bytes(b"p")
    fixed = tmp_path / "ForgottenWorlds_Game"
    fixed.write_bytes(b"p")
    monkeypatch.setattr(uade_players, "forgotten_worlds", lambda: fixed)
    monkeypatch.setattr(uf, "_accepts", lambda b, p, extra: bool(extra) and extra[1] == str(tme))
    for name in ("sun.two.tme", "fw.two.tme"):
        f = tmp_path / name
        f.write_bytes(b"\0" * 64)
        assert uf.player_args(f, "/bin/uade123") == ["-P", str(tme)]


def test_player_files_are_looked_at_on_a_cache_miss_only(tmp_path, fake_uade, monkeypatch):
    # A name with no candidate player: no player lookup, no stat of the
    # module.  A candidate: the player files are looked at once; the next
    # call is answered from the cache.  A changed module is looked at again.
    fixed = tmp_path / "data" / "ForgottenWorlds_Game"
    fixed.parent.mkdir()
    fixed.write_bytes(b"p")
    looks = []
    monkeypatch.setattr(uade_players, "forgotten_worlds", lambda: looks.append("fw") or fixed)
    monkeypatch.setattr(uade_players, "bundled", lambda n: looks.append(n) or None)
    monkeypatch.setattr(uf, "_accepts", lambda b, p, extra: bool(extra))
    plain = tmp_path / "plain.mod"
    plain.write_bytes(b"\0" * 64)
    stats = []
    real_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda self, *a, **k: stats.append(self.name)
                        or real_stat(self, *a, **k))
    assert uf.player_args(plain, "/bin/uade123") == []
    assert looks == [] and "plain.mod" not in stats
    f = tmp_path / "a.fw"
    f.write_bytes(b"\0" * 64)
    assert uf.player_args(f, "/bin/uade123") == ["-P", str(fixed)]
    assert looks == ["fw"]
    assert uf.player_args(f, "/bin/uade123") == ["-P", str(fixed)]
    assert looks == ["fw"]                                  # (the cache answered)
    f.write_bytes(b"\1" * 65)                               # another file now
    assert uf.player_args(f, "/bin/uade123") == ["-P", str(fixed)]
    assert looks == ["fw", "fw"]
    fixed.unlink()                                          # the player it named is gone
    assert uf.player_args(f, "/bin/uade123") == ["-P", str(fixed)]
    assert looks == ["fw", "fw", "fw"]


def test_a_forgotten_worlds_extension_beats_another_prefix(tmp_path, fake_uade, monkeypatch):
    fake_uade.write_text(fake_uade.read_text() + "NTSP-system\tprefixes=two\n")
    (fake_uade.parent / "players" / "ForgottenWorlds_Game").write_bytes(b"stock")
    fixed = tmp_path / "fixed_fw"
    fixed.write_bytes(b"p")
    monkeypatch.setattr(uade_players, "forgotten_worlds", lambda: fixed)
    monkeypatch.setattr(uf, "_accepts", lambda b, p, extra: bool(extra))
    f = tmp_path / "two.fw"
    f.write_bytes(b"\0" * 64)
    assert uf.player_args(f, "/bin/uade123") == ["-P", str(fixed)]


def test_only_a_dm2_name_is_looked_at_on_disk(tmp_path, fake_uade, monkeypatch):
    f = tmp_path / "song.mod.x"
    f.write_bytes(_dm2([1, 2, 3, 4]))                       # DM2 content, another name
    real_stat = Path.stat
    touched = []
    monkeypatch.setattr(Path, "stat", lambda self, *a, **k: touched.append(self) or real_stat(self, *a, **k))
    assert dm2_length.song_seconds(f) is None
    assert uf.player_args(tmp_path / "song.unknownext", "/bin/uade123") == []
    assert [t for t in touched if t.parent == tmp_path] == []     # (uade's conf aside)


def test_the_jo_fallback_still_uses_the_installed_player(tmp_path, fake_uade, monkeypatch):
    (fake_uade.parent / "players" / "JesperOlsen_EP").write_bytes(b"p")
    monkeypatch.setattr(uf, "_accepts", lambda b, p, extra: bool(extra))
    f = tmp_path / "x.jo"
    f.write_bytes(b"\0" * 64)
    assert uf.player_args(f, "/bin/uade123") == ["-P", str(fake_uade.parent / "players" / "JesperOlsen_EP")]


# ── the bundled SUNtronic2 player ────────────────────────────────────────────

def test_the_bundled_suntronic2_is_the_documented_build():
    p = uade_players.bundled("SUNtronic2")
    readme = (Path(uade_players.__file__).resolve().parent.parent / "native" / "uade-players"
              / "README.md").read_text()
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    assert digest in readme
    assert (p.parent / "SUNtronic2.asm").read_text().startswith(
        "; SPDX-License-Identifier: AGPL-3.0-or-later")
    assert uade_players.bundled("NoSuchPlayer") is None


# ── the corrected Forgotten Worlds player ────────────────────────────────────

def _fake_stock() -> bytes:
    b = bytearray(1484)
    b[:4] = b"\x00\x00\x03\xf3"
    b[uade_players.FW_PATCH_AT:uade_players.FW_PATCH_AT + 6] = uade_players.FW_STOCK_BYTES
    return bytes(b)


@pytest.fixture
def fake_fw(tmp_path, monkeypatch):
    """A players folder holding a stand-in "stock" Forgotten Worlds player
    (the module's checksums pointed at it) and a data folder."""
    stock = _fake_stock()
    fixed = (stock[:uade_players.FW_PATCH_AT] + uade_players.FW_PATCH_BYTES
             + stock[uade_players.FW_PATCH_AT + 6:])
    monkeypatch.setattr(uade_players, "FW_STOCK_SHA256", hashlib.sha256(stock).hexdigest())
    monkeypatch.setattr(uade_players, "FW_PATCHED_SHA256", hashlib.sha256(fixed).hexdigest())
    players = tmp_path / "uade" / "players"
    players.mkdir(parents=True)
    (players / uade_players.FW_PLAYER).write_bytes(stock)
    dest = tmp_path / "data" / "uade-players" / uade_players.FW_PLAYER
    monkeypatch.setattr(uade_players, "_players_dir", lambda: players)
    monkeypatch.setattr(uade_players, "_fw_dest", lambda: dest)
    monkeypatch.setattr(uade_players, "_fw_memo", {})
    return players / uade_players.FW_PLAYER, dest, fixed


def test_the_corrected_player_is_written_once_from_the_stock_one(fake_fw, monkeypatch):
    stock, dest, fixed = fake_fw
    assert uade_players.forgotten_worlds() == dest
    assert dest.read_bytes() == fixed
    assert stock.read_bytes() == _fake_stock()              # the installed player is untouched
    assert not list(dest.parent.glob(".*.tmp"))             # no temp file left
    writes = []
    real = uade_players._write_atomic
    monkeypatch.setattr(uade_players, "_write_atomic", lambda d, b: (writes.append(d), real(d, b)))
    monkeypatch.setattr(uade_players, "_fw_memo", {})
    assert uade_players.forgotten_worlds() == dest and writes == []   # already there
    dest.write_bytes(b"damaged")                            # (no restart: the memo notices)
    assert uade_players.forgotten_worlds() == dest and writes == [dest]
    assert dest.read_bytes() == fixed


def test_another_installed_player_gets_no_correction(fake_fw):
    stock, dest, _fixed = fake_fw
    other = bytearray(_fake_stock())
    other[0x20] = 1                                          # a different file
    stock.write_bytes(bytes(other))
    assert uade_players.forgotten_worlds() is None
    assert not dest.exists()


def test_the_bytes_at_the_patch_site_must_be_the_stock_ones(monkeypatch):
    stock = bytearray(_fake_stock())
    stock[uade_players.FW_PATCH_AT] = 0x4E                   # not the expected instruction
    monkeypatch.setattr(uade_players, "FW_STOCK_SHA256", hashlib.sha256(stock).hexdigest())
    assert uade_players.fw_patched_bytes(bytes(stock)) is None


def test_no_installed_player_no_correction(fake_fw):
    stock, dest, _fixed = fake_fw
    stock.unlink()
    assert uade_players.forgotten_worlds() is None


_INSTALLED_FW = (uf.conf_path().parent / "players" / uade_players.FW_PLAYER
                 if uf.conf_path() else None)


@pytest.mark.skipif(not (_INSTALLED_FW and _INSTALLED_FW.is_file()), reason="uade not installed")
def test_the_installed_uade_305_player_gets_the_documented_correction():
    stock = _INSTALLED_FW.read_bytes()
    if hashlib.sha256(stock).hexdigest() != uade_players.FW_STOCK_SHA256:
        pytest.skip("installed player isn't uade 3.05's")
    fixed = uade_players.fw_patched_bytes(stock)
    assert hashlib.sha256(fixed).hexdigest() == uade_players.FW_PATCHED_SHA256
    assert [i for i in range(len(stock)) if stock[i] != fixed[i]] == [0x40C, 0x40E, 0x40F]


# ── the render and scan paths ────────────────────────────────────────────────

async def test_a_dm2_render_stops_at_the_song_length(tmp_path, monkeypatch):
    from soniqboom.api import stream
    monkeypatch.setattr(dm2_length, "_CACHE", {})
    seen = {}

    async def fake_file(src, *args, **kwargs):
        seen.update(kwargs, src=src)
        return src
    monkeypatch.setattr(stream, "_render_uade_file", fake_file)
    f = tmp_path / "song.dm2"
    f.write_bytes(_dm2([1, 2, 3, 4]))
    await stream._render_uade(f, subsong=0, expected_seconds=99.0)
    assert seen["stop_after_s"] == seen["expected_seconds"] == round(4 * BLOCK, 2)
    seen.clear()
    g = tmp_path / "plain.mod"
    g.write_bytes(bytes(0x1000))
    await stream._render_uade(g, subsong=0, expected_seconds=99.0)
    assert seen.get("stop_after_s") is None and seen["expected_seconds"] == 99.0


def test_the_scan_stores_a_dm2_modules_length(tmp_path, monkeypatch):
    from soniqboom.core import metadata
    monkeypatch.setattr(dm2_length, "_CACHE", {})
    f = tmp_path / "song.dm2"
    f.write_bytes(_dm2([1, 2, 3, 4]))
    d = metadata._extract_uade(f, "t1", {"_ok": True, "playername": "DeltaMusic 2.0"})
    assert d["duration"] == round(4 * BLOCK, 2)
    g = tmp_path / "x.fw"
    g.write_bytes(bytes(64))
    assert metadata._extract_uade(g, "t2", {"_ok": True})["duration"] == 0.0


# ── a stored uade length is promised only when the current revision read it ──

def test_a_stored_length_from_an_older_render_revision_is_not_promised():
    """A first play after the uade output changed must not stop at the length
    an earlier revision's render stored (the render may now be longer)."""
    import types
    from soniqboom.api import stream as _s
    cur = _s._uade_render_rev()
    assert cur >= 1
    old = types.SimpleNamespace(duration=20.0)                      # no revision recorded
    older = types.SimpleNamespace(duration=20.0, duration_rev=cur - 1)
    now = types.SimpleNamespace(duration=20.0, duration_rev=cur)
    assert _s._uade_expected_seconds(old, 0) == 0.0
    assert _s._uade_expected_seconds(older, 0) == 0.0
    assert _s._uade_expected_seconds(now, 0) == 20.0


def test_the_uade_backfill_records_the_revision_also_when_the_length_is_unchanged(tmp_path):
    import asyncio, struct, types
    from soniqboom.api import stream as _s
    wav = tmp_path / "t.wav"
    data = bytes(176400 * 2)                                        # 2 s, 44.1 kHz stereo 16-bit
    wav.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
                    + struct.pack("<IHHIIHH", 16, 1, 2, 44100, 176400, 4, 16)
                    + b"data" + struct.pack("<I", len(data)) + data)
    cur = _s._uade_render_rev()
    same = types.SimpleNamespace(duration=2.0)
    sink: list = []
    assert asyncio.run(_s._backfill_rendered_duration(
        "a", same, wav, 0.0, authoritative=True, sink=sink, rev=cur)) == 2.0
    assert sink == [("a", {"duration_rev": cur})]
    sink.clear()
    stale = types.SimpleNamespace(duration=20.0, duration_rev=cur - 1)
    asyncio.run(_s._backfill_rendered_duration(
        "b", stale, wav, 0.0, authoritative=True, sink=sink, rev=cur))
    assert sink == [("b", {"duration": 2.0, "duration_rev": cur})]
    sink.clear()
    done = types.SimpleNamespace(duration=2.0, duration_rev=cur)
    asyncio.run(_s._backfill_rendered_duration(
        "c", done, wav, 0.0, authoritative=True, sink=sink, rev=cur))
    assert sink == []                                               # nothing to write
    # other families pass no revision: unchanged behaviour
    asyncio.run(_s._backfill_rendered_duration(
        "d", types.SimpleNamespace(duration=20.0), wav, 0.0, authoritative=True, sink=sink))
    assert sink == [("d", {"duration": 2.0})]


def test_an_unchanged_rescan_keeps_a_recorded_revision_also_when_it_is_0():
    """``duration_rev`` is a post-scan field whose 0 is a real value: an
    unchanged file's re-extract (which has no revision) is no change."""
    from soniqboom.core import scanner
    old = {"id": "r", "path": "/x/a.dw", "title": "t", "duration": 40.0, "duration_rev": 0,
           "mtime": 1.0, "file_size": 10}
    new = {"id": "r", "path": "/x/a.dw", "title": "t", "duration": 0.0, "mtime": 1.0,
           "file_size": 10}
    assert scanner._same_track_content(old, new)
    assert scanner._same_track_content({**old, "duration_rev": 1}, new)
