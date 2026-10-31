# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A list that got "no cover yet" (204) for a network-share track revalidates
it (If-None-Match → 304).  The ETag can't change until the cover is cached, so
the revalidation itself must start what caches it: the embedded cover's
backfill, or the folder's cover already cached for another track."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from soniqboom.api import art as A


class _Req:
    def __init__(self, inm=None):
        self.headers = {"if-none-match": inm} if inm else {}


@pytest.fixture
def remote(monkeypatch):
    t = SimpleNamespace(id="r1", path="ftp://share.invalid/Music:/A/a.m4a",
                        cover_art="/api/art/r1", dir_hash="d1", format="AAC")

    async def _get(_tid):
        return t
    monkeypatch.setattr(A, "get_track", _get)

    async def _none(*_a, **_k):
        return None
    monkeypatch.setattr(A.art_cache, "get_art", _none)
    monkeypatch.setattr(A, "_art_cached_mtime", lambda *_a: None)

    async def _no_mtime(_p):
        return None
    monkeypatch.setattr(A, "_source_mtime_for_async", _no_mtime)
    calls = {"backfill": [], "resolve": []}
    import soniqboom.core.art_backfill as AB
    monkeypatch.setattr(AB, "request_backfill", lambda tr: calls["backfill"].append(tr.id))

    async def _resolve(tid):
        calls["resolve"].append(tid)
        return None, None
    monkeypatch.setattr(A, "_resolve_full_art", _resolve)
    A._bg_resolved.clear()
    return t, calls


async def _etag_then_revalidate(tid="r1"):
    first = await A.cover_art(tid, _Req(), size="sm", fallback="none")
    assert first.status_code == 204
    etag = first.headers["ETag"]
    second = await A.cover_art(tid, _Req(etag), size="sm", fallback="none")
    assert second.status_code == 304
    await asyncio.sleep(0)
    return etag


async def test_a_revalidated_empty_answer_starts_the_embedded_cover_backfill(remote):
    t, calls = remote
    first = await A.cover_art("r1", _Req(), size="sm", fallback="none")
    assert first.status_code == 204
    calls["backfill"].clear()
    r = await A.cover_art("r1", _Req(first.headers["ETag"]), size="sm", fallback="none")
    assert r.status_code == 304
    assert calls["backfill"] == ["r1"]


async def test_folder_art_already_cached_is_copied_in_the_background(remote, monkeypatch):
    t, calls = remote
    t.cover_art = None

    async def _folder(key, size):
        return b"\xff\xd8jpeg" if key == A._folder_art_cache_key("d1") else None
    monkeypatch.setattr(A.art_cache, "get_art", _folder)
    first = await A.cover_art("r1", _Req(), size="sm", fallback="none")
    calls["resolve"].clear()
    for _ in range(3):                                  # rows revalidating
        r = await A.cover_art("r1", _Req(first.headers["ETag"]), size="sm", fallback="none")
        assert r.status_code == 304
    await asyncio.sleep(0)
    assert calls["resolve"] == ["r1"]                   # once a minute, not per request
    assert calls["backfill"] == []


async def test_no_embedded_cover_and_no_cached_folder_art_does_nothing(remote):
    t, calls = remote
    t.cover_art = None
    first = await A.cover_art("r1", _Req(), size="sm", fallback="none")
    calls["resolve"].clear()                            # (the first answer's own lookup)
    r = await A.cover_art("r1", _Req(first.headers["ETag"]), size="sm", fallback="none")
    await asyncio.sleep(0)
    assert r.status_code == 304
    assert calls["backfill"] == [] and calls["resolve"] == []   # no share listing per revalidation


async def test_a_local_track_revalidates_as_before(remote, monkeypatch):
    t, calls = remote
    t.path = "/m/A/a.m4a"
    calls["resolve"].clear()
    first = await A.cover_art("r1", _Req(), size="sm", fallback="none")
    calls["resolve"].clear()
    await A.cover_art("r1", _Req(first.headers["ETag"]), size="sm", fallback="none")
    await asyncio.sleep(0)
    assert calls["backfill"] == [] and calls["resolve"] == []


def test_the_cover_fetch_reads_ahead_of_background_passes():
    """The embedded cover's range reads use the share's "browse" lane (after
    playback, before the background passes' "scan" reads)."""
    from soniqboom.core import art_backfill as AB
    from soniqboom.core.filesource import register_source, remove_source
    lanes = []

    class _Src:
        def read_partial(self, path, n, *, lane="scan"):
            lanes.append(lane)
            return b""

        def read_at(self, path, off, n, *, lane="scan"):
            lanes.append(lane)
            return b""

        def close(self):
            pass
    root = "smb://lanes.invalid/share"
    register_source(root, _Src())
    try:
        AB._fetch_remote_cover(f"{root}:/a.mp3", 5_000_000, ".mp3")
        AB._fetch_remote_cover(f"{root}:/a.m4a", 5_000_000, ".m4a")
    finally:
        remove_source(root)
    assert lanes and set(lanes) == {"browse"}
