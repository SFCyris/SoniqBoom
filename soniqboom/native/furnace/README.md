# Furnace (headless) — FamiTracker and DefleMask modules

SoniqBoom plays a FamiTracker module (`.ftm` with a `FamiTracker Module`
header) and a DefleMask module (`.dmf`: zlib data starting
`.DelekDefleMask.`) with Furnace's headless player (`soniqboom/api/stream.py`,
`_render_famitracker`):

```
furnace-headless -output OUT.wav -loops 1 FILE.ftm
```

Furnace imports the module (FamiTracker 0.4.5 and earlier files, a few
effects not carried over; DefleMask files up to its format version 27) and
renders its first song to its end, its loop once more, as a 16-bit stereo WAV.  The length is the render's.  Furnace
writes a config folder under HOME (`~/.config/furnace`, on macOS
`~/Library/Application Support/Furnace`) on every start: the server runs it
with a temporary HOME, removed after.

## Building

`build.sh DEST_DIR` clones Furnace at the commit pinned in the Dockerfile
(`ARG FURNACE_COMMIT`, the v0.6.8.3 release) with only the `extern/fmt`
and `extern/adpcm` submodules and builds `DEST_DIR/furnace` unmodified with
CMake as a headless player, installed as `furnace-headless` (a `furnace` on
PATH is Furnace the tracker, which an install never replaces; the server
uses it when there is no `furnace-headless`): no GUI, SDL, audio device, MIDI, JACK or
PortAudio, the system zlib (Furnace's vendored copy doesn't compile against
current macOS SDKs).  It needs CMake, a C and a C++17 compiler, git and
zlib's headers.  `LICENSE_DIR=dir build.sh …` also copies Furnace's licence.
`install.sh` and the Docker build both use it.

## Licences

- [Furnace](https://github.com/tildearrow/furnace) by tildearrow and
  contributors: GPL-2.0-or-later (`LICENSE`); its FamiTracker importer is
  partly derived from FamiTracker's source (GPL-2.0-or-later), its emulation
  cores and libraries are under their own licences in their source files
  (NSFplay's NES core: "You can reuse these source code freely").  The demo
  songs in its source tree are not under the GPL and are not installed.
