<!-- SPDX-FileCopyrightText: 2026 S.F. Cyris · SPDX-License-Identifier: AGPL-3.0-or-later -->
# Deploying SoniqBoom with Docker

This is the copy-paste guide. If you've never used Docker before, follow it top to
bottom — every step is a command you paste into a terminal. The **same commands work
on both Intel/AMD (x86_64) and ARM (Apple Silicon, Raspberry Pi, ARM servers)** — Docker
automatically builds the version that matches your machine.

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

## Step 1 — Get SoniqBoom and start it (4 commands)

> **Skip the build?** A prebuilt multi-arch image (Intel/AMD + ARM) is published at
> `ghcr.io/sfcyris/soniqboom:latest`. In `docker-compose.yml`, comment out `build: .`
> and uncomment the `image:` line (there's a comment marking the spot), then use
> `docker compose pull && docker compose up -d` at step 3 — it starts in seconds
> instead of compiling the players. Building from source (below) is only needed to
> modify SoniqBoom.

```bash
# 1. Download the code
git clone https://github.com/SFCyris/SoniqBoom.git
cd SoniqBoom

# 2. Put your music where the server can see it (or symlink it)
mkdir -p music          # then copy/move your music into the ./music folder
                        # (Mac/Linux shortcut instead of copying: ln -s /path/to/your/music ./music)

# 3. Build and start (first run takes a few minutes — it compiles the
#    retro-format players; after that, starts in seconds)
docker compose up -d

# 4. Create your login — replace 'me' and the password with your own (keep the quotes)
docker compose exec soniqboom soniqboom-setadm -user me -passwd 'change-this-password'
#    You'll see:  Created user 'me' …  ·  Notified the running server — the change is live now
```

That's it — the server is running. Go to **Step 2** to open it.

> **Is it running?** `docker compose ps` should list `soniqboom` as **Up**. To watch it
> start (or see an error), run `docker compose logs -f` and press Ctrl-C to stop watching.

> **No `git`?** Download the ZIP instead: grab
> <https://github.com/SFCyris/SoniqBoom/archive/refs/heads/main.zip>, unzip it, and
> `cd` into the folder before running `docker compose up -d`.

---

## Step 2 — Open it and add your music

1. On **the same computer**, open a browser to **<http://localhost:8080>**.
2. Sign in with the username and password you made in Step 1.
3. Click the **gear icon** (top-right) → **Music Folders** → add **`/music`** → **Add**.
4. Wait for the scan (about 2 minutes per 50,000 tracks). Done — press play.

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
on phones.)

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

**Build on the machine you'll run it on** and you never have to think about architecture.
The niche Atari-ST `.sc68` player only builds cleanly on a native machine — building it
for a *different* CPU than your own (cross-building) can skip it; every other format is
unaffected.

### Plain `docker run` (without Compose)

```bash
# Use the prebuilt image (no local build; Docker fetches the right CPU automatically):
IMAGE=ghcr.io/sfcyris/soniqboom:latest
# …or build it yourself instead:  docker build -t soniqboom . && IMAGE=soniqboom

docker run -d --name soniqboom \
  -p 8080:8080 \
  -v /path/to/your/music:/music:ro \
  -v soniqboom-data:/data \
  -e SONIQBOOM_DATA_DIR=/data \
  --restart unless-stopped \
  "$IMAGE"

docker exec soniqboom soniqboom-setadm -user me -passwd 'change-this-password'
```

### One image for both CPU types (advanced, for sharing/registries)

