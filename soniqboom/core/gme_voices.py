# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The voices of a libgme chiptune (NSF / NSFe, SPC, GBS, VGM / VGZ, AY, KSS,
SAP, GYM, HES): the per-voice VU sidecar, and the renders of the voice
mixer — the tune with some voices muted, and each voice on its own.

Every render here plays the tune on the REFERENCE timeline: the one
``gme_render.render_pcm`` (the cached render the listener hears) plays.
libgme skips the silence at the start of a track in whole 1024-frame blocks
when it starts the track, judged on what it outputs; with voices muted the
output — and so the skip — would differ.  So these renders turn libgme's
silence handling off (``gme_ignore_silence``), play the reference's
``lead_in_frames`` (measured: the first sound of a plain start against an
unskipped one), then render in the reference's 4096-frame calls, with the
fade moved by the lead-in rounded to the millisecond libgme takes.  A tune
with no lead-in (SPC, VGM) is then the reference sample for sample; with one
(NSF, GBS: 1024 frames), the fade's 512-sample gain steps can land one step
from the reference's where the 0.5 ms rounding crosses a step.  libgme mixes its voices linearly, so on this timeline the
renders of single voices add up to the reference render: within 2 of 32768
for the NSF and GBS fixtures, 8 for VGM, 19 for SPC (its DSP rounds each
voice) — measured 2026-11-25 (``tests/test_gme_voices.py``).

``gme_mute_voices`` mutes a voice inside the emulator: the chip runs as it
does in the reference render and only that voice's output is left out, so a
muted render is what the hardware plays with that voice switched off.

Blocking throughout (run in a worker thread); ctypes releases the GIL while
libgme renders.  Never raises on a bad file: None / 0 / an empty result.
"""
from __future__ import annotations

import array
import ctypes
import logging
import sys

from soniqboom.core import gme_render
from soniqboom.core.openmpt_vu import VUResult, DEFAULT_VU_RATE_HZ

log = logging.getLogger(__name__)

SR = gme_render._SR                 # 44100: what the reference renders at
_BLOCK_FRAMES = 2048                # frames per gme_play call of the lead-in probe
# Frames per gme_play call of ``render_pcm`` (8192 shorts).  libgme starts a
# fade only in a call that begins after the fade start, so the renders here
# make their calls on the same grid as the reference.
_REF_BLOCK_FRAMES = 4096
_MAX_LEAD_IN_S = 25                 # libgme skips at most 21 s of silence
_MAX_VOICES = 32                    # a VUMR holds at most 32 channels
# Pan from the summed left / right levels, as the tracker meters
# (openmpt_vu._PAN_RATIO_THRESHOLD).
_PAN_RATIO = 1.5

_lib = gme_render._lib
_ok = False
if _lib is not None:
    try:
        _lib.gme_voice_count.restype = ctypes.c_int
        _lib.gme_voice_count.argtypes = [ctypes.c_void_p]
        _lib.gme_voice_name.restype = ctypes.c_char_p
        _lib.gme_voice_name.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _lib.gme_mute_voices.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _lib.gme_ignore_silence.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _ok = True
    except AttributeError:          # pragma: no cover - an old libgme
        log.info("libgme lacks the voice API — no per-voice meters for chiptunes")


def is_available() -> bool:
    return _ok


def fade_ms(duration_s: float, fade_at_s: "float | None") -> "tuple[int, int]":
    """``(start, length)`` in ms of the fade the reference render
    (``stream._render_gme`` → ``gme_render.render_pcm``) of a tune of
    ``duration_s`` with its fade at ``fade_at_s`` has — the chip fade set
    (``render_options.chip_fade_s``) or the NSFe-stated one; length 0: none.
    The one helper both use (``gme_render.reference_fade_len_s`` /
    ``fade_window_ms``), so they can't drift."""
    return gme_render.fade_window_ms(
        duration_s, fade_at_s, gme_render.reference_fade_len_s(duration_s, fade_at_s))


