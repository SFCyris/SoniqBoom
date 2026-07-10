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
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from soniqboom.core import radiodir

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


async def notify_station_meta(payload: dict) -> None:
    """Relay a station ``radio_meta`` / ``radio_art`` event to any room that is
    currently streaming that station.  Multi-room members live on the multiroom
    WS (not the library WS the relay normally broadcasts over), so without this
    they'd never see the live now-playing title / cover — only the station name.
    Cheap no-op when no room is playing the sid."""
    sid = payload.get("sid")
    if not sid:
        return
    mtype = payload.get("event")             # "radio_meta" | "radio_art"
    for room in list(_rooms.values()):
        ct = room.current_track
        if ct and ct.get("sid") == sid:
            await _broadcast(room.room_id, {**payload, "type": mtype})


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


# ── Jukebox → multiroom bridge ───────────────────────────────────────────────
#
# The Subsonic jukeboxControl API is a server-owned queue with no on-box sound
# card (SoniqBoom plays in browsers).  We realise its audio by making the
# jukebox a *virtual master* of a reserved multiroom room: any SoniqBoom browser
# that joins the "Jukebox" room plays whatever jukeboxControl is driving, using
# the exact prepare/play_at/pause/seek/state messages a human master emits — so
# the existing slave sync engine plays it with no frontend change.  master_id is
# a phantom (never a real client) so joiners don't see a "master left" banner.
JUKEBOX_ROOM_ID = "__jukebox__"
JUKEBOX_MASTER_ID = "__jukebox_master__"
# Change-detector baseline, keyed by room id so it resets when the room is
# (re)created; the lock serialises the whole notify so two concurrent
# jukeboxControl requests can't interleave a check-then-act across an await.
_jb_last: dict[str, dict] = {}
_jb_bridge_lock = asyncio.Lock()


async def notify_jukebox_room() -> None:
    """Push the current jukebox state to the Jukebox room.  Called after every
    jukeboxControl mutation.  Best-effort — a bridge hiccup must never fail the
    Subsonic command."""
    try:
        from soniqboom.core.jukebox import get_jukebox
        from soniqboom.core.store import get_store
        async with _jb_bridge_lock:
            jb = get_jukebox()
            st = jb.status()
            tid = jb.current_id()
            pos = jb.position()          # float seconds — no int-floor sync drift
            track = get_store().get_track(tid) if tid else None

            # Create/fetch under _rooms_lock — the SAME lock the WS hello handler
            # uses to mutate _rooms — so the two room-creation paths never race
            # (defensive: the critical section is await-free, so on the single
            # event loop it is already atomic, but sharing the lock removes the
            # landmine if a worker or an await is ever added here).
            async with _rooms_lock:
                room = _rooms.get(JUKEBOX_ROOM_ID)
                if room is None:
                    room = Room(room_id=JUKEBOX_ROOM_ID, room_name="Jukebox")
                    _rooms[JUKEBOX_ROOM_ID] = room
                    _jb_last.pop(JUKEBOX_ROOM_ID, None)   # fresh room → fresh baseline
                # The jukebox is the (phantom) master of its room; a real WS
                # client can only ever join it as a slave (enforced in the hello
                # handler), so master_id is only ever None or JUKEBOX_MASTER_ID.
                if room.master_id is None or room.master_id == JUKEBOX_MASTER_ID:
                    room.master_id = JUKEBOX_MASTER_ID

            last = _jb_last.get(JUKEBOX_ROOM_ID) or {"trackId": None, "playing": None}

            # Cache the state so a late joiner syncs immediately from `welcome`.
            # `gain` rides along for a future gain-aware sink (the browser sink
            # currently uses its own volume; jukebox gain is control-plane only).
            room.last_state = {
                "trackId":      tid,
                # Float seconds, matching the prepare/play_at/seek broadcasts —
                # a late joiner syncing from `welcome.last_state` gets the same
                # sub-second precision as a live in-room joiner (status()'s int
                # floor here would re-introduce the ~1s drift position() avoids).
                "position":     pos,
                "playing":      st["playing"],
                "gain":         st["gain"],
                "duration":     (track or {}).get("duration", 0),
                "track":        track,
                "serverMonoMs": _mono_ms(),
            }
            room.current_track = track

            # Decide, then COMMIT the baseline BEFORE broadcasting — so a
            # mid-broadcast error can't leave the change-detector stale (the lock
            # already rules out a concurrent writer).
            track_changed = tid != last["trackId"]
            was_playing   = last["playing"]
            _jb_last[JUKEBOX_ROOM_ID] = {"trackId": tid, "playing": st["playing"]}

            if track_changed:
                if tid:
                    # Load the new track on every member (barrier prepare), then,
                    # if playing, start it.  Fire-and-forget play_at (no ready-ack
                    # wait): a jukebox sink is typically a single device.
                    await _broadcast(JUKEBOX_ROOM_ID, {
                        "type": "prepare", "ts": _now_ms(),
                        "trackId": tid, "track": track,
                        "seek": pos, "barrierId": str(uuid.uuid4()),
                    })
                    if st["playing"]:
                        await _broadcast(JUKEBOX_ROOM_ID, {
                            "type": "play_at", "ts": _now_ms(),
                            "serverEpochMs": _now_ms() + 600, "positionAtStart": pos,
                        })
                else:
                    # Queue emptied (clear / removed the last track) — stop the
                    # sink instead of leaving it playing the old track.
                    await _broadcast(JUKEBOX_ROOM_ID, {
                        "type": "pause", "ts": _now_ms(), "serverEpochMs": _now_ms(),
                    })
            else:
                # Same track — sync play / pause / position.
                if st["playing"] and was_playing is not True:
                    await _broadcast(JUKEBOX_ROOM_ID, {
                        "type": "play_at", "ts": _now_ms(),
                        "serverEpochMs": _now_ms() + 200, "positionAtStart": pos,
                    })
                elif not st["playing"] and was_playing is not False:
                    await _broadcast(JUKEBOX_ROOM_ID, {
                        "type": "pause", "ts": _now_ms(), "serverEpochMs": _now_ms(),
                    })
                elif st["playing"]:
                    await _broadcast(JUKEBOX_ROOM_ID, {
                        "type": "seek", "ts": _now_ms(),
                        "position": pos, "serverEpochMs": _now_ms(),
                    })
    except Exception:
        log.debug("jukebox→multiroom bridge notify failed", exc_info=True)


