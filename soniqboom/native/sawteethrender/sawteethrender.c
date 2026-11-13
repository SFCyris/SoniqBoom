/* SPDX-FileCopyrightText: 2026 S.F. Cyris
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * The playback code (loading, the sequencer, the instrument, wave, LFO and
 * filter synthesis, the channel mixing and the end detection) is ported from
 * NostalgicPlayer's Sawteeth player (Source/Agents/Players/Sawteeth at commit
 * c6eb0c5791eaab993a03fa57a72fe1d0dc8f7a4a) and its mixer, Copyright (c) 2023
 * Thomas Neumann, MIT licence — the full permission text is in
 * NostalgicPlayer-LICENSE beside this file.
 *
 * sawteethrender — render a Sawteeth module (the binary "SWTD" and the text
 * "SWTT" format) to a 16-bit stereo WAV.
 *
 *   sawteethrender [-r rate] [-m max_seconds] [-f fade_ms] [-l loops]
 *                  FILE OUT.wav
 *   sawteethrender -i [-r rate] [-m max_seconds] [-f fade_ms] [-l loops] FILE
 *   sawteethrender --version
 *   (-v VU_FILE with either form: also write the per-channel levels)
 *
 * The song plays until NostalgicPlayer would report its end `loops` times
 * (default 1: every channel has passed its loop point since the last end),
 * or for `max_seconds` (default 1800, at most 7200), whose last `fade_ms`
 * (default 8000) fade out linearly.  The synthesis runs at 44100 Hz, one
 * tick of the module's samples-per-tick at a time; at another `rate` each
 * tick is resampled as NostalgicPlayer's mixer does it without interpolation
 * (floor(rate * spspal / 44100) frames per tick, each the sample at or
 * before its position).  Each channel's 16-bit output is mixed at full
 * volume at the pan position ((256 - left) * 256 + 256 * right) / 512
 * (0 left, 256 right), and the mix is scaled to 16 bits as NostalgicPlayer's
 * mixer and down-mixer do it.  For a song that uses the noise waveform, the
 * song is first synthesized silently to its end (at most 600 s of it), as
 * NostalgicPlayer's duration calculation does, which leaves the noise
 * generator where a freshly started NostalgicPlayer has it.  The WAV is
 * written as it renders (a 44-byte header first, its sizes patched at the
 * end), flushed every 0.1 s of audio, so a reader can follow it.  A FILE
 * over 32 MiB is not read.
 * -i plays the song the same way without writing it and prints "seconds="
 * (the length a render with the same options has), "title=", "author=" and
 * "channels=".
 * -v VU_FILE (with a render or with -i) also writes each channel's level,
 * 30 times a second, as a VUMR v1 file (SoniqBoom's per-channel meter data,
 * docs/vu-cache-format.md): the peak of the channel's synthesized signal
 * over each 1/30 s of the output (before the channel-count scaling and the
 * pan; 0–1 as 0–255), a last partial window included, and each channel's
 * side (0 centre, 1 left, 2 right) from its left / right amplitudes.  With
 * -i the song is synthesized for it (noise songs with their pre-pass), so
 * the levels are the render's.
 * Exit status 0 with audio written, 1 on a file that isn't a
 * Sawteeth module, a damaged or empty one, or a failed write (a partial WAV
 * may be left), 2 on usage. */

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* NostalgicPlayer computes in IEEE single precision without fused
 * multiply-adds; keep the compiler from contracting a*b+c into one. */
#pragma STDC FP_CONTRACT OFF

#ifndef SAWTEETHRENDER_VERSION
#define SAWTEETHRENDER_VERSION "1.1"
#endif
#define NP_COMMIT "c6eb0c5"

#define CURRENT_FILE_VERSION 1200
#define MAX_CHAN 12
#define CHN_STEPS 8192
#define SYNTH_RATE 44100
#define VU_RATE 30                     /* level frames a second (-v) */
#define MIN_SPS_PAL 32              /* below: a tick rate over 1378 Hz, rejected */
#define MAX_FILE_BYTES (32L << 20)
#define FRAC_BITS 11                /* NostalgicPlayer's mixer position fraction */
#define PREPASS_SECONDS 600         /* the noise-seed pass's song-time limit */

/* ── .NET conversions (saturating since .NET 9; NaN → 0) ─────────────────── */

static int32_t f2i(float f) {
  if (f != f) return 0;
  if (f >= 2147483648.0f) return INT32_MAX;
  if (f <= -2147483648.0f) return INT32_MIN;
  return (int32_t)f;
}

/* (short)f: converted to int (saturating), then narrowed (wrapping). */
static int16_t f2s(float f) {
  return (int16_t)(uint16_t)(uint32_t)f2i(f);
}

static uint32_t f2u(float f) {
  if (f != f || f <= 0.0f) return 0;
  if (f >= 4294967296.0f) return UINT32_MAX;
  return (uint32_t)f;
}

/* ── The module ──────────────────────────────────────────────────────────── */

typedef struct { uint8_t part; int8_t transp; uint8_t damp; } ChStep;
typedef struct {
  uint8_t left, right;
  uint16_t len, lloop, rloop;
  ChStep steps[CHN_STEPS];
} Chan;
typedef struct { uint8_t ins, eff, note; } Step;
typedef struct { uint8_t sps, len; Step steps[255]; } Part;
typedef struct { uint8_t time, lev; } TimeLev;
typedef struct { uint8_t note, relative, wform; } InsStep;
typedef struct {
  InsStep steps[255];
  TimeLev amp[255], filter[255];
  uint8_t amp_points, filter_points, filter_mode, clip_mode, boost;
  uint8_t sps, res, vib_s, vib_d, pwm_s, pwm_d, loop, len;
} Ins;

typedef struct {
  uint16_t version, sps_pal;
  uint8_t channel_count, part_count;
  int instrument_count;
  Chan *chan;
  Part *parts;
  Ins *ins;
  char title[300], author[300];
} Song;

/* ── Reading (NostalgicPlayer's ModuleStream: a short read returns 0 and
 *    sets EndOfStream, which the next full read clears) ─────────────────── */

typedef struct {
  const uint8_t *data;
  size_t len, pos;
  int eof, text, bad;     /* bad: a text number didn't parse (a .NET exception) */
} Reader;

static uint8_t rd_byte(Reader *r) {
  if (r->pos < r->len) {
    r->eof = 0;
    return r->data[r->pos++];
  }
  r->eof = 1;
  return 0;
}

static uint32_t rd_be(Reader *r, int n) {
  if (r->len - r->pos < (size_t)n) {
    r->pos = r->len;     /* the partial read consumes what is left */
    r->eof = 1;
    return 0;
  }
  uint32_t v = 0;
  for (int i = 0; i < n; i++)
    v = (v << 8) | r->data[r->pos++];
  r->eof = 0;
  return v;
}

/* ReadString: up to 299 bytes, CR skipped, ending at NUL / LF / the end. */
static int rd_string(Reader *r, char *buf) {
  int i;
  for (i = 0; i < 299; i++) {
    uint8_t b = rd_byte(r);
    if (r->eof)
      break;
    if (b == 0x0d) {
      i--;
      continue;
    }
    if (b == 0x00 || b == 0x0a)
      break;
    buf[i] = (char)b;
  }
  buf[i] = '\0';
  return i;
}

