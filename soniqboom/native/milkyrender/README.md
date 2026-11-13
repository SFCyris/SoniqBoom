# milkyrender

Renders an early Digital Sound Interface Kit module (DSIK v1: a file that
starts with `DSM\x10`) to a 16-bit stereo WAV with MilkyPlay, the playback
library of MilkyTracker, for SoniqBoom's DSIK playback
(`soniqboom/api/stream.py`, `_render_dsik_v1`) and the length the library
scan stores (`soniqboom/core/milkyrender.py`).

```
milkyrender [-r rate] [-m max_seconds] [-f fade_ms] [-l loops] FILE OUT.wav
milkyrender [-r rate] [-m max_seconds] [-f fade_ms] [-l loops] --length FILE
milkyrender --version
```

The song plays to its natural end: where it stops, or where it has played
through `-l` times (default 1) and jumps back; a song that jumps back plays
on for `-f` milliseconds (default 100) while fading out.  It plays for at
most `-m` seconds (default 3600), the last `-f` milliseconds fading out.
`--length` renders no audio: it prints, to the microsecond, how many seconds
of audio the same options write.  The mix is 48 kHz unless `-r` gives
another rate, with linear interpolation and volume ramping.  MilkyPlay's
timer advances in steps of rate / 250 frames, so at a rate that isn't a
multiple of 250 Hz the song plays slightly fast (0.23 % at 44.1 kHz).

A module whose header, instruments or patterns lie past the end of the file,
that states no channels or more than 32, more than 128 song positions or a
pattern shorter than its own length field, or whose packed pattern data runs
past its stated length or its 64th row, is refused before MilkyPlay reads
it: exit status 1 and `milkyrender: damaged DSIK module: <what is wrong>`.
So is one whose samples past the end of the file all start more than 16
bytes past it (no sample is cut by the end).
Sample data cut short by the end of the file (the file ends inside a sample,
or in the padding before one) plays, the missing bytes as silence, and
stderr has the line `milkyrender: warning: truncated sample data (N bytes
missing)` (with `--length` too).  Any other file is
refused with `milkyrender: not a DSIK v1 module`.  Exit status 0 with audio
written (or a length printed), 1 on a file it can't play or an output it
can't write, 2 on usage.

## Building

`build.sh DEST_DIR` fetches MilkyTracker at the commit pinned in the
Dockerfile (`ARG MILKYTRACKER_COMMIT`), copies the MilkyPlay files it needs
out of `src/milkyplay`, and builds `DEST_DIR/milkyrender` with a C++20
compiler (no cmake, no SDL, no other dependency).  `LICENSE_DIR=dir
build.sh …` also writes MilkyPlay's licence (`milkyplay-LICENSE`).
`install.sh` and the Docker build both use it.

## Licences

- `milkyrender.cpp`, `Limiter.h`: AGPL-3.0-or-later (SoniqBoom).
- [MilkyPlay](https://github.com/milkytracker/MilkyTracker/tree/master/src/milkyplay)
  by The MilkyTracker Team: BSD-3-Clause (MilkyTracker's `COPYING`: "Milkyplay
  … is now licensed under the New-BSD license"; the rest of MilkyTracker is
  GPL and none of it is used).
- `ResamplerFactory.cpp`: MilkyPlay's, BSD-3-Clause, with the resamplers of
  `ResamplerFast.h` only.

Each MilkyPlay file the build compiles, as checked at commit
`e3ceb05105e8c5bdc907d6451245bf5a1926b1f9` (every one carries the
MilkyTracker Team's BSD-3-Clause notice):

| Files | Licence |
|---|---|
| `AudioDriverBase.cpp/.h`, `AudioDriver_NULL.cpp/.h`, `AudioDriver_WAVWriter.cpp/.h`, `AudioDriverManager.h` | BSD-3-Clause |
| `ChannelMixer.cpp/.h` (its resonant-filter coefficients credit DUMB) | BSD-3-Clause |
| `LittleEndian.cpp/.h`, `XMFile.cpp/.h`, `MilkyPlay.h`, `MilkyPlayCommon.h`, `MilkyPlayResults.h`, `MilkyPlayTypes.h`, `Mixable.h` | BSD-3-Clause |
| `LoaderDSM.cpp`, `Loaders.h` | BSD-3-Clause |
| `MasterMixer.cpp/.h` | BSD-3-Clause |
| `PlayerBase.cpp/.h`, `PlayerSTD.cpp/.h`, `PlayerIT.cpp/.h`, `PlayerFAR.cpp/.h`, `PlayerGeneric.cpp/.h` | BSD-3-Clause |
| `ResamplerFactory.h`, `ResamplerFast.h`, `ResamplerMacros.h` | BSD-3-Clause |
| `XModule.cpp/.h` (compiled with only the DSIK v1 loader registered and without its IT-compressed sample decoder, which credits CheeseTracker / xmp / OpenCP) | BSD-3-Clause |

Not compiled: `AudioDriverManager.cpp` and the platform audio drivers
(milkyrender has its own null-device manager), `ResamplerAmiga.h` and its
`computed-blep.h` (a table from uade), `ResamplerCubic.h`, `ResamplerSinc.h`,
`Limiter.h` (a port of Steve Harris's LADSPA fast lookahead limiter; replaced
by a pass-through), every loader but `LoaderDSM.cpp`, the sample loaders
(`SampleLoaderSF2.cpp` is GPL-2.0-or-later), `ExporterXM.cpp`,
`XIInstrument.cpp`, and everything outside `src/milkyplay`.
