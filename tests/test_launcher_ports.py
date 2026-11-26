# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""run.sh / shutdown.sh / restart.sh use the port the server really listens on.

Without ``--port`` the scripts used to assume 8080, so a server configured
elsewhere (``server.port`` in SoniqBoom.conf, ``SONIQBOOM_PORT``) was started
with ``--port 8080`` anyway, and shutdown.sh's port fallbacks looked at 8080.

The scripts run for real (bash) from a temp folder, against a FAKE server: a
tiny HTTP stub installed as ``.venv/bin/soniqboom`` that records the ``--port``
it was given.  ``.venv/bin/python`` is the real interpreter, so the port is
resolved by the real ``soniqboom.config``.  HOME / SONIQBOOM_CONF /
SONIQBOOM_DATA_DIR point at temp dirs.

Safety: shutdown.sh stops processes found via ``pgrep`` / ``lsof`` and runs
``pkill -f soniqboom-menubar.py``.  Shims first on PATH make ``pgrep`` and
``pkill`` no-ops and let ``lsof`` report only this test's fake servers, so even
a wrong port can never reach a real server or menu bar on this machine.  Every
box configures a non-8080 port, and the scripts' literal 8080 fallback is
swapped for an unused guard port in the copies, so nothing here even connects
to :8080 (where a real server may run).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

