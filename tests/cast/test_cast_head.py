# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""HEAD on the cast byte server: same auth + headers as GET, never renders."""
import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import cast_stream
from soniqboom.core import cast_tokens


class _T:
    def __init__(self, tid, path):
        self.id, self.path, self.format, self.duration = tid, path, "SID", 120.0


def _app(monkeypatch, track):
    async def get_track(tid):
        return track if tid == track.id else None
    monkeypatch.setattr(cast_stream, "get_track", get_track)

    def boom(*a, **k):
        raise AssertionError("HEAD must not start a render/transcode")
    monkeypatch.setattr(cast_stream, "render_stream", boom)
    cast_tokens._reset_replay_state_for_tests()
    app = FastAPI()
    app.include_router(cast_stream.router)
    return app


@pytest.mark.asyncio
async def test_head_on_a_to_be_transcoded_track_answers_headers_only(monkeypatch):
    track = _T("t1", "/m/tune.sid")
    app = _app(monkeypatch, track)
    tok = cast_tokens.issue_token(track_id="t1", codec="mp3", bitrate_kbps=320)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.head(f"/cast/{tok}/tune.mp3")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("audio/mpeg")
        assert r.headers.get("accept-ranges") == "none"
        assert any(k.lower().startswith("contentfeatures") for k in r.headers)
        assert r.content == b""
        bad = await c.head("/cast/not-a-token/x.mp3")
        assert bad.status_code == 404


@pytest.mark.asyncio
async def test_head_checks_the_ip_binding_but_never_claims_the_link(monkeypatch):
    """A DLNA control point (phone) may HEAD the link before telling the TV to
    play it: the TV's first request must still own it."""
    track = _T("t2", "/m/tune.sid")
    app = _app(monkeypatch, track)
    tok = cast_tokens.issue_token(track_id="t2", codec="mp3")
    phone = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("10.0.0.5", 1)), base_url="http://t")
    tv = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("10.0.0.9", 1)), base_url="http://t")
    async with phone, tv:
        assert (await phone.head(f"/cast/{tok}/x.mp3")).status_code == 200
        claims = cast_tokens.verify_token(tok)
        assert cast_tokens.replay_ok(claims, "10.0.0.9")          # the TV can still claim it
        assert (await phone.head(f"/cast/{tok}/x.mp3")).status_code == 404   # bound elsewhere now
        assert (await tv.head(f"/cast/{tok}/x.mp3")).status_code == 200
