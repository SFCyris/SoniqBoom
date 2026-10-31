# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A network share that is a web server's folder listing (protocol
``httpindex``): the HTML index pages a plain web server makes for a folder
(Apache / nginx / lighttpd autoindex, Caddy's browse page, ``python -m
http.server``) — read-only, no WebDAV needed.

Folders are listed by reading the links of their index page: only links to
the folder's own direct children on the same server are taken (a link to
another host, a parent folder, a sort link with a query string is never
followed).  A child whose link ends in ``/`` is a folder.  A size or date
the page states next to a link is used when it is exact (nginx states bytes;
Apache's rounded "1.2K" counts as unknown, 0) — a file's ``stat`` asks the
server itself (``HEAD``, else a one-byte ranged ``GET``), and a scan's walk
(``walk_with_stat``) asks it for every file whose listing lacks an exact size
or date, so an unchanged file is not read again.  Files are read
with plain ``GET`` / ``Range`` requests through the same client as a WebDAV
share (``WebDAVFileSource``: Basic credentials when given, certificate
checks, redirects on the same server).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from soniqboom.core.filesource import DirEntry, FileStat, is_auth_failure
from soniqboom.core.filesource_webdav import WebDAVFileSource, _http_reason, _parse_http_date

log = logging.getLogger(__name__)

# The index pages read for a listing are capped (a folder of ~100,000 files).
_INDEX_MAX_BYTES = 32 * 1024 * 1024
# A scan's walk asks the server for the facts a listing lacks this many files
# at a time.
_STAT_WORKERS = 8

# nginx: the date, then the size — in bytes, or (``autoindex_exact_size
# off``) rounded with a unit ("12K", "1.5M"), which is not exact.
_NGINX_TAIL = re.compile(r"^\s*(\d{1,2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2})\s+"
                         r"(\d+(?:\.\d+)?[KMGTP]?|-)(?![\w.])")
_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::(\d{2}))?")
_EXACT_SIZE = re.compile(r"^\s*(\d+)\s*$")


