/* SPDX-FileCopyrightText: 2026 S.F. Cyris
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * mdxrender — render a Sharp X68000 MDX song (with the PDX ADPCM sample bank
 * it names) to a 16-bit stereo WAV, with portable_mdx (Yosshin): the MXDRV
 * 2.06+17 driver (milk., K.MAEKAWA, Missy.M, Yatsube; GORRY's MXDRVg C
 * conversion) driving m_puusan's X68Sound OPM + ADPCM emulation.
 *
 *   mdxrender [-r rate] [-l loops] [-m max_seconds] [-f fade_ms] [-p PDX]
 *             FILE OUT.wav
 *   mdxrender -i [-l loops] FILE
 *   mdxrender --version
 *
 * Length: MXDRV's own measure (MXDRV_MeasurePlayTime2) — the song plays
 * until it has looped `loops` times (default 2), then fades out with MXDRV's
 * fade-out; a song that doesn't loop plays to its end; 2 s are added for the
 * notes' release.  The render has exactly that length: the fade-out starts at
 * the same loop as in the measure (the same OPM-interrupt test).  A song
 * longer than `max_seconds` (default 180) is cut there, its last `fade_ms`
 * (default 8000) faded out.
 *
 * PDX: the file the MDX names (".PDX" added when the name has none; a DOS
 * path in it dropped, split outside Shift-JIS two-byte characters), looked
 * up beside FILE as spelled, else in any letter case — by the song's own
 * (Shift-JIS) spelling, then by that name in UTF-8 —, or given with -p.  -i
 * prints "seconds=<length>", "pdx=<name, as its bytes, or empty>" and
 * "pdx_found=0|1" and renders nothing (it reads no PDX: the length doesn't
 * depend on the samples).
 *
 * The WAV is written as it renders (a 44-byte header first, its sizes
 * patched at the end), in 0.1-second blocks flushed as they are made, so a
 * reader can follow it.  Exit status 0 with audio written, 1 on a bad file or
 * a failed write, 2 on usage, 3 when the PDX the song needs isn't found. */

#include <dirent.h>
#include <iconv.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include <mdx_util.h>
#include <mxdrv.h>
#include <mxdrv_context.h>

#ifndef MDXRENDER_VERSION
#define MDXRENDER_VERSION "1.1"   /* 1.1: Shift-JIS PDX names, big PDX files, -i reads no PDX */
#endif
#ifndef PORTABLE_MDX_COMMIT
#define PORTABLE_MDX_COMMIT "unknown"
#endif

static const uint32_t MAX_FILE = 64u * 1024 * 1024;

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
  fprintf(stderr, "usage: mdxrender [-r rate] [-l loops] [-m max_seconds] [-f fade_ms] "
                  "[-p PDX] FILE OUT.wav\n"
                  "       mdxrender -i [-l loops] FILE\n");
  return 2;
}

/* The whole file, or NULL (missing, unreadable, empty or over MAX_FILE). */
static void *read_file(const char *path, uint32_t *size) {
  FILE *fp = fopen(path, "rb");
  if (!fp) return NULL;
  void *buf = NULL;
  long n = -1;
  if (fseek(fp, 0, SEEK_END) == 0 && (n = ftell(fp)) > 0 && (unsigned long)n <= MAX_FILE
      && fseek(fp, 0, SEEK_SET) == 0) {
    buf = malloc((size_t)n);
    if (buf && fread(buf, 1, (size_t)n, fp) != (size_t)n) {
      free(buf);
      buf = NULL;
    }
  }
  fclose(fp);
  if (buf) *size = (uint32_t)n;
  return buf;
}

/* `name` (Shift-JIS, as an MDX song spells it) in UTF-8 — the name the file
   has on a disk that stores UTF-8 names; malloc'd, or NULL when it can't be
   converted or is the same. */
static char *sjis_to_utf8(const char *name) {
  iconv_t cd = iconv_open("UTF-8", "CP932");
  if (cd == (iconv_t)-1) cd = iconv_open("UTF-8", "SHIFT_JIS");
  if (cd == (iconv_t)-1) return NULL;
  size_t in_left = strlen(name), out_size = in_left * 3 + 1, out_left = out_size - 1;
  char *out = (char *)calloc(out_size, 1);
  char *in = (char *)name, *o = out;
  if (!out || iconv(cd, &in, &in_left, &o, &out_left) == (size_t)-1 || in_left != 0
      || strcmp(out, name) == 0) {
    free(out);
    out = NULL;
  }
  iconv_close(cd);
  return out;
}

