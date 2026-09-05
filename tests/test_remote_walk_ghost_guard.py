# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fix G — a per-directory listing HARD-FAILURE during a remote walk must
PROTECT that subtree's tracks from ghost cleanup, WITHOUT disabling ghost
cleanup for the rest of the share.

Root cause: `_list_entries` swallows a borrow-timeout / socket / transient 5xx
to `[]` (so an interactive browse renders empty instead of crashing).  During a
FULL/drift-sweep walk that empty subtree looked *deleted*, and ghost cleanup
purged every track under it — silent subtree data loss.  The walk now threads an
`error_sink`; `_find_remote_audio_entries` returns the set of ROOT-RELATIVE
`failed_dirs`, and the scanner's ghost gate purges only ghosts NOT under a
failed subtree (so one reliably-failing directory can't wedge ghost cleanup for
the whole share forever — the second-round review finding).
"""
from soniqboom.core.filesource import DirEntry
from soniqboom.core.scanner import (
    _find_remote_audio_entries, _rel_under_any_dir, _mtime_matches_tolerant,
)


# ── MLSD(seconds)↔LIST(minutes) precision-tolerant mtime compare ───────────
def test_mtime_tolerant_same_precision_exact():
    # steady state, both MLSD seconds → exact skip; a real change re-extracts
    assert _mtime_matches_tolerant(1000.0, 1000.0) is True
    assert _mtime_matches_tolerant(1000.0, 1005.0) is False


def test_mtime_tolerant_absorbs_mlsd_to_list_transition():
    # stored MLSD 12:34:03, listed LIST 12:34:00 (minute-aligned) → SAME file,
    # must skip (else a latch transition re-extracts ~the whole share)
    base = 1773603240          # a whole-minute epoch (…:00)
    stored_mlsd = base + 3     # :03 seconds
    listed_list = base         # :00 (LIST truncates)
    assert listed_list % 60 == 0
    assert _mtime_matches_tolerant(stored_mlsd, listed_list) is True
    # reciprocal LIST→MLSD direction too
    assert _mtime_matches_tolerant(listed_list, stored_mlsd) is True


def test_mtime_tolerant_still_catches_cross_minute_retag():
    # a re-tag that crosses a minute boundary must NOT be masked
    base = 1773603240
    assert _mtime_matches_tolerant(base + 3, base + 65) is False


def test_mtime_tolerant_rejects_zero_stored():
    assert _mtime_matches_tolerant(0.0, 1000.0) is False


# ── per-subtree protection predicate (the ghost gate's core rule) ──────────
def test_rel_under_any_dir_matches_only_the_failed_subtree():
    failed = {"/lost_subdir"}
    # protected: files inside the failed subtree (any depth)
    assert _rel_under_any_dir("/lost_subdir/a.mod", failed) is True
    assert _rel_under_any_dir("/lost_subdir/deep/b.sid", failed) is True
    assert _rel_under_any_dir("/lost_subdir", failed) is True
    # purgeable: everything OUTSIDE it — including a sibling with a shared prefix
    assert _rel_under_any_dir("/other/c.mod", failed) is False
    assert _rel_under_any_dir("/lost_subdir_extra/d.mod", failed) is False  # not a path boundary


def test_rel_under_any_dir_root_failure_protects_everything():
    assert _rel_under_any_dir("/anything/x.mod", {"/"}) is True
    assert _rel_under_any_dir("/anything/x.mod", {""}) is True


def test_rel_under_any_dir_empty_set_protects_nothing():
    assert _rel_under_any_dir("/a/b.mod", set()) is False


def test_rel_under_any_dir_matches_archive_member_under_failed_dir():
    # archive-member ghost "/d/arc.zip::song.mod" whose parent dir "/d" failed
    assert _rel_under_any_dir("/d/arc.zip::song.mod", {"/d"}) is True


class _Source:
    """Minimal FileSource stand-in whose walk yields one good dir and,
    optionally, records a per-directory listing failure into error_sink."""

    def __init__(self, *, fail: bool):
        self._fail = fail

    def walk_with_stat(self, top, *, skip_subtree_fn=None, error_sink=None):
        yield "/", [], [
            DirEntry(name="a.mod", path="/a.mod", is_dir=False, size=10, mtime=5.0),
        ]
        if self._fail and error_sink is not None:
            # A second directory whose listing hard-failed and was swallowed
            # to [] inside the source — recorded here (root-relative), not raised.
            error_sink.append(("/lost_subdir", "borrow timed out"))


def _walk(fail: bool):
    return _find_remote_audio_entries(
        "ftp://h/s", _Source(fail=fail),
        dir_mtime_cap=None, scan_zips=False, archive_skip=None,
    )


def test_listing_failure_reports_failed_subtree_but_walk_completed():
    entries, _pruned, walk_completed, failed_dirs = _walk(fail=True)
    assert any(e.name == "a.mod" for e in entries)   # good dir still collected
    assert walk_completed is True                    # the walk did NOT die — it finished
    assert failed_dirs == {"/lost_subdir"}           # …but this subtree is flagged


def test_clean_walk_reports_no_failed_dirs():
    entries, _pruned, walk_completed, failed_dirs = _walk(fail=False)
    assert any(e.name == "a.mod" for e in entries)
    assert walk_completed is True
    assert failed_dirs == set()                      # no failures → ghost cleanup allowed everywhere
