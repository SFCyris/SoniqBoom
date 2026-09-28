# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pytest fixtures shared across the SoniqBoom test suite.

Two patterns we lean on:

  • ``tmp_data_dir`` — every test that touches the persistence /
    conversion-cache / users layers gets a clean ``$DATA_DIR`` so
    parallel tests can't poison each other's snapshots.

  • ``sine_wav`` — a 2-second 44.1 kHz / stereo / 16-bit WAV
    generated via ffmpeg's ``lavfi sine`` source, suitable for
    feeding into cast_pipe and cast_render without dragging real
    library files into version control.

The harness deliberately avoids hardcoding the project root path —
the ``ROOT`` fixture walks up from the conftest until it finds a
``pyproject.toml``, which makes the suite portable across
checkout locations and CI runners.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


# ── Config isolation ───────────────────────────────────────────────────────
# ``soniqboom.config`` resolves SoniqBoom.conf ONCE, at import.  Point it at a
# throw-away file before anything imports it, so the suite runs on the shipped
# defaults (every optional service mounted) instead of whatever this machine's
# personal config says — with Cast switched off locally, main.py mounts 404
# stubs and the cast API tests fail — and can never write to the real file.
#
# Every service is switched ON in it: main.py decides which routers to mount at
# import, and it is imported once per process — a fixture that enables a service
# later (``set_service_enabled("dlna_server", True)``) only works if no earlier
# test imported the app first.  (That fixture used to write into the REAL conf.)
if "SONIQBOOM_CONF" not in os.environ:
    import json
    import tempfile
    _conf = Path(tempfile.mkdtemp(prefix="soniqboom-test-conf-")) / "SoniqBoom.conf"
    _conf.write_text(json.dumps({"services": {
        "subsonic": True, "multiroom": True, "cast": True, "dlna_server": True}}))
    os.environ["SONIQBOOM_CONF"] = str(_conf)


# ── Path bootstrap ─────────────────────────────────────────────────────────

