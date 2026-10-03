# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A small threaded WebDAV server for tests: OPTIONS, PROPFIND (Depth 0/1),
GET / HEAD with a single ``Range``, HTTP Basic auth, an optional path prefix
(``/remote.php/dav/files/u/`` like Nextcloud), an optional per-connection
throttle, and a request log the tests assert against.  Serves a local folder.
"""
from __future__ import annotations

import base64
import os
import ssl
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape


class DavServer:
    def __init__(self, root: Path, *, prefix: str = "/", user: str | None = None,
                 password: str | None = None, rate: float | None = None,
                 tls: tuple[str, str] | None = None, port: int = 0,
                 host: str = "127.0.0.1") -> None:
        self.root = Path(root)
        self.prefix = "/" + prefix.strip("/") + "/" if prefix.strip("/") else "/"
        self.user, self.password = user, password
        self.rate = rate                  # bytes / s per response, None = unthrottled
        self.requests: list[tuple[str, str, str]] = []   # (method, path, range)
        self.bytes_sent = 0
        self._lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet
                pass

            def handle(self):
                try:
                    super().handle()
                except (ConnectionResetError, BrokenPipeError):
                    pass                  # the client hung up

            # ── helpers ──────────────────────────────────────────────
            def _authed(self) -> bool:
                if server.user is None:
                    return True
                hdr = self.headers.get("Authorization", "")
                want = base64.b64encode(
                    f"{server.user}:{server.password}".encode()).decode()
                return hdr == f"Basic {want}"

            def _deny(self):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="dav"')
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _fs(self) -> tuple[Path | None, str]:
                path = unquote(urlsplit(self.path).path)
                if not (path + "/").startswith(server.prefix):
                    return None, ""
                rel = path[len(server.prefix):].strip("/") if len(path) >= len(server.prefix) else ""
                fs = (server.root / rel).resolve()
                if server.root.resolve() not in (fs, *fs.parents):
                    return None, rel
                return fs, rel

            def _simple(self, code: int, body: bytes = b"", ctype: str = "text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body and self.command != "HEAD":
                    self.wfile.write(body)

            def _record(self):
                with server._lock:
                    server.requests.append(
                        (self.command, unquote(urlsplit(self.path).path),
                         self.headers.get("Range", "")))

            # ── verbs ────────────────────────────────────────────────
            def do_OPTIONS(self):
                self._record()
                self.send_response(200)
                self.send_header("DAV", "1, 2")
                self.send_header("Allow", "OPTIONS, GET, HEAD, PROPFIND")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_PROPFIND(self):
                self._record()
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                if not self._authed():
                    return self._deny()
                fs, rel = self._fs()
                if fs is None or not fs.exists():
                    return self._simple(404)
                depth = self.headers.get("Depth", "1")
                items = [(fs, rel)]
                if fs.is_dir() and depth != "0":
                    for child in sorted(fs.iterdir()):
                        items.append((child, f"{rel}/{child.name}".strip("/")))
                parts = ['<?xml version="1.0" encoding="utf-8"?>',
                         '<d:multistatus xmlns:d="DAV:">']
                for p, r in items:
                    st = p.stat()
                    href = quote(server.prefix + r) + ("/" if p.is_dir() and r else "")
                    rt = "<d:collection/>" if p.is_dir() else ""
                    length = "" if p.is_dir() else f"<d:getcontentlength>{st.st_size}</d:getcontentlength>"
                    parts.append(
                        f"<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop>"
                        f"<d:resourcetype>{rt}</d:resourcetype>{length}"
                        f"<d:getlastmodified>{formatdate(st.st_mtime, usegmt=True)}</d:getlastmodified>"
                        f"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
                parts.append("</d:multistatus>")
                body = "".join(parts).encode()
                self.send_response(207)
                self.send_header("Content-Type", 'application/xml; charset="utf-8"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                self._record()
                if not self._authed():
                    return self._deny()
                fs, _rel = self._fs()
                if fs is None or not fs.is_file():
                    return self._simple(404)
                size = fs.stat().st_size
                start, end, code = 0, size - 1, 200
                rng = self.headers.get("Range", "")
                if rng.startswith("bytes="):
                    a, _, b = rng[6:].partition("-")
                    try:
                        start = int(a) if a else max(0, size - int(b))
                        end = int(b) if (a and b) else size - 1
                    except ValueError:
                        start, end = 0, size - 1
                    if start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    end = min(end, size - 1)
                    code = 206
                self.send_response(code)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Last-Modified", formatdate(fs.stat().st_mtime, usegmt=True))
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if self.command == "HEAD":
                    return
                chunk = 64 * 1024
                t0 = time.monotonic()
                sent = 0
                try:
                    with open(fs, "rb") as fh:
                        fh.seek(start)
                        left = end - start + 1
                        while left > 0:
                            data = fh.read(min(chunk, left))
                            if not data:
                                break
                            self.wfile.write(data)
                            left -= len(data)
                            sent += len(data)
                            with server._lock:
                                server.bytes_sent += len(data)
                            if server.rate:
                                ahead = sent / server.rate - (time.monotonic() - t0)
                                if ahead > 0:
                                    time.sleep(ahead)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._httpd.daemon_threads = True
        self.scheme = "http"
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*tls)
            self._httpd.socket = ctx.wrap_socket(self._httpd.socket, server_side=True)
            self.scheme = "https"
        self.port = self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """The share URL (no trailing slash)."""
        return f"{self.scheme}://127.0.0.1:{self.port}{self.prefix.rstrip('/')}"

    def start(self) -> "DavServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="test-webdav", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def gets(self) -> list[tuple[str, str, str]]:
        with self._lock:
            return [r for r in self.requests if r[0] == "GET"]


if __name__ == "__main__":       # manual runs: python _webdav_server.py ROOT PORT [prefix] [rate]
    import sys
    srv = DavServer(Path(sys.argv[1]), port=int(sys.argv[2]),
                    prefix=sys.argv[3] if len(sys.argv) > 3 else "/",
                    rate=float(sys.argv[4]) if len(sys.argv) > 4 else None,
                    user=os.environ.get("DAV_USER"), password=os.environ.get("DAV_PASS"))
    print(srv.url, flush=True)
    srv._httpd.serve_forever()
