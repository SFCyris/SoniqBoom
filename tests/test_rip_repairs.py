"""Known damaged rips repaired for uade (``core/rip_repairs.py``): a file is
repaired only when its size, digest and bytes are what its entry expects;
any other file passes through untouched; a Synth Dream tune whose sampled
instruments don't start at the samples of the bank beside it is refused."""
from __future__ import annotations

import hashlib
import shutil
import struct

import pytest
from fastapi import HTTPException

from soniqboom.core import rip_repairs as rr
from soniqboom.core import uade_formats as uf
from soniqboom.core import xpk


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _entry(damaged: bytes, repaired: bytes, fill: int, name: str = "x", **kw) -> "rr._Entry":
    """A table entry for ``damaged`` → ``repaired`` (runs: the bytes that differ)."""
    runs, i = [], 0
    while i < len(damaged):
        if damaged[i] != repaired[i]:
            j = i
            while j < len(damaged) and damaged[j] != repaired[j]:
                j += 1
            runs.append(f"{i:x}" if j - i == 1 else f"{i:x}+{j - i:x}")
            i = j
        else:
            i += 1
    return rr._Entry(name, len(damaged), fill, _sha(damaged), _sha(repaired),
                     runs=" ".join(runs), **kw)


@pytest.fixture(autouse=True)
def _own_temp_dir(tmp_path_factory, monkeypatch):
    """Staged copies go to this test's own temp folder (other tests watch the
    system one for ``sb-xpk-*`` folders)."""
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("staged")))


@pytest.fixture
def table(monkeypatch):
    """Replace the known-file table with the entries a test adds."""
    entries: list = []

    def use(*new):
        entries.extend(new)
        monkeypatch.setattr(rr, "_TABLE", tuple(entries))
        monkeypatch.setattr(rr, "_BY_SHA", {e.sha256: e for e in entries})
        monkeypatch.setattr(rr, "_SIZES", frozenset(e.size for e in entries))
    use()
    return use


# ── the table itself ─────────────────────────────────────────────────────────

def test_every_table_entry_is_consistent():
    assert len(rr._TABLE) == 21
    assert len({e.sha256 for e in rr._TABLE}) == len(rr._TABLE)
    for e in rr._TABLE:
        if e.truncate is not None:
            assert 0 < e.truncate < e.size and not e.runs
            continue
        last = -1
        for off, old, new in e.patches():
            assert off > last and off + len(old) <= e.size, e.name
            assert old == bytes([e.fill]) * len(old) and new == bytes(len(old))
            last = off + len(old) - 1
        assert e.fill not in (0, 0xFF)


def test_runs_are_hex_offsets_and_hex_lengths():
    e = rr._Entry("x", 64, 0xFC, "", "", runs="3 1a+10 ")
    assert e.patches() == [(3, b"\xfc", b"\0"), (0x1A, b"\xfc" * 16, bytes(16))]


# ── applying an entry ────────────────────────────────────────────────────────

_GOOD = bytes(range(1, 40)) + bytes(8) + b"\x11\x22" + bytes(3) + b"tail"
_BAD = _GOOD.replace(bytes(8), b"\xfc" * 8).replace(b"\x22" + bytes(3), b"\x22\xfc\xfc\x00")


def test_an_entry_puts_the_zeros_back():
    e = _entry(_BAD, _GOOD, 0xFC)
    assert rr._apply(e, _BAD) == _GOOD


@pytest.mark.parametrize("what", ["size", "run bytes", "result digest"])
def test_an_entry_that_does_not_match_is_not_used(what):
    e = _entry(_BAD, _GOOD, 0xFC)
    if what == "size":
        assert rr._apply(e, _BAD + b"!") is None
    elif what == "run bytes":
        assert rr._apply(e, _BAD.replace(b"\xfc" * 8, b"\xfc" * 7 + b"\x01")) is None
    else:
        wrong = rr._Entry(e.name, e.size, e.fill, e.sha256, _sha(b"other"), runs=e.runs)
        assert rr._apply(wrong, _BAD) is None


def test_a_truncation_entry_cuts_the_file():
    whole = b"PACK2" * 10 + b"FRAGMENT-OF-PACK3"
    e = rr._Entry("p5.hipc", len(whole), 0, _sha(whole), _sha(whole[:50]), truncate=50,
                  defect="partial", detail="only its first part plays")
    assert rr._apply(e, whole) == whole[:50]


# ── which files are read ─────────────────────────────────────────────────────

def test_a_known_file_is_repaired_and_never_written(tmp_path, table):
    table(_entry(_BAD, _GOOD, 0xFC, "sdr.tune"))
    f = tmp_path / "SDR.tune"
    f.write_bytes(_BAD)
    assert rr.repaired(f) == _GOOD
    assert f.read_bytes() == _BAD


