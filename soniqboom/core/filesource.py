# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pluggable filesystem abstraction for local and remote sources.

Each source provides a uniform interface for directory walking, file reading,
and stat operations.  The scanner, folder tree, and stream endpoints use this
abstraction so that SMB/FTP shares work without OS-level mounts.
"""
from __future__ import annotations

import atexit
import ftplib
import io
import logging
import os
import socket
import threading
import time
import weakref
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

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


# Every scheme a network-share scan root / track path starts with — THE list.
# WebDAV scan roots are the share's http(s):// base URL.  Callers test paths
# with ``is_remote_path`` rather than carrying their own copy: a narrower copy
# (smb/ftp only) sent WebDAV roots down the local-filesystem branch, which
# answered every browse with 404 (GitHub #18).
REMOTE_SCHEMES: tuple[str, ...] = (
    "smb://", "ftp://", "http://", "https://", "webdav://", "webdavs://",
)


def is_remote_path(path) -> bool:
    """True for a network-share scan root or track path (SMB / FTP / WebDAV),
    including a composite remote-archive member (``…/a.zip::x.mod``).  The
    scheme is case-insensitive (``Https://…`` is a URL, not a local path)."""
    if not path:
        return False
    s = str(path)
    return s.startswith(REMOTE_SCHEMES) or s[:10].lower().startswith(REMOTE_SCHEMES)


def scan_root_for_share(share: dict) -> str:
    """The scan-root URL a configured network share is registered under —
    the key of ``scan_dirs``, the source registry and every track path
    (``<scan_root>:<path>``).  WebDAV shares use the base URL exactly as
    entered."""
    proto = (share.get("protocol") or "").lower()
    host = share.get("host", "")
    if proto == "smb":
        return f"smb://{host}/{share['share']}"
    if proto == "ftp":
        return f"ftp://{host}{share.get('remote_path', '/')}"
    if proto in ("webdav", "webdavs", "http", "https"):
        return share.get("base_url") or (
            f"{'https' if proto in ('webdavs', 'https') else 'http'}://"
            f"{host}{share.get('remote_path', '/')}"
        )
    raise ValueError(f"Unknown protocol: {proto}")


_AUTH_KEY_SALT = os.urandom(16)     # per process: the fingerprint never leaves memory


def share_auth_key(share: dict, password: str | None = None) -> str:
    """``scheme://user@host:port#<fingerprint>`` — the shares that log in to
    the same server with the same credentials (a refused login refuses them
    all; a share with another password is not held back by it).  The
    fingerprint is a salted HMAC of the password, never the password; ``password``
    defaults to the share's stored (encrypted) one."""
    import hashlib
    import hmac
    try:
        proto, host, port = share_endpoint(share)
    except Exception:
        proto, host, port = str(share.get("protocol", "")), str(share.get("host", "")), 0
    user = share.get("username") or ""
    if password is None:
        password = ""
        if share.get("password_enc"):
            try:
                from soniqboom.core.credentials import decrypt
                password = decrypt(share["password_enc"]) or ""
            except Exception:
                password = ""
    fp = hmac.new(_AUTH_KEY_SALT, f"{user}\0{password}".encode("utf-8", "surrogatepass"),
                  hashlib.sha256).hexdigest()[:16]
    return f"{proto}://{user}@{host}:{port}#{fp}"


def share_endpoint(share: dict) -> tuple[str, str, int]:
    """``(protocol, host, port)`` of the server a configured share lives on —
    shares with the same endpoint share its reachability."""
    proto = (share.get("protocol") or "").lower()
    if proto in ("webdav", "webdavs", "http", "https"):
        from urllib.parse import urlsplit
        url = scan_root_for_share(share)
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            port = None
        return (parts.scheme, (parts.hostname or "").lower(),
                port or (443 if parts.scheme == "https" else 80))
    default = 445 if proto == "smb" else 21
    try:
        port = int(share.get("port") or default)
    except (TypeError, ValueError):
        port = default
    return proto, str(share.get("host", "")).lower(), port

# Dedup so a legacy track being hit repeatedly (album view fans out to art,
# stream, lyrics endpoints) only logs once per unique URL.
_LEGACY_URL_LOGGED: set[str] = set()


def _sanitise_url_for_log(url: str) -> str:
    """Strip ``user:pass@`` userinfo before a URL goes to the log file.

    Prevents stored share credentials from ending up in plain-text logs
    every time a legacy URL is parsed.
    """
    from urllib.parse import urlsplit, urlunsplit
    try:
        p = urlsplit(url)
        netloc = p.hostname or ""
        if p.port:
            netloc = f"{netloc}:{p.port}"
        return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
    except Exception:
        return url


def parse_remote_path(path_str: str) -> tuple[str, str]:
    """Split a remote URL like ``smb://host/share:/relative`` into
    ``(scan_root, remote_path)``.

    The earlier in-line ``path_str.index(":", 6)`` mishandled URLs with
    userinfo, ports, missing path components — and a regression sweep
    flagged that the first parse-helper rewrite still mis-routed track
    filenames containing ``@`` and ``host:port`` URLs.  This version
    delegates to ``urllib.parse.urlsplit`` for the scheme / netloc / path
    split, then splits the path component on the first ``:`` to recover
    the share / remote-path boundary.

    **Critical** for filenames containing ``#`` or ``?``: these are URL
    metacharacters, so ``urlsplit("ftp://h/share:/foo (sm#2).flac")``
    returns ``path="/share:/foo (sm"`` with the ``#2).flac`` tail
    sitting in ``parts.fragment``.  Naïve callers (the FTP/SMB fetch
    path) then ask the server for the truncated filename and get a
    bogus 550 / ENOENT.  We re-attach the literal ``#fragment`` and
    ``?query`` to the remote-path tail so the file actually round-trips
    — these characters are valid in real filenames (Sawano's
    "sm2_Final#2", "Track ?", chiptune "what's up?.mod", etc.) and the
    FTP/SMB layers treat them as opaque bytes, not URL syntax.

    Returns ``(path_str, "")`` if there's no share-vs-path separator at
    all so callers can detect "URL points at the share root" without
    catching ValueError.
    """
    from urllib.parse import urlsplit

    if not is_remote_path(path_str):
        raise ValueError(f"Not a remote URL: {path_str!r}")

    # A registered scan root followed by ``:/`` (or ``:`` at the very end)
    # is the exact share / path boundary whatever the root looks like — a
    # WebDAV root without a path (``https://dav.example.com:/Artist/x.flac``)
    # or a ``:`` inside the share's own path.  The longest matching root wins.
    # A bare ``root + ":"`` prefix is NOT a boundary: the tracks of
    # ``https://dav.example.com:8443/dav`` start with the path-less root
    # ``https://dav.example.com`` + ``:`` too.
    best = ""
    for root in list(_active_sources):
        if len(root) > len(best) and (path_str.startswith(root + ":/")
                                      or path_str == root + ":"):
            best = root
    if best:
        return best, path_str[len(best) + 1:]

    parts = urlsplit(path_str)
    path = parts.path
    # Reattach literal URL-metacharacter tails to ``path`` so the file
    # round-trips correctly to FTP/SMB.  ``urlsplit`` is strict about
    # ``?`` and ``#`` — but in our remote-source schemes both are
    # legitimate filename characters, not URL syntax.
    if parts.query:
        path = f"{path}?{parts.query}"
    if parts.fragment:
        path = f"{path}#{parts.fragment}"
    # The scheme as written (``urlsplit`` lowercases it) so the root stays
    # the registry / track-path key of a share entered as ``Https://…``.
    scheme = path_str.split("://", 1)[0]
    if parts.netloc.endswith(":"):
        # A root without a path (WebDAV ``https://dav.example.com``): its
        # ``:`` separator lands right after the host, as an empty port.
        return f"{scheme}://{parts.netloc[:-1]}", path
    sep = path.find(":") if path else -1
    if sep == -1:
        # Either the URL targets the share root (no trailing path) or the
        # legacy ``:`` separator is missing.  Distinguish: ``urlsplit``
        # always returns ``/share`` for share-root URLs, so ``path`` of
        # just ``/share`` (no further slashes) is the legitimate root case;
        # ``/share/dir/file.mp3`` with no ``:`` is legacy data we want
        # operator visibility on.
        is_share_root = path in ("", "/") or "/" not in path.lstrip("/")
        if not is_share_root:
            sanitised = _sanitise_url_for_log(path_str)
            if sanitised not in _LEGACY_URL_LOGGED:
                _LEGACY_URL_LOGGED.add(sanitised)
                log.info(
                    "parse_remote_path: legacy URL %s has no ':' separator — "
                    "remote_path empty, callers will fall back",
                    sanitised,
                )
        return path_str, ""
    share, remote = path[:sep], path[sep + 1:]
    scan_root = f"{scheme}://{parts.netloc}{share}"
    return scan_root, remote


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


# Patterns that signal the FTP server is enforcing a concurrent-client
# limit.  Multiple vendors phrase it differently — vsftpd says
# "421 There are too many connections from your internet address",
# ProFTPD says "530 Sorry, the maximum number of clients (10) for this
# user are already connected", pure-ftpd says "421 Too many connections",
# IIS says "421 Maximum number of clients reached".  Match generously
# (case-insensitive substring) so we don't miss a vendor we haven't
# seen yet — false positives just throttle us, false negatives leave
# the cap unlearned.
_TOO_MANY_CLIENTS_PATTERNS = (
    "too many connections",
    "too many clients",
    "too many users",
    "maximum number of clients",
    "maximum number of users",
    "max number of clients",
    "max clients",
    "max users",
    "allowed clients",           # ProFTPD MaxClients: "maximum number of allowed clients"
    "maximum number of hosts",
    "too many sessions",
)


class FTPLoginRefused(ftplib.error_perm):
    """The server refused the LOGIN (530 that isn't a too-many-clients reply)
    — raised by the pool's connection factory only, so a mid-session
    "530 Not logged in" is never mistaken for bad credentials."""


def _is_too_many_clients_error(exc: BaseException) -> bool:
    """True if *exc* is the FTP server saying "you've hit the per-host limit".

    Triggered by both error_perm (530) and error_temp (421) — the
    protocol allows either, depending on whether the server thinks the
    condition might clear later.  Caller uses this to lower the
    persisted detected_cap.
    """
    if not isinstance(exc, (ftplib.error_perm, ftplib.error_temp)):
        return False
    msg = str(exc).lower()
    return any(pat in msg for pat in _TOO_MANY_CLIENTS_PATTERNS)


# SMB NTSTATUS codes that mean "these credentials will not log in" — retrying
# them only feeds a server's failed-login counter (account lockout, fail2ban).
_SMB_AUTH_STATUSES = frozenset({
    0xC000006D,   # STATUS_LOGON_FAILURE
    0xC000006A,   # STATUS_WRONG_PASSWORD
    0xC0000064,   # STATUS_NO_SUCH_USER
    0xC000006E,   # STATUS_ACCOUNT_RESTRICTION
    0xC000006F,   # STATUS_INVALID_LOGON_HOURS
    0xC0000070,   # STATUS_INVALID_WORKSTATION
    0xC0000071,   # STATUS_PASSWORD_EXPIRED
    0xC0000072,   # STATUS_ACCOUNT_DISABLED
    0xC0000193,   # STATUS_ACCOUNT_EXPIRED
    0xC0000224,   # STATUS_PASSWORD_MUST_CHANGE
    0xC0000234,   # STATUS_ACCOUNT_LOCKED_OUT
})


def is_auth_failure(exc: BaseException | None) -> bool:
    """True when *exc* says the server REFUSED the credentials (an FTP login
    answered 530 — ``FTPLoginRefused`` —, an SMB logon failure, HTTP 401, or
    429 from a login throttle) — a failure that retrying can't fix but can
    turn into a lockout, and that holds for every share logging in with them."""
    if exc is None:
        return False
    if isinstance(exc, ftplib.Error):
        return isinstance(exc, FTPLoginRefused)
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        # 429: a brute-force throttle (Nextcloud) — the same reason to stop.
        # NOT 403: that is one folder the account may not read, not a refused
        # login — counting it would take the account's other shares offline.
        return status in (401, 429)
    try:
        from smbprotocol import exceptions as _smbx
    except Exception:          # smbprotocol not installed
        return False
    if isinstance(exc, _smbx.SMBAuthenticationError):
        return True
    if isinstance(exc, _smbx.SMBResponseException):
        try:
            return exc.status in _SMB_AUTH_STATUSES
        except Exception:
            return False
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


class SourceStream:
    """A sequential read of one source file, from an offset onward.

    ``size`` is the WHOLE file's size (not the remaining bytes) when the
    server says, else ``None``.  ``read(n)`` returns up to ``n`` bytes and
    ``b""`` at the end; ``close()`` releases the connection (a pooled FTP
    connection goes back to its pool).  Used to download into the remote
    cache chunk by chunk — a reader can play the head of a file while its
    tail is still arriving — and to answer a seek past what has arrived
    straight from the share.
    """

    size: int | None = None

    def read(self, n: int = 256 * 1024) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self) -> "SourceStream":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _BytesStream(SourceStream):
    """``SourceStream`` over bytes already in memory (the base-class fallback)."""

    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.size = len(data)
        self._view = memoryview(data)[max(0, offset):]

    def read(self, n: int = 256 * 1024) -> bytes:
        out, self._view = self._view[:n], self._view[n:]
        return bytes(out)


