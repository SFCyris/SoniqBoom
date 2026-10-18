# VUMR Cache Format — Per-Channel VU Sidecar (v1)

> **Status**: v1 stable from SoniqBoom 1.3.0.
> **Owner**: S.F. Cyris

The VUMR (**VU MeteR**) file is a small binary sidecar stored in the
conversion cache, normally beside the tune's rendered WAV (`<name>.vu` next
to `<name>.wav`).  A sidecar can also exist without its WAV: one written
before the audio was cached, or a tracker backfill written to a slot of its
own.  It carries pre-computed per-channel volume samples that the frontend
indexes by playback time to render true per-channel VU bars.

Three pipelines write it, all in the layout below:

* **Tracker modules** — ``soniqboom/core/openmpt_vu.py`` reads the module
  through ``libopenmpt`` via a thin ``ctypes`` binding, alongside the
  ``openmpt123`` render of the audio WAV.  When a tracker track has no
  sidecar yet, the first ``GET /api/tracks/<id>/vu`` extracts one from the
  source file (every tracker format except ASYLUM/DMP ``.amf``).  One
  channel per module channel, at most 32: a module with more channels gets
  meters for its first 32.
* **C64 SID tunes** — three channels, one per SID voice.  The browser
  renders them with its WebAssembly SID engine (this needs WebAssembly SIMD)
  and, for an account with edit or admin rights, uploads the sidecar (see
  *Uploaded sidecars*).  The browser renders at most the first 600 s of a
  tune, so an uploaded sidecar for a longer tune covers only its first
  10 minutes.  With *Play SID in browser* on, the browser uploads its
  sidecar only together with the WAV it rendered, which needs an admin
  account and the default SID fidelity settings.  If no sidecar has arrived
  8 s after a play starts, the server renders each voice on its own with the
  other two muted and builds the sidecar from those renders
  (``soniqboom/core/sid_vu.py``); it does this for tunes up to 600 s.
* **Amiga modules played through uade** (AHX included) up to 420 s — four
  channels, one per Paula voice, from a second uade pass that dumps the
  voices (``soniqboom/core/uade_vu.py``).  The pass is requested when the
  web player asks for the meters of the track it is playing, once that track
  has been served for 3 s, and waits a further 4 s before it queues.  The
  *Per-voice VU meters for Amiga modules* setting (gear → Renderers →
  Playback preparation) turns it off.  HivelyTracker (``.hvl``) tunes are
  rendered by ``hvl2wav`` and have no sidecar.

The web player shows meters only while the visualizations master switch and
the now-playing switch are on.  With either off, it asks for no sidecar, so
no Amiga pass starts and the browser renders no SID sidecar except as part of
*Play SID in browser*; the server still renders a SID sidecar when a SID tune
plays.  These settings are under gear → System → Settings → Visualizations
(*Enable visualizations*, *Now-playing*, *Play SID in browser*); for accounts
without admin rights, under gear → Preferences (*Visualizations*,
*Now-playing visualizations*, *Play SID in browser (experimental)*).  They
apply to the browser they are set in.

A sidecar stays on disk when its WAV is evicted from the cache, and **Clear
transcoded only** leaves it in place; **Clear all conversion cache** (gear →
Renderers → Transcode & render cache) removes it.  A render found to be
silent is not cached, and a sidecar beside it is deleted with it.

---

## Layout (little-endian)

```
Offset  Size   Field           Notes
0       4      Magic           ASCII "VUMR"
4       1      Version         0x01 for v1
5       1      Channels (N)    1–32
6       1      Flags           v1: reserved (0)
7       1      Reserved        v1: writers set 0; readers ignore
8       4      Sample rate Hz  uint32 LE — frames per second (30 from every writer)
12      4      Frame count F   uint32 LE — total captured frames
16      N      Pan per channel uint8 × N (see encoding table below)
16 + N  F*N    Mono amplitude  uint8 × F × N — row-major (frame-major)
```

**Total file size**: `16 + N + F * N` bytes.

For a 4-minute, 8-channel module at 30 Hz:
`16 + 8 + 7200 * 8 = 57 624 bytes` on disk.  The API response is
gzip-compressed when the client accepts it.

---

## Pan encoding (1 byte per channel)

| Value | Meaning           | Frontend glyph |
|-------|-------------------|----------------|
| 0     | Centre / unknown  | `○●○`          |
| 1     | Left              | `●○○`          |
| 2     | Right             | `○○●`          |

Stored once in the header.  Pan automation within the song is not
captured in v1.

* **Tracker** — pan is **derived** during VU extraction by summing each
  channel's left and right VU readings over the entire song and classifying:

  ```
  if   L + R == 0   -> 0 (centre; a silent channel)
  elif L > R * 1.5  -> 1 (left)
  elif R > L * 1.5  -> 2 (right)
  else              -> 0 (centre)
  ```

  This captures the effective pan distribution as the song actually
  plays, including any default-pan settings from the song header.  Songs
  with pan automation that swings hard are classified by where the
  channel spent most of its energy.
* **SID** — every voice is 0 (the SID output is mono).
* **Amiga** — the Paula channel layout: `1 2 2 1` (channels 0 and 3 left,
  1 and 2 right).

---

## Mono amplitude (1 byte per channel per frame)

0 = silent / no note playing on that channel this frame.  How a byte is
produced depends on the writer:

* **Tracker** — a `uint8` quantisation of libopenmpt's
  ``openmpt_module_get_current_channel_vu_mono(mod, ch)`` reading, taken
  after each 1/30 s of the song:

  ```
  byte = floor(clamp(vu_mono, 0.0, 1.0) * 255)
  ```

  255 = full-scale (rare but possible for trackers using volume slides).
