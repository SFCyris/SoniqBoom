# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in lockout per client address (defect 6 of the v2.2.0 review).

Until 2.3.0, 15 wrong passwords from ANYONE locked the account's password
sign-in for 15 minutes — a stranger could lock the owner and their Subsonic
apps out.  Now the guesses count per (account, scope, client address): the
stranger locks out only the stranger.  Guesses spread over many addresses
trip an account-wide slow-down that holds back only clients that never
signed in to the account.  The failed-guess tables are pruned and capped.
A flood of anonymous sign-ins can't fill the default executor any more.

In-process only: throw-away UserStores (real scrypt), no live server.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
import types

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soniqboom.api import stream
from soniqboom.api import subsonic
from soniqboom.api import users as users_api
from soniqboom.core import users as users_mod
from soniqboom.core.users import UserStore, client_key

PW = "Correct-horse-9!"
MAX = users_mod._LOCKOUT_MAX_ATTEMPTS


@pytest.fixture()
def store(monkeypatch, tmp_path):
    st = UserStore(tmp_path)
    st.create("admin", PW, "admin")
    st.get_by_username("admin").subsonic_password = "app-secret"
    monkeypatch.setattr(users_mod, "_instance", st)
    monkeypatch.setattr(subsonic, "get_user_store", lambda: st)
    monkeypatch.setattr(users_api, "get_user_store", lambda: st)
    return st


def _req(ip: str):
    return types.SimpleNamespace(client=types.SimpleNamespace(host=ip), headers={},
                                 query_params={})


def _users_app() -> FastAPI:
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api")
    return app


# ── client keys ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("host,key", [
    ("10.0.0.66", "10.0.0.66"),
    ("::ffff:10.0.0.66", "10.0.0.66"),
    ("2a00:1450:4001:80b::1", "2a00:1450:4001:80b::/64"),            # global: by its /64
    ("2a00:1450:4001:80b:ffff:ffff:ffff:ffff", "2a00:1450:4001:80b::/64"),
    ("fe80::1%en0", "fe80::1"), ("fd12:3456::7", "fd12:3456::7"),     # a LAN's: one host each
    ("", "?"), (None, "?"), ("testclient", "testclient"),
])
def test_client_key(host, key):
    assert client_key(host) == key


# ── the store: per-client locks ──────────────────────────────────────────────

def test_a_strangers_guesses_lock_only_the_stranger(store):
    for i in range(MAX):
        assert store.authenticate("admin", f"x{i}", client="10.0.0.66") is None
    assert store.is_locked("admin", client="10.0.0.66")
    # the owner, another address: not locked, the right password signs in
    assert not store.is_locked("admin", client="10.0.0.10")
    assert store.authenticate("admin", PW, client="10.0.0.10") is not None
    # the stranger stays locked — even with the right password
    assert store.authenticate("admin", PW, client="10.0.0.66") is None
    assert 800 < store.sign_in_wait("admin", "10.0.0.66") <= users_mod._LOCKOUT_COOLDOWN_SEC


def test_the_owners_success_clears_only_the_owners_count(store):
    for i in range(MAX - 1):
        store.authenticate("admin", f"x{i}", client="10.0.0.66")
        store.authenticate("admin", f"y{i}", client="10.0.0.10")
    assert store.authenticate("admin", PW, client="10.0.0.10") is not None
    # the owner's address starts over …
    for i in range(MAX - 1):
        store.authenticate("admin", f"z{i}", client="10.0.0.10")
    assert not store.is_locked("admin", client="10.0.0.10")
    # … the stranger's does not: one more guess locks it
    store.authenticate("admin", "x-last", client="10.0.0.66")
    assert store.is_locked("admin", client="10.0.0.66")


def test_ipv6_guesses_from_one_slash64_count_together(store):
    for i in range(MAX):
        store.authenticate("admin", f"x{i}",
                           client=client_key(f"2a00:1450:4001:81b::{i + 1:x}"))
    assert store.is_locked("admin", client=client_key("2a00:1450:4001:81b::ffff"))
    assert not store.is_locked("admin", client=client_key("2a00:1450:4001:82b::1"))


# ── the store: the account-wide slow-down ────────────────────────────────────

def _spread_failures(store, n, prefix="198.51.100.", name="admin"):
    for i in range(n):
        store.note_failed_attempt(name, "token", client=f"{prefix}{i}")


