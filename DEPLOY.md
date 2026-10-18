<!-- SPDX-FileCopyrightText: 2026 S.F. Cyris · SPDX-License-Identifier: AGPL-3.0-or-later -->
# Deploying SoniqBoom with Docker

This is the copy-paste guide. If you've never used Docker before, follow it top to
bottom — every step is a command you paste into a terminal. The **same commands work
on both Intel/AMD (x86_64) and 64-bit ARM (Apple Silicon, Raspberry Pi with a 64-bit OS,
ARM servers)** — Docker automatically builds the version that matches your machine.

> **Which guide is this?** This is the **Docker** path — the only supported way to run
> SoniqBoom on **Windows**, and the easiest on a Raspberry Pi or home server. On **Mac or
> Linux** and prefer a non-Docker install? See the [README](README.md) instead.

One container plays every format SoniqBoom supports — chiptune, tracker, SID, MIDI,
lossless and DSD — with ffmpeg and the retro-format players baked in.

---

> **Never used a terminal?** A *terminal* is a text window where you type commands.
> **Mac:** open the **Terminal** app (press Cmd-Space, type "Terminal"). **Windows:** after
> installing Docker Desktop, open **PowerShell** (Start menu → type "PowerShell").
> **Linux:** you already know where it is. Paste each command and press Enter.

## Step 0 — Install Docker (once)

