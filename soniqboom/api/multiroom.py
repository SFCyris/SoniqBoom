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


_rooms: dict[str, Room] = {}
_rooms_lock = asyncio.Lock()


# ── Internal helpers ─────────────────────────────────────────────────────────

async def _broadcast(room_id: str, data: dict, exclude: str | None = None) -> None:
    """Send `data` to every client in `room_id` except optionally one."""
    room = _rooms.get(room_id)
    if not room:
        return
    dead: list[str] = []
    for cid, client in list(room.clients.items()):
        if exclude and cid == exclude:
            continue
        try:
            await client.ws.send_json(data)
        except Exception:
            dead.append(cid)
    for cid in dead:
        room.clients.pop(cid, None)


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

@router.websocket("/ws")
async def multiroom_ws(ws: WebSocket):
    """One WS endpoint handles all rooms; first `hello` message assigns room."""
    await ws.accept()
    client: Client | None = None
    room: Room | None = None

    try:
        while True:
            msg = await ws.receive_json()
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
                # First-come-first-master: only promote if room currently has none
                if room.master_id is None:
                    room.master_id = client.client_id
                    client.role = "master"
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
