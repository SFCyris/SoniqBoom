# Vendored: HivelyTracker replay (hvl2wav)

Source: https://github.com/pete-gordon/hivelytracker (`hvl2wav/`)
Files: hvl2wav.c, replay.c, replay.h, types.h, makefile
Modified: hvl2wav.c adds a `-l` option that prints a song's length (frames at
50 Hz, and whether the song end was reached) without writing audio — used to
end AHX renders where the song ends.
Purpose: decode HivelyTracker (.hvl) modules to WAV — neither the Homebrew
uade123 build nor libopenmpt can load HVL.  Compiled on first use by
`soniqboom/api/stream.py:_ensure_hvl2wav()` into `<data_dir>/native/hvl2wav`.

HivelyTracker is distributed under the BSD 3-Clause license
(Copyright (c) 2006-2018, Pete Gordon); the full text is in `LICENSE`
in this folder.
