# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Store-first folder-children derivation (``fstree._*_children_from_store``).

These helpers bisect a scan root's sorted-path cache to list a folder's
immediate subdirectories with zero filesystem I/O.  The cache is mocked here
so the tests exercise the pure bisect logic — including the subtree-skip
sentinel that must survive astral-plane directory names (the regression that
hung the event loop), prefix-sibling separation, and the path-safety contract
that anything not under an indexed prefix returns ``None`` (caller falls back
to a live listing that enforces ``relative_to``).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from soniqboom.api import fstree
from soniqboom.core.data import path_hash


def _mock_sorted(monkeypatch, paths):
    monkeypatch.setattr(
        fstree, "_get_or_build_scan_root_sorted",
        lambda store, h: (sorted(paths), {}),
    )


def _mock_indexes(monkeypatch, by_root: dict[str, list[str]]):
    """Each scan root's own sorted path index (root path → its tracks' paths)."""
    idx = {path_hash(r): sorted(p) for r, p in by_root.items()}
    monkeypatch.setattr(fstree, "_get_or_build_scan_root_sorted",
                        lambda store, h: (idx.get(h, []), {}))


class _Roots:
    """A store that only lists its registered scan roots (indexes are mocked)."""

    def __init__(self, *paths: str) -> None:
        self._dirs = [{"path": p, "path_hash": path_hash(p)} for p in paths]

    def list_scan_dirs(self) -> list[dict]:
        return [dict(d) for d in self._dirs]


_NO_ROOTS = _Roots()          # (the folder's root alone: the mocked index)


# ── local ─────────────────────────────────────────────────────────────────────

def test_local_children_basic(monkeypatch):
    _mock_sorted(monkeypatch, [
        "/M/Foo/Ghost/Impera/a.mp3",
        "/M/Foo/Ghost/Meliora/b.mp3",
        "/M/Foo/Ghostemane/c.mp3",     # prefix-sibling of "Ghost"
        "/M/Foo/Ayreon/d.flac",
        "/M/Foo/top.mp3",              # a file directly in Foo — not a subdir
    ])
    kids = fstree._local_children_from_store(_NO_ROOTS, "/M/Foo", "/M/Foo")
    assert [k["name"] for k in kids] == ["Ayreon", "Ghost", "Ghostemane"]
    assert all(k["has_audio"] for k in kids)
    assert kids[1]["path"] == "/M/Foo/Ghost"

    # Drill into Ghost: the prefix-sibling "Ghostemane" must NOT leak in.
    sub = fstree._local_children_from_store(_NO_ROOTS, "/M/Foo/Ghost", "/M/Foo")
    assert [k["name"] for k in sub] == ["Impera", "Meliora"]
    assert sub[0]["rel"] == "Ghost/Impera"


def test_local_children_astral_grandchild_terminates(monkeypatch):
    # Regression: a grandchild dir starting in the astral plane (emoji, CJK-Ext)
    # sorts AFTER a U+FFFF sentinel, which pinned the skip-bisect and hung the
    # event loop.  Must terminate and list the children correctly.
    _mock_sorted(monkeypatch, [
        "/M/Foo/Bar/\U0001F600sub/x.mp3",   # 😀sub
        "/M/Foo/Bar/\U0001F600sub/y.mp3",
        "/M/Foo/Baz/z.mp3",
    ])
    kids = fstree._local_children_from_store(_NO_ROOTS, "/M/Foo", "/M/Foo")
    assert [k["name"] for k in kids] == ["Bar", "Baz"]


def test_local_children_path_outside_index_returns_none(monkeypatch):
    _mock_sorted(monkeypatch, ["/M/Foo/Ghost/a.mp3"])
    # Traversal-looking path: passes the literal startswith guard but matches no
    # indexed prefix → None → caller's live path enforces resolve()/relative_to.
    assert fstree._local_children_from_store(_NO_ROOTS, "/M/Foo/../../etc", "/M/Foo") is None
    # A real-but-unindexed subpath → None (live fallback covers it).
    assert fstree._local_children_from_store(_NO_ROOTS, "/M/Foo/Nope", "/M/Foo") is None
    # Path not under root at all → None.
    assert fstree._local_children_from_store(_NO_ROOTS, "/Other", "/M/Foo") is None


# ── nested scan roots ──────────────────────────────────────────────────────────
# /M and /M/Box/CD2 both registered: a track carries the tag of the root that
# last scanned it (the scanner moves tracks between them), so a folder's
# children come from the indexes of the roots above it AND below it.

def test_local_children_list_a_folder_that_a_root_below_holds(monkeypatch):
    st = _Roots("/M", "/M/Box/CD2")
    _mock_indexes(monkeypatch, {
        "/M": ["/M/Box/CD1/a.flac", "/M/Box/CD3/c.flac", "/M/Other/o.flac"],
        "/M/Box/CD2": ["/M/Box/CD2/Disc 2/y.flac", "/M/Box/CD2/z.flac"]})
    kids = fstree._local_children_from_store(st, "/M/Box", "/M")
    assert [k["name"] for k in kids] == ["CD1", "CD2", "CD3"]
    assert (kids[1]["path"], kids[1]["rel"], kids[1]["has_audio"]) == ("/M/Box/CD2", "Box/CD2", True)
    # Only the root below holds tracks there: listed from the index, not left
    # to a walk of the disk.
    _mock_indexes(monkeypatch, {"/M": ["/M/Other/o.flac"], "/M/Box/CD2": ["/M/Box/CD2/z.flac"]})
    assert [k["name"] for k in fstree._local_children_from_store(st, "/M", "/M")] == ["Box", "Other"]
    assert [k["name"] for k in fstree._local_children_from_store(st, "/M/Box", "/M")] == ["CD2"]
    assert fstree._local_children_from_store(st, "/M/Box/CD2", "/M") == []   # (own tracks only)
    assert fstree._local_children_from_store(st, "/M/Empty", "/M") is None


