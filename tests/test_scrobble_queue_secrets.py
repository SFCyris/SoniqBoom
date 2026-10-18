# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scrobble retry queue holds no token (an item names its account; the
token is looked up when it is sent), is owner-only, drops plays of accounts
that disconnected the service, converts an older queue; and the shared HTTP
clients (scrobbling, lyrics) work again on a new event loop — the bundled
app's in-process Stop/Start."""
from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

from soniqboom.core import scrobble
from soniqboom.core import users as users_mod
from soniqboom.core.users import UserStore

PW = "Old-pass-123!"
LB = "lb-secret-token-0123456789"
FM = "fm-session-key-abcdef"


@pytest.fixture
def env(tmp_data_dir, monkeypatch):
    st = UserStore(tmp_data_dir)
    monkeypatch.setattr(users_mod, "_instance", st)
    u = st.create("bob", PW, "readonly")
    st.update(u.id, listenbrainz_token=LB, lastfm_session_key=FM)
    monkeypatch.setattr(scrobble, "LASTFM_API_KEY", "k")
    monkeypatch.setattr(scrobble, "LASTFM_API_SECRET", "s")
    class E:
        store, user, q = st, st.get(u.id), tmp_data_dir / "scrobble_queue.json"
        ok = False
        sent: list = []

    async def fake_lb(token, payload):
        E.sent.append(("lb", token))
        return E.ok

    async def fake_fm(key, method, extra):
        E.sent.append(("fm", key))
        return E.ok
    monkeypatch.setattr(scrobble, "_post_listenbrainz", fake_lb)
    monkeypatch.setattr(scrobble, "_post_lastfm", fake_fm)
    return E


TRACK = {"artist": "A", "title": "T", "album": "", "duration": 100}


def test_a_queued_play_holds_no_token_and_the_file_is_owner_only(env):
    asyncio.run(scrobble.submit_play(env.user, TRACK))
    raw = env.q.read_bytes()
    assert LB.encode() not in raw and FM.encode() not in raw
    items = json.loads(raw)
    assert {i["service"] for i in items} == {"lastfm", "listenbrainz"}
    assert all(i["uid"] == env.user.id for i in items)
    assert stat.S_IMODE(os.stat(env.q).st_mode) == 0o600


def test_the_retry_sends_with_the_accounts_token_of_now(env):
    asyncio.run(scrobble.submit_play(env.user, TRACK))
    env.store.update(env.user.id, listenbrainz_token="lb-replaced-token")
    env.sent.clear()
    env.ok = True
    assert asyncio.run(scrobble.retry_pending()) == 0
    assert sorted(env.sent) == [("fm", FM), ("lb", "lb-replaced-token")]


def test_a_disconnected_service_drops_its_queued_plays(env):
    asyncio.run(scrobble.submit_play(env.user, TRACK))
    env.store.update(env.user.id, listenbrainz_token="")        # the user cleared it
    env.sent.clear()
    left = asyncio.run(scrobble.retry_pending())
    assert ("lb", LB) not in env.sent and not any(s[0] == "lb" for s in env.sent)
    assert [i["service"] for i in json.loads(env.q.read_text())] == ["lastfm"]
    assert left == 1
    env.store.update(env.user.id, enabled=False)                # disabled: all dropped
    assert asyncio.run(scrobble.retry_pending()) == 0


def test_an_older_queue_with_tokens_inside_is_converted(env):
    old = [{"service": "listenbrainz", "token": LB, "ts": 1, "artist": "A", "track": "T",
            "album": "", "albumArtist": "", "duration": "1"},
           {"service": "lastfm", "session_key": "someone-elses-old-key", "ts": 2, "artist": "A",
            "track": "T", "album": "", "albumArtist": "", "duration": "1"}]
    env.q.write_text(json.dumps(old))
    os.chmod(env.q, 0o644)
    env.ok = False
    assert asyncio.run(scrobble.retry_pending()) == 1
    raw = env.q.read_bytes()
    assert LB.encode() not in raw and b"someone-elses-old-key" not in raw
    items = json.loads(raw)
    assert [(i["service"], i["uid"]) for i in items] == [("listenbrainz", env.user.id)]
    assert stat.S_IMODE(os.stat(env.q).st_mode) == 0o600
    assert env.sent == [("lb", LB)]


def test_the_scrobble_client_works_on_a_new_event_loop():
    async def get():
        return scrobble._client()
    a = asyncio.run(get())
    b = asyncio.run(get())                    # a new loop (the app's next server run)
    assert a is not b
    asyncio.run(scrobble.shutdown())
    assert scrobble._HTTP_CLIENT is None


def test_the_lyrics_client_works_on_a_new_event_loop():
    from soniqboom.api import tracks

    async def get():
        return tracks._get_lrclib_client()
    a = asyncio.run(get())
    b = asyncio.run(get())
    assert a is not b

    async def same():
        return tracks._get_lrclib_client() is tracks._get_lrclib_client()
    assert asyncio.run(same())
    asyncio.run(tracks.shutdown_lyrics_client())
    assert tracks._lrclib_client is None


def test_the_server_stop_closes_both_clients():
    import inspect
    from soniqboom import main
    src = inspect.getsource(main.shutdown)
    assert "_scrobble.shutdown()" in src and "shutdown_lyrics_client()" in src
