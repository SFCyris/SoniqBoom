# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Endless play of a looping retro tune (2.3.0): the engine plays on through
the tune's own loop — seamless at its loop point, never faded, never cached.

The web player asks for it for Repeat-One on a tune that loops and, with
"Play looping tunes endlessly" on, for every such tune
(``GET /api/stream/{id}/endless``, ``stream.stream_endless``).  The answer is
a WAV of ``render_options.ENDLESS_SECONDS`` (3 hours — a listener moves on
long before) produced on demand: a Range request starts the engine at that
offset (``start_s``), and the engine renders only as fast as the client
reads (a pipe, or a bounded queue for the in-process libgme — no file grows
on disk, nothing runs ahead).

Engines (``family``):

* ``tracker`` — openmpt123 ``--repeat -1 --seek <s> --stdout`` (32-bit
  float, 48 kHz stereo; the Amiga resampler setting as in the cached render);
* ``gme`` — libgme in-process (16-bit, 44.1 kHz stereo): no length, no fade,
  silence never ends the track; a tune that does end (a VGM without a loop)
  starts again at once — Repeat-One without a gap;
* ``uade`` — uade123 ``-n`` (the song end its player reports is ignored)
  with no timeouts (16-bit, 44.1 kHz stereo), from ``--jump <s>``.