class _FileObjStream(SourceStream):
    """``SourceStream`` over an open binary file object."""

    def __init__(self, fh, size: int | None) -> None:
        self.size = size
        self._fh = fh

    def read(self, n: int = 256 * 1024) -> bytes:
        return self._fh.read(n) or b""

    def close(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass


class FileSource(ABC):
    """Abstract filesystem source."""

    # Why the last ``is_dir`` probe failed (network sources set it) — the
    # share Test / Connect buttons show it instead of a bare "not accessible".
    last_error: str | None = None
    # The last failed ``is_dir`` probe was the server refusing the
    # credentials (``is_auth_failure``) — the health monitor backs off.
    last_error_auth: bool = False
    # ``share_auth_key`` of the share it was created for (``create_source``).
    auth_key: str | None = None

    @abstractmethod
    def walk(self, top: str) -> Iterator[tuple[str, list[str], list[str]]]:
        """Yield (dirpath, dirnames, filenames) like os.walk."""

    @abstractmethod
    def list_dir(self, path: str) -> list[DirEntry]:
        """List entries in a directory."""

    def walk_with_stat(
        self, top: str, *,
        skip_subtree_fn: "Callable[[DirEntry], bool] | None" = None,
        error_sink: "list | None" = None,
    ) -> Iterator[tuple[str, list["DirEntry"], list["DirEntry"]]]:
        """Yield ``(dirpath, dir_entries, file_entries)`` preserving the
        ``DirEntry.size`` and ``DirEntry.mtime`` already returned by
        :meth:`list_dir`.

        ``skip_subtree_fn`` (optional) is invoked on every encountered
        directory entry BEFORE we recurse into it.  Returning ``True``
        prunes that subtree — neither its contents nor any descendants
        are walked.  Used by the freshness loop to skip subtrees whose
        ``dir.mtime`` hasn't changed since the last walk (turning a
        30 K-entry walk into a 50-entry walk when nothing changed).

        ``error_sink`` (optional): if a directory listing FAILS mid-walk
        (network/protocol error) the walk logs and continues over the
        remaining directories — but it also appends ``(dirpath, reason)``
        here so the caller can tell a PARTIAL walk (some dir failed) apart
        from a complete one.  This matters for ghost cleanup: a subtree
        absent because its listing errored is NOT proof its files were
        deleted, and purging it would be silent data loss.

        The default implementation bridges through ``list_dir`` so every
        backend works without an override.  Backends whose underlying
        protocol returns size+mtime in the directory-listing response
        (FTP MLSD, SMB FIND, WebDAV PROPFIND, local scandir) get this
        for free; backends that need a per-file ``STAT`` round-trip will
        also work but pay a round-trip per entry.

        Used by the scanner to skip files whose ``(mtime, size)`` haven't
        changed since the last index — turning a re-scan from "download
        every byte" into "list everything, fetch what changed."
        """
        stack = [top]
        while stack:
            current = stack.pop()
            try:
                entries = self.list_dir(current)
            except Exception as exc:
                log.warning("walk_with_stat: list_dir(%s) failed: %s", current, exc)
                if error_sink is not None:
                    error_sink.append((current, str(exc)))
                continue
            dir_entries = [e for e in entries if e.is_dir]
            file_entries = [e for e in entries if not e.is_dir]
            yield current, dir_entries, file_entries
            for d in dir_entries:
                if skip_subtree_fn is not None and skip_subtree_fn(d):
                    continue
                stack.append(d.path)

    def read_partial(
        self, path: str, max_bytes: int, *, lane: str = "stream",
    ) -> bytes:
        """Read up to ``max_bytes`` from the start of *path*.

        The default implementation reads the whole file and slices —
        no win for backends that don't support partial fetch (local
        FS, basic SMB).  Backends with a streaming protocol (FTP REST,
        HTTP Range, SMB Read with explicit byte ranges) MUST override
        with a real partial-fetch implementation; that's where the
        scanner's "tag header only" optimisation buys its 5–20× on
        FLAC / MP3.

        Implementations may return fewer than ``max_bytes`` (file is
        smaller) or more (when partial fetch is more expensive than
        full fetch for small files — caller already paid the cost,
        give them everything).
        """
        return self.read_file(path, lane=lane)[:max_bytes]

    def read_at(
        self, path: str, offset: int, length: int, *, lane: str = "scan",
    ) -> bytes:
        """Read ``length`` bytes starting at byte ``offset``.

        Used by the remote album-art backfill to fetch just an MP4 ``moov``
        atom (which can live at either end of the file) or a tag header,
        without pulling the whole multi-MB audio payload.  The default reads
        the whole file and slices — correct for every backend; backends with
        byte-range support (FTP ``REST``) override it to actually save the
        bandwidth.
        """
        if length <= 0:
            return b""
        off = max(0, int(offset))
        return self.read_file(path, lane=lane)[off:off + length]

    def open_stream(self, path: str, *, offset: int = 0, lane: str = "stream",
                    length: int | None = None) -> SourceStream:
        """Open a sequential read of *path* from byte ``offset`` (see
        :class:`SourceStream`).  ``length`` is a hint that the caller reads
        at most that many bytes (an HTTP backend asks for just that range).

        The default reads the whole file first — correct for every backend;
        the network backends override it to stream from the wire, holding
        one connection (on ``lane``) until the stream is closed.
        """
        return _BytesStream(self.read_file(path, lane=lane), max(0, int(offset)))

    @abstractmethod
    def read_file(self, path: str, *, lane: str = "stream") -> bytes:
        """Read entire file contents.

        ``lane`` is a hint to backends that maintain pooled connections
        with priority lanes (e.g. :class:`FTPFileSource`).  Two values:

        * ``"stream"`` (default) — playback / waveform fetch.  On a
          pooled backend, gets queue-jump priority on saturation.
        * ``"scan"`` — bulk download for indexing.  Uses the larger
          shared scan pool so a re-index isn't bottlenecked by the
          smaller stream budget.

        Backends without lane semantics (local FS, SMB, WebDAV today)
        ignore the kwarg.  The kwarg is keyword-only so positional
        callers (the abstract base interface) keep working unchanged.
        """

    @abstractmethod
    def stat(self, path: str) -> FileStat:
        """Get file/directory metadata.  Built-in backends also take a
        keyword-only ``lane`` (the priority lane on pooled backends)."""

    @abstractmethod
    def is_dir(self, path: str) -> bool:
        """Check if path is an accessible directory."""

    def close(self) -> None:
        """Release any held connections gracefully.

        Used on mid-session reconnect, where some servers rate-limit
        clients that drop without a clean handshake.  Network sources
        override to send QUIT / LOGOFF / SESSION-END before closing
        the socket.  Local sources are a no-op."""

    def force_close(self) -> None:
        """Hard teardown — close sockets immediately, skip protocol-level
        handshakes (FTP QUIT, SMB LOGOFF, etc.).

        Used during shutdown where graceful close has only cosmetic
        benefit (a tidier server-side log line) but can cost up to ~75 s
        per source if the remote is unreachable.  We hold no locks
        (read-only access) and have no transactions to commit, so a
        TCP RST is semantically equivalent to QUIT from our side.

        Default falls through to ``close()``; network sources override
        to skip the slow handshake."""
        try:
            self.close()
        except Exception:
            pass

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

    def read_file(self, path: str, *, lane: str = "stream") -> bytes:
        # ``lane`` is irrelevant for the local FS (no connection pool).
        # Accepted for interface parity so callers don't need to branch
        # on source type.
        return Path(path).read_bytes()

    def read_at(self, path: str, offset: int, length: int, *, lane: str = "scan") -> bytes:
        if length <= 0:
            return b""
        with open(path, "rb") as fh:
            return os.pread(fh.fileno(), length, max(0, int(offset)))

    def open_stream(self, path: str, *, offset: int = 0, lane: str = "stream",
                    length: int | None = None) -> SourceStream:
        fh = open(path, "rb")
        try:
            size = os.fstat(fh.fileno()).st_size
            fh.seek(max(0, int(offset)))
        except BaseException:
            fh.close()
            raise
        return _FileObjStream(fh, size)

    def stat(self, path: str, *, lane: str = "scan") -> FileStat:
        st = os.stat(path)
        return FileStat(size=st.st_size, mtime=st.st_mtime, is_dir=os.path.isdir(path))

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)


# ── SMB ─────────────────────────────────────────────────────────────────────


# smbclient keeps ONE connection per ``server:port`` and every share of that
# server rides on it — ``smbclient.delete_session`` tears down the connection
# (all its sessions), not one share's.  So the sources registered on a
# connection are counted here and only the LAST one to close deletes it: a
# missing share being retried (its connect succeeds at the host level, its
# root check fails, its source is closed) used to disconnect the healthy
# sibling shares of the same server on every health pass.  Every smbclient
# call passes the share's port and credentials (``_kw``), so a call whose
# connection or session is gone builds a new one for THIS share's user — it
# never rides another user's session or a credential-less one (except a guest
# share whose server took the empty user name: smbclient reads "" as "any
# session"), and a share on a port other than 445 is reached at all.  A tree / session the server
# dropped is rebuilt by the call that meets it (``_heal``); a teardown bumps
# the generation, so the other sources re-register (re-claim) on their next
# call.
_SMB_SESSIONS_LOCK = threading.Lock()
_SMB_SESSION_USERS: "dict[str, weakref.WeakSet]" = {}
_SMB_SESSION_GEN: dict[str, int] = {}
_SMB_ECHO_TIMEOUT_S = 5
_SMB_LOGOFF_TIMEOUT_S = 10
# One registration at a time per server: smbclient's register_session isn't
# safe to run concurrently (two callers each create a Connection, one leaks).
_SMB_REG_LOCKS: dict[str, threading.Lock] = {}
# host:port → (when, why) of the last registration that failed on the network.
_SMB_REG_FAILED: dict[str, tuple[float, str]] = {}
_SMB_REG_FAIL_HOLD_S = 5.0
# NTSTATUS of a tree connect the server dropped (the share was removed /
# re-created, a USB disk re-plugged) and of a session it dropped (kicked,
# expired).  smbclient reuses both from its cache without checking them.
_SMB_TREE_GONE = frozenset({0xC00000C9})                  # NETWORK_NAME_DELETED
_SMB_SESSION_GONE = frozenset({0xC0000203, 0xC000035C})   # USER_SESSION_DELETED, NETWORK_SESSION_EXPIRED


def _smb_session_key(host: str, port: int) -> str:
    return f"{str(host).lower()}:{int(port)}"


def _smb_status(exc: BaseException | None) -> int | None:
    """The NTSTATUS an smbclient / smbprotocol error carries, if any — also
    when it was replaced by an error raised while cleaning up (the close of
    a handle on a tree another thread dropped meanwhile)."""
    seen = 0
    while exc is not None and seen < 5:
        for attr in ("ntstatus", "status"):
            try:
                v = getattr(exc, attr, None)
            except Exception:
                v = None
            if isinstance(v, int):
                return v
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


def _smb_force_close(conn) -> None:
    """Close an smbprotocol connection's socket and wake every request still
    waiting on it: smbprotocol signals none of them when its socket closes,
    so their ``receive()`` (no timeout) would wait forever — a playing stream
    or a download of another share on the same server hung for good."""
    try:
        conn.transport.close()
    except Exception:
        pass
    try:
        for req in list(getattr(conn, "outstanding_requests", {}).values()):
            req.response_event.set()        # → receive() raises SMBConnectionClosed
    except Exception:
        pass


