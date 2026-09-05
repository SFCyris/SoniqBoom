# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the FTP MLSD-latch bug family (filesource.py).

Root cause (fixed): a 550 on a bogus archive-internal path ("zip::member")
permanently disabled MLSD for the WHOLE source — ftplib raises error_perm for
every 5xx, and the old handler treated any error_perm as "server lacks MLSD".
Every later listing then fell to LIST, whose parser reports mtime=0, defeating
the scanner's incremental skip. These lock the fixes:

  A  — MLSD disabled ONLY on FTP reply codes 500/502 (`_mlsd_unsupported`).
  B  — archive-internal "::" paths are never sent to FTP (`_list_entries` → []).
  C' — a latched source re-probes MLSD after `_MLSD_REPROBE_S`; reconnect clears
       the latch immediately.
  D  — the dead `self._reset()` calls are gone, so an MLSD failure falls back to
       LIST in the same call instead of raising AttributeError.

Post-review hardening (v1.9.2):

  J  — the "::" guard fires ONLY for genuine archive / disk-image containers,
       so a benign directory named e.g. "Artist :: Album" still lists + scans.
  K  — MLSD latches only after `_MLSD_LATCH_STREAK` (2) CONSECUTIVE 500/502s,
       so one spurious "500 OOPS" / desync no longer degrades the whole source;
       `_MLSD_REPROBE_S` shortened to 30 min so a false latch self-heals fast.
  L  — the LIST parser now derives a real mtime from the date columns, so a
       genuinely MLSD-less server still gets incremental (mtime,size) skip.
  G  — a per-directory listing HARD-FAILURE is signalled via `error_sink` so a
       partial walk can be told apart from a genuinely-empty dir (ghost-cleanup
       data-loss guard).