static int is_net_white(char c) {
  return c == ' ' || (c >= 0x09 && c <= 0x0d);
}

/* Convert.ToByte / ToUInt16 / ToUInt32 of a text line: white space around,
 * an optional sign, decimal digits; out of range or malformed is an error. */
static uint32_t rd_text_number(Reader *r, uint32_t max) {
  char buf[300];
  for (;;) {
    int n = rd_string(r, buf);
    if (n == 0) {
      if (r->eof) {          /* NostalgicPlayer loops forever here */
        r->bad = 1;
        return 0;
      }
      continue;
    }
    if (strncmp(buf, "//", 2) != 0)
      break;
  }
  const char *s = buf;
  while (is_net_white(*s)) s++;
  int neg = 0;
  if (*s == '+' || *s == '-')
    neg = *s++ == '-';
  if (*s < '0' || *s > '9') {
    r->bad = 1;
    return 0;
  }
  uint64_t v = 0;
  while (*s >= '0' && *s <= '9') {
    v = v * 10 + (uint64_t)(*s++ - '0');
    if (v > 0xffffffffull) v = 0x100000000ull;   /* stays out of range */
  }
  while (is_net_white(*s)) s++;
  if (*s != '\0' || v > max || (neg && v != 0)) {
    r->bad = 1;
    return 0;
  }
  return (uint32_t)v;
}

static uint8_t rd8(Reader *r) {
  return r->text ? (uint8_t)rd_text_number(r, 0xff) : rd_byte(r);
}
static uint16_t rd16(Reader *r) {
  return r->text ? (uint16_t)rd_text_number(r, 0xffff) : (uint16_t)rd_be(r, 2);
}
static uint32_t rd32(Reader *r) {
  return r->text ? rd_text_number(r, 0xffffffffu) : rd_be(r, 4);
}

/* Returns 0 on success, 1 when it isn't a Sawteeth module, 2 when it is a
 * damaged one. */
static int load_song(Song *s, const uint8_t *data, size_t len) {
  char line[300];
  if (len < 10 || (memcmp(data, "SWTD", 4) != 0 && memcmp(data, "SWTT", 4) != 0))
    return 1;
  Reader r = { data, len, 0, 0, data[3] == 'T', 0 };

  /* Identify */
  if (r.text)
    rd_string(&r, line);
  else
    r.pos = 4;
  uint16_t ver = rd16(&r);
  if (r.bad || ver > CURRENT_FILE_VERSION)
    return 1;
  if (ver >= 900) {
    uint16_t pal = rd16(&r);
    if (r.bad || pal < 1)
      return 1;
  }
  uint8_t chn = rd8(&r);
  if (r.bad || chn < 1 || chn > MAX_CHAN)
    return 1;

  /* Load */
  r.pos = 0;
  r.eof = 0;
  if (r.text)
    rd_string(&r, line);
  else
    r.pos = 4;
  s->version = rd16(&r);
  s->sps_pal = s->version < 900 ? 882 : rd16(&r);
  s->channel_count = rd8(&r);
  if (r.bad || s->sps_pal < MIN_SPS_PAL)
    return 2;

  s->chan = calloc(s->channel_count, sizeof *s->chan);
  if (!s->chan)
    return 2;
  for (int i = 0; i < s->channel_count; i++) {
    Chan *c = &s->chan[i];
    c->left = rd8(&r);
    c->right = rd8(&r);
    c->len = rd16(&r);
    c->lloop = s->version < 910 ? 0 : rd16(&r);
    c->rloop = s->version < 1200 ? (uint16_t)(c->len - 1) : rd16(&r);
    if (r.bad || c->len < 1 || c->len > CHN_STEPS)
      return 2;
    if (c->rloop >= c->len)
      c->rloop = (uint16_t)(c->len - 1);
    /* (NostalgicPlayer indexes past the sequence with such a loop point) */
    if (c->lloop >= c->len)
      c->lloop = c->rloop;
    for (int j = 0; j < c->len && !r.bad; j++) {
      c->steps[j].part = rd8(&r);
      c->steps[j].transp = (int8_t)rd8(&r);
      c->steps[j].damp = rd8(&r);
    }
    if (r.bad || r.eof)
      return 2;
  }

  s->part_count = rd8(&r);
  if (r.bad || s->part_count < 1)
    return 2;
  s->parts = calloc(s->part_count, sizeof *s->parts);
  if (!s->parts)
    return 2;
  for (int i = 0; i < s->part_count; i++) {
    Part *p = &s->parts[i];
    p->sps = rd8(&r);
    if (r.bad || p->sps < 1)
      return 2;
    p->len = rd8(&r);
    if (r.bad || p->len < 1)
      return 2;
    for (int j = 0; j < p->len && !r.bad; j++) {
      p->steps[j].ins = rd8(&r);
      p->steps[j].eff = rd8(&r);
      p->steps[j].note = rd8(&r);
    }
    if (r.bad || r.eof)
      return 2;
  }
  for (int i = 0; i < s->channel_count; i++)
    for (int j = 0; j < s->chan[i].len; j++)
      if (s->chan[i].steps[j].part >= s->part_count)
        s->chan[i].steps[j].part = (uint8_t)(s->part_count - 1);

  uint8_t count = (uint8_t)(rd8(&r) + 1);
  if (r.bad || count < 2)
    return 2;
  s->instrument_count = count;
  s->ins = calloc((size_t)count, sizeof *s->ins);
  if (!s->ins)
    return 2;
  Ins *d = &s->ins[0];            /* the dummy instrument 0 */
  d->filter_points = 1;
  d->amp_points = 1;
  d->filter_mode = 0;
  d->clip_mode = 0;
  d->boost = 1;
  d->sps = 30;
  d->res = 0;
  d->vib_s = 1;
  d->vib_d = 1;
  d->pwm_s = 1;
  d->pwm_d = 1;
  d->len = 0;
  d->loop = 0;
  for (int i = 1; i < count; i++) {
    Ins *in = &s->ins[i];
    in->filter_points = rd8(&r);
    if (r.bad || in->filter_points < 1)
      return 2;
    for (int j = 0; j < in->filter_points; j++) {
      in->filter[j].time = rd8(&r);
      in->filter[j].lev = rd8(&r);
    }
    in->amp_points = rd8(&r);
    if (r.bad || in->amp_points < 1)
      return 2;
    for (int j = 0; j < in->amp_points; j++) {
      in->amp[j].time = rd8(&r);
      in->amp[j].lev = rd8(&r);
    }
    in->filter_mode = rd8(&r);
    in->clip_mode = rd8(&r);
    in->boost = in->clip_mode & 15;
    in->clip_mode >>= 4;
    in->vib_s = rd8(&r);
    in->vib_d = rd8(&r);
    in->pwm_s = rd8(&r);
    in->pwm_d = rd8(&r);
    in->res = rd8(&r);
    in->sps = rd8(&r);
    if (r.bad || in->sps < 1)
      return 2;
    if (s->version < 900) {
      uint8_t tmp = rd8(&r);
      in->len = tmp & 127;
      in->loop = (uint8_t)((tmp & 1) != 0 ? 0 : in->len - 1);
    } else {
      in->len = rd8(&r);
      in->loop = rd8(&r);
      if (r.bad || in->loop >= in->len)
        return 2;
    }
    if (r.bad || in->len < 1)
      return 2;
    for (int j = 0; j < in->len && !r.bad; j++) {
      uint8_t t = rd8(&r);
      in->steps[j].relative = (t & 0x80) != 0;
      in->steps[j].wform = t & 0xf;
      in->steps[j].note = rd8(&r);
    }
    if (r.bad || r.eof)
      return 2;
  }

  uint8_t breakpoints = rd8(&r);
  for (int i = 0; i < breakpoints && !r.bad; i++) {
    (void)rd32(&r);            /* PAL, command: not used for playback */
    (void)rd32(&r);
  }
  if (r.bad || r.eof)
    return 2;

  rd_string(&r, s->title);
  rd_string(&r, s->author);
  return 0;
}

