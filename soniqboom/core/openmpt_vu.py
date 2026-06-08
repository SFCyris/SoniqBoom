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


def _load() -> ctypes.CDLL | None:
    """Try every reasonable name + path; return the first that loads.

    Cached after the first successful resolution.  A failed load
    returns ``None`` and is NOT retried — the operator can re-import
    after fixing their library install.
    """
    global _LIB
    if _LIB is not None:
        return _LIB
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


def write_sidecar(path: Path, result: VUResult) -> None:
    """Atomic write of the VUMR sidecar to *path*."""
    payload = serialize_vumr(result)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(payload)
    os.replace(tmp, path)
