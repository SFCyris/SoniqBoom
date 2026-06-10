# VUMR Cache Format — Per-Channel VU Sidecar (v1)

> **Status**: v1 stable from SoniqBoom 1.3.0.
> **Owner**: S.F. Cyris

The VUMR (**VU MeteR**) file is a small binary sidecar stored next to a
rendered tracker-module audio cache.  It carries pre-computed
per-channel volume samples that the frontend indexes by playback time
to render true per-channel VU bars.

A VUMR file is produced by ``soniqboom/core/openmpt_vu.py`` during the
same transcode pass that produces the audio WAV / MP3, using
``libopenmpt`` via a thin ``ctypes`` binding.

---

## Layout (little-endian)

```
Offset  Size   Field           Notes
0       4      Magic           ASCII "VUMR"
4       1      Version         0x01 for v1
5       1      Channels (N)    1–32
6       1      Flags           v1: reserved (0)
7       1      Reserved        v1: must be 0
8       4      Sample rate Hz  uint32 LE — frames per second (typically 30)
12      4      Frame count F   uint32 LE — total captured frames
16      N      Pan per channel uint8 × N (see encoding table below)
16 + N  F*N    Mono amplitude  uint8 × F × N — row-major (frame-major)
```

**Total file size**: `16 + N + F * N` bytes.

For a typical 4-minute, 8-channel ProTracker at 30 Hz:
`16 + 8 + 7200 * 8 = 57 624 bytes` raw, ~10–20 KB gzip-compressed.

---

## Pan encoding (1 byte per channel)

| Value | Meaning           | Frontend glyph |
|-------|-------------------|----------------|
| 0     | Centre / unknown  | `○●○`          |
| 1     | Left              | `●○○`          |
| 2     | Right             | `○○●`          |

Stored once in the header.  Pan automation within the song is not
captured in v1.  Pan is **derived** during VU extraction by summing each
channel's L/R contribution over the entire song and classifying:

```
ratio = mean_L / mean_R
if   ratio > 1.5  -> 1 (left)
elif ratio < 1/1.5 -> 2 (right)
else              -> 0 (centre)
```

This captures the effective pan distribution as the song actually
plays, including any default-pan settings from the song header.  Songs
with pan automation that swings hard are classified by where the
channel spent most of its energy.

---

## Mono amplitude (1 byte per channel per frame)

A `uint8` quantisation of libopenmpt's
``openmpt_module_get_current_channel_vu_mono(mod, ch)`` reading,
captured at `Sample rate Hz`:

```
byte = round(clamp(vu_mono, 0.0, 1.0) * 255)
```

255 = full-scale (rare but possible for trackers using volume slides).
0 = silent / no note playing on that channel this frame.

**Row-major layout** means frame N starts at offset
`16 + N + N_channels * frame_idx`.  Random access by frame is `O(1)`:

```python
def get_frame(buf: bytes, channels: int, frame_idx: int) -> bytes:
    base = 16 + channels + frame_idx * channels
    return buf[base : base + channels]
```

The frontend uses this to index by `audio.currentTime`:

```javascript
const frameIdx = Math.floor(audio.currentTime * sampleRateHz);
const base = 16 + channels + frameIdx * channels;
const frame = new Uint8Array(buf, base, channels);
// frame[ch] / 255 = display level for channel ch
```

---

## Sample rate

The default is **30 Hz**.  Custom sample rates 1–240 Hz are valid in the format.  The frontend
honours whatever rate the file declares.

---

## Atomic write

Files are written via `path.with_suffix(".vu.tmp")` then `os.replace()`
to the final `.vu` path.  Readers never see a partial file.

---

## Fallback behaviour

If a VUMR sidecar is absent (legacy cache, no transcode on record,
libopenmpt unavailable on this host, non-tracker format), the
`/api/tracks/<id>/vu` endpoint returns HTTP 404.  The frontend
gracefully falls back to its FFT-spectrum visualiser and labels itself:

> `FFT Spectrum Analyzer Fallback — Individual Channel Meter not available for <format>`

---

## Versioning

* Version byte at offset 4 will be incremented on any breaking change.
* Readers MUST check the version byte before parsing.
* The flags byte at offset 6 is reserved and must be 0 in v1.
