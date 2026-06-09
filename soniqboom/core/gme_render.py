"""In-process ``ctypes`` binding for **libgme** (game-music-emu) — renders
console / chiptune formats (NSF, NSFe, SPC, GBS, VGM/VGZ, AY, KSS, SAP, GYM,
HES) to a WAV entirely in-process.

Why ctypes (same reasoning as ``openmpt_vu.py``)
------------------------------------------------
On macOS the only practical libgme renderer is the shared library itself:
Homebrew's ``ffmpeg`` ships **without** ``--enable-libgme`` (so the GME demuxer
is absent), and there is no standalone ``gme`` CLI that does file→WAV.  We bind
``libgme`` directly and write the WAV ourselves, so GME formats play with no
external tool beyond ``brew install game-music-emu`` / ``apt install libgme0``
(added to ``install.sh``).

Used by ``soniqboom.api.stream._render_gme`` as the preferred path, with the
existing ``gme`` CLI / ffmpeg-libgme branches kept as fallbacks.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import io
import logging
import wave

log = logging.getLogger(__name__)

_SR = 44100  # libgme renders 16-bit stereo at the rate we request


def _load_libgme():
    """Locate and load libgme, or return ``None`` if unavailable."""
    candidates: list[str] = []
    for name in ("gme",):
        p = ctypes.util.find_library(name)
        if p:
            candidates.append(p)
    candidates += [
        "/opt/homebrew/opt/game-music-emu/lib/libgme.dylib",  # Apple Silicon brew
        "/usr/local/opt/game-music-emu/lib/libgme.dylib",     # Intel brew
        "libgme.so.0", "libgme.so",                            # Linux runtime
        "/usr/lib/x86_64-linux-gnu/libgme.so.0",
        "libgme.dylib",
    ]
    for cand in candidates:
        try:
            return ctypes.CDLL(cand)
        except OSError:
            continue
    return None


_lib = _load_libgme()

if _lib is not None:
    try:
        _lib.gme_open_data.restype = ctypes.c_char_p
        _lib.gme_open_data.argtypes = [
            ctypes.c_char_p, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
        ]
        _lib.gme_start_track.restype = ctypes.c_char_p
        _lib.gme_start_track.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _lib.gme_play.restype = ctypes.c_char_p
        _lib.gme_play.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_short)]
        _lib.gme_set_fade.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _lib.gme_track_ended.restype = ctypes.c_int
        _lib.gme_track_ended.argtypes = [ctypes.c_void_p]
        _lib.gme_delete.argtypes = [ctypes.c_void_p]
    except AttributeError as exc:        # pragma: no cover - wrong/old lib
        log.warning("libgme loaded but missing expected symbols (%s); disabling", exc)
        _lib = None


def is_available() -> bool:
    """True if libgme is loaded and usable."""
    return _lib is not None


def render_wav(data: bytes, subsong: int = 0, duration_s: int = 180) -> bytes | None:
    """Render GME file *data* (raw NSF/SPC/… bytes) to a 44.1 kHz 16-bit
    stereo WAV and return the WAV bytes.

    ``subsong`` is the 0-based track index (NSF/GBS/AY can hold many).
    ``duration_s`` caps the render — many chiptunes loop forever, so we stop at
    the cap (or when the track genuinely ends) and fade out the last ~8 s.

    Returns ``None`` on any failure so the caller can fall back to the CLI /
    ffmpeg path.  Never raises.
    """
    if _lib is None or not data:
        return None
    emu = ctypes.c_void_p()
    try:
        err = _lib.gme_open_data(data, len(data), ctypes.byref(emu), _SR)
    except Exception:
        log.debug("gme_open_data raised", exc_info=True)
        return None
    if err or not emu:
        log.debug("gme_open_data failed: %s", err)
        return None
    try:
        track = subsong if subsong and subsong > 0 else 0
        err = _lib.gme_start_track(emu, track)
        if err:
            log.debug("gme_start_track(%d) failed: %s", track, err)
            return None
        dur = max(1, int(duration_s))
        # Fade out the final ~8 s so capped/looping tunes end cleanly.
        _lib.gme_set_fade(emu, max(1000, dur * 1000 - 8000))
        total_frames = _SR * dur
        n_shorts = 8192                  # stereo interleaved → n_shorts/2 frames
        pcm = bytearray()
        frames = 0
        while frames < total_frames and not _lib.gme_track_ended(emu):
            buf = (ctypes.c_short * n_shorts)()
            if _lib.gme_play(emu, n_shorts, buf):
                break
            pcm += bytes(buf)
            frames += n_shorts // 2
        if not pcm:
            return None
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(_SR)
            w.writeframes(bytes(pcm))
        return out.getvalue()
    except Exception:
        log.warning("libgme render failed", exc_info=True)
        return None
    finally:
        try:
            _lib.gme_delete(emu)
        except Exception:
            pass
