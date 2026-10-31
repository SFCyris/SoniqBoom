# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The phone Search view splits the field's text into operator terms,
exclusions and free text the way ``api/search._parse_advanced_query`` reads
it (``soniqboom/frontend/js/mobile/search_query.js`` ``splitQuery``).  Both
sides check the same cases, tests/js/m_search_split_cases.json: this test
against the server's own patterns (``_SCAN_RE``, ``_OPERATORS``, ``_WORD_RE``,
``_excluded_word``, the range tightening), tests/js/m_search_query.test.mjs
against the phone's code — a change to either parser fails one of them.

In a scope a space right after an operator's colon keeps the term text
(``track: 2`` stays part of "Back on Track: 2"); a comparator or value that
starts at the colon still joins across spaces (``year:>= 2000``,
``year:1990 - 1999``)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from soniqboom.api import search as S

CASES = json.loads((Path(__file__).parent / "js" / "m_search_split_cases.json").read_text("utf-8"))


def _tighten_in_scope(q: str) -> str:
    pieces = S._QUOTED_RE.split(q)
    for i in range(0, len(pieces), 2):
        p = S._RANGE_OP_SPACE_RE.sub(
            lambda m: m.group(0) if m.group(0)[len(m.group(1)) + 1:][:1].isspace()
            else m.group(1) + ":" + (m.group(2) or ""), pieces[i])
        p = S._UNIT_SPACE_RE.sub(r"\1\2", p)
        p = S._RANGE_RANGE_SPACE_RE.sub(r"\1-", p)
        pieces[i] = S._UNIT_SPACE_RE.sub(r"\1\2", p)
    return "".join(pieces)


def _split(q: str):
    q = _tighten_in_scope(q)
    ops, pieces, at = [], [], 0
    for m in S._SCAN_RE.finditer(q):
        if m.group(2) is None:
            continue
        if m.group(2).lower() in S._OPERATORS:
            ops.append(q[m.start():m.end()])
            pieces.append(q[at:m.start()])
            at = m.end()
    pieces.append(q[at:])
    words = S._WORD_RE.findall("".join(pieces))
    excl = [w for w in words if S._excluded_word(w) is not None]
    free = " ".join(w for w in words if S._excluded_word(w) is None)
    return sorted(ops), sorted(excl), free


@pytest.mark.parametrize("case", CASES, ids=[c["text"] for c in CASES])
def test_server_split_matches_the_phone_cases(case):
    assert _split(case["text"]) == (case["ops"], case["excl"], case["free"])


def test_the_cases_cover_the_tricky_forms():
    texts = {c["text"] for c in CASES}
    for t in ("back on track: 2", "vnv year:>= 2000", "year:1990 - 1999 x", "format:flac, alac -live foo",
              '"song year:1999 remaster', 'foo -"live at" bar', "multi-track:2 - 3", 'artist:"vnv nat'):
        assert t in texts
    assert len(CASES) >= 100
