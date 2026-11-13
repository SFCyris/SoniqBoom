# kssrender

Renders one song of an MSX or Sega Master System KSS file (KSCC and KSSX:
PSG, SCC, OPLL / FM-PAC, MSX-AUDIO, SN76489), or an MSX MGSDRV song (`.mgs`),
to a 16-bit stereo WAV, for SoniqBoom's KSS playback
(`soniqboom/api/stream.py`, `_render_kss`) and MGSDRV playback (`_render_mgs`).

```
kssrender [-s song] [-r rate] [-m max_seconds] [-f fade_ms] [-l loops] [-q quality] FILE OUT.wav
kssrender -i [same options] FILE
kssrender --version
```

A song plays until it has looped (`-l`, default once) or stopped, or for
`-m` seconds (default 180), then fades over `-f` milliseconds (default 8000);
one that falls silent for 3 s ends there.  `-i` plays the song the same way
without writing it and prints `seconds=` — the length a render with the same
options has.

## Building

`build.sh DEST_DIR` clones libkss at the commit pinned in the Dockerfile
(`ARG LIBKSS_COMMIT`), with only the sound-chip and Z80 submodules, fetches
the MGSDRV driver header (`drivers/mgsdrv.h` of libkss's `kss-drivers` at
`KSS_DRIVERS_COMMIT`, checked against `MGSDRV_H_SHA256`) and builds
`DEST_DIR/kssrender` with a C compiler, git and curl; it checks that the
driver is in the binary.  `LICENSE_DIR=dir build.sh …` also copies the
licence files (`MGSDRV-NOTICE.txt` among them).  `install.sh` and the Docker
build both use it.

## Licences

- `kssrender.c`: AGPL-3.0-or-later (SoniqBoom).
- [libkss](https://github.com/digital-sound-antiques/libkss) by Mitsutaka
  Okazaki: ISC-style licence (`LICENSE.md`).
- Its sound-chip cores emu2149, emu2212, emu2413, emu8950, emu76489: MIT.
- Its Z80 core kmz80 by Mamiya (from NEZplug): public domain ("License: PDS",
  `kmz80.txt`).
- MGSDRV (MGSDRV (C) Ain./Gigamix, <https://www.gigamix.jp/mgsdrv/>): free
  software; its embedding licence allows building it, unmodified, into other
  software, paid or free, with that credit — and counts libkss's driver
  header, used unmodified, as unmodified.
- libkss's other drivers (KINROU5 for MuSICA `.bgm`, MoonBlaster `.mbm`,
  OPX4KSS for OPLLDRV `.opx`, MPK) state no licence and are compiled out
  (`EXCLUDE_DRIVER_*`): those formats are not played.
