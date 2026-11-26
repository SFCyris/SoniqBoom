/* SPDX-FileCopyrightText: 2026 S.F. Cyris
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * oplvoices — the per-channel side of an AdLib / OPL tune, played by AdPlug
 * (libadplug, LGPL-2.1-or-later) through the Nuked OPL3 core that libadplug
 * carries (OPL3_* in nukedopl.h): the same player and emulator as
 * `adplay -e nuked --stereo -O disk`, SoniqBoom's AdLib renderer.
 *
 *   oplvoices [-s N] [-r RATE] [-m MASK] [-t MAXSECS] [-w OUT.wav]
 *             [-v OUT.vu] [-c OUT.raw] FILE
 *   oplvoices --version
 *
 *   -s N        subsong (as adplay -s; without it the player's own default)
 *   -r RATE     output rate in Hz (default 49716, the chip's own rate)
 *   -m MASK     the OPL channels to hear (bit c = channel c, 0-17); every
 *               other channel is left out of the mix (default: all)
 *   -t MAXSECS  stop after this many seconds (default 3600)
 *   -w OUT.wav  write the mix as a 16-bit stereo WAV ("-": standard output,
 *               its sizes then left at 0xFFFFFFFF)
 *   -v OUT.vu   write each channel's level, 30 times a second, as a VUMR v1
 *               sidecar (docs/vu-cache-format.md) with the channel names
 *   -c OUT.raw  write each channel's output as 18 interleaved 16-bit
 *               little-endian mono samples per frame, at RATE ("-": stdout)
 *
 * Playback follows adplay's disk writer exactly: the player's update() runs
 * whenever the sample counter (freq per tick, refresh * samples per sample,
 * in adplay's float arithmetic) goes below zero, the emulator renders in
 * blocks of at most 512 samples, rounded to 4 samples per tick, and the song
 * ends with the 512-sample block in which the player reports its end.  So
 * the mix with every channel heard is the reference render sample for
 * sample (`adplay -q -O disk -e nuked --stereo -f RATE`).
 *
 * A channel is left out of the mix by clearing its output-enable masks
 * (cha/chb/chc/chd — the bits register C0-C8 sets in OPL3 mode, always on
 * in OPL2 mode) before every sample, so the chip emulates exactly as before
 * and the mix is the reference mix without that channel's term.
 *
 * Per-channel output (-v, -c) is read after every sample from the operator
 * outputs each channel's mix term is built from (*out[0..3], masked by the
 * left / right enables as the mix masks them): exactly each channel's own
 * share of the mix (the mix with only that channel heard), one chip sample
 * (20 microseconds) ahead of the left output and two ahead of the right
 * (Nuked OPL3's own output delays).  -v takes the peak of
 * max(|left|, |right|) over each 1/30 s window, scaled so the loudest
 * window of any channel is 255, and each channel's side from its summed
 * left and right magnitudes (left / right when one is 1.5 times the other,
 * else centre), keeping only the channels that made a sound; its names are
 * "OPL <n>" (n = 1-18, the channel number), with the percussion a channel
 * played in rhythm mode.
 *
 * Exit status: 0 on success, 1 when AdPlug can't read FILE or an output
 * can't be written, 2 on usage.
 */

#include <adplug/adplug.h>
#include <adplug/database.h>
#include <adplug/nukedopl.h>
#include <adplug/opl.h>

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <sys/stat.h>
#include <vector>

#define OPLVOICES_VERSION "1.0"

static const int kChannels = 18;

// Nuked OPL3 1.8 has four output enables per channel (cha … chd), older
// cores two: clear whichever the libadplug headers declare.
template <typename C> static auto clear_cd(C *ch, int) -> decltype(ch->chc, void()) {
  ch->chc = ch->chd = 0;
}
template <typename C> static void clear_cd(C *, long) {}
static const long kBlock = 512;          // adplay's disk writer buffer, in samples
static const int kVuRate = 30;

class TapOpl : public Copl {
 public:
  TapOpl(int rate, uint32_t mask, bool taps)
      : rate_(rate), mask_(mask), taps_(taps) {
    currType = TYPE_OPL3;
    OPL3_Reset(&chip_, rate_);
  }

  void write(int reg, int val) override {
    OPL3_WriteRegBuffered(&chip_, (uint16_t)((currChip << 8) | reg), (uint8_t)val);
  }

  void init() override { OPL3_Reset(&chip_, rate_); }

