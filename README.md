<p align="center">
  <img src="images/logo-banner.png" alt="SoniqBoom" width="600">
</p>
<p align="center"><strong>Self-hosted music server for your personal library</strong></p>
<p align="center">
  Streams FLAC, ALAC, MP3, Opus, plus SID, MIDI, and 20+ tracker formats &mdash; from any device on your network.
</p>
<p align="center">
  Self-hosted &bull; Browser-based &bull; AGPL-3.0 &bull; Zero cloud dependencies
</p>

<p align="center">
  <a href="https://github.com/SFCyris/SoniqBoom/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg" alt="License: AGPL-3.0-or-later"></a>
  <a href="https://github.com/SFCyris/SoniqBoom/releases"><img src="https://img.shields.io/github/v/release/SFCyris/SoniqBoom?include_prereleases&sort=semver" alt="Latest release"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
</p>

---

## What it is

SoniqBoom is a single-binary music server you run on your own machine. Point it
at your music folders, open `http://localhost:8080` in any browser, and your
library is ready &mdash; no cloud account, no database server, no app per
device.

The whole catalog lives in RAM with pre-computed indexes, so browsing a
170,000-track library is as fast as browsing 100. Retro formats (SID, MOD, XM,
S3M, IT, MIDI, etc.) are rendered on the fly via optional helper tools.

<img src="images/ui-main.png" alt="Main library view" width="700">

---

## Highlights

- **In-memory library** &mdash; instant browse and search at any scale.
- **Native retro format support** &mdash; SID, tracker modules, MIDI with SoundFonts.
- **Multi-room sync** &mdash; play the same track in lockstep across every browser on your LAN.
- **Cast / AirPlay / DLNA** &mdash; send audio to Chromecast, Apple TV, HomePod, or any UPnP receiver.
- **OpenSubsonic API** &mdash; compatible with Subsonic / OpenSubsonic apps (Amperfy, Symfonium, DSub, etc.).
- **Direct network shares** &mdash; FTP, SMB, and WebDAV connected from the admin UI, no OS mount required.
- **Privacy first** &mdash; 100% local, zero telemetry.

---

## Install

**macOS** (one-shot install + start):

```bash
git clone https://github.com/SFCyris/SoniqBoom.git
cd SoniqBoom
bash install.sh        # installs Python, ffmpeg, optional renderers
bash run.sh            # starts the server on port 8080
```

Then open `http://localhost:8080` in any browser.

**Linux** &mdash; install the prerequisites with your package manager, then run
the same `run.sh`:

```bash
sudo apt install -y python3 python3-venv ffmpeg \
    fluid-synth libopenmpt0 libsidplayfp   # last 3 are optional renderers
git clone https://github.com/SFCyris/SoniqBoom.git
cd SoniqBoom
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
bash run.sh
```

`bash shutdown.sh` stops it; `bash restart.sh` restarts it. To change the
default port: `bash run.sh --port 9090` (or set `SONIQBOOM_PORT=9090`).

---

## First-time setup

1. **Create the first admin account.** A fresh install has no users, and
   registration via the UI is locked until at least one admin exists, so the
   first one is bootstrapped from the CLI on the server host:

   ```bash
   .venv/bin/soniqboom-setadm -user alice -passwd 'changeme123'
   ```

   Username is 2&ndash;64 chars (letters, digits, `.`, `_`, `-`); password is
   at least 8 chars. New users default to the `admin` role. To rotate the
   password later, re-run the same command with a new `-passwd`; to add other
   users, use `-role admin|edit|readonly`, or invite them from the admin UI
   once you&rsquo;re signed in.

2. **Sign in.** Open `http://localhost:8080`, enter the credentials you just
   set, and you&rsquo;ll land in the library view.

3. Click the **gear icon** in the top-right to open the admin panel.
4. **Add music folders** &mdash; enter the path to your library and click **Add**.
5. Wait for the initial scan (about 2 minutes per 50,000 tracks).
6. Close the admin panel. Your library is ready.

---

## Supported formats

**Standard audio**: MP3, FLAC, AAC/M4A, ALAC, OGG Vorbis, Opus, WAV, AIFF, WMA,
APE, WavPack, DSF/DFF.

**Retro / niche** *(via optional renderers)*: SID (sidplayfp), MOD/XM/S3M/IT/MPTM
and other tracker modules (libopenmpt), MIDI/MID/KAR (FluidSynth), NSF/SPC/GBS/VGM
(libgme). ZIP archives are scanned inline.

---

## Configuration

| Platform | Path |
|----------|------|
| macOS | `~/Library/Application Support/SoniqBoom/SoniqBoom.conf` |
| Linux | `~/.local/share/soniqboom/SoniqBoom.conf` |

Most settings can be changed from the admin UI. Environment variables
(`SONIQBOOM_HOST`, `SONIQBOOM_PORT`, etc.) override config file values.

---

## License

SoniqBoom is licensed under the **GNU Affero General Public License v3.0 or
later (AGPL-3.0-or-later)**. See [LICENSE](LICENSE) for the full text.

In short: you may run, study, modify, and redistribute SoniqBoom freely. If you
distribute a modified version &mdash; including running it as a network service
that others interact with &mdash; you must make the corresponding source of your
modifications available under the same license.