/* `name`'s path beside `file`: as spelled when that file exists, else
   matched in any letter case; malloc'd, or NULL. */
static char *find_name_beside(const char *file, const char *name) {
  const char *slash = strrchr(file, '/');
  size_t dlen = slash ? (size_t)(slash - file) : 0;
  char *dir = (char *)malloc(dlen + 2);
  if (!dir) return NULL;
  if (slash) { memcpy(dir, file, dlen); dir[dlen] = '\0'; if (!dlen) strcpy(dir, "/"); }
  else strcpy(dir, ".");
  char *found = (char *)malloc(strlen(dir) + strlen(name) + 2);
  if (found) {
    sprintf(found, "%s/%s", dir, name);
    FILE *fp = fopen(found, "rb");
    if (fp) {
      fclose(fp);
      free(dir);
      return found;
    }
    free(found);
    found = NULL;
  }
  DIR *d = opendir(dir);
  if (d) {
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
      if (strcasecmp(e->d_name, name) == 0) {
        found = (char *)malloc(strlen(dir) + strlen(e->d_name) + 2);
        if (found) sprintf(found, "%s/%s", dir, e->d_name);
        break;
      }
    }
    closedir(d);
  }
  free(dir);
  return found;
}

/* The PDX file `name` beside `file`: by the song's own spelling, else by that
   name in UTF-8 (``sjis_to_utf8``); malloc'd, or NULL. */
static char *find_beside(const char *file, const char *name) {
  char *found = find_name_beside(file, name);
  if (!found) {
    char *utf8 = sjis_to_utf8(name);
    if (utf8) found = find_name_beside(file, utf8);
    free(utf8);
  }
  return found;
}

/* The OPM-interrupt test of MXDRV_MeasurePlayTime2, for the real render:
   once the song has looped `g_loops` times, MXDRV's fade-out starts. */
static int g_loops = 2;
static bool g_fading = false;
static volatile MXWORK_GLOBAL *g_work = NULL;

static void on_opmint(MxdrvContext *context) {
  if (g_fading || g_work == NULL) return;
  uint16_t loops = g_work->L002246;
  if (loops != 65535 && loops >= g_loops) {
    g_fading = true;
    MXDRV_Fadeout(context);
  }
}

