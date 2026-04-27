# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Built-in visualizer plugins.

The browser-side Web Audio API handles most real-time visualisation natively.
Server-side visualizers are for headless or advanced analysis (e.g., BPM, key detection).
"""
from __future__ import annotations

import struct
import time
from typing import Any

from soniqboom.plugins.base import PluginRegistry, VisualizerPlugin


class WaveformVisualizer(VisualizerPlugin):
    """Downsample PCM to a waveform array suitable for canvas rendering."""
    name = "waveform"
    version = "1.0.0"
    description = "Returns a downsampled PCM waveform (256 points) for canvas display."

    def analyze(self, pcm: bytes, sample_rate: int, channels: int) -> dict[str, Any]:
        samples = struct.unpack(f"{len(pcm) // 2}h", pcm)
        # Mix to mono if stereo
        if channels == 2:
            samples = [(samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples) - 1, 2)]
        # Downsample to 256 points
        n = 256
        step = max(1, len(samples) // n)
        points = [samples[i] / 32768.0 for i in range(0, len(samples), step)][:n]
        return {"type": "waveform", "data": points, "timestamp": time.time()}


class RMSVisualizer(VisualizerPlugin):
    """Returns the RMS energy (single value 0-1) per chunk."""
    name = "rms"
    version = "1.0.0"
    description = "Returns RMS energy per audio chunk — useful for VU meter display."

    def analyze(self, pcm: bytes, sample_rate: int, channels: int) -> dict[str, Any]:
        import math
        samples = struct.unpack(f"{len(pcm) // 2}h", pcm)
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0 if samples else 0.0
        return {"type": "rms", "data": round(rms, 4), "timestamp": time.time()}


def register_builtins(registry: PluginRegistry) -> None:
    registry.register(WaveformVisualizer())
    registry.register(RMSVisualizer())
