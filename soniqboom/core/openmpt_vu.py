# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Thin ``ctypes`` binding for libopenmpt — extracts per-channel VU data
from a tracker module by advancing the module's mixer state at a fixed
rate and polling :c:func:`openmpt_module_get_current_channel_vu_mono`.

Why ``ctypes`` and not a PyPI binding
─────────────────────────────────────
At the time of writing there is no maintained PyPI binding for
libopenmpt (``pyopenmpt`` / ``libopenmpt-py`` / ``openmpt-python`` —
none are published).  Wrapping the few calls we need ourselves keeps
the dependency surface to "the same ``libopenmpt.so`` that the
installer drops on every supported distro for ``openmpt123`` to use."

Scope: read-only mixer state inspection
───────────────────────────────────────
We never render audio output here — that path stays on the
``openmpt123`` CLI subprocess (battle-tested across thousands of edge-
case files).  This module only opens a second copy of the same module
in-process, advances its state at the VU sample rate, and reads the
per-channel VU registers without writing the audio anywhere.

Failure modes
─────────────
If libopenmpt can't be loaded (missing shared lib, version too old) or
the module file can't be opened, every public function returns
``None`` — the caller falls back to the existing FFT-spectrum
visualiser.  No exception ever escapes this module to the request
handler.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# ── Library loader ──────────────────────────────────────────────────────────

_LIB: ctypes.CDLL | None = None


def _candidate_paths() -> list[str]:
    """Platform-aware list of likely libopenmpt locations."""
    if sys.platform == "darwin":
        return [
            "/opt/homebrew/lib/libopenmpt.dylib",        # Apple Silicon Homebrew
            "/usr/local/lib/libopenmpt.dylib",           # Intel Homebrew
            "/opt/homebrew/lib/libopenmpt.0.dylib",
            "/usr/local/lib/libopenmpt.0.dylib",
        ]
    if sys.platform.startswith("linux"):
        return [
            "libopenmpt.so.0",                            # SONAME, works after ldconfig
            "/usr/lib/x86_64-linux-gnu/libopenmpt.so.0", # Debian/Ubuntu
            "/usr/lib64/libopenmpt.so.0",                # Fedora/RHEL/openSUSE
            "/usr/lib/libopenmpt.so.0",                  # Arch
        ]
    if sys.platform.startswith("win"):
        return ["libopenmpt.dll", "openmpt.dll"]
    return []


_LOAD_FAILED = False


def _load() -> ctypes.CDLL | None:
    """Try every reasonable name + path; return the first that loads.

    Cached after the first successful resolution.  A failed load
    returns ``None`` and is NOT retried (``_LOAD_FAILED``: no
    ``find_library`` — on Linux an ``ldconfig`` process — and no log line
    per call) — the operator can re-import after fixing their library
    install.
    """
    global _LIB, _LOAD_FAILED
    if _LIB is not None:
        return _LIB
    if _LOAD_FAILED:
        return None
    # ctypes.util.find_library first — honours LD_LIBRARY_PATH, etc.
    found = ctypes.util.find_library("openmpt")
    candidates = ([found] if found else []) + _candidate_paths()
    for cand in candidates:
        if not cand:
            continue
        try:
            lib = ctypes.CDLL(cand)
            # Sanity-check one core symbol so we don't hand back a half-
            # loaded library that'll crash later.
            lib.openmpt_module_create_from_memory
            _LIB = lib
            log.info("openmpt_vu: loaded libopenmpt from %s", cand)
            return _LIB
        except (OSError, AttributeError):
            continue
    log.info(
        "openmpt_vu: libopenmpt not found in any candidate path; "
        "per-channel VU disabled — frontend will fall back to FFT spectrum",
    )
    _LOAD_FAILED = True
    return None


def is_available() -> bool:
    """True iff libopenmpt can be loaded on this host."""
    return _load() is not None


# ── Function-prototype setup ────────────────────────────────────────────────

