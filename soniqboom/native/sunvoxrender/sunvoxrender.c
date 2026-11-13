/* SPDX-FileCopyrightText: 2026 S.F. Cyris
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * sunvoxrender — render a SunVox project (.sunvox) to a 16-bit stereo WAV
 * with the SunVox Library (Alexander Zolotov, WarmPlace.ru; MIT), linked
 * statically, offline: no audio device is opened.
 *
 *   sunvoxrender [-r rate] [-m max_seconds] [-f fade_ms] FILE OUT.wav
 *   sunvoxrender -i FILE
 *   sunvoxrender --version
 *
 * Length: the project's own (sv_get_song_length_frames — its last pattern's
 * end; the project plays once, it doesn't loop).  A project longer than
 * `max_seconds` (default 1800) is cut there, its last `fade_ms` (default
 * 8000) faded out.  -i prints "seconds=<length>" and "title=<project name>"
 * and renders nothing.
 *
 * The WAV is written as it renders (a 44-byte header first, its sizes
 * patched at the end), in 0.1-second blocks flushed as they are made, so a
 * reader can follow it.
 *
 * The library runs in a home of its own: sunvoxrender makes a temp folder
 * (under TMPDIR, else /tmp), points the library's folders into it and
 * removes it, with what the library left there, at the end.  The library
 * looks for its config file in its "work" folder first, then in its "config"
 * folder, and its list of the places to look ends with "0" instead of a null
 * pointer: when neither holds the file it reads past the list (it crashed on
 * Linux arm64 and macOS).  Its work folder is the working folder on Linux,
 * on macOS the folder above the one sunvoxrender is in; its config folder
 * ~/.config/SunVox Library (Linux, made when looked at) or ~/Library/
 * Application Support/SunVox Library (macOS, made at start).  So the temp
 * folder holds an empty "sunvox_dll_config.ini" where the library looks:
 * in the folder itself on Linux (sunvoxrender starts the library there, so
 * the test file the library writes into its work folder at start lands
 * there too), in its Library/Application Support/SunVox Library on macOS,
 * where the system's home (CFFIXED_USER_HOME) is the temp folder — read when
 * the process starts, so sunvoxrender makes the folder, then starts itself
 * again with CFFIXED_USER_HOME (and SUNVOXRENDER_HOME, naming the folder)
 * set, through a link to itself in the folder's bin/ (the library's work
 * folder — where it writes a test file at start — is then the temp folder
 * too); that run renders and removes the folder.  Exit status 0 with audio
 * written (-i: with the project read), 1 on a file that isn't a SunVox
 * project or that the library can't load, on an empty project or a failed
 * write, 2 on usage. */

#ifndef __APPLE__
#define _GNU_SOURCE                    /* (glibc: nftw, mkdtemp) */
#endif
#include <ftw.h>
#include <sys/stat.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define SUNVOX_STATIC_LIB
#include "sunvox.h"

#ifndef SUNVOX_LIB_VERSION
#define SUNVOX_LIB_VERSION "unknown"
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

static int usage(void) {
  fprintf(stderr, "usage: sunvoxrender [-r rate] [-m max_seconds] [-f fade_ms] FILE OUT.wav\n"
                  "       sunvoxrender -i FILE\n");
  return 2;
}

/* Does the file start like a SunVox project ("SVOX" + a chunk)? */
static int is_sunvox(const char *path) {
  unsigned char head[4];
  FILE *fp = fopen(path, "rb");
  if (!fp) return 0;
  size_t n = fread(head, 1, sizeof head, fp);
  fclose(fp);
  return n == sizeof head && memcmp(head, "SVOX", 4) == 0;
}

/* The path ``p`` as an absolute path in ``buf`` (``p`` itself when it is
   absolute; the working folder's path before it otherwise).  0 on success. */
static int absolute(const char *p, char *buf, size_t size) {
  if (p[0] == '/') {
    if (strlen(p) >= size) return -1;
    strcpy(buf, p);
    return 0;
  }
  if (!getcwd(buf, size)) return -1;
  size_t n = strlen(buf);
  if (n + 1 + strlen(p) >= size) return -1;
  buf[n] = '/';
  strcpy(buf + n + 1, p);
  return 0;
}

static char g_home[PATH_MAX];          /* the library's own home (a temp folder) */

/* The folder in the library's home ``g_home`` where its config file goes:
   the home itself on Linux (its work folder), Library/Application
   Support/SunVox Library on macOS. */
static void config_folder(char *buf, size_t size) {
#ifdef __APPLE__
  snprintf(buf, size, "%s/Library/Application Support/SunVox Library", g_home);
#else
  snprintf(buf, size, "%s", g_home);
#endif
}

/* The folder temp homes are made in: TMPDIR (else /tmp), without a
   trailing slash, in ``buf``. */
