# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Multi-room sync — named rooms with master/slave WebSocket sync."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

log = logging.getLogger("soniqboom.multiroom")

router = APIRouter(prefix="/multiroom", tags=["multiroom"])


def _now_ms() -> int:
    return int(time.time() * 1000)


def _mono_ms() -> int:
    return int(time.monotonic() * 1000)


@dataclass
class Client:
    client_id: str
    ws: WebSocket
    label: str
    role: str = "slave"        # "master" | "slave"
    last_ping_ts: int = 0


@dataclass
class Room:
    room_id: str
    room_name: str
    clients: dict[str, Client] = field(default_factory=dict)
    master_id: str | None = None
    last_state: dict[str, Any] | None = None   # most recent state_update from master
    current_track: dict[str, Any] | None = None  # last broadcast track (for landing preview)
    # Serialises the master-promotion check so two clients sending
    # ``take_master`` concurrently can't both win.
    master_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_rooms: dict[str, Room] = {}
_rooms_lock = asyncio.Lock()


# ── Internal helpers ─────────────────────────────────────────────────────────

async def _broadcast(room_id: str, data: dict, exclude: str | None = None) -> None:
    """Send `data` to every client in `room_id` except optionally one.

    Parallel fan-out with per-client timeout — a single back-pressured
    multiroom slave used to stall every other listener (and master) on
    every state_update / play_at tick.
    """
    room = _rooms.get(room_id)
    if not room:
        return
    targets = [
        (cid, client) for cid, client in list(room.clients.items())
        if not (exclude and cid == exclude)
    ]
    if not targets:
        return

    async def _send(cid, client):
        try:
            await asyncio.wait_for(client.ws.send_json(data), timeout=2.0)
            return None
        except Exception:
            return cid

    results = await asyncio.gather(
        *(_send(cid, client) for cid, client in targets),
        return_exceptions=True,
    )
    for r in results:
        if r is not None and not isinstance(r, BaseException):
            room.clients.pop(r, None)


def _roster_payload(room: Room) -> list[dict]:
    return [
        {"client_id": c.client_id, "label": c.label, "role": c.role}
        for c in room.clients.values()
    ]


async def _send_roster(room: Room) -> None:
    await _broadcast(room.room_id, {
        "type": "roster",
        "ts": _now_ms(),
        "clients": _roster_payload(room),
    })


async def _promote_if_needed(room: Room) -> None:
    """If the room has no master, broadcast master_changed{master_id: null}."""
    await _broadcast(room.room_id, {
        "type": "master_changed",
        "ts": _now_ms(),
        "master_id": room.master_id,
    })


# ── REST endpoints ───────────────────────────────────────────────────────────

@router.get("/rooms")
async def list_rooms():
    """Snapshot of all active rooms (for the landing page)."""
    out = []
    for r in _rooms.values():
        out.append({
            "room_id":       r.room_id,
            "room_name":     r.room_name,
            "client_count":  len(r.clients),
            "has_master":    r.master_id is not None,
            "current_track": (
                {"title": r.current_track.get("title"),
                 "artist": r.current_track.get("artist")}
                if r.current_track else None
            ),
        })
    return out


@router.get("/state/{room_id}")
async def room_state(room_id: str):
    """Debug snapshot of a single room."""
    r = _rooms.get(room_id)
    if not r:
        raise HTTPException(404, f"No room: {room_id}")
    return {
        "room_id":   r.room_id,
        "room_name": r.room_name,
        "master_id": r.master_id,
        "clients":   _roster_payload(r),
        "last_state": r.last_state,
    }


# ── WebSocket endpoint ──────────────────────────────────────────────────────

def _ws_auth_ok(ws: WebSocket) -> bool:
    """Gate a WS on the sb_session cookie.  Pre-bootstrap installs (no
    users at all) keep the old anonymous-open behaviour so single-user
    setups aren't broken on upgrade."""
    try:
        from soniqboom.core.users import get_user_store
        store = get_user_store()
    except Exception:
        return True
    if not store.has_any():
        return True
    cookie = ws.cookies.get("sb_session") if hasattr(ws, "cookies") else None
    if not cookie:
        return False
    user = store.lookup_session(cookie)
    return user is not None and user.enabled


