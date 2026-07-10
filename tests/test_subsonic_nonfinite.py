# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One corrupt file must never 500 a Subsonic listing.

A track can decode to a non-finite numeric field — mutagen reports
``audio.info.length`` as ``nan`` for a truncated stream, a broken header yields
``inf`` bitrate, a ``1e999`` literal in a tag parses to ``inf``.  Starlette's
``JSONResponse`` serialises with ``allow_nan=False``, so a single non-finite
value reaching the encoder raises and turns the WHOLE ``getSong`` /
``getAlbumList`` / ``getRandomSongs`` response into an HTTP 500 — the exact
"one bad file kills the listing" failure the ReplayGain ``_fin`` guard set out
to prevent, but for the *core* song fields (duration, bitRate, size, track,
disc, channels, sample rate, bit depth, year).  Two of them (``int(round(nan))``
→ ValueError, ``int(inf)`` → OverflowError) even raise *before* the encoder.
``_safe_int`` stops the non-finite at the source, so a clean response no longer
depends on the ``@_wrap`` catch-all downgrading a 500 to an error envelope.

``_track_to_song`` now routes every one of those fields through ``_safe_int`` /
``_normalise_year``, which coerce a non-finite value to ``0`` before it ever
reaches ``int(round(...))`` or the JSON encoder.  These tests pin that:

  * the mapper + JSON render never raises and emits no ``NaN`` / ``Infinity``
    token, for each field poisoned in isolation and for all of them at once;
  * the ``getSong`` endpoint returns HTTP 200 over a real ASGI round-trip;
  * a control track with finite values still reports its real numbers — the
    sanitiser zeroes ONLY the non-finite ones, it does not blanket-zero (the
    falsifying companion: if it did, the "== 0" assertions above would be
    vacuous).