static void free_song(Song *s) {
  free(s->chan);
  free(s->parts);
  free(s->ins);
}

/* ── Tables ──────────────────────────────────────────────────────────────── */

static float n2f[22 * 12], r2f[22 * 12];
static float sint[513], trit[513];
static int32_t jng_seed = 1;

static void init_tables(void) {
  const double mul = 1.0594630943593;
  int count = 0;
  double oct_base = 1.02197486445547712033;
  for (int oc = 0; oc < 22; oc++) {
    double bas = oct_base;
    for (int c = 0; c < 12; c++) {
      n2f[count++] = (float)bas;
      bas *= mul;
    }
    oct_base *= 2.0;
  }
  count = 0;
  oct_base = 1.0;
  for (int oc = 0; oc < 22; oc++) {
    double bas = oct_base;
    for (int c = 0; c < 12; c++) {
      r2f[count++] = (float)bas;
      bas *= mul;
    }
    oct_base *= 2.0;
  }

  int c;
  for (c = 0; c < 513; c++)
    sint[c] = (float)sin(c * (2.0f * 3.14159265358979323846) / 512.0f);
  float smp = -1.0f;
  const float add = 4.0f / 512.0f;
  for (c = 0; c < 256; c++) {
    trit[c] = smp;
    smp += add;
  }
  for (; c < 512; c++) {
    trit[c] = smp;
    smp -= add;
  }
  trit[512] = trit[0];
}

static uint32_t jng_rand(void) {
  const int32_t big_mod = 0x7fffffff, w = 127773, c = 2836;
  jng_seed = 16807 * (jng_seed % w) - (jng_seed / w) * c;
  if (jng_seed < 0)
    jng_seed += big_mod;
  return (uint32_t)jng_seed;
}

/* ── Lfo ─────────────────────────────────────────────────────────────────── */

typedef struct { float curr, step; } Lfo;

static void lfo_init(Lfo *l) {
  l->curr = 0.0f;
  l->step = (float)(6.28 * 0.00002267573696145124);
}
static void lfo_set_freq(Lfo *l, float freq) {
  l->step = (float)(freq * (6.28 * 0.00002267573696145124));
}
static float lfo_next(Lfo *l) {
  l->curr += l->step;
  return (float)cos(l->curr);
}

/* ── Wave ────────────────────────────────────────────────────────────────── */

enum { W_HOLD, W_SAW, W_SQR, W_TRI, W_NOS, W_SIN, W_TRIU, W_SINU };
#define PWM_LIM 0.9f

typedef struct {
  int form, pwm_lo;
  float amp, from_amp, pwm, curr_val, curr, step, noise_val;
  uint32_t sin_curr_val, sin_step;
} Wave;

static void wave_set_form(Wave *w, int form) {
  if (w->form != form) {
    if (w->curr_val > 1.0f) w->curr_val = 0.0f;
    if (w->curr > 1.0f) w->curr = 0.0f;
  }
  w->form = form;
}

static void wave_set_freq(Wave *w, float freq) {
  w->step = (float)(freq * 0.00002267573696145124);   /* 1/44100 */
  if (w->step > 1.9f)
    w->step = 1.9f;
  w->sin_step = f2u((float)(1 << 22) * 512.0f * w->step);
}

static void wave_set_pwm(Wave *w, float p) {
  w->pwm = p > PWM_LIM ? PWM_LIM : p < -PWM_LIM ? -PWM_LIM : p;
}

static void wave_init(Wave *w) {
  memset(w, 0, sizeof *w);
  w->form = W_HOLD;
  wave_set_form(w, W_SAW);
  wave_set_freq(w, 440);
}

/* The fills below advance until the buffer is full; a phase that can't
 * advance (only a NaN could cause one) ends the fill with silence. */
static void zero_rest(float *o, uint32_t i, uint32_t count) {
  for (; i < count; i++) o[i] = 0.0f;
}

static void fill_saw(Wave *w, float *o, uint32_t count, float amp) {
  float amp_add = (amp - w->from_amp) / (float)count;
  amp = w->from_amp;
  for (uint32_t i = 0; i < count;) {
    int wrapped = 0;
    if (w->curr >= 1.0f) {
      float d = (w->curr - 1.0f) / w->step;
      w->curr -= 2.0f;
      o[i++] = amp * (-2.0f * d + 1.0f);
      amp += amp_add;
      w->curr += w->step;
      wrapped = 1;
    }
    float walk_diff = 1.0f - w->curr;
    int walk_steps = f2i((walk_diff / w->step) + 1);
    int steps = (int)(count - i);
    if (steps > walk_steps)
      steps = walk_steps;
    if (steps <= 0 && !wrapped && !(w->curr >= 1.0f)) {
      zero_rest(o, i, count);
      break;
    }
    while (steps > 0) {
      o[i++] = amp * w->curr;
      amp += amp_add;
      w->curr += w->step;
      steps--;
    }
  }
  w->from_amp = amp;
}

static void fill_square(Wave *w, float *o, uint32_t count, float amp) {
  float amp_add = (amp - w->from_amp) / (float)count;
  amp = w->from_amp;
  for (uint32_t i = 0; i < count;) {
    int wrapped = 0;
    if (w->curr >= 1.0f) {
      float d = (w->curr - 1.0f) / w->step;
      w->curr -= 1.0f;
      if (w->pwm_lo) {
        o[i] = amp * d + ((1.0f - d) * amp);
        w->curr -= w->pwm;
      } else {
        o[i] = -amp * d + ((1.0f - d) * amp);
        w->curr += w->pwm;
      }
      w->pwm_lo = !w->pwm_lo;
      i++;
      amp += amp_add;
      w->curr += w->step;
      wrapped = 1;
    }
    float walk_diff = 1.0f - w->curr;
    int walk_steps = f2i((walk_diff / w->step) + 1);
    int steps = (int)(count - i);
    if (steps > walk_steps)
      steps = walk_steps;
    if (steps <= 0 && !wrapped && !(w->curr >= 1.0f)) {
      zero_rest(o, i, count);
      break;
    }
    /* (a negative step count writes nothing but moves amp and curr back) */
    float tmp_amp, tmp_amp_add;
    if (w->pwm_lo) {
      tmp_amp = -amp;
      tmp_amp_add = -amp_add;
    } else {
      tmp_amp = amp;
      tmp_amp_add = amp_add;
    }
    for (int counter = steps; counter > 0; counter--) {
      o[i++] = tmp_amp;
      tmp_amp += tmp_amp_add;
    }
    amp += (float)steps * amp_add;
    w->curr += (float)steps * w->step;
  }
  w->from_amp = amp;
}

