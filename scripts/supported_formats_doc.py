# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rewrite the UADE player table of ``docs/SUPPORTED-FORMATS.md`` (between its
``uade-table:begin`` / ``uade-table:end`` markers) from the installed UADE's
``eagleplayer.conf``, as SoniqBoom reads it (``uade_formats``): each player's
display name, the names it is recognised by as a suffix and prefix, those
only as a prefix (a prefix another engine keeps — ``uade_formats.
_PREFIX_OWNED_ELSEWHERE`` — is left out, and a player left with no name),
and the other suffixes its modules are saved under (``_SUFFIX_ALIASES``); a
suffix whose prefix form another engine keeps is marked "(suffix only)".
The rest of the document is edited by hand.

Usage: ``python scripts/supported_formats_doc.py [--check]`` — ``--check``
exits 1 (writing nothing) when the table differs from the installed UADE's.
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "SUPPORTED-FORMATS.md"
BEGIN = "<!-- uade-table:begin (scripts/supported_formats_doc.py) -->\n"
END = "<!-- uade-table:end -->"


def _code(tokens: "list[str]") -> str:
    return ", ".join(f"`{t}`" for t in tokens)


def uade_table() -> str:
    """The table's Markdown (header and rows, one line each); empty without
    an installed UADE."""
    from soniqboom.core import uade_formats as uf
    prefix, suffix = uf.prefix_tokens(), uf.new_suffix_tokens()
    if not prefix:
        return ""
    by = collections.defaultdict(list)
    for tok, player in prefix.items():
        # (a prefix another engine keeps — ``mod.``, ``med.``, ``ptm.`` … — is
        # no UADE name, nor a suffix registered elsewhere: neither is listed)
        if tok in suffix or tok not in uf._PREFIX_OWNED_ELSEWHERE:
            by[player].append(tok)
    aliases = collections.defaultdict(list)
    for alias in uf._SUFFIX_ALIASES:
        if alias in suffix:
            aliases[suffix[alias]].append(alias)
    out = ["| Player | Suffix (`song.<x>`) and prefix (`<x>.song`) names "
           "| Prefix name only (`<x>.song`) |", "|---|---|---|"]
    for player in sorted(by, key=lambda p: uf.display_name(p).lower()):
        toks = sorted(by[player])
        both = ", ".join(f"`{t}`" + (" (suffix only)" if t in uf._PREFIX_OWNED_ELSEWHERE else "")
                         for t in toks if t in suffix)
        if aliases[player]:
            both = (both + "; also " if both else "also ") + _code(sorted(aliases[player]))
        only = _code([t for t in toks if t not in suffix])
        out.append(f"| {uf.display_name(player)} | {both or '—'} | {only or '—'} |")
    return "\n".join(out) + "\n"


def main() -> int:
    text = DOC.read_text(encoding="utf-8")
    start, end = text.index(BEGIN) + len(BEGIN), text.index(END)
    table = uade_table()
    if not table:
        print("supported_formats_doc: no UADE installed — table left as is", file=sys.stderr)
        return 1
    new = text[:start] + table + text[end:]
    if "--check" in sys.argv[1:]:
        if new != text:
            print("docs/SUPPORTED-FORMATS.md: the UADE table differs from the installed UADE's "
                  "— run scripts/supported_formats_doc.py", file=sys.stderr)
            return 1
        return 0
    DOC.write_text(new, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
