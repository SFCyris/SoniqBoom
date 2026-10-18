# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""User authentication + management API.

Endpoints fall into three groups:

1. **Public** — ``/auth/login``, ``/auth/register``, ``/auth/me``.
2. **Authed** — ``/auth/logout``, ``/auth/change-password``, ``/me/tokens``,
   ``/me/subsonic-password`` (Subsonic app password), ``/me/api-keys``
   (Subsonic API keys), ``/me/play-queue`` (the saved play queue Subsonic's
   getPlayQueue / savePlayQueue share).
3. **Admin-only** — ``/users`` (list / create / update / delete / role-change).

Session model: HTTP-only cookie ``sb_session`` issued on login.  Same-site
``Lax``, ``Secure`` only when the request was HTTPS (so localhost dev still
works).  TTL is 7 days from the last use: the server-side expiry slides on
every authed request, and the cookie is sent again (``main``'s request gate,
``UserStore.session_cookie_due``) about once an hour so the browser's copy
follows it.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Literal

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from soniqboom.core.users import (
    DISPLAY_NAME_MAX_LEN,
    PasswordChangedMeanwhile,
    SessionRefused,
    SignInsClosed,
    _SESSION_TTL_SEC,
    get_user_store,
    role_rank,
    validate_password,
    validate_username,
)
from soniqboom.models.user import ROLES, Role, User

log = logging.getLogger(__name__)

# The answer to a change of one's own password that an administrator's reset
# (or another change of it) overtook — web and Subsonic.
PASSWORD_CHANGED_MEANWHILE = ("Your password was changed by an administrator — "
                              "sign in again.")

router = APIRouter(tags=["auth"])

_SESSION_COOKIE = "sb_session"


# ── Open-WebSocket registry (used to slam shut sessions on demote/disable) ──
#
# Other modules (api/library.py, api/multiroom etc.) register/unregister their
# accepted WebSocket on accept/close so admin actions that revoke a user's
# privileges can iterate the user's live sockets and close them with code
# ``4401`` — without this an open WS could keep streaming events after its
# owner was demoted to read-only or disabled.
#
# Registry is a plain dict from user-id to a set of WebSocket objects.  We
# don't hold weak references because a closed WebSocket should be removed
# explicitly by its handler's ``finally`` block; lingering entries would
# mean the producer forgot to call ``unregister_open_ws`` and we want that
# to surface as a "set still contained sockets after handler returned" log
# message rather than be silently masked.
_open_ws_by_user: dict[str, set] = {}


def register_open_ws(user_id: str, ws) -> None:
    """Register an accepted WebSocket against ``user_id``.

    Idempotent — registering the same socket twice is a no-op (set semantics).
    """
    if not user_id:
        return
    _open_ws_by_user.setdefault(user_id, set()).add(ws)


def unregister_open_ws(user_id: str, ws) -> None:
    """Drop ``ws`` from the open-socket registry for ``user_id``.

    Safe to call when the user_id has no entry (handler tearing down after
    auth failure / unauth WS arrives in pre-bootstrap state).
    """
    if not user_id:
        return
    bucket = _open_ws_by_user.get(user_id)
    if not bucket:
        return
    bucket.discard(ws)
    if not bucket:
        _open_ws_by_user.pop(user_id, None)


async def close_open_ws_for(user_id: str, code: int = 4401) -> int:
    """Close every WebSocket currently registered for ``user_id``.

    Used by demote/disable paths to slam shut a user's live sessions so
    they can't keep streaming server-pushed events with stale privileges.
    Returns the number of sockets closed.
    """
    import asyncio
    bucket = _open_ws_by_user.pop(user_id, None)
    if not bucket:
        return 0
    sockets = list(bucket)

    async def _close(ws):
        try:
            await asyncio.wait_for(ws.close(code=code), timeout=1.0)
        except Exception:
            pass

    await asyncio.gather(*(_close(ws) for ws in sockets), return_exceptions=True)
    return len(sockets)


def _pub(user: User) -> dict:
    """A user as the API returns it — ``to_public()`` plus whether the
    Subsonic token secret is a generated app password
    (``subsonic_password_custom``, see ``UserStore.public``)."""
    fn = getattr(get_user_store(), "public", None)
    return fn(user) if fn is not None else user.to_public()


