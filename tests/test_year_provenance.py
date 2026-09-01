# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Provenance of a retro track's release year across the store, the metadata
repair path, and the manual year-edit endpoint.

The Demozoo backfill overwrites ``year`` and records where it came from
(``year_source`` demozoo/user, original in ``year_file``).  These tests pin the
invariants that keep that correction from being silently undone:

  * a rescan (``store.upsert_tracks_batch`` → ``_carry_enrichment``) must carry
    a stamped year forward, not revert it to the file's year;
  * metadata repair must not overwrite a stamped year with the file's;
  * the store-only year endpoint (the ONLY year editor retro formats have)
    stamps ``user`` and preserves the original once, and reverts stickily.
"""
from __future__ import annotations

import asyncio

import pytest


# ── store._carry_enrichment (rescan) ─────────────────────────────────────────
from soniqboom.core.store import _carry_enrichment


def test_carry_stamped_year_survives_rescan():
    old = {"artist": "Skaven", "file_md5": "abc",
           "year": 1994, "year_source": "demozoo", "year_file": 1986,
           "scene_group": "Future Crew", "scene_path": "S3M/Skaven/x.s3m"}
    new = {"artist": "Skaven", "file_md5": "abc", "year": 1986}  # fresh file extract
    _carry_enrichment(old, new)
    assert new["year"] == 1994                  # correction preserved
    assert new["year_source"] == "demozoo"
    assert new["year_file"] == 1986
    assert new["scene_group"] == "Future Crew"  # apply-only enrichment carried
    assert new["scene_path"] == "S3M/Skaven/x.s3m"


def test_carry_drops_stale_enrichment_on_identity_change():
    """A changed composer/file must NOT keep the old scene_group / demozoo year
    (no apply pass has a clear branch for those) — but a USER year survives."""
    old = {"artist": "Skaven", "file_md5": "abc",
           "year": 1994, "year_source": "demozoo", "year_file": 1986,
           "scene_group": "Future Crew", "scene_path": "S3M/Skaven/x.s3m"}
    new = {"artist": "SomeoneElse", "file_md5": "def", "year": 1986}
    _carry_enrichment(old, new)
    assert new.get("scene_group") is None       # artist changed → not carried
    assert new.get("scene_path") is None        # md5 changed → not carried
    assert new.get("year_source") in (None, "") # demozoo year dropped
    assert new["year"] == 1986                  # file year stands; next apply re-evaluates
    # ...but a USER year is a deliberate choice and survives an artist change
    old2 = {"artist": "A", "year": 1990, "year_source": "user", "year_file": 1986}
    new2 = {"artist": "B", "year": 1986}
    _carry_enrichment(old2, new2)
    assert new2["year"] == 1990 and new2["year_source"] == "user"


def test_carry_user_year_survives_rescan():
    old = {"artist": "A", "year": 1990, "year_source": "user", "year_file": 1986}
    new = {"artist": "A", "year": 1986}
    _carry_enrichment(old, new)
    assert new["year"] == 1990 and new["year_source"] == "user"


def test_carry_no_provenance_lets_file_year_win():
    old = {"year": 1986}                         # no stamp — a normal tagged file
    new = {"year": 1990}                         # user retagged the file
    _carry_enrichment(old, new)
    assert new["year"] == 1990                   # file year is authoritative
    assert new.get("year_source") in (None, "")


def test_carry_user_edited_fields_survive_rescan():
    """Store-only metadata edits (non-taggable formats) carry across a rescan's
    fresh file extract; year still rides its own provenance."""
    old = {"artist": "OldFile", "title": "Real Title", "composer": "P. Hajba",
           "user_edited": ["title", "composer"]}
    new = {"artist": "OldFile", "title": "mojibake�", "composer": ""}
    _carry_enrichment(old, new)
    assert new["title"] == "Real Title"
    assert new["composer"] == "P. Hajba"
    assert new["user_edited"] == ["title", "composer"]


def test_carry_edited_artist_does_not_drop_enrichment():
    """Regression (QA round 2): hand-editing the Artist via the store-only editor
    must NOT drop the track's scene_group / demozoo year on the next rescan.  The
    edited artist is restored BEFORE the identity check, so same_artist compares
    the user's corrected value against itself (True) rather than the file's stale
    demoscene handle — otherwise the very act of fixing the artist wiped the
    enrichment that motivated opening the editor."""
    old = {"artist": "Matthew Simmonds", "file_md5": "m1",
           "user_edited": ["artist"],
           "scene_group": "Anarchy", "scene_path": "MOD/4mat/x.mod",
           "year": 1991, "year_source": "demozoo", "year_file": None}
    new = {"artist": "4-mat", "file_md5": "m1",      # file still holds the handle
           "scene_group": None, "scene_path": None, "year": None}
    _carry_enrichment(old, new)
    assert new["artist"] == "Matthew Simmonds"       # hand edit restored
    assert new["scene_group"] == "Anarchy"           # …and enrichment KEPT
    assert new["scene_path"] == "MOD/4mat/x.mod"
    assert new["year"] == 1991 and new["year_source"] == "demozoo"
    assert new["user_edited"] == ["artist"]


def test_carry_is_idempotent_on_replay():
    """Replaying an already-merged record (AOF replay) changes nothing."""
    old = {"artist": "A", "year": 1994, "year_source": "demozoo", "year_file": 1986}
    new = {"artist": "A", "year": 1994, "year_source": "demozoo", "year_file": 1986}
    _carry_enrichment(old, new)
    assert new == {"artist": "A", "year": 1994, "year_source": "demozoo", "year_file": 1986}


# ── repair._changed_fields (metadata re-extract) ─────────────────────────────
from soniqboom.core.repair import _changed_fields


def test_repair_does_not_clobber_stamped_year():
    old = {"title": "Shades", "year": 1994, "year_source": "demozoo"}
    new = {"title": "Shades", "year": 1986}      # re-extract read the file's year
    out = _changed_fields(old, new)
    assert "year" not in out                     # provenance-stamped → left alone


def test_repair_does_not_clobber_user_year():
    old = {"year": 1990, "year_source": "user"}
    out = _changed_fields(old, {"year": 1986})
    assert "year" not in out


def test_repair_updates_unstamped_year():
    old = {"title": "Song", "year": 1986}        # no provenance
    out = _changed_fields(old, {"title": "Song", "year": 1990})
    assert out.get("year") == 1990               # a plain file year still refreshes


# ── /api/tracks/{id}/year endpoint logic ─────────────────────────────────────
from soniqboom.api import tracks as tracks_api


class _Rec:
    def __init__(self, track):
        self.applied = None
        self._track = track

    def get_track(self, tid):
        return self._track

    def update_track_fields(self, tid, updates):
        self.applied = updates


def _call_year(monkeypatch, track: dict, body_kw: dict):
    import soniqboom.core.store as store_mod
    rec = _Rec(track)
    monkeypatch.setattr(store_mod, "get_store", lambda: rec)   # imported locally in update_year
    body = tracks_api._YearUpdate(**body_kw)
    res = asyncio.run(tracks_api.update_year("t1", body, user=object()))
    return rec.applied, res


def test_year_endpoint_stamps_user_and_preserves_original(monkeypatch):
    applied, _ = _call_year(monkeypatch, {"id": "t1", "year": 1986}, {"year": 1990})
    assert applied == {"year": 1990, "year_source": "user", "year_file": 1986}


def test_year_endpoint_over_demozoo_keeps_true_original(monkeypatch):
    """Editing over a Demozoo stamp must keep the FILE year in year_file, not
    stamp the demozoo value as the 'file' year."""
    track = {"id": "t1", "year": 1994, "year_source": "demozoo", "year_file": 1986}
    applied, _ = _call_year(monkeypatch, track, {"year": 1991})
    assert applied == {"year": 1991, "year_source": "user"}   # year_file untouched


def test_year_endpoint_revert_is_sticky_user_and_keeps_anchor(monkeypatch):
    track = {"id": "t1", "year": 1994, "year_source": "demozoo", "year_file": 1986}
    applied, _ = _call_year(monkeypatch, track, {"revert": True})
    # year_file KEPT as the anchor (so a later edit/revert can recover 1986)
    assert applied == {"year": 1986, "year_source": "user"}


def test_year_endpoint_revert_refuses_unstamped(monkeypatch):
    """Revert on a track that was never overridden must NOT blank its file year."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _call_year(monkeypatch, {"id": "t1", "year": 1987}, {"revert": True})


def test_year_endpoint_rejects_out_of_range(monkeypatch):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _call_year(monkeypatch, {"id": "t1", "year": 1986}, {"year": 3005})