# ── REST endpoints ───────────────────────────────────────────────────────────

@router.get("/rooms")
async def list_rooms():
    """Snapshot of all active rooms (for the landing page)."""
    out = []
    for r in _rooms.values():
        # The reserved jukebox room is a phantom-mastered audio sink for the
        # Subsonic jukebox bridge — it must never appear as a user-joinable room
        # in the landing-page list.
        if r.room_id == JUKEBOX_ROOM_ID:
            continue
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
    # The reserved jukebox room is internal to the Subsonic bridge; don't expose
    # its debug state (keep it fully invisible to the REST surface).
    if not r or room_id == JUKEBOX_ROOM_ID:
        raise HTTPException(404, f"No room: {room_id}")
    return {
        "room_id":   r.room_id,
        "room_name": r.room_name,
        "master_id": r.master_id,
        "clients":   _roster_payload(r),
        "last_state": r.last_state,
    }


class PlayStationBody(BaseModel):
    sid: str
    v: int = 0


@router.post("/{room_id}/play_station")
async def play_station(room_id: str, body: PlayStationBody):
    """Stream an internet-radio station to every member of a room.

    Unlike library-track playback (master-driven, position-synced), a live
    station has no seekable position: every member just loads the shared,
    hub-backed relay URL and plays at the live edge.  Because they all pull the
    SAME StationHub, one upstream connection feeds the whole room — this is the
    feature the hub was built for.  Any authenticated user may push a station to
    a room (the endpoint is behind the /api auth middleware).
    """
    room = _rooms.get(room_id)
    if not room:
        raise HTTPException(404, f"No room: {room_id}")
    st = await radiodir.resolve_station(body.sid)
    if not st:
        raise HTTPException(404, "Unknown station")
    streams = st.get("streams") or []
    if not streams:
        raise HTTPException(404, "Station has no playable streams")
    v = min(max(body.v, 0), len(streams) - 1)
    # Same-origin, hub-backed URL every member (browser) pulls.  The sid is
    # path-encoded; the relay route re-validates it (SSRF) and shares one hub.
    url = f"/api/stations/relay/{quote(body.sid, safe='')}?v={v}"
    track = {
        "id":         f"station:{body.sid}",
        "sid":        body.sid,
        "title":      st.get("name") or "Live radio",
        "artist":     "Live radio",
        "is_station": True,
        "cover_art":  st.get("favicon") or None,
        "url":        url,
    }
    room.current_track = track
    # Broadcast a live-stream directive to EVERY member (master + slaves): load
    # the URL and play now, bypassing the seekable-position sync path.
    await _broadcast(room_id, {
        "type":    "play_station",
        "ts":      _now_ms(),
        "sid":     body.sid,
        "v":       v,
        "url":     url,
        "station": {"name": st.get("name"), "favicon": st.get("favicon")},
        "track":   track,
    })
    log.info("Room %s → play station %s (%s)", room_id, body.sid, st.get("name"))
    return {"ok": True, "room_id": room_id, "sid": body.sid, "v": v, "url": url}


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

                    if room_id == JUKEBOX_ROOM_ID:
                        # Reserved jukebox room: the Subsonic jukebox is the
                        # permanent phantom master.  A real WS client may only
                        # ever be a slave sink here — it can neither seize
                        # mastership on join nor (see take_master) via take_master,
                        # so it can never hijack or, on leaving, vacate the room.
                        room.master_id = JUKEBOX_MASTER_ID
                        client.role = "slave"
                    elif role_wanted == "master" and room.master_id is None:
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
                if room.room_id == JUKEBOX_ROOM_ID:
                    # The reserved jukebox room is always phantom-mastered by the
                    # Subsonic bridge; a client can never take it.
                    await ws.send_json({
                        "type": "error", "ts": _now_ms(),
                        "code": "MASTER_LOCKED",
                        "message": "The jukebox room is controlled by the server.",
                    })
                    continue
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
            is_jukebox = room.room_id == JUKEBOX_ROOM_ID
            # A real client is never the jukebox room's master (it always joins
            # as a slave), so master_vacated is already False there — this guard
            # is belt-and-braces so a departing sink can never null the phantom
            # master or wipe the queue's last_state.
            master_vacated = (not is_jukebox
                              and room.master_id == client.client_id)
            if master_vacated:
                room.master_id = None
                room.last_state = None
            if not room.clients and not is_jukebox:
                # GC empty room — but never the reserved jukebox room: the bridge
                # keeps it alive across the server lifetime (0 clients is normal)
                # so its last_state survives for the next sink that joins.
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
