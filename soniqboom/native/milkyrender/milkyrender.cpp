/* SPDX-FileCopyrightText: 2026 S.F. Cyris
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * milkyrender — render an early Digital Sound Interface Kit module (DSIK
 * v1, a file starting with "DSM\x10") to a 16-bit stereo WAV with
 * MilkyPlay, MilkyTracker's playback library (BSD-3-Clause).
 *
 *   milkyrender [-r rate] [-m max_seconds] [-f fade_ms] [-l loops] FILE OUT.wav
 *   milkyrender [-r rate] [-m max_seconds] [-f fade_ms] [-l loops] --length FILE
 *   milkyrender --version
 *
 * The song plays to its natural end: where it stops, or where it has played
 * through `loops` times (default 1) and jumps back — then it plays on for
 * `fade_ms` (default 100) while fading out.  It plays for at most
 * `max_seconds` (default 3600), the last `fade_ms` fading out, mixed at
 * `rate` (default 48000) with linear interpolation and volume ramping.
 * `--length`
 * renders nothing: it prints the number of seconds the same options would
 * write (to the microsecond, e.g. "253.436000"), worked out with mixing off.
 *
 * The module is checked before MilkyPlay reads it: a header, pattern or
 * instrument that lies past the end of the file, a pattern length MilkyPlay
 * would wrap round, or packed pattern data that runs past its own length or
 * row 64 is refused ("damaged DSIK module: ...") — MilkyPlay's loader
 * trusts all of them, as are samples that all start more than 16 bytes past
 * the end of the file.
 * Sample data cut short by the end of the file plays with the missing bytes
 * as silence, and a line on stderr says so (in both modes): "milkyrender:
 * warning: truncated sample data (N bytes missing)".
 * The WAV is written as it renders (a 44-byte header
 * first, its sizes patched at the end), flushed every 0.1 s, so a reader
 * can follow it.  Exit status 0 with audio written (or a length printed),
 * 1 on a file it can't play or an output it can't write, 2 on usage. */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <new>
#include <vector>

#include "MilkyPlay.h"
#include "AudioDriver_NULL.h"

#ifndef MILKYRENDER_VERSION
#define MILKYRENDER_VERSION "1.1"
#endif
#ifndef MILKYPLAY_COMMIT
#define MILKYPLAY_COMMIT "unknown"
#endif

/* ── MilkyPlay's audio-device manager, without devices ────────────────────
 * PlayerGeneric asks it for a default driver; every render here passes its
 * own (``Sink``), so all it ever hands out is a null driver.  This replaces
 * MilkyPlay's AudioDriverManager.cpp (which pulls in the platform drivers). */
static AudioDriver_NULL g_null_driver;
AudioDriverManager::AudioDriverManager()
    : driverList(0), numDrivers(0), defaultDriverIndex(0), enumerationIndex(0) {}
AudioDriverManager::~AudioDriverManager() {}
AudioDriverInterface* AudioDriverManager::getPreferredAudioDriver() { return &g_null_driver; }
AudioDriverInterface* AudioDriverManager::getAudioDriverByName(const char*) { return &g_null_driver; }
const char* AudioDriverManager::getFirstDriverName() const { return g_null_driver.getDriverID(); }
const char* AudioDriverManager::getNextDriverName() const { return 0; }
mp_sint32 AudioDriverManager::getPreferredAudioDriverSampleRate() const { return 44100; }
mp_sint32 AudioDriverManager::getPreferredAudioDriverBufferSize() const { return 1024; }

/* ── The module, from memory ──────────────────────────────────────────────
 * MilkyPlay reads the bytes that were checked (never the file a second
 * time); a read past the end gives zeros. */
class MemFile : public XMFileBase {
  const unsigned char *data_;
  mp_uint32 size_, pos_;

