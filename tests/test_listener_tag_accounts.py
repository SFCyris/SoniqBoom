# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The listener a Subsonic or cast stream plays for is its account (and cast
target), not only its address + User-Agent: two accounts using the same app
from one address are two listeners (``stream._client_tag``)."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi.responses import Response

from soniqboom.api import stream


def _req(ua="Symfonium/12.3 (Android 15)", host="192.168.1.20"):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers={"user-agent": ua},
                           method="GET", query_params={})


async def _subsonic_tag(monkeypatch, user, req):
    from soniqboom.api import subsonic
    got = {}

    async def fake_stream_track(**kw):
        got.update(kw)
        return Response(b"x")
    monkeypatch.setattr(stream, "stream_track", fake_stream_track)
    await subsonic._serve_stream(req, "t1", target_format=None, max_bitrate_kbps=0,
                                 target_sample_rate=0, seek=0.0, user=user)
    return stream._client_tag(req, got["sb_session"], got["u"])


async def test_two_subsonic_accounts_on_one_address_are_two_listeners(monkeypatch):
    alice = SimpleNamespace(id="u1", username="alice")
    bob = SimpleNamespace(id="u2", username="bob")
    req = _req()
    a1 = await _subsonic_tag(monkeypatch, alice, req)
    a2 = await _subsonic_tag(monkeypatch, alice, _req())
    b = await _subsonic_tag(monkeypatch, bob, _req())
    assert a1 == a2                     # one account, one device: one listener
    assert a1 != b
    # the device still counts: the same account on another player is another listener
    assert await _subsonic_tag(monkeypatch, alice, _req(ua="DSub/5.5")) != a1


async def test_cast_streams_are_tagged_by_their_user_and_target(monkeypatch):
    from soniqboom.api import cast_stream as cs
    tags = []

    async def fake_stream_track(**kw):
        tags.append(stream._client_tag(kw["request"], kw["sb_session"], kw["u"]))
        return Response(b"x")
    monkeypatch.setattr(stream, "stream_track", fake_stream_track)
    track = SimpleNamespace(id="t1", path="/music/a.flac", format="FLAC")

    async def fake_authorize(token, request, bind=True):
        uid, tg = token.split("/")
        return {"tid": "t1", "uid": uid, "tg": tg}, track, "t1"

    async def fake_open(track_id, t):
        return t
    monkeypatch.setattr(cs, "_authorize_cast", fake_authorize)
    monkeypatch.setattr(cs, "_open_if_package", fake_open)
    req = _req(ua="Chromecast/1.0", host="192.168.1.50")
    for tok in ("u1/tvA", "u1/tvA", "u2/tvA", "u1/tvB"):
        await cs.cast_stream(tok, "a.flac", req)
    assert tags[0] == tags[1]
    assert len({tags[0], tags[2], tags[3]}) == 3
