# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pluggable filesystem abstraction for local and remote sources.

Each source provides a uniform interface for directory walking, file reading,
and stat operations.  The scanner, folder tree, and stream endpoints use this
abstraction so that SMB/FTP shares work without OS-level mounts.
"""
from __future__ import annotations

import ftplib
import io
import logging
import os
import socket
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterator

log = logging.getLogger(__name__)


# ── Retry / backoff tunables ────────────────────────────────────────────────
#
# A brief network blip (ARP cache miss, Wi-Fi roaming, macOS TCC prompt
# latency, transient router reboot) can EHOSTUNREACH a perfectly-healthy
# FTP/SMB share for 1–3 seconds.  Retrying the TCP handshake with short
# exponential backoff recovers automatically without any user action.
#
# Budget: _CONNECT_ATTEMPTS attempts with waits 0, BASE, BASE*2, BASE*4, …
# With ATTEMPTS=3 and BASE=0.5 the worst-case wait is ~1.5 s — under the
# 15 s connect timeout for the first attempt, so total op time stays bounded.
_CONNECT_ATTEMPTS = 3
_CONNECT_BACKOFF_BASE = 0.5


def _is_transient_network_error(exc: BaseException) -> bool:
    """True if *exc* looks like a retry-worthy network blip.

    Excludes auth / permission errors (retrying will never succeed) and
    anything that clearly indicates a misconfigured share.
    """
    # ftplib.error_perm = "530 Login incorrect" / "550 permission denied" —
    # never transient, do NOT retry.
    if isinstance(exc, ftplib.error_perm):
        return False
    # Socket-level errors, FTP protocol temp errors, timeouts — all retry-able.
    if isinstance(exc, (OSError, socket.timeout, ftplib.error_temp,
                        ftplib.error_reply, ftplib.error_proto,
                        EOFError, TimeoutError)):
        return True
    return False


@dataclass
class FileStat:
    size: int = 0
    mtime: float = 0.0
    is_dir: bool = False


@dataclass
class DirEntry:
    name: str
    path: str
    is_dir: bool = False
    size: int = 0
    mtime: float = 0.0


class FileSource(ABC):
    """Abstract filesystem source."""

    @abstractmethod
    def walk(self, top: str) -> Iterator[tuple[str, list[str], list[str]]]:
        """Yield (dirpath, dirnames, filenames) like os.walk."""

    @abstractmethod
    def list_dir(self, path: str) -> list[DirEntry]:
        """List entries in a directory."""

    @abstractmethod
    def read_file(self, path: str) -> bytes:
        """Read entire file contents."""

    @abstractmethod
    def stat(self, path: str) -> FileStat:
        """Get file/directory metadata."""

    @abstractmethod
    def is_dir(self, path: str) -> bool:
        """Check if path is an accessible directory."""

    def close(self) -> None:
        """Release any held connections."""

    def reconnect(self) -> bool:
        """Force-rebuild the connection.  Returns True on success.

        Local sources have nothing to reconnect — they return True always.
        Network sources override this with real logic.
        """
        return True


# ── Local filesystem ────────────────────────────────────────────────────────


class LocalFileSource(FileSource):
    """Delegates to stdlib os/pathlib — the existing behavior."""

    def walk(self, top: str) -> Iterator[tuple[str, list[str], list[str]]]:
        yield from os.walk(top)

    def list_dir(self, path: str) -> list[DirEntry]:
        entries: list[DirEntry] = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat()
                    entries.append(DirEntry(
                        name=e.name, path=e.path,
                        is_dir=e.is_dir(), size=st.st_size, mtime=st.st_mtime,
                    ))
                except OSError:
                    continue
        return entries

    def read_file(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def stat(self, path: str) -> FileStat:
        st = os.stat(path)
        return FileStat(size=st.st_size, mtime=st.st_mtime, is_dir=os.path.isdir(path))

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)


# ── SMB ─────────────────────────────────────────────────────────────────────


class SMBFileSource(FileSource):
    """Direct SMB access via smbprotocol — no OS mount required."""

    def __init__(self, host: str, share: str, username: str = "",
                 password: str = "", port: int = 445):
        self._host = host
        self._share = share
        self._port = port
        self._username = username
        self._password = password
        self._registered = False

    def _ensure_registered(self) -> None:
        if self._registered:
            return
        import smbclient

        def _do_register() -> None:
            if self._username or self._password:
                smbclient.register_session(
                    self._host, username=self._username, password=self._password,
                    port=self._port,
                )
            else:
                # Guest / anonymous: try "Guest" first, fall back to empty creds
                try:
                    smbclient.register_session(
                        self._host, username="Guest", password="",
                        port=self._port,
                    )
                except Exception:
                    smbclient.register_session(
                        self._host, username="", password="",
                        port=self._port,
                    )

        last_exc: BaseException | None = None
        for attempt in range(_CONNECT_ATTEMPTS):
            try:
                _do_register()
                self._registered = True
                if attempt > 0:
                    log.info("SMB reconnected to //%s/%s after %d retries",
                             self._host, self._share, attempt)
                return
            except BaseException as exc:
                last_exc = exc
                # Can't cheaply distinguish auth vs network for SMB — smbprotocol
                # raises opaque exceptions.  Treat OSError/socket errors/timeouts
                # as transient and retry; let anything else propagate immediately.
                msg = str(exc).lower()
                transient = (
                    isinstance(exc, (OSError, socket.timeout, TimeoutError))
                    or "timed out" in msg
                    or "no route" in msg
                    or "connection refused" in msg
                    or "connection reset" in msg
                )
                if not transient:
                    raise
                if attempt < _CONNECT_ATTEMPTS - 1:
                    wait = _CONNECT_BACKOFF_BASE * (2 ** attempt)
                    log.info(
                        "SMB register to //%s/%s failed (attempt %d/%d): "
                        "%s: %s — retrying in %.1fs",
                        self._host, self._share, attempt + 1,
                        _CONNECT_ATTEMPTS, type(exc).__name__, exc, wait,
                    )
                    time.sleep(wait)
        assert last_exc is not None
        raise last_exc

    def reconnect(self) -> bool:
        """Force-rebuild the SMB session.  Returns True on success."""
        self.close()  # closes + clears _registered
        try:
            self._ensure_registered()
            return True
        except Exception as exc:
            log.info("SMB reconnect to //%s/%s failed: %s: %s",
                     self._host, self._share, type(exc).__name__, exc)
            return False

    def _smb_path(self, path: str) -> str:
        rel = path.lstrip("/")
        return f"\\\\{self._host}\\{self._share}\\{rel}".replace("/", "\\")

    def _to_posix(self, smb_path: str) -> str:
        return "/" + smb_path.split("\\", 3)[-1].replace("\\", "/") if "\\" in smb_path else smb_path

    def walk(self, top: str) -> Iterator[tuple[str, list[str], list[str]]]:
        import smbclient
        self._ensure_registered()
        smb_top = self._smb_path(top)
        for dirpath, dirnames, filenames in smbclient.walk(smb_top):
            yield self._to_posix(dirpath), dirnames, filenames

    def list_dir(self, path: str) -> list[DirEntry]:
        import smbclient
        self._ensure_registered()
        smb_path = self._smb_path(path)
        entries: list[DirEntry] = []
        with smbclient.scandir(smb_path) as it:
            for e in it:
                try:
                    st = e.stat()
                    entries.append(DirEntry(
                        name=e.name,
                        path=self._to_posix(e.path),
                        is_dir=e.is_dir(),
                        size=st.st_size,
                        mtime=st.st_mtime,
                    ))
                except OSError:
                    continue
        return entries

    def read_file(self, path: str) -> bytes:
        import smbclient
        self._ensure_registered()
        with smbclient.open_file(self._smb_path(path), mode="rb") as f:
            return f.read()

    def stat(self, path: str) -> FileStat:
        import smbclient
        self._ensure_registered()
        st = smbclient.stat(self._smb_path(path))
        return FileStat(
            size=st.st_size, mtime=st.st_mtime,
            is_dir=smbclient.path.isdir(self._smb_path(path)),
        )

    def is_dir(self, path: str) -> bool:
        import smbclient
        try:
            self._ensure_registered()
            return smbclient.path.isdir(self._smb_path(path))
        except Exception as exc:
            # Log at WARNING so share-connection failures surface in the log.
            # Previously this silently returned False, so "Share ... root not
            # accessible" left no trail of *why* — was it auth, DNS, firewall,
            # a macOS Local Network privacy block?  Now we know.
            log.warning(
                "SMB is_dir(%s) on //%s/%s failed: %s: %s",
                path, self._host, self._share, type(exc).__name__, exc,
            )
            return False

    def close(self) -> None:
        if self._registered:
            try:
                from smbclient import delete_session
                delete_session(self._host, port=self._port)
            except Exception:
                pass
            self._registered = False


# ── FTP ─────────────────────────────────────────────────────────────────────


class FTPFileSource(FileSource):
    """Direct FTP access via stdlib ftplib."""

    def __init__(self, host: str, username: str = "", password: str = "",
                 port: int = 21, remote_path: str = "/"):
        self._host = host
        self._port = port
        self._username = username or "anonymous"
        self._password = password or ""
        self._remote_path = remote_path.rstrip("/") or "/"
        self._ftp: ftplib.FTP | None = None
        self._use_mlsd: bool = True  # try MLSD first, fall back to LIST
        self._xfer_count: int = 0    # downloads since last reconnect
        self._encoding: str = "utf-8"  # downgraded to latin-1 on decode errors

    def _connect(self) -> ftplib.FTP:
        if self._ftp is not None:
            try:
                self._ftp.voidcmd("NOOP")
                return self._ftp
            except Exception:
                # Any error (error_reply, error_perm, OSError, …) means the
                # connection is stale or desynchronised — reconnect.
                self._reset()

        # Retry with exponential backoff on transient network errors.  A
        # freshly-dropped connection (Wi-Fi roam, NAT state flush, macOS
        # Local Network TCC prompt in flight) often recovers in under 2 s.
        last_exc: BaseException | None = None
        for attempt in range(_CONNECT_ATTEMPTS):
            try:
                ftp = ftplib.FTP()
                ftp.encoding = self._encoding
                ftp.connect(self._host, self._port, timeout=15)
                ftp.login(self._username, self._password)
                # Enable UTF-8 mode on servers that support it (e.g. ProFTPD).
                # This is required for CWD/RETR on paths with multi-byte UTF-8
                # characters (Japanese, symbols like ☆♥µ).  Combined with
                # latin-1 client encoding the bytes round-trip correctly for
                # BOTH valid-UTF-8 and non-UTF-8 filenames.
                try:
                    ftp.sendcmd("OPTS UTF8 ON")
                except (ftplib.error_perm, ftplib.error_temp):
                    pass  # server doesn't support UTF-8 opts — harmless
                self._ftp = ftp
                if attempt > 0:
                    log.info("FTP reconnected to %s:%d after %d retries",
                             self._host, self._port, attempt)
                return ftp
            except BaseException as exc:
                last_exc = exc
                # Auth failures never recover with retries — fail fast.
                if not _is_transient_network_error(exc):
                    raise
                if attempt < _CONNECT_ATTEMPTS - 1:
                    wait = _CONNECT_BACKOFF_BASE * (2 ** attempt)
                    log.info(
                        "FTP connect to %s:%d failed (attempt %d/%d): "
                        "%s: %s — retrying in %.1fs",
                        self._host, self._port, attempt + 1,
                        _CONNECT_ATTEMPTS, type(exc).__name__, exc, wait,
                    )
                    time.sleep(wait)
        # Exhausted retries — propagate the last error.
        assert last_exc is not None
        raise last_exc

    def _reset(self) -> None:
        """Close the FTP connection so the next operation reconnects cleanly."""
        if self._ftp:
            try:
                self._ftp.close()
            except Exception:
                pass
        self._ftp = None

    def reconnect(self) -> bool:
        """Force-rebuild the FTP connection.  Returns True if successful.

        Intended for stream/health paths that want to retry after a failure
        without waiting for the next NOOP cycle.  Never raises — on failure
        the caller should fall back to its own error handling.
        """
        self._reset()
        try:
            self._connect()
            return True
        except Exception as exc:
            log.info("FTP reconnect to %s:%d failed: %s: %s",
                     self._host, self._port, type(exc).__name__, exc)
            return False

    def _abs(self, path: str) -> str:
        if path.startswith(self._remote_path):
            return path
        rel = path.lstrip("/")
        return f"{self._remote_path}/{rel}" if rel else self._remote_path

    # ── Directory listing with MLSD → LIST fallback ────────────────────────

    def _switch_encoding_latin1(self) -> None:
        """Switch to latin-1 encoding (can decode any byte) and reconnect."""
        if self._encoding != "latin-1":
            log.info("Non-UTF-8 filenames on %s, switching to latin-1 encoding",
                     self._host)
            self._encoding = "latin-1"
            self._reset()

    def _list_entries(self, path: str) -> list[DirEntry]:
        abs_path = self._abs(path)

        # Prefer MLSD (structured output, RFC 3659)
        if self._use_mlsd:
            try:
                return self._list_via_mlsd(abs_path)
            except UnicodeDecodeError:
                self._switch_encoding_latin1()
                try:
                    return self._list_via_mlsd(abs_path)
                except Exception as exc:
                    log.info("MLSD failed on %s (%s), using LIST fallback",
                             self._host, exc)
                    self._use_mlsd = False
                    self._reset()
            except Exception as exc:
                log.info("MLSD not supported on %s (%s), using LIST fallback",
                         self._host, exc)
                self._use_mlsd = False
                self._reset()

        # Fallback: LIST (universally supported)
        try:
            return self._list_via_list(abs_path)
        except UnicodeDecodeError:
            self._switch_encoding_latin1()
            try:
                return self._list_via_list(abs_path)
            except Exception as exc:
                log.warning("FTP LIST failed for %s: %s", abs_path, exc)
                self._reset()
                return []
        except Exception as exc:
            log.warning("FTP LIST failed for %s: %s", abs_path, exc)
            self._reset()
            return []

    def _list_via_mlsd(self, abs_path: str) -> list[DirEntry]:
        """List a directory via MLSD (RFC 3659 — structured output)."""
        ftp = self._connect()
        entries: list[DirEntry] = []
        for name, facts in ftp.mlsd(abs_path):
            if name in (".", ".."):
                continue
            is_d = facts.get("type", "").lower() in ("dir", "cdir", "pdir")
            sz = int(facts.get("size", "0")) if facts.get("size") else 0
            mtime = self._parse_mtime(facts.get("modify", ""))
            entry_path = f"{abs_path}/{name}" if abs_path != "/" else f"/{name}"
            entries.append(DirEntry(name=name, path=entry_path,
                                    is_dir=is_d, size=sz, mtime=mtime))
        return entries

    def _list_via_list(self, abs_path: str) -> list[DirEntry]:
        """List a directory via LIST (universal fallback)."""
        ftp = self._connect()
        ftp.cwd(abs_path)
        lines: list[str] = []
        ftp.retrlines("LIST", lines.append)
        entries: list[DirEntry] = []
        for line in lines:
            entry = self._parse_list_line(line, abs_path)
            if entry:
                entries.append(entry)
        return entries

    @staticmethod
    def _parse_list_line(line: str, parent: str) -> DirEntry | None:
        """Parse a single LIST output line (Unix or Windows format).

        Unix:    drwxr-xr-x  2 user group  4096 Jan 01 12:00 filename
        Windows: 01-01-26  12:00PM       <DIR>  dirname
        """
        if not line or line.startswith("total "):
            return None

        # Unix-style (most common)
        parts = line.split(None, 8)
        if len(parts) >= 9 and len(parts[0]) >= 10 and parts[0][0] in "dlbcps-":
            perms = parts[0]
            is_dir = perms[0] in ("d", "l")
            name = parts[8]
            if name in (".", ".."):
                return None
            try:
                size = int(parts[4])
            except ValueError:
                size = 0
            entry_path = f"{parent}/{name}" if parent != "/" else f"/{name}"
            return DirEntry(name=name, path=entry_path, is_dir=is_dir,
                            size=size, mtime=0.0)

        # Windows-style: 01-01-26  12:00PM  <DIR>  dirname
        wparts = line.split(None, 3)
        if len(wparts) >= 4:
            is_dir = "<DIR>" in wparts[2].upper()
            name = wparts[3]
            if name in (".", ".."):
                return None
            size = 0
            if not is_dir:
                try:
                    size = int(wparts[2])
                except ValueError:
                    pass
            entry_path = f"{parent}/{name}" if parent != "/" else f"/{name}"
            return DirEntry(name=name, path=entry_path, is_dir=is_dir,
                            size=size, mtime=0.0)

        return None

    @staticmethod
    def _parse_mtime(val: str) -> float:
        if not val or len(val) < 14:
            return 0.0
        try:
            import datetime
            dt = datetime.datetime.strptime(val[:14], "%Y%m%d%H%M%S")
            return dt.timestamp()
        except (ValueError, OSError):
            return 0.0

    def _rel(self, abs_path: str) -> str:
        """Strip ``_remote_path`` prefix so yielded paths are root-relative."""
        if self._remote_path != "/" and abs_path.startswith(self._remote_path):
            sub = abs_path[len(self._remote_path):]
            return sub or "/"
        return abs_path

    def walk(self, top: str) -> Iterator[tuple[str, list[str], list[str]]]:
        abs_top = self._abs(top)
        stack = [abs_top]
        while stack:
            current = stack.pop()
            entries = self._list_entries(current)
            dirs = [e.name for e in entries if e.is_dir]
            files = [e.name for e in entries if not e.is_dir]
            yield self._rel(current), dirs, files
            for d in dirs:
                child = f"{current}/{d}" if current != "/" else f"/{d}"
                stack.append(child)

    def list_dir(self, path: str) -> list[DirEntry]:
        return self._list_entries(path)

    def read_file(self, path: str) -> bytes:
        # ftplib sends TYPE I before each retrbinary.  After many rapid
        # downloads the control-channel responses can desynchronise, causing
        # "200 Type set to I" errors.  Periodically reconnect and retry on
        # any FTP error to stay resilient.
        _MAX_PER_CONN = 40  # fresh TCP connection every N downloads
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                if self._xfer_count >= _MAX_PER_CONN:
                    self._reset()
                    self._xfer_count = 0
                ftp = self._connect()
                buf = io.BytesIO()
                ftp.retrbinary(f"RETR {self._abs(path)}", buf.write)
                self._xfer_count += 1
                return buf.getvalue()
            except Exception as exc:
                last_exc = exc
                self._reset()
                self._xfer_count = 0
                if attempt < 2:
                    time.sleep(0.3)     # brief pause before retry
        raise last_exc  # type: ignore[misc]

    def stat(self, path: str) -> FileStat:
        try:
            ftp = self._connect()
            abs_path = self._abs(path)
            try:
                size = ftp.size(abs_path)
                return FileStat(size=size or 0, mtime=0.0, is_dir=False)
            except ftplib.error_perm:
                ftp.cwd(abs_path)
                return FileStat(size=0, mtime=0.0, is_dir=True)
        except Exception:
            self._reset()
            return FileStat()

    def is_dir(self, path: str) -> bool:
        try:
            ftp = self._connect()
            ftp.cwd(self._abs(path))
            return True
        except Exception as exc:
            self._reset()
            # Log at WARNING so connection failures surface in the log.
            # OSError / socket.gaierror / EHOSTUNREACH commonly means macOS
            # has silently blocked the connection because the bundle lacks
            # NSLocalNetworkUsageDescription.  ftplib.error_perm means auth
            # failed.  error_reply / error_temp mean the server misbehaved.
            log.warning(
                "FTP is_dir(%s) on %s:%d failed: %s: %s",
                path, self._host, self._port, type(exc).__name__, exc,
            )
            return False

    def close(self) -> None:
        if self._ftp:
            try:
                self._ftp.quit()
            except Exception:
                try:
                    self._ftp.close()
                except Exception:
                    pass
            self._ftp = None


# ── Factory ─────────────────────────────────────────────────────────────────

_active_sources: dict[str, FileSource] = {}


def create_source(share: dict, password: str = "") -> FileSource:
    proto = share.get("protocol", "").lower()
    if proto == "smb":
        return SMBFileSource(
            host=share["host"], share=share["share"],
            username=share.get("username", ""), password=password,
            port=share.get("port") or 445,
        )
    if proto == "ftp":
        return FTPFileSource(
            host=share["host"], username=share.get("username", ""),
            password=password, port=share.get("port") or 21,
            remote_path=share.get("remote_path", "/"),
        )
    raise ValueError(f"Unsupported protocol: {proto}")


def register_source(share_id: str, source: FileSource) -> None:
    old = _active_sources.get(share_id)
    if old is not None:
        old.close()
    _active_sources[share_id] = source


def get_source(share_id: str) -> FileSource | None:
    return _active_sources.get(share_id)


def remove_source(share_id: str) -> None:
    src = _active_sources.pop(share_id, None)
    if src is not None:
        src.close()


def all_sources() -> dict[str, FileSource]:
    return dict(_active_sources)


def find_source_for_path(path: str) -> tuple[str, str, FileSource] | None:
    """Find the source whose scan_root is a prefix of *path*.

    Returns ``(scan_root, remote_subpath, source)`` or ``None``.
    """
    for scan_root, source in _active_sources.items():
        # Normalise trailing slashes so "ftp://host/dir/" matches
        # "ftp://host/dir/subdir" without producing a double-slash.
        root = scan_root.rstrip("/")
        if path == scan_root or path.rstrip("/") == root or path.startswith(root + "/"):
            subpath = path[len(root):] or "/"
            return scan_root, subpath, source
    return None
