# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""WebDAV FileSource — Nextcloud / ownCloud / Apache / generic WebDAV servers.

Implementation uses raw ``httpx`` to speak the standard WebDAV verbs
(PROPFIND, GET with ``Range``) instead of pulling in another library — httpx
is already a top-level dependency.

Auth: HTTP Basic (over HTTPS for ``webdavs``).  Token auth (Nextcloud app
passwords) is served as username + token over Basic too, so this works for
both.

Paths: like the SMB and FTP sources, every path this source takes and returns
is ROOT-RELATIVE (``/Artist/Album/01.flac``) — relative to the share's base
URL, never an absolute URL.  The scanner stores tracks as
``<base_url>:<path>``, the folder tree maps ``<base_url>/<rel>`` back through
``find_source_for_path``, and the remote cache keys on the same path, so a
WebDAV share behaves exactly like the other two.

Why no ``async`` here?  The FileSource ABC is synchronous because the scanner
walks libraries inside a thread pool (``asyncio.to_thread``).  We use
``httpx.Client`` (the sync flavour, safe to share between threads)
accordingly.
"""
from __future__ import annotations

import logging
import re
# Use defusedxml so a malicious WebDAV peer can't DoS the scanner via
# billion-laughs / entity-expansion attacks against PROPFIND responses
# (pen-test #2 P0-2).  Stdlib ``xml.etree.ElementTree`` is documented
# vulnerable to these.  Only the *parser* changes.
try:
    from defusedxml.ElementTree import fromstring as _safe_fromstring
except ImportError:
    # Fallback at import-time only; the parser path falls through to the
    # stdlib (with the documented risk).
    from xml.etree.ElementTree import fromstring as _safe_fromstring
from typing import Iterator
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from soniqboom.core.filesource import (
    DirEntry, FileSource, FileStat, SourceStream, is_auth_failure,
)

log = logging.getLogger(__name__)

# Standard WebDAV namespace prefix — every Apache mod_dav / Nextcloud /
# ownCloud server uses this.  We don't bother negotiating Content-Type.
_DAV_NS = "{DAV:}"
_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<propfind xmlns="DAV:">'
      '<prop>'
        '<getcontentlength/>'
        '<getlastmodified/>'
        '<resourcetype/>'
      '</prop>'
    '</propfind>'
)
_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")
_CHUNK = 256 * 1024


def _parse_http_date(s: str) -> float:
    """Parse an RFC 1123 date (``Tue, 15 Nov 2024 10:00:00 GMT``) into a
    Unix timestamp.  Falls back to 0.0 if anything's off."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _http_reason(exc: BaseException) -> str:
    """A short, user-facing reason for a failed request (shown by the share
    Test / Connect buttons and logged by the health monitor)."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        hint = {
            401: " — check the user name and password",
            403: " — the account may not read this folder",
            404: " — check the URL's path",
            405: " — the URL may not be a WebDAV folder",
        }.get(code, "")
        return f"HTTP {code} {exc.response.reason_phrase}{hint}"
    if isinstance(exc, httpx.ConnectError):
        msg = str(exc) or type(exc).__name__
        if "CERTIFICATE_VERIFY_FAILED" in msg:
            return f"TLS certificate not trusted ({msg})"
        return f"cannot connect ({msg})"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    return f"{type(exc).__name__}: {exc}"


class _WebDAVStream(SourceStream):
    """One ``GET`` (optionally from an offset) read sequentially."""

    def __init__(self, cm, response: httpx.Response, size: int | None, skip: int) -> None:
        self.size = size
        self._cm = cm
        self._resp = response
        encoded = response.headers.get("content-encoding", "identity").lower()
        self._iter = (response.iter_raw(_CHUNK) if encoded in ("", "identity")
                      else response.iter_bytes(_CHUNK))
        self._buf = b""
        self._skip = skip            # server ignored Range: discard the prefix

    def read(self, n: int = _CHUNK) -> bytes:
        while True:
            if self._buf:
                out, self._buf = self._buf[:n], self._buf[n:]
                return out
            try:
                chunk = next(self._iter)
            except StopIteration:
                return b""
            if self._skip:
                drop = min(self._skip, len(chunk))
                self._skip -= drop
                chunk = chunk[drop:]
            self._buf = chunk

    def close(self) -> None:
        cm, self._cm = self._cm, None
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass


class WebDAVFileSource(FileSource):
    """WebDAV-over-HTTP(S) filesystem source.

    ``base_url`` points at the WebDAV folder that is the share's root
    (``https://cloud.example.com/remote.php/dav/files/alice/Music`` for
    Nextcloud, ``https://dav.example.com`` for a server root).
    """

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        verify_ssl: bool = True,
        timeout: float = 30.0,
    ) -> None:
        # Normalise: always end with a slash so relative requests and
        # ``urljoin`` resolve UNDER the base folder.
        if not base_url.endswith("/"):
            base_url += "/"
        self._base = base_url
        parts = urlsplit(base_url)
        self._origin = (parts.scheme.lower(), parts.netloc.lower())
        # Decoded absolute path of the base folder ("/remote.php/dav/files/u/Music/").
        self._base_path = unquote(parts.path or "/")
        self._auth = httpx.BasicAuth(username, password) if username else None
        self._verify = verify_ssl
        self._timeout = httpx.Timeout(connect=5.0, read=timeout, write=timeout, pool=5.0)
        self._client = self._new_client()
        # Why the last ``is_dir`` probe failed — surfaced by the share Test /
        # Connect buttons instead of a bare "root not accessible".
        self.last_error: str | None = None

    def _new_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._base, auth=self._auth, verify=self._verify,
            timeout=self._timeout, follow_redirects=True,
        )

    # ── Helpers ─────────────────────────────────────────────────────────

    def _rel(self, path: str) -> str:
        """Root-relative path (or a legacy absolute URL under ``base_url``) →
        the decoded path relative to the base folder, without a leading
        slash ("" for the root)."""
        if path.startswith(self._base):
            path = unquote(path[len(self._base):])
        return path.lstrip("/")

    def _url(self, path: str, *, collection: bool = False) -> str:
        """Request URL (relative to ``base_url``) for a root-relative path."""
        rel = self._rel(path)
        if collection:
            rel = rel.rstrip("/")
            rel = f"{rel}/" if rel else ""
        # ``safe="/"``: ``?``/``#``/``%``/``:`` in names are data, not syntax.
        return quote(rel, safe="/")

    def _href_path(self, href: str, request_url: str) -> str | None:
        """Decoded absolute server path of a PROPFIND ``href`` — or None for
        an href on ANOTHER origin.  Such an entry is skipped, never followed:
        re-issuing requests (with our Basic credentials) to a peer-chosen
        origin would be an SSRF / credential leak (pen-test #2 P1-3)."""
        full = urljoin(urljoin(self._base, request_url), href.strip())
        target = urlsplit(full)
        if (target.scheme.lower(), target.netloc.lower()) != self._origin:
            log.warning(
                "WebDAV server returned a cross-host href (%s://%s) — skipped",
                target.scheme, target.netloc,
            )
            return None
        return unquote(target.path)

    def _propfind(self, url: str, depth: str) -> list[dict]:
        r = self._client.request(
            "PROPFIND", url,
            headers={"Depth": depth, "Content-Type": "application/xml"},
            content=_PROPFIND_BODY,
        )
        if r.status_code == 404:
            raise FileNotFoundError(url)
        r.raise_for_status()
        tree = _safe_fromstring(r.content)
        out: list[dict] = []
        for resp in tree.findall(f"{_DAV_NS}response"):
            href_el = resp.find(f"{_DAV_NS}href")
            if href_el is None or not href_el.text:
                continue
            length = 0
            mtime = 0.0
            is_dir = False
            for ps in resp.findall(f"{_DAV_NS}propstat"):
                status_el = ps.find(f"{_DAV_NS}status")
                if status_el is None or " 200 " not in (status_el.text or ""):
                    continue
                prop = ps.find(f"{_DAV_NS}prop")
                if prop is None:
                    continue
                rt = prop.find(f"{_DAV_NS}resourcetype")
                if rt is not None and rt.find(f"{_DAV_NS}collection") is not None:
                    is_dir = True
                length_el = prop.find(f"{_DAV_NS}getcontentlength")
                if length_el is not None and length_el.text and length_el.text.strip().isdigit():
                    length = int(length_el.text.strip())
                last_el = prop.find(f"{_DAV_NS}getlastmodified")
                if last_el is not None and last_el.text:
                    mtime = _parse_http_date(last_el.text.strip())
            out.append({"href": href_el.text, "is_dir": is_dir,
                        "size": length, "mtime": mtime})
        return out

    # ── FileSource ABC ──────────────────────────────────────────────────

    def is_dir(self, path: str) -> bool:
        try:
            items = self._propfind(self._url(path, collection=True), depth="0")
        except (httpx.HTTPError, FileNotFoundError, ValueError) as exc:
            self.last_error = ("not found — check the URL's path"
                               if isinstance(exc, FileNotFoundError) else _http_reason(exc))
            self.last_error_auth = is_auth_failure(exc)
            log.warning("WebDAV is_dir(%s) on %s failed: %s", path, self._base,
                        self.last_error)
            return False
        except Exception as exc:          # malformed PROPFIND XML
            self.last_error = f"not a WebDAV response ({type(exc).__name__})"
            self.last_error_auth = False
            log.warning("WebDAV is_dir(%s) on %s failed: %s: %s", path, self._base,
                        type(exc).__name__, exc)
            return False
        ok = bool(items and items[0].get("is_dir"))
        self.last_error = None if ok else "not a folder"
        self.last_error_auth = False
        return ok

    def stat(self, path: str, *, lane: str = "scan") -> FileStat:
        items = self._propfind(self._url(path), depth="0")
        if not items:
            raise FileNotFoundError(path)
        item = items[0]
        return FileStat(size=item["size"], mtime=item["mtime"], is_dir=item["is_dir"])

    def list_dir(self, path: str) -> list[DirEntry]:
        rel_dir = self._rel(path).strip("/")
        url = self._url(rel_dir, collection=True)
        # PROPFIND Depth: 1 returns the folder itself + its immediate children.
        items = self._propfind(url, depth="1")
        self_path = (self._base_path + (f"{rel_dir}/" if rel_dir else "")).rstrip("/")
        entries = []
        for it in items:
            hpath = self._href_path(it["href"], url)
            if hpath is not None:
                entries.append((it, hpath.rstrip("/")))
        if not any(h == self_path for _it, h in entries) and entries:
            # The folder's own entry under another spelling — a reverse proxy
            # that serves the share at a path the server doesn't know about
            # (hrefs under /webdav/ for https://dav.example.com).  It is the
            # entry every other one lies under; alone, it is the folder.
            own = min((h for _it, h in entries), key=len)
            if len(entries) == 1 or all(h == own or h.startswith(own + "/")
                                        for _it, h in entries):
                self_path = own
        result: list[DirEntry] = []
        for it, hpath in entries:
            if hpath == self_path:
                continue                  # the folder's own entry
            # Children are addressed by THIS folder's path + their name, so
            # the base path's spelling on the server (encoding, a proxy
            # prefix) never leaks into the root-relative paths we store.
            name = hpath.rsplit("/", 1)[-1]
            if not name or name in (".", ".."):
                continue
            result.append(DirEntry(
                name=name,
                path=f"/{rel_dir}/{name}" if rel_dir else f"/{name}",
                is_dir=it["is_dir"],
                size=it["size"],
                mtime=it["mtime"],
            ))
        return result

    def walk(self, top: str) -> Iterator[tuple[str, list[str], list[str]]]:
        """BFS the tree using PROPFIND Depth:1 at each directory."""
        queue = [top or "/"]
        while queue:
            dirpath = queue.pop(0)
            try:
                entries = self.list_dir(dirpath)
            except (httpx.HTTPError, FileNotFoundError):
                continue
            dirnames = [e.name for e in entries if e.is_dir]
            filenames = [e.name for e in entries if not e.is_dir]
            yield dirpath, dirnames, filenames
            for e in entries:
                if e.is_dir:
                    queue.append(e.path)

    def read_file(self, path: str, *, lane: str = "stream") -> bytes:
        # One HTTP client per source; ``lane`` is accepted for interface
        # parity with FTPFileSource (no pooled priority lanes over HTTP).
        r = self._client.get(self._url(path))
        if r.status_code == 404:
            raise FileNotFoundError(path)
        r.raise_for_status()
        return r.content

    def read_at(self, path: str, offset: int, length: int, *, lane: str = "scan") -> bytes:
        """HTTP ``Range`` read of ``length`` bytes at ``offset`` (a server that
        ignores ``Range`` costs a read of the prefix, never a wrong answer)."""
        if length <= 0:
            return b""
        with self.open_stream(path, offset=offset, lane=lane, length=length) as st:
            out = bytearray()
            while len(out) < length:
                chunk = st.read(min(_CHUNK, length - len(out)))
                if not chunk:
                    break
                out += chunk
        return bytes(out)

    def read_partial(self, path: str, max_bytes: int, *, lane: str = "scan") -> bytes:
        return self.read_at(path, 0, max_bytes, lane=lane)

    def open_stream(self, path: str, *, offset: int = 0, lane: str = "stream",
                    length: int | None = None) -> SourceStream:
        offset = max(0, int(offset))
        # The file's own bytes: a compressed body would make a ranged read's
        # offsets and the reported length the wire's, not the file's.
        headers = {"Accept-Encoding": "identity"}
        if offset or length:
            end = "" if not length else str(offset + int(length) - 1)
            headers["Range"] = f"bytes={offset}-{end}"
        cm = self._client.stream("GET", self._url(path), headers=headers)
        resp = cm.__enter__()
        try:
            if resp.status_code == 404:
                raise FileNotFoundError(path)
            if resp.status_code == 416:      # offset at / past the end: no bytes
                total = resp.headers.get("content-range", "").rpartition("/")[2].strip()
                st = _WebDAVStream(cm, resp, int(total) if total.isdigit() else None, 0)
                st._iter = iter(())
                return st
            resp.raise_for_status()
            size: int | None = None
            skip = 0
            if resp.status_code == 206:
                m = _CONTENT_RANGE_RE.match(resp.headers.get("content-range", ""))
                if m and m.group(3).isdigit():
                    size = int(m.group(3))
                if m and int(m.group(1)) != offset:
                    raise OSError(f"WebDAV range answered from {m.group(1)}, asked {offset}")
            else:
                cl = resp.headers.get("content-length", "")
                size = int(cl) if cl.isdigit() else None
                skip = offset
            if resp.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                size = None              # the wire length isn't the file's
            return _WebDAVStream(cm, resp, size, skip)
        except BaseException:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
            raise

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def reconnect(self) -> bool:
        old = self._client
        self._client = self._new_client()
        try:
            old.close()
        except Exception:
            pass
        # A quick PROPFIND on the root tells us the credentials are valid.
        try:
            self._propfind("", depth="0")
            return True
        except (httpx.HTTPError, FileNotFoundError) as e:
            log.warning("WebDAV reconnect to %s failed: %s", self._base, e)
            return False