# ── Request / response schemas ───────────────────────────────────────────────

# Field length ceilings (the request body itself is capped by the request
# gate in ``main``): a username is validated to 64 characters and a password
# to 1024 — these only stop absurd input before any work is done on it.
_NAME_FIELD = Field(max_length=256)
_PW_FIELD = Field(max_length=4096)
_DISPLAY_FIELD = Field(default=None, max_length=DISPLAY_NAME_MAX_LEN)
_TOKEN_MAX_LEN = 512


class LoginBody(BaseModel):
    username: str = _NAME_FIELD
    password: str = _PW_FIELD


class RegisterBody(BaseModel):
    username: str = _NAME_FIELD
    password: str = _PW_FIELD
    display_name: str | None = _DISPLAY_FIELD


class ChangePasswordBody(BaseModel):
    current_password: str = _PW_FIELD
    new_password: str = _PW_FIELD


class CreateUserBody(BaseModel):
    username: str = _NAME_FIELD
    password: str = _PW_FIELD
    role: Literal["admin", "edit", "readonly"] = "readonly"
    display_name: str | None = _DISPLAY_FIELD


class UpdateUserBody(BaseModel):
    role: Literal["admin", "edit", "readonly"] | None = None
    enabled: bool | None = None
    display_name: str | None = _DISPLAY_FIELD


class AdminSetPasswordBody(BaseModel):
    new_password: str = _PW_FIELD


class UpdateTokensBody(BaseModel):
    listenbrainz_token: str | None = Field(default=None, max_length=_TOKEN_MAX_LEN,
                                           description="Empty string clears it")
    lastfm_session_key: str | None = Field(default=None, max_length=_TOKEN_MAX_LEN,
                                           description="Empty string clears it")


# ── Auth helpers (FastAPI dependencies) ─────────────────────────────────────

def request_is_tls(scheme: str, forwarded_proto: str | None) -> bool:
    """The wire is TLS: the request's own scheme, or a TLS-terminating
    reverse proxy's ``X-Forwarded-Proto`` (nginx, Caddy, Cloudflare)."""
    fwd = (forwarded_proto or "").lower().split(",")[0].strip()
    return scheme in ("https", "wss") or fwd == "https"


def session_cookie_header(token: str, secure: bool) -> bytes:
    """The ``Set-Cookie`` value of the session cookie: HttpOnly, SameSite=Lax,
    path /, Max-Age = the session TTL, ``Secure`` on TLS."""
    r = Response()
    r.set_cookie(key=_SESSION_COOKIE, value=token, max_age=int(_SESSION_TTL_SEC),
                 httponly=True, samesite="lax", secure=secure, path="/")
    return next(v for k, v in r.raw_headers if k == b"set-cookie")


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    """Issue the session cookie.  ``Secure`` is set whenever the wire is
    TLS — honour ``X-Forwarded-Proto`` so deployments behind a TLS-
    terminating reverse proxy (nginx, Caddy, Cloudflare) get the right
    flag.  Without this the cookie would lack ``Secure`` even on real
    HTTPS, leaking to any downgrade attack."""
    secure = request_is_tls(request.url.scheme, request.headers.get("x-forwarded-proto"))
    response.raw_headers.append(
        (b"set-cookie", session_cookie_header(token, secure)))


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(_SESSION_COOKIE, path="/")


# The auth dependencies are ``async def``: they only read the user store's
# in-memory maps (no I/O, no lock), and FastAPI runs a plain ``def`` dependency
# in its thread pool — a thread hop per dependency per request (a signed-in
# /api/me/api-keys, two of them: 718 → 545 µs in-process).

async def current_user(
    sb_session: str | None = Cookie(default=None),
) -> User | None:
    """Resolve the calling user from the ``sb_session`` cookie, or None.

    Use this on endpoints that *may* be accessed anonymously (public UI
    config, ping, etc.).  For required-auth endpoints, use
    :func:`require_user`.
    """
    if not sb_session:
        return None
    return get_user_store().lookup_session(sb_session)


async def require_user(user: User | None = Depends(current_user)) -> User:
    """Reject if there's no signed-in user."""
    if user is None:
        raise HTTPException(401, "Not signed in.")
    return user