_FAKE_SERVER = textwrap.dedent("""\
    # Fake soniqboom: records its argv, answers /api/health on --port,
    # optionally writes startup-status.json like the real server.
    import http.server, json, os, signal, sys
    port = int(sys.argv[sys.argv.index("--port") + 1])    # run.sh always passes it
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "pid": os.getpid()}) + "\\n")
    print("Library loaded — 0 tracks loaded", flush=True)   # run.sh greps this
    if os.environ.get("FAKE_STATUS") == "1":
        data = os.environ["SONIQBOOM_DATA_DIR"]
        with open(os.path.join(data, "startup-status.json"), "w") as f:
            json.dump({"pid": os.getpid(), "port": port, "ready": True, "data_dir": data}, f)
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"version": "test", "services": []}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def log_message(self, *a): pass
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
""")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Box:
    """A throw-away install: the three scripts, a fake venv, temp HOME / conf
    / data dir, and PATH shims."""

    def __init__(self, root: Path, conf_port: int, no_network: bool = False):
        assert conf_port != 8080
        self.root = root
        self.scripts = root / "app"
        bin_ = self.scripts / ".venv" / "bin"
        bin_.mkdir(parents=True)
        self.guard_port = _free_port()             # stands in for the 8080 fallback
        for name in ("run.sh", "shutdown.sh", "restart.sh"):
            text = (REPO / name).read_text().replace("PORT=8080", f"PORT={self.guard_port}")
            code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
            assert not any("8080" in ln for ln in code), name      # no route to :8080
            (self.scripts / name).write_text(text)
            shutil.copymode(REPO / name, self.scripts / name)   # restart.sh execs them
        (bin_ / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        (self.scripts / "soniqboom_fake.py").write_text(_FAKE_SERVER)
        (bin_ / "soniqboom").write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{self.scripts / "soniqboom_fake.py"}" "$@"\n')
        for f in bin_.iterdir():
            f.chmod(0o755)
        self.home = root / "home"
        self.data = root / "data"
        self.home.mkdir()
        self.data.mkdir()
        self.conf = root / "SoniqBoom.conf"
        self.set_conf_port(conf_port)
        self.fake_log = root / "fake.log"
        self.allow = root / "lsof-allow"          # pids lsof may report
        self.allow.write_text("")
        shims = root / "shims"
        shims.mkdir()
        real_lsof = shutil.which("lsof") or ""
        (shims / "pkill").write_text(f'#!/bin/sh\necho "pkill $*" >> "{root}/shim.log"\nexit 0\n')
        if sys.platform == "darwin" and os.path.exists("/bin/bash"):
            # macOS's stock bash 3.2 — what the menu bar's `bash` usually is.
            (shims / "bash").write_text('#!/bin/sh\nexec /bin/bash "$@"\n')
        if no_network:                      # a Mac without a network address
            (shims / "ifconfig").write_text("#!/bin/sh\nexit 0\n")
        (shims / "pgrep").write_text(f'#!/bin/sh\necho "pgrep $*" >> "{root}/shim.log"\nexit 1\n')
        (shims / "lsof").write_text(
            "#!/bin/sh\n"
            f'[ -x "{real_lsof}" ] || exit 1\n'
            f'"{real_lsof}" "$@" 2>/dev/null | grep -Fx -f "{self.allow}"\n')
        for f in shims.iterdir():
            f.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("SONIQBOOM_") and k != "XDG_DATA_HOME"}
        self.env.update(PATH=f"{shims}:{os.environ['PATH']}", HOME=str(self.home),
                        SONIQBOOM_CONF=str(self.conf), SONIQBOOM_DATA_DIR=str(self.data),
                        FAKE_LOG=str(self.fake_log))
        self.pids: list[int] = []

    def set_conf_port(self, port: int) -> None:
        assert port != 8080
        self.conf.write_text(json.dumps({"server": {"port": port}}))

    def run(self, script: str, *args: str, timeout: float = 60, **env) -> subprocess.CompletedProcess:
        if "--port" not in args:
            # What the scripts will resolve — never :8080, where a real server may run.
            resolved = subprocess.run(
                [str(self.scripts / ".venv" / "bin" / "python"), "-I", "-c",
                 "from soniqboom.config import settings; print(int(settings.port))"],
                env={**self.env, **env}, capture_output=True, text=True).stdout.strip()
            assert resolved != "8080", "this box would resolve to :8080"
        bash = "/bin/bash" if sys.platform == "darwin" and os.path.exists("/bin/bash") else "bash"
        r = subprocess.run([bash, str(self.scripts / script), *args],
                           env={**self.env, **env}, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        self._track()
        return r

    def start_fake(self, port: int, status: bool) -> int:
        """A fake server started by hand (no run.sh, no pidfile)."""
        p = subprocess.Popen([str(self.scripts / ".venv" / "bin" / "soniqboom"), "--port", str(port)],
                             env={**self.env, "FAKE_STATUS": "1" if status else "0"},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if _listening(port):
                break
            time.sleep(0.05)
        self._track()
        return p.pid

    def _track(self) -> None:
        if self.fake_log.exists():
            for line in self.fake_log.read_text().splitlines():
                pid = json.loads(line)["pid"]
                if pid not in self.pids:
                    self.pids.append(pid)
        self.allow.write_text("".join(f"{p}\n" for p in self.pids))

    def launches(self) -> list[list[str]]:
        if not self.fake_log.exists():
            return []
        return [json.loads(line)["argv"] for line in self.fake_log.read_text().splitlines()]

    def cleanup(self) -> None:
        for pid in self.pids:
            try:
                os.kill(pid, 9)
            except OSError:
                pass


@pytest.fixture()
def box(tmp_path):
    boxes: list[Box] = []

    def make(conf_port: int, no_network: bool = False) -> Box:
        b = Box(tmp_path / f"box{len(boxes)}", conf_port, no_network)
        boxes.append(b)
        return b
    yield make
    for b in boxes:
        b.cleanup()


def test_run_without_port_uses_the_conf_port_and_shutdown_finds_it(box):
    port = _free_port()
    b = box(conf_port=port)
    r = b.run("run.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert b.launches() == [["--port", str(port)]]
    assert f"http://localhost:{port}" in r.stdout
    assert _listening(port)

    r = b.run("shutdown.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"port {port} is free" in r.stdout
    assert not _listening(port)


def test_run_without_a_network_address_finishes(box):
    """No non-loopback address: run.sh used to abort on the empty address
    list under bash 3.2 (set -u) after starting the server."""
    port = _free_port()
    b = box(conf_port=port, no_network=True)
    r = b.run("run.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "unbound variable" not in r.stderr
    assert f"http://localhost:{port}" in r.stdout and "Stop:" in r.stdout
    b.run("shutdown.sh")
    assert not _listening(port)


def test_soniqboom_port_env_beats_the_conf(box):
    conf_port, env_port = _free_port(), _free_port()
    b = box(conf_port=conf_port)
    r = b.run("run.sh", SONIQBOOM_PORT=str(env_port))
    assert r.returncode == 0, r.stdout + r.stderr
    assert b.launches() == [["--port", str(env_port)]]
    assert _listening(env_port) and not _listening(conf_port)
    b.run("shutdown.sh", SONIQBOOM_PORT=str(env_port))
    assert not _listening(env_port)


def test_explicit_port_beats_env_and_conf(box):
    conf_port, env_port, cli_port = _free_port(), _free_port(), _free_port()
    b = box(conf_port=conf_port)
    r = b.run("run.sh", "--port", str(cli_port), SONIQBOOM_PORT=str(env_port))
    assert r.returncode == 0, r.stdout + r.stderr
    assert b.launches() == [["--port", str(cli_port)]]
    b.run("shutdown.sh", "--port", str(cli_port))
    assert not _listening(cli_port)


_needs_lsof = pytest.mark.skipif(shutil.which("lsof") is None, reason="port fallback needs lsof")


@_needs_lsof
def test_shutdown_without_pidfile_finds_the_port_from_startup_status(box):
    """No pidfile, server bound a port that is NOT the configured one (e.g.
    started with --port, or the conf changed since): the status file says
    where it listens."""
    conf_port, bound = _free_port(), _free_port()
    b = box(conf_port=conf_port)
    pid = b.start_fake(bound, status=True)
    r = b.run("shutdown.sh")
    assert f"(pid {pid})" in r.stdout, r.stdout + r.stderr
    assert not _listening(bound)


@_needs_lsof
def test_shutdown_without_pidfile_or_status_uses_the_configured_port(box):
    port = _free_port()
    b = box(conf_port=port)
    pid = b.start_fake(port, status=False)
    r = b.run("shutdown.sh")
    assert f"(pid {pid})" in r.stdout, r.stdout + r.stderr
    assert not _listening(port)


def test_restart_after_a_conf_port_change_moves_the_server(box):
    """restart.sh without args: stop the running server where it listens (the
    status file), start the new one on the newly configured port."""
    old, new = _free_port(), _free_port()
    b = box(conf_port=old)
    assert b.run("run.sh", FAKE_STATUS="1").returncode == 0
    assert _listening(old)
    b.set_conf_port(new)
    r = b.run("restart.sh", FAKE_STATUS="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert b.launches() == [["--port", str(old)], ["--port", str(new)]]
    assert _listening(new) and not _listening(old)
    b.run("shutdown.sh")
    assert not _listening(new)


@pytest.mark.parametrize("conf", [
    '{"server": {"port": "not-a-number"}}',
    '{"server": {"port": 70000}}',
    '{"server": {"port": 0}}',
])
def test_unusable_configured_port_falls_back_to_the_old_default(box, conf):
    """A port the resolver can't use → the old fixed default (8080; the guard
    port in these copies).  An unreadable conf is NOT this case: the server
    then uses its built-in default, which the resolver reports as such."""
    b = box(conf_port=_free_port())
    assert (REPO / "run.sh").read_text().count("PORT=8080") == 1
    b.conf.write_text(conf)
    r = b.run("run.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert b.launches() == [["--port", str(b.guard_port)]]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS menu bar")
@pytest.mark.parametrize("argv, expected", [
    (["18123", "/x", "configured"], []),
    (["18123", "/x", "--port"], ["--port", "18123"]),
    (["18123", "/x"], ["--port", "18123"]),          # an older run.sh
    ([], []),                                         # by hand: configured too
])
def test_menubar_start_and_restart_leave_a_configured_port_to_run_sh(argv, expected, tmp_path):
    pytest.importorskip("rumps")
    probe = textwrap.dedent(f"""
        import importlib.util, json, sys
        sys.argv = ["soniqboom-menubar.py"] + {argv!r}
        spec = importlib.util.spec_from_file_location("menubar", {str(REPO / "soniqboom-menubar.py")!r})
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        calls = []
        m._run_script = lambda script, *args: calls.append([script.name, *args])
        m.rumps.notification = lambda *a, **k: None
        m._is_running = lambda: None
        m.SoniqBoomMenuBar.start_server(None, None)
        m.SoniqBoomMenuBar.restart_server(None, None)    # not running → run.sh
        m._is_running = lambda: 4242
        m.SoniqBoomMenuBar.restart_server(None, None)    # running → restart.sh
        print(json.dumps(calls))
    """)
    conf = tmp_path / "SoniqBoom.conf"
    conf.write_text('{"server": {"port": 18123}}')
    env = {**os.environ, "HOME": str(tmp_path), "SONIQBOOM_DATA_DIR": str(tmp_path),
           "SONIQBOOM_CONF": str(conf), "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    calls = json.loads(out.stdout.strip().splitlines()[-1])
    assert calls == [["run.sh", *expected], ["run.sh", *expected], ["restart.sh", *expected]]