# Opaque module handle.  We always pass / return it as void*.
_Module = ctypes.c_void_p


def _bind() -> bool:
    """Apply ``argtypes`` / ``restype`` to every libopenmpt function we use.

    Returns False if the library isn't loadable (caller short-circuits).
    """
    lib = _load()
    if lib is None:
        return False
    if getattr(lib, "_sb_bound", False):
        return True

    # openmpt_module * openmpt_module_create_from_memory2(
    #     const void * filedata, size_t filesize,
    #     openmpt_log_func logfunc, void * loguser,
    #     openmpt_error_func errfunc, void * erruser,
    #     int * error, const char ** error_message,
    #     const openmpt_module_initial_ctl * ctls)
    lib.openmpt_module_create_from_memory2.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_char_p),
        ctypes.c_void_p,
    ]
    lib.openmpt_module_create_from_memory2.restype = _Module

    lib.openmpt_module_destroy.argtypes = [_Module]
    lib.openmpt_module_destroy.restype  = None

    lib.openmpt_module_get_duration_seconds.argtypes = [_Module]
    lib.openmpt_module_get_duration_seconds.restype  = ctypes.c_double

    lib.openmpt_module_get_num_channels.argtypes = [_Module]
    lib.openmpt_module_get_num_channels.restype  = ctypes.c_int32

    lib.openmpt_module_get_num_subsongs.argtypes = [_Module]
    lib.openmpt_module_get_num_subsongs.restype  = ctypes.c_int32

    lib.openmpt_module_select_subsong.argtypes = [_Module, ctypes.c_int32]
    lib.openmpt_module_select_subsong.restype  = ctypes.c_int

    # size_t openmpt_module_read_float_stereo(openmpt_module *mod,
    #     int32_t samplerate, size_t count, float *left, float *right);
    lib.openmpt_module_read_float_stereo.argtypes = [
        _Module, ctypes.c_int32, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float),
    ]
    lib.openmpt_module_read_float_stereo.restype = ctypes.c_size_t

    # float openmpt_module_get_current_channel_vu_mono(mod, channel)
    lib.openmpt_module_get_current_channel_vu_mono.argtypes  = [_Module, ctypes.c_int32]
    lib.openmpt_module_get_current_channel_vu_mono.restype   = ctypes.c_float
    lib.openmpt_module_get_current_channel_vu_left.argtypes  = [_Module, ctypes.c_int32]
    lib.openmpt_module_get_current_channel_vu_left.restype   = ctypes.c_float
    lib.openmpt_module_get_current_channel_vu_right.argtypes = [_Module, ctypes.c_int32]
    lib.openmpt_module_get_current_channel_vu_right.restype  = ctypes.c_float

    lib._sb_bound = True
    return True


# ── Public extraction API ───────────────────────────────────────────────────

@dataclass
class VUResult:
    """Result of one VU-extraction pass."""
    channels:    int
    sample_rate: int     # Hz at which frames were captured (typically 30)
    frames:      int     # total number of frames captured
    mono:        bytes   # frames * channels bytes (row-major: frame 0 ch 0..N, frame 1 ch 0..N, …)
    pan:         bytes   # channels bytes (0 = center, 1 = left, 2 = right)


# Default cadence — see docs/vu-cache-format.md for the rationale.
DEFAULT_VU_RATE_HZ  = 30
# Pan classification thresholds.  After integrating over the whole
# song, a channel is "left" if its mean L exceeds its mean R by this
# factor (and vice versa).  1.5 is loose enough to call slightly-off-
# centre channels as centre, tight enough to catch hard-panned MOD
# channels (Amiga 1+4 left, 2+3 right) cleanly.
_PAN_RATIO_THRESHOLD = 1.5


