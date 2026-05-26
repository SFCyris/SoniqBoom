# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Scrobble shim — submits plays to last.fm and/or ListenBrainz on
behalf of a user, using the per-user tokens stored in ``users.json``.

The submitters are deliberately lightweight:

* ``submit_now_playing(user, track)`` — fire-and-forget when a track
  starts.  Both services use this for "now playing" widgets and don't
  retry on failure.
* ``submit_play(user, track)`` — called after the track has been
  listened to enough to count as a play (the existing
  ``record_play`` threshold in ``api/tracks.py``).  Failures are
  queued to disk (``scrobble_queue.json``) and retried by a small
  background task.

Network calls use ``httpx`` (already a top-level dep) with a 5 s timeout
and never block the audio path — every submit is awaited via
``asyncio.create_task`` so a network hiccup can't park playback.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

from soniqboom.config import get_data_dir
from soniqboom.models.user import User

log = logging.getLogger(__name__)

# Default API keys can be supplied via env so users don't have to
# register an app to scrobble.  These are widely-published, anonymous
# "shared" keys; if either service rate-limits them, the user can
# override via env without a rebuild.
LASTFM_API_KEY    = os.environ.get("SONIQBOOM_LASTFM_API_KEY", "")
LASTFM_API_SECRET = os.environ.get("SONIQBOOM_LASTFM_API_SECRET", "")
LASTFM_API        = "https://ws.audioscrobbler.com/2.0/"

LISTENBRAINZ_API  = "https://api.listenbrainz.org/1/submit-listens"

_HTTP_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)

# Module-level client so the connection pool + SSL session is reused
# across calls.  Previously every scrobble spawned a fresh AsyncClient
# (SSL handshake + DNS each time) — under 5 users that's 5 handshakes/min
# steady state and 1000 in a queue-drain (load-test #1 P2-9).
_HTTP_CLIENT: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _HTTP_CLIENT


async def shutdown() -> None:
    """Close the shared client on app shutdown."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None:
        try: await _HTTP_CLIENT.aclose()
        except Exception: pass
        _HTTP_CLIENT = None

# ── Failure queue (persisted) ───────────────────────────────────────────────

def _queue_path() -> Path:
    return get_data_dir() / "scrobble_queue.json"


def _load_queue() -> list[dict]:
    try:
        return json.loads(_queue_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_queue(items: list[dict]) -> None:
    tmp = _queue_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _queue_path())


_queue_lock = asyncio.Lock()

# Counter of plays dropped because the queue overflowed.  Reset at
# process restart; persistent counts would need its own AOF, which is
# over-engineering for what's already a "remote service was down for
# hours" edge case.  Surface via /api/auth/me so the My Account UI can
# show "N plays lost — last.fm has been down" (UX-under-load #10).
_dropped_count = 0


def dropped_scrobbles() -> int:
    return _dropped_count


def queue_depth() -> int:
    try: return len(_load_queue())
    except Exception: return 0


async def _enqueue(item: dict) -> None:
    global _dropped_count
    async with _queue_lock:
        items = _load_queue()
        items.append(item)
        # Cap the queue so a long network outage doesn't grow unbounded.
        if len(items) > 1000:
            dropped = len(items) - 1000
            _dropped_count += dropped
            items = items[-1000:]
        _save_queue(items)


# ── last.fm signing ─────────────────────────────────────────────────────────

def _lastfm_sig(params: dict[str, str]) -> str:
    """Per-call API signature: md5 of sorted "key1value1key2value2…secret"."""
    base = "".join(f"{k}{params[k]}" for k in sorted(params))
    return hashlib.md5((base + LASTFM_API_SECRET).encode("utf-8")).hexdigest()


class _PermanentScrobbleError(Exception):
    """The remote service rejected this request in a way that further
    retries will keep producing.  The queue drops these on the floor
    instead of looping forever."""


# last.fm error codes that are *permanent* — caller must re-auth, fix
# the request, or surface a config error.  Anything else (rate limit,
# server unavailable, intermittent network) is treated as transient.
# Reference: https://www.last.fm/api/errorcodes
_LASTFM_PERMANENT = frozenset({
    4,   # auth: invalid signature
    6,   # invalid parameters
    8,   # operation failed (often token-related)
    9,   # invalid session key — user must re-auth
    10,  # invalid api key
    13,  # invalid method signature
    14,  # unauthorised token
    17,  # log-in required
    26,  # suspended api key
})


def lastfm_keys_configured() -> bool:
    """Server-level scrobble-misconfig check: even if a user has a session
    key set, scrobble forwarding silently no-ops unless the server admin
    has provisioned SONIQBOOM_LASTFM_API_KEY + SECRET.  Surface this state
    to /api/users/me so the My Account UI can honestly report
    'scrobbling configured but won't fire — server missing keys'."""
    return bool(LASTFM_API_KEY and LASTFM_API_SECRET)