def test_guesses_from_many_addresses_shrink_unknown_clients_budgets(store):
    store.note_signed_in("admin", "10.0.0.10")          # the owner signed in before
    _spread_failures(store, users_mod._ACCOUNT_SLOW_AFTER)
    slow = users_mod._ACCOUNT_SLOW_MAX_ATTEMPTS
    # a client that never signed in: locked after 3 guesses, not 15
    for i in range(slow):
        assert store.authenticate("admin", f"x{i}", client="203.0.113.5") is None
    assert store.is_locked("admin", client="203.0.113.5")
    # … each such client has its own budget: another one still gets its tries
    assert store.token_check_allowed("admin", "203.0.113.6")
    assert store.authenticate("admin", PW, client="203.0.113.7") is not None
    # the owner's known address keeps the full budget
    for i in range(MAX - 1):
        store.authenticate("admin", f"y{i}", client="10.0.0.10")
    assert not store.is_locked("admin", client="10.0.0.10")
    assert store.authenticate("admin", PW, client="10.0.0.10") is not None


def test_a_sustained_attack_never_starves_the_owner_on_a_new_address(store, monkeypatch):
    """The QA's scenario (a shared check every 30 s was always taken by an
    attacker polling faster): two attacking addresses keep the account under
    the slow-down for hours; the owner's app on a NEW address (a restart: no
    address is known) signs in at its first try every time."""
    clock = [time.time()]
    monkeypatch.setattr(users_mod.time, "time", lambda: clock[0])
    salt = "s"
    good = hashlib.md5(("app-secret" + salt).encode()).hexdigest()
    owner_ok = owner_tries = 0
    for minute in range(120):
        for k in range(20):                 # every 3 s, alternately from two addresses
            atk = _req(f"203.0.113.{1 + k % 2}")
            subsonic._resolve_user(atk, None, "admin", None, f"x{minute}-{k}", "0" * 32)
            clock[0] += 3
        if minute % 10 == 9:                # the owner, from a new address each time
            owner_tries += 1
            owner = _req(f"198.18.0.{minute}")
            owner_ok += subsonic._resolve_user(owner, None, "admin", None, salt, good) is not None
    assert owner_tries == 12 and owner_ok == owner_tries
    # … while the attackers stay throttled: at most 15 (then 3) guesses per
    # address per 15 min — both are locked now
    assert store.is_locked("admin", "token", client="203.0.113.1")
    assert store.is_locked("admin", "token", client="203.0.113.2")


def test_a_password_change_forgets_the_known_addresses(store):
    store.note_signed_in("admin", "10.0.0.10")
    assert store._is_known_good("admin", "10.0.0.10", time.time())
    store.set_password(store.get_by_username("admin").id, "Another-horse-9!")
    assert not store._is_known_good("admin", "10.0.0.10", time.time())


def test_below_the_threshold_nobody_is_slowed(store):
    _spread_failures(store, users_mod._ACCOUNT_SLOW_AFTER - MAX)   # (+ the 14 below: 29)
    for i in range(MAX - 1):
        store.authenticate("admin", f"x{i}", client="203.0.113.9")
    assert not store.is_locked("admin", client="203.0.113.9")


# ── the store: bounded tables ────────────────────────────────────────────────