 public:
  MemFile(const unsigned char *data, mp_uint32 size) : data_(data), size_(size), pos_(0) {}
  virtual mp_sint32 read(void *ptr, mp_sint32 size, mp_sint32 count) {
    if (size <= 0 || count <= 0)
      return 0;
    uint64_t want = (uint64_t)size * (uint64_t)count;
    uint64_t have = pos_ < size_ ? size_ - pos_ : 0;
    uint64_t n = want < have ? want : have;
    if (n)
      memcpy(ptr, data_ + pos_, (size_t)n);
    if (n < want)
      memset((unsigned char *)ptr + n, 0, (size_t)(want - n));
    pos_ += (mp_uint32)n;
    return (mp_sint32)(n / (uint64_t)size);
  }
  virtual mp_sint32 write(const void *, mp_sint32, mp_sint32) { return 0; }
  virtual void seek(mp_uint32 pos, SeekOffsetTypes type = SeekOffsetTypeStart) {
    uint64_t p = pos;
    if (type == SeekOffsetTypeCurrent)
      p += pos_;
    else if (type == SeekOffsetTypeEnd)
      p += size_;
    pos_ = p > size_ ? size_ : (mp_uint32)p;
  }
  virtual mp_uint32 pos() { return pos_; }
  virtual mp_uint32 size() { return size_; }
  virtual const SYSCHAR *getFileName() { return ""; }
  virtual const char *getFileNameASCII() { return ""; }
  virtual bool isOpen() { return true; }
  virtual bool isOpenForWriting() { return false; }
};

/* ── The checks MilkyPlay's DSIK v1 loader (LoaderDSMv1) doesn't make ──── */
static unsigned le16(const unsigned char *p) { return p[0] | (p[1] << 8); }
static uint32_t le32(const unsigned char *p) {
  return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24);
}

static char g_why[160];
static uint64_t g_missing;           /* sample bytes past the end of the file */

static bool refuse(const char *fmt, int n) {
  snprintf(g_why, sizeof g_why, fmt, n);
  return false;
}

/* The fixed header (176 bytes: magic, name, counts, 128 orders), then the
 * instrument and pattern paragraph pointers (x16 = file offsets).  Sample
 * data that runs past the end of the file is a file cut short when the first
 * such sample starts before the end (or in the 16 bytes of padding past it):
 * the bytes the samples lack are counted in g_missing (and read as zeros).
 * Otherwise it is refused. */
static bool dsm1_ok(const unsigned char *d, size_t n) {
  if (n < 176)
    return refuse("its header is cut short", 0);
  unsigned channels = d[37], orders = d[38], instruments = d[39], patterns = d[40];
  if (channels < 1 || channels > 32)
    return refuse("it states %d channels", (int)channels);
  if (orders > 128)
    return refuse("it states %d song positions (at most 128)", (int)orders);
  size_t ptrs = 176 + 2 * (size_t)instruments + 2 * (size_t)patterns;
  if (n < ptrs)
    return refuse("its header is cut short", 0);
  for (unsigned i = 0; i < patterns; i++) {
    size_t off = (size_t)le16(d + 176 + 2 * instruments + 2 * i) * 16;
    if (!off)
      continue;
    if (off + 2 > n)
      return refuse("pattern %d lies past the end of the file", (int)i);
    unsigned word = le16(d + off);
    if (word < 2)
      return refuse("pattern %d states a length below its own header", (int)i);
    size_t size = word - 2;
    if (off + 2 + size > n)
      return refuse("pattern %d is cut short", (int)i);
    /* The loader's unpacking: a 0 byte ends a row; otherwise the low four
       bits are the channel and the top four say which fields follow (note,
       instrument, volume: one byte each; effect: two).  The flag bytes are
       XORed with a counter that steps once per byte read. */
    const unsigned char *pk = d + off + 2;
    size_t idx = 0;
    unsigned row = 0;
    unsigned char obf = 3;
    while (idx < size) {
      unsigned char pi = (unsigned char)(pk[idx++] ^ obf++);
      if (pi == 0) {
        row++;
        continue;
      }
      unsigned need = ((pi & 128) ? 1 : 0) + ((pi & 64) ? 1 : 0) + ((pi & 32) ? 1 : 0)
                      + ((pi & 16) ? 2 : 0);
      if (need && row >= 64)
        return refuse("pattern %d has data past its 64th row", (int)i);
      if (idx + need > size)
        return refuse("pattern %d is cut short", (int)i);
      idx += need;
      obf = (unsigned char)(obf + need);
    }
  }
  size_t first_cut = (size_t)-1;     /* the start of the first sample past the end */
  int first_cut_ins = 0;
  for (unsigned i = 0; i < instruments; i++) {
    size_t off = (size_t)le16(d + 176 + 2 * i) * 16;
    if (!off)
      continue;
    if (off + 4 > n)
      return refuse("instrument %d lies past the end of the file", (int)i + 1);
    if (le32(d + off) != 0x10495344u)            /* "DSI\x10": else skipped */
      continue;
    if (off + 47 > n)
      return refuse("instrument %d is cut short", (int)i + 1);
    size_t sample = (((size_t)d[off + 35] << 16) + le16(d + off + 36)) * 16;
    size_t len = le16(d + off + 38);
    if (len && sample + len > n) {
      g_missing += sample >= n ? len : sample + len - n;
      if (sample < first_cut) {
        first_cut = sample;
        first_cut_ins = (int)i + 1;
      }
    }
  }
  if (first_cut != (size_t)-1 && first_cut >= n + 16)
    return refuse("instrument %d's sample data lies past the end of the file", first_cut_ins);
  return true;
}

