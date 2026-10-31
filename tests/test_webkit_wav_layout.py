# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The WebKit layout of a served WAV (``stream._WEBKIT_ALIGN``): a JUNK chunk
after the preamble so the audio ends on a 64 KB boundary, and one after the
audio — WebKit's read of the end of the audio then asks for the added bytes
alone, answered before a render has finished."""
from __future__ import annotations

import io
import random
import struct
import wave

import pytest
from starlette.responses import Response

from soniqboom.api import stream as S


def _wav(frames: int, ch: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(bytes(random.Random(frames).getrandbits(8) for _ in range(frames * ch * 2)))
    return buf.getvalue()


def _laid_out(real: bytes) -> bytes:
    """The layout built directly — what the streamed pieces must add up to."""
    junk, padded = S._webkit_layout(len(real))
    riff = struct.unpack("<I", real[4:8])[0] + junk + S._WEBKIT_TRAILER
    out = (real[:4] + struct.pack("<I", riff) + real[8:12] + S._junk_chunk(junk)
           + real[12:] + S._junk_chunk(S._WEBKIT_TRAILER))
    assert len(out) == padded
    return out


@pytest.mark.parametrize("frames", [1000, 22050, 44101, 16384])
def test_the_layout_is_the_same_wav_with_the_audio_ending_aligned(frames):
    real = _wav(frames)
    out = _laid_out(real)
    with wave.open(io.BytesIO(out)) as w, wave.open(io.BytesIO(real)) as r:
        assert w.getparams() == r.getparams()
        assert w.readframes(frames) == r.readframes(frames)
    data_end = out.index(b"data") + 8 + frames * 4
    assert data_end % S._WEBKIT_ALIGN == 0                 # WebKit's read starts here
    assert len(out) - data_end == S._WEBKIT_TRAILER
    assert struct.unpack("<I", out[4:8])[0] == len(out) - 8


def _served(real: bytes, first: int, last: int, rnd: random.Random) -> bytes:
    """What the route gives for the layout's ``first``–``last``: the WAV's
    bytes it plans to read, fed in random pieces through the map."""
    plan = S._webkit_real_range(first, last, len(real))
    m = S._WebKitWavMap(len(real), first, last, len(real) if plan is None else plan[0])
    out = bytearray()
    if plan is not None:
        chunk = real[plan[0]:plan[1] + 1]
        i = 0
        while i < len(chunk):
            n = rnd.choice((1, 2, 5, 7, 11, 4096, 70000))
            out += m.feed(chunk[i:i + n])
            i += n
    out += m.finish()
    return bytes(out)


def test_every_range_of_the_layout_is_the_same_bytes():
    rnd = random.Random(7)
    real = _wav(30001)
    want = _laid_out(real)
    junk, padded = S._webkit_layout(len(real))
    edges = [0, 1, 4, 7, 8, 11, 12, 13, 12 + junk - 1, 12 + junk, 12 + junk + 1,
             len(real) + junk - 1, len(real) + junk, padded - 2, padded - 1]
    cases = [(a, b) for a in edges for b in edges if a <= b]
    cases += [tuple(sorted(rnd.sample(range(padded), 2))) for _ in range(300)]
    for a, b in cases:
        assert _served(real, a, b, rnd) == want[a:b + 1], (a, b)


def test_the_end_of_the_audio_onwards_needs_no_byte_of_the_wav():
    real = _wav(5000)
    junk, padded = S._webkit_layout(len(real))
    data_end = len(real) + junk
    assert data_end % 16384 == 0
    assert S._webkit_real_range(data_end, padded - 1, len(real)) is None
    assert S._webkit_real_range(data_end - 1, padded - 1, len(real)) == (len(real) - 1,) * 2


async def _run(resp) -> "tuple[dict, bytes]":
    msgs = []

    async def send(m):
        msgs.append(m)

    async def receive():
        return {"type": "http.disconnect"}
    await resp({"type": "http", "method": "GET", "headers": []}, receive, send)
    start = msgs[0]
    return ({k.decode(): v.decode() for k, v in start["headers"]} | {"status": start["status"]},
            b"".join(m.get("body", b"") for m in msgs[1:]))


async def test_a_ranged_answer_is_rewritten_into_the_layout():
    real = _wav(20000)
    want = _laid_out(real)
    junk, padded = S._webkit_layout(len(real))
    for a, b in ((3, 12 + junk + 500),          # preamble, the JUNK, some audio
                 (0, 100),                      # ends in the JUNK: only the preamble read
                 (12 + junk - 3, 12 + junk + 3),
                 (5000, padded - 1),            # audio, then the trailer
                 (0, padded - 1)):
        lo, hi = S._webkit_real_range(a, b, len(real))
        inner = Response(content=real[lo:hi + 1], status_code=206, media_type="audio/wav",
                         headers={"Content-Range": f"bytes {lo}-{hi}/{len(real)}",
                                  "X-Rendered": "test"})
        hdrs, body = await _run(S._WebKitAlignedWav(inner, "k1", first=a, last=b, ranged=True,
                                                    real_start=lo, total=len(real)))
        assert hdrs["status"] == 206
        assert hdrs["content-range"] == f"bytes {a}-{b}/{padded}", (a, b)
        assert int(hdrs["content-length"]) == len(body) == b - a + 1, (a, b)
        assert body == want[a:b + 1], (a, b)
    assert S._webkit_totals["k1"] == len(real)


async def test_a_first_probe_learns_the_size_and_a_whole_answer_is_the_whole_layout():
    real = _wav(20000)
    junk, padded = S._webkit_layout(len(real))
    probe = Response(content=real[:2], status_code=206, media_type="audio/wav",
                     headers={"Content-Range": f"bytes 0-1/{len(real)}", "X-Rendered": "t"})
    hdrs, body = await _run(S._WebKitAlignedWav(probe, "k2", first=0, last=1, ranged=True,
                                                real_start=0, total=None))
    assert (hdrs["content-range"], body) == (f"bytes 0-1/{padded}", b"RI")
    whole = Response(content=real, media_type="audio/wav", headers={"X-Transcoded": "1"})
    hdrs, body = await _run(S._WebKitAlignedWav(whole, "k3", first=0, last=None, ranged=False,
                                                real_start=0, total=None))
    assert hdrs["status"] == 200 and int(hdrs["content-length"]) == padded
    assert body == _laid_out(real)


async def test_other_answers_pass_unchanged():
    real = _wav(2000)
    for inner in (Response(content=real, media_type="audio/wav"),            # a file as it is
                  Response(content=b"x", status_code=503, media_type="audio/wav",
                           headers={"X-Rendered": "t"}),
                  Response(content=real, media_type="audio/flac", headers={"X-Rendered": "t"})):
        hdrs, body = await _run(S._WebKitAlignedWav(inner, "k4", first=0, last=None,
                                                    ranged=False, real_start=0, total=None))
        assert body == inner.body and "x-webkit-layout" not in hdrs
    # planned for another size (the render's length changed): as it is
    inner = Response(content=real, media_type="audio/wav", headers={"X-Rendered": "t"})
    hdrs, body = await _run(S._WebKitAlignedWav(inner, "k5", first=0, last=None, ranged=False,
                                                real_start=0, total=len(real) + 2))
    assert body == real and S._webkit_totals["k5"] == len(real)


def test_only_webkit_web_player_gets_the_layout():
    from starlette.requests import Request

    def req(ua, cookie=True, qs=b""):
        h = [(b"user-agent", ua.encode())] + ([(b"cookie", b"sb_session=x")] if cookie else [])
        return Request({"type": "http", "method": "GET", "headers": h, "query_string": qs,
                        "path": "/api/stream/t"})
    safari = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")
    home_app = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
                "(KHTML, like Gecko) Mobile/15E148")
    chrome = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    assert S._webkit_pad_wanted(req(safari)) and S._webkit_pad_wanted(req(home_app))
    assert not S._webkit_pad_wanted(req(chrome))
    assert not S._webkit_pad_wanted(req(safari, cookie=False))
    assert not S._webkit_pad_wanted(req(safari, qs=b"u=a&t=b&s=c"))


def test_a_body_that_ends_short_gets_no_trailer_in_its_place():
    """A growing render that ends early: the answer stops where the audio did,
    instead of putting the trailer's bytes there (QA 2026-10-28)."""
    real = _wav(20000)
    want = _laid_out(real)
    junk, padded = S._webkit_layout(len(real))
    short = len(real) - 5000                            # the render's bytes stop here
    m = S._WebKitWavMap(len(real), 0, padded - 1, 0)
    got = m.feed(real[:short]) + m.finish()
    assert got == want[:short + junk]
    assert b"JUNK" not in got[12 + junk:]
    # ending inside the preamble's JUNK (only the preamble read) still gives it
    m = S._WebKitWavMap(len(real), 0, 100, 0)
    assert m.feed(real[:12]) + m.finish() == want[:101]


async def test_laid_out_answers_have_their_own_etag_and_if_range_maps_back():
    """The layout is other bytes than the file: its ETag is tagged ``-wk``, and
    an If-Range carrying that tag reaches the handlers as the file's own."""
    from starlette.requests import Request
    real = _wav(4000)
    inner = Response(content=real, media_type="audio/wav",
                     headers={"X-Rendered": "t", "ETag": '"abc"'})
    hdrs, _body = await _run(S._WebKitAlignedWav(inner, "k7", first=0, last=None,
                                                 ranged=False, real_start=0, total=None))
    assert hdrs["etag"] == '"abc-wk"'
    seen = {}

    @S._webkit_aligned
    async def handler(request):
        seen["if-range"] = request.headers.get("if-range")
        return Response(content=b"x", media_type="text/plain")
    safari = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")
    req = Request({"type": "http", "method": "GET", "query_string": b"", "path": "/api/stream/t",
                   "headers": [(b"user-agent", safari.encode()), (b"cookie", b"sb_session=x"),
                               (b"if-range", b'"abc-wk"')]})
    await handler(request=req)
    assert seen["if-range"] == '"abc"'