def _smb_close_in_background(conn, graceful: bool, fallback=None) -> None:
    """Close *conn* off the caller's thread (``close()`` runs on the event
    loop): log the sessions off only when the server still answers an ECHO
    within 2 s, give that ``_SMB_LOGOFF_TIMEOUT_S`` + a margin (smbprotocol
    closes opens and trees without any timeout), then force-close."""
    def _run() -> None:
        try:
            if fallback is not None:
                fallback()
                return
            if graceful:
                ok = False
                try:
                    sid = next(iter(list(conn.session_table)), 0)
                    conn.echo(sid=sid, timeout=2)
                    ok = True
                except Exception:
                    pass
                if ok:
                    def _logoff() -> None:
                        try:
                            conn.disconnect(close=True, timeout=_SMB_LOGOFF_TIMEOUT_S)
                        except Exception:
                            pass
                    t = threading.Thread(target=_logoff, name="smb-logoff", daemon=True)
                    t.start()
                    t.join(_SMB_LOGOFF_TIMEOUT_S + 5)
        finally:
            if fallback is None:
                _smb_force_close(conn)  # (also frees a logoff stuck in receive())
    try:
        threading.Thread(target=_run, name="smb-close", daemon=True).start()
    except Exception:
        if fallback is None:
            _smb_force_close(conn)


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
        self._gen = 0                       # _SMB_SESSION_GEN when registered
        self._skey = _smb_session_key(host, port)
        # The credentials the server accepted (guest: "Guest" or "") — what
        # every call passes, so it finds / rebuilds THIS user's session.
        self._reg_user = username
        self._reg_pass = password

    def _kw(self) -> dict:
        return {"port": self._port, "username": self._reg_user,
                "password": self._reg_pass}

    def _registration_live(self) -> bool:
        return self._registered and self._gen == _SMB_SESSION_GEN.get(self._skey, 0)

    def _drop_registration(self) -> bool:
        """Forget this source's claim on the server connection.  True when no
        other registered source still uses it (the caller may delete it)."""
        with _SMB_SESSIONS_LOCK:
            # A claim from before a teardown is gone already: closing such a
            # source must not delete the connection the others rebuilt.
            was = self._registration_live()
            self._registered = False
            users = _SMB_SESSION_USERS.get(self._skey)
            if users is not None:
                users.discard(self)
            if not was:
                return False
            return not users

    def _ensure_registered(self) -> None:
        if self._registration_live():
            return
        with _SMB_SESSIONS_LOCK:
            reg_lock = _SMB_REG_LOCKS.setdefault(self._skey, threading.Lock())
        with reg_lock:
            if self._registration_live():
                return
            # A registration with the server that just failed (another
            # share's, up to ~3 min of connect retries): fail at once instead
            # of every sibling repeating the wait in turn.
            failed = _SMB_REG_FAILED.get(self._skey)
            if failed is not None and time.monotonic() - failed[0] < _SMB_REG_FAIL_HOLD_S:
                raise ConnectionError(f"SMB server {self._skey} unreachable: {failed[1]}")
            try:
                self._register()
            except Exception as exc:
                msg = str(exc).lower()
                if (isinstance(exc, (OSError, socket.timeout, TimeoutError))
                        or "timed out" in msg or "no route" in msg
                        or "connection refused" in msg):
                    _SMB_REG_FAILED[self._skey] = (time.monotonic(),
                                                   f"{type(exc).__name__}: {exc}")
                raise
            _SMB_REG_FAILED.pop(self._skey, None)

    def _register(self) -> None:
        import smbclient

        def _do_register() -> None:
            if self._username or self._password:
                smbclient.register_session(
                    self._host, username=self._username, password=self._password,
                    port=self._port,
                )
                self._reg_user, self._reg_pass = self._username, self._password
            else:
                # Guest / anonymous: try "Guest" first, fall back to empty creds
                try:
                    smbclient.register_session(
                        self._host, username="Guest", password="",
                        port=self._port,
                    )
                    self._reg_user, self._reg_pass = "Guest", ""
                except Exception:
                    smbclient.register_session(
                        self._host, username="", password="",
                        port=self._port,
                    )
                    self._reg_user, self._reg_pass = "", ""

        last_exc: BaseException | None = None
        for attempt in range(_CONNECT_ATTEMPTS):
            try:
                gen = _SMB_SESSION_GEN.get(self._skey, 0)
                _do_register()
                with _SMB_SESSIONS_LOCK:
                    _SMB_SESSION_USERS.setdefault(self._skey, weakref.WeakSet()).add(self)
                    self._gen = gen
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

    def _connection(self):
        try:
            from smbclient._pool import _SMB_CONNECTIONS
            return _SMB_CONNECTIONS.get(self._skey)
        except Exception:
            return None

    def _connection_alive(self) -> bool:
        """An SMB2 ECHO on the server connection shared by this host:port
        answers (False when there is none, or it doesn't)."""
        try:
            conn = self._connection()
            if conn is None or not conn.transport.connected:
                return False
            # A Windows server closes the socket on an ECHO without a valid
            # session id; Samba accepts 0.
            sid = next(iter(list(conn.session_table)), 0)
            conn.echo(sid=sid, timeout=_SMB_ECHO_TIMEOUT_S)
            return True
        except Exception:
            return False

    def _tree_name(self) -> str:
        # The tree is the share itself — a share field with a sub-folder
        # ("Music/Classical") still connects \\host\Music.
        top = self._share.replace("/", "\\").strip("\\").split("\\", 1)[0]
        return f"\\\\{self._host}\\{top}".lower()

    def _share_trees(self, conn=None) -> list:
        """The tree connects of THIS share cached on the server connection
        right now (all sessions) — the objects: holding them keeps their
        ``id()`` from being reused by a fresh tree."""
        conn = conn if conn is not None else self._connection()
        if conn is None:
            return []
        want = self._tree_name()
        out: list = []
        try:
            for sess in list(conn.session_table.values()):
                for tree in list(sess.tree_connect_table.values()):
                    if str(getattr(tree, "share_name", "")).lower() == want:
                        out.append(tree)
        except Exception:
            pass
        return out

    def _forget_trees(self, conn, trees: list) -> int:
        """Drop the given tree connects (the server deleted them) and their
        open handles from the connection's sessions, so the next call
        connects the share afresh.  No network.  The tables are REPLACED, not
        mutated: smbclient iterates them in other threads; a tree another
        thread connects meanwhile is carried over.  Only trees captured
        before the failing call are dropped — never a fresh one a concurrent
        heal's retry just connected."""
        if conn is None or not trees:
            return 0
        doomed = {id(t) for t in trees}       # (``trees`` keeps them alive)
        n = 0
        try:
            for sess in list(conn.session_table.values()):
                old = sess.tree_connect_table
                snap = list(old.items())
                gone = {tid for tid, t in snap if id(t) in doomed}
                if not gone:
                    continue
                new = {tid: t for tid, t in snap if tid not in gone}
                sess.tree_connect_table = new
                for tid, t in list(old.items()):          # connected meanwhile
                    if tid not in gone and tid not in new:
                        new[tid] = t
                dead_trees = {id(t) for tid, t in snap if tid in gone}
                opens = sess.open_table
                osnap = list(opens.items())
                new_opens = {fid: o for fid, o in osnap
                             if id(getattr(o, "tree_connect", None)) not in dead_trees}
                sess.open_table = new_opens
                for fid, o in list(opens.items()):        # opened meanwhile
                    if (fid not in new_opens
                            and id(getattr(o, "tree_connect", None)) not in dead_trees):
                        new_opens[fid] = o
                n += len(gone)
        except Exception:
            log.debug("SMB tree cleanup for //%s/%s failed", self._host, self._share,
                      exc_info=True)
        return n

    def _drop_connection(self, conn=None, *, graceful: bool = False) -> bool:
        """Remove the server connection (*conn*, or the current one) from
        smbclient's cache — only if it is still the cached one — and close
        it; every share on it re-registers on its next call (generation
        bump).  Never blocks: ``graceful`` (a healthy last user) logs the
        sessions off in a background thread (``_smb_close_in_background``);
        otherwise only the socket is closed, at once — a logoff on a dead
        session raises before smbprotocol closes the transport, leaking the
        socket and its receive thread.  Requests still waiting on it are woken
        (``_smb_force_close``).  True when *conn* was the cached connection
        (and is gone now)."""
        try:
            from smbclient._pool import _SMB_CONNECTIONS
        except Exception:                   # smbclient internals moved
            _SMB_CONNECTIONS = None
        dropped = False
        with _SMB_SESSIONS_LOCK:
            if _SMB_CONNECTIONS is not None:
                cur = _SMB_CONNECTIONS.get(self._skey)
                if conn is None:
                    conn = cur
                if conn is not None and cur is conn:
                    _SMB_CONNECTIONS.pop(self._skey, None)
                    dropped = True
            if dropped or _SMB_CONNECTIONS is None:
                _SMB_SESSION_GEN[self._skey] = _SMB_SESSION_GEN.get(self._skey, 0) + 1
                _SMB_SESSION_USERS.pop(self._skey, None)
            self._registered = False
        if _SMB_CONNECTIONS is None:
            def _delete() -> None:
                try:
                    from smbclient import delete_session
                    delete_session(self._host, port=self._port)
                except Exception:
                    pass
            _smb_close_in_background(None, graceful, fallback=_delete)
            return True
        if dropped and conn is not None:
            if graceful:
                _smb_close_in_background(conn, True)
            else:
                _smb_force_close(conn)
        return dropped

    def _teardown_connection(self) -> None:
        """Delete the server connection for every share on it (it is dead,
        or nobody else uses it); each share re-registers on its next call."""
        self._drop_connection()

    def _heal(self, exc: BaseException, conn0, trees0: list) -> bool:
        """Rebuild what *exc* says the server dropped; True when the call is
        worth one retry.  *conn0* / *trees0*: the connection and this share's
        tree connects as they were before the failing call — only those are
        dropped, so concurrent calls meeting the same failure rebuild once."""
        status = _smb_status(exc)
        if status in _SMB_TREE_GONE:
            if self._forget_trees(conn0, trees0):
                log.info("SMB share //%s/%s was dropped by the server — "
                         "reconnecting it", self._host, self._share)
            return True
        if status in _SMB_SESSION_GONE and conn0 is not None:
            # Responses of a dropped session can't be verified any more (that
            # is fatal for the connection's receive thread), so the connection
            # goes: its socket is closed, no logoff.  Other users' calls on it
            # fail once and come back on a new one (``_kw``).
            if self._drop_connection(conn0):
                log.info("SMB session on %s ended by the server — reconnecting",
                         self._host)
            return True
        return False

    def _call(self, fn, *args, **kwargs):
        """An smbclient call with this share's session; a tree / session the
        server dropped is rebuilt once and the call retried."""
        self._ensure_registered()
        conn0 = self._connection()
        trees0 = self._share_trees(conn0)
        try:
            return fn(*args, **self._kw(), **kwargs)
        except Exception as exc:
            if not self._heal(exc, conn0, trees0):
                raise
        self._ensure_registered()
        return fn(*args, **self._kw(), **kwargs)

    def reconnect(self) -> bool:
        """Rebuild this share's SMB session.  Returns True when the share
        answers again.

        The server connection is shared with the other shares of the same
        host:port, so it is rebuilt only when nobody else uses it or it is
        dead (no ECHO answer) — a failure of THIS share (its folder gone)
        must not cut a sibling's stream.  The root check heals a tree or
        session the server dropped (``_call``)."""
        if self._drop_registration() or not self._connection_alive():
            self._teardown_connection()
        try:
            import smbclient
            return bool(self._call(smbclient.path.isdir, self._smb_path("/")))
        except Exception as exc:
            log.info("SMB reconnect to //%s/%s failed: %s: %s",
                     self._host, self._share, type(exc).__name__, exc)
            return False

    def _smb_path(self, path: str) -> str:
        # Reject control characters before the path reaches smbclient.  CR/LF
        # don't have the same protocol-injection risk SMB has as FTP, but
        # they still cause smbclient to throw obscure errors mid-listing;
        # NUL truncates a Python string at the C layer.  Mirrors the FTP
        # guard for consistency.
        if any(c in path for c in ("\r", "\n", "\x00")):
            raise ValueError(
                "SMB path contains illegal control characters (CR/LF/NUL)",
            )
        rel = path.lstrip("/")
        return f"\\\\{self._host}\\{self._share}\\{rel}".replace("/", "\\")

    def _to_posix(self, smb_path: str) -> str:
        return "/" + smb_path.split("\\", 3)[-1].replace("\\", "/") if "\\" in smb_path else smb_path

    def walk(self, top: str) -> Iterator[tuple[str, list[str], list[str]]]:
        import smbclient
        self._ensure_registered()
        smb_top = self._smb_path(top)
        for dirpath, dirnames, filenames in smbclient.walk(smb_top, **self._kw()):
            yield self._to_posix(dirpath), dirnames, filenames

    def list_dir(self, path: str) -> list[DirEntry]:
        import smbclient
        smb_path = self._smb_path(path)

        def _list(**kw) -> list[DirEntry]:
            entries: list[DirEntry] = []
            with smbclient.scandir(smb_path, **kw) as it:
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
        return self._call(_list)

    def read_file(self, path: str, *, lane: str = "stream") -> bytes:
        # SMB has no pooled-connection lane semantics today; ``lane`` is
        # accepted for parity with FTPFileSource so callers don't branch.
        # Every open shares the file for reading (``share_access="r"``):
        # smbclient's default is an exclusive open, which made a second
        # reader of the same file (a seek answered from the share while the
        # download runs, a scan beside a play) fail with a sharing violation.
        import smbclient

        def _read(**kw) -> bytes:
            with smbclient.open_file(self._smb_path(path), mode="rb", share_access="r",
                                     **kw) as f:
                return f.read()
        return self._call(_read)

    def read_at(self, path: str, offset: int, length: int, *, lane: str = "scan") -> bytes:
        """SMB2 ranged read — open, seek, read ``length`` bytes.

        Without this override the base class would read the WHOLE file just to
        slice out a few bytes; the MP4 art backfill issues several small ranged
        reads per file, so a 50 MB share file would otherwise be transferred
        many times over.  smbclient's file handle supports seek + bounded read,
        so we transfer only what's asked.
        """
        if length <= 0:
            return b""
        import smbclient

        def _read(**kw) -> bytes:
            with smbclient.open_file(self._smb_path(path), mode="rb", share_access="r",
                                     **kw) as f:
                off = max(0, int(offset))
                if off:
                    f.seek(off)
                return f.read(length)
        return self._call(_read)

    def read_partial(self, path: str, max_bytes: int, *, lane: str = "scan") -> bytes:
        # Front-only ranged read — same one-shot transfer as read_at(0, n).
        return self.read_at(path, 0, max_bytes, lane=lane)

    def open_stream(self, path: str, *, offset: int = 0, lane: str = "stream",
                    length: int | None = None) -> SourceStream:
        import smbclient
        fh = self._call(smbclient.open_file, self._smb_path(path), mode="rb",
                        share_access="r")
        try:
            size: int | None
            try:
                size = int(fh.seek(0, os.SEEK_END))
            except Exception:
                size = None
            fh.seek(max(0, int(offset)))
        except BaseException:
            fh.close()
            raise
        return _FileObjStream(fh, size)

    def stat(self, path: str, *, lane: str = "scan") -> FileStat:
        import smbclient
        st = self._call(smbclient.stat, self._smb_path(path))
        import stat as _stat
        return FileStat(size=st.st_size, mtime=st.st_mtime,
                        is_dir=_stat.S_ISDIR(st.st_mode))

    def is_dir(self, path: str) -> bool:
        import smbclient
        self.last_error_auth = False
        try:
            return bool(self._call(smbclient.path.isdir, self._smb_path(path)))
        except Exception as exc:
            # Log at WARNING so share-connection failures surface in the log.
            # Previously this silently returned False, so "Share ... root not
            # accessible" left no trail of *why* — was it auth, DNS, firewall,
            # a macOS Local Network privacy block?  Now we know.
            log.warning(
                "SMB is_dir(%s) on //%s/%s failed: %s: %s",
                path, self._host, self._share, type(exc).__name__, exc,
            )
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.last_error_auth = is_auth_failure(exc)
            return False

    def close(self) -> None:
        """Drop this share's claim on the server connection; the connection
        itself is deleted (logged off, socket closed) only when no other
        registered share uses it."""
        if not self._drop_registration():
            return
        # (A source that registered meanwhile is made to re-register by the
        # generation bump.)
        self._drop_connection(graceful=True)

    def force_close(self) -> None:
        """Skip the SMB2_LOGOFF / SMB2_TREE_DISCONNECT handshake — those
        wait for an ack from the server, which costs ~75 s when the share
        is unreachable.  We just drop the registration; the kernel closes
        the underlying TCP socket on process exit (and the server times
        out the session within 10 min regardless)."""
        self._drop_registration()


# ── FTP ─────────────────────────────────────────────────────────────────────
#
# Connection pooling rationale
# ----------------------------
# The original ``FTPFileSource`` kept ONE ``ftplib.FTP`` socket per source
# and had no thread-safety on ``_connect()``.  Under the scanner's 8-way
# concurrent ``run_in_executor(source.read_file, …)`` window, that produced
# the classic single-socket race:
#
#   * Thread A enters _connect, sees ``self._ftp`` is set, runs ``voidcmd
#     ("NOOP")``.
#   * Thread B is mid-``retrbinary`` on the SAME socket; A's NOOP gets a
#     mangled reply and ``_reset()`` is called.
#   * Thread A opens a fresh control channel; B's RETR completes and writes
#     to a now-orphaned socket.
#   * Repeat across 8 threads + multiple shares-per-host and the server
#     quickly sees its per-IP client cap (commonly 10) tripped — the
#     "530 Sorry, the maximum number of clients (10) from your host are
#     already connected" failure the scanner now emits.
#
# The pool below replaces that with one bounded, thread-safe queue per
# (host, port, username, password, encoding).  Different shares with the
# SAME credentials share one queue (so adding three folders on the same
# server doesn't 3× the connection budget).  The default ceiling is 4 —
# below typical server caps and below the scanner's 8-way concurrency, so
# scan workers serialise on the pool rather than racing on socket setup.
# Override via ``SONIQBOOM_FTP_MAX_CONN_PER_HOST``.


# Per-host ceiling.  The pool is now a *shared* lane pool: scan and
# stream workers both borrow from the same physical socket budget, with
# stream borrows holding queue-jump priority on contention (see
# ``borrow(lane='stream'|'scan')``).  Effective max =
#   user_configured_scan + user_configured_stream
# clamped to ``detected_server_cap - 1`` if the cap has been learned
# from a 421/530 trip.  The defaults below are the *budget* halves used
# when the operator hasn't set per-share values in the UI.
#
# Why a shared pool rather than two physically separate pools: the user
# explicitly wanted "dynamic allocation" so that scan can use the full
# budget when stream is idle, while stream still jumps the queue when
# the user clicks Play.  A priority queue gives both properties without
# rigid wall-off.
#
# Environment overrides (also exposed as legacy):
#   SONIQBOOM_FTP_SCAN_CONN_PER_HOST   – scan budget (default 6)
#   SONIQBOOM_FTP_STREAM_CONN_PER_HOST – stream budget (default 2)
#   SONIQBOOM_FTP_BROWSE_CONN_PER_HOST – browse budget (default 1)
#   SONIQBOOM_FTP_MAX_CONN_PER_HOST    – legacy total, splits 75/25 if set
_FTP_POOL_SCAN_DEFAULT   = max(1, int(os.environ.get("SONIQBOOM_FTP_SCAN_CONN_PER_HOST",   "6")))
_FTP_POOL_STREAM_DEFAULT = max(1, int(os.environ.get("SONIQBOOM_FTP_STREAM_CONN_PER_HOST", "2")))
_FTP_POOL_BROWSE_DEFAULT = max(1, int(os.environ.get("SONIQBOOM_FTP_BROWSE_CONN_PER_HOST", "1")))

