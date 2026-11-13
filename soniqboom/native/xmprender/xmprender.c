/* SPDX-FileCopyrightText: 2026 S.F. Cyris
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * xmprender — render a module that libxmp (MIT) plays to a 16-bit stereo
 * WAV: the tracker formats no other SoniqBoom engine reads (Archimedes
 * Tracker, Coconizer, FlexTrax, Funktracker, Liquid Tracker, Megatracker)
 * and the packed Amiga modules of libxmp's ProWizard loaders.
 *
 *   xmprender [-s sequence] [-r rate] [-m max_seconds] [-f fade_ms] [-l loops]
 *             FILE OUT.wav
 *   xmprender -i [-s sequence] [-r rate] [-m max_seconds] [-f fade_ms]
 *             [-l loops] FILE
 *   xmprender --version
 *
 * The sequence (a subsong; default 0) plays until it has looped `loops` times
 * (default 1) — its end, for a module that doesn't loop — or for
 * `max_seconds` (default 1800), whose last `fade_ms` (default 8000) fade
 * out.  Mixing: libxmp's defaults (full stereo separation) with cubic-spline
 * interpolation.  The WAV is written as it renders (a 44-byte header first,
 * its sizes patched at the end), flushed after every libxmp frame, so a
 * reader can follow it.
 * -i plays the sequence the same way without writing it and prints
 * "seconds=", "sequences=", "channels=", "format=" (libxmp's name for the
 * format) and "title=" — the length is the one a render with the same
 * options has (the playback is deterministic).  Exit status 0 with audio
 * written (-i: with the sequence played), 1 on a file libxmp can't load, an
 * empty module, a sequence that plays nothing or a failed write (a partial
 * WAV may be left), 2 on usage. */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "xmp.h"

#ifndef XMPRENDER_VERSION
#define XMPRENDER_VERSION "1.0"
#endif

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

/* One line of text with no control characters (a title may hold any byte). */
static void print_clean(const char *key, const char *s) {
  printf("%s=", key);
  for (; s && *s; s++)
    putchar((unsigned char)*s < 0x20 || *s == 0x7f ? ' ' : *s);
  putchar('\n');
}

static int usage(void) {
  fprintf(stderr, "usage: xmprender [-s sequence] [-r rate] [-m max_seconds] [-f fade_ms] "
                  "[-l loops] FILE OUT.wav\n"
                  "       xmprender -i [same options] FILE\n"
                  "       xmprender --version\n");
  return 2;
}

