<p align="center">
  <img src="images/logo-banner.png" alt="SoniqBoom" width="600">
</p>
<p align="center"><strong>Self-hosted music server for your personal library</strong></p>
<p align="center">
  Streams FLAC, ALAC, MP3, Opus, plus SID, MIDI, and 20+ tracker formats &mdash; and doesn&rsquo;t choke on your 250k-track collection.
</p>
<p align="center">
  Self-hosted &bull; Browser-based &bull; AGPL-3.0 &bull; Zero cloud dependencies
</p>

<p align="center">
  <a href="https://github.com/SFCyris/SoniqBoom/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg" alt="License: AGPL-3.0-or-later"></a>
  <a href="https://github.com/SFCyris/SoniqBoom/releases"><img src="https://img.shields.io/github/v/release/SFCyris/SoniqBoom?include_prereleases&sort=semver" alt="Latest release"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="https://github.com/SFCyris/SoniqBoom"><img src="https://img.shields.io/badge/source-GitHub-181717.svg?logo=github" alt="Source on GitHub"></a>
</p>

---

## Table of Contents

- [What Makes SoniqBoom Different](#what-makes-soniqboom-different)
- [System Requirements](#system-requirements)
- [Installation](#installation)
  - [Linux (manual)](#linux-manual)
  - [Choosing a different port](#choosing-a-different-port)
- [Getting Started](#getting-started)
- [Features at a Glance](#features-at-a-glance)
  - [Library Browser](#library-browser)
  - [Instant Search](#instant-search)
  - [Folder Tree](#folder-tree)
  - [Smart Playlists](#smart-playlists)
  - [Multi-room Sync](#multi-room-sync)
  - [Admin Panel](#admin-panel)
  - [Network Shares](#network-shares)
- [Supported Formats](#supported-formats)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Usage and Copyright](#usage-and-copyright)
- [License](#license)

---

## What Makes SoniqBoom Different

| | SoniqBoom | iTunes / Apple Music | Plex / Jellyfin | foobar2000 |
|---|---|---|---|---|
| **Scale** | 170K+ tracks, instant | Slows above ~50K | Requires transcoding server | Desktop-only |
| **Setup** | One install script, then `run.sh` | Cloud account required | Docker + database server | Windows-only, plugins |
| **Access** | Any device with a browser | Apple devices only | App per platform | Local only |
| **Multi-room sync** | Native, browser-based, sub-100 ms | AirPlay 2 (Apple-only hardware) | Limited / per-app | None |
| **Retro formats** | SID, MOD, MIDI, tracker natively | None | None | Via plugins |
| **Network drives** | Direct FTP/SMB protocol access | Mount required | Mount required | Mount required |
| **Dependencies** | None (self-contained server) | Apple ecosystem | PostgreSQL / SQLite + ffmpeg | Windows |
| **Privacy** | 100% local, zero telemetry | Cloud-synced | Local but complex setup | Local |

**SoniqBoom is built for collectors.** If you have tens of thousands of tracks across FLAC libraries, Bandcamp purchases, C64 SID archives, and tracker modules from the demoscene era -- and you want them all searchable and playable from any device on your network -- SoniqBoom is designed for exactly that.

### Key differentiators

- **In-memory library** -- the entire catalog lives in RAM with pre-computed indexes. Browsing 170K tracks is as fast as browsing 100. No database server. No query latency.
- **Native retro format support** -- plays Commodore 64 SID files, Amiga MOD/XM/S3M/IT tracker modules, and MIDI files with SoundFont rendering. No plugins, no configuration -- just add the folders.
- **Direct network share access** -- connect FTP and SMB shares from the admin UI. SoniqBoom reads files over the protocol directly -- no OS-level mount points needed.
- **Browser-based multi-room sync** -- play the same track in lockstep across every browser on your LAN. No extra hardware, no apps, no cloud relay. Multiple independent rooms run simultaneously.
- **Zero infrastructure** -- single binary, no Docker, no database, no cloud account. Install and play.

---

## System Requirements

- **macOS** 12 Monterey or later (Apple Silicon or Intel) — primary, with `install.sh` automating setup
- **Linux** also supported via a [manual install path](#linux-manual)
- **Python** 3.11 or later (installed automatically by `install.sh` on macOS)
- **ffmpeg** (installed automatically by `install.sh` on macOS)
- **RAM**: ~500 MB base + ~1 MB per 10,000 tracks indexed
- **Disk**: ~200 MB plus cache space for transcoded files

---

## Installation

Clone the repository, run the install script once to set up Python, ffmpeg, and the optional audio renderers, then start the server with `run.sh`:

```bash
git clone https://github.com/SFCyris/SoniqBoom.git
cd SoniqBoom
bash install.sh        # Installs Python, ffmpeg, renderers, creates the venv
bash run.sh            # Starts the server in the background on port 8080
```

Open `http://localhost:8080` in any browser.

`run.sh` keeps SoniqBoom running in the background; `bash shutdown.sh` stops it and `bash restart.sh` restarts it.

### Linux (manual)

`install.sh` is macOS-only, but the runtime code already has Linux branches (`config.py`, `run.sh`, `shutdown.sh`). Install the prerequisites with your distro's package manager and the server runs the same way.

```bash
# Debian / Ubuntu — adjust package names for dnf, pacman, zypper, etc.
sudo apt install -y python3 python3-venv ffmpeg \
    fluid-synth libopenmpt0 libsidplayfp                      # last 3 are optional renderers

git clone https://github.com/SFCyris/SoniqBoom.git
cd SoniqBoom
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
soniqboom --port 8080                                          # or use bash run.sh
```

Differences from the macOS install:

- The macOS menu-bar icon is skipped on Linux. Manage the server with `Ctrl+C`, `bash shutdown.sh`, or your service manager (e.g. a systemd unit you write).
- Admin-panel password authentication uses `dscl` and is only available on macOS. On Linux the admin panel runs without OS-level auth, so SoniqBoom expects a trusted local network.
- The config file lives at `${XDG_DATA_HOME:-~/.local/share}/soniqboom/SoniqBoom.conf` instead of `~/Library/Application Support/SoniqBoom/SoniqBoom.conf`.

`run.sh`, `shutdown.sh`, and `restart.sh` work on Linux too — they detect the platform and pick the right data directory.

### Choosing a different port

The default port is `8080`. To change it, you have three options:

1. **Pass `--port` to `run.sh`** (one-off override):

   ```bash
   bash run.sh --port 9090
   bash restart.sh --port 9090   # restart.sh forwards args to run.sh
   ```

2. **Edit `SoniqBoom.conf`** (persistent default):

   Open the config file (path shown in the [Configuration](#configuration) section) and change the `server.port` value:

   ```jsonc
   {
     "server": { "host": "0.0.0.0", "port": 9090 }
   }
   ```

   Then restart the server (`bash restart.sh`).

3. **Set the `SONIQBOOM_PORT` environment variable** (per-launch override):

   ```bash
   SONIQBOOM_PORT=9090 bash run.sh
   ```

The CLI argument wins over the environment variable, and the environment variable wins over the config file.

---

## Getting Started

1. **Open the Admin panel** -- click the gear icon in the top-right corner.
2. **Add music folders** -- enter the path to your music library and click **Add**.
3. **Wait for indexing** -- SoniqBoom scans your folders and extracts metadata. Progress is shown in the admin panel. A 50K-track library takes about 2 minutes.
4. **Browse and play** -- close the admin panel. Your library is ready.

<img src="images/ui-main.png" alt="Main library view" width="700">

*Main library view -- browsing 172,000 tracks with sidebar navigation, folder tree, smart playlists, and player bar.*

---

## Features at a Glance

### Library Browser

Browse your collection by **Artist**, **Album Artist**, **Album**, **Genre**, or **Year**. Each view shows aggregated counts and supports inline filtering.

<img src="images/ui-artists.png" alt="Artists view" width="700">

*Artist browser with 5,435 artists -- click any name to see their tracks.*

### Instant Search

Type anywhere in the search bar for real-time results across your entire library. Results show track title, artist, album art, and duration as you type.

<img src="images/ui-search.png" alt="Search results" width="700">

*Instant search across 172K tracks -- results appear as you type.*

### Folder Tree

Navigate your music the way it's organized on disk. SoniqBoom shows your actual directory structure with expandable folders. Folder aliases let you rename long paths to friendly names.

<img src="images/ui-folders.png" alt="Folder tree" width="700">

*Folder tree with aliases -- "SID" maps to a C64Music archive with 60,000 files.*

### Smart Playlists

Built-in smart playlists that update automatically:

| Playlist | What it shows |
|----------|--------------|
| **History** | Tracks you've listened to, most recent first |
| **Most Played** | Your all-time favorites by play count |
| **Recently Added** | Newest additions to your library |
| **Top Rated** | Highest-rated tracks |
| **Unplayed** | Tracks you haven't listened to yet |
| **Duplicates** | Duplicate detection by audio fingerprint |

### Multi-room Sync

Play the same track in lockstep across every browser on your LAN -- like Sonos, but in your browser, with no extra hardware. Open `http://<server>:8080/multiroom` on any device to start.

- **Named rooms, independent of one another** -- create as many as you want ("Kitchen", "Office", "Living Room"). Each has its own master, queue, and current track. Pausing one room doesn't affect the others.
- **First-come master** -- the device that creates a room is the master. Other browsers join as listeners and play in sync. If the master leaves, any listener can take over.
- **Sub-100 ms cross-machine sync** -- a barrier-based scheduling protocol aligns track starts; steady-state drift correction keeps speakers in step over the whole track. Typical drift on a healthy 5 GHz Wi-Fi: under 50 ms.
- **Full master controls** -- search, pick from playlists or albums, queue tracks, shuffle, and cycling repeat (off / all / one). Every change replicates to listeners automatically.
- **Auto-reconnect** -- listeners resume on tab reload; if the master disconnects, the room offers a take-over banner.
- **No mobile app required** -- it's just a webpage. Works on any device with a modern browser, including phones and tablets.

**How to use it**

1. On the device you want as the source, open `http://<server>:8080/multiroom`.
2. Enter a label (e.g. "Kitchen speaker"), click **+ Create new room**, name it, and click **Create & become master**.
3. On any other device on the same network, open the same URL. The new room appears in the lobby list -- click it to join as a listener.
4. Pick a track, playlist, or album on the master. Listeners follow within ~3 seconds.

### Admin Panel

Manage your library, monitor index health, add or remove scan directories, configure renderers and SoundFonts, view logs, and control disk usage -- all from the browser.

<img src="images/ui-admin.png" alt="Admin panel" width="700">

*Admin panel showing library health (172K tracks indexed), music folders, and indexing controls.*

### Network Shares

Connect FTP and SMB shares directly from the admin panel -- no OS-level mount required. SoniqBoom reads files over the protocol, caches them locally for playback, and monitors connectivity with automatic reconnection.

---

## Supported Formats

### Standard Audio
MP3, FLAC, AAC/M4A, OGG Vorbis, Opus, WAV, AIFF, ALAC, WMA, APE, WavPack

### Retro / Niche *(via optional renderers)*
| Format | Renderer | Description |
|--------|----------|-------------|
| SID | sidplayfp | Commodore 64 SID chip emulation |
| MOD, XM, S3M, IT, MPTM | openmpt123 | Amiga/PC tracker modules |
| MIDI, MID, KAR | FluidSynth | MIDI synthesis with SoundFont (.sf2) instruments |

### Container Support
- **ZIP archives** -- SoniqBoom can scan inside ZIP files (configurable)
- **Embedded art** -- extracts cover art from ID3, Vorbis, FLAC, and MP4 tags

---

## Configuration

SoniqBoom stores its configuration at:

| Platform | Path |
|----------|------|
| macOS | `~/Library/Application Support/SoniqBoom/SoniqBoom.conf` |
| Linux | `~/.local/share/soniqboom/SoniqBoom.conf` |

The config file is JSON with sensible defaults. Most settings can be changed from the admin UI. Key options:

```jsonc
{
  "server": { "host": "0.0.0.0", "port": 8080 },
  "scan_zips": true,           // Scan inside ZIP archives
  "folder_aliases": {},        // Path-to-name display mappings
  "network_shares": {},        // FTP/SMB connections (managed via UI)
  "renderers": {
    "soundfont_path": "",      // Path to .sf2 for MIDI playback
    "sid_default_duration": 180 // SID track length in seconds
  }
}
```

Environment variables (`SONIQBOOM_HOST`, `SONIQBOOM_PORT`, etc.) override config file values.

---

## Architecture

```
Browser (any device)  <──HTTP──>  SoniqBoom Server  <──>  Music Files
                                       │                    ├── Local disk
                                       │                    ├── FTP shares
                                       │                    └── SMB shares
                                       │
                                  In-Memory Store
                                  ├── Track metadata (RAM)
                                  ├── Pre-computed indexes
                                  ├── AOF persistence (append-only file)
                                  └── Periodic snapshots (library.json)
```

- **FastAPI** + **uvicorn** -- async HTTP server
- **In-memory store** -- all metadata in RAM with hash-based indexes; no database
- **AOF + snapshot** -- Redis-style persistence: every mutation appends to an AOF file; periodic merges write full snapshots
- **On-the-fly transcoding** -- ffmpeg converts ALAC/AIFF/etc. to FLAC/MP3 for browser playback
- **Renderer pipeline** -- SID/MIDI/tracker files are rendered to WAV via subprocess, then served as standard audio
- **Multi-room sync** -- a per-room WebSocket fans out master state to every listener. Track changes use a barrier (preload + ack + scheduled `play_at`); steady-state uses periodic state broadcasts and per-listener drift correction (rate trim under 150 ms, hard seek beyond)

---

## Usage and Copyright

SoniqBoom is a tool for streaming music you already own or have the right to
use &mdash; for example, your own CD/vinyl rips, files you have purchased, or
freely-distributed material such as demoscene tracker modules and HVSC SID
archives. **Users are solely responsible** for ensuring that their use of the
software, including the music they index and stream through it, complies with
applicable copyright and licensing law in their jurisdiction.

SoniqBoom is intended for personal libraries on private networks. The project
maintainers do not host, distribute, or facilitate access to copyrighted
material.

---

## License

SoniqBoom is licensed under the **GNU Affero General Public License v3.0 or
later (AGPL-3.0-or-later)**. See [LICENSE](LICENSE) for the full text.

In short:

- You may run, study, modify, and redistribute SoniqBoom freely.
- If you distribute a modified version &mdash; **including running it as a
  network service that others interact with** &mdash; you must make the
  corresponding source of your modifications available under the same license.
- Third-party components retain their original licenses; see
  [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md).

Commercial licensing (for use cases that cannot comply with AGPL-3.0&rsquo;s
network-use copyleft, such as embedding SoniqBoom inside a closed-source
product or hosted commercial service) is available from the copyright holder
on request &mdash; contact <scyris@outlook.com>.

**Project home:** <https://github.com/SFCyris/SoniqBoom>
&middot; **Issues:** <https://github.com/SFCyris/SoniqBoom/issues>

Copyright &copy; 2026 S.F. Cyris. All rights reserved.

---

<p align="center"><em>Built by S.F. Cyris</em></p>