def test_a_digest_mismatch_passes_through(tmp_path, table):
    table(_entry(_BAD, _GOOD, 0xFC, "sdr.tune"))
    f = tmp_path / "sdr.tune"
    f.write_bytes(_BAD[:-1] + b"X")                    # same size, other bytes
    assert rr.repaired(f) is None


def test_other_files_are_not_read(tmp_path, table, monkeypatch):
    table(_entry(_BAD, _GOOD, 0xFC, "sdr.tune"))
    opened: list = []
    real_open = open

    def spy(p, *a, **k):
        opened.append(str(p))
        return real_open(p, *a, **k)
    monkeypatch.setattr("builtins.open", spy)
    for name, data in [("mod.tune", _BAD),                 # same bytes, other name
                       ("sdr.other", _BAD + b"!"),          # another size
                       ("readme.txt", _BAD)]:
        f = tmp_path / name
        f.write_bytes(data)
        opened.clear()
        assert rr.repaired(f) is None
        assert str(f) not in opened, name
    assert rr.repaired(tmp_path / "missing.sdr") is None


def test_a_name_that_is_no_candidate_is_not_even_stat(tmp_path, table, monkeypatch):
    table(_entry(_BAD, _GOOD, 0xFC, "sdr.tune"))
    f = tmp_path / "tune.mod"
    f.write_bytes(_BAD)
    stats: list = []
    real_stat = rr.os.stat
    monkeypatch.setattr(rr.os, "stat", lambda p, *a, **k: stats.append(str(p)) or real_stat(p, *a, **k))
    assert rr.repaired(f) is None and rr.known_defect(f) is None
    assert stats == []


def test_the_candidate_gate_uses_the_first_or_last_name_part(table):
    table(_entry(_BAD, _GOOD, 0xFC))
    n = len(_BAD)
    assert rr._candidate("SDR.tune", n) and rr._candidate("tune.pvp", n)
    assert rr._candidate("max.x y", n) and rr._candidate("a - b.hipc", n)
    assert not rr._candidate("tune.mod", n) and not rr._candidate("sdr.tune", n + 1)
    assert not rr._candidate("sdr", n)


def test_known_defect_is_set_only_for_a_partial_entry(tmp_path, table):
    whole = b"PACK2" * 10 + b"FRAGMENT-OF-PACK3"
    table(rr._Entry("p5.hipc", len(whole), 0, _sha(whole), _sha(whole[:50]), truncate=50,
                    defect="partial", detail="only its first part plays"),
          _entry(_BAD, _GOOD, 0xFC, "sdr.tune"))
    (tmp_path / "pack5.hipc").write_bytes(whole)
    (tmp_path / "sdr.tune").write_bytes(_BAD)
    assert rr.known_defect(tmp_path / "pack5.hipc") == ("partial", "only its first part plays")
    assert rr.known_defect(tmp_path / "sdr.tune") is None


def test_the_scan_marks_a_partial_rip(tmp_path, table):
    from soniqboom.core import metadata
    whole = b"PACK2" * 10 + b"FRAGMENT-OF-PACK3"
    table(rr._Entry("p5.hipc", len(whole), 0, _sha(whole), _sha(whole[:50]), truncate=50,
                    defect="partial", detail="only its first part plays"))
    f = tmp_path / "pack5.hipc"
    f.write_bytes(whole)
    d = metadata._extract_uade(f, "t1", {"_ok": True, "playername": "Hippel-COSO"})
    assert (d.get("defect"), d.get("defect_detail")) == ("partial", "only its first part plays")
    other = tmp_path / "pack2.hipc"
    other.write_bytes(whole[:50])
    assert "defect" not in metadata._extract_uade(other, "t2", {"_ok": True})


# ── staging ──────────────────────────────────────────────────────────────────

needs_conf = pytest.mark.skipif("sdr" not in uf.player_map(), reason="uade conf missing")


@needs_conf
def test_a_known_module_and_bank_are_staged_repaired(tmp_path, table):
    bank_bad = b"\0" * 8 + b"FORM" + b"\x05" * 20
    bank_good = b"\0" * 8 + b"FORM" + b"\x05" * 4 + bytes(16)
    table(_entry(_BAD, _GOOD, 0xFC, "sdr.tune"), _entry(bank_bad, bank_good, 0x05, "smp.tune"))
    (tmp_path / "sdr.tune").write_bytes(_BAD)
    (tmp_path / "smp.tune").write_bytes(bank_bad)
    (tmp_path / "smp.set").write_bytes(b"other bank")
    out = xpk.stage_for_uade(tmp_path / "sdr.tune")
    try:
        assert out.parent != tmp_path and out.name == "sdr.tune"
        assert out.read_bytes() == _GOOD
        assert (out.parent / "smp.tune").read_bytes() == bank_good
        assert not (out.parent / "smp.tune").is_symlink()
        assert (out.parent / "smp.set").is_symlink()
    finally:
        shutil.rmtree(out.parent)
    assert (tmp_path / "sdr.tune").read_bytes() == _BAD
    assert (tmp_path / "smp.tune").read_bytes() == bank_bad


