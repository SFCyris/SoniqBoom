# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cast codec negotiation + DLNA contentFeatures."""
from __future__ import annotations

import pytest

from soniqboom.core import cast_codecs


# ── Negotiation matrix ────────────────────────────────────────────────────

@pytest.mark.parametrize("src,caps,expected,transcode", [
    # Native pass-through when renderer speaks the source codec.
    ("flac", {"flac", "mp3", "wav"}, "flac", False),
    ("mp3",  {"mp3"},                "mp3",  False),
    ("wav",  {"wav", "mp3"},         "wav",  False),
    # Lossless transcode preferred when available.
    ("aac",  {"flac", "mp3"},        "flac", True),
    ("alac", {"flac", "mp3"},        "flac", True),
    # DSD always transcodes; FLAC preferred when supported.
    ("dsf",  {"flac", "mp3"},        "flac", True),
    ("dff",  {"flac", "mp3"},        "flac", True),
    # MP3-only renderer is the universal fallback.
    ("flac", {"mp3"},                "mp3",  True),
    ("dsf",  {"mp3"},                "mp3",  True),
    ("sid",  {"mp3"},                "mp3",  True),
])
def test_negotiate_basic(src, caps, expected, transcode):
    deliv, tx = cast_codecs.negotiate_codec(
        source_codec=src, renderer_caps=caps, protocol="dlna",
    )
    assert deliv == expected
    assert tx == transcode


def test_negotiate_airplay_prefers_alac_over_mp3():
    """AirPlay 2 receivers want ALAC for any lossless source — falling
    back to MP3 would be a quality regression on Apple TV / HomePod."""
    deliv, tx = cast_codecs.negotiate_codec(
        source_codec="flac",
        renderer_caps={"alac", "aac", "mp3"},
        protocol="airplay",
    )
    assert deliv in ("alac", "flac")  # either is acceptable


def test_negotiate_force_mp3_wins():
    """User-pinned preference overrides every other rule."""
    deliv, tx = cast_codecs.negotiate_codec(
        source_codec="flac",
        renderer_caps={"flac", "mp3"},
        protocol="dlna",
        user_pref="force-mp3",
    )
    assert deliv == "mp3"
    assert tx is True


def test_negotiate_unknown_codec_defaults_to_mp3():
    """An untranscribed source codec (something we forgot to add) must
    still produce SOMETHING playable — fall back to MP3 with a warning."""
    deliv, tx = cast_codecs.negotiate_codec(
        source_codec="qrt",  # not a real codec
        renderer_caps={"mp3"},
        protocol="dlna",
    )
    assert deliv == "mp3"


# ── DLNA contentFeatures.dlna.org header ──────────────────────────────────

def test_content_features_mp3_has_profile_op_flags():
    """MP3 DLNA profile MUST advertise PN+OP+FLAGS — older Samsung
    firmware refuses to play when any are missing."""
    cf = cast_codecs.content_features("mp3")
    assert "DLNA.ORG_PN=MP3" in cf
    assert "DLNA.ORG_OP=01" in cf
    assert "DLNA.ORG_FLAGS=" in cf


def test_content_features_flac_omits_profile_name():
    """FLAC has no canonical DLNA profile name — Sonos / LG accept the
    OP-only form, strict Samsung-pre-2017 rejects an invented one."""
    cf = cast_codecs.content_features("flac")
    assert "DLNA.ORG_PN=" not in cf
    assert "DLNA.ORG_OP=01" in cf


def test_content_features_flags_no_play_container_bit():
    """0x00400000 (PLAY_CONTAINER) is for playlists, not single tracks.
    Including it on a single-file URL trips strict Samsung firmware
    into a 'codec not supported' error.  Defensive regression check —
    if anyone re-introduces it, this test fails loudly."""
    cf = cast_codecs.content_features("mp3")
    flags_part = next(p for p in cf.split(";") if p.startswith("DLNA.ORG_FLAGS="))
    flags = flags_part.split("=", 1)[1]
    # First 8 hex chars are the meaningful flag word
    n = int(flags[:8], 16)
    PLAY_CONTAINER = 0x00400000
    assert n & PLAY_CONTAINER == 0, (
        f"DLNA.ORG_FLAGS includes PLAY_CONTAINER bit, value={flags[:8]}"
    )


# ── SinkProtocolInfo parser ───────────────────────────────────────────────

def test_parse_sink_protocol_info_sonos_like():
    """Sonos S2 declares MP3 / FLAC / WAV / L16 — we treat L16 as WAV."""
    s = (
        "http-get:*:audio/mpeg:*,"
        "http-get:*:audio/flac:*,"
        "http-get:*:audio/x-wav:*,"
        "http-get:*:audio/L16;rate=44100;channels=2:*"
    )
    caps = cast_codecs.parse_sink_protocol_info(s)
    assert caps == {"mp3", "flac", "wav"}


def test_parse_sink_protocol_info_ignores_unknown_mimes():
    s = "http-get:*:audio/super-future-codec-9000:*,http-get:*:audio/mpeg:*"
    caps = cast_codecs.parse_sink_protocol_info(s)
    assert caps == {"mp3"}


def test_parse_sink_protocol_info_empty():
    assert cast_codecs.parse_sink_protocol_info("") == set()
