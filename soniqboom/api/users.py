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
works).  TTL is 7 days, refreshed on every authed request.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from soniqboom.core.users import (
    get_user_store,
    validate_password,
    validate_username,
)
from soniqboom.models.user import ROLES, Role, User

log = logging.getLogger(__name__)

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

class LoginBody(BaseModel):
    username: str
    password: str


class RegisterBody(BaseModel):
    username: str
    password: str
    display_name: str | None = None


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


class CreateUserBody(BaseModel):
    username: str
    password: str
    role: Literal["admin", "edit", "readonly"] = "readonly"
    display_name: str | None = None


class UpdateUserBody(BaseModel):
    role: Literal["admin", "edit", "readonly"] | None = None
    enabled: bool | None = None
    display_name: str | None = None


class AdminSetPasswordBody(BaseModel):
    new_password: str


class UpdateTokensBody(BaseModel):
    listenbrainz_token: str | None = Field(default=None, description="Empty string clears it")
    lastfm_session_key: str | None = Field(default=None, description="Empty string clears it")


# ── Auth helpers (FastAPI dependencies) ─────────────────────────────────────

def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    """Issue the session cookie.  ``Secure`` is set whenever the wire is
    TLS — honour ``X-Forwarded-Proto`` so deployments behind a TLS-
    terminating reverse proxy (nginx, Caddy, Cloudflare) get the right
    flag.  Without this the cookie would lack ``Secure`` even on real
    HTTPS, leaking to any downgrade attack."""
    fwd = (request.headers.get("x-forwarded-proto") or "").lower().split(",")[0].strip()
    secure = request.url.scheme == "https" or fwd == "https"
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        max_age=7 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


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
require_edit = require_role("admin", "edit")


# ── Public endpoints ─────────────────────────────────────────────────────────

@router.get("/auth/status")
async def auth_status():
    """Public summary of the auth setup — used by the login UI to decide
    whether to show the "Create account" link (only when at least one
    admin already exists; bootstrap creation is via CLI).

    ``data_dir`` is included so the bootstrap hint can show the exact path
    the server reads users.json from — if the operator's shell uses a
    different ``SONIQBOOM_DATA_DIR``, this surfaces the mismatch."""
    from soniqboom.config import get_data_dir
    store = get_user_store()
    return {
        "has_any_user":  store.has_any(),
        "has_any_admin": store.has_any_admin(),
        "registration_open": store.has_any_admin(),
        "session_cookie": _SESSION_COOKIE,
        "data_dir":      str(get_data_dir().absolute()),
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
      only while no enabled admin exists (the bootstrap window).  Afterwards it
      gets 403 and falls back to ``GET /auth/status`` (auth.js).

    Everyone else gets 403: a reload takes the users.json flock and rolls the
    in-memory ``last_login_at`` values back to their last-persisted ones.  500
    when users.json could not be read, so the CLI never reports a change live
    that the server failed to load."""
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
    elif store.has_any_admin():
        raise HTTPException(403, "Reloading user accounts is only allowed "
                                 "from the server's own host or container.")
    if not store.reload():
        raise HTTPException(500, "users.json could not be read — see the server log.")
    return await auth_status()


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
    user = await asyncio.to_thread(store.authenticate, body.username, body.password)
    if not user:
        raise HTTPException(401, "Wrong username or password.")
    token, expiry = store.issue_session(user.id)
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
    try:
        user = store.create(
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
    token, expiry = store.issue_session(user.id)
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
        get_user_store().revoke_session(sb_session)
    _clear_session_cookie(response)
    return {"ok": True}


@router.post("/auth/change-password")
async def change_password(
    body: ChangePasswordBody,
    user: User = Depends(require_user),
):
    import asyncio
    store = get_user_store()
    # scrypt off the event loop — same reason as /auth/login.
    ok = await asyncio.to_thread(store.authenticate, user.username, body.current_password)
    if not ok:
        raise HTTPException(401, "Current password is incorrect.")
    try:
        validate_password(body.new_password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await asyncio.to_thread(store.set_password, user.id, body.new_password)
    return {"ok": True}


@router.put("/me/tokens")
async def update_my_tokens(body: UpdateTokensBody, user: User = Depends(require_user)):
    """Update the signed-in user's scrobble tokens (used by E-11)."""
    store = get_user_store()
    store.update(
        user.id,
        listenbrainz_token=body.listenbrainz_token,
        lastfm_session_key=body.lastfm_session_key,
    )
    return {"user": _pub(store.get(user.id))}


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
    store.update(user.id, subsonic_password=pw, subsonic_password_custom=bool(pw))
    return {"user": _pub(store.get(user.id))}


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
        key, ent = get_user_store().create_api_key(user.id, body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"key": key, **ent}


@router.delete("/me/api-keys/{key_id}")
async def revoke_my_api_key(key_id: str, user: User = Depends(require_user)):
    """Revoke one of the signed-in user's API keys (immediately invalid)."""
    if not get_user_store().revoke_api_key(user.id, key_id):
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
        user = store.create(
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
    was_admin = target.role == "admin"
    was_enabled = target.enabled
    try:
        target = store.update(
            user_id,
            role=body.role,
            enabled=body.enabled,
            display_name=body.display_name,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    # If the user was demoted from admin or disabled, slam any open
    # WebSocket connections so they can't keep receiving server-pushed
    # events under the previous privilege level.  Cheap no-op when the
    # user has no live sockets.
    demoted = was_admin and target.role != "admin"
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
):
    store = get_user_store()
    if not store.get(user_id):
        raise HTTPException(404, "User not found.")
    try:
        store.set_password(user_id, body.new_password)
    except ValueError as e:
        raise HTTPException(400, str(e))
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
        store.delete(user_id)
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
