# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The folder a running server's renders and transcodes write their temp
files in.

Each process image gets its own folder in the system temp folder:
``soniqboom-render-<host>-<pid>-<token>`` (``host``: 8 hex digits of the host
name — containers sharing a temp folder have the same pids but not the same
host name; ``token``: 8 hex digits drawn when the image starts, so the image
an in-place restart (``os.execv``) starts — same pid — has another one).  The stop empties and removes it once the renderers
are stopped (``clear_own``); the start removes the folders of images that are
gone (``sweep_orphans``): on this host, another pid that no longer runs, or
this pid's previous image.  Nothing outside such a folder is ever removed; temp WAVs of
unknown origin directly in the temp folder (``tmp*.wav``) are only counted
(``stray_temp_wavs``).  Before that, the start ends the renderers such an
image left running (``kill_orphan_renderers``: a server killed outright —
its children end with it, ``forksafe``, this is the safety net).
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "soniqboom-render-"
_NAME_RE = re.compile(r"^soniqboom-render-([0-9a-f]{8})-(\d+)-([0-9a-f]{8})$")
_TOKEN = secrets.token_hex(4)


def _host() -> str:
    import hashlib
    import socket
    try:
        h = socket.gethostname()
    except OSError:
        h = ""
    return hashlib.sha1(h.encode("utf-8", "replace")).hexdigest()[:8]


_HOST = _host()
_lock = threading.Lock()
_made: "Path | None" = None


def name() -> str:
    return f"{PREFIX}{_HOST}-{os.getpid()}-{_TOKEN}"


def path() -> Path:
    """This image's render temp folder, created on first use (owner-only).
    Falls back to the temp folder itself when it can't be made."""
    global _made
    base = Path(tempfile.gettempdir())
    d = base / name()
    if _made == d and d.is_dir():
        return d
    with _lock:
        try:
            d.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            log.warning("Render temp folder %s could not be made — using %s", d, base,
                        exc_info=True)
            return base
        _made = d
        global _atexit_set
        if not _atexit_set:
            # (an interpreter that exits normally — a tool, the test suite —
            # leaves no folder; the server's stop has removed it already)
            import atexit
            atexit.register(_clear_at_exit)
            _atexit_set = True
    return d


_atexit_set = False


def _clear_at_exit() -> None:
    try:
        clear_own()
    except Exception:                   # noqa: BLE001 — interpreter shutdown
        pass


def tmp_dir() -> str:
    """``path()`` as a string — the ``dir=`` of a ``tempfile`` call."""
    return str(path())


def _rm_tree(d: Path) -> "tuple[int, int]":
    """Remove folder ``d`` (never following a link); returns ``(files,
    bytes)`` removed."""
    n = size = 0
    for p in d.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                size += p.stat().st_size
                n += 1
        except OSError:
            pass
    shutil.rmtree(d, ignore_errors=True)
    return n, size


def clear_own() -> "tuple[int, int]":
    """At the stop, once no renderer runs any more: remove this image's
    folder and what is in it.  Returns ``(files, bytes)``."""
    global _made
    d = Path(tempfile.gettempdir()) / name()
    with _lock:
        _made = None
        if not d.is_dir() or d.is_symlink():
            return 0, 0
        return _rm_tree(d)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                     # another user's process
    except OSError:
        return True
    return True


def sweep_orphans() -> "tuple[int, int, int]":
    """At the start: remove the render temp folders of server images of this
    host that are gone — this pid's previous image (an in-place restart) and
    pids that no longer run.  A folder of a process that still runs (another
    server on this machine) or of another host is left alone.  Returns ``(folders, files, bytes)``."""
    base = Path(tempfile.gettempdir())
    me = os.getpid()
    folders = files = size = 0
    try:
        candidates = [p for p in base.iterdir() if p.name.startswith(PREFIX)]
    except OSError:
        return 0, 0, 0
    for d in candidates:
        m = _NAME_RE.match(d.name)
        if m is None or d.is_symlink() or not d.is_dir():
            continue
        host, pid, token = m.group(1), int(m.group(2)), m.group(3)
        if host != _HOST:
            continue                    # another host's (a container sharing /tmp)
        if pid == me:
            if token == _TOKEN:
                continue                # ours
        elif _pid_alive(pid):
            continue
        n, b = _rm_tree(d)
        folders += 1
        files += n
        size += b
    return folders, files, size


def stray_temp_wavs() -> "tuple[int, int]":
    """``(count, bytes)`` of ``tmp*.wav`` files directly in the temp folder —
    left by an older version's renders or by any other program: counted
    for the log, never removed."""
    base = Path(tempfile.gettempdir())
    n = size = 0
    try:
        for p in base.glob("tmp*.wav"):
            try:
                if p.is_file() and not p.is_symlink():
                    size += p.stat().st_size
                    n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n, size


# The programs ``api.stream`` renders and transcodes with (their file names).
RENDERER_NAMES = frozenset({
    "zxtune123", "uade123", "uadecore", "sidplayfp", "fluidsynth", "adplay",
    "psgplay", "sc68", "ym2wav", "hvl2wav", "openmpt123", "gme", "ffmpeg",
    "metaflac", "lha",
})
_IN_CMD_RE = re.compile(r"soniqboom-render-([0-9a-f]{8})-(\d+)-([0-9a-f]{8})(?=/)")


def _program_name(cmd: str) -> str:
    first = cmd.split(" ", 1)[0] if cmd else ""
    return os.path.basename(first)


def orphan_renderers(table: "dict | None" = None) -> "list[tuple[int, str]]":
    """``(pid, command line)`` of every renderer a server image of this host
    that is gone left running: a process whose program is a renderer
    (``RENDERER_NAMES``), whose command line names a file in that image's
    render temp folder (``<temp>/soniqboom-render-<host>-<pid>-<token>/…``:
    a pid that no longer runs, or this pid's previous image), and whose
    parent is init (it was re-parented when its server died) — or, for this
    pid's previous image, this process.  Nothing else is ever listed."""
    if table is None:
        from soniqboom.core.procinfo import process_table
        table = process_table()
    base = str(Path(tempfile.gettempdir()))
    me = os.getpid()
    out = []
    for pid, (ppid, state, cmd) in table.items():
        if pid == me or state.startswith("Z") or _program_name(cmd) not in RENDERER_NAMES:
            continue
        for m in _IN_CMD_RE.finditer(cmd):
            host, owner, token = m.group(1), int(m.group(2)), m.group(3)
            if host != _HOST or cmd[max(0, m.start() - len(base) - 1):m.start()] != base + "/":
                continue
            if owner == me:
                gone = token != _TOKEN and ppid in (1, me)
            else:
                gone = ppid == 1 and not _pid_alive(owner)
            if gone:
                out.append((pid, cmd))
                break
    return out


def kill_orphan_renderers() -> int:
    """At the start: SIGKILL the renderers ``orphan_renderers`` lists (a
    server killed outright left them writing into a folder that is about to
    be removed).  Returns how many were ended."""
    import signal
    n = 0
    for pid, cmd in orphan_renderers():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
        n += 1
        log.info("Ended renderer %d left running by a stopped server run: %s", pid, cmd[:200])
    return n