If you maintain a container registry and want a single tag that runs on both Intel and
ARM, build a multi-arch image with buildx (run each on a native machine of that CPU, or
use CI runners):

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t <your-registry>/soniqboom:latest --push .
```

Anyone can then `docker pull <your-registry>/soniqboom:latest` and Docker fetches the
right CPU variant automatically.

---

## What goes where

| Path | Purpose | How it's mounted |
|------|---------|------------------|
| `/music` | Your audio library | read-only (`-v /your/music:/music:ro`) — the server never writes here |
| `/data`  | Index, conversion cache, config, **and user accounts** | a named volume (`soniqboom-data`) — **this is what you back up** |

The server reads music and writes everything else under `/data`, so one volume holds all
the state that must survive a restart or upgrade.

---

## Backups & recovery

Back up the `soniqboom-data` volume (`/data`) and you have everything. Your
**music files are never touched** — all of this is just the index and settings.

**Portable export.** For a self-contained copy of the library index, use
**Settings → Backup → Export `.sbz`**; **Import** restores it.

**On-disk snapshots.** The library index is stored as `library.json` under
`/data`, alongside automatic copies you can fall back to:

| File | What it is |
|------|-----------|
| `library.json` | the live index |
| `library.json.bak` | the previous write (rotated on each save) |
| `library.json.prev` | the last snapshot that **loaded cleanly on startup** — a stable "known good" copy |
| `library.aof` | recent changes not yet folded into `library.json` (replayed on the next start) |

**Restore by hand** — only if the index won't load (e.g. after a disk problem).
Stop the server, then in `/data`:

```bash
cp library.json.prev library.json   # roll back to the last known-good snapshot
rm -f library.aof                    # optional: drop unmerged changes if the journal is suspect
```

Start the server again — it rebuilds its in-memory indexes from `library.json`
on boot. A fresh start also refreshes `library.json.prev` once the restored
snapshot loads cleanly.

---

## Updating to a newer version

```bash
git pull
docker compose up -d --build      # rebuild + restart; /data (and your library index) is kept
```

Because the index lives in `/data`, an upgrade reuses it — no full rescan.

---

## Automatic HTTPS (access over the internet)

The repo ships a Caddy setup that gets a real Let's Encrypt certificate automatically:

1. Point your domain's DNS at the host and open ports **80** and **443**.
2. Edit [`deploy/Caddyfile`](deploy/Caddyfile) — replace `music.example.com` with your domain.
3. Launch:

   ```bash
   docker compose -f deploy/docker-compose.https.yml up -d
   ```

4. Open `https://your-domain`. The certificate is issued on the first request.

Here SoniqBoom is **not** published on the host — only Caddy is — so the single way in is
over TLS. (Prefer Traefik or nginx-proxy-manager? Point any reverse proxy at the
`soniqboom` container's port `8080`.)

---

## Configuration

| Variable | Default | Notes |
|----------|---------|-------|
| `SONIQBOOM_DATA_DIR` | `/data` | Where all state is written (set in the image). |
| `TZ` | container default | Set e.g. `Europe/Berlin` for correct log/scan timestamps. |

To change the **host** port, edit the mapping in `docker-compose.yml` (`"9000:8080"`
publishes on 9000) rather than the in-container port.

---

## Handy commands

```bash
docker compose logs -f          # watch the server's output
docker compose restart          # restart it
docker compose down             # stop it (keeps your data volume)
docker compose exec soniqboom soniqboom-setadm -user bob -passwd 'pw-1234' -role readonly
                                # add another user (roles: admin, edit, readonly)
```

---

## Notes

- **Renderers degrade gracefully.** If a format won't play, the player names the package
  it needs; everything else keeps working. The image bundles all the common players;
  `uade123` (Amiga AHX and the ~150 Amiga "exotica" formats) is compiled from source, so
  it's best-effort — if that build fails, only those Amiga formats are disabled and
  everything else keeps working.
- **Memory** scales with library size (the whole index is held in RAM). Budget accordingly
  for six-figure collections.
- **MIDI** plays with a bundled General-MIDI SoundFont out of the box; add richer
  SoundFonts later under **Settings → Renderers → Soundfonts**.
- **Backups & recovery**: see the [section above](#backups--recovery) — copy the
  `soniqboom-data` volume, export a portable `.sbz`, or restore from the on-disk
  `library.json.prev` known-good snapshot.
