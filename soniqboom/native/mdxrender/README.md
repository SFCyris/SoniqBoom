# mdxrender

Renders a Sharp X68000 MDX song, with the PDX ADPCM sample file it names, to
a 16-bit stereo WAV with portable_mdx — the MXDRV 2.06+17 driver with
m_puusan's X68Sound OPM (YM2151) and ADPCM (MSM6258, PCM8) emulation — for
SoniqBoom's MDX playback (`soniqboom/api/stream.py`, `_render_mdx`).

```
mdxrender [-r rate] [-l loops] [-m max_seconds] [-f fade_ms] [-p PDX] FILE OUT.wav
mdxrender -i [-l loops] FILE
mdxrender --version
```

The length is MXDRV's own measure: a song plays until it has looped `-l`
times (default 2) and then fades out with MXDRV's fade-out; a song that
doesn't loop plays to its end; 2 s are added for the release.  The render
has exactly that length — the fade-out starts at the same loop as in the
measure.  A song longer than `-m` seconds (default 180) is cut there, its
last `-f` milliseconds (default 8000) faded out.  `-r` is 44100 (default),
48000 or 96000 Hz.

The PDX file is the one the song names (".PDX" added to a name without an
extension, a DOS path in it dropped — split outside Shift-JIS two-byte
characters), looked up beside `FILE` as spelled, else in any letter case —
by the song's own (Shift-JIS) spelling, then by that name in UTF-8 —, or
given with `-p`.  `-i` prints `seconds=`, `pdx=` (the name as the song
spells it, Shift-JIS) and `pdx_found=0|1`, and renders nothing; it reads
no PDX (the length doesn't depend on the samples).

Exit status: 0 with audio written, 1 on a file it can't read as MDX
("not an MDX file …"), on running out of memory ("out of memory"), on a
PDX it can't read ("read error: …" — one over 64 MB too) or a failed
write, 2 on usage, 3 when the PDX the song names isn't found.
MXDRV checks no bounds: a damaged song can end the process with a signal.

## Building

`build.sh DEST_DIR` clones portable_mdx at the commit pinned in the
Dockerfile (`ARG PORTABLE_MDX_COMMIT`) and builds `DEST_DIR/mdxrender`
from its unmodified `src/mxdrv`, `src/x68sound` and `src/mdx_util.c` with a
C and a C++11 compiler (no other dependency).  `LICENSE_DIR=dir build.sh …`
also copies portable_mdx's readme, which holds its licence terms.
`install.sh` and the Docker build both use it.

## Licences

- `mdxrender.cpp`: AGPL-3.0-or-later (SoniqBoom).
- [portable_mdx](https://github.com/yosshin4004/portable_mdx), as its
  readme ("ライセンス") states:
  - Yosshin's files — `include/mxdrv_context.h`, `include/x68sound_context.h`,
    `include/mdx_util.h`, `src/mdx_util` and the examples: Apache-2.0
    (portable_mdx ships no licence text; the Docker image adds Debian's).
  - `include/mxdrv.h` and `src/mxdrv/*`: from MXDRV.X (milk., K.MAEKAWA,
    Missy.M, Yatsube) by way of GORRY's C conversion MXDRVg; Yosshin's
    changes follow their licence, of which there is no written statement
    (the readme calls it freeware by X68000 convention).
  - `include/x68sound.h` and `src/x68sound/*`: from X68Sound.dll
    (© m_puusan), whose terms allow modifying it, publishing the modified
    code, building it into other software and distributing that software
    (m_puusan takes no responsibility for any damage); Yosshin's changes
    follow them.
