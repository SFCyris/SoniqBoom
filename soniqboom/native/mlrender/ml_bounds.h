/* SPDX-FileCopyrightText: 2026 S.F. Cyris
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * ml_bounds — the float buffers musicline_playback may read waveform data
 * from, for the bounds checks musicline_playback.patch adds to it (README.md,
 * "Changes to the replayer"): the module's samples (added as LoadMod
 * allocates them, dropped as FreeMod frees them), each channel's effect wave
 * buffers and ZeroSample.  A read the replayer would make outside them reads
 * silence instead. */
#pragma once

#include <cstddef>
#include <cstdint>
#include <map>

namespace ml_bounds {

// The largest region `src` stands zeros in for: an effect wave buffer.
constexpr size_t kZeros = 4096;

inline std::map<uintptr_t, size_t>& spans() {
    static std::map<uintptr_t, size_t> m;   // start address → length in floats
    return m;
}

inline void add(const float* p, size_t n) {
    if (p)
        spans()[(uintptr_t)p] = n;
}

inline void drop(const float* p) {
    spans().erase((uintptr_t)p);
}

// Do the `n` floats from address `a` lie within one buffer?  (n 0: nothing
// is read.)
inline bool ok_addr(uintptr_t a, size_t n) {
    if (n == 0)
        return true;
    auto& m = spans();
    auto it = m.upper_bound(a);
    if (it == m.begin())
        return false;
    --it;
    uintptr_t end = it->first + it->second * sizeof(float);
    return a >= it->first && a < end && n <= (end - a) / sizeof(float);
}

inline bool ok(const float* p, size_t n) {
    return ok_addr((uintptr_t)p, n);
}

// The `n` floats `off` floats on from `base` (computed without forming the
// pointer).
inline bool ok_at(const float* base, long off, size_t n) {
    return ok_addr((uintptr_t)base + (uintptr_t)(intptr_t)off * sizeof(float), n);
}

// A mixer channel playing `size` floats from `p`, then looping from float
// `loop` on (negative: no loop) to `size` (just that float when it is past
// `size`); the mixers take the loop start as a 32-bit index.
inline bool dma_ok(const float* p, unsigned long size, long loop) {
    if (!ok(p, size))
        return false;
    if (loop < 0)
        return true;
    unsigned long at = (uint32_t)loop;
    return ok_at(p, (long)at, at < size ? size - at : 1);
}

// `p` when its `n` floats lie within one buffer, else as many zeros (n is at
// most kZeros).
inline float* src(float* p, size_t n) {
    static float zeros[kZeros];
    return ok(p, n) ? p : zeros;
}

}  // namespace ml_bounds
