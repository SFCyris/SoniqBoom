# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scrobble retry pass removes exactly the items it settled: a play
queued while the pass runs is neither lost nor duplicated."""
import asyncio
import json

import pytest

from soniqboom.core import scrobble


def _item(n, service="lastfm"):
    # (a queued play names its account; the token is looked up when it is sent)
    return {"service": service, "uid": "u1", "ts": 1700000000 + n, "artist": f"A{n}",
            "track": f"T{n}", "album": "", "albumArtist": "", "duration": "60"}


@pytest.fixture
def q(tmp_data_dir, monkeypatch):
    # (the locks are per event loop: each test's asyncio.run gets its own)
    monkeypatch.setattr(scrobble, "_service_token", lambda uid, service: "k")
    return tmp_data_dir / "scrobble_queue.json"


def _queue(path):
    return json.loads(path.read_text())


def test_items_queued_during_the_pass_survive_exactly_once(q, monkeypatch):
    sent = []

    async def fake_lastfm(session_key, method, extra):
        artist = extra["artist[0]"]
        sent.append(artist)
        if artist == "A1":
            # a play fails while the pass is under way → queued meanwhile
            await scrobble._enqueue(_item(100))
            await scrobble._enqueue(_item(101))
            return True                                  # A1 sent
        if artist == "A2":
            return False                                 # still failing
        raise scrobble._PermanentScrobbleError("revoked")  # A3 dropped
    monkeypatch.setattr(scrobble, "_post_lastfm", fake_lastfm)

    async def run():
        for n in (1, 2, 3):
            await scrobble._enqueue(_item(n))
        return await scrobble.retry_pending()
    left = asyncio.run(run())
    items = _queue(q)
    assert [i["artist"] for i in items] == ["A2", "A100", "A101"]
    assert left == 3
    assert len({i["qid"] for i in items}) == 3
    assert sent == ["A1", "A2", "A3"]                    # the new ones wait for the next pass


def test_a_concurrent_submit_during_the_pass_is_kept(q, monkeypatch):
    gate = asyncio.Event
    state = {}

    async def fake_lastfm(session_key, method, extra):
        state["in_pass"].set()
        await state["release"].wait()
        return True
    monkeypatch.setattr(scrobble, "_post_lastfm", fake_lastfm)

    async def run():
        state["in_pass"], state["release"] = gate(), gate()
        await scrobble._enqueue(_item(1))
        task = asyncio.create_task(scrobble.retry_pending())
        await state["in_pass"].wait()                    # the pass is mid-network-call
        await scrobble._enqueue(_item(2))                # another task queues a play
        state["release"].set()
        return await task
    assert asyncio.run(run()) == 1
    assert [i["artist"] for i in _queue(q)] == ["A2"]


def test_legacy_items_without_an_id_are_handled(q, monkeypatch):
    q.write_text(json.dumps([_item(1), _item(2)]))       # written before ``qid``

    async def fake_lastfm(session_key, method, extra):
        if extra["artist[0]"] == "A1":
            await scrobble._enqueue(_item(9))
            return True
        return False
    monkeypatch.setattr(scrobble, "_post_lastfm", fake_lastfm)
    assert asyncio.run(scrobble.retry_pending()) == 2
    assert [i["artist"] for i in _queue(q)] == ["A2", "A9"]


def test_overlapping_passes_never_send_an_item_twice(q, monkeypatch):
    sent = []

    async def fake_lastfm(session_key, method, extra):
        sent.append(extra["artist[0]"])
        await asyncio.sleep(0.01)
        return True
    monkeypatch.setattr(scrobble, "_post_lastfm", fake_lastfm)

    async def run():
        for n in range(5):
            await scrobble._enqueue(_item(n))
        return await asyncio.gather(scrobble.retry_pending(), scrobble.retry_pending())
    assert asyncio.run(run()) == [0, 0]
    assert sorted(sent) == [f"A{n}" for n in range(5)]
    assert _queue(q) == []


def test_a_pass_that_ends_early_still_removes_what_it_sent(q, monkeypatch):
    async def fake_lastfm(session_key, method, extra):
        if extra["artist[0]"] == "A2":
            raise RuntimeError("bug mid-pass")
        return True
    monkeypatch.setattr(scrobble, "_post_lastfm", fake_lastfm)

    async def run():
        for n in (1, 2, 3):
            await scrobble._enqueue(_item(n))
        with pytest.raises(RuntimeError):
            await scrobble.retry_pending()
    asyncio.run(run())
    assert [i["artist"] for i in _queue(q)] == ["A2", "A3"]


def test_the_locks_work_on_a_second_event_loop(q, monkeypatch):
    """The bundled app runs each server run on a new event loop in the same
    process: contended retry passes and enqueues on a second loop must not
    hit a lock bound to the first ("bound to a different event loop")."""
    async def slow_lastfm(session_key, method, extra):
        await asyncio.sleep(0.02)
        return False
    monkeypatch.setattr(scrobble, "_post_lastfm", slow_lastfm)

    async def contend():
        await scrobble._enqueue(_item(1))
        await asyncio.gather(scrobble.retry_pending(), scrobble.retry_pending(),
                             scrobble._enqueue(_item(2)), scrobble._enqueue(_item(3)))
    asyncio.run(contend())            # loop 1: both locks contended (bound to loop 1)
    asyncio.run(contend())            # loop 2
    assert [i["artist"] for i in _queue(q)] == ["A1", "A2", "A3", "A1", "A2", "A3"]