static void temp_base(char *buf, size_t size) {
  const char *tmp = getenv("TMPDIR");
  if (!tmp || !*tmp) tmp = "/tmp";
  snprintf(buf, size, "%s", tmp);
  size_t n = strlen(buf);
  while (n > 1 && buf[n - 1] == '/') buf[--n] = 0;
}

#ifdef __APPLE__
/* Is ``p`` a temp home this program makes — "sunvoxrender.XXXXXX" directly
   in the temp base?  (SUNVOXRENDER_HOME names the folder the run is to use
   and remove: never any other.) */
static int is_own_home(const char *p) {
  char base[PATH_MAX];
  temp_base(base, sizeof base);
  size_t n = strlen(base);
  if (strncmp(p, base, n) != 0 || p[n] != '/') return 0;
  const char *leaf = p + n + 1;
  return strncmp(leaf, "sunvoxrender.", 13) == 0 && strlen(leaf) == 19 && !strchr(leaf, '/');
}
#endif

/* Make the library's home (see the top of the file): a temp folder with the
   empty config file in its work folder.  0 on success. */
static int make_home(void) {
  char base[PATH_MAX];
  temp_base(base, sizeof base);
  if ((size_t)snprintf(g_home, sizeof g_home, "%s/sunvoxrender.XXXXXX", base) >= sizeof g_home)
    return -1;
  if (!mkdtemp(g_home)) {
    g_home[0] = 0;
    return -1;
  }
  char dir[PATH_MAX + 64], cfg[PATH_MAX + 96];
  config_folder(dir, sizeof dir);
#ifdef __APPLE__
  snprintf(cfg, sizeof cfg, "%s/Library", g_home);
  if (mkdir(cfg, 0700) != 0) return -1;
  snprintf(cfg, sizeof cfg, "%s/Library/Application Support", g_home);
  if (mkdir(cfg, 0700) != 0 || mkdir(dir, 0700) != 0) return -1;
#endif
  snprintf(cfg, sizeof cfg, "%s/sunvox_dll_config.ini", dir);
  FILE *f = fopen(cfg, "wb");
  if (!f || fclose(f) != 0) return -1;
  return 0;
}

/* Point the library's folders into its home and start it there.  0 on
   success. */
static int enter_home(void) {
  if (setenv("HOME", g_home, 1) != 0) return -1;
  unsetenv("XDG_CONFIG_HOME");
  return chdir(g_home);
}

static int remove_entry(const char *path, const struct stat *st, int type, struct FTW *ftw) {
  (void)st; (void)type; (void)ftw;
  remove(path);
  return 0;
}

/* Remove the library's home and everything it left there. */
static void leave_home(void) {
  if (!g_home[0]) return;
  if (chdir("/") != 0) { /* (the folder is removed either way) */ }
  nftw(g_home, remove_entry, 16, FTW_DEPTH | FTW_PHYS);
}

/* The project's name on one line: control characters dropped. */
static void print_title(const char *name) {
  fputs("title=", stdout);
  for (const unsigned char *p = (const unsigned char *)(name ? name : ""); *p; p++)
    if (*p >= 0x20 && *p != 0x7f) fputc(*p, stdout);
  fputc('\n', stdout);
}

static int run(const char *in, const char *out, int info, long rate, long max_s, long fade_ms);