Commercial licensing (for use cases that cannot comply with AGPL-3.0&rsquo;s
network-use copyleft) is available from the copyright holder on request
&mdash; contact <scyris@outlook.com>.

SoniqBoom is intended for streaming music you already own or have the right to
use. Users are solely responsible for ensuring their use complies with
applicable copyright law.

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
- [Mutagen](https://github.com/quodlibet/mutagen) by [Quod Libet](https://github.com/quodlibet) is licensed under [GPL-2.0](https://github.com/quodlibet/mutagen/blob/main/COPYING)
- [mido](https://github.com/mido/mido) by [mido](https://github.com/mido) is licensed under [MIT License](https://github.com/mido/mido/blob/main/LICENSE)
- [Pillow](https://github.com/python-pillow/Pillow) by [Python Pillow](https://github.com/python-pillow) is licensed under [HPND License](https://github.com/python-pillow/Pillow/blob/main/LICENSE)
- [cryptography](https://github.com/pyca/cryptography) by [PyCA](https://github.com/pyca) is licensed under [Apache-2.0 / BSD-3-Clause](https://github.com/pyca/cryptography/blob/main/LICENSE)
- [PyYAML](https://github.com/yaml/pyyaml) by [YAML](https://github.com/yaml) is licensed under [MIT License](https://github.com/yaml/pyyaml/blob/main/LICENSE)
- [python-dotenv](https://github.com/theskumar/python-dotenv) by [Saurabh Kumar](https://github.com/theskumar) is licensed under [BSD-3-Clause](https://github.com/theskumar/python-dotenv/blob/main/LICENSE)
- [defusedxml](https://github.com/tiran/defusedxml) by [Christian Heimes](https://github.com/tiran) is licensed under [PSF-2.0](https://github.com/tiran/defusedxml/blob/main/LICENSE)
- [smbprotocol](https://github.com/jborean93/smbprotocol) by [Jordan Borean](https://github.com/jborean93) is licensed under [MIT License](https://github.com/jborean93/smbprotocol/blob/master/LICENSE)
- [pyatv](https://github.com/postlund/pyatv) by [Pierre Ståhl](https://github.com/postlund) is licensed under [MIT License](https://github.com/postlund/pyatv/blob/master/LICENSE.md)
- [PyChromecast](https://github.com/home-assistant-libs/pychromecast) by [Home Assistant](https://github.com/home-assistant-libs) is licensed under [MIT License](https://github.com/home-assistant-libs/pychromecast/blob/master/LICENSE)
- [async-upnp-client](https://github.com/StevenLooman/async_upnp_client) by [Steven Looman](https://github.com/StevenLooman) is licensed under [MIT License](https://github.com/StevenLooman/async_upnp_client/blob/master/LICENSE)
- [rumps](https://github.com/jaredks/rumps) by [Jared Suttles](https://github.com/jaredks) is licensed under [BSD-3-Clause](https://github.com/jaredks/rumps/blob/master/LICENSE)
- [pyobjc](https://github.com/ronaldoussoren/pyobjc) by [Ronald Oussoren](https://github.com/ronaldoussoren) is licensed under [MIT License](https://github.com/ronaldoussoren/pyobjc/blob/master/License.txt)

External tools (invoked via subprocess &mdash; not linked into SoniqBoom):

- [FFmpeg](https://ffmpeg.org/) is licensed under [LGPL-2.1 / GPL-2.0](https://ffmpeg.org/legal.html) (depending on build configuration)
- [libsidplayfp](https://github.com/libsidplayfp/libsidplayfp) is licensed under [GPL-2.0](https://github.com/libsidplayfp/libsidplayfp/blob/master/COPYING)
- [FluidSynth](https://github.com/FluidSynth/fluidsynth) is licensed under [LGPL-2.1](https://github.com/FluidSynth/fluidsynth/blob/master/LICENSE)
- [libopenmpt](https://lib.openmpt.org/libopenmpt/) by [OpenMPT](https://openmpt.org/) is licensed under [BSD-3-Clause](https://lib.openmpt.org/libopenmpt/license/)
- [Game_Music_Emu (libgme)](https://github.com/libgme/game-music-emu) is licensed under [LGPL-2.1](https://github.com/libgme/game-music-emu/blob/master/license.txt)

Data / assets:

- [GeneralUser GS SoundFont](https://schristiancollins.com/generaluser.php) by S. Christian Collins (Free, attribution required) &mdash; default SoundFont for MIDI synthesis
- [HVSC (High Voltage SID Collection)](https://www.hvsc.c64.org/) (Free, archive of SID music) &mdash; Songlengths.md5 and STIL metadata used for SID playback

The full per-component license texts are recorded in
[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md).

**SoniqBoom license:** [AGPL-3.0-or-later](LICENSE)

---

**Project home:** <https://github.com/SFCyris/SoniqBoom>
&middot; **Issues:** <https://github.com/SFCyris/SoniqBoom/issues>

Copyright &copy; 2026 S.F. Cyris.

<p align="center"><em>Built by S.F. Cyris</em></p>