def test_expired_records_are_pruned(store, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(users_mod.time, "time", lambda: clock[0])
    for i in range(200):
        store.note_failed_attempt(f"nobody{i}", "token", client="10.0.0.66")
    store.note_failed_attempt("admin", "token", client="10.0.0.66")
    assert len(store._lockout_x) == 200 and len(store._acct_fails_x) == 200
    assert len(store._lockout) == 1 and len(store._acct_fails) == 1
    clock[0] += users_mod._LOCKOUT_WINDOW_SEC + 1
    store.note_failed_attempt("someone", "token", client="10.0.0.66")
    store.note_failed_attempt("admin", "", client="10.0.0.67")
    assert len(store._lockout_x) == 1 and len(store._acct_fails_x) == 1
    assert list(store._lockout) == [("admin", "", "10.0.0.67")]


def test_a_locked_record_outlives_the_window_until_its_lock_ends(store, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(users_mod.time, "time", lambda: clock[0])
    for _ in range(MAX):
        store.note_failed_attempt("admin", "", client="10.0.0.66")
    clock[0] += users_mod._LOCKOUT_COOLDOWN_SEC - 5
    store.note_failed_attempt("admin", "", client="10.0.0.77")     # (prunes)
    assert store.is_locked("admin", client="10.0.0.66")
    clock[0] += 10
    store.note_failed_attempt("admin", "", client="10.0.0.77")
    assert not store.is_locked("admin", client="10.0.0.66")
    assert ("admin", "", "10.0.0.66") not in store._lockout


def test_made_up_names_can_never_push_out_a_real_accounts_lock(store, monkeypatch):
    """The QA's attack: a locked attacker floods made-up names (one md5 each
    in token mode) until its own lock and the account's slow-down fell out of
    the capped tables."""
    monkeypatch.setattr(users_mod, "_LOCKOUT_X_MAX_ENTRIES", 50)
    monkeypatch.setattr(users_mod, "_ACCOUNT_X_MAX_ENTRIES", 40)
    for i in range(MAX):
        store.note_failed_attempt("admin", "token", client="10.0.0.66")
        store.authenticate("admin", f"x{i}", client="10.0.0.66")
    _spread_failures(store, users_mod._ACCOUNT_SLOW_AFTER)
    atk = _req("10.0.0.66")
    for i in range(500):                    # through the real token path
        subsonic._resolve_user(atk, None, f"junk{i}", None, "s", "0" * 32)
    assert len(store._lockout_x) <= 50 and len(store._acct_fails_x) <= 40
    assert ("junk0", "token", "10.0.0.66") not in store._lockout_x   # least recent: gone
    assert store.is_locked("admin", "token", client="10.0.0.66")
    assert store.is_locked("admin", client="10.0.0.66")
    assert store._slowed("admin", "203.0.113.200", time.time())       # the slow-down stays


def test_a_real_accounts_table_keeps_locked_records_over_its_cap(store, monkeypatch):
    monkeypatch.setattr(users_mod, "_LOCKOUT_MAX_ENTRIES", 30)
    for _ in range(MAX):
        store.note_failed_attempt("admin", "token", client="10.0.0.66")
    for i in range(100):                    # many other addresses, one guess each
        store.note_failed_attempt("admin", "token", client=f"198.51.100.{i}")
    assert len(store._lockout) <= 30
    assert store.is_locked("admin", "token", client="10.0.0.66")


def test_a_huge_user_name_is_kept_as_its_hash(store):
    big = "A" * 2_000_000
    store.note_failed_attempt(big, "token", client="10.0.0.66")
    (akey, _scope, _client), = store._lockout_x
    assert len(akey) == 65 and akey.startswith("#")
    assert store.is_locked(big, "token", client="10.0.0.66") is False
    assert sum(len(k) for k in store._acct_fails_x) == 65


def test_records_hold_a_bounded_number_of_timestamps(store):
    for _ in range(500):
        store.note_failed_attempt("admin", "token", client="10.0.0.66")
    assert len(store._lockout[("admin", "token", "10.0.0.66")]["fails"]) == MAX
    assert len(store._acct_fails["admin"]["fails"]) == users_mod._ACCOUNT_SLOW_AFTER


def test_a_resolution_leaves_no_client_bound_behind(store):
    """The client is bound for one ``_resolve_user`` only: a direct call (a
    test, the CLI) afterwards still means "any client"."""
    subsonic._resolve_user(_req("10.0.0.11"), None, "admin", "nope", None, None)
    assert users_mod._REQUEST_CLIENT.get() is None
    for _ in range(MAX):
        store.note_failed_attempt("admin", "p", client="testclient")
    assert store.is_locked("admin", "p")


# ── the request paths ────────────────────────────────────────────────────────

def test_web_sign_in_a_stranger_cannot_lock_out_the_owner(store):
    app = _users_app()
    attacker = TestClient(app, client=("10.0.0.66", 40000))
    owner = TestClient(app, client=("10.0.0.10", 40000))
    codes = [attacker.post("/api/auth/login", json={"username": "admin", "password": f"x{i}"}
                           ).status_code for i in range(MAX)]
    assert codes == [401] * MAX
    r = attacker.post("/api/auth/login", json={"username": "admin", "password": PW})
    assert r.status_code == 429 and "Try again in about" in r.json()["detail"]
    assert int(r.headers["retry-after"]) > 800
    r = owner.post("/api/auth/login", json={"username": "admin", "password": PW})
    assert r.status_code == 200, r.text


def test_web_sign_in_under_the_slow_down(store):
    _spread_failures(store, users_mod._ACCOUNT_SLOW_AFTER)
    app = _users_app()
    stranger = TestClient(app, client=("203.0.113.50", 1))
    for i in range(users_mod._ACCOUNT_SLOW_MAX_ATTEMPTS):
        assert stranger.post("/api/auth/login", json={
            "username": "admin", "password": f"x{i}"}).status_code == 401
    assert stranger.post("/api/auth/login", json={
        "username": "admin", "password": PW}).status_code == 429
    # the owner on a new address: the right password at the first try
    r = TestClient(app, client=("198.18.0.7", 1)).post(
        "/api/auth/login", json={"username": "admin", "password": PW})
    assert r.status_code == 200


def test_subsonic_token_guesses_lock_only_the_guessing_address(store):
    salt = "abc123"
    good = hashlib.md5(("app-secret" + salt).encode()).hexdigest()
    owner, attacker = _req("10.0.0.11"), _req("10.0.0.66")
    assert subsonic._resolve_user(owner, None, "admin", None, salt, good) is not None
    for i in range(MAX):
        assert subsonic._resolve_user(attacker, None, "admin", None, f"s{i}", "0" * 32) is None
    # the attacker is locked out — even with the right token …
    assert subsonic._resolve_user(attacker, None, "admin", None, salt, good) is None
    # … the owner's app is not
    assert subsonic._resolve_user(owner, None, "admin", None, salt, good) is not None
    assert store.is_locked("admin", "token", client="10.0.0.66")
    assert not store.is_locked("admin", "token", client="10.0.0.11")


def test_subsonic_p_and_stream_paths_count_per_address(store):
    attacker, owner = _req("10.0.0.66"), _req("10.0.0.12")
    for i in range(MAX):
        with pytest.raises(stream.HTTPException):
            asyncio.run(stream._require_stream_auth(attacker, None, "admin", f"stale-{i}"))
    assert store.is_locked("admin", "p", client="10.0.0.66")
    # the attacker's O(1) fast path obeys its lock …
    with pytest.raises(stream.HTTPException):
        asyncio.run(stream._require_stream_auth(attacker, None, "admin", "app-secret"))
    # … the owner's app on another address streams
    asyncio.run(stream._require_stream_auth(owner, None, "admin", "app-secret"))
    assert subsonic._resolve_user(owner, None, "admin", "app-secret", None, None) is not None
    assert subsonic._resolve_user(attacker, None, "admin", "app-secret", None, None) is None


def test_stream_token_guesses_lock_only_the_guessing_address(store):
    salt = "s9"
    good = hashlib.md5(("app-secret" + salt).encode()).hexdigest()
    attacker, owner = _req("10.0.0.66"), _req("10.0.0.13")
    for i in range(MAX):
        with pytest.raises(stream.HTTPException):
            asyncio.run(stream._require_stream_auth(attacker, None, "admin", None, f"x{i}",
                                                    "0" * 32))
    with pytest.raises(stream.HTTPException):
        asyncio.run(stream._require_stream_auth(attacker, None, "admin", None, salt, good))
    asyncio.run(stream._require_stream_auth(owner, None, "admin", None, salt, good))
    assert store._is_known_good("admin", "10.0.0.13", time.time())


# ── the sign-in gate: a flood can't fill the default executor ────────────────

def test_a_sign_in_flood_from_one_address_leaves_others_unaffected(store):
    app = _users_app()

    async def run():
        flood_t = httpx.ASGITransport(app=app, client=("10.0.0.66", 40000))
        owner_t = httpx.ASGITransport(app=app, client=("10.0.0.10", 40001))
        async with httpx.AsyncClient(transport=flood_t, base_url="http://t") as fc, \
                httpx.AsyncClient(transport=owner_t, base_url="http://t") as oc:
            flood = [asyncio.create_task(fc.post("/api/auth/login", json={
                "username": f"nobody{i}", "password": "y"})) for i in range(60)]
            await asyncio.sleep(0.02)
            t0 = time.perf_counter()
            probe = asyncio.create_task(asyncio.to_thread(lambda: time.perf_counter()))
            r = await oc.post("/api/auth/login", json={"username": "admin", "password": PW})
            owner_s = time.perf_counter() - t0
            probe_wait = (await probe) - t0
            codes = [x.status_code for x in await asyncio.gather(*flood)]
        return r.status_code, owner_s, probe_wait, codes

    status, owner_s, probe_wait, codes = asyncio.run(run())
    assert status == 200
    assert set(codes) <= {401, 429} and codes.count(429) >= 50
    # at most _SIGNIN_PER_CLIENT of the flood ran: the owner waited for at
    # most that many scrypt checks, an unrelated worker job for none
    assert owner_s < 2.0, owner_s
    assert probe_wait < 0.5, probe_wait
    assert users_api._signin_inflight == {}


def test_registration_counts_an_ipv6_slash64_as_one_address(store, monkeypatch):
    from soniqboom import config
    monkeypatch.setattr(users_api, "_register_log", __import__("collections").deque())
    monkeypatch.setattr(config, "is_self_registration_enabled", lambda: True)
    monkeypatch.setattr(users_api, "is_self_registration_enabled", lambda: True, raising=False)
    app = _users_app()
    codes = []
    for i in range(users_api._REGISTER_PER_CLIENT + 1):
        c = TestClient(app, client=(f"2a00:1450:4001:85b::{i + 1:x}", 1))
        codes.append(c.post("/api/auth/register", json={
            "username": f"visitor{i}", "password": "visitorpass1"}).status_code)
    assert codes[:-1] == [200] * users_api._REGISTER_PER_CLIENT, codes
    assert codes[-1] == 429


# ── QA round 2: saturation, enumeration, parallel requests of one app ───────

def test_a_table_full_of_locked_records_still_counts_a_new_client(store, monkeypatch):
    """The QA's attack: thousands of addresses lock themselves until the
    real account's table holds only locked records — a newcomer's record was
    then dropped at once, never counted, never locked."""
    monkeypatch.setattr(users_mod, "_LOCKOUT_MAX_ENTRIES", 200)
    for i in range(400):
        for _ in range(MAX):
            store.note_failed_attempt("admin", "token", client=f"10.9.{i >> 8}.{i & 255}")
    assert len(store._lockout) <= 200
    fresh = "198.51.100.77"
    allowed = 0
    for _ in range(50):
        if store.token_check_allowed("admin", fresh):
            allowed += 1
            store.note_failed_attempt("admin", "token", client=fresh)
    assert allowed == users_mod._ACCOUNT_SLOW_MAX_ATTEMPTS       # (the account is slowed)
    assert store.is_locked("admin", "token", client=fresh)


def test_a_junk_flood_does_not_tell_real_names_from_made_up_ones(store, monkeypatch):
    monkeypatch.setattr(users_mod, "_LOCKOUT_X_MAX_ENTRIES", 100)
    for name in ("admin", "nosuchuser"):
        for i in range(MAX):
            store.authenticate(name, f"x{i}", client="203.0.113.5")
    for i in range(1000):
        store.note_failed_attempt(f"junk{i}", "token", client="203.0.113.6")
    assert store.sign_in_wait("admin", "203.0.113.5") > 800
    assert store.sign_in_wait("nosuchuser", "203.0.113.5") > 800


def test_one_apps_parallel_requests_share_one_scrypt_check(store, monkeypatch):
    """The first requests after a restart all miss the O(1) cache: four at
    once used to exceed the per-client cap and get "wrong password"."""
    calls = []
    real = store.authenticate_subsonic_password

    def counted(u, p, client=None):
        calls.append(p)
        time.sleep(0.05)
        return real(u, p, client)
    monkeypatch.setattr(store, "authenticate_subsonic_password", counted)

    async def run():
        right = [subsonic._scrypt_authenticate("admin", PW, "10.0.0.11") for _ in range(6)]
        wrong = [subsonic._scrypt_authenticate("admin", f"guess{i}", "10.0.0.66")
                 for i in range(6)]
        return await asyncio.gather(*right), await asyncio.gather(*wrong)
    right, wrong = asyncio.run(run())
    assert all(u is not None and u.username == "admin" for u in right)
    assert calls.count(PW) == 1                                  # one check for all six
    assert all(u is None for u in wrong)
    assert len([c for c in calls if c != PW]) <= subsonic._SCRYPT_PER_CLIENT
    assert subsonic._SCRYPT_SINGLE == {} and subsonic._scrypt_inflight == {}
