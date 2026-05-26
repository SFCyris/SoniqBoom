#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""SoniqBoom macOS menu bar icon.

Provides Start / Restart / Stop / Quit from the system menu bar.
Launched automatically by run.sh; can also be run standalone.

Usage:  python3 soniqboom-menubar.py [PORT] [SCRIPT_DIR]
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import objc
import rumps

# AppKit ships with rumps; WebKit is an extra (pyobjc-framework-WebKit).
from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
    NSScrollView,
    NSTextView,
    NSViewHeightSizable,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSURL, NSMakeRect, NSMakeSize, NSObject, NSURLRequest

try:
    from WebKit import WKUserScript, WKWebView, WKWebViewConfiguration
    _HAS_WEBKIT = True
except ImportError:
    _HAS_WEBKIT = False

# ── Singleton ────────────────────────────────────────────────────────────────

import time as _time


def _kill_existing_instances(wait_seconds: float = 2.0) -> None:
    """SIGTERM any other soniqboom-menubar.py processes, then wait briefly
    for them to actually exit so we don't end up with two menubar icons
    fighting over the NSStatusItem.
    """
    my_pid = os.getpid()
    others: list[int] = []
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "soniqboom-menubar\\.py"],
            text=True,
        )
        for line in out.strip().splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid != my_pid:
                others.append(pid)
    except subprocess.CalledProcessError:
        return  # no other instances

    for pid in others:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue

    deadline = _time.monotonic() + wait_seconds
    remaining = list(others)
    while remaining and _time.monotonic() < deadline:
        still_alive = []
        for pid in remaining:
            try:
                os.kill(pid, 0)
                still_alive.append(pid)
            except ProcessLookupError:
                pass
        if not still_alive:
            return
        remaining = still_alive
        _time.sleep(0.05)


# ── Config ───────────────────────────────────────────────────────────────────

PORT = sys.argv[1] if len(sys.argv) > 1 else "8080"
SCRIPT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent
_DATA_DIR_ENV = os.environ.get("SONIQBOOM_DATA_DIR")
DATA_DIR = Path(_DATA_DIR_ENV).expanduser() if _DATA_DIR_ENV else (
    Path.home() / "Library" / "Application Support" / "SoniqBoom"
)
PID_FILE = DATA_DIR / "soniqboom.pid"
STARTUP_STATUS_FILE = DATA_DIR / "startup-status.json"

RUN_SH = SCRIPT_DIR / "run.sh"
SHUTDOWN_SH = SCRIPT_DIR / "shutdown.sh"
RESTART_SH = SCRIPT_DIR / "restart.sh"


