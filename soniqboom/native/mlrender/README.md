# mlrender

Renders one subsong of a Musicline Editor module (`MLEDMODL`, `.ml`) to a
16-bit stereo WAV with musicline_playback, the C++ port of the Musicline
Editor 1.16 replayer, for SoniqBoom's Musicline playback
(`soniqboom/api/stream.py`, `_render_mlrender`): uade123 plays Musicline
Editor modules, and a tune it renders only as silence is rendered by
mlrender instead.

```
mlrender [-s subsong] [-r rate] [-m max_seconds] [-f fade_ms] [-p separation] FILE OUT.wav
mlrender -i [-s subsong] [-r rate] [-m max_seconds] [-f fade_ms] [-p separation] FILE
mlrender --version
```

A subsong (numbered from 0) plays until every channel has reached its end
command, or until the replayer is back in a state it has played before (the
song loops; it then fades over `-f` milliseconds, default 8000), or for `-m`
seconds (default 600, the last `-f` milliseconds fading out).  Once audio has
started, output that stays digitally silent for 20 s ends the song, keeping
0.5 s of the silence; a subsong silent for its first 30 s ends at 30 s.
`-r` sets the sample rate, 8000 to 48000 Hz (default 44100), `-p` the stereo
separation from 0 (mono) to 1 (hard left / right, the default).  A tune whose
tempo is under 32 is not played.  `OUT.wav` `-` writes the WAV to standard
output.

`-i` prints the subsong count and, for each subsong (or the one `-s` names),
the length a render with the same options has, how it ends (`end`, `loop`,
`silence` or `max`) and its name:

```
subsongs 2
subsong 0 266.038 silence Xenex-Remix
subsong 1 11.860 loop The Beat
```

A module whose chunks the replayer's loader would read or store out of
bounds — a chunk or channel list running past the end of the file, more
tunes (256), channels (8 per tune), steps (256 per channel, 128 per
arpeggio), instruments or samples (255) than the replayer holds, a part
number over 1023 or an arpeggio number over 255, packed sample data cut
short — is refused with one line, `mlrender: damaged Musicline Editor
module: <why>` (e.g. `sample 3's data runs past the end`).  An unpacked
sample of at most 128 KB that the end of the file cuts short plays with
the missing bytes as silence, and stderr has the line `mlrender: warning:
truncated sample data (N bytes missing)` (with `-i` too).

Exit status 0 on success; 1 on a file it can't read, load, play or write
(also a refused module, and out of memory); 2 on usage; 3 on a subsong the
module doesn't have (`mlrender: no subsong 2 (the module has 1)`).

## Building

`build.sh DEST_DIR` fetches DEViLBOX at the commit pinned in the Dockerfile
(`ARG DEVILBOX_COMMIT`) as a shallow sparse checkout of DEViLBOX's `LICENSE`
and `THIRD_PARTY_NOTICES.md` and the port's replayer sources only
(`third-party/musicline_playback-main/musicline/*.cpp`, `*.h` and
`player/mline_backend.*`), and builds `DEST_DIR/mlrender` with a C++20
compiler and git (no other dependency).  `build.sh DEST_DIR
DEVILBOX_SOURCE_DIR` builds from an existing DEViLBOX checkout instead.
`LICENSE_DIR=dir build.sh …` also copies the licence files.  `install.sh`
and the Docker build both use it.

## Changes to the replayer

The port is built with these changes:

- its tick buffers (`MAX_INTERNAL_TICK` in `module.h`) hold 2400 frames
  instead of 1200, enough for its slowest tempo (32);
- `musicline_playback.patch` (applied with `git apply`; the build stops if
  it doesn't apply) adds bounds checks, with `ml_bounds.h`, the list of the
  float buffers the replayer reads waveform data from (the module's
  samples, each channel's effect wave buffers, its zero sample):
  - a mixer channel (`StoreMix`, `NormalMix`, `softMixResample`) whose
    waveform or repeat region doesn't lie within one of those buffers plays
    nothing (as an empty one does);
  - the phase, mix, transform, filter and resonance effects read a source
    wave that doesn't lie within one of them as silence, write at most the
    4096 samples of their wave buffers and look up their tables (sizer,
    filter and resonance lists) within the tables (at most 255 phase
    lookups); a missing mix or transform wave (no such sample) is silence,
    a transform that would blend in a 7th wave leaves the wave as it is, and
    the filter and resonance leave a wave of no samples alone;
  - an instrument naming an arpeggio table over 255 has none; an arpeggio
    jump goes to one of its 128 steps, and an arpeggio that meets 256 jumps
    without reaching a step ends;
  - a loop effect on an instrument whose sample isn't loaded loops silence;
  - a pitch below `PalPitchTable` (-32 to -1) reads 0x4040 (the end of the
    Musicline Editor replayer's Square wave), and any pitch past the table's
    last entry (1952 to 2112) reads that entry;
  - samples are allocated filled with zeros and freed with `delete[]`.
- it is compiled as C++20.

`ml_bounds.h` is SoniqBoom's (AGPL-3.0-or-later); `musicline_playback.patch`
changes musicline_playback, under its licence (below).

## Licences

- `mlrender.cpp`: AGPL-3.0-or-later (SoniqBoom).
- musicline_playback (in [DEViLBOX](https://github.com/spotUP/DEViLBOX),
  `third-party/musicline_playback-main`) carries no licence statement of its
  own.  DEViLBOX's `THIRD_PARTY_NOTICES.md` (copied as
  `DEViLBOX-THIRD_PARTY_NOTICES.md`) gives its licence as GPL-2.0-or-later
  by inheritance from UADE, which it embeds.  DEViLBOX, which publishes it,
  is GPL-3.0 (its `LICENSE`, copied as `DEViLBOX-LICENSE`).
- Only the port's C++ replayer files are fetched and built
  (`musicline/*.cpp`, `*.h`, `player/mline_backend.*`); the uade copy the
  port embeds (`musicline/uade`) and the rest of DEViLBOX never are.
- The port follows the 68000 Musicline Editor 1.16 replayer (`Mline116.asm`,
  which DEViLBOX does not publish).  uade publishes an earlier source of
  that replayer (`mlineplayer102.asm`, © John Carehag, in
  `amigasrc/players/other/musicline_editor`) under the LGPL.
