# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``docs/SUPPORTED-FORMATS.md`` follows the formats SoniqBoom plays: every
supported suffix is named in it, and its UADE player table is the installed
UADE's (``scripts/supported_formats_doc.py`` rewrites that table)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from soniqboom.core import metadata as md, uade_formats as uf

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs/SUPPORTED-FORMATS.md").read_text(encoding="utf-8")


def _script():
    spec = importlib.util.spec_from_file_location("supported_formats_doc",
                                                  ROOT / "scripts/supported_formats_doc.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_supported_suffix_is_in_the_document():
    """A suffix added to (or found only by) the code must be documented:
    the formats libopenmpt reads where it does, every engine's own suffixes
    and the other names of uade's formats (``_SUFFIX_ALIASES``)."""
    exts = {e for e in md.SUPPORTED_EXTENSIONS if e not in md._UADE_SUFFIX_EXTS}
    exts |= set(md._OPENMPT_OPTIONAL) | {f".{a}" for a in uf._SUFFIX_ALIASES}
    missing = sorted(e for e in exts if f"`{e}`" not in DOC and f"`{e[1:]}`" not in DOC)
    assert missing == [], f"add these to docs/SUPPORTED-FORMATS.md: {missing}"


def test_the_uade_table_is_the_installed_uades():
    if not uf.player_map():
        pytest.skip("uade not installed")
    script = _script()
    begin, end = DOC.index(script.BEGIN) + len(script.BEGIN), DOC.index(script.END)
    assert DOC[begin:end] == script.uade_table(), \
        "run scripts/supported_formats_doc.py to rewrite the UADE table"


def test_the_uade_table_names_what_uade_plays_and_nothing_else():
    """Every name UADE is given a file by (``classify``, prefix or suffix
    form) is in the table, and no name it isn't (``mod.song`` is
    libopenmpt's)."""
    if not uf.player_map():
        pytest.skip("uade not installed")
    script = _script()
    table = DOC[DOC.index(script.BEGIN):DOC.index(script.END)]
    listed = set(__import__("re").findall(r"`([^`]+)`", table)) - {"song.<x>", "<x>.song"}
    for tok in listed:
        assert uf.classify(f"{tok}.song") or uf.classify(f"song.{tok}"), tok
    missing = sorted(t for t in uf.prefix_tokens()
                     if (uf.classify(f"{t}.song") or uf.classify(f"song.{t}")) and t not in listed)
    assert missing == []
