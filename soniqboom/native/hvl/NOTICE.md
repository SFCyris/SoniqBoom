# Vendored: HivelyTracker replay (hvl2wav)

Source: https://github.com/pete-gordon/hivelytracker (`hvl2wav/`)
Files: hvl2wav.c, replay.c, replay.h, types.h, makefile
Purpose: decode HivelyTracker (.hvl) modules to WAV — neither the Homebrew
uade123 build nor libopenmpt can load HVL.  Compiled on first use by
`soniqboom/api/stream.py:_ensure_hvl2wav()` into `<data_dir>/native/hvl2wav`.

HivelyTracker is distributed under the BSD 3-Clause license.
