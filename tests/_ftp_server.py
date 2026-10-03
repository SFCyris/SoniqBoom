# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A small threaded FTP server for tests: passive mode, ``SIZE``, ``MDTM``,
``REST``, ``RETR`` (optionally throttled), ``ABOR``, ``MLSD``, ``LIST``,
``CWD``.  Serves a local folder read-only; any user name / password logs in
(unless ``password`` is set).  Records the bytes sent and the RETRs (path,
offset) the tests assert against.  Opt-in misbehaviour: no ``SIZE``
(``size=False``), the first ``abort_retrs`` RETRs cut after ``abort_after``
bytes with a 426 reply, a per-IP connection cap (``max_conns``, 421 at the
greeting; ``peak_conns`` records the most at once).
"""
from __future__ import annotations

import os
import socket
import socketserver
import threading
import time
from pathlib import Path


class FtpServer:
    def __init__(self, root: Path, *, rate: float | None = None, port: int = 0,
                 host: str = "127.0.0.1", abor_replies: int = 2, size: bool = True,
                 abort_after: int | None = None, abort_retrs: int = 1,
                 password: str | None = None, max_conns: int | None = None) -> None:
        self.root = Path(root).resolve()
        self.rate = rate                 # bytes / s per transfer, None = unthrottled
        # 2: an interrupted RETR answers 426, then ABOR answers 226 (RFC 959);
        # 1: a terse server — the interrupted RETR sends nothing, ABOR 226.
        self.abor_replies = abor_replies
        self.size = size
        self.abort_after = abort_after
        self.abort_retrs = abort_retrs
        self.password = password
        self.max_conns = max_conns
        self.conns = 0
        self.peak_conns = 0
        self.refused_logins = 0
        self.bytes_sent = 0
        self.retrs: list[tuple[str, int]] = []
        self.logins = 0
        self._lock = threading.Lock()
        server = self

        class Handler(socketserver.StreamRequestHandler):
            def setup(self):
                super().setup()
                self.request.setsockopt(socket.SOL_SOCKET, socket.SO_OOBINLINE, 1)
                self.cwd = "/"
                self.rest = 0
                self.pasv: socket.socket | None = None

            def reply(self, line: str) -> None:
                self.wfile.write(line.encode("utf-8") + b"\r\n")
                self.wfile.flush()

            def fs(self, arg: str) -> Path | None:
                p = arg if arg.startswith("/") else (self.cwd.rstrip("/") + "/" + arg)
                full = (server.root / p.lstrip("/")).resolve()
                if server.root not in (full, *full.parents):
                    return None
                return full

            def data_conn(self) -> socket.socket | None:
                if self.pasv is None:
                    return None
                self.pasv.settimeout(10)
                try:
                    conn, _ = self.pasv.accept()
                finally:
                    self.pasv.close()
                    self.pasv = None
                return conn

            def handle(self):
                with server._lock:
                    server.conns += 1
                    server.peak_conns = max(server.peak_conns, server.conns)
                    over = server.max_conns is not None and server.conns > server.max_conns
                try:
                    if over:
                        self.reply("421 There are too many connections from your internet address")
                        return
                    self._session()
                except OSError:            # the client dropped the connection
                    pass
                finally:
                    with server._lock:
                        server.conns -= 1

            def _session(self):
                self.reply("220 test ftp ready")
                aborted = False
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode("utf-8", "replace").lstrip("\xff\xf4\xf2").strip()
                    line = line.replace("�", "").strip()
                    cmd, _, arg = line.partition(" ")
                    cmd = cmd.upper()
                    if cmd == "USER":
                        self.reply("331 password please")
                    elif cmd == "PASS":
                        if server.password is not None and arg != server.password:
                            with server._lock:
                                server.refused_logins += 1
                            self.reply("530 Login incorrect.")
                            continue
                        with server._lock:
                            server.logins += 1
                        self.reply("230 logged in")
                    elif cmd in ("SYST",):
                        self.reply("215 UNIX Type: L8")
                    elif cmd == "FEAT":
                        self.wfile.write(b"211-Features:\r\n MLSD\r\n SIZE\r\n MDTM\r\n REST STREAM\r\n UTF8\r\n211 End\r\n")
                        self.wfile.flush()
                    elif cmd in ("OPTS", "TYPE", "NOOP", "MODE", "STRU"):
                        self.reply("200 ok")
                    elif cmd == "PWD":
                        self.reply(f'257 "{self.cwd}"')
                    elif cmd == "CWD":
                        p = self.fs(arg)
                        if p is not None and p.is_dir():
                            self.cwd = "/" + str(p.relative_to(server.root)).strip(".")
                            self.cwd = self.cwd if self.cwd != "/." else "/"
                            self.reply("250 ok")
                        else:
                            self.reply("550 No such file or directory")
                    elif cmd == "PASV":
                        s = socket.socket()
                        s.bind(("127.0.0.1", 0))
                        s.listen(1)
                        self.pasv = s
                        p = s.getsockname()[1]
                        self.reply(f"227 Entering Passive Mode (127,0,0,1,{p >> 8},{p & 255})")
                    elif cmd == "EPSV":
                        s = socket.socket()
                        s.bind(("127.0.0.1", 0))
                        s.listen(1)
                        self.pasv = s
                        self.reply(f"229 Entering Extended Passive Mode (|||{s.getsockname()[1]}|)")
                    elif cmd == "SIZE" and not server.size:
                        self.reply("502 SIZE not implemented")
                    elif cmd == "SIZE":
                        p = self.fs(arg)
                        if p is not None and p.is_file():
                            self.reply(f"213 {p.stat().st_size}")
                        else:
                            self.reply("550 No such file or directory")
                    elif cmd == "MDTM":
                        p = self.fs(arg)
                        if p is not None and p.exists():
                            self.reply("213 " + time.strftime("%Y%m%d%H%M%S", time.gmtime(p.stat().st_mtime)))
                        else:
                            self.reply("550 No such file or directory")
                    elif cmd == "REST":
                        self.rest = int(arg or 0)
                        self.reply(f"350 restarting at {self.rest}")
                    elif cmd == "RETR":
                        p = self.fs(arg)
                        if p is None or not p.is_file():
                            self.rest = 0
                            self.reply("550 No such file or directory")
                            continue
                        offset, self.rest = self.rest, 0
                        with server._lock:
                            server.retrs.append(("/" + str(p.relative_to(server.root)), offset))
                        self.reply("150 opening data connection")
                        conn = self.data_conn()
                        if conn is None:
                            self.reply("425 no data connection")
                            continue
                        ok = True
                        t0 = time.monotonic()
                        sent = 0
                        cut = None
                        with server._lock:
                            if server.abort_after is not None and server.abort_retrs > 0:
                                server.abort_retrs -= 1
                                cut = server.abort_after
                        try:
                            with open(p, "rb") as fh:
                                fh.seek(offset)
                                while True:
                                    chunk = fh.read(64 * 1024)
                                    if cut is not None:
                                        chunk = chunk[:max(0, cut - sent)]
                                        if not chunk:
                                            raise OSError("server-side abort")
                                    if not chunk:
                                        break
                                    conn.sendall(chunk)
                                    sent += len(chunk)
                                    with server._lock:
                                        server.bytes_sent += len(chunk)
                                    if server.rate:
                                        ahead = sent / server.rate - (time.monotonic() - t0)
                                        if ahead > 0:
                                            time.sleep(ahead)
                        except OSError:
                            ok = False
                        finally:
                            try:
                                conn.close()
                            except OSError:
                                pass
                        if ok:
                            self.reply("226 transfer complete")
                        else:
                            aborted = True
                            if server.abor_replies == 2:
                                self.reply("426 connection closed; transfer aborted")
                    elif cmd == "ABOR":
                        self.reply("226 abort successful" if aborted else "225 no transfer to abort")
                        aborted = False
                    elif cmd in ("MLSD", "LIST", "NLST"):
                        p = self.fs(arg) if arg and not arg.startswith("-") else self.fs(self.cwd)
                        if p is None or not p.is_dir():
                            self.reply("550 No such file or directory")
                            continue
                        self.reply("150 listing")
                        conn = self.data_conn()
                        out = []
                        for c in sorted(p.iterdir()):
                            st = c.stat()
                            mod = time.strftime("%Y%m%d%H%M%S", time.gmtime(st.st_mtime))
                            if cmd == "MLSD":
                                kind = "dir" if c.is_dir() else "file"
                                out.append(f"type={kind};size={st.st_size};modify={mod}; {c.name}")
                            elif cmd == "NLST":
                                out.append(c.name)
                            else:
                                kind = "d" if c.is_dir() else "-"
                                out.append(f"{kind}rw-r--r-- 1 u g {st.st_size} "
                                           + time.strftime("%b %d %H:%M", time.gmtime(st.st_mtime))
                                           + f" {c.name}")
                        try:
                            conn.sendall(("\r\n".join(out) + "\r\n").encode("utf-8"))
                        finally:
                            conn.close()
                        self.reply("226 done")
                    elif cmd == "QUIT":
                        self.reply("221 bye")
                        return
                    else:
                        self.reply("502 not implemented")

        class TCP(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._srv = TCP((host, port), Handler)
        self.port = self._srv.server_address[1]
        self._thread: threading.Thread | None = None

    def start(self) -> "FtpServer":
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        name="test-ftp", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


if __name__ == "__main__":     # manual runs: python _ftp_server.py ROOT PORT
    import sys
    srv = FtpServer(Path(sys.argv[1]), port=int(sys.argv[2]))
    rate_file = os.environ.get("FTP_RATE_FILE")
    if rate_file:
        def _poll():
            while True:
                try:
                    srv.rate = float(Path(rate_file).read_text().strip() or 0) or None
                except Exception:
                    srv.rate = None
                time.sleep(0.3)
        threading.Thread(target=_poll, daemon=True).start()
    print(f"ftp://127.0.0.1:{srv.port}", flush=True)
    srv._srv.serve_forever()
