# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cast / AirPlay / DLNA control surface (E-CAST-V2).

The frontend hits these endpoints to drive playback on a Cast /
AirPlay / DLNA target on the local network.  Discovery happens in
``cast_targets`` (already wired); per-protocol byte delivery happens
in ``cast_stream`` at the anonymous ``/cast/{token}/{filename}``
endpoint; this file is the *control* surface only.

Endpoint roster (every endpoint requires an authenticated user):

  GET  /api/cast/status          – which discovery backends are installed
  GET  /api/cast/targets         – discovered targets on the LAN
  GET  /api/cast/sessions        – currently-active per-target sessions
  POST /api/cast/play            – body { target_id, track_id } – start a track
  POST /api/cast/queue           – body { target_id, items: [{track_id,…}] }
  POST /api/cast/control         – body { target_id, action, ...args }
  GET  /api/cast/position/{tid}  – live playback position for a target
  POST /api/cast/preference      – body { target_id, pref } – set codec pref
  POST /api/cast/disconnect      – body { target_id } – tear down a session
  GET  /api/cast/telemetry       – stats / p95 latency dashboard
  DELETE /api/cast/telemetry     – wipe the local telemetry ring buffer
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from soniqboom.api.users import current_user, require_user, require_admin
from soniqboom.core import cast_session, cast_telemetry, cast_targets

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cast", tags=["cast"])

_INSTALL_HINTS = {
    "cast":    "pip install pychromecast",
    "airplay": "pip install pyatv",
    "dlna":    "pip install async-upnp-client",
}


# ── Pydantic bodies ───────────────────────────────────────────────────────

class PlayBody(BaseModel):
    target_id: str = Field(..., min_length=1, max_length=256)
    track_id:  str = Field(..., min_length=1, max_length=128)
    # Sub-song selector for SID / tracker / GME formats.  0-indexed
    # (matches sidplayfp / openmpt123 / libgme conventions).  Capped
    # at 4096 — no real container has more tunes than that.
    subsong:   int = Field(0, ge=0, le=4096)


class QueueItemBody(BaseModel):
    track_id:      str = Field(..., min_length=1, max_length=128)
    title:         str = ""
    artist:        str = ""
    album:         str = ""
    duration_s:    float = Field(0.0, ge=0.0)
    album_art_url: str = ""
    subsong:       int = Field(0, ge=0, le=4096)


class QueueBody(BaseModel):
    target_id: str = Field(..., min_length=1, max_length=256)
    # Cap the queue length to keep a hostile client from OOMing the
    # per-track get_track loop in cast_session.queue_load.  A normal
    # listening session is < 100 tracks; 1000 leaves slack.
    items:     list[QueueItemBody] = Field(..., max_length=1000)


class ControlBody(BaseModel):
    target_id: str = Field(..., min_length=1, max_length=256)
    action:    Literal["pause", "resume", "stop", "seek", "next", "prev"]
    seconds:   float = Field(0.0, ge=0.0, le=86400.0, description="for action=seek")


class PreferenceBody(BaseModel):
    target_id: str = Field(..., min_length=1, max_length=256)
    pref:      Literal["auto", "force-mp3", "force-flac", "force-original"]


class TargetIdBody(BaseModel):
    target_id: str = Field(..., min_length=1, max_length=256)


# ── Helpers ───────────────────────────────────────────────────────────────

async def _resolve_target(target_id: str) -> cast_targets.CastTarget:
    """Look up a target by id.  We re-run discovery (fast — uses
    cached SSDP responses) rather than maintaining a long-lived
    inventory, since users join/leave the LAN frequently."""
    targets = await cast_targets.discover(timeout=4.0)
    for t in targets:
        if t.id == target_id:
            return t
    raise HTTPException(404, "Target no longer visible on the network.")


def _user_field(user) -> str | None:
    """Best-effort user-identity for token claims + audit log.
    Different code paths in SoniqBoom return user objects with
    different attribute names — be liberal."""
    if user is None:
        return None
    for attr in ("id", "username", "name"):
        val = getattr(user, attr, None)
        if val is not None:
            return str(val)
    return None


