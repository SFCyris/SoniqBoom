"""uade players SoniqBoom adds to the installed ones.

* ``bundled(name)``: a player that ships with SoniqBoom, in
  ``soniqboom/native/uade-players/`` (its README says what each plays).
* ``forgotten_worlds()``: uade's Forgotten Worlds player
  (``ForgottenWorlds_Game``, FWMP V1.1) with one instruction of its init
  routine replaced: at file offset 0x40A, ``move.w #$000F,$96(a1)`` (A1 then
  points at $DFF0E0, so the write lands on $DFF176) becomes
  ``move.w #$800F,-$4A(a1)``, a write to DMACON ($DFF096) that switches the
  four audio channels' DMA on.  The corrected copy is made from the installed
  player and written once into the data folder, only when the installed file
  is the stock uade 3.05 one (its SHA-256 and the six bytes at 0x40A are
  checked); with any other installed file there is no corrected player.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# ── players bundled with SoniqBoom ───────────────────────────────────────────


def _bundled_dirs() -> "list[Path]":
    """Where the bundled players are: ``<package>/native/uade-players`` (a
    source install, the Docker image), else beside a frozen app's
    executable."""
    here = Path(__file__).resolve().parent.parent / "native" / "uade-players"
    exe_dir = Path(sys.executable).resolve().parent
    return [here, exe_dir / "native" / "uade-players",
            exe_dir.parent / "Resources" / "native" / "uade-players"]


def bundled(name: str) -> "Path | None":
    """The bundled uade player file ``name`` (``SUNtronic2``), or None when
    it isn't there."""
    for d in _bundled_dirs():
        p = d / name
        if p.is_file():
            return p
    return None


# ── the corrected Forgotten Worlds player ───────────────────────────────────

FW_PLAYER = "ForgottenWorlds_Game"
FW_STOCK_SHA256 = "57b1fcb6bdc97761cf50b5dde271dfdf5ce37716f45aff6dd8e908e0e28b9bef"
FW_PATCHED_SHA256 = "acf7041bc61368b6fec823eb6cf0922c8fb20153ef6478ec0821b462eec37ceb"
FW_PATCH_AT = 0x40A
FW_STOCK_BYTES = bytes.fromhex("337c000f0096")     # move.w #$000F,$96(a1)
FW_PATCH_BYTES = bytes.fromhex("337c800fffb6")     # move.w #$800F,-$4A(a1)

_fw_lock = threading.Lock()
_fw_memo: dict = {}


def _players_dir() -> "Path | None":
    from soniqboom.core import uade_formats
    conf = uade_formats.conf_path()
    return conf.parent / "players" if conf else None


def fw_patched_bytes(stock: bytes) -> "bytes | None":
    """``stock`` with the Forgotten Worlds correction applied, when ``stock``
    is the uade 3.05 ``ForgottenWorlds_Game`` file (SHA-256 and the bytes at
    0x40A); else None."""
    if hashlib.sha256(stock).hexdigest() != FW_STOCK_SHA256:
        return None
    end = FW_PATCH_AT + len(FW_STOCK_BYTES)
    if stock[FW_PATCH_AT:end] != FW_STOCK_BYTES:
        return None
    out = stock[:FW_PATCH_AT] + FW_PATCH_BYTES + stock[end:]
    return out if hashlib.sha256(out).hexdigest() == FW_PATCHED_SHA256 else None


def _write_atomic(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fw_dest() -> Path:
    from soniqboom.config import get_data_dir
    return get_data_dir() / "uade-players" / FW_PLAYER


def forgotten_worlds() -> "Path | None":
    """The corrected Forgotten Worlds player in the data folder — written
    there (atomically) from the installed stock player the first time, and
    rewritten when the copy there isn't the corrected file — or None when
    the installed player is missing or isn't the stock uade 3.05 file, or
    the copy can't be written.  Remembered for the installed file's size and
    time, and the copy's (a copy changed since is checked again)."""
    players = _players_dir()
    stock_path = players / FW_PLAYER if players else None
    try:
        st = stock_path.stat() if stock_path else None
    except OSError:
        st = None
    if st is None:
        return None
    key = (str(stock_path), st.st_size, st.st_mtime_ns)
    with _fw_lock:
        if key in _fw_memo:
            hit, seen = _fw_memo[key]
            if hit is None or (seen is not None and _stamp(hit) == seen):
                return hit
        result = None
        try:
            patched = fw_patched_bytes(stock_path.read_bytes())
            if patched is None:
                log.info("uade's %s is not the stock 3.05 file: it plays unchanged",
                         stock_path)
            else:
                dest = _fw_dest()
                try:
                    have = dest.read_bytes()
                except OSError:
                    have = b""
                if have != patched:
                    _write_atomic(dest, patched)
                result = dest
        except OSError as exc:
            log.warning("Corrected Forgotten Worlds player unavailable: %s", exc)
            result = None
        _fw_memo.clear()
        _fw_memo[key] = (result, _stamp(result) if result is not None else None)
        return result


def _stamp(p: Path) -> "tuple[int, int] | None":
    """``p``'s size and modification time, or None when it is gone."""
    try:
        st = p.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns
