/*
 * sidwasm.cpp - C API around libsidplayfp + reSIDfp for the SoniqBoom
 * WebAssembly SID engine (frontend/js/vendor/sidwasm.{js,wasm}).
 *
 * Copyright (C) 2026 S.F. Cyris
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
 *
 * One tune is loaded at a time. Settings made with sid_set_* apply to the
 * next sid_load. Every sid_load builds a new engine, builder and tune; the
 * previous ones are destroyed first. Audio is mono, 16-bit, 44100 Hz, PAL,
 * no ROMs. sid_mute and sid_reg address the first SID chip. On tunes with
 * more than one SID chip the per-voice peaks are the maximum of voice 0, 1
 * and 2 over all chips.
 *
 * The library is built without exception catching: input that libsidplayfp
 * rejects by throwing (malformed or truncated files, unsupported SID
 * addresses, a CPU halt during play) aborts the call with a JavaScript
 * exception instead of returning an error code.
 *
 * Exported functions (C linkage):
 *   int   sid_load(const uint8_t* data, int len, int tune)
 *           tune: 1-based tune number, 0 = the file's start song.
 *           Returns the tune's song count, or 0 on failure (see sid_error).
 *   int   sid_render(int16_t* out, int nsamples)
 *           Renders up to nsamples samples; returns the number produced.
 *   int   sid_render_vu(float* out, int nframes, int spf)
 *           Renders nframes frames of spf samples each, discarding the audio,
 *           and writes 3 floats per frame: the peak |voice output| (pre-filter)
 *           of voices 0..2 over that frame. Returns the frames written; a
 *           frame in which the engine produced fewer than spf samples is the
 *           last one written. Successive calls continue the same tune.
 *   int   sid_render_audio_vu(int16_t* audio, float* vu, int nframes, int spf)
 *           As sid_render_vu, and also writes the frame's spf audio samples to
 *           audio + frame*spf (samples the engine did not produce are 0).
 *   void  sid_mute(int voice, int mute)
 *           voice 0..2 of the loaded tune; nonzero mutes, 0 unmutes.
 *   void  sid_set_model(int model)
 *           0 = use the tune's SID model, 1 = force MOS6581, 2 = force MOS8580.
 *   void  sid_set_sampling(int method)
 *           0 = interpolate, nonzero = resample (RESAMPLE_INTERPOLATE).
 *   void  sid_set_power_delay(int delay)
 *           SidConfig::powerOnDelay (stored as a 16-bit value); 0 by default.
 *           Values 0..8191 give a fixed delay, 8192 and above a random one.
 *   int   sid_reg(int n)
 *           Last value written to register n (0..31) of SID chip 0,
 *           or -1 when nothing is loaded or n is out of range.
 *   int   sid_is_playing(void)
 *   int   sid_cur_song(void)     current 1-based song, 0 when nothing is loaded
 *   int   sid_songs(void)        song count, 0 when nothing is loaded
 *   int   sid_time_ms(void)      playback time in ms, 0 when nothing is loaded
 *   const char* sid_error(void)  message for the last failure ("" if none)
 */

#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include <sidplayfp/sidplayfp.h>
#include <sidplayfp/SidConfig.h>
#include <sidplayfp/SidInfo.h>
#include <sidplayfp/SidTune.h>
#include <sidplayfp/SidTuneInfo.h>
#include <sidplayfp/builders/residfp.h>

// play(short*, count) and isPlaying() are marked deprecated in 2.16; they
// remain the library's sample-count based rendering and state API.
#if defined(__clang__) || defined(__GNUC__)
#  pragma GCC diagnostic ignored "-Wdeprecated-declarations"
#endif

// Per-voice level tap added to reSIDfp by patches/0001-residfp-per-voice-vu-tap.patch.
extern bool sb_vu_tap;
extern float sb_vu_peak[3];

namespace
{

sidplayfp*      g_engine  = nullptr;
ReSIDfpBuilder* g_builder = nullptr;
SidTune*        g_tune    = nullptr;
std::string     g_error;

int g_sampling   = 0;   // 0 = INTERPOLATE, else RESAMPLE_INTERPOLATE
int g_powerDelay = 0;
int g_model      = 0;   // 0 = tune default, 1 = force 6581, 2 = force 8580

void release()
{
    // The engine holds the builder's emulations: destroy it first.
    delete g_engine;  g_engine  = nullptr;
    delete g_builder; g_builder = nullptr;
    delete g_tune;    g_tune    = nullptr;
}

int fail(const char* msg)
{
    g_error = msg ? msg : "unknown error";
    release();
    return 0;
}

void resetPeaks()
{
    sb_vu_peak[0] = sb_vu_peak[1] = sb_vu_peak[2] = 0.f;
}

void storePeaks(float* dst)
{
    dst[0] = sb_vu_peak[0];
    dst[1] = sb_vu_peak[1];
    dst[2] = sb_vu_peak[2];
}

// Render one frame of spf samples into buf with the tap on; writes the
// frame's per-voice peaks to vu and returns the samples produced.
int renderFrame(short* buf, int spf, float* vu)
{
    resetPeaks();
    const int got = static_cast<int>(g_engine->play(buf, static_cast<uint_least32_t>(spf)));
    storePeaks(vu);
    return got;
}

} // namespace

