# dsfrender

Renders a Sega Dreamcast or Naomi DSF rip — a `.dsf`, or a `.minidsf` with
the `.dsflib` libraries its `_lib` tags name — to a 16-bit stereo 44.1 kHz
WAV, for SoniqBoom's Dreamcast DSF playback (`soniqboom/core/dsf_render.py`,
called from `soniqboom/api/stream.py`'s `_render_psf`).

```
dsfrender [-r rate] [-m max_seconds] [-f fade_ms] FILE OUT.wav
dsfrender --version
```

The rip plays for `-m` seconds (decimals allowed, at most 3600; up to a
microsecond over counts as 3600) and fades out over its last `-f`
milliseconds (linearly).  Without `-m` it plays its `length` tag plus its
`fade` tag, or 180 s when it states no length; without `-f` it fades over its
`fade` tag (no fade when it has none).  Each program section is loaded into
the 8 MB of sound RAM a Naomi board has.  `-r` must be 44100, the AICA
chip's output rate.

A library named by a `_lib` tag is looked up in FILE's folder: its path as
written (`/` or `\` separated), then its file name in any case, then a file
of that name beside FILE.  A `..` component or a leading separator in a
library path is not followed (only the file name beside FILE is then tried).

The WAV is written while it renders (a 44-byte header first, its sizes
patched at the end), in 0.1-second blocks, so a reader can follow it.  Exit
status 0 with audio written, 1 on a file it can't play or a write failure, 2
on usage.

## Building

`build.sh DEST_DIR` clones Highly Theoretical and psflib at the commits
pinned in the Dockerfile (`ARG HT_COMMIT`, `ARG PSFLIB_COMMIT`) and builds
`DEST_DIR/dsfrender` with a C compiler; it needs git and zlib's headers
(`zlib1g-dev` on Debian).  `build.sh DEST_DIR HT_SOURCE_DIR PSFLIB_SOURCE_DIR`
builds from existing checkouts instead.  Only the Dreamcast part of Highly
Theoretical is compiled (`sega.c`, `dcsound.c`, `yam.c`, `arm.c`, with
`DISABLE_SSF`), and of psflib only `psflib.c`.  `LICENSE_DIR=dir build.sh …`
also copies the licence files.  `install.sh` and the Docker build both use
it.  The build is for little-endian machines (x86-64, ARM64).

## Licences

- `dsfrender.c`: AGPL-3.0-or-later (SoniqBoom).
- [Highly Theoretical](https://github.com/kode54/Highly_Theoretical): GPL-3.0
  (`LICENSE.TXT`).  Its 68000 cores — Musashi (`Core/m68k`), Starscream
  (`Core/Starscream`) and C68K (`Core/c68k`), used only for the Saturn — are
  not built.
- [psflib](https://github.com/kode54/psflib) by Christopher Snowhill: MIT
  (`LICENSE`).
