/* SPDX-FileCopyrightText: 2026 S.F. Cyris
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * dsfrender — render a Sega Dreamcast / Naomi DSF rip (a `.dsf`, or a
 * `.minidsf` with the `.dsflib` libraries its `_lib` tags name) to a 16-bit
 * stereo 44.1 kHz WAV, with Highly Theoretical (the AICA sound chip and its
 * ARM7 CPU; GPL-3.0) and psflib (Christopher Snowhill, MIT).
 *
 *   dsfrender [-r rate] [-m max_seconds] [-f fade_ms] FILE OUT.wav
 *   dsfrender --version
 *
 * The rip plays for `max_seconds` (decimals allowed; at most 3600 — a value
 * at most a microsecond over is 3600) and fades out over its
 * last `fade_ms` milliseconds (a linear fade: frame k of an n-frame fade is
 * scaled by (n - k) / n).  Without -m it plays its `length` tag plus its
 * `fade` tag (PSF v1.4 times: ss, ss.ddd, mm:ss, h:mm:ss.ddd; a length over
 * an hour is no length; the fade is clamped to 60 s), and 180 s when it
 * states no length; without -f it fades over its `fade` tag (none when it has
 * none).  Each section is loaded into the 8 MB of sound RAM a Naomi board
 * has.  `rate` must be 44100, the AICA's own output rate.
 *
 * A library is looked up relative to FILE's folder: its path as written ("/"
 * or "\" separated), then its file name in any case, then a file of that
 * name beside FILE.  A ".." component or a leading separator in a library
 * path is not followed (only the file name beside FILE is then tried).
 *
 * The WAV is written as it renders (a 44-byte header first, its sizes patched
 * at the end), in 0.1-second blocks flushed as they are made, so a reader can
 * follow it.  Exit status 0 with audio written, 1 on a file it can't play (or
 * a write failure), 2 on usage. */

#include <ctype.h>
#include <dirent.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>

#include "psflib.h"
#include "sega.h"

#ifndef DSFRENDER_VERSION
#define DSFRENDER_VERSION "1.1"
#endif
#ifndef HT_COMMIT
#define HT_COMMIT "unknown"
#endif
#ifndef PSFLIB_COMMIT
#define PSFLIB_COMMIT "unknown"
#endif

#define AICA_RATE 44100
#define SOUND_RAM_END 0x800000u     /* Naomi: 8 MB of AICA RAM */
#define DEFAULT_SECONDS 180.0       /* a rip stating no length */
#define MAX_SECONDS 3600.0
#define MAX_FADE_SECONDS 60.0
#define OVER_MAX_SECONDS 1e-6       /* a -m / -f this far over the hour is the hour */

/* ── WAV ──────────────────────────────────────────────────────────────── */

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

/* ── Tags ─────────────────────────────────────────────────────────────── */

/* One ":"-separated field of a PSF time: a number (`whole`: a whole one),
   -1e300 when it isn't one. */
static double time_field(const char *s, size_t n, int whole) {
  char f[32];
  if (!n || n >= sizeof f) return -1e300;
  memcpy(f, s, n);
  f[n] = '\0';
  char *end;
  double x = whole ? (double)strtol(f, &end, 10) : strtod(f, &end);
  while (*end == ' ' || *end == '\t') end++;
  if (end == f || *end || !isfinite(x)) return -1e300;
  return x;
}

/* A PSF time ("ss", "ss.ddd", "mm:ss", "h:mm:ss.ddd") in seconds, its last
   three fields read (seconds, whole minutes, whole hours); -1 when it doesn't
   read as one.  Only its first line counts. */
static double psf_time(const char *v) {
  size_t end = strcspn(v, "\r\n");
  double secs = 0;
  for (int k = 0; k < 3; k++) {             /* seconds, then minutes, then hours */
    size_t start = end;
    while (start > 0 && v[start - 1] != ':') start--;
    double x = time_field(v + start, end - start, k > 0);
    if (x == -1e300) return -1;
    secs += k == 0 ? x : x * (k == 1 ? 60 : 3600);
    if (start == 0) break;
    end = start - 1;
  }
  return secs < 0 ? 0 : secs;
}

struct tags {
  const char *length, *fade;   /* the first `length` / `fade` of the tag block */
  char *store[2];
};