/* ── The output: one beat packet per advance ──────────────────────────────
 * MilkyPlay's mixer runs a 250 Hz timer; one packet is rate / 250 frames
 * (192 at 48 kHz), the finest step its song position moves in.  At a rate
 * that isn't a multiple of 250 the packet is rounded down and the song
 * plays that much faster (44.1 kHz: 176 for 176.4 frames, 0.23 %). */
class Sink : public AudioDriver_NULL {
 public:
  const mp_sword *block() const { return compensateBuffer; }
  mp_uint32 frames() const { return (mp_uint32)bufferSize / MP_NUMCHANNELS; }
};

struct Plan {
  uint64_t end;      /* frames: where the song stops or has looped `loops` times */
  bool looped;       /* it plays on there (a jump back or the cap): fade over it */
};

/* One pass with mixing off: how far the song plays.  `repeat` false: it
 * halts where it would play a row again (or stops).  Entries into song
 * position (track_order, track_row) are recorded (the frame count after the
 * packet in which the position became it). */
struct Pass {
  uint64_t frames;
  bool halted;
  mp_sint32 order, row;
  std::vector<uint64_t> entries;
};

static void setup(PlayerGeneric &p, mp_uint32 packet) {
  p.setBufferSize(packet);
  p.setResamplerType(ChannelMixer::MIXER_LERPING_RAMPING);
  p.setSampleShift(1);
  p.setMasterVolume(256);
}

static Pass run_pass(XModule &mod, long rate, mp_uint32 packet, bool repeat, uint64_t limit,
                     mp_sint32 track_order, mp_sint32 track_row) {
  Pass r = {0, false, 0, 0, std::vector<uint64_t>()};
  Sink sink;
  PlayerGeneric p((mp_sint32)rate, &sink);
  setup(p, packet);
  p.setDisableMixing(true);
  p.startPlaying(&mod, repeat);
  p.setRamp(true);                               /* (the player startPlaying made) */
  mp_sint32 prev_o = -1, prev_r = -1;
  while (r.frames < limit) {
    sink.advance();
    r.frames += sink.frames();
    if (p.hasSongHalted()) {
      r.halted = true;
      break;
    }
    mp_sint32 o, rw;
    p.getPosition(o, rw);
    if (track_order >= 0 && o == track_order && rw == track_row
        && !(prev_o == o && prev_r == rw)) {
      r.entries.push_back(r.frames);
    }
    prev_o = o;
    prev_r = rw;
  }
  p.getPosition(r.order, r.row);
  p.stopPlaying();
  return r;
}

static Plan plan(XModule &mod, long rate, mp_uint32 packet, long loops, uint64_t max_frames) {
  Plan pl = {max_frames, true};
  Pass a = run_pass(mod, rate, packet, false, max_frames, -1, -1);
  if (!a.halted)
    return pl;                                   /* plays on to the cap */
  /* Played again with repeat on: a song that stops halts there too (no
     fade); one that jumps back plays on — the entries into the position it
     halted at count its loops. */
  uint64_t limit = loops > 1 ? max_frames : a.frames + packet;
  Pass b = run_pass(mod, rate, packet, true, limit, a.order, a.row);
  if (b.halted && b.frames <= a.frames + packet) {
    pl.end = b.frames < a.frames ? b.frames : a.frames;
    pl.looped = false;
    return pl;
  }
  pl.end = a.frames;
  if (loops > 1) {
    size_t k = 0;
    while (k < b.entries.size() && b.entries[k] + packet < a.frames)
      k++;                                       /* the entry where pass a halted */
    if (k < b.entries.size()) {
      size_t last = k + (size_t)(loops - 1);
      pl.end = last < b.entries.size() ? a.frames + (b.entries[last] - b.entries[k])
                                       : max_frames;
    }
    if (b.halted && b.frames < pl.end) {         /* it stops before that */
      pl.end = b.frames;
      pl.looped = false;
    }
  }
  if (pl.end > max_frames)
    pl.end = max_frames;
  return pl;
}