# Legacy single-knob compatibility — if the operator set the OLD env var
# we honour it as an ABSOLUTE total cap.  Allows existing deployments
# (e.g. the user's prior _FTP_POOL_MAX=4 setup) to work unchanged on
# upgrade.  Browse is carved OUT of the legacy total first (the lane
# didn't exist when the knob was set); the remainder splits 75% scan /
# 25% stream.  Folding browse on TOP — as an earlier version did — would
# silently exceed the operator's cap by 1 and trip a "too many clients"
# rejection on a server pinned to exactly _lm.  Browse can still be tuned
# independently via SONIQBOOM_FTP_BROWSE_CONN_PER_HOST, but is clamped so
# scan+stream always keep at least one slot between them.
_legacy_max = os.environ.get("SONIQBOOM_FTP_MAX_CONN_PER_HOST")
if _legacy_max:
    try:
        _lm = max(2, int(_legacy_max))
        _FTP_POOL_BROWSE_DEFAULT = max(1, min(_FTP_POOL_BROWSE_DEFAULT, _lm - 1))
        _usable = max(1, _lm - _FTP_POOL_BROWSE_DEFAULT)
        _FTP_POOL_STREAM_DEFAULT = max(1, _usable // 4)
        _FTP_POOL_SCAN_DEFAULT   = max(1, _usable - _FTP_POOL_STREAM_DEFAULT)
    except ValueError:
        pass

# Aggregate default — the actual pool's max_size.  Per-share UI / config
# can override at pool-creation time (or via _resize() once running).
_FTP_POOL_MAX = _FTP_POOL_SCAN_DEFAULT + _FTP_POOL_STREAM_DEFAULT + _FTP_POOL_BROWSE_DEFAULT

# Per-host warm-pool floor — how many connections to KEEP established and
# alive at all times so the next operation skips the TCP handshake + LOGIN
# round-trip (typically 100–300 ms each on a LAN, more for cloud FTP).  A
# background daemon thread pre-warms this many sockets on pool creation
# and tops up after recycling, and NOOPs them every ``_FTP_KEEPALIVE_S``
# seconds to keep the server from silently dropping idle sessions.
# Clamped to [0, _FTP_POOL_MAX] at pool construction so a config typo
# doesn't deadlock or oversubscribe the server.
_FTP_POOL_MIN = max(0, int(os.environ.get("SONIQBOOM_FTP_MIN_CONN_PER_HOST", "2")))

# Keep-alive cadence.  Typical FTP servers idle-disconnect after 5 min
# (ProFTPD ``TimeoutIdle 300``, vsftpd ``idle_session_timeout=300``);
# 60 s NOOPs leaves a 4× safety margin without flooding the control
# channel.  Floor at 15 s so a config typo doesn't melt the server.
_FTP_KEEPALIVE_S = max(15, int(os.environ.get("SONIQBOOM_FTP_KEEPALIVE_S", "60")))

# Per-connection transfer ceiling — the previous comment in ``read_file``
# noted that ``retrbinary`` 's control-channel responses can desync after
# many rapid downloads ("200 Type set to I" errors).  Recycling proactively
# keeps that bounded.  Bookkeeping moved from FTPFileSource to the per-
# handle ``xfer_count`` so each pooled connection has its own counter.
_FTP_MAX_PER_CONN = 40

# How long ``pool.borrow()`` waits when the pool is at capacity before
# raising TimeoutError.  Generous because under heavy scan load every
# slot can be doing a multi-second download.  ``read_file`` 's retry loop
# absorbs the (rare) timeout case.
_FTP_BORROW_TIMEOUT_S = 60.0

# Back-off of the keep-alive warm-up after the server refused the login: 5 min,
# doubling per refusal, at most 6 h (an on-demand borrow still tries at once).
_FTP_AUTH_BACKOFF_S = 300.0
_FTP_AUTH_BACKOFF_MAX_S = 6 * 3600.0

# Per-thread override of that wait (``borrow_wait``).
_BORROW_WAIT = threading.local()


@contextmanager
def borrow_wait(seconds: float) -> Iterator[None]:
    """Cap how long a pooled-FTP borrow made by THIS thread waits for a free
    connection.  For a caller that gives up on an open after a few seconds:
    without the cap its abandoned open stays queued as a (priority) stream-lane
    waiter for the full ``_FTP_BORROW_TIMEOUT_S``, holding back every scan /
    browse borrow of the server meanwhile.  Other backends ignore it."""
    prev = getattr(_BORROW_WAIT, "s", None)
    _BORROW_WAIT.s = max(0.01, float(seconds))
    try:
        yield
    finally:
        _BORROW_WAIT.s = prev


class _PooledFTP:
    """A pool handle wrapping one ``ftplib.FTP`` plus bookkeeping.

    The handle is what callers actually touch — they read ``handle.conn``
    for the FTP object, call ``handle.note_transfer()`` after every RETR
    so the pool can recycle the connection after ``_FTP_MAX_PER_CONN``
    downloads, and ``handle.mark_broken()`` to signal that the socket is
    desynchronised and must NOT go back to the idle queue.

    The context manager in ``_FTPConnectionPool.borrow`` calls
    ``mark_broken`` automatically when the ``with`` block exits via an
    exception, so most call sites don't need to think about it.
    """

    __slots__ = ("conn", "xfer_count", "_broken", "lane")

    def __init__(self, conn: ftplib.FTP):
        self.conn = conn
        self.xfer_count = 0
        self._broken = False
        # Lane this handle is currently borrowed under ("stream"|"scan"|"browse").
        # Set by the pool on each borrow; used so _release decrements the
        # right per-lane in-use counter (drives the admin FTP-lanes viz).
        self.lane = "scan"

    def note_transfer(self) -> None:
        """Bump the transfer counter.  Pool will recycle when the count
        reaches ``_FTP_MAX_PER_CONN`` on the next return."""
        self.xfer_count += 1

    def mark_broken(self) -> None:
        """Signal the pool to close this connection on return instead of
        putting it back in the idle queue."""
        self._broken = True

    def abort_transfer(self) -> None:
        """Stop a ``RETR`` the caller quit reading early and leave the control
        channel in step.

        An ``ABOR`` gets two replies — 426 for the interrupted transfer then
        226 for the ``ABOR`` (RFC 959), or, when the transfer had already
        finished, its 226 then the ``ABOR``'s 225/226 — and ``ftplib.abort``
        reads only the first.  The one left pending shifted every later reply
        on the connection by one (a ``SIZE`` read the previous command's
        answer; the next ``REST`` + ``RETR`` failed and burned the
        connection).  So: ``NOOP`` after the abort and read up to its 200,
        skipping whatever the abort left behind — no timeout needed, it works
        with servers that send one reply as well as two."""
        ftp = self.conn
        try:
            ftp.abort()
            ftp.putcmd("NOOP")
            for _ in range(4):
                try:
                    resp = ftp.getresp()
                except (ftplib.error_temp, ftplib.error_perm):
                    continue               # a late 4xx/5xx for the transfer
                if resp.startswith("200"):
                    return
            self.mark_broken()
        except Exception:
            self.mark_broken()


class _FTPConnectionPool:
    """Bounded, thread-safe pool of ftplib.FTP connections to a single
    (host, port, user, password, encoding) tuple.

    Behaviour:
      * ``borrow()`` returns an idle connection if available (validated
        with NOOP), or creates a fresh one if we're below ``max_size``,
        or blocks up to ``_FTP_BORROW_TIMEOUT_S`` waiting for a return.
      * On the ``with`` block exiting normally, the handle goes back to
        the idle queue (or is closed if it crossed the per-conn transfer
        ceiling).
      * On the block exiting via exception, the handle is marked broken
        and closed.  This is what catches mid-RETR socket desync — the
        next caller gets a fresh connection instead of inheriting a
        desynchronised one.
      * ``close_all()`` is idempotent; safe to call from atexit.
    """

    def __init__(self, factory: Callable[[], ftplib.FTP], max_size: int,
                 *, min_size: int = 0, keepalive_s: float = 60.0,
                 label: str = "", host: str = "", port: int = 0):
        self._factory     = factory
        self._max_size    = max_size
        # Clamp to [0, max_size] so a misconfigured env var can't either
        # deadlock the pool (min > max → top-up loop spins forever) or
        # blow past the server cap.
        self._min_size    = max(0, min(min_size, max_size))
        # No floor on keepalive_s at this layer — the env-var entry point
        # (``_FTP_KEEPALIVE_S = max(15, …)``) is where we enforce the
        # production-safe minimum.  Tests pass sub-second values directly
        # to exercise the loop, and we don't want to override that.
        self._keepalive_s = max(0.0, float(keepalive_s))
        self._label       = label  # for logs only, e.g. "host:port"
        # Server identification for the persistent cap-detection store.
        # Both can be empty in unit tests; cap-detection is then a no-op.
        self._host        = host
        self._port        = port
        self._lock        = threading.Lock()
        self._cond        = threading.Condition(self._lock)
        self._idle: list[_PooledFTP] = []
        self._in_use_count = 0
        # Per-lane in-use breakdown (sum == _in_use_count, except transiently
        # during _refresh_idle's NOOP validation, which reserves idle handles
        # against the total without assigning them a lane).  Lets the admin
        # FTP-lanes viz show scan vs stream vs browse activity accurately —
        # without it a scan-heavy reindex was mis-rendered on the stream lane.
        self._in_use_stream = 0
        self._in_use_scan = 0
        self._in_use_browse = 0
        self._closed = False
        # Per-lane waiting bookkeeping.  Each waiting borrow registers
        # in one of these counters BEFORE entering ``cond.wait()``; on
        # release we notify only if a stream borrow is pending OR
        # (no stream, no browse) and there's a scan borrow waiting.
        # The Condition is shared so wakes are correctly delivered; the
        # counters just steer who we *want* to wake.
        self._waiting_stream = 0
        self._waiting_scan   = 0
        self._waiting_browse = 0
        # Set when a reactive too-many-clients trip resizes the pool DOWN, so
        # _maybe_relax_cap knows to reconcile the live size back up later even
        # for a trip that persisted no cap (observed<2 first-connection
        # refusal).  Keeps the never-tripped common pool off the reconcile path.
        self._cap_dirty = False
        # Credentials the server refused (530): the keep-alive warm-up waits
        # ``_FTP_AUTH_BACKOFF_S`` × 2^(streak-1) (capped) before it tries
        # them again — it used to log in with them every keep-alive cycle,
        # forever, which is how a fail2ban / server lockout gets triggered.
        self._auth_fail_streak = 0
        self._auth_retry_at = 0.0
        # No warm-up before a login has worked once: with wrong credentials
        # the warm-up's own logins were refused next to the caller's.
        self._logged_in = False

        # Background warm + keep-alive thread.  Runs when there's a warm floor
        # to maintain (min_size > 0) OR whenever the pool has a real server
        # identity (host+port) — the latter so the periodic detected-cap
        # recovery (F1) works even in purely-on-demand mode (min_size == 0),
        # where the warm/refresh work below is skipped but cap-decay still runs.
        # Daemon so it doesn't block interpreter exit.
        #
        # Both events live on the instance regardless of whether the
        # thread starts, so callers (``_release`` → ``_kalive_stop_or_nudge``
        # and ``close_all``) can poke them without an attribute-exists
        # check or an init-order race.
        self._kalive_stop   = threading.Event()
        self._kalive_nudge  = threading.Event()
        self._kalive_thread: threading.Thread | None = None
        if self._min_size > 0 or (self._host and self._port):
            self._kalive_thread = threading.Thread(
                target=self._keepalive_loop,
                name=f"ftp-keepalive[{self._label or 'pool'}]",
                daemon=True,
            )
            self._kalive_thread.start()

    @contextmanager
    def borrow(self, lane: str = "scan") -> Iterator[_PooledFTP]:
        """Acquire a connection.  ``lane`` is a priority hint:

        * ``"stream"`` — high priority.  Jumps the queue when the pool
          is saturated, so a play attempt during a heavy scan returns
          within milliseconds of the next release.
        * ``"browse"`` — medium priority.  Interactive folder listing;
          jumps ahead of scan so a file-browser click stays responsive
          during a heavy reindex, but yields to stream playback.
        * ``"scan"`` — normal priority (the default).  Yields to any
          pending stream or browse borrow on release.

        The same single physical pool serves all three lanes.  When no
        higher-priority borrows are in flight, scan can use the full
        ``max_size`` budget (this is the "dynamic allocation" property
        the operator asked for).
        """
        if lane not in ("stream", "scan", "browse"):
            lane = "scan"
        handle = self._acquire(getattr(_BORROW_WAIT, "s", None) or _FTP_BORROW_TIMEOUT_S,
                               lane=lane)
        try:
            yield handle
        except BaseException:
            handle.mark_broken()
            raise
        finally:
            self._release(handle)

    def _acquire(self, timeout: float, *, lane: str = "scan") -> _PooledFTP:
        deadline = time.monotonic() + timeout
        # Track whether we've been registered as a waiter, so we
        # decrement the right counter on every exit path (success,
        # timeout, exception).
        registered = False
        while True:
            with self._cond:
                if self._closed:
                    if registered:
                        if lane == "stream": self._waiting_stream -= 1
                        elif lane == "browse": self._waiting_browse -= 1
                        else:                self._waiting_scan -= 1
                    raise RuntimeError("FTP pool is closed")

                # Priority fairness: stream > browse > scan. When higher-
                # priority borrows are waiting, yield to them.
                higher_priority_ahead = (
                    (lane == "scan" and (self._waiting_stream > 0 or self._waiting_browse > 0))
                    or (lane == "browse" and self._waiting_stream > 0)
                )

                # 1. Reuse an idle connection if one validates cleanly,
                # provided no higher-priority borrow is queued ahead of us.
                if not higher_priority_ahead:
                    while self._idle:
                        handle = self._idle.pop()
                        try:
                            handle.conn.voidcmd("NOOP")
                            self._in_use_count += 1
                            handle.lane = lane
                            if lane == "stream": self._in_use_stream += 1
                            elif lane == "browse": self._in_use_browse += 1
                            else:                self._in_use_scan += 1
                            if registered:
                                if lane == "stream": self._waiting_stream -= 1
                                elif lane == "browse": self._waiting_browse -= 1
                                else:                self._waiting_scan -= 1
                            return handle
                        except Exception:
                            # Stale / desynced — quietly drop and try the next
                            # one (or fall through to create a new one).
                            try:
                                handle.conn.close()
                            except Exception:
                                pass

                # 2. Reserve a slot if we're under the cap AND nobody
                # higher-priority is waiting.  TCP handshake happens
                # OUTSIDE the lock so other threads can still acquire
                # idle connections while we're connecting.
                total = self._in_use_count + len(self._idle)
                if total < self._max_size and not higher_priority_ahead:
                    self._in_use_count += 1
                    if lane == "stream": self._in_use_stream += 1
                    elif lane == "browse": self._in_use_browse += 1
                    else:                self._in_use_scan += 1
                    if registered:
                        if lane == "stream": self._waiting_stream -= 1
                        elif lane == "browse": self._waiting_browse -= 1
                        else:                self._waiting_scan -= 1
                    break  # leave the ``with`` block to do the connect

                # 3. Pool is saturated (or yielding to higher priority)
                # — register as a waiter and wait for a return.
                if not registered:
                    if lane == "stream": self._waiting_stream += 1
                    elif lane == "browse": self._waiting_browse += 1
                    else:                self._waiting_scan   += 1
                    registered = True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Clean up waiter bookkeeping before raising.
                    if lane == "stream": self._waiting_stream -= 1
                    elif lane == "browse": self._waiting_browse -= 1
                    else:                self._waiting_scan -= 1
                    raise TimeoutError(
                        f"FTP pool {self._label!r} acquire timed out after "
                        f"{timeout:.1f}s (max={self._max_size}, lane={lane})",
                    )
                self._cond.wait(remaining)
                continue

        # Outside the lock — slow TCP handshake.  On failure we MUST give
        # back the reserved slot or the pool counter drifts upward forever.
        try:
            conn = self._factory()
            self._note_login(None)
            handle = _PooledFTP(conn)
            handle.lane = lane
            return handle
        except BaseException as exc:
            self._note_login(exc)
            # Capture the current in-use count BEFORE giving the slot back
            # so cap-detection records the true peak we hit.  If this
            # ``too many clients`` error came from the factory's LOGIN
            # under contention, ``_in_use_count`` is the right number to
            # report (it includes the slot we just reserved + would-have-
            # connected).
            with self._cond:
                observed = self._in_use_count
                self._in_use_count -= 1
                # Roll back the per-lane counter too (we reserved it above).
                if lane == "stream": self._in_use_stream = max(0, self._in_use_stream - 1)
                elif lane == "browse": self._in_use_browse = max(0, self._in_use_browse - 1)
                else:                self._in_use_scan = max(0, self._in_use_scan - 1)
                # Wake one waiter so the cap reservation doesn't strand
                # the next person in line.
                self._cond.notify()
            # If the server rejected us with too-many-clients, persist
            # the cap one slot below where we were and resize.  Done
            # outside the lock to avoid holding the pool lock across
            # disk IO.
            if _is_too_many_clients_error(exc) and self._host and self._port:
                try:
                    from soniqboom.core import ftp_pool_config as _fcc
                    new_cap = _fcc.record_too_many_clients(
                        self._host, self._port, observed,
                    )
                    # A trip must BACK OFF, never grow.  Resize to the resolved
                    # clamp min(configured, detected-1) but never above the
                    # reactive value — as a single min():
                    #   * normal reactive → detected-1 (the -1 headroom slot),
                    #     not the raw detected (which was one over and briefly
                    #     re-opened the offending slot);
                    #   * manual pin → record returns the pin unchanged, so this
                    #     lands at min(configured, pin-1) and NEVER grows the
                    #     pool above its clamp (no grow-on-trip mini-storm);
                    #   * observed<2 refusal (new_cap==1, no cap persisted) →
                    #     min(1, configured)=1, a hard backoff.
                    self.resize(min(max(1, new_cap),
                                    _resolve_pool_size(self._host, self._port)[0]))
                    # Mark dirty so the keepalive loop reconciles the live size
                    # back up later — critical for the observed<2 refusal, which
                    # shrank us to 1 but persisted NO cap (so get_detected_cap
                    # stays None and the fast path would otherwise skip us).
                    self._cap_dirty = True
                except Exception:
                    log.exception("Failed to record too-many-clients for %s",
                                  self._label)
            raise

    def _note_login(self, exc: BaseException | None) -> None:
        """Track logins for the warm-up: the first success starts it, a
        refused one backs it off (None = success)."""
        if exc is None:
            with self._lock:
                first = not self._logged_in
                self._logged_in = True
                self._auth_fail_streak = 0
                self._auth_retry_at = 0.0
            if first and self._min_size > 0:
                self._kalive_stop_or_nudge()     # warm up now
            return
        if is_auth_failure(exc):
            with self._lock:
                self._auth_fail_streak += 1
                streak = self._auth_fail_streak
                delay = min(_FTP_AUTH_BACKOFF_MAX_S,
                            _FTP_AUTH_BACKOFF_S * (2 ** min(streak - 1, 16)))
                self._auth_retry_at = time.monotonic() + delay
            if streak == 1:
                log.warning("FTP %s refused the login (%s) — not retrying it "
                            "in the background for %.0f s", self._label, exc, delay)

    def _release(self, handle: _PooledFTP) -> None:
        need_topup = False
        with self._cond:
            self._in_use_count -= 1
            # Decrement the per-lane counter matching how this handle was
            # borrowed (clamped — defensive against any bookkeeping drift).
            if handle.lane == "stream":
                self._in_use_stream = max(0, self._in_use_stream - 1)
            elif handle.lane == "browse":
                self._in_use_browse = max(0, self._in_use_browse - 1)
            else:
                self._in_use_scan = max(0, self._in_use_scan - 1)
            recycle = (
                self._closed
                or handle._broken
                or handle.xfer_count >= _FTP_MAX_PER_CONN
                # Also recycle if we're now OVER the (possibly recently
                # resized-down) max — return the connection to the wild
                # rather than keeping it idle past the cap.
                or self._in_use_count + len(self._idle) >= self._max_size
            )
            if recycle:
                try:
                    handle.conn.close()
                except Exception:
                    pass
                # If recycling dropped us below the warm floor, ask the
                # keepalive loop to backfill on its next cycle.  We could
                # spawn an immediate top-up thread here, but that would
                # add a TCP handshake to the release-path latency; the
                # keepalive loop catches it within ``_keepalive_s`` which
                # is short enough for the warm-pool guarantee.
                if (
                    not self._closed
                    and self._min_size > 0
                    and len(self._idle) < self._min_size
                ):
                    need_topup = True
            else:
                self._idle.append(handle)
            # Notify ALL waiters when a higher-priority borrow (stream or
            # browse) is pending — they have to re-check whether the head
            # of the priority queue owns the freed slot.  A single notify()
            # could wake a lower-priority waiter who then yields and re-
            # waits, stranding the higher-priority waiter until timeout;
            # notify_all() lets the priority waiter re-contend immediately.
            # When only scan borrows are waiting, one notify() is enough.
            # This is the cheap version of a priority queue: counters +
            # notify_all-when-priority-pending.
            if self._waiting_stream > 0 or self._waiting_browse > 0:
                self._cond.notify_all()
            else:
                self._cond.notify()
        if need_topup:
            # Nudge the keepalive thread to run sooner.  The Event-based
            # sleep below honours set() as an early wakeup signal.
            self._kalive_stop_or_nudge()

    # ── Dynamic resize + status ────────────────────────────────────────────

    def resize(self, new_max: int) -> None:
        """Change the pool's ``max_size`` live.

        Used by:
          * The auto-detector after a too-many-clients trip (lower).
          * The Settings UI when the operator drags the worker sliders.
          * Active probe endpoint after detecting the actual server cap.

        Shrinking is best-effort: in-flight borrows complete on the old
        limit and the next ``_release`` recycles the over-cap handle
        rather than returning it to idle.  Growing is immediate: the
        next ``_acquire`` sees the new ceiling and can create a fresh
        connection up to it.
        """
        new_max = max(1, int(new_max))
        with self._cond:
            if new_max == self._max_size:
                return
            old = self._max_size
            self._max_size = new_max
            # Trim min so it never exceeds the new ceiling.
            if self._min_size > new_max:
                self._min_size = new_max
            # If we shrunk, close as many idle handles as needed to fit
            # under the cap right now.  In-use handles can't be reclaimed
            # without interrupting their owner; they'll auto-recycle on
            # release via the over-cap check in _release.
            over = (self._in_use_count + len(self._idle)) - new_max
            closed_now = 0
            while over > 0 and self._idle:
                handle = self._idle.pop()
                try:
                    handle.conn.close()
                except Exception:
                    pass
                over -= 1
                closed_now += 1
            # Growing: wake every waiter so they re-check the cap.
            if new_max > old:
                self._cond.notify_all()
        log.info(
            "FTP pool %s resized %d → %d (closed %d idle)",
            self._label, old, new_max, closed_now,
        )

    def status(self) -> dict:
        """Snapshot of pool state — for admin / debug endpoints."""
        with self._cond:
            return {
                "label":          self._label,
                "max_size":       self._max_size,
                "min_size":       self._min_size,
                "in_use":         self._in_use_count,
                "in_use_stream":  self._in_use_stream,
                "in_use_scan":    self._in_use_scan,
                "in_use_browse":  self._in_use_browse,
                "idle":           len(self._idle),
                "waiting_stream": self._waiting_stream,
                "waiting_scan":   self._waiting_scan,
                "waiting_browse": self._waiting_browse,
                "closed":         self._closed,
            }

    # ── Warm-min + keep-alive ───────────────────────────────────────────────

    def _kalive_stop_or_nudge(self) -> None:
        """Wake the keepalive loop early without telling it to exit.

        The loop sleeps via ``Event.wait(timeout)``; we use a second
        Event (``_kalive_nudge``) plus a tiny re-check so the loop can
        distinguish "shutdown" from "do a cycle now".
        """
        if self._kalive_thread is not None:
            self._kalive_nudge.set()

    def _keepalive_loop(self) -> None:
        """Maintain ``min_size`` warm idle connections; NOOP them
        regularly so the server doesn't drop them as idle.

        Daemon thread, one per pool.  Exits when ``close_all`` sets
        ``_kalive_stop``.  Errors inside a cycle are logged and skipped
        — a dead server shouldn't kill the keepalive thread; the next
        cycle gets a fresh chance.

        Also runs the optional ``auto_grow`` probe: when the per-server
        ``ftp_pools.<host:port>.auto_grow`` toggle is on AND there's
        active demand (waiting borrows OR pool fully in use) AND the
        detected server cap hasn't been hit yet, attempt to open one
        extra connection.  Successful probe → bump max_size by 1, raise
        the configured scan budget so the change persists across
        restarts.  Probe failure with too-many-clients → record the cap
        (existing reactive flow already handles the resize-down).
        """
        # Warm immediately so the FIRST borrow lands on an established
        # connection rather than triggering a sync TCP handshake.  Only when
        # there's a warm floor — a min_size==0 (on-demand) pool runs this loop
        # solely for cap recovery and must NOT start holding idle sockets.
        if self._min_size > 0:
            try:
                self._top_up_idle()
            except Exception:
                log.exception("FTP keepalive initial warm failed (pool=%s)",
                              self._label)

        probe_consecutive_fails = 0  # back off after repeated failures

        while not self._kalive_stop.is_set():
            # Sleep for the cadence OR until a nudge / shutdown fires.
            self._kalive_nudge.wait(self._keepalive_s)
            self._kalive_nudge.clear()
            if self._kalive_stop.is_set():
                return
            if self._min_size > 0:
                try:
                    self._refresh_idle()
                    self._top_up_idle()
                except Exception:
                    log.exception("FTP keepalive cycle failed (pool=%s)",
                                  self._label)

            if self._host and self._port:
                # Cap-decay: let a reactively-lowered detected cap creep back
                # toward the user's configured budget after a trip-free spell,
                # so a TRANSIENT 421/530 doesn't pin the pool below what the
                # user asked for until a manual reset.  Runs for EVERY real
                # pool (incl. on-demand min_size==0), and before the grow-probe
                # so the probe sees the recovered clamp.
                try:
                    self._maybe_relax_cap()
                except Exception:
                    log.exception("FTP cap-relax failed (pool=%s)", self._label)

                # Optional growth probe — pushes ABOVE the budget on demand.
                # Gated to warm pools (min_size>0): it opens+idles a probe
                # connection that ONLY _refresh_idle (also min_size>0) keeps
                # alive, so running it on an on-demand pool would strand an
                # unrefreshed idle socket.  Same cadence as keepalive (60 s).
                if self._min_size > 0:
                    try:
                        grew = self._maybe_probe_grow()
                        if grew:
                            probe_consecutive_fails = 0
                        else:
                            probe_consecutive_fails += 1
                    except Exception:
                        log.exception("FTP grow-probe failed (pool=%s)",
                                      self._label)

    def _maybe_relax_cap(self) -> None:
        """Recover a reactively-lowered pool toward the configured budget after
        a trip-free spell, and reconcile the live ``max_size`` to the resolved
        source of truth.

        A 421/530 clamps ``max_size = min(configured, detected-1)``.  If the
        trip was transient (temporary overload) the pool would otherwise stay
        stuck below the user's configured budget until a manual reset.  Two
        recovery paths, both handled here:

        * Persisted cap → ``ftp_pool_config.relax_detected_cap`` does the AIMD
          time-gate + persistence (+1 per ``_CAP_DECAY_INTERVAL_S``, dropped
          straight back by the reactive handler if the higher value trips).
        * A trip that persisted NO cap (``observed < 2`` first-connection
          refusal still resizes the live pool down to 1) — ``_cap_dirty`` marks
          it so we reconcile back up even though ``get_detected_cap`` is None.

        We then resize the live pool to ``_resolve_pool_size`` (which also
        corrects the reactive handler's off-by-one: it resizes to ``detected``,
        the resolved clamp is ``detected-1``).  Unlike auto-grow this never
        exceeds the configured budget — it only undoes a reactive clamp, so it
        needs neither the ``auto_grow`` opt-in nor a demand gate (a higher
        ``max_size`` opens no sockets until something borrows).
        """
        from soniqboom.core import ftp_pool_config as _fcc
        has_cap = _fcc.get_detected_cap(self._host, self._port) is not None
        # Fast path: never-tripped pool with nothing to reconcile — skip the
        # conf read entirely (keeps idle on-demand pools cheap).
        if not has_cap and not self._cap_dirty:
            return
        if has_cap:
            _mx, _mn, configured_total, _det = _resolve_pool_size(self._host, self._port)
            _fcc.relax_detected_cap(self._host, self._port, configured_total)
        # Reconcile the live pool to the (possibly raised / cleared) clamp.
        max_size, _min, _ct, det_now = _resolve_pool_size(self._host, self._port)
        with self._cond:
            cur = self._max_size
        if max_size != cur:
            self.resize(max_size)
            log.info(
                "FTP cap-reconcile: pool=%s max %d → %d (detected_cap now %s)",
                self._label, cur, max_size, det_now,
            )
        # Once no cap remains AND the live size matches the resolved budget,
        # there's nothing left to reconcile — clear the dirty flag so a
        # never-re-tripping on-demand pool returns to the cheap fast path.
        if det_now is None and max_size == self._max_size:
            self._cap_dirty = False

    def _maybe_probe_grow(self) -> bool:
        """Attempt one growth-probe cycle.  Returns True if the pool
        ceiling was raised, False otherwise (auto_grow disabled, no
        demand, server cap reached, or probe just didn't fit).

        Read-only against the conf — the user's UI toggle is the
        source of truth.  Probe is silent on the no-op paths so a
        normal-load idle pool doesn't spam the log.
        """
        # 1. Is auto_grow enabled for this server?
        try:
            from soniqboom.config import load_local_conf
            conf = load_local_conf() or {}
        except Exception:
            return False
        server_key = f"{self._host}:{int(self._port)}"
        pool_cfg = (conf.get("ftp_pools") or {}).get(server_key) or {}
        if not pool_cfg.get("auto_grow"):
            return False

        # 2. Is there real demand?  No point growing if the pool is
        # half-idle — that just creates handles the server can stale-
        # drop later.  Demand = waiters OR fully-saturated borrows.
        with self._cond:
            saturated = self._in_use_count >= self._max_size
            waiting = (
                self._waiting_scan > 0
                or self._waiting_stream > 0
                or self._waiting_browse > 0
            )
            current_max = self._max_size
        if not (saturated or waiting):
            return False

        # 3. Don't probe past the detected server cap if we have one
        # (the reactive 421/530 handler will have set it).
        try:
            from soniqboom.core import ftp_pool_config as _fcc
            detected = _fcc.get_detected_cap(self._host, self._port)
        except Exception:
            detected = None
        if isinstance(detected, int) and current_max >= max(1, detected - 1):
            return False

        # 4. Attempt the probe.  Open ONE extra connection outside the
        # lock; if it succeeds, take the lock and bump max_size + push
        # to idle so a waiting borrow grabs it next round.
        log.info(
            "FTP auto-grow probe: pool=%s trying max %d → %d",
            self._label, current_max, current_max + 1,
        )
        try:
            handle = _PooledFTP(self._factory())
        except Exception as exc:
            # The reactive path inside _acquire already records the cap
            # on too-many-clients failures, so we don't duplicate that
            # here — just log and back off.
            log.info(
                "FTP auto-grow probe failed for pool=%s: %s",
                self._label, exc,
            )
            return False

        with self._cond:
            if self._closed:
                # Race with close_all — discard the connection.
                try:
                    handle.conn.close()
                except Exception:
                    pass
                return False
            self._max_size = current_max + 1
            self._idle.append(handle)
            self._cond.notify()
        log.info(
            "FTP auto-grow: pool=%s expanded to max=%d",
            self._label, current_max + 1,
        )

        # 5. Persist the new ceiling so it survives restart.  Bump the
        # configured scan budget by 1 (stream stays put).  Reload-safe:
        # _resolve_pool_size will read the new value on next startup.
        try:
            from soniqboom.config import load_local_conf, save_local_conf
            conf = load_local_conf() or {}
            pools = conf.setdefault("ftp_pools", {})
            entry = pools.setdefault(server_key, {})
            entry["scan"] = int(entry.get("scan", _FTP_POOL_SCAN_DEFAULT)) + 1
            entry.setdefault("stream", _FTP_POOL_STREAM_DEFAULT)
            entry.setdefault("browse", _FTP_POOL_BROWSE_DEFAULT)
            entry["auto_grow"] = True  # preserve
            save_local_conf(conf)
        except Exception:
            log.warning("FTP auto-grow: persist failed for pool=%s",
                        self._label, exc_info=True)
        return True

    def _refresh_idle(self) -> None:
        """NOOP every currently-idle connection; drop the dead ones.

        Strategy: snapshot the idle queue under the lock, mark those
        slots as "in use" so concurrent borrows don't race us for the
        same handles, then run the NOOPs WITHOUT the lock held (one
        round trip each, typically <30 ms but variable on slow LANs).
        Survivors go back to idle; dead ones are closed and their slot
        is freed for the next top-up cycle.
        """
        with self._cond:
            if self._closed:
                return
            snapshot = list(self._idle)
            self._idle.clear()
            self._in_use_count += len(snapshot)

        alive: list[_PooledFTP] = []
        for handle in snapshot:
            try:
                handle.conn.voidcmd("NOOP")
                alive.append(handle)
            except Exception:
                try:
                    handle.conn.close()
                except Exception:
                    pass

        with self._cond:
            # Return survivors; release slots taken by the dead ones.
            self._in_use_count -= len(snapshot)
            self._idle.extend(alive)
            if alive:
                self._cond.notify_all()

    def _top_up_idle(self) -> None:
        """Open new connections up to ``min_size`` idle, never breaching
        ``max_size`` total.

        Slots are reserved under the lock BEFORE the slow TCP handshake;
        unused reservations are returned in a single ``finally`` block so
        a partial failure (server is half-up) doesn't leak budget.
        """
        if not self._logged_in:
            return                          # no login has worked yet
        if self._auth_retry_at and time.monotonic() < self._auth_retry_at:
            return                          # refused credentials: backing off
        with self._cond:
            if self._closed:
                return
            total      = self._in_use_count + len(self._idle)
            slots_left = self._max_size - total
            need       = max(0, self._min_size - len(self._idle))
            to_create  = min(need, slots_left)
            if to_create <= 0:
                return
            self._in_use_count += to_create  # reserve slots

        new_handles: list[_PooledFTP] = []
        try:
            for _ in range(to_create):
                if self._kalive_stop.is_set():  # bail early on shutdown
                    break
                try:
                    conn = self._factory()
                    self._note_login(None)
                    new_handles.append(_PooledFTP(conn))
                except Exception as exc:
                    self._note_login(exc)
                    # Stop trying this cycle — if the server's down we
                    # don't want to hammer it.  The next keepalive tick
                    # gives it another chance.
                    log.info(
                        "FTP warm-up to %s failed (%s); will retry on "
                        "next keepalive cycle",
                        self._label, exc,
                    )
                    break
        finally:
            with self._cond:
                # Release ALL reserved slots — the freshly-built conns go
                # to ``_idle`` (not into in-use) and the unused reservations
                # for failed factories need to go back to the budget.  If
                # we only released ``to_create - len(new_handles)`` (the
                # unused ones) the slots for the successful handles would
                # stay marked in-use forever even though their handles are
                # now sitting in the idle queue — the pool's total budget
                # would drift upward every time the keepalive ran, and
                # eventually saturate against ``max_size`` without ever
                # holding the connections it thinks it does.
                self._in_use_count -= to_create
                self._idle.extend(new_handles)
                if new_handles:
                    self._cond.notify_all()

    def recycle_all_idle(self) -> None:
        """Close every currently-idle connection.

        Used by ``FTPFileSource.reconnect()`` — the operator clicked
        "Reconnect" because something's wrong, so wipe the cached idle
        sockets and let the next op start fresh.  In-use connections are
        not touched; they'll close on return because of the broken flag
        their caller sets (or naturally next time around).
        """
        with self._cond:
            old, self._idle = self._idle, []
        for handle in old:
            try:
                handle.conn.close()
            except Exception:
                pass

    def close_all(self) -> None:
        """Close everything and refuse further borrows.  Idempotent.

        Also wakes the keepalive thread so it can notice ``_closed`` and
        exit instead of sleeping out the rest of its cycle (which would
        delay process shutdown by up to ``_keepalive_s``).
        """
        with self._cond:
            self._closed = True
            old, self._idle = self._idle, []
            self._cond.notify_all()
        # Tell the keepalive thread to exit.  ``_kalive_nudge`` may not
        # exist yet if close_all races a pool that was just constructed
        # — guard with getattr for that.
        self._kalive_stop.set()
        nudge = getattr(self, "_kalive_nudge", None)
        if nudge is not None:
            nudge.set()
        for handle in old:
            try:
                handle.conn.close()
            except Exception:
                pass


# Module-level pool registry, keyed by the credentials tuple.  Multiple
# ``FTPFileSource`` instances pointed at the same server share one pool
# (so adding three shares on the same NAS doesn't fan out to 3× the
# connection budget — the original failure mode).  Encoding is part of
# the key because ``ftplib.FTP.encoding`` is per-instance and we don't
# want to mix latin-1 and utf-8 sockets in the same queue.
_FTP_POOLS: dict[tuple, _FTPConnectionPool] = {}
_FTP_POOLS_LOCK = threading.Lock()


def _build_ftp_factory(host: str, port: int, username: str, password: str,
                       encoding: str) -> Callable[[], ftplib.FTP]:
    """Return a zero-arg factory that opens + logs in one new FTP socket.

    Closes over the credentials so the pool can call it whenever it needs
    a fresh connection.  Mirrors the retry behaviour of the old
    ``FTPFileSource._connect``: short exponential backoff for transient
    network errors, fail-fast for permanent ones (auth failures).
    """
    def _factory() -> ftplib.FTP:
        last_exc: BaseException | None = None
        for attempt in range(_CONNECT_ATTEMPTS):
            try:
                ftp = ftplib.FTP()
                ftp.encoding = encoding
                ftp.connect(host, port, timeout=15)
                try:
                    ftp.login(username, password)
                except ftplib.error_perm as exc:
                    try:
                        ftp.close()
                    except Exception:
                        pass
                    if (str(exc).strip()[:3] == "530"
                            and not _is_too_many_clients_error(exc)):
                        raise FTPLoginRefused(*exc.args) from exc
                    raise
                # Enable UTF-8 mode on servers that support it (e.g. ProFTPD).
                # Required for CWD/RETR on paths with multi-byte characters
                # (Japanese, symbols like ☆♥µ).  Combined with latin-1 client
                # encoding the bytes round-trip for both valid-UTF-8 and
                # non-UTF-8 filenames.
                try:
                    ftp.sendcmd("OPTS UTF8 ON")
                except (ftplib.error_perm, ftplib.error_temp):
                    pass  # server doesn't support UTF-8 opts — harmless
                if attempt > 0:
                    log.info("FTP reconnected to %s:%d after %d retries",
                             host, port, attempt)
                return ftp
            except BaseException as exc:
                last_exc = exc
                if not _is_transient_network_error(exc):
                    raise
                if attempt < _CONNECT_ATTEMPTS - 1:
                    wait = _CONNECT_BACKOFF_BASE * (2 ** attempt)
                    log.info(
                        "FTP connect to %s:%d failed (attempt %d/%d): "
                        "%s: %s — retrying in %.1fs",
                        host, port, attempt + 1, _CONNECT_ATTEMPTS,
                        type(exc).__name__, exc, wait,
                    )
                    time.sleep(wait)
        assert last_exc is not None
        raise last_exc
    return _factory


def _resolve_pool_size(host: str, port: int) -> tuple[int, int, int, int | None]:
    """Compute the effective pool size for ``host:port``.

    Returns ``(max_size, min_size, configured_total, detected_cap)``:
      * ``max_size`` — what we'll actually use for the pool
      * ``min_size`` — warm-min floor (capped to max)
      * ``configured_total`` — scan_budget + stream_budget the user asked for
      * ``detected_cap`` — the learned server cap, or None if not yet learned

    The clamp rule: if a detected cap exists, ``max_size = min(configured,
    detected - 1)`` — minus-1 reserves a slot for occasional probes /
    health checks without tripping the limit.
    """
    # Pool config storage is keyed by ``host:port`` — NOT per-share —
    # because the actual ``_FTPConnectionPool`` registry is keyed by
    # ``(host, port, user, pass, encoding)``.  Six shares on the same
    # NAS share ONE pool; the UI surfaces ONE card per host:port for
    # exactly that reason.  Source of truth: ``conf["ftp_pools"]
    # ["10.0.0.88:21"] = {"scan": 6, "stream": 2}``.  Legacy fallback:
    # if any share in ``network_shares`` carries a per-share
    # ``ftp_pool`` field (from the original short-lived design) we
    # accept it as a one-time migration source — the next save will
    # write the canonical top-level shape.
    try:
        from soniqboom.config import load_local_conf
        conf = load_local_conf()
    except Exception:
        conf = {}
    if not isinstance(conf, dict):
        conf = {}
    scan_budget = _FTP_POOL_SCAN_DEFAULT
    stream_budget = _FTP_POOL_STREAM_DEFAULT
    browse_budget = _FTP_POOL_BROWSE_DEFAULT
    # 1. Canonical per-server override.
    server_key = f"{host}:{int(port)}"
    pools_map = conf.get("ftp_pools") or {}
    if isinstance(pools_map, dict):
        pool_cfg = pools_map.get(server_key)
        if isinstance(pool_cfg, dict):
            try:
                scan_budget   = max(1, int(pool_cfg.get("scan",   scan_budget)))
                stream_budget = max(1, int(pool_cfg.get("stream", stream_budget)))
                browse_budget = max(1, int(pool_cfg.get("browse", browse_budget)))
            except (TypeError, ValueError):
                pass
            # Stop here — explicit per-server override wins over any
            # leftover per-share legacy.
            configured_total_early = True
        else:
            configured_total_early = False
    else:
        configured_total_early = False
    # 2. Legacy per-share fallback (only when no per-server override).
    if not configured_total_early:
        shares = conf.get("network_shares", {})
        for s in shares.values() if isinstance(shares, dict) else []:
            if not isinstance(s, dict):
                continue
            if s.get("host") != host:
                continue
            if int(s.get("port", 21)) != port:
                continue
            if s.get("protocol", "").lower() != "ftp":
                continue
            legacy = s.get("ftp_pool")
            if isinstance(legacy, dict):
                try:
                    scan_budget   = max(1, int(legacy.get("scan",   scan_budget)))
                    stream_budget = max(1, int(legacy.get("stream", stream_budget)))
                    browse_budget = max(1, int(legacy.get("browse", browse_budget)))
                except (TypeError, ValueError):
                    pass
            break

    configured_total = scan_budget + stream_budget + browse_budget

    # Detected cap (auto-learned).  Clamp configured total to (cap - 1)
    # so we leave a slot of headroom for any out-of-band probes / health
    # checks that bypass the pool counter.
    detected = None
    try:
        from soniqboom.core import ftp_pool_config as _fcc
        detected = _fcc.get_detected_cap(host, port)
    except Exception:
        pass
    if isinstance(detected, int) and detected > 0:
        max_size = max(1, min(configured_total, detected - 1))
    else:
        max_size = configured_total

    min_size = min(_FTP_POOL_MIN, max_size)
    return max_size, min_size, configured_total, detected


def _get_or_create_ftp_pool(host: str, port: int, username: str,
                            password: str, encoding: str,
                            ) -> _FTPConnectionPool:
    """Look up the pool for these credentials, creating it lazily.

    First creation kicks off the warm-min + keep-alive daemon (when
    ``_FTP_POOL_MIN > 0``) so the FIRST ``borrow()`` doesn't pay a TCP
    handshake — the connections are already established and waiting.
    """
    key = (host, port, username, password, encoding)
    with _FTP_POOLS_LOCK:
        pool = _FTP_POOLS.get(key)
        if pool is None:
            max_size, min_size, configured_total, detected = _resolve_pool_size(host, port)
            if detected is not None and configured_total > max_size:
                log.info(
                    "FTP pool %s:%d configured %d, clamped to %d "
                    "(detected server cap %d, reserving 1 for headroom)",
                    host, port, configured_total, max_size, detected,
                )
            pool = _FTPConnectionPool(
                factory=_build_ftp_factory(host, port, username, password,
                                           encoding),
                max_size=max_size,
                min_size=min_size,
                keepalive_s=_FTP_KEEPALIVE_S,
                label=f"{host}:{port}",
                host=host,
                port=port,
            )
            _FTP_POOLS[key] = pool
        return pool


def reload_ftp_pool_sizes() -> list[dict]:
    """Re-read per-share pool config and resize live pools to match.

    Called by the admin UI after a settings save.  Returns a list of
    ``{label, old, new}`` dicts so the caller can show a confirmation
    toast like "Resized 1 pool: 10.0.0.88:21 6 → 8".
    """
    changes: list[dict] = []
    with _FTP_POOLS_LOCK:
        pools = list(_FTP_POOLS.items())
    for key, pool in pools:
        host, port, *_ = key
        max_size, _min_size, _ct, _det = _resolve_pool_size(host, port)
        old = pool._max_size
        if old != max_size:
            pool.resize(max_size)
            changes.append({"label": pool._label, "old": old, "new": max_size})
    return changes


def list_ftp_pool_status() -> list[dict]:
    """Snapshot every live pool — for the admin UI's status display."""
    with _FTP_POOLS_LOCK:
        pools = list(_FTP_POOLS.values())
    return [p.status() for p in pools]


def _close_all_ftp_pools() -> None:
    """Tear down every FTP pool — registered with atexit so the process
    doesn't leave dangling sockets when uvicorn dies."""
    with _FTP_POOLS_LOCK:
        pools = list(_FTP_POOLS.values())
        _FTP_POOLS.clear()
    for pool in pools:
        pool.close_all()


atexit.register(_close_all_ftp_pools)


# FTP reply codes that genuinely mean "the server does not implement this
# command" — the ONLY condition under which MLSD should be disabled for a
# source.  ``ftplib`` raises ``error_perm`` for EVERY 5xx reply (including 550
# file-not-found and 530 not-logged-in), so an ``isinstance`` check alone is far
# too broad: a single 550 on a bogus path once disabled MLSD share-wide, which
# forced every later listing onto LIST (mtime=0) and broke incremental skip.
# Inspect the leading 3-digit reply code and disable only on 500 / 502.
_MLSD_UNSUPPORTED_CODES = frozenset({"500", "502"})
# A latched source re-probes MLSD after this long, so it can recover from a
# latch (transient 500/502, or a server that later gains MLSD) without a
# process restart.  reconnect() clears the latch immediately; this is the
# fallback for a LIST-degraded-but-reachable source that never reconnects.
#
# Kept SHORT (30 min) deliberately.  A latch degrades every listing to LIST
# (coarse mtimes) and can re-arm a full re-extract storm, so a FALSE latch
# (transient/desynced 500) must not persist for hours.  The only cost on a
# server that genuinely lacks MLSD is one wasted MLSD command per interval —
# it simply re-latches on the first 500/502 after each re-probe (the streak
# below is already ≥ the latch threshold, so recovery is immediate).
_MLSD_REPROBE_S = 30 * 60
# Latch MLSD off only after this many CONSECUTIVE "unsupported" (500/502)
# replies.  A one-off desync/"500 OOPS" on an otherwise MLSD-capable server
# (vsftpd under load, a stale pooled control channel) no longer flips the
# whole source to LIST — a single spurious 500 is absorbed by the LIST
# fallback for that one listing and MLSD is retried on the next.  A server
# that truly lacks MLSD answers 500/502 every time, so it still latches
# within two listings.
_MLSD_LATCH_STREAK = 2


def _mlsd_unsupported(exc: Exception) -> bool:
    """True iff *exc* is an FTP reply that means MLSD itself is unsupported."""
    if not isinstance(exc, ftplib.error_perm):
        return False
    return str(exc).strip()[:3] in _MLSD_UNSUPPORTED_CODES


class _FTPStream(SourceStream):
    """A ``RETR`` (from a ``REST`` offset) read off the data socket.  Holds
    its pooled connection until closed — an early close ``ABOR``s the
    transfer, a desynced control channel retires the connection."""

    def __init__(self, borrow_cm, handle: "_PooledFTP", sock, size: int | None) -> None:
        self.size = size
        self._cm = borrow_cm
        self._handle = handle
        self._sock = sock
        self._eof = False
        self._failed = False
        self._replied = False             # the end-of-transfer reply was read

    def read(self, n: int = 256 * 1024) -> bytes:
        if self._eof or self._sock is None:
            return b""
        try:
            chunk = self._sock.recv(n)
        except BaseException:
            self._failed = True
            raise
        if not chunk:
            # The data socket closing is not proof the file is complete: a
            # server that aborts the transfer (426, 451 …) closes it too.  The
            # control reply decides — an error RAISES, so a download of a file
            # whose size the server never told (no SIZE) can't be cached cut
            # short as if it were whole.
            self._eof = True
            try:
                self._sock.close()
            except Exception:
                pass
            try:
                self._handle.conn.voidresp()    # the 226 "transfer complete"
            except BaseException:
                self._failed = True
                raise
            self._replied = True
        return chunk

    def close(self) -> None:
        cm, self._cm = self._cm, None
        if cm is None:
            return
        handle, sock = self._handle, self._sock
        self._sock = None
        try:
            sock.close()
        except Exception:
            pass
        ftp = handle.conn
        if self._failed:
            handle.mark_broken()
        elif self._eof:
            if not self._replied:
                try:
                    ftp.voidresp()        # the 226 "transfer complete"
                except Exception:
                    handle.mark_broken()
        else:
            handle.abort_transfer()       # stopped early
        handle.note_transfer()
        try:
            cm.__exit__(None, None, None)  # back to the pool (or closed if broken)
        except Exception:
            pass


class FTPFileSource(FileSource):
    """Direct FTP access via stdlib ftplib.

    Connection management is delegated to a per-credential
    ``_FTPConnectionPool`` (see module-level rationale above).  This class
    only holds source-level state — the remote root path, the MLSD-vs-LIST
    preference, and the current encoding — none of which is per-socket.
    """

    def __init__(self, host: str, username: str = "", password: str = "",
                 port: int = 21, remote_path: str = "/"):
        self._host = host
        self._port = port
        self._username = username or "anonymous"
        self._password = password or ""
        self._remote_path = remote_path.rstrip("/") or "/"
        self._use_mlsd: bool = True  # try MLSD first, fall back to LIST
        self._mlsd_disabled_at: float = 0.0  # wall time MLSD was latched off (0 = never); drives the periodic re-probe
        self._mlsd_fail_streak: int = 0  # consecutive 500/502 MLSD replies; latches at _MLSD_LATCH_STREAK
        # One FTPFileSource per share is SHARED across threads (scanner scan
        # lane, interactive browse lane, freshness poller — see _active_sources
        # + the pool's lane split).  The MLSD latch trio above is read-decide-
        # written from all of them; without this lock a torn interleave could
        # leave the inconsistent pair (_use_mlsd=False, _mlsd_disabled_at=0.0),
        # which the re-probe guard would never clear — wedging the share on
        # LIST until process restart.  The lock guards ONLY the small state
        # mutations, never the socket round-trip, so listings still parallelise.
        self._mlsd_lock = threading.Lock()
        self._encoding: str = "utf-8"  # downgraded to latin-1 on decode errors

    @property
    def _pool(self) -> _FTPConnectionPool:
        """Resolve the pool for the current credentials + encoding.

        The encoding is part of the pool key, so flipping it via
        ``_switch_encoding_latin1`` automatically routes future borrows
        to a separate pool (rather than poisoning the existing UTF-8
        sockets).  No lock here — the registry lookup is itself locked.
        """
        return _get_or_create_ftp_pool(
            self._host, self._port, self._username, self._password,
            self._encoding,
        )

    def reconnect(self) -> bool:
        """Recycle every idle pool connection and validate the next acquire.

        Called by the health-check loop in main.py when a previously-healthy
        share starts failing — we want the next ``read_file`` to see a fresh
        socket, not an idle one that's been silently dropped by the server.
        Never raises; returns False on failure so the caller can fall back
        to its own error handling.
        """
        self._pool.recycle_all_idle()
        try:
            # ``scan`` lane: this is a health probe, not user playback.
            with self._pool.borrow(lane="scan"):
                pass
            # A fresh connection is our chance to re-probe MLSD: if the source
            # was latched to LIST by a transient/bogus failure, re-enable
            # structured listing so it can recover.  A server that genuinely
            # lacks MLSD simply re-latches on the next listing (500/502).
            with self._mlsd_lock:
                self._use_mlsd = True
                self._mlsd_disabled_at = 0.0
                self._mlsd_fail_streak = 0
            return True
        except Exception as exc:
            log.info("FTP reconnect to %s:%d failed: %s: %s",
                     self._host, self._port, type(exc).__name__, exc)
            return False

    def _abs(self, path: str) -> str:
        # Reject CR/LF + NUL up front — anything that reaches the FTP
        # command channel verbatim could otherwise be used to inject extra
        # FTP commands (path is later interpolated into ``RETR <path>``,
        # ``CWD <path>``, ``MLSD <path>``).
        if any(c in path for c in ("\r", "\n", "\x00")):
            raise ValueError(
                "FTP path contains illegal control characters (CR/LF/NUL)",
            )
        if path.startswith(self._remote_path):
            return path
        rel = path.lstrip("/")
        return f"{self._remote_path}/{rel}" if rel else self._remote_path

    # ── Directory listing with MLSD → LIST fallback ────────────────────────

    def _switch_encoding_latin1(self) -> None:
        """Switch to latin-1 encoding (can decode any byte).

        With pooled connections we don't ``_reset`` anything — the encoding
        is part of the pool key, so the next ``self._pool`` access lands on
        a fresh latin-1 pool.  The old UTF-8 pool keeps serving any
        in-flight callers and idles down naturally.
        """
        if self._encoding != "latin-1":
            log.info("Non-UTF-8 filenames on %s, switching to latin-1 encoding",
                     self._host)
            self._encoding = "latin-1"

    def _note_mlsd_failure(self, exc: Exception) -> None:
        """Record an MLSD listing error and latch MLSD off ONLY after
        ``_MLSD_LATCH_STREAK`` consecutive "command not implemented"
        (500/502) replies.

        A one-off desync / "500 OOPS" on an otherwise MLSD-capable server
        (vsftpd under load, a stale pooled control channel) is absorbed by
        the per-call LIST fallback for that single listing and MLSD is
        retried on the next — it no longer flips the whole source to LIST
        (which reports coarse mtimes and can re-arm a full re-extract
        storm).  A server that truly lacks MLSD answers 500/502 every time,
        so it still latches within two listings.

        A non-unsupported error (550 bogus path, 530 auth, a borrow
        timeout, a socket error) is NOT evidence MLSD is missing — the
        streak and the latch are left untouched.  See ``_mlsd_unsupported``.
        """
        with self._mlsd_lock:
            if not _mlsd_unsupported(exc):
                # A non-500/502 MLSD error (borrow timeout, socket error, a 550
                # bogus path, 530 auth) is not evidence MLSD is missing AND it
                # breaks the "consecutive" run — reset so only genuinely
                # back-to-back 500/502s (what a truly MLSD-less server emits
                # every time) latch, not a 500 / socket-blip / 500 sequence.
                self._mlsd_fail_streak = 0
                return
            self._mlsd_fail_streak += 1
            if self._mlsd_fail_streak >= _MLSD_LATCH_STREAK:
                # Latch as a CONSISTENT pair: whenever _use_mlsd is False,
                # _mlsd_disabled_at is a real timestamp so the re-probe fires.
                self._use_mlsd = False
                self._mlsd_disabled_at = time.time()

    def _mark_mlsd_ok(self) -> None:
        """A successful MLSD listing → clear the latch as a CONSISTENT trio
        (_use_mlsd=True, streak=0, disabled_at=0).  Re-asserting _use_mlsd=True
        here (not merely clearing the counters) means every critical section
        leaves a VALID pair, so a concurrent latch/success race can only
        resolve to one of the two consistent states — never the wedged
        (_use_mlsd=False, _mlsd_disabled_at=0) pair."""
        with self._mlsd_lock:
            self._use_mlsd = True
            self._mlsd_fail_streak = 0
            self._mlsd_disabled_at = 0.0

    def _list_entries(self, path: str, lane: str = "scan", *,
                      error_sink: "list | None" = None) -> list[DirEntry]:
        # ``lane`` selects the pool bucket: ``"scan"`` (default) for bulk
        # scanner walks; ``"stream"`` for INTERACTIVE folder browsing so a
        # running scan (which saturates the scan lane) can't starve the file
        # browser.  The symptom was a remote folder rendering EMPTY mid-scan:
        # the listing borrow timed out on the contended scan lane and
        # fstree's _remote_list_children swallowed the exception → [].
        #
        # ``error_sink`` (optional): a listing that HARD-FAILS (borrow
        # timeout, socket error, or an FTP error that isn't a genuinely-empty
        # dir) still returns [] here — an interactive browse must render
        # empty, not crash — but it ALSO appends ``(abs_path, reason)`` to
        # ``error_sink`` so a WALK can tell "this dir failed to list" apart
        # from "this dir is genuinely empty".  Without that signal a single
        # transient per-directory failure during a full / drift-sweep walk
        # makes the whole subtree look deleted, and ghost cleanup purges
        # every track under it (silent subtree data loss — QA finding).
        #
        # Archive-internal virtual paths ("archive.zip::member/…") are NOT
        # real FTP directories — the server answers 550 for them.  That 550
        # must never reach the MLSD-unsupported check below: a single bogus 550
        # once disabled MLSD for the WHOLE source, degrading every subsequent
        # listing to LIST (which reports mtime=0) and breaking incremental
        # scan-skip.  Archive contents are enumerated by the archive layer,
        # never by FTP LIST.
        if "::" in path:
            # Refuse to FTP-list only GENUINE archive / disk-image interiors.
            # A benign user directory that merely CONTAINS "::" in its name
            # (e.g. "Artist :: Album", "Live :: 1998") IS a real, listable
            # server directory and must NOT be short-circuited — doing so
            # makes its whole subtree invisible in the browser and unscanned
            # by the indexer (every track inside silently missing).
            from soniqboom.core import archive as _archive
            from soniqboom.core import diskimage as _diskimage
            outer = path.split("::", 1)[0]
            if _archive.is_archive_name(outer) or _diskimage.is_disk_image(outer):
                return []
        abs_path = self._abs(path)

        # Periodic re-probe (under the latch lock): a source latched to LIST is
        # given MLSD another try after _MLSD_REPROBE_S, so a transient/bogus
        # latch recovers without a restart.  A torn concurrent write could also
        # leave the inconsistent pair (_use_mlsd=False, _mlsd_disabled_at<=0);
        # treat that as "re-probe due" so it self-heals instead of wedging on
        # LIST forever.  ``use_mlsd`` is snapshotted here and drives THIS call;
        # the socket round-trip below runs WITHOUT the lock held so concurrent
        # listings on other lanes still parallelise.
        with self._mlsd_lock:
            if not self._use_mlsd and (
                self._mlsd_disabled_at <= 0.0
                or time.time() - self._mlsd_disabled_at > _MLSD_REPROBE_S
            ):
                self._use_mlsd = True
            use_mlsd = self._use_mlsd

        # Prefer MLSD (structured output, RFC 3659)
        if use_mlsd:
            try:
                entries = self._list_via_mlsd(abs_path, lane)
                self._mark_mlsd_ok()          # success → consistent "MLSD on" state
                return entries
            except UnicodeDecodeError:
                self._switch_encoding_latin1()
                try:
                    entries = self._list_via_mlsd(abs_path, lane)
                    self._mark_mlsd_ok()
                    return entries
                except Exception as exc:
                    self._note_mlsd_failure(exc)
                    log.info("MLSD failed on %s (%s), using LIST fallback",
                             self._host, exc)
            except Exception as exc:
                # Latch decision is delegated to _note_mlsd_failure: disable
                # MLSD only on repeated genuine "command unrecognized / not
                # implemented" (FTP 500/502).  Any other error_perm (550 bogus
                # path, 530 auth), a borrow-timeout, or a socket error is NOT
                # evidence the server lacks MLSD.
                self._note_mlsd_failure(exc)
                log.info("MLSD not supported on %s (%s), using LIST fallback",
                         self._host, exc)

        # Fallback: LIST (universally supported).  On error the pool's borrow()
        # already marks the connection broken and discards it, so there is
        # nothing to reset here (the old self._reset() was removed in the
        # pooled-connection refactor and would AttributeError).  A hard failure
        # is recorded in ``error_sink`` as a ROOT-RELATIVE dir path so the
        # scanner can protect exactly that subtree's tracks from ghost cleanup.
        try:
            return self._list_via_list(abs_path, lane)
        except UnicodeDecodeError:
            self._switch_encoding_latin1()
            try:
                return self._list_via_list(abs_path, lane)
            except Exception as exc:
                log.warning("FTP LIST failed for %s: %s", abs_path, exc)
                if error_sink is not None:
                    error_sink.append((self._rel(abs_path), str(exc)))
                return []
        except Exception as exc:
            log.warning("FTP LIST failed for %s: %s", abs_path, exc)
            if error_sink is not None:
                error_sink.append((self._rel(abs_path), str(exc)))
            return []

    def _list_via_mlsd(self, abs_path: str, lane: str = "scan") -> list[DirEntry]:
        """List a directory via MLSD (RFC 3659 — structured output)."""
        entries: list[DirEntry] = []
        with self._pool.borrow(lane=lane) as handle:
            ftp = handle.conn
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

    def _list_via_list(self, abs_path: str, lane: str = "scan") -> list[DirEntry]:
        """List a directory via LIST (universal fallback)."""
        with self._pool.borrow(lane=lane) as handle:
            ftp = handle.conn
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

        The date/time columns are parsed to a real mtime where possible.
        A genuinely MLSD-less server (LIST is the ONLY listing path) would
        otherwise report mtime=0 for every file, defeating the scanner's
        incremental ``(mtime, size)`` skip and re-downloading the whole
        share on every poll — the exact multi-minute full-re-index symptom
        the MLSD path avoids.  LIST dates are coarse (minute precision,
        year sometimes omitted) and locale-dependent, so parsing is
        best-effort: an unparseable date yields ``0.0`` (identical to the
        old behaviour — no regression, just no incremental win for that
        line), while a parsed date is stable across polls of the same
        server so skip works from the second LIST scan onward.
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
            mtime = FTPFileSource._parse_list_mtime_unix(
                parts[5], parts[6], parts[7])
            entry_path = f"{parent}/{name}" if parent != "/" else f"/{name}"
            return DirEntry(name=name, path=entry_path, is_dir=is_dir,
                            size=size, mtime=mtime)

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
            mtime = FTPFileSource._parse_list_mtime_windows(wparts[0], wparts[1])
            entry_path = f"{parent}/{name}" if parent != "/" else f"/{name}"
            return DirEntry(name=name, path=entry_path, is_dir=is_dir,
                            size=size, mtime=mtime)

        return None

    _LIST_MONTHS = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    @staticmethod
    def _parse_list_mtime_unix(mon: str, day: str, year_or_time: str) -> float:
        """Parse the ``MMM DD (HH:MM | YYYY)`` columns of a Unix LIST line
        into a Unix timestamp; 0.0 if unparseable.

        ``ls`` omits the year for files newer than ~6 months (showing
        ``HH:MM`` instead) and shows ``YYYY`` (no time) once older.  For the
        recent form we keep the real minute-precision time and infer the
        year (this year, or last year if that would fall in the future —
        ``ls``'s own rule).

        Irreducible LIST tradeoff (deliberate choice, see the two review
        rounds): the recent form carries a time, the aged form does not, so
        ONE representation can't be stable across the ~6-month display switch
        AND detect a same-day re-tag.  We keep the time here, which means:
          * a same-day, same-size re-tag is DETECTED while the file is still
            recent (its minute changes → re-extract), and
          * a file re-extracts exactly ONCE as it ages past ~6 months (its
            stored HH:MM no longer matches the aged midnight), after which the
            stored value is the aged midnight and stays stable.
        The alternative (store midnight always) is stable across the switch
        but SILENTLY misses same-size same-day re-tags forever — a permanent
        correctness miss traded for avoiding a bounded, visible, self-
        correcting re-extract.  We prefer the visible re-extract.  (Aged
        same-size re-tags are undetectable from LIST regardless — the aged
        form has no time; that needs MDTM/MLSD.)  All of this is moot on an
        MLSD-capable server, which never reaches LIST.
        """
        import datetime
        mon_i = FTPFileSource._LIST_MONTHS.get(str(mon)[:3].lower())
        if mon_i is None:
            return 0.0
        try:
            day_i = int(day)
        except (ValueError, TypeError):
            return 0.0
        now = datetime.datetime.now()
        try:
            if ":" in year_or_time:
                hh, mm = year_or_time.split(":", 1)
                year = now.year
                cand = datetime.datetime(year, mon_i, day_i, int(hh), int(mm))
                if cand.timestamp() > now.timestamp() + 86400:
                    cand = cand.replace(year=year - 1)
                dt = cand
            else:
                dt = datetime.datetime(int(year_or_time), mon_i, day_i)
            return dt.timestamp()
        except (ValueError, OSError, OverflowError):
            return 0.0

    @staticmethod
    def _parse_list_mtime_windows(date_s: str, time_s: str) -> float:
        """Parse a Windows/IIS LIST ``MM-DD-YY HH:MM(AM|PM)`` date into a
        Unix timestamp; 0.0 if unparseable."""
        import datetime
        stamp = f"{date_s} {time_s}"
        for fmt in ("%m-%d-%y %I:%M%p", "%m-%d-%Y %I:%M%p",
                    "%m-%d-%y %H:%M", "%m-%d-%Y %H:%M"):
            try:
                return datetime.datetime.strptime(stamp, fmt).timestamp()
            except (ValueError, OSError, OverflowError):
                continue
        return 0.0

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

    def walk_with_stat(
        self, top: str, *,
        skip_subtree_fn: "Callable[[DirEntry], bool] | None" = None,
        error_sink: "list | None" = None,
    ) -> Iterator[tuple[str, list[DirEntry], list[DirEntry]]]:
        """FTP-specific walk that preserves the size+mtime MLSD already
        returns in the listing response.

        ``skip_subtree_fn`` (optional) is called on every dir entry
        before we'd recurse into it.  Returning ``True`` prunes the
        subtree — used by the freshness loop with a dir-mtime cap.

        ``error_sink`` (optional): threaded into ``_list_entries`` so a
        per-directory listing that HARD-FAILS (borrow timeout / socket /
        5xx that isn't an empty dir) is recorded even though the walk
        continues.  A non-empty sink means the walk was PARTIAL — the
        caller must suppress ghost cleanup, or a transiently-unreadable
        subtree gets purged as if its files were deleted.

        Why override the base bridge implementation: MLSD returns size
        and mtime in the SAME response as the directory listing — one
        round-trip per directory.  The base bridge would call
        ``list_dir`` and then a separate ``stat`` per file (two extra
        round trips per file for SIZE and MDTM), which would defeat
        the whole point of mtime-skip on large remote libraries.

        Yields ``(rel_dirpath, dir_entries, file_entries)`` where the
        ``DirEntry.path`` on every yielded entry is **root-relative**
        (i.e. ``/REOL/foo.flac``, not ``/Music/Music FLAC/REOL/foo.flac``).

        Why this matters: tracks are stored in the index as
        ``ftp://host/share:/relative-path``.  The scanner builds those
        URLs by appending ``f"{scan_root}:{entry.path}"``.  If
        ``entry.path`` were absolute, the share segment would be
        duplicated (``ftp://host/share:/share/REOL/foo.flac``),
        producing a NEW track id (uuid5 over the malformed path) that
        DOESN'T match the existing stored row — silently duplicating
        every file in the index on every re-scan.  A previous pass of
        this scanner ran with absolute paths and created exactly that
        damage; the cure is to ensure every entry yielded here passes
        through ``self._rel()`` first.
        """
        abs_top = self._abs(top)
        stack = [abs_top]
        while stack:
            current = stack.pop()
            try:
                entries = self._list_entries(current, error_sink=error_sink)
            except Exception as exc:
                log.warning("walk_with_stat: _list_entries(%s) failed: %s",
                            current, exc)
                if error_sink is not None:
                    # Record ROOT-RELATIVE (same frame _list_entries uses and
                    # the scanner's ghost keys are in) so per-subtree ghost
                    # protection actually matches this failed directory.
                    error_sink.append((self._rel(current), str(exc)))
                continue
            dir_entries: list[DirEntry] = []
            file_entries: list[DirEntry] = []
            for e in entries:
                # Normalise entry.path from FTP-absolute to scan-root-
                # relative.  ``_list_via_mlsd`` produced paths like
                # ``/Music/Music FLAC/REOL/foo.flac``; we want
                # ``/REOL/foo.flac`` so the scanner can build the
                # canonical ``ftp://host/share:/REOL/foo.flac`` URL.
                rel_entry = DirEntry(
                    name=e.name,
                    path=self._rel(e.path),
                    is_dir=e.is_dir,
                    size=e.size,
                    mtime=e.mtime,
                )
                if e.is_dir:
                    dir_entries.append(rel_entry)
                else:
                    file_entries.append(rel_entry)
            yield self._rel(current), dir_entries, file_entries
            # Recurse: stack holds ABSOLUTE paths (what _list_entries
            # expects).  Translate each dir's now-relative path back
            # via ``_abs`` so the next iteration's _list_entries call
            # has what it needs.
            for d in dir_entries:
                if skip_subtree_fn is not None and skip_subtree_fn(d):
                    continue
                stack.append(self._abs(d.path))

    def list_dir(self, path: str) -> list[DirEntry]:
        # Interactive file-browser listing → "browse" lane, NOT "scan" or "stream".
        # A running scan saturates the scan lane; a cold remote render holds the
        # stream lane for the entire file RETR. The dedicated browse lane keeps
        # folder listing responsive without waiting behind bulk work or playback.
        # The scanner's own walk_with_stat still calls _list_entries() with "scan".
        return self._list_entries(path, lane="browse")

    def read_file(self, path: str, *, lane: str = "stream") -> bytes:
        # ``lane`` is forwarded to ``_pool.borrow`` so the caller can
        # pick its priority bucket:
        #   - ``"scan"`` — bulk re-index work.  Uses the larger scan
        #     budget (default 6) so the scanner runs in parallel and
        #     finishes in a reasonable time.  Earlier code hard-coded
        #     ``stream`` here, which meant every download took a
        #     stream slot (default 2) and the scanner was effectively
        #     2-wide — exactly the "almost like it's single threaded"
        #     symptom reported with in_use=1, idle=4 in the pool stats.
        #   - ``"stream"`` (default) — playback / waveform fetch.
        # ftplib sends TYPE I before each retrbinary.  After many rapid
        # downloads the control-channel responses can desynchronise,
        # causing "200 Type set to I" errors.  The pool recycles
        # connections at ``_FTP_MAX_PER_CONN`` transfers via the per-handle
        # counter we bump below — so each pooled socket caps out cleanly
        # without us juggling a global ``_xfer_count``.
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                buf = io.BytesIO()
                with self._pool.borrow(lane=lane) as handle:
                    handle.conn.retrbinary(
                        f"RETR {self._abs(path)}", buf.write,
                    )
                    # Mark the transfer so the pool can recycle this socket
                    # on return when it crosses the per-conn ceiling.  Note:
                    # the ``with`` block exits NORMALLY here, so the handle
                    # goes back to the idle queue (or gets closed if the
                    # counter is now ≥ _FTP_MAX_PER_CONN — pool decides).
                    handle.note_transfer()
                return buf.getvalue()
            except Exception as exc:
                # The ``with`` already marked the handle broken via the
                # exception path, so the next borrow gets a fresh socket.
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.3)     # brief pause before retry
        raise last_exc  # type: ignore[misc]

    def read_partial(
        self, path: str, max_bytes: int, *, lane: str = "scan",
    ) -> bytes:
        """Fetch only the first ``max_bytes`` of the remote file.

        Used by the scanner to read just the tag header (FLAC: ~256 KB,
        MP3: ~64 KB, chiptune: a few KB) instead of the whole 50 MB
        audio file.  A 50 MB FLAC payload contains ~10 KB of useful
        metadata at the start; transferring the rest is pure waste.

        Implementation:
          * Open a data channel via ``transfercmd("RETR …")`` — same as
            ``retrbinary`` does internally, but we hold the socket
            ourselves so we can stop reading after ``max_bytes``.
          * Read up to ``max_bytes`` (or EOF, whichever first).
          * If we stopped early, send ``ABOR`` so the server discards
            the remaining bytes and the control channel returns to
            "command" state.  If ABOR fails (some servers handle it
            poorly), mark the handle broken and let the pool recycle
            it — losing one connection beats a desynced control
            channel that corrupts the NEXT transfer.

        Returns the actual bytes fetched.  May be shorter than
        ``max_bytes`` for small files.

        The default value for ``lane`` is ``"scan"`` because the only
        current caller is the indexer — partial fetch isn't useful for
        playback (which needs the full file).
        """
        if max_bytes <= 0:
            return b""

        last_exc: Exception | None = None
        for attempt in range(3):
            handle = None
            try:
                with self._pool.borrow(lane=lane) as handle:
                    ftp = handle.conn
                    ftp.voidcmd("TYPE I")
                    abs_path = self._abs(path)
                    # ``transfercmd`` returns the data socket and sends
                    # the RETR command.  The control-channel reply
                    # (150/125) is consumed inside transfercmd.
                    data_sock = ftp.transfercmd(f"RETR {abs_path}")
                    try:
                        buf = bytearray()
                        chunk_size = 64 * 1024
                        eof = False
                        while len(buf) < max_bytes:
                            want = min(chunk_size, max_bytes - len(buf))
                            chunk = data_sock.recv(want)
                            if not chunk:
                                eof = True
                                break
                            buf.extend(chunk)
                    finally:
                        # Always close the data socket BEFORE handling
                        # the control side; the server signals end-of-
                        # transfer on data-channel close.
                        try:
                            data_sock.close()
                        except Exception:
                            pass

                    if eof:
                        # Whole file fit in the budget — server already
                        # closed the data socket, just consume the 226
                        # "transfer complete" reply.
                        try:
                            ftp.voidresp()
                        except Exception:
                            # Desynced control channel — burn the
                            # handle so the pool replaces it.
                            handle.mark_broken()
                    else:
                        # We stopped early.  Send ABOR and resync the
                        # control channel (its replies come in pairs — see
                        # ``abort_transfer``); on failure we burn the
                        # handle and let the pool open a fresh one.
                        handle.abort_transfer()

                    handle.note_transfer()
                return bytes(buf)
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.3)
        raise last_exc  # type: ignore[misc]

    def read_at(
        self, path: str, offset: int, length: int, *, lane: str = "scan",
    ) -> bytes:
        """Read ``length`` bytes from byte ``offset`` via FTP ``REST``.

        Seeks with ``transfercmd(..., rest=offset)`` (a ``REST`` command before
        ``RETR``), then reads exactly ``length`` bytes — so the art backfill
        can grab an MP4 ``moov`` atom from the END of a 50 MB file without
        transferring the audio in between.  Mirrors ``read_partial``'s
        control-channel discipline (early ``ABOR``, handle recycling on
        desync).
        """
        if length <= 0:
            return b""
        offset = max(0, int(offset))
        last_exc: Exception | None = None
        for attempt in range(3):
            handle = None
            try:
                with self._pool.borrow(lane=lane) as handle:
                    ftp = handle.conn
                    ftp.voidcmd("TYPE I")
                    abs_path = self._abs(path)
                    data_sock = ftp.transfercmd(f"RETR {abs_path}", rest=offset)
                    try:
                        buf = bytearray()
                        chunk_size = 64 * 1024
                        eof = False
                        while len(buf) < length:
                            want = min(chunk_size, length - len(buf))
                            chunk = data_sock.recv(want)
                            if not chunk:
                                eof = True
                                break
                            buf.extend(chunk)
                    finally:
                        try:
                            data_sock.close()
                        except Exception:
                            pass
                    if eof:
                        try:
                            ftp.voidresp()
                        except Exception:
                            handle.mark_broken()
                    else:
                        handle.abort_transfer()
                    handle.note_transfer()
                return bytes(buf)
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.3)
        raise last_exc  # type: ignore[misc]

    def open_stream(self, path: str, *, offset: int = 0, lane: str = "stream",
                    length: int | None = None) -> SourceStream:
        """``SIZE`` + ``RETR`` (after ``REST offset``) on ONE pooled connection
        of ``lane``, held until the stream is closed — the pool's caps count
        it like any other transfer."""
        import sys as _sys
        offset = max(0, int(offset))
        last_exc: Exception | None = None
        for attempt in range(2):
            cm = self._pool.borrow(lane=lane)
            handle = cm.__enter__()
            try:
                ftp = handle.conn
                ftp.voidcmd("TYPE I")
                abs_path = self._abs(path)
                try:
                    size: int | None = ftp.size(abs_path)
                except (ftplib.error_perm, ftplib.error_reply, ftplib.error_temp):
                    size = None        # SIZE unsupported — length unknown
                sock = ftp.transfercmd(f"RETR {abs_path}", rest=offset or None)
                return _FTPStream(cm, handle, sock, size)
            except Exception as exc:
                cm.__exit__(*_sys.exc_info())   # marks the handle broken, releases it
                last_exc = exc
                if isinstance(exc, ftplib.error_perm) or attempt:
                    raise
                time.sleep(0.3)
        raise last_exc  # type: ignore[misc]  # pragma: no cover

    def stat(self, path: str, *, lane: str = "scan", strict: bool = False) -> FileStat:
        """``SIZE`` (or ``CWD`` for a folder).  Any failure reads as an empty
        ``FileStat()`` — unless ``strict``: then a failure to ASK (no
        connection, a refused login, a timeout, a broken control channel)
        raises, while a server that answers but can't tell — no ``SIZE``, or
        both ``SIZE`` and ``CWD`` refused (a missing path too) — still gives
        the empty ``FileStat()`` (size 0 = unknown)."""
        try:
            abs_path = self._abs(path)
            # ``scan`` lane by default — SIZE/CWD probes are scan-grade work;
            # playback passes ``lane="stream"``.
            with self._pool.borrow(lane=lane) as handle:
                ftp = handle.conn
                try:
                    # Binary mode first: vsftpd / ProFTPD answer SIZE in ASCII
                    # mode (a fresh connection's default) with 550.
                    ftp.voidcmd("TYPE I")
                except ftplib.error_perm:
                    pass
                try:
                    size = ftp.size(abs_path)
                    return FileStat(size=size or 0, mtime=0.0, is_dir=False)
                except ftplib.error_perm:
                    try:
                        ftp.cwd(abs_path)
                    except ftplib.error_perm:
                        if strict:
                            return FileStat()     # the server can't tell
                        raise
                    return FileStat(size=0, mtime=0.0, is_dir=True)
        except Exception:
            if strict:
                raise
            return FileStat()

    def is_dir(self, path: str) -> bool:
        self.last_error_auth = False
        try:
            with self._pool.borrow(lane="scan") as handle:
                handle.conn.cwd(self._abs(path))
            return True
        except Exception as exc:
            # Log at WARNING so connection failures surface in the log.
            # OSError / socket.gaierror / EHOSTUNREACH commonly means macOS
            # has silently blocked the connection because the bundle lacks
            # NSLocalNetworkUsageDescription.  ftplib.error_perm means auth
            # failed.  error_reply / error_temp mean the server misbehaved.
            log.warning(
                "FTP is_dir(%s) on %s:%d failed: %s: %s",
                path, self._host, self._port, type(exc).__name__, exc,
            )
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.last_error_auth = is_auth_failure(exc)
            return False

    def close(self) -> None:
        """No-op: pooled connections are shared across sources of the same
        credentials, so a single source closing doesn't tear down the pool.
        Process-wide teardown is the atexit-registered
        ``_close_all_ftp_pools``."""

    def force_close(self) -> None:
        """Recycle the idle pool connections without touching in-flight ones.

        Preserves the legacy semantic of "drop sockets fast, don't wait for
        a QUIT/221 round-trip" but at the pool layer instead of an
        individual source's single socket."""
        self._pool.recycle_all_idle()