def extract_vu(
    file_bytes: bytes,
    *,
    subsong:     int = -1,
    rate_hz:     int = DEFAULT_VU_RATE_HZ,
    samplerate:  int = 48000,
    max_seconds: float = 0.0,
) -> VUResult | None:
    """Open *file_bytes* via libopenmpt, advance the mixer in
    ``samplerate / rate_hz``-sample chunks, and capture per-channel
    VU on each chunk boundary.

    ``subsong``: -1 means "default" (libopenmpt picks 0 for plain
    modules, the engine-chosen default for IT/MPTM with multiple
    subsongs).  Pass an integer to force a specific subsong.

    ``max_seconds``: defensive ceiling on how long the VU pass will
    run.  0.0 means "use the module's reported duration".  Useful
    for malformed modules that report 0 duration.

    Returns ``None`` on any failure path (library not loaded, file
    not parseable, no channels).
    """
    if not _bind():
        return None
    lib = _load()
    assert lib is not None  # _bind() returned True

    # Allocate the error pointer + message slot.
    err     = ctypes.c_int(0)
    err_msg = ctypes.c_char_p(None)
    buf     = (ctypes.c_char * len(file_bytes)).from_buffer_copy(file_bytes)

    mod = lib.openmpt_module_create_from_memory2(
        ctypes.cast(buf, ctypes.c_void_p), len(file_bytes),
        None, None, None, None,
        ctypes.byref(err), ctypes.byref(err_msg),
        None,
    )
    if not mod:
        msg = err_msg.value.decode("utf-8", "replace") if err_msg.value else "unknown"
        log.debug("openmpt_vu: module_create failed (err=%d): %s", err.value, msg)
        return None

    try:
        if subsong >= 0:
            try:
                lib.openmpt_module_select_subsong(mod, subsong)
            except Exception:
                pass

        channels = int(lib.openmpt_module_get_num_channels(mod))
        if channels <= 0:
            log.debug("openmpt_vu: 0 channels reported — bailing")
            return None
        # Cap at 32 — VUMR's channels field is 1 byte and 32 covers
        # every realistic tracker format (IT supports more in theory
        # but practical files don't).
        if channels > 32:
            log.warning(
                "openmpt_vu: module reports %d channels; clamping to 32",
                channels,
            )
            channels = 32

        duration = float(lib.openmpt_module_get_duration_seconds(mod))
        if max_seconds > 0:
            duration = min(duration, max_seconds)
        elif subsong < 1 and duration > 0:
            # The length SoniqBoom renders: a song-end loop is cut where it
            # first repeats (``song_end_loop``) — the sidecar matches the WAV.
            cut = _loop_cut(lib, mod, file_bytes)
            if cut is not None:
                duration = min(duration, cut)
        if duration <= 0:
            duration = 600.0  # 10-min defensive ceiling for broken metadata

        chunk_samples = max(1, samplerate // rate_hz)
        total_frames  = int(duration * rate_hz) + 1

        # Output buffers — Bytes() to avoid per-frame allocs.
        mono_out = bytearray(total_frames * channels)
        # For pan derivation we accumulate L/R sums per channel across
        # the whole song.  Local floats (no numpy dependency).
        l_sum = [0.0] * channels
        r_sum = [0.0] * channels

        # Reusable PCM scratch (we throw the audio away).
        left  = (ctypes.c_float * chunk_samples)()
        right = (ctypes.c_float * chunk_samples)()

        frame_idx = 0
        while frame_idx < total_frames:
            read = lib.openmpt_module_read_float_stereo(
                mod, samplerate, chunk_samples, left, right,
            )
            if read == 0:
                # Module finished early.  Stop here — captured frames
                # accurately reflect the audio that was produced.
                break

            base = frame_idx * channels
            for ch in range(channels):
                m  = lib.openmpt_module_get_current_channel_vu_mono(mod, ch)
                lv = lib.openmpt_module_get_current_channel_vu_left(mod, ch)
                rv = lib.openmpt_module_get_current_channel_vu_right(mod, ch)
                # Clamp + quantise to uint8 0–255.
                level = max(0.0, min(1.0, m))
                mono_out[base + ch] = int(level * 255)
                l_sum[ch] += max(0.0, lv)
                r_sum[ch] += max(0.0, rv)
            frame_idx += 1

        # Pan classification from accumulated L/R sums.
        pan_out = bytearray(channels)
        for ch in range(channels):
            l = l_sum[ch]
            r = r_sum[ch]
            if l + r <= 1e-9:
                pan_out[ch] = 0  # silent channel → centre is the safe default
            elif l > r * _PAN_RATIO_THRESHOLD:
                pan_out[ch] = 1  # left
            elif r > l * _PAN_RATIO_THRESHOLD:
                pan_out[ch] = 2  # right
            else:
                pan_out[ch] = 0  # centre

        # Trim mono_out to the frames actually captured (early-exit case).
        captured_frames = frame_idx
        mono_out = bytes(mono_out[:captured_frames * channels])

        return VUResult(
            channels=channels,
            sample_rate=rate_hz,
            frames=captured_frames,
            mono=mono_out,
            pan=bytes(pan_out),
        )
    finally:
        try:
            lib.openmpt_module_destroy(mod)
        except Exception:
            pass


def _open_silent(lib, file_bytes: bytes):
    """A module handle for *file_bytes* that logs nothing to stderr
    (``openmpt_log_func_silent``), or None when libopenmpt can't open them."""
    if not hasattr(lib, "_sb_silent"):
        try:
            lib._sb_silent = ctypes.cast(lib.openmpt_log_func_silent, ctypes.c_void_p)
        except AttributeError:
            lib._sb_silent = None               # a very old library: its default logging
        lib.openmpt_free_string.argtypes = [ctypes.c_void_p]
        lib.openmpt_free_string.restype = None
    err = ctypes.c_int(0)
    err_msg = ctypes.c_char_p(None)
    mod = lib.openmpt_module_create_from_memory2(
        ctypes.c_char_p(file_bytes), len(file_bytes),
        lib._sb_silent, None, None, None,
        ctypes.byref(err), ctypes.byref(err_msg),
        None,
    )
    if not mod:
        msg = ctypes.cast(err_msg, ctypes.c_void_p).value
        if msg:
            lib.openmpt_free_string(msg)
        return None
    return mod


def module_duration(file_bytes: bytes) -> float | None:
    """The play length of the module in *file_bytes*, in seconds, without
    starting a process: what ``openmpt123 --info`` reports as its Duration
    (every subsong played in turn, subsong -1), to the same millisecond
    (``duration_ms``) — or, for a module that ends in a repeat-count loop
    (``song_end_loop``), the time the loop first repeats, which is what
    SoniqBoom renders.

    ``None`` when libopenmpt can't be loaded on this host (the caller falls
    back to the CLI); ``0.0`` for bytes libopenmpt can't open.  Nothing is
    logged to stderr (``openmpt_log_func_silent``)."""
    if not _bind():
        return None
    lib = _load()
    assert lib is not None
    mod = _open_silent(lib, file_bytes)
    if not mod:
        return 0.0
    try:
        lib.openmpt_module_select_subsong(mod, -1)
        duration = float(lib.openmpt_module_get_duration_seconds(mod))
        if duration <= 0:
            return 0.0
        cut = _loop_cut(lib, mod, file_bytes)
        return duration_ms(cut if cut is not None else duration) / 1000
    finally:
        lib.openmpt_module_destroy(mod)


# ── Song-end loops written as a repeat count ────────────────────────────────
#
# libopenmpt plays a module until it would repeat itself — a jump back to an
# earlier position ends the song there — but a loop with a repeat COUNT is
# played that many times.  Face The Music's loader writes a counted loop out
# into the order list, pass by pass: Aminet's "Jonahara" sets its closing 32
# measures to repeat 63 times (the most its 6-bit count holds) and libopenmpt
# plays 5:02 of music followed by 62 sample-identical repeats of the last
# 58.9 s — 65:56, a 1.5 GB render.
#
# The rule: when the order list ENDS in one block of orders played back to
# back at least ``LOOP_MIN_PASSES`` times, and two of those passes render
# sample for sample the same (float output, bit-exact), the song ends where
# the block first repeats — exactly where libopenmpt ends a module that
# jumps back to that block instead of counting.  Everything else plays as
# libopenmpt plays it: repeats with more music after them, fewer passes than
# ``LOOP_MIN_PASSES`` (an arrangement that plays its last section twice or
# three times), passes that change (a fade or a slide across the repeats),
# modules with several subsongs, and libraries without the position API.
LOOP_MIN_PASSES = 8
# Longest order list examined, and longest single pass compared (seconds):
# beyond these the rule does not apply.
_LOOP_MAX_ORDERS = 8192
_LOOP_MAX_PASS_S = 600.0
_LOOP_RATE = 44100
_LOOP_CHUNK = 4096


def terminal_block(orders: "list[int]", num_patterns: int,
                   min_passes: int = LOOP_MIN_PASSES) -> "tuple[int, int, int] | None":
    """``(first, length, passes)`` of the shortest block of orders that the
    order list ``orders`` ends with, repeated back to back at least
    ``min_passes`` times — ``orders[first:]`` is ``passes`` copies of
    ``orders[first:first + length]`` — or None.  An entry that names no
    pattern (a skip / end marker, or ``>= num_patterns``) in the repeats
    disqualifies them.  Pure."""
    n = len(orders)
    if n < min_passes or n > _LOOP_MAX_ORDERS:
        return None
    for k in range(1, n // min_passes + 1):
        block = orders[n - k:]
        if any(not (0 <= p < num_patterns) for p in block):
            return None
        passes = 1
        while n - (passes + 1) * k >= 0 and orders[n - (passes + 1) * k:n - passes * k] == block:
            passes += 1
        if passes >= min_passes:
            return n - passes * k, k, passes
    return None


def _bind_positions(lib) -> bool:
    """Prototypes of the order / position calls the loop rule uses; False on
    a library without them (``openmpt_module_get_time_at_position`` is
    libopenmpt 0.7+)."""
    if getattr(lib, "_sb_pos_bound", None) is not None:
        return lib._sb_pos_bound
    try:
        V = _Module
        lib.openmpt_module_get_num_orders.argtypes = [V]
        lib.openmpt_module_get_num_orders.restype = ctypes.c_int32
        lib.openmpt_module_get_num_patterns.argtypes = [V]
        lib.openmpt_module_get_num_patterns.restype = ctypes.c_int32
        lib.openmpt_module_get_order_pattern.argtypes = [V, ctypes.c_int32]
        lib.openmpt_module_get_order_pattern.restype = ctypes.c_int32
        lib.openmpt_module_get_time_at_position.argtypes = [V, ctypes.c_int32, ctypes.c_int32]
        lib.openmpt_module_get_time_at_position.restype = ctypes.c_double
        lib.openmpt_module_set_position_order_row.argtypes = [V, ctypes.c_int32, ctypes.c_int32]
        lib.openmpt_module_set_position_order_row.restype = ctypes.c_double
        lib.openmpt_module_get_current_order.argtypes = [V]
        lib.openmpt_module_get_current_order.restype = ctypes.c_int32
        lib.openmpt_module_read_interleaved_float_stereo.argtypes = [
            V, ctypes.c_int32, ctypes.c_size_t, ctypes.POINTER(ctypes.c_float)]
        lib.openmpt_module_read_interleaved_float_stereo.restype = ctypes.c_size_t
        lib._sb_pos_bound = True
    except AttributeError:
        lib._sb_pos_bound = False
    return lib._sb_pos_bound


def _ctl_on(lib, mod, name: bytes) -> bool:
    """Set the boolean ctl ``name`` of ``mod`` on; False when it can't be."""
    try:
        f = lib.openmpt_module_ctl_set_boolean
        f.argtypes = [_Module, ctypes.c_char_p, ctypes.c_int]
        f.restype = ctypes.c_int
        return bool(f(mod, name, 1))
    except AttributeError:
        pass
    try:
        f = lib.openmpt_module_ctl_set                 # (before libopenmpt 0.5)
        f.argtypes = [_Module, ctypes.c_char_p, ctypes.c_char_p]
        f.restype = ctypes.c_int
        return bool(f(mod, name, b"1"))
    except AttributeError:
        return False


def _passes_identical(lib, file_bytes: bytes, start_a: int, start_b: int,
                      stop_a: int, max_frames: int) -> bool:
    """Do the passes starting at orders ``start_a`` and ``start_b`` render
    the same, sample for sample?  Two copies of the module, one placed at
    each, are rendered in step (float, ``_LOOP_RATE``) and compared chunk by
    chunk — memory stays one chunk whatever the length — until copy A
    reaches order ``stop_a`` (the start of its next pass).  False on any
    difference, on an end before that, or past ``max_frames``."""
    a = _open_silent(lib, file_bytes)
    b = _open_silent(lib, file_bytes) if a else None
    if not (a and b):
        for m in (a, b):
            if m:
                lib.openmpt_module_destroy(m)
        return False
    try:
        # A seek must rebuild what still sounds from before (a long sample
        # played on through the repeats): without it a pass would look like
        # the next one while the real playback differs.  libopenmpt's default,
        # set explicitly — the rule doesn't apply where it can't be.
        if not (_ctl_on(lib, a, b"seek.sync_samples") and _ctl_on(lib, b, b"seek.sync_samples")):
            return False
        lib.openmpt_module_set_position_order_row(a, start_a, 0)
        lib.openmpt_module_set_position_order_row(b, start_b, 0)
        buf_a = (ctypes.c_float * (2 * _LOOP_CHUNK))()
        buf_b = (ctypes.c_float * (2 * _LOOP_CHUNK))()
        done = 0
        compared = 0
        while done < max_frames:
            na = lib.openmpt_module_read_interleaved_float_stereo(a, _LOOP_RATE, _LOOP_CHUNK, buf_a)
            nb = lib.openmpt_module_read_interleaved_float_stereo(b, _LOOP_RATE, _LOOP_CHUNK, buf_b)
            if lib.openmpt_module_get_current_order(a) >= stop_a:
                # This chunk crossed into A's next pass: its part after the
                # boundary is compared with B's next pass — still the same
                # if the passes are, but not needed.
                return compared > 0
            if na != _LOOP_CHUNK or nb != _LOOP_CHUNK:
                return False
            if ctypes.string_at(buf_a, 8 * na) != ctypes.string_at(buf_b, 8 * nb):
                return False
            done += na
            compared += 1
        return False
    finally:
        lib.openmpt_module_destroy(a)
        lib.openmpt_module_destroy(b)


def song_end_loop(file_bytes: bytes, subsong: int = -1) -> "float | None":
    """Seconds at which the module in *file_bytes* ends under the song-end
    loop rule (see above), or None when the rule doesn't apply — the module
    then plays its full libopenmpt length.  Only the default play (subsong
    -1 / 0) of a module with one subsong is ever cut.  Never raises."""
    if subsong > 0 or not file_bytes:
        return None
    try:
        if not _bind():
            return None
        lib = _load()
        if lib is None:
            return None
        mod = _open_silent(lib, file_bytes)
        if not mod:
            return None
        try:
            return _loop_cut(lib, mod, file_bytes)
        finally:
            lib.openmpt_module_destroy(mod)
    except Exception:                                   # noqa: BLE001
        log.debug("openmpt_vu: song-end loop check failed", exc_info=True)
        return None


def _loop_cut(lib, mod, file_bytes: bytes) -> "float | None":
    """``song_end_loop`` on the open module ``mod`` (made from *file_bytes*;
    its play position is not moved).  Only a module whose order list ends in
    a repeated block (``terminal_block``) costs more than reading its order
    list: two more copies render two passes to compare.  Never raises."""
    try:
        if not _bind_positions(lib):
            return None
        if int(lib.openmpt_module_get_num_subsongs(mod)) != 1:
            return None
        n = int(lib.openmpt_module_get_num_orders(mod))
        if not (LOOP_MIN_PASSES <= n <= _LOOP_MAX_ORDERS):
            return None
        npat = int(lib.openmpt_module_get_num_patterns(mod))
        orders = [int(lib.openmpt_module_get_order_pattern(mod, i)) for i in range(n)]
        found = terminal_block(orders, npat)
        if found is None:
            return None
        first, k, passes = found
        # Start times of the first four passes: each pass must be played, in
        # order, and as long as the others; and the song's length must be
        # the passes played through to its end.
        t = [float(lib.openmpt_module_get_time_at_position(mod, first + i * k, 0))
             for i in range(4)]
        duration = float(lib.openmpt_module_get_duration_seconds(mod))
        if min(t) < 0 or not (t[0] < t[1] < t[2] < t[3] < duration):
            return None
        pass_s = t[2] - t[1]
        if pass_s <= 0 or pass_s > _LOOP_MAX_PASS_S:
            return None
        if any(abs((t[i + 1] - t[i]) - pass_s) > 0.05 for i in range(3)):
            return None
        if abs(duration - (t[0] + passes * pass_s)) > max(1.0, 0.002 * duration):
            return None
        # The second and third passes (the first follows other music, whose
        # notes may still ring into it) must sound the same.
        max_frames = int((pass_s + 2.0) * _LOOP_RATE)
        if not _passes_identical(lib, file_bytes, first + k, first + 2 * k,
                                 first + 2 * k, max_frames):
            return None
        return t[1]
    except Exception:                                   # noqa: BLE001
        log.debug("openmpt_vu: song-end loop check failed", exc_info=True)
        return None


def render_plan(file_bytes: bytes, subsong: int = 0) -> "tuple[float, float | None] | None":
    """``(seconds, end_at)`` of the render of tune index ``subsong`` (0: the
    default play — every subsong in turn, as ``openmpt123`` without
    ``--subsong``; N > 0: libopenmpt subsong N): how long it plays, and the
    time to stop it at (``openmpt123 --end-time``) when a song-end loop is
    cut there (``song_end_loop``), else None.  ``(0.0, None)`` for bytes
    libopenmpt can't open; None when libopenmpt can't be loaded.  Never
    raises."""
    try:
        if not _bind():
            return None
        lib = _load()
        assert lib is not None
        mod = _open_silent(lib, file_bytes)
        if not mod:
            return 0.0, None
        try:
            lib.openmpt_module_select_subsong(mod, subsong if subsong > 0 else -1)
            duration = float(lib.openmpt_module_get_duration_seconds(mod))
            if duration <= 0:
                return 0.0, None
            cut = _loop_cut(lib, mod, file_bytes) if subsong <= 0 else None
        finally:
            lib.openmpt_module_destroy(mod)
        if cut is not None and 0 < cut < duration:
            return cut, cut
        return duration, None
    except Exception:                                   # noqa: BLE001
        log.debug("openmpt_vu: render plan failed", exc_info=True)
        return None


def duration_ms(seconds: float) -> int:
    """Whole milliseconds of *seconds*, cut as ``openmpt123`` cuts them
    for its Duration line (``int64(seconds * 1000)``) — so a duration read
    in-process equals one parsed from the CLI to the last bit."""
    return int(seconds * 1000)


# ── VUMR serialisation ──────────────────────────────────────────────────────

VUMR_MAGIC   = b"VUMR"
VUMR_VERSION = 1


def serialize_vumr(result: VUResult) -> bytes:
    """Encode a :class:`VUResult` as the on-disk VUMR v1 binary format.

    See ``docs/vu-cache-format.md`` for the byte-by-byte layout.
    """
    if not (1 <= result.channels <= 32):
        raise ValueError(f"channels out of range: {result.channels}")
    if result.sample_rate <= 0 or result.sample_rate > 240:
        raise ValueError(f"sample_rate out of range: {result.sample_rate}")
    if result.frames < 0 or result.frames > 0xFFFFFFFF:
        raise ValueError(f"frames out of range: {result.frames}")
    if len(result.pan) != result.channels:
        raise ValueError(
            f"pan size mismatch: {len(result.pan)} vs {result.channels}",
        )
    expected_mono = result.frames * result.channels
    if len(result.mono) != expected_mono:
        raise ValueError(
            f"mono size mismatch: {len(result.mono)} vs {expected_mono}",
        )

    header = bytearray(16)
    header[0:4]   = VUMR_MAGIC
    header[4]     = VUMR_VERSION
    header[5]     = result.channels
    header[6]     = 0  # flags, reserved
    header[7]     = 0  # reserved
    header[8:12]  = result.sample_rate.to_bytes(4, "little")
    header[12:16] = result.frames.to_bytes(4, "little")

    return bytes(header) + result.pan + result.mono


def parse_and_validate_vumr(raw: bytes, *, max_channels: int = 32,
                            max_frames: int = 240 * 600) -> "tuple[int, int, int]":
    """Validate a raw VUMR v1 blob (e.g. an untrusted client upload) against the
    exact invariants :func:`serialize_vumr` guarantees, WITHOUT trusting the
    sender.  Returns ``(channels, sample_rate, frames)`` on success; raises
    ``ValueError`` on any structural violation so a malformed/oversized payload
    can never reach disk or the meter parser.

    Checks: magic ``b"VUMR"``, version 1, ``1 <= channels <= max_channels``,
    ``1 <= sample_rate <= 240``, ``0 <= frames <= max_frames``, and an EXACT
    total length ``16 + channels + frames*channels`` (rejects trailing garbage
    or a truncated body — the frontend parser tolerates trailing bytes, so the
    length gate is what actually bounds a hostile upload).
    """
    if len(raw) < 16:
        raise ValueError("VUMR too short for header")
    if raw[0:4] != VUMR_MAGIC:
        raise ValueError("bad VUMR magic")
    if raw[4] != VUMR_VERSION:
        raise ValueError(f"unsupported VUMR version: {raw[4]}")
    channels = raw[5]
    sample_rate = int.from_bytes(raw[8:12], "little")
    frames = int.from_bytes(raw[12:16], "little")
    if not (1 <= channels <= max_channels):
        raise ValueError(f"channels out of range: {channels}")
    if not (1 <= sample_rate <= 240):
        raise ValueError(f"sample_rate out of range: {sample_rate}")
    if not (0 <= frames <= max_frames):
        raise ValueError(f"frames out of range: {frames}")
    expected = 16 + channels + frames * channels
    if len(raw) != expected:
        raise ValueError(f"length mismatch: {len(raw)} != {expected}")
    return channels, sample_rate, frames


def write_sidecar(path: Path, result: VUResult) -> None:
    """Atomic write of the VUMR sidecar to *path*."""
    payload = serialize_vumr(result)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def write_sidecar_bytes(path: Path, raw: bytes) -> None:
    """Atomic write of pre-serialized VUMR *raw* bytes (validated upstream) to
    *path* — the client-upload analogue of :func:`write_sidecar`.

    Unlike :func:`write_sidecar` (serialised behind ``_SID_VU_SEM`` server-side),
    concurrent client uploads for the same track are unsynchronised, so the tmp
    name is made unique per write — a shared ``<name>.tmp`` would let one
    writer's ``os.replace`` rename the tmp out from under another's, raising
    ``FileNotFoundError``.  Each writer renames its own tmp; last rename wins,
    the sidecar is never torn."""
    import uuid
    tmp = path.parent / f"{path.name}.{uuid.uuid4().hex}.tmp"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_bytes(raw)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
