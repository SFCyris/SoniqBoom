# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render- and playback-time options of the retro renderers (2.3.0).

* **Amiga resampler** (``renderers.amiga_resampler``): libopenmpt's Amiga
  (Paula) resampler for the module formats libopenmpt treats as Amiga
  (ProTracker and the other Amiga trackers; libopenmpt leaves every other
  format alone).  "auto" (libopenmpt picks the A500 or A1200 filter by the
  format — the default), "a500", "a1200" or "off" (libopenmpt's normal
  interpolation, the render before 2.3.0).  A render-time option: it is part
  of the tracker cache key (``tracker_key_parts``), so a change re-renders.
* **Stereo width** (``renderers.stereo_width``): the mid/side width of a
  retro tune (rendered formats) at PLAYBACK, in percent — 100 is the render
  as it is, 0 mono, above 100 wider.  The cached render stays the bit-exact
  reference: the web player applies it in its Web Audio chain (``player.js``),
  Subsonic / DLNA / cast through an ffmpeg ``pan`` stage on the stream they
  are sent (``ffmpeg_width_filter``), keyed by ``width_variant``.
* **Loops and endings** per family (``renderers.tracker_loops`` /
  ``tracker_fade``, ``chip_loops`` / ``chip_fade``, ``sid_fade``): how many
  times a looping tune plays and how long it fades out, where the engine
  supports it.  0 loops / a negative fade mean the engine's default — the
  render before 2.3.0 — and add nothing to a cache key, so every existing
  entry stays valid; any other value is part of the key.

