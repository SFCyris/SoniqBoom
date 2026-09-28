# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Runs the player.js queue / shuffle and render-core tests (``tests/js/``)
under Node.

The queue state machine and the rendered-track helpers (render recovery, the
unknown-length render watch, prewarm URLs) live in the browser bundle, so their
tests are JavaScript; this wrapper makes them part of ``pytest``, one test per
suite so a failure names it.  Skipped when Node is not installed.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def _run_node_suite(name: str) -> None:
    proc = subprocess.run(
        [NODE, "--test", str(ROOT / "tests" / "js" / name)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-6000:] + proc.stderr[-2000:]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_player_queue_state_machine():
    _run_node_suite("player_queue.test.mjs")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_player_render_core():
    _run_node_suite("render_core.test.mjs")
