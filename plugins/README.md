# SoniqBoom Plugin Development

## Plugin types

| Type | Base class | Interface |
|------|-----------|-----------|
| Effect | `EffectPlugin` | `process(pcm, sample_rate, channels)` takes raw PCM and returns PCM in the same format (EQ, reverb, compression, …) |
| Visualizer | `VisualizerPlugin` | `analyze(pcm, sample_rate, channels)` takes raw PCM and returns a JSON-serialisable dict (spectrum, VU, …) |

PCM is signed 16-bit little-endian; `channels` is 1 (mono) or 2 (stereo). Both base
classes live in `soniqboom.plugins.base` and share `name`, `version`, `description` and
`setup(config)`, which is called once when the plugin is loaded.

SoniqBoom loads plugins at startup and lists them at `GET /api/plugins`. Playback and the
web player don't run them: an effect doesn't change what you hear, and a visualizer's
frames aren't shown anywhere.

## Built-in plugins

| Name | Type | What it does |
|------|------|--------------|
| `passthrough` | Effect | Returns the PCM unchanged — a template for new effects |
| `gain` | Effect | Multiplies the samples by a gain factor (loaded with `1.0`) |
| `waveform` | Visualizer | A 256-point downsampled waveform |
| `rms` | Visualizer | The RMS energy (0–1) of each chunk |

They live in `soniqboom/plugins/effects/` and `soniqboom/plugins/visualizers/`.

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

Install the package into SoniqBoom's Python environment (`.venv` in the SoniqBoom folder
for an `install.sh` install), then restart — the plugin is loaded and listed
automatically. A plugin that fails to import, construct or set up is skipped with a
warning in the log; the others still load.

## Configuration

An external plugin's `setup(config)` receives the entry under its `name` in the `plugins`
section of `SoniqBoom.conf` (an empty dict when there is none); the built-in plugins don't
read this section:

```json
"plugins": {
  "parametric_eq": { "low": 1.2, "mid": 1.0, "high": 0.9 }
}
```

## API endpoint

```
GET /api/plugins      → list all loaded plugins (no sign-in needed)
```

The answer lists `effects` and `visualizers`, each entry with `name`, `version` and `desc`.
