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

import array
import ctypes
import ctypes.util
import logging
import os
import struct
import sys
import zlib

log = logging.getLogger(__name__)

_SR = 44100  # libgme renders 16-bit stereo at the rate we request


def _load_libgme():
    """Locate and load libgme, or return ``None`` if unavailable."""
    # A libgme built into /usr/local (the Docker image's 0.6.5) before the
    # distribution's copy another package depends on (Debian: 0.6.3 for ffmpeg).
    candidates: list[str] = ["/usr/local/lib/libgme.so.0"] if sys.platform.startswith("linux") else []
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


# ``gme_track_info`` — the lengths a file states (optional: an old libgme
# without it still renders, at the default length).
class _GmeInfo(ctypes.Structure):
    # Leading fields of gme_info_t (gme.h); libgme allocates and frees it, so
    # only the fields read here need declaring.
    _fields_ = [("length", ctypes.c_int), ("intro_length", ctypes.c_int),
                ("loop_length", ctypes.c_int), ("play_length", ctypes.c_int)]


_have_info = False
if _lib is not None:
    try:
        _lib.gme_track_info.restype = ctypes.c_char_p
        _lib.gme_track_info.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_GmeInfo)), ctypes.c_int,
        ]
        _lib.gme_free_info.argtypes = [ctypes.POINTER(_GmeInfo)]
        _have_info = True
    except AttributeError:               # pragma: no cover - old lib
        log.info("libgme has no gme_track_info; chiptunes play at the default length")

# Seconds a tune fades out over after its stated length (the fade a capped /
# looping tune has always had — GYM's own length is extended by it too).
FADE_S = 8
# Sanity bounds on a length a file states (ID666 / NSFe / VGM header).
# (A stated length from 0.1 s: a console sound effect can be shorter than a
# second; 0 / -1 mean "none stated".)
_MIN_STATED_S, _MAX_STATED_S = 0.1, 3600.0


def is_available() -> bool:
    """True if libgme is loaded and usable."""
    return _lib is not None


def play_seconds(data: bytes, subsong: int = 0) -> float | None:
    """How long track ``subsong`` of GME ``data`` plays before its fade, from
    the lengths the file states (libgme ``gme_track_info``): its own length
    (SPC ID666, NSFe, an M3U-tagged rip), else intro + the loop played twice
    (VGM, as libgme's own ``play_length``).  None when the file states none
    (most NSF / GBS / AY) — the caller keeps the default.  Never raises;
    blocking."""
    if _lib is None or not _have_info or not data:
        return None
    emu = ctypes.c_void_p()
    try:
        if _lib.gme_open_data(data, len(data), ctypes.byref(emu), _SR) or not emu:
            return None
    except Exception:
        return None
    info = ctypes.POINTER(_GmeInfo)()
    try:
        track = subsong if subsong and subsong > 0 else 0
        if _lib.gme_track_info(emu, ctypes.byref(info), track) or not info:
            return None
        i = info.contents
        if i.length > 0:
            ms = i.length
        elif i.loop_length > 0:
            ms = max(0, i.intro_length) + 2 * i.loop_length
        else:
            return None
        secs = ms / 1000.0
        return round(secs, 2) if _MIN_STATED_S <= secs <= _MAX_STATED_S else None
    except Exception:
        log.debug("gme_track_info failed", exc_info=True)
        return None
    finally:
        try:
            if info:
                _lib.gme_free_info(info)
            _lib.gme_delete(emu)
        except Exception:
            pass


def lib_name() -> str:
    """The path/name libgme was loaded from — for the admin status panel."""
    return getattr(_lib, "_name", "libgme") if _lib is not None else ""


def lib_path() -> str:
    """The file libgme was loaded from, symlinks resolved ("" when unknown):
    the name it was loaded by when that is a path, else the libgme image
    this process has mapped (Linux ``/proc/self/maps``, macOS dyld)."""
    if _lib is None:
        return ""
    name = getattr(_lib, "_name", "") or ""
    if os.path.isabs(name) and os.path.exists(name):
        return os.path.realpath(name)
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/self/maps", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    path = line.rstrip("\n").split(" ", 5)[-1].strip()
                    if "/libgme" in path:
                        return os.path.realpath(path)
        elif sys.platform == "darwin":
            dyld = ctypes.CDLL(None)
            dyld._dyld_get_image_name.restype = ctypes.c_char_p
            dyld._dyld_get_image_name.argtypes = [ctypes.c_uint32]
            for i in range(dyld._dyld_image_count()):
                img = (dyld._dyld_get_image_name(i) or b"").decode("utf-8", "replace")
                if "/libgme" in img:
                    return os.path.realpath(img)
    except Exception:
        pass
    return ""