  // ``buf``: n stereo samples.  With taps on, ``tap`` gets n * 18 channel
  // samples (the left + right term each channel added, halved) and
  // ``tap_pk`` n * 18 levels (max(|left term|, |right term|)).
  void update(short *buf, int n) override {
    for (int i = 0; i < n; i++) {
      if (mask_ != 0x3FFFF) {
        for (int c = 0; c < kChannels; c++) {
          if (!(mask_ & (1u << c))) {
            opl3_channel *ch = &chip_.channel[c];
            ch->cha = ch->chb = 0;
            clear_cd(ch, 0);
          }
        }
      }
      OPL3_GenerateResampled(&chip_, (int16_t *)&buf[2 * i]);
      if (taps_) tap_sample();
    }
  }

  // Per-channel results of the samples rendered since the last take.
  std::vector<int16_t> tap;               // 18 per sample (mono: (L + R) / 2)
  std::vector<uint16_t> tap_pk;           // 18 per sample: max(|L|, |R|)
  double sumL[kChannels] = {0}, sumR[kChannels] = {0};
  bool rhythm_used[3] = {false, false, false};
  bool sounded[kChannels] = {false};

 private:
  void tap_sample() {
    const bool rhy = (chip_.rhy & 0x20) != 0;
    for (int c = 0; c < kChannels; c++) {
      opl3_channel *ch = &chip_.channel[c];
      // The channel's mix term, as Nuked OPL3 forms it: an int16_t sum of
      // its operator outputs, masked by the channel's output enables.
      int16_t accm = (int16_t)(*ch->out[0] + *ch->out[1] + *ch->out[2] + *ch->out[3]);
      int32_t l = (int16_t)(accm & ch->cha);
      int32_t r = (int16_t)(accm & ch->chb);
      if (!(mask_ & (1u << c))) l = r = 0;
      int32_t al = l < 0 ? -l : l, ar = r < 0 ? -r : r;
      int32_t pk = al > ar ? al : ar;
      tap_pk.push_back((uint16_t)pk);
      sumL[c] += al;
      sumR[c] += ar;
      if (pk) {
        sounded[c] = true;
        if (rhy && c >= 6 && c <= 8) rhythm_used[c - 6] = true;
      }
      tap.push_back((int16_t)((l + r) / 2));
    }
  }

  opl3_chip chip_;
  int rate_;
  uint32_t mask_;
  bool taps_;
};

static void put32(std::vector<uint8_t> &b, uint32_t v) {
  for (int i = 0; i < 4; i++) b.push_back((uint8_t)(v >> (8 * i)));
}

static void wav_header(FILE *f, int rate, uint32_t data_bytes) {
  std::vector<uint8_t> h;
  const char *riff = "RIFF", *wave = "WAVEfmt ", *data = "data";
  h.insert(h.end(), riff, riff + 4);
  put32(h, data_bytes == 0xFFFFFFFFu ? 0xFFFFFFFFu : 36 + data_bytes);
  h.insert(h.end(), wave, wave + 8);
  put32(h, 16);
  h.push_back(1); h.push_back(0);        // PCM
  h.push_back(2); h.push_back(0);        // stereo
  put32(h, (uint32_t)rate);
  put32(h, (uint32_t)rate * 4);
  h.push_back(4); h.push_back(0);
  h.push_back(16); h.push_back(0);
  h.insert(h.end(), data, data + 4);
  put32(h, data_bytes);
  fwrite(h.data(), 1, h.size(), f);
}

static bool host_little_endian() {
  const uint16_t one = 1;
  return *(const uint8_t *)&one == 1;
}

static void le16(int16_t *s, size_t n) {
  if (host_little_endian()) return;
  for (size_t i = 0; i < n; i++) {
    uint16_t v = (uint16_t)s[i];
    s[i] = (int16_t)((v >> 8) | (v << 8));
  }
}

static void usage() {
  fprintf(stderr,
          "usage: oplvoices [-s N] [-r RATE] [-m MASK] [-t MAXSECS] [-w OUT.wav]\n"
          "                 [-v OUT.vu] [-c OUT.raw] FILE\n"
          "       oplvoices --version\n");
}

static void load_databases(CAdPlugDatabase &db) {
  // The databases adplay reads: the user's, then the system one.
  const char *home = getenv("HOME");
  std::vector<std::string> paths;
  if (home && *home) paths.push_back(std::string(home) + "/.adplug/adplug.db");
  paths.push_back("/usr/share/adplug/adplug.db");
  paths.push_back("/usr/local/share/adplug/adplug.db");
  paths.push_back("/opt/homebrew/share/adplug/adplug.db");
  struct stat st;
  bool any = false;
  for (const std::string &p : paths) {
    if (stat(p.c_str(), &st) == 0 && S_ISREG(st.st_mode)) {
      db.load(p);
      any = true;
    }
  }
  if (any) CAdPlug::set_database(&db);
}

