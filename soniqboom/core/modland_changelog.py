# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Modland's daily changelogs: parse them and plan what they change.

Modland publishes one ``changelog_YYYY-MM-DD.txt`` per day with changes (none
on quiet days): the newest in the archive's root until it is archived, the
older ones in ``/pub/changelogs``.  Paths are relative to ``/pub/modules``;
records (verbs right-aligned to 7 characters)::

      added: Protracker/Gizmo (DE)/alien1.mod
    updated: Fasttracker 2/Kenet/chip or not.xm
    deleted: JamCracker/Lydian/bronx megademo ii.jam
      moved: FredMon/- unknown/game shorty.fred
         to: FredMon/Shorty
         as: ghh-first-demo.fred            (optional: a new name too)
    renamed: Protracker/Gary Copper/nice-2 0.mod
         to: nicetune2.mod

``moved … to`` names the new FOLDER (``as`` the new file name, else the old
one is kept); ``renamed … to`` names the new file name in the same folder.

``parse`` turns a file into operations (``ChangelogError`` on anything it
doesn't understand — the caller then downloads the full index instead).
``plan`` applies operations in order over the current index (``exists``) and
returns the NET change — what to add, delete, re-size and move (old path →
new path) — so a file added and moved in the same run is just added at its
final place, a move of a move is one move, and re-applying an add, a delete
or a single move the index already contains changes nothing.  Records the
index may already contain (a day the last full index covers, a changelog
rewritten since it was read) are planned in REPLAY mode (``replay``): a move
whose file is at neither end, or one whose source only exists because an
earlier replayed record added it, is taken as applied already instead of
refused.  (A swap of two names replayed is swapped again: names alone can't
tell — the caller reads only what is new in a file it has read before.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

_RECORD_RE = re.compile(r"^\s*(added|updated|deleted|moved|renamed|to|as): (.*)$")
_NAME_RE = re.compile(r"^changelog_(\d{4}-\d{2}-\d{2})\.txt$")


class ChangelogError(ValueError):
    """A changelog (or the plan of one) that can't be applied with certainty."""


def date_of(name: str) -> str | None:
    """``YYYY-MM-DD`` of a ``changelog_YYYY-MM-DD.txt`` file name, else None."""
    m = _NAME_RE.match(name or "")
    return m.group(1) if m else None


def _norm(p: str) -> str:
    """A path as the index keeps it (``modland_catalog._index_lines``)."""
    p = p.strip().strip("/")
    if (not p or "\x00" in p or "\r" in p or "\n" in p or "�" in p
            or any(seg in ("", ".", "..") for seg in p.split("/"))):
        raise ChangelogError(f"unusable path in the changelog: {p!r}")
    return p


def parse(data: bytes | str) -> list[tuple]:
    """``[("added"|"updated"|"deleted", rel) | ("moved", src, dst), …]`` in
    file order.  Raises ``ChangelogError`` on an undecodable file, an unknown
    line or a ``moved`` / ``renamed`` without its ``to``."""
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ChangelogError(f"the changelog is not UTF-8 ({exc})") from None
    else:
        text = data
    ops: list[tuple] = []
    pending: list | None = None          # ["moved"|"renamed", src, to, as]

    def _flush() -> None:
        nonlocal pending
        if pending is None:
            return
        verb, src, to, as_ = pending
        pending = None
        if to is None:
            raise ChangelogError(f"{verb}: {src} has no 'to:' line")
        src = _norm(src)
        if verb == "moved":
            folder = to.strip().strip("/")
            name = _norm(as_) if as_ is not None else src.rsplit("/", 1)[-1]
            if "/" in name:
                raise ChangelogError(f"'as:' names a path, not a file name: {as_!r}")
            dst = _norm(f"{folder}/{name}" if folder else name)
        else:                                            # renamed
            if as_ is not None:
                raise ChangelogError(f"renamed: {src} has an 'as:' line")
            name = _norm(to)
            if "/" in name:
                raise ChangelogError(f"renamed: {src} to a path: {to!r}")
            parent = src.rpartition("/")[0]
            dst = f"{parent}/{name}" if parent else name
        if dst != src:
            ops.append(("moved", src, dst))

    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        m = _RECORD_RE.match(raw)
        if m is None:
            raise ChangelogError(f"line {n} isn't a changelog record: {raw[:80]!r}")
        verb, rest = m.group(1), m.group(2)
        if verb in ("to", "as"):
            if pending is None:
                raise ChangelogError(f"line {n}: '{verb}:' without a moved / renamed line")
            slot = 2 if verb == "to" else 3
            if pending[slot] is not None:
                raise ChangelogError(f"line {n}: a second '{verb}:' line")
            if verb == "as" and pending[2] is None:
                raise ChangelogError(f"line {n}: 'as:' before 'to:'")
            pending[slot] = rest
            continue
        _flush()
        if verb in ("moved", "renamed"):
            pending = [verb, rest, None, None]
        else:
            ops.append((verb, _norm(rest)))
    _flush()
    return ops


@dataclass
class Plan:
    """The net change of a run of changelogs over the index."""
    added: list[str] = field(default_factory=list)       # new files (size unknown)
    deleted: list[str] = field(default_factory=list)     # files of the index that went
    updated: list[str] = field(default_factory=list)     # files of the index whose content changed
    moved: dict[str, str] = field(default_factory=dict)  # index path → new path
    moved_changed: set[str] = field(default_factory=set)  # new paths of moves updated too
    ops: int = 0                                          # operations read

    def need_size(self) -> list[str]:
        """The paths whose size the listing must supply."""
        return [*self.added, *self.updated, *sorted(self.moved_changed)]

    def __bool__(self) -> bool:
        return bool(self.added or self.deleted or self.updated or self.moved)


def plan(ops: Iterable[tuple], exists: Callable[[str], bool],
         replay: "Iterable[bool] | None" = None) -> Plan:
    """The net effect of ``ops`` (in order) on an index where ``exists(rel)``
    says which files it lists.  Idempotent: an operation the index already
    reflects (an ``added`` file that is there, a ``deleted`` one that isn't,
    a ``moved`` file already at its new place) is a no-op.  Raises
    ``ChangelogError`` for a move that can't be placed with certainty (its
    file is at neither end, or at both) — unless ``replay`` (one flag per
    operation) marks it as a record the index may already contain (module
    docstring)."""
    # Working view: path → (the index path it came from | None for a new file,
    # content changed?).  ``gone`` = index paths no longer where they were.
    cur: dict[str, tuple[str | None, bool]] = {}
    gone: set[str] = set()
    n = 0

    def present(rel: str) -> bool:
        return rel in cur or (rel not in gone and exists(rel))

    def take(rel: str) -> tuple[str | None, bool]:
        """Remove ``rel`` from the working view, returning its origin."""
        if rel in cur:
            return cur.pop(rel)
        gone.add(rel)
        return rel, False

    flags = iter(replay) if replay is not None else None
    for op in ops:
        n += 1
        again = bool(next(flags, False)) if flags is not None else False
        verb = op[0]
        if verb == "added":
            rel = op[1]
            if not present(rel):
                cur[rel] = (None, True)
        elif verb == "updated":
            rel = op[1]
            if present(rel):
                origin, _changed = take(rel)
                cur[rel] = (origin, True)
            else:
                cur[rel] = (None, True)                   # (listed nowhere: a new file)
        elif verb == "deleted":
            rel = op[1]
            if present(rel):
                take(rel)
        elif verb == "moved":
            src, dst = op[1], op[2]
            s, d = present(src), present(dst)
            if s and not d:
                origin, changed = take(src)
                cur[dst] = (origin, changed)
            elif d and not s:
                continue                                  # (already applied)
            elif again and s and d:
                if src in cur and cur[src][0] is None:
                    cur.pop(src)                          # (an earlier replayed add of it)
                continue
            elif again:
                continue                                  # (applied, and moved / deleted since)
            elif s and d:
                raise ChangelogError(f"moved: {src} → {dst}: both exist in the index")
            else:
                raise ChangelogError(f"moved: {src} → {dst}: neither exists in the index")
        else:
            raise ChangelogError(f"unknown operation {verb!r}")

    out = Plan(ops=n)
    sources: set[str] = set()
    for rel, (origin, changed) in sorted(cur.items()):
        if origin is None and rel in gone:
            origin, changed = rel, True        # its index file went, a new one is here: re-sized
        if origin is None:
            out.added.append(rel)
        elif origin == rel:
            if changed:
                out.updated.append(rel)
            sources.add(rel)
        else:
            out.moved[origin] = rel
            sources.add(origin)
            if changed:
                out.moved_changed.add(rel)
    out.deleted = sorted(g for g in gone if g not in sources)
    return out