int main(int argc, char **argv) {
  long seq = 0, rate = 44100, max_s = 1800, fade_ms = 8000, loops = 1;
  if (argc == 2 && strcmp(argv[1], "--version") == 0) {
    printf("xmprender %s (libxmp %s)\n", XMPRENDER_VERSION, xmp_version);
    return 0;
  }
  const char *in = NULL, *out = NULL;
  int info = 0;
  for (int i = 1; i < argc; i++) {
    if (strcmp(argv[i], "-i") == 0) {
      info = 1;
    } else if (argv[i][0] == '-' && argv[i][1] && argv[i][2] == '\0' && i + 1 < argc
        && strchr("srmfl", argv[i][1])) {
      char *end;
      long v = strtol(argv[i + 1], &end, 10);
      if (*argv[i + 1] == '\0' || *end != '\0')
        return usage();
      switch (argv[i][1]) {
        case 's': seq = v; break;
        case 'r': rate = v; break;
        case 'm': max_s = v; break;
        case 'f': fade_ms = v; break;
        case 'l': loops = v; break;
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
  if (!in || (!info && !out) || (info && out) || seq < 0 || rate < 8000 || rate > 48000
      || max_s < 1 || max_s > 7200 || fade_ms < 0 || fade_ms > 60000 || loops < 1 || loops > 16)
    return usage();

  xmp_context ctx = xmp_create_context();
  if (!ctx) {
    fprintf(stderr, "xmprender: out of memory\n");
    return 1;
  }
  if (xmp_load_module(ctx, (char *)in) != 0) {
    fprintf(stderr, "xmprender: not a module libxmp plays: %s\n", in);
    xmp_free_context(ctx);
    return 1;
  }
  struct xmp_module_info mi;
  xmp_get_module_info(ctx, &mi);
  if (!mi.mod || mi.mod->len < 1 || mi.num_sequences < 1 || seq >= mi.num_sequences) {
    fprintf(stderr, "xmprender: %s: %s\n", in,
            seq > 0 && mi.mod && seq >= mi.num_sequences ? "no such sequence" : "an empty module");
    xmp_release_module(ctx);
    xmp_free_context(ctx);
    return 1;
  }
  if (xmp_start_player(ctx, (int)rate, 0) != 0) {
    fprintf(stderr, "xmprender: can't play %s\n", in);
    xmp_release_module(ctx);
    xmp_free_context(ctx);
    return 1;
  }
  xmp_set_player(ctx, XMP_PLAYER_INTERP, XMP_INTERP_SPLINE);
  if (seq > 0)
    xmp_set_position(ctx, mi.seq_data[seq].entry_point);

  FILE *fp = info ? NULL : fopen(out, "wb");
  int failed = 0;
  if (!info && !fp) {
    fprintf(stderr, "xmprender: can't write %s\n", out);
    failed = 1;
  }
  unsigned char h[44];
  wav_header(h, (uint32_t)rate, 0);
  if (fp && (fwrite(h, 1, sizeof h, fp) != sizeof h || fflush(fp) != 0))
    failed = 1;

  uint64_t frames = 0, max_frames = (uint64_t)max_s * (uint64_t)rate;
  uint64_t fade_frames = (uint64_t)fade_ms * (uint64_t)rate / 1000;
  if (fade_frames > max_frames)
    fade_frames = max_frames;
  uint64_t fade_at = max_frames - fade_frames;
  unsigned char *le = NULL;
  size_t le_size = 0;
  struct xmp_frame_info fi;
  while (!failed && frames < max_frames) {
    if (xmp_play_frame(ctx) != 0)
      break;                                   /* the module ended */
    xmp_get_frame_info(ctx, &fi);
    if (fi.loop_count >= loops)
      break;
    uint64_t n = (uint64_t)fi.buffer_size / 4;   /* stereo 16-bit frames */
    if (frames + n > max_frames)
      n = max_frames - frames;
    if (fp) {
      if (le_size < n * 4) {
        free(le);
        le_size = n * 4;
        le = malloc(le_size);
        if (!le) {
          fprintf(stderr, "xmprender: out of memory\n");
          failed = 1;
          break;
        }
      }
      const int16_t *s = (const int16_t *)fi.buffer;
      for (uint64_t k = 0; k < n; k++) {
        uint64_t at = frames + k;
        int32_t l = s[2 * k], r = s[2 * k + 1];
        if (at >= fade_at && fade_frames > 0) {
          /* (the faded part: linear, to silence at max_seconds) */
          double g = (double)(max_frames - at) / (double)fade_frames;
          l = (int32_t)(l * g);
          r = (int32_t)(r * g);
        }
        put16(le + 4 * k, (uint16_t)(int16_t)l);
        put16(le + 4 * k + 2, (uint16_t)(int16_t)r);
      }
      if (fwrite(le, 1, (size_t)(n * 4), fp) != (size_t)(n * 4) || fflush(fp) != 0) {
        failed = 1;                              /* a full disk, a closed pipe */
        break;
      }
    }
    frames += n;
  }
  if (!failed && frames == 0) {
    fprintf(stderr, "xmprender: %s: nothing to play\n", in);
    failed = 1;
  }

  if (info) {
    if (!failed) {
      printf("seconds=%.3f\n", (double)frames / (double)rate);
      printf("sequences=%d\n", mi.num_sequences);
      printf("channels=%d\n", mi.mod->chn);
      print_clean("format", mi.mod->type);
      print_clean("title", mi.mod->name);
    }
  } else if (fp) {
    uint64_t bytes = frames * 4;
    wav_header(h, (uint32_t)rate, (uint32_t)bytes);
    if (!failed && (fseek(fp, 0, SEEK_SET) != 0 || fwrite(h, 1, sizeof h, fp) != sizeof h))
      failed = 1;
    if (fclose(fp) != 0)
      failed = 1;
    if (failed)
      fprintf(stderr, "xmprender: can't write %s\n", out);
  }
  free(le);
  xmp_end_player(ctx);
  xmp_release_module(ctx);
  xmp_free_context(ctx);
  return failed ? 1 : 0;
}
