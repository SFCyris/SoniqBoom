# Cast / DLNA / AirPlay — Real-Device Validation Runbook

The pytest suite (`tests/cast/`) verifies the **protocol-call shapes** —
DIDL-Lite XML well-formed, Chromecast `play_media` invoked with the right
arguments, pyatv `stream_url` / `play_url` selected per AirPlay version,
DLNA flags conform to the spec.  What pytest can NOT verify is that a
real device on the LAN accepts the payload and renders audio.  This
runbook lists the manual checks per protocol + device class.

## Prerequisites

```
brew install uade ffmpeg              # macOS — install renderers
./run.sh                              # start SoniqBoom on the LAN
soniqboom services enable cast        # ensure /api/cast/* is mounted
.venv/bin/pip install pychromecast pyatv async-upnp-client
```

Verify discovery before driving any device — if discovery doesn't see the
target, no amount of `/api/cast/play` will help:

```bash
curl -b cookies.txt http://localhost:8080/api/cast/targets | jq
```

Expected: a JSON array containing each renderer on the LAN with its
`id`, `name`, `protocol` (cast / airplay / dlna), `host`, `port`.

---

## DLNA (Sonos / Samsung TV / LG TV / Yamaha receiver)

### A1 — SetAVTransportURI accepts the DIDL-Lite payload

```bash
TARGET_ID=$(curl -s -b cookies.txt http://localhost:8080/api/cast/targets \
  | jq -r '.targets[] | select(.protocol == "dlna") | .id' | head -1)
TRACK_ID=<a known FLAC track id>

curl -s -b cookies.txt -X POST http://localhost:8080/api/cast/play \
  -H 'Content-Type: application/json' \
  -d "{\"target_id\":\"$TARGET_ID\",\"track_id\":\"$TRACK_ID\"}"
```

PASS criteria:
- HTTP 200 with `{"ok":true,"delivered_codec":"flac",...}`.
- Audio is audible on the renderer within 3 seconds.
- Renderer's "Now Playing" display shows the track title and artist
  (DIDL-Lite metadata delivered).

FAIL → grab the server log and grep `dlna_controller`. Common causes:

| Symptom | Likely cause |
|---|---|
| 502 from `/api/cast/play` | `description_url` from SSDP was unreachable; check `cast_targets.discover` output for the actual URL |
| 200 but no audio, renderer says "format not supported" | Codec negotiation chose something the renderer doesn't really have; capture `GetProtocolInfo` response and feed to `cast_codecs.parse_sink_protocol_info` |
| 718 ("Invalid InstanceID") | DIDL-Lite XML malformed — check `test_dlna_didl_lite_metadata_well_formed` is still passing |

### A2 — Pause / resume / seek round-trips

```bash
# pause
curl -s -b cookies.txt -X POST http://localhost:8080/api/cast/control \
  -H 'Content-Type: application/json' \
  -d "{\"target_id\":\"$TARGET_ID\",\"action\":\"pause\"}"
# resume
curl -s -b cookies.txt -X POST http://localhost:8080/api/cast/control \
  -d "{\"target_id\":\"$TARGET_ID\",\"action\":\"resume\"}"
# seek to 30 s
curl -s -b cookies.txt -X POST http://localhost:8080/api/cast/control \
  -d "{\"target_id\":\"$TARGET_ID\",\"action\":\"seek\",\"seconds\":30}"
```

PASS: each command takes effect within 1 second on the renderer.

### A3 — Format coverage per renderer

For each format below, set up a queue containing one track of that
format and verify the renderer plays it:

| Renderer | MP3 | FLAC | WAV | AAC | ALAC | DSD-as-FLAC | SID-as-MP3 |
|---|---|---|---|---|---|---|---|
| Sonos S2          | ⬚ | ⬚ | ⬚ | ⬚ | ⬚ | ⬚ | ⬚ |
| Samsung TV (2018+)| ⬚ | ⬚ | ⬚ | ⬚ | ⬚ | ⬚ | ⬚ |
| LG TV WebOS       | ⬚ | ⬚ | ⬚ | ⬚ | ⬚ | ⬚ | ⬚ |
| Yamaha receiver   | ⬚ | ⬚ | ⬚ | ⬚ | ⬚ | ⬚ | ⬚ |
| Denon HEOS        | ⬚ | ⬚ | ⬚ | ⬚ | ⬚ | ⬚ | ⬚ |

Mark ✓ when audio plays end-to-end, ✗ when something fails (note the
specific symptom).

---

## Chromecast / Cast Audio / Nest Hub

### B1 — play_media accepts the URL

```bash
TARGET_ID=$(curl -s -b cookies.txt http://localhost:8080/api/cast/targets \
  | jq -r '.targets[] | select(.protocol == "cast") | .id' | head -1)
```

Then same `/api/cast/play` call as DLNA.

PASS:
- Audio audible on the Chromecast within 3 seconds.
- Nest Hub / Chromecast-enabled TV displays the track title + album
  art (if available).

### B2 — Queue-load gapless transition

```bash
# Load 3 tracks at once; the second should start automatically
curl -s -b cookies.txt -X POST http://localhost:8080/api/cast/queue \
  -H 'Content-Type: application/json' \
  -d "{\"target_id\":\"$TARGET_ID\",\"items\":[
        {\"track_id\":\"$T1\"},
        {\"track_id\":\"$T2\"},
        {\"track_id\":\"$T3\"}
      ]}"
```

PASS: track 2 starts within 100 ms of track 1 ending — no audible gap.
Verifies the lookahead prewarm landed a hot cache for tracks 2 and 3.

---

## AirPlay (Apple TV / HomePod / AirPlay 2 receivers)

### C1 — Detect AirPlay 1 vs 2

```bash
curl -s -b cookies.txt http://localhost:8080/api/cast/targets \
  | jq '.targets[] | select(.protocol == "airplay")'
```

The `model` field should distinguish (Apple TV 4K, HomePod, etc.).
The pyatv library detects AirPlay 2 automatically; you can verify by
checking the controller log for `is_airplay2=True/False` after the
first `/api/cast/play`.

### C2 — Stream URL with metadata (AirPlay 2)

PASS:
- Apple TV / HomePod displays the SoniqBoom track title and artist.
- Track changes during a queue do NOT trigger a re-handshake — the
  receiver stays connected.

### C3 — Legacy RAOP fallback (AirPlay 1 / older speakers)

PASS:
- Audio audible on the legacy AirPlay receiver.
- Acceptable: metadata is "Unknown Track" (RAOP wire protocol has no
  metadata field).  This is a protocol limitation, not a SoniqBoom bug.

---

## Telemetry sanity check (any protocol)

After playing 10+ tracks across DLNA / Cast / AirPlay:

```bash
curl -s -b cookies.txt http://localhost:8080/api/cast/telemetry | jq
```

Expected:
- `outcomes.played` ≥ 10
- `outcomes.errored` close to 0 (≤ 10% of played is acceptable on
  flaky LAN)
- `p95_first_ms` reported per (protocol, target_codec) bucket
- Per-bucket p95 < 500 ms for any codec the budget gate covers
  (cold-cache rendered formats — SID / MIDI / tracker — are exempt)

If `p95_first_ms` for a (protocol, codec) bucket exceeds the budget
gate's threshold, investigate the controller / renderer pair.