# ── Factory ─────────────────────────────────────────────────────────────────

_active_sources: dict[str, FileSource] = {}


def create_source(share: dict, password: str = "") -> FileSource:
    proto = share.get("protocol", "").lower()
    src: FileSource
    if proto == "smb":
        src = SMBFileSource(
            host=share["host"], share=share["share"],
            username=share.get("username", ""), password=password,
            port=share.get("port") or 445,
        )
    elif proto == "ftp":
        src = FTPFileSource(
            host=share["host"], username=share.get("username", ""),
            password=password, port=share.get("port") or 21,
            remote_path=share.get("remote_path", "/"),
        )
    elif proto in ("webdav", "webdavs", "http", "https"):
        # WebDAV / Nextcloud / ownCloud / generic HTTP mounts.  Imported
        # lazily so the module doesn't fail to load when httpx isn't
        # installed yet (it's a top-level dep, but tests import filesource
        # before app boot completes).
        from soniqboom.core.filesource_webdav import WebDAVFileSource
        src = WebDAVFileSource(
            base_url=scan_root_for_share(share),
            username=share.get("username", ""), password=password,
            verify_ssl=bool(share.get("verify_ssl", True)),
        )
    else:
        raise ValueError(f"Unsupported protocol: {proto}")
    src.auth_key = share_auth_key(share, password)
    return src