static void fill_tri(Wave *w, float *o, uint32_t count, float amp) {
  float a_step = (amp - w->from_amp) / (float)count;
  amp = w->from_amp;
  for (uint32_t i = 0; i < count; i++) {
    o[i] = amp * (2.0f * ((w->curr_val > 0.0f) ? -w->curr_val : w->curr_val) + 1.0f);
    w->curr_val += w->step;
    if (w->curr_val > 1.0f)
      w->curr_val -= 2.0f;
    amp += a_step;
  }
  w->from_amp = amp;
}

static void fill_noise(Wave *w, float *o, uint32_t count, float amp) {
  float amp_add = (amp - w->from_amp) / (float)count;
  amp = w->from_amp;
  for (uint32_t i = 0; i < count;) {
    if (w->curr >= 1.0f) {
      w->curr -= 2.0f;
      w->noise_val = amp * (((float)jng_rand() / (64.0f * 256.0f * 256.0f * 256.0f)) - 1.0f);
    }
    float walk_diff = 1.0f - w->curr;
    int walk_steps = f2i((walk_diff / w->step) + 1);
    int steps = (int)(count - i);
    if (steps > walk_steps)
      steps = walk_steps;
    if (steps <= 0 && !(w->curr >= 1.0f)) {
      zero_rest(o, i, count);
      break;
    }
    for (int counter = steps; counter > 0; counter--)
      o[i++] = w->noise_val;
    w->curr += (float)steps * w->step;
    amp += (float)steps * amp_add;
  }
  w->from_amp = amp;
  if (w->curr >= 1.0f)
    w->curr -= 2.0f;
}

static void fill_sin(Wave *w, float *o, uint32_t count, float amp) {
  float a_step = (amp - w->from_amp) / (float)count;
  amp = w->from_amp;
  for (uint32_t i = 0; i < count; i++) {
    w->sin_curr_val += w->sin_step;
    uint32_t pos = w->sin_curr_val >> 23;
    float dec = (float)(w->sin_curr_val & ((1u << 23) - 1)) / (float)((1 << 23) - 1);
    float val0 = sint[pos];
    float val1 = sint[pos + 1];
    float val = (val1 * dec) + (val0 * (1.0f - dec));
    o[i] = amp * val;
    amp += a_step;
  }
  w->from_amp = amp;
}

static void fill_tri_u(Wave *w, float *o, uint32_t count, float amp) {
  float a_step = (amp - w->from_amp) / (float)count;
  amp = w->from_amp;
  for (uint32_t i = 0; i < count; i++) {
    w->sin_curr_val += w->sin_step;
    o[i] = amp * trit[w->sin_curr_val >> 23];
    amp += a_step;
  }
  w->from_amp = amp;
}

static void fill_sin_u(Wave *w, float *o, uint32_t count, float amp) {
  float a_step = (amp - w->from_amp) / (float)count;
  amp = w->from_amp;
  for (uint32_t i = 0; i < count; i++) {
    w->sin_curr_val += w->sin_step;
    o[i] = amp * sint[w->sin_curr_val >> 23];
    amp += a_step;
  }
  w->from_amp = amp;
}

static int wave_next(Wave *w, float *o, uint32_t count) {
  if (w->amp < 0.001f)
    return 0;
  switch (w->form) {
    case W_SAW: fill_saw(w, o, count, w->amp); break;
    case W_SQR: fill_square(w, o, count, w->amp); break;
    case W_NOS: fill_noise(w, o, count, w->amp); break;
    case W_TRI: fill_tri(w, o, count, w->amp); break;
    case W_SIN: fill_sin(w, o, count, w->amp); break;
    case W_TRIU: fill_tri_u(w, o, count, w->amp); break;
    case W_SINU: fill_sin_u(w, o, count, w->amp); break;
    default: return 0;
  }
  return 1;
}

/* ── InsPly (the instrument player) ──────────────────────────────────────── */

typedef struct {
  const Song *song;
  Lfo vib, pwm;
  float v_amp, p_amp, pwm_offs;
  Wave w;
  const InsStep *curr_step;
  int step_c;                 /* byte in NostalgicPlayer; never exceeds 255 */
  int next_s;
  const Ins *curr_ins;
  int trigged;
  float curr_amp, amp_step;
  int adsr, next_adsr;        /* adsr / f_adsr: see the README (sbyte there) */
  float curr_f, f_step;
  int f_adsr, next_f_adsr;
  float ins_freq;
  float curr_part_freq, curr_part_amp, curr_part_co;
  float res, amp, cut_off;
  float lo, hi, bp, bs;
} InsPly;

static void ip_set_reso(InsPly *ip, float a) {
  ip->res = 1.0f - a;
  ip->res *= ip->res;
  ip->res = 1.0f - ip->res;
}

static void ip_trig_adsr(InsPly *ip, int i) {
  ip->pwm_offs = 0.0f;
  ip->trigged = 1;
  ip->curr_ins = &ip->song->ins[i];
  lfo_set_freq(&ip->vib, (float)ip->curr_ins->vib_s * 50.0f);
  ip->v_amp = (float)ip->curr_ins->vib_d / 2000.0f;
  lfo_set_freq(&ip->pwm, (float)ip->curr_ins->pwm_s * 5.0f);
  ip->p_amp = (float)ip->curr_ins->pwm_d / 255.1f;
  ip_set_reso(ip, (float)ip->curr_ins->res / 255.0f);
  ip->adsr = 0;
  ip->next_adsr = 0;
  ip->f_adsr = 0;
  ip->next_f_adsr = 0;
  ip->step_c = 0;
  ip->next_s = 0;
}

static void ip_init(InsPly *ip, const Song *song) {
  memset(ip, 0, sizeof *ip);
  lfo_init(&ip->vib);
  lfo_init(&ip->pwm);
  wave_init(&ip->w);
  ip->song = song;
  ip->curr_ins = &song->ins[0];
  ip->curr_step = &song->ins[0].steps[0];   /* C#: a zeroed InsStep until the first Next */
  ip->ins_freq = 440.0f;
  ip->curr_part_freq = 440.0f;
  ip_trig_adsr(ip, 0);
}

static void filt_svf(InsPly *ip, float *b, uint32_t count, int out) {
  for (uint32_t i = 0; i < count; i++) {
    float f = b[i];
    float t = ip->lo + ip->cut_off * ip->bp;
    ip->hi = f - ip->lo - (1.8f - ip->res * 1.8f) * ip->bp;
    ip->bp += ip->cut_off * ip->hi;
    if (t < -ip->amp)
      ip->lo = -ip->amp;
    else if (t > ip->amp)
      ip->lo = ip->amp;
    else
      ip->lo = t;
    ip->bs = ip->lo + ip->hi;
    b[i] = out == 3 ? ip->lo : out == 4 ? ip->hi : out == 5 ? ip->bp : ip->bs;
  }
}