extern "C" {

int sid_load(const uint8_t* data, int len, int tune)
{
    g_error.clear();
    release();

    if (data == nullptr || len <= 0)
        return fail("empty tune buffer");

    g_tune = new SidTune(data, static_cast<uint_least32_t>(len));
    if (!g_tune->getStatus())
    {
        std::string msg = "SidTune parse failed: ";
        msg += g_tune->statusString();
        return fail(msg.c_str());
    }
    g_tune->selectSong(tune > 0 ? static_cast<unsigned int>(tune) : 0u);

    g_engine = new sidplayfp();

    g_builder = new ReSIDfpBuilder("residfp");
    g_builder->create(g_engine->info().maxsids());
    if (!g_builder->getStatus())
        return fail(g_builder->error());
    g_builder->filter(true);

    SidConfig cfg = g_engine->config();
    cfg.frequency      = 44100;
    cfg.playback       = SidConfig::MONO;
    cfg.samplingMethod = g_sampling ? SidConfig::RESAMPLE_INTERPOLATE
                                    : SidConfig::INTERPOLATE;
    cfg.fastSampling   = false;
    cfg.powerOnDelay   = static_cast<uint_least16_t>(g_powerDelay);
    if (g_model == 1 || g_model == 2)
    {
        cfg.defaultSidModel = (g_model == 2) ? SidConfig::MOS8580 : SidConfig::MOS6581;
        cfg.forceSidModel   = true;
    }
    else
    {
        cfg.forceSidModel = false;
    }
    cfg.sidEmulation = g_builder;

    if (!g_engine->config(cfg))
        return fail(g_engine->error());
    if (!g_engine->load(g_tune))
        return fail(g_engine->error());

    return static_cast<int>(g_tune->getInfo()->songs());
}

void sid_set_power_delay(int delay)
{
    g_powerDelay = delay;
}

void sid_set_model(int model)
{
    g_model = model;
}

void sid_set_sampling(int method)
{
    g_sampling = method;
}

int sid_songs(void)
{
    return g_tune ? static_cast<int>(g_tune->getInfo()->songs()) : 0;
}

int sid_cur_song(void)
{
    return g_tune ? static_cast<int>(g_tune->getInfo()->currentSong()) : 0;
}

void sid_mute(int voice, int mute)
{
    if (g_engine == nullptr || voice < 0 || voice > 2)
        return;
    // sidplayfp::mute(sid, voice, enable) forwards `enable` to
    // sidemu::voice(voice, mute): true mutes the voice.
    g_engine->mute(0, static_cast<unsigned int>(voice), mute != 0);
}

int sid_render(int16_t* out, int nsamples)
{
    if (out == nullptr || nsamples <= 0 || g_engine == nullptr)
        return 0;
    return static_cast<int>(g_engine->play(out, static_cast<uint_least32_t>(nsamples)));
}

int sid_render_vu(float* out, int nframes, int spf)
{
    if (out == nullptr || nframes <= 0 || spf <= 0 || g_engine == nullptr)
        return 0;

    std::vector<short> buf(static_cast<size_t>(spf), 0);
    int frames = 0;
    sb_vu_tap = true;
    while (frames < nframes)
    {
        const int got = renderFrame(buf.data(), spf, out + frames * 3);
        frames++;
        if (got < spf)
            break;
    }
    sb_vu_tap = false;
    return frames;
}

int sid_render_audio_vu(int16_t* audio, float* vu, int nframes, int spf)
{
    if (audio == nullptr || vu == nullptr || nframes <= 0 || spf <= 0 || g_engine == nullptr)
        return 0;

    int frames = 0;
    sb_vu_tap = true;
    while (frames < nframes)
    {
        short* dst = audio + static_cast<size_t>(frames) * spf;
        const int got = renderFrame(dst, spf, vu + frames * 3);
        frames++;
        if (got < spf)
        {
            std::memset(dst + (got > 0 ? got : 0), 0,
                        static_cast<size_t>(spf - (got > 0 ? got : 0)) * sizeof(short));
            break;
        }
    }
    sb_vu_tap = false;
    return frames;
}

int sid_time_ms(void)
{
    return g_engine ? static_cast<int>(g_engine->timeMs()) : 0;
}

int sid_is_playing(void)
{
    return (g_engine && g_engine->isPlaying()) ? 1 : 0;
}

int sid_reg(int n)
{
    if (g_engine == nullptr || n < 0 || n > 31)
        return -1;
    uint8_t regs[32];
    if (!g_engine->getSidStatus(0, regs))
        return -1;
    return regs[n];
}

const char* sid_error(void)
{
    return g_error.c_str();
}

} // extern "C"