static int info_cb(void *ctx, const char *name, const char *value) {
  struct tags *t = ctx;
  /* (psflib merges a repeated tag into one value, a line per occurrence in
     file order: psf_time reads the first) */
  int i = !strcasecmp(name, "length") ? 0 : !strcasecmp(name, "fade") ? 1 : -1;
  if (i >= 0) {
    char *copy = strdup(value ? value : "");
    if (!copy) return -1;
    free(t->store[i]);
    t->store[i] = copy;
    if (i == 0) t->length = copy; else t->fade = copy;
  }
  return 0;
}

/* ── Files: FILE, then the libraries its tags name ────────────────────── */

struct loader {
  const char *file;       /* FILE as given */
  char *dir;              /* its folder, "" for the current one, else ending in "/" */
  int opened;             /* FILE itself was opened */
  void *sega;
  int sections;
};

static int regular_file(const char *p) {
  struct stat st;
  return stat(p, &st) == 0 && S_ISREG(st.st_mode);
}

static char *join(const char *a, const char *b, const char *c) {
  size_t n = strlen(a) + strlen(b) + strlen(c) + 1;
  char *s = malloc(n);
  if (s) snprintf(s, n, "%s%s%s", a, b, c);
  return s;
}

/* `dir` + `name` when that is a regular file, else `dir` + the first (in
   byte order) entry of `dir` equal to `name` ignoring case.  NULL when
   neither is there. */
static char *find_in(const char *dir, const char *name) {
  char *p = join(dir, "", name);
  if (!p) return NULL;
  if (regular_file(p)) return p;
  free(p);
  DIR *d = opendir(*dir ? dir : ".");
  if (!d) return NULL;
  char *best = NULL;
  struct dirent *e;
  while ((e = readdir(d)) != NULL) {
    if (strcasecmp(e->d_name, name) != 0) continue;
    char *q = join(dir, "", e->d_name);
    if (!q) continue;
    if (regular_file(q) && (!best || strcmp(q, best) < 0)) {
      free(best);
      best = q;
    } else {
      free(q);
    }
  }
  closedir(d);
  return best;
}

/* The file library `lib` (as its tag states it) names, under FILE's folder
   `dir` (see the header comment); NULL when there is none. */
static char *resolve_lib(const char *dir, const char *lib) {
  char *rel = strdup(lib);
  if (!rel) return NULL;
  for (char *c = rel; *c; c++)
    if (*c == '\\') *c = '/';
  char *start = rel;
  int rooted = *start == '/';
  while (*start == '/') start++;
  size_t n = strlen(start);
  while (n && start[n - 1] == '/') start[--n] = '\0';
  char *slash = strrchr(start, '/');
  const char *base = slash ? slash + 1 : start;
  char *found = NULL;
  if (!*base || !strcmp(base, ".") || !strcmp(base, "..")) {
    free(rel);
    return NULL;
  }
  if (slash && !rooted) {
    /* its folders as written — none of them ".." */
    int ok = 1;
    *slash = '\0';
    for (char *seg = start; ok && seg;) {
      char *next = strchr(seg, '/');
      if (next) *next = '\0';
      if (!strcmp(seg, "..")) ok = 0;
      if (next) *next = '/';
      seg = next ? next + 1 : NULL;
    }
    if (ok) {
      char *sub = join(dir, start, "/");
      if (sub) {
        found = find_in(sub, base);
        free(sub);
      }
    }
  }
  if (!found)
    found = find_in(dir, base);           /* the file of that name beside FILE */
  free(rel);
  return found;
}

static void *f_open(void *ctx, const char *p) {
  struct loader *L = ctx;
  if (!L->opened) {
    /* (the first open is FILE: psflib's base folder + its name) */
    L->opened = 1;
    return regular_file(L->file) ? fopen(L->file, "rb") : NULL;
  }
  size_t dl = strlen(L->dir);
  if (strncmp(p, L->dir, dl) != 0) return NULL;
  char *lib = resolve_lib(L->dir, p + dl);
  if (!lib) {
    fprintf(stderr, "dsfrender: can't open library %s: no such file in the rip's folder\n",
            p + dl);
    return NULL;
  }
  FILE *fh = fopen(lib, "rb");
  free(lib);
  return fh;
}

