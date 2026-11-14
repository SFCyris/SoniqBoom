# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a station last said it plays, kept for a listener who tunes in
between two changes of song (``stations._note_now``,
``GET /api/stations/now/{sid}``): the title a relay told every client, the
cover found for exactly that title, only while a relay of the station runs."""
from __future__ import annotations

import asyncio

import pytest

from soniqboom.api import stations as S


class _Hub:
    def __init__(self, alive: bool = True):
        self.alive = alive


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(S, "_now_playing", {})
    monkeypatch.setattr(S._hub_registry, "_hubs", {})


def _meta(sid="rb:1", title="A - B", artist="A", song="B", station="Station"):
    return {"event": "radio_meta", "sid": sid, "station": station,
            "title": title, "artist": artist, "song": song}


def _art(sid="rb:1", title="A - B", cover="/api/stations/nowplaying-art/a-b", **kw):
    return {"event": "radio_art", "sid": sid, "title": title, "artist": "A", "song": "B",
            "cover_url": cover, "album": kw.get("album", "Alb"), "year": kw.get("year", 1992),
            "label": "Lab", "source": "discogs"}


def _now(sid="rb:1") -> dict:
    return asyncio.run(S.now_playing(sid))


def _live(*keys, alive=True) -> None:
    for k in keys:
        S._hub_registry._hubs[k] = _Hub(alive)


EMPTY = {"sid": "rb:1", "station": "", "title": "", "artist": "", "song": "", "cover_url": None}


def test_the_title_then_its_cover():
    _live("rb:1#0")
    assert _now() == EMPTY                                   # nothing said yet
    S._note_now(_meta())
    assert _now() == {"sid": "rb:1", "station": "Station", "title": "A - B",
                      "artist": "A", "song": "B", "cover_url": None}
    S._note_now(_art())
    assert _now() == {"sid": "rb:1", "station": "Station", "title": "A - B",
                      "artist": "A", "song": "B",
                      "cover_url": "/api/stations/nowplaying-art/a-b",
                      "album": "Alb", "year": 1992, "label": "Lab", "source": "discogs"}


def test_another_title_drops_the_cover_and_a_late_cover_is_refused():
    _live("rb:1#0")
    S._note_now(_meta())
    S._note_now(_art())
    S._note_now(_meta(title="C - D", artist="C", song="D"))
    assert _now()["title"] == "C - D" and _now()["cover_url"] is None
    assert "album" not in _now()
    S._note_now(_art())                                      # of "A - B": too late
    assert _now()["cover_url"] is None
    S._note_now(_art(title="C - D", cover="/api/art/89ef/lg.jpg"))
    assert _now()["cover_url"] == "/api/art/89ef/lg.jpg"


def test_the_same_title_said_again_keeps_its_cover():
    _live("rb:1#0")
    S._note_now(_meta())
    S._note_now(_art())
    S._note_now(_meta())                                     # the relay reconnected
    assert _now()["cover_url"] == "/api/stations/nowplaying-art/a-b"


def test_a_cover_before_any_title_and_events_without_sid_or_title_are_nothing():
    _live("rb:1#0")
    S._note_now(_art())
    S._note_now({"event": "radio_meta", "sid": "", "title": "A - B"})
    S._note_now({"event": "radio_meta", "sid": "rb:1", "title": ""})
    S._note_now({"event": "radio_meta", "sid": "rb:1"})
    assert S._now_playing == {} and _now() == EMPTY


def test_only_while_a_relay_of_the_station_runs():
    S._note_now(_meta())
    assert _now() == EMPTY                                   # no relay at all
    _live("rb:1#0", alive=False)
    assert _now() == EMPTY                                   # one that ended
    _live("rb:10#0", "rb:1#x", "rb:1#0#0", "rb:1")           # other stations' keys
    assert _now() == EMPTY
    _live("rb:1#2")                                          # any stream of it
    assert _now()["title"] == "A - B"


def test_stations_do_not_mix():
    _live("rb:1#0", "rb:2#0")
    S._note_now(_meta())
    S._note_now(_meta(sid="rb:2", title="E - F", artist="E", song="F", station="Two"))
    S._note_now(_art(sid="rb:2"))                            # "A - B" is not what rb:2 plays
    assert _now("rb:2") == {"sid": "rb:2", "station": "Two", "title": "E - F",
                            "artist": "E", "song": "F", "cover_url": None}
    assert _now("rb:1")["title"] == "A - B"


def test_the_oldest_station_goes_first_once_full(monkeypatch):
    monkeypatch.setattr(S, "_NOW_MAX", 3)
    for i in range(3):
        S._note_now(_meta(sid=f"s{i}", title=f"t{i}"))
    S._note_now(_meta(sid="s0", title="again"))              # s0 is the newest now
    S._note_now(_meta(sid="s3", title="t3"))
    assert list(S._now_playing) == ["s2", "s0", "s3"]
    assert len(S._now_playing) == 3


def test_every_broadcast_is_noted(monkeypatch):
    sent = []

    async def _b(payload):
        sent.append(payload["event"])
    from soniqboom.api import library, multiroom
    monkeypatch.setattr(library, "_broadcast", _b)
    monkeypatch.setattr(multiroom, "notify_station_meta", _b)
    _live("rb:1#0")
    asyncio.run(S._broadcast_meta(_meta()))
    asyncio.run(S._broadcast_meta(_art()))
    assert sent == ["radio_meta", "radio_meta", "radio_art", "radio_art"]
    assert _now()["cover_url"] == "/api/stations/nowplaying-art/a-b"


# ── A relay that starts ─────────────────────────────────────────────────────

class _StartedHub:
    media_type = "audio/ogg"
    headers: dict = {}
    alive = True

    def __init__(self, key, *a, **k):
        self.key = key

    async def start(self):
        pass

    async def stream(self):
        yield b""


@pytest.fixture
def station(monkeypatch):
    async def _resolve(sid):
        return {"sid": sid, "name": "Station",
                "streams": [{"url": "http://example.org/a"}, {"url": "http://example.org/b"}]}

    async def _public(url):
        return None
    monkeypatch.setattr(S.radiodir, "resolve_station", _resolve)
    monkeypatch.setattr(S, "_assert_public_url", _public)
    monkeypatch.setattr(S, "StationHub", _StartedHub)
    monkeypatch.setattr(S._hub_registry, "_creating", {})


def test_a_first_relay_forgets_what_an_earlier_one_said(station):
    S._note_now(_meta())                                     # (of a relay long gone)
    S._note_now(_meta(sid="rb:2", title="E - F"))

    async def go():
        await S.relay("rb:1", 0)
    asyncio.run(go())
    assert "rb:1" not in S._now_playing and "rb:2" in S._now_playing
    assert "rb:1#0" in S._hub_registry._hubs


def test_a_second_stream_of_a_playing_station_keeps_it(station):
    _live("rb:1#0")
    S._note_now(_meta())

    async def go():
        await S.relay("rb:1", 1)
    asyncio.run(go())
    assert S._now_playing["rb:1"]["title"] == "A - B"
    assert _now()["title"] == "A - B"
