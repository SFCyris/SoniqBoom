/* SPDX-FileCopyrightText: 2026 S.F. Cyris
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * mlrender — render one subsong of a Musicline Editor module ("MLEDMODL")
 * to a 16-bit stereo WAV with musicline_playback, the C++ port of the
 * Musicline Editor 1.16 replayer (in DEViLBOX:
 * third-party/musicline_playback-main; GPL-2.0-or-later as DEViLBOX states it).
 *
 *   mlrender [-s subsong] [-r rate] [-m max_seconds] [-f fade_ms]
 *            [-p separation] FILE OUT.wav
 *   mlrender -i [-s subsong] [-r rate] [-m max_seconds] [-f fade_ms]
 *            [-p separation] FILE
 *   mlrender --version
 *
 * A subsong plays until every channel has reached its end command, or until
 * the replayer is back in a state it has played before (the song loops: it
 * then fades over `fade_ms`, default 8000), or for `max_seconds` (default
 * 600; the last `fade_ms` fade).  Once audio has started, output that stays
 * digitally silent (every sample 0) for 20 s ends the song there, keeping
 * 0.5 s of the silence; a subsong silent for its first 30 s ends at 30 s.
 * The rate is 8000 to 48000 Hz; a tune whose tempo is under 32 (the slowest
 * the replayer's tick buffers hold) is not played.
 * `-p` sets the stereo separation, 0 (mono) to 1 (the Amiga's hard left /
 * right, the default).  Subsongs are numbered from 0.
 *
 * The WAV is written as it renders (a 44-byte header first, its sizes patched
 * at the end), flushed every 0.1 s, so a reader can follow it; a silent
 * stretch is written once the sound resumes.  OUT.wav "-" writes to standard
 * output (the header's sizes then say "to the end of the stream").
 *
 * `-i` renders without writing and prints the subsong count and, per subsong
 * (all of them, or the one `-s` names), the length a render with the same
 * options has and why it ends:
 *
 *   subsongs 2
 *   subsong 0 266.038 silence Xenex-Remix
 *   subsong 1 11.860 loop The Beat
 *
 * (the end: "end", "loop", "silence" or "max"; the name runs to the end of
 * the line).
 *
 * A module whose chunks the replayer's loader would read or store out of
 * bounds (a length past the end of the file, more tunes, channels, steps,
 * instruments or samples than it holds, a part or arpeggio number past its
 * lists, packed sample data cut short) is refused with "mlrender: damaged
 * Musicline Editor module: <why>".  An unpacked sample (of at most 128 KB)
 * cut short by the end of the file plays with the missing bytes as silence,
 * and stderr has "mlrender: warning: truncated sample data (N bytes
 * missing)".  Exit status 0 on success, 1 on a file it can't read, load, play
 * or write (and when out of memory), 2 on usage, 3 on a subsong the module
 * doesn't have ("mlrender: no subsong N (the module has M)"). */

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <new>
#include <string>
#include <vector>

#include "mline_backend.h"
#include "module.h"

#ifndef MLRENDER_VERSION
#define MLRENDER_VERSION "1.1"
#endif
#ifndef MUSICLINE_COMMIT
#define MUSICLINE_COMMIT "unknown"
#endif