static size_t f_read(void *b, size_t s, size_t n, void *h) { return fread(b, s, n, (FILE *)h); }
static int f_seek(void *h, int64_t o, int w) { return fseeko((FILE *)h, (off_t)o, w) ? -1 : 0; }
static int f_close(void *h) { return fclose((FILE *)h) ? -1 : 0; }
static long f_tell(void *h) { return ftell((FILE *)h); }

/* A program section: a 4-byte little-endian load address, then its bytes —
   loaded into the 8 MB sound RAM, any part past its end left out.  A file
   without one (an empty program) loads nothing. */
static int load_cb(void *ctx, const uint8_t *exe, size_t n, const uint8_t *res, size_t rn) {
  struct loader *L = ctx;
  (void)res;
  (void)rn;
  if (n == 0) return 0;
  if (n <= 4) return -1;
  uint32_t start = (exe[0] | exe[1] << 8 | exe[2] << 16 | (uint32_t)exe[3] << 24) & 0x7fffffu;
  size_t len = n - 4;
  if (len > SOUND_RAM_END - start) len = SOUND_RAM_END - start;
  uint8_t *sec = malloc(4 + len);
  if (!sec) return -1;
  put32(sec, start);
  memcpy(sec + 4, exe + 4, len);
  int r = len ? sega_upload_program(L->sega, sec, (uint32_t)(4 + len)) : 0;
  free(sec);
  L->sections++;
  return r ? -1 : 0;
}

/* ── Main ─────────────────────────────────────────────────────────────── */

static int usage(void) {
  fprintf(stderr, "usage: dsfrender [-r rate] [-m max_seconds] [-f fade_ms] FILE OUT.wav\n");
  return 2;
}

