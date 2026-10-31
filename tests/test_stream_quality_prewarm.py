# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The web player's streaming-quality setting asks the next-track prewarm
for its conversion (``POST /stream/{id}/prewarm?format=mp3&maxBitRate=N``):
a file the browser plays as it is is then prepared as that MP3 — and still
skipped without the hints."""
from __future__ import annotations

import types

from soniqboom.api import stream
from test_render_r1_stream import _app, _client


async def test_a_capped_prewarm_of_a_native_file_is_prepared(monkeypatch, tmp_path):
    f = tmp_path / "a.flac"
    f.write_bytes(b"fLaC" + bytes(64))
    tracks = {"t1": types.SimpleNamespace(id="t1", path=str(f), format="FLAC", duration=60.0,
                                          bitrate=900, defect=None)}
    app, _fs = _app(monkeypatch, tmp_path, tracks)
    got = []

    def schedule(track_id, track, subsong, prio, who, ext, *, named, serve, hover):
        got.append((track_id, ext, serve.get("target_format"), serve.get("max_bitrate_kbps")))
        return {"status": "queued"}
    monkeypatch.setattr(stream, "_schedule_prewarm", schedule)
    async with _client(app) as c:
        r = await c.post("/api/stream/t1/prewarm?priority=next")
        assert r.json() == {"status": "skipped", "reason": "native (no transcode needed)"}
        r = await c.post("/api/stream/t1/prewarm?priority=next&format=mp3&maxBitRate=128")
        assert r.json() == {"status": "queued"}
        r = await c.post("/api/stream/t1/prewarm?priority=next&format=raw")
        assert r.json()["status"] == "skipped"                      # (raw: no conversion)
    assert got == [("t1", ".flac", "mp3", 128)]