"""
import contextlib
import ftplib
import time

import pytest

from soniqboom.core import filesource as F
from soniqboom.core.filesource import DirEntry, FTPFileSource


def _raise(exc):
    raise exc


def _src():
    # Construction is connection-free (the pool is lazy) — safe with a bogus host.
    return FTPFileSource("nonexistent.invalid", "u", "p", 21, "/")


def _wire(src):
    """Shadow the two real listing methods so _list_entries never touches the
    pool; record which lane was taken."""
    calls = []
    src._list_via_mlsd = lambda ap, lane="scan": (calls.append("MLSD") or
        [DirEntry(name="m", path=ap + "/m", is_dir=False, size=1, mtime=111.0)])
    src._list_via_list = lambda ap, lane="scan": (calls.append("LIST") or
        [DirEntry(name="l", path=ap + "/l", is_dir=False, size=1, mtime=0.0)])
    return calls


# ── A: latch predicate — only 500/502 mean "MLSD unsupported" ──────────────
@pytest.mark.parametrize("msg,want", [
    ("500 Syntax error, command unrecognized", True),
    ("502 Command not implemented", True),
    ("501 Syntax error in parameters or arguments", False),
    ("504 Command not implemented for that parameter", False),
    ("530 Not logged in", False),
    ("550 No such file or directory", False),
    ("550 Failed to change directory", False),
])
def test_mlsd_unsupported_only_500_502(msg, want):
    assert F._mlsd_unsupported(ftplib.error_perm(msg)) is want


def test_mlsd_unsupported_ignores_non_error_perm():
    assert F._mlsd_unsupported(ftplib.error_temp("425 Address already in use")) is False
    assert F._mlsd_unsupported(TimeoutError("borrow timed out")) is False
    assert F._mlsd_unsupported(OSError("connection reset")) is False
    assert F._mlsd_unsupported(ValueError("weird")) is False


# ── B: archive-internal "::" paths are never FTP-listed ────────────────────
def test_archive_internal_path_returns_empty_without_ftp():
    src = _src()
    calls = _wire(src)
    assert src._list_entries("music/disks/foo.zip::Batthew Musicdisk/songs") == []
    assert calls == []            # neither MLSD nor LIST attempted → no 550 → no latch
    assert src._use_mlsd is True  # the exact trigger that broke the share is inert


# ── D + A: a 550 must NOT latch; MLSD→LIST fallback runs in the same call ──
def test_550_does_not_latch_and_falls_back_to_list():
    src = _src()
    seen = []
    src._list_via_mlsd = lambda ap, lane="scan": _raise(ftplib.error_perm("550 No such file"))
    src._list_via_list = lambda ap, lane="scan": (seen.append("LIST") or [])
    out = src._list_entries("/real/dir")   # must NOT raise (dead _reset removed → no AttributeError)
    assert out == []
    assert src._use_mlsd is True           # A: a 550 does not disable MLSD
    assert seen == ["LIST"]                # D: fell back to LIST in-call


# ── K + C': TWO consecutive 502s latch (one spurious 500 does not) ─────────
def test_single_502_does_not_latch():
    # A one-off desync/"500 OOPS" must be absorbed by the per-call LIST
    # fallback, NOT flip the whole source to LIST.
    src = _src()
    src._list_via_mlsd = lambda ap, lane="scan": _raise(ftplib.error_perm("500 OOPS: priv_sock_get_result"))
    src._list_via_list = lambda ap, lane="scan": []
    src._list_entries("/real/dir")
    assert src._use_mlsd is True           # K: one 500 does not latch
    assert src._mlsd_fail_streak == 1
    assert src._mlsd_disabled_at == 0.0


def test_two_consecutive_502_latch_and_stamp_reprobe_clock():
    src = _src()
    src._list_via_mlsd = lambda ap, lane="scan": _raise(ftplib.error_perm("502 Command not implemented"))
    src._list_via_list = lambda ap, lane="scan": []
    assert src._mlsd_disabled_at == 0.0
    src._list_entries("/real/dir")         # streak → 1, not yet latched
    assert src._use_mlsd is True
    src._list_entries("/real/dir2")        # streak → 2 → latch
    assert src._use_mlsd is False
    assert src._mlsd_disabled_at > 0.0     # C': clock stamped so it can re-probe later


def test_mlsd_success_resets_the_500_streak():
    # A 500 followed by a healthy MLSD listing must clear the streak so two
    # spurious-but-separated 500s never accumulate into a false latch.
    src = _src()
    box = {"n": 0}

    def _mlsd(ap, lane="scan"):
        box["n"] += 1
        if box["n"] in (1, 3):             # calls 1 and 3 fail, call 2 succeeds
            raise ftplib.error_perm("500 OOPS")
        return [DirEntry(name="m", path=ap + "/m", is_dir=False, size=1, mtime=9.0)]

    src._list_via_mlsd = _mlsd
    src._list_via_list = lambda ap, lane="scan": []
    src._list_entries("/d1")               # 500 → streak 1
    assert src._mlsd_fail_streak == 1
    src._list_entries("/d2")               # MLSD ok → streak 0
    assert src._mlsd_fail_streak == 0 and src._use_mlsd is True
    src._list_entries("/d3")               # 500 → streak 1 (never reached 2)
    assert src._use_mlsd is True and src._mlsd_fail_streak == 1


def test_transient_error_does_not_latch():
    src = _src()
    src._list_via_mlsd = lambda ap, lane="scan": _raise(TimeoutError("borrow timeout"))
    src._list_via_list = lambda ap, lane="scan": []
    src._list_entries("/real/dir")
    src._list_entries("/real/dir2")        # even repeated → still not a latch
    assert src._use_mlsd is True           # transient → MLSD stays enabled
    assert src._mlsd_fail_streak == 0      # a non-500/502 never latches


def test_transient_between_two_500s_breaks_the_consecutive_run():
    # 500 → socket-blip → 500 must NOT latch: a truly MLSD-less server emits
    # 500 EVERY time; an intervening non-500 proves the 500s weren't consecutive.
    src = _src()
    box = {"n": 0}

    def _mlsd(ap, lane="scan"):
        box["n"] += 1
        if box["n"] == 2:
            raise TimeoutError("socket blip")           # the interruption
        raise ftplib.error_perm("500 OOPS")             # calls 1 and 3

    src._list_via_mlsd = _mlsd
    src._list_via_list = lambda ap, lane="scan": []
    src._list_entries("/d1")               # 500 → streak 1
    assert src._mlsd_fail_streak == 1
    src._list_entries("/d2")               # socket blip → streak reset to 0
    assert src._mlsd_fail_streak == 0
    src._list_entries("/d3")               # 500 → streak 1 (never reached 2)
    assert src._use_mlsd is True and src._mlsd_fail_streak == 1


def test_reprobe_window_is_short():
    # A false latch must self-heal in well under the old 6h — guard the knob.
    assert F._MLSD_REPROBE_S <= 60 * 60
    assert F._MLSD_LATCH_STREAK >= 2


# ── C': periodic re-probe of a latched source ──────────────────────────────
def test_cold_latch_stays_on_list():
    src = _src()
    calls = _wire(src)
    src._use_mlsd = False
    src._mlsd_disabled_at = time.time()    # just latched — well within the window
    src._list_entries("/real/dir")
    assert calls == ["LIST"]
    assert src._use_mlsd is False          # no premature re-probe


def test_expired_latch_reprobes_mlsd():
    src = _src()
    calls = _wire(src)
    src._use_mlsd = False
    src._mlsd_disabled_at = time.time() - (F._MLSD_REPROBE_S + 60)
    src._list_entries("/real/dir")
    assert src._use_mlsd is True           # re-probed
    assert calls == ["MLSD"]


def test_torn_latch_pair_reprobes_to_recover():
    # The inconsistent pair (_use_mlsd=False, _mlsd_disabled_at=0) can only
    # arise from a torn concurrent write; it must be treated as "re-probe due"
    # so the share self-heals instead of wedging on LIST forever (2nd-round
    # concurrency finding).  A successful re-probe restores the consistent trio.
    src = _src()
    calls = _wire(src)
    src._use_mlsd = False
    src._mlsd_disabled_at = 0.0
    src._list_entries("/real/dir")
    assert calls == ["MLSD"]                # re-probed instead of wedging on LIST
    assert src._use_mlsd is True
    assert src._mlsd_fail_streak == 0 and src._mlsd_disabled_at == 0.0


def test_mlsd_success_restores_consistent_trio():
    # _mark_mlsd_ok must re-assert _use_mlsd=True (not just clear counters) so a
    # concurrent latch/success race can never settle on the wedged (False, 0).
    src = _src()
    _wire(src)
    src._use_mlsd = True
    src._mlsd_fail_streak = 1
    src._mlsd_disabled_at = 123.0          # a stale/torn timestamp
    src._list_entries("/real/dir")         # MLSD succeeds
    assert src._use_mlsd is True and src._mlsd_fail_streak == 0
    assert src._mlsd_disabled_at == 0.0


# ── C': reconnect clears the latch immediately ─────────────────────────────
def test_reconnect_clears_latch_and_reprobe_clock(monkeypatch):
    src = _src()
    pool = src._pool                       # lazy pool object; created, not connected
    monkeypatch.setattr(pool, "recycle_all_idle", lambda: None)

    @contextlib.contextmanager
    def _fake_borrow(lane="scan"):
        yield object()                     # a health-probe borrow that never connects

    monkeypatch.setattr(pool, "borrow", _fake_borrow)
    src._use_mlsd = False
    src._mlsd_disabled_at = time.time()
    src._mlsd_fail_streak = 5
    assert src.reconnect() is True
    assert src._use_mlsd is True
    assert src._mlsd_disabled_at == 0.0
    assert src._mlsd_fail_streak == 0


# ── J: the "::" guard fires ONLY for genuine archive / disk-image interiors ─
@pytest.mark.parametrize("path", [
    "music/disks/foo.zip::Batthew Musicdisk/songs",   # ZIP interior
    "/x/outer.zip::inner.zip::track.mod",             # nested archive
    "/games/disk.d64::THE RUNNER.sid",                # C64 disk image
    "/amiga/mod.lha::mods/song.mod",                  # LHA interior
])
def test_double_colon_archive_paths_are_not_ftp_listed(path):
    src = _src()
    calls = _wire(src)
    assert src._list_entries(path) == []
    assert calls == []                     # never hit the wire → no 550 → no latch
    assert src._use_mlsd is True


@pytest.mark.parametrize("path", [
    "Artist :: Album",                     # human folder-naming style
    "/Live :: 1998",
    "/Best of :: Vol 2/tracks",
])
def test_double_colon_directory_names_ARE_listed(path):
    # A benign directory whose NAME contains "::" is a real, listable server
    # directory — it must NOT be short-circuited (that hid the whole subtree).
    src = _src()
    calls = _wire(src)
    out = src._list_entries(path)
    assert calls == ["MLSD"]               # J: fell through to a real listing
    assert out and out[0].name == "m"


# ── L: LIST date columns parse to a real, stable mtime ─────────────────────
def test_parse_list_mtime_unix_year_form():
    ts = FTPFileSource._parse_list_mtime_unix("Jan", "15", "2024")
    import datetime
    assert ts == datetime.datetime(2024, 1, 15).timestamp()


def test_parse_list_mtime_unix_time_form_keeps_minute_precision():
    # "MMM DD HH:MM" (no year) → a recent file; we KEEP the real time so a
    # same-day same-size re-tag is detected while the file is recent (its
    # minute changes).  Year inferred: this year, or last if that's future.
    import datetime
    now = datetime.datetime.now()
    ts = FTPFileSource._parse_list_mtime_unix("Jan", "15", "12:34")
    got = datetime.datetime.fromtimestamp(ts)
    assert got.month == 1 and got.day == 15
    assert got.hour == 12 and got.minute == 34        # real time preserved
    assert got.year in (now.year, now.year - 1)
    assert got <= now + datetime.timedelta(days=1)


def test_parse_list_unix_retag_shifts_mtime_while_recent():
    # A same-day re-tag while the file is still recent shifts the parsed mtime
    # by the minute delta → past the 2s skip tolerance → correctly re-extracted
    # (the property date-only would have SILENTLY lost).
    a = FTPFileSource._parse_list_mtime_unix("Jan", "15", "12:34")
    b = FTPFileSource._parse_list_mtime_unix("Jan", "15", "18:05")
    assert abs(a - b) > 2.0


def test_parse_list_mtime_unix_bad_inputs_return_zero():
    assert FTPFileSource._parse_list_mtime_unix("Xxx", "15", "2024") == 0.0
    assert FTPFileSource._parse_list_mtime_unix("Jan", "notaday", "2024") == 0.0
    assert FTPFileSource._parse_list_mtime_unix("Jan", "15", "garbage") == 0.0


def test_parse_list_mtime_windows():
    import datetime
    ts = FTPFileSource._parse_list_mtime_windows("01-15-24", "12:34PM")
    assert ts == datetime.datetime(2024, 1, 15, 12, 34).timestamp()
    assert FTPFileSource._parse_list_mtime_windows("bad", "bad") == 0.0


def test_parse_list_line_carries_nonzero_mtime_and_is_stable():
    line = "-rw-r--r-- 1 user group 4096 Jan 15 2024 track.mod"
    e1 = FTPFileSource._parse_list_line(line, "/d")
    e2 = FTPFileSource._parse_list_line(line, "/d")
    assert e1 is not None and e1.name == "track.mod" and e1.size == 4096
    assert e1.mtime > 0
    assert e1.mtime == e2.mtime            # deterministic → skip works poll-to-poll


def test_parse_list_line_unparseable_date_is_zero_not_a_crash():
    # A weird/locale date must degrade to mtime=0 (old behaviour), never raise.
    line = "-rw-r--r-- 1 user group 4096 ??? ?? ????? track.mod"
    e = FTPFileSource._parse_list_line(line, "/d")
    assert e is not None and e.mtime == 0.0


# ── G: a per-directory hard failure is signalled via error_sink ────────────
def test_list_entries_hard_failure_appends_to_error_sink():
    src = _src()
    src._list_via_mlsd = lambda ap, lane="scan": _raise(TimeoutError("borrow timeout"))
    src._list_via_list = lambda ap, lane="scan": _raise(TimeoutError("borrow timeout"))
    sink: list = []
    out = src._list_entries("/real/dir", error_sink=sink)
    assert out == []                       # still swallowed → browse renders empty
    assert len(sink) == 1                  # …but the walk can SEE the failure


def test_genuinely_empty_dir_does_not_touch_error_sink():
    src = _src()
    src._list_via_mlsd = lambda ap, lane="scan": []   # MLSD ok, dir is just empty
    src._list_via_list = lambda ap, lane="scan": []
    sink: list = []
    assert src._list_entries("/empty", error_sink=sink) == []
    assert sink == []                      # empty != failed


def test_archive_guard_does_not_touch_error_sink():
    src = _src()
    _wire(src)
    sink: list = []
    assert src._list_entries("/x/foo.zip::m", error_sink=sink) == []
    assert sink == []                      # refusing an archive interior is not a failure
