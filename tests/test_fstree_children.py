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

import pytest

from soniqboom.api import fstree


def _mock_sorted(monkeypatch, paths):
    monkeypatch.setattr(
        fstree, "_get_or_build_scan_root_sorted",
        lambda store, h: (sorted(paths), {}),
    )


# ── local ─────────────────────────────────────────────────────────────────────

def test_local_children_basic(monkeypatch):
    _mock_sorted(monkeypatch, [
        "/M/Foo/Ghost/Impera/a.mp3",
        "/M/Foo/Ghost/Meliora/b.mp3",
        "/M/Foo/Ghostemane/c.mp3",     # prefix-sibling of "Ghost"
        "/M/Foo/Ayreon/d.flac",
        "/M/Foo/top.mp3",              # a file directly in Foo — not a subdir
    ])
    kids = fstree._local_children_from_store(object(), "/M/Foo", "/M/Foo")
    assert [k["name"] for k in kids] == ["Ayreon", "Ghost", "Ghostemane"]
    assert all(k["has_audio"] for k in kids)
    assert kids[1]["path"] == "/M/Foo/Ghost"

    # Drill into Ghost: the prefix-sibling "Ghostemane" must NOT leak in.
    sub = fstree._local_children_from_store(object(), "/M/Foo/Ghost", "/M/Foo")
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
    kids = fstree._local_children_from_store(object(), "/M/Foo", "/M/Foo")
    assert [k["name"] for k in kids] == ["Bar", "Baz"]


def test_local_children_path_outside_index_returns_none(monkeypatch):
    _mock_sorted(monkeypatch, ["/M/Foo/Ghost/a.mp3"])
    # Traversal-looking path: passes the literal startswith guard but matches no
    # indexed prefix → None → caller's live path enforces resolve()/relative_to.
    assert fstree._local_children_from_store(object(), "/M/Foo/../../etc", "/M/Foo") is None
    # A real-but-unindexed subpath → None (live fallback covers it).
    assert fstree._local_children_from_store(object(), "/M/Foo/Nope", "/M/Foo") is None
    # Path not under root at all → None.
    assert fstree._local_children_from_store(object(), "/Other", "/M/Foo") is None


def test_local_children_remote_path_returns_none(monkeypatch):
    _mock_sorted(monkeypatch, ["smb://h/Foo/a.mp3"])
    assert fstree._local_children_from_store(object(), "smb://h/Foo", "smb://h") is None


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