static void put32(unsigned char *p, uint32_t v) {
  p[0] = v & 0xff; p[1] = (v >> 8) & 0xff; p[2] = (v >> 16) & 0xff; p[3] = (v >> 24) & 0xff;
}

static void put16(unsigned char *p, uint16_t v) {
  p[0] = v & 0xff; p[1] = (v >> 8) & 0xff;
}

static void wav_header(unsigned char *h, uint32_t rate, uint32_t data_bytes) {
  memcpy(h, "RIFF", 4);
  put32(h + 4, 36 + data_bytes);
  memcpy(h + 8, "WAVEfmt ", 8);
  put32(h + 16, 16);
  put16(h + 20, 1);              /* PCM */
  put16(h + 22, 2);              /* stereo */
  put32(h + 24, rate);
  put32(h + 28, rate * 4);       /* bytes per second */
  put16(h + 32, 4);              /* block align: 2 channels x 16 bits */
  put16(h + 34, 16);
  memcpy(h + 36, "data", 4);
  put32(h + 40, data_bytes);
}

static int g_stdout = -1;            /* the real stdout, once MilkyPlay's is stderr */

static int usage(void) {
  fprintf(stderr, "usage: milkyrender [-r rate] [-m max_seconds] [-f fade_ms] [-l loops] "
                  "FILE OUT.wav | --length FILE\n");
  return 2;
}

