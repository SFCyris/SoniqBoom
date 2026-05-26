# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Signed-URL tokens for Cast / AirPlay / DLNA stream delivery.

Renderers (TVs, speakers, Chromecast Audio, HomePod, AirPlay receivers)
can't carry session cookies or Subsonic-style auth params — they only
HTTP-GET the URL we hand them.  The accepted industry pattern is to
mint a short-lived HMAC-signed token in the URL itself; the bytes
endpoint validates the token and serves the file anonymously.

This module:

* Issues tokens with (track_id, codec, bitrate, sample_rate, exp, nonce,
  user_id, target_id) claims, signed HMAC-SHA256 with the server-local
  secret.
* Verifies tokens in constant time, with replay protection (same nonce
  from a different IP is rejected — Range requests from the same
  renderer keep working).
* Renders a renderer-friendly URL — ``/cast/{token}/{filename}.{ext}``
  — so dumb DLNA renderers that sniff Content-Type from the path
  extension see something they recognise.

The signing secret reuses ``credentials._derive_key()`` so it's
deterministic across restarts on the same host (a token issued at
T=0 still verifies at T+15 min after a graceful restart) but distinct
per host (a token from box A doesn't verify on box B).

Replay protection.  We keep an LRU of (user_id, nonce) → first-seen IP.
A second GET from the SAME IP is allowed — that's how Range / pause /
resume work.  A second GET from a DIFFERENT IP is rejected: this
defeats URL-sharing (someone screenshotting their phone's debug
console and leaking a stream URL to a network neighbour).

Why we don't just sign a Subsonic-style ``?u&s&t`` URL: every consumer
DLNA stack we tested truncates or normalises query strings.  The path
component is the only piece guaranteed to survive intact across the
SetAVTransportURI → SOAP → renderer HTTP-GET round trip.  Putting the
token in the path also makes signed URLs visible-and-cancellable in
access logs without leaking the username.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections import OrderedDict
from threading import Lock
from typing import Any

log = logging.getLogger(__name__)


# ── Token lifetime ──────────────────────────────────────────────────────────
# 30 min covers a long track + buffer + a pause/resume cycle.  Tokens
# are re-minted on every `/api/cast/play` call, so a user genuinely
# session-active will never see a token expire mid-listen.
_TOKEN_TTL_SECONDS = 30 * 60

# Replay LRU: bounded so a flood of new tokens can't OOM the server.
# 4 K entries × ~120 B ≈ 480 KB worst-case — negligible.
_REPLAY_LRU_MAX = 4096

# Path-allowed extensions.  Anything else is rewritten to ``.bin`` —
# we never want a renderer to GET ``.exe`` from us.  Keep this list in
# sync with TRANSCODE_MIME + NATIVE in stream.py.
_PATH_EXTS = {"mp3", "flac", "wav", "ogg", "opus", "m4a", "aac"}


# ── Signing secret ──────────────────────────────────────────────────────────

def _server_secret() -> bytes:
    """Server-local HMAC key.

    Reuses the same machine-identity-derived key the credential store
    uses for Fernet — deterministic across restarts on the same host,
    distinct per host, no separate key file needed.

    Hardened fallback: only specific bootstrap-stage exceptions
    (ImportError, FileNotFoundError) downgrade to the static key.
    Any other exception is logged loudly and re-raised — a corrupt
    credential store should NEVER silently downgrade a production
    deployment to a public constant.
    """
    try:
        from soniqboom.core.credentials import _derive_key
        return base64.urlsafe_b64decode(_derive_key())
    except (ImportError, FileNotFoundError):
        # Bootstrap-stage / very-early-boot only.  Tokens issued
        # under this key only work locally and don't survive any
        # cross-host migration — intentional.
        return b"sb-cast-fallback-secret-do-not-use-in-prod-32b"
    except Exception:
        log.exception("cast_tokens: credential derivation failed unexpectedly")
        raise


# ── Encode / decode ────────────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    # Pad back to multiple of 4 for the stdlib decoder.
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_token(
    *,
    track_id: str,
    codec: str | None = None,
    bitrate_kbps: int | None = None,
    sample_rate: int | None = None,
    user_id: str | int | None = None,
    target_id: str | None = None,
    subsong: int = 0,
    ttl_seconds: int = _TOKEN_TTL_SECONDS,
) -> str:
    """Mint a fresh signed token.  All fields except ``track_id`` are
    optional — defaults preserve the existing ``/api/stream`` behaviour.

    ``subsong`` selects the sub-tune index for multi-song chiptune
    formats (SID, tracker modules, GME containers).  Encoded in the
    claims so a cast renderer's URL fetch picks the same sub-tune the
    user clicked in the web UI — without it, every SID with ``subsong=3``
    queued to a Chromecast would play track 0 instead.

    The returned string is URL-safe (base64url, no padding) and contains
    no separators apart from the two ``.`` between header / body / sig
    — paste-and-go for a ``/cast/{token}/song.mp3`` URL.
    """
    if not track_id:
        raise ValueError("track_id is required to issue a cast token")
    now = int(time.time())
    nonce = _b64url(os.urandom(9))  # 72 bits — collision-free at our scale
    claims: dict[str, Any] = {
        "tid":  track_id,
        "exp":  now + max(60, int(ttl_seconds)),
        "iat":  now,
        "jti":  nonce,
    }
    if codec:        claims["c"]   = str(codec).lower()
    if bitrate_kbps: claims["br"]  = int(bitrate_kbps)
    if sample_rate:  claims["sr"]  = int(sample_rate)
    if user_id is not None:  claims["uid"] = str(user_id)
    if target_id:    claims["tg"]  = str(target_id)
    if subsong:      claims["sn"]  = int(subsong)

    header_b64 = _b64url(b'{"alg":"HS256","typ":"SBC"}')
    body_b64   = _b64url(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    )
    sig = hmac.new(
        _server_secret(),
        f"{header_b64}.{body_b64}".encode(),
        hashlib.sha256,
    ).digest()
    sig_b64 = _b64url(sig)
    return f"{header_b64}.{body_b64}.{sig_b64}"


def verify_token(token: str) -> dict[str, Any] | None:
    """Return claims dict on success, ``None`` on any failure.

    Constant-time signature check — never short-circuits on a partial
    mismatch.  Every failure mode (bad format, bad signature, expired,
    malformed claims) yields ``None`` and the caller maps it to a uniform
    404 + identical body so a probing attacker can't distinguish "wrong
    signature" from "unknown token" from "expired" — either from the
    status code or from response timing.

    Specifically, the b64url decode of the signature happens BEFORE the
    HMAC compare; on decode failure we substitute a zero-buffer so the
    compare_digest still runs.  Without this, "invalid base64 sig" got a
    fast reject and "valid-base64-bad-sig" got the full compare path —
    a binary timing oracle.
    """
    try:
        header_b64, body_b64, sig_b64 = token.split(".")
    except ValueError:
        return None
    expected = hmac.new(
        _server_secret(),
        f"{header_b64}.{body_b64}".encode(),
        hashlib.sha256,
    ).digest()
    try:
        provided = _b64url_decode(sig_b64)
    except Exception:
        # Substitute zero bytes so compare_digest still runs over the
        # same input size — closes the timing side channel.
        provided = b"\x00" * len(expected)
    if not hmac.compare_digest(expected, provided):
        return None
    # Past the signature check — the rest is just shape validation,
    # which can leak timing only between "valid-sig + bad-claims" cases
    # (the attacker had to forge a valid signature to get here, which
    # means they already have the key — game over either way).
    try:
        claims = json.loads(_b64url_decode(body_b64).decode("utf-8"))
        if not isinstance(claims, dict):
            return None
        tid = claims.get("tid")
        if not tid or not isinstance(tid, str):
            return None
        # Cap the tid length so a forged-but-validated token can't
        # cause an oversized DB lookup downstream.
        claims["tid"] = tid[:128]
        try:
            exp = int(claims.get("exp", 0))
        except (TypeError, ValueError):
            return None
        if exp and exp < int(time.time()):
            return None
    except Exception:
        return None
    return claims


# ── Replay protection ──────────────────────────────────────────────────────

_replay_lock = Lock()
_replay_lru: "OrderedDict[str, str]" = OrderedDict()


def _replay_key(claims: dict[str, Any]) -> str:
    return f"{claims.get('uid','-')}:{claims.get('jti','-')}"


def replay_ok(claims: dict[str, Any], remote_ip: str | None) -> bool:
    """Return True if this (jti, ip) is the same IP that first claimed
    the token (or first claim ever).  Return False on cross-IP replay.

    Renderers that follow Range / pause-resume re-fetch the URL from
    the SAME IP — those calls are fine.  A different IP grabbing the
    URL is the threat we block.
    """
    if not claims.get("jti"):
        return True  # no nonce — best-effort (legacy)
    key = _replay_key(claims)
    ip = remote_ip or "-"
    with _replay_lock:
        seen = _replay_lru.get(key)
        if seen is None:
            _replay_lru[key] = ip
            # LRU eviction
            while len(_replay_lru) > _REPLAY_LRU_MAX:
                _replay_lru.popitem(last=False)
            return True
        # Move-to-front to keep active streams hot in the LRU.
        _replay_lru.move_to_end(key)
        return seen == ip


# ── URL builders ───────────────────────────────────────────────────────────

def safe_filename(track_meta: dict[str, Any] | None, ext: str) -> str:
    """Build a renderer-friendly filename for the ``/cast/{tok}/...``
    path.  Strips characters DLNA renderers commonly choke on (slashes,
    backslashes, control chars, quotes) and caps at 80 chars so the
    full URL stays under DLNA's de-facto 1024-byte upper bound.

    The filename is COSMETIC — it shows up in the "Now Playing" string
    on some renderers — but the extension matters: many cheap DLNA
    stacks pick the codec from ``.mp3`` / ``.flac`` / ``.wav`` rather
    than from Content-Type.
    """
    ext = (ext or "bin").lstrip(".").lower()
    if ext not in _PATH_EXTS:
        ext = "bin"
    if not track_meta:
        return f"track.{ext}"
    title  = (track_meta.get("title")  or "").strip()
    artist = (track_meta.get("artist") or "").strip()
    base = f"{artist} - {title}".strip(" -") or "track"
    # Strip characters that break DLNA URL parsing or filesystem paths
    # on remote renderers (some DLNA stacks try to "Save As" using the
    # filename verbatim).  ASCII-only: cheap DLNA stacks routinely
    # mishandle UTF-8 in URLs (the spec allows it, real-world receivers
    # don't), so anything outside [A-Za-z0-9 ._-()[]] becomes "_".
    allowed = []
    for ch in base:
        if (ch.isascii() and ch.isalnum()) or ch in " ._-()[]":
            allowed.append(ch)
        else:
            allowed.append("_")
    cleaned = "".join(allowed).strip()
    cleaned = cleaned[:80].rstrip(" .")
    return f"{cleaned or 'track'}.{ext}"


def build_stream_url(
    *,
    base_url: str,
    track_id: str,
    track_meta: dict[str, Any] | None,
    codec: str | None = None,
    bitrate_kbps: int | None = None,
    sample_rate: int | None = None,
    user_id: str | int | None = None,
    target_id: str | None = None,
    subsong: int = 0,
    ttl_seconds: int = _TOKEN_TTL_SECONDS,
) -> str:
    """Compose a full ``{base_url}/cast/{token}/{filename}.{ext}`` URL.

    ``base_url`` should be the externally-reachable LAN URL of the
    server (e.g. ``http://10.0.0.5:8080``) — that's what we hand the
    renderer.  Localhost wouldn't work, the renderer is on a different
    host.
    """
    token = issue_token(
        track_id=track_id, codec=codec, bitrate_kbps=bitrate_kbps,
        sample_rate=sample_rate, user_id=user_id, target_id=target_id,
        subsong=subsong, ttl_seconds=ttl_seconds,
    )
    # Map codec → URL extension.  ``codec`` is None when the caller
    # asked for native delivery; in that case sniff from the source
    # extension stored in track_meta['format'] (set by the library).
    ext: str
    if codec:
        ext = codec.lower()
    elif track_meta:
        ext = (track_meta.get("format") or "").lower() or "bin"
        # Library 'format' is sometimes "FLAC" / "MP3 (mpeg-1)" — pick
        # the first whitespace/parens-free token.
        ext = ext.split()[0].split("/")[0].lower()
    else:
        ext = "bin"
    filename = safe_filename(track_meta, ext)
    return f"{base_url.rstrip('/')}/cast/{token}/{filename}"


# ── Test / debug helpers ───────────────────────────────────────────────────

def _reset_replay_state_for_tests() -> None:
    """Clear the replay LRU.  Used by the test suite between cases —
    never call from production code."""
    with _replay_lock:
        _replay_lru.clear()
