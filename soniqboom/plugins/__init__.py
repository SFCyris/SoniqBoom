# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Plugin system — load built-ins and third-party entry-point plugins."""
from .base import EffectPlugin, VisualizerPlugin, registry, load_entry_point_plugins
from . import effects, visualizers


def load_all() -> None:
    """Load built-in plugins then scan entry points.

    Pulls per-plugin configuration out of ``SoniqBoom.conf`` under the
    ``plugins`` key (default empty) and passes it through to entry-point
    plugins so their ``setup(config)`` hook actually receives data — the
    earlier wiring accepted a ``config`` arg but no caller passed one.
    """
    from soniqboom.config import load_local_conf

    effects.register_builtins(registry)
    visualizers.register_builtins(registry)

    plugin_config: dict = {}
    try:
        plugin_config = load_local_conf().get("plugins", {}) or {}
        if not isinstance(plugin_config, dict):
            plugin_config = {}
    except Exception:
        # Plugins still load with empty config if the conf is unreadable.
        plugin_config = {}
    load_entry_point_plugins(plugin_config)