namespace {

const uint64_t kSilenceEndMs = 20000;  // digital silence that ends a song...
const uint64_t kSilenceTailMs = 500;   // ...of which this much is kept
const uint64_t kLeadSilenceMs = 30000; // a subsong silent this long from its start ends
// Frames asked of the replayer at a time: its ring buffer holds three of its
// longest ticks, so a request must stay within two of them (over 700 frames at 8 kHz).
const uint32_t kChunk = 256;
// The slowest tempo the tempo effect sets; build.sh sizes the replayer's
// tick buffers (MAX_INTERNAL_TICK) for it at up to 48 kHz.
const int kMinTempo = 32;

void put32(unsigned char* p, uint32_t v) {
    p[0] = v & 0xff; p[1] = (v >> 8) & 0xff; p[2] = (v >> 16) & 0xff; p[3] = (v >> 24) & 0xff;
}

void put16(unsigned char* p, uint16_t v) {
    p[0] = v & 0xff; p[1] = (v >> 8) & 0xff;
}

// `data_bytes` 0xFFFFFFFF: a stream whose length isn't known.
void wav_header(unsigned char* h, uint32_t rate, uint32_t data_bytes) {
    memcpy(h, "RIFF", 4);
    put32(h + 4, data_bytes == 0xFFFFFFFFu ? data_bytes : 36 + data_bytes);
    memcpy(h + 8, "WAVEfmt ", 8);
    put32(h + 16, 16);
    put16(h + 20, 1);              // PCM
    put16(h + 22, 2);              // stereo
    put32(h + 24, rate);
    put32(h + 28, rate * 4);       // bytes per second
    put16(h + 32, 4);              // block align: 2 channels x 16 bits
    put16(h + 34, 16);
    memcpy(h + 36, "data", 4);
    put32(h + 40, data_bytes);
}

int usage() {
    fprintf(stderr,
            "usage: mlrender [-s subsong] [-r rate] [-m max_seconds] [-f fade_ms] "
            "[-p separation] FILE OUT.wav\n"
            "       mlrender -i [-s subsong] [-r rate] [-m max_seconds] [-f fade_ms] "
            "[-p separation] FILE\n"
            "       mlrender --version\n");
    return 2;
}

struct Options {
    long subsong = -1;            // -1: subsong 0 (render) / every subsong (-i)
    long rate = 44100;
    long max_s = 600;
    long fade_ms = 8000;
    double separation = 1.0;
};

enum class End { Running, SongEnd, Loop, Silence, Max };

const char* end_name(End e) {
    switch (e) {
        case End::SongEnd: return "end";
        case End::Loop: return "loop";
        case End::Silence: return "silence";
        case End::Max: return "max";
        default: return "running";
    }
}

// Where the PCM goes: a WAV file (`fp`), or nowhere (`-i`: only counted).
struct Sink {
    FILE* fp = nullptr;
    uint64_t frames = 0;              // frames written (or counted)
    bool failed = false;
    std::vector<unsigned char> buf;   // LE bytes not yet written
    uint32_t flush_frames = 4410;     // written every this many frames