async def _post_lastfm(session_key: str, method: str, extra: dict[str, str]) -> bool:
    """Returns True on success.  Raises _PermanentScrobbleError on
    rejection that will never succeed (revoked key, bad api key).
    Otherwise returns False so the caller can decide whether to retry."""
    if not (LASTFM_API_KEY and LASTFM_API_SECRET):
        # Server-side misconfig — there's no point queuing; nothing will
        # ever drain it.  Treat as permanent so the queue stays clean.
        raise _PermanentScrobbleError("last.fm API keys not configured on server")
    if not session_key:
        raise _PermanentScrobbleError("no last.fm session key")
    params = {
        "method":  method,
        "api_key": LASTFM_API_KEY,
        "sk":      session_key,
        "format":  "json",
        **extra,
    }
    params["api_sig"] = _lastfm_sig({k: v for k, v in params.items() if k != "format"})
    try:
        r = await _client().post(LASTFM_API, data=params)
        if r.status_code in (401, 403):
            raise _PermanentScrobbleError(
                f"last.fm {method} rejected (HTTP {r.status_code}) — session key likely revoked",
            )
        if r.status_code != 200:
            log.warning("last.fm %s → HTTP %d: %s", method, r.status_code, r.text[:200])
            return False
        j = r.json()
        if isinstance(j, dict) and j.get("error") in _LASTFM_PERMANENT:
            raise _PermanentScrobbleError(
                f"last.fm {method} permanent error {j.get('error')}: {j.get('message')}",
            )
        if isinstance(j, dict) and j.get("error"):
            log.warning("last.fm %s → %s", method, j)
            return False
        return True
    except _PermanentScrobbleError:
        raise
    except (httpx.HTTPError, ValueError) as e:
        log.warning("last.fm %s failed: %s", method, e)
        return False


# ── ListenBrainz ────────────────────────────────────────────────────────────

async def _post_listenbrainz(token: str, payload: dict) -> bool:
    if not token:
        raise _PermanentScrobbleError("no ListenBrainz token")
    try:
        r = await _client().post(
            LISTENBRAINZ_API,
            json=payload,
            headers={"Authorization": f"Token {token}"},
        )
        if r.status_code in (401, 403):
            raise _PermanentScrobbleError(
                f"ListenBrainz rejected token (HTTP {r.status_code}) — "
                "user must re-paste a valid token",
            )
        if r.status_code >= 400:
            log.warning("ListenBrainz → HTTP %d: %s", r.status_code, r.text[:200])
            return False
        return True
    except _PermanentScrobbleError:
        raise
    except httpx.HTTPError as e:
        log.warning("ListenBrainz failed: %s", e)
        return False


# ── Public submitters ───────────────────────────────────────────────────────

def _track_payload(track: dict) -> dict[str, str]:
    """Common artist / track / album / album-artist tuple."""
    return {
        "artist": (track.get("artist") or track.get("album_artist") or "").strip(),
        "track":  (track.get("title") or "").strip(),
        "album":  (track.get("album") or "").strip(),
        "albumArtist": (track.get("album_artist") or "").strip(),
        "duration":    str(int(round(track.get("duration") or 0))),
    }