@needs_conf
def test_an_unknown_module_is_not_staged(tmp_path, table):
    table(_entry(_BAD, _GOOD, 0xFC, "sdr.tune"))
    (tmp_path / "sdr.tune").write_bytes(_GOOD)          # the clean file
    (tmp_path / "smp.tune").write_bytes(b"bank")
    assert xpk.stage_for_uade(tmp_path / "sdr.tune") is None
    (tmp_path / "sdr.tune").write_bytes(_BAD[:-1] + b"X")  # damaged otherwise
    assert xpk.stage_for_uade(tmp_path / "sdr.tune") is None


# ── Synth Dream: the bank must hold the tune's samples ──────────────────────

def _bank(n_samples: int, body: int = 40) -> "tuple[bytes, list[int]]":
    """An SMP bank: 8 zero bytes, then IFF 8SVX FORMs with a ``body``-byte BODY each."""
    out = bytearray(8)
    starts = []
    for _ in range(n_samples):
        form = b"8SVX" + b"VHDR" + struct.pack(">I", 20) + bytes(20) + b"BODY" + struct.pack(">I", body)
        out += b"FORM" + struct.pack(">I", len(form) + body) + form
        starts.append(len(out))
        out += bytes(range(1, body + 1))
    return bytes(out), starts


def _sdr(instruments: "list[tuple[int, int, int]]") -> bytes:
    """A Synth Dream module whose instrument table holds (synth offset,
    sample start, length in words) entries; the rest of the file is filler."""
    table_at = 64
    out = bytearray(b"\x01" * table_at)
    out[0:4] = struct.pack(">I", table_at)
    out[table_at - 2:table_at] = b"\xff\xff"
    for synth, start, length in instruments:
        out += bytes(16)                                  # flag, pitch, 3 envelope offsets
        out += struct.pack(">IIHIH", synth, start, length, 0, 2)
    out += b"\x46" + bytes(200)                         # envelope data (first byte not 0)
    return bytes(out)


def test_a_tune_that_matches_its_bank_plays(tmp_path):
    bank, starts = _bank(3)
    (tmp_path / "SMP.tune").write_bytes(bank)
    (tmp_path / "SDR.tune").write_bytes(_sdr([(0, s, 10) for s in starts] + [(0x60, 0, 0)]))
    assert rr.synthdream_refusal(tmp_path / "SDR.tune") is None


def test_a_tune_against_another_tunes_bank_is_refused(tmp_path):
    bank, starts = _bank(4)
    (tmp_path / "smp.set").write_bytes(bank)             # the shared fallback bank
    (tmp_path / "sdr.tune").write_bytes(_sdr([(0, starts[0], 10), (0, 0x1234, 10),
                                              (0, 0x2000, 10), (0, 0x2222, 10)]))
    assert rr.synthdream_refusal(tmp_path / "sdr.tune") == rr.SAMPLE_BANK_MISSING


def test_the_tunes_own_bank_is_preferred_to_smp_set(tmp_path):
    own, starts = _bank(3, body=40)
    other, _ = _bank(3, body=64)                         # (only the first sample starts alike)
    (tmp_path / "smp.tune").write_bytes(own)
    (tmp_path / "SMP.set").write_bytes(other)
    (tmp_path / "sdr.tune").write_bytes(_sdr([(0, s, 10) for s in starts]))
    assert rr.synthdream_refusal(tmp_path / "sdr.tune") is None
    (tmp_path / "smp.tune").unlink()
    assert rr.synthdream_refusal(tmp_path / "sdr.tune") == rr.SAMPLE_BANK_MISSING


def _damaged(sdr: bytes, fill: int = 0xFC) -> bytes:
    """``sdr`` with every zero byte of its instrument table set to ``fill``
    (the second entry's first byte too)."""
    o = struct.unpack(">I", sdr[:4])[0]
    end = sdr.index(b"\x46", o)
    out = bytearray(sdr)
    for i in range(o + 1, end):
        if out[i] == 0:
            out[i] = fill
    out[o + 32] = fill
    return bytes(out)


def test_a_damaged_rip_beside_its_own_bank_is_not_refused(tmp_path):
    bank, starts = _bank(3)
    (tmp_path / "smp.tune").write_bytes(bank)
    sdr = _damaged(_sdr([(0, s, 10) for s in starts]))
    (tmp_path / "sdr.tune").write_bytes(sdr)
    found, fill = rr._sample_starts(sdr)
    assert fill == 0xFC and len(found) == 3          # (past the entry whose first byte is the fill)
    assert rr.synthdream_refusal(tmp_path / "sdr.tune") is None