SID has no endless play: sidplayfp refuses an unbounded record ("-t0 invalid
in record mode"), its tunes state no loop point, and HVSC's length is the
reference — a SID repeats from its start.
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# (rate, channels, bits, float) of each family's PCM.
FORMATS = {
    "tracker": (48000, 2, 32, True),
    "gme": (44100, 2, 16, False),
    "uade": (44100, 2, 16, False),
}
_CHUNK = 64 * 1024


def frame_bytes(family: str) -> int:
    rate, ch, bits, _f = FORMATS[family]
    return ch * bits // 8


def tracker_seek(start_s: float, plan: "tuple[float, float] | None") -> float:
    """Where libopenmpt starts a stream that is ``start_s`` into the endless
    play: libopenmpt seeks within one pass of the song, so a time past its
    end (``plan`` = ``openmpt_vu.loop_plan``: one pass, the restart time) is
    that far into the loop it repeats.  Without a plan, ``start_s``.  Pure."""
    if not plan or start_s <= 0:
        return max(0.0, start_s)
    once, restart = plan
    loop = once - restart
    if start_s < once or loop <= 0:
        return start_s
    return restart + (start_s - once) % loop


def tracker_cmd(binary: str, path: Path, subsong: int, start_s: float,
                ctl_args: "list[str]") -> "list[str]":
    """openmpt123 argv for an endless tracker stream from ``start_s``."""
    cmd = [binary, "--batch", "--quiet", "--float", "--samplerate", "48000",
           "--repeat", "-1", *ctl_args]
    if start_s > 0:
        cmd += ["--seek", f"{start_s:.3f}"]
    if subsong > 0:
        cmd += ["--subsong", str(subsong)]
    return cmd + ["--stdout", "--", str(path)]


def uade_cmd(binary: str, path: Path, player_args: "list[str]",
             subsong_args: "list[str]", start_s: float,
             stereo_args: "tuple[str, ...]") -> "list[str]":
    """uade123 argv for an endless Amiga stream from ``start_s`` (run in the
    module's folder with its bare name, like the cached render)."""
    cmd = [binary, "-1", "--filter=A1200", *stereo_args, "-n",
           "--subsong-timeout=-1", "--timeout=-1", "-c", *player_args, *subsong_args]
    if start_s > 0:
        cmd += ["--jump", f"{start_s:.3f}"]
    return cmd + ["--", path.name]


async def pipe_pcm(cmd: "list[str]", *, cwd: "str | None" = None, skip_wav_header: bool):
    """The PCM a renderer writes to stdout, as it writes it — read only as
    fast as the caller takes it (the pipe throttles the renderer).  A
    renderer that writes a WAV header first (uade ``-c``) has it dropped
    (``skip_wav_header``).  The process is killed when the caller stops."""
    from soniqboom.core import forksafe
    proc = await forksafe.spawn_pipe_stdout(*cmd, stderr=asyncio.subprocess.DEVNULL, cwd=cwd)
    try:
        head = b""
        header_done = not skip_wav_header
        while True:
            chunk = await proc.stdout.read(_CHUNK)
            if not chunk:
                return
            if not header_done:
                head += chunk
                i = head.find(b"data")
                if i < 0 or len(head) < i + 8:
                    if len(head) > 4096:
                        return
                    continue
                chunk = head[i + 8:]
                header_done = True
                if not chunk:
                    continue
            yield chunk
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
        forksafe.close_stdout(proc)


def _gme_endless(data: bytes, subsong: int, start_s: float, put, stop: threading.Event) -> None:
    """Worker thread: libgme plays ``subsong`` of ``data`` from ``start_s``
    for as long as ``put(chunk)`` takes its blocks (False / ``stop``: done).
    No fade, no length; silence ends nothing; an ended track starts again."""
    from soniqboom.core import gme_render as g
    lib = g._lib
    if lib is None:
        return
    emu = ctypes.c_void_p()
    if lib.gme_open_data(data, len(data), ctypes.byref(emu), g._SR) or not emu:
        return
    try:
        try:
            lib.gme_ignore_silence.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.gme_ignore_silence(emu, 1)
        except AttributeError:
            pass
        try:
            lib.gme_set_autoload_playback_limit.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.gme_set_autoload_playback_limit(emu, 0)
        except AttributeError:
            pass
        track = max(0, int(subsong or 0))
        if lib.gme_start_track(emu, track):
            return
        if start_s > 0:
            lib.gme_seek.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.gme_seek.restype = ctypes.c_char_p
            lib.gme_seek(emu, int(start_s * 1000))
        n_shorts = 8192
        buf = (ctypes.c_short * n_shorts)()
        while not stop.is_set():
            if lib.gme_track_ended(emu):
                if lib.gme_start_track(emu, track):          # (the whole tune again)
                    return
            if lib.gme_play(emu, n_shorts, buf):
                return
            chunk = bytes(buf)
            if sys.byteorder == "big":
                import array
                a = array.array("h")
                a.frombytes(chunk)
                a.byteswap()
                chunk = a.tobytes()
            if put(chunk) is False:
                return
    except Exception:                                   # noqa: BLE001
        log.debug("endless libgme stream failed", exc_info=True)
    finally:
        with contextlib.suppress(Exception):
            lib.gme_delete(emu)


async def gme_pcm(data: bytes, subsong: int, start_s: float):
    """libgme's endless PCM (``_gme_endless``) as an async iterator; the
    worker waits on a small queue, so it renders only as fast as the caller
    reads (at most a few blocks ahead)."""
    loop = asyncio.get_running_loop()
    q: "asyncio.Queue[bytes | None]" = asyncio.Queue(maxsize=8)
    stop = threading.Event()

    def _put(chunk: bytes) -> bool:
        if stop.is_set():
            return False
        fut = asyncio.run_coroutine_threadsafe(q.put(chunk), loop)
        while not stop.is_set():
            try:
                fut.result(timeout=0.5)
                return True
            except TimeoutError:
                continue
            except Exception:                           # noqa: BLE001
                return False
        fut.cancel()
        return False

    def _run() -> None:
        try:
            _gme_endless(data, subsong, start_s, _put, stop)
        finally:
            if not stop.is_set():
                with contextlib.suppress(Exception):
                    asyncio.run_coroutine_threadsafe(q.put(None), loop)

    th = threading.Thread(target=_run, name="gme-endless", daemon=True)
    th.start()
    try:
        while True:
            chunk = await q.get()
            if chunk is None:
                return
            yield chunk
    finally:
        stop.set()
        # (unblock a worker waiting on a full queue)
        while not q.empty():
            q.get_nowait()
