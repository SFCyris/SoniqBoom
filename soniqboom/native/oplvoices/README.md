# oplvoices

The per-channel side of an AdLib / OPL tune, for SoniqBoom's per-channel VU
meters and its voice mixer (mute / solo, per-channel scopes).  The tune is
played by AdPlug's player through the Nuked OPL3 core that libadplug carries
— the same player and emulator as `adplay -e nuked --stereo -O disk`,
SoniqBoom's AdLib renderer.

```
oplvoices [-s N] [-r RATE] [-m MASK] [-t MAXSECS] [-w OUT.wav] [-v OUT.vu] [-c OUT.raw] FILE
oplvoices --version
```

- `-s N` — the subsong (as adplay's `-s`; without it the player's default).
- `-r RATE` — the output rate (default 49716 Hz, the OPL chip's own).
- `-m MASK` — the OPL channels to hear, bit c = channel c (0–17); every
  other channel is left out of the mix (default: all).
- `-t MAXSECS` — stop after this many seconds (default 3600).
- `-w OUT.wav` — write the mix as a 16-bit stereo WAV (`-`: standard output,
  with its sizes left at 0xFFFFFFFF).
- `-v OUT.vu` — write each channel's level, 30 times a second, as a VUMR v1
  sidecar (`docs/vu-cache-format.md`) with the channel names.
- `-c OUT.raw` — write each channel's output as 18 interleaved 16-bit
  little-endian mono samples per frame, at RATE (`-`: standard output).

The results (`channels=` with `-v`, `frames=`, `seconds=`) are printed to
standard output, or to standard error when the audio goes there.  Exit status
0 on success, 1 when AdPlug can't read FILE or an output can't be written, 2
on usage.

## How it plays

Playback follows adplay's disk writer: the player's `update()` runs whenever
the sample counter (the rate per tick, the refresh rate times the samples per
sample, in adplay's float arithmetic) goes below zero; the emulator renders in
blocks of at most 512 samples, rounded to 4 samples per tick; the song ends
with the 512-sample block in which the player reports its end.  With every
channel heard the WAV is adplay's (`adplay -q -O disk -e nuked --stereo -f
RATE`), byte for byte — checked against adplay 1.9 / AdPlug 2.4 on three
AdLib Tracker 2 tunes (`tests/test_voices_mixer.py` checks one of them).  At
a rate other than the chip's, Nuked OPL3 may run two chip samples for one
output sample, and a channel's output enables a register write restores in
the first are cleared only before the next: a muted channel can sound for
one chip sample, and the per-channel output is read once per output sample.

A channel is left out of the mix by clearing its output enables (`cha` …
`chd`, the bits register C0–C8 sets in OPL3 mode, always on in OPL2 mode)
before every sample: the chip emulates as before, and the mix is the
reference mix without that channel's term.  The mixes of each channel alone
add up to the full mix exactly, unless the mix clips.

The per-channel output (`-v`, `-c`) is read after every sample from the
operator outputs each channel's mix term is made of, masked by its left /
right enables as the mix masks them: exactly each channel's own share of the
mix, one chip sample ahead of the left output and two ahead of the right
(Nuked OPL3's own output delays).  `-v` keeps only the channels that made a
sound, names them `OPL <n>` (n = 1–18) — with `BD`, `SD/HH` or `TOM/CY` for a
channel that played percussion in rhythm mode — and takes the peak of
max(|left|, |right|) over each 1/30 s, scaled so the loudest frame of any
channel is 255, and each channel's side from its summed left and right
magnitudes (left / right when one is 1.5 times the other).

## Building

```
sh soniqboom/native/oplvoices/build.sh DEST_DIR
```

One C++11 file linked against libadplug and libbinio, found with
`pkg-config adplug` (or `ADPLUG_CFLAGS` / `ADPLUG_LIBS`): Homebrew `adplug`,
Debian / Ubuntu `libadplug-dev`.  Nothing is downloaded.  `install.sh` and
the Docker build run it.

## Licence

`oplvoices.cpp` and `build.sh`: AGPL-3.0-or-later, as SoniqBoom.  It links
libadplug (AdPlug, LGPL-2.1-or-later), which contains Nuked OPL3
(LGPL-2.1-or-later), and libbinio (LGPL-2.1-or-later), dynamically — see
`THIRD-PARTY-LICENSES.md`.
