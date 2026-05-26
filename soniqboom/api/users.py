# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""User authentication + management API.

Endpoints fall into three groups:

1. **Public** — ``/auth/login``, ``/auth/register``, ``/auth/me``.
2. **Authed** — ``/auth/logout``, ``/auth/change-password``, ``/me/tokens``.
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


def current_user(
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


def require_user(user: User | None = Depends(current_user)) -> User:
    """Reject if there's no signed-in user."""
    if user is None:
        raise HTTPException(401, "Not signed in.")
    return user


def require_role(*allowed: Role):
    """Build a FastAPI dependency that lets through only listed roles."""
    def _dep(user: User = Depends(require_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(403, f"Requires role {' or '.join(allowed)}.")
        return user
    return _dep


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin role required.")
    return user


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
        "data_dir":      str(get_data_dir()),
    }


@router.post("/auth/reload")
async def auth_reload():
    """Re-read users.json from disk.  Used by the "re-check" affordance
    on the login overlay after the operator runs ``soniqboom-setadm``
    so the running server sees the new admin without a process restart.

    Public on purpose — the worst a hostile caller can do is force a
    file-system read of users.json (which the server already did at
    startup) and observe the new ``/auth/status`` payload."""
    store = get_user_store()
    store.reload()
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
    return {"user": user.to_public(), "expires_at": expiry}


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
    return {"user": user.to_public(), "expires_at": expiry}


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
        "user": user.to_public(),
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
    return {"user": store.get(user.id).to_public()}


class SubsonicPasswordBody(BaseModel):
    """``password`` field: send a non-empty string to set, or null/empty
    to clear.  We don't accept a "generate me one" flag here — the
    client (Settings → My Account UI, or curl) is responsible for
    picking a random value, which keeps the endpoint API-symmetric
    with the scrobble-token endpoint above."""
    password: str | None = Field(default=None, description="Plaintext Subsonic API password, or empty to clear")


@router.put("/me/subsonic-password")
async def update_my_subsonic_password(
    body: SubsonicPasswordBody,
    user: User = Depends(require_user),
):
    """Set or clear the signed-in user's Subsonic API password.

    The Subsonic spec's token auth (``?u&s&t``) demands the server hold
    the plaintext password to recompute ``md5(password + salt)``.  We
    keep that *separate* from the scrypt-hashed main password so the
    main browser-login credential is never weakened.  Third-party
    Subsonic clients (Amperfy, DSub, Symfonium, play:Sub) use this
    password — recommend a long random string.
    """
    store = get_user_store()
    pw = (body.password or "").strip()  # empty string → clear; non-empty → set
    store.update(user.id, subsonic_password=pw)
    return {"user": store.get(user.id).to_public()}


# ── Admin: user management ──────────────────────────────────────────────────

@router.get("/users")
async def list_users(_admin: User = Depends(require_admin)):
    store = get_user_store()
    return {"users": [u.to_public() for u in store.list_users()]}


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
    return {"user": user.to_public()}


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
    return {"user": target.to_public()}


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
    return {"ok": True}