async def submit_now_playing(user: User, track: dict) -> None:
    """Best-effort 'now playing' notification (no retry)."""
    if not user:
        return
    info = _track_payload(track)
    if not info["artist"] or not info["track"]:
        return

    tasks = []
    if user.lastfm_session_key:
        tasks.append(_post_lastfm(
            user.lastfm_session_key,
            "track.updateNowPlaying",
            {"artist": info["artist"], "track": info["track"],
             "album": info["album"], "albumArtist": info["albumArtist"],
             "duration": info["duration"]},
        ))
    if user.listenbrainz_token:
        tasks.append(_post_listenbrainz(user.listenbrainz_token, {
            "listen_type": "playing_now",
            "payload": [{
                "track_metadata": {
                    "artist_name":  info["artist"],
                    "track_name":   info["track"],
                    "release_name": info["album"] or None,
                },
            }],
        }))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def submit_play(user: User, track: dict) -> None:
    """Submit a completed play.  Queues + retries on failure."""
    if not user:
        return
    info = _track_payload(track)
    if not info["artist"] or not info["track"]:
        return

    ts = int(time.time())

    # last.fm — permanent errors drop the submission; transient errors queue.
    if user.lastfm_session_key:
        try:
            ok = await _post_lastfm(
                user.lastfm_session_key,
                "track.scrobble",
                {"artist[0]":   info["artist"],
                 "track[0]":    info["track"],
                 "album[0]":    info["album"],
                 "albumArtist[0]": info["albumArtist"],
                 "duration[0]": info["duration"],
                 "timestamp[0]": str(ts)},
            )
            if not ok:
                await _enqueue({
                    "service": "lastfm",
                    "session_key": user.lastfm_session_key,
                    "ts": ts,
                    **info,
                })
        except _PermanentScrobbleError as e:
            log.warning("last.fm permanent error for user '%s' — dropping: %s", user.username, e)

    # ListenBrainz — same permanent/transient split.
    if user.listenbrainz_token:
        try:
            ok = await _post_listenbrainz(user.listenbrainz_token, {
                "listen_type": "single",
                "payload": [{
                    "listened_at": ts,
                    "track_metadata": {
                        "artist_name":  info["artist"],
                        "track_name":   info["track"],
                        "release_name": info["album"] or None,
                        "additional_info": {
                            "duration": int(info["duration"]) if info["duration"].isdigit() else 0,
                        },
                    },
                }],
            })
            if not ok:
                await _enqueue({
                    "service": "listenbrainz",
                    "token":   user.listenbrainz_token,
                    "ts":      ts,
                    **info,
                })
        except _PermanentScrobbleError as e:
            log.warning("ListenBrainz permanent error for user '%s' — dropping: %s",
                        user.username, e)


# ── Retry pump (called once a minute by the startup task) ────────────────────

async def retry_pending() -> int:
    """Drain the failure queue.  Returns the number of items still pending.

    Lock discipline: load items under the lock, then release it while
    doing the per-item network IO.  The previous behaviour held the lock
    across the entire drain — at 1000 items × ~150 ms RTT that's >2 min
    of holding the lock, blocking every concurrent ``submit_play``
    (load-test #1 P2-10)."""
    async with _queue_lock:
        items = _load_queue()
    if not items:
        return 0
    remaining: list[dict] = []
    for item in items:
        ok = False
        try:
            if item.get("service") == "lastfm" and item.get("session_key"):
                ok = await _post_lastfm(
                    item["session_key"],
                    "track.scrobble",
                    {"artist[0]":      item["artist"],
                     "track[0]":       item["track"],
                     "album[0]":       item["album"],
                     "albumArtist[0]": item["albumArtist"],
                     "duration[0]":    item["duration"],
                     "timestamp[0]":   str(item["ts"])},
                )
            elif item.get("service") == "listenbrainz" and item.get("token"):
                ok = await _post_listenbrainz(item["token"], {
                    "listen_type": "single",
                    "payload": [{
                        "listened_at": item["ts"],
                        "track_metadata": {
                            "artist_name":  item["artist"],
                            "track_name":   item["track"],
                            "release_name": item["album"] or None,
                        },
                    }],
                })
        except _PermanentScrobbleError as e:
            # Drop permanent failures — never retry these.
            log.warning("Dropping scrobble (%s) permanent error: %s",
                        item.get("service"), e)
            continue
        if not ok:
            remaining.append(item)
    async with _queue_lock:
        _save_queue(remaining)
    return len(remaining)


async def retry_loop(period_sec: int = 60) -> None:
    """Background task that wakes every minute and drains pending scrobbles."""
    while True:
        try:
            await retry_pending()
        except Exception:
            log.exception("scrobble retry pump failed")
        await asyncio.sleep(period_sec)
