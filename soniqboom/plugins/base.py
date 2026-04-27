# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Plugin base classes and registry.

Effect plugins:    process PCM audio bytes in-place (DSP chain).
Visualizer plugins: consume PCM and emit JSON frames via WebSocket.

Registering a plugin
--------------------
Option A — built-in: place it under soniqboom/plugins/effects/ or /visualizers/
Option B — external package: add an entry_point group "soniqboom_plugin" pointing
           to your plugin class.

Example entry_point (pyproject.toml of external package):
    [project.entry-points."soniqboom_plugin"]
    my_eq = "mypackage.eq:EQPlugin"
"""
from __future__ import annotations

import importlib
import importlib.metadata
import logging
from abc import ABC, abstractmethod
from typing import Any

log = logging.getLogger(__name__)


# ── Abstract interfaces ───────────────────────────────────────────────────────

class SoniqPlugin(ABC):
    name: str = "unnamed"
    version: str = "0.0.0"
    description: str = ""

    def setup(self, config: dict) -> None:
        """Called once on load. Override to accept config."""

    def teardown(self) -> None:
        """Called when plugin is unloaded."""


class EffectPlugin(SoniqPlugin):
    """Audio DSP effect — transforms raw PCM bytes."""

    @abstractmethod
    def process(self, pcm: bytes, sample_rate: int, channels: int) -> bytes:
        """
        Args:
            pcm:         raw signed 16-bit little-endian PCM bytes
            sample_rate: e.g. 44100
            channels:    1 = mono, 2 = stereo
        Returns:
            Transformed PCM bytes (same format).
        """


class VisualizerPlugin(SoniqPlugin):
    """Real-time visualizer — turns PCM into JSON frames for the browser."""

    @abstractmethod
    def analyze(self, pcm: bytes, sample_rate: int, channels: int) -> dict[str, Any]:
        """
        Returns a JSON-serialisable dict sent to the frontend via WebSocket.
        Suggested keys: "type", "data", "timestamp"
        """


# ── Registry ─────────────────────────────────────────────────────────────────

class PluginRegistry:
    def __init__(self) -> None:
        self._effects: dict[str, EffectPlugin] = {}
        self._visualizers: dict[str, VisualizerPlugin] = {}

    def register(self, plugin: SoniqPlugin) -> None:
        if isinstance(plugin, EffectPlugin):
            self._effects[plugin.name] = plugin
            log.info("Registered effect: %s", plugin.name)
        elif isinstance(plugin, VisualizerPlugin):
            self._visualizers[plugin.name] = plugin
            log.info("Registered visualizer: %s", plugin.name)
        else:
            log.warning("Unknown plugin type: %s", type(plugin))

    def effects(self) -> list[EffectPlugin]:
        return list(self._effects.values())

    def visualizers(self) -> list[VisualizerPlugin]:
        return list(self._visualizers.values())

    def get_effect(self, name: str) -> EffectPlugin | None:
        return self._effects.get(name)

    def get_visualizer(self, name: str) -> VisualizerPlugin | None:
        return self._visualizers.get(name)

    def info(self) -> dict:
        return {
            "effects": [{"name": p.name, "version": p.version, "desc": p.description}
                        for p in self._effects.values()],
            "visualizers": [{"name": p.name, "version": p.version, "desc": p.description}
                            for p in self._visualizers.values()],
        }


registry = PluginRegistry()


def load_entry_point_plugins() -> None:
    """Auto-discover plugins via Python package entry points."""
    try:
        eps = importlib.metadata.entry_points(group="soniqboom_plugin")
    except Exception:
        return
    for ep in eps:
        try:
            cls = ep.load()
            registry.register(cls())
        except Exception as exc:
            log.warning("Failed to load plugin %s: %s", ep.name, exc)