def _find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for parent in (cur, *cur.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError(f"Could not find repo root (pyproject.toml) starting from {start}")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repository root (the directory containing
    ``pyproject.toml``)."""
    return _find_repo_root(Path(__file__).parent)


@pytest.fixture(scope="session", autouse=True)
def add_repo_root_to_path(repo_root: Path) -> None:
    """Make ``soniqboom`` importable from anywhere the tests run.

    We don't rely on pip-install editable mode being active because
    the test suite is often run from a fresh clone before ``install.sh``
    has finished.  Inserting at position 0 also wins over any older
    site-packages install that might shadow the working tree.
    """
    p = str(repo_root)
    if p not in sys.path:
        sys.path.insert(0, p)


# ── Optional-binary skip markers ───────────────────────────────────────────

def _which(name: str) -> str | None:
    return shutil.which(name)


@pytest.fixture(scope="session")
def have_ffmpeg() -> bool:
    return _which("ffmpeg") is not None


@pytest.fixture(scope="session")
def have_uade123() -> bool:
    """uade123 is the renderer for AHX, Hively, and ~200 other Amiga
    formats.  Optional dep — tests that need it skip when absent."""
    return _which("uade123") is not None


@pytest.fixture(scope="session")
def have_sidplayfp() -> bool:
    return _which("sidplayfp") is not None


@pytest.fixture(scope="session")
def have_openmpt123() -> bool:
    return _which("openmpt123") is not None


# ── Test-asset fixtures ───────────────────────────────────────────────────

@pytest.fixture()
def sine_wav(tmp_path: Path, have_ffmpeg: bool) -> Path:
    """2-second 440 Hz sine, 44.1 kHz / stereo / 16-bit.

    Re-generated per test (deterministic content, ~350 KB) so a test
    that mutates the file in-place can't affect siblings.
    """
    if not have_ffmpeg:
        pytest.skip("ffmpeg not on PATH")
    out = tmp_path / "sine.wav"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-ac", "2", "-ar", "44100", str(out),
        ],
        check=True,
    )
    return out


# ── Isolated data dir ──────────────────────────────────────────────────────

@pytest.fixture()
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point SoniqBoom at a fresh data dir for this test only.

    Setting ``SONIQBOOM_DATA_DIR`` matches the env-var our config
    layer honours (see config.get_data_dir).  We avoid touching
    the user's real ``~/Library/Application Support/SoniqBoom``
    so the test suite is safe to run on a developer's daily-driver
    machine.
    """
    monkeypatch.setenv("SONIQBOOM_DATA_DIR", str(tmp_path))
    # ``settings`` is read once at import, so the env var alone doesn't
    # redirect ``config.get_data_dir()`` — patch the live value too.
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _isolate_cache_dirs(tmp_path_factory: pytest.TempPathFactory,
                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test renders / caches art into a throw-away dir, never into the
    developer's real ``~/Library/Application Support/SoniqBoom/cache``.  A test
    that points a cache elsewhere itself still wins (its monkeypatch runs
    after this fixture)."""
    from soniqboom.config import settings
    d = tmp_path_factory.mktemp("cache")
    monkeypatch.setattr(settings, "conversion_cache_dir", str(d / "conversion"))
    monkeypatch.setattr(settings, "art_cache_dir", str(d / "art"))


@pytest.fixture(autouse=True)
def _fresh_repair_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """No repair task, and no pending one-time-backfill settle, carries over
    from another test (their event loops are closed: a task or done-callback
    left pending would read as a run still finishing — ``repair._busy``)."""
    from soniqboom.core import repair
    monkeypatch.setattr(repair, "_task", None)
    monkeypatch.setattr(repair, "_pending_settles", 0)
    monkeypatch.setattr(repair, "_probe_inflight", {})
    # Runs in a test follow each other in milliseconds: count each (the
    # spacing itself is tested on its own).
    monkeypatch.setattr(repair, "_UNREACHABLE_INTERVAL_S", 0.0)


@pytest.fixture(autouse=True)
def _isolate_songdb_index(tmp_path_factory: pytest.TempPathFactory,
                          monkeypatch: pytest.MonkeyPatch) -> None:
    """The UADE song-database index lives in the data dir, which is the
    developer's real one unless a test redirects it: point it at a throw-away
    path so a real index never feeds the post-scan runner in a test (a test
    that builds its own index still wins — its monkeypatch runs later)."""
    from soniqboom.core import songdb
    p = tmp_path_factory.mktemp("songdb") / "songdb.sqlite"
    monkeypatch.setattr(songdb, "_db_path", lambda: p)


# ── Async helpers ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_game_title_downloads(tmp_path_factory: pytest.TempPathFactory,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """Downloaded game-title lists (TOSEC / Redump) go to a per-test folder,
    and no download job or pass signature leaks between tests."""
    from soniqboom.core import game_titles
    d = tmp_path_factory.mktemp("game_titles")
    monkeypatch.setattr(game_titles, "_download_dir", lambda: d)
    monkeypatch.setattr(game_titles, "_jobs", {})
    monkeypatch.setattr(game_titles, "_last_sig", None)
    monkeypatch.setattr(game_titles, "_last_result", None)
    monkeypatch.setattr(game_titles, "_pending", False)
    monkeypatch.setattr(game_titles, "_running", False)
    # A list download re-runs the scene enrichment in the app; in tests the data
    # dir may be the developer's, whose Modland index that runner would read —
    # only this module's own pass runs (tests of the hook patch it back).
    monkeypatch.setattr(game_titles, "_lists_changed_unpatched", game_titles._lists_changed,
                        raising=False)
    monkeypatch.setattr(game_titles, "_lists_changed", game_titles.schedule)


@pytest.fixture(scope="session")
def event_loop_policy():
    """Force the default asyncio policy on macOS — uvloop integration
    is fine for prod but the default policy is what we ship the test
    matrix against."""
    import asyncio
    return asyncio.DefaultEventLoopPolicy()
