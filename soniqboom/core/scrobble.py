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
  queued to disk (``scrobble_queue.json``, 0600) and retried by a small
  background task.  A queued play names its account (``uid``), never a
  token: the retry sends it with the account's token of that moment, and
  drops it once the account is gone or disabled or disconnected that
  service.  A queue written before (tokens inside) is converted at the
  first retry pass.

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
import uuid
import weakref
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
# One per event loop: the bundled app runs each server run on a new loop in
# the same process, and a client made on a closed loop fails ("Event loop is
# closed").
_HTTP_CLIENT: httpx.AsyncClient | None = None
_HTTP_CLIENT_LOOP = None


def _client() -> httpx.AsyncClient:
    global _HTTP_CLIENT, _HTTP_CLIENT_LOOP
    loop = asyncio.get_running_loop()
    if _HTTP_CLIENT is None or _HTTP_CLIENT_LOOP is not loop or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        _HTTP_CLIENT_LOOP = loop
    return _HTTP_CLIENT


async def shutdown() -> None:
    """Close the shared client on app shutdown."""
    global _HTTP_CLIENT, _HTTP_CLIENT_LOOP
    client, _HTTP_CLIENT, _HTTP_CLIENT_LOOP = _HTTP_CLIENT, None, None
    if client is not None:
        try: await client.aclose()
        except Exception: pass

# ── Failure queue (persisted) ───────────────────────────────────────────────

def _queue_path() -> Path:
    return get_data_dir() / "scrobble_queue.json"