# ── Routes: discovery + status ─────────────────────────────────────────────

@router.get("/status")
async def cast_status(_user = Depends(current_user)):
    """Report which discovery backends are installed.  The UI shows
    actionable install hints when a protocol is missing."""
    status = cast_targets.backend_status()
    hints = {p: _INSTALL_HINTS[p] for p, ok in status.items() if not ok}
    return {"available": status, "install_hints": hints}


@router.get("/targets")
async def cast_targets_route(_user = Depends(current_user)):
    """LAN-visible Cast / AirPlay / DLNA targets.  Returns the same
    shape as before (the prior 1-line stub) so existing UI code
    continues to work."""
    targets = await cast_targets.discover(timeout=4.0)
    return {"targets": [t.to_public() for t in targets]}


@router.get("/sessions")
async def cast_sessions(_user = Depends(current_user)):
    """Currently-active per-target sessions on the server side."""
    sessions = await cast_session.list_sessions()
    return {"sessions": [s.state.to_public() for s in sessions]}


# ── Routes: playback control ───────────────────────────────────────────────

@router.post("/play")
async def cast_play(body: PlayBody, user = Depends(require_user)):
    """Start ``track_id`` on ``target_id``.  Idempotent: a second
    /play to the same target replaces whatever was playing."""
    target = await _resolve_target(body.target_id)
    session = await cast_session.get_session(target)
    try:
        result = await session.play_track(
            track_id = body.track_id,
            user_id  = _user_field(user),
            subsong  = body.subsong,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except RuntimeError as exc:
        # Missing optional dependency (pychromecast / pyatv / async-upnp-
        # client).  This is a permanent server-config state, not a
        # transient renderer outage — 501 Not Implemented lets the
        # frontend distinguish "install hint" from "device offline".
        # Curate the message so we don't leak internal import paths.
        log.info("cast/play missing backend: %s", exc)
        raise HTTPException(
            501,
            "This cast protocol isn't enabled on the server. "
            "See /api/cast/status for install instructions.",
        )
    except Exception as exc:
        # AirPlay device requires pairing → distinct response so the UI
        # can pop the PIN-entry modal instead of a generic toast.  We
        # use 412 Precondition Failed because the play *request* is
        # valid; it's the *target state* (unpaired) that blocks it.
        # Imported lazily so non-AirPlay deployments don't pull pyatv.
        try:
            from soniqboom.core.cast_airplay import PairingRequiredError
        except ImportError:
            PairingRequiredError = None  # type: ignore[assignment]
        if PairingRequiredError is not None and isinstance(exc, PairingRequiredError):
            log.info("cast/play target=%s needs pairing", body.target_id)
            raise HTTPException(
                412,
                detail={
                    "requires_pairing": True,
                    "identifier": getattr(exc, "identifier", body.target_id),
                    "target_id":  body.target_id,
                    "message":    "AirPlay device requires pairing — enter the PIN shown on the device.",
                },
            )
        log.exception("cast/play failed for target=%s track=%s", body.target_id, body.track_id)
        raise HTTPException(502, "Could not reach the cast target — is it still on the network?")
    return {"ok": True, **result}


# ── Routes: AirPlay pairing ────────────────────────────────────────────────

class _PairBeginBody(BaseModel):
    target_id: str = Field(..., min_length=1, max_length=256)


class _PairFinishBody(BaseModel):
    target_id: str = Field(..., min_length=1, max_length=256)
    pin:       str = Field(..., min_length=1, max_length=16,
                           description="The 4-digit code shown on the AirPlay device.")


@router.post("/airplay/pair/begin")
async def cast_airplay_pair_begin(
    body: _PairBeginBody, user = Depends(require_user),
):
    """Start an AirPlay pairing handshake — the device should display a PIN.

    The session created here lives on the controller for the duration of
    the pair flow; the matching POST to ``/airplay/pair/finish`` consumes
    the same handler and submits the PIN.  If the user abandons the
    flow, the handler is cleaned up on the next connect / disconnect.
    """
    target = await _resolve_target(body.target_id)
    if target.protocol != "airplay":
        raise HTTPException(400, "Pairing is only meaningful for AirPlay targets.")

    # Get-or-create the controller for this target via the session
    # layer — we want the SAME controller instance for begin and finish
    # so the pair_handler state survives between requests.
    session = await cast_session.get_session(target)
    controller = getattr(session, "_controller", None)
    if controller is None or not hasattr(controller, "begin_pair"):
        raise HTTPException(
            501,
            "AirPlay support isn't enabled on the server. "
            "Install pyatv to use AirPlay targets.",
        )

    try:
        result = await controller.begin_pair(device_name=getattr(target, "name", "") or "")
    except RuntimeError as exc:
        # "Device not found on network" / "pyatv not installed"
        raise HTTPException(503, str(exc))
    except Exception:
        log.exception("airplay pair/begin failed for target=%s", body.target_id)
        raise HTTPException(502, "Couldn't start AirPlay pairing — see server log.")
    return {"ok": True, **result}


@router.post("/airplay/pair/finish")
async def cast_airplay_pair_finish(
    body: _PairFinishBody, user = Depends(require_user),
):
    """Submit the PIN and persist the resulting credentials.

    On success the next ``/play`` to this target should succeed without
    further prompting — credentials are written to disk so they survive
    a server restart.

    Returns 400 with a clear message on wrong PIN; the UI re-enables the
    PIN input so the user can re-try (within the device's PIN-display
    window, after which they'll need /pair/begin again).
    """
    target = await _resolve_target(body.target_id)
    if target.protocol != "airplay":
        raise HTTPException(400, "Pairing is only meaningful for AirPlay targets.")

    session = await cast_session.get_session(target)
    controller = getattr(session, "_controller", None)
    if controller is None or not hasattr(controller, "finish_pair"):
        raise HTTPException(
            501,
            "AirPlay support isn't enabled on the server.",
        )

    try:
        from soniqboom.core.cast_airplay import PairingError
    except ImportError:
        PairingError = Exception  # type: ignore[assignment]

    pin = body.pin.strip()
    if not pin or not pin.isdigit():
        raise HTTPException(400, "PIN must be the 4-digit code shown on the device.")

    try:
        result = await controller.finish_pair(pin)
    except PairingError as exc:
        # Wrong PIN / expired session — actionable feedback to the UI.
        raise HTTPException(400, f"Pairing failed: {exc}")
    except RuntimeError as exc:
        # ``No pairing session in progress`` — caller forgot to begin.
        raise HTTPException(409, str(exc))
    except Exception:
        log.exception("airplay pair/finish failed for target=%s", body.target_id)
        raise HTTPException(502, "Couldn't complete AirPlay pairing — see server log.")
    return {"ok": True, **result}


@router.post("/airplay/pair/forget")
async def cast_airplay_pair_forget(
    body: _PairBeginBody, _user = Depends(require_user),
):
    """Forget stored credentials for an AirPlay target.

    Use when the device's allow-list has been reset (e.g. the user
    revoked access in System Settings) or when a saved credentials
    blob is rejected on connect.  Next ``/play`` will trigger a fresh
    PIN prompt.
    """
    target = await _resolve_target(body.target_id)
    if target.protocol != "airplay":
        raise HTTPException(400, "Forget is only meaningful for AirPlay targets.")

    try:
        from soniqboom.core import airplay_credentials
    except ImportError:
        raise HTTPException(501, "AirPlay support isn't enabled on the server.")

    identifier = getattr(target, "identifier", "") or body.target_id
    removed = airplay_credentials.forget(identifier)
    return {"ok": True, "removed": removed, "identifier": identifier}


@router.post("/queue")
async def cast_queue(body: QueueBody, user = Depends(require_user)):
    """Replace the device queue.  For renderers without native queue
    support we still load the first track and manage advancement
    locally."""
    target = await _resolve_target(body.target_id)
    session = await cast_session.get_session(target)
    items = [
        cast_session.QueueItem(
            track_id     = it.track_id,
            title        = it.title,
            artist       = it.artist,
            album        = it.album,
            duration_s   = it.duration_s,
            album_art_url= it.album_art_url,
            subsong      = it.subsong,
        ) for it in body.items
    ]
    try:
        await session.queue_load(items, user_id=_user_field(user))
    except RuntimeError as exc:
        log.info("cast/queue missing backend: %s", exc)
        raise HTTPException(
            501,
            "This cast protocol isn't enabled on the server.",
        )
    except Exception:
        log.exception("cast/queue failed for target=%s", body.target_id)
        raise HTTPException(502, "Queue load failed.")
    return {"ok": True, "queue_size": len(items)}


@router.post("/control")
async def cast_control(body: ControlBody, user = Depends(require_user)):
    """Dispatch a transport control action to the target."""
    target = await _resolve_target(body.target_id)
    session = await cast_session.get_session(target)
    action = body.action.lower()
    try:
        if action == "pause":
            await session.pause()
        elif action == "resume":
            await session.resume()
        elif action == "stop":
            await session.stop()
        elif action == "seek":
            await session.seek(seconds=body.seconds)
        elif action == "next":
            await session.queue_next(user_id=_user_field(user))
        elif action == "prev":
            # Cast delegates to the receiver's native QUEUE_UPDATE jump:-1;
            # other protocols step the server-side queue index back one.
            await session.queue_prev(user_id=_user_field(user))
        # No "else" branch needed — Pydantic Literal already rejects
        # unknown actions before reaching here.
    except HTTPException:
        raise
    except RuntimeError as exc:
        log.info("cast/control missing backend: %s", exc)
        raise HTTPException(501, "This cast protocol isn't enabled on the server.")
    except Exception:
        log.exception("cast/control %s failed for target=%s", action, body.target_id)
        raise HTTPException(502, "Control command failed on the target.")
    return {"ok": True, "action": action}


@router.get("/position/{target_id}")
async def cast_position(target_id: str, _user = Depends(current_user)):
    """Live playback position for a target.  Used by the UI to drive
    the progress bar without re-querying every second by hand."""
    session = await cast_session.get_session_by_id(target_id)
    if session is None:
        raise HTTPException(404, "No active session for that target.")
    pos = await session.position()
    return {"target_id": target_id, "position": pos}


@router.post("/preference")
async def cast_preference(body: PreferenceBody, _user = Depends(require_user)):
    """Set the codec preference for this target's session.  Affects
    every subsequent ``/play`` and ``/queue`` call."""
    target = await _resolve_target(body.target_id)
    session = await cast_session.get_session(target)
    try:
        await session.set_user_pref(body.pref)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "pref": body.pref}


@router.post("/disconnect")
async def cast_disconnect(body: TargetIdBody, _user = Depends(require_user)):
    """Tear down the per-target session, freeing the controller's
    network resources.  Idempotent — no error on already-disconnected,
    but the response distinguishes the two cases via ``closed``."""
    existed = (await cast_session.get_session_by_id(body.target_id)) is not None
    await cast_session.close_session(body.target_id)
    return {"ok": True, "closed": existed}


# ── Routes: telemetry ──────────────────────────────────────────────────────

@router.get("/telemetry")
async def cast_telemetry_get(_user = Depends(current_user)):
    """Return the rolling-window outcome counts + p95 first-byte
    latency by (protocol, codec) bucket."""
    return {
        "outcomes":     cast_telemetry.outcome_counts(window_seconds=3600),
        "p95_first_ms": cast_telemetry.p95_first_byte_ms(),
        "recent":       [e.to_public() for e in cast_telemetry.iter_recent(50)],
    }


@router.delete("/telemetry")
async def cast_telemetry_clear(_admin = Depends(require_admin)):
    """Wipe the local telemetry ring buffer.  Admin-only: a regular
    user wiping the ops dashboard is a foot-gun, not a feature."""
    cast_telemetry.clear()
    return {"ok": True}