def lib_version() -> str:
    """libgme's version as its file name states it (``libgme.0.6.5.dylib``,
    ``libgme.so.0.6.5``), or "" when it states none."""
    import re
    m = re.search(r"libgme(?:\.so)?\.(\d+\.\d+\.\d+)", os.path.basename(lib_path()))
    return m.group(1) if m else ""


# ── GYM (Mega Drive register dumps) ───────────────────────────────────────
# A ``GYMX`` file carries a 428-byte header (song, game, copyright, emulator,
# dumper — 32 bytes each — a 256-byte comment, the loop start in frames and
# the UNPACKED size); when that size is not 0 the body is zlib-compressed.
# libgme refuses packed files ("Packed GYM file not supported"), so they are
# unpacked first — the same file with a zero size field and the raw body.
GYMX_HEADER = 428
_GYM_MAX_UNPACKED = 64 * 1024 * 1024     # a GYM is 60 register frames/s — MBs at most


def gymx_fields(data: bytes) -> "dict | None":
    """``{"song", "game", "copyright", "loop_start", "packed_size"}`` of a GYMX
    header, or None when ``data`` doesn't start with one."""
    if len(data) < GYMX_HEADER or data[:4] != b"GYMX":
        return None

    def text(off: int, n: int) -> str:
        return data[off:off + n].split(b"\0", 1)[0].decode("latin-1").strip()
    loop, packed = struct.unpack_from("<II", data, 420)
    return {"song": text(4, 32), "game": text(36, 32), "copyright": text(68, 32),
            "loop_start": loop, "packed_size": packed}


def unpack_gym(data: bytes) -> bytes:
    """``data`` with a packed GYMX body inflated (header kept, its size field
    zeroed) — what libgme can play; anything else is returned unchanged.
    Raises ``ValueError`` when the packed body is damaged or doesn't inflate
    to the size its header gives."""
    f = gymx_fields(data)
    if f is None or not f["packed_size"]:
        return data
    want = f["packed_size"]
    if want > _GYM_MAX_UNPACKED:
        raise ValueError(f"GYMX unpacked size {want} is implausible")
    d = zlib.decompressobj()
    try:
        raw = d.decompress(data[GYMX_HEADER:], want + 1)
    except zlib.error as exc:
        raise ValueError(f"GYMX body does not inflate: {exc}") from exc
    if len(raw) != want:
        raise ValueError(f"GYMX body inflates to {len(raw)} bytes, header says {want}")
    head = bytearray(data[:GYMX_HEADER])
    struct.pack_into("<I", head, 424, 0)
    return bytes(head) + raw


def gym_seconds(data: bytes) -> "float | None":
    """Play length of an (unpacked) GYM / GYMX: one 1/60 s frame per ``0x00``
    command (``0x01``/``0x02`` are 3-byte YM2612 writes, ``0x03`` a 2-byte
    PSG write).  None when there are no frames."""
    body = data[GYMX_HEADER:] if data[:4] == b"GYMX" else data
    i, n, frames = 0, len(body), 0
    while i < n:
        c = body[i]
        if c == 0:
            frames += 1
            i += 1
        elif c in (1, 2):
            i += 3
        elif c == 3:
            i += 2
        else:
            i += 1
    return frames / 60.0 if frames else None


# ── Tune count / default tune from the file header ──────────────────────────
# Formats whose header states how many tunes the file holds (and which one it
# starts with).  libgme numbers tracks from 0 whatever the header's first
# song is.  HES and KSS have no count (libgme reports 256 slots, mostly
# silent), VGM / VGZ / GYM / SPC are one tune.
_MAX_TUNES = 256


# VGM header fields from 0x30 on that are NOT chip clocks (data offset, flags,
# volume / loop modifiers, the extra-header offset, channel counts).  Every
# other non-zero field there is a chip libgme's VGM player lacks — it plays
# only the SN76489 (0x0C) and YM2612 (0x2C); its YM2413 (0x10) is a stub that
# refuses the file ("YM2413 FM sound isn't supported").
_VGM_NOT_CLOCKS = {0x34, 0x3C, 0x78, 0x7C, 0x94, 0xBC, 0xD4}
_VGM_LAST_CLOCK = 0xE0                    # GA20; reserved after it


