# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stream sign-in from review round 3: a ``p=`` that misses the O(1) checks
runs its scrypt off the event loop behind /rest's 2-slot gate, and counts its
misses in the Subsonic ``p`` lockout scope (never the web login's).

In-process only: a throw-away UserStore with real scrypt, no live server.
"""
from __future__ import annotations

import asyncio
import time
import types

import pytest

from soniqboom.api import stream
from soniqboom.api import subsonic as _subsonic   # imported by the app at startup


@pytest.fixture()
def real_users(monkeypatch, tmp_path):
    from soniqboom.core import users as users_mod
    ustore = users_mod.UserStore(tmp_path)
    ustore.create(username="amy", password="loginpass1", role="admin")
    ustore.get_by_username("amy").subsonic_password = "app-secret"
    monkeypatch.setattr(users_mod, "get_user_store", lambda: ustore)
    return ustore


async def _loop_max_gap(coro, tick: float = 0.002) -> tuple[float, object]:
    """Run ``coro`` while a ticker measures the longest event-loop stall."""
    gaps = [0.0]
    stop = asyncio.Event()

    async def ticker():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(tick)
            now = time.perf_counter()
            gaps[0] = max(gaps[0], now - last - tick)
            last = now
    t = asyncio.ensure_future(ticker())
    await asyncio.sleep(0.02)
    try:
        res = await coro
    finally:
        stop.set()
        await t
    return gaps[0], res


@pytest.mark.asyncio
async def test_wrong_passwords_do_not_stall_the_event_loop(real_users):
    from soniqboom.core.users import verify_password, hash_password
    h = hash_password("x")
    t0 = time.perf_counter()
    verify_password("y", h)
    one_scrypt = time.perf_counter() - t0          # ~60 ms on the dev box
    req = types.SimpleNamespace()

    async def burst():
        async def one(i):
            try:
                await stream._require_stream_auth(req, None, "amy", f"wrong{i}")
            except stream.HTTPException as e:
                return e.status_code
            return 200
        return await asyncio.gather(*[one(i) for i in range(8)])

    gap, codes = await _loop_max_gap(burst())
    assert codes == [401] * 8
    # Inline scrypt stalled the loop one full scrypt per request (8 × ~60 ms
    # back to back).  Off-loop, the loop never waits on one.
    assert gap < max(0.03, one_scrypt / 2), (gap, one_scrypt)


@pytest.mark.asyncio
async def test_scrypt_checks_share_the_rest_gate(monkeypatch, real_users):
    from soniqboom.core import users as users_mod
    live = {"now": 0, "max": 0}
    real_verify = users_mod.verify_password

    def slow_verify(pw, h):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        try:
            time.sleep(0.05)
            return real_verify(pw, h)
        finally:
            live["now"] -= 1
    monkeypatch.setattr(users_mod, "verify_password", slow_verify)
    req = types.SimpleNamespace()

    async def one(i):
        with pytest.raises(stream.HTTPException):
            await stream._require_stream_auth(req, None, "amy", f"bad{i}")
    await asyncio.gather(*[one(i) for i in range(6)])
    assert live["max"] <= _subsonic._SCRYPT_SLOTS


def test_wrong_p_locks_the_p_scope_not_the_web_login(real_users):
    req = types.SimpleNamespace()
    for i in range(15):
        with pytest.raises(stream.HTTPException):
            asyncio.run(stream._require_stream_auth(req, None, "amy", f"stale-{i}"))
    assert real_users.is_locked("amy", "p")
    assert not real_users.is_locked("amy")
    assert real_users.authenticate("amy", "loginpass1") is not None   # web login fine
    # …while the p scope is locked even the right Subsonic app password is
    # refused on /api/stream (the O(1) compare obeys the lock like /rest).
    with pytest.raises(stream.HTTPException):
        asyncio.run(stream._require_stream_auth(req, None, "amy", "app-secret"))


def test_a_locked_web_login_also_refuses_the_o1_subsonic_password(real_users):
    for _ in range(15):
        real_users.authenticate("amy", "nope")
    assert real_users.is_locked("amy")
    with pytest.raises(stream.HTTPException):
        asyncio.run(stream._require_stream_auth(types.SimpleNamespace(), None,
                                                "amy", "app-secret"))
