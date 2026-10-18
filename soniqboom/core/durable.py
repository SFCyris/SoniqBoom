# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Crash-safe small-file writes.

``atomic_write_bytes(path, data)`` replaces ``path`` so that after a crash or
power loss at any point the file holds either its old or its new contents —
never a torn mix, never an empty file:

  1. the bytes go to a unique temp file in the SAME directory (created with
     the final mode, so no umask window exposes it);
  2. the temp file is flushed to stable storage (``full_fsync``);
  3. ``os.replace`` swaps it in (atomic on POSIX);
  4. the directory is flushed the same way, so the rename itself survives a
     power loss.

A failure at any step removes the temp file and raises; the old file is left
as it was.  ``fsync_dir`` is the directory step on its own.

``full_fsync``: on macOS ``fsync`` only hands the data to the drive, whose
write cache may still lose it on a power cut; ``fcntl(F_FULLFSYNC)`` also
flushes that cache.  It is used there, falling back to ``os.fsync`` when the
file system refuses it.  Elsewhere it is ``os.fsync``.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:                     # pragma: no cover - not POSIX
    fcntl = None                        # type: ignore[assignment]


def full_fsync(fd: int) -> None:
    """Flush ``fd`` to stable storage: ``F_FULLFSYNC`` on macOS (falling back
    to ``os.fsync`` if it is unavailable or fails), ``os.fsync`` elsewhere.
    Raises what ``os.fsync`` raises."""
    if sys.platform == "darwin" and fcntl is not None:
        op = getattr(fcntl, "F_FULLFSYNC", None)
        if op is not None:
            try:
                fcntl.fcntl(fd, op)
                return
            except OSError:
                pass
    os.fsync(fd)


def fsync_dir(path: Path) -> None:
    """Flush directory ``path`` (``full_fsync``; best-effort: a file system
    that can't flush a directory — some network shares — is not an error)."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        full_fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Durably replace ``path`` with ``data`` (see the module docstring).
    The file ends up with permission bits ``mode``; its directory must
    exist."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("short write")
                view = view[n:]
            try:
                os.fchmod(fd, mode)            # O_CREAT's mode is masked by the umask
            except OSError:
                pass
            full_fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    fsync_dir(path.parent)
