# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Built-in audio effect plugins.

Add new effects here or in separate packages (entry_point group "soniqboom_plugin").
"""
from __future__ import annotations

import struct
from soniqboom.plugins.base import EffectPlugin, PluginRegistry


class PassthroughEffect(EffectPlugin):
    """Identity effect — returns PCM unchanged. Template for new effects."""
    name = "passthrough"
    version = "1.0.0"
    description = "No-op passthrough. Use as a template for custom effects."

    def process(self, pcm: bytes, sample_rate: int, channels: int) -> bytes:
        return pcm


class GainEffect(EffectPlugin):
    """Simple gain (volume multiplier) effect."""
    name = "gain"
    version = "1.0.0"
    description = "Multiply PCM amplitude by a gain factor."

    def setup(self, config: dict) -> None:
        self.gain = float(config.get("gain", 1.0))

    def process(self, pcm: bytes, sample_rate: int, channels: int) -> bytes:
        fmt = f"{len(pcm) // 2}h"
        samples = struct.unpack(fmt, pcm)
        clipped = [max(-32768, min(32767, int(s * self.gain))) for s in samples]
        return struct.pack(fmt, *clipped)


def register_builtins(registry: PluginRegistry) -> None:
    registry.register(PassthroughEffect())
    p = GainEffect()
    p.setup({"gain": 1.0})
    registry.register(p)