def _load_queue() -> list[dict]:
    try:
        items = json.loads(_queue_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []


def _save_queue(items: list[dict]) -> None:
    """Replace the queue file (owner-only, 0600)."""
    path = _queue_path()
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            try:
                os.fchmod(f.fileno(), 0o600)      # (O_CREAT's mode is masked; an old tmp kept its own)
            except OSError:
                pass
            f.write(json.dumps(items, ensure_ascii=False).encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


_SECRET_KEYS = {"lastfm": "session_key", "listenbrainz": "token"}


def _service_token(uid: str | None, service: str) -> str | None:
    """The account ``uid``'s token for ``service`` now — None when the
    account is gone or disabled, or that service is disconnected."""
    if not uid:
        return None
    try:
        from soniqboom.core.users import get_user_store
        user = get_user_store().get(uid)
    except Exception:                                       # noqa: BLE001
        return None
    if user is None or not user.enabled:
        return None
    return (user.lastfm_session_key if service == "lastfm"
            else user.listenbrainz_token if service == "listenbrainz" else None) or None


def _migrate_items(items: list[dict]) -> bool:
    """Queue items written with the token inside (before ``uid``): the
    account whose current token it is takes it (the token leaves the item);
    one no account holds any more (disconnected, replaced) is dropped.  In
    place; True when anything changed.  Left as is while the user store
    isn't up yet."""
    legacy = [it for it in items if it.get(_SECRET_KEYS.get(it.get("service"), ""))]
    if not legacy:
        return False
    try:
        from soniqboom.core.users import get_user_store
        users = get_user_store().list_users()
    except Exception:                                       # noqa: BLE001
        return False
    owners: dict[tuple[str, str], str] = {}
    for u in users:
        if u.lastfm_session_key:
            owners[("lastfm", u.lastfm_session_key)] = u.id
        if u.listenbrainz_token:
            owners[("listenbrainz", u.listenbrainz_token)] = u.id
    keep = []
    for it in items:
        key = _SECRET_KEYS.get(it.get("service"), "")
        secret = it.get(key) if key else None
        if secret:
            uid = owners.get((it["service"], secret))
            it.pop(key, None)
            if uid is None:
                continue                                    # nobody's token now: dropped
            it["uid"] = uid
        keep.append(it)
    items[:] = keep
    return True


def _restrict_queue_file() -> None:
    """An older queue file may be world-readable: owner-only."""
    try:
        path = _queue_path()
        if path.stat().st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass


# The queue file's lock, and one retry pass at a time (two overlapping passes
# would send the same queued play twice) — one pair per event loop, made on
# first use: an ``asyncio.Lock`` belongs to the loop that first waited on it,
# and the bundled app runs each server run on a new loop in the same process.
_loop_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[asyncio.Lock, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary())


def _locks() -> "tuple[asyncio.Lock, asyncio.Lock]":
    """``(queue lock, retry lock)`` of the running loop."""
    loop = asyncio.get_running_loop()
    pair = _loop_locks.get(loop)
    if pair is None:
        pair = _loop_locks[loop] = (asyncio.Lock(), asyncio.Lock())
    return pair

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
    # ``qid``: the item's identity, so a retry pass removes exactly the items
    # it sent (``retry_pending``) and never one queued while it ran.
    item = {**item, "qid": item.get("qid") or uuid.uuid4().hex}
    async with _locks()[0]:
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
    from soniqboom.core import netpolicy
    if netpolicy.blocked():
        return False                     # offline mode: stays queued
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
    from soniqboom.core import netpolicy
    if netpolicy.blocked(LISTENBRAINZ_API):
        return False                     # offline mode: stays queued
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


async def submit_play(user: User, track: dict, ts: int | None = None) -> None:
    """Submit a completed play.  Queues + retries on failure.  ``ts`` (epoch
    seconds) is when it was played — a Subsonic client's back-dated scrobble —
    clamped to ``[0, now]``."""
    if not user:
        return
    info = _track_payload(track)
    if not info["artist"] or not info["track"]:
        return

    now = int(time.time())
    if ts is None:
        ts = now
    else:
        try:
            ts = max(0, min(int(ts), now))
        except (TypeError, ValueError):
            ts = now

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
                    "uid": user.id,
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
                    "uid":     user.id,
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
    (load-test #1 P2-10).

    Items queued while the pass runs are kept: at the end the queue is
    re-read under the lock and only the items this pass settled (sent, or
    dropped for a permanent error) are removed from it, by their ``qid`` —
    never a wholesale overwrite with this pass's snapshot.  Items still
    failing stay where they are; nothing is added back, so nothing is
    duplicated."""
    async with _locks()[1]:
        return await _retry_pass()


async def _retry_pass() -> int:
    async with _locks()[0]:
        items = _load_queue()
        _restrict_queue_file()
        # Items with a token inside (an older queue) name their account now;
        # items queued before ``qid`` existed get one (saved, so the removal
        # below can find them).
        changed = _migrate_items(items)
        if any(not it.get("qid") for it in items):
            for it in items:
                if not it.get("qid"):
                    it["qid"] = uuid.uuid4().hex
            changed = True
        if changed:
            _save_queue(items)
    if not items:
        return 0
    settled: set[str] = set()
    try:
        for item in items:
            ok = False
            service = item.get("service")
            # (an item still holding a token: the user store wasn't up to
            # convert it — kept for a later pass, not sent)
            legacy = bool(item.get(_SECRET_KEYS.get(service, "")))
            token = None if legacy else _service_token(item.get("uid"), service)
            if token is None and not legacy:
                # The account is gone / disabled, or disconnected the service.
                log.info("Dropping a queued %s scrobble: its account no longer "
                         "scrobbles there", service)
                settled.add(item["qid"])
                continue
            try:
                if service == "lastfm" and token:
                    ok = await _post_lastfm(
                        token,
                        "track.scrobble",
                        {"artist[0]":      item["artist"],
                         "track[0]":       item["track"],
                         "album[0]":       item["album"],
                         "albumArtist[0]": item["albumArtist"],
                         "duration[0]":    item["duration"],
                         "timestamp[0]":   str(item["ts"])},
                    )
                elif service == "listenbrainz" and token:
                    ok = await _post_listenbrainz(token, {
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
                settled.add(item["qid"])
                continue
            if ok:
                settled.add(item["qid"])
    finally:
        # (also when the pass ends early — an exception, a cancelled task:
        # what was already sent must not be sent again by the next pass)
        async with _locks()[0]:
            current = _load_queue()
            remaining = [it for it in current if it.get("qid") not in settled]
            if len(remaining) != len(current):
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