def _ws_session_still_valid(ws: WebSocket) -> bool:
    """Cheap revalidation called on every incoming message.

    Without this, a user disabled or demoted mid-session would keep
    pushing state_update / play_at messages to other clients in the
    room until they disconnected on their own.  Looking the session up
    in the in-memory dict is a single read, so the overhead is
    negligible vs the cost of an audio decision being driven by a
    revoked operator.
    """
    try:
        from soniqboom.core.users import get_user_store
        store = get_user_store()
    except Exception:
        return True
    if not store.has_any():
        return True
    cookie = ws.cookies.get("sb_session") if hasattr(ws, "cookies") else None
    if not cookie:
        return False
    user = store.lookup_session(cookie)
    return user is not None and user.enabled


def _resolve_ws_user_id(ws: WebSocket) -> str | None:
    """Return the user_id behind the WS cookie, or None for pre-bootstrap.

    Used to register the socket with ``api.users`` so an admin demote /
    disable / delete on the user can broadcast-close the socket
    immediately — without this, the multiroom socket survived
    revocation until the next inbound message (R2 finding).
    """
    try:
        from soniqboom.core.users import get_user_store
        store = get_user_store()
    except Exception:
        return None
    if not store.has_any():
        return None
    cookie = ws.cookies.get("sb_session") if hasattr(ws, "cookies") else None
    if not cookie:
        return None
    user = store.lookup_session(cookie)
    return user.id if user else None


