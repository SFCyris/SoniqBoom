# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for ``soniqboom.core.cast_tokens``.

Covers sign/verify round-trip, replay protection (same-IP allowed,
cross-IP rejected), tamper resistance, expiry, and the URL builder's
filename sanitisation (CRLF / non-ASCII / oversized).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from soniqboom.core import cast_tokens


# ── Sign / verify round-trip ───────────────────────────────────────────────

def test_issue_verify_roundtrip_minimal():
    tok = cast_tokens.issue_token(track_id="abc123")
    claims = cast_tokens.verify_token(tok)
    assert claims is not None
    assert claims["tid"] == "abc123"
    assert claims["exp"] > int(time.time())


def test_issue_verify_roundtrip_all_fields():
    tok = cast_tokens.issue_token(
        track_id="abc", codec="mp3", bitrate_kbps=320, sample_rate=44100,
        user_id="alice", target_id="dlna-1", subsong=3,
    )
    claims = cast_tokens.verify_token(tok)
    assert claims == {
        **claims,
        "tid": "abc",
        "c":   "mp3",
        "br":  320,
        "sr":  44100,
        "uid": "alice",
        "tg":  "dlna-1",
        "sn":  3,
    }


def test_issue_rejects_empty_track_id():
    with pytest.raises(ValueError, match="track_id is required"):
        cast_tokens.issue_token(track_id="")


# ── Tamper resistance ─────────────────────────────────────────────────────

def test_tampered_body_rejected():
    tok = cast_tokens.issue_token(track_id="abc")
    header, body, sig = tok.split(".")
    # Flip one bit of the body
    bad = header + "." + ("Z" + body[1:]) + "." + sig
    assert cast_tokens.verify_token(bad) is None


def test_tampered_signature_rejected():
    tok = cast_tokens.issue_token(track_id="abc")
    header, body, sig = tok.split(".")
    bad = header + "." + body + "." + ("A" * len(sig))
    assert cast_tokens.verify_token(bad) is None


def test_malformed_token_rejected():
    # No dots at all
    assert cast_tokens.verify_token("garbage") is None
    # Only one dot
    assert cast_tokens.verify_token("a.b") is None
    # Empty string
    assert cast_tokens.verify_token("") is None


def test_invalid_base64_signature_rejected_timing_safe():
    """Bad signature decode must still run compare_digest — no timing
    side-channel between 'invalid b64' and 'valid b64 but wrong'."""
    tok = cast_tokens.issue_token(track_id="abc")
    header, body, _sig = tok.split(".")
    # Use literally invalid base64 chars in the sig position
    bad = header + "." + body + "." + "!!!not-base64!!!"
    assert cast_tokens.verify_token(bad) is None


# ── Expiry ────────────────────────────────────────────────────────────────

def test_expired_token_rejected():
    """Craft a token whose exp is in the past."""
    claims = {"tid": "abc", "exp": int(time.time()) - 10, "jti": "x"}
    header_b64 = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"SBC"}').rstrip(b"=").decode()
    body_b64   = base64.urlsafe_b64encode(
        json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    sig = hmac.new(
        cast_tokens._server_secret(),
        f"{header_b64}.{body_b64}".encode(),
        hashlib.sha256,
    ).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    assert cast_tokens.verify_token(f"{header_b64}.{body_b64}.{sig_b64}") is None


# ── Replay protection ─────────────────────────────────────────────────────

def test_replay_same_ip_allowed():
    """Range / pause-resume re-fetches from the same renderer must work."""
    cast_tokens._reset_replay_state_for_tests()
    tok = cast_tokens.issue_token(track_id="abc", user_id="u")
    claims = cast_tokens.verify_token(tok)
    assert cast_tokens.replay_ok(claims, "10.0.0.5") is True
    assert cast_tokens.replay_ok(claims, "10.0.0.5") is True
    assert cast_tokens.replay_ok(claims, "10.0.0.5") is True


def test_replay_cross_ip_rejected():
    """Token leaked / shared / proxied to a different host gets denied."""
    cast_tokens._reset_replay_state_for_tests()
    tok = cast_tokens.issue_token(track_id="abc", user_id="u")
    claims = cast_tokens.verify_token(tok)
    assert cast_tokens.replay_ok(claims, "10.0.0.5") is True
    assert cast_tokens.replay_ok(claims, "10.0.0.99") is False


def test_replay_no_jti_passes():
    """Tokens without jti claim (legacy / hand-crafted) shouldn't crash
    the replay check — they pass through with no protection."""
    claims = {"tid": "abc"}  # no jti
    assert cast_tokens.replay_ok(claims, "10.0.0.5") is True


# ── URL builder ───────────────────────────────────────────────────────────

def test_build_stream_url_native_uses_source_extension():
    url = cast_tokens.build_stream_url(
        base_url="http://10.0.0.5:8080",
        track_id="xyz",
        track_meta={"title": "Hello", "artist": "A", "format": "FLAC"},
        codec=None,
    )
    assert url.startswith("http://10.0.0.5:8080/cast/")
    assert url.endswith(".flac")


def test_build_stream_url_transcode_uses_codec_extension():
    url = cast_tokens.build_stream_url(
        base_url="http://10.0.0.5:8080",
        track_id="xyz",
        track_meta={"title": "X", "format": "FLAC"},
        codec="mp3",
    )
    assert url.endswith(".mp3")


def test_safe_filename_strips_crlf_and_non_ascii():
    """The filename ends up in Content-Disposition; CRLF would split
    the response header.  Non-ASCII trips cheap DLNA parsers."""
    name = cast_tokens.safe_filename(
        {"title": "Hello\r\nSet-Cookie:x=y", "artist": "ω 中"},
        "mp3",
    )
    assert "\r" not in name and "\n" not in name
    assert "ω" not in name and "中" not in name
    assert name.endswith(".mp3")


def test_safe_filename_caps_length():
    """Renderer URLs are routinely capped around 1024 bytes total —
    keep the filename short so the rest of the path has room."""
    name = cast_tokens.safe_filename(
        {"title": "A" * 200, "artist": "B" * 200}, "flac",
    )
    # 80-char body + ".flac" + a little slack
    assert len(name) <= 86


def test_safe_filename_unknown_ext_falls_back_to_bin():
    name = cast_tokens.safe_filename({"title": "x"}, "exe")
    assert name.endswith(".bin")
