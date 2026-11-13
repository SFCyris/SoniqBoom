# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Songs saved in the Fred Editor's own format ("Fred Editor " header) have
no player here — uade's Fred player plays the compiled modules only — so the
scan skips them like any format it has no player for (``NotPlayableHere``:
no scan error), whatever name they carry; compiled Fred modules still play."""
from __future__ import annotations

from pathlib import Path

import pytest

from soniqboom.core import metadata as md
from soniqboom.core import uade_formats as uf

ROOT = Path(__file__).resolve().parents[1]
EDITOR = ROOT / "internal/testdata/nostalgicplayer/frededitor"
COMPILED = next((ROOT / "internal/testdata/uade").rglob("exploding fist.fred"), None) \
    if (ROOT / "internal/testdata/uade").is_dir() else None

needs_uade = pytest.mark.skipif(not uf.player_map(), reason="uade not installed")


@needs_uade
@pytest.mark.skipif(not EDITOR.is_dir(), reason="no Fred Editor test songs")
@pytest.mark.parametrize("name", ["Stein.fred", "Wildy.fred"])
def test_an_editor_song_is_skipped_quietly(name, tmp_path):
    raw = (EDITOR / name).read_bytes()
    assert raw.startswith(md.FRED_EDITOR_MAGIC)
    for saved_as in (name, "fred.song", "song.frd"):
        f = tmp_path / saved_as
        f.write_bytes(raw)
        with pytest.raises(md.NotPlayableHere):
            md.extract(f, "x")


@needs_uade
@pytest.mark.skipif(COMPILED is None, reason="no compiled Fred test module")
def test_a_compiled_fred_module_still_plays():
    assert not COMPILED.read_bytes().startswith(md.FRED_EDITOR_MAGIC)
    assert md.extract(COMPILED, "c").format == "FredMonitor"


@needs_uade
@pytest.mark.skipif(not EDITOR.is_dir(), reason="no Fred Editor test songs")
def test_the_scan_counts_no_error_for_it(tmp_path):
    """The scan worker's result is the quiet "no player here" skip, not an
    error; a truncated copy that isn't an editor song is still an error."""
    from soniqboom.core import scanner
    (tmp_path / "Stein.fred").write_bytes((EDITOR / "Stein.fred").read_bytes())
    _p, res, _sm, _lg = scanner._extract_one(tmp_path / "Stein.fred")
    assert isinstance(res, str) and scanner._not_playable_here(res), res
    (tmp_path / "junk.fred").write_bytes(b"\x01\x02junk" * 40)
    _p, res, _sm, _lg = scanner._extract_one(tmp_path / "junk.fred")
    assert isinstance(res, str) and not scanner._not_playable_here(res), res
