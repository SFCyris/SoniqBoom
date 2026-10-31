# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``HVSC.stil_key_for`` is string work only for a track: the library apply
(``hvsc_apply``) calls it on the event loop for every SID, and a filesystem
call per track on a mounted share stalled the whole server for minutes after
a start (2026-10-28: ~60K SIDs on SMB, the documents from the online
collection — every SID outside the root took a ``realpath``)."""
from __future__ import annotations

import os

import pytest

from soniqboom.core.hvsc import HVSC


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "C64Music"
    (root / "DOCUMENTS").mkdir(parents=True)
    (root / "MUSICIANS" / "H").mkdir(parents=True)
    sid = root / "MUSICIANS" / "H" / "Tune.sid"
    sid.write_bytes(b"PSID")
    link = tmp_path / "linked"
    link.symlink_to(root)                       # the root reached through a link
    return root, link, sid


def _counting(monkeypatch):
    calls = []
    real_lstat, real_stat = os.lstat, os.stat

    def lstat(p, *a, **k):
        calls.append(("lstat", str(p)))
        return real_lstat(p, *a, **k)

    def stat(p, *a, **k):
        calls.append(("stat", str(p)))
        return real_stat(p, *a, **k)
    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os, "stat", stat)
    return calls


def test_keys_inside_and_outside_the_tree(tree, monkeypatch):
    root, link, sid = tree
    h = HVSC()
    for given in (str(root), str(link)):           # the root as configured
        monkeypatch.setattr(h, "hvsc_root", lambda g=given: g)
        assert h.stil_key_for(str(sid)) == "/MUSICIANS/H/Tune.sid"
        # (a scan of the root as configured: its tracks under that spelling)
        assert h.stil_key_for(given + "/MUSICIANS/H/Tune.sid") == "/MUSICIANS/H/Tune.sid"
        assert h.stil_key_for(str(root.parent / "elsewhere" / "x.sid")) is None
        assert h.stil_key_for(str(root) + "Extra/MUSICIANS/x.sid") is None   # a sibling's prefix
        assert h.stil_key_for(str(root / "MUSICIANS" / "H" / ".." / ".." / ".." / "x.sid")) is None
        assert h.stil_key_for(str(root / "a.zip::MUSICIANS/H/x.sid")) == "/a.zip::MUSICIANS/H/x.sid"


def test_no_filesystem_call_per_track(tree, monkeypatch):
    root, _link, _sid = tree
    h = HVSC()
    monkeypatch.setattr(h, "hvsc_root", lambda: str(root))
    h.stil_key_for(str(root / "MUSICIANS" / "H" / "Tune.sid"))   # the root is resolved once
    calls = _counting(monkeypatch)
    for i in range(500):
        h.stil_key_for(f"/mnt/share/C64Music/MUSICIANS/A/T{i}.sid")  # outside
        h.stil_key_for(str(root / "MUSICIANS" / "H" / f"T{i}.sid"))                 # inside
    assert calls == []