def _open(data: bytes):
    emu = ctypes.c_void_p()
    try:
        if _lib.gme_open_data(data, len(data), ctypes.byref(emu), SR) or not emu:
            return None
    except Exception:                                   # noqa: BLE001
        return None
    return emu


def _delete(emu) -> None:
    try:
        _lib.gme_delete(emu)
    except Exception:                                   # noqa: BLE001
        pass


def _track(subsong: int) -> int:
    return subsong if subsong and subsong > 0 else 0


def voice_names(data: bytes) -> "list[str] | None":
    """The names libgme gives the voices of ``data`` ("Square 1", "DSP 3",
    "FM 2" …), or None when libgme can't open it."""
    if not _ok or not data:
        return None
    emu = _open(data)
    if emu is None:
        return None
    try:
        n = max(0, min(_MAX_VOICES, int(_lib.gme_voice_count(emu))))
        out = []
        for i in range(n):
            raw = _lib.gme_voice_name(emu, i) or b""
            out.append(raw.decode("latin-1").strip() or f"Voice {i + 1}")
        return out
    finally:
        _delete(emu)


def _play(emu, frames: int, buf=None) -> "bytes | None":
    shorts = frames * 2
    if buf is None or len(buf) < shorts:
        buf = (ctypes.c_short * shorts)()
    if _lib.gme_play(emu, shorts, buf):
        return None
    chunk = ctypes.string_at(buf, shorts * 2)
    if sys.byteorder == "big":
        a = array.array("h")
        a.frombytes(chunk)
        a.byteswap()
        chunk = a.tobytes()
    return chunk


def _first_sound(a: "array.array") -> int:
    for i, v in enumerate(a):
        if v:
            return i
    return -1


def lead_in_frames(data: bytes, subsong: int = 0) -> "int | None":
    """Frames of silence libgme skips when it starts track ``subsong`` of
    ``data`` the way the reference render starts it: where a plain start's
    first sound sits in a start with the skip off.  None when libgme can't
    start the track, or the start is silent for ``_MAX_LEAD_IN_S``."""
    if not _ok or not data:
        return None
    track = _track(subsong)

    def run(ignore: int, limit_s: int) -> "array.array | None":
        emu = _open(data)
        if emu is None:
            return None
        try:
            _lib.gme_ignore_silence(emu, ignore)
            if _lib.gme_start_track(emu, track):
                return None
            out = array.array("h")
            buf = (ctypes.c_short * (_BLOCK_FRAMES * 2))()
            found = -1
            while len(out) < limit_s * SR * 2:
                chunk = _play(emu, _BLOCK_FRAMES, buf)
                if chunk is None:
                    break
                out.frombytes(chunk)
                if found < 0 and any(chunk):
                    found = len(out)
                if found >= 0 and len(out) >= found + 8192:
                    break
                if _lib.gme_track_ended(emu):
                    break
            if sys.byteorder == "big":          # (compare as the PCM's values)
                out.byteswap()
            return out
        finally:
            _delete(emu)

    plain = run(0, _MAX_LEAD_IN_S)
    raw = run(1, _MAX_LEAD_IN_S)
    if plain is None or raw is None:
        return None
    a, b = _first_sound(plain), _first_sound(raw)
    if a < 0 or b < 0 or b < a or (b - a) % 2:
        return None
    n = min(4096, len(plain) - a, len(raw) - b)
    if n <= 0 or plain[a:a + n] != raw[b:b + n]:
        log.info("libgme lead-in: the starts don't line up (track %d)", track)
        return None
    return (b - a) // 2