    void put(const int16_t* lr, uint64_t n) {
        frames += n;
        if (!fp)
            return;
        size_t at = buf.size();
        buf.resize(at + 4 * (size_t)n);
        for (uint64_t i = 0; i < 2 * n; i++)
            put16(&buf[at + 2 * i], (uint16_t)lr[i]);
        if (buf.size() >= 4 * (size_t)flush_frames)
            flush();
    }
    void flush() {
        if (!fp || failed || buf.empty())
            return;
        if (fwrite(buf.data(), 1, buf.size(), fp) != buf.size() || fflush(fp) != 0)
            failed = true;                // a full disk, a closed pipe
        buf.clear();
    }
};

// Render subsong `sub` of the module in `data` into `sink`; what ended it in
// `end`.  False when the module or the subsong can't be loaded.
bool render(const std::vector<uint8_t>& data, int sub, const Options& o, Sink& sink,
            End& end) {
    MlineBackend b;
    if (!b.load(data.data(), data.size()))
        return false;
    if (sub < 0 || sub >= b.get_subsong_count())
        return false;
    MLModule* m = b.get_module();
    m->SetOutputRate((int)o.rate);
    b.set_subsong(sub);
    if (b.get_current_subsong() != sub || m->m_TuneTmp < kMinTempo)
        return false;
    m->SetStereoSeparation((float)o.separation);

    const uint64_t rate = (uint64_t)o.rate;
    const uint64_t max_frames = (uint64_t)o.max_s * rate;
    const uint64_t fade_frames = (uint64_t)o.fade_ms * rate / 1000;
    const uint64_t silence_end = kSilenceEndMs * rate / 1000;
    const uint64_t silence_tail = kSilenceTailMs * rate / 1000;
    const uint64_t lead_silence = kLeadSilenceMs * rate / 1000;

    std::vector<int16_t> chunk(2 * kChunk);
    std::vector<int16_t> quiet;          // the current silent stretch, not yet written
    bool heard = false;                  // a non-zero sample so far
    uint64_t made = 0;                   // frames the replayer produced
    uint64_t fade_at = 0, fade_len = 0;  // a fade from frame `fade_at` over `fade_len`
    bool fading = false;
    End fade_why = End::Running;         // what the fade ends the song as
    end = End::Running;

    while (end == End::Running) {
        uint64_t n = kChunk;
        if (fading && fade_at + fade_len - made < n)
            n = fade_at + fade_len - made;
        if (made + n > max_frames)
            n = max_frames - made;
        if (n == 0) {
            end = fading ? fade_why : End::Max;
            break;
        }
        m->Output(chunk.data(), (u32)(n * 4));
        if (fading) {
            for (uint64_t i = 0; i < n; i++) {
                double g = 1.0 - (double)(made + i - fade_at) / (double)fade_len;
                chunk[2 * i] = (int16_t)(chunk[2 * i] * g);
                chunk[2 * i + 1] = (int16_t)(chunk[2 * i + 1] * g);
            }
        }
        made += n;
        bool silent = true;
        for (uint64_t i = 0; i < 2 * n; i++) {
            if (chunk[i] != 0) {
                silent = false;
                break;
            }
        }
        if (silent && !fading) {
            quiet.insert(quiet.end(), chunk.begin(), chunk.begin() + 2 * n);
            uint64_t q = quiet.size() / 2;
            if (heard ? q >= silence_end : q >= lead_silence) {
                // Silent for good: keep the start of the stretch, end there.
                sink.put(quiet.data(), heard ? silence_tail : q);
                quiet.clear();
                end = End::Silence;
                break;
            }
        } else {
            if (!quiet.empty()) {
                sink.put(quiet.data(), quiet.size() / 2);
                quiet.clear();
            }
            if (!silent)
                heard = true;
            sink.put(chunk.data(), n);
        }
        if (sink.failed)
            break;
        if (m->isSongEnd() || m->m_IntMode == 0) {
            end = End::SongEnd;
            break;
        }
        if (!fading) {
            bool looped = m->isLoopDetected();
            if (looped || made + fade_frames >= max_frames) {
                End why = looped ? End::Loop : End::Max;
                if (fade_frames == 0 || made >= max_frames) {
                    end = why;
                    break;
                }
                // A loop fades over `fade_ms` (within the cap); the cap's own
                // fade ends at the cap.
                fading = true;
                fade_why = why;
                fade_at = made;
                fade_len = max_frames - made;
                if (looped && fade_frames < fade_len)
                    fade_len = fade_frames;
                if (!quiet.empty()) {
                    sink.put(quiet.data(), quiet.size() / 2);
                    quiet.clear();
                }
            }
        }
    }
    // A song that ended (end command, loop, cap) in a silent stretch keeps
    // only the start of it.
    if (!quiet.empty()) {
        uint64_t q = quiet.size() / 2;
        sink.put(quiet.data(), heard && q > silence_tail ? silence_tail : q);
    }
    sink.flush();
    return true;
}

// 0: read; 1: can't be read; 2: not a Musicline Editor module.
int read_file(const char* in, std::vector<uint8_t>& data) {
    FILE* f = fopen(in, "rb");
    if (!f)
        return 1;
    uint8_t tmp[65536];
    size_t got;
    while ((got = fread(tmp, 1, sizeof tmp, f)) > 0) {
        data.insert(data.end(), tmp, tmp + got);
        if (data.size() > 64u * 1024 * 1024) {      // far larger than any module
            fclose(f);
            return 2;
        }
    }
    bool err = ferror(f) != 0;
    fclose(f);
    if (err)
        return 1;
    return data.size() >= 8 && memcmp(data.data(), "MLEDMODL", 8) == 0 ? 0 : 2;
}

uint32_t be32(const uint8_t* p) {
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

uint16_t be16(const uint8_t* p) {
    return (uint16_t)(p[0] << 8 | p[1]);
}

bool chunk_id(const uint8_t* p) {
    static const char* const ids[] = {"MODL", "VERS", "TUNE", "PART",
                                      "ARPG", "INST", "SMPL", "INFO"};
    for (const char* id : ids)
        if (memcmp(p, id, 4) == 0)
            return true;
    return false;
}

// The source bytes Smpl::DeltaDePacker reads to unpack `size` samples from
// `src` (`avail` bytes) with command byte `cmd`; more than `avail` when the
// packed data runs past it.
uint64_t packed_bytes(const uint8_t* src, uint64_t avail, int8_t cmd, uint32_t size) {
    uint64_t n = 0;
    while (size != 0) {
        if (n >= avail)
            return avail + 1;
        int8_t delta = (int8_t)src[n++];
        if (delta != cmd) {
            size--;
            continue;
        }
        if (n + 3 > avail)
            return avail + 1;
        uint32_t count = (uint32_t)src[n + 1] << 8 | src[n + 2];
        n += 3;
        size--;
        while (count != 0) {
            if (n >= avail)
                return avail + 1;
            n++;
            if (size == 0)
                break;
            size--;
            if (--count == 0 || size == 0)
                break;
            size--;
            count--;
        }
    }
    return n;
}

// The longest unpacked sample a module cut short inside it still plays with
// (a sample states its length in 16-bit words).
const uint64_t kMaxCutSample = 131072;

// A module the replayer's loader (MLModule::LoadMod) would read or write out
// of bounds on: the reason it is refused, or "" when it can be loaded.  It
// walks the chunks as LoadMod does.  An unpacked sample (of at most
// kMaxCutSample bytes) that the end of the file cuts short loads with the
// rest silent; `missing` counts the bytes it lacks.
std::string check_module(const std::vector<uint8_t>& d, uint64_t& missing) {
    missing = 0;
    char why[160];
    const uint8_t* p = d.data() + 4;      // past "MLED", as LoadMod starts
    uint64_t len = d.size() - 4;
    int tunes = 0, insts = 0, smpls = 0;
    while (len >= 4) {
        while (len >= 4 && !chunk_id(p)) {
            p++;
            len--;
        }
        if (len < 4)
            break;
        char id[5] = {(char)p[0], (char)p[1], (char)p[2], (char)p[3], 0};
        p += 4;
        len -= 4;
        if (len < 4) {
            snprintf(why, sizeof why, "%s chunk cut short", id);
            return why;
        }
        uint64_t clen = be32(p);
        if (strcmp(id, "MODL") == 0 && memcmp(p, "VERS", 4) == 0)
            continue;                     // LoadMod reads a MODL with no length
        p += 4;
        len -= 4;
        if (strcmp(id, "TUNE") == 0) {
            // LoadMod takes a TUNE's length from its channel lists, not clen.
            if (tunes >= MAX_TUNES)
                return "more than 256 tunes";
            if (len < tune_LOADSIZE)
                return "TUNE chunk cut short";
            unsigned ch = p[tune_Channels];
            if (ch > MAXCHANS) {
                snprintf(why, sizeof why, "tune %d has %u channels (at most %d)", tunes, ch,
                         MAXCHANS);
                return why;
            }
            p += tune_LOADSIZE;
            len -= tune_LOADSIZE;
            if (len < 4u * ch)
                return "TUNE chunk cut short";
            const uint8_t* lens = p;
            p += 4 * ch;
            len -= 4 * ch;
            for (unsigned c = 0; c < ch; c++) {
                uint64_t cl = be32(lens + 4 * c);
                if (cl > len) {
                    snprintf(why, sizeof why, "tune %d channel %u runs past the end", tunes, c);
                    return why;
                }
                if (cl / 2 > 256) {
                    snprintf(why, sizeof why, "tune %d channel %u has %llu steps (at most 256)",
                             tunes, c, (unsigned long long)(cl / 2));
                    return why;
                }
                p += cl;
                len -= cl;
            }
            tunes++;
        } else if (strcmp(id, "PART") == 0) {
            // LoadMod moves on by the bytes the part's rows take, not clen.
            if (clen > len || len < 2)
                return "PART chunk runs past the end";
            unsigned num = be16(p);
            if (num >= 1024) {
                snprintf(why, sizeof why, "part number %u (at most 1023)", num);
                return why;
            }
            uint64_t o = 2;
            for (int row = 0; row < 128; row++) {       // a part's 128 rows
                if (o >= len)
                    return "PART chunk runs past the end";
                unsigned pak = p[o++];
                if (pak & 0x80)
                    break;
                o += (pak & 1) ? 2 : 0;
                for (int j = 0; j < 5; j++)
                    o += (pak & (2 << j)) ? 2 : 0;
                if (o > len)
                    return "PART chunk runs past the end";
            }
            p += o;
            len -= o;
        } else if (strcmp(id, "ARPG") == 0) {
            if (clen > len || len < 2)
                return "ARPG chunk runs past the end";
            unsigned num = be16(p);
            if (num >= 256) {
                snprintf(why, sizeof why, "arpeggio number %u (at most 255)", num);
                return why;
            }
            if (clen >= 2 && (clen - 2) / 6 > 128) {
                snprintf(why, sizeof why, "arpeggio %u has %llu steps (at most 128)", num,
                         (unsigned long long)((clen - 2) / 6));
                return why;
            }
            p += clen;
            len -= clen;
        } else if (strcmp(id, "INST") == 0) {
            if (insts >= MAX_INSTURUMENTS - 1)
                return "more than 255 instruments";
            if (clen >= len || len < inst_SIZEOF)
                return "INST chunk runs past the end";
            p += clen;
            len -= clen;
            insts++;
        } else if (strcmp(id, "SMPL") == 0) {
            if (smpls >= 255)
                return "more than 255 samples";
            if (len < 6 + smpl_SIZEOF || clen < smpl_SIZEOF)
                return "SMPL chunk cut short";
            uint32_t orig = be32(p);
            int8_t cmd = (int8_t)p[4];
            p += 6 + smpl_SIZEOF;
            len -= 6 + smpl_SIZEOF;
            uint64_t pack = clen - smpl_SIZEOF;
            uint64_t avail = len < pack ? len : pack;
            // Unpacked: LoadMod reads `avail` bytes; packed: DeltaDePacker
            // reads on until it has `orig` samples.
            bool cut = orig == pack ? pack > len && pack > kMaxCutSample
                                    : packed_bytes(p, len, cmd, orig) > len;
            if (cut) {
                snprintf(why, sizeof why, "sample %d's data runs past the end", smpls + 1);
                return why;
            }
            if (orig == pack && pack > len)
                missing += pack - len;
            p += avail;
            len -= avail;
            smpls++;
        } else {
            // MODL, VERS, INFO (LoadMod reads an INFO within clen).
            if (clen > len) {
                snprintf(why, sizeof why, "%s chunk runs past the end", id);
                return why;
            }
            p += clen;
            len -= clen;
        }
    }
    if (tunes == 0)
        return "no tune";
    return "";
}

std::string subsong_name(const std::vector<uint8_t>& data, int sub) {
    MlineBackend b;
    if (!b.load(data.data(), data.size()))
        return "";
    const char* raw = b.get_subsong_name(sub);
    std::string s;
    for (int i = 0; raw && i < 32 && raw[i]; i++) {
        unsigned char c = (unsigned char)raw[i];
        s += (c >= 0x20 && c != 0x7f) ? (char)c : ' ';
    }
    while (!s.empty() && s.back() == ' ')
        s.pop_back();
    return s;
}

}  // namespace

static int run(int argc, char** argv) {
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        printf("mlrender %s (musicline_playback %s)\n", MLRENDER_VERSION, MUSICLINE_COMMIT);
        return 0;
    }
    Options o;
    bool info = false;
    const char *in = nullptr, *out = nullptr;
    for (int i = 1; i < argc; i++) {
        const char* a = argv[i];
        if (strcmp(a, "-i") == 0) {
            info = true;
        } else if (a[0] == '-' && a[1] && a[2] == '\0' && strchr("srmfp", a[1])) {
            if (i + 1 >= argc)
                return usage();
            char* endp = nullptr;
            const char* v = argv[++i];
            if (a[1] == 'p') {
                o.separation = strtod(v, &endp);
            } else {
                long n = strtol(v, &endp, 10);
                switch (a[1]) {
                    case 's': o.subsong = n; break;
                    case 'r': o.rate = n; break;
                    case 'm': o.max_s = n; break;
                    case 'f': o.fade_ms = n; break;
                }
            }
            if (endp == v || *endp)
                return usage();
        } else if (!in) {
            in = a;
        } else if (!out) {
            out = a;
        } else {
            return usage();
        }
    }
    if (!in || (info ? out != nullptr : out == nullptr) || o.subsong < -1 || o.subsong > 255
        || o.rate < 8000 || o.rate > 48000 || o.max_s < 1 || o.max_s > 3600
        || o.fade_ms < 0 || o.fade_ms > 60000 || !(o.separation >= 0.0 && o.separation <= 1.0))
        return usage();