"""
from __future__ import annotations

import json
import types
import xml.etree.ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soniqboom.api import subsonic


# Raw track keys that _track_to_song coerces, paired with (output key, always
# present?).  The OpenSubsonic optional fields (channelCount / samplingRate /
# bitDepth) are OMITTED rather than emitted as 0 when they degrade, so their
# assertion is "absent" instead of "== 0".
_FIELD_TO_OUT: dict[str, tuple[str, bool]] = {
    "duration":     ("duration",     True),
    "bitrate":      ("bitRate",      True),
    "track_number": ("track",        True),
    "file_size":    ("size",         True),
    "disc_number":  ("discNumber",   True),
    "year":         ("year",         True),
    "channels":     ("channelCount", False),
    "sample_rate":  ("samplingRate", False),
    "bit_depth":    ("bitDepth",     False),
}

# nan, +inf, -inf, and a literal that overflows to +inf on parse — every shape
# of non-finite a decoder or tag can hand us.
_NON_FINITE = [float("nan"), float("inf"), float("-inf"), 1e999]
_NON_FINITE_IDS = ["nan", "inf", "-inf", "1e999"]


def _reject_constant(tok: str):
    """``json.loads`` calls this for the ``NaN`` / ``Infinity`` / ``-Infinity``
    literals — which strict JSON forbids.  A well-formed Subsonic body must
    never contain them, so any call here is a leak."""
    raise AssertionError(f"non-finite JSON constant leaked into body: {tok!r}")


def _strict_loads(raw: bytes | str) -> dict:
    """Parse rejecting non-finite constants — robust where a plain substring
    check would false-match legitimate text (e.g. a track titled 'Infinity')."""
    return json.loads(raw, parse_constant=_reject_constant)


def _base_track() -> dict:
    """A well-formed track — finite everywhere.  Tests poison one field.

    The string fields deliberately avoid the substrings ``nan`` / ``inf`` so the
    literal-token assertions can't false-match on the fixture's own content."""
    return {
        "id":           "trk1",
        "title":        "Corrupt Header Blues",
        "artist":       "The Glitch Trio",
        "album_artist": "The Glitch Trio",
        "album":        "Out Of Range",
        "genre":        ["Noise"],
        "path":         "/music/corrupt.flac",
        "format":       "flac",
        "year":         2020,
        "track_number": 3,
        "disc_number":  1,
        "duration":     200.4,      # round -> 200
        "bitrate":      320_000,    # / 1000 -> 320
        "file_size":    12_345_678,
        "channels":     2,
        "sample_rate":  44_100,
        "bit_depth":    16,
        "added_at":     1_700_000_000,
    }


# ── The mapper + JSON serialiser must survive a non-finite in any field ──────

@pytest.mark.parametrize("field", list(_FIELD_TO_OUT))
@pytest.mark.parametrize("bad", _NON_FINITE, ids=_NON_FINITE_IDS)
def test_single_nonfinite_field_renders(field: str, bad: float):
    t = _base_track()
    t[field] = bad
    # Must not raise: allow_nan=False would 500 on a leaked non-finite, and a
    # pre-encoder int(round(nan)) would ValueError.  Returning at all is a pass
    # for the "no raw 500" half of the contract.
    resp = subsonic._ok({"song": subsonic._track_to_song(t)}, fmt="json")
    body = resp.body
    assert b"NaN" not in body
    assert b"Infinity" not in body

    song = _strict_loads(body)["subsonic-response"]["song"]
    out_key, always_present = _FIELD_TO_OUT[field]
    if always_present:
        assert song[out_key] == 0
    else:
        # Optional OpenSubsonic field: degrades to absent, never a bogus 0/NaN.
        assert out_key not in song


def test_all_fields_nonfinite_at_once_renders():
    """Every numeric field poisoned in one track — plus the ReplayGain tags the
    original H1 fix covered — must still render clean in BOTH encodings."""
    t = _base_track()
    for f in _FIELD_TO_OUT:
        t[f] = float("nan")
    t.update({
        "replaygain_track_gain": float("nan"),
        "replaygain_album_gain": float("inf"),
        "replaygain_track_peak": float("-inf"),
        "replaygain_album_peak": 1e999,
    })

    # JSON path (the allow_nan=False landmine).  The load-bearing check is
    # _strict_loads (rejects the NaN/Infinity JSON constants); the two substring
    # asserts are belt-and-suspenders — allow_nan=False raises *before* emitting
    # those bytes, so on a leak _ok would already have thrown above.
    resp = subsonic._ok({"song": subsonic._track_to_song(t)}, fmt="json")
    assert b"NaN" not in resp.body
    assert b"Infinity" not in resp.body
    song = _strict_loads(resp.body)["subsonic-response"]["song"]
    assert song["duration"] == 0
    assert song["bitRate"] == 0
    assert song["track"] == 0
    assert song["size"] == 0
    assert song["discNumber"] == 0
    assert song["year"] == 0
    # Optional fields drop out entirely rather than emit 0.
    assert "channelCount" not in song
    assert "samplingRate" not in song
    assert "bitDepth" not in song
    # All four RG values were non-finite -> the sub-dict is empty -> omitted.
    assert "replayGain" not in song

    # XML path has NO allow_nan guard — a leaked non-finite would serialise as
    # the attribute literal ``duration="nan"`` without raising.  Parse the
    # element and coerce each numeric attribute to int: "nan"/"inf" fails int(),
    # so this is a real token check, not a substring scan that a title like
    # "To Infinity" could false-trip.
    xml = subsonic._ok({"song": subsonic._track_to_song(t)}, fmt="xml")
    # The body carries a default namespace (xmlns=…/restapi), so match the song
    # element namespace-agnostically with the {*} wildcard.
    song_el = ET.fromstring(xml.body).find(".//{*}song")
    assert song_el is not None, "song element missing from XML render"
    for attr in ("duration", "bitRate", "track", "size", "discNumber", "year"):
        assert int(song_el.get(attr, "")) == 0
    for attr in ("channelCount", "samplingRate", "bitDepth"):
        assert song_el.get(attr) is None


def test_finite_values_survive_unchanged():
    """Falsifying companion: the sanitiser zeroes ONLY non-finite values.  A
    clean track keeps its real numbers — proving the ``== 0`` assertions above
    are the guard firing, not a blanket zero-out of every numeric field."""
    song = subsonic._track_to_song(_base_track())
    assert song["duration"] == 200        # round(200.4)
    assert song["bitRate"] == 320         # 320_000 / 1000
    assert song["track"] == 3
    assert song["size"] == 12_345_678
    assert song["discNumber"] == 1
    assert song["year"] == 2020
    assert song["channelCount"] == 2
    assert song["samplingRate"] == 44_100
    assert song["bitDepth"] == 16


# ── The endpoint must return HTTP 200 over a real ASGI round-trip ────────────

@pytest.mark.parametrize(
    "poison, expected_duration",
    [(True, 0), (False, 200)],
    ids=["nonfinite->0", "finite->real"],
)
def test_getsong_asgi_status_200(monkeypatch, poison: bool, expected_duration: int):
    t = _base_track()
    if poison:
        for f in _FIELD_TO_OUT:
            t[f] = float("nan")
    store = types.SimpleNamespace(
        get_track=lambda tid: t if tid == "trk1" else None
    )
    monkeypatch.setattr(subsonic, "get_store", lambda: store)
    monkeypatch.setattr(subsonic, "_require_user", lambda *a, **k: None)

    app = FastAPI()
    app.include_router(subsonic.router)
    client = TestClient(app)

    resp = client.get("/rest/getSong.view", params={"id": "trk1", "f": "json"})
    assert resp.status_code == 200
    assert "NaN" not in resp.text
    assert "Infinity" not in resp.text
    doc = _strict_loads(resp.content)
    assert doc["subsonic-response"]["status"] == "ok"
    # The finite case proves the 200 isn't a blanket short-circuit: real numbers
    # flow through unchanged, so the poisoned case's 0 is the guard, not a stub.
    assert doc["subsonic-response"]["song"]["duration"] == expected_duration


def test_getalbum_asgi_survives_nonfinite_track_duration(monkeypatch):
    """getAlbum sums per-track durations for the album total.  A single track
    with a non-finite duration must not fail the WHOLE album — the total is
    summed from the sanitised song durations, so the bad track contributes 0
    and the good track's real time still shows.

    Falsifiable: if the aggregate summed the *raw* durations it would raise
    (int(nan)) and _wrap would return a code-0 error envelope with no album; if
    it blanket-zeroed, the total would be 0 instead of the good track's 200.
    """
    bad = _base_track()
    bad["id"] = "bad"
    bad["duration"] = float("nan")
    good = _base_track()
    good["id"] = "good"
    good["duration"] = 200.0
    tracks = [bad, good]

    store = types.SimpleNamespace(
        filter_tracks=lambda **kw: list(tracks),
    )
    monkeypatch.setattr(subsonic, "get_store", lambda: store)
    monkeypatch.setattr(subsonic, "_require_user", lambda *a, **k: None)
    monkeypatch.setattr(
        subsonic, "_decode_album_id",
        lambda _id, _store: ("The Glitch Trio", "Out Of Range"),
    )

    app = FastAPI()
    app.include_router(subsonic.router)
    client = TestClient(app)

    resp = client.get("/rest/getAlbum.view", params={"id": "al:whatever", "f": "json"})
    assert resp.status_code == 200
    assert "NaN" not in resp.text
    assert "Infinity" not in resp.text
    album = _strict_loads(resp.content)["subsonic-response"]["album"]
    assert album["songCount"] == 2
    # bad track -> 0, good track -> 200; the total is the sanitised sum.
    assert album["duration"] == 200


# ── The `created` timestamp (_iso) is the last non-finite sink in the mapper ──
# ``added_at`` is server-set today, but persistence reloads state with the
# default ``json.loads`` (which accepts ``NaN`` / ``Infinity`` literals), so a
# corrupt or tampered snapshot can reintroduce a non-finite ``added_at`` — and
# ``time.gmtime(nan)`` raises, blanking the WHOLE listing via @_wrap.

@pytest.mark.parametrize(
    "ts",
    [float("nan"), float("inf"), float("-inf"), 1e999, 1e18, "not-a-number"],
    ids=["nan", "inf", "-inf", "1e999", "overflow-finite", "non-numeric"],
)
def test_iso_degrades_bad_timestamp_to_empty(ts):
    # gmtime raises on non-finite AND on out-of-range finite (time_t overflow);
    # float() raises on a non-numeric string.  None may escape as a raise.
    assert subsonic._iso(ts) == ""


def test_iso_keeps_a_real_timestamp():
    # Falsifying companion: a valid epoch still formats — the guard drops only
    # bad values, it does not blanket-empty every timestamp.
    assert subsonic._iso(1_700_000_000) == "2023-11-14T22:13:20"


def test_getsong_asgi_survives_nonfinite_added_at(monkeypatch):
    t = _base_track()
    t["added_at"] = float("nan")   # as if reloaded from a corrupt snapshot
    store = types.SimpleNamespace(
        get_track=lambda tid: t if tid == "trk1" else None
    )
    monkeypatch.setattr(subsonic, "get_store", lambda: store)
    monkeypatch.setattr(subsonic, "_require_user", lambda *a, **k: None)

    app = FastAPI()
    app.include_router(subsonic.router)
    resp = TestClient(app).get(
        "/rest/getSong.view", params={"id": "trk1", "f": "json"}
    )
    assert resp.status_code == 200
    doc = _strict_loads(resp.content)
    # Without the _iso guard this comes back status="failed" (code-0 envelope,
    # no song) — the whole listing blanked on one bad timestamp.
    assert doc["subsonic-response"]["status"] == "ok"
    assert doc["subsonic-response"]["song"]["created"] == ""