def _is_running() -> int | None:
    """Return the server PID if running, else None."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # check if alive
        return pid
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return None


def _read_startup_status() -> dict | None:
    """Return the most recent startup-status.json contents, or None.

    Written by the server's :mod:`soniqboom.core.startup_status` at every
    phase boundary.  The menubar polls this so it can show the current
    phase ("Building search indexes…") in the title and tooltip while
    the HTTP API isn't yet listening — uvicorn doesn't start accepting
    requests until the lifespan startup handler completes, which is
    exactly when this file is at its most useful.

    Failures (file gone, mid-rename race, corrupt JSON) return None so
    callers fall back to the existing pid-only state.
    """
    try:
        if not STARTUP_STATUS_FILE.exists():
            return None
        with open(STARTUP_STATUS_FILE, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# Hold strong refs to spawned shell scripts so we can ``poll()`` them and
# reap their zombies, rather than letting defunct entries accumulate.
_spawned_children: list[subprocess.Popen] = []


def _reap_finished_children() -> None:
    """Best-effort: drop any spawned Popen objects whose process has exited.

    ``poll()`` is non-blocking and triggers waitpid() if the child has died,
    which is enough to clear the zombie.  Called from the status-poll timer.
    """
    if not _spawned_children:
        return
    still_alive: list[subprocess.Popen] = []
    for proc in _spawned_children:
        try:
            if proc.poll() is None:
                still_alive.append(proc)
        except Exception:
            pass
    _spawned_children[:] = still_alive


def _run_script(script: Path, *args: str) -> None:
    """Run a shell script detached from this process.

    The Popen object is tracked in ``_spawned_children`` so the status-poll
    timer can reap it once the shell exits — previously the children sat
    around as defunct/zombie processes until the menubar quit.
    """
    proc = subprocess.Popen(
        ["bash", str(script)] + list(args),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _spawned_children.append(proc)
    _reap_finished_children()


# ── Native windows ───────────────────────────────────────────────────────────

# Strong refs so windows + delegates aren't garbage-collected while visible.
_open_windows: list[tuple] = []


class _WindowCloser(NSObject):
    """Drops the window/delegate refs from _open_windows on close."""

    def windowWillClose_(self, notification):
        win = notification.object()
        for entry in list(_open_windows):
            if entry[0] is win:
                _open_windows.remove(entry)
                break


def _make_window(title: str, width: float, height: float,
                 min_width: float, min_height: float) -> NSWindow:
    style = (
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable
        | NSWindowStyleMaskMiniaturizable
        | NSWindowStyleMaskResizable
    )
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, width, height), style, NSBackingStoreBuffered, False,
    )
    win.setTitle_(title)
    win.setMinSize_(NSMakeSize(min_width, min_height))
    win.setReleasedWhenClosed_(False)
    win.center()
    return win


def _network_addresses() -> list[str]:
    """IPv4 addresses excluding loopback, parsed from `ifconfig`."""
    addrs: list[str] = []
    try:
        out = subprocess.check_output(
            ["ifconfig"], text=True, stderr=subprocess.DEVNULL, timeout=2,
        )
        for raw in out.splitlines():
            line = raw.strip()
            if line.startswith("inet ") and not line.startswith("inet 127."):
                parts = line.split()
                if len(parts) >= 2:
                    addrs.append(parts[1])
    except Exception:
        pass
    return addrs


def _http_get_json(url: str, timeout: float = 2.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _http_post_json(url: str, body: dict, timeout: float = 2.0):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")


def _gather_status() -> str:
    """Build the banner-style status text shown in the Status window."""
    base = f"http://127.0.0.1:{PORT}"
    version = "—"
    track_count: object = "—"
    state = "stopped"
    pid = _is_running()

    if pid is not None:
        state = "ready"
        try:
            version = _http_get_json(f"{base}/api/health").get("version", "—")
        except Exception:
            state = "starting"
        try:
            _http_post_json(f"{base}/api/admin/auth/skip", {"disabled": True})
            track_count = _http_get_json(
                f"{base}/api/admin/stats", timeout=4
            ).get("track_count", "—")
        except Exception:
            pass

    addrs = _network_addresses()
    hostname = f"{socket.gethostname().split('.')[0]}.local"
    conf_file = DATA_DIR / "SoniqBoom.conf"
    log_file = DATA_DIR / "log" / "soniqboom.log"

    bar = "─" * 58
    lines = [
        bar,
        f"SoniqBoom {version}  ·  {state}  ·  {track_count} tracks  ·  pid {pid if pid else '—'}",
        bar,
        f"Local:     http://localhost:{PORT}",
    ]
    for addr in addrs:
        lines.append(f"Network:   ✓  http://{addr}:{PORT}")
    lines.append(f"Hostname:  ✓  http://{hostname}:{PORT}")
    lines.append(f"Config:    {conf_file}")
    lines.append(f"Data:      {DATA_DIR}")
    lines.append(f"Log:       {log_file}")
    lines.append(bar)
    return "\n".join(lines)


def _show_status_window() -> None:
    win = _make_window("SoniqBoom — Status", 760, 540, 560, 360)
    content = win.contentView()
    bounds = content.bounds()

    pad = 24.0
    frame = NSMakeRect(
        pad, pad, bounds.size.width - 2 * pad, bounds.size.height - 2 * pad,
    )
    scroll = NSScrollView.alloc().initWithFrame_(frame)
    scroll.setHasVerticalScroller_(True)
    scroll.setHasHorizontalScroller_(False)
    scroll.setBorderType_(0)
    scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

    text = NSTextView.alloc().initWithFrame_(frame)
    text.setEditable_(False)
    text.setSelectable_(True)
    text.setRichText_(False)
    text.setFont_(NSFont.userFixedPitchFontOfSize_(13.0))
    text.setTextColor_(NSColor.textColor())
    text.setBackgroundColor_(NSColor.textBackgroundColor())
    text.setTextContainerInset_(NSMakeSize(12.0, 12.0))
    text.setString_(_gather_status())

    scroll.setDocumentView_(text)
    content.addSubview_(scroll)

    delegate = _WindowCloser.alloc().init()
    win.setDelegate_(delegate)
    _open_windows.append((win, delegate))

    win.makeKeyAndOrderFront_(None)
    NSApp.activateIgnoringOtherApps_(True)


class _CloseMessageHandler(NSObject):
    """JS bridge: window.webkit.messageHandlers.closeWindow.postMessage(null)."""

    def initWithWindow_(self, window):
        self = objc.super(_CloseMessageHandler, self).init()
        if self is None:
            return None
        self._window = window
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        if message.name() == "closeWindow" and self._window is not None:
            self._window.performClose_(None)


# JS injected into the settings webview: hides everything except the admin
# overlay, auto-opens the admin panel, and posts `closeWindow` whenever the
# overlay gets re-hidden (close button, cancel, escape, click outside).
_SETTINGS_INJECT_JS = """
(function () {
  try { localStorage.setItem('sb_skip_auth', '1'); } catch (e) {}

  var style = document.createElement('style');
  style.textContent =
    'body > *:not(#admin-overlay):not(script):not(style):not(link) { display: none !important; }' +
    'html, body { background: #000 !important; }' +
    '#admin-overlay { background: #000 !important; }';
  (document.head || document.documentElement).appendChild(style);

  function closeNative() {
    try {
      window.webkit.messageHandlers.closeWindow.postMessage(null);
    } catch (e) {}
  }

  function init(tries) {
    var btn = document.getElementById('btn-admin');
    var overlay = document.getElementById('admin-overlay');
    if (!btn || !overlay) {
      if (tries > 0) setTimeout(function () { init(tries - 1); }, 100);
      return;
    }
    btn.click();
    var obs = new MutationObserver(function () {
      if (overlay.classList.contains('hidden')) closeNative();
    });
    obs.observe(overlay, { attributes: true, attributeFilter: ['class'] });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { init(50); });
  } else {
    init(50);
  }
})();
"""


def _show_webview_window(title: str, width: float, height: float,
                          min_width: float, min_height: float,
                          inject_js: str | None = None) -> None:
    """Open a native NSWindow hosting a WKWebView pointing at the local app."""
    if not _HAS_WEBKIT:
        rumps.alert(
            f"{title} unavailable",
            "This window requires pyobjc-framework-WebKit.\n"
            "Install with:  pip install pyobjc-framework-WebKit",
        )
        return
    if _is_running() is None:
        rumps.alert(title, "Start SoniqBoom first.")
        return

    win = _make_window(title, width, height, min_width, min_height)
    content = win.contentView()
    bounds = content.bounds()

    config = WKWebViewConfiguration.alloc().init()
    handler = None

    if inject_js:
        # WKUserScriptInjectionTimeAtDocumentEnd = 1
        script = WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
            inject_js, 1, True,
        )
        ucc = config.userContentController()
        ucc.addUserScript_(script)
        handler = _CloseMessageHandler.alloc().initWithWindow_(win)
        ucc.addScriptMessageHandler_name_(handler, "closeWindow")

    pad = 0.0 if inject_js else 0.0
    frame = NSMakeRect(
        pad, pad, bounds.size.width - 2 * pad, bounds.size.height - 2 * pad,
    )
    web = WKWebView.alloc().initWithFrame_configuration_(frame, config)
    web.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

    url = NSURL.URLWithString_(f"http://127.0.0.1:{PORT}/")
    web.loadRequest_(NSURLRequest.requestWithURL_(url))

    content.addSubview_(web)

    delegate = _WindowCloser.alloc().init()
    win.setDelegate_(delegate)
    _open_windows.append((win, delegate, web, handler))

    win.makeKeyAndOrderFront_(None)
    NSApp.activateIgnoringOtherApps_(True)


def _show_settings_window() -> None:
    # Skip auth server-side so the embedded panel opens without a prompt.
    try:
        _http_post_json(
            f"http://127.0.0.1:{PORT}/api/admin/auth/skip", {"disabled": True},
        )
    except Exception:
        pass
    _show_webview_window(
        "SoniqBoom — Settings", 1180, 820, 880, 600, inject_js=_SETTINGS_INJECT_JS,
    )


# ── Menu bar app ─────────────────────────────────────────────────────────────

class SoniqBoomMenuBar(rumps.App):
    def __init__(self):
        super().__init__(
            name="SoniqBoom",
            title="🔊",
            quit_button=None,  # we add our own Quit item
        )
        self.menu = [
            rumps.MenuItem("Open SoniqBoom", callback=self.open_browser),
            None,  # separator
            rumps.MenuItem("Start SoniqBoom", callback=self.start_server),
            rumps.MenuItem("Restart SoniqBoom", callback=self.restart_server),
            rumps.MenuItem("Stop SoniqBoom", callback=self.stop_server),
            None,  # separator
            rumps.MenuItem("Settings", callback=self.show_settings),
            rumps.MenuItem("Status", callback=self.show_status),
            None,  # separator
            rumps.MenuItem("Source on GitHub", callback=self.open_source),
            rumps.MenuItem("About SoniqBoom", callback=self.show_about),
            None,  # separator
            rumps.MenuItem("Quit Menu Icon (server keeps running)", callback=self.quit_app),
        ]
        # Update state on launch
        self._update_menu_state()

        # Poll server status.  Default cadence is 5s once steady, but while
        # the server is mid-startup we tighten to 1s so the title chip
        # ("Loading library…", "Building search indexes…") tracks the
        # actual phase boundaries instead of lagging a window behind.
        self._poll_interval_idle = 5
        self._poll_interval_busy = 1
        self._timer = rumps.Timer(self._poll_status, self._poll_interval_idle)
        self._timer.start()

    def _startup_phase_chip(self) -> str | None:
        """Build a short title suffix from startup-status.json, e.g.
        ``"Loading library snapshot…"``.

        Returns None when the server isn't mid-startup (file missing, or
        marked ready, or the on-disk status is stale relative to the
        running PID).  None means the title falls back to plain "🔊".
        """
        status = _read_startup_status()
        if not status:
            return None
        # ``ready`` set → startup is done; we should show the normal title.
        if status.get("ready"):
            return None
        # Stale status file (left from a previous run that crashed before
        # mark_ready()) can confuse the title.  If the file's PID doesn't
        # match the running PID, ignore it.
        running_pid = _is_running()
        file_pid = status.get("pid")
        if file_pid and running_pid and int(file_pid) != int(running_pid):
            return None
        label = status.get("label") or ""
        msg = status.get("message") or ""
        if msg:
            return f"{label} ({msg})"
        return label or None

    def _update_menu_state(self):
        """Enable/disable menu items based on whether the server is running."""
        running = _is_running() is not None
        self.menu["Start SoniqBoom"].set_callback(None if running else self.start_server)
        self.menu["Stop SoniqBoom"].set_callback(self.stop_server if running else None)
        # Restart is only meaningful while the server is running.  Earlier
        # we wired it unconditionally so the "cold start" fallback in
        # restart_server() was reachable, but that gave the menu two
        # indistinguishable Start/Restart entries when stopped — cold start
        # is what the dedicated Start item is for.
        self.menu["Restart SoniqBoom"].set_callback(self.restart_server if running else None)
        self.menu["Open SoniqBoom"].set_callback(self.open_browser if running else None)
        self.menu["Settings"].set_callback(self.show_settings if running else None)
        # Status is always available — it shows "stopped" state too.
        self.menu["Status"].set_callback(self.show_status)

        # Title precedence:
        #   1. Mid-startup (server PID exists but startup-status says not
        #      ready) → show "🔊  Loading library snapshot…" so the user
        #      knows the menubar IS launched and what it's doing
        #   2. Running and ready → plain "🔊"
        #   3. Not running → dimmed "🔇"
        startup_chip = self._startup_phase_chip() if running else None
        if startup_chip:
            self.title = f"🔊  {startup_chip}"
        elif running:
            self.title = "🔊"
        else:
            self.title = "🔇"

    def _poll_status(self, _sender):
        self._update_menu_state()
        _reap_finished_children()
        # Adaptive cadence: tighten the poll while mid-startup so the chip
        # updates feel snappy, relax once steady-state to keep idle CPU
        # cost negligible.  rumps.Timer.interval is a plain attribute so
        # we can mutate it directly between fires.
        is_starting = bool(self._startup_phase_chip()) if _is_running() else False
        target = self._poll_interval_busy if is_starting else self._poll_interval_idle
        if self._timer.interval != target:
            self._timer.interval = target

    def open_browser(self, _sender):
        subprocess.Popen(["open", f"http://127.0.0.1:{PORT}"])

    def start_server(self, _sender):
        if _is_running():
            rumps.notification("SoniqBoom", "", "Server is already running.")
            return
        _run_script(RUN_SH, "--port", PORT)
        rumps.notification("SoniqBoom", "", "Server starting...")

    def restart_server(self, _sender):
        if not _is_running():
            # Previously this said "Server restarting..." even when nothing
            # was running — restart.sh then just starts a new instance, so
            # the message was technically wrong on a cold start.
            _run_script(RUN_SH, "--port", PORT)
            rumps.notification("SoniqBoom", "", "Server starting...")
            return
        _run_script(RESTART_SH, "--port", PORT)
        rumps.notification("SoniqBoom", "", "Server restarting...")

    def stop_server(self, _sender):
        if not _is_running():
            rumps.notification("SoniqBoom", "", "Server is not running.")
            return
        _run_script(SHUTDOWN_SH)
        rumps.notification("SoniqBoom", "", "Server stopping...")

    def show_settings(self, _sender):
        _show_settings_window()

    def show_status(self, _sender):
        _show_status_window()

    def open_source(self, _sender):
        subprocess.Popen(["open", "https://github.com/SFCyris/SoniqBoom"])

    def show_about(self, _sender):
        rumps.alert(
            title="SoniqBoom",
            message=(
                "Self-hosted music server for personal libraries.\n\n"
                "FLAC, ALAC, MP3, Opus, plus SID, MIDI, and 20+ tracker formats.\n\n"
                "Source:  https://github.com/SFCyris/SoniqBoom\n"
                "License: AGPL-3.0-or-later\n"
                "© 2026 S.F. Cyris"
            ),
        )

    def quit_app(self, _sender):
        rumps.quit_application()


if __name__ == "__main__":
    _kill_existing_instances()
    SoniqBoomMenuBar().run()