int main(int argc, char **argv) {
  long rate = 44100, max_s = 180, fade_ms = 8000, loops = 2;
  int info = 0;
  const char *in = NULL, *out = NULL, *pdx_arg = NULL;
  if (argc == 2 && strcmp(argv[1], "--version") == 0) {
    printf("mdxrender %s (portable_mdx %s)\n", MDXRENDER_VERSION, PORTABLE_MDX_COMMIT);
    return 0;
  }
  for (int i = 1; i < argc; i++) {
    if (strcmp(argv[i], "-i") == 0) {
      info = 1;
    } else if (argv[i][0] == '-' && argv[i][1] && argv[i][2] == '\0'
               && strchr("rlmfp", argv[i][1]) && i + 1 < argc) {
      if (argv[i][1] == 'p') {
        pdx_arg = argv[i + 1];
      } else {
        char *end = NULL;
        long v = strtol(argv[i + 1], &end, 10);
        if (!end || *end != '\0') return usage();
        switch (argv[i][1]) {
          case 'r': rate = v; break;
          case 'l': loops = v; break;
          case 'm': max_s = v; break;
          case 'f': fade_ms = v; break;
        }
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
  if (!in || (!info && !out) || (info && out)
      || (rate != 44100 && rate != 48000 && rate != 96000)
      || loops < 1 || loops > 16 || max_s < 1 || max_s > 3600
      || fade_ms < 0 || fade_ms > 60000)
    return usage();

  uint32_t mdx_size = 0;
  void *mdx = read_file(in, &mdx_size);
  char title[256];
  if (!mdx || !MdxGetTitle(mdx, mdx_size, title, sizeof title)) {
    fprintf(stderr, "mdxrender: not an MDX file this build can play: %s\n", in);
    free(mdx);
    return 1;
  }
  bool has_pdx = false;
  char pdx_name[FILENAME_MAX] = {0};
  if (!MdxHasPdxFileName(mdx, mdx_size, &has_pdx)) {
    fprintf(stderr, "mdxrender: not an MDX file this build can play: %s\n", in);
    free(mdx);
    return 1;
  }
  if (has_pdx && !MdxGetPdxFileName(mdx, mdx_size, pdx_name, sizeof pdx_name))
    has_pdx = false;
  /* (a DOS path in the name: its file name — split outside Shift-JIS
     two-byte characters, whose second byte can be '\\') */
  const char *pdx_base = pdx_name;
  for (const char *p = pdx_name; *p; p++) {
    unsigned char c = (unsigned char)*p;
    if (((c >= 0x81 && c <= 0x9F) || (c >= 0xE0 && c <= 0xFC)) && p[1]) {
      p++;
      continue;
    }
    if (c == '\\' || c == '/' || c == ':') pdx_base = p + 1;
  }
  if (has_pdx && !*pdx_base) has_pdx = false;

  void *pdx = NULL;
  uint32_t pdx_size = 0;
  bool pdx_found = false;
  if (has_pdx) {
    char *pdx_path = pdx_arg ? strdup(pdx_arg) : find_beside(in, pdx_base);
    if (info) {
      /* (the length is measured without the samples: only whether it's there) */
      FILE *fp = pdx_path ? fopen(pdx_path, "rb") : NULL;
      pdx_found = fp != NULL;
      if (fp) fclose(fp);
    } else if (pdx_path) {
      pdx = read_file(pdx_path, &pdx_size);
      pdx_found = pdx != NULL;
      if (!pdx) {
        fprintf(stderr, "mdxrender: read error: %s\n", pdx_path);  /* (too big, unreadable) */
        free(pdx_path);
        free(mdx);
        return 1;
      }
    }
    free(pdx_path);
  }
  if (!info && has_pdx && !pdx) {
    fprintf(stderr, "mdxrender: PDX not found: %s\n", pdx_base);
    free(mdx);
    return 3;
  }

  uint32_t mdx_buf_size = 0, pdx_buf_size = 0;
  if (!MdxGetRequiredBufferSize(mdx, mdx_size, pdx_size, &mdx_buf_size, &pdx_buf_size)) {
    fprintf(stderr, "mdxrender: not an MDX file this build can play: %s\n", in);
    free(mdx); free(pdx);
    return 1;
  }
  /* (a song that names a PDX gets a PDX buffer even when the file is
     missing — -i — as portable_mdx's own mdx2wav does) */
  void *mdx_buf = malloc(mdx_buf_size ? mdx_buf_size : 1);
  void *pdx_buf = has_pdx ? malloc(pdx_buf_size ? pdx_buf_size : 1) : NULL;
  if (!mdx_buf || (has_pdx && !pdx_buf)
      || !MdxUtilCreateMdxPdxBuffer(mdx, mdx_size, pdx, pdx_size,
                                    mdx_buf, mdx_buf_size, pdx_buf, pdx_buf_size)) {
    fprintf(stderr, "mdxrender: not an MDX file this build can play: %s\n", in);
    free(mdx); free(pdx); free(mdx_buf); free(pdx_buf);
    return 1;
  }
  free(mdx);
  free(pdx);
  if (!pdx_buf) pdx_buf_size = 0;

  const uint32_t mdx_area = mdx_buf_size > (1u << 20) ? mdx_buf_size : (1u << 20);
  const uint32_t pdx_area = pdx_buf_size > (2u << 20) ? pdx_buf_size : (2u << 20);
  /* (MXDRV_SetData2 copies the song and the samples into the pool again) */
  const uint64_t pool = (uint64_t)mdx_area + pdx_area + mdx_buf_size + pdx_buf_size + (4u << 20);
  MxdrvContext context;
  if (pool > 0x7fffffffu || !MxdrvContext_Initialize(&context, (int)pool)
      || MXDRV_Start(&context, (int)rate, 0, 0, 0, (int)mdx_area, (int)pdx_area, 0) != 0) {
    fprintf(stderr, "mdxrender: out of memory\n");
    free(mdx_buf); free(pdx_buf);
    return 1;
  }
  MXDRV_PCM8Enable(&context, 1);
  MXDRV_TotalVolume(&context, 256);
  const int set = MXDRV_SetData2(&context, mdx_buf, mdx_buf_size, pdx_buf, pdx_buf_size);
  if (set != 0) {
    if (set == MXDRV_ERR_MEMORY)
      fprintf(stderr, "mdxrender: out of memory\n");
    else
      fprintf(stderr, "mdxrender: not an MDX file this build can play: %s\n", in);
    MXDRV_End(&context);
    MxdrvContext_Terminate(&context);
    free(mdx_buf); free(pdx_buf);
    return 1;
  }
  const uint32_t ms = MXDRV_MeasurePlayTime2(&context, (int)loops, 1);
  if (info) {
    printf("seconds=%.3f\npdx=%s\npdx_found=%d\n", ms / 1000.0, has_pdx ? pdx_base : "",
           pdx_found ? 1 : 0);
    MXDRV_End(&context);
    MxdrvContext_Terminate(&context);
    free(mdx_buf); free(pdx_buf);
    return 0;
  }

  FILE *fp = fopen(out, "wb");
  if (!fp) {
    fprintf(stderr, "mdxrender: can't write %s\n", out);
    MXDRV_End(&context);
    MxdrvContext_Terminate(&context);
    free(mdx_buf); free(pdx_buf);
    return 1;
  }
  unsigned char h[44];
  wav_header(h, (uint32_t)rate, 0);
  int failed = fwrite(h, 1, sizeof h, fp) != sizeof h || fflush(fp) != 0;

  uint64_t total = (uint64_t)ms * (uint64_t)rate / 1000;
  const uint64_t cap = (uint64_t)max_s * (uint64_t)rate;
  uint64_t fade_frames = 0;
  if (total > cap) {
    total = cap;
    fade_frames = (uint64_t)fade_ms * (uint64_t)rate / 1000;
    if (fade_frames > total) fade_frames = total;
  }
  g_loops = (int)loops;
  g_fading = false;
  g_work = (volatile MXWORK_GLOBAL *)MXDRV_GetWork(&context, MXDRV_WORK_GLOBAL);
  *(void (**)(MxdrvContext *))MXDRV_GetWork(&context, MXDRV_CALLBACK_OPMINT) = on_opmint;
  MXDRV_Play2(&context);

  const uint32_t block = (uint32_t)rate / 10;              /* 0.1 s */
  int16_t *buf = (int16_t *)malloc(sizeof(int16_t) * 2 * block);
  unsigned char *le = (unsigned char *)malloc(4 * block);
  if (!buf || !le) {
    fprintf(stderr, "mdxrender: out of memory\n");
    failed = 1;
  }
  uint64_t frames = 0;
  const uint64_t fade_from = total - fade_frames;
  while (!failed && frames < total) {
    uint32_t n = (uint32_t)((total - frames) < block ? (total - frames) : block);
    MXDRV_GetPCM(&context, buf, (int)n);
    for (uint32_t i = 0; i < n; i++) {
      uint64_t f = frames + i;
      for (int c = 0; c < 2; c++) {
        int32_t s = buf[2 * i + c];
        if (fade_frames && f >= fade_from)
          s = (int32_t)((int64_t)s * (int64_t)(total - f) / (int64_t)fade_frames);
        put16(le + 4 * i + 2 * c, (uint16_t)(int16_t)s);
      }
    }
    if (fwrite(le, 1, 4 * (size_t)n, fp) != 4 * (size_t)n || fflush(fp) != 0) {
      failed = 1;                                 /* a full disk, a closed pipe */
      break;
    }
    frames += n;
  }

  uint64_t bytes = frames * 4;
  wav_header(h, (uint32_t)rate, (uint32_t)bytes);
  if (fseek(fp, 0, SEEK_SET) != 0 || fwrite(h, 1, sizeof h, fp) != sizeof h)
    failed = 1;
  if (fclose(fp) != 0)
    failed = 1;
  if (failed)
    fprintf(stderr, "mdxrender: can't write %s\n", out);
  free(le);
  free(buf);
  MXDRV_End(&context);
  MxdrvContext_Terminate(&context);
  free(mdx_buf);
  free(pdx_buf);
  return failed ? 1 : 0;
}