def require_role(*allowed: Role):
    """Build a FastAPI dependency that lets through only listed roles."""
    async def _dep(user: User = Depends(require_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(403, f"Requires role {' or '.join(allowed)}.")
        return user
    return _dep


async def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin role required.")
    return user


# Mutation of SHARED library/config data (track tags, delete, global rating,
# duplicate recompute, server-local station favorites) — allowed for admin or
# edit, blocked for readonly.  Per-user data (own playlists) and playback stay
# on ``require_user``.
EDIT_ROLES = ("admin", "edit")
require_edit = require_role(*EDIT_ROLES)


def has_edit_role(user: User | None) -> bool:
    """Does ``user`` hold the ``require_edit`` roles?  For an endpoint any
    signed-in user may call that writes shared data only for edit users
    (the web lyrics lookup's tag write-back)."""
    return getattr(user, "role", None) in EDIT_ROLES


# ── Public endpoints ─────────────────────────────────────────────────────────

def _registration_open(store) -> bool:
    """Self-registration needs an admin to exist (bootstrap is CLI-only) and
    the "Allow self-registration" setting (Settings → Users) to be on."""
    from soniqboom.config import is_self_registration_enabled
    return store.has_any_admin() and is_self_registration_enabled()


@router.get("/auth/status")
async def auth_status(request: Request):
    """Public summary of the auth setup (``_auth_status_doc``)."""
    return _auth_status_doc(request)


def _auth_status_doc(request: Request, cli: bool = False) -> dict:
    """Public summary of the auth setup — used by the login UI to decide
    whether to show the "Create account" link (only when at least one
    admin already exists; bootstrap creation is via CLI).

    ``data_dir`` (the folder the server reads users.json from) is included
    for an admin, and — while no admin account is loaded (first run, or
    recovery) — for a request from the server's own host: the sign-in
    screen's setup hint shows it, so a shell using another
    ``SONIQBOOM_DATA_DIR`` shows up.  ``soniqboom-setadm``'s reload (it
    proved it is the server's own CLI with the run's reload token) always
    gets it: that is how it checks the server it notified serves its data
    dir.  Anyone else gets ``null``."""
    from soniqboom.config import get_data_dir, is_strong_passwords_required
    from soniqboom.core.users import RECOVERY_MESSAGE
    store = get_user_store()
    recovery = getattr(store, "recovery_required", False) is True
    if cli:
        show_dir = True
    elif not store.has_any_admin():
        show_dir = _is_loopback_request(request)
    else:
        u = store.lookup_session(request.cookies.get(_SESSION_COOKIE) or "")
        show_dir = u is not None and u.enabled and u.role == "admin"
    return {
        # (False in recovery: no account is loaded, nobody can sign in —
        # ``has_any()`` stays True there to keep the gates closed)
        "has_any_user":  False if recovery else store.has_any(),
        "has_any_admin": store.has_any_admin(),
        "recovery_required": recovery,
        "recovery_message": RECOVERY_MESSAGE if recovery else None,
        "registration_open": _registration_open(store),
        "strong_passwords": is_strong_passwords_required(),
        "session_cookie": _SESSION_COOKIE,
        "data_dir":      str(get_data_dir().absolute()) if show_dir else None,
    }


# Headers a reverse proxy adds for the client it relays.  A proxy on this host
# reaches us over loopback, so the peer address alone would pass its remote
# clients off as local.  (A proxy that sets none of them still does.)
_FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-real-ip",
                       "x-forwarded-host", "x-forwarded-proto", "via")


def _is_loopback_request(request: Request) -> bool:
    """True iff *request* came straight from a process on this host: the peer
    is a loopback address (``127.0.0.0/8``, ``::1``, or v4-mapped
    ``::ffff:127.x``) and no proxy-forwarding header is present."""
    if any(h in request.headers for h in _FORWARDING_HEADERS):
        return False
    import ipaddress
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


@router.post("/auth/reload")
async def auth_reload(request: Request):
    """Re-read users.json from disk so the running server sees an account
    that ``soniqboom-setadm`` just created or changed — no process restart.

    Two callers, and only these are let in (the path is on the middleware's
    public allowlist, so the gate lives here):

    * **the CLI** — ``soniqboom-setadm`` on the same host, or inside the same
      container via ``docker compose exec``: a loopback client that sends this
      run's reload token, which it can only have read from THIS server's
      ``startup-status.json`` (owner-only, in the data dir).  A wrong token is
      409 and nothing is reloaded — the caller changed another data dir, so a
      stale status file or a copied data dir never reloads the wrong server;
    * **the login overlay's "re-check" button** — any browser, no token, but
      only while accounts are loaded and none of them is an enabled admin.
      Otherwise it gets 403 and falls back to ``GET /auth/status`` (auth.js).

    While NO account is loaded — recovery (users.json unreadable on a set-up
    install) or a fresh install — only the CLI's token reloads: a reload reads
    and may move aside (quarantine) whatever users.json holds at that instant,
    so an anonymous caller could otherwise move away a users.json an admin is
    still copying back in.  Without the CLI the server picks a restored file
    up by itself (``UserStore.check_restored``, polled from the lifespan),
    and that path never moves a file aside.  A loopback address alone is no
    proof of the CLI: a reverse proxy on the same host connects from it too.

    The re-check button's reload never moves a file aside either (a
    users.json still being copied in, read half-written, is left alone and
    nothing in memory changes): it adopts users.json only when that holds a
    valid account list (``UserStore.adopt_from_disk``).

    Everyone else gets 403: a reload takes the users.json flock and rolls the
    in-memory ``last_login_at`` values back to their last-persisted ones.  500
    when users.json could not be read, so the CLI never reports a change live
    that the server failed to load.  The CLI's answer carries ``data_dir``."""
    import secrets
    from soniqboom.core import startup_status
    store = get_user_store()
    token = request.headers.get(startup_status.RELOAD_TOKEN_HEADER)
    if token is not None:
        if not _is_loopback_request(request):
            raise HTTPException(403, "Reloading user accounts is only allowed "
                                     "from the server's own host or container.")
        expected = startup_status.reload_token()
        if not (expected and secrets.compare_digest(
                token.encode("utf-8"), expected.encode("utf-8"))):
            raise HTTPException(409, "This server does not serve the data dir "
                                     "that reload token came from.")
    elif store.count() == 0:
        # Recovery or a fresh install: only soniqboom-setadm (with the token).
        raise HTTPException(403, "While no user account is loaded, user accounts "
                                 "are reloaded only by soniqboom-setadm on the "
                                 "server; a restored users.json is picked up by "
                                 "itself.")
    elif store.has_any_admin():
        raise HTTPException(403, "Reloading user accounts is only allowed "
                                 "from the server's own host or container.")
    else:
        # The re-check button: accounts loaded, none an enabled admin.
        await asyncio.to_thread(store.adopt_from_disk, False)
        return _auth_status_doc(request)
    if not await asyncio.to_thread(store.reload):
        raise HTTPException(500, "users.json could not be read — see the server log.")
    return _auth_status_doc(request, cli=True)


def _issue_session(store, user_id: str, pw_hash: str | None = None) -> tuple[str, float]:
    """``store.issue_session`` — 503 while the server is restarting (sign-ins
    closed, ``UserStore.close_sign_ins``); 401 when the password changed
    while the sign-in ran (``SessionRefused``)."""
    try:
        return store.issue_session(user_id, pw_hash)
    except SignInsClosed:
        raise HTTPException(503, "The server is restarting — sign in again in a moment.",
                            headers={"Retry-After": "2"})
    except SessionRefused:
        raise HTTPException(401, "Wrong username or password.")


# Self-registration attempts accepted per client address, and in all, per
# hour (a default install has self-registration on: an anonymous client must
# not be able to grow users.json without bound).
_REGISTER_PER_CLIENT = 10
_REGISTER_TOTAL = 60
_REGISTER_WINDOW_SEC = 3600.0
_register_log: "collections.deque[tuple[float, str]]" = collections.deque()


def _register_allowed(client: str) -> float:
    """0 when a registration from ``client`` may proceed now (and it is
    counted), else the seconds until one may."""
    now = time.monotonic()
    while _register_log and now - _register_log[0][0] >= _REGISTER_WINDOW_SEC:
        _register_log.popleft()
    mine = [t for t, c in _register_log if c == client]
    if len(mine) >= _REGISTER_PER_CLIENT:
        return _REGISTER_WINDOW_SEC - (now - mine[0])
    if len(_register_log) >= _REGISTER_TOTAL:
        return _REGISTER_WINDOW_SEC - (now - _register_log[0][0])
    _register_log.append((now, client))
    return 0.0


@router.post("/auth/login")
async def login(body: LoginBody, request: Request, response: Response):
    """Sign in.  ``authenticate`` runs scrypt which blocks ~80ms — move it
    off the event loop so concurrent requests (multiroom heartbeats,
    other users' API calls) aren't stalled during password verify."""
    import asyncio
    store = get_user_store()
    locked, remaining = store._is_locked((body.username or "").lower())
    if locked:
        # Don't tell an unauthenticated caller *who* is locked beyond what
        # they asked about — but for the actual user, a clear "try again
        # in N min" is preferable to a generic 401.
        mins = max(1, int(remaining // 60))
        raise HTTPException(
            429,
            f"Too many failed attempts. Try again in about {mins} minute"
            + ("s" if mins != 1 else "") + ".",
        )
    hit = await asyncio.to_thread(store.authenticate_with_hash, body.username, body.password)
    if not hit:
        raise HTTPException(401, "Wrong username or password.")
    user, verified = hit
    # (bound to the hash just verified: a reset that ran meanwhile ends it)
    token, expiry = _issue_session(store, user.id, verified)
    _set_session_cookie(response, request, token)
    log.info("user '%s' (role=%s) signed in", user.username, user.role)
    return {"user": _pub(user), "expires_at": expiry}


@router.post("/auth/register")
async def register(body: RegisterBody, request: Request, response: Response):
    """Self-service account creation.

    Allowed only after at least one admin exists (created via the
    ``soniqboom-setadm`` CLI).  Pre-admin, registration is closed so a
    drive-by visitor can't grant themselves admin on a fresh install.
    """
    store = get_user_store()
    if not store.has_any_admin():
        raise HTTPException(
            403,
            "Registration is closed — an administrator must exist first. "
            "Run `soniqboom-setadm -user <name> -passwd <pass>` on the server.",
        )
    if not _registration_open(store):
        raise HTTPException(403, "Registration is turned off. Ask an administrator for an account.")
    wait = _register_allowed(request.client.host if request.client else "")
    if wait:
        mins = max(1, int(wait // 60) + 1)
        raise HTTPException(429, f"Too many new accounts — try again in about {mins} minute"
                                 + ("s" if mins != 1 else "") + ".",
                            headers={"Retry-After": str(int(wait) + 1)})
    try:
        # (scrypt + a durable users.json write: off the event loop)
        user = await asyncio.to_thread(
            store.create,
            username=body.username,
            password=body.password,
            role="readonly",
            display_name=body.display_name,
        )
    except ValueError as e:
        msg = str(e)
        # Don't enumerate existing usernames to anonymous registration
        # callers — pen-test #1 P1-2.  Validation failures (username
        # format / password length) still leak through with the specific
        # message because they aren't existence oracles.
        if "already taken" in msg.lower():
            raise HTTPException(400, "Username unavailable.")
        raise HTTPException(400, msg)
    token, expiry = _issue_session(store, user.id, user.password_hash)
    _set_session_cookie(response, request, token)
    log.info("user '%s' self-registered (role=readonly)", user.username)
    return {"user": _pub(user), "expires_at": expiry}


@router.get("/auth/me")
async def me(user: User | None = Depends(current_user)):
    """Returns the current user, plus server-level scrobble readiness so
    the My Account UI can honestly report whether tokens will actually
    fire (a session-key set with no server API key silently no-ops)."""
    if user is None:
        raise HTTPException(401, "Not signed in.")
    from soniqboom.core.scrobble import (
        lastfm_keys_configured, dropped_scrobbles, queue_depth,
    )
    return {
        "user": _pub(user),
        "server": {
            "lastfm_keys_configured": lastfm_keys_configured(),
            "scrobble_queue_depth":   queue_depth(),
            "scrobble_dropped":       dropped_scrobbles(),
        },
    }


# ── Authed endpoints ─────────────────────────────────────────────────────────

@router.post("/auth/logout")
async def logout(
    response: Response,
    sb_session: str | None = Cookie(default=None),
):
    if sb_session:
        import asyncio
        # (writes sessions.json before answering — off the event loop)
        await asyncio.to_thread(get_user_store().revoke_session, sb_session)
    _clear_session_cookie(response)
    return {"ok": True}


@router.post("/auth/change-password")
async def change_password(
    body: ChangePasswordBody,
    user: User = Depends(require_user),
    sb_session: str | None = Cookie(default=None),
):
    """Change the signed-in user's own password.  Every other session of the
    account ends; this one stays signed in (bound to the new password).

    The change applies only over the password that was verified: when the
    account's password changes between the check of ``current_password`` and
    the write (an administrator's reset), the request is refused with 409 and
    the other change stands."""
    import asyncio
    store = get_user_store()
    # scrypt off the event loop — same reason as /auth/login.
    hit = await asyncio.to_thread(store.authenticate_with_hash, user.username,
                                  body.current_password)
    if not hit or hit[0].id != user.id:
        raise HTTPException(401, "Current password is incorrect.")
    verified_hash = hit[1]
    try:
        validate_password(body.new_password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        await asyncio.to_thread(store.set_password, user.id, body.new_password, sb_session,
                                expected_hash=verified_hash)
    except PasswordChangedMeanwhile:
        raise HTTPException(409, PASSWORD_CHANGED_MEANWHILE)
    except KeyError:                    # deleted meanwhile
        raise HTTPException(404, "User not found.")
    return {"ok": True}


@router.put("/me/tokens")
async def update_my_tokens(body: UpdateTokensBody, user: User = Depends(require_user)):
    """Update the signed-in user's scrobble tokens (used by E-11)."""
    store = get_user_store()
    try:
        await asyncio.to_thread(
            store.update, user.id,
            listenbrainz_token=body.listenbrainz_token,
            lastfm_session_key=body.lastfm_session_key,
        )
    except KeyError:
        raise HTTPException(404, "User not found.")
    u = store.get(user.id)
    if u is None:                       # deleted meanwhile
        raise HTTPException(404, "User not found.")
    return {"user": _pub(u)}


class SubsonicPasswordBody(BaseModel):
    """``password``: a non-empty string sets the Subsonic app password;
    null / empty removes it.  The client picks the value (the web UI
    generates a long random one and shows it once)."""
    password: str | None = Field(default=None, max_length=1024,
                                 description="Plaintext Subsonic app password, or empty to remove")


@router.put("/me/subsonic-password")
async def update_my_subsonic_password(
    body: SubsonicPasswordBody,
    user: User = Depends(require_user),
):
    """Set or remove the signed-in user's Subsonic app password — the
    secret Subsonic token-mode sign-in (``t = md5(password + salt)``) is
    checked against, which needs it in plaintext.  Kept separate from the
    scrypt-hashed login password: a login or a login-password change never
    overwrites a password set here, and a removal stays a removal (token
    sign-in off) until a new one is set.  Apps that send the password
    itself (``p=``) keep working with the login password either way."""
    store = get_user_store()
    pw = (body.password or "").strip()  # empty string → remove; non-empty → set
    try:
        await asyncio.to_thread(store.update, user.id, subsonic_password=pw,
                                subsonic_password_custom=bool(pw))
    except KeyError:
        raise HTTPException(404, "User not found.")
    u = store.get(user.id)
    if u is None:                       # deleted meanwhile
        raise HTTPException(404, "User not found.")
    return {"user": _pub(u)}


class CreateApiKeyBody(BaseModel):
    name: str = Field(default="", max_length=64,
                      description="Label shown in the key list (e.g. the client's name)")


# OpenSubsonic ``apiKeyAuthentication``: per-user keys a Subsonic client sends
# as ``apiKey=`` instead of a username + password.  The plaintext is returned
# ONCE, by the create call; the server keeps only its sha256.

@router.get("/me/api-keys")
async def list_my_api_keys(user: User = Depends(require_user)):
    """The signed-in user's Subsonic API keys (id, name, created, last used)."""
    return {"keys": get_user_store().list_api_keys(user.id)}


@router.post("/me/api-keys")
async def create_my_api_key(body: CreateApiKeyBody, user: User = Depends(require_user)):
    """Mint a Subsonic API key.  ``key`` in the response is the only time the
    plaintext is ever shown."""
    try:
        key, ent = await asyncio.to_thread(get_user_store().create_api_key, user.id, body.name)
    except KeyError:                    # deleted meanwhile
        raise HTTPException(404, "User not found.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"key": key, **ent}


@router.delete("/me/api-keys/{key_id}")
async def revoke_my_api_key(key_id: str, user: User = Depends(require_user)):
    """Revoke one of the signed-in user's API keys (immediately invalid)."""
    if not await asyncio.to_thread(get_user_store().revoke_api_key, user.id, key_id):
        raise HTTPException(404, "API key not found.")
    return {"ok": True}


# ── Saved play queue (shared with Subsonic) ─────────────────────────────────
# The same per-user record Subsonic's savePlayQueue / getPlayQueue (and the
# indexBasedQueue variants) read and write (core/subsonic_state.py), so a
# queue saved in the web / mobile player can be resumed in a Subsonic app and
# the other way round.  Ids only: the client hydrates them in one request
# (``POST /api/tracks/meta/batch``).  Both handlers stay on the event loop.

class PlayQueueBody(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=10_000,
                           description="Song ids in play order (a tune: '<id>~<n>'); "
                                       "empty clears the saved queue")
    current_index: int | None = Field(default=None, ge=0,
                                      description="Index of the current song in ids")
    position: int = Field(default=0, ge=0, description="Position in the current song, ms")
    client: str = Field(default="SoniqBoom Web", max_length=64,
                        description="Shown to other players as who saved the queue")


@router.get("/me/play-queue")
async def get_my_play_queue(user: User = Depends(require_user)):
    """The signed-in user's saved play queue — ``{ids, current,
    current_index, position (ms), changed (epoch s), changed_by}`` — or
    ``{}`` when none is saved."""
    from soniqboom.core.subsonic_state import get_state
    return get_state().play_queue(user.id) or {}


@router.put("/me/play-queue")
async def save_my_play_queue(body: PlayQueueBody, user: User = Depends(require_user)):
    """Save (empty ``ids``: clear) the signed-in user's play queue — what a
    Subsonic app's getPlayQueue then resumes.  Unusable ids are dropped and
    the list capped by the state layer, as for savePlayQueue.  A JSON body
    (never a form post) keeps this out of reach of cross-site forms.
    Answers ``{ok, changed}`` — the saved queue's ``changed`` stamp (epoch s,
    as GET reports it; ``null`` after a clear), so the player needs no
    follow-up GET to learn it."""
    from soniqboom.core import subsonic_state
    ids, idx = body.ids, body.current_index
    ok = lambda i: 0 < len(i) <= subsonic_state._MAX_ID_LEN      # noqa: E731
    keep = [i for i in ids if ok(i)]
    if len(keep) != len(ids) and idx is not None and idx < len(ids):
        # The state layer drops unusable ids: keep the index on the SAME song.
        idx = sum(1 for i in ids[:idx] if ok(i)) if ok(ids[idx]) else None
    changed = await subsonic_state.get_state().save_play_queue(
        user.id, keep, current_index=idx, position=body.position,
        changed_by=(body.client or "").strip() or "SoniqBoom Web")
    return {"ok": True, "changed": changed}


# ── Admin: user management ──────────────────────────────────────────────────

class AccountSettingsBody(BaseModel):
    self_registration: bool | None = None
    strong_passwords: bool | None = None


def _account_settings() -> dict:
    from soniqboom.config import is_self_registration_enabled, is_strong_passwords_required
    return {"self_registration": is_self_registration_enabled(),
            "strong_passwords": is_strong_passwords_required()}


@router.get("/users/settings")
async def get_account_settings(_admin: User = Depends(require_admin)):
    """Settings → Users: self-registration and the strong-password rule."""
    return _account_settings()


@router.put("/users/settings")
async def set_account_settings(body: AccountSettingsBody, admin: User = Depends(require_admin)):
    from soniqboom import config
    if body.self_registration is not None:
        config.set_self_registration_enabled(body.self_registration)
        log.info("self-registration %s by '%s'",
                 "enabled" if body.self_registration else "disabled", admin.username)
    if body.strong_passwords is not None:
        config.set_strong_passwords_required(body.strong_passwords)
        log.info("strong passwords %s by '%s'",
                 "required" if body.strong_passwords else "not required", admin.username)
    return _account_settings()


@router.get("/users")
async def list_users(_admin: User = Depends(require_admin)):
    store = get_user_store()
    return {"users": [_pub(u) for u in store.list_users()]}


@router.post("/users")
async def create_user(
    body: CreateUserBody,
    _admin: User = Depends(require_admin),
):
    store = get_user_store()
    try:
        validate_username(body.username)
        validate_password(body.password)
        # (scrypt + a durable users.json write: off the event loop —
        # ``UserStore`` serialises its writers with its own locks)
        user = await asyncio.to_thread(
            store.create,
            username=body.username,
            password=body.password,
            role=body.role,
            display_name=body.display_name,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"user": _pub(user)}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    body: UpdateUserBody,
    admin: User = Depends(require_admin),
):
    store = get_user_store()
    target = store.get(user_id)
    if not target:
        raise HTTPException(404, "User not found.")
    # Guard rails: an admin cannot demote / disable themselves if they
    # would be the last enabled admin remaining.
    if target.id == admin.id and (
        (body.role is not None and body.role != "admin")
        or (body.enabled is False)
    ):
        others = [
            u for u in store.list_users()
            if u.id != admin.id and u.role == "admin" and u.enabled
        ]
        if not others:
            raise HTTPException(
                400,
                "You're the last enabled admin — promote another user "
                "to admin first.",
            )
    # Snapshot pre-update state so we can detect demote / disable below.
    was_rank = role_rank(target.role)
    was_enabled = target.enabled
    try:
        target = await asyncio.to_thread(
            store.update, user_id,
            role=body.role,
            enabled=body.enabled,
            display_name=body.display_name,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, "User not found.")
    # If the user was demoted (to any lower role) or disabled, slam any open
    # WebSocket connections so they can't keep receiving server-pushed
    # events under the previous privilege level (their sessions ended with
    # it — ``UserStore.update``).  Cheap no-op when the user has no live
    # sockets.
    demoted = role_rank(target.role) < was_rank
    disabled = was_enabled and not target.enabled
    if demoted or disabled:
        try:
            closed = await close_open_ws_for(target.id)
            if closed:
                log.info(
                    "Closed %d WebSocket(s) for user %s (%s)",
                    closed, target.username,
                    "disabled" if disabled else "demoted",
                )
        except Exception:
            log.exception("Error closing WSs for user %s", target.username)
    return {"user": _pub(target)}


@router.post("/users/{user_id}/password")
async def admin_set_password(
    user_id: str,
    body: AdminSetPasswordBody,
    _admin: User = Depends(require_admin),
    sb_session: str | None = Cookie(default=None),
):
    """Set a user's password: every session of that account ends — the
    calling admin's own stays when it is their own account."""
    store = get_user_store()
    if not store.get(user_id):
        raise HTTPException(404, "User not found.")
    keep = sb_session if user_id == _admin.id else None
    try:
        await asyncio.to_thread(store.set_password, user_id, body.new_password, keep)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, "User not found.")
    return {"ok": True}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    admin: User = Depends(require_admin),
):
    if user_id == admin.id:
        raise HTTPException(
            400, "You can't delete your own account while signed in.",
        )
    store = get_user_store()
    try:
        await asyncio.to_thread(store.delete, user_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Tear down any live WebSocket sessions the now-deleted user held.
    try:
        closed = await close_open_ws_for(user_id)
        if closed:
            log.info("Closed %d WebSocket(s) for deleted user %s", closed, user_id)
    except Exception:
        log.exception("Error closing WSs for deleted user %s", user_id)
    # Their Subsonic state (stars, ratings, play queue, bookmarks) and
    # now-playing entries go too — the same cleanup as Subsonic deleteUser.
    # Imported here: api/subsonic imports this module.  A failure is logged,
    # never turned into an error for a delete that already happened.
    try:
        from soniqboom.api.subsonic import forget_user
        await forget_user(user_id)
    except Exception:
        log.exception("Error dropping Subsonic state for deleted user %s", user_id)
    return {"ok": True}