    std::vector<uint8_t> data;
    int rd = read_file(in, data);
    if (rd == 1) {
        fprintf(stderr, "mlrender: can't open %s\n", in);
        return 1;
    }
    if (rd == 2) {
        fprintf(stderr, "mlrender: not a Musicline Editor module: %s\n", in);
        return 1;
    }
    uint64_t missing = 0;
    std::string why = check_module(data, missing);
    if (!why.empty()) {
        fprintf(stderr, "mlrender: damaged Musicline Editor module: %s\n", why.c_str());
        return 1;
    }
    if (missing)
        fprintf(stderr, "mlrender: warning: truncated sample data (%llu bytes missing)\n",
                (unsigned long long)missing);
    int count;
    {
        MlineBackend probe;
        if (!probe.load(data.data(), data.size())) {
            fprintf(stderr, "mlrender: can't load this Musicline Editor module: %s\n", in);
            return 1;
        }
        count = probe.get_subsong_count();
    }
    if (o.subsong >= count) {
        fprintf(stderr, "mlrender: no subsong %ld (the module has %d)\n", o.subsong, count);
        return 3;
    }

    if (info) {
        printf("subsongs %d\n", count);
        int lo = o.subsong >= 0 ? (int)o.subsong : 0;
        int hi = o.subsong >= 0 ? (int)o.subsong + 1 : count;
        for (int s = lo; s < hi; s++) {
            Sink sink;
            End end;
            if (!render(data, s, o, sink, end)) {
                fprintf(stderr, "mlrender: can't play subsong %d\n", s);
                return 1;
            }
            printf("subsong %d %.3f %s %s\n", s, (double)sink.frames / (double)o.rate,
                   end_name(end), subsong_name(data, s).c_str());
        }
        return 0;
    }

