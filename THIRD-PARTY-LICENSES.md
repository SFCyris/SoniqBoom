# Third-Party Software Licenses

SoniqBoom uses the following third-party software and data. We gratefully
acknowledge the work of these projects and their contributors.


## Python Dependencies

Installed from PyPI by `install.sh` and in the Docker image (`pyproject.toml`).
The packages these pull in themselves (e.g. uvloop, httptools, watchfiles, h11,
httpcore, certifi, idna, pydantic-core, click) come under their own licences.

| Package | License | Usage |
|---------|---------|-------|
| [FastAPI](https://github.com/tiangolo/fastapi) | MIT | Web framework powering the SoniqBoom REST API |
| [Starlette](https://github.com/encode/starlette) | BSD-3-Clause | ASGI toolkit FastAPI is built on (also used directly for responses and middleware) |
| [AnyIO](https://github.com/agronholm/anyio) | MIT | Async I/O helpers (internet-radio relay) |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI server used to run the FastAPI application |
| [websockets](https://github.com/python-websockets/websockets) | BSD-3-Clause | WebSocket support (live updates, multi-room) |
| [Mutagen](https://github.com/quodlibet/mutagen) | GPL-2.0-or-later | Audio metadata reading and tag writing |
| [lhafile](https://github.com/FrodeSolheim/python-lhafile) | BSD-3-Clause | Reading tracks inside Amiga LHA/LZH archives |
| [aiofiles](https://github.com/Tinche/aiofiles) | Apache-2.0 | Asynchronous file I/O |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT | Data validation for API models |
| [pydantic-settings](https://github.com/pydantic/pydantic-settings) | MIT | Configuration management |
| [python-multipart](https://github.com/Kludex/python-multipart) | Apache-2.0 | Multipart form parsing for uploads |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | HTTP client for metadata sources, downloads and internet radio |
| [orjson](https://github.com/ijl/orjson) | MPL-2.0 AND (Apache-2.0 OR MIT) | Fast JSON serialization for track lists and search results |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU (HPND) | Album art extraction and thumbnails |
| [mido](https://github.com/mido/mido) | MIT | MIDI file reading and metadata parsing |
| [smbprotocol](https://github.com/jborean93/smbprotocol) | MIT | SMB network shares without mounting |
| [cryptography](https://github.com/pyca/cryptography) | Apache-2.0 OR BSD-3-Clause | Encryption of stored share credentials and secrets |
| [defusedxml](https://github.com/tiran/defusedxml) | PSF-2.0 | Safe XML parsing (WebDAV shares) |
| [watchdog](https://github.com/gorakhargosh/watchdog) | Apache-2.0 | Watching library folders for changes |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT | Declared dependency |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | BSD-3-Clause | Reading settings from `~/.soniqboom.env` (through pydantic-settings) |

macOS only (menu-bar app, installed by `install.sh` on macOS):

| Package | License | Usage |
|---------|---------|-------|
| [rumps](https://github.com/jaredks/rumps) | BSD-3-Clause | macOS menu-bar icon |
| [PyObjC](https://github.com/ronaldoussoren/pyobjc) (Cocoa, WebKit) | MIT | macOS menu-bar window |

Optional, installed separately by the user to enable casting (Beta):

| Package | License | Usage |
|---------|---------|-------|
| [PyChromecast](https://github.com/home-assistant-libs/pychromecast) | MIT | Chromecast targets (`pip install pychromecast`) |
| [pyatv](https://github.com/postlund/pyatv) | MIT | AirPlay targets (`pip install pyatv`) |
| [async-upnp-client](https://github.com/StevenLooman/async_upnp_client) | Apache-2.0 | DLNA / UPnP renderer targets (`pip install async-upnp-client`) |


## External Tools (invoked via subprocess)

These players and tools run as separate processes. Where each comes from
(`install.sh` — from the system package manager or built from upstream source —
and/or the Docker image) is given per row. The Docker image's source-built
renderers are built from the upstream sources and versions named in the
[Dockerfile](Dockerfile); the rest of the image comes from Debian packages.

| Tool | License | Usage |
|------|---------|-------|
| [FFmpeg](https://ffmpeg.org/) | LGPL-2.1-or-later / GPL (depending on build configuration) | Transcoding, DSD and modern-codec playback. The Docker image uses Debian's `ffmpeg` package; `install.sh` also downloads a static GPL build (macOS: [Martin Riedl's builds](https://ffmpeg.martin-riedl.de/); Linux: [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)) into the data directory, which `soniqboom fetch-ffmpeg` refreshes. |
| [sidplayfp / libsidplayfp / libresidfp](https://github.com/libsidplayfp/sidplayfp) | GPL-2.0-or-later | Commodore 64 SID playback (reSIDfp engine). `install.sh` and the Docker image build libresidfp 1.2.2, libsidplayfp 3.1.0 and the sidplayfp 3.1.0 CLI from upstream source (on Linux, unless a sidplayfp with libsidplayfp 3.1 or newer is already installed; the distribution package is used when that build fails). On macOS, when Homebrew's [libusb](https://libusb.info) (LGPL-2.1-or-later) is installed, that libsidplayfp build links it and `install.sh` copies it next to the binary. libsidplayfp includes the hashlib MD5 code (MIT, © 2025 Cra3z) and sidplayfp includes [ghc::filesystem](https://github.com/gulrak/filesystem) (MIT, © 2018 Steffen Schümann); in the Docker image these notices are under `/usr/local/share/doc/third-party/libsidplayfp/` and `…/sidplayfp/` with the GPL texts. |
| [FluidSynth](https://github.com/FluidSynth/fluidsynth) | LGPL-2.1-or-later | MIDI rendering with SoundFonts. Debian package in the Docker image; Homebrew or distribution package with `install.sh`. |
| [libopenmpt / openmpt123](https://lib.openmpt.org/libopenmpt/) | BSD-3-Clause | Tracker module playback (MOD, XM, S3M, IT, …) via `openmpt123`; libopenmpt is also loaded as a shared library (ctypes) for module lengths, the per-channel VU meters and the pattern view. The Docker image builds libopenmpt and openmpt123 0.8.9 from the upstream release; its licence texts (BSD-3-Clause; the `mpt` library it includes is BSL-1.0 or BSD-3-Clause, and its FFT code is adapted from Ryuhei Mori's [TinyFFT](https://github.com/ryuhei-mori/tinyfft), BSD-2-Clause) are under `/usr/local/share/doc/third-party/libopenmpt/`. Homebrew or distribution package with `install.sh`. |
| [UADE / uade123](https://gitlab.com/uade-music-player/uade) | GPL-2.0 (Amiga player files under `players/` carry their own licences; the `score` sound core is LGPL) | AHX and ~175 Amiga "exotica" formats via the original Amiga player code. Homebrew package on macOS; on Linux the distribution package where there is one, otherwise UADE 3.05 built from source, as in the Docker image. |
| [bencodetools](https://gitlab.com/heikkiorsila/bencodetools) and [libzakalwe](https://gitlab.com/hors/libzakalwe) | BSD-2-Clause | Libraries a source-built UADE uses (Linux source build and the Docker image). |
| [libao](https://xiph.org/ao/) | GPL-2.0-or-later | Audio output library a source-built UADE links (Linux source build and the Docker image, Debian `libao4`). |
| [AdPlay](https://github.com/adplug/adplay-unix) with the [AdPlug](https://github.com/adplug/adplug) library | AdPlay: GPL-2.0-or-later; AdPlug: LGPL-2.1-or-later | AdLib / OPL FM formats (IMF, ROL, CMF, D00, RAD, …), rendered with AdPlug's Nuked OPL3 emulator. Debian package in the Docker image; Homebrew or distribution package with `install.sh`. |
| [psgplay](https://github.com/frno7/psgplay) | GPL-2.0 (some files LGPL-2.1 / MIT; see its `licence/` folder) | Atari ST SNDH playback. Built by `install.sh` and the Docker build from a pinned source revision. |
| [libkss](https://github.com/digital-sound-antiques/libkss) (in `kssrender`) | ISC (libkss); MIT (its emu2149, emu2212, emu2413, emu8950 and emu76489 sound-chip cores); public domain (its kmz80 Z80 core). Of its kss-drivers only MGSDRV is included (below). Each licence text is in the image under `/usr/local/share/doc/third-party/libkss/`. | MSX and Sega Master System KSS playback through `kssrender` (`soniqboom/native/kssrender`, AGPL-3.0-or-later). Built by `install.sh` and the Docker build from a pinned libkss revision. |
| [MGSDRV](https://www.gigamix.jp/mgsdrv/) (in `kssrender`) | MGSDRV (C) Ain./Gigamix. Free software; its embedding licence allows building the driver, unmodified, into other software, paid or free, with that credit, and counts libkss's driver header used unmodified as unmodified. The notice is in the image under `/usr/local/share/doc/third-party/libkss/MGSDRV-NOTICE.txt`. | MSX MGSDRV songs (`.mgs`) through `kssrender`. Built in from libkss's `kss-drivers` header `mgsdrv.h` at a pinned revision, checked by its SHA-256. |
| [Highly Theoretical](https://github.com/kode54/Highly_Theoretical) and [psflib](https://github.com/kode54/psflib) (in `dsfrender`) | Highly Theoretical: GPL-3.0 (only its Dreamcast sound core — AICA and ARM7 — is built; its 68000 cores — Musashi and Starscream (under their own licences) and C68K (GPL-2.0-or-later) — are neither built nor included); psflib: MIT (© Christopher Snowhill). Each licence text is in the image under `/usr/local/share/doc/third-party/dsfrender/`. | Sega Dreamcast / Naomi DSF playback (`.dsf`, `.minidsf` with `.dsflib`) through `dsfrender` (`soniqboom/native/dsfrender`, AGPL-3.0-or-later). Built by `install.sh` and the Docker build from pinned revisions of both. |
| [MilkyPlay](https://github.com/milkytracker/MilkyTracker/tree/master/src/milkyplay) (in `milkyrender`) | BSD-3-Clause (MilkyPlay, the playback library of MilkyTracker, whose other parts are GPL and are not included). Its licence text is in the image under `/usr/local/share/doc/third-party/milkyplay/`; `soniqboom/native/milkyrender/README.md` lists every MilkyPlay file the build compiles. | Early Digital Sound Interface Kit modules (DSIK v1, `.dsm` files starting with `DSM\x10`) through `milkyrender` (`soniqboom/native/milkyrender`, AGPL-3.0-or-later). Built by `install.sh` and the Docker build from a pinned MilkyTracker revision. |
| [musicline_playback](https://github.com/spotUP/DEViLBOX/tree/57b3e19d2f312c70501f9675436062b947f94acb/third-party/musicline_playback-main) (in `mlrender`) | The port carries no licence statement of its own. The third-party notices (`THIRD_PARTY_NOTICES.md`) of [DEViLBOX](https://github.com/spotUP/DEViLBOX), which publishes it, give its licence as GPL-2.0-or-later by inheritance from UADE, which it embeds; DEViLBOX's own licence is GPL-3.0. Only the port's C++ replayer files are built — not the uade copy the port embeds (`musicline/uade`), nor any other part of DEViLBOX. The port follows the 68000 Musicline Editor 1.16 replayer (`Mline116.asm`, which DEViLBOX does not publish); uade publishes an earlier source of that replayer (`mlineplayer102.asm`, © John Carehag, in `amigasrc/players/other/musicline_editor`) under the LGPL. DEViLBOX's licence text and notices are in the image under `/usr/local/share/doc/third-party/musicline_playback/`. | Musicline Editor tunes that uade renders only as silence, through `mlrender` (`soniqboom/native/mlrender`, AGPL-3.0-or-later). Built by `install.sh` and the Docker build from the port's replayer sources at a pinned DEViLBOX revision, with its tick buffers enlarged and SoniqBoom's bounds checks applied (`musicline_playback.patch`, `ml_bounds.h` in `soniqboom/native/mlrender`; `build.sh`). |
| [portable_mdx](https://github.com/yosshin4004/portable_mdx) (in `mdxrender`) | Mixed, as its readme ("ライセンス") states: the files Yosshin wrote — `include/mxdrv_context.h`, `include/x68sound_context.h`, `include/mdx_util.h`, `src/mdx_util` and the examples — are Apache-2.0. `include/mxdrv.h` and `src/mxdrv/*` derive from MXDRV.X 2.06+17 (milk., K.MAEKAWA, Missy.M, Yatsube), converted to C by GORRY as MXDRVg; Yosshin's changes follow their licence, of which there is no written statement (the readme calls it freeware by X68000 convention). `include/x68sound.h` and `src/x68sound/*` derive from X68Sound.dll (© m_puusan), whose terms allow modifying it, publishing the modified code, building it into other software and distributing that software; Yosshin's changes follow them. The readme and the Apache-2.0 text are in the image under `/usr/local/share/doc/third-party/portable_mdx/`. | Sharp X68000 MDX songs, with the PDX sample file they name, through `mdxrender` (`soniqboom/native/mdxrender`, AGPL-3.0-or-later). Built by `install.sh` and the Docker build from a pinned portable_mdx revision, unmodified. |
| [GoatTracker 2](https://sourceforge.net/projects/goattracker2/) (`gt2reloc`) | GPL-2.0-or-later (GoatTracker, © Lasse Öörni and its authors; the reSID / reSIDfp it includes are GPL too). Its licence text and author notes are in the image under `/usr/local/share/doc/third-party/goattracker/`. | GoatTracker songs (`.sng`): `gt2reloc` converts them to a C64 SID tune that sidplayfp plays. Built by `install.sh` and the Docker build from the pinned GoatTracker 2.77 release archive, unmodified; linked with `soniqboom/native/goattracker/sdlstub.c` (GPL-2.0-or-later) in place of the SDL library. |
| [SunVox Library](https://warmplace.ru/soft/sunvox/sunvox_lib.php) (in `sunvoxrender`) | MIT (its source, © Alexander Zolotov, WarmPlace.ru; the SunDog engine too). Its use terms ask that this line be included in the documentation of software using it: "Powered by SunVox (modular synth & tracker) Copyright (c) 2008 - 2026, Alexander Zolotov <nightradio@gmail.com>, WarmPlace.ru", with the licence texts of the libraries it includes — libFLAC's and Tremor's (BSD-3-Clause, © Josh Coalson, Xiph.Org Foundation). Every text is in the image under `/usr/local/share/doc/third-party/sunvox/`. | SunVox projects (`.sunvox`) through `sunvoxrender` (`soniqboom/native/sunvoxrender`, AGPL-3.0-or-later). Built by `install.sh` and the Docker build from the source in the pinned SunVox Library 2.1.4d release archive, unmodified. |
| [Furnace](https://github.com/tildearrow/furnace) (`furnace-headless`) | GPL-2.0-or-later (© tildearrow and contributors); its FamiTracker importer is partly derived from FamiTracker (GPL-2.0-or-later); its emulation cores and libraries carry their own licences in their source files. Its licence text is in the image under `/usr/local/share/doc/third-party/furnace/`. | FamiTracker modules (`.ftm`) and DefleMask modules (`.dmf`) through Furnace's headless player. Built by `install.sh` and the Docker build from the pinned v0.6.8.3 commit, unmodified, without its GUI. |
| [libxmp](https://github.com/libxmp/libxmp) (in `xmprender`) | MIT (© Claudio Matsuoka, Hipolito Carraro Jr and contributors; its ProWizard loaders also © Sylvain "Asle" Chipaux, MIT); a few of its depackers and its Vorbis decoder are in the public domain, its bunzip2 depacker is 0BSD, its bundled miniz is under the Unlicense, and its fnmatch fallback (built only where the C library has none) is BSD-3-Clause, as their source files state. Its licence texts are in the image under `/usr/local/share/doc/third-party/libxmp/`. | The modules only libxmp plays here (Archimedes Tracker, Coconizer, FlexTrax, Funktracker, Liquid Tracker, Megatracker, and the Titanics Player, STIM, The Dark Demon and Eureka packers), and the ProTracker modules `soniqboom/core/prowizard.py` converts packed modules to, through `xmprender` (`soniqboom/native/xmprender`, AGPL-3.0-or-later). Built by `install.sh` and the Docker build from the pinned libxmp 4.7.4 release archive, unmodified, as a static library. |
| [sc68](https://sourceforge.net/projects/sc68/) | GPL-2.0-or-later (sc68 2.2.1) | Native Atari ST `.sc68` disks. Homebrew package on macOS; sc68 2.2.1 built from source on Linux (when the distribution has no package) and in the Docker image. |
| [zxtune / zxtune123](https://github.com/vitamin-caig/zxtune) | zxtune's own code: LGPL-3.0. The `zxtune123` binary also contains third-party code under GPL-3.0 (Highly Experimental), GPL-2.0 (DeSmuME in vio2sf, Mupen64Plus in lazyusf2), GPL-2.0-or-later (libsidplayfp), MPL-2.0 (mGBA), LGPL, BSD/MIT-style licences, the Boost Software License (Boost.Program_options, linked statically) and the unRAR licence, whose source may not be used to re-create the RAR compression algorithm. Each licence text is in the image under `/usr/local/share/doc/third-party/zxtune/`. | PSF console-music family (PSF/PSF2, USF, GSF, 2SF, NCSF, SSF, DSF), S98 and Farbrausch V2 files, MED 2–4 modules, and VGM / VGZ files that use sound chips libgme does not play. Official prebuilt r5112 on Linux and in the Docker image; built from source on macOS against Homebrew's [Boost](https://www.boost.org) (BSL-1.0), whose libraries are copied next to the binary. |
| [lhasa](https://github.com/fragglet/lhasa) (`lha`) | ISC | Last resort for an LHA archive the in-process readers cannot read. Installed by `install.sh` where the package manager has it; Debian package in the Docker image. |
| [tini](https://github.com/krallin/tini) | MIT | Init process (PID 1) of the Docker image. |
| [Python](https://www.python.org) (CPython) | PSF-2.0 | Runtime of the Docker image (`python:3.12-slim-trixie`). |
| [metaflac](https://xiph.org/flac/) (FLAC tools) | GPL | Used when installed on the host (not installed by SoniqBoom): adds a seek table to FLAC files cached from network shares. |


## Shared Libraries (loaded via ctypes)

| Library | License | Usage |
|---------|---------|-------|
| [Game_Music_Emu (libgme)](https://github.com/libgme/game-music-emu) | LGPL-2.1-or-later (GPL-2.0-or-later when built with its MAME YM2612 emulator) | Console chiptunes (NSF, SPC, GBS, VGM, AY, KSS, SAP, HES, GYM, …). The Docker image builds libgme 0.6.5 from its upstream release with its default Nuked OPN2 YM2612 emulator (LGPL-2.1-or-later); native installs use the system or Homebrew package. |
| [libopenmpt](https://lib.openmpt.org/libopenmpt/) | BSD-3-Clause | Module lengths, per-channel VU meters and pattern view for tracker modules (see above). |


## Vendored & Compiled-In Code

| Component | License | Usage |
|------|---------|-------|
| [HivelyTracker replayer](https://github.com/pete-gordon/hivelytracker), (c) 2006-2018 Pete Gordon | BSD-3-Clause | Vendored under `soniqboom/native/hvl/` with its `LICENSE` (`hvl2wav.c` modified for SoniqBoom; see `NOTICE.md` there); built into the `hvl2wav` helper for `.hvl` playback and for finding where AHX tunes end (pre-built in the Docker image, compiled on first use otherwise). |
| [ST-Sound (StSound) by Arnaud Carré](https://github.com/arnaud-carre/StSound) | MIT (repository licence) / BSD-2-Clause (per-file headers, (c) 1995-1999 Arnaud Carré) | Vendored under `soniqboom/native/stsound/` with its `LICENSE`; includes the LZH depacking code by Haruhiko Okumura and Kerwin F. Medina. Built into the `ym2wav` helper for Atari ST `.ym` playback (pre-built in the Docker image, compiled on first use otherwise). |
| [libsidplayfp](https://github.com/libsidplayfp/libsidplayfp) 2.16.1 with the reSIDfp engine, compiled to WebAssembly | GPL-2.0-or-later | `soniqboom/frontend/js/vendor/sidwasm.js` + `sidwasm.wasm`; renders C64 SID tunes in the browser for the per-voice VU meter and for the optional *Play SID in browser* setting. Built from the source, patches and build script in `soniqboom/native/sidwasm/`. libsidplayfp includes code derived from [VICE](https://vice-emu.sourceforge.io/) (GPL-2.0-or-later) and an MD5 implementation by Michael Schwendt; see `AUTHORS` in its source. |
| [Emscripten](https://emscripten.org) runtime, with [musl](https://musl.libc.org) libc and LLVM libc++ / libc++abi / compiler-rt | Emscripten: MIT / University of Illinois-NCSA; musl: MIT; LLVM runtimes: Apache-2.0 WITH LLVM-exception | Compiled into `sidwasm.js` + `sidwasm.wasm`. |
| [libxmp XPK-SQSH depacker](https://github.com/libxmp/libxmp/blob/master/src/depackers/unsqsh.c) by Claudio Matsuoka (algorithm by Bert Jahn; checksum by Sipos Attila) | MIT | Ported to Python in `soniqboom/core/xpk.py` (licence notice in the module); unpacks XPK-SQSH packed Amiga modules. The same module's PowerPacker decoder follows the PowerPacker 2.0 data format as Stuart Caie's public-domain decruncher reads it. |
| [NostalgicPlayer DeltaMusic20 player](https://github.com/neumatho/NostalgicPlayer) by Thomas Neumann | MIT | Its sequencer and song-end logic ported to Python in `soniqboom/core/dm2_length.py` (licence notice in the module); works out how long a Delta Music 2 song plays. |
| [NostalgicPlayer ProWizard converters](https://github.com/neumatho/NostalgicPlayer/tree/main/Source/Agents/ModuleConverters/ProWizardConverter) by Thomas Neumann | MIT | Fifteen of its packed-module converters (Ben Replay, Binary Packer, Devils Replay, GnoiPacker, Gnu Protected Module, GnuPlayer, HCD Protector, Newtron Packer 1.0 / 2.0, Perfect Song 1 / 2, Module-Patterncompressor, NoiseTracker Compressed, The Player 2.2A / 3.0A) ported to Python in `soniqboom/core/prowizard.py` (licence notice in the module); they turn those modules into ProTracker modules, which `xmprender` plays. |
| [NostalgicPlayer Sawteeth player](https://github.com/neumatho/NostalgicPlayer/tree/main/Source/Agents/Players/Sawteeth) by Thomas Neumann | MIT | Ported to C as `sawteethrender` (`soniqboom/native/sawteethrender/`, licence text in `NostalgicPlayer-LICENSE` beside it, and in the image under `/usr/local/share/doc/third-party/sawteethrender/`); plays Sawteeth songs. |


## Data / Assets

Included with SoniqBoom:

| Asset | License | Usage |
|-------|---------|-------|
| [Wikidata](https://www.wikidata.org) video-game titles (`soniqboom/data/game_titles/wikidata.tsv.gz`) | [CC0 1.0](https://www.wikidata.org/wiki/Wikidata:Licensing) | Game titles per retro platform, matched against archive names. Data from Wikidata. |
| [No-Intro](https://no-intro.org) DAT game titles (`soniqboom/data/game_titles/nointro.tsv.gz`) | [DAT-o-MATIC Data Usage License](https://datomatic.no-intro.org/terms.html) | Game titles per retro platform, matched against archive names. |
| [MAME software lists](https://github.com/mamedev/mame/tree/master/hash) titles (`soniqboom/data/game_titles/mame.tsv.gz`) | CC0 1.0 | Game titles per retro platform, matched against archive names. |
| [ZXDB](https://github.com/zxdb/ZXDB) ZX Spectrum game titles (`soniqboom/data/game_titles/zxdb.tsv.gz`) | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/) | ZX Spectrum game titles, matched against archive names. Contains information from ZXDB, made available under the ODbL; the derived title file is available under the ODbL. |
| [FluidR3_GM SoundFont](https://packages.debian.org/trixie/fluid-soundfont-gm) (Debian `fluid-soundfont-gm`) | MIT | General-MIDI SoundFont in the Docker image. |

Downloaded by the user (not included with SoniqBoom):

| Source | License | Usage |
|--------|---------|-------|
| [HVSC (High Voltage SID Collection)](https://www.hvsc.c64.org/) | Free archive of SID music | `Songlengths.md5` (`Songlengths.txt` in older releases) and `STIL.txt` from the user's own HVSC `DOCUMENTS` folder: per-tune SID lengths and credits. |
| [audacious-uade-tools](https://github.com/mvtiaine/audacious-uade-tools) song database by Matti Tiainen | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) | Module artists, publishers, albums, years and song lengths, matched by checksum. Downloaded under gear → Metadata from a pinned commit of the repository (raw.githubusercontent.com), or, when that commit is no longer available, from its `master` branch, shown as "latest files from GitHub (not validated)"; the download also asks api.github.com whether newer data exists. |
| [Modland](https://ftp.modland.com/) | — | Checksum list (`allmods_md5.zip`) for Scene origin; the index (`allmods.zip`), daily changelogs and tune files for the Modland catalogue. Downloaded under gear → Metadata → Scene origin (Modland) and gear → Library → External sources. |
| [Demozoo](https://demozoo.org) database export | — | Composers' scene groups, release years, game soundtracks, productions and party competitions; the composer profiles (notes, location, links), productions and releases of Track Info's Scene tab. Downloaded under gear → Metadata. |
| [TOSEC](https://www.tosecdev.org) and [Redump](http://redump.org) DAT game titles | — | Game titles per retro platform. Downloaded under gear → Metadata (Redump over plain HTTP). |
| SoundFonts (GeneralUser GS, FluidR3_GM, MuseScore_General, Timbres of Heaven, TimGM6mb, SC-55, Salamander Grand Piano) | Each SoundFont's own licence, shown next to it in the list | Optional SoundFonts for MIDI, downloaded from the list under gear → Renderers → Soundfonts (the server fetches them from dropbox.com, sourceforge.net, ftp.osuosl.org, archive.org and musical-artifacts.com, following their redirects to those sites' download servers, e.g. SourceForge mirrors). |

Online services contacted at runtime:

| Service | Usage |
|---------|-------|
| [MusicBrainz](https://musicbrainz.org), [Wikidata](https://www.wikidata.org) and [Wikipedia](https://en.wikipedia.org) | Artist biographies and portraits. |
| [MusicBrainz](https://musicbrainz.org) + [Cover Art Archive](https://coverartarchive.org), and [Discogs](https://www.discogs.com) when a Discogs token is configured (`discogs_token` in `SoniqBoom.conf`) | Cover art for the song playing on an internet-radio station (`SoniqBoom.conf` setting `radio_art_lookup`, on by default). |
| [LRCLib](https://lrclib.net) and [lyrics.ovh](https://lyrics.ovh) | Lyrics for tracks without embedded lyrics (MIDI and ordinary audio; other retro formats are not looked up). Both are timed at server start, and again on a lyrics request once the last timing is more than 30 minutes old; the faster one is asked first. |
| [Radio Browser](https://www.radio-browser.info/) | Worldwide internet-radio directory: the country list (fetched at server start when the cached copy is more than 7 days old), country station lists, station searches — including every search-box query of 2 or more characters — station lookups by ID when a directory station that is not in a cached country list is played, hovered or added to Favorites, and a click report when a directory station is played. |
| [last.fm](https://www.last.fm/api) and [ListenBrainz](https://listenbrainz.org) | Scrobbling, when a user saves a ListenBrainz token or a last.fm session key. last.fm also needs the server environment variables `SONIQBOOM_LASTFM_API_KEY` and `SONIQBOOM_LASTFM_API_SECRET`. |
| Scene radio stations (SceneSat Radio, Nectarine Demoscene Radio, SLAY Radio, Kohina, Radio PARALAX, CVGM.net, Rainwave Chiptunes, Rainwave Game Music, OverClocked ReMix Radio, Keygen-FM, CGM UK DemoScene) | Station streams in the built-in scene station pack. |
| Other internet-radio stations (Radio Browser directory stations, Favorites, and stations added from a Subsonic app) | Station streams from each station's own server, fetched through the server's relay; the mobile page plays a station straight from its server first and uses the relay when the browser can't. Station logos come from each station's own logo address: the browser loads them directly, and the server fetches a Favorite's logo once to show it in Subsonic apps. |


---

The tools in *External Tools* run as separate processes (bencodetools,
libzakalwe and libao are libraries linked into uade; libresidfp and
libsidplayfp into sidplayfp; on macOS, Boost into zxtune123 and, when
installed, libusb into libsidplayfp). libgme and libopenmpt are loaded at
runtime as shared libraries. The vendored components listed above are
included under their own licences.


## Source Code for Distributed Binaries

**Docker image.** Every [GitHub release](https://github.com/SFCyris/SoniqBoom/releases)
carries, next to the SoniqBoom source zip:

- `soniqboom-<version>-third-party-sources.tar` — the upstream source code of
  the programs the image builds or downloads (psgplay with its submodules,
  libkss with the submodules it is built with, the MGSDRV driver header,
  Highly Theoretical, psflib, MilkyTracker, musicline_playback (from
  DEViLBOX), portable_mdx, GoatTracker, the SunVox Library, libxmp, Furnace (with the
  submodules it is built with, without its demo songs), zxtune, libgme, sc68, UADE,
  bencodetools, libzakalwe, libresidfp, libsidplayfp, sidplayfp and libopenmpt)
  at the exact versions pinned
  in the [Dockerfile](Dockerfile), and of the libsidplayfp the browser uses at
  the version pinned in `soniqboom/native/sidwasm/build.sh`, collected by
  [`scripts/collect_third_party_sources.sh`](scripts/collect_third_party_sources.sh).
  The build steps, including the changes applied to sc68's build files, are in
  the Dockerfile.
- `soniqboom-<version>-third-party-sources.txt` — origin and SHA-256 of each
  archive.
- `soniqboom-<version>-debian-packages-linux-amd64.txt` and `…-linux-arm64.txt`
  — every Debian package in the image with its version and source package.
- `soniqboom-<version>-debian-sources.tar` — the Debian source packages of
  those packages at those versions, collected by
  [`scripts/collect_debian_sources.sh`](scripts/collect_debian_sources.sh), with
  the list in `soniqboom-<version>-debian-sources.txt`.

Inside the image, the licence and copyright files of the programs built or
downloaded by the Dockerfile are in `/usr/local/share/doc/third-party/`, the
Debian packages' in `/usr/share/doc/`, the Python packages' in their
`*.dist-info` folders under `/usr/local/lib/python3.12/site-packages/`, the
licences of the HivelyTracker replayer, ST-Sound and the browser SID engine
next to their sources under `/app/soniqboom/native/` (`hvl/LICENSE`,
`stsound/LICENSE`, `sidwasm/COPYING`), the XPK-SQSH depacker's notice in
`/app/soniqboom/core/xpk.py`, NostalgicPlayer's notice in
`/app/soniqboom/core/dm2_length.py` and `/app/soniqboom/core/prowizard.py`, and SoniqBoom's `LICENSE` and this file in
`/app`. UADE's Amiga player files and sc68's replay routines ship as binaries
for the emulated Amiga and Atari ST, as their upstream releases distribute
them; the licences that apply to them are in those upstream `COPYING` files.

**Browser SID engine.** `soniqboom/native/sidwasm/` holds the wrapper source,
the patches to libsidplayfp, the pinned upstream tarball (URL and SHA-256) and
the build script that produces `sidwasm.js` and `sidwasm.wasm`.

**`install.sh`.** Programs installed by `install.sh` are downloaded from their
upstream projects or package managers, or built from upstream source, on the
machine they run on.

---

SoniqBoom itself is copyright (c) 2026 S.F. Cyris and is distributed under the
**GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)**. See
the project [LICENSE](LICENSE) file for the full text.

Source code: <https://github.com/SFCyris/SoniqBoom>

A running server links the source code of its own version as *Source code of
this version*, under gear → System → About (admin accounts) and in the About
section of Settings on the mobile page.

Commercial licensing for use cases that cannot comply with AGPL-3.0 (e.g.,
embedding SoniqBoom inside a closed-source product or running it as a
proprietary hosted service) is available on request &mdash; contact
<scyris@outlook.com>.