def test_a_damaged_rip_against_another_bank_is_refused(tmp_path):
    bank, starts = _bank(3)
    (tmp_path / "smp.set").write_bytes(bank)
    sdr = _damaged(_sdr([(0, starts[0], 10), (0, 0x1234, 10), (0, 0x2000, 10)]))
    (tmp_path / "sdr.tune").write_bytes(sdr)
    assert rr.synthdream_refusal(tmp_path / "sdr.tune") == rr.SAMPLE_BANK_MISSING


def test_a_clean_entry_gives_no_fill_byte():
    assert rr._fill_byte(bytes(16) + struct.pack(">IIHIH", 0, 0x38, 10, 0, 2)) is None
    assert rr._fill_byte(b"\0\xfc\x01\x90" + b"\xfc" * 4 + bytes(24)) == 0xFC


def test_a_bank_without_body_chunks_is_not_judged(tmp_path):
    # A bank holding no BODY chunk (not an 8SVX sample set, or one damaged
    # past reading) can't tell a mismatched tune from a matching one: the
    # tune plays.  Control: the same tune against a real bank is refused.
    (tmp_path / "smp.set").write_bytes(b"\x01\x02" * 400)
    (tmp_path / "sdr.tune").write_bytes(_sdr([(0, 0x1234, 10), (0, 0x2000, 10)]))
    assert rr._body_offsets((tmp_path / "smp.set").read_bytes()) == set()
    assert rr.synthdream_refusal(tmp_path / "sdr.tune") is None
    bank, _ = _bank(2)
    (tmp_path / "smp.set").write_bytes(bank)
    assert rr.synthdream_refusal(tmp_path / "sdr.tune") == rr.SAMPLE_BANK_MISSING


@pytest.mark.parametrize("case", ["other format", "no bank", "only synths"])
def test_nothing_to_check_is_not_refused(tmp_path, case):
    bank, starts = _bank(2)
    if case == "other format":
        (tmp_path / "smp.set").write_bytes(bank)
        (tmp_path / "max.tune").write_bytes(_sdr([(0, 0x1234, 10)]))
        assert rr.synthdream_refusal(tmp_path / "max.tune") is None
    elif case == "no bank":
        (tmp_path / "sdr.tune").write_bytes(_sdr([(0, 0x1234, 10)]))
        assert rr.synthdream_refusal(tmp_path / "sdr.tune") is None
    else:
        (tmp_path / "smp.set").write_bytes(bank)
        (tmp_path / "sdr.tune").write_bytes(_sdr([(0x60, 0x1234, 10)]))
        assert rr.synthdream_refusal(tmp_path / "sdr.tune") is None


async def test_a_refused_tune_is_a_render_refusal(tmp_path):
    from soniqboom.api import stream
    from soniqboom.core import unplayable
    bank, _ = _bank(3)
    (tmp_path / "smp.set").write_bytes(bank)
    (tmp_path / "sdr.tune").write_bytes(_sdr([(0, 0x1234, 10), (0, 0x2000, 10)]))
    with pytest.raises(HTTPException) as exc:
        async with stream._xpk_unpacked(tmp_path / "sdr.tune"):
            pass
    err = exc.value
    assert isinstance(err, unplayable.RenderRefusal) and err.status_code == 422
    assert (err.renderer, err.diagnosed) == ("uade123", True)
    assert err.why == f"can't play this file: {rr.SAMPLE_BANK_MISSING}"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sdr.tune", "smp.set"]


@needs_conf
async def test_a_staged_tune_that_is_refused_leaves_no_temp_folder(tmp_path, table, monkeypatch):
    from soniqboom.api import stream
    bank, _ = _bank(3)
    good = _sdr([(0, 0x1234, 10), (0, 0x2000, 10)])
    bad = good[:-1] + b"\xfc"
    table(_entry(bad, good, 0xFC, "sdr.tune"))
    (tmp_path / "smp.set").write_bytes(bank)
    (tmp_path / "sdr.tune").write_bytes(bad)
    staged: list = []
    real = xpk.stage_for_uade
    monkeypatch.setattr(stream._xpk, "stage_for_uade",
                        lambda *a, **k: staged.append(real(*a, **k)) or staged[-1])
    with pytest.raises(HTTPException) as exc:
        async with stream._xpk_unpacked(tmp_path / "sdr.tune"):
            pass
    assert exc.value.status_code == 422 and staged and staged[0] is not None
    assert not staged[0].parent.exists()
    assert (tmp_path / "sdr.tune").read_bytes() == bad
