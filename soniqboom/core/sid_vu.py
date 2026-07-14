"""Per-voice VU extraction for C64 SID tunes.

sidplayfp has no per-voice audio output, but its ``-u<n>`` flag MUTES voice
``n`` (1-3).  Rendering the tune three times — each pass muting two of the
three voices — isolates one SID voice per pass.  Taking the peak amplitude of
each isolated voice over 30 Hz windows yields a per-voice envelope, which
serialises through the SAME VUMR v1 sidecar the tracker (libopenmpt) and
Amiga (uade Paula dump) pipelines write — so the frontend's per-channel VU
meters light up for SID with **zero frontend changes**, giving the classic
3-voice SID scope look (cf. DeepSID).

Fidelity note: an isolated voice's absolute level isn't bit-identical to its
contribution in the full mix — muting two voices changes the shared SID
filter's summed input — but for a NORMALISED per-voice VU that's inaudible and
invisible.  This is an activity/level meter, not a sample-accurate scope.

Stdlib-only (``wave`` + ``array``), matching the project convention (no numpy).
The isolated renders are produced by the caller (stream.py, which owns the
sidplayfp binary + flags); this module only reads the resulting mono 16-bit
WAVs and builds the ``VUResult``.
"""
from __future__ import annotations

import array
import logging
import wave
from pathlib import Path

from soniqboom.core.openmpt_vu import VUResult, DEFAULT_VU_RATE_HZ

log = logging.getLogger(__name__)

# ``-u<n>`` mute sets that ISOLATE each voice by muting the other two.
# sidplayfp voices are 1-indexed; index 0 of this list → voice 1, etc.
VOICE_MUTES: tuple[tuple[int, ...], ...] = ((2, 3), (1, 3), (1, 2))


def _read_wav_samples(wav_path: Path) -> "tuple[array.array, int] | tuple[None, None]":
    """Read a mono 16-bit PCM WAV into a signed-short array + its sample rate.
    Returns (None, None) if it can't be read as such.

    NOTE: ``array('h')`` is host-endian; WAV is little-endian, so this assumes a
    little-endian host — true on every SoniqBoom target (x86-64 / arm64)."""
    try:
        with wave.open(str(wav_path), "rb") as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                return None, None
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
    except (OSError, wave.Error, EOFError):
        return None, None
    samples = array.array("h")
    usable = len(raw) - (len(raw) % samples.itemsize)   # drop any trailing odd byte
    try:
        samples.frombytes(raw[:usable])
    except ValueError:
        return None, None
    return samples, rate


def build_vu(
    voice_wavs: "list[Path]",
    duration_s: float,
    *,
    vu_rate_hz: int = DEFAULT_VU_RATE_HZ,
) -> "VUResult | None":
    """Build a per-voice ``VUResult`` from N isolated-voice mono WAVs.

    Each WAV is one SID voice rendered in isolation.  Windows are a FIXED
    ``rate // vu_rate_hz`` samples wide, so VU frame ``f`` maps to audio
    ``[f/vu_rate_hz, (f+1)/vu_rate_hz)`` EXACTLY — the meter can't drift against
    ``audio.currentTime`` even if sidplayfp overshoots ``-t`` by a few ms or the
    caller's ``duration_s`` hint is slightly off.  The frame count is taken from
    the SHORTEST voice so all channels stay uniform (VUMR requires it); the
    dropped tail is < one frame (~33 ms) — imperceptible.

    Envelopes are normalised by the song-wide peak ACROSS all voices — preserving
    the balance between voices while using the meter's full range (mirrors
    uade_vu / libopenmpt's per-channel [0,1] convention).  Best-effort: any read
    failure or an all-silent render returns None (→ FFT fallback)."""
    if duration_s <= 0 or not voice_wavs:
        return None
    voices: "list[array.array]" = []
    rate = 0
    for wp in voice_wavs:
        s, r = _read_wav_samples(wp)
        if s is None or not r:
            return None
        voices.append(s)
        rate = rate or r
    spf = max(1, rate // vu_rate_hz)                     # samples per VU frame (1470 @ 44100/30)
    shortest = min(len(s) for s in voices)
    if shortest <= 0:
        return None
    n_win = max(1, shortest // spf)
    channels = len(voices)

    cols: "list[list[int]]" = []
    for s in voices:
        col = [0] * n_win
        for k in range(n_win):
            window = s[k * spf:(k + 1) * spf]
            if window:
                hi = max(window)
                lo = min(window)
                # |sample| peak; guard the -32768 edge (its magnitude is 32768).
                col[k] = max(hi, 32767 if lo <= -32768 else -lo)
        cols.append(col)

    peak = max((max(c) for c in cols), default=0)
    if peak <= 0:
        return None  # completely silent — no useful meter

    mono = bytearray(n_win * channels)
    for k in range(n_win):
        base = k * channels
        for ch in range(channels):
            mono[base + ch] = min(255, (cols[ch][k] * 255) // peak)

    return VUResult(
        channels=channels,
        sample_rate=vu_rate_hz,
        frames=n_win,
        mono=bytes(mono),
        pan=bytes(channels),   # all 0 = centre (SID output is mono)
    )
