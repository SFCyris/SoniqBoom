# sunvoxrender

Renders a SunVox project (`.sunvox`) to a 16-bit stereo WAV with the SunVox
Library (Alexander Zolotov, WarmPlace.ru), linked statically and run offline —
no audio device is opened — for SoniqBoom's SunVox playback
(`soniqboom/api/stream.py`, `_render_sunvox`).

```
sunvoxrender [-r rate] [-m max_seconds] [-f fade_ms] FILE OUT.wav
sunvoxrender -i FILE
sunvoxrender --version
```

The length is the project's own (its last pattern's end; the project plays
once).  A project longer than `-m` seconds (default 1800) is cut there, its
last `-f` milliseconds (default 8000) faded out.  `-i` prints `seconds=` and
`title=` (the project's name) and renders nothing.  Exit status 0 with audio
written, 1 on a file that isn't a SunVox project or that the library can't
load, an empty project or a failed write, 2 on usage.

The library runs in a temp folder of its own (under `TMPDIR`, else `/tmp`),
removed at the end with what the library left there.  The library looks for
its config file (`sunvox_dll_config.ini`) in its work folder, then in its
config folder (`~/.config/SunVox Library`; on macOS `~/Library/Application
Support/SunVox Library`), and its list of the places to look isn't
terminated — with the file in neither, it reads past the list and crashes.
So the temp folder holds an empty config file where the library looks first
and its HOME points there: on Linux the library starts in the folder (its
work folder, where it also writes a test file at start); on macOS, where
the system takes its home from `CFFIXED_USER_HOME` when a process starts,
sunvoxrender starts itself again with that set to the folder, through a
link to itself in the folder's `bin/` — the library's work folder on macOS
is the one above the program's, so its write test lands in the temp folder
too.

## Building

`build.sh DEST_DIR` downloads the SunVox Library release archive pinned in
the Dockerfile (`ARG SUNVOX_LIB_VERSION`, checked against
`ARG SUNVOX_LIB_SHA256`) — it holds the library's full source — builds the
library unmodified as a static library with its own Makefile, and links
`DEST_DIR/sunvoxrender`.  It needs make, a C and a C++ compiler, curl and
unzip; on Linux the ALSA and JACK headers (`libasound2-dev`,
`libjack-jackd2-dev`: the library links ALSA and loads JACK only when asked
to).  `LICENSE_DIR=dir build.sh …` also copies the library's licence texts.
`install.sh` and the Docker build both use it.

## Licences

- `sunvoxrender.c`: AGPL-3.0-or-later (SoniqBoom).
- [SunVox Library](https://warmplace.ru/soft/sunvox/sunvox_lib.php): its
  source is under the MIT licence (`lib_sunvox/LICENSE`; the SunDog engine
  too, `sundog.txt`); its use terms (`LICENSE.txt`) allow using it in any
  product, commercial ones too, with the line "Powered by SunVox (modular
  synth & tracker) Copyright (c) 2008 - 2026, Alexander Zolotov
  <nightradio@gmail.com>, WarmPlace.ru" and the other texts of that folder
  in its documentation: libFLAC's and Tremor's BSD licences (`libflac.txt`,
  `tremor.txt`).