| Your machine | What to install |
|---|---|
| **Mac** (Intel or Apple Silicon) | [Docker Desktop](https://www.docker.com/products/docker-desktop/) — download, open it, done. |
| **Windows** | [Docker Desktop](https://www.docker.com/products/docker-desktop/) (enable WSL 2 when it asks). |
| **Linux** | `curl -fsSL https://get.docker.com \| sh` then `sudo usermod -aG docker $USER` and log out/in. |

Check it works — this should print a version number:

```bash
docker --version
```

---

## Step 1 — Get SoniqBoom and start it (4 steps)

> **Skip the build?** A prebuilt multi-arch image (Intel/AMD + 64-bit ARM) is published at
> `ghcr.io/sfcyris/soniqboom:latest`; each release is also tagged with its version
> (for example `:1.14.0`, and `:1.14` for the newest 1.14.x). In `docker-compose.yml`,
> comment out `build: .` and uncomment the `image:` line (there's a comment marking the
> spot), then use `docker compose pull && docker compose up -d` at step 3 — it starts in
> seconds instead of compiling the players. Building from source (below) is only needed
> to modify SoniqBoom.

```bash
# 1. Download the code
git clone https://github.com/SFCyris/SoniqBoom.git
cd SoniqBoom

# 2. Put your music where the server can see it — either a folder you copy/move it into:
mkdir -p music
#    …or, on Mac/Linux, instead of that folder, a link to the folder your music is in:
#    ln -s /path/to/your/music music

# 3. Build and start (first run takes a few minutes — it compiles the
#    retro-format players; after that, starts in seconds)
docker compose up -d

# 4. Create your login — replace 'me' and the password with your own (keep the quotes)
docker compose exec soniqboom soniqboom-setadm -user me -passwd 'change-this-password'
#    You'll see:  Created user 'me' …  ·  Notified the running server (port 8080) — the change is live now
```

That's it — the server is running. Go to **Step 2** to open it.

> **Do step 4 right away** — create your login before anyone else on your network opens
> SoniqBoom. Once it exists, anyone who can reach SoniqBoom can create a Read-only account
> on the sign-in page; to turn that off, untick gear → **Users → Sign-up & passwords →
> Allow self-registration**.

> **Is it running?** `docker compose ps` should list `soniqboom` as **Up**, and as
> **healthy** once it has finished starting. To watch it start, run
> `docker compose logs -f` (Ctrl-C stops watching): it shows the startup banner with the
> version, the services and whether each is on, and the Config and Data paths.
> SoniqBoom's own messages, warnings and errors go to `/data/log/soniqboom.log` — follow
> them with `docker compose exec soniqboom tail -f /data/log/soniqboom.log`, or open
> gear → **Log**.

> **No `git`?** Download the ZIP instead: grab
> <https://github.com/SFCyris/SoniqBoom/archive/refs/heads/main.zip>, unzip it, and
> `cd` into the folder before running `docker compose up -d`.

---

## Step 2 — Open it and add your music

1. On **the same computer**, open a browser to **<http://localhost:8080>**.
2. Sign in with the username and password you made in Step 1.
3. Click the **gear icon** (top-right) → **Library** → **Music Folders** → type
   **`/music`** → **+ Add**.
4. Wait for the scan (about 2 minutes per 50,000 tracks). Done — press play.

The **📖** button in the header opens the user guide (also at
<http://localhost:8080/manual/>).

---

## Step 3 — Connect from your phone or another computer

The server listens on port **8080** on the machine you started it on. From any other
device on the **same network**, open `http://<that-machine's-IP>:8080`.

Find the machine's IP address:

| On the server machine | Command |
|---|---|
| **Mac** | `ipconfig getifaddr en0`  (try `en1` on Wi-Fi if blank) |
| **Linux** | `hostname -I \| awk '{print $1}'` |
| **Windows** | `ipconfig`  → look for "IPv4 Address" |

Say it prints `192.168.1.50` — then on your phone's browser open
**`http://192.168.1.50:8080`** and sign in. (SoniqBoom auto-switches to a touch layout
on phones. For the full layout, tap **Desktop site** at the top of the phone layout or
open `http://192.168.1.50:8080/?ui=desktop`; `http://192.168.1.50:8080/?ui=mobile`
switches back. iPads with Safari's default settings get the full layout.)

Subsonic apps (Amperfy, Symfonium, DSub, …) connect to the same address,
`http://192.168.1.50:8080`, with your SoniqBoom username and password. The multi-room
controller is at `http://192.168.1.50:8080/multiroom`.

> **Can't connect from another device?** The server machine's firewall is probably
> blocking port 8080. Allow it, or temporarily turn the firewall off to test. For access
> over the **internet** (not just your home network), see *Automatic HTTPS* below — never
> expose port 8080 directly to the internet without TLS.

---

## Does my CPU (ARM vs AMD/Intel) matter?

**No — the commands above are identical on both.** `docker compose up -d` builds the
image for whatever machine you run it on. To confirm which you have:

```bash
uname -m
#  x86_64  → Intel/AMD (most cloud servers, most Windows/Linux desktops)
#  arm64 / aarch64 → Apple Silicon Mac, Raspberry Pi 4/5, ARM cloud servers
```

(`uname` is a Mac/Linux command. On **Windows**, run `docker version` and read the
**OS/Arch** line, or check Docker Desktop → Settings.)

The image is for **64-bit** systems. On a Raspberry Pi, check the operating system as
well: `dpkg --print-architecture` must print `arm64`. If it prints `armhf`, the Pi runs a
32-bit OS (even when `uname -m` prints `aarch64`): install the 64-bit Raspberry Pi OS.

**Build on the machine you'll run it on** and you never have to think about architecture.
Several players are compiled from source during the build — the C64 SID chain
(`sidplayfp` with reSIDfp), `libgme`, `psgplay`, the Amiga `uade123`, the Atari-ST
`sc68` and the bundled HivelyTracker and ST-Sound engines (`hvl2wav`, `ym2wav`) — and
compiling them for a *different* CPU than your own (cross-building) can fail.
The build then stops and lists the missing players. Build on a machine of the target CPU;
or pass `--build-arg SLIM=1` to leave out `uade123` and `sc68` (AHX, the Amiga "exotica"
formats and `.sc68` disks then don't play), or `--build-arg ALLOW_MISSING_RENDERERS=1` to
finish the build without whichever players are missing.

### Plain `docker run` (without Compose)

```bash
# Use the prebuilt image (no local build; Docker fetches the right CPU automatically):
IMAGE=ghcr.io/sfcyris/soniqboom:latest
# …or build it yourself instead:  docker build -t soniqboom . && IMAGE=soniqboom

docker run -d --name soniqboom \
  -p 8080:8080 \
  -v /path/to/your/music:/music:ro \
  -v soniqboom-data:/data \
  --restart unless-stopped \
  "$IMAGE"

docker exec soniqboom soniqboom-setadm -user me -passwd 'change-this-password'
```

The `soniqboom-data` volume keeps everything — see [What goes where](#what-goes-where).

### One image for both CPU types (advanced, for sharing/registries)

If you maintain a container registry and want a single tag that runs on both Intel and
ARM, build each CPU's image on a native machine of that CPU (or a CI runner), then join
them into one multi-arch tag:

```bash
# on an Intel/AMD machine:
docker buildx build --platform linux/amd64 -t <your-registry>/soniqboom:amd64 --push .
# on a 64-bit ARM machine:
docker buildx build --platform linux/arm64 -t <your-registry>/soniqboom:arm64 --push .
# then, on either:
docker buildx imagetools create -t <your-registry>/soniqboom:latest \
  <your-registry>/soniqboom:amd64 <your-registry>/soniqboom:arm64
```

A single `docker buildx build --platform linux/amd64,linux/arm64` on one machine
cross-builds the other CPU, which can stop at the missing-players check described above.

Anyone can then `docker pull <your-registry>/soniqboom:latest` and Docker fetches the
right CPU variant automatically.

The image contains GPL-licensed programs. If you publish it, also make their source code
available: `scripts/collect_third_party_sources.sh <dir>` downloads the upstream sources
of the programs the image builds or downloads (and of the in-browser SID engine), at the
pinned versions, and `scripts/collect_debian_sources.sh` downloads the source packages of
the Debian packages inside it. List those packages first, then run the script in a
`debian:trixie` container:

```bash
docker run --rm --entrypoint dpkg-query <image> \
  -W -f '${Package}\t${Version}\t${source:Package}\t${source:Version}\n' > packages.txt
docker run --rm -v "$PWD:/w" -w /w debian:trixie \
  bash scripts/collect_debian_sources.sh debian-sources packages.txt
```

See [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md#source-code-for-distributed-binaries).

---

## What goes where

| Path | Purpose | How it's mounted |
|------|---------|------------------|
| `/music` | Your audio library | read-only (`-v /your/music:/music:ro`) — the server can't change your files |
| `/data`  | The library index, **user accounts**, settings, SoundFonts you add, saved share passwords and last.fm / ListenBrainz logins, logs, the cover-art and render caches, the cache of files played from network shares, and downloaded metadata indexes | a named volume (`soniqboom-data`) — **this is what you back up** |

Editing tags in the browser (Track Info → **✏️ Edit tags**) and **Save fetched lyrics to
files** write into your music files, so they need the music mounted without `:ro`.

Paths you type into SoniqBoom are paths inside the container — music folders under
`/music`, and the HVSC `DOCUMENTS` folder (gear → **Renderers → HVSC SID database**) too,
for example `/music/C64Music/DOCUMENTS`.

Inside `/data`:

- `/data/soniqboom` holds the settings file `SoniqBoom.conf` (network shares, folder
  aliases, services, sign-up and password rules, renderer, SID and HVSC settings, cache
  sizes), `prefs.json` and the cover-art and render caches (gear → **System → Disk Usage**
  counts these caches under **Library Data**; the render cache grows up to the size set
  under gear → **Renderers → Transcode & render cache**).
- `/data/soniqboom/soundfonts` holds the SoundFonts you add under gear → **Renderers →
  Soundfonts**. While it holds none, the General-MIDI SoundFont FluidR3_GM is added there
  when the server starts, so MIDI plays out of the box.
- `/data/credential.key` holds the key that saved network-share passwords and last.fm /
  ListenBrainz logins are encrypted with. It is created when it is first needed; keep it
  with the rest of `/data`.

All of it is kept when the container is re-created — after an update, or `docker compose
down` followed by `up`. The startup banner (`docker compose logs`) shows where the
settings file is (**Config:**).

A container from an image before 1.14.0 kept its settings, and the SoundFonts added in it,
inside the container, so after the first update to 1.14.0 or later, settings (shares,
services, sign-up and password rules, renderer settings, the *Re-apply automatically*
switches under gear → **Metadata**) need to be set once more; from then on they're kept.
Its SoundFonts go with the old container: to keep them, copy them out before you update
(`docker cp soniqboom:/app/soundfonts ./old-soundfonts`), then add them again under
gear → **Renderers → Soundfonts** (**↑ Upload .sf2/.sf3**). The new container can't read
that container's last.fm / ListenBrainz logins either: enter them again under gear →
**System → My Account** (admin accounts only).

---

## Backups & recovery

Back up the `soniqboom-data` volume (`/data`): it holds the library index, the user
accounts (`users.json` with the `secret.key` it needs), the Subsonic apps' stars,
bookmarks and saved play queues (`subsonic_state.json`), the settings file and SoundFonts
(`/data/soniqboom`), the key for saved passwords and logins (`credential.key`) and the
logs. Caches you can leave out of the backup: `/data/cache/remote`, `/data/zip-extracts`
and `/data/soniqboom/cache`. Backups and restores
**never touch your music files** — all of this is just the index and settings.

If `users.json` is damaged, it is moved aside as `users.json.corrupt-<time>` and SoniqBoom
uses its copy `users.json.bak` (a damaged copy is moved aside as
`users.json.bak.corrupt-<time>`). If no account can be loaded although accounts existed
before — both files are damaged or gone, or `users.json` can't be read at all (for example a
permission error; the copy isn't used then) — sign-in stays locked for every account (the
DLNA Media Server, Beta, doesn't list or play anything either) until you put a good
`users.json` back (the running server picks it up within a few seconds; then click
**re-check** on the sign-in screen) or create an admin account — this starts a new account
list (the accounts in the unreadable file are not brought back):

```bash
docker compose exec soniqboom soniqboom-setadm -user me -passwd 'change-this-password' -role admin
```

**Portable export.** For a self-contained copy of the library index — tracks, playlists,
ratings, play history and the music folder list — use gear → **Backup → Export .sbz**.
With the server stopped, `docker compose run --rm -v "${PWD}:/backup" soniqboom soniqboom export /backup/soniqboom.sbz`
writes one into the SoniqBoom folder instead. User accounts, the settings file, the Subsonic
apps' stars, bookmarks and play queues, and the radio Favorites are not in the `.sbz`.

To restore one, copy the `.sbz` into the SoniqBoom folder (the one with
`docker-compose.yml`) and run these commands there (for the HTTPS setup, add
the same `-p` and `-f` you use for that setup — e.g. `-p soniqboom -f deploy/docker-compose.https.yml` — to each command). The import replaces
the library index; changes made since the export are lost. `soniqboom import` refuses to
run while the server is using `/data`, so stop it first:

```bash
docker compose stop
docker compose run --rm -v "${PWD}:/backup:ro" soniqboom soniqboom import /backup/soniqboom-2026-10-01.sbz
docker compose start
```

Use your file's name in the second command. With plain `docker run`, from the folder
holding the `.sbz` file (again with your file's name; with an image you built yourself, put
its name, e.g. `soniqboom`, in place of `ghcr.io/sfcyris/soniqboom:latest`):

```bash
docker stop soniqboom
docker run --rm -v soniqboom-data:/data -v "$PWD":/backup:ro ghcr.io/sfcyris/soniqboom:latest soniqboom import /backup/soniqboom-2026-10-01.sbz
docker start soniqboom
```

You can also restore from the browser: **↑ Import .sbz** on the Backup tab replaces the
library index of the running server the same way and reports *Import complete* with the
number of tracks restored.

**On-disk snapshots.** The library index is stored as `library.json` under
`/data`, alongside automatic copies you can fall back to:

| File | What it is |
|------|-----------|
| `library.json` | the live index |
| `library.json.bak` | the previous write (rotated on each save) |
| `library.json.prev` | the last snapshot that **loaded cleanly on startup** — a stable "known good" copy |
| `library.aof` | recent changes not yet folded into `library.json` (replayed on the next start) |

**Restore by hand** — only if the index won't load (e.g. after a disk problem).
In the SoniqBoom folder, stop the server (`docker compose stop`), open a shell on the data
volume with `docker compose run --rm --entrypoint sh soniqboom` (for the HTTPS setup, add
the same `-p` and `-f` to each command), then in `/data` (`cd /data`):

```bash
cp library.json.prev library.json   # roll back to the last known-good snapshot
rm -f browse_cache.pickle            # the folder views' cache of the newer index (rebuilt as needed)
rm -f library.aof                    # optional: drop unmerged changes if the journal is suspect
```

Type `exit`, then start the server again (`docker compose start`) — it rebuilds its
in-memory indexes from `library.json` on boot. A fresh start also refreshes
`library.json.prev` once the restored snapshot loads cleanly.

---

## Updating to a newer version

Use the block for the way you run SoniqBoom — run only that one. Updating from an image
before 1.14.0? First copy out the SoundFonts you added in it
(`docker cp soniqboom:/app/soundfonts ./old-soundfonts`; see [What goes where](#what-goes-where)).

**Built from source** (the default `docker-compose.yml`), in the SoniqBoom folder:

```bash
git pull
docker compose up -d --build      # rebuild + restart; /data (and your library index) is kept
```

Installed from the ZIP? Download the new ZIP, unzip it somewhere else, copy everything in
its `SoniqBoom-main` folder into your SoniqBoom folder (replacing the files there), make
your `docker-compose.yml` changes again, then run `docker compose up -d --build` in your
SoniqBoom folder.

**Prebuilt image** (the `image:` line in `docker-compose.yml`):

```bash
docker compose pull
docker compose up -d
```

**HTTPS setup** ([Automatic HTTPS](#automatic-https-access-over-the-internet)), with the
`-p` and `-f` you start it with:

```bash
git pull
docker compose -p soniqboom -f deploy/docker-compose.https.yml up -d --build
```

**HTTPS setup, prebuilt image.** As shipped, `deploy/docker-compose.https.yml` builds
SoniqBoom from source (`build: ..`, with the `image:` line commented out), so `pull` fetches
no SoniqBoom image. To use the prebuilt image, comment out `build: ..` and uncomment the
`image:` line below it in that file once; then:

```bash
docker compose -p soniqboom -f deploy/docker-compose.https.yml pull
docker compose -p soniqboom -f deploy/docker-compose.https.yml up -d
```

**Plain `docker run`:**

```bash
docker pull ghcr.io/sfcyris/soniqboom:latest
docker stop soniqboom
docker rm soniqboom
```

Then run the same `docker run` command as before. (With an image you built yourself, run
`git pull` and `docker build -t soniqboom .` in place of `docker pull`.)

Everything on `/data` is kept — the library index (so there is no full rescan), user
accounts, settings and SoundFonts. After the first update from an image before 1.14.0, set
the settings and last.fm / ListenBrainz logins once more (see [What goes where](#what-goes-where)).
Browser sign-ins are kept on `/data` too (`sessions.json`), so browsers stay signed in across
a restart; Subsonic apps sign in with every request. The version you're running is shown in
gear → **System → About**.

---

## Automatic HTTPS (access over the internet)

The repo ships a Caddy setup that gets a real Let's Encrypt certificate automatically:

1. Point your domain's DNS at the host and open ports **80** and **443**.
2. Edit [`deploy/Caddyfile`](deploy/Caddyfile) — replace `music.example.com` with your domain.
3. If the Step 1 setup is running, stop it with `docker compose down` (your data volume,
   with your settings, is kept). Then launch:

   ```bash
   docker compose -p soniqboom -f deploy/docker-compose.https.yml up -d
   ```

   `-p` must be the Step 1 setup's project name, so this setup uses the same data
   volume: the part before `_soniqboom-data` in `docker volume ls` (`soniqboom` for a git
   clone, `soniqboom-main` for a folder unzipped from the ZIP). Use the same `-p` and `-f`
   with every `docker compose` command for this setup. An HTTPS setup first started
   without `-p` uses its own volume, `deploy_soniqboom-data`: keep leaving `-p` out for
   that one.
4. On a new install, create your login now:

   ```bash
   docker compose -p soniqboom -f deploy/docker-compose.https.yml exec soniqboom \
     soniqboom-setadm -user me -passwd 'change-this-password'
   ```

5. Open `https://your-domain`. The certificate is issued on the first request.

Here SoniqBoom is **not** published on the host — only Caddy is — so the single way in is
over TLS. (Prefer Traefik or nginx-proxy-manager? Point any reverse proxy at the
`soniqboom` container's port `8080`.)

### Behind nginx

Retro formats (Amiga modules, SID, MIDI, …) are rendered on first play, and audio is
streamed while it renders. With nginx in front, turn off response buffering for the
audio streams (web player, Subsonic apps and internet radio), allow long reads, keep
the port in the `Host` header, and pass WebSocket upgrades through:

```nginx
# in the http { } block
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

# in the server { } block
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Host $http_host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}

location ~ ^/(api/stream/|api/stations/relay/|rest/(stream|download|radioStream|getTranscodeStream)) {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Host $http_host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```

Keep the `Host` (or `X-Forwarded-Host`) header as above: SoniqBoom compares it with the
browser's `Origin` to refuse changes made from other sites' pages. Request bodies are
limited to 16 MB (sign-in and the other `/api/auth/` requests to 64 KB); **Import .sbz** and
SoundFont uploads may be up to 4 GB, and with *Play SID in browser* on, a browser sends the
SID audio it rendered (up to 64 MB) to the server's render cache — let them through the
proxy (nginx: `client_max_body_size`).

Audio that is still rendering or being converted to another format is also sent with
`X-Accel-Buffering: no`, so nginx streams it even where the second location is missing.

HTTP status 499 lines for `/api/tracks/<id>/waveform` in the nginx log are normal: the
player cancels that request when you skip to another track.

---

## Configuration

| Variable | Default | Notes |
|----------|---------|-------|
| `SONIQBOOM_DATA_DIR` | `/data` | Where the library index, user accounts, logs, the network-share and archive caches and downloaded metadata indexes are written (set in the image). |
| `XDG_DATA_HOME` | `/data` | SoniqBoom keeps `SoniqBoom.conf`, `prefs.json` and the cover-art and render caches in `$XDG_DATA_HOME/soniqboom` (set in the image). |
| `SONIQBOOM_CREDENTIAL_KEY_FILE` | `/data/credential.key` | The file holding the key saved network-share passwords and last.fm / ListenBrainz logins are encrypted with (set in the image). |
| `SONIQBOOM_CONF` | not set | Full path of the settings file, e.g. `/data/SoniqBoom.conf` — wins over `XDG_DATA_HOME` for that one file. |
| `TZ` | container default | Set e.g. `Europe/Berlin` for correct log/scan timestamps. |
| `SONIQBOOM_SUBSONIC_CORS` | `1` | Set to `0` to stop web-based Subsonic players hosted on other sites from calling `/rest`. Native apps are not affected. |
| `SONIQBOOM_CORS_ORIGINS` | not set | Comma-separated list of web origins (e.g. `https://dashboard.example.com`) allowed to call `/api` from a browser, with the browser's SoniqBoom sign-in. By default only SoniqBoom's own pages can; a page of any other origin — another port of the same host included — can't change anything through `/api` or open its live connections (403). |
| `SONIQBOOM_FRAME_ANCESTORS` | not set | Space- or comma-separated list of web origins whose pages may show SoniqBoom's web UI in a frame (by default only SoniqBoom's own pages can). |
| `SONIQBOOM_LASTFM_API_KEY`, `SONIQBOOM_LASTFM_API_SECRET` | not set | Your last.fm API account's key and shared secret. last.fm scrobbling only works when both are set; ListenBrainz needs neither. The last.fm session key entered under gear → **System → My Account** must be one issued for this same API key (last.fm's `auth.getSession`). |
| `SONIQBOOM_SCAN_WORKERS` | half the usable CPUs (2–16) | Number of parallel scan processes (1–64). |
| `SONIQBOOM_RENDER_SLOTS` | half the usable CPUs (at least 2) | Number of retro-format renders and cached ffmpeg conversions that can run at once (2–64). |
| `SONIQBOOM_PLAYBACK_IO_THREADS` | 4 × the usable CPUs (32–128) | Number of threads that read the files being played (4–256). |
| `SONIQBOOM_OFFLINE` | `0` | Set to `1` to make no requests to hosts on the internet — at start-up, in the background or when a feature is used: the radio directory, station streams and logos, lyrics, artist info and photos, radio covers, Demozoo, the song database, the Modland index and catalogue, online collections (HVSC, Aminet, scene.org), game-title lists, last.fm / ListenBrainz (scrobbles stay queued until it is off again), and SoundFont and ffmpeg downloads. Those features then answer as if the service were unreachable. Hosts on the local network (`localhost`, private and link-local addresses, `.local` names), such as a NAS holding your network shares, are still reached. Same as `"offline": true` in `SoniqBoom.conf`. |

"Usable CPUs" respects a container's CPU limit (`--cpuset-cpus`, `--cpus`, an LXC core limit, a systemd `CPUQuota=`).

Some `SoniqBoom.conf` settings can also be given as environment variables, which win over
the file: `SONIQBOOM_CONVERSION_CACHE_DIR`, `SONIQBOOM_ART_CACHE_DIR`,
`SONIQBOOM_SOUNDFONTS_DIR` (`/data/soniqboom/soundfonts` in the image; while the folder
holds no SoundFont, the server links in the one named by `SONIQBOOM_DEFAULT_SOUNDFONT`,
FluidR3_GM in the image, when it starts), `SONIQBOOM_DISCOGS_TOKEN` (a Discogs
personal access token, used to find covers for the song a radio station is playing) and
`SONIQBOOM_RADIO_ART_LOOKUP` (`false` turns that cover lookup off, so the station's
artist and title aren't sent to MusicBrainz or Discogs). Services (web UI, the
`soniqboom services` command or the file), network shares and the sign-up and password
rules (web UI or the file) can't be set by environment variables.

To change the **host** port, edit the mapping in `docker-compose.yml` (`"9000:8080"`
publishes on 9000) rather than the in-container port.

---

## Handy commands

```bash
docker compose logs -f          # watch the server's console output (startup banner)
docker compose exec soniqboom tail -f /data/log/soniqboom.log
                                # follow SoniqBoom's own log (also gear → Log)
docker compose exec soniqboom curl -s http://127.0.0.1:8080/api/health
                                # is it answering, which version, which services are on
docker compose restart          # restart it
docker compose stop             # stop it; docker compose start starts it again
docker compose down             # stop and remove the container (keeps your data volume)
docker compose exec soniqboom soniqboom-setadm -user bob -passwd 'pw-1234' -role readonly
                                # add another user (roles: admin, edit, readonly;
                                # new users default to admin)
docker compose exec soniqboom soniqboom-setadm -user bob -passwd 'new-password'
                                # reset a password
docker compose exec soniqboom soniqboom-setadm -user bob -disable
                                # block sign-in (-enable lets them in again)
docker compose exec soniqboom soniqboom services list
                                # optional services (subsonic, multiroom, cast (Beta),
                                # dlna_server (Beta)); enable / disable <name>, then restart
```

`soniqboom-setadm` flags are spelled in full (`-user`, `-passwd`, `-role`,
`-display-name`, `-disable`, `-enable`). With **Require strong passwords** on (gear →
**Users**), passwords set here follow that rule too.

After 15 wrong passwords at the sign-in page within 15 minutes, that account can't sign
in for 15 minutes, even with the right password; `docker compose restart` ends that early.

---

## Notes

- **Base image.** Debian 13 ("trixie") with Python 3.12; the container runs `soniqboom`
  under `tini`, and Docker's health check asks `/api/health` every 30 seconds.
- **Renderers degrade gracefully.** If a format won't play, the player names the package
  it needs; everything else keeps working. The image includes every player: ffmpeg,
  FluidSynth and adplay from Debian; libopenmpt / openmpt123 0.8.9, sidplayfp 3.1.0
  (with the reSIDfp engine), libgme 0.6.5, psgplay, sc68 and `uade123` (Amiga AHX and
  the ~175 Amiga "exotica" players) compiled from source; the prebuilt zxtune123; and
  the bundled ST-Sound (`.ym`) and HivelyTracker (`.hvl`) engines.
  The image build stops if any of them is missing (unless built with
  `--build-arg ALLOW_MISSING_RENDERERS=1`). gear → **Renderers → Renderer Status** shows
  each one and the version it reports.
- **LHA archives.** The image reads `.lha` / `.lzh` archives with its built-in readers —
  header levels 0–3 and the methods `-lh0-`, `-lh1-`, `-lh4-` … `-lh7-`, `-lz4-`, `-lz5-`
  and `-lzs-` — and has no `lha` (lhasa) program, so an archive packed with another
  method (`-lh2-`, `-lh3-`, `-pm0-` … `-pm2-`, `-lk7-`) isn't opened.
- **Face The Music.** `.ftm` modules (and `FTM.title` names in archives) play through the
  image's libopenmpt 0.8.9; a module saved without its samples doesn't play.
- **Slim image.** `docker build --build-arg SLIM=1 -t soniqboom .` leaves out `uade123`
  and `sc68` (AHX, the Amiga "exotica" family and native `.sc68` disks); every other
  format still plays.
- **Memory** scales with library size (the whole index is held in RAM). Budget accordingly
  for six-figure collections.
- **Modland catalogue** (optional, off by default — gear → **Collections**): the container
  reaches `ftp.modland.com` over HTTPS for the index and over FTP (port 21, passive mode)
  for index updates and for the tunes it downloads — when you play a tune or open its
  Track Info (for most formats), and, with *For tunes shown in lists*, the SC68, SNDH and
  console chiptune files (NSF, NSFe, GBS, HES, KSS, AY, SAP) shown in a list. A firewall in
  front of it must allow both. With the catalogue on, plan for about 1 GB more memory.
- **Online collections** (optional, each off by default — gear → **Collections**), over
  HTTPS: `6581.nl` (HVSC: its song-length list and STIL, then the `.sid` files), `aminet.net`
  (Aminet: its index and category names, then the uploads with their readme) and
  `archive.scene.org` (scene.org: its folder listings, then the files). Each reads its index
  when you switch it on, on the schedule you pick (daily, weekly, every 2 weeks or monthly),
  with **Refresh now**, again after a failed read, and shortly after a start that finds its
  index missing or out of date (as after an update); it downloads a tune when you play it,
  when it is about to play or when you open its Track Info.
- **Other downloads** you start under gear → **Metadata** reach their own sites over HTTPS:
  `ftp.modland.com` (Scene origin), `raw.githubusercontent.com` and `api.github.com`
  (song database, and its check for newer data), `data.demozoo.org` (Demozoo) and
  `www.tosecdev.org` (TOSEC); Redump lists come from `redump.org` over plain HTTP (port 80).
- **Other outbound connections**, each only when the feature is used:
  - each radio station's own server, for its stream (the address the station gives, HTTP or
    HTTPS), and, for Subsonic apps, each favourite station's logo address;
  - Radio Browser (`*.api.radio-browser.info`, HTTPS): its country list when the world
    directory is opened and the stored copy is missing or more than a week old; a
    country's stations when you open that country (refreshed daily); station matches for a
    search in the Stations view — typed (two or more characters), or with **Show all** or
    Enter (Enter: any length) — and in the phone page's Radio → Browse search (two or more
    characters); a directory station's details, when they aren't stored yet, as its row is
    hovered or focused in a list or when it is played (also by a multi-room room or a
    Subsonic app) or added to Favorites; and a click report when a directory station is
    played;
  - `lrclib.net` and `api.lyrics.ovh` for lyrics, for a track whose tags hold none (never for
    retro formats other than MIDI); at the first lookup, and then at most every 30 minutes
    while lookups are made, both are also asked how fast they answer;
  - `musicbrainz.org`, `www.wikidata.org` and `*.wikipedia.org` for artist info (Track
    Info, and Subsonic apps' artist info), and `upload.wikimedia.org` for the artist photos
    Subsonic apps show;
  - `musicbrainz.org` and `coverartarchive.org` (its images come from `archive.org`), and
    `api.discogs.com` (and the image address it names) with a Discogs token, for the cover
    of the song a station is playing;
  - `demozoo.org` for scener and production details (Track Info's **Scene** tab);
  - `api.listenbrainz.org` / `ws.audioscrobbler.com` (last.fm) for scrobbling, once an
    account is connected (a scrobble that couldn't be sent is tried again every minute,
    from startup on);
  - downloads you start under gear → **Renderers**: the SoundFonts under **Download Known
    Soundfonts** come from Dropbox, SourceForge, `ftp.osuosl.org`, `archive.org` and
    `musical-artifacts.com`, and **Download bundled ffmpeg** (offered when ffmpeg is
    missing or incomplete) from `github.com`.

  With `SONIQBOOM_OFFLINE=1` (see [Configuration](#configuration)) the server contacts none
  of the hosts in these notes; network shares on your own network still work.
- **API reference.** `http://<host>:8080/api/docs` lists SoniqBoom's HTTP API (sign in first).
- **MIDI** plays with the General-MIDI SoundFont FluidR3_GM (Debian's `fluid-soundfont-gm`)
  out of the box; add richer SoundFonts later under gear → **Renderers → Soundfonts**
  (they're stored in `/data/soniqboom/soundfonts`, on the volume).
- **Casting (Beta) and the DLNA Media Server (Beta).** The image doesn't include the
  Python packages casting uses (`pychromecast`, `pyatv`, `async-upnp-client`); when no
  device is found, the cast picker lists the ones missing. Both casting and the DLNA
  Media Server find devices or announce themselves by multicast on your network (the
  DLNA Media Server on UDP port 1900) and give devices the server's own address, so on
  Docker's default network they see the container's internal address instead of your
  machine's. They need the container on the host's network (on a Linux host:
  `network_mode: host` in place of `ports:` in `docker-compose.yml`, or `--network host`
  in place of `-p` with `docker run`); casting also needs those packages added to the image.
- **Licences and source code**: each [GitHub release](https://github.com/SFCyris/SoniqBoom/releases)
  includes the source code of the third-party programs in the prebuilt image, a list of its
  Debian packages and their source packages — see [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md#source-code-for-distributed-binaries).
- **Backups & recovery**: see the [section above](#backups--recovery) — copy the
  `soniqboom-data` volume, export a portable `.sbz`, or restore from the on-disk
  `library.json.prev` known-good snapshot.
