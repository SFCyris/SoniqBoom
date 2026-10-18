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
| [sidplayfp / libsidplayfp / libresidfp](https://github.com/libsidplayfp/sidplayfp) | GPL-2.0-or-later | Commodore 64 SID playback (reSIDfp engine). `install.sh` on macOS and the Docker image build libresidfp 1.2.2, libsidplayfp 3.1.0 and the sidplayfp 3.1.0 CLI from upstream source; a native Linux install uses the distribution package. On macOS, when Homebrew's [libusb](https://libusb.info) (LGPL-2.1-or-later) is installed, that libsidplayfp build links it and `install.sh` copies it next to the binary. libsidplayfp includes the hashlib MD5 code (MIT, © 2025 Cra3z) and sidplayfp includes [ghc::filesystem](https://github.com/gulrak/filesystem) (MIT, © 2018 Steffen Schümann); in the Docker image these notices are under `/usr/local/share/doc/third-party/libsidplayfp/` and `…/sidplayfp/` with the GPL texts. |
| [FluidSynth](https://github.com/FluidSynth/fluidsynth) | LGPL-2.1-or-later | MIDI rendering with SoundFonts. Debian package in the Docker image; Homebrew or distribution package with `install.sh`. |
| [libopenmpt / openmpt123](https://lib.openmpt.org/libopenmpt/) | BSD-3-Clause | Tracker module playback (MOD, XM, S3M, IT, …) via `openmpt123`; libopenmpt is also loaded as a shared library (ctypes) for module lengths, the per-channel VU meters and the pattern view. The Docker image builds libopenmpt and openmpt123 0.8.9 from the upstream release; its licence texts (BSD-3-Clause; the `mpt` library it includes is BSL-1.0 or BSD-3-Clause, and its FFT code is adapted from Ryuhei Mori's [TinyFFT](https://github.com/ryuhei-mori/tinyfft), BSD-2-Clause) are under `/usr/local/share/doc/third-party/libopenmpt/`. Homebrew or distribution package with `install.sh`. |
| [UADE / uade123](https://gitlab.com/uade-music-player/uade) | GPL-2.0 (Amiga player files under `players/` carry their own licences; the `score` sound core is LGPL) | AHX and ~175 Amiga "exotica" formats via the original Amiga player code. Homebrew package on macOS; on Linux the distribution package where there is one, otherwise UADE 3.05 built from source, as in the Docker image. |
| [bencodetools](https://gitlab.com/heikkiorsila/bencodetools) and [libzakalwe](https://gitlab.com/hors/libzakalwe) | BSD-2-Clause | Libraries a source-built UADE uses (Linux source build and the Docker image). |
| [libao](https://xiph.org/ao/) | GPL-2.0-or-later | Audio output library a source-built UADE links (Linux source build and the Docker image, Debian `libao4`). |
| [AdPlay](https://github.com/adplug/adplay-unix) with the [AdPlug](https://github.com/adplug/adplug) library | AdPlay: GPL-2.0-or-later; AdPlug: LGPL-2.1-or-later | AdLib / OPL FM formats (IMF, ROL, CMF, D00, RAD, …), rendered with AdPlug's Nuked OPL3 emulator. Debian package in the Docker image; Homebrew or distribution package with `install.sh`. |
| [psgplay](https://github.com/frno7/psgplay) | GPL-2.0 (some files LGPL-2.1 / MIT; see its `licence/` folder) | Atari ST SNDH playback. Built by `install.sh` and the Docker build from a pinned source revision. |
| [sc68](https://sourceforge.net/projects/sc68/) | GPL-2.0-or-later (sc68 2.2.1) | Native Atari ST `.sc68` disks. Homebrew package on macOS; sc68 2.2.1 built from source on Linux (when the distribution has no package) and in the Docker image. |
| [zxtune / zxtune123](https://github.com/vitamin-caig/zxtune) | zxtune's own code: LGPL-3.0. The `zxtune123` binary also contains third-party code under GPL-3.0 (Highly Experimental), GPL-2.0 (DeSmuME in vio2sf, Mupen64Plus in lazyusf2), GPL-2.0-or-later (libsidplayfp), MPL-2.0 (mGBA), LGPL, BSD/MIT-style licences, the Boost Software License (Boost.Program_options, linked statically) and the unRAR licence, whose source may not be used to re-create the RAR compression algorithm. Each licence text is in the image under `/usr/local/share/doc/third-party/zxtune/`. | PSF console-music family (PSF/PSF2, USF, GSF, 2SF, NCSF, SSF, DSF), MED 2–4 modules, and VGM / VGZ files that use sound chips libgme does not play. Official prebuilt r5112 on Linux and in the Docker image; built from source on macOS against Homebrew's [Boost](https://www.boost.org) (BSL-1.0), whose libraries are copied next to the binary. |
| [lhasa](https://github.com/fragglet/lhasa) (`lha`) | ISC | Last resort for an LHA archive the in-process readers cannot read. Installed by `install.sh` where the package manager has it; optional, and not in the Docker image. |
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
| [libxmp XPK-SQSH depacker](https://github.com/libxmp/libxmp/blob/master/src/depackers/unsqsh.c) by Claudio Matsuoka (algorithm by Bert Jahn; checksum by Sipos Attila) | MIT | Ported to Python in `soniqboom/core/xpk.py` (licence notice in the module); unpacks XPK-SQSH packed Amiga modules. |


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
| [Demozoo](https://demozoo.org) database export | — | Composers' scene groups, release years, game soundtracks, productions and party competitions. Downloaded under gear → Metadata. |
| [TOSEC](https://www.tosecdev.org) and [Redump](http://redump.org) DAT game titles | — | Game titles per retro platform. Downloaded under gear → Metadata (Redump over plain HTTP). |
| SoundFonts (GeneralUser GS, FluidR3_GM, MuseScore_General, Timbres of Heaven, TimGM6mb, SC-55, Salamander Grand Piano) | Each SoundFont's own licence, shown next to it in the list | Optional SoundFonts for MIDI, downloaded from the list under gear → Renderers → Soundfonts (the server fetches them from dropbox.com, sourceforge.net, ftp.osuosl.org, archive.org and musical-artifacts.com, following their redirects to those sites' download servers, e.g. SourceForge mirrors). |

Online services contacted at runtime:

| Service | Usage |
|---------|-------|
| [MusicBrainz](https://musicbrainz.org), [Wikidata](https://www.wikidata.org) and [Wikipedia](https://en.wikipedia.org) | Artist biographies and portraits. |
| [MusicBrainz](https://musicbrainz.org) + [Cover Art Archive](https://coverartarchive.org), and [Discogs](https://www.discogs.com) when a Discogs token is configured (`discogs_token` in `SoniqBoom.conf`) | Cover art for the song playing on an internet-radio station (`SoniqBoom.conf` setting `radio_art_lookup`, on by default). |
| [LRCLib](https://lrclib.net) and [lyrics.ovh](https://lyrics.ovh) | Lyrics for tracks without embedded lyrics (MIDI and ordinary audio; other retro formats are not looked up). Both are timed at server start, and again on a lyrics request once the last timing is more than 30 minutes old; the faster one is asked first. |
| [Radio Browser](https://www.radio-browser.info/) | Worldwide internet-radio directory: the country list (fetched at server start when the cached copy is more than 7 days old), country station lists, station searches — including every search-box query of 2 or more characters — station lookups by ID when a directory station that is not in a cached country list is played, hovered or added to Favorites, and a click report when a directory station is played. |
| [Demozoo API](https://demozoo.org/api/v1/) | Composer and production details. |
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
  zxtune, libgme, sc68, UADE, bencodetools, libzakalwe, libresidfp, libsidplayfp,
  sidplayfp and libopenmpt) at the exact versions pinned
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
`/app/soniqboom/core/xpk.py`, and SoniqBoom's `LICENSE` and this file in
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
