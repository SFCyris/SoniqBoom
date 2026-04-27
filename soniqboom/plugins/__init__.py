# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Plugin system — load built-ins and third-party entry-point plugins."""
from .base import EffectPlugin, VisualizerPlugin, registry, load_entry_point_plugins
from . import effects, visualizers


def load_all() -> None:
    """Load built-in plugins then scan entry points."""
    effects.register_builtins(registry)
    visualizers.register_builtins(registry)
    load_entry_point_plugins()