def vgm_needs_other_engine(path) -> bool:
    """Does the VGM / VGZ file ``path`` use a sound chip libgme can't play
    (YM2413, YM2151, NES APU, AY-3-8910, …)?  libgme renders those as silence, so the
    file goes to another engine.  False when the header can't be read."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64 * 1024)
    except OSError:
        return False
    return vgm_bytes_need_other_engine(head)


def vgm_bytes_need_other_engine(data: bytes) -> bool:
    """``vgm_needs_other_engine`` of a VGM / VGZ file's bytes (its start is
    enough: the header is in the first 256 bytes, unpacked)."""
    import gzip
    import io
    try:
        if data[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                hdr = gz.read(0x100)
        else:
            hdr = data[:0x100]
    except (OSError, EOFError, zlib.error):
        return False
    if len(hdr) < 0x40 or hdr[:4] != b"Vgm ":
        return False
    version = struct.unpack_from("<I", hdr, 0x08)[0]
    end = 0x40
    if version >= 0x150:
        rel = struct.unpack_from("<I", hdr, 0x34)[0]
        if rel:
            end = 0x34 + rel
    if version >= 0x110 and struct.unpack_from("<I", hdr, 0x10)[0] & 0x3FFFFFFF:
        return True                       # YM2413 (before 1.10 the field was shared)
    end = min(end, len(hdr), _VGM_LAST_CLOCK + 4)
    for off in range(0x30, end - 3, 4):
        if off not in _VGM_NOT_CLOCKS and struct.unpack_from("<I", hdr, off)[0] & 0x3FFFFFFF:
            return True
    return False


def header_tunes(data: bytes) -> "tuple[int | None, int | None]":
    """``(tune count, 0-based default tune)`` the header of GME ``data``
    states — NSF, NSFe, GBS, AY, SAP — each None when it states none."""
    def ok(n: int) -> "int | None":
        return n if 1 <= n <= _MAX_TUNES else None
    try:
        if data[:5] == b"NESM\x1a" and len(data) >= 8:          # NSF
            n = ok(data[6])
            return n, (data[7] - 1 if n and 1 <= data[7] <= n else None)
        if data[:3] == b"GBS" and len(data) >= 6:                # GBS
            n = ok(data[4])
            return n, (data[5] - 1 if n and 1 <= data[5] <= n else None)
        if data[:8] == b"ZXAYEMUL" and len(data) >= 18:          # AY
            n = ok(data[16] + 1)
            return n, (data[17] if n and data[17] < n else None)
        if data[:4] == b"NSFE":                                  # NSFe INFO chunk
            pos = 4
            for _ in range(64):
                if pos + 8 > len(data):
                    break
                size, cid = struct.unpack_from("<I4s", data, pos)
                if cid == b"INFO":
                    info = data[pos + 8:pos + 8 + size]
                    if len(info) >= 10:
                        n = ok(info[8])
                        return n, (info[9] if n and info[9] < n else None)
                    return None, None
                if cid == b"NEND":
                    break
                pos += 8 + size
            return None, None
        if data[:4] == b"SAP\r":                                 # SAP text header
            head = data[:data.find(b"\xff\xff")] if b"\xff\xff" in data else data[:4096]
            n = start = None
            for line in head.decode("latin-1", "replace").splitlines():
                key, _, val = line.strip().partition(" ")
                if key == "SONGS" and val.strip().isdigit():
                    n = ok(int(val.strip()))
                elif key == "DEFSONG" and val.strip().isdigit():
                    start = int(val.strip())
            return n, (start if n and start is not None and start < n else None)
    except (IndexError, struct.error):
        pass
    return None, None


def tune_lengths(data: bytes, count: int) -> "list[float] | None":
    """Each of the ``count`` tunes' length as it plays (``render_plan``: the
    stated length plus the fade), in tune order, 0 for a tune that states
    none; None when no tune states one.  Blocking."""
    out = []
    for k in range(max(0, min(int(count or 0), _MAX_TUNES))):
        total, fade = render_plan(data, k, 0.0)
        out.append(round(total, 2) if fade is not None else 0.0)
    return out if any(v > 0 for v in out) else None


def render_plan(data: bytes, subsong: int, default_s: float) -> tuple[float, float | None]:
    """``(seconds to render, fade start or None)`` for track ``subsong``: the
    stated length (``play_seconds``) plus the ``FADE_S`` fade after it, or
    ``default_s`` with the default fade over its last seconds.  The scan
    stores the same total (``metadata._extract_gme``), so a list shows what
    plays.  Blocking."""
    secs = play_seconds(data, subsong)
    if secs is None:
        return float(default_s), None
    return min(_MAX_STATED_S, secs + FADE_S), secs


def render_wav(data: bytes, subsong: int = 0, duration_s: float = 180,
               fade_at_s: float | None = None) -> bytes | None:
    """Render GME file *data* (raw NSF/SPC/… bytes) to a 44.1 kHz 16-bit
    stereo WAV and return the WAV bytes.

    ``subsong`` is the 0-based track index (NSF/GBS/AY can hold many).
    ``duration_s`` caps the render — many chiptunes loop forever, so we stop at
    the cap (or when the track genuinely ends) and fade out the last ~8 s —
    or, with ``fade_at_s``, from that point (a stated length; ``play_seconds``).

    Returns ``None`` on any failure so the caller can fall back to the CLI /
    ffmpeg path.  Never raises.
    """
    pcm = bytearray()
    if not render_pcm(data, subsong, duration_s, pcm.extend, fade_at_s=fade_at_s):
        return None
    # The canonical 44-byte PCM header (``render_pcm`` already gives
    # little-endian samples, so no ``wave`` module byte swapping).
    n = len(pcm)
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, _SR, _SR * 4, 4, 16)
            + b"data" + struct.pack("<I", n) + bytes(pcm))


def render_pcm(data: bytes, subsong: int = 0, duration_s: float = 180,
               sink=None, *, fade_at_s: float | None = None) -> int:
    """``render_wav``'s render, streamed: hand each block of 44.1 kHz 16-bit
    stereo little-endian PCM to ``sink(bytes)`` as libgme produces it (a
    caller can play the render while it runs).  ``sink`` returning False
    stops the render early.  Returns the PCM byte count — 0 on any failure
    (libgme can't open the data or start the track).  Never raises; blocking
    (run it in a worker thread)."""
    if _lib is None or not data or sink is None:
        return 0
    emu = ctypes.c_void_p()
    try:
        err = _lib.gme_open_data(data, len(data), ctypes.byref(emu), _SR)
    except Exception:
        log.debug("gme_open_data raised", exc_info=True)
        return 0
    if err or not emu:
        log.debug("gme_open_data failed: %s", err)
        return 0
    total = 0
    try:
        track = subsong if subsong and subsong > 0 else 0
        err = _lib.gme_start_track(emu, track)
        if err:
            log.debug("gme_start_track(%d) failed: %s", track, err)
            return 0
        dur = max(1.0, float(duration_s))
        # Fade out the final ~8 s so capped/looping tunes end cleanly — from
        # the stated length when there is one.
        fade_ms = (int(fade_at_s * 1000) if fade_at_s is not None
                   else int(dur * 1000) - FADE_S * 1000)
        _lib.gme_set_fade(emu, max(1000, fade_ms))
        total_frames = int(_SR * dur)
        n_shorts = 8192                  # stereo interleaved → n_shorts/2 frames
        frames = 0
        while frames < total_frames and not _lib.gme_track_ended(emu):
            buf = (ctypes.c_short * n_shorts)()
            if _lib.gme_play(emu, n_shorts, buf):
                break
            chunk = bytes(buf)
            if sys.byteorder == "big":       # WAV PCM is little-endian
                a = array.array("h")
                a.frombytes(chunk)
                a.byteswap()
                chunk = a.tobytes()
            total += len(chunk)
            frames += n_shorts // 2
            if sink(chunk) is False:
                break
        return total
    except Exception:
        log.warning("libgme render failed", exc_info=True)
        return 0
    finally:
        try:
            _lib.gme_delete(emu)
        except Exception:
            pass


def tune_audible(data: bytes, subsong: int = 0, *, min_seconds: float = 1.0,
                 max_seconds: float = 30.0) -> "bool | None":
    """Is track ``subsong`` of GME ``data`` a real tune: does it play at least
    ``min_seconds`` with audible sound (``core.silence``) within its first
    ``max_seconds``?  libgme ends a track after a few seconds of silence, so
    an empty track settles at once.  Stops as soon as it knows.  None when
    libgme can't open the data or start the track.  Never raises."""
    from soniqboom.core.silence import AudibilityMeter
    if _lib is None or not data:
        return None
    emu = ctypes.c_void_p()
    try:
        err = _lib.gme_open_data(data, len(data), ctypes.byref(emu), _SR)
    except Exception:
        return None
    if err or not emu:
        return None
    try:
        if _lib.gme_start_track(emu, max(0, int(subsong))):
            return None
        meter = AudibilityMeter(2, _SR)
        n_shorts = 8192
        buf = (ctypes.c_short * n_shorts)()
        while meter.seconds < max_seconds and not _lib.gme_track_ended(emu):
            if _lib.gme_play(emu, n_shorts, buf):
                break
            meter.feed(bytes(buf))
            if meter.audible and meter.seconds >= min_seconds:
                return True
        return meter.audible and meter.seconds >= min_seconds
    except Exception:
        log.debug("libgme probe failed", exc_info=True)
        return None
    finally:
        try:
            _lib.gme_delete(emu)
        except Exception:
            pass
