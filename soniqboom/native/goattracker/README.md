# gt2reloc (GoatTracker 2)

SoniqBoom plays a GoatTracker song (`.sng`, a `GTS!` / `GTS2`–`GTS5`
header) by converting it with GoatTracker's own packer/relocator `gt2reloc`
to a C64 PSID tune and playing that with sidplayfp
(`soniqboom/api/stream.py`, `_render_goattracker`): the song's first
subtune, in stereo, with the SID settings every SID play uses, for the SID
duration setting (a GoatTracker song loops), its last 8 s faded out.

```
gt2reloc <songname> <outfile.sid> [options]
```

`gt2reloc` copies its file arguments into 60-byte buffers (GoatTracker's
`MAX_FILENAME`): a longer path overflows them.  The server converts in a
folder of its own under `/tmp` with short names (`s.sng` → `s.sid`).  It
packs any file starting `GTS…` and never fails on content: a song whose
packing report shows no instrument or no pattern data (an empty or damaged
song) is refused instead of played as silence.  The `.sng` format doesn't
store GoatTracker's speed multiplier (a tracker setting): a song written
for 2x speed plays at the normal speed.

## Building

`build.sh DEST_DIR` downloads the GoatTracker release archive pinned in the
Dockerfile (`ARG GOATTRACKER_VERSION`, checked against
`ARG GOATTRACKER_SHA256`), removes the object files it ships and builds
`DEST_DIR/gt2reloc` from its unmodified source with GoatTracker's Linux
makefile.  It needs make, a C and a C++ compiler, curl, unzip and SDL 1.2's
headers (`sdl-config`; sdl12-compat) — but links `sdlstub.c` in place of the
SDL library: gt2reloc calls no SDL function, while linking SDL made it load
SDL2 at start, which on macOS stalled before `main` ran.
`LICENSE_DIR=dir build.sh …` also copies GoatTracker's licence and author
notes.  `install.sh` and the Docker build both use it.

## Licences

- `sdlstub.c`: GPL-2.0-or-later (SoniqBoom), as it is linked into
  GoatTracker's code.
- [GoatTracker 2](https://sourceforge.net/projects/goattracker2/) by Lasse
  Öörni (Cadaver) and its authors (`authors`): GPL-2.0-or-later
  (`copying`; reSID and reSIDfp, which it includes, under the GPL too —
  `readme_resid.txt`).