def register_source(share_id: str, source: FileSource) -> None:
    old = _active_sources.get(share_id)
    if old is not None:
        old.close()
    _active_sources[share_id] = source


def get_source(share_id: str) -> FileSource | None:
    """The registered source of a scan root — None when there is none, and
    while the server's refusal of its credentials is backing off
    (``note_share_auth_failure``): plays, browsing, background readers and
    the freshness poller then treat the share as offline (cached bytes still
    play) instead of logging in again.  ``all_sources`` still lists it."""
    src = _active_sources.get(share_id)
    if src is not None and share_retry_wait(getattr(src, "auth_key", None)) > 0:
        return None
    return src


def credentials_refused(share_id: str) -> bool:
    """A share whose credentials are backing off (see above) — registered,
    or refused before it ever connected."""
    src = _active_sources.get(share_id)
    key = getattr(src, "auth_key", None) if src is not None else None
    if key is None:
        with _share_backoff_lock:
            key = _share_backoff_roots.get(share_id)
    return share_retry_wait(key) > 0


def remove_source(share_id: str) -> None:
    src = _active_sources.pop(share_id, None)
    if src is not None:
        src.close()


def all_sources() -> dict[str, FileSource]:
    return dict(_active_sources)


# ── Retry back-off for credentials the server refused ───────────────────────
#
# The health monitor retries configured shares that aren't connected every
# 60–300 s.  For a share whose server REFUSED the login that is a failed login
# per pass, forever — exactly what trips fail2ban, a Nextcloud brute-force
# throttle or an account lockout.  Refused credentials (keyed by
# ``share_auth_key``: every share logging in to that server as that user)
# wait 5 min, doubling per refusal, at most 6 h — no connect, no health probe,
# no play login meanwhile; a manual Reconnect or re-adding the share resets
# it.
_SHARE_AUTH_BACKOFF_S = 300.0
_SHARE_AUTH_BACKOFF_MAX_S = 6 * 3600.0
_share_backoff: dict[str, tuple[int, float]] = {}   # share_auth_key → (refusals, retry-at)
_share_backoff_lock = threading.Lock()