def test_local_children_inside_an_inner_root_read_the_outer_root_too(monkeypatch):
    st = _Roots("/M", "/M/Box/CD2")
    _mock_indexes(monkeypatch, {"/M": ["/M/Box/CD2/Disc 1/x.flac"],
                                "/M/Box/CD2": ["/M/Box/CD2/Disc 2/y.flac"]})
    sub = fstree._local_children_from_store(st, "/M/Box/CD2", "/M/Box/CD2")
    assert [(k["name"], k["rel"]) for k in sub] == [("Disc 1", "Disc 1"), ("Disc 2", "Disc 2")]


def test_subtree_has_indexed_audio_counts_a_root_below(monkeypatch):
    # "Do not display empty folders": a folder whose only music is tagged with
    # a root below it is not empty.
    st = _Roots("/M", "/M/Box/CD2")
    _mock_indexes(monkeypatch, {"/M": ["/M/Other/o.flac"], "/M/Box/CD2": ["/M/Box/CD2/z.flac"]})
    assert fstree._subtree_has_indexed_audio(st, Path("/M/Box")) is True
    assert fstree._subtree_has_indexed_audio(st, Path("/M/Box/CD2")) is True
    assert fstree._subtree_has_indexed_audio(st, Path("/M/Empty")) is False
    assert fstree._subtree_has_indexed_audio(st, Path("/Elsewhere")) is True   # (under no root)


def test_local_root_hashes_are_the_roots_holding_a_folder_and_below_it():
    st = _Roots("/", "/M", "/M/Box/CD2", "/M/Box2", "smb://nas/M", "/Other")
    h = path_hash
    holding_box = [h("/"), h("/M")]
    assert fstree._local_root_hashes(st, "/M/Box") == (holding_box, [h("/M/Box/CD2")])
    assert fstree._local_root_hashes(st, "/M/Box/") == (holding_box, [h("/M/Box/CD2")])
    assert fstree._local_root_hashes(st, "/M/Box/CD2") == (holding_box + [h("/M/Box/CD2")], [])
    # (a share is never a local root; "/" holds every folder)
    assert fstree._local_root_hashes(st, "/") == (
        [h("/")], [h("/M"), h("/M/Box/CD2"), h("/M/Box2"), h("/Other")])
    assert fstree._local_root_hashes(_NO_ROOTS, "/M/Box") == ([], [])


def test_local_children_remote_path_returns_none(monkeypatch):
    _mock_sorted(monkeypatch, ["smb://h/Foo/a.mp3"])
    assert fstree._local_children_from_store(_NO_ROOTS, "smb://h/Foo", "smb://h") is None


# ── remote (same skip-bisect, mirror of the P0 fix) ────────────────────────────

def test_remote_children_astral_grandchild_terminates(monkeypatch):
    sr = "ftp://h/Music"
    _mock_sorted(monkeypatch, [
        f"{sr}:/Bar/\U0001F600sub/x.mp3",
        f"{sr}:/Bar/\U0001F600sub/y.mp3",
        f"{sr}:/Baz/z.mp3",
    ])
    kids = fstree._remote_children_from_store(object(), sr, "/")
    assert [k["name"] for k in kids] == ["Bar", "Baz"]


def test_remote_children_prefix_siblings(monkeypatch):
    sr = "ftp://h/Music"
    _mock_sorted(monkeypatch, [
        f"{sr}:/Ghost/Impera/a.mp3",
        f"{sr}:/Ghostemane/b.mp3",
    ])
    kids = fstree._remote_children_from_store(object(), sr, "/")
    assert [k["name"] for k in kids] == ["Ghost", "Ghostemane"]
    sub = fstree._remote_children_from_store(object(), sr, "/Ghost")
    assert [k["name"] for k in sub] == ["Impera"]


# ── order: numbers as numbers ──────────────────────────────────────────────────

def test_children_sort_numbers_as_numbers(monkeypatch):
    # A box set's discs (CD1 … CD14) list in disc order, not CD1, CD10 … CD2;
    # case doesn't matter, and the name breaks a tie (CD01 / CD1).
    names = ["CD10 Exciter", "CD2 A Broken Frame", "cd1 Speak & Spell", "CD14 Spirit",
             "CD01 Bonus", "Disc 9", "Disc 10", "Ägypten", "ラブライブ"]
    _mock_sorted(monkeypatch, [f"/M/DM/{n}/a.flac" for n in names])
    kids = fstree._local_children_from_store(_NO_ROOTS, "/M/DM", "/M")
    assert [k["name"] for k in kids] == [
        "CD01 Bonus", "cd1 Speak & Spell", "CD2 A Broken Frame", "CD10 Exciter",
        "CD14 Spirit", "Disc 9", "Disc 10", "Ägypten", "ラブライブ"]
    sr = "ftp://h/Music"
    _mock_sorted(monkeypatch, [f"{sr}:/DM/{n}/a.flac" for n in names])
    rk = fstree._remote_children_from_store(object(), sr, "/DM")
    assert [k["name"] for k in rk] == [k["name"] for k in kids]


def test_folder_sort_key_compares_any_two_names():
    # Text meets text and numbers meet numbers in every pair (no TypeError),
    # also for names that start with digits or are digits only.
    names = ["10", "2", "a10", "a2", "", "1a", "01", "x٣", "x３", "x 10 y 2", "x 10 y 10"]
    ordered = sorted(names, key=fstree._folder_sort_key)
    assert ordered.index("2") < ordered.index("10")
    assert ordered.index("a2") < ordered.index("a10")
    assert ordered.index("x 10 y 2") < ordered.index("x 10 y 10")
