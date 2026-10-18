<p align="center">
  <img src="images/logo-banner.png" alt="SoniqBoom" width="600">
</p>

<p align="center"><strong>The self-hosted music server that actually plays the formats every other server forgot.</strong></p>

<p align="center">
  SID &bull; MOD &bull; XM &bull; IT &bull; AHX &bull; HivelyTracker &bull; NSF &bull; SPC &bull; VGM &bull; MIDI<br>
  &mdash; plus FLAC, DSD, and every modern codec, rendered with the same obsessive care.
</p>

<p align="center">
  Self-hosted &bull; Browser-based &bull; Zero cloud &bull; Zero telemetry &bull; AGPL-3.0
</p>

<p align="center">
  <a href="https://github.com/SFCyris/SoniqBoom/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg" alt="License: AGPL-3.0-or-later"></a>
  <a href="https://github.com/SFCyris/SoniqBoom/releases/latest"><img src="https://img.shields.io/github/v/release/SFCyris/SoniqBoom?include_prereleases&sort=semver" alt="Latest release"></a>
  <a href="https://github.com/SFCyris/SoniqBoom/pkgs/container/soniqboom"><img src="https://img.shields.io/badge/ghcr.io-soniqboom-2496ED?logo=docker&logoColor=white" alt="Docker image on GHCR"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
</p>

<p align="center">
  <strong><a href="https://github.com/SFCyris/SoniqBoom/releases/latest/download/soniqboom-latest.zip">⬇&nbsp; Download the latest release</a></strong>
  &nbsp;·&nbsp; <a href="https://github.com/SFCyris/SoniqBoom/releases/latest">release notes</a>
</p>

---

## What is SoniqBoom?

**SoniqBoom is a free music server you run on your own computer.**

Install it on a **Mac or Linux** machine, point it at your music folders, and listen from any web browser on your network — phone, laptop, tablet, anything.

It plays your everyday files (FLAC, MP3, ALAC, and the rest) **and** the retro formats most servers can't: SID, MOD, chiptunes, AdLib, dozens more. No cloud, no account, no tracking — it all stays on your machine.

<p align="center">
  <img src="images/demo-player.gif" alt="SoniqBoom in action — browsing and playing a music library in the browser" width="820">
</p>
<p align="center"><sub>Your whole library, in the browser — browse, search, queue, and play, with a live now-playing bar and per-track queue.</sub></p>

---

## Install &amp; run it

The steps are **identical on macOS and Linux**. Three commands: download, install, run.

> **On Windows, or want to run it in a container** (Raspberry Pi, a home server)? This native installer is macOS/Linux only — use the Docker guide instead → **[DEPLOY.md](DEPLOY.md)**.

> **Before you start:** open the **Terminal** app. Copy the block below, paste it in, and press Enter. That's the whole install.

```bash
# 1 — Download and unpack
curl -LO https://github.com/SFCyris/SoniqBoom/releases/latest/download/soniqboom-latest.zip
unzip soniqboom-latest.zip
cd soniqboom-*/        # opens the folder you just unzipped

# 2 — Install everything (same command on macOS and Linux)
bash install.sh

# 3 — Start it
bash run.sh
```

Then open **<http://localhost:8080>** in any browser. That's it. 🎉

> Macs and most Linux desktops already include `curl` and `unzip`; on Linux the installer also needs `git`. On a bare-bones Linux box that's missing them, install them once first — e.g. `sudo apt install -y curl unzip git` (or `dnf` / `pacman` / `zypper`).

**What each step does:**

1. **Download** — grabs the latest release and unzips it.
2. **`bash install.sh`** — installs Python, ffmpeg, and the retro-format players for you (Homebrew on macOS; apt / dnf / pacman / zypper on Linux). On a fresh machine it may ask for your **computer password** so it can install those. On an interactive terminal it then **asks you to create your admin login** — pick a username and a password; this is the account you'll sign in with. (If it doesn't prompt — for example you pasted all three commands at once — just create the login afterward with `bash setup-admin.sh`. Until the first account exists, the web page only shows how to create it, and the server doesn't check sign-ins, so create it right away.)
3. **`bash run.sh`** — starts the server in the background and prints the address to open. (If port 8080 is already in use, run `bash run.sh --port 9090` to pick another.) On a Mac it also puts a 🔊 icon in the menu bar to open, start, restart or stop SoniqBoom and show its settings and status; quitting the icon leaves the server running.

> *Prefer git?* `git clone https://github.com/SFCyris/SoniqBoom.git && cd SoniqBoom`, then run steps 2 and 3.

If anything looks missing afterwards, just **re-run `bash install.sh`** — it's safe to run as many times as you like and only fills in what's absent.

---

## Run it with Docker

Prefer a container — a Raspberry Pi, a home server, or Windows via Docker Desktop? A prebuilt multi-arch image (Intel/AMD **and** ARM) is published on GHCR, so there's nothing to build.

```bash
# 1 — Start it.  The named volume keeps your library index, user accounts,
#     settings, SoundFonts, saved share passwords and last.fm / ListenBrainz
#     logins, and logs when the container is re-created; swap
#     /path/to/your/music for your library (or drop that -v line to start
#     empty and add folders later from the UI).  Drop the :ro if you want
#     to edit tags or save fetched lyrics into your files from the browser.
docker run -d --name soniqboom \
  -p 8080:8080 \
  -v /path/to/your/music:/music:ro \
  -v soniqboom-data:/data \
  --restart unless-stopped \
  ghcr.io/sfcyris/soniqboom:latest

# 2 — Create your admin login (any username + a password) right away: until
#     the first account exists, the web page only shows how to create it,
#     and the server doesn't check sign-ins.
#     New users are admins by default; the change goes live with no restart.
docker exec soniqboom soniqboom-setadm -user me -passwd 'change-this-password'

# 3 — Log in.
#     Open  http://localhost:8080  and sign in with:  me  /  change-this-password
```

Once you're in, click the **gear icon** (top-right) and add a library folder — `/music` if you mounted it above, or any local / FTP / SMB / WebDAV path. Internet **Radio** plays right away with no library at all.

**Updating:**

```bash
docker pull ghcr.io/sfcyris/soniqboom:latest
docker stop soniqboom
docker rm soniqboom
```