class VoiceRender:
    """Track ``subsong`` of ``data`` on the reference timeline with only the
    voices in ``audible`` (bit v = voice v) heard.  ``read(n)`` returns the
    next ``n`` frames as 16-bit stereo little-endian PCM (silence once the
    track has ended), ``pos`` the frames read so far."""

    def __init__(self, data: bytes, subsong: int, audible: int, *,
                 fade_at_ms: "tuple[int, int]", lead_in: int):
        self.pos = 0
        self._ended = False
        self._pending = b""            # rendered, not yet read
        self._buf = (ctypes.c_short * (_REF_BLOCK_FRAMES * 2))()
        self._emu = _open(data) if _ok else None
        if self._emu is None:
            raise ValueError("libgme can't open this file")
        try:
            _lib.gme_ignore_silence(self._emu, 1)
            if _lib.gme_start_track(self._emu, _track(subsong)):
                raise ValueError("libgme can't start this track")
            n = max(0, min(_MAX_VOICES, int(_lib.gme_voice_count(self._emu))))
            self.voices = n
            _lib.gme_mute_voices(self._emu, ~int(audible) & ((1 << n) - 1) if n else 0)
            # (``fade_at_ms``: ``fade_ms``'s (start, length) on the reference
            # timeline, moved by the lead-in)
            start, flen = fade_at_ms
            gme_render.set_fade(self._emu, int(start) + round(lead_in * 1000 / SR), int(flen))
            left = int(lead_in)
            while left > 0:
                k = min(left, _BLOCK_FRAMES)
                if _play(self._emu, k, self._buf) is None:
                    raise ValueError("libgme failed in the lead-in")
                left -= k
        except BaseException:
            self.close()
            raise

    def read(self, frames: int) -> bytes:
        want = max(0, int(frames)) * 4
        out = bytearray(self._pending[:want])
        self._pending = self._pending[want:]
        while len(out) < want:
            chunk = None
            if not self._ended and self._emu is not None:
                chunk = _play(self._emu, _REF_BLOCK_FRAMES, self._buf)
                if chunk is None:
                    self._ended = True
            if chunk is None:
                out += bytes(want - len(out))
                break
            take = want - len(out)
            out += chunk[:take]
            self._pending = chunk[take:]
        self.pos += max(0, int(frames))
        return bytes(out)

    def close(self) -> None:
        if self._emu is not None:
            _delete(self._emu)
            self._emu = None

    def __del__(self):                  # (one dropped without close: free libgme's emulator)
        try:
            self.close()
        except Exception:                               # noqa: BLE001
            pass


def reference_frames(data: bytes, subsong: int, duration_s: float,
                     fade_at_s: "float | None") -> int:
    """Frames the reference render (``gme_render.render_pcm``) of this tune
    has — one more plain render, counting.  0 when it fails."""
    n = [0]

    def sink(chunk: bytes):
        n[0] += len(chunk)
    total = gme_render.render_pcm(
        data, subsong, duration_s, sink, fade_at_s=fade_at_s,
        fade_len_s=gme_render.reference_fade_len_s(duration_s, fade_at_s))
    return total // 4 if total else 0


def _window_peaks(pcm: "array.array", spf: int, start: int, count: int,
                  out_l: list, out_r: list) -> None:
    """Peaks of |left| and |right| of ``count`` windows of ``spf`` frames of
    the interleaved stereo ``pcm`` from window ``start`` on (a last window may
    be short)."""
    step = spf * 2
    for w in range(start, start + count):
        sl = pcm[w * step:(w + 1) * step]
        if not sl:
            out_l.append(0)
            out_r.append(0)
            continue
        lch, rch = sl[0::2], sl[1::2]
        pl = max(max(lch), -min(lch)) if lch else 0
        pr = max(max(rch), -min(rch)) if rch else 0
        out_l.append(min(pl, 32767))
        out_r.append(min(pr, 32767))