int main(int argc, char **argv) {
  if (argc == 2 && strcmp(argv[1], "--version") == 0) {
    printf("dsfrender %s (Highly Theoretical %s, psflib %s)\n", DSFRENDER_VERSION,
           HT_COMMIT, PSFLIB_COMMIT);
    return 0;
  }
  long rate = AICA_RATE;
  double max_s = 0, fade_ms = 0;
  int have_m = 0, have_f = 0;
  const char *in = NULL, *out = NULL;
  for (int i = 1; i < argc; i++) {
    if (argv[i][0] == '-' && argv[i][1] && i + 1 < argc && strchr("rmf", argv[i][1])
        && argv[i][2] == '\0') {
      char *end;
      double v = strtod(argv[i + 1], &end);
      if (end == argv[i + 1] || *end || !isfinite(v)) return usage();
      switch (argv[i][1]) {
        case 'r': if (v != AICA_RATE) return usage(); break;
        case 'm': max_s = v; have_m = 1; break;
        case 'f': fade_ms = v; have_f = 1; break;
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
  if (have_m && max_s > MAX_SECONDS && max_s <= MAX_SECONDS + OVER_MAX_SECONDS)
    max_s = MAX_SECONDS;
  if (have_f && fade_ms > MAX_SECONDS * 1000 && fade_ms <= (MAX_SECONDS + OVER_MAX_SECONDS) * 1000)
    fade_ms = MAX_SECONDS * 1000;
  if (!in || !out || (have_m && (max_s <= 0 || max_s > MAX_SECONDS))
      || (have_f && (fade_ms < 0 || fade_ms > MAX_SECONDS * 1000)))
    return usage();

  if (sega_init()) {
    fprintf(stderr, "dsfrender: the sound core failed to start\n");
    return 1;
  }
  struct loader L = {0};
  L.file = in;
  const char *slash = strrchr(in, '/');
  L.dir = slash ? strndup(in, (size_t)(slash - in + 1)) : strdup("");
  L.sega = malloc(sega_get_state_size(2));
  if (!L.dir || !L.sega) {
    fprintf(stderr, "dsfrender: out of memory\n");
    return 1;
  }
  sega_clear_state(L.sega, 2);              /* 2: Dreamcast (AICA + ARM7) */
  sega_enable_dry(L.sega, 1);
  sega_enable_dsp(L.sega, 1);
  sega_enable_dsp_dynarec(L.sega, 0);

  struct tags t = {0};
  /* Only "/" separates FILE's folder ("\" is a file-name character here);
     a library path's "\" is handled by resolve_lib. */
  static const psf_file_callbacks cb = {"/", NULL, f_open, f_read, f_seek, f_close, f_tell};
  psf_file_callbacks cbs = cb;
  cbs.context = &L;
  int v = psf_load(in, &cbs, 0x12, load_cb, &L, info_cb, &t, 0, NULL, NULL);
  if (v != 0x12 || L.sections == 0) {
    fprintf(stderr, "dsfrender: not a Dreamcast DSF this build can play: %s\n", in);
    return 1;
  }

  /* How long it plays, and its fade (see the header comment). */
  double length = t.length ? psf_time(t.length) : -1;
  int has_length = length > 0 && length <= MAX_SECONDS;
  double fade = t.fade ? psf_time(t.fade) : -1;
  if (!(fade > 0)) fade = 0;
  if (fade > MAX_FADE_SECONDS) fade = MAX_FADE_SECONDS;
  if (has_length && fade > MAX_SECONDS - length) fade = MAX_SECONDS - length;
  double total = have_m ? max_s : has_length ? length + fade : DEFAULT_SECONDS;
  double fade_s = have_f ? fade_ms / 1000.0 : fade;
  free(t.store[0]);
  free(t.store[1]);

  uint64_t frames_total = (uint64_t)nearbyint(total * (double)rate);
  uint64_t fade_frames = (uint64_t)nearbyint(fade_s * (double)rate);
  if (fade_frames > frames_total) fade_frames = frames_total;
  uint64_t fade_from = frames_total - fade_frames;

  FILE *fp = fopen(out, "wb");
  if (!fp) {
    fprintf(stderr, "dsfrender: can't write %s\n", out);
    return 1;
  }
  unsigned char h[44];
  wav_header(h, (uint32_t)rate, 0);
  int failed = fwrite(h, 1, sizeof h, fp) != sizeof h || fflush(fp) != 0;
  int emu_error = 0;

  const uint32_t block = (uint32_t)rate / 10;              /* 0.1 s */
  int16_t *buf = malloc(sizeof(int16_t) * 2 * block);
  unsigned char *le = malloc(4 * block);
  if (!buf || !le) {
    fprintf(stderr, "dsfrender: out of memory\n");
    failed = 1;
  }
  uint64_t frames = 0;
  while (!failed && frames < frames_total) {
    uint32_t want = frames_total - frames < block ? (uint32_t)(frames_total - frames) : block;
    uint32_t done = 0;
    while (done < want) {
      uint32_t got = want - done;
      sint32 r = sega_execute(L.sega, 0x7fffffff, buf + 2 * done, &got);
      if (r < 0 || got == 0) {
        fprintf(stderr, "dsfrender: the emulation stopped at %.1f s (error %d)\n",
                (double)(frames + done) / rate, (int)r);
        emu_error = 1;
        break;
      }
      done += got;
    }
    for (uint32_t i = 0; i < done; i++) {
      uint64_t k = frames + i;
      for (int c = 0; c < 2; c++) {
        int16_t s = buf[2 * i + c];
        if (k >= fade_from) {
          /* frame k - fade_from of the fade: scaled by (n - j) / n */
          double g = (double)(fade_frames - (k - fade_from)) / (double)fade_frames;
          s = (int16_t)((double)s * g);
        }
        put16(le + 4 * i + 2 * c, (uint16_t)s);
      }
    }
    if (done && (fwrite(le, 1, 4 * (size_t)done, fp) != 4 * (size_t)done || fflush(fp) != 0)) {
      failed = 1;                                 /* a full disk, a closed pipe */
      break;
    }
    frames += done;
    if (emu_error) break;
  }

  uint64_t bytes = frames * 4;
  wav_header(h, (uint32_t)rate, (uint32_t)bytes);
  if (fseek(fp, 0, SEEK_SET) != 0 || fwrite(h, 1, sizeof h, fp) != sizeof h)
    failed = 1;
  if (fclose(fp) != 0)
    failed = 1;
  if (failed)
    fprintf(stderr, "dsfrender: can't write %s\n", out);
  free(le);
  free(buf);
  free(L.sega);
  free(L.dir);
  if (!failed && frames == 0) {
    fprintf(stderr, "dsfrender: nothing rendered (%g s)\n", total);
    return 1;
  }
  return failed || emu_error ? 1 : 0;
}