static int ip_next(InsPly *ip, float *buffer, uint32_t count) {
  const Ins *ci = ip->curr_ins;
  if (ip->next_s < 1) {
    ip->curr_step = &ci->steps[ip->step_c];
    ip->next_s = ci->sps;
    if (ip->curr_step->note != 0)
      ip->ins_freq = ip->curr_step->relative ? r2f[ip->curr_step->note] : n2f[ip->curr_step->note];
    if (ip->curr_step->wform != 0)
      wave_set_form(&ip->w, ip->curr_step->wform);
    ip->step_c++;
    if (ip->step_c >= ci->len)
      ip->step_c = ci->loop;
  }
  if (ip->next_adsr < 1) {
    if (ip->adsr < ci->amp_points) {
      ip->next_adsr = 1 + ci->amp[ip->adsr].time;
      ip->amp_step = (((float)ci->amp[ip->adsr].lev / 257.0f) - ip->curr_amp) / (float)ip->next_adsr;
      ip->adsr++;
    } else {
      ip->amp_step = 0.0f;
    }
  }
  if (ip->next_f_adsr < 1) {
    if (ip->f_adsr < ci->filter_points) {
      ip->next_f_adsr = 1 + ci->filter[ip->f_adsr].time;
      float target = (float)ci->filter[ip->f_adsr].lev / 257.0f;
      target *= target * target;
      ip->f_step = (target - ip->curr_f) / (float)ip->next_f_adsr;
      ip->f_adsr++;
    } else {
      ip->f_step = 0.0f;
    }
  }
  ip->next_adsr--;
  ip->next_f_adsr--;
  ip->next_s--;

  if (ip->curr_step->relative)
    wave_set_freq(&ip->w, ip->curr_part_freq * ip->ins_freq * (1.0f + ip->v_amp * lfo_next(&ip->vib)));
  else
    wave_set_freq(&ip->w, ip->ins_freq * (1.0f + ip->v_amp * lfo_next(&ip->vib)));
  wave_set_pwm(&ip->w, ip->pwm_offs + ip->p_amp * lfo_next(&ip->pwm));

  ip->curr_amp += ip->amp_step;
  ip->amp = ip->curr_amp * ip->curr_part_amp;
  ip->curr_f += ip->f_step;
  ip->cut_off = ip->curr_f * ip->curr_part_co;

  ip->w.amp = ip->amp;
  if (ip->trigged) {
    ip->trigged = 0;
    ip->w.from_amp = ip->w.amp;
  }
  if (!wave_next(&ip->w, buffer, count))
    return 0;

  switch (ci->filter_mode) {
    case 1:   /* SLP */
      for (uint32_t i = 0; i < count; i++) {
        float f = buffer[i];
        ip->lo = (ip->cut_off * f) + (ip->lo * (1.0f - ip->cut_off));
        buffer[i] = ip->lo;
      }
      break;
    case 2:   /* OLP */
      for (uint32_t i = 0; i < count; i++) {
        ip->lo = ip->cut_off * buffer[i] + (1.0f - ip->cut_off) * ip->hi;
        ip->lo += (ip->lo - ip->bp) * ip->res;
        ip->bp = ip->hi;
        ip->hi = ip->lo;
        buffer[i] = ip->lo;
      }
      break;
    case 3: case 4: case 5: case 6:   /* LP, HP, BP, BS */
      filt_svf(ip, buffer, count, ci->filter_mode);
      break;
  }

  if (ci->clip_mode == 1) {
    float mul = 2.0f * (float)(1 + ci->boost);
    if (fabsf(mul - 1.0f) < 0.1f) {
      for (uint32_t i = 0; i < count; i++)
        buffer[i] = buffer[i] > 1.0f ? 1.0f : buffer[i] < -1.0f ? -1.0f : buffer[i];
    } else {
      for (uint32_t i = 0; i < count; i++) {
        buffer[i] *= mul;
        buffer[i] = buffer[i] > 1.0f ? 1.0f : buffer[i] < -1.0f ? -1.0f : buffer[i];
      }
    }
  } else if (ci->clip_mode == 2) {
    float mul = 0.7f * (1.3f + (float)ci->boost);
    if (fabsf(mul - 1.0f) < 0.1f) {
      for (uint32_t i = 0; i < count; i++)
        buffer[i] = (float)sin(buffer[i]);
    } else {
      for (uint32_t i = 0; i < count; i++) {
        buffer[i] *= mul;
        buffer[i] = (float)sin(buffer[i] * mul);
      }
    }
  }
  return 1;
}

/* ── Player (one channel's sequencer) ────────────────────────────────────── */

typedef struct {
  int channel;
  const Song *song;
  const Chan *ch;
  InsPly ip;
  const Step *curr_step;
  const Part *curr_part;
  uint32_t seq_count;
  int step_c;                 /* byte; never exceeds 255 */
  uint32_t nexts;
  int tmp_loop;
  float d_amp, amp, amp_step, freq, freq_step, target_freq, cut_off, cut_off_step;
  float *buffer;
} Player;

/* Duration / end bookkeeping: ModulePlayerWithSubSongDurationAgentBase. */
static int channels_done[MAX_CHAN];
static int has_end_reached;

static void end_reached(const Song *s, int channel) {
  channels_done[channel] = 1;
  for (int i = 0; i < s->channel_count; i++)
    if (!channels_done[i])
      return;
  has_end_reached = 1;
  memset(channels_done, 0, sizeof channels_done);
}

static void player_init(Player *p) {
  p->tmp_loop = 0;
  p->amp = 1.0f;
  p->amp_step = 0.0f;
  p->freq = 1.0f;
  p->freq_step = 1.0f;
  p->cut_off = 1.0f;
  p->cut_off_step = 1.0f;
  /* JumpPos(0, 0, 0) */
  p->step_c = 0;
  p->seq_count = 0;
  p->nexts = 0;
  p->curr_part = &p->song->parts[p->ch->steps[0].part];
  p->curr_step = &p->curr_part->steps[0];
  p->d_amp = (255.0f - (float)p->ch->steps[0].damp) / 255.0f;
}

/* The first half of Player.NextBuffer: a new step when one is due, and the
 * tick counted. */
static void player_step(Player *p);

static void player_sequence(Player *p) {
  if (p->nexts < 1)
    player_step(p);
  p->nexts--;
}

