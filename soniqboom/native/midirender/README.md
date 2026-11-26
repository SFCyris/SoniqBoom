# midirender — MIDI on OPL3 / OPN2 FM and the MT-32, and DOS game MIDI

SoniqBoom plays MIDI-family songs through `midirender` when Settings →
Renderers → MIDI synths picks an FM chip or the MT-32 for them, and always
for the game formats FluidSynth can't read (`soniqboom/api/stream.py`,
`_render_midirender`):

```
midirender -s opl3 -b "AIL (The Fat Man 2op set, default AIL)" -c 1 SONG.xmi OUT.wav
midirender -s opn2 -c 2 SONG.mid OUT.wav
midirender -s mt32 -R /path/to/rom/folder SONG.mid OUT.wav
midirender -i SONG.hmi          # seconds= loop_start= loop_end= songs= title= copyright=
midirender -L                   # synths=opl3,opn2,mt32 and the embedded OPL3 banks
midirender -V /path/to/rom/folder   # machine= control= pcm=, or error= (exit 3)
```

libADLMIDI's MIDI sequencer reads every format: Standard MIDI files (also
RIFF `.rmi` and karaoke `.kar`), Miles AIL XMIDI (`.xmi`), DMX MUS (Doom),
HMI SOS (`.hmi`, `.hmp`), Creative GMF and Wacky Wheels KLM (OPL register
writes: OPL3 only).  The song plays once to its end (its loop is not
repeated), then the synth's release and reverb tail until it falls silent
(at most 6 s); a song past `-m` seconds (default 1800) is cut with a fade.
The WAV (16-bit stereo, 44.1 kHz) is written as it renders — a 44-byte
header first, its sizes patched at the end — so the server plays it while it
renders.

- **OPL3**: libADLMIDI with the Nuked OPL3 emulator (`-e 0`, the default; the
  DOSBox one is built too) and one of its embedded FM banks (`-b`: a number,
  the start of a bank's name, or a `.wopl` file).  The server picks the bank of
  the game's sound library — AIL's for XMI and GMF, HMI's for HMI / HMP,
  DMXOPL3 for DMX MUS — and the Fat Man's General MIDI set of the Windows 9x
  OPL3 driver for Standard MIDI files; one chip for a game format, two for a
  Standard MIDI file.
- **OPN2**: libOPNMIDI with the Nuked OPN2 (YM3438) emulator (`-e 1`, the
  default; YMFM is built too) and its own `xg.wopn` bank, compiled in.
- **MT-32**: libmt32emu (munt) with the control and PCM ROMs of the folder
  given with `-R` — full or partial images under any names; libmt32emu
  identifies them by their SHA-1.  Without `-M`, the first of
  `MACHINE_ORDER` (MT-32 control ROM 1.07 to 1.04, then the 2.x
  revisions, then the CM-32L and CM-32LN) the folder has both ROMs for is used.  The analogue output
  emulation is "accurate", resampled to 44.1 kHz at the best quality.  The
  MIDI events — SysEx included — come from libADLMIDI's sequencer
  (`adl_setRawEventHook`), so every format above plays on the MT-32 too.

Exit status: 0 with audio written, 1 for a file that can't be loaded, a song
that played nothing or a failed write, 2 for usage, 3 when the MT-32 ROMs are
missing or not recognised, 4 for a synth not compiled in.

## Building

`build.sh DEST_DIR` downloads the release archives of libADLMIDI, libOPNMIDI
and munt at the versions pinned in the Dockerfile (`ARG ADLMIDI_VERSION`,
`OPNMIDI_VERSION`, `MUNT_VERSION` with their `…_SHA256`), checks their
SHA-256, builds each unmodified as a static library with CMake and links
`midirender.c` against them.  `LICENSE_DIR=dir` copies their licence texts
into `dir/libadlmidi`, `dir/libopnmidi` and `dir/munt`.  It needs CMake, a C
and a C++14 compiler and curl.  libADLMIDI is required; a library that fails
to build leaves midirender without that synth (`midirender -L` says which it
has).  `install.sh` and the Docker build both use it.

## Licences

- [libADLMIDI](https://github.com/Wohlstand/libADLMIDI) and
  [libOPNMIDI](https://github.com/Wohlstand/libOPNMIDI) by Vitaliy Novichkov:
  each of their parts is under an "or later" licence (LGPL-2.1+, LGPL-2+,
  GPL-2+, GPL-3+, MIT, BSD-3-Clause, BSL-1.0), so each library as built is
  GPL-3.0-or-later — combinable with midirender's AGPL-3.0-or-later code
  (GPL-3.0 section 13).  libADLMIDI is built with `BUILD_NO_GREY_BANKS`: the
  banks its author lists as of unclear status ("grey zone") are not built in.
  libOPNMIDI's MAME emulators are not built: the YM2608 one compiles a
  GPL-2.0-only resampler, which can't be combined with AGPL-3.0 code, and the
  YM2612 one's source states no licence of its own.  Its `xg.wopn` bank is
  MIT.
- [munt](https://github.com/munt/munt)'s libmt32emu: LGPL-2.1-or-later.  No
  Roland ROM is included, downloaded or copied anywhere: the MT-32 plays only
  with ROMs the user provides.
- `midirender.c`: AGPL-3.0-or-later (SoniqBoom).