    int sub = o.subsong >= 0 ? (int)o.subsong : 0;
    bool to_stdout = strcmp(out, "-") == 0;
    FILE* fp = to_stdout ? stdout : fopen(out, "wb");
    if (!fp) {
        fprintf(stderr, "mlrender: can't open %s for writing\n", out);
        return 1;
    }
    unsigned char h[44];
    wav_header(h, (uint32_t)o.rate, to_stdout ? 0xFFFFFFFFu : 0);
    Sink sink;
    sink.fp = fp;
    sink.flush_frames = (uint32_t)o.rate / 10;
    sink.failed = fwrite(h, 1, sizeof h, fp) != sizeof h || fflush(fp) != 0;
    End end = End::Running;
    bool ok = !sink.failed && render(data, sub, o, sink, end);
    bool failed = sink.failed;
    if (!to_stdout) {
        wav_header(h, (uint32_t)o.rate, (uint32_t)(sink.frames * 4));
        if (fseek(fp, 0, SEEK_SET) != 0 || fwrite(h, 1, sizeof h, fp) != sizeof h)
            failed = true;
        if (fclose(fp) != 0)
            failed = true;
    }
    if (failed) {
        fprintf(stderr, "mlrender: write error on %s\n", out);
        return 1;
    }
    if (!ok) {
        fprintf(stderr, "mlrender: can't play subsong %d of %s\n", sub, in);
        return 1;
    }
    fprintf(stderr, "mlrender: subsong %d: %.3f s (%s)\n", sub,
            (double)sink.frames / (double)o.rate, end_name(end));
    return 0;
}

int main(int argc, char** argv) {
    try {
        return run(argc, argv);
    } catch (const std::bad_alloc&) {
        fprintf(stderr, "mlrender: out of memory\n");
    } catch (const std::exception& e) {
        fprintf(stderr, "mlrender: %s\n", e.what());
    }
    return 1;
}