static void player_step(Player *p) {
  const Song *s = p->song;
  const ChStep *seq = p->ch->steps;
  p->d_amp = (255.0f - (float)seq[p->seq_count].damp) / 255.0f;
  p->nexts = p->curr_part->sps;
  p->curr_step = &p->curr_part->steps[p->step_c];
  const Step *st = p->curr_step;
  if (st->ins != 0) {
    /* (an instrument past the last one: the silent instrument 0) */
    ip_trig_adsr(&p->ip, st->ins < s->instrument_count ? st->ins : 0);
    p->amp = 1.0f;
    p->amp_step = 0.0f;
    p->cut_off = 1.0f;
    p->cut_off_step = 1.0f;
  }
  if (st->note != 0) {
    int n = st->note + seq[p->seq_count].transp;
    if (n < 0) n = 0;                  /* (NostalgicPlayer: out of its table) */
    if (n > 22 * 12 - 1) n = 22 * 12 - 1;
    p->target_freq = n2f[n];
    p->freq_step = 1.0f;
    if ((st->eff & 0xf0) != 0x30)
      p->freq = p->target_freq;
  }
  const float divider = 50.0f;
  int e = st->eff & 0x0f;
  if (st->eff != 0) {
    switch (st->eff & 0xf0) {
      case 0x10:
        p->target_freq = 44100.0f;
        p->freq_step = 1 + ((float)(e + 1) / (divider * 3));
        break;
      case 0x20:
        p->target_freq = 1.0f;
        p->freq_step = 1 - ((float)(e + 1) / (divider * 3));
        break;
      case 0x30:
        if (p->target_freq > p->freq)
          p->freq_step = 1 + ((float)(e + 1) / divider);
        else
          p->freq_step = 1 - ((float)(e + 1) / divider);
        break;
      case 0x40:
        p->ip.pwm_offs = (float)e / 16.1f;
        break;
      case 0x50:
        ip_set_reso(&p->ip, (float)e / 15.0f);
        break;
      case 0x70:
        p->cut_off_step = 1.0f - (float)(e + 1) / 256.0f;
        break;
      case 0x80:
        p->cut_off_step = 1.0f + (float)(e + 1) / 256.0f;
        break;
      case 0x90:
        p->cut_off_step = 1.0f;
        p->cut_off = (float)(e + 1) / 16.0f;
        p->cut_off *= p->cut_off;
        break;
      case 0xa0:
        p->amp_step = (float)(-(e + 1)) / 256.0f;
        break;
      case 0xb0:
        p->amp_step = (float)(e + 1) / 256.0f;
        break;
      case 0xc0:
        p->amp_step = 0.0f;
        p->amp = (float)e / 15.0f;
        break;
    }
  }
  if (p->tmp_loop) {
    p->tmp_loop = 0;
    end_reached(s, p->channel);
  }
  p->step_c++;
  if (p->step_c >= p->curr_part->len) {
    p->step_c = 0;
    p->seq_count++;
    if (p->seq_count > p->ch->rloop) {
      p->seq_count = p->ch->lloop;
      p->tmp_loop = 1;
    }
    p->curr_part = &s->parts[seq[p->seq_count].part];
  }
}

/* The second half: the per-tick slides and the instrument's samples. */
static int player_synth(Player *p) {
  p->cut_off *= p->cut_off_step;
  if (p->cut_off < 0.0f) {
    p->cut_off = 0.0f;
    p->cut_off_step = 1.0f;
  } else if (p->cut_off > 1.0f) {
    p->cut_off = 1.0f;
    p->cut_off_step = 1.0f;
  }
  p->freq *= p->freq_step;
  if (p->freq_step > 1.0001f) {
    if (p->freq > p->target_freq) {
      p->freq = p->target_freq;
      p->freq_step = 1.0f;
    }
  } else if (p->freq_step < 0.9999f) {
    if (p->freq < p->target_freq) {
      p->freq = p->target_freq;
      p->freq_step = 1.0f;
    }
  }
  p->amp += p->amp_step;
  if (p->amp < 0.0f) {
    p->amp = 0.0f;
    p->amp_step = 0.0f;
  } else if (p->amp > 1.0f) {
    p->amp = 1.0f;
    p->amp_step = 0.0f;
  }
  p->ip.curr_part_amp = p->amp * p->d_amp;
  p->ip.curr_part_freq = p->freq;
  p->ip.curr_part_co = p->cut_off;
  return ip_next(&p->ip, p->buffer, p->song->sps_pal);
}

/* ── Output ──────────────────────────────────────────────────────────────── */

/* DownMixer (stereo to stereo, factor 1): the sample is scaled by a float
 * that drops, for the rest of the song, whenever a scaled sample would not
 * fit 32 bits (float rounding makes the clamped full scale overflow). */
static float adjust_volume_by = 1.0f;

