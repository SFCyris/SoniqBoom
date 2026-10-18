# Vendored: HivelyTracker replay (hvl2wav)

Source: https://github.com/pete-gordon/hivelytracker (`hvl2wav/`)
Files: hvl2wav.c, replay.c, replay.h, types.h, makefile
Modified: hvl2wav.c adds a `-l` option that prints a song's length (frames at
50 Hz, and whether the song end was reached) without writing audio — used to
end AHX renders (played through uade123) where the song ends.  The change is
noted at the top of hvl2wav.c (2026-10-09).
Purpose: decode HivelyTracker (.hvl) modules to WAV.  An `hvl2wav` already on
`PATH` is used as is (the Docker image ships one, pre-built in `/usr/local/bin`).
Otherwise `soniqboom/api/stream.py:_ensure_hvl2wav()` compiles it on first use
into `<data_dir>/native/hvl2wav` with `cc -O2 -w hvl2wav.c replay.c -o hvl2wav -lm`
(`cc`, `clang` or `gcc`); the makefile is not used.  For the AHX song-end
measurement, an `hvl2wav` on `PATH` without `-l` is passed over and the
vendored source is compiled into `<data_dir>/native/hvl2wav` instead.

HivelyTracker is distributed under the BSD 3-Clause license
(Copyright (c) 2006-2018, Pete Gordon); the full text is in `LICENSE`
in this folder.