static int run(int argc, char **argv) {
  long rate = 48000, max_s = 3600, fade_ms = 100, loops = 1;
  int length_only = 0;
  if (argc == 2 && strcmp(argv[1], "--version") == 0) {
    printf("milkyrender %s (MilkyPlay %s)\n", MILKYRENDER_VERSION, MILKYPLAY_COMMIT);
    return 0;
  }
  /* What MilkyPlay prints goes to stderr: stdout carries the length alone. */
  fflush(stdout);
  g_stdout = dup(1);
  if (g_stdout >= 0)
    dup2(2, 1);
  const char *in = NULL, *out = NULL;
  for (int i = 1; i < argc; i++) {
    if (strcmp(argv[i], "--length") == 0) {
      length_only = 1;
    } else if (argv[i][0] == '-' && argv[i][1] && i + 1 < argc && strchr("rmfl", argv[i][1])
               && argv[i][2] == '\0') {
      char *end = NULL;
      long v = strtol(argv[i + 1], &end, 10);
      if (!end || *end)
        return usage();
      switch (argv[i][1]) {
        case 'r': rate = v; break;
        case 'm': max_s = v; break;
        case 'f': fade_ms = v; break;
        case 'l': loops = v; break;
      }
      i++;
    } else if (argv[i][0] == '-' && argv[i][1]) {
      return usage();
    } else if (!in) {
      in = argv[i];
    } else if (!out) {
      out = argv[i];
    } else {
      return usage();
    }
  }
  if (!in || (length_only ? out != NULL : out == NULL) || rate < 8000 || rate > 192000
      || max_s < 1 || max_s > 3600 || fade_ms < 0 || fade_ms > 60000 || loops < 1
      || loops > 100)
    return usage();

  FILE *fi = fopen(in, "rb");
  if (!fi) {
    fprintf(stderr, "milkyrender: can't open %s\n", in);
    return 1;
  }
  std::vector<unsigned char> data;
  unsigned char chunk[65536];
  size_t got;
  while ((got = fread(chunk, 1, sizeof chunk, fi)) > 0) {
    data.insert(data.end(), chunk, chunk + got);
    if (data.size() > 64u * 1024 * 1024)
      break;
  }
  int read_failed = ferror(fi);
  fclose(fi);
  if (read_failed) {
    fprintf(stderr, "milkyrender: read error: %s\n", in);
    return 1;
  }
  if (data.size() > 64u * 1024 * 1024) {
    fprintf(stderr, "milkyrender: not a DSIK v1 module (larger than 64 MB)\n");
    return 1;
  }
  if (data.size() < 4 || memcmp(&data[0], "DSM\x10", 4) != 0) {
    fprintf(stderr, "milkyrender: not a DSIK v1 module (no DSM\\x10 header)\n");
    return 1;
  }
  if (!dsm1_ok(&data[0], data.size())) {
    fprintf(stderr, "milkyrender: damaged DSIK module: %s\n", g_why);
    return 1;
  }
  if (g_missing)
    fprintf(stderr, "milkyrender: warning: truncated sample data (%llu bytes missing)\n",
            (unsigned long long)g_missing);

  XModule *mod = new XModule();
  MemFile mf(&data[0], (mp_uint32)data.size());
  mp_sint32 res = mod->loadModule(mf);
  if (res != MP_OK) {
    fprintf(stderr, "milkyrender: MilkyPlay can't load this module (error %d)\n", (int)res);
    delete mod;
    return 1;
  }

  const mp_uint32 packet = (mp_uint32)ChannelMixer::beatPacketsToBufferSize((mp_uint32)rate, 1);
  const uint64_t max_frames = (uint64_t)max_s * (uint64_t)rate;
  const uint64_t fade_frames = (uint64_t)fade_ms * (uint64_t)rate / 1000;
  Plan pl = plan(*mod, rate, packet, loops, max_frames);
  uint64_t total = pl.end;
  if (pl.looped && pl.end < max_frames)
    total = pl.end + fade_frames < max_frames ? pl.end + fade_frames : max_frames;
  const uint64_t fade_from = pl.looped ? (total > fade_frames ? total - fade_frames : 0) : total;

  if (length_only) {
    FILE *o = g_stdout >= 0 ? fdopen(g_stdout, "w") : NULL;
    int bad = !o || fprintf(o, "%.6f\n", (double)total / (double)rate) < 0 || fflush(o) != 0;
    delete mod;
    return bad ? 1 : 0;
  }

  FILE *fp = fopen(out, "wb");
  if (!fp) {
    fprintf(stderr, "milkyrender: could not create %s\n", out);
    delete mod;
    return 1;
  }
  unsigned char h[44];
  wav_header(h, (uint32_t)rate, 0);
  int failed = fwrite(h, 1, sizeof h, fp) != sizeof h || fflush(fp) != 0;

  Sink sink;
  PlayerGeneric *p = new PlayerGeneric((mp_sint32)rate, &sink);
  setup(*p, packet);
  p->startPlaying(mod, true);
  p->setRamp(true);              /* volume ramp-in: MilkyPlay's default, on the new player */
  std::vector<unsigned char> le((size_t)packet * 4);
  const uint64_t flush_every = (uint64_t)rate / 10;
  uint64_t frames = 0, unflushed = 0;
  while (!failed && frames < total) {
    sink.advance();
    const mp_sword *s = sink.block();
    uint64_t n = sink.frames();
    if (n > total - frames)
      n = total - frames;
    for (uint64_t f = 0; f < n; f++) {
      uint64_t at = frames + f;
      for (int c = 0; c < 2; c++) {
        int v = s[2 * f + c];
        if (at >= fade_from && fade_frames)
          v = (int)((int64_t)v * (int64_t)(total - at) / (int64_t)fade_frames);
        put16(&le[4 * f + 2 * c], (uint16_t)(int16_t)v);
      }
    }
    if (fwrite(&le[0], 1, (size_t)n * 4, fp) != (size_t)n * 4) {
      failed = 1;                              /* a full disk, a closed pipe */
      break;
    }
    frames += n;
    unflushed += n;
    if (unflushed >= flush_every) {
      if (fflush(fp) != 0)
        failed = 1;
      unflushed = 0;
    }
    if (p->hasSongHalted())
      break;                                   /* it stopped (the packet it did is in) */
  }
  p->stopPlaying();
  delete p;

  uint64_t bytes = frames * 4;
  wav_header(h, (uint32_t)rate, (uint32_t)bytes);
  if (fseek(fp, 0, SEEK_SET) != 0 || fwrite(h, 1, sizeof h, fp) != sizeof h)
    failed = 1;
  if (fclose(fp) != 0)
    failed = 1;
  if (failed)
    fprintf(stderr, "milkyrender: write error: %s\n", out);
  else if (!frames)
    fprintf(stderr, "milkyrender: the module plays nothing\n");
  delete mod;
  return failed || !frames ? 1 : 0;
}

int main(int argc, char **argv) {
  try {
    return run(argc, argv);
  } catch (const std::bad_alloc &) {
    fprintf(stderr, "milkyrender: out of memory\n");
    return 1;
  }
}