int main(int argc, char **argv) {
  long rate = 44100, max_s = 1800, fade_ms = 8000;
  int info = 0;
  const char *in = NULL, *out = NULL;
  if (argc == 2 && strcmp(argv[1], "--version") == 0) {
    printf("sunvoxrender 1.2 (SunVox Library %s)\n", SUNVOX_LIB_VERSION);
    return 0;
  }
  for (int i = 1; i < argc; i++) {
    if (strcmp(argv[i], "-i") == 0) {
      info = 1;
    } else if (argv[i][0] == '-' && argv[i][1] && i + 1 < argc && strchr("rmf", argv[i][1])
               && argv[i][2] == '\0') {
      char *end = NULL;
      long v = strtol(argv[i + 1], &end, 10);
      if (!end || *end) return usage();
      switch (argv[i][1]) {
        case 'r': rate = v; break;
        case 'm': max_s = v; break;
        case 'f': fade_ms = v; break;
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
  if (!in || (!info && !out) || (info && out)
      || (rate != 44100 && rate != 48000 && rate != 96000)
      || max_s < 1 || max_s > 7200 || fade_ms < 0)
    return usage();

  if (!is_sunvox(in)) {
    fprintf(stderr, "sunvoxrender: not a SunVox project: %s\n", in);
    return 1;
  }
  static char in_abs[PATH_MAX], out_abs[PATH_MAX];
  if (absolute(in, in_abs, sizeof in_abs) != 0
      || (out && absolute(out, out_abs, sizeof out_abs) != 0)) {
    fprintf(stderr, "sunvoxrender: can't open %s\n", in);
    return 1;
  }
  in = in_abs;
  if (out) out = out_abs;
  const char *given = NULL;
#ifdef __APPLE__
  given = getenv("SUNVOXRENDER_HOME");          /* (made by the run that started this one) */
  if (given && (!*given || strlen(given) >= sizeof g_home || !is_own_home(given))) {
    fprintf(stderr, "sunvoxrender: SUNVOXRENDER_HOME names no folder of its own\n");
    return 1;
  }
#endif
  if (given) {
    strcpy(g_home, given);
  } else if (make_home() != 0) {
    fprintf(stderr, "sunvoxrender: can't make a temp folder\n");
    leave_home();
    return 1;
  }
#ifdef __APPLE__
  if (!(given && *given)) {
    /* the system reads CFFIXED_USER_HOME at start: start again with it —
       through a link in the temp folder's bin/, so the library's work
       folder (the one above the program's) is the temp folder too */
    char exe[PATH_MAX], link[PATH_MAX + 32];
    uint32_t n = sizeof exe;
    snprintf(link, sizeof link, "%s/bin", g_home);
    if (_NSGetExecutablePath(exe, &n) != 0 || mkdir(link, 0700) != 0
        || (size_t)snprintf(link, sizeof link, "%s/bin/sunvoxrender", g_home) >= sizeof link
        || symlink(exe, link) != 0
        || setenv("CFFIXED_USER_HOME", g_home, 1) != 0
        || setenv("SUNVOXRENDER_HOME", g_home, 1) != 0) {
      fprintf(stderr, "sunvoxrender: can't start in its temp folder\n");
      leave_home();
      return 1;
    }
    execv(link, argv);
    fprintf(stderr, "sunvoxrender: can't start in its temp folder\n");
    leave_home();
    return 1;
  }
#endif
  if (enter_home() != 0) {
    fprintf(stderr, "sunvoxrender: can't make a temp folder\n");
    leave_home();
    return 1;
  }
  int rc = run(in, out, info, rate, max_s, fade_ms);
  leave_home();
  return rc;
}

/* The render (``main`` after its checks, in the library's own folder). */
static int run(const char *in, const char *out, int info, long rate, long max_s, long fade_ms) {
  uint32_t flags = SV_INIT_FLAG_OFFLINE | SV_INIT_FLAG_AUDIO_INT16 | SV_INIT_FLAG_ONE_THREAD
                   | SV_INIT_FLAG_NO_DEBUG_OUTPUT;
  if (sv_init(NULL, (int)rate, 2, flags) < 0) {
    fprintf(stderr, "sunvoxrender: the SunVox engine didn't start\n");
    return 1;
  }
  sv_open_slot(0);
  if (sv_load(0, in) != 0) {
    fprintf(stderr, "sunvoxrender: not a SunVox project this library can load: %s\n", in);
    sv_close_slot(0);
    sv_deinit();
    return 1;
  }
  uint64_t total = sv_get_song_length_frames(0);
  if (info) {
    printf("seconds=%.3f\n", (double)total / (double)rate);
    print_title(sv_get_song_name(0));
    sv_close_slot(0);
    sv_deinit();
    return 0;
  }
  if (total == 0) {
    fprintf(stderr, "sunvoxrender: the project is empty (no pattern plays): %s\n", in);
    sv_close_slot(0);
    sv_deinit();
    return 1;
  }

  FILE *fp = fopen(out, "wb");
  if (!fp) {
    fprintf(stderr, "sunvoxrender: can't write %s\n", out);
    sv_close_slot(0);
    sv_deinit();
    return 1;
  }
  unsigned char h[44];
  wav_header(h, (uint32_t)rate, 0);
  int failed = fwrite(h, 1, sizeof h, fp) != sizeof h || fflush(fp) != 0;

  const uint64_t cap = (uint64_t)max_s * (uint64_t)rate;
  uint64_t fade_frames = 0;
  if (total > cap) {
    total = cap;
    fade_frames = (uint64_t)fade_ms * (uint64_t)rate / 1000;
    if (fade_frames > total) fade_frames = total;
  }
  sv_set_autostop(0, 1);                         /* play once, no loop */
  sv_play_from_beginning(0);

  const uint32_t block = (uint32_t)rate / 10;    /* 0.1 s */
  int16_t *buf = (int16_t *)malloc(sizeof(int16_t) * 2 * block);
  unsigned char *le = (unsigned char *)malloc(4 * (size_t)block);
  if (!buf || !le) {
    fprintf(stderr, "sunvoxrender: out of memory\n");
    failed = 1;
  }
  uint64_t frames = 0;
  const uint64_t fade_from = total - fade_frames;
  while (!failed && frames < total) {
    uint32_t n = (uint32_t)((total - frames) < block ? (total - frames) : block);
    if (sv_audio_callback(buf, (int)n, 0, sv_get_ticks()) == 0)
      memset(buf, 0, sizeof(int16_t) * 2 * n);   /* (a silent piece) */
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
    fprintf(stderr, "sunvoxrender: can't write %s\n", out);
  free(le);
  free(buf);
  sv_stop(0);
  sv_close_slot(0);
  sv_deinit();
  return failed ? 1 : 0;
}
