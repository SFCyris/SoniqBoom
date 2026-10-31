/* SPDX-FileCopyrightText: 2026 S.F. Cyris
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * kssrender — render one song of an MSX / Sega Master System KSS file
 * (KSCC, KSSX: PSG, SCC, OPLL, MSX-AUDIO, SN76489) to a 16-bit stereo WAV,
 * with libkss (Mitsutaka Okazaki, ISC; its emu* chip cores MIT, kmz80 public
 * domain).  Built against libkss without its kss-drivers (EXCLUDE_DRIVER_ALL):
 * a KSS file carries its own driver.
 *
 *   kssrender [-s song] [-r rate] [-m max_seconds] [-f fade_ms] [-l loops]
 *             [-q quality] FILE OUT.wav
 *   kssrender --version
 *
 * The song plays until it has looped `loops` times (default 1) or stopped,
 * or for `max_seconds` (default 180) — then fades over `fade_ms` (default
 * 8000).  A song that falls silent for 3 s stops there.  The WAV is written as
 * it renders (a 44-byte header first, its sizes patched at the end), in
 * 0.1-second blocks flushed as they are made, so a reader can follow it.
 * Exit status 0 with audio written, 1 on a bad file, 2 on usage. */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "kssplay.h"

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

static int usage(void) {
  fprintf(stderr, "usage: kssrender [-s song] [-r rate] [-m max_seconds] [-f fade_ms] "
                  "[-l loops] [-q quality] FILE OUT.wav\n");
  return 2;
}

#ifndef KSSRENDER_VERSION
#define KSSRENDER_VERSION "1.1"   /* 1.1: write / allocation failures exit 1 */
#endif
#ifndef LIBKSS_COMMIT
#define LIBKSS_COMMIT "unknown"
#endif

int main(int argc, char **argv) {
  long song = 0, rate = 44100, max_s = 180, fade_ms = 8000, loops = 1, quality = 1;
  if (argc == 2 && strcmp(argv[1], "--version") == 0) {
    printf("kssrender %s (libkss %s)\n", KSSRENDER_VERSION, LIBKSS_COMMIT);
    return 0;
  }
  const char *in = NULL, *out = NULL;
  for (int i = 1; i < argc; i++) {
    if (argv[i][0] == '-' && argv[i][1] && i + 1 < argc && strchr("srmflq", argv[i][1])
        && argv[i][2] == '\0') {
      long v = strtol(argv[i + 1], NULL, 10);
      switch (argv[i][1]) {
        case 's': song = v; break;
        case 'r': rate = v; break;
        case 'm': max_s = v; break;
        case 'f': fade_ms = v; break;
        case 'l': loops = v; break;
        case 'q': quality = v; break;
      }
      i++;
    } else if (!in) {
      in = argv[i];
    } else if (!out) {
      out = argv[i];
    } else {
      return usage();
    }
  }
  if (!in || !out || song < 0 || song > 255 || rate < 8000 || rate > 192000
      || max_s < 1 || max_s > 3600 || fade_ms < 0 || loops < 1)
    return usage();

  KSS *kss = KSS_load_file((char *)in);
  if (!kss) {
    fprintf(stderr, "kssrender: not a KSS file this build can play: %s\n", in);
    return 1;
  }
  FILE *fp = fopen(out, "wb");
  if (!fp) {
    fprintf(stderr, "kssrender: can't write %s\n", out);
    KSS_delete(kss);
    return 1;
  }
  unsigned char h[44];
  wav_header(h, (uint32_t)rate, 0);
  int failed = fwrite(h, 1, sizeof h, fp) != sizeof h || fflush(fp) != 0;

  KSSPLAY *kp = KSSPLAY_new((uint32_t)rate, 2, 16);
  if (!kp) {
    fprintf(stderr, "kssrender: out of memory\n");
    fclose(fp);
    KSS_delete(kss);
    return 1;
  }
  KSSPLAY_set_data(kp, kss);
  KSSPLAY_reset(kp, (uint32_t)song, 0);
  KSSPLAY_set_device_quality(kp, KSS_DEVICE_PSG, (uint32_t)quality);
  KSSPLAY_set_device_quality(kp, KSS_DEVICE_SCC, (uint32_t)quality);
  KSSPLAY_set_device_quality(kp, KSS_DEVICE_OPLL, (uint32_t)quality);
  KSSPLAY_set_device_quality(kp, KSS_DEVICE_OPL, (uint32_t)quality);
  /* The stereo image kss2wav gives: PSG left of centre, SCC right, the
     OPLL's channels alternating. */
  KSSPLAY_set_device_pan(kp, KSS_DEVICE_PSG, -32);
  KSSPLAY_set_device_pan(kp, KSS_DEVICE_SCC, 32);
  kp->opll_stereo = 1;
  for (uint32_t ch = 0; ch < 6; ch++)
    KSSPLAY_set_channel_pan(kp, KSS_DEVICE_OPLL, ch, (ch & 1) ? 2 : 1);
  KSSPLAY_set_silent_limit(kp, 3000);

  const uint32_t block = (uint32_t)rate / 10;              /* 0.1 s */
  int16_t *buf = malloc(sizeof(int16_t) * 2 * block);
  unsigned char *le = malloc(4 * block);
  if (!buf || !le) {
    fprintf(stderr, "kssrender: out of memory\n");
    failed = 1;
  }
  uint64_t frames = 0, max_frames = (uint64_t)max_s * (uint64_t)rate;
  uint64_t fade_frames = (uint64_t)fade_ms * (uint64_t)rate / 1000;
  while (!failed && frames < max_frames) {
    KSSPLAY_calc(kp, buf, block);
    for (uint32_t i = 0; i < 2 * block; i++)
      put16(le + 2 * i, (uint16_t)buf[i]);
    if (fwrite(le, 1, 4 * block, fp) != 4 * block || fflush(fp) != 0) {
      failed = 1;                                 /* a full disk, a closed pipe */
      break;
    }
    frames += block;
    if (KSSPLAY_get_fade_flag(kp) == KSSPLAY_FADE_NONE
        && (KSSPLAY_get_loop_count(kp) >= loops
            || frames + fade_frames >= max_frames)) {
      if (fade_frames == 0)
        break;
      KSSPLAY_fade_start(kp, (uint32_t)fade_ms);
    }
    if (KSSPLAY_get_fade_flag(kp) == KSSPLAY_FADE_END || KSSPLAY_get_stop_flag(kp))
      break;
  }

  uint64_t bytes = frames * 4;
  wav_header(h, (uint32_t)rate, (uint32_t)bytes);
  if (fseek(fp, 0, SEEK_SET) != 0 || fwrite(h, 1, sizeof h, fp) != sizeof h)
    failed = 1;
  if (fclose(fp) != 0)
    failed = 1;
  if (failed)
    fprintf(stderr, "kssrender: can't write %s\n", out);
  free(le);
  free(buf);
  KSSPLAY_delete(kp);
  KSS_delete(kss);
  return failed ? 1 : 0;
}
