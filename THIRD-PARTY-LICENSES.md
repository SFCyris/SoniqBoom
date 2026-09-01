# Third-Party Software Licenses

SoniqBoom uses the following third-party software. We gratefully acknowledge
the work of these projects and their contributors.


## Python Dependencies

| Package | License | Usage |
|---------|---------|-------|
| [FastAPI](https://github.com/tiangolo/fastapi) | MIT | Web framework powering the SoniqBoom REST API |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI server used to run the FastAPI application |
| [Mutagen](https://github.com/quodlibet/mutagen) | GPL-2.0 | Audio metadata parsing during library scanning (used as a library; SoniqBoom's use is compatible under process separation for the scanning component) |
| [aiofiles](https://github.com/Tinche/aiofiles) | Apache-2.0 | Asynchronous file I/O for non-blocking file access |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT | Data validation and settings management for API models |
| [pydantic-settings](https://github.com/pydantic/pydantic-settings) | MIT | Configuration management via environment variables and settings files |
| [python-multipart](https://github.com/Kludex/python-multipart) | Apache-2.0 | Multipart form data parsing for file upload endpoints |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | Async HTTP client for external service communication |
| [Pillow](https://github.com/python-pillow/Pillow) | HPND (Historical Permission Notice and Disclaimer) | Image processing for album art extraction and thumbnail generation |
| [mido](https://github.com/mido/mido) | MIT | MIDI file reading and metadata parsing |


## External Tools (invoked via subprocess)

These tools are called as separate processes. SoniqBoom does not link against
or include their source code.

| Tool | License | Usage |
|------|---------|-------|
| [FFmpeg](https://ffmpeg.org/) | LGPL-2.1 / GPL-2.0 (depending on build configuration) | Audio transcoding and format conversion. Invoked via subprocess for on-the-fly audio stream delivery. |
| [libresidfp / libsidplayfp / sidplayfp](https://github.com/libsidplayfp/sidplayfp) | GPL-2.0-or-later | Commodore 64 SID music emulation and playback (accurate reSIDfp engine). Built from upstream source by install.sh (libresidfp → libsidplayfp → sidplayfp CLI, since Homebrew's libsidplayfp ships only the lighter `sidlite` engine) with the library closure bundled locally via `@loader_path` so a `brew upgrade` can't orphan it. Invoked via subprocess to render SID files to audio. |
| [FluidSynth](https://github.com/FluidSynth/fluidsynth) | LGPL-2.1 | Software MIDI synthesizer. Invoked via subprocess to render MIDI files to audio using SoundFont banks. |
| [libopenmpt / openmpt123](https://lib.openmpt.org/libopenmpt/) | BSD-3-Clause | Tracker module file playback (MOD, XM, S3M, IT, etc.). Invoked via subprocess to render module files to audio. |
| [UADE / uade123](https://zakalwe.fi/uade/) | GPL-2.0 | Exotic Amiga music formats (AHX, TFMX, Future Composer, SidMon, and ~150 others) via emulated original Amiga player code. Invoked via subprocess. |
| [AdPlug / adplay](https://adplug.github.io/) | LGPL-2.1 | AdLib / OPL2 FM formats (IMF, ROL, CMF, D00, RAD, ...). Invoked via subprocess. |
| [Game_Music_Emu (libgme)](https://github.com/libgme/game-music-emu) | LGPL-2.1 | Console chiptunes (NSF, SPC, GBS, VGM, AY, KSS, ...). Bound via ctypes as a shared library. |
| [psgplay](https://github.com/frno7/psgplay) | GPL-2.0 | Atari ST SNDH playback (68000 + YM2149 + MFP + STE DMA emulation). Built from a pinned source revision by install.sh; invoked via subprocess. |
| [sc68](https://sc68.atari.org/) | GPL-2.0-or-later | Native Atari ST .sc68 disk playback. Invoked via subprocess. |
| [zxtune / zxtune123](https://github.com/vitamin-caig/zxtune) | GPL-3.0 | PSF console-music family (PSF/PSF2, USF, GSF, 2SF, NCSF, SSF, DSF) via its bundled reference cores (Highly Experimental, Highly Theoretical, lazyusf2, mGBA, vio2sf). Invoked via subprocess. |

## Vendored & Compiled-In Code

| Component | License | Usage |
|------|---------|-------|
| [HivelyTracker replayer](https://github.com/pete-gordon/hivelytracker) | BSD-3-Clause | Vendored under `soniqboom/native/hvl/`; compiled on first use into the `hvl2wav` helper for `.hvl` playback. |
| [ST-Sound (StSound) by Arnaud Carre](https://github.com/arnaud-carre/StSound) | BSD (per-file headers, (c) 1995-1999 Arnaud Carre) / MIT (repository license) | Vendored under `soniqboom/native/stsound/`; compiled on first use into the `ym2wav` helper for Atari ST `.ym` playback. |


## Data / Assets

| Asset | License | Usage |
|-------|---------|-------|
| [GeneralUser GS SoundFont](https://schristiancollins.com/generaluser.php) by S. Christian Collins | Free (attribution required) | Default SoundFont for MIDI synthesis via FluidSynth |
| [MuseScore_General SoundFont](https://musescore.org/en/handbook/3/soundfonts-and-sfx) | MIT | Alternative SoundFont for MIDI synthesis |
| [FluidR3_GM SoundFont](https://github.com/musescore/MuseScore/tree/master/share/sound) | MIT | Alternative General MIDI SoundFont for MIDI synthesis |


---

All external tools (FFmpeg, sidplayfp, FluidSynth, openmpt123) are invoked as
separate processes via subprocess. SoniqBoom does not link against or include
their source code. This subprocess relationship does not create a derivative
work under GPL/LGPL terms.

---

SoniqBoom itself is copyright (c) 2026 S.F. Cyris and is distributed under the
**GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)**. See
the project [LICENSE](LICENSE) file for the full text.

Source code: <https://github.com/SFCyris/SoniqBoom>

Commercial licensing for use cases that cannot comply with AGPL-3.0 (e.g.,
embedding SoniqBoom inside a closed-source product or running it as a
proprietary hosted service) is available on request &mdash; contact
<scyris@outlook.com>.