class _Links(HTMLParser):
    """``(href, text after the link up to the next link)`` of every ``<a>``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[list[str]] = []
        self._in_a = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href is not None:
                self.links.append([href, ""])
                self._in_a = True
                return
        if tag in ("td", "tr", "br") and self.links:
            self.links[-1][1] += " | "

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_a = False

    def handle_data(self, data):
        if self.links and not self._in_a:
            self.links[-1][1] += data


def _tail_facts(tail: str) -> "tuple[int, float]":
    """``(size, mtime)`` an index page states after a link — 0 when unknown
    or not exact."""
    text = tail.replace("\xa0", " ")
    m = _NGINX_TAIL.match(text)
    if m:                                               # nginx: date then bytes
        try:
            mt = datetime.strptime(m.group(1), "%d-%b-%Y %H:%M").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            mt = 0.0
        return (int(m.group(2)) if m.group(2).isdigit() else 0), mt   # (rounded: 0)
    mt = 0.0
    d = _ISO_DATE.search(text)
    if d:
        try:
            mt = datetime.strptime(f"{d.group(1)} {d.group(2)}:{d.group(3) or '00'}",
                                   "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            mt = 0.0
    size = 0
    for cell in text.split("|"):
        m2 = _EXACT_SIZE.match(cell)
        if m2 and not _ISO_DATE.search(cell):
            size = int(m2.group(1))
    return size, mt


def _same_server(a, b) -> bool:
    """Do two parsed URLs name one server (``host`` and ``host:80`` alike)?"""
    def norm(u):
        scheme = (u.scheme or "").lower()
        try:
            port = u.port or {"http": 80, "https": 443}.get(scheme)
        except ValueError:
            return None
        return scheme, (u.hostname or "").lower(), port
    na = norm(a)
    return na is not None and na == norm(b)


class HTTPIndexFileSource(WebDAVFileSource):
    """A web server's folder listing as a read-only share (see the module)."""

    def _get_index(self, url: str) -> "httpx.Response":
        r = self._client.get(url, headers={"Accept": "text/html,*/*;q=0.5"})
        if r.status_code == 404:
            raise FileNotFoundError(url)
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").lower()
        if ctype and "html" not in ctype:
            raise ValueError(f"not a folder listing ({ctype.split(';')[0]})")
        if len(r.content) > _INDEX_MAX_BYTES:
            raise ValueError("folder listing too large")
        return r

    def is_dir(self, path: str) -> bool:
        try:
            self._get_index(self._url(path, collection=True))
        except (httpx.HTTPError, FileNotFoundError, ValueError) as exc:
            self.last_error = ("not found — check the URL's path"
                               if isinstance(exc, FileNotFoundError) else
                               (str(exc) if isinstance(exc, ValueError) else _http_reason(exc)))
            self.last_error_auth = is_auth_failure(exc)
            log.warning("Web folder is_dir(%s) on %s failed: %s", path, self._base,
                        self.last_error)
            return False
        self.last_error = None
        self.last_error_auth = False
        return True

    def stat(self, path: str, *, lane: str = "scan") -> FileStat:
        if path.endswith("/") or not self._rel(path):
            if self.is_dir(path):
                return FileStat(size=0, mtime=0.0, is_dir=True)
            raise FileNotFoundError(path)
        url = self._url(path)
        r = self._client.head(url)
        if r.status_code == 404:
            raise FileNotFoundError(path)
        if r.status_code in (405, 501) or "content-length" not in r.headers:
            # No HEAD (or no length in it): one byte of the file, whose
            # Content-Range states the whole size.
            with self._client.stream("GET", url, headers={"Range": "bytes=0-0"}) as g:
                if g.status_code == 404:
                    raise FileNotFoundError(path)
                g.raise_for_status()
                total = (g.headers.get("content-range") or "").rpartition("/")[2]
                size = int(total) if total.isdigit() else int(g.headers.get("content-length") or 0)
                return FileStat(size=size, mtime=_parse_http_date(
                    g.headers.get("last-modified") or ""), is_dir=False)
        r.raise_for_status()
        if "html" in (r.headers.get("content-type") or "").lower() and r.url.path.endswith("/"):
            return FileStat(size=0, mtime=0.0, is_dir=True)   # a folder (redirected to /)
        return FileStat(size=int(r.headers.get("content-length") or 0),
                        mtime=_parse_http_date(r.headers.get("last-modified") or ""),
                        is_dir=False)

    def list_dir(self, path: str):
        rel_dir = self._rel(path).strip("/")
        r = self._get_index(self._url(rel_dir, collection=True))
        # (``page``: where the listing was answered — a share named http://
        # that the server redirects to https:// lists https:// links)
        page = urlsplit(str(r.url))
        base = urlsplit(self._base)
        folder = unquote(page.path)
        if not folder.endswith("/"):
            folder += "/"
        parser = _Links()
        try:
            parser.feed(r.text)
            parser.close()
        except Exception:                                   # noqa: BLE001 — a broken page
            log.debug("Web folder listing of %s did not parse completely", r.url,
                      exc_info=True)
        seen: set[str] = set()
        out: list[DirEntry] = []
        for href, tail in parser.links:
            href = href.strip()
            if not href or href.startswith(("#", "?", "javascript:", "mailto:")):
                continue
            target = urlsplit(urljoin(str(r.url), href))
            if not (_same_server(target, base) or _same_server(target, page)):
                continue                  # another server: never followed
            if target.query or target.fragment:
                continue                  # sort links (?C=N;O=D) and anchors
            tpath = unquote(target.path)
            if not tpath.startswith(folder):
                continue                  # the parent, or elsewhere on the server
            rest = tpath[len(folder):]
            is_dir = rest.endswith("/")
            name = rest.rstrip("/")
            if not name or "/" in name or "\x00" in name or name in (".", ".."):
                continue                  # the folder itself, deeper, or no name a file has
            if name in seen:
                continue                  # an icon link and a name link to one file
            seen.add(name)
            size, mtime = (0, 0.0) if is_dir else _tail_facts(tail)
            out.append(DirEntry(
                name=name,
                path=f"/{rel_dir}/{name}" if rel_dir else f"/{name}",
                is_dir=is_dir, size=size, mtime=mtime,
            ))
        return out

    def walk_with_stat(self, top: str, *, skip_subtree_fn=None, error_sink=None):
        """The base walk, with the size and date of every file whose listing
        did not state both exactly asked of the server (``HEAD``,
        ``_STAT_WORKERS`` at a time) — the scanner compares them with the
        stored ones, and an unknown 0 would read as a changed file on every
        scan.  A file whose ``stat`` fails keeps the listing's facts."""
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=_STAT_WORKERS,
                                thread_name_prefix="sb-httpindex-stat") as pool:
            for dirpath, dirs, files in super().walk_with_stat(
                    top, skip_subtree_fn=skip_subtree_fn, error_sink=error_sink):
                unknown = [e for e in files if not (e.size and e.mtime)]
                for e, st in zip(unknown, pool.map(self._stat_or_none, unknown)):
                    if st is not None and not st.is_dir:
                        e.size = st.size or e.size
                        e.mtime = st.mtime or e.mtime
                yield dirpath, dirs, files

    def _stat_or_none(self, entry: DirEntry) -> "FileStat | None":
        try:
            return self.stat(entry.path)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            log.debug("Web folder stat(%s) on %s failed: %s", entry.path, self._base, exc)
            return None

    def reconnect(self) -> bool:
        """A new client, and the root's listing read through it."""
        old = self._client
        self._client = self._new_client()
        try:
            old.close()
        except Exception:                                   # noqa: BLE001
            pass
        return self.is_dir("/")
