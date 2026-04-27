# SoniqBoom Plugin Development

## Plugin types

| Type | Base class | Purpose |
|------|-----------|---------|
| Effect | `EffectPlugin` | Transform raw PCM audio (EQ, reverb, compression, …) |
| Visualizer | `VisualizerPlugin` | Analyse PCM and emit JSON frames (spectrum, VU, …) |

## Quickstart (external package)

```python
# mypackage/eq.py
from soniqboom.plugins.base import EffectPlugin
import struct

class EQPlugin(EffectPlugin):
    name = "parametric_eq"
    version = "1.0.0"
    description = "3-band parametric equalizer"

    def setup(self, config: dict) -> None:
        self.low = float(config.get("low", 1.0))
        self.mid = float(config.get("mid", 1.0))
        self.high = float(config.get("high", 1.0))

    def process(self, pcm: bytes, sample_rate: int, channels: int) -> bytes:
        # implement DSP here — return transformed PCM
        return pcm
```

Register via `pyproject.toml`:

```toml
[project.entry-points."soniqboom_plugin"]
parametric_eq = "mypackage.eq:EQPlugin"
```

Install the package into SoniqBoom's venv, then restart — the plugin appears automatically.

## API endpoint

```
GET /plugins          → list all loaded plugins
```