* **SID and Amiga** — the peak magnitude of each voice within the frame,
  scaled by the loudest frame of any voice in the whole tune:

  ```
  byte = min(255, frame_peak * 255 // tune_peak)
  ```

  The loudest moment of the tune reaches 255, and the balance between voices
  is kept.  SID peaks come from 1470-sample windows (1/30 s at 44.1 kHz) of
  the isolated-voice renders on the server, or from the engine's per-voice
  output in the browser.  Amiga peaks come from the high byte of each Paula
  voice's output.

**Row-major layout** means frame `i` starts at offset
`16 + N + N * i`.  Random access by frame is `O(1)`:

```python
def get_frame(buf: bytes, channels: int, frame_idx: int) -> bytes:
    base = 16 + channels + frame_idx * channels
    return buf[base : base + channels]
```

The frontend uses this to index by the playback position:

```javascript
const frameIdx = Math.min(frames - 1,
                          Math.max(0, Math.floor(currentTime * sampleRateHz)));
const base = 16 + channels + frameIdx * channels;
const frame = new Uint8Array(buf, base, channels);
// display level for channel ch = (frame[ch] / 255) ** 0.7
```

---

## Sample rate and frame count

Every writer uses **30 Hz**.  Rates 1–240 Hz are valid in the format; the
frontend and the upload check accept them and honour whatever rate the file
declares.

The frame count follows the audio: a tracker sidecar has one frame per
1/30 s until the module ends (at most its reported length plus one frame;
600 s plus one frame when it reports no length); a SID sidecar has one frame
per 1/30 s of the render; an Amiga sidecar has `floor(length * 30)` frames
for the rendered length.

---

## Atomic write

The server writes a sidecar to `<name>.vu.tmp`, then `os.replace()`s it to
the final `<name>.vu` path.  An uploaded sidecar is written to a temporary
file with a unique name (`<name>.vu.<random>.tmp`) and moved into place the
same way.  Readers never see a partial file.

---

## Serving

`GET /api/tracks/<id>/vu` returns the sidecar as
`application/octet-stream` with `Cache-Control: public, max-age=31536000,
immutable`, an `ETag` and `X-VU-Version: 1`.  `subsong=<n>` (the same
`subsong` value the stream URL carries; 0 is the file's default tune) selects the tune of a multi-tune file, which has
one sidecar per tune; without it, the tune a plain play of the file renders.

---

## Uploaded sidecars

`POST /api/tracks/<id>/vu?subsong=<n>&content_hash=<sha256>` stores a
browser-rendered SID sidecar.  It needs an account with edit or admin
rights.  The body must be a VUMR v1 file with exactly 3 channels, a sample
rate of 1–240 Hz, at most 144 000 frames and a total size of exactly
`16 + N + F * N` bytes (and no more than 256 KB), and `content_hash` must be
its SHA-256.  For any tune other than the file's default, `tune=<n>` must
name the 1-based tune the browser rendered.

| Response | When |
|----------|------|
| 204 | stored, or a sidecar for that tune already exists (it is never replaced) |
| 401 / 403 | not signed in, or the account lacks edit rights |
| 404 | no such track |
| 409 | `subsong` is above 0 and `tune` is missing or names another tune than the server plays for it |
| 413 | the body is larger than 256 KB |
| 415 | the track is not a C64 SID |
| 422 | the body is not a valid 3-channel VUMR v1 file, `content_hash` is missing, not 64 characters or doesn't match, or `subsong` (0–1024) or `tune` (1–1025) is out of range |
| 425 | the SID's WAV is not in the conversion cache yet — try again later |

---

## Fallback behaviour

If no VUMR sidecar exists (legacy cache, no transcode on record,
libopenmpt unavailable on this host, a format without per-voice data),
`GET /api/tracks/<id>/vu` returns HTTP 204 (No Content); an unknown track
is a 404.  For an Amiga track, a miss
also starts the background per-voice pass, unless the request passes
`start=0`.  The frontend then shows its FFT-spectrum visualiser with a
label under the seek bar:

* **204 without an `X-VU-Unavailable` header** — meters may still arrive
  (an Amiga pass pending or running, a SID sidecar still rendering).  For
  SID and Amiga tracks the label reads
  `Spectrum — per-voice meters loading…` and the player asks again on a
  retry schedule.
* **204 with `X-VU-Unavailable`** — no meters will come for this track,
  and the player stops asking.  The value says why, and the label follows
  it:

  | Value         | Meaning                                              | Label |
  |---------------|------------------------------------------------------|-------|
  | `off`         | the per-voice VU setting for Amiga modules is off    | `Spectrum — per-voice meters are turned off (Settings → Renderers)` for an admin, `Spectrum — per-voice meters are turned off on this server` for anyone else |
  | `unsupported` | this uade build can't produce per-voice data          | `Spectrum — per-voice meters not available on this server` |
  | `skipped`     | this tune's per-voice pass ran without a result       | `Spectrum — per-voice meters not available for this tune` |

Other formats show `Spectrum — per-voice meters not available for <format>`
straight away, and a `loading…` label turns into that one when the player
stops waiting.

---

## Versioning

* The version byte at offset 4 identifies the layout; this document
  describes version 1.
* Readers check the version byte before parsing: the player and the upload
  check reject any version other than 1.
* The flags byte at offset 6 and the byte at offset 7 are reserved: v1
  writers set them to 0, and readers ignore them.