static int32_t down_mix(int32_t src) {
  for (;;) {
    float v = (float)src * adjust_volume_by;
    if (v < 2147483648.0f && v >= -2147483648.0f)
      return (int32_t)v;
    float nv = (float)INT32_MAX / (float)(src < 0 ? -(int64_t)src : (int64_t)src);
    if (nv == adjust_volume_by)
      nv -= 0.0001f;
    adjust_volume_by = nv;
  }
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

/* One line of text without control characters; the name is decoded as
 * NostalgicPlayer decodes it (UTF-8, a malformed sequence becoming U+FFFD). */
static void print_clean(const char *key, const char *str) {
  const unsigned char *s = (const unsigned char *)str;
  printf("%s=", key);
  while (*s) {
    unsigned c = *s;
    int n = c < 0x80 ? 0 : (c & 0xe0) == 0xc0 ? 1 : (c & 0xf0) == 0xe0 ? 2 : (c & 0xf8) == 0xf0 ? 3 : -1;
    if (n == 0) {
      putchar(c < 0x20 || c == 0x7f ? ' ' : (int)c);
      s++;
      continue;
    }
    uint32_t cp = n == 1 ? (c & 0x1f) : n == 2 ? (c & 0x0f) : (c & 0x07);
    int k = 1, ok = n > 0;
    for (; ok && k <= n; k++) {
      unsigned b = s[k];
      /* the second byte's range rules out overlongs, surrogates, > U+10FFFF */
      unsigned lo = 0x80, hi = 0xbf;
      if (k == 1) {
        if (n == 1 && c < 0xc2) ok = 0;
        if (n == 2 && c == 0xe0) lo = 0xa0;
        if (n == 2 && c == 0xed) hi = 0x9f;
        if (n == 3 && c == 0xf0) lo = 0x90;
        if (n == 3 && c == 0xf4) hi = 0x8f;
        if (n == 3 && c > 0xf4) ok = 0;
      }
      if (!ok || b < lo || b > hi) {
        ok = 0;
        break;
      }
      cp = (cp << 6) | (b & 0x3f);
    }
    if (!ok) {
      fputs("\xef\xbf\xbd", stdout);              /* U+FFFD for the maximal subpart */
      s += k > 1 ? k : 1;
      continue;
    }
    if (cp >= 0x80 && cp < 0xa0)
      putchar(' ');                               /* a C1 control character */
    else
      fwrite(s, 1, (size_t)n + 1, stdout);
    s += n + 1;
  }
  putchar('\n');
}

static int usage(void) {
  fprintf(stderr, "usage: sawteethrender [-r rate] [-m max_seconds] [-f fade_ms] [-l loops] "
                  "FILE OUT.wav\n"
                  "       sawteethrender -i [same options] FILE\n"
                  "       (either form: -v VU_FILE also writes the per-channel levels)\n"
                  "       sawteethrender --version\n");
  return 2;
}

/* -v: append one level frame (each channel's peak, 0–1 as 0–255) to *buf,
 * growing it as needed, and start the next window.  0 when out of memory. */
static int vu_emit(unsigned char **buf, uint64_t *cap, uint64_t *frames, float *peak,
                   int *heard, int nch) {
  if (*frames >= *cap) {
    uint64_t ncap = *cap ? *cap * 2 : 1024;
    unsigned char *nb = realloc(*buf, (size_t)(ncap * (uint64_t)nch));
    if (!nb) {
      fprintf(stderr, "sawteethrender: out of memory\n");
      return 0;
    }
    *buf = nb;
    *cap = ncap;
  }
  unsigned char *row = *buf + *frames * (uint64_t)nch;
  for (int c = 0; c < nch; c++) {
    float v = peak[c] > 1.0f ? 1.0f : peak[c];
    row[c] = (unsigned char)(v * 255.0f);
    if (row[c])
      heard[c] = 1;
    peak[c] = 0.0f;
  }
  (*frames)++;
  return 1;
}

static uint8_t *read_file(const char *name, size_t *len) {
  FILE *f = fopen(name, "rb");
  if (!f)
    return NULL;
  size_t cap = 1 << 16, n = 0;
  uint8_t *buf = malloc(cap);
  while (buf) {
    if (n == cap) {
      if (cap >= (size_t)MAX_FILE_BYTES) {
        if (fgetc(f) == EOF && !ferror(f))
          break;                       /* exactly the limit */
        free(buf);
        buf = NULL;
        break;
      }
      uint8_t *nb = realloc(buf, cap * 2);
      if (!nb) {
        free(buf);
        buf = NULL;
        break;
      }
      buf = nb;
      cap *= 2;
    }
    size_t got = fread(buf + n, 1, cap - n, f);
    n += got;
    if (got == 0) {
      if (ferror(f)) {
        free(buf);
        buf = NULL;
      }
      break;
    }
  }
  fclose(f);
  *len = n;
  return buf;
}

int main(int argc, char **argv) {
  long rate = 44100, max_s = 1800, fade_ms = 8000, loops = 1;
  if (argc == 2 && strcmp(argv[1], "--version") == 0) {
    printf("sawteethrender %s (NostalgicPlayer %s)\n", SAWTEETHRENDER_VERSION, NP_COMMIT);
    return 0;
  }
  const char *in = NULL, *out = NULL, *vu_out = NULL;
  int info = 0;
  for (int i = 1; i < argc; i++) {
    if (strcmp(argv[i], "-i") == 0) {
      info = 1;
    } else if (strcmp(argv[i], "-v") == 0 && i + 1 < argc && argv[i + 1][0] != '\0') {
      vu_out = argv[++i];
    } else if (argv[i][0] == '-' && argv[i][1] && argv[i][2] == '\0' && i + 1 < argc
        && strchr("rmfl", argv[i][1])) {
      char *end;
      long v = strtol(argv[i + 1], &end, 10);
      if (*argv[i + 1] == '\0' || *end != '\0')
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
  if (!in || (!info && !out) || (info && out) || rate < 8000 || rate > 48000
      || max_s < 1 || max_s > 7200 || fade_ms < 0 || fade_ms > 60000 || loops < 1 || loops > 16)
    return usage();

  size_t len = 0;
  uint8_t *data = read_file(in, &len);
  if (!data) {
    fprintf(stderr, "sawteethrender: can't read %s\n", in);
    return 1;
  }
  Song song;
  memset(&song, 0, sizeof song);
  int lr = load_song(&song, data, len);
  free(data);
  if (lr != 0) {
    fprintf(stderr, "sawteethrender: %s: %s\n", in,
            lr == 1 ? "not a Sawteeth module" : "a damaged Sawteeth module");
    free_song(&song);
    return 1;
  }

  init_tables();
  const int nch = song.channel_count;
  const uint32_t sps = song.sps_pal;
  /* SawteethWorker.Init: cMul[c] = (1 - 0.1) / c + 0.1 (its table stops at
   * 11 channels; the formula is used for 12 too) */
  const float floor_ = 0.1f;
  float channel_mul = (((1.0f - floor_) / (float)nch) + floor_) / 255.0f;
  float level = 255.0f * channel_mul;
  level *= 32768.0f;

  Player pl[MAX_CHAN];
  int pan[MAX_CHAN];
  float *fbuf = calloc((size_t)nch * sps, sizeof *fbuf);
  int16_t *sbuf = calloc((size_t)nch * sps, sizeof *sbuf);
  const uint32_t fpt = (uint32_t)((uint64_t)rate * sps / SYNTH_RATE);   /* frames per tick */
  const int64_t inc = ((int64_t)SYNTH_RATE << FRAC_BITS) / rate;
  int64_t idx_end = ((int64_t)sps << FRAC_BITS) - 1;
  uint32_t played = (uint32_t)(idx_end / inc + 1);    /* frames before the tick's buffer ends */
  if (played > fpt)
    played = fpt;
  const size_t block_frames = (size_t)rate / 10;
  unsigned char *obuf = malloc((block_frames + fpt) * 4);
  int32_t *mix = malloc((size_t)fpt * 2 * sizeof *mix);
  if (!fbuf || !sbuf || !obuf || !mix || fpt < 1) {
    fprintf(stderr, "sawteethrender: %s\n", fpt < 1 ? "a damaged Sawteeth module" : "out of memory");
    free(fbuf); free(sbuf); free(obuf); free(mix);
    free_song(&song);
    return 1;
  }
  uint64_t frames = 0, max_frames = (uint64_t)max_s * (uint64_t)rate;
  FILE *fp = info ? NULL : fopen(out, "wb");
  int failed = 0;
  if (!info && !fp) {
    fprintf(stderr, "sawteethrender: can't write %s\n", out);
    failed = 1;
  }
  unsigned char h[44];
  wav_header(h, (uint32_t)rate, 0);
  if (fp && (fwrite(h, 1, sizeof h, fp) != sizeof h || fflush(fp) != 0))
    failed = 1;

  /* The channels whose sequence triggers an instrument with a noise step. */
  int noise_ins[256] = { 0 }, noise_ch[MAX_CHAN] = { 0 }, uses_noise = 0;
  for (int i = 1; i < song.instrument_count; i++)
    for (int j = 0; j < song.ins[i].len; j++)
      if (song.ins[i].steps[j].wform == W_NOS)
        noise_ins[i] = 1;
  for (int c = 0; c < nch; c++)
    for (int j = 0; j < song.chan[c].len; j++) {
      const Part *pt = &song.parts[song.chan[c].steps[j].part];
      for (int k = 0; k < pt->len; k++)
        if (pt->steps[k].ins < song.instrument_count && noise_ins[pt->steps[k].ins])
          noise_ch[c] = uses_noise = 1;
    }
  /* (synthesized: a render, or -i with -v) */
  const int synth = fp != NULL || vu_out != NULL;
  for (int pass = !synth || failed || !uses_noise ? 1 : 0; pass < 2; pass++) {
    for (int c = 0; c < nch; c++) {
      memset(&pl[c], 0, sizeof pl[c]);
      pl[c].channel = c;
      pl[c].song = &song;
      pl[c].ch = &song.chan[c];
      pl[c].buffer = fbuf + (size_t)c * sps;
      ip_init(&pl[c].ip, &song);
      player_init(&pl[c]);
      pan[c] = (((256 - song.chan[c].left) * 256) + (256 * song.chan[c].right)) / 512;
    }
    memset(channels_done, 0, sizeof channels_done);
    has_end_reached = 0;
    if (pass == 1)
      break;
    /* NostalgicPlayer calculates the duration first, synthesizing the song
     * to its end (that tick included), and then plays from a snapshot of the
     * fresh players — but the noise generator's seed is static and goes on
     * from where that pass left it.  Only noise draws on the seed, and the
     * seed it ends with depends only on how many draws there were, so only
     * the channels that can play noise are synthesized (and none for a song
     * without noise).  At most PREPASS_SECONDS of song time. */
    uint64_t pre_ticks = (uint64_t)PREPASS_SECONDS * SYNTH_RATE / sps;
    for (uint64_t t = 0; t < pre_ticks; t++) {
      for (int c = 0; c < nch; c++)
        player_sequence(&pl[c]);
      for (int c = 0; c < nch; c++)
        if (noise_ch[c])
          (void)player_synth(&pl[c]);
      if (has_end_reached)
        break;
    }
  }

  uint64_t fade_frames = (uint64_t)fade_ms * (uint64_t)rate / 1000;
  if (fade_frames > max_frames)
    fade_frames = max_frames;
  uint64_t fade_at = max_frames - fade_frames;
  size_t pending = 0;          /* frames in obuf */
  long ends = 0;

  /* -v: per-channel peaks over each 1/VU_RATE s of the output */
  const uint64_t vu_win = (uint64_t)rate / VU_RATE;
  float vu_peak[MAX_CHAN] = { 0 };
  int vu_heard[MAX_CHAN] = { 0 }, active[MAX_CHAN] = { 0 };
  uint64_t vu_fill = 0, vu_frames = 0, vu_cap = 0;
  unsigned char *vu_buf = NULL;

  while (!failed && frames < max_frames) {
    /* SawteethWorker.Play, then the mixer's check of HasEndReached: the tick
     * that reports the end is not played. */
    for (int c = 0; c < nch; c++)
      player_sequence(&pl[c]);
    if (has_end_reached) {
      has_end_reached = 0;
      if (++ends >= loops)
        break;
    }
    uint64_t n = fpt;
    if (frames + n > max_frames)
      n = max_frames - frames;
    if (synth) {
      for (int c = 0; c < nch; c++) {
        int16_t *d = sbuf + (size_t)c * sps;
        active[c] = player_synth(&pl[c]);
        if (active[c]) {
          const float *src = pl[c].buffer;
          for (uint32_t k = 0; k < sps; k++)
            d[k] = f2s(src[k] * level);
        } else {
          memset(d, 0, sps * sizeof *d);
        }
      }
    }
    if (vu_out) {
      for (uint64_t k = 0; k < n && !failed; k++) {
        if (k < played) {
          uint32_t at = (uint32_t)(((int64_t)k * inc) >> FRAC_BITS);
          for (int c = 0; c < nch; c++) {
            float a = active[c] ? fabsf(pl[c].buffer[at]) : 0.0f;
            if (a > vu_peak[c])                    /* (a NaN never is) */
              vu_peak[c] = a;
          }
        }
        if (++vu_fill == vu_win) {
          if (!vu_emit(&vu_buf, &vu_cap, &vu_frames, vu_peak, vu_heard, nch))
            failed = 1;
          vu_fill = 0;
        }
      }
    }
    if (fp) {
      for (uint64_t k = 0; k < n; k++) {
        int64_t l = 0, r = 0;
        if (k < played) {
          uint32_t at = (uint32_t)(((int64_t)k * inc) >> FRAC_BITS);
          for (int c = 0; c < nch; c++) {
            int32_t smp = sbuf[(size_t)c * sps + at];
            l += (int64_t)(256 - pan[c]) * smp;
            r += (int64_t)pan[c] * smp;
          }
        }
        /* MixConvertTo32: << 7, clamped to 32 bits */
        l *= 128;
        r *= 128;
        mix[2 * k] = (int32_t)(l >= 2147483647 ? 2147483647 - 1 : l < -2147483647 ? -2147483647 : l);
        mix[2 * k + 1] = (int32_t)(r >= 2147483647 ? 2147483647 - 1 : r < -2147483647 ? -2147483647 : r);
      }
      /* DownMixer.ConvertToOutputFormat, the left channel then the right */
      for (int side = 0; side < 2; side++)
        for (uint64_t k = 0; k < n; k++)
          mix[2 * k + side] = down_mix(mix[2 * k + side]);
      for (uint64_t k = 0; k < n; k++) {
        /* the top 16 bits of the 32-bit output */
        int32_t lo = mix[2 * k] >> 16, ro = mix[2 * k + 1] >> 16;
        uint64_t at = frames + k;
        if (at >= fade_at && fade_frames > 0) {
          /* (the faded part: linear, to silence at max_seconds) */
          double g = (double)(max_frames - at) / (double)fade_frames;
          lo = (int32_t)(lo * g);
          ro = (int32_t)(ro * g);
        }
        put16(obuf + 4 * pending, (uint16_t)(int16_t)lo);
        put16(obuf + 4 * pending + 2, (uint16_t)(int16_t)ro);
        pending++;
      }
      if (pending >= block_frames) {
        if (fwrite(obuf, 1, pending * 4, fp) != pending * 4 || fflush(fp) != 0)
          failed = 1;                              /* a full disk, a closed pipe */
        pending = 0;
      }
    }
    frames += n;
  }
  if (fp && !failed && pending > 0
      && (fwrite(obuf, 1, pending * 4, fp) != pending * 4 || fflush(fp) != 0))
    failed = 1;
  if (vu_out && !failed && vu_fill > 0
      && !vu_emit(&vu_buf, &vu_cap, &vu_frames, vu_peak, vu_heard, nch))
    failed = 1;
  if (!failed && frames == 0) {
    fprintf(stderr, "sawteethrender: %s: nothing to play\n", in);
    failed = 1;
  }

  if (info) {
    if (!failed) {
      printf("seconds=%.3f\n", (double)frames / (double)rate);
      print_clean("title", song.title);
      print_clean("author", song.author);
      printf("channels=%d\n", nch);
    }
  } else if (fp) {
    uint64_t bytes = frames * 4;
    wav_header(h, (uint32_t)rate, (uint32_t)bytes);
    if (!failed && (fseek(fp, 0, SEEK_SET) != 0 || fwrite(h, 1, sizeof h, fp) != sizeof h))
      failed = 1;
    if (fclose(fp) != 0)
      failed = 1;
    if (failed)
      fprintf(stderr, "sawteethrender: can't write %s\n", out);
  }
  if (vu_out && !failed && frames > 0) {
    unsigned char vh[16];
    memcpy(vh, "VUMR", 4);
    vh[4] = 1;                                     /* version */
    vh[5] = (unsigned char)nch;
    vh[6] = vh[7] = 0;
    put32(vh + 8, VU_RATE);
    put32(vh + 12, (uint32_t)vu_frames);
    unsigned char side[MAX_CHAN];
    for (int c = 0; c < nch; c++) {
      /* the channel's amplitudes on each side (pan 0 left … 256 right) */
      double l = 256 - pan[c], r = pan[c];
      side[c] = !vu_heard[c] ? 0 : l > r * 1.5 ? 1 : r > l * 1.5 ? 2 : 0;
    }
    FILE *vf = fopen(vu_out, "wb");
    if (!vf || fwrite(vh, 1, 16, vf) != 16 || fwrite(side, 1, (size_t)nch, vf) != (size_t)nch
        || (vu_frames && fwrite(vu_buf, (size_t)nch, (size_t)vu_frames, vf) != (size_t)vu_frames))
      failed = 1;
    if (vf && fclose(vf) != 0)
      failed = 1;
    if (failed)
      fprintf(stderr, "sawteethrender: can't write %s\n", vu_out);
  }
  free(vu_buf);
  free(fbuf);
  free(sbuf);
  free(obuf);
  free(mix);
  free_song(&song);
  return failed ? 1 : 0;
}
