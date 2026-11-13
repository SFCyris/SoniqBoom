# xmprender

Renders a module libxmp plays to a 16-bit stereo WAV, for SoniqBoom's
playback of the formats no other engine here reads — Archimedes Tracker,
Coconizer, FlexTrax, Funktracker, Liquid Tracker, Megatracker and the packed
Amiga modules of libxmp's ProWizard loaders (Titanics Player, STIM, The Dark
Demon, Eureka) — and of the ProTracker modules `soniqboom/core/prowizard.py`
converts other packed modules to (`soniqboom/api/stream.py`, `_render_xmp`).

```
xmprender [-s sequence] [-r rate] [-m max_seconds] [-f fade_ms] [-l loops] FILE OUT.wav
xmprender -i [same options] FILE
xmprender --version
```

The sequence (a subsong; default 0) plays until it has looped `-l` times
(default 1) — to its end, for a module that doesn't loop — or for `-m`
seconds (default 1800, at most 7200), whose last `-f` milliseconds (default
8000) fade out.  Mixing: libxmp's defaults (full stereo separation) with
cubic-spline interpolation; there is no limiter, so a loud module can clip.
The WAV is written as it renders, its header's sizes patched at the end.
`-i` plays the sequence the same way without writing it and prints
`seconds=` (the length a render with the same options has), `sequences=`,
`channels=`, `format=` (libxmp's name for the format) and `title=`.  Exit
status 0 with audio written, 1 on a file libxmp can't load, an empty module,
a sequence that plays nothing or a failed write, 2 on usage.

## Building

`build.sh DEST_DIR [LIBXMP_SOURCE_DIR]` downloads the libxmp release archive
pinned in the Dockerfile (`ARG LIBXMP_VERSION`, checked against
`ARG LIBXMP_SHA256`), builds libxmp unmodified as a static library with its
own CMake build and links `DEST_DIR/xmprender`.  It needs CMake, make, a C
compiler and curl.  `LICENSE_DIR=dir build.sh …` also writes libxmp's licence
texts (`libxmp-LICENSE`).  `install.sh` and the Docker build both use it.

## Licences

- `xmprender.c`: AGPL-3.0-or-later (SoniqBoom).
- [libxmp](https://github.com/libxmp/libxmp): MIT (© Claudio Matsuoka,
  Hipolito Carraro Jr and contributors, `docs/COPYING`); its ProWizard
  loaders MIT too (© Sylvain "Asle" Chipaux, Claudio Matsuoka and Hipolito
  Carraro Jr, `src/loaders/prowizard/LICENSE.txt`); a few of its depackers
  and its Vorbis decoder state in their source that they are in the public
  domain.
