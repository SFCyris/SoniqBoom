<!-- SPDX-FileCopyrightText: 2026 S.F. Cyris · SPDX-License-Identifier: AGPL-3.0-or-later -->
# Deploying SoniqBoom with Docker

One container plays every format SoniqBoom supports — chiptune, tracker, SID, MIDI,
lossless and DSD — with ffmpeg and all the renderers baked in.

## Quick start (Docker Compose)

```bash
git clone https://github.com/SFCyris/SoniqBoom.git
cd SoniqBoom
mkdir -p music                      # put or symlink your library here
docker compose up -d                # builds the image and starts the server
```

Then create the first admin (web sign-up is locked until one admin exists):

```bash
docker compose exec soniqboom soniqboom-setadm -user alice -passwd 'a-strong-password'
```

Open `http://<host>:8080`, sign in, then **Settings → Music Folders → add `/music`**
and wait for the scan (~2 minutes per 50,000 tracks).

## Quick start (plain `docker run`)

```bash
docker run -d --name soniqboom \
  -p 8080:8080 \
  -v /path/to/your/music:/music:ro \
  -v soniqboom-data:/data \
  ghcr.io/sfcyris/soniqboom:latest     # or: build locally, see below

docker exec soniqboom soniqboom-setadm -user alice -passwd 'a-strong-password'
```

(Until a published image exists, build it yourself: `docker build -t soniqboom .`
and use `soniqboom` in place of the `ghcr.io/...` tag.)

## What goes where

| Path | Purpose | Mount as |
|------|---------|----------|
| `/music` | Your audio library | read-only bind (`-v /your/music:/music:ro`) |
| `/data`  | Index, conversion cache, config, **and user accounts** | named volume — **this is what you back up** |

The server reads music; it never writes to `/music`. Everything that must survive a
restart or upgrade lives under `/data`.

## Automatic HTTPS (remote access)

The repo ships a Caddy stack that gets a real Let's Encrypt certificate with zero
manual cert wrangling:

1. Point your domain's DNS at the host and open ports **80** and **443**.
2. Edit [`deploy/Caddyfile`](deploy/Caddyfile) — replace `music.example.com` with your domain.
3. Launch:

   ```bash
   docker compose -f deploy/docker-compose.https.yml up -d
   ```

4. Open `https://your-domain`. The certificate is issued on the first request.

Here SoniqBoom is **not** published on the host — only Caddy is — so the single way
in is over TLS. (Prefer Traefik or nginx-proxy-manager? Point any reverse proxy at
the `soniqboom` container's port `8080`.)

## Configuration

| Variable | Default | Notes |
|----------|---------|-------|
| `SONIQBOOM_DATA_DIR` | `/data` | Where all state is written (set in the image). |
| `TZ` | container default | Set e.g. `Europe/Berlin` for correct log/scan timestamps. |

Bind address/port are `0.0.0.0:8080` by default; change the **host** mapping in the
compose file (`"9000:8080"`) rather than the in-container port.

## Updating

```bash
git pull
docker compose up -d --build          # rebuild + restart; /data is preserved
```

Because the library index lives in `/data`, an upgrade re-uses it — no full rescan.

## Notes

- **Renderers degrade gracefully.** If a format won't play, the player names the
  package; everything else keeps working. The image bundles all the common ones;
  `uade123` (Amiga AHX) is best-effort depending on the Debian mirror.
- **Memory** scales with library size (the whole index is held in RAM). Budget
  accordingly for six-figure collections.
- **Backups**: copy the `soniqboom-data` volume, or use the in-app
  **Settings → Backup → Export .sbz** for a portable library snapshot.
