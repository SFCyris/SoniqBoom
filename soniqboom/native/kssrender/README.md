# kssrender

Renders one song of an MSX or Sega Master System KSS file (KSCC and KSSX:
PSG, SCC, OPLL / FM-PAC, MSX-AUDIO, SN76489) to a 16-bit stereo WAV, for
SoniqBoom's KSS playback (`soniqboom/api/stream.py`, `_render_kss`).

```
kssrender [-s song] [-r rate] [-m max_seconds] [-f fade_ms] [-l loops] [-q quality] FILE OUT.wav
kssrender --version
```

A song plays until it has looped (`-l`, default once) or stopped, or for
`-m` seconds (default 180), then fades over `-f` milliseconds (default 8000);
one that falls silent for 3 s ends there.

## Building

`build.sh DEST_DIR` clones libkss at the commit pinned in the Dockerfile
(`ARG LIBKSS_COMMIT`), with only the sound-chip and Z80 submodules, and builds
`DEST_DIR/kssrender` with a C compiler (no other dependency).
`LICENSE_DIR=dir build.sh …` also copies the licence files.  `install.sh` and
the Docker build both use it.

## Licences

- `kssrender.c`: AGPL-3.0-or-later (SoniqBoom).
- [libkss](https://github.com/digital-sound-antiques/libkss) by Mitsutaka
  Okazaki: ISC-style licence (`LICENSE.md`).
- Its sound-chip cores emu2149, emu2212, emu2413, emu8950, emu76489: MIT.
- Its Z80 core kmz80 by Mamiya (from NEZplug): public domain ("License: PDS",
  `kmz80.txt`).
- libkss's `kss-drivers` submodule is not under libkss's licence and is never
  fetched or built (`EXCLUDE_DRIVER_ALL`): the MGS, MuSICA (BGM), OPX, MPK
  and MBM formats it converts are not played.