then the same `docker run` command as before. Everything on `/data` is kept — the library index (so there is no full rescan), user accounts, settings and SoundFonts. A container from an image before 1.14.0 kept its settings, and the SoundFonts added in it, inside the container, so after the first update to 1.14.0 or later, settings (shares, services, sign-up and password rules, renderer settings, the *Re-apply automatically* switches) need to be set once more; from then on they're kept. SoundFonts added in such a container go with it: to keep them, copy them out before `docker rm soniqboom` (`docker cp soniqboom:/app/soundfonts ./old-soundfonts`), then add them again under gear → Renderers → Soundfonts (**↑ Upload .sf2/.sf3**). The last.fm / ListenBrainz logins saved in such a container can't be read by the new one either: enter them again under gear → System → My Account (admin accounts only — other accounts can't set them in the web app). Each release is also published under its version number (e.g. `:1.14.0`), and `:1.14` follows the newest 1.14.x release.

**Restoring a backup** made with **Export .sbz** (from the folder holding the `.sbz` file — **Export .sbz** names it `soniqboom-YYYY-MM-DD.sbz`):

```bash
docker stop soniqboom
docker run --rm -v soniqboom-data:/data -v "$PWD":/backup:ro ghcr.io/sfcyris/soniqboom:latest soniqboom import /backup/soniqboom-2026-10-01.sbz
docker start soniqboom
```

Use your file's name in the second command. The import replaces the library index; changes made since the export are lost. `soniqboom import` refuses to run while the server is using `/data` — stop it first. You can also restore from the browser: **↑ Import .sbz** under gear → Backup replaces the library index of the running server the same way.

Using **Docker Compose** instead, or want automatic HTTPS? See the full guide → **[DEPLOY.md](DEPLOY.md)**.

---

## First-time setup

1. **Sign in.** Open <http://localhost:8080> and log in with the admin username and password you created during install.
2. **Add your music.** Click the **gear icon** (top-right) to open the admin panel, enter a folder path, and click **Add**. Local folders work, and so do **FTP, SMB, and WebDAV** shares — no mounting required.
3. **Wait for the scan** (about 2 minutes per 50,000 tracks), then close the panel. Your library is ready.

The full user guide is built in: click **📖** in the header (or open <http://localhost:8080/manual/>), and press the **?** key for the keyboard shortcuts (the **?** button in the header opens the welcome guide).

---

## Handy commands

Run these from inside the SoniqBoom folder:

| You want to… | Command |
|--------------|---------|
| **Start the server** | `bash startup.sh` (same as `bash run.sh`) |
| **Change the port** (default is 8080) | `bash run.sh --port 9090` |
| **Stop the server** | `bash shutdown.sh` |
| **Restart the server** | `bash restart.sh` (add `--port 9090` if it runs on another port) — browsers and Subsonic apps stay signed in |
| **Rebuild the retro-format players** | `bash install.sh --rebuild` |
| **Delete the built-from-source players** | `bash install.sh --clean` |
| **Skip the slow zxtune build** (the PSF family, MED 2–4 modules and VGM files using chips libgme lacks won't play) | `bash install.sh --slim` |
| **Create your first admin login** | `bash setup-admin.sh` (once an admin exists, it names that account and shows how to reset it) |
| Create a login or reset its password non-interactively | `bash setup-admin.sh -user alice -passwd 'your-password'` |
| Disable (or re-enable) an account | `bash setup-admin.sh -user bob -disable` (or `-enable`) |
| Switch Subsonic, multi-room, casting *(Beta)* or the DLNA Media Server *(Beta)* on or off | `.venv/bin/soniqboom services list`, then e.g. `.venv/bin/soniqboom services enable dlna_server` (or `disable`) and `bash restart.sh` |
| Re-download the bundled ffmpeg | `.venv/bin/soniqboom fetch-ffmpeg --force`, then `bash restart.sh` |
| Back up the library index, playlists, ratings and play history | gear → **Backup** → **Export .sbz**, or with the server stopped (`bash shutdown.sh`): `.venv/bin/soniqboom export backup.sbz` — the settings file, user accounts, Subsonic apps' stars, bookmarks and play queues, and the radio Favorites aren't included |
| Restore that backup | `bash shutdown.sh`, then `.venv/bin/soniqboom import backup.sbz`, then `bash run.sh` — the import replaces the library index (changes made since the export are lost); or use **↑ Import .sbz** under gear → **Backup** while the server runs |
| Read the server log | gear → **Log**, or `log/soniqboom.log` in the data folder (the folder shown under [Configuration](#configuration); `/data` in Docker) — how much is logged is set under gear → System → Settings → Logging |

> Usernames are 2–64 characters (letters, digits, `.`, `_`, `-`); passwords can be up to 1,024 characters, and once an admin turns on **Require strong passwords** (gear → Users) every new password — `setup-admin.sh` included — needs at least 10 characters with an uppercase letter, a lowercase letter, a number and a symbol. New logins made with `setup-admin.sh` are admins unless you pass a role: add more people with `bash setup-admin.sh -user bob -passwd '…' -role readonly` (roles: `admin`, `edit`, `readonly`; `-display-name '…'` sets the name shown in the UI, up to 100 characters), or with **+ Add user** under gear → Users once you're signed in. While **Allow self-registration** is on (the default; gear → Users → Sign-up & passwords), visitors can also create a Read-only account themselves from the sign-in page (at most 10 new accounts an hour from one address, 60 in all) — switch it off if anyone who can reach the server shouldn't be able to listen. If the server is already running and listens on all interfaces (the default) or on localhost, a `setup-admin.sh` change takes effect right away (`setup-admin.sh` tells it to reload); otherwise restart the server. Under gear → Users an admin can also change an account's role, disable it, set a new password or delete it (you can't delete the account you're signed in with, and the last enabled admin can't be demoted or disabled); a lower role, a disabled account or a new password set there signs that user out everywhere, a higher role doesn't. Changing your own password keeps you signed in on the browser you changed it in and signs out every other one. A browser stays signed in — across server restarts too — until it hasn't used the server for 7 days: each use moves the end of the sign-in on, and the browser's sign-in cookie is sent again with the new end at most once an hour. An account keeps at most 50 browser sign-ins (a new one beyond that ends the one unused longest), and 15 wrong passwords within 15 minutes lock that account's sign-in for 15 minutes. A damaged `users.json` is moved aside as `users.json.corrupt-…` and its copy `users.json.bak` is used (a damaged copy is moved aside as `users.json.bak.corrupt-…`). If no account can be loaded although accounts existed before — both files are damaged or gone, or `users.json` can't be read at all (for example a permission error; the copy isn't used then) — sign-in stays locked (and the DLNA Media Server, Beta, doesn't list or play anything) until you put a good `users.json` back into the data folder (the running server picks it up within a few seconds; then click **re-check** on the sign-in page) or create an admin with `setup-admin.sh`; creating one starts a new account list (the accounts in the unreadable file are not brought back; a `users.json` that couldn't be read is moved aside as `users.json.corrupt-…` then). A change `setup-admin.sh` makes while the server runs is never undone by the server's own next save of `users.json`.

> **`install.sh` sets up every renderer for you** — `sidplayfp` (SID), `fluidsynth` (MIDI), `libopenmpt` (trackers), `uade` (AHX and the Amiga exotics), `libgme` (console chiptunes), `adplay` (AdLib/OPL), `psgplay` (SNDH), `sc68` (`.sc68`), `zxtune123` (PSF family, MED 2–4 modules, and VGM files that use a chip libgme lacks), `lha` from lhasa (LHA archives), and a static `ffmpeg`. On Linux, `lha` comes from your distribution's packages when it has them (on Arch, install lhasa from the AUR). `install.sh` builds `sidplayfp` 3.1.0 from source — on macOS unless a working `sidplayfp` is already installed, on Linux unless one with libsidplayfp 3.1 or newer is (on Linux, if that build fails, the distribution's `sidplayfp` is used) — and the Docker image ships that version too. If one ever fails to install, SoniqBoom names the exact missing package and everything else keeps working; a player installed somewhere SoniqBoom doesn't look can be set under gear → **Renderers → Renderer binaries** (uade123, gme, adplay, psgplay, sc68, zxtune123). (HivelyTracker `.hvl` and Atari ST `.ym` need no extra package — their engines ship with SoniqBoom and are compiled the first time one plays, which needs a C compiler for `.hvl` and a C++ compiler for `.ym`.)
>
> If a player ever stops working after a system update, `bash install.sh --rebuild` rebuilds the players SoniqBoom compiles from source (the rest are managed by your package manager). `bash install.sh --clean` just removes them. In a hurry? `bash install.sh --slim` skips the slow `zxtune123` build (~10–15 min on macOS) — the **PSF family** (PSF, PSF2, USF, GSF, 2SF, NCSF, SSF, DSF), MED 2–4 modules and VGM files that use a chip other than the Sega PSG/YM2612 won't play, and re-running without `--slim` adds them back. `bash install.sh --help` lists all the options. On startup SoniqBoom also checks every player and logs a one-line health summary, so a broken one is easy to spot; gear → **Renderers → Renderer Status** lists each player with the version it reports.

---

## Retro formats, first-class

SoniqBoom treats **demoscene, chiptune, and tracker formats as first-class citizens** — rendered on the fly, tagged with *real* scene metadata, visualized channel by channel, and streamed to any browser — with casting to Chromecast, AirPlay, and DLNA receivers in Beta.

Then, once you're hooked, you'll notice it's *also* a ruthlessly fast, fully-featured modern music server.

<p align="center">
  <img src="images/ui-main.png" alt="SoniqBoom main library view" width="800">
</p>

---

## Every format the others gave up on

**🎮 Chiptune & console** — SID (C64), NSF / NSFe (NES), SPC (SNES), GBS (Game Boy), VGM / VGZ (Sega & arcade), AY (ZX Spectrum / Amstrad), KSS (MSX), SAP (Atari), GYM, HES.

**🎹 Tracker / module** — ProTracker (MOD), FastTracker 2 (XM), Impulse Tracker (IT), ScreamTracker 2 & 3 (STM / S3M), OctaMED, MultiTracker, DigiBooster Pro, Composer 669, UltraTracker, Oktalyzer, Imago Orpheus, Farandole, General DigiMusic, SoundFX, Grave Composer, DSIK… roughly twenty module formats in all.

**🕹️ Amiga heritage** — AHX *and* HivelyTracker (`.hvl`), the latter via a **bundled HivelyTracker engine** (prebuilt in the Docker image, compiled on first play on a native install). AHX tunes stop where the song ends instead of looping on, whenever the same bundled HivelyTracker engine finds that end. We render the unrenderable.

**🟥 AdLib / OPL2 FM** — the sound of DOS gaming: id Software / Apogee **IMF** (Wolfenstein 3D, Commander Keen, Duke Nukem), plus ROL, CMF, D00, RAD, LucasArts LAA, Sierra SCI, DOSBox DRO, HSC, RIX… rendered through **AdPlug**. (`.imf` files are detected automatically: Imago Orpheus modules play via the tracker engine, id Software IMF via the OPL synth.)

**🎼 MIDI** — synthesized with swappable SoundFonts, plus a one-click SoundFont marketplace. The Docker image includes a General-MIDI SoundFont; on a native install, add one under gear → **Renderers → Soundfonts** (download from the list or upload your own `.sf2` / `.sf3`) before MIDI plays.

**…and the everyday stuff, of course** — FLAC, ALAC, MP3, AAC, Ogg Vorbis, Opus, WavPack, Musepack, WAV, AIFF, and 1-bit **DSD** (DSF / DFF / WSD). ZIP and Amiga LHA/LZH archives are scanned *inside the archive* (a ZIP inside a ZIP too), as are C64 `.d64`/`.d71`/`.d81` and Amiga `.adf` disk images, so your `modarchive_2007.zip` hoard simply works — no unpacking.

> Forty-plus retro formats. One unified render pipeline. Zero "unsupported file" dead ends.

---

## We don't just play them. We understand them.

SoniqBoom speaks the culture:

- **🎚️ Per-channel VU meters for modules and chip tunes.** Watch every channel of a module — up to 32 — dance in real time, right in the browser, and follow the notes in Track Info's live pattern view. Your `.it` files have never looked like this. C64 SID tunes get a three-voice meter and Amiga player formats (AHX included) a per-voice one (for tunes up to 10 and 7 minutes long respectively); other chip formats show a spectrum.
- **🧬 Real SID metadata from HVSC.** Per-tune song lengths straight from `Songlengths.md5` — so your SID tunes show *correct* durations instead of a flat three-minute guess — plus **STIL** credits for the tunes in your HVSC copy. Point SoniqBoom at your HVSC `DOCUMENTS` folder under gear → **Renderers**, or let a scan of your SIDs find it further up the folder tree.
- **🎛️ SID fidelity, your call.** Choose the chip (follow the tune, MOS 6581 or MOS 8580, optionally forced over the tune's own choice), filter emulation and filter curve, and 8580 digiboost under gear → **Renderers → SID fidelity** — the next play of each tune uses the new sound, no restart. Track Info shows the chip each tune was written for (from its header).
- **🎼 Module credits from the audacious-uade-tools song database.** Download it once under **gear → Metadata** and your Amiga and tracker modules get their artist, game or demo, publisher, release year and, for many, their song length — matched by checksum against about 380,000 modules compiled from some 400 scene sources ([audacious-uade-tools](https://github.com/mvtiaine/audacious-uade-tools) by Matti Tiainen, licensed [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)).
- **🎮 Game names from archive names.** An Amiga TFMX tune inside `Turrican.lha` or an SPC rip inside `Super_Mario_World.zip` gets its game from the archive's name when that name is a known game of the tune's platform, checked against the included title lists of Wikidata, No-Intro, the MAME software lists and ZXDB and, on request, TOSEC and Redump (their downloads are kept: a removed list is added again without downloading, TOSEC is downloaded again only for a newer release, and an Update rebuilds a Redump list only when its contents changed). How a retro track's game is chosen — a game you type (or an album you set on a retro track) always wins; otherwise the first of these that names one:
  1. **The file itself** — the game in an SPC, NSF/NSFe, GBS, VGM/VGZ or PSF-family header, or a GAME tag
  2. **Modland** — the game folder of the tune's exact Modland match
  3. **Demozoo** — the game the tune is the soundtrack of, when composer, title and game name match
  4. **The song database** (audacious-uade-tools) — the game of an exact checksum match
  5. **A Modland file name** such as `gold of the aztecs-intro` (spelled as the title lists spell the game), while *Guess game from Modland file names* is on
  6. **The archive's name**, when it is a known game of the track's platform, while *Game from archive names* is on
  7. **The folder or archive name**, while *Album from folder name for retro formats* is on

  The names the other sources give become the game's other names (Track Info's *also* line; `game:` search finds any of them).
- **🏷️ Retro genres.** Chip tunes and modules with no genre tag are listed under what SoniqBoom knows about them: **Game**, **Demo**, **Cracktro**, **Music Disk**, **Diskmag** or **Scene** — from the track's game, console-rip formats, Demozoo (the production released the same year that uses the tune, and the party competition the tune entered), the composer's Demozoo group, HVSC's `GAMES` / `DEMOS` folders and folders named `games`, `demos`, `cracktros`, `musicdisks` … — plus its platform: **SID**, **Amiga**, **Atari ST**, **NES**, **Game Boy**, **AdLib**, **MIDI** and more. With *Game and group names as genres* (on by default) the game, scene group and party are genres too, so Subsonic apps can browse a game's or a group's tunes. A genre in the file's own tags (other than the format words SoniqBoom adds, such as *Chiptune* or *C64*), or one you type, replaces them.
- **🛰️ The Library Galaxy.** Your entire collection rendered as a drifting star field, every format its own glowing constellation, sized by how much of it you own — or flip to a sortable, searchable list and browse by format family (trackers, chiptune, lossless, lossy, other). Click a format to list its tracks.
- **🔌 Live signal-chain visualization.** See the decode path of the playing track — `HVL → hvl2wav → PCM → ReplayGain → WebAudio` — laid out and lit up in Track Info, with the renderer named for SID, MIDI, FLAC / ALAC, DSD, NSF and SPC tracks, AHX and HivelyTracker tunes, and MOD, S3M, XM, IT, MTM, STM and ULT modules. Nerdy and weirdly mesmerizing.
- **🗂️ Multi-subtune aware.** SID, SNDH, SC68, Amiga player-format (UADE, other than `.ahx` files) and NSF / NSFe / GBS / AY / SAP files with multiple subsongs are addressed individually, not flattened into one — pick any tune in Track Info or in the phone's Now Playing, and Subsonic apps list each tune as its own song (up to 60 per file). Desktop track lists and search results show a multi-tune file's count as a badge (`5 tunes`). Playing the file itself starts at its default tune: the start song it names, or for Amiga player formats (played by UADE), console rips and SC68 disks the first tune that isn't empty.
- **📼 Bundled HivelyTracker and ST-Sound engines.** `.hvl` modules and Atari ST `.ym` files need no extra package — the engines ship with SoniqBoom, prebuilt in the Docker image and compiled on first play on a native install (with the machine's C / C++ compiler).
- **🧑‍🎤 Scene credits in Track Info.** For SID, tracker, Amiga, Atari ST and PSF tracks a **Scene** tab shows the composer's [Demozoo](https://demozoo.org) profile — real name, groups, aliases — the release the tune belongs to with its party placings, and more by the same composer.
- **🩺 Report broken tunes, skip the short ones.** Users with edit rights can mark a track *Glitched* or *Broken* from Track Info (with an optional note), the right-click menu or the phone. The report records who, when and which renderer version played it, covers identical copies, and a broken track is passed by when a list plays through (a click still plays it). **Skip short tunes** (gear → System → Settings) passes by retro tunes known to be shorter than 5, 10, 20 or 30 seconds — sound effects and jingles — while a tune you pick still plays. An Atari ST `.ym` file no engine can decode is flagged *corrupt* at scan and passed by too, and a render that comes out silent is never played.
- **⏱️ Plays while it renders.** In the browser, the first play of a SID, MIDI, AdLib, SNDH, PSF, console or Amiga tune starts while the rest is still rendering (Safari waits for the render when the tune's length isn't known yet); the finished render is kept in the render cache (gear → Renderers → Transcode & render cache), and the next track in the queue is prepared in advance.

<p align="center">
  <img src="images/demo-tracker.gif" alt="Live per-channel VU meters on a tracker module, with module metadata and the decode signal path" width="49%">
  <img src="images/demo-subsong.gif" alt="A SID tune's individually-addressable subsongs and STIL commentary from HVSC" width="49%">
</p>
<p align="center"><sub>Left: a ScreamTracker 3 module with <b>per-channel VU meters dancing in real time</b>, real module metadata, the scene composer's bio, and the live decode path. Right: a Rob Hubbard SID with <b>individually-addressable subsongs</b> and full <b>STIL</b> commentary, straight from HVSC.</sub></p>

<p align="center">
  <img src="images/ui-galaxy.jpg" alt="The Library Galaxy — every format its own constellation" width="800">
</p>
<p align="center"><sub>The Library Galaxy — your whole collection as a drifting star field, every format its own glowing constellation, sized by how much of it you own.</sub></p>

---

## Oh — and it happens to be a complete modern music server

Every retro format is rendered to standard audio on the fly — a 1987 SID tune streams to your phone *exactly* like a FLAC does (casting it to a HomePod or the speakers in the next room is in Beta). The same server that sees your `.mod` hoard handles your everyday listening.

- **⚡ Entire library held in RAM.** Browse and search a six-figure collection as fast as a ten-song playlist.
- **🎧 Plays what your browser plays, converts the rest.** MP3, FLAC, WAV, Ogg and Opus stream untouched, seekable from the first second (older Safari gets Ogg and Opus converted); AAC goes straight to every browser that decodes it, and ALAC straight to Safari. Everything else — ALAC in other browsers, AIFF, WavPack, Musepack and 1-bit DSD (as PCM up to 96 kHz) — is converted on the fly and starts playing at once. Subsonic apps can ask for MP3, Ogg or FLAC, and for a bitrate cap.
- **📚 Browse your way.** All Tracks, Artists, Album Artists, Albums (as a list or a cover grid), Genres, Years, folders and — once Demozoo data is applied — demoscene **Scene groups**, with an A–Z jump rail, sortable columns you can show, hide and resize, and **Play all** / **Shuffle** on track lists (a shuffle covers every match, not just the rows on screen). Smart views list the server's listening **History** and its **Most Played**, **Recently Added**, **Top Rated** and **Unplayed** tracks, and a **Duplicates** view groups every copy of a song and exports the list as CSV.
- **📻 Internet radio — with the scene built in.** A curated demoscene & chiptune station pack — **SceneSat**, **Nectarine**, **SLAY Radio**, **Kohina**, **Radio PARALAX**, **CVGM**, **Rainwave**, **OverClocked ReMix Radio**, **Keygen-FM** and **CGM UK DemoScene** — alongside the worldwide [Radio Browser](https://www.radio-browser.info/) directory (browse by continent and country), with live now-playing titles and a shared **Favorites** list (edit and admin accounts add stations with ★). While a Stations list is open, the main search finds stations too, with a **Show all** view for the full list.
- **🎲 Instant Mix radio.** Press the radio button on any track and SoniqBoom builds an endless, self-refilling queue around it — picked by genre, artist, era, tempo and format — in a focused radio view with a live oscilloscope over the cover. A SID radio stays chiptune; a FLAC radio follows the genre.
- **🔎 More like this & smart playlists.** Surface the closest matches to any song, ranked by what the tracks have in common — artist, genre, era, tempo and format, or for chip tunes and modules the composer, sound chip, replayer, samples and scene group — and, when both have been played, by how their loudness rises and falls. Save any search — say `format:SID year:>1988` — as a playlist that keeps itself up to date.
- **🎚️ 10-band graphic equalizer.** Ten bands from 32 Hz to 16 kHz, seven presets (Rock, Jazz, Vocal…) and clip-safe pre-gain — right in the browser. Press **E** or hit the **EQ** button.
- **🔊 ReplayGain volume levelling.** Even out loudness across the library — choose off, per-track, or per-album under *Volume leveling* in the EQ panel, applied live at playback from the file's ReplayGain tags.
- **⏯️ Smooth hand-offs, on every device.** Set *Crossfade* (up to 12 seconds; gear → System → Settings, or **Preferences**) and the current track fades out while the next one fades in, starting that many seconds early, or leave it at 0 and the next track is loaded during the last 20 seconds of the current one, so it starts without a pause. Media keys and the lock screen's controls work, and the screen stays awake while music plays. Your play queue is kept on the server for your other devices and Subsonic apps, and a browser offers to resume a newer queue saved on another device (*Sync the play queue across devices*, on by default, per browser).
- **📡 Cast / AirPlay / DLNA (Beta).** Send your everyday files and SID, MIDI, tracker, AHX / HivelyTracker and console-chiptune tunes — rendered and transcoded on the fly when the receiver needs it — to Chromecast, Apple TV, HomePod, or UPnP receivers. Each needs its Python package installed into SoniqBoom's environment: `pychromecast` (Chromecast), `pyatv` (AirPlay) or `async-upnp-client` (DLNA). On a Linux Docker host, run the container with `--network host` (instead of `-p 8080:8080`) so casting and the DLNA Media Server (Beta) can reach your devices; a package added with `docker exec soniqboom pip install …` has to be added again whenever the container is re-created.
- **🖧 DLNA Media Server (Beta).** Optionally announce your library on the LAN as a UPnP/DLNA MediaServer so any DLNA player — a TV, a console, a network amp — can browse and play it. Off by default; switch it on in gear → **System → Services**.
- **🔁 Multi-room sync.** Open `/multiroom` on each device: the same track, in lockstep, across every browser on your LAN — or send a favourite internet-radio station to a whole room, its live station, artist and cover art updating in place.
- **📲 Touch-first mobile UI.** Phones get a dedicated phone shell automatically, at `/m` — browse and play your library, pick a multi-tune file's tunes, tune in internet radio streamed straight to the phone, manage playlists, swipe to queue, long-press for more, and a tap-to-expand mini-player. To switch a browser between the phone and desktop layouts, open the server's address with `?ui=mobile` or `?ui=desktop` (e.g. `http://your-server:8080/?ui=desktop`); that browser remembers the choice for a year.
- **📱 OpenSubsonic API.** Works with Amperfy, Symfonium, DSub, and the rest of the Subsonic app ecosystem (Subsonic API 1.16.1 plus OpenSubsonic extensions such as synced lyrics, API keys, transcoding and similar tracks). Each tune of a multi-tune file is its own song, and the radio **Favorites** play in the apps too. Apps sign in with your SoniqBoom username and password; each account can also generate a separate Subsonic app password and create or revoke OpenSubsonic API keys (gear → System → My Account for admins, gear → Preferences for everyone else).
- **🗄️ Network shares without mounting.** Attach FTP, SMB, and WebDAV libraries straight from the admin UI — no OS mount required. MP3, WAV, Ogg, Opus and seekable FLAC files on a share start playing while they download, and a track inside a large ZIP on a share is read on its own, without fetching the whole archive. When a drive or share isn't connected, its tracks stay in the library and are marked **offline** in lists and search results — or hidden, or listed as usual (gear → **System → Settings → Tracks on unavailable drives**). Marked or hidden tracks are skipped by shuffle, Instant Mix and the next track. A track on a share whose file is already in the cache still plays. Local folders are watched, so new and changed files are picked up on their own; shares are re-checked every 5 minutes to 4 hours, depending on how often they change (or on demand with **Check now**). For FTP servers, gear → **Library → FTP Connection Pools** sets how many connections scans and playback may use per server, and SoniqBoom keeps within a server's own connection limit.
- **🌐 The Modland catalogue.** One switch (gear → **Collections**) lists [Modland](https://ftp.modland.com/)'s module archive in your library — about 330,000 tunes of Amiga, C64, Atari ST, console and tracker music, or about 415,000 with the big PC tracker folders — under a **Modland** folder, browsable and searchable right away. Nothing is copied: a tune is downloaded from Modland (with the sample or library files its format needs) when you play it or open its Track Info — and, if you choose *For tunes shown in lists*, SC68, SNDH and console chiptune files (NSF, NSFe, GBS, HES, KSS, AY, SAP) are also read when a list shows them — and then plays from SoniqBoom's remote cache; that first download fills in its length, its tunes and its own tags, as for a file of your own. With Modland's checksum list installed (gear → Metadata → Scene origin), Amiga modules show how many tunes they hold before that when the song database is installed, and HVSC SIDs when HVSC's song lengths are set up. The index is refreshed daily, weekly, every 2 weeks or monthly (your choice) or on demand, and a refresh usually downloads only Modland's changelogs since the last one — a few kilobytes. A moved or renamed tune keeps its ratings, play counts and playlist entries. Subsonic apps and the DLNA Media Server (Beta) don't see the catalogue unless you switch that on.
- **🎹 HVSC, online.** Another switch under gear → **Collections** lists the [High Voltage SID Collection](https://6581.nl/) — every C64 SID tune, over 60,000 — under an **HVSC** folder, from HVSC's own song-length list: each tune shows its number of tunes, their lengths and its STIL comment before it is played, and is downloaded from 6581.nl when you play it. With no HVSC folder of your own set, its song lengths and STIL comments serve your own SID files too, matched by checksum. Modland's copy of HVSC is left out while it is on (a switch).
- **📦 Aminet and scene.org, online.** Two more switches list [Aminet](https://aminet.net/)'s music section (about 20,000 uploads of Amiga modules and module sets) and [scene.org](https://files.scene.org/browse/music/)'s music archive (artists, groups, compos, music disks, compilations). Their archives are listed as one entry each — titled, with Aminet's description or the folder's composer, group or album — and are opened when you play them or open their Track Info: the archive is downloaded (a large ZIP only in part) and the tunes inside it take its place: the first one keeps the entry's place in your queue and playlists, the others follow it in the queue.
- **🌊 Waveform seek bar, visualizers and shortcuts.** The player bar draws each track's waveform (each tune's, for multi-tune files) to seek on; **V** cycles a visualizer behind the track list — oscilloscope, CRT scope, spectrogram, hyperspace, synthwave, globe, cosmic, lava lamp and raccoon — and the **?** key lists the keyboard shortcuts. Under gear → System → Settings → **Visualizations** (or **Preferences**), each browser can switch the animations off (all of them, or just the now-playing ones) and pick the VU meter's style, **Bars** or **Circuit board**.
- **👥 Multi-user with roles** (admin, edit, read-only — for accounts without admin rights the gear opens **Preferences**, with this browser's crossfade, preload buffer, theme, visualizations and more), **last.fm + ListenBrainz scrobbling**, **time-synced lyrics**, **chapter lists for MP3 / M4A podcasts and audiobooks**, **star ratings** (one set for the whole library, given by edit and admin accounts), **in-browser tag editing**, **field-operator search** (`artist:Ghost year:>2020 format:FLAC`), **dark and light themes**, **offline downloads** (⤓, saved in that browser when it reaches SoniqBoom over HTTPS or on localhost), **installable PWA**, and **absolutely zero telemetry** (the outside services SoniqBoom contacts are listed under [Attributions](#attributions)).

<p align="center">
  <img src="images/ui-folders.png" alt="Folder browser" width="49%">
  <img src="images/ui-search.png" alt="Instant search" width="49%">
</p>
<p align="center">
  <img src="images/ui-artists.png" alt="Artists view" width="49%">
  <img src="images/ui-admin.png" alt="Admin panel" width="49%">
</p>
<p align="center">
  <img src="images/ui-lyrics.jpg" alt="Time-synced lyrics scroll with the track" width="800">
</p>
<p align="center"><sub>Time-synced lyrics scroll line-by-line with playback — fetched from LRCLib or lyrics.ovh when the file has none embedded.</sub></p>

<p align="center">
  <img src="docs/manual/img/24-stations.png" alt="Internet radio Stations — the curated scene pack and the Radio Browser world directory" width="800">
</p>
<p align="center"><sub>Internet-radio <b>Stations</b> — a curated demoscene/chiptune pack plus the worldwide Radio Browser directory (continent → country), with the live now-playing track and a shared Favorites list.</sub></p>

<p align="center">
  <img src="docs/manual/img/25-stationinfo.png" alt="A scene radio station up close — tags, website, the live now-playing track, and the full quality ladder" width="49%">
  <img src="docs/manual/img/16-multiroom-master.png" alt="Multi-room — the same track in lockstep across every browser on the LAN" width="49%">
</p>
<p align="center"><sub>Left: a <b>scene radio</b> station up close — tags, website, the live now-playing track, and every available stream quality. Right: <b>multi-room sync</b> — the same track, in lockstep, across every browser on your LAN.</sub></p>

---

## Supported formats

| Family | Formats | Rendered by |
|--------|---------|-------------|
| **Lossless / PCM** | FLAC, ALAC (M4A), WAV, AIFF, WavPack | native / ffmpeg |
| **Lossy** | MP3, AAC, Ogg Vorbis, Opus, Musepack | native / ffmpeg |
| **DSD (1-bit)** | DSF, DFF, WSD | ffmpeg *(to PCM, up to 96 kHz)* |
| **SID** (C64) | `.sid`, `.psid`, `.rsid` | sidplayfp + HVSC Songlengths & STIL |
| **MIDI** | `.mid`, `.midi` | FluidSynth + SoundFonts |
| **Tracker / module** | MOD, S3M, XM, IT, MTM, MED, OCT, 669, DBM, ULT, STM, FAR, AMF, GDM, IMF *(Imago Orpheus)*, OKT, SFX, WOW, DSM | libopenmpt *(MED 2–4 modules: zxtune123)* |
| **Amiga** | AHX, HivelyTracker (HVL) | uade123 / bundled HivelyTracker engine |
| **Amiga exotics** | ~175 custom players: TFMX *(Turrican)*, Future Composer, SidMon 1/2, David Whittaker, Rob Hubbard, Jochen Hippel (+COSO/ST), Delta Music, SoundMon, JamCracker, Sonic Arranger, SoundFactory *(also when saved as `.psf`)*, ProWizard-packed MODs… — both Amiga prefix naming (`mdat.song`) and suffix naming, companion sample files resolved automatically | uade123 (runs the original Amiga player code) |
| **Atari ST** | SNDH *(with per-tune FRMS/TIME lengths and subsongs)*, YM register dumps, SC68 | psgplay / bundled ST-Sound engine / sc68 |
| **Console rips (PSF family)** | PSF, PSF2 *(PlayStation)*, USF *(N64)*, GSF *(GBA)*, 2SF + NCSF *(NDS)*, SSF *(Saturn)*, DSF *(Dreamcast)* — mini + shared-lib pairs kept together | zxtune123 (Highly Experimental / lazyusf2 / mGBA / vio2sf reference cores) |
| **Console chiptune** | NSF, NSFe, SPC, GBS, VGM, VGZ, AY, KSS, SAP, GYM (incl. packed GYMX), HES | libgme *(VGM/VGZ using a chip other than the Sega PSG and YM2612 — YM2413, YM2151, NES APU, AY-3-8910 …: zxtune123)* |
| **AdLib / OPL2 FM** | id IMF *(Wolfenstein 3D, Keen…)*, ROL, CMF, D00, RAD, LAA, SCI, DRO, HSC, RIX, A2M, ADL, BAM, KSM, AMD | AdPlug (`adplay`, Nuked OPL3 core) |

Archives and disk images are scanned and played **inline** — tracks inside `.zip` files (a ZIP inside a ZIP too), Amiga `.lha`/`.lzh` archives, C64 `.d64`/`.d71`/`.d81` and Amiga `.adf` disk images appear in your library without unpacking (on network shares: ZIP and LHA/LZH). Both switches for it — **Scan ZIPs** and **Scan ZIPs on remote shares** under gear → System → Settings — are on by default.

> **Curious where these formats came from?** [docs/FORMATS.md](docs/FORMATS.md) has the lore — every family's origin, claim to fame, and the stories behind them (why TFMX travels as two files, which chip designer went on to build the PlayStation, what `M.K.` stands for…).

---

## Comparison

How SoniqBoom stacks up against the five self-hosted music servers it's most often weighed against — Navidrome, Jellyfin, Plexamp, Music Assistant, and Lyrion Music Server. Snapshot as of September 2026, taken from each project's own documentation; projects evolve, so check their docs for the latest.

| | **SoniqBoom** | Navidrome | Jellyfin | Plexamp | Music Assistant | Lyrion |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Retro / tracker / chiptune formats** (SID, MOD, XM, IT, AHX, NSF, SPC, VGM, PSF, AdLib…) | ✅ **40+ families** | ❌ | ❌ | ❌ | ❌ | ⚠ via third-party plugin (beta) |
| **Everyday formats** (FLAC, ALAC, MP3, AAC, Opus, DSD) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Free — no paid tier** | ✅ AGPL | ✅ | ✅ | 💰 Plex Pass gates offline, EQ &amp; remote streaming | ✅ | ✅ |
| **Zero cloud · no account · no telemetry** | ✅ | ✅ | ✅ | ❌ account required | ✅ | ✅ |
| **Multi-room sync** (native) | ✅ any browsers on the LAN | ❌ | ⚠ SyncPlay (video-oriented) | ❌ | ✅ via Home Assistant hub | ✅ via Squeezebox players |
| **Cast — AirPlay · Chromecast · DLNA** | ✅ all three *(Beta)* | ❌ | ⚠ Chromecast; DLNA via plugin | ✅ AirPlay · Chromecast | ✅ all three | ⚠ via bridge plugins |
| **Subsonic / OpenSubsonic API** (Amperfy, Symfonium, DSub…) | ✅ | ✅ | ❌ | ❌ | ❌ | ⚠ via third-party plugin |
| **Internet radio** (Radio Browser directory) | ✅ + curated scene station pack | ⚠ manual station list | ❌ | ❌ | ✅ | ✅ (plugin) |
| **Demoscene metadata** (HVSC song lengths &amp; STIL · Demozoo composer, crew, release year &amp; game soundtracks · audacious-uade-tools module credits, games &amp; lengths) | ✅ | ❌ | ❌ | ❌ | ❌ | ⚠ HVSC song lengths via the retro plugin |
| **Plays tracks inside ZIP archives** (no unpacking) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Network shares without mounting** (FTP · SMB · WebDAV) | ✅ | ❌ | ❌ | ❌ | ✅ (SMB · NFS · WebDAV) | ❌ |
| **In-browser tag editing** (writes real tags into local files) | ✅ | ❌ read-only | ⚠ database only | ⚠ database only | ❌ | ❌ |
| **Graphic equalizer in the web player** | ✅ 10-band | ❌ | ❌ | 💰 Plex Pass | ❌ | ❌ |
| **Tracker visualization** (per-channel VU · live pattern view) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Smart playlists &amp; similar-track suggestions** | ✅ + Instant Mix radio | ⚠ similarity via plugin | ⚠ via plugins | 💰 Plex Pass | ✅ | ⚠ via plugin |

### Where SoniqBoom reigns supreme

- **It plays the formats the others don't.** Forty-plus retro families — SID, ~20 tracker formats, AHX and HivelyTracker, ~175 Amiga player formats, Atari ST, the whole PSF family, console chiptunes, AdLib/OPL2, and MIDI. Of the servers above, only Lyrion plays some of them, through a third-party plugin in beta.
- **Scene metadata** Correct per-tune SID lengths and STIL credits from HVSC, composer, crew, release year and the game a tune was made for from Demozoo, plus module artists, games, publishers, years and many song lengths from the audacious-uade-tools song database - a set of metadata no other server carries.
- **Your ZIP hoards just work.** Tracks inside `.zip` archives appear and play in place; every other server needs them unpacked first.
- **Multi-room that needs nothing extra.** Lockstep playback across any browsers on your LAN — no Squeezebox hardware, no Home Assistant hub.
- **Your files where they already are.** Attach FTP, SMB, and WebDAV shares straight from the admin UI with no OS mount - only Music Assistant comes close.
- **Real tag editing, in the browser.** It writes actual tags into local MP3, FLAC, M4A, Ogg, Opus, WavPack and Musepack files, and other tracks (chip tunes, modules, archive members, files on shares) get their corrections saved in the library; Navidrome is read-only by design, and Jellyfin and Plexamp only edit their own database.
- **A full listening toolkit, free.** A 10-band EQ, ReplayGain, Instant Mix radio, "more like this" ranked by what tracks have in common and how their loudness moves, and self-updating smart playlists — with no paid tier, no account, and no telemetry.
- **Channel-by-channel visualization.** Per-channel VU meters, a live pattern view for tracker modules, and the Library Galaxy — a view of your collection no other server offers.

---

## Configuration

Most settings live in the admin UI. The config file is created for you on first run:

| Platform | Path |
|----------|------|
| macOS | `~/Library/Application Support/SoniqBoom/SoniqBoom.conf` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/soniqboom/SoniqBoom.conf` |
| Docker | `/data/soniqboom/SoniqBoom.conf` on the data volume |

Where things are kept, and how to change it:

- `SONIQBOOM_CONF` uses a config file somewhere else, and `SONIQBOOM_DATA_DIR` moves the library index, user accounts, logs and downloaded metadata. The config file and the art and render caches stay in the platform folder above unless `SONIQBOOM_CONF`, `art_cache_dir` and `conversion_cache_dir` say otherwise; `prefs.json` (the song database's and Demozoo's *Re-apply automatically after each library scan* switches) always stays in the platform folder.
- **In Docker**, only what's under `/data` outlives a re-created container. `/data` holds the library index (`library.json`, `.bak`, `.prev`, `library.aof`), user accounts (`users.json`, its copy `users.json.bak` and `secret.key`), browser sign-ins (`sessions.json`), Subsonic stars, bookmarks and play queues (`subsonic_state.json`), logs, the network-share file cache and downloaded metadata. `/data/soniqboom` holds the settings file (`SoniqBoom.conf`), `prefs.json`, the cover-art and render caches and, in `/data/soniqboom/soundfonts`, the SoundFonts you add under gear → Renderers → Soundfonts (the General-MIDI SoundFont is added there while it holds none). `/data/credential.key` holds the key saved network-share passwords and last.fm / ListenBrainz logins are encrypted with — back it up together with the rest of `/data`. No extra `docker run` options are needed for any of this.
- **Environment variables:** most settings at the top level or under `renderers` in `SoniqBoom.conf` can also be given as `SONIQBOOM_<NAME>` — e.g. `SONIQBOOM_CONVERSION_CACHE_DIR` or `SONIQBOOM_SIDPLAYFP_PATH`, as well as `SONIQBOOM_FFMPEG_PATH` and `SONIQBOOM_TRANSCODE_FORMAT` — in the environment or in `~/.soniqboom.env`. Both win over `SoniqBoom.conf`, so a setting given that way also overrides what you save for it in the admin UI once the server restarts. The sign-up switches, services, network shares, plugin settings and `remote_cache_max_mb` are read from `SoniqBoom.conf` only. The last.fm keys below, `SONIQBOOM_CONF`, `SONIQBOOM_SCAN_WORKERS`, `SONIQBOOM_RENDER_SLOTS`, `SONIQBOOM_PLAYBACK_IO_THREADS`, `SONIQBOOM_CORS_ORIGINS`, `SONIQBOOM_FRAME_ANCESTORS` and `SONIQBOOM_SUBSONIC_CORS` must be real environment variables — they aren't read from `~/.soniqboom.env`.
- `SONIQBOOM_CORS_ORIGINS` takes a comma-separated list of origins (e.g. `https://dashboard.example.com`) whose web pages may call SoniqBoom's web API (`/api`) from the browser, with the browser's SoniqBoom sign-in; otherwise only SoniqBoom's own pages can. A page of any other origin — another port of the same host included — can't change anything through `/api` or open its live connections (the server answers 403). `SONIQBOOM_FRAME_ANCESTORS` takes a space- or comma-separated list of origins whose pages may show SoniqBoom's web UI in a frame; by default only SoniqBoom's own pages can. The Subsonic API (`/rest`) accepts calls from web pages on any origin (`SONIQBOOM_SUBSONIC_CORS=0` turns that off).
- `SONIQBOOM_OFFLINE=1` (or `"offline": true` in `SoniqBoom.conf`) makes the server send no request to hosts on the internet; hosts on your own network, such as a NAS holding your shares, are still reached — see [DEPLOY.md → Configuration](DEPLOY.md#configuration).

Environment variables like `SONIQBOOM_HOST` / `SONIQBOOM_PORT` apply when you launch the raw `soniqboom` binary directly (`soniqboom --host 0.0.0.0 --port 9090` works too). With the helper scripts, change the port with `bash run.sh --port 9090` instead — `run.sh` always passes its own port (8080 unless you give one), so it reads neither `SONIQBOOM_PORT` nor the port in `SoniqBoom.conf`. (It does honour `SONIQBOOM_DATA_DIR`.) `SONIQBOOM_SCAN_WORKERS` and `SONIQBOOM_RENDER_SLOTS` set how many scan processes and retro-format renders (and cached ffmpeg conversions) run at once (default: about half the usable CPUs), and `SONIQBOOM_PLAYBACK_IO_THREADS` how many threads read the files being played — see [DEPLOY.md → Configuration](DEPLOY.md#configuration).

A few options have no switch in the admin UI:

- **last.fm scrobbling** needs the server's last.fm API key and secret in `SONIQBOOM_LASTFM_API_KEY` and `SONIQBOOM_LASTFM_API_SECRET`, on top of the session key saved under gear → System → My Account (a section of the System tab, which only admin accounts have). ListenBrainz needs only the user token saved there.
- **Radio now-playing covers**: `discogs_token` in `SoniqBoom.conf` adds Discogs (a personal access token) to the lookup, and `"radio_art_lookup": false` stops looking up covers for what a station plays.

---

## Requirements & footprint

Runs on **x86-64 and ARM64** — Raspberry Pi 4/5, ARM cloud servers, Apple Silicon, and ordinary Intel/AMD boxes. The Docker image is multi-arch, so `docker pull` fetches the right one automatically. A native install needs **Python 3.11 or newer**: `install.sh` installs Python 3.12 through Homebrew on macOS and uses your distribution's `python3` on Linux.

Honest, approximate numbers (idle, on the ARM64 image):

| | |
|---|---|
| **Idle memory** | ~125 MB with an empty library. The track index lives in RAM, so memory grows with library size — budget more for very large collections and test on your target hardware. |
| **Docker image** | ~1.3 GB on disk, **~400 MB compressed** to pull. Most of it is ffmpeg, the retro-format players and a General-MIDI SoundFont. |
| **CPU** | Idle is near-zero. Retro formats are rendered to audio server-side on play; a SID or tracker module decodes comfortably on a Pi-class core. Large first-time library scans are the main CPU spike. |
| **Disk** | Your music (left untouched unless you edit tags in the browser or turn on *Save fetched lyrics to files*) + the data folder (`/data` in Docker: library index, accounts, logs, remote and ZIP caches, downloaded metadata) + the render & transcode cache, in `cache/conversion` under the platform folder listed in [Configuration](#configuration) unless `conversion_cache_dir` is set. That cache grows as you play transcoded/retro formats, up to the size set under gear → Renderers → Transcode & render cache, and can be cleared any time. |
| **Modland catalogue** (optional) | About 1 GB more memory with the default *Tunes* selection (about 330,000 tunes), about 1.2 GB with *Everything*; about 200–250 MB more disk for the library index (measured natively on an Apple-silicon Mac). |

**Building the Docker image** and don't need AHX, the Amiga "exotica" family or native `.sc68` disks? Pass `--build-arg SLIM=1` to skip `uade` and `sc68`, two of the players the *image* compiles from source. Everything else — SID, MIDI, trackers, HivelyTracker, YM/SNDH, console chiptune, PSF, AdLib, all modern codecs — still ships, and the build is faster. The **default** image ships everything.

The **native installer** compiles a different set. On macOS it builds `zxtune123`, `sidplayfp`, and `psgplay` from source (`uade` and `sc68` come from Homebrew there), and the zxtune build is the slow one at roughly 10–15 minutes. On Linux it builds `psgplay` and — unless one with libsidplayfp 3.1 or newer is already installed — `sidplayfp` (with libsidplayfp 3.1 and libresidfp, into `/usr/local`) from source, builds `uade123` and `sc68` from source when the distribution has no package for them, and downloads a prebuilt `zxtune123`. Its shortcut is `bash install.sh --slim`, which skips only zxtune: the **PSF family** — PSF, PSF2, USF, GSF, 2SF, NCSF, SSF, DSF (PlayStation, N64, GBA, DS, Saturn, and Dreamcast rips) — MED 2–4 modules and VGM files that use a chip other than the Sega PSG / YM2612 won't play until you re-run the installer without `--slim`. (On Linux the same flag skips the prebuilt zxtune download, with the identical effect.)

---

## Who this is for

- **Demosceners & chiptune collectors** whose libraries are invisible to every other server.
- **HVSC / Modland / Modarchive hoarders** who want correct song lengths, STIL credits, and channel-level VU meters — not a generic file list.
- **Hi-fi self-hosters** who *also* keep FLAC and DSD and refuse to run a cloud account to hear them.
- **Anyone** who believes a 40-year-old `.mod` deserves a real player, not a "format not supported" toast.

If your music collection has a weird corner, SoniqBoom is the server that finally lights it up.

**Got a format we don't render?** That's the most interesting kind of bug report. Open an [issue](https://github.com/SFCyris/SoniqBoom/issues) with a sample file, and [star the repo](https://github.com/SFCyris/SoniqBoom) if it earned it. Format and feature ideas are welcome in [Discussions](https://github.com/SFCyris/SoniqBoom/discussions) — the code is maintained by me, but requests and bug reports genuinely steer what's built next.

---

## License

SoniqBoom is licensed under the **GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)**. See [LICENSE](LICENSE) for the full text.

In short: run, study, modify, and redistribute freely. If you distribute a modified version — including running it as a network service others interact with — you must make the corresponding source of your modifications available under the same license.

Commercial licensing (for use cases that cannot comply with AGPL-3.0's network-use copyleft) is available from the copyright holder on request — contact <scyris@outlook.com>.

SoniqBoom is intended for streaming music you already own or have the right to use. Users are solely responsible for ensuring their use complies with applicable copyright law.

---

## Attributions

SoniqBoom stands on the shoulders of these excellent open-source projects.

- [FastAPI](https://github.com/tiangolo/fastapi) by [Sebastián Ramírez](https://github.com/tiangolo) is licensed under [MIT License](https://github.com/tiangolo/fastapi/blob/master/LICENSE)
- [Uvicorn](https://github.com/encode/uvicorn) by [Encode](https://github.com/encode) is licensed under [BSD-3-Clause](https://github.com/encode/uvicorn/blob/master/LICENSE.md)
- [httpx](https://github.com/encode/httpx) by [Encode](https://github.com/encode) is licensed under [BSD-3-Clause](https://github.com/encode/httpx/blob/master/LICENSE.md)
- [Pydantic](https://github.com/pydantic/pydantic) by [Pydantic](https://github.com/pydantic) is licensed under [MIT License](https://github.com/pydantic/pydantic/blob/main/LICENSE)
- [pydantic-settings](https://github.com/pydantic/pydantic-settings) by [Pydantic](https://github.com/pydantic) is licensed under [MIT License](https://github.com/pydantic/pydantic-settings/blob/main/LICENSE)
- [aiofiles](https://github.com/Tinche/aiofiles) by [Tin Tvrtković](https://github.com/Tinche) is licensed under [Apache License 2.0](https://github.com/Tinche/aiofiles/blob/master/LICENSE)
- [python-multipart](https://github.com/Kludex/python-multipart) by [Marcelo Trylesinski](https://github.com/Kludex) is licensed under [Apache License 2.0](https://github.com/Kludex/python-multipart/blob/master/LICENSE.txt)
- [websockets](https://github.com/python-websockets/websockets) by [Aymeric Augustin](https://github.com/aaugustin) is licensed under [BSD-3-Clause](https://github.com/python-websockets/websockets/blob/main/LICENSE)
- [watchdog](https://github.com/gorakhargosh/watchdog) by [Yesudeep Mangalapilly](https://github.com/gorakhargosh) is licensed under [Apache License 2.0](https://github.com/gorakhargosh/watchdog/blob/master/LICENSE)
- [Mutagen](https://github.com/quodlibet/mutagen) by [Quod Libet](https://github.com/quodlibet) is licensed under [GPL-2.0-or-later](https://github.com/quodlibet/mutagen/blob/main/COPYING)
- [lhafile](https://github.com/FrodeSolheim/python-lhafile) by Hidekazu Ohnishi is licensed under BSD-3-Clause
- [orjson](https://github.com/ijl/orjson) by [ijl](https://github.com/ijl) is licensed under MPL-2.0 AND (Apache-2.0 OR MIT)
- [mido](https://github.com/mido/mido) by [mido](https://github.com/mido) is licensed under [MIT License](https://github.com/mido/mido/blob/main/LICENSE)
- [Pillow](https://github.com/python-pillow/Pillow) by [Python Pillow](https://github.com/python-pillow) is licensed under [MIT-CMU (HPND)](https://github.com/python-pillow/Pillow/blob/main/LICENSE)
- [cryptography](https://github.com/pyca/cryptography) by [PyCA](https://github.com/pyca) is licensed under [Apache-2.0 / BSD-3-Clause](https://github.com/pyca/cryptography/blob/main/LICENSE)
- [PyYAML](https://github.com/yaml/pyyaml) by [YAML](https://github.com/yaml) is licensed under [MIT License](https://github.com/yaml/pyyaml/blob/main/LICENSE)
- [python-dotenv](https://github.com/theskumar/python-dotenv) by [Saurabh Kumar](https://github.com/theskumar) is licensed under [BSD-3-Clause](https://github.com/theskumar/python-dotenv/blob/main/LICENSE)
- [defusedxml](https://github.com/tiran/defusedxml) by [Christian Heimes](https://github.com/tiran) is licensed under [PSF-2.0](https://github.com/tiran/defusedxml/blob/main/LICENSE)
- [smbprotocol](https://github.com/jborean93/smbprotocol) by [Jordan Borean](https://github.com/jborean93) is licensed under [MIT License](https://github.com/jborean93/smbprotocol/blob/master/LICENSE)
- [pyatv](https://github.com/postlund/pyatv) by [Pierre Ståhl](https://github.com/postlund) is licensed under [MIT License](https://github.com/postlund/pyatv/blob/master/LICENSE.md) — optional, installed separately for AirPlay casting (Beta)
- [PyChromecast](https://github.com/home-assistant-libs/pychromecast) by [Home Assistant](https://github.com/home-assistant-libs) is licensed under [MIT License](https://github.com/home-assistant-libs/pychromecast/blob/master/LICENSE) — optional, installed separately for Chromecast casting (Beta)
- [async-upnp-client](https://github.com/StevenLooman/async_upnp_client) by [Steven Looman](https://github.com/StevenLooman) is licensed under Apache-2.0 — optional, installed separately for DLNA casting (Beta)
- [rumps](https://github.com/jaredks/rumps) by [Jared Suttles](https://github.com/jaredks) is licensed under [BSD-3-Clause](https://github.com/jaredks/rumps/blob/master/LICENSE)
- [pyobjc](https://github.com/ronaldoussoren/pyobjc) by [Ronald Oussoren](https://github.com/ronaldoussoren) is licensed under MIT License

External tools (invoked via subprocess) and libraries:

- [FFmpeg](https://ffmpeg.org/) is licensed under [LGPL-2.1-or-later / GPL](https://ffmpeg.org/legal.html) (depending on build configuration) — `install.sh`, `soniqboom fetch-ffmpeg` and the Renderers tab's ffmpeg download fetch a static build from [ffmpeg.martin-riedl.de](https://ffmpeg.martin-riedl.de) (macOS) or [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) (Linux)
- [sidplayfp / libsidplayfp / libresidfp](https://github.com/libsidplayfp/libsidplayfp) are licensed under [GPL-2.0-or-later](https://github.com/libsidplayfp/libsidplayfp/blob/master/COPYING) — renders SID; libsidplayfp 2.16.1 with reSIDfp is also included compiled to WebAssembly, which renders SID tunes in the browser for the per-voice VU meter and the optional *Play SID in browser* setting
- [FluidSynth](https://github.com/FluidSynth/fluidsynth) is licensed under [LGPL-2.1](https://github.com/FluidSynth/fluidsynth/blob/master/LICENSE)
- [libopenmpt](https://lib.openmpt.org/libopenmpt/) by [OpenMPT](https://openmpt.org/) is licensed under [BSD-3-Clause](https://lib.openmpt.org/libopenmpt/license/) — renders tracker modules (`openmpt123`) and drives the per-channel VU meters and pattern view
- [UADE](https://zakalwe.fi/uade/) (Unix Amiga Delitracker Emulator) is licensed under GPL-2.0 (its Amiga player files carry their own licences) — renders AHX and the other Amiga formats
- [HivelyTracker replayer](https://github.com/pete-gordon/hivelytracker) by Pete Gordon et al. is licensed under [BSD-3-Clause](https://github.com/pete-gordon/hivelytracker) — vendored to decode `.hvl` modules and to find where AHX tunes end
- [Game_Music_Emu (libgme)](https://github.com/libgme/game-music-emu) is licensed under [LGPL-2.1](https://github.com/libgme/game-music-emu/blob/master/license.txt)
- [AdPlay](https://github.com/adplug/adplay-unix) (GPL-2.0) with the [AdPlug](https://adplug.github.io/) library ([LGPL-2.1](https://github.com/adplug/adplug/blob/master/COPYING)) — renders AdLib / OPL2 FM formats
- [psgplay](https://github.com/frno7/psgplay) by Fredrik Noring is licensed under [GPL-2.0](https://github.com/frno7/psgplay/tree/main/licence) (some files LGPL-2.1 / MIT) — renders Atari ST SNDH
- [sc68](https://sc68.atari.org/) by Benjamin Gerard is licensed under GPL-2.0-or-later — renders native .sc68 disks
- [zxtune](https://github.com/vitamin-caig/zxtune) by Vitamin/CAIG is licensed under [LGPL-3.0](https://github.com/vitamin-caig/zxtune/blob/master/LICENSE.md) — renders the PSF console-music family
- [ST-Sound](https://github.com/arnaud-carre/StSound) by Arnaud Carré is licensed under BSD/MIT — vendored to decode Atari ST `.ym` files
- [libxmp](https://github.com/libxmp/libxmp)'s XPK-SQSH depacker by Claudio Matsuoka is licensed under MIT — ported to unpack XPK-SQSH packed Amiga modules
- [lhasa](https://github.com/fragglet/lhasa) by Simon Howard is licensed under ISC — on native installs, the `lha` tool reads an LHA archive the built-in readers can't
- [tini](https://github.com/krallin/tini) is licensed under MIT — the Docker image's init process

Data / assets:

- [FluidR3_GM SoundFont](https://packages.debian.org/trixie/fluid-soundfont-gm) (MIT) — the General-MIDI SoundFont in the Docker image; more SoundFonts (GeneralUser GS by S. Christian Collins, MuseScore_General and others, each under its own licence) can be downloaded under gear → Renderers → Soundfonts
- [HVSC (High Voltage SID Collection)](https://www.hvsc.c64.org/) (Free archive of SID music) — Songlengths.md5 and STIL metadata from your own HVSC `DOCUMENTS` folder, used for SID playback
- [audacious-uade-tools](https://github.com/mvtiaine/audacious-uade-tools) song database by Matti Tiainen, licensed [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) — module artists, publishers, albums, years and song lengths; downloaded by the user under gear → Metadata, not included with SoniqBoom
- [Wikidata](https://www.wikidata.org) video-game titles, [CC0 1.0](https://www.wikidata.org/wiki/Wikidata:Licensing) — game titles per retro platform, matched against archive names
- [No-Intro](https://no-intro.org) DAT game titles, [DAT-o-MATIC Data Usage License](https://datomatic.no-intro.org/terms.html) — game titles per retro platform
- [MAME software lists](https://github.com/mamedev/mame/tree/master/hash) titles, CC0 1.0 — game titles per retro platform
- [ZXDB](https://github.com/zxdb/ZXDB) ZX Spectrum game titles, [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/) — contains information from ZXDB, made available under the ODbL
- [TOSEC](https://www.tosecdev.org) and [Redump](http://redump.org) DAT game titles — downloaded by the user under gear → Metadata, not included with SoniqBoom
- [Modland](https://ftp.modland.com/) — Scene origin matches your modules against Modland's checksum list (`allmods_md5.zip`), and the Modland catalogue lists Modland's archive from its index (`allmods.zip`) and downloads a tune from Modland when it is played or its Track Info is opened; downloaded by the user under gear → Metadata → Scene origin (Modland) and gear → Collections, not included with SoniqBoom
- [The High Voltage SID Collection](https://www.hvsc.c64.org/), served by [6581.nl](https://6581.nl/) — the online HVSC collection lists HVSC from its song-length list and STIL comments, and downloads the tunes that are played
- [Aminet](https://aminet.net/) — the online Aminet collection lists Aminet's music section from its index and category names, and downloads the uploads (with their readme) that are played
- [scene.org](https://www.scene.org/) — the online scene.org collection lists its music archive from its folder listings, and downloads the files that are played
- [Demozoo](https://demozoo.org) — composers' scene groups, release years, game soundtracks and the productions and party competitions behind retro genres, from Demozoo's database export; downloaded by the user under gear → Metadata, not included with SoniqBoom

Online services contacted while you use SoniqBoom: [MusicBrainz](https://musicbrainz.org), [Wikidata](https://www.wikidata.org) and [Wikipedia](https://en.wikipedia.org) (artist bios; for Subsonic apps also the artist photo from Wikimedia), the [Cover Art Archive](https://coverartarchive.org) and, with a token, [Discogs](https://www.discogs.com) (covers for the song playing on a radio station, after your own library is checked), [LRCLib](https://lrclib.net) and [lyrics.ovh](https://lyrics.ovh) (lyrics for a track whose tags hold none — never for retro formats other than MIDI; at the first lookup, and then at most every 30 minutes while lookups are made, both are also checked for how fast they answer), [Radio Browser](https://www.radio-browser.info/) and the stations you play (radio — for Subsonic apps also a favourite station's logo; Radio Browser's country list is fetched when the world directory is opened and there is no stored copy or it is more than a week old; a search typed while a Stations list is open also looks for matching stations there once it has two or more characters, and Enter looks up whatever was typed, even one character; and playing a directory station also counts a click there), the Demozoo API (scener and production details), and [last.fm](https://www.last.fm) / [ListenBrainz](https://listenbrainz.org) (scrobbling, once you connect an account). The online collections you switch on under gear → Collections reach their archives — ftp.modland.com, 6581.nl, aminet.net, archive.scene.org — for their index (when switched on, on the schedule you pick, with **Refresh now**, and after a start that finds the index missing or out of date) and for the tunes that are played, about to play or opened in Track Info. Downloads you start yourself also reach GitHub (the song database, and its check for newer data), Demozoo's data export, TOSEC and Redump (game-title lists), Modland's checksum list, the SoundFont hosts listed under gear → Renderers → Soundfonts, and the ffmpeg build hosts named above. With `SONIQBOOM_OFFLINE=1` the server contacts none of these (see [Configuration](#configuration)).

The components, their licences and how SoniqBoom uses them are listed in [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md).

**SoniqBoom license:** [AGPL-3.0-or-later](LICENSE)

---

<p align="center">
  <strong>Point it at the weird corner of your collection. Watch it light up.</strong><br>
  <a href="https://github.com/SFCyris/SoniqBoom">github.com/SFCyris/SoniqBoom</a> &middot; <a href="https://github.com/SFCyris/SoniqBoom/issues">Issues</a>
</p>

<p align="center"><sub>Copyright &copy; 2026 S.F. Cyris &middot; Built by S.F. Cyris</sub></p>
