# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cast queue follows the skip rules (GitHub #36/#37): the first entry of a
loaded queue is the one asked for and plays; the rest are dealt, and a track
reported broken, an undecodable one or — with "Skip short tunes" on — a tune
known to be short is passed by, at load and on "next" (DLNA / AirPlay step the
queue server-side)."""
from __future__ import annotations

import pytest

from soniqboom.core import cast_session, skip_rules
from soniqboom.core.cast_session import CastSession, QueueItem
from soniqboom.core.cast_targets import CastTarget


class _Ctrl:
    def __init__(self):
        self.loaded, self.played = [], []

    async def queue_load(self, metas):
        self.loaded = [m["stream_url"] for m in metas]

    async def play(self, stream_url, **kw):
        self.played.append(stream_url)


class _Store:
    def __init__(self, tracks, secs=0):
        self._tracks, self._secs = tracks, secs

    def get_track(self, tid):
        return self._tracks.get(tid)


class _TM:
    def __init__(self, tid):
        self.id, self.path, self.title, self.artist, self.album = tid, f"/m/{tid}.mp3", tid, "", ""
        self.duration = 60.0


@pytest.fixture()
def wired(monkeypatch):
    tracks = {
        "ok1": {"id": "ok1"}, "ok2": {"id": "ok2"},
        "broken": {"id": "broken", "health": "broken"},
        "corrupt": {"id": "corrupt", "defect": "corrupt"},
        "short": {"id": "short"},
    }
    store = _Store(tracks)
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    monkeypatch.setattr(skip_rules, "short_secs", lambda s=None: 10)
    monkeypatch.setattr(skip_rules, "tune_is_short",
                        lambda t, wire, secs: bool(secs) and t.get("id") == "short")

    async def get_track(tid):
        return _TM(tid) if tid in tracks else None

    async def base():
        return "http://h"
    monkeypatch.setattr(cast_session, "get_track", get_track)
    monkeypatch.setattr(cast_session, "_server_base_url", base)
    monkeypatch.setattr(cast_session.cast_tokens, "build_stream_url",
                        lambda **kw: f"u:{kw['track_id']}")
    return tracks


def _session(proto, ctrl):
    s = CastSession(CastTarget(id="t", name="T", protocol=proto, host="h"))

    async def ensure():
        return ctrl
    s._ensure_controller = ensure
    s._caps = {"mp3"}

    async def no_prewarm(*a, **k):
        return None
    s._prewarm_lookahead = no_prewarm
    return s


@pytest.mark.asyncio
async def test_chromecast_queue_leaves_dealt_skips_out(wired):
    ctrl = _Ctrl()
    s = _session("cast", ctrl)
    await s.queue_load([QueueItem(track_id=t) for t in
                        ("broken", "ok1", "short", "corrupt", "ok2", "missing")])
    # the first (asked for) plays even though it is broken; the rest are dealt
    assert ctrl.loaded == ["u:broken", "u:ok1", "u:ok2"]
    assert [q.track_id for q in s.state.queue] == ["broken", "ok1", "ok2"]


@pytest.mark.asyncio
async def test_server_side_next_passes_entries_that_became_skippable(wired, monkeypatch):
    ctrl = _Ctrl()
    s = _session("airplay", ctrl)
    played = []

    async def play_locked(*, track_id, **kw):
        played.append(track_id)
        return {}
    s._play_track_locked = play_locked
    monkeypatch.setattr(cast_session, "_airplay_artwork_bytes", lambda tid: _none())
    await s.queue_load([QueueItem(track_id=t) for t in ("ok1", "ok2", "short")])
    assert [q.track_id for q in s.state.queue] == ["ok1", "ok2"]
    wired["ok2"]["health"] = "broken"                    # reported after the load
    assert await s.queue_next() is None                  # passed by → end of queue
    assert played == []


async def _none():
    return None


@pytest.mark.asyncio
async def test_with_the_setting_off_short_tunes_stay(wired, monkeypatch):
    monkeypatch.setattr(skip_rules, "short_secs", lambda s=None: 0)
    ctrl = _Ctrl()
    s = _session("cast", ctrl)
    await s.queue_load([QueueItem(track_id=t) for t in ("ok1", "short", "broken")])
    assert ctrl.loaded == ["u:ok1", "u:short"]


@pytest.mark.asyncio
async def test_next_and_prev_on_the_server_side_queue(wired, monkeypatch):
    """AirPlay / DLNA step the queue themselves: next lands on the next
    playable entry, stays put when none is left, and prev never runs off the
    end (it used to raise IndexError) nor lands on an entry passed by."""
    ctrl = _Ctrl()
    s = _session("airplay", ctrl)
    played = []

    async def play_locked(*, track_id, **kw):
        played.append(track_id)
        return {}
    s._play_track_locked = play_locked
    monkeypatch.setattr(cast_session, "_airplay_artwork_bytes", lambda tid: _none())
    wired["c"] = {"id": "c"}
    await s.queue_load([QueueItem(track_id=t) for t in ("ok1", "ok2", "c")])
    wired["ok2"]["health"] = "broken"
    await s.queue_next()
    assert played == ["c"] and s.state.queue_index == 2
    wired["c"]["defect"] = "corrupt"
    for _ in range(3):
        assert await s.queue_next() is None          # nothing playable left
    assert s.state.queue_index == 2                    # … and the index stays put
    await s.queue_prev()                              # past broken ok2 → ok1
    assert played[-1] == "ok1" and s.state.queue_index == 0
    await s.queue_prev()                              # at the head: restart it
    assert played[-1] == "ok1"


def test_entry_skipped_uses_the_tune_asked_for(monkeypatch):
    """The real wire → tune path (no mocks of the length rules): a SID whose
    start tune is short is passed by for a bare play, not for a long tune."""
    store = _Store({"sid": {"id": "sid", "path": "/m/x.sid", "format": "SID",
                            "subsongs": 3, "start_subsong": 1,
                            "hvsc_lengths": [100.0, 3.0, 50.0]}})
    monkeypatch.setattr(skip_rules, "short_secs", lambda s=None: 10)
    results = {w: skip_rules.entry_skipped("sid", w, store) for w in (0, 1, 2, 3)}
    assert results[0] is True                          # bare play = start tune (3 s)
    assert results[1] is False and results[3] is False