int main(int argc, char **argv) {
  int subsong = -1;
  long rate = 49716;
  uint32_t mask = 0x3FFFF;
  double max_secs = 3600;
  const char *wav_out = nullptr, *vu_out = nullptr, *raw_out = nullptr, *file = nullptr;

  for (int i = 1; i < argc; i++) {
    std::string a = argv[i];
    auto need = [&](const char *opt) -> const char * {
      if (i + 1 >= argc) { fprintf(stderr, "oplvoices: %s needs a value\n", opt); usage(); exit(2); }
      return argv[++i];
    };
    if (a == "--version") { printf("oplvoices %s (%s)\n", OPLVOICES_VERSION, CAdPlug::get_version().c_str()); return 0; }
    else if (a == "-s") subsong = atoi(need("-s"));
    else if (a == "-r") rate = atol(need("-r"));
    else if (a == "-m") mask = (uint32_t)strtoul(need("-m"), nullptr, 0) & 0x3FFFF;
    else if (a == "-t") max_secs = atof(need("-t"));
    else if (a == "-w") wav_out = need("-w");
    else if (a == "-v") vu_out = need("-v");
    else if (a == "-c") raw_out = need("-c");
    else if (!a.empty() && a[0] == '-' && a != "-") { fprintf(stderr, "oplvoices: unknown option %s\n", a.c_str()); usage(); return 2; }
    else if (!file) file = argv[i];
    else { usage(); return 2; }
  }
  if (!file || rate < 8000 || rate > 192000 || !(max_secs > 0) || (!wav_out && !vu_out && !raw_out)) {
    usage();
    return 2;
  }

  CAdPlugDatabase db;
  load_databases(db);
  const bool taps = vu_out || raw_out;
  TapOpl opl((int)rate, mask, taps);
  CPlayer *p = CAdPlug::factory(file, &opl);
  if (!p) { fprintf(stderr, "oplvoices: AdPlug can't read %s\n", file); return 1; }
  if (subsong != -1) p->rewind(subsong);

  FILE *wf = nullptr, *rf = nullptr;
  // Results go to standard output unless the audio does.
  FILE *info = ((wav_out && strcmp(wav_out, "-") == 0) || (raw_out && strcmp(raw_out, "-") == 0))
               ? stderr : stdout;
  bool wav_seekable = false;
  if (wav_out) {
    wf = strcmp(wav_out, "-") == 0 ? stdout : fopen(wav_out, "wb");
    if (!wf) { fprintf(stderr, "oplvoices: can't write %s: %s\n", wav_out, strerror(errno)); return 1; }
    wav_seekable = wf != stdout && fseek(wf, 0, SEEK_CUR) == 0;
    wav_header(wf, (int)rate, wav_seekable ? 0 : 0xFFFFFFFFu);
  }
  if (raw_out) {
    rf = strcmp(raw_out, "-") == 0 ? stdout : fopen(raw_out, "wb");
    if (!rf) { fprintf(stderr, "oplvoices: can't write %s: %s\n", raw_out, strerror(errno)); return 1; }
  }

  // adplay's EmuPlayer::frame(), one 512-sample block per call.
  std::vector<short> block(kBlock * 2);
  long minicnt = 0;
  bool playing = true;
  uint64_t frames_out = 0;
  const uint64_t max_frames = (uint64_t)(max_secs * rate);
  // -v: the window peaks of every channel (window w = frames [w*rate/30, (w+1)*rate/30)).
  std::vector<std::vector<uint16_t>> win(kChannels);
  uint64_t win_start = 0, win_end = (uint64_t)rate / kVuRate;
  int win_peak[kChannels] = {0};
  double sumL[kChannels] = {0}, sumR[kChannels] = {0};
  uint64_t w_index = 0;
  bool ok = true;

  do {
    long towrite = kBlock;
    short *pos = block.data();
    size_t tap0 = 0;
    opl.tap.clear();
    opl.tap_pk.clear();
    while (towrite > 0) {
      while (minicnt < 0) {
        minicnt += rate;
        playing = p->update();
      }
      long i = std::min(towrite, (long)(minicnt / p->getrefresh() + 4) & ~3L);
      opl.update(pos, (int)i);
      pos += i * 2;
      towrite -= i;
      minicnt -= (long)(p->getrefresh() * i);
    }
    if (wf) {
      le16(block.data(), block.size());
      if (fwrite(block.data(), sizeof(short), block.size(), wf) != block.size()) ok = false;
    }
    if (taps) {
      const std::vector<int16_t> &t = opl.tap;
      const size_t n = t.size() / kChannels;
      if (rf) {
        std::vector<int16_t> out(t);
        le16(out.data(), out.size());
        if (fwrite(out.data(), sizeof(int16_t), out.size(), rf) != out.size()) ok = false;
      }
      if (vu_out) {
        for (size_t s = 0; s < n; s++) {
          const uint64_t f = frames_out + s;
          while (f >= win_end) {
            for (int c = 0; c < kChannels; c++) { win[c].push_back((uint16_t)win_peak[c]); win_peak[c] = 0; }
            w_index++;
            win_start = win_end;
            win_end = (uint64_t)((w_index + 1) * (uint64_t)rate / kVuRate);
          }
          for (int c = 0; c < kChannels; c++) {
            int a = opl.tap_pk[tap0 + s * kChannels + c];
            if (a > win_peak[c]) win_peak[c] = a;
          }
        }
      }
      (void)win_start;
    }
    frames_out += kBlock;
    if (!ok) break;
  } while (playing && frames_out < max_frames);

  if (vu_out) {
    // The last, partial window counts.
    bool partial = false;
    for (int c = 0; c < kChannels; c++) if (win_peak[c]) partial = true;
    if (partial || frames_out > win_start) {
      for (int c = 0; c < kChannels; c++) win[c].push_back((uint16_t)win_peak[c]);
    }
    for (int c = 0; c < kChannels; c++) { sumL[c] = opl.sumL[c]; sumR[c] = opl.sumR[c]; }
    std::vector<int> used;
    int top = 0;
    for (int c = 0; c < kChannels; c++) {
      if (!opl.sounded[c]) continue;
      used.push_back(c);
      for (uint16_t v : win[c]) top = std::max(top, (int)v);
    }
    std::vector<uint8_t> vu;
    const char *magic = "VUMR";
    const size_t frames = win.empty() ? 0 : win[0].size();
    if (used.empty() || top <= 0) {
      fprintf(stderr, "oplvoices: the tune made no sound\n");
      ok = false;
    } else {
      vu.insert(vu.end(), magic, magic + 4);
      vu.push_back(1);                              // version
      vu.push_back((uint8_t)used.size());           // channels
      vu.push_back(1);                              // flags: names follow the levels
      vu.push_back(0);
      put32(vu, kVuRate);
      put32(vu, (uint32_t)frames);
      for (int c : used) {
        const double L = sumL[c], R = sumR[c];
        vu.push_back(L + R == 0 ? 0 : L > R * 1.5 ? 1 : R > L * 1.5 ? 2 : 0);
      }
      for (size_t f = 0; f < frames; f++) {
        for (int c : used) vu.push_back((uint8_t)std::min(255L, (long)win[c][f] * 255 / top));
      }
      static const char *drums[3] = {"BD", "SD/HH", "TOM/CY"};
      for (int c : used) {
        std::string name = "OPL " + std::to_string(c + 1);
        if (c >= 6 && c <= 8 && opl.rhythm_used[c - 6]) name += std::string(" ") + drums[c - 6];
        vu.push_back((uint8_t)name.size());
        vu.insert(vu.end(), name.begin(), name.end());
      }
      std::string tmp = std::string(vu_out) + ".tmp";
      FILE *vf = fopen(tmp.c_str(), "wb");
      if (!vf || fwrite(vu.data(), 1, vu.size(), vf) != vu.size() || fclose(vf) != 0
          || rename(tmp.c_str(), vu_out) != 0) {
        fprintf(stderr, "oplvoices: can't write %s: %s\n", vu_out, strerror(errno));
        ok = false;
      }
    }
    if (ok) {
      fprintf(info, "channels=");
      for (size_t k = 0; k < used.size(); k++) fprintf(info, "%s%d", k ? "," : "", used[k]);
      fprintf(info, "\n");
    }
  }
  if (wf) {
    if (wav_seekable && frames_out * 4 < 0xFFFFFFF0ull) {
      if (fseek(wf, 0, SEEK_SET) == 0) wav_header(wf, (int)rate, (uint32_t)(frames_out * 4));
    }
    if (wf != stdout && fclose(wf) != 0) ok = false;
    else if (wf == stdout) fflush(stdout);
  }
  if (rf && rf != stdout && fclose(rf) != 0) ok = false;
  fprintf(info, "frames=%llu\nseconds=%.3f\n", (unsigned long long)frames_out,
          (double)frames_out / rate);
  delete p;
  return ok ? 0 : 1;
}
