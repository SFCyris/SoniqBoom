# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retro / chip / tracker format taxonomy for retro-aware similarity.

Retro music (SID, tracker modules, console chip rips, AdLib/OPL, Amiga custom
players, MIDI) is a different universe from modern recorded audio: it barely
shares genre tags, its "bpm"/"year" are unreliable, and its perceived similarity
runs along composer / sound-chip / replayer / sample-set lineage — as the
canonical archives organise it (Modland = format→author, Demozoo/AMP =
composer + scene-group + platform).  This module gives the similarity engine:

  * ``is_retro_format`` — the segregation gate (retro seeds match retro only)
  * ``chip_family``     — the "same sound chip" axis (SID ≠ Paula ≠ 2A03 ≠ SPC …)
  * ``instrument_tokens`` — normalised sample/instrument names for the tracker
    lineage signal (shared samples / a composer's copied signature block reveal
    the same hands / scene / sample library)

Format NAMES mirror the frontend taxonomy (frontend/js/utils.js) so client and
server agree; SID and MIDI are added (they aren't in the frontend chip sets).
"""
from __future__ import annotations

import re as _re

# ── Format-name → chip family ────────────────────────────────────────────────
# Sample-based trackers (Amiga/PC): one sonic family — PCM samples via Paula/SB.
_TRACKER = {
    "ProTracker", "ScreamTracker 3", "ScreamTracker 2", "FastTracker 2",
    "Impulse Tracker", "MultiTracker", "OctaMED", "Composer 669",
    "DigiBooster Pro", "UltraTracker", "Farandole", "ASYLUM/DMP",
    "General DigiMusic", "Imago Orpheus", "Oktalyzer", "SoundFX",
    "Grave Composer", "DSIK",
}
_AHX = {"AHX", "HivelyTracker"}                       # Amiga synth (not sample)
_ADLIB = {
    "AdLib IMF", "AdLib ROL", "Creative Music", "EdLib", "Reality AdLib",
    "LucasArts AdLib", "Sierra AdLib", "DOSBox OPL", "HSC AdLib", "RIX OPL",
    "AdLib Tracker 2", "AdLib", "Bob's AdLib", "Ken's AdLib", "AMUSIC AdLib",
}
_ATARI = {"SNDH", "YM", "SC68"}                       # Atari ST YM2149
_PSF = {"PSF", "PSF2", "USF", "GSF", "2SF", "SSF", "DSF (Dreamcast)", "NCSF"}
_SID = {"SID"}
_MIDI = {"MIDI", "General MIDI"}
# Distinct single-chip console/computer formats — each its OWN family.
_SINGLE = {
    "NSF": "nes", "NSFe": "nes", "SPC": "snes", "GBS": "gameboy",
    "AY": "ay", "KSS": "ay", "SAP": "pokey", "GYM": "genesis",
    "HES": "pce", "VGM": "vgm", "VGZ": "vgm",
}


def _build_family() -> dict[str, str]:
    m: dict[str, str] = {}
    for f in _TRACKER:
        m[f] = "tracker"
    for f in _AHX:
        m[f] = "ahx"
    for f in _ADLIB:
        m[f] = "adlib"
    for f in _ATARI:
        m[f] = "atari"
    for f in _PSF:
        m[f] = "psf"
    for f in _SID:
        m[f] = "sid"
    for f in _MIDI:
        m[f] = "midi"
    m.update(_SINGLE)
    return m


_FAMILY = _build_family()
RETRO_FORMATS = frozenset(_FAMILY.keys())


def is_retro_format(fmt: str | None) -> bool:
    """True when *fmt* is a chip/tracker/synth format (its own similarity universe)."""
    return bool(fmt) and fmt in RETRO_FORMATS


def chip_family(fmt: str | None) -> str | None:
    """Coarse sound-chip family for *fmt* (``sid``/``tracker``/``nes``/…), or None."""
    return _FAMILY.get(fmt) if fmt else None


_TOK = _re.compile(r"[^a-z0-9]+")


def instrument_tokens(instruments) -> frozenset[str]:
    """Normalised WHOLE instrument/sample names for the tracker lineage signal.

    Whole (not word-split) names keep the signal specific: two modules sharing
    ``syntom1 snd`` share a sample, and a composer's copied signature block
    (``tune made by mace warp inc``) matches verbatim across their modules —
    both strong "same hands / scene / sample library" evidence.  Names shorter
    than 3 chars after normalising are dropped as noise.
    """
    if not isinstance(instruments, (list, tuple)):
        return frozenset()
    out: set[str] = set()
    for name in instruments:
        if not isinstance(name, str):
            continue
        n = " ".join(_TOK.sub(" ", name.lower()).split())
        if len(n) >= 3:
            out.add(n)
    return frozenset(out)
