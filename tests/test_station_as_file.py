# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The station relay answered as a file (``stations._AS_FILE_SIZE``): WebKit
gives the page's audio graph the samples of a file, not of an endless
stream."""
from __future__ import annotations

import asyncio

from soniqboom.api import stations as S

# MPEG-1 Layer III, 128 kbit/s, 44.1 kHz, no padding: 417 bytes a frame.
HDR = bytes([0xFF, 0xFB, 0x90, 0x44])


def _frame(payload: bytes = b"") -> bytes:
    return HDR + (payload + bytes(417))[:413]


class _Hub:
    def __init__(self, chunks, media_type="audio/mpeg"):
        self.chunks = list(chunks)
        self.media_type = media_type
        self.headers = {"X-Station-Name": "Test"}
        self.subscribers = 0
        self.released = 0
        self.burst_size = 0

    def release_unused(self):
        self.released += 1

    async def stream(self):
        self.subscribers += 1
        try:
            i = 0
            while True:                         # endless, like a station
                yield self.chunks[i % len(self.chunks)]
                i += 1
        finally:
            self.subscribers -= 1


async def _read(resp, limit: int) -> bytes:
    out = b""
    it = resp.body_iterator
    try:
        async for chunk in it:
            out += chunk
            if len(out) >= limit:
                break
    finally:
        await it.aclose()
    return out


def test_mp3_frame_length():
    assert S._mp3_frame_len(HDR) == 417
    assert S._mp3_frame_len(bytes([0xFF, 0xFB, 0x92, 0x44])) == 418      # padded
    assert S._mp3_frame_len(b"ID3\x04") == 0 and S._mp3_frame_len(b"\xff") == 0


async def _gen(chunks):
    for c in chunks:
        yield c


async def _collect(agen) -> bytes:
    return b"".join([c async for c in agen])


def test_a_first_vbr_header_frame_is_dropped():
    xing = _frame(b"\x00" * 32 + b"Xing")
    audio = _frame(b"\x11") + _frame(b"\x22")
    for split in (1, 7, 417, 5000):             # however the stream is cut
        data = xing + audio
        chunks = [data[i:i + split] for i in range(0, len(data), split)]
        assert asyncio.run(_collect(S._without_vbr_header(_gen(chunks)))) == audio
    plain = _frame(b"\x33") + audio
    assert asyncio.run(_collect(S._without_vbr_header(_gen([plain])))) == plain
    # an ID3v2 tag, then a LAME "Info" frame (a stream that began as a file)
    id3 = b"ID3\x04\x00\x00\x00\x00\x00\x3c" + bytes(60)
    info = _frame(b"\x00" * 32 + b"Info")
    for split in (1, 3, 9, 70, 4096):
        data = id3 + info + audio
        chunks = [data[i:i + split] for i in range(0, len(data), split)]
        assert asyncio.run(_collect(S._without_vbr_header(_gen(chunks)))) == audio


def test_ranges_of_the_file():
    async def go():
        size = S._AS_FILE_SIZE
        hub = _Hub([_frame(b"\x44"), _frame(b"\x55")])
        r = S._as_file_response("bytes=0-1", hub)
        assert r.status_code == 206 and r.headers["content-range"] == f"bytes 0-1/{size}"
        assert r.headers["accept-ranges"] == "bytes" and r.headers["content-length"] == "2"
        assert await _read(r, 10) == HDR[:2]
        assert hub.subscribers == 0                         # the listener let go
        r = S._as_file_response("bytes=0-", hub)
        assert r.headers["content-length"] == str(size)
        body = await _read(r, 2000)
        assert body.startswith(HDR) and hub.subscribers == 0
        # the end of the "file": zeros at once, no listener
        r = S._as_file_response(f"bytes={size - 128}-", hub)
        assert r.status_code == 206 and r.body == bytes(128)
        assert hub.released == 1            # no listener: the hub may close
        r = S._as_file_response("bytes=-10", hub)
        assert r.headers["content-range"] == f"bytes {size - 10}-{size - 1}/{size}"
        assert r.body == bytes(10)
        assert S._as_file_response(f"bytes={size}-", hub).status_code == 416
        assert hub.released == 3
        r = S._as_file_response(None, hub)
        assert r.status_code == 200 and r.headers["content-length"] == str(size)
        assert "content-range" not in r.headers and r.media_type == "audio/mpeg"
        await _read(r, 10)
    asyncio.run(go())


def test_only_mp3_and_aac_stations_become_files(monkeypatch):
    async def go():
        for mtype, as_file in (("audio/mpeg", True), ("audio/aacp", True),
                               ("audio/ogg", False), ("application/ogg", False)):
            hub = _Hub([b"\x00" * 100], media_type=mtype)

            async def _goc(key, build, hub=hub):
                return hub

            async def _ok(url):
                return None

            async def _resolve(sid):
                return {"name": "T", "streams": [{"url": "https://radio.example/s"}]}
            monkeypatch.setattr(S._hub_registry, "get_or_create", _goc)
            monkeypatch.setattr(S, "_assert_public_url", _ok)
            monkeypatch.setattr(S.radiodir, "resolve_station", _resolve)
            r = await S.relay("scene:a", 0, as_file=True, range_hdr="bytes=0-1")
            assert ("accept-ranges" in r.headers) is as_file, mtype
            await _read(r, 2)
            r = await S.relay("scene:a", 0)                 # not asked: the stream
            assert "accept-ranges" not in r.headers and "content-length" not in r.headers
            await _read(r, 2)
    asyncio.run(go())


def test_a_later_range_does_not_replay_what_was_read():
    """WebKit reads the start, then the rest from where it stopped: that range
    leaves out the part of the recent audio (the burst) it already has."""
    async def go():
        data = bytes(range(256)) * 8                   # 2048 bytes a "loop"
        hub = _Hub([data[i:i + 100] for i in range(0, len(data), 100)])
        hub.burst_size = 1500
        r = S._as_file_response("bytes=600-", hub)
        assert (await _read(r, 300))[:300] == data[600:900]
        r = S._as_file_response("bytes=5000-", hub)     # past the burst: all of it skipped
        assert (await _read(r, 100))[:100] == data[1500:1600]
        assert hub.subscribers == 0
    asyncio.run(go())


def test_a_real_hub_tells_its_burst_and_closes_after_an_answer_without_a_listener(monkeypatch):
    """``StationHub.burst_size`` is what a new listener is sent first, and the
    zero-filled answer for the end of the "file" lets an unused hub close
    (``get_or_create`` had cancelled its teardown) — QA 2026-10-28."""
    from soniqboom.core import station_hub as H
    monkeypatch.setattr(H, "_TEARDOWN_GRACE", 0.1)

    async def go():
        async def producer(set_info):
            for i in range(5):
                yield bytes([i + 1]) * 1000
            await asyncio.sleep(30)                     # still connected
        hub = H.StationHub("t#0", producer, media_type="audio/mpeg")
        await hub.start()
        await asyncio.sleep(0.05)
        assert hub.burst_size == 5000
        r = S._as_file_response(f"bytes={S._AS_FILE_SIZE - 10}-", hub)
        assert r.body == bytes(10) and hub.subscriber_count == 0
        for _ in range(40):
            if hub._closed:
                break
            await asyncio.sleep(0.05)
        assert hub._closed
    asyncio.run(go())