Pure helpers over ``settings``; no I/O.
"""
from __future__ import annotations

import math

from soniqboom.config import settings

# ── Amiga resampler ─────────────────────────────────────────────────────────

AMIGA_TYPES = ("auto", "a500", "a1200")
AMIGA_CHOICES = AMIGA_TYPES + ("off",)
AMIGA_DEFAULT = "auto"


def normalize_amiga(value) -> str:
    """A setting value as one of ``AMIGA_CHOICES`` (anything else: the default)."""
    v = str(value or "").strip().lower()
    return v if v in AMIGA_CHOICES else AMIGA_DEFAULT


def amiga_resampler() -> str:
    """The Amiga resampler in force: "auto" / "a500" / "a1200" / "off"."""
    return normalize_amiga(getattr(settings, "amiga_resampler", AMIGA_DEFAULT))


def openmpt_ctl_args() -> list[str]:
    """``openmpt123`` arguments for the Amiga resampler setting (none when
    off).  ``render.resampler.emulate_amiga`` only changes the formats
    libopenmpt treats as Amiga modules."""
    t = amiga_resampler()
    if t == "off":
        return []
    return ["--ctl", "render.resampler.emulate_amiga=1",
            "--ctl", f"render.resampler.emulate_amiga_type={t}"]


# ── Loops and endings ───────────────────────────────────────────────────────

FAMILIES = ("tracker", "chip", "sid")
LOOPS_MAX = 16
FADE_MAX_S = 30.0
# The engines' own endings (what "default" means, for the UI and the plans):
# libopenmpt ends a song with a 100 ms fade; libgme plays a VGM's loop twice
# and fades every capped / looping tune over 8 s; kssrender plays a KSS song
# until it loops once, then fades over 8 s; sidplayfp stops at the length.
TRACKER_DEFAULT_FADE_S = 0.1
CHIP_DEFAULT_LOOPS = 2
CHIP_DEFAULT_FADE_S = 8.0


def normalize_loops(value) -> int:
    """A loop count setting: 0 (the engine's default) or 1..``LOOPS_MAX``."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if 1 <= n <= LOOPS_MAX else 0


def normalize_fade(value, *, allow_default: bool = True) -> float:
    """A fade setting in seconds: -1 (the engine's default; only with
    ``allow_default``) or 0..``FADE_MAX_S``, rounded to 0.1 s."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return -1.0 if allow_default else 0.0
    if not math.isfinite(f) or f < 0:
        return -1.0 if allow_default else 0.0
    return round(min(FADE_MAX_S, f), 1)


def loops(family: str) -> int:
    """The loop count set for ``family`` ("tracker" / "chip"), 0 = default."""
    return normalize_loops(getattr(settings, f"{family}_loops", 0))


def fade(family: str) -> float:
    """The fade set for ``family`` in seconds; -1 = the engine's default
    ("sid": 0 = none, its default)."""
    if family == "sid":
        return normalize_fade(getattr(settings, "sid_fade", 0.0), allow_default=False)
    return normalize_fade(getattr(settings, f"{family}_fade", -1.0))


def _fmt(x: float) -> str:
    """A number for a cache key (a file name): no dot — 2.5 → "2p5"."""
    return f"{x:g}".replace(".", "p")


def tracker_key_parts() -> list[str]:
    """Cache-key parts of a tracker render for the settings in force: the
    Amiga resampler (``a<type>``, none when off) and a loop count / fade
    other than the default (``l<n>``, ``f<s>``).  Empty — the key before
    2.3.0 — for resampler off and default endings."""
    parts: list[str] = []
    t = amiga_resampler()
    if t != "off":
        parts.append(f"am{t}")
    n, f = tracker_ending()
    if n:
        parts.append(f"l{n}")
    if f is not None:
        parts.append(f"f{_fmt(f)}")
    return parts


def tracker_ending() -> "tuple[int, float | None]":
    """``(loops, fade)`` of a tracker render: ``(0, None)`` for the default
    ending (once, libopenmpt's 100 ms fade); else the loop count (1 when only
    the fade was set) and the fade in seconds."""
    n, f = loops("tracker"), fade("tracker")
    if n <= 1 and (f < 0 or abs(f - TRACKER_DEFAULT_FADE_S) < 1e-9):
        return 0, None
    return max(1, n), (TRACKER_DEFAULT_FADE_S if f < 0 else f)


def chip_key_parts() -> list[str]:
    """Cache-key parts of a libgme / kssrender render: a loop count / fade
    other than the default (``l<n>``, ``f<s>``)."""
    parts: list[str] = []
    n, f = loops("chip"), fade("chip")
    if n:
        parts.append(f"l{n}")
    if f >= 0 and abs(f - CHIP_DEFAULT_FADE_S) > 1e-9:
        parts.append(f"f{_fmt(f)}")
    return parts


def chip_loops(default: int = CHIP_DEFAULT_LOOPS) -> int:
    """How many times a libgme / kssrender tune with a loop plays its loop
    (``default``: the engine's own count)."""
    return loops("chip") or default


def chip_fade_s() -> float:
    """The fade of a libgme / kssrender tune, seconds."""
    f = fade("chip")
    return CHIP_DEFAULT_FADE_S if f < 0 else f


def sid_fade_s() -> float:
    """Seconds a SID fades out over at the end of its length (HVSC's, which
    stays the length): 0 = none, sidplayfp's default."""
    return fade("sid")


def sid_key_parts() -> list[str]:
    """Cache-key part of a SID render's fade (``fo<s>``, none without)."""
    f = sid_fade_s()
    return [f"fo{_fmt(f)}"] if f > 0 else []


# ── Stereo width (playback) ─────────────────────────────────────────────────

WIDTH_MIN, WIDTH_MAX = 0, 200
WIDTH_DEFAULT = 100
# The headphone preset: a hard-panned voice (an Amiga channel) heard at 80 %
# on its own side and 20 % on the other — the other ear 12 dB down, about
# what a pair of speakers lets each ear hear of the far one.
WIDTH_HEADPHONES = 60


def normalize_width(value) -> int:
    """A width setting in whole percent, ``WIDTH_MIN``..``WIDTH_MAX``
    (anything unreadable: ``WIDTH_DEFAULT``)."""
    try:
        w = float(value)
    except (TypeError, ValueError):
        return WIDTH_DEFAULT
    if not math.isfinite(w):
        return WIDTH_DEFAULT
    return int(round(max(WIDTH_MIN, min(WIDTH_MAX, w))))


def stereo_width() -> int:
    """The stereo width set for retro tunes, percent (100 = as rendered)."""
    return normalize_width(getattr(settings, "stereo_width", WIDTH_DEFAULT))


def width_gains(percent: int) -> "tuple[float, float]":
    """``(same, cross)``: the mid/side width ``percent`` as a 2×2 matrix,
    L' = same·L + cross·R and R' = cross·L + same·R — the mid (L+R)/2 kept,
    the side (L−R)/2 scaled by ``percent`` / 100.  (1, 0) at 100."""
    w = normalize_width(percent) / 100.0
    return (1.0 + w) / 2.0, (1.0 - w) / 2.0


def ffmpeg_width_filter(percent: int) -> str:
    """The ffmpeg filter applying ``percent`` width to a stream (``pan``,
    exact; a mono input is made stereo first — its side is 0, so it stays
    the same on both channels), or "" at 100 (nothing to do)."""
    p = normalize_width(percent)
    if p == WIDTH_DEFAULT:
        return ""
    a, b = width_gains(p)
    return (f"aformat=channel_layouts=stereo,"
            f"pan=stereo|c0={a:.6g}*c0+{b:.6g}*c1|c1={b:.6g}*c0+{a:.6g}*c1")


def width_variant(percent: int) -> str:
    """The transcode-key suffix of a width (``-w<p>``; "" at 100), so a
    transcode made at one width is never served at another."""
    p = normalize_width(percent)
    return "" if p == WIDTH_DEFAULT else f"-w{p}"


def width_af_args(args: "list[str]", percent: int) -> "list[str]":
    """ffmpeg output ``args`` with the width stage appended to the filter
    chain (merged into an ``-af`` already there).  Unchanged at 100."""
    flt = ffmpeg_width_filter(percent)
    if not flt:
        return list(args)
    out = list(args)
    if "-af" in out:
        i = out.index("-af") + 1
        out[i] = f"{out[i]},{flt}"
        return out
    return ["-af", flt] + out


# ── uade's stereo ───────────────────────────────────────────────────────────

# uade123 post-processes its output by default: ``--panning`` 0.7 (its pan
# effect L' = L(1 - p/2) + R·p/2 is exactly a mid/side width of 1 - p: 30 %)
# and, as SoniqBoom asked until 2.3.0, ``--headphones`` (a crossfeed).
# Measured on JamCracker "bump set spike": side/mid 0.14 with both, 0.93 for
# Paula's own hard stereo.  Since 2.3.0 the render is Paula's output
# (``--panning=0``, no headphone effect) — the reference, like openmpt's
# Amiga modules — and the stereo width setting narrows it at playback like
# every other retro tune: one setting, applied once.  A key part, so the
# narrowed renders cached before are never served as the reference.
UADE_STEREO_ARGS = ("--panning=0",)
UADE_KEY_PART = "pan0"


# ── Endless play ────────────────────────────────────────────────────────────

# An endless stream is a WAV of this many seconds (the 4 GB WAV limit holds
# 3.1 hours of 48 kHz float stereo): the engine plays on through the tune's
# own loop for that long — a listener moves on long before.
ENDLESS_SECONDS = 3 * 3600


def public_config() -> dict:
    """The options the web player reads (``/api/ui-config``)."""
    return {"stereo_width": stereo_width(),
            "stereo_width_headphones": WIDTH_HEADPHONES}
