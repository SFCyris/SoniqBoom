# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-channel VU backfill (``/api/tracks/<id>/vu`` on a cache miss) runs
for every format the stream plays through libopenmpt — decided by the
stream's own routing, not a separate suffix list — so the meter shows on
the very FIRST play (the web player asks a tracker module once; only SID and
Amiga tunes keep re-asking).  PolyTracker ``.ptm`` was left out of the old
list: its meter appeared only on a replay."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from soniqboom.api import tracks as tracks_api
from soniqboom.core import metadata as md, openmpt_vu

ROOT = Path(__file__).resolve().parents[1]
PTM = ROOT / "internal/testdata/nostalgicplayer/polytracker/A Fish In The Sea.ptm"


def _t(path: str, fmt: str, genre=None):
    return SimpleNamespace(id="t", path=path, format=fmt, genre=genre or ["Tracker", "Module"])


@pytest.mark.parametrize("path, fmt, expected", [
    ("/m/a.mod", "ProTracker", True),
    ("/m/a.ptm", "Poly Tracker", True),
    ("/m/a.md3", "OctaMED", True),
    ("/m/x.zip::inner/a.ptm", "Poly Tracker", True),
    ("/m/a.flac", "FLAC", False),
    ("/m/a.dmf", "DefleMask", False),          # (Furnace)
    ("/m/a.ftm", "FamiTracker", False),        # (Furnace)
    ("/m/a.st", "Sawteeth", True),             # (sawteethrender -v writes its levels)
    ("/m/a.musx", "Archimedes Tracker", False),  # (xmprender — libopenmpt can't read it)
    ("/m/a.ntc", "NoiseTracker Compressed", True),  # (packed: the converted module's meters)
    ("/m/a.p22a", "The Player 2.2A", True),
])
def test_the_backfill_follows_the_streams_routing(path, fmt, expected):
    assert tracks_api._vu_backfill_route(_t(path, fmt)) is expected


def test_every_libopenmpt_extension_is_backfilled():
    from soniqboom.api import stream
    for ext in md.OPENMPT_EXTRA_EXTS | set(md._TRACKER_ALT_EXTS):
        if ext in (".ftm", ".dmf"):
            continue                           # (told by the record — above)
        assert tracks_api._vu_backfill_route(_t(f"/m/a{ext}", "x")), ext
        assert ext in stream._TRACKER_EXTS


@pytest.mark.asyncio
@pytest.mark.skipif(not PTM.is_file(), reason="no PolyTracker test module")
async def test_a_polytracker_module_gets_its_meter_on_first_ask(tmp_path, monkeypatch):
    if not openmpt_vu.is_available() or ".ptm" not in md._TRACKER_ALT_EXTS:
        pytest.skip("libopenmpt unavailable")
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    track = _t(str(PTM), "Poly Tracker")
    sidecar = await tracks_api._try_backfill_vu_sidecar(track, "ptm1", 0)
    assert sidecar is not None and sidecar.exists()
    channels, rate, frames = openmpt_vu.parse_and_validate_vumr(sidecar.read_bytes())
    assert channels == 5 and frames > rate * 50          # (5 channels, ~59 s of meter frames)
    levels = sidecar.read_bytes()[16 + channels:]
    assert max(levels[0::channels]) > 0                  # (channel 1 plays)


NP = ROOT / "internal/testdata/nostalgicplayer"


@pytest.mark.asyncio
@pytest.mark.skipif(not NP.is_dir(), reason="no NostalgicPlayer test files")
async def test_every_packed_module_gets_the_converted_modules_meter(tmp_path, monkeypatch):
    if not openmpt_vu.is_available():
        pytest.skip("libopenmpt unavailable")
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    seen = 0
    for p in sorted(NP.rglob("*")):
        if p.suffix.lower() not in md.PW_EXTS or not p.is_file():
            continue
        if "Part #15" in p.name:
            continue                            # (its samples are silent in the source)
        sidecar = await tracks_api._try_backfill_vu_sidecar(
            _t(str(p), md.PW_EXT_NAMES[p.suffix.lower()]), f"pw{seen}", 0)
        assert sidecar is not None, p.name
        channels, rate, frames = openmpt_vu.parse_and_validate_vumr(sidecar.read_bytes())
        levels = sidecar.read_bytes()[16 + channels:]
        assert channels in (4, 8) and frames > rate and max(levels) > 0, p.name
        seen += 1
    assert seen >= 25


@pytest.mark.asyncio
async def test_a_file_that_isnt_a_packed_module_gets_none(tmp_path, monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    f = tmp_path / "junk.ntc"
    f.write_bytes(bytes(2000))
    assert await tracks_api._try_backfill_vu_sidecar(_t(str(f), "NoiseTracker Compressed"),
                                                     "j", 0) is None