_share_backoff_roots: dict[str, str] = {}   # scan root → its share_auth_key (refused ones)


def note_share_auth_failure(key: str, scan_root: str | None = None) -> float:
    """Record a refused login for *key* (``share_auth_key``) — seen on
    *scan_root*; returns the wait (seconds) before anything logs in with
    those credentials again."""
    with _share_backoff_lock:
        n = _share_backoff.get(key, (0, 0.0))[0] + 1
        delay = min(_SHARE_AUTH_BACKOFF_MAX_S,
                    _SHARE_AUTH_BACKOFF_S * (2 ** min(n - 1, 16)))
        _share_backoff[key] = (n, time.monotonic() + delay)
        if scan_root:
            _share_backoff_roots[scan_root] = key
    return delay


def share_retry_wait(key: str | None) -> float:
    """Seconds until credentials *key* may be tried again (0 = now)."""
    if not key:
        return 0.0
    with _share_backoff_lock:
        got = _share_backoff.get(key)
    return max(0.0, got[1] - time.monotonic()) if got else 0.0


def reset_share_backoff(key: str | None) -> None:
    with _share_backoff_lock:
        _share_backoff.pop(key, None)


def source_auth_key(source) -> str | None:
    """The ``share_auth_key`` a source was created with (None: a local or
    hand-made source)."""
    return getattr(source, "auth_key", None)


def find_source_for_path(path: str) -> tuple[str, str, FileSource] | None:
    """Find the source whose scan_root is a prefix of *path*.

    Returns ``(scan_root, remote_subpath, source)`` or ``None``.  When roots
    nest (two WebDAV shares, one inside the other) the longest one wins.
    """
    best: tuple[str, str, FileSource] | None = None
    for scan_root, source in list(_active_sources.items()):
        # Normalise trailing slashes so "ftp://host/dir/" matches
        # "ftp://host/dir/subdir" without producing a double-slash.
        root = scan_root.rstrip("/")
        if path == scan_root or path.rstrip("/") == root or path.startswith(root + "/"):
            if best is None or len(root) > len(best[0].rstrip("/")):
                best = (scan_root, path[len(root):] or "/", source)
    return best
