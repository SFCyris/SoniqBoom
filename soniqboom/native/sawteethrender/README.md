# sawteethrender

Renders a Sawteeth module (`.st`: the binary `SWTD` format and the text
`SWTT` format) to a 16-bit stereo WAV, for SoniqBoom's Sawteeth playback.
The player is a C port of NostalgicPlayer's Sawteeth player
(`Source/Agents/Players/Sawteeth` at commit
`c6eb0c5791eaab993a03fa57a72fe1d0dc8f7a4a`) and of the parts of
NostalgicPlayer's mixer it uses.

```
sawteethrender [-r rate] [-m max_seconds] [-f fade_ms] [-l loops] FILE OUT.wav
sawteethrender -i [same options] FILE
sawteethrender --version
```

With either form, `-v VU_FILE` also writes each channel's level, 30 times a
second, as a VUMR v1 file (SoniqBoom's per-channel meter data,
`docs/vu-cache-format.md`): the peak of the channel's synthesized signal over
each 1/30 s of the output, before the channel-count scaling and the pan (0–1
as 0–255; a last partial window included), and each channel's side (0
centre, 1 left, 2 right) from its left / right amplitudes.  With `-i` the
song is synthesized for it, so the levels are the render's.

The song plays until it has ended `-l` times (default 1, at most 16) as
NostalgicPlayer detects the end — every channel has passed its loop point
since the previous end; the tick that reports the end is not played — or for
`-m` seconds (default 1800, at most 7200), whose last `-f` milliseconds
(default 8000, at most 60000) fade out linearly.  `-r` (default 44100, 8000
to 48000) is the output rate.

Playback follows NostalgicPlayer: the synthesis (waves, LFOs, envelopes,
filters, clipping) runs in single-precision floats at 44100 Hz, one tick of
the module's samples-per-tick at a time, with .NET's saturating float-to-int
conversions.  Each channel's tick is converted to 16 bits with the
channel-count scaling and mixed at full volume at the pan position
((256 − left) × 256 + 256 × right) / 512 (0 left, 256 right) of its left /
right amplitudes, as NostalgicPlayer's mixer mixes it (no interpolation, full
stereo separation; its down-mixer's volume scaling; the 32-bit mix's top 16
bits).  At a rate other than 44100 each tick lasts floor(rate ×
samples-per-tick / 44100) frames, each the sample at or before its position,
as that mixer does it.  For a song that uses the noise waveform, the song is
first synthesized silently to its end (at most 600 s of it), as
NostalgicPlayer's duration calculation does, so the noise generator starts
where it does in a freshly started NostalgicPlayer.  A file over 32 MiB is
not read (exit 1).

The WAV is written as it renders (a 44-byte header first, its sizes patched
at the end), flushed every 0.1 s of audio, so a reader can follow it.  `-i`
plays the song the same way without writing it and prints `seconds=` (the
length a render with the same options has), `title=`, `author=` and
`channels=`; the names are decoded as UTF-8 (a malformed sequence becomes
U+FFFD) and control characters are printed as spaces.  Exit status 0 with
audio written, 1 on a file that isn't a Sawteeth module, a damaged or empty
one, or a failed write (a partial WAV may be left), 2 on usage.

Where NostalgicPlayer fails on a malformed module (an exception, or a loop
that never ends), sawteethrender does this instead:

- a text module that ends where a number is expected, or holds a number that
  doesn't parse or is out of range: rejected (exit 1);
- a step's instrument past the last instrument: instrument 0 (silent);
- a note plus transposition outside the frequency table: clamped to it;
- a channel loop start past the channel's length: its loop end;
- an envelope with 128 or more points: all of them are used;
- a 12-channel module: scaled by the channel-count formula
  (1 − 0.1) / 12 + 0.1;
- a wave phase that cannot advance (a NaN): silence for the rest of the
  tick.

A module whose samples-per-tick value is below 32, which NostalgicPlayer
plays, is rejected (exit 1).

## Building

`build.sh DEST_DIR` compiles `sawteethrender.c` (C99, libc and libm only)
with `${CC:-cc} -O2 -std=c99 -ffp-contract=off` and installs
`DEST_DIR/sawteethrender`; nothing is downloaded.  `LICENSE_DIR=dir build.sh
DEST_DIR` also copies NostalgicPlayer's licence to
`dir/NostalgicPlayer-LICENSE`.

## Licences

- `sawteethrender.c`: AGPL-3.0-or-later (SoniqBoom); its playback code is
  ported from [NostalgicPlayer](https://github.com/neumatho/NostalgicPlayer):
  MIT, © 2023 Thomas Neumann (`NostalgicPlayer-LICENSE`).
