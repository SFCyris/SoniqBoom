# DLNA Media Server — Real-Device Runbook

The pytest suite + the `scripts/discoverability_harness.py` script
verify the protocol *shape* (SSDP framing, device description schema,
SOAP Browse + DIDL-Lite payload) is well-formed.  What pytest can't
verify is that an actual TV / phone / speaker / DLNA controller
accepts the payload and plays the audio.  This document lists per-
device manual checks.

## Prereqs

1. Enable the service (off by default since DLNA broadcasts the
   library to anyone on the LAN):

   ```
   .venv/bin/soniqboom services enable dlna_server
   ./shutdown.sh && ./run.sh
   ```

   Or via the new Settings → Services panel in the SoniqBoom admin
   page.  Restart is required.

2. Verify SoniqBoom shows up on the LAN before testing a device:

   ```
   .venv/bin/python scripts/discoverability_harness.py
   ```

   Expected output ends with:

   ```
   PASS: cast byte-server returned bytes (first 8B: …)
   ```

3. If the harness shows nothing, check `lsof -nP -iUDP:1900` — only
   one DLNA stack can own port 1900 at a time.  macOS users running
   a media-server app (Plex, Universal Media Server) need to stop
   that other server before SoniqBoom can take 1900.

---

## A — LG WebOS TV (smart TV)

This was the easiest target on my dev box because the LG TV was
already running its built-in DLNA controller.

1. On the TV, open the "Photo & Video" or "Music" app (LG renames
   this every firmware).  The DLNA browser is one of the source
   options.
2. Expected: "SoniqBoom (your-hostname)" shows up in the list.
3. Browse into Music → All Tracks → pick a track.
4. PASS criteria: audio plays within 3 s.  Track title shows on
   the TV's playback UI.
5. FAIL modes:
   - "Device not found" on the TV → multicast may not be routing
     between SoniqBoom's interface and the TV's.  See diagnostic
     below.
   - Plays for 1 s then stops → check the cast token's TTL is
     compatible with the TV's HTTP timing.  TTL is 15 minutes;
     should never trip.

## B — Sonos (S2 firmware)

Sonos uses the LAN UPnP-AV pathway to browse external libraries.

1. Open the Sonos S2 app.
2. Settings → Services & Voice → Add a music service → "Music
   Library" / "On this computer" depending on app version.
3. Sonos auto-discovers UPnP MediaServers.  "SoniqBoom" should
   appear within 10 seconds.
4. Open the new library in the Browse tab → Music → All Tracks
   → pick one.
5. PASS criteria: audio starts within 5 s.  Sonos shows the track
   metadata.
6. FAIL modes:
   - Sonos says "no music found" → it sometimes ignores libraries
     with > 65 K tracks; if your library is huge, narrow the
     visible set with a folder selection.
   - Track plays only 30 s → Sonos's network-error retry kicks in
     when a chunk is delayed; usually means the source needs
     transcoding the renderer didn't predict (rare with our
     content-type advertising).

## C — BubbleUPnP (Android control point)

A control point isn't a renderer — it discovers servers and renderers
separately and bridges them.

1. Install BubbleUPnP from the Play Store.
2. Tap the library icon (top-left).  "SoniqBoom" should appear under
   Servers.
3. Tap a renderer (your TV / speaker) in the same screen.
4. Browse to a track → play.
5. PASS criteria: audio plays on the renderer.  BubbleUPnP shows the
   metadata + time bar.
6. FAIL modes:
   - "Empty library" — BubbleUPnP requires `Browse(0)` to return a
     specific container; we return Music.  Should work.

## D — VLC ("Network Streams")

Useful as a sanity check because VLC's UPnP-AV browser is well-tested
and works on every platform.

1. VLC → View → Playlist → "Local Network" → "Universal Plug 'n' Play"
2. SoniqBoom should appear in the device list.
3. Drill into Music → All Tracks → double-click a track.
4. PASS criteria: VLC plays the track within 2 s.

---

## Diagnostic: SSDP traffic capture

If a device doesn't see SoniqBoom but the harness does, the most
likely cause is a network-segmenting issue (VPN, bridged Docker
network, Wi-Fi client isolation).  Capture multicast traffic with:

```
sudo tcpdump -i any -n -A -s 0 'udp port 1900'
```

You should see:

- Our `NOTIFY * HTTP/1.1` ssdp:alive bursts every ~5 minutes.
- M-SEARCH probes from the controller you're testing.
- Our 200-shaped reply within ~1 second of the M-SEARCH.

If you see our NOTIFYs but no M-SEARCH from the controller, the
controller can't reach the multicast group — fix the network
segmentation.

If you see M-SEARCH but no reply from us, the controller's M-SEARCH
isn't reaching the SoniqBoom host — confirm with `ifconfig` that
your SoniqBoom is bound to an interface on the same subnet.

## Privacy posture

The DLNA Media Server is **OFF by default** because it advertises
the user's entire music library on the LAN.  Anyone on the same
network can browse the track list and stream audio without
authentication — DLNA is intrinsically anonymous.

When the service is ON:

- Track URLs (`/cast/{token}/...`) are signed but anonymous
  (no `user_id` claim).
- Tokens expire 15 minutes after minting.
- A leaked URL from a Browse response remains valid for that
  window — not enough for a long-term archive theft, but enough
  for one play.

When the service is OFF:

- The SSDP socket is not opened.
- `/dlna/device.xml` and the SOAP endpoints return 404.
- SoniqBoom is invisible to LAN DLNA discovery.

The toggle is per-server: a multi-machine deployment must enable
it on each SoniqBoom instance individually.