@router.websocket("/ws")
async def multiroom_ws(ws: WebSocket):
    """One WS endpoint handles all rooms; first `hello` message assigns room."""
    if not _ws_auth_ok(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    # Register this socket with the user registry so admin-driven
    # revocation can close it.  Lazy-import to avoid circular deps.
    _registered_user_id: str | None = _resolve_ws_user_id(ws)
    if _registered_user_id:
        try:
            from soniqboom.api.users import register_open_ws
            register_open_ws(_registered_user_id, ws)
        except Exception:
            _registered_user_id = None
    client: Client | None = None
    room: Room | None = None

    try:
        while True:
            msg = await ws.receive_json()
            # Per-message auth re-check: a user disabled/demoted between
            # initial WS accept and now must stop driving the room.  Close
            # with 4401 (custom "auth revoked") so the client surfaces it
            # distinctly from a transport error.
            if not _ws_session_still_valid(ws):
                try:
                    await ws.close(code=4401)
                except Exception:
                    pass
                return
            mtype = msg.get("type")

            # ── First message must be `hello` ──────────────────────────────
            if client is None:
                if mtype != "hello":
                    await ws.send_json({
                        "type": "error", "ts": _now_ms(),
                        "code": "NO_HELLO", "message": "First message must be 'hello'",
                    })
                    await ws.close(code=1008)
                    return

                client_id   = msg.get("client_id") or str(uuid.uuid4())
                room_id     = msg.get("room_id")   or str(uuid.uuid4())
                room_name   = (msg.get("room_name") or "Room").strip()[:64]
                role_wanted = msg.get("role_wanted", "slave")
                label       = (msg.get("label") or "Device").strip()[:64]

                async with _rooms_lock:
                    room = _rooms.get(room_id)
                    if room is None:
                        room = Room(room_id=room_id, room_name=room_name)
                        _rooms[room_id] = room
                        log.info("Room created: %s (%s)", room_id, room_name)

                    client = Client(client_id=client_id, ws=ws, label=label)

                    if role_wanted == "master" and room.master_id is None:
                        client.role = "master"
                        room.master_id = client_id
                    else:
                        client.role = "slave"

                    room.clients[client_id] = client

                # Snapshot reply to this client
                await ws.send_json({
                    "type": "welcome", "ts": _now_ms(),
                    "your_role":   client.role,
                    "client_id":   client.client_id,
                    "room_id":     room.room_id,
                    "room_name":   room.room_name,
                    "master_id":   room.master_id,
                    "clients":     _roster_payload(room),
                    "last_state":  room.last_state,
                })
                # Tell the rest of the room who joined
                await _send_roster(room)
                continue

            # ── Subsequent messages ────────────────────────────────────────

            if mtype == "ping":
                # Echo pong back with server monotonic for skew estimation
                await ws.send_json({
                    "type": "pong", "ts": _now_ms(),
                    "nonce":         msg.get("nonce"),
                    "clientMonoMs":  msg.get("clientMonoMs"),
                    "serverMonoMs":  _mono_ms(),
                })
                continue

            if mtype == "pong":
                # Server-initiated probes — not used in v1; accept silently.
                continue

            if mtype == "bye":
                break

            if mtype == "take_master":
                # Serialised check-then-set so two clients sending
                # ``take_master`` at the same time can't both become master.
                async with room.master_lock:
                    if room.master_id is None:
                        room.master_id = client.client_id
                        client.role = "master"
                        promoted = True
                    else:
                        promoted = False
                if promoted:
                    await _broadcast(room.room_id, {
                        "type": "master_changed", "ts": _now_ms(),
                        "master_id": room.master_id,
                    })
                    await _send_roster(room)
                else:
                    await ws.send_json({
                        "type": "error", "ts": _now_ms(),
                        "code": "MASTER_LOCKED",
                        "message": "Room already has a master",
                    })
                continue

            if mtype == "ready":
                # Slave finished preloading — forward to the room's master so
                # the barrier can release play_at once all slaves are ready.
                master = room.clients.get(room.master_id) if room.master_id else None
                if master is not None and master.ws is not ws:
                    try:
                        await master.ws.send_json({
                            "type":      "ready",
                            "ts":        _now_ms(),
                            "clientId":  client.client_id,
                            "barrierId": msg.get("barrierId"),
                            "trackId":   msg.get("trackId"),
                        })
                    except Exception:
                        pass
                continue

            # The following messages are master-only.
            if client.role != "master":
                # Slaves silently ignore state-writing messages they shouldn't send.
                continue

            if mtype == "state_update":
                # Authoritative state from master — cache + relay to room.
                room.last_state = {
                    "trackId":         msg.get("trackId"),
                    "position":        msg.get("position", 0),
                    "playing":         msg.get("playing", False),
                    "duration":        msg.get("duration", 0),
                    "track":           msg.get("track"),
                    "sampledAtServer": msg.get("sampledAtServer"),
                    "serverMonoMs":    _mono_ms(),
                }
                room.current_track = msg.get("track")
                await _broadcast(room.room_id, {
                    "type": "state", "ts": _now_ms(),
                    **room.last_state,
                }, exclude=client.client_id)
                continue

            if mtype == "prepare":
                # Master initiated a track-change barrier.
                barrier_id = msg.get("barrierId") or str(uuid.uuid4())
                await _broadcast(room.room_id, {
                    "type": "prepare", "ts": _now_ms(),
                    "trackId":   msg.get("trackId"),
                    "path":      msg.get("path"),
                    "seek":      msg.get("seek", 0),
                    "barrierId": barrier_id,
                    "track":     msg.get("track"),
                }, exclude=client.client_id)
                continue

            if mtype == "play_at":
                # Master picked an absolute wall-clock start time; relay to slaves.
                await _broadcast(room.room_id, {
                    "type": "play_at", "ts": _now_ms(),
                    "serverEpochMs":    msg.get("serverEpochMs"),
                    "positionAtStart":  msg.get("positionAtStart", 0),
                }, exclude=client.client_id)
                continue

            if mtype == "seek":
                await _broadcast(room.room_id, {
                    "type": "seek", "ts": _now_ms(),
                    "position":      msg.get("position", 0),
                    "serverEpochMs": msg.get("serverEpochMs"),
                }, exclude=client.client_id)
                continue

            if mtype == "pause":
                await _broadcast(room.room_id, {
                    "type": "pause", "ts": _now_ms(),
                    "serverEpochMs": msg.get("serverEpochMs"),
                }, exclude=client.client_id)
                continue

            # Unknown message type — ignore silently.

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("multiroom ws error: %s", exc)
    finally:
        if _registered_user_id:
            try:
                from soniqboom.api.users import unregister_open_ws
                unregister_open_ws(_registered_user_id, ws)
            except Exception:
                pass
        if client is not None and room is not None:
            room.clients.pop(client.client_id, None)
            master_vacated = (room.master_id == client.client_id)
            if master_vacated:
                room.master_id = None
                room.last_state = None
            if not room.clients:
                # GC empty room
                _rooms.pop(room.room_id, None)
                log.info("Room removed (empty): %s", room.room_id)
            else:
                if master_vacated:
                    await _broadcast(room.room_id, {
                        "type": "master_changed", "ts": _now_ms(),
                        "master_id": None,
                    })
                    await _broadcast(room.room_id, {
                        "type": "pause", "ts": _now_ms(),
                        "serverEpochMs": _now_ms(),
                    })
                await _send_roster(room)
