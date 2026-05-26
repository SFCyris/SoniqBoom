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
    return tmp_path


# ── Async helpers ──────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop_policy():
    """Force the default asyncio policy on macOS — uvloop integration
    is fine for prod but the default policy is what we ship the test
    matrix against."""
    import asyncio
    return asyncio.DefaultEventLoopPolicy()