class VuBuilder:
    """The per-voice VU sidecar of a libgme tune, built a slice at a time
    (``step``) so a caller can give its background slot back between slices.

    One render per voice, each with only that voice heard, on the reference
    timeline, ``ref_frames`` frames long (the cached render's length).  Each
    VU frame is the peak of max(|left|, |right|) of a voice over 1/30 s
    (1470 frames; the last may be short), scaled so the loudest frame of any
    voice is 255; each voice's side from its summed left / right peaks."""

    def __init__(self, data: bytes, subsong: int, *, ref_frames: int,
                 fade_at_ms: "tuple[int, int]", lead_in: int,
                 vu_rate_hz: int = DEFAULT_VU_RATE_HZ):
        self.data, self.subsong = data, subsong
        self.ref_frames = max(0, int(ref_frames))
        self.fade_at_ms = (int(fade_at_ms[0]), int(fade_at_ms[1]))
        self.lead_in = int(lead_in)
        self.rate = vu_rate_hz
        self.spf = SR // vu_rate_hz
        self.windows = -(-self.ref_frames // self.spf) if self.ref_frames else 0
        self.names = voice_names(data) or []
        self.voice = 0
        self._cur: "VoiceRender | None" = None
        self._done_frames = 0
        self._peaks_l: "list[list[int]]" = []
        self._peaks_r: "list[list[int]]" = []
        self.failed = not self.names or not self.windows

    @property
    def done(self) -> bool:
        return self.failed or self.voice >= len(self.names)

    def step(self, seconds: float = 30.0) -> None:
        """Render up to ``seconds`` of the current voice (whole VU windows)."""
        if self.done:
            return
        try:
            if self._cur is None:
                self._cur = VoiceRender(self.data, self.subsong, 1 << self.voice,
                                        fade_at_ms=self.fade_at_ms, lead_in=self.lead_in)
                self._done_frames = 0
                self._peaks_l.append([])
                self._peaks_r.append([])
            wins = max(1, int(seconds * self.rate))
            first = self._done_frames // self.spf
            wins = min(wins, self.windows - first)
            frames = min(wins * self.spf, self.ref_frames - self._done_frames)
            pcm = array.array("h")
            pcm.frombytes(self._cur.read(frames))
            if sys.byteorder == "big":
                pcm.byteswap()
            _window_peaks(pcm, self.spf, 0, wins, self._peaks_l[-1], self._peaks_r[-1])
            self._done_frames += frames
            if self._done_frames >= self.ref_frames:
                self._cur.close()
                self._cur = None
                self.voice += 1
        except Exception:                               # noqa: BLE001
            log.debug("libgme VU pass failed", exc_info=True)
            self.close()
            self.failed = True

    def close(self) -> None:
        if self._cur is not None:
            self._cur.close()
            self._cur = None

    def result(self) -> "VUResult | None":
        """The sidecar, once ``done`` — None when it failed or every voice
        was silent."""
        if self.failed or not self.done:
            return None
        n = len(self.names)
        cols = [[max(l, r) for l, r in zip(self._peaks_l[v], self._peaks_r[v])]
                for v in range(n)]
        top = max((max(c) for c in cols if c), default=0)
        if top <= 0:
            return None
        frames = self.windows
        mono = bytearray(frames * n)
        for v, col in enumerate(cols):
            for f in range(min(frames, len(col))):
                mono[f * n + v] = min(255, col[f] * 255 // top)
        pan = bytearray(n)
        for v in range(n):
            sl, sr = sum(self._peaks_l[v]), sum(self._peaks_r[v])
            if sl + sr == 0:
                pan[v] = 0
            elif sl > sr * _PAN_RATIO:
                pan[v] = 1
            elif sr > sl * _PAN_RATIO:
                pan[v] = 2
        return VUResult(channels=n, sample_rate=self.rate, frames=frames,
                        mono=bytes(mono), pan=bytes(pan), names=tuple(self.names))


def build_vu(data: bytes, subsong: int, *, ref_frames: int, duration_s: float,
             fade_at_s: "float | None") -> "VUResult | None":
    """``VuBuilder`` run to the end in one go (tests, scripts)."""
    lead = lead_in_frames(data, subsong)
    if lead is None:
        return None
    b = VuBuilder(data, subsong, ref_frames=ref_frames,
                  fade_at_ms=fade_ms(duration_s, fade_at_s), lead_in=lead)
    try:
        while not b.done:
            b.step(600.0)
        return b.result()
    finally:
        b.close()
