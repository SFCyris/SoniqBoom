#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Self-contained validation runner for the E-CAST-V2 modules.

Exercises every Phase-0 path without needing a real renderer:

  • Token sign / verify round trip + expiry + tampering
  • Replay protection (same-IP allowed, cross-IP rejected)
  • Codec negotiation matrix
  • DLNA contentFeatures.dlna.org generator
  • Telemetry timer + p95 aggregation
  • Stream-as-render with a synthetic source (sox-generated WAV)
  • safe_filename / build_stream_url

Usage:  .venv/bin/python scripts/validate_cast.py
Exit code 0 = all green; non-zero = at least one assertion failed.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Path bootstrap so we run cleanly from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from soniqboom.core import (
    cast_codecs, cast_pipe, cast_render, cast_telemetry, cast_tokens,
)

PASS, FAIL = 0, 0
def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"  [OK]   {label}")
def bad(label: str, why: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"  [FAIL] {label}  {why}")


# ── 1. Token sign / verify ────────────────────────────────────────────────

def test_tokens():
    print("== tokens ==")
    tok = cast_tokens.issue_token(
        track_id="abc123", codec="mp3", bitrate_kbps=192,
        user_id="alice", target_id="dlna-1",
    )
    claims = cast_tokens.verify_token(tok)
    if claims and claims["tid"] == "abc123" and claims["c"] == "mp3":
        ok("issue→verify round-trip")
    else:
        bad("issue→verify round-trip", repr(claims))

    # Tamper: flip a body bit, signature must reject.
    parts = tok.split(".")
    bad_tok = parts[0] + "." + ("Z" + parts[1][1:]) + "." + parts[2]
    if cast_tokens.verify_token(bad_tok) is None:
        ok("tampered body rejected")
    else:
        bad("tampered body rejected")

    # Expired.
    tok_exp = cast_tokens.issue_token(track_id="abc", ttl_seconds=60)
    # Force expiry by patching the claims clock.  Crude — re-issue with negative TTL via
    # a min-clamped path; the issue function clamps to 60 s, so we test via wait would be
    # too slow.  Instead: poke the internal _b64url + sign path with a past-exp.
    import json, base64, hmac as _hmac, hashlib as _hashlib
    past = {"tid": "abc", "exp": int(time.time()) - 10, "jti": "x"}
    body = base64.urlsafe_b64encode(json.dumps(past, sort_keys=True, separators=(",", ":")).encode()).rstrip(b"=").decode()
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"SBC"}').rstrip(b"=").decode()
    sig = _hmac.new(cast_tokens._server_secret(), f"{header}.{body}".encode(), _hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    if cast_tokens.verify_token(f"{header}.{body}.{sig_b64}") is None:
        ok("expired token rejected")
    else:
        bad("expired token rejected")


# ── 2. Replay protection ──────────────────────────────────────────────────

def test_replay():
    print("== replay ==")
    cast_tokens._reset_replay_state_for_tests()
    tok = cast_tokens.issue_token(track_id="abc", user_id="u1")
    claims = cast_tokens.verify_token(tok)
    if cast_tokens.replay_ok(claims, "10.0.0.5"):
        ok("first GET from IP A allowed")
    else:
        bad("first GET from IP A allowed")
    if cast_tokens.replay_ok(claims, "10.0.0.5"):
        ok("second GET from IP A allowed (Range / pause)")
    else:
        bad("second GET from IP A allowed")
    if not cast_tokens.replay_ok(claims, "10.0.0.99"):
        ok("cross-IP GET rejected")
    else:
        bad("cross-IP GET rejected")


# ── 3. Codec negotiation ──────────────────────────────────────────────────

def test_codec():
    print("== codec negotiation ==")
    # Native FLAC source, Sonos-like renderer → no transcode.
    deliv, tx = cast_codecs.negotiate_codec(
        source_codec="flac", renderer_caps={"mp3", "flac", "wav"}, protocol="dlna",
    )
    if deliv == "flac" and not tx:
        ok("FLAC src + FLAC-capable renderer → native FLAC")
    else:
        bad("FLAC src + FLAC-capable renderer", f"got={deliv},tx={tx}")

    # DSD source → must transcode.  FLAC preferred when available.
    deliv, tx = cast_codecs.negotiate_codec(
        source_codec="dsf", renderer_caps={"mp3", "flac"}, protocol="dlna",
    )
    if deliv == "flac" and tx:
        ok("DSF src + FLAC-capable → transcode FLAC")
    else:
        bad("DSF src + FLAC-capable", f"got={deliv},tx={tx}")

    # DSD source, MP3-only renderer → MP3 transcode.
    deliv, tx = cast_codecs.negotiate_codec(
        source_codec="dsf", renderer_caps={"mp3"}, protocol="dlna",
    )
    if deliv == "mp3" and tx:
        ok("DSF src + MP3-only → transcode MP3")
    else:
        bad("DSF src + MP3-only", f"got={deliv},tx={tx}")

    # AirPlay 2 + ALAC preference for FLAC source.
    deliv, tx = cast_codecs.negotiate_codec(
        source_codec="flac", renderer_caps={"alac", "aac", "mp3"}, protocol="airplay",
    )
    if deliv in ("alac", "flac"):
        ok(f"FLAC src + AirPlay → {deliv}")
    else:
        bad("FLAC src + AirPlay → alac/flac", f"got={deliv}")

    # force-mp3 always wins.
    deliv, tx = cast_codecs.negotiate_codec(
        source_codec="mp3", renderer_caps={"mp3", "flac"},
        protocol="cast", user_pref="force-mp3",
    )
    if deliv == "mp3":
        ok("force-mp3 user pref wins")
    else:
        bad("force-mp3", f"got={deliv}")


# ── 4. DLNA headers ───────────────────────────────────────────────────────

def test_dlna_headers():
    print("== DLNA headers ==")
    h = cast_codecs.dlna_response_headers("mp3")
    cf = h.get("contentFeatures.dlna.org", "")
    if "DLNA.ORG_PN=MP3" in cf and "DLNA.ORG_OP=01" in cf and "DLNA.ORG_FLAGS=" in cf:
        ok("MP3 contentFeatures emits PN + OP + FLAGS")
    else:
        bad("MP3 contentFeatures", cf)
    h_flac = cast_codecs.dlna_response_headers("flac")
    cf_flac = h_flac.get("contentFeatures.dlna.org", "")
    if "DLNA.ORG_PN=" not in cf_flac and "DLNA.ORG_OP=01" in cf_flac:
        ok("FLAC contentFeatures omits PN (Sonos / LG accept this)")
    else:
        bad("FLAC contentFeatures", cf_flac)

    caps = cast_codecs.parse_sink_protocol_info(
        "http-get:*:audio/mpeg:*,http-get:*:audio/flac:*,http-get:*:audio/L16;rate=44100;channels=2:*"
    )
    if caps == {"mp3", "flac", "wav"}:
        ok("SinkProtocolInfo parsed correctly")
    else:
        bad("SinkProtocolInfo parsed", str(caps))


# ── 5. Telemetry ──────────────────────────────────────────────────────────

def test_telemetry():
    print("== telemetry ==")
    cast_telemetry.clear()
    with cast_telemetry.CastTimer(
        protocol="dlna", target_id="t1", source="flac", target="mp3",
    ) as tm:
        tm.mark_first_byte()
        tm.set_bytes(123456)
        tm.outcome = "played"
    evs = cast_telemetry.all_events()
    if len(evs) == 1 and evs[0].outcome == "played" and evs[0].total_bytes == 123456:
        ok("CastTimer records played event")
    else:
        bad("CastTimer played", str(evs))

    # Errored path.
    try:
        with cast_telemetry.CastTimer(
            protocol="cast", target_id="t2", source="dsf", target="mp3",
        ) as tm:
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    evs = cast_telemetry.all_events()
    if any(e.outcome == "errored" and e.error_class == "RuntimeError" for e in evs):
        ok("CastTimer captures errored outcome")
    else:
        bad("CastTimer errored", str(evs[-1]))


# ── 6. URL builder ────────────────────────────────────────────────────────

def test_url_builder():
    print("== URL builder ==")
    url = cast_tokens.build_stream_url(
        base_url="http://10.0.0.5:8080",
        track_id="xyz",
        track_meta={"title": "Hello/World? \"yes\"", "artist": "A B", "format": "FLAC"},
        codec=None,   # native delivery → ext from track_meta['format']
        user_id="u",
        target_id="dlna-7",
    )
    if url.startswith("http://10.0.0.5:8080/cast/") and url.endswith(".flac"):
        ok("native URL: ext from source format")
    else:
        bad("native URL", url)

    url_mp3 = cast_tokens.build_stream_url(
        base_url="http://10.0.0.5:8080",
        track_id="xyz",
        track_meta={"title": "Long " * 30, "artist": "ω", "format": "FLAC"},
        codec="mp3",
        user_id="u",
        target_id="dlna-7",
    )
    if url_mp3.endswith(".mp3") and "/cast/" in url_mp3 and "ω" not in url_mp3:
        ok("transcode URL: ext from codec, non-ASCII stripped")
    else:
        bad("transcode URL", url_mp3)


# ── 7. Stream-as-render with a synthetic source ───────────────────────────

async def test_stream_as_render():
    """Per-codec stream-as-render coverage — every codec the negotiator
    can pick MUST produce non-zero output under the 1.5 s CI-tolerant
    budget.  This catches "bogus ffmpeg flag" class regressions where a
    typo silently breaks one codec while the others pass."""
    print("== stream-as-render (per-codec matrix) ==")
    if not shutil.which("ffmpeg"):
        print("  [SKIP] ffmpeg not on PATH")
        return
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "sine.wav"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-ac", "2", "-ar", "44100", str(src)],
            check=True,
        )

        async def _run(codec: str, *, protocol: str | None = None, label: str | None = None):
            shown = label or codec
            sink = Path(td) / "cache" / f"out_{shown}.bin"
            gen = cast_pipe.render_stream(
                src, codec=codec, cache_sink=sink, protocol=protocol,
            )
            started = time.time()
            first_ms = None
            total = 0
            try:
                async for chunk in gen:
                    if first_ms is None:
                        first_ms = int(round((time.time() - started) * 1000))
                    total += len(chunk)
            except Exception as exc:
                bad(f"{shown}: render raised", repr(exc))
                return
            if total > 1024 and first_ms is not None:
                ok(f"{shown}: streamed {total} B, first byte in {first_ms} ms")
                if first_ms < 1500:
                    ok(f"{shown}: first-byte under 1500 ms budget")
                else:
                    bad(f"{shown}: first-byte budget", f"{first_ms} ms")
            else:
                bad(f"{shown}: empty / no first byte",
                    f"total={total} first_ms={first_ms}")
            if sink.exists() and sink.stat().st_size == total:
                ok(f"{shown}: cache sink mirrored full output")
            else:
                bad(f"{shown}: cache sink size",
                    f"sink={sink.stat().st_size if sink.exists() else 'missing'} vs total={total}")

        # Every codec the negotiator can choose.  Failure on any one
        # means a renderer pointed at that codec gets a silent empty
        # stream — the symptom that prompted this expansion.
        for codec in ("mp3", "flac", "wav", "aac", "opus", "ogg"):
            await _run(codec)
        # AirPlay AAC path uses fragmented MP4 — separate render branch.
        await _run("aac", protocol="airplay", label="aac-mp4")


