# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""WebDAV FileSource — Nextcloud / ownCloud / generic WebDAV servers.

Implementation uses raw ``httpx`` to speak the standard WebDAV verbs
(PROPFIND, GET) instead of pulling in another library — httpx is already
a top-level dependency.  This keeps the dep footprint tight and lets
us share connection pooling with the rest of the app.

Auth: HTTP Basic over HTTPS.  Token auth (Nextcloud app passwords) is
served as username + token over Basic too, so this works for both.

Why no ``async`` here?  The FileSource ABC is synchronous because the
scanner walks libraries inside a thread pool (``asyncio.to_thread``).
Switching to async for one source would force the scanner to be async
end-to-end, which is out of scope.  We use ``httpx.Client`` (the sync
flavour) accordingly.
"""
from __future__ import annotations

import logging
# Use defusedxml so a malicious WebDAV peer can't DoS the scanner via
# billion-laughs / entity-expansion attacks against PROPFIND responses
# (pen-test #2 P0-2).  Stdlib ``xml.etree.ElementTree`` is documented
# vulnerable to these.  We keep stdlib ``ET.TreeBuilder`` types for the
# Element API; only the *parser* changes.
try:
    from defusedxml.ElementTree import fromstring as _safe_fromstring
except ImportError:
    # Fallback at import-time only; the module logs and the parser
    # path falls through to the stdlib (with the documented risk).
    from xml.etree.ElementTree import fromstring as _safe_fromstring
import xml.etree.ElementTree as ET
from typing import Iterator
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx

from soniqboom.core.filesource import DirEntry, FileSource, FileStat

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


def _parse_http_date(s: str) -> float:
    """Parse an RFC 1123 date (``Tue, 15 Nov 2024 10:00:00 GMT``) into a
    Unix timestamp.  Falls back to 0.0 if anything's off."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        return 0.0


class WebDAVFileSource(FileSource):
    """WebDAV-over-HTTP(S) filesystem source.

    ``base_url`` should point at the WebDAV root (``https://cloud.example.com
    /remote.php/dav/files/alice/`` for Nextcloud).  All paths handed to
    ``walk`` / ``list_dir`` / ``read_file`` / ``stat`` / ``is_dir`` are
    server-side absolute URLs starting with ``base_url`` — the scanner
    treats them like any other string path.
    """

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        verify_ssl: bool = True,
        timeout: float = 30.0,
    ) -> None:
        # Normalise: always end with a slash so ``urljoin`` does the right
        # thing when joining relative entries.
        if not base_url.endswith("/"):
            base_url += "/"
        self._base = base_url
        self._auth = httpx.BasicAuth(username, password) if username else None
        self._verify = verify_ssl
        self._client = httpx.Client(
            base_url=base_url, auth=self._auth, verify=verify_ssl,
            timeout=httpx.Timeout(connect=5.0, read=timeout, write=timeout, pool=5.0),
            follow_redirects=True,
        )

    # ── Helpers ─────────────────────────────────────────────────────────

    def _rel(self, path: str) -> str:
        """Convert an absolute URL (or already-relative path) into a path
        relative to ``base_url``, suitable for httpx's relative URLs."""
        if path.startswith(self._base):
            return path[len(self._base):]
        if path.startswith("/"):
            return path.lstrip("/")
        return path

    def _abs(self, rel: str) -> str:
        """Reconstruct an absolute URL from a server-side ``href`` value.

        Guards against an SSRF / open-redirect via the WebDAV peer
        (pen-test #2 P1-3): if the server returns an absolute href to a
        *different host*, refuse — we'd otherwise re-issue requests
        (with Basic credentials) against an attacker-chosen origin."""
        if rel.startswith("http://") or rel.startswith("https://"):
            base = urlsplit(self._base)
            target = urlsplit(rel)
            if (target.scheme, target.netloc) != (base.scheme, base.netloc):
                raise ValueError(
                    f"WebDAV server returned cross-host href ({target.scheme}://{target.netloc}); "
                    f"refusing to follow off-base URL.",
                )
            return rel
        return urljoin(self._base, rel.lstrip("/"))

    def _propfind(self, path: str, depth: str) -> list[dict]:
        rel = quote(self._rel(path), safe="/")
        r = self._client.request(
            "PROPFIND", rel,
            headers={"Depth": depth, "Content-Type": "application/xml"},
            content=_PROPFIND_BODY,
        )
        if r.status_code == 404:
            return []
        r.raise_for_status()
        tree = _safe_fromstring(r.content)
        out: list[dict] = []
        for resp in tree.findall(f"{_DAV_NS}response"):
            href_el = resp.find(f"{_DAV_NS}href")
            if href_el is None or not href_el.text:
                continue
            href = href_el.text.strip()
            # Pick the first 2xx propstat.
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
                if length_el is not None and length_el.text and length_el.text.isdigit():
                    length = int(length_el.text)
                last_el = prop.find(f"{_DAV_NS}getlastmodified")
                if last_el is not None and last_el.text:
                    mtime = _parse_http_date(last_el.text)
            out.append({"href": href, "is_dir": is_dir, "size": length, "mtime": mtime})
        return out

    # ── FileSource ABC ──────────────────────────────────────────────────

    def is_dir(self, path: str) -> bool:
        try:
            items = self._propfind(path, depth="0")
        except httpx.HTTPError:
            return False
        return bool(items and items[0].get("is_dir"))

    def stat(self, path: str) -> FileStat:
        items = self._propfind(path, depth="0")
        if not items:
            raise FileNotFoundError(path)
        item = items[0]
        return FileStat(size=item["size"], mtime=item["mtime"], is_dir=item["is_dir"])

    def list_dir(self, path: str) -> list[DirEntry]:
        # PROPFIND Depth: 1 returns the directory itself + immediate children.
        items = self._propfind(path, depth="1")
        # Skip the entry whose href matches the dir itself.
        base_rel = self._rel(path).rstrip("/")
        result: list[DirEntry] = []
        for it in items:
            href = it["href"]
            # Server may serve href as ``/remote.php/.../subdir/`` — strip
            # any trailing slash for comparison.
            name = href.rstrip("/").rsplit("/", 1)[-1]
            from urllib.parse import unquote
            name = unquote(name)
            # Skip the parent self-entry.
            if name == base_rel.rsplit("/", 1)[-1] and not it["is_dir"]:
                continue
            if href.rstrip("/").endswith(base_rel):
                continue
            result.append(DirEntry(
                name=name,
                path=self._abs(href),
                is_dir=it["is_dir"],
                size=it["size"],
                mtime=it["mtime"],
            ))
        return result

    def walk(self, top: str) -> Iterator[tuple[str, list[str], list[str]]]:
        """BFS the tree using PROPFIND Depth:1 at each directory."""
        queue = [top]
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
        # WebDAV uses an HTTP client per-source today; ``lane`` is accepted
        # for parity with FTPFileSource so the abstract interface is uniform.
        rel = quote(self._rel(path), safe="/")
        r = self._client.get(rel)
        r.raise_for_status()
        return r.content

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def reconnect(self) -> bool:
        try:
            self._client.close()
        except Exception:
            pass
        self._client = httpx.Client(
            base_url=self._base, auth=self._auth, verify=self._verify,
            timeout=self._client.timeout,
            follow_redirects=True,
        )
        # A quick PROPFIND on the root tells us the credentials are valid.
        try:
            self._propfind(self._base, depth="0")
            return True
        except httpx.HTTPError as e:
            log.warning("WebDAV reconnect failed: %s", e)
            return False
