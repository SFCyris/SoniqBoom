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

# ── uade / Amiga exotica (Paula) ──────────────────────────────────────────────
# uade renders ~175 exotic Amiga formats, each stored under uade's RUNTIME
# playername ("TFMX Pro", "SoundMon 2.0", "Rob Hubbard", "Jochen Hippel ST", …).
# They're not a clean static family — the runtime playername varies (version
# suffixes, spacing) from uade's conf-derived labels — so we recognise them from
# uade's own label table (``uade_formats.format_labels()``) UNIONed with a
# curated supplement for the runtime variants that table misses.  All map to the
# Paula chip family.  (A durable per-track family marker stamped at scan time
# would be fully robust; this string match is best-effort but covers the classic
# composers + every uade format observed in a large real library.)
_AMIGA_EXOTICA = frozenset({
    "Paul Tonge", "Paul Robotham", "DIGI Booster", "Zound Monitor", "PreTracker",
    "Musicline Editor", "MusiclineEditor", "Protracker and family",
    "ProTracker (packed)", "ArtOfNoise (4ch)", "UFO", "Future Composer",
    "FutureComposer 1.3", "FutureComposer 1.4", "Core Design", "Protracker4",
    "Tronic", "Amiga Custom", "Sonix Music Driver", "Forgotten Worlds Game",
    "Speedy System", "Jochen Hippel", "Jochen Hippel ST", "Hippel-COSO",
    "Sonic Arranger", "Mike Davies", "AMOS", "Ashley Hogg", "Ben Daglish",
    "Benn Daglish", "David Whittaker", "Rob Hubbard", "TFMX Pro", "SoundMon 2.0",
    "DeltaMusic 2.0", "FredMonitor", "Amiga",
})

_uade_retro_cache: frozenset[str] | None = None


def _uade_retro_formats() -> frozenset[str]:
    """uade/Amiga-exotica format labels recognised as retro — the curated
    supplement UNIONed with uade's own conf-derived labels (lazy + cached, so a
    missing uade install just yields the curated set; no import-time I/O)."""
    global _uade_retro_cache
    if _uade_retro_cache is None:
        labels = set(_AMIGA_EXOTICA)
        try:
            from soniqboom.core import uade_formats as _uf
            labels |= _uf.format_labels()
        except Exception:                               # noqa: BLE001
            pass
        _uade_retro_cache = frozenset(labels)
    return _uade_retro_cache


def is_retro_format(fmt: str | None) -> bool:
    """True when *fmt* is a chip/tracker/synth/Amiga-exotica format (its own
    similarity universe).  Covers the static families AND uade's Amiga formats
    (TFMX, Hippel, Hubbard, Whittaker, ProWizard …), whose dynamic playernames
    aren't in the static set."""
    return bool(fmt) and (fmt in RETRO_FORMATS or fmt in _uade_retro_formats())


def chip_family(fmt: str | None) -> str | None:
    """Coarse sound-chip family for *fmt* (``sid``/``tracker``/``paula``/…), or None."""
    if not fmt:
        return None
    fam = _FAMILY.get(fmt)
    if fam is not None:
        return fam
    return "paula" if fmt in _uade_retro_formats() else None


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