# ── Main ──────────────────────────────────────────────────────────────────

def test_rendered_format_routing():
    """Verify cast_render.is_rendered_format covers every extension the
    web UI knows how to render.  This is purely a coverage check — we
    don't actually run sidplayfp/fluidsynth/openmpt123/libgme here
    because the dev box may not have all four installed, and a real
    SID/MOD/NSF/MID sample file isn't shipped in the repo."""
    print("== rendered-format routing ==")
    SHOULD_RENDER = [
        # SID
        ".sid", ".psid",
        # MIDI
        ".mid", ".midi",
        # Tracker (subset of _TRACKER_EXTS)
        ".mod", ".s3m", ".xm", ".it", ".669", ".oct", ".ahx",
        # GME
        ".nsf", ".spc", ".gbs", ".vgm", ".vgz", ".ay", ".kss",
        ".sap", ".gym", ".hes",
    ]
    SHOULD_NOT_RENDER = [
        ".mp3", ".flac", ".wav", ".ogg", ".opus",
        ".m4a", ".aac", ".aiff", ".dsf", ".dff", ".wsd",
        ".wv", ".mpc", ".tta",
    ]
    for ext in SHOULD_RENDER:
        if cast_render.is_rendered_format(ext):
            ok(f"{ext} → routed to renderer")
        else:
            bad(f"{ext} should route to renderer (web UI would render it)")
    for ext in SHOULD_NOT_RENDER:
        if not cast_render.is_rendered_format(ext):
            ok(f"{ext} → ffmpeg direct")
        else:
            bad(f"{ext} mis-routed through renderer (ffmpeg can decode it)")
    # Subsong claim plumbing.
    tok = cast_tokens.issue_token(track_id="abc", subsong=7)
    claims = cast_tokens.verify_token(tok)
    if claims and claims.get("sn") == 7:
        ok("subsong claim survives sign/verify round-trip")
    else:
        bad("subsong claim", str(claims))


def main():
    test_tokens()
    test_replay()
    test_codec()
    test_dlna_headers()
    test_telemetry()
    test_url_builder()
    test_rendered_format_routing()
    asyncio.run(test_stream_as_render())
    print()
    print(f"== {PASS} passed, {FAIL} failed ==")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
