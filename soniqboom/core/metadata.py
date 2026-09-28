# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Audio metadata extraction via mutagen + lightweight parsers.

Supported: MP3, FLAC, ALAC/M4A, AAC, Ogg Vorbis, Opus, AIFF, WAV, WavPack, Musepack,
           SID/PSID (C64), MIDI, tracker modules (MOD/S3M/XM/IT and many more).
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import logging
import re
import struct
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Callable

from mutagen import File as MutagenFile
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from mutagen.aiff import AIFF, AIFFInfo
from mutagen.id3 import ID3

from soniqboom.core import forksafe
from soniqboom.models.track import TrackMeta

log = logging.getLogger(__name__)

_EASY_GAME_REGISTERED = False


def register_easy_game_key() -> None:
    """Teach mutagen's easy interface the ``game`` key (process-wide, once):
    an ID3 ``TXXX:GAME`` frame and an MP4 ``----:com.apple.iTunes:GAME`` atom
    (Vorbis comments and APEv2 take any key as it is) — the keys the tag
    editor writes and ``_id3_game`` / ``_mp4`` read.  Run at import, so the
    generic easy-tag reader (``extract``'s fallback) sees a GAME tag in every
    process, not only in one that has written tags."""
    global _EASY_GAME_REGISTERED
    if _EASY_GAME_REGISTERED:
        return
    from mutagen.easyid3 import EasyID3
    from mutagen.easymp4 import EasyMP4Tags
    EasyID3.RegisterTXXXKey("game", "GAME")
    EasyMP4Tags.RegisterFreeformKey("game", "GAME")
    _EASY_GAME_REGISTERED = True


register_easy_game_key()

SUPPORTED_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus",
    ".aiff", ".aif", ".wav", ".wv", ".mpc",
    # SID (C64)
    ".sid", ".psid",
    # MIDI
    ".mid", ".midi",
    # Tracker modules
    ".mod", ".s3m", ".xm", ".it", ".mtm", ".med", ".oct",
    ".669", ".dbm", ".ahx", ".hvl", ".ult", ".stm", ".far",
    ".amf", ".gdm", ".imf", ".okt", ".sfx", ".wow", ".dsm",
    # Retro chiptune via libgme (E-14): NES, SNES, Game Boy,
    # Master System / Genesis, ZX Spectrum, MSX.
    ".nsf", ".nsfe", ".spc", ".gbs", ".vgm", ".vgz",
    ".ay", ".kss", ".sap", ".gym", ".hes",
    # DSD (Direct Stream Digital).  Streamed via ffmpeg transcoding to PCM —
    # the audiophile bit-perfect-to-DAC story belongs to local players like
    # Roon/JRiver, but we can serve the audible content of any DSD library to
    # any browser/Subsonic client.  DFF requires the dsdiff demuxer which is
    # absent from some ffmpeg builds (notably Homebrew 8.x) — startup probe
    # warns if the user has DFF files but the demuxer isn't available.
    ".dsf", ".dff", ".wsd",
    # AdLib / OPL2 FM (AdPlug): id/Apogee IMF rides the ``.imf`` entry above
    # (disambiguated from Imago Orpheus by content), plus the wider family.
    # ``.amd`` = AMUSIC Adlib Tracker — Modland files it under ``Ad Lib/`` and
    # AdPlug plays it; note names like ``star.amd`` collide with uade's
    # ProWizard ``star`` prefix, so the AdLib extension must win (see extract()).
    ".rol", ".cmf", ".d00", ".rad", ".laa", ".sci", ".dro",
    ".hsc", ".rix", ".a2m", ".adl", ".bam", ".ksm", ".amd",
    # Atari ST: SNDH archive files (psgplay), YM register dumps (StSound),
    # native sc68 disks (sc68).
    ".sndh", ".ym", ".sc68",
    # PSF console-music family (zxtune123 renders via the reference cores —
    # Highly Experimental / Highly Theoretical / lazyusf2 / mGBA / vio2sf).
    # ``.dsf`` is ALREADY listed under DSD above — Dreamcast Sound Format
    # shares the extension and is disambiguated by magic ('PSF\x12' vs
    # 'DSD ') in extract() and the stream router.  *lib companions
    # (.psflib …) are fetched beside their mini file, never indexed.
    ".psf", ".minipsf", ".psf2", ".minipsf2",
    ".usf", ".miniusf", ".gsf", ".minigsf",
    ".2sf", ".mini2sf", ".ssf", ".minissf",
    ".minidsf", ".ncsf", ".minincsf",
}

FORMAT_NAMES = {
    # ``.m4a`` is intentionally unset to a codec name here — the actual
    # codec is filled in by ``_mp4`` after an ffprobe lookup so we never
    # mis-label an AAC file as "ALAC" or vice versa.  The legacy
    # "ALAC/AAC" combo string broke filtering on codec in the library UI.
    ".mp3": "MP3", ".flac": "FLAC", ".m4a": "M4A", ".aac": "AAC",
    ".ogg": "Ogg Vorbis", ".opus": "Opus", ".aiff": "AIFF", ".aif": "AIFF",
    ".wav": "WAV", ".wv": "WavPack", ".mpc": "Musepack",
    # SID
    ".sid": "SID", ".psid": "SID",
    # MIDI
    ".mid": "MIDI", ".midi": "MIDI",
    # Tracker modules
    ".mod": "ProTracker", ".s3m": "ScreamTracker 3", ".xm": "FastTracker 2",
    ".it": "Impulse Tracker", ".mtm": "MultiTracker", ".med": "OctaMED",
    ".oct": "OctaMED", ".669": "Composer 669", ".dbm": "DigiBooster Pro",
    ".ahx": "AHX", ".hvl": "HivelyTracker", ".ult": "UltraTracker",
    ".stm": "ScreamTracker 2", ".far": "Farandole", ".amf": "ASYLUM/DMP",
    ".gdm": "General DigiMusic", ".imf": "Imago Orpheus",
    ".okt": "Oktalyzer", ".sfx": "SoundFX", ".wow": "Grave Composer",
    ".dsm": "DSIK",
    # libgme-rendered (E-14)
    ".nsf":  "NSF",  ".nsfe": "NSFe", ".spc": "SPC", ".gbs": "GBS",
    ".vgm":  "VGM",  ".vgz":  "VGZ",  ".ay":  "AY",  ".kss": "KSS",
    ".sap":  "SAP",  ".gym":  "GYM",  ".hes": "HES",
    # DSD — actual quality tier (DSD64/128/256/...) is filled in at
    # extract time once the source sample rate is known.
    ".dsf":  "DSD",  ".dff":  "DSD",  ".wsd": "DSD",
    # AdLib / OPL2 FM (AdPlug)
    ".rol": "AdLib ROL", ".cmf": "Creative Music", ".d00": "EdLib",
    ".rad": "Reality AdLib", ".laa": "LucasArts AdLib", ".sci": "Sierra AdLib",
    ".dro": "DOSBox OPL", ".hsc": "HSC AdLib", ".rix": "RIX OPL",
    ".a2m": "AdLib Tracker 2", ".adl": "AdLib", ".bam": "Bob's AdLib",
    ".ksm": "Ken's AdLib", ".amd": "AMUSIC AdLib",
    # Atari ST
    ".sndh": "SNDH", ".ym": "YM", ".sc68": "SC68",
    # PSF console-music family (.dsf stays "DSD" here — the Dreamcast case
    # is rewritten by content in extract())
    ".psf": "PSF", ".minipsf": "PSF", ".psf2": "PSF2", ".minipsf2": "PSF2",
    ".usf": "USF", ".miniusf": "USF", ".gsf": "GSF", ".minigsf": "GSF",
    ".2sf": "2SF", ".mini2sf": "2SF", ".ssf": "SSF", ".minissf": "SSF",
    ".minidsf": "DSF (Dreamcast)", ".ncsf": "NCSF", ".minincsf": "NCSF",
}

_ATARI_EXTS = {".sndh", ".ym", ".sc68"}

# PSF family: extension set + the platform-version byte after the 'PSF' magic
# (spec v1.4 + per-format specs; verified against real files from each set).
_PSF_EXTS = {
    ".psf", ".minipsf", ".psf2", ".minipsf2", ".usf", ".miniusf",
    ".gsf", ".minigsf", ".2sf", ".mini2sf", ".ssf", ".minissf",
    ".minidsf", ".ncsf", ".minincsf",
}
_PSF_VERSION_NAMES = {
    0x01: "PSF", 0x02: "PSF2", 0x11: "SSF", 0x12: "DSF (Dreamcast)",
    0x21: "USF", 0x22: "GSF", 0x24: "2SF", 0x25: "NCSF",
}

_DSD_EXTS = {".dsf", ".dff", ".wsd"}

_SID_EXTS = {".sid", ".psid"}
_MIDI_EXTS = {".mid", ".midi"}
_TRACKER_EXTS = {
    ".mod", ".s3m", ".xm", ".it", ".mtm", ".med", ".oct",
    ".669", ".dbm", ".ahx", ".hvl", ".ult", ".stm", ".far",
    ".amf", ".gdm", ".imf", ".okt", ".sfx", ".wow", ".dsm",
}
# libgme — Game Music Emu — covers chiptune formats from NES/SNES/
# Game Boy/Genesis/Master System/MSX/ZX Spectrum.  Rendered to WAV
# via the ``gme`` CLI when the user installs it.
_GME_EXTS = {
    ".nsf", ".nsfe", ".spc", ".gbs", ".vgm", ".vgz",
    ".ay", ".kss", ".sap", ".gym", ".hes",
}

# AdLib / OPL2 FM formats decoded by AdPlug (adplay).  ``.imf`` is NOT here —
# it's shared with the Imago Orpheus tracker and disambiguated by content (see
# ``_extract_imf`` / ``stream._render_imf``).
_ADLIB_EXTS = {
    ".rol", ".cmf", ".d00", ".rad", ".laa", ".sci", ".dro",
    ".hsc", ".rix", ".a2m", ".adl", ".bam", ".ksm", ".amd",
}
_ADLIB_DEFAULT_DURATION = 180   # seconds; the rendered WAV carries the real length

# ── UADE — exotic Amiga formats (TFMX, Future Composer, SidMon, ...) ─────────
# uade's own eagleplayer.conf is the source of truth for which name tokens it
# claims (~350 tokens across ~175 players).  Modland-style SUFFIX naming
# (``song.fc13``) is registered below as ordinary extensions so every
# extension-keyed gate (scanner walk, zip/LHA members, remote walk, stream
# routing) works unchanged; Amiga PREFIX naming (``mdat.song``) can't be an
# extension and is handled by ``is_supported_music_name`` /
# ``uade_formats.classify`` at the scanner gates and extract dispatch.
# Ownership: tokens other engines already claim are excluded inside
# ``new_suffix_tokens`` — .mod/.med/.okt stay libopenmpt, .sid stays
# sidplayfp, .ahx/.hvl keep their dedicated routes.  No conf → empty set →
# everything degrades to exactly the pre-UADE behaviour.
from soniqboom.core import uade_formats as _uade

def _register_uade_suffixes() -> set[str]:
    # Function scope (not module-level loop vars): with no eagleplayer.conf
    # the token dict is EMPTY, and a module-level ``del`` of never-bound loop
    # variables raised NameError at import — bricking every uade-less host
    # (QA C1, 2026-07-02).
    exts: set[str] = set()
    for tok, player in _uade.new_suffix_tokens().items():
        ext = f".{tok}"
        exts.add(ext)
        SUPPORTED_EXTENSIONS.add(ext)
        FORMAT_NAMES.setdefault(ext, _uade.display_name(player))
    return exts


_UADE_SUFFIX_EXTS: set[str] = _register_uade_suffixes()


def is_supported_music_name(name: str) -> bool:
    """Scanner gate: extension-supported OR a uade prefix-form candidate.

    Companion halves (``smpl.*`` / ``*.ins`` / ...) are excluded by
    ``classify`` — they are materialised next to their module at render
    time, never indexed as tracks.
    """
    import os as _os
    ext = _os.path.splitext(name)[1].lower()
    # Core (non-uade) extensions win outright — a song someone named
    # ``SMP.remix.mp3`` must never be swallowed by Amiga companion naming.
    if ext in SUPPORTED_EXTENSIONS and ext not in _UADE_SUFFIX_EXTS:
        return True
    # Companion halves next: a sample half's arbitrary body may collide with
    # a registered token (``smpl.fc13``) and sneak past the extension gate.
    if _uade.is_companion_half(name):
        return False
    if ext in _UADE_SUFFIX_EXTS:
        return True
    return _uade.classify(name) is not None

# ── General MIDI program names ────────────────────────────────────────────────

_GM_PROGRAMS = {
    0: "Acoustic Grand Piano", 1: "Bright Acoustic Piano", 2: "Electric Grand Piano",
    3: "Honky-tonk Piano", 4: "Electric Piano 1", 5: "Electric Piano 2",
    6: "Harpsichord", 7: "Clavinet", 8: "Celesta", 9: "Glockenspiel",
    10: "Music Box", 11: "Vibraphone", 12: "Marimba", 13: "Xylophone",
    14: "Tubular Bells", 15: "Dulcimer", 16: "Drawbar Organ", 17: "Percussive Organ",
    18: "Rock Organ", 19: "Church Organ", 20: "Reed Organ", 21: "Accordion",
    22: "Harmonica", 23: "Tango Accordion", 24: "Acoustic Guitar (nylon)",
    25: "Acoustic Guitar (steel)", 26: "Electric Guitar (jazz)",
    27: "Electric Guitar (clean)", 28: "Electric Guitar (muted)",
    29: "Overdriven Guitar", 30: "Distortion Guitar", 31: "Guitar Harmonics",
    32: "Acoustic Bass", 33: "Electric Bass (finger)", 34: "Electric Bass (pick)",
    35: "Fretless Bass", 36: "Slap Bass 1", 37: "Slap Bass 2",
    38: "Synth Bass 1", 39: "Synth Bass 2", 40: "Violin", 41: "Viola",
    42: "Cello", 43: "Contrabass", 44: "Tremolo Strings", 45: "Pizzicato Strings",
    46: "Orchestral Harp", 47: "Timpani", 48: "String Ensemble 1",
    49: "String Ensemble 2", 50: "Synth Strings 1", 51: "Synth Strings 2",
    52: "Choir Aahs", 53: "Voice Oohs", 54: "Synth Choir", 55: "Orchestra Hit",
    56: "Trumpet", 57: "Trombone", 58: "Tuba", 59: "Muted Trumpet",
    60: "French Horn", 61: "Brass Section", 62: "Synth Brass 1", 63: "Synth Brass 2",
    64: "Soprano Sax", 65: "Alto Sax", 66: "Tenor Sax", 67: "Baritone Sax",
    68: "Oboe", 69: "English Horn", 70: "Bassoon", 71: "Clarinet",
    72: "Piccolo", 73: "Flute", 74: "Recorder", 75: "Pan Flute",
    76: "Blown Bottle", 77: "Shakuhachi", 78: "Whistle", 79: "Ocarina",
    80: "Lead 1 (square)", 81: "Lead 2 (sawtooth)", 82: "Lead 3 (calliope)",
    83: "Lead 4 (chiff)", 84: "Lead 5 (charang)", 85: "Lead 6 (voice)",
    86: "Lead 7 (fifths)", 87: "Lead 8 (bass + lead)", 88: "Pad 1 (new age)",
    89: "Pad 2 (warm)", 90: "Pad 3 (polysynth)", 91: "Pad 4 (choir)",
    92: "Pad 5 (bowed)", 93: "Pad 6 (metallic)", 94: "Pad 7 (halo)",
    95: "Pad 8 (sweep)", 96: "FX 1 (rain)", 97: "FX 2 (soundtrack)",
    98: "FX 3 (crystal)", 99: "FX 4 (atmosphere)", 100: "FX 5 (brightness)",
    101: "FX 6 (goblins)", 102: "FX 7 (echoes)", 103: "FX 8 (sci-fi)",
    104: "Sitar", 105: "Banjo", 106: "Shamisen", 107: "Koto",
    108: "Kalimba", 109: "Bagpipe", 110: "Fiddle", 111: "Shanai",
    112: "Tinkle Bell", 113: "Agogo", 114: "Steel Drums", 115: "Woodblock",
    116: "Taiko Drum", 117: "Melodic Tom", 118: "Synth Drum",
    119: "Reverse Cymbal", 120: "Guitar Fret Noise", 121: "Breath Noise",
    122: "Seashore", 123: "Bird Tweet", 124: "Telephone Ring",
    125: "Helicopter", 126: "Applause", 127: "Gunshot",
}

# ── MOD channel magic bytes ───────────────────────────────────────────────────

_MOD_MAGIC_CHANNELS = {
    b"M.K.": 4, b"M!K!": 4, b"M&K!": 4, b"N.T.": 4,
    b"FLT4": 4, b"FLT8": 8, b"OCTA": 8,
    b"2CHN": 2, b"4CHN": 4, b"6CHN": 6, b"8CHN": 8,
    b"10CH": 10, b"12CH": 12, b"14CH": 14, b"16CH": 16,
    b"18CH": 18, b"20CH": 20, b"22CH": 22, b"24CH": 24,
    b"26CH": 26, b"28CH": 28, b"30CH": 30, b"32CH": 32,
    b"CD81": 8, b"TDZ1": 1, b"TDZ2": 2, b"TDZ3": 3,
    b"5CHN": 5, b"7CHN": 7, b"9CHN": 9,
}

# ── Partial-fetch header budgets ──────────────────────────────────────────────
#
# Number of bytes from the START of a file that the scanner can fetch
# in lieu of the whole payload, and still extract complete metadata.
# A value of ``None`` means "must fetch entire file" — either the tag
# container is at the end (DSF's ID3 chunk position is implementation-
# defined; the Suara DFF files have it at file END), or the format
# uses random-access seeks (M4A/MP4 ``moov`` atom can be at start or
# end depending on the muxer) that can't be safely truncated.
#
# Numbers are deliberately generous — saving 2 KB by tightening the
# budget at the cost of a single fall-back full fetch is a bad trade
# (full fetch is 100× more expensive on a typical FLAC).  Each entry
# is sized to fit the largest realistic header for that container,
# including embedded album art:
#
#   * MP3:  ID3v2.4 frame headers ~10 bytes + APIC frame.  Most embedded
#           covers cap out at 50–100 KB.  Anything larger is rare.
#   * FLAC: STREAMINFO (42 B) + VORBIS_COMMENT (avg ~1 KB) + PICTURE
#           block (can hold embedded JPEG 50–200 KB).
#   * Ogg/Opus: vorbis comments in the second logical page, usually
#           within the first 32 KB; pad for embedded art.
#   * Tracker formats: header is tens to hundreds of bytes at offset 0;
#           we pad to KB-range for safety on unusual variants.
#   * SID/PSID: 128-byte header at offset 0; 256 B is overkill.
#   * SPC: 256-byte header + ID666 tag at offset 0x2E; the optional
#           extended (xid6) tag starts at 0x10200, past the 64 KB RAM image,
#           so SPC is always fetched whole (budget None below).
#
# All values are upper bounds — the partial fetch may stop earlier on
# EOF.  If extract returns a result whose ``title`` is just the
# filename stem and other fields are empty, the scanner treats that as
# "partial fetch undershot" and falls back to a full fetch.
HEADER_BUDGET: dict[str, int | None] = {
    # ID3-based / common audio
    #
    # FLAC bumped to 1.5 MB after observing a 10G-LAN re-index running
    # at ~1 MB/s instead of 600 MB/s: 88% of the user's FLACs (1014 /
    # 1147 sampled) fell back to full fetch because the 384 KB budget
    # cut off mid-PICTURE-block on Hi-Res rips with embedded album
    # covers (one sample: 549 KB cover, metadata ends at 553 KB).
    # 1.5 MB covers art up to ~1.3 MB with header padding — the long
    # tail of Hi-Res rips with bigger covers still gets caught by the
    # full-fetch fallback in _process_one.  Cost of the bump: ~4×
    # more bytes per partial fetch, still 33× less than full fetch
    # on a 50 MB file.
    ".mp3":  512 * 1024,
    ".flac": 1536 * 1024,
    ".ogg":  512 * 1024,
    ".opus": 512 * 1024,
    # AIFF: the tag is an ``ID3 `` chunk that ffmpeg / mutagen (and our own
    # ``write_lyrics``) place AFTER the ``SSND`` audio chunk, at the file END —
    # a front read parses cleanly (COMM gives the duration) but finds no tag,
    # and the "looks incomplete" check can't tell, so fetch the whole file.
    ".aiff": None,
    ".aif":  None,
    ".wav":  128 * 1024,
    # Tracker formats — header at start, small
    ".mod":  64 * 1024,    # MOD samples can inflate; 64 KB covers most
    ".s3m":  64 * 1024,
    ".it":   64 * 1024,
    ".xm":   64 * 1024,
    ".mtm":  64 * 1024,
    ".med":  64 * 1024,
    ".669":  64 * 1024,
    # Chiptune containers — tiny headers
    # SID files are 2–64 KB total; a full fetch is trivial AND required so the
    # whole-file MD5 (the HVSC Songlengths key) is computed over the real file,
    # not a truncated header.  See _extract_sid / hvsc.lookup_durations_by_md5.
    ".sid":  None,
    ".psid": None,
    ".rsid": None,
    ".nsf":  8 * 1024,
    ".nsfe": 64 * 1024,    # NSFe has chunks throughout — generous
    ".spc":  None,         # SPC files are 64-256 KB total; full fetch trivial
    ".gbs":  4 * 1024,
    ".vgm":  None,         # VGM headers vary; full fetch is cheap (small files)
    ".vgz":  None,         # gzip — must decompress whole stream
    ".ay":   4 * 1024,
    ".kss":  4 * 1024,
    ".sap":  4 * 1024,
    ".gym":  None,
    ".hes":  4 * 1024,
    # MUST fetch full file:
    ".m4a":  None,         # moov atom can be at start or end
    ".mp4":  None,
    ".aac":  None,
    ".dsf":  None,         # ID3 chunk position is mastering-tool dependent
    ".dff":  None,         # observed: Suara album has ID3 chunk at file END
    ".wsd":  None,         # no mutagen support; ffprobe needs full file
    ".mid":  None,         # SMF parsed sequentially
    ".midi": None,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _decode_tracker_str(b: bytes, *, latin1_native: bool = False) -> str:
    """Decode a fixed-size text field from a tracker / chiptune header.

    Tracker formats (MOD/S3M/IT/XM) and chiptune containers (SID/NSF/SPC/
    GBS/etc.) store text in 8-bit encodings that predate UTF-8.  Bytes
    ≥ 0x80 are common — DOS-era CP437 box-drawing chars, ISO-8859-1
    Western European, occasional Shift-JIS for Japanese demoscene
    files.  Decoding such bytes as ``ascii`` with ``errors='replace'``
    (the original code's choice) produced the user-visible mojibake
    where titles like ``finality`` were padded with U+FFFD diamonds.

    Strategy: try strict UTF-8 first (modern files); fall back to
    CP437 (the DOS code page); finally Latin-1 (single-byte, lossless,
    never raises — guarantees we always return *some* text).  Strips
    NUL padding and surrounding whitespace at the end.

    ``latin1_native=True`` for formats whose header charset is ISO-8859-1
    by *specification* rather than DOS-scene convention — the PSID/RSID SID
    header is the case that matters: byte ``0xFC`` there is ``ü`` (Latin-1),
    NOT CP437's ``ⁿ`` (U+207F).  For those we skip the CP437 guess entirely so
    ``Chris Hülsbeck`` doesn't come out as ``Chris Hⁿlsbeck``.

    Returns ``""`` for empty / all-NUL inputs.
    """
    if not b:
        return ""
    # NUL-terminate the field at the first NUL byte (every tracker /
    # chiptune format pads with NULs, not spaces).
    b = b.split(b"\x00", 1)[0]
    if not b:
        return ""
    # Strict UTF-8 — wins for modern files.
    try:
        return b.decode("utf-8").strip()
    except UnicodeDecodeError:
        pass
    # CP437 — DOS code page, the de-facto tracker scene encoding from
    # the FastTracker / Impulse Tracker era.  Single-byte, can't fail
    # on any byte, but we keep the try/except for paranoia.  Skipped for
    # Latin-1-native formats (SID), where CP437 would corrupt accented
    # Western-European characters.
    if not latin1_native:
        try:
            return b.decode("cp437").strip()
        except (UnicodeDecodeError, LookupError):
            pass
    # Latin-1 catch-all: 256 distinct chars covering bytes 0x00–0xFF.
    # Never raises.  Visual output may be mojibake for Shift-JIS files,
    # but at least it's stable readable bytes the user can search on
    # and the round-trip is lossless if they ever need the raw text.
    return b.decode("latin-1").strip()


def _str(v) -> str:
    return str(v).strip() if v is not None else ""


def _psf_tag_text(blob: bytes) -> str:
    """A PSF ``[TAG]`` block's text: UTF-8 when it decodes as such (the spec's
    ``utf8=1`` sets) — unless, without ``utf8=1``, it reads as legacy bytes
    (``_unlikely_utf8``: a legacy "ß´" is valid UTF-8 for an NKo letter) and
    a legacy reading is plausible, or holds a C1 control; else whichever of
    Western (cp1252 — Latin-1 plus Windows punctuation) and Shift-JIS (cp932,
    the usual encoding of Japanese sets) reads more plausibly
    (``_legacy_text``)."""
    utf8 = b"utf8=1" in blob.lower()
    try:
        text: str | None = blob.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None:
        if utf8 or not _unlikely_utf8(text):
            return text
        legacy = _legacy_text(blob)
        if legacy is not None and (legacy[1] > 0 or _has_c1(text)):
            return legacy[0]
        return text
    if utf8:
        return blob.decode("utf-8", "replace")
    legacy = _legacy_text(blob)
    return legacy[0] if legacy is not None else blob.decode("cp1252", "replace")


def _legacy_text(blob: bytes) -> tuple[str, float] | None:
    """The more plausible of the cp1252 and cp932 readings of ``blob`` with
    its score (``_western_score`` / ``_japanese_score``; a tie is Japanese:
    a lone kanji and a lone accented letter score alike), or None when
    neither decodes."""
    try:
        western = blob.decode("cp1252")
    except UnicodeDecodeError:
        western = None
    try:
        japanese = blob.decode("cp932")
    except UnicodeDecodeError:
        japanese = None
    ws = _western_score(western) if western is not None else None
    js = _japanese_score(japanese) if japanese is not None else None
    if ws is not None and (js is None or ws > js):
        return western, ws
    if js is not None:
        return japanese, js
    return None


def _has_c1(text: str) -> bool:
    return any(0x80 <= ord(c) <= 0x9F for c in text)


def _stray_mark(text: str) -> bool:
    """Does ``text`` hold a combining mark (U+0300–036F) on nothing it could
    mark: at the start of a line or of a ``key=value`` value (half-width
    katakana often decode as one), or after a character that is no letter,
    digit or math symbol (a decomposed "≠" is "=" + U+0338) — looking past
    marks stacked on one base."""
    base = ""
    in_value = False
    for c in text:
        if c == "\n":
            base, in_value = "", False
        elif 0x300 <= ord(c) <= 0x36F:
            if not base or not (base.isalnum() or unicodedata.category(base) == "Sm"):
                return True
        elif c == "=" and not in_value:
            base, in_value = "", True                   # the tag's own separator
        else:
            base = c
    return False


def _unlikely_utf8(text: str) -> bool:
    """Does ``text`` (a UTF-8 decode) read as legacy bytes: a C1 control or a
    combining mark on no letter or digit (half-width katakana often decode
    as one) anywhere; or, for all its non-ASCII, lone IPA letters and
    Armenian … NKo characters between ASCII — what two legacy bytes (a
    Latin-1 letter and a symbol) often decode as.  Such a letter beside
    other non-ASCII text ("Λsʜᴇs", "שלום", "ətˈæk") is real text, and so is
    a mark on a letter or digit (a decomposed "Garçon", "Z0̸NE")."""
    if _has_c1(text) or _stray_mark(text):
        return True
    n = len(text)
    lone = False
    for k, c in enumerate(text):
        o = ord(c)
        if o < 0x80:
            continue
        if not ((0x250 <= o <= 0x2AF or 0x530 <= o <= 0x7FF)
                and (k == 0 or text[k - 1].isascii()) and (k + 1 == n or text[k + 1].isascii())):
            return False
        lone = True
    return lone


# cp1252 letters of its 0x80–0x9F range (Czech, French …); as Shift-JIS bytes
# they lead a kanji ("ŠC" is 海).
_C1_LETTERS = frozenset("ŠŒŽšœžŸ")
# Windows punctuation (0x80–0x9F), each with the places it sits in Western
# text; and Latin-1 symbols and the no-break space.
_WESTERN_PUNCT = frozenset("‘’“”–—…•™€‚„‹›")
_WESTERN_SYMBOLS = frozenset("©®°±²³µ·¹º¼½¾¿×÷«»¡§¶£¥¢¬´¸¨\xa0")
_W_SYMBOL = 1.0


def _latin_letter(c: str) -> bool:
    return ((c.isascii() and c.isalpha()) or ("\xc0" <= c <= "\xff" and c not in "×÷")
            or c in _C1_LETTERS)


def _western_score(text: str) -> float:
    """How plausibly ``text`` (a cp1252 decode) is Western: accented letters
    inside words, Windows punctuation where it goes ("Don’t", " – ", "“Live”",
    "Game™"), symbols — against runs of odd characters, a capital inside a
    word ("žÙ", "šA"), and anything else — "ƒ" (the Shift-JIS katakana lead
    byte) among it."""
    score = 0.0
    n = len(text)
    i = 0
    while i < n:
        if text[i].isascii():
            i += 1
            continue
        j = i
        while j < n and not text[j].isascii():
            j += 1
        if j - i > 2:
            score -= j - i - 2                      # Western text: one or two at a time
        before = text[i - 1] if i else ""
        after = text[j] if j < n else ""
        # a run with no ASCII letter beside it is no part of a word
        anchored = (before.isascii() and before.isalpha()) or (after.isascii() and after.isalpha())
        for k in range(i, j):
            c = text[k]
            prev = text[k - 1] if k else ""
            nxt = text[k + 1] if k + 1 < n else ""
            in_word = anchored and ((bool(prev) and _latin_letter(prev))
                                    or (bool(nxt) and _latin_letter(nxt)))
            if c.isalpha() and c != "ß" and ((c.isupper() and prev.isalpha() and prev.islower())
                                             or (c.islower() and nxt.isalpha() and nxt.isupper())):
                score -= 2                          # a capital inside a word ("GRÜßE" is none)
            pa = bool(prev) and prev.isalnum()
            na = bool(nxt) and nxt.isalnum()
            if c in _C1_LETTERS:
                tail = text[k + 1:k + 3]
                if (c in "ŠŒŽ" and not prev.isalpha() and len(tail) == 2 and tail.isascii()
                        and tail.isalpha() and (tail.islower() or tail.isupper())):
                    score += 3                      # "Škoda", "Œuvre", "ŠKODA"
                elif c != "Ÿ":
                    score += 1.5 if in_word else 0.5
            elif _latin_letter(c):
                score += 3 if in_word else 1.5
            elif c in _WESTERN_PUNCT:
                if c == "’" and pa and (na or not nxt or nxt in " ,.!?;:)"):
                    score += 3                      # Don’t, Rock ‘n’ Roll
                elif c in "‘“„‚‹" and (not prev or prev in " ([/-") and na:
                    score += 2
                elif c in "’”›" and (pa or prev in ".!?") and (not nxt or nxt in " )],.!?:;/-"):
                    score += 2
                elif c in "–—" and prev == " " and na:
                    score += 3                      # "After Dark –prologue-"
                elif c in "–—" and ((prev == " " and nxt == " ") or (pa and (na or nxt == " "))):
                    score += 2
                elif c == "…" and (pa or prev in ".!?") and (not nxt or not nxt.isalnum()):
                    score += 2
                elif c == "™" and pa and (not nxt or not nxt.isalnum()):
                    score += 2
                elif c == "€" and (na or (bool(prev) and prev.isdigit())):
                    score += 2
                elif c == "•" and prev in (" ", "") and nxt == " ":
                    score += 2
                else:
                    score -= 1
            elif c == "¥":                          # a price: "¥500", "500 ¥"
                near = text[max(k - 2, 0):k] + text[k + 1:k + 3]
                score += 1 if any(d.isdigit() for d in near) else -1
            elif c in _WESTERN_SYMBOLS:
                score += _W_SYMBOL
            else:
                score -= 3
        i = j
    return score


# Half-width katakana (U+FF61–FF9F) — what Latin-1 symbols and capitals read
# as in cp932: a run with a voicing mark after a letter or a small kana after
# a full-size one ("ｼﾞ", "ﾛｯｸﾏﾝ"), or of three or more with two different
# letters ("ﾀｲﾄﾙ"), is Japanese; so, a little less, are two full-size letters
# standing alone ("ﾕﾒ") — beside Latin text ("CD³²" reads as "CDｳｲ") they say
# nothing either way; a run of middle dots ("･･･") is an ellipsis, a lone one
# a separator ("R･I･O･T"); anything else counts against it ("°°°°" reads as
# "ｰｰｰｰ", "·°·" as "ｷｰｷ").
_HW_WORD = 2.0
_HW_DOTS = 1.5
_HW_SEPARATOR = 0.5
_HW_OTHER = -2.5
_HW_VOICED = frozenset("ﾞﾟ")
_HW_SMALL = frozenset("ｧｨｩｪｫｬｭｮｯ")


def _half_width_scores(text: str) -> dict[int, float]:
    """The score of each half-width katakana in ``text``, by index."""
    out: dict[int, float] = {}
    n = len(text)
    i = 0
    while i < n:
        if not 0xFF61 <= ord(text[i]) <= 0xFF9F:
            i += 1
            continue
        j = i
        while j < n and 0xFF61 <= ord(text[j]) <= 0xFF9F:
            j += 1
        run = text[i:j]
        letters = {c for c in run if 0xFF66 <= ord(c) <= 0xFF9D and c != "ｰ"}
        # a voicing mark after a letter ("ｼﾞ"), or a small kana after a
        # full-size one ("ｼｮ", "ﾛｯｸ") — "ß´" reads as a mark first, "©®" as
        # two small kana, "«»" as a small one first
        marked = any(run[m - 1] in letters
                     and (run[m] in _HW_VOICED
                          or (run[m] in _HW_SMALL and run[m - 1] not in _HW_SMALL))
                     for m in range(1, len(run)))
        if letters and (marked or (len(run) >= 3 and len(letters) >= 2)):
            w = _HW_WORD
        elif len(run) == 2 and len(letters) == 2:
            # two letters: a word on its own ("ﾕﾒ"); beside Latin text ("CD³²"
            # reads as "CDｳｲ"), or small kana ("©®"), no evidence either way
            before = text[i - 1] if i else ""
            after = text[j] if j < n else ""
            alone = not (before.isascii() and before.isalnum()) and not (
                after.isascii() and after.isalnum())
            w = _HW_WORD * 0.75 if alone and _HW_SMALL.isdisjoint(run) else 0.0
        elif len(run) >= 2 and set(run) == {"･"}:
            w = _HW_DOTS
        elif run == "･":
            w = _HW_SEPARATOR
        else:
            w = _HW_OTHER
        for k in range(i, j):
            out[k] = w
        i = j
    return out


def _japanese_score(text: str) -> float:
    """How plausibly ``text`` (a cp932 decode) is Japanese: kana, common
    (JIS level-1) and rarer (level-2) kanji, full-width forms, half-width
    katakana words (``_half_width_scores``) — against anything else; a kanji
    beside a Latin letter counts less ("Dušan" read as cp932 puts one inside
    a word)."""
    score = 0.0
    n = len(text)
    half = _half_width_scores(text)
    for k, c in enumerate(text):
        if c.isascii():
            continue
        o = ord(c)
        if 0x3041 <= o <= 0x30FF:                   # hiragana, katakana, ー
            score += 4
            continue
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF:
            try:
                code = int.from_bytes(c.encode("cp932"), "big")
            except UnicodeEncodeError:
                code = 0
            s = 3 if 0x889F <= code <= 0x9872 else (2.5 if 0x989F <= code <= 0xEAA4 else 0.5)
        elif 0x3000 <= o <= 0x303F or 0xFF01 <= o <= 0xFF5E:
            s = 2                                   # CJK punctuation, full-width forms
        elif 0xFF61 <= o <= 0xFF9F:
            score += half[k]                        # half-width katakana
            continue
        elif unicodedata.east_asian_width(c) in ("W", "F"):
            s = 0.5
        else:
            score -= 6                              # user-defined, control …
            continue
        for nb in (text[k - 1] if k else "", text[k + 1] if k + 1 < n else ""):
            if nb and nb.isascii() and nb.isalpha():
                s -= 1
        score += s
    return score


def _mp4_game(tags, g) -> str:
    """The GAME of an MP4 tag — the ``----:com.apple.iTunes:GAME`` freeform
    atom the tag editor writes, or one another tool wrote in another case."""
    v = (g("----:com.apple.iTunes:GAME", "") or "").strip()
    if v or not tags:
        return v
    for k in list(tags.keys()):
        if k.lower() == "----:com.apple.itunes:game":
            v = (g(k, "") or "").strip()
            if v:
                return v
    return ""


def _id3_game(tags) -> str:
    """The GAME of an ID3 tag — a ``TXXX:GAME`` user frame (there is no
    standard frame; this is the key the tag editor writes)."""
    getall = getattr(tags, "getall", None)
    for frame in (getall("TXXX") if getall else []):
        if (getattr(frame, "desc", "") or "").strip().upper() == "GAME":
            # A multi-value frame: its first value (they'd join with NULs).
            for v in (getattr(frame, "text", None) or [frame]):
                if _str(v):
                    return _str(v)
    return ""


def _list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    return [s] if s else []


def _int(v, default=None) -> int | None:
    try:
        return int(str(v).split("/")[0].strip())
    except Exception:
        return default


def _year(v) -> int | None:
    """Extract a 4-digit year from any tag value.

    Handles mutagen ID3TimeStamp objects, ISO dates ("2025-04-04"),
    compact date integers ("20250404"), plain years ("2025"), and
    track-number-style "n/total" fractions.
    """
    if v is None:
        return None
    # mutagen ID3TimeStamp exposes a .year attribute
    if hasattr(v, "year") and v.year:
        try:
            y = int(v.year)
            if 1900 <= y <= 2100:
                return y
        except Exception:
            pass
    # Fall back to string parsing — take first 4 numeric characters
    s = str(v).strip()
    # Strip "n/total" notation
    s = s.split("/")[0].strip()
    # Strip ISO dashes: "2025-04-04" → first 4 chars = "2025"
    digits = s[:4]
    try:
        y = int(digits)
        if 1900 <= y <= 2100:
            return y
    except Exception:
        pass
    return None


def _total(v) -> int | None:
    """Extract the 'total' from a 'n/total' tag value."""
    try:
        parts = str(v).split("/")
        return int(parts[1].strip()) if len(parts) > 1 else None
    except Exception:
        return None


def _float(v, default=None) -> float | None:
    try:
        f = float(str(v).strip())
    except Exception:
        return default
    # Reject NaN/inf: a tag literally "nan"/"inf" parses as a float but is not a
    # valid bpm/gain, and NaN poisons sorted indexes (NaN != NaN makes
    # ``_sorted_bpm`` compare unequal to a rebuild forever — the integrity sweep
    # would flap and "auto-heal" endlessly).
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _cover_b64(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


# Formats considered lossless.  Used to set the ``is_lossless`` flag on
# every track so the UI can badge appropriately and so smart-search
# filters like "show me only lossless rips" can build cleanly.
_LOSSLESS_FORMATS = {
    "FLAC", "ALAC", "WAV", "AIFF", "WavPack", "DSD",
    "DSD64", "DSD128", "DSD256", "DSD512", "TTA",
}


def _is_lossless_format(fmt: str | None) -> bool:
    """True if ``fmt`` denotes a lossless audio container/codec.

    MPC is intentionally not in the lossless set — most MPC files are
    lossy SV7/SV8 streams.  WavPack (.wv) is lossless by spec.
    """
    if not fmt:
        return False
    return fmt.split("/", 1)[0].strip() in _LOSSLESS_FORMATS


def _parse_gain(raw) -> float | None:
    """Parse a ReplayGain tag value ("-6.32 dB" or "-6.32") to float dB."""
    if raw is None:
        return None
    if isinstance(raw, list):
        if not raw:
            return None
        raw = raw[0]
    s = str(raw).strip()
    if not s:
        return None
    # Strip the trailing "dB" if present.
    if s.lower().endswith("db"):
        s = s[:-2].strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_peak(raw) -> float | None:
    """Parse a ReplayGain peak (linear float, 0–1)."""
    if raw is None:
        return None
    if isinstance(raw, list):
        if not raw:
            return None
        raw = raw[0]
    try:
        return float(str(raw).strip())
    except ValueError:
        return None


def _replaygain_from_vorbis(tags) -> dict:
    """Pull ReplayGain fields out of a Vorbis-comment-style tags object.

    Returns the four ``replaygain_*`` floats (in dB / linear peak) when
    present; missing keys are simply absent from the dict.  Works for
    FLAC, Ogg Vorbis, and Opus tag containers (all share the same
    string-keyed multi-value model).
    """
    out: dict = {}
    if tags is None:
        return out
    keys = (
        ("replaygain_track_gain", "REPLAYGAIN_TRACK_GAIN", "track_gain"),
        ("replaygain_album_gain", "REPLAYGAIN_ALBUM_GAIN", "album_gain"),
        ("replaygain_track_peak", "REPLAYGAIN_TRACK_PEAK", "track_peak"),
        ("replaygain_album_peak", "REPLAYGAIN_ALBUM_PEAK", "album_peak"),
    )
    for src1, src2, dst in keys:
        # mutagen Vorbis tags index by lowercase; tolerate either casing.
        raw = None
        try:
            raw = tags.get(src1) or tags.get(src1.lower()) or tags.get(src2)
        except Exception:
            raw = None
        parser = _parse_peak if "peak" in dst else _parse_gain
        val = parser(raw)
        if val is not None:
            out[f"replaygain_{dst}"] = val
    # Opus uses R128_TRACK_GAIN (Q7.8 integer dB × 256, per the Opus spec
    # extension).  Convert to a plain dB float for consistency with the
    # other tag families.
    r128 = None
    try:
        r128 = tags.get("R128_TRACK_GAIN") or tags.get("r128_track_gain")
    except Exception:
        r128 = None
    if r128 is not None:
        if isinstance(r128, list) and r128:
            r128 = r128[0]
        try:
            iv = int(str(r128).strip())
            # Q7.8 → dB.  Opus's R128 tag is signed Q7.8 with -127 dB at 0
            # and the reference loudness at 0 dB.
            out.setdefault("replaygain_track_gain", iv / 256.0)
        except ValueError:
            pass
    return out


def _replaygain_from_id3(tags) -> dict:
    """Pull ReplayGain fields out of an ID3 (MP3) tag object.

    ID3 carries ReplayGain as TXXX frames keyed on description (case-
    sensitive ``REPLAYGAIN_TRACK_GAIN``).  Some encoders also embed RVA2
    frames; we read those as a secondary source.
    """
    out: dict = {}
    if tags is None:
        return out
    try:
        # TXXX[REPLAYGAIN_TRACK_GAIN] etc.  mutagen exposes these via
        # ``getall("TXXX:NAME")`` or a flat ``tags.get("TXXX:NAME")``.
        for name, dst in (
            ("REPLAYGAIN_TRACK_GAIN", "track_gain"),
            ("REPLAYGAIN_ALBUM_GAIN", "album_gain"),
            ("REPLAYGAIN_TRACK_PEAK", "track_peak"),
            ("REPLAYGAIN_ALBUM_PEAK", "album_peak"),
        ):
            frame = tags.get(f"TXXX:{name}")
            if frame is None:
                continue
            try:
                raw = frame.text[0] if hasattr(frame, "text") else str(frame)
            except Exception:
                raw = str(frame)
            parser = _parse_peak if "peak" in dst else _parse_gain
            val = parser(raw)
            if val is not None:
                out[f"replaygain_{dst}"] = val
    except Exception:
        pass
    return out


def resize_cover(data: bytes, max_size: int, quality: int = 85) -> bytes:
    """Resize cover art to fit within max_size x max_size, returned as JPEG bytes."""
    from io import BytesIO
    try:
        from PIL import Image
        img = Image.open(BytesIO(data))
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        return data  # return original if resize fails


def cap_full_cover(data: bytes, max_size: int = 1024, min_bytes: int = 262_144) -> bytes:
    """Bound the size of a cached *full* cover.

    The UI only ever renders the sm/lg thumbnails; the ``full`` tier exists as
    the one-time resize source (and Subsonic ``getCoverArt`` at ``size > 600``),
    so a multi-MB original is wasted disk per track for something nothing
    displays.  Cap large covers to ``max_size`` px JPEG — still well above the
    550 px ``lg`` thumbnail, so quality for every real surface is unchanged.

    Covers already under ``min_bytes`` are returned VERBATIM to skip a pointless
    re-encode of an already-small image.  Falls back to the original on any
    error, or when the re-encoded result isn't actually smaller (already lean).
    """
    if len(data) < min_bytes:
        return data
    capped = resize_cover(data, max_size)
    return capped if (capped and len(capped) < len(data)) else data


# ── MP3 (ID3) ─────────────────────────────────────────────────────────────────

def id3_picture(tags):
    """The cover of an ID3 tag: its front-cover ``APIC`` (type 3), else its
    first ``APIC`` with image data, else None — never another frame that
    carries ``mime`` + ``data`` (a ``GEOB``, e.g. DJ software's Serato data).
    ID3v2.2 ``PIC`` frames load as ``APIC``.  Shared with ``api/art``."""
    pics = [f for f in (tags.getall("APIC") if tags is not None else []) if f.data]
    return next((f for f in pics if f.type == 3), pics[0] if pics else None)


def _id3_fields(tags) -> dict:
    """The track fields of an ID3 tag — shared by MP3 (a tag at the head of
    the file) and AIFF (an ``ID3 `` chunk inside the IFF container): text
    frames, the GAME (``_id3_game``), the cover (``id3_picture``), ReplayGain.
    ``tags`` None (a file with no ID3 tag) reads as an empty tag."""
    if tags is None:
        tags = ID3()
    trck = str(tags.get("TRCK", ""))
    tpos = str(tags.get("TPOS", ""))
    d: dict = {
        "title": _str(tags.get("TIT2")),
        "artist": _str(tags.get("TPE1")),
        "album_artist": _str(tags.get("TPE2")),
        "album": _str(tags.get("TALB")),
        "composer": _str(tags.get("TCOM")),
        "comment": _str(next(iter(tags.getall("COMM") or []), "")),
        "label": _str(tags.get("TPUB")),
        "isrc": _str(tags.get("TSRC")),
        "game": _id3_game(tags),
        "bpm": _float(tags.get("TBPM")),
        "genre": _list(tags.get("TCON")),
        "year": _year(tags.get("TDRC") or tags.get("TYER")),
        "track_number": _int(trck),
        "total_tracks": _total(trck),
        "disc_number": _int(tpos),
        "total_discs": _total(tpos),
    }
    pic = id3_picture(tags)
    if pic is not None:
        d["cover_art"] = _cover_b64(pic.data, pic.mime or "image/jpeg")
    d.update(_replaygain_from_id3(tags))
    return d


def _mp3(path: Path, track_id: str) -> dict:
    audio = MP3(path)
    d: dict = {
        "id": track_id,
        "path": str(path),
        "format": "MP3",
        "duration": audio.info.length,
        "bitrate": audio.info.bitrate,
        "channels": audio.info.channels,
        "sample_rate": audio.info.sample_rate,
    }
    d.update(_id3_fields(audio.tags))
    return d


# ── AIFF (ID3 chunk) ──────────────────────────────────────────────────────────

def _aiff(path: Path, track_id: str) -> dict:
    """AIFF: the tag is an ID3 ``ID3 `` chunk inside the IFF container, never
    at the head of the file — ``MP3`` / ``ID3(path)`` can't read it; the
    stream info comes from the COMM chunk."""
    try:
        audio = AIFF(path)
        info, tags = audio.info, audio.tags
    except Exception as exc:
        _swallow_io(exc)
        # A bad ID3 chunk (an unsupported version, a cut frame) fails the
        # whole load: keep the COMM stream info without tags.  A bad COMM
        # still raises here.
        with open(path, "rb") as fh:
            info, tags = AIFFInfo(fh), None
    d: dict = {
        "id": track_id,
        "path": str(path),
        "format": "AIFF",
        "duration": info.length,
        "bitrate": info.bitrate,
        "channels": info.channels,
        "sample_rate": info.sample_rate,
        "bit_depth": info.bits_per_sample,
    }
    d.update(_id3_fields(tags))
    return d


# ── FLAC ──────────────────────────────────────────────────────────────────────

def _flac(path: Path, track_id: str) -> dict:
    audio = FLAC(path)
    tags = audio.tags or {}

    def g(key, default=""):
        vals = audio.get(key.lower(), [])
        return vals[0] if vals else default

    trck = g("tracknumber")
    tpos = g("discnumber")
    # Bitrate: the uncompressed PCM bps figure mutagen advertises is
    # misleading — for FLAC users want the *actual* compressed bitrate
    # (which is what the file occupies on disk per second of playback).
    # Compute file-size × 8 / duration for the true number; fall back to
    # the uncompressed-PCM estimate only when duration is missing.
    duration = audio.info.length or 0
    flac_bitrate = audio.info.bits_per_sample * audio.info.sample_rate
    try:
        size = path.stat().st_size
        if duration and duration > 0:
            flac_bitrate = int(size * 8 / duration)
    except OSError:
        pass
    d: dict = {
        "id": track_id,
        "path": str(path),
        "format": "FLAC",
        "duration": duration,
        "bitrate": flac_bitrate,
        "channels": audio.info.channels,
        "sample_rate": audio.info.sample_rate,
        "bit_depth": audio.info.bits_per_sample,
        "title": g("title", path.stem),
        "artist": g("artist"),
        "album_artist": g("albumartist") or g("album artist"),
        "album": g("album"),
        "composer": g("composer"),
        "comment": g("comment"),
        "label": g("organization") or g("label"),
        "isrc": g("isrc"),
        "game": (g("game") or "").strip(),
        "bpm": _float(g("bpm", None)),
        "genre": audio.get("genre", []),
        "year": _year(g("date", None)),
        "track_number": _int(trck),
        "total_tracks": _total(trck),
        "disc_number": _int(tpos),
        "total_discs": _total(tpos),
    }
    if audio.pictures:
        pic = audio.pictures[0]
        d["cover_art"] = _cover_b64(pic.data, pic.mime)
    d.update(_replaygain_from_vorbis(tags))
    return d


# ── ALAC / AAC / M4A (MP4 container) ─────────────────────────────────────────

def _mp4(path: Path, track_id: str) -> dict:
    audio = MP4(path)
    tags = audio.tags or {}

    def g(key, default=""):
        v = tags.get(key, [default])
        if not v:
            return default
        # A freeform ``----:`` atom holds bytes (``str()`` would give "b'…'").
        return v[0].decode("utf-8", "replace") if isinstance(v[0], bytes) else str(v[0])

    # ``tags.get("trkn", default)`` returns the default only when the key is
    # absent — an explicit empty list, or a 1-element tuple from a malformed
    # atom, would still crash a later ``trkn[1]`` access.  Normalise to a
    # 2-tuple here so downstream code can index freely.
    def _pair(raw):
        v = raw[0] if raw else None
        if not isinstance(v, tuple):
            return (None, None)
        if len(v) < 2:
            return (v[0] if v else None, None)
        return v

    trkn = _pair(tags.get("trkn") or [(None, None)])
    disk = _pair(tags.get("disk") or [(None, None)])

    # Codec detection — prefer ffprobe over mutagen's heuristic.  Mutagen
    # reads the codec name from the atom table; for files written by
    # certain encoders (notably older iTunes Match exports) that token
    # reads "mp4a" without disambiguating ALAC vs AAC.  ffprobe always
    # returns the real codec name from the elementary-stream header, so
    # we end up with the right format label even on those edge cases.
    fmt: str | None = None
    try:
        probed = forksafe.run(
            ["ffprobe", "-v", "quiet",
             "-select_streams", "a:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if probed.returncode == 0:
            codec_name = (probed.stdout or "").strip().lower()
            if codec_name == "alac":
                fmt = "ALAC"
            elif codec_name == "aac":
                fmt = "AAC"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    if fmt is None:
        # Fallback to mutagen heuristic when ffprobe unavailable.  Note we
        # never produce the old "ALAC/AAC" combo string — we pick one
        # side and commit, so the column stays canonical.
        fmt = "ALAC" if getattr(audio.info, "codec", "").startswith("alac") else "AAC"

    d: dict = {
        "id": track_id,
        "path": str(path),
        "format": fmt,
        "duration": audio.info.length,
        "bitrate": getattr(audio.info, "bitrate", None),
        "channels": audio.info.channels,
        "sample_rate": audio.info.sample_rate,
        "bit_depth": getattr(audio.info, "bits_per_sample", None),
        "title": g("\xa9nam", path.stem),
        "artist": g("\xa9ART"),
        "album_artist": g("aART"),
        "album": g("\xa9alb"),
        "composer": g("\xa9wrt"),
        "comment": g("\xa9cmt"),
        "label": g("----:com.apple.iTunes:LABEL", "") or g("\xa9grp", ""),
        "isrc": g("----:com.apple.iTunes:ISRC", ""),
        "game": _mp4_game(tags, g),
        "bpm": _float(g("tmpo", None)),
        "genre": _list(tags.get("\xa9gen", [])),
        "year": _year(g("\xa9day", None)),
        "track_number": trkn[0],
        "total_tracks": trkn[1],
        "disc_number": disk[0],
        "total_discs": disk[1],
    }
    covers = tags.get("covr", [])
    if covers:
        d["cover_art"] = _cover_b64(bytes(covers[0]))
    return d


# ── Vorbis comment (Ogg, Opus) ────────────────────────────────────────────────

def _vorbis(path: Path, track_id: str, audio, fmt: str) -> dict:
    tags = audio.tags or {}

    def g(key, default=""):
        v = tags.get(key.lower(), [])
        return v[0] if v else default

    trck = g("tracknumber")
    tpos = g("discnumber")
    out: dict = {
        "id": track_id,
        "path": str(path),
        "format": fmt,
        "duration": audio.info.length,
        "bitrate": getattr(audio.info, "bitrate", None),
        "channels": audio.info.channels,
        # (Opus has no sample-rate field: it always decodes at 48 kHz —
        # reading ``info.sample_rate`` raised and dropped every tag)
        "sample_rate": getattr(audio.info, "sample_rate", None) or (48000 if fmt == "Opus" else None),
        "title": g("title", path.stem),
        "artist": g("artist"),
        "album_artist": g("albumartist") or g("album_artist"),
        "album": g("album"),
        "composer": g("composer"),
        "comment": g("comment"),
        "label": g("organization") or g("label"),
        "isrc": g("isrc"),
        "game": (g("game") or "").strip(),
        "bpm": _float(g("bpm", None)),
        "genre": tags.get("genre", []),
        "year": _year(g("date", None)),
        "track_number": _int(trck),
        "total_tracks": _total(trck),
        "disc_number": _int(tpos),
        "total_discs": _total(tpos),
    }
    out.update(_replaygain_from_vorbis(tags))
    return out


# ── SID (C64) ────────────────────────────────────────────────────────────────

# Real SID files top out well under 64 KB; cap the whole-file read used for
# the HVSC MD5 so a mislabelled huge ``.sid`` can't exhaust memory.
_SID_MAX_BYTES = 1024 * 1024


def _extract_sid(path: Path, track_id: str) -> dict:
    """Parse PSID/RSID binary header to extract SID metadata."""
    from soniqboom.config import settings

    # Read the whole file once: the header drives metadata, and the MD5 of the
    # ENTIRE file is the key HVSC's Songlengths database is indexed by.  SID
    # files are tiny (2–64 KB), so this is cheap — and for remote tracks the
    # ``path`` here is a temp file holding the full download (HEADER_BUDGET is
    # None for .sid), so the MD5 matches the real file.  Cap the read so a
    # mislabelled giant ``.sid`` can't be slurped into RAM — anything over the
    # cap isn't a real SID and won't match HVSC anyway.
    with open(path, "rb") as f:
        data = f.read(_SID_MAX_BYTES)
    header = data[:124]
    sid_md5 = hashlib.md5(data).hexdigest()

    if len(header) < 118 or header[0:4] not in (b"PSID", b"RSID"):
        return {
            "id": track_id, "path": str(path), "title": path.stem,
            "format": "SID", "duration": float(settings.sid_default_duration),
            "genre": ["Chiptune", "C64"], "sid_md5": sid_md5,
        }

    version = struct.unpack(">H", header[4:6])[0]

    # PSID/RSID header string fields are ISO-8859-1 by spec — decode as
    # Latin-1-native so accented names (Hülsbeck, Følner, …) aren't corrupted
    # by the CP437 fallback that trackers need.
    title_raw     = _decode_tracker_str(header[22:54], latin1_native=True)
    artist_raw    = _decode_tracker_str(header[54:86], latin1_native=True)
    copyright_raw = _decode_tracker_str(header[86:118], latin1_native=True)

    # Subsong info (bytes 14-17)
    subsongs = struct.unpack(">H", header[14:16])[0]
    default_song = struct.unpack(">H", header[16:18])[0]

    # SID model & channel count (PSID v2+, flags at offset 0x76 = 118)
    sid_model: str | None = None
    channels = 1
    if version >= 2 and len(header) >= 120:
        flags = struct.unpack(">H", header[0x76:0x78])[0]
        sid_bits = (flags >> 4) & 0x03
        sid_model = {0: None, 1: "6581", 2: "8580", 3: "6581/8580"}.get(sid_bits)
        # Second SID flag bits 6-7
        second_sid = (flags >> 6) & 0x03
        if second_sid:
            channels = 2
        # Third SID flag bits 8-9 (PSID v3+/v4)
        if version >= 3 and len(header) >= 124:
            third_sid = (flags >> 8) & 0x03
            if third_sid:
                channels = 3

    # Try to extract a 4-digit year from the copyright string
    year: int | None = None
    m = re.search(r"\b(19|20)\d{2}\b", copyright_raw)
    if m:
        year = int(m.group())

    d: dict = {
        "id": track_id,
        "path": str(path),
        "format": "SID",
        "title": title_raw or path.stem,
        "artist": artist_raw,
        "comment": copyright_raw,
        "year": year,
        "duration": float(settings.sid_default_duration),
        "genre": ["Chiptune", "C64"],
        "subsongs": subsongs if subsongs and subsongs > 1 else None,
        "channels": channels,
        "sid_md5": sid_md5,
    }
    if sid_model:
        d["sid_model"] = sid_model

    # ── HVSC enrichment ──────────────────────────────────────────────
    # When the user has pointed at the High Voltage SID Collection
    # documents folder, swap our default-duration estimate for the real
    # per-subsong durations and attach the STIL commentary blob.  Durations
    # match by the cached whole-file MD5 (works local OR remote); STIL is
    # path-keyed, so it resolves here only for LOCAL files at their canonical
    # HVSC paths — remote tracks pick up STIL in the re-apply pass, which has
    # the real remote path (this ``path`` is a temp file for remote scans).
    try:
        from soniqboom.core.hvsc import get_hvsc
        hvsc = get_hvsc()
        if hvsc.is_configured():
            durations = hvsc.lookup_durations_by_md5(sid_md5)
            if durations:
                d["duration"]    = durations[0]
                d["hvsc_lengths"] = durations
                # Update subsong count if HVSC disagrees with the PSID header.
                if len(durations) > 1:
                    d["subsongs"] = len(durations)
            stil = hvsc.lookup_stil(path)
            if stil and stil.get("text"):
                d["stil"] = stil["text"]
    except Exception:
        log.exception("HVSC enrichment failed for %s", path)

    # The default tune (header start song, 1-based) when it isn't tune 1: the
    # bare track id plays it, so it is recorded (0-based) for the Subsonic tune
    # ids and the web picker, and the track's duration is ITS length.
    start = sid_default_tune(default_song, d.get("subsongs"))
    if start:
        d["start_subsong"] = start
        lengths = d.get("hvsc_lengths")
        if lengths and start < len(lengths) and lengths[start]:
            d["duration"] = lengths[start]

    return d


def sid_default_tune(start_song, count) -> int | None:
    """0-based index of a multi-tune file's default tune from its 1-based
    start song (PSID/RSID header word, SNDH ``!#``) and tune count — only when
    it is a valid tune other than tune 1, else None (the ``start_subsong``
    field's convention)."""
    if (isinstance(start_song, int) and isinstance(count, int)
            and 2 <= start_song <= count):
        return start_song - 1
    return None


# ── MIDI ─────────────────────────────────────────────────────────────────────

def _extract_midi(path: Path, track_id: str) -> dict:
    """Extract MIDI metadata via mido."""
    try:
        import mido
    except ImportError:
        log.warning("mido not installed — MIDI metadata will be minimal")
        return {
            "id": track_id, "path": str(path), "title": path.stem,
            "format": "MIDI", "duration": 0.0, "genre": ["MIDI"],
        }

    try:
        mid = mido.MidiFile(str(path))
    except Exception as exc:
        log.warning("Failed to parse MIDI file %s: %s", path, exc)
        return {
            "id": track_id, "path": str(path), "title": path.stem,
            "format": "MIDI", "duration": 0.0, "genre": ["MIDI"],
        }

    duration = mid.length  # seconds (float)

    # Look for track_name meta messages
    title = ""
    for track in mid.tracks:
        for msg in track:
            if msg.type == "track_name" and msg.name.strip():
                title = msg.name.strip()
                break
        if title:
            break

    # Collect distinct channels and program changes
    used_channels: set[int] = set()
    program_numbers: set[int] = set()
    for track in mid.tracks:
        for msg in track:
            if hasattr(msg, "channel"):
                used_channels.add(msg.channel)
            if msg.type == "program_change":
                program_numbers.add(msg.program)

    # Map General MIDI program numbers to names
    instruments = [_GM_PROGRAMS.get(p, f"Program {p}") for p in sorted(program_numbers)]

    return {
        "id": track_id,
        "path": str(path),
        "format": "MIDI",
        "title": title or path.stem,
        "duration": duration,
        "genre": ["MIDI"],
        "channels": len(used_channels) if used_channels else None,
        "instruments": instruments if instruments else None,
        "patterns": len(mid.tracks),  # stored as midi_tracks via patterns field
    }


# ── libgme chiptune (NSF / SPC / GBS / VGM / AY / KSS / SAP / HES / GYM) ──

# Rippers fill unknown header fields with a placeholder rather than leaving
# them blank ("<?>" is the NSF/GBS convention).  Never an album, never an
# artist (``_extract_gme`` filters both through ``_game_name``).
_GAME_PLACEHOLDERS = frozenset({"<?>", "?", "??", "???", "unknown", "n/a", "-"})


def _game_name(raw: str) -> str:
    """A header's game-name field, trimmed with inner whitespace collapsed —
    or ``""`` when it is empty, a ripper placeholder, or not text at all
    (control characters ⇒ a binary/garbage field, never shown as an album)."""
    if not raw:
        return ""
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return ""
    s = " ".join(raw.split())
    if not s or s.lower() in _GAME_PLACEHOLDERS:
        return ""
    return s


def _nsfe_auth(path: Path) -> tuple[str, str]:
    """``(game, artist)`` from an NSFe file's ``auth`` chunk.

    NSFe is chunked (``[u32 LE size][4-byte id][data]`` after the ``NSFE``
    magic); ``auth`` holds four NUL-terminated strings — game title, artist,
    copyright, ripper.  Walks chunk HEADERS only (seeking past each body), so
    a large ``DATA`` chunk is never read; a truncated partial fetch simply
    ends the walk."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"NSFE":
                return "", ""
            for _ in range(64):                    # a real file has < 10 chunks
                h = f.read(8)
                if len(h) < 8:
                    break
                size, cid = struct.unpack("<I4s", h)
                if cid == b"auth":
                    fields = f.read(min(size, 4096)).split(b"\x00")
                    game = _decode_tracker_str(fields[0]) if fields else ""
                    artist = _decode_tracker_str(fields[1]) if len(fields) > 1 else ""
                    return game, artist
                if cid == b"NEND":
                    break
                f.seek(size, 1)
    except (OSError, struct.error) as exc:
        _swallow_io(exc)
    return "", ""


# SPC extended ID666 ("xid6") — after the 64 KB RAM dump + DSP/extra RAM.
_SPC_XID6_OFFSET = 0x10200
_SPC_XID6_MAX = 0x4000          # the real chunk is a few hundred bytes


def _spc_xid6_game(path: Path) -> str:
    """The game name from an SPC's extended ID666 (``xid6``) chunk, or ``""``.

    The fixed ID666 game field is 32 bytes, so longer names are cut off
    ("Street Fighter 2 - The World Warr"); the extended tag's sub-chunk 0x02
    holds the full string.  Sub-chunk header: id u8, type u8, length u16 LE;
    type 0 keeps its value in the length field (no payload), otherwise the
    payload is ``length`` bytes padded to 4.  Anything malformed → ``""``."""
    try:
        with open(path, "rb") as f:
            f.seek(_SPC_XID6_OFFSET)
            blob = f.read(_SPC_XID6_MAX)
    except OSError as exc:
        _swallow_io(exc)
        return ""
    if len(blob) < 8 or blob[:4] != b"xid6":
        return ""
    end = min(8 + struct.unpack_from("<I", blob, 4)[0], len(blob))
    pos = 8
    while pos + 4 <= end:
        sid, typ = blob[pos], blob[pos + 1]
        length = struct.unpack_from("<H", blob, pos + 2)[0]
        pos += 4
        if typ == 0:
            continue
        if pos + length > end:
            break
        if sid == 0x02 and typ == 1:
            return _game_name(_decode_tracker_str(blob[pos:pos + length]))
        pos += (length + 3) & ~3
    return ""


# GD3 lives at the END of a VGM (after the command stream).  A .vgz is gzip, so
# reaching it means decompressing up to that point — capped so a corrupt or
# hostile offset can't make the scan worker inflate gigabytes.
_VGM_MAX_GD3_OFFSET = 64 * 1024 * 1024
_GD3_MAX_BYTES = 64 * 1024


def _vgm_gd3(path: Path) -> dict[str, str]:
    """``{"track", "game", "author"}`` from a VGM/VGZ GD3 tag (English field,
    Japanese fallback), or ``{}``.

    Reads the 0x18-byte header, then seeks straight to the GD3 block (a gzip
    stream's forward seek decompresses-and-discards without buffering the
    whole file).  GD3 strings are NUL-terminated UTF-16LE in a fixed order:
    track EN/JP, game EN/JP, system EN/JP, author EN/JP, date, ripper, notes."""
    import gzip
    try:
        with open(path, "rb") as raw:
            gz = raw.read(2) == b"\x1f\x8b"
            raw.seek(0)
            f = gzip.GzipFile(fileobj=raw) if gz else raw
            try:
                hdr = f.read(0x18)
                if len(hdr) < 0x18 or hdr[:4] != b"Vgm ":
                    return {}
                gd3_rel = struct.unpack_from("<I", hdr, 0x14)[0]
                off = 0x14 + gd3_rel
                if not gd3_rel or off > _VGM_MAX_GD3_OFFSET:
                    return {}
                f.seek(off)
                th = f.read(12)
                if len(th) < 12 or th[:4] != b"Gd3 ":
                    return {}
                length = struct.unpack_from("<I", th, 8)[0]
                body = f.read(min(length, _GD3_MAX_BYTES))
            finally:
                if gz:
                    f.close()
    except Exception as exc:               # corrupt gzip/zlib stream, short file…
        _swallow_io(exc)                   # — never fail the whole extract
        return {}
    body = body[: len(body) - (len(body) % 2)]
    fields = body.decode("utf-16-le", "replace").split("\x00")

    def pick(en: int, jp: int) -> str:
        for i in (en, jp):
            if i < len(fields):
                v = " ".join(fields[i].split())
                if v and "�" not in v:
                    return v
        return ""

    return {"track": pick(0, 1), "game": pick(2, 3), "author": pick(6, 7)}


def _extract_gme(path: Path, track_id: str) -> dict:
    """Best-effort header read for libgme-rendered chiptune formats.

    Most of these formats have a small, well-documented header with a
    title + artist string.  We parse just enough to display in the UI;
    detailed track-list metadata (multi-song NSFs, SPC ID666) needs
    the actual gme library and is left to the renderer.

    The GAME a console rip belongs to is read into ``album`` (provenance
    ``album_source="tag"``) from the header field each format defines for it:
    SPC ID666 game title (0x4E; the extended ``xid6`` tag's full name when
    the 32-byte field is full), the NSF name / NSFe ``auth`` game title, the
    GBS title, and the VGM/VGZ GD3 game name."""
    from soniqboom.config import settings
    ext = path.suffix.lower()
    fmt = FORMAT_NAMES.get(ext, ext.lstrip(".").upper())
    title = path.stem
    artist = ""
    game = ""
    duration = float(getattr(settings, "sid_default_duration", 180))
    try:
        with open(path, "rb") as f:
            hdr = f.read(256)
    except OSError as exc:
        _swallow_io(exc)
        hdr = b""

    # NSF header (NES Sound Format) — 0x80 bytes, fields at fixed offsets.
    # The "name" field is the game (or, for a homebrew release, the album).
    if ext in (".nsf", ".nsfe") and hdr[:5] == b"NESM\x1a":
        title  = _decode_tracker_str(hdr[0x0E:0x2E])
        artist = _decode_tracker_str(hdr[0x2E:0x4E])
        game   = _game_name(title)
    # NSFe — chunked; the game/artist live in the ``auth`` chunk.
    elif ext in (".nsf", ".nsfe") and hdr[:4] == b"NSFE":
        g, a = _nsfe_auth(path)
        game = _game_name(g)
        if _game_name(a):
            artist = a
    # SPC700 ID666 (SNES) — 0x100 byte SPC header + 0xD0-byte ID666 block.
    # Byte 0x23 = 27 means "no ID666 tag" (the fields are then garbage).
    elif ext == ".spc" and hdr[:33] == b"SNES-SPC700 Sound File Data v0.30":
        title  = _decode_tracker_str(hdr[0x2E:0x4E])
        artist = _decode_tracker_str(hdr[0xB1:0xD1])
        if len(hdr) > 0x6E and hdr[0x23] != 27:
            game = _game_name(_decode_tracker_str(hdr[0x4E:0x6E]))
            if game and b"\x00" not in hdr[0x4E:0x6E]:
                # A full field may be a cut-off name — prefer the extended tag.
                game = _spc_xid6_game(path) or game
    # GBS (Game Boy Sound) — 0x70 byte header; the title field is the game.
    elif ext == ".gbs" and hdr[:3] == b"GBS":
        title  = _decode_tracker_str(hdr[0x10:0x30])
        artist = _decode_tracker_str(hdr[0x30:0x50])
        game   = _game_name(title)
    # VGM / VGZ — GD3 tag (track, game, author).
    elif ext in (".vgm", ".vgz"):
        gd3 = _vgm_gd3(path)
        if gd3.get("track"):
            title = gd3["track"]
        if gd3.get("author"):
            artist = gd3["author"]
        game = _game_name(gd3.get("game", ""))
    # Other formats fall back to filename; gme renderer will surface
    # the proper metadata when streaming.

    # A ripper placeholder ("<?>") or a garbage field names nobody — left
    # empty, the Modland apply can credit the real composer.
    artist = _game_name(artist)
    d = {
        "id": track_id,
        "path": str(path),
        "format": fmt,
        "title": title or path.stem,
        "duration": duration,
        "genre": ["Chiptune"],
    }
    if artist:
        d["artist"] = artist   # TrackMeta.artist is str — never None
    if game:
        d["album"] = game
        d["album_source"] = "tag"
        d["game_by_tag"] = game                # the header's own game name
    return d


# ── DSD (.dsf / .dff / .wsd) ────────────────────────────────────────────────

def _dsd_quality_label(sample_rate: int | None) -> str:
    """Return ``DSDxxx`` based on the source rate.  DSD64 = 64×CD =
    2.8224 MHz; DSD128 = 5.6448 MHz; DSD256 = 11.2896 MHz; DSD512 = 22.5792 MHz."""
    if not sample_rate:
        return "DSD"
    # Round to nearest 100 kHz so we don't trip on 2822399 vs 2822400.
    rate = round(sample_rate / 100_000)
    if rate >= 220:
        return "DSD512"
    if rate >= 110:
        return "DSD256"
    if rate >= 55:
        return "DSD128"
    if rate >= 27:
        return "DSD64"
    return "DSD"


def _extract_dsd(path: Path, track_id: str) -> dict:
    """Pull duration / sample-rate / channels from a DSD file via ffprobe.

    Mutagen's DSD support is limited (DSF only, and even then the
    tag-reading path is fragile) and we already require ffmpeg for the
    actual transcode — using ffprobe keeps the extractor path consistent
    across all three DSD containers (DSF/DFF/WSD)."""
    from soniqboom.config import settings
    import json
    import subprocess

    ext = path.suffix.lower()
    bin_ = settings.ffmpeg_path
    # ffprobe lives alongside ffmpeg; derive its path from the configured
    # ffmpeg binary so installations with a custom ffmpeg also find ffprobe.
    if bin_:
        probe = str(Path(bin_).parent / "ffprobe")
        if not Path(probe).exists():
            probe = "ffprobe"
    else:
        probe = "ffprobe"

    d: dict = {
        "id": track_id,
        "path": str(path),
        "title": path.stem,
        "format": "DSD",
    }
    try:
        out = forksafe.run(
            [probe, "-v", "error",
             "-show_entries",
             "stream=sample_rate,channels,duration:format=duration,size,bit_rate:format_tags",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            log.warning("ffprobe failed on %s (%s): %s",
                        path, ext, (out.stderr or "").strip()[:200])
            return d
        meta = json.loads(out.stdout or "{}")
        streams = meta.get("streams") or [{}]
        fmt_info = meta.get("format") or {}
        s = streams[0] if streams else {}
        sr = _int(s.get("sample_rate"))
        ch = _int(s.get("channels"))

        # Duration fallback chain — DFF in particular often omits
        # format.duration in ffprobe output (no fixed-size header), so the
        # player ends up with a 0:00 timeline and no Range-target ceiling.
        # 1) format.duration   (DSF, well-formed DFF)
        # 2) streams[0].duration (some DFF builds expose it here)
        # 3) filesize ÷ (sample_rate × channels / 8) — DSD is 1 bit/sample
        #    so total bytes ≈ duration × sr × ch / 8.  Works for any of the
        #    three containers when ffprobe declines to compute it.
        #
        # Edge case still un-handled: very short DSD samples (<2 s test
        # tones) where the container header dwarfs the audio payload.  The
        # filesize fallback over-estimates duration by the header size in
        # that regime, but real music libraries don't contain sub-2-s
        # files so we don't pay the precision cost of subtracting a fixed
        # header constant.  If this surfaces, switch to ``size − DSD_HDR``
        # where DSD_HDR is ~92 bytes (DSF) or variable (DFF).
        duration = float(fmt_info.get("duration") or 0) or 0.0
        if duration <= 0:
            duration = float(s.get("duration") or 0) or 0.0
        if duration <= 0 and sr and ch:
            size = _int(fmt_info.get("size"))
            if not size:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
            if size:
                # DSD audio payload only — the container header is a few
                # KB, well inside the precision the UI needs.
                duration = size / (sr * ch / 8.0)

        d.update({
            "sample_rate": sr,
            "channels": ch,
            "duration": duration,
            "format": _dsd_quality_label(sr),
            "bit_depth": 1,
        })
        # Tags: prefer mutagen over ffprobe.
        #
        # ffprobe's text-tag decode mangles non-Latin-1 bytes — Japanese
        # DFF rips from Pyramix mastering software (Suara - キミガタメ
        # and friends) come back as ``"\x1bnK��"`` instead of
        # ``"キミガタメ"``.  Mutagen's DSDIFF / DSF readers parse the
        # embedded ID3v2 frames directly with the correct per-frame
        # encoding byte (0x03 = UTF-8, 0x01 = UTF-16+BOM, …) and
        # produce the right Unicode.  Fall back to ffprobe's tags only
        # if mutagen can't open the file at all (corrupt container).
        mutagen_tags: dict[str, str] = {}
        try:
            from mutagen import File as _MutagenFile
            mf = _MutagenFile(path)
            if mf is not None and getattr(mf, "tags", None):
                # ID3 frame → our field-name mapping.  Genre / track /
                # disc keep their multi-value / "N/M" semantics handled
                # below.
                _ID3_MAP = {
                    "TIT2": "title",
                    "TPE1": "artist",
                    "TALB": "album",
                    "TPE2": "album_artist",
                    "TCOM": "composer",
                    "TDRC": "year",
                    "TCON": "genre",
                    "TRCK": "track_number",
                    "TPOS": "disc_number",
                    "COMM": "comment",
                    "TPUB": "label",
                    "TSRC": "isrc",
                }
                for frame_id, dst in _ID3_MAP.items():
                    frame = mf.tags.get(frame_id)
                    if not frame:
                        continue
                    # ID3 frames expose ``.text`` as a list.  COMM has
                    # ``.text`` too but the value is a list of strings.
                    txt = getattr(frame, "text", None)
                    if txt is None:
                        continue
                    val = txt[0] if isinstance(txt, list) and txt else txt
                    if not val:
                        continue
                    mutagen_tags[dst] = str(val)
        except Exception as exc:
            log.debug("DSD mutagen tag read failed for %s: %s", path, exc)

        # ffprobe tags as fallback (lowercased keys → our field names).
        # If mutagen produced a value we trust that; otherwise take
        # ffprobe's.
        ffprobe_tags = {k.lower(): v for k, v in (fmt_info.get("tags") or {}).items()}
        _FFPROBE_MAP = {
            "title": "title",
            "artist": "artist",
            "album": "album",
            "albumartist": "album_artist",
            "composer": "composer",
            "date": "year",
            "genre": "genre",
            "track": "track_number",
            "disc": "disc_number",
        }
        merged: dict[str, str] = {}
        for src, dst in _FFPROBE_MAP.items():
            if dst in mutagen_tags:
                merged[dst] = mutagen_tags[dst]
            elif ffprobe_tags.get(src):
                merged[dst] = ffprobe_tags[src]
        # Mutagen-only fields (comment / label / isrc) — pass through.
        for k in ("comment", "label", "isrc"):
            if k in mutagen_tags:
                merged[k] = mutagen_tags[k]

        for dst, v in merged.items():
            if dst == "year":
                d[dst] = _year(v)
            elif dst == "track_number":
                d[dst] = _int(v)
                d["total_tracks"] = _total(v)
            elif dst == "disc_number":
                d[dst] = _int(v)
                d["total_discs"] = _total(v)
            elif dst == "genre":
                d[dst] = [v] if isinstance(v, str) else list(v)
            else:
                d[dst] = v
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as exc:
        log.warning("DSD extract fallback for %s: %s", path, exc)
    return d


# ── Tracker modules (MOD / S3M / XM / IT / …) ──────────────────────────────

def _extract_tracker(path: Path, track_id: str) -> dict:
    """Extract tracker module metadata by parsing binary headers.

    Falls back gracefully if headers are malformed — uses filename as title.
    """
    ext = path.suffix.lower()
    title = ""
    fmt = FORMAT_NAMES.get(ext, ext.lstrip(".").upper())
    instruments: list[str] = []
    channels: int | None = None
    patterns: int | None = None

    try:
        with open(path, "rb") as f:
            raw = f.read()  # read full file for instrument headers
        if raw[:4] == b"XPKF":
            # An XPK-packed module: parse the unpacked one (uade plays one
            # unpacked too — ``api.stream._xpk_unpacked``; openmpt unpacks
            # XPK itself).
            from soniqboom.core import xpk as _xpk
            try:
                raw = _xpk.unpack(raw)
            except _xpk.XpkError as exc:
                log.debug("XPK unpack failed for %s: %s", path, exc)

        if ext == ".mod" and len(raw) >= 1084:
            title = _decode_tracker_str(raw[0:20])

            # Channel count from magic bytes at offset 1080
            magic = raw[1080:1084]
            channels = _MOD_MAGIC_CHANNELS.get(magic, 4)

            # 31 sample headers at bytes 20-949 (each 30 bytes)
            for i in range(31):
                offset = 20 + i * 30
                if offset + 30 > len(raw):
                    break
                try:
                    name = _decode_tracker_str(raw[offset:offset + 22])
                    if name and name.isprintable():
                        instruments.append(name)
                except Exception:
                    pass

            # Pattern count: highest pattern number in the order table + 1
            try:
                song_length = raw[950]
                order_table = raw[952:952 + 128]
                if song_length > 0:
                    patterns = max(order_table[:song_length]) + 1
            except Exception:
                pass

        elif ext == ".s3m" and len(raw) >= 96:
            title = _decode_tracker_str(raw[0:28])

            # Header fields
            num_orders = struct.unpack("<H", raw[32:34])[0]
            num_instruments = struct.unpack("<H", raw[34:36])[0]
            patterns = struct.unpack("<H", raw[36:38])[0]

            # Channel count: count active channels from channel settings (offset 64, 32 bytes)
            ch_count = 0
            for i in range(32):
                if raw[64 + i] < 128:  # bit 7 clear = channel enabled
                    ch_count += 1
            channels = ch_count if ch_count > 0 else None

            # Instrument names: parapointers start after orders
            para_offset = 96 + num_orders
            for i in range(num_instruments):
                if para_offset + i * 2 + 2 > len(raw):
                    break
                try:
                    ptr = struct.unpack("<H", raw[para_offset + i * 2:para_offset + i * 2 + 2])[0] * 16
                    if ptr + 48 <= len(raw):
                        name = _decode_tracker_str(raw[ptr + 48:ptr + 76])
                        if name and name.isprintable():
                            instruments.append(name)
                except Exception:
                    pass

        elif ext == ".xm" and len(raw) >= 80:
            # XM starts with "Extended Module: " (17 bytes), then 20-byte title
            if raw[0:17] == b"Extended Module: ":
                title = _decode_tracker_str(raw[17:37])

            header_size = struct.unpack("<I", raw[60:64])[0]
            channels = struct.unpack("<H", raw[68:70])[0]
            patterns = struct.unpack("<H", raw[70:72])[0]
            num_instruments = struct.unpack("<H", raw[72:74])[0]

            # Walk instrument headers to get names
            inst_offset = 60 + header_size
            for i in range(num_instruments):
                if inst_offset + 29 > len(raw):
                    break
                try:
                    inst_hdr_size = struct.unpack("<I", raw[inst_offset:inst_offset + 4])[0]
                    name = _decode_tracker_str(raw[inst_offset + 4:inst_offset + 26])
                    if name and name.isprintable():
                        instruments.append(name)
                    num_samples = struct.unpack("<H", raw[inst_offset + 27:inst_offset + 29])[0]
                    if num_samples > 0 and inst_offset + inst_hdr_size <= len(raw):
                        # Skip sample headers and data
                        sample_hdr_size = struct.unpack("<I", raw[inst_offset + 29:inst_offset + 33])[0] if inst_offset + 33 <= len(raw) else 40
                        sample_offset = inst_offset + inst_hdr_size
                        total_sample_data = 0
                        for s in range(num_samples):
                            sh_off = sample_offset + s * sample_hdr_size
                            if sh_off + 4 <= len(raw):
                                total_sample_data += struct.unpack("<I", raw[sh_off:sh_off + 4])[0]
                        inst_offset = sample_offset + num_samples * sample_hdr_size + total_sample_data
                    else:
                        inst_offset += inst_hdr_size
                except Exception:
                    break

        elif ext == ".it" and len(raw) >= 192:
            if raw[0:4] == b"IMPM":
                title = _decode_tracker_str(raw[4:30])

                num_orders = struct.unpack("<H", raw[32:34])[0]
                num_instruments = struct.unpack("<H", raw[34:36])[0]
                num_samples = struct.unpack("<H", raw[36:38])[0]
                patterns = struct.unpack("<H", raw[38:40])[0]

                # Channel count from channel panning table (offset 64, 64 bytes)
                ch_count = 0
                for i in range(64):
                    if 64 + i < len(raw) and raw[64 + i] < 128:  # bit 7 clear = enabled
                        ch_count += 1
                channels = ch_count if ch_count > 0 else None

                # Instrument names from instrument pointer table
                inst_ptr_offset = 192 + num_orders
                for i in range(num_instruments):
                    ptr_off = inst_ptr_offset + i * 4
                    if ptr_off + 4 > len(raw):
                        break
                    try:
                        ptr = struct.unpack("<I", raw[ptr_off:ptr_off + 4])[0]
                        if ptr + 32 <= len(raw):
                            name = _decode_tracker_str(raw[ptr + 4:ptr + 30])
                            if name and name.isprintable():
                                instruments.append(name)
                    except Exception:
                        pass

                # If no instrument names, try sample names
                if not instruments:
                    smp_ptr_offset = inst_ptr_offset + num_instruments * 4
                    for i in range(num_samples):
                        ptr_off = smp_ptr_offset + i * 4
                        if ptr_off + 4 > len(raw):
                            break
                        try:
                            ptr = struct.unpack("<I", raw[ptr_off:ptr_off + 4])[0]
                            if ptr + 30 <= len(raw):
                                name = _decode_tracker_str(raw[ptr + 4:ptr + 30])
                                if name and name.isprintable():
                                    instruments.append(name)
                        except Exception:
                            pass

        elif ext in (".hvl", ".ahx") and len(raw) >= 6:
            # AHX / HivelyTracker store the song name as a NUL-terminated
            # string at the big-endian offset in bytes 4-5 — per the
            # HivelyTracker replay (hvl2wav/replay.c):
            #   strncpy(ht_Name, &buf[(buf[4]<<8)|buf[5]], 128)
            # Offset 0 (the generic heuristic below) is the format MAGIC
            # ("HVL\0" / "THX\0"), so every module would get title "HVL"/"THX"
            # and distinct files collapse under duplicate-filtering.
            name_off = (raw[4] << 8) | raw[5]
            if 0 < name_off < len(raw):
                end = raw.find(b"\x00", name_off)
                if end == -1:
                    end = min(name_off + 128, len(raw))
                title = _decode_tracker_str(raw[name_off:end])

        else:
            # Other tracker formats — try reading first 20 bytes as title
            if len(raw) >= 20:
                candidate = _decode_tracker_str(raw[0:20])
                if candidate and candidate.isprintable():
                    title = candidate

    except Exception as exc:
        log.debug("Tracker header parse failed for %s: %s", path, exc)

    # Try to get duration via openmpt123 --info
    duration = 0.0
    try:
        from soniqboom.config import settings
        import shutil
        binary = settings.openmpt123_path or shutil.which("openmpt123")
        if binary:
            # forksafe: this is THE hot fork site — it runs from scan and
            # drill-down worker threads (206 of the 219 segfault dumps on
            # 2026-07-02 crashed exactly here).
            result = forksafe.run(
                [binary, "--info", str(path)],
                capture_output=True, text=True, timeout=10,
            )
            # Parse "Duration" line from info output
            for line in result.stdout.splitlines():
                if "duration" in line.lower():
                    # Try to find seconds value — formats vary
                    m = re.search(r"(\d+):(\d+)", line)
                    if m:
                        duration = int(m.group(1)) * 60 + int(m.group(2))
                        break
                    m = re.search(r"([\d.]+)\s*s", line, re.IGNORECASE)
                    if m:
                        duration = float(m.group(1))
                        break
    except Exception:
        pass  # openmpt123 not available or failed — duration stays 0

    # Fallback title from the INNER filename for zip-virtual paths
    # ("a.zip::b.zip::song.hvl" → "song"); plain path.stem would otherwise
    # yield the mangled "a.zip::b.zip::song" string as the title.
    fallback = Path(str(path).split("::")[-1]).stem
    d: dict = {
        "id": track_id,
        "path": str(path),
        "format": fmt,
        "title": title or fallback,
        "duration": duration,
        "genre": ["Tracker", "Module"],
    }
    if instruments:
        d["instruments"] = instruments
    if channels is not None:
        d["channels"] = channels
    if patterns is not None:
        d["patterns"] = patterns
    return d


# ── UADE (exotic Amiga) extraction ────────────────────────────────────────────

_UADE123_INFO_TIMEOUT_S = 20


def _find_uade123() -> str | None:
    import shutil as _shutil
    try:
        from soniqboom.config import settings as _settings
        cand = getattr(_settings, "uade123_path", "") or ""
        if cand and Path(cand).exists():
            return cand
    except Exception:
        pass
    found = _shutil.which("uade123")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/uade123", "/usr/local/bin/uade123",
                 "/usr/bin/uade123"):
        if Path(cand).exists():
            return cand
    return None


def uade_get_info(path: Path) -> dict:
    """Probe *path* with ``uade123 -g`` (boots the emulator, ~110 ms).

    Returns the parsed key/value lines (playername, modulename, subsongs, …)
    plus ``_ok``: True iff uade accepted the file.  ``_ok`` False covers
    unknown formats, corrupt modules, MISSING COMPANION halves (TFMX without
    its ``smpl.``), and uade123 not being installed at all.  An XPK-SQSH
    packed module is probed unpacked (``xpk``); another XPK method, or
    damaged packed data, is ``_ok`` False.
    """
    import shutil as _shutil
    import subprocess as _sp
    from soniqboom.core import xpk as _xpk
    binary = _find_uade123()
    if not binary:
        return {"_ok": False, "_error": "uade123 not installed"}
    # uade refuses an XPK-packed module ("Please depack first"): probe an
    # unpacked copy (in a temp folder, with its companion halves linked).
    probe = Path(path)
    unpacked = None
    try:
        with open(probe, "rb") as fh:
            packed = _xpk.xpk_method(fh.read(12)) is not None
    except OSError:
        packed = False
    if packed:
        try:
            unpacked = probe = _xpk.write_unpacked(
                probe, _xpk.unpack_file(probe), _uade.companion_sibling_names(probe.name))
        except (_xpk.XpkError, OSError) as exc:
            return {"_ok": False, "_error": f"XPK: {exc}"}
    try:
        r = forksafe.run(
            [binary, "-g", str(probe)],
            capture_output=True, text=True, timeout=_UADE123_INFO_TIMEOUT_S,
        )
    except (_sp.TimeoutExpired, OSError) as exc:
        return {"_ok": False, "_error": str(exc)}
    finally:
        if unpacked is not None:
            _shutil.rmtree(unpacked.parent, ignore_errors=True)
    info: dict = {"_ok": r.returncode == 0}
    # uade prints "module check failed" to stderr ONLY when the input is
    # genuinely NOT an Amiga module (a PC ``.dat``/``.fc``, a Gravis ``.pat``
    # patch, a ``.jpg`` image that matched an eagleplayer token by name).  A
    # REAL module whose companion sample half is merely absent fails ``-g``
    # with "score died" instead — verified: TFMX ``mdat.X`` and RJP ``.sng``
    # sans companion → "score died"; MELLOW.DAT/MENTAL.FC/Star.jpg/bass.pat →
    # "module check failed".  Expose the distinction so the scanner's lenient
    # "remote loose module" admit (below) can refuse non-module data instead
    # of stamping it a bogus Amiga format that 422s at play (mirrors the
    # play-time guard in api/stream.py).
    _stderr = (r.stderr or "")
    info["_module_check_failed"] = "module check failed" in _stderr.lower()
    for line in r.stdout.splitlines():
        key, sep, val = line.partition(":")
        if sep:
            info[key.strip()] = val.strip()
    return info


_UADE_SUBSONGS_RE = re.compile(r"min\s+(-?\d+)\s+max\s+(-?\d+)")


def _extract_uade(path: Path, track_id: str, info: dict) -> dict:
    """Metadata for a uade-rendered Amiga module, from a prior ``-g`` probe."""
    name = Path(str(path).split("::")[-1]).name
    cls = _uade.classify(name)
    # Format: uade's own playername ("TFMX Pro", "SoundMon 2.0") is already
    # human-friendly and runtime-authoritative; fall back to the conf name.
    fmt = (info.get("playername") or "").strip()
    if not fmt and cls:
        fmt = _uade.display_name(cls[0])
    fmt = fmt or "Amiga"
    # Normalize playername variants that would split one format across two
    # library groups (suffix-.ahx files are labelled "AHX" by the tracker
    # extractor; uade's AbyssHighestExperience reports "AHX v1/v2").
    if fmt.startswith("AHX"):
        fmt = "AHX"
    # Title: embedded modulename when the format stores one, else the name
    # BODY (``mdat.acieed1`` → "acieed1"; ``legendcrack.fc`` → "legendcrack").
    title = (info.get("modulename") or "").strip()
    if not title:
        first, _, rest = name.partition(".")
        if cls and cls[1] == first.lower() and rest:
            title = rest
        else:
            title = name.rsplit(".", 1)[0]
    d: dict = {
        "id": track_id,
        "path": str(path),
        "format": fmt,
        "title": title or Path(name).stem,
        "duration": 0.0,          # render-only — backfilled from the WAV
        "genre": ["Amiga", "Module"],
    }
    m = _UADE_SUBSONGS_RE.search(info.get("subsongs", ""))
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi > lo:
            d["subsongs"] = hi - lo + 1
            # Numbering starts at ``lo`` (Hippel families report min 1): keep
            # it, so picker index N can map to replayer subsong N + lo.
            if lo:
                d["subsong_base"] = lo
    return d


# ── Atari ST extraction (SNDH / YM / SC68) ────────────────────────────────────

_ATARI_DEFAULT_DURATION = 180.0   # SNDH without TIME tags renders this long


def _find_atari_binary(setting_name: str, binary: str) -> str | None:
    import shutil as _shutil
    try:
        from soniqboom.config import settings as _settings
        cand = getattr(_settings, setting_name, "") or ""
        if cand and Path(cand).exists():
            return cand
    except Exception:
        pass
    return _shutil.which(binary)


def _extract_sndh(path: Path, track_id: str) -> dict:
    """SNDH metadata via ``psgplay -i`` (TITL/COMM/YEAR/##/TIME tags).

    Output format verified against psgplay (2026-07): lines like
    ``tag field TITL Funfares``, ``tag field ## 11``, ``tag field TIME 2 95``.
    Duration = default track's TIME tag; absent/0 → the Atari default cap
    (that's exactly how long the renderer will play it).
    """
    import subprocess as _sp
    name = Path(str(path).split("::")[-1]).stem
    title, artist, year = "", "", None
    subsongs, default_track = None, 1
    times: dict[int, int] = {}
    binary = _find_atari_binary("psgplay_path", "psgplay")
    if binary:
        try:
            r = forksafe.run([binary, "-i", str(path)], capture_output=True,
                             text=True, timeout=20)
            for line in r.stdout.splitlines():
                parts = line.split(None, 3)
                if parts[:2] != ["tag", "field"] or len(parts) < 3:
                    continue
                tag = parts[2]
                val = parts[3].strip() if len(parts) > 3 else ""
                if tag == "TITL" and val:
                    title = val
                elif tag == "COMM" and val:
                    artist = val
                elif tag == "YEAR":
                    try:
                        year = int(val[:4])
                    except ValueError:
                        pass
                elif tag == "##":
                    try:
                        subsongs = int(val)
                    except ValueError:
                        pass
                elif tag == "!#":
                    try:
                        default_track = max(1, int(val))
                    except ValueError:
                        pass
                elif tag == "TIME":
                    tv = val.split()
                    if len(tv) >= 2:
                        try:
                            times[int(tv[0])] = int(tv[1])
                        except ValueError:
                            pass
        except (_sp.TimeoutExpired, OSError):
            pass
    dur = float(times.get(default_track, 0) or 0)
    d: dict = {
        "id": track_id,
        "path": str(path),
        "format": "SNDH",
        "title": title or name,
        "duration": dur if dur > 0 else _ATARI_DEFAULT_DURATION,
        "genre": ["Chiptune", "Atari ST"],
    }
    if artist:
        d["artist"] = artist   # TrackMeta.artist is str — never None
    if year:
        d["year"] = year
    if subsongs and subsongs > 1:
        d["subsongs"] = subsongs
        start = sid_default_tune(default_track, subsongs)
        if start:
            d["start_subsong"] = start      # the default tune (``!#``), 0-based
    return d


# Raw YM magics StSound's ymDecode accepts (mirrors Ymload.cpp's enum); every
# other real YM is LHA-wrapped with an ``-lh5-`` payload.  Kept in sync with the
# play-time gate in api/stream.py, which imports ``ym_is_decodable`` from here.
_YM_RAW_MAGICS = (b"YM2!", b"YM3!", b"YM3b", b"YM4!", b"YM5!", b"YM6!",
                  b"MIX1", b"YMT1", b"YMT2")


def ym_is_decodable(path: Path) -> bool:
    """Cheap pre-flight: can StSound actually load this ``.ym``?

    StSound accepts only a raw YM magic or an ``-lh5-`` LZH-wrapped payload.
    A handful of files carry a corrupt ``-lh5-`` header (binary bytes clobber
    the LHA level-0 header, which ``lha``/``7z`` reject too) or are a foreign
    Atari format ("YMST") mislabelled ``.ym`` — the vendored depacker then
    yields garbage and ymDecode fails.

    The LHA level-0 header checksum (byte 1 == sum(header[2:2+size]) & 0xFF) is
    used as a cheap *corruption heuristic* — NOTE StSound's own ``depackFile``
    does not validate it (it gates only on ``size!=0 && id=="-lh5-"``); a file
    whose payload is intact but whose checksum byte is wrong would decode in
    StSound yet be rejected here.  Over this library that never happens: all
    files that fail the checksum are also payload-corrupt and fail ym2wav, so
    the heuristic has zero real false-rejects — but it is deliberately stricter
    than the engine.  Used to reject at play (honest 415) and to stamp
    ``defect="corrupt"`` at scan.  Returns True on I/O error (real error at play).
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return True
    if head[:4] in _YM_RAW_MAGICS:
        return True
    if head[2:7] == b"-lh5-":
        hsize = head[0]
        if hsize == 0 or 2 + hsize > len(head):
            return False
        return (sum(head[2:2 + hsize]) & 0xFF) == head[1]
    return False        # unknown magic (e.g. "YMST") — not a YM StSound reads


def _extract_ym(path: Path, track_id: str) -> dict:
    """YM metadata parsed straight from the (usually LHA-wrapped) header.

    YM5!/YM6! carry frame count + play rate (duration = frames/rate,
    verified to 0.1 s against a StSound render) plus NT-terminated
    title/author/comment strings.  Older YM2!/YM3! have no header —
    duration ≈ (size-4)/14 frames at 50 Hz.
    """
    import struct as _struct
    name = Path(str(path).split("::")[-1]).stem
    raw = b""
    try:
        import lhafile as _lha
        lf = _lha.Lhafile(str(path))
        names = lf.namelist()
        if names:
            raw = lf.read(names[0])
    except Exception:
        try:
            raw = path.read_bytes()       # uncompressed YM
        except OSError:
            raw = b""
    title, artist, comment, dur = "", "", "", 0.0
    magic = raw[:4]
    try:
        if magic in (b"YM5!", b"YM6!") and raw[4:12] == b"LeOnArD!":
            nb = _struct.unpack(">I", raw[12:16])[0]
            nd = _struct.unpack(">H", raw[20:22])[0]
            rate = _struct.unpack(">H", raw[26:28])[0] or 50
            extra = _struct.unpack(">H", raw[32:34])[0]
            dur = min(nb / float(rate), 86400.0)   # untrusted header — clamp
            off = 34 + extra
            for _ in range(nd):
                sz = _struct.unpack(">I", raw[off:off + 4])[0]
                off += 4 + sz
            def _nts(b: bytes, o: int) -> tuple[str, int]:
                e = b.find(b"\x00", o)
                if e == -1:
                    return "", o
                return b[o:e].decode("latin-1", "replace").strip(), e + 1
            title, off = _nts(raw, off)
            artist, off = _nts(raw, off)
            comment, off = _nts(raw, off)
        elif magic in (b"YM2!", b"YM3!", b"YM3b"):
            dur = max(0.0, (len(raw) - 4) / 14.0 / 50.0)
    except (_struct.error, IndexError):
        pass
    d: dict = {
        "id": track_id,
        "path": str(path),
        "format": "YM",
        "title": title or name,
        "duration": round(dur, 2),
        "genre": ["Chiptune", "Atari ST"],
    }
    if artist:
        d["artist"] = artist   # TrackMeta.artist is str — never None
    if comment:
        d["comment"] = comment
    # Flag files no engine can render (foreign "YMST", corrupt -lh5-) so the UI
    # can badge them instead of the user hitting a bare 415 at play.  Same
    # predicate as the play-time gate, so the badge appears iff play 415s.
    if not ym_is_decodable(path):
        d["defect"] = "corrupt"
        d["defect_detail"] = (
            "Not a YM register dump any available engine can decode "
            "(foreign Atari format or corrupt LHA wrapper)")
    return d


def _extract_sc68(path: Path, track_id: str) -> dict:
    """SC68 metadata: track count via ``info68 -#``; durations are embedded
    and honoured by the renderer (backfilled from the WAV on first play)."""
    import subprocess as _sp
    name = Path(str(path).split("::")[-1]).stem
    subsongs = None
    binary = _find_atari_binary("sc68_path", "sc68")
    info68 = None
    if binary:
        sib = Path(binary).with_name("info68")
        info68 = str(sib) if sib.exists() else _find_atari_binary("", "info68")
    if info68:
        try:
            r = forksafe.run([info68, str(path), "-#"], capture_output=True,
                             text=True, timeout=15)
            n = int((r.stdout or "").strip().splitlines()[-1])
            if n > 1:
                subsongs = n
        except (ValueError, IndexError, _sp.TimeoutExpired, OSError):
            pass
    d: dict = {
        "id": track_id,
        "path": str(path),
        "format": "SC68",
        "title": name,
        "duration": 0.0,          # render-only; backfilled from the WAV
        "genre": ["Chiptune", "Atari ST"],
    }
    if subsongs:
        d["subsongs"] = subsongs
    return d


# ── PSF console-music family extraction ──────────────────────────────────────

def _psf_parse_length(val: str) -> float:
    """PSF length/fade tag → seconds.  Formats: 'ss', 'ss.ddd', 'mm:ss',
    'h:mm:ss.ddd' (spec v1.4)."""
    try:
        parts = val.strip().split(":")
        secs = float(parts[-1])
        if len(parts) >= 2:
            secs += int(parts[-2]) * 60
        if len(parts) >= 3:
            secs += int(parts[-3]) * 3600
        return max(0.0, secs)
    except (ValueError, IndexError):
        return 0.0


def _extract_psf(path: Path, track_id: str) -> dict:
    """PSF-family metadata from the header version byte + the [TAG] block.

    The tag block sits at EOF after the compressed program: ``[TAG]`` then
    ``key=value`` lines (title/artist/game/year/length/fade/_lib...).
    Duration = length + fade when tagged; else 0 (backfilled from the WAV).
    """
    name = Path(str(path).split("::")[-1]).stem
    raw = b""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        _swallow_io(exc)
    version = raw[3] if len(raw) >= 4 and raw[:3] == b"PSF" else None
    fmt = _PSF_VERSION_NAMES.get(version or -1) or FORMAT_NAMES.get(
        path.suffix.lower(), "PSF")
    tags: dict[str, str] = {}
    # Per the PSF v1.4 spec the tag block sits at EOF — search only the last
    # 64 KB so '[TAG]' bytes occurring randomly inside the zlib-compressed
    # program can't fake a tag block.
    idx = raw.rfind(b"[TAG]", max(0, len(raw) - 65536))
    if idx != -1:
        for line in _psf_tag_text(raw[idx + 5:]).splitlines():
            k, sep, v = line.partition("=")
            if sep:
                tags.setdefault(k.strip().lower(), v.strip())
    dur = 0.0
    if "length" in tags:
        dur = _psf_parse_length(tags["length"])
        if dur > 0 and "fade" in tags:
            dur += _psf_parse_length(tags["fade"])
    d: dict = {
        "id": track_id,
        "path": str(path),
        "format": fmt,
        "title": tags.get("title") or name,
        "duration": round(dur, 2),
        "genre": ["Chiptune", "Game Rip"],
    }
    # TrackMeta's artist/album are plain ``str`` — a None fails validation
    # and DROPS the track (QA 2026-07-02: every untagged SSF/DSF vanished).
    if tags.get("artist"):
        d["artist"] = tags["artist"]
    if tags.get("game"):
        d["album"] = tags["game"]
        d["album_source"] = "tag"
        d["game_by_tag"] = tags["game"]        # the header's own game name
    year = tags.get("year", "")[:4]
    if year.isdigit():
        d["year"] = int(year)
    if tags.get("comment"):
        d["comment"] = tags["comment"]
    return d


def _wants_scene_md5(ext: str, name: str, d: dict) -> bool:
    """Should this file's MD5 be cached for the scene joins (Modland, the
    UADE song database)?

    Chiptune/tracker-family formats Modland hosts.  A C64 ``.sid`` is
    excluded — it already carries ``sid_md5`` for the HVSC join; an Amiga
    SidMon module named ``.sid`` (no ``sid_md5``) is not.
    """
    if ext in _SID_EXTS:
        return "sid_md5" not in d
    if (ext in _TRACKER_EXTS or ext in _UADE_SUFFIX_EXTS
            or ext in _ATARI_EXTS or ext in _GME_EXTS
            or ext in _ADLIB_EXTS or ext in _PSF_EXTS):
        return True
    return _uade.classify(name) is not None


# ── Lyrics extraction ─────────────────────────────────────────────────────────

def extract_lyrics(path: Path) -> str | None:
    """Return embedded lyrics text from an audio file, or None if not found."""
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3
            tags = ID3(path)
            # USLT = Unsynchronised Lyrics, pick first available
            for key in tags:
                if key.startswith("USLT"):
                    return str(tags[key].text).strip() or None
            return None
        elif ext == ".flac":
            audio = FLAC(path)
            for key in ("lyrics", "unsyncedlyrics", "unsynchronisedlyrics"):
                vals = audio.get(key, [])
                if vals:
                    return str(vals[0]).strip() or None
            return None
        elif ext in (".m4a", ".aac", ".mp4"):
            audio = MP4(path)
            tags = audio.tags or {}
            for key in ("\xa9lyr", "----:com.apple.iTunes:LYRICS"):
                v = tags.get(key)
                if v:
                    text = str(v[0]).strip()
                    return text or None
            return None
        elif ext in (".ogg",):
            audio = OggVorbis(path)
            for key in ("lyrics", "unsyncedlyrics"):
                vals = audio.get(key, [])
                if vals:
                    return str(vals[0]).strip() or None
            return None
        elif ext in (".opus",):
            audio = OggOpus(path)
            for key in ("lyrics", "unsyncedlyrics"):
                vals = audio.get(key, [])
                if vals:
                    return str(vals[0]).strip() or None
            return None
        elif ext in (".aiff", ".aif"):
            # The ID3 tag is a chunk of the IFF container, not the file head.
            tags = AIFF(path).tags
            for key in (tags or {}):
                if key.startswith("USLT"):
                    return str(tags[key].text).strip() or None
            return None
    except Exception:
        pass
    return None


def write_lyrics(path: Path, lyrics: str) -> bool:
    """Embed *lyrics* into a LOCAL audio file that has NO lyrics yet.

    Mirrors :func:`extract_lyrics`'s per-format tag choice — ID3 ``USLT`` for
    MP3/AIFF, the ``lyrics`` Vorbis comment for FLAC/Ogg/Opus, the ``\\xa9lyr``
    atom for MP4/M4A.  Returns True only when it actually wrote; False when the
    file is missing/remote, the format can't carry lyrics, the write failed, or
    — the key guarantee — the file ALREADY has lyrics (existing lyrics are never
    overwritten).
    """
    text = (lyrics or "").strip()
    if not text:
        return False
    try:
        if not path.is_file():
            return False
        # Never clobber lyrics that are already embedded (belt-and-suspenders:
        # the caller only reaches here on a miss, but re-check at the write).
        if extract_lyrics(path):
            return False
    except Exception:
        return False

    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3, USLT, ID3NoHeaderError
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.setall("USLT", [USLT(encoding=3, lang="eng", desc="", text=text)])
            tags.save(path)
            return True
        if ext in (".aiff", ".aif"):
            # Through the container: its ID3 chunk.  ``ID3().save(path)``
            # would prepend a tag before ``FORM`` — an unreadable file.
            from mutagen.id3 import USLT
            audio = AIFF(path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.setall("USLT", [USLT(encoding=3, lang="eng", desc="", text=text)])
            audio.save()
            return True
        if ext == ".flac":
            audio = FLAC(path)
            audio["lyrics"] = text
            audio.save()
            return True
        if ext in (".m4a", ".aac", ".mp4"):
            audio = MP4(path)
            audio["\xa9lyr"] = [text]
            audio.save()
            return True
        if ext == ".ogg":
            audio = OggVorbis(path)
            audio["lyrics"] = text
            audio.save()
            return True
        if ext == ".opus":
            audio = OggOpus(path)
            audio["lyrics"] = text
            audio.save()
            return True
    except Exception as exc:                    # never let a tag write 500 a request
        log.warning("write_lyrics failed for %s: %s", path.name, exc)
        return False
    return False                                 # format not in the write-supported set


def _extract_adlib(path: Path, track_id: str) -> dict:
    """Minimal metadata for an AdLib / OPL2 FM tune (id IMF, ROL, CMF, …).

    AdPlug renders these at play time; we don't bind libadplug at scan time, so
    the title is the filename and the duration is a sensible default — the
    rendered WAV carries the real length once the track is played.
    """
    ext = path.suffix.lower()
    return {
        "id": track_id, "path": str(path),
        "format": FORMAT_NAMES.get(ext, "AdLib"),
        "title": path.stem, "artist": "", "album": "", "album_artist": "",
        "duration": float(_ADLIB_DEFAULT_DURATION),
    }


def _extract_imf(path: Path, track_id: str) -> dict:
    """Disambiguate the overloaded ``.imf`` extension.

    Imago Orpheus modules carry an ``IM10`` signature at offset 0x3C (60) and
    are decoded by openmpt123; id Software / Apogee AdLib IMF files do not and
    are decoded by AdPlug.  Route extraction (and the displayed format label)
    accordingly.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError:
        head = b""
    if len(head) >= 64 and head[60:64] == b"IM10":
        return _extract_tracker(path, track_id)       # Imago Orpheus
    d = _extract_adlib(path, track_id)
    d["format"] = "AdLib IMF"                          # id / Apogee
    return d


# ── Public API ────────────────────────────────────────────────────────────────

def _is_io_error(exc: BaseException) -> bool:
    """Is ``exc`` (or what it wraps — mutagen re-raises an ``OSError`` as a
    ``MutagenError``) an I/O error rather than an unparsable file?  Only an
    ``OSError`` with an ``errno`` counts: mutagen reports a short read of a
    truncated file as an errno-less ``IOError``."""
    seen = 0
    e: BaseException | None = exc
    while e is not None and seen < 8:
        if isinstance(e, OSError) and e.errno is not None:
            return True
        e = e.__cause__ or e.__context__
        seen += 1
    return False


# Set for the duration of ``extract(strict_io=True)``: the helpers that read
# an unreadable file as "no tags" (``_swallow_io``) let an I/O error through.
_STRICT_IO: contextvars.ContextVar[bool] = contextvars.ContextVar("_STRICT_IO", default=False)


def _swallow_io(exc: BaseException) -> None:
    """Called by a helper that treats a file it can't read as having no tags:
    re-raises ``exc`` when it is an I/O error (``_is_io_error``) inside
    ``extract(strict_io=True)``, so it can't pass for an empty tag."""
    if _STRICT_IO.get() and _is_io_error(exc):
        raise exc


def extract(
    path: Path, track_id: str,
    *, pc_program_check: Callable[[], bool] | None = None,
    strict_io: bool = False,
) -> TrackMeta:
    """Extract full metadata from any supported audio file.

    A file that fails to parse is returned as a stub (the file name as title,
    no tags) so a scan still lists it.  ``strict_io``: an I/O error while
    reading (``_is_io_error``) raises instead — also one a format helper
    would otherwise read as "no tags" (``_swallow_io``) — for a re-extract,
    whose stub would replace stored fields.
    """
    token = _STRICT_IO.set(bool(strict_io))
    try:
        return _extract(path, track_id, pc_program_check=pc_program_check,
                        strict_io=strict_io)
    finally:
        _STRICT_IO.reset(token)


def _extract(
    path: Path, track_id: str,
    *, pc_program_check: Callable[[], bool] | None = None,
    strict_io: bool = False,
) -> TrackMeta:
    """``extract``'s body.

    ``pc_program_check`` is an optional, lazily-evaluated veto used only by the
    uade lenient fallback below: callers that materialise a module from an
    archive pass a callable that returns True when that archive is a PC program
    bundle (a DOS ``MZ`` ``.exe``/``.com`` sibling).  It is invoked at most
    once, and ONLY when a ``uade123 -g`` rejection would otherwise be
    leniently overridden — so a normal scan never pays for it.  See the
    ``QA C1`` block for why.
    """
    ext = path.suffix.lower()
    file_size = path.stat().st_size if path.exists() else None

    # PC AdLib/OPL formats (incl. id/Apogee .imf) are never Amiga executables, so
    # a file with one of these extensions whose first bytes are the AmigaDOS HUNK
    # magic (0x000003F3) is an EXTENSION-ONLY misdetection — e.g. demoscene ".SCI"
    # members of Amiga demo archives that are HUNK binaries, not Sierra AdLib tunes
    # (AdPlug reports "unknown filetype", UADE too).  Reject so the scanner records
    # an error instead of indexing an unplayable binary as music.  The raise is
    # BEFORE the catch-all below, so it propagates to _extract_one → skip.
    if ext in _ADLIB_EXTS or ext == ".imf":
        try:
            with open(path, "rb") as _fh:
                _magic = _fh.read(4)
        except OSError:
            _magic = b""
        if _magic == b"\x00\x00\x03\xf3":
            raise ValueError(
                f"Amiga HUNK executable misdetected as "
                f"{FORMAT_NAMES.get(ext, 'AdLib')} by extension; not a playable "
                f"tune: {path.name}"
            )

    # ── UADE candidates: authoritative content probe BEFORE the permissive
    # try below (mirrors the HUNK guard — the catch-all would otherwise index
    # rejected files with junk fallback metadata).  Candidates are:
    #   * suffix-form registered uade extensions  (song.fc13)
    #   * Amiga prefix-form eagleplayer names     (mdat.song → classify)
    #   * bare ``.sid`` WITHOUT PSID/RSID magic — Modland stores Amiga
    #     SidMon modules as ``*.sid``; only C64 files carry the PSID header.
    _uade_probe: dict | None = None
    _uade_cls = _uade.classify(path.name)
    # A file with a known AdLib extension is AdLib, not uade — even when its
    # NAME collides with a uade token.  AMUSIC ``star.amd`` files (Modland
    # ``Ad Lib/…``) collide with uade's ProWizard ``star`` prefix; uade's -g
    # then rejects them ("module check failed") but the lenient-index path
    # below would still stamp them "ProTracker (packed)" and they fail at
    # play.  AdPlug plays them.  (Archive members arrive here already stripped
    # to their ``.amd`` name, so this covers the ``star.amd``/``STAR.AMD.star``
    # zip forms too.)
    if ext in _ADLIB_EXTS or ext == ".imf":
        _uade_cls = None
    # A PREFIX token never overrides an extension another engine owns — the
    # playback routing rule (``stream._uade_name_routes``): ``One.mp3`` /
    # ``Two.wav`` / ``P10.mp3`` are plain audio (never probed with uade — it
    # rejected them and they were never indexed), ``ONE.IT`` / ``UFO.XM`` are
    # tracker modules.  Tracker extensions and ``.sid`` can still BE Amiga
    # modules: those are probed, and a rejection falls back to the normal
    # extractor instead of failing the file.
    _owned_prefix = False
    if (_uade_cls is not None and ext not in _UADE_SUFFIX_EXTS
            and _uade.ext_owned_elsewhere(ext)):
        if _uade.owned_ext_can_be_amiga(ext):
            _owned_prefix = True
        else:
            _uade_cls = None
    _is_uade = ext in _UADE_SUFFIX_EXTS or _uade_cls is not None
    if ext in _SID_EXTS:
        # ``.sid`` is decided by content, whatever the name: a PSID/RSID header
        # is a C64 tune (no uade boot); anything else must pass uade's strict
        # check — ``Fred.sid`` junk is not indexed as a C64 tune.
        try:
            with open(path, "rb") as _fh:
                _sid_magic = _fh.read(4)
        except OSError:
            _sid_magic = b""
        _is_c64 = _sid_magic in (b"PSID", b"RSID")
        _is_uade = not _is_c64
        _owned_prefix = False
        if _is_c64:
            _uade_cls = None
    if _is_uade:
        # Cheap binary sniff first (QA M1): Amiga modules are binary; a file
        # whose head is pure printable text (README.md next to the music,
        # docs named like tokens) is silently NOT music — no uade boot, no
        # scan-error noise.
        try:
            with open(path, "rb") as _fh:
                _head = _fh.read(256)
        except OSError:
            _head = b""
        _texty = _head and all(
            32 <= b < 127 or b in (9, 10, 13) for b in _head)
        if (_texty or not _head) and not _owned_prefix:
            raise ValueError(
                f"not an Amiga module (text or empty content): {path.name}")
        _uade_probe = uade_get_info(path) if not (_texty or not _head) else {"_ok": False}
        if not _uade_probe.get("_ok") and _owned_prefix:
            _uade_probe = None          # not Amiga after all: its own extension decides
        elif not _uade_probe.get("_ok"):
            # QA C1: remote loose modules are scanned from a LONE temp copy —
            # a companion-needing format (TFMX mdat/smpl, RJP sng/ins) always
            # fails -g here even though play-time materialization fetches the
            # sibling and works.  When the NAME classifies and the file sits
            # alone (no companion beside it), index leniently from the
            # classification; genuinely corrupt files fail at play with a
            # clear 422.  Local files WITH their companions present keep the
            # strict content verdict.
            # D1: uade's "module check failed" verdict is authoritative — the
            # file is NOT a module (PC .dat/.fc, Gravis .pat, .jpg image, etc.
            # that matched an eagleplayer token purely by name/extension).  It
            # must NEVER be admitted leniently, even when it sits alone with no
            # companion — "lone file" is the shape of BOTH a remote loose
            # module AND a mis-classified PC data file, so it can't be the
            # discriminator.  A genuine companion-needing module reports "score
            # died" instead (``_module_check_failed`` False) and stays eligible
            # for the lenient path.  This is the systemic fix for the ~540
            # non-music files mis-indexed as Paul Tonge/Paul Robotham/Zound
            # Monitor/Future Composer/ProTracker(packed)/... buckets.
            _lenient = False
            if _uade_cls is not None and not _uade_probe.get(
                    "_module_check_failed"):
                _sibs = _uade.companion_sibling_names(path.name)
                _here = {p.name.lower() for p in path.parent.glob("*")}
                if not any(s.lower() in _here for s in _sibs):
                    _lenient = True
            if not _lenient:
                raise ValueError(
                    f"uade123 rejected {path.name}: unknown/corrupt Amiga "
                    f"module or missing companion sample file"
                )
            # QA C1b: "no companion beside it" is NOT proof of a remote
            # loose module — a lone PC data file (a demo's ``X.dat`` that
            # matched PaulRobotham purely by the ``.dat`` extension) also
            # sits alone, and the lenient path above would stamp it an
            # Amiga module that 502s at play.  A DOS ``MZ`` ``.exe``/``.com``
            # sibling in the SAME archive proves the archive is a PC program
            # bundle, not an Amiga module scanned without its sample half —
            # so refuse rather than admit.  Checked here (not eagerly) so the
            # cost lands only on the rare ``-g`` rejection, never a clean scan.
            if pc_program_check is not None and pc_program_check():
                _fmt = _uade.display_name(_uade_cls[0]) if _uade_cls else "an Amiga module"
                raise ValueError(
                    f"{path.name} matched {_fmt} by extension, but its archive "
                    f"contains a DOS/PC executable (MZ header) — PC program "
                    f"data, not a playable Amiga module"
                )
            _uade_probe = {"_ok": False}   # classify-only metadata below

    try:
        if _uade_probe is not None:
            d = _extract_uade(path, track_id, _uade_probe)
        elif ext in _SID_EXTS:
            d = _extract_sid(path, track_id)
        elif ext in _MIDI_EXTS:
            d = _extract_midi(path, track_id)
        elif ext == ".imf":
            d = _extract_imf(path, track_id)       # Imago Orpheus vs AdLib IMF
        elif ext in _ADLIB_EXTS:
            d = _extract_adlib(path, track_id)
        elif ext in _TRACKER_EXTS:
            d = _extract_tracker(path, track_id)
        elif ext in _GME_EXTS:
            d = _extract_gme(path, track_id)
        elif ext in _PSF_EXTS:
            d = _extract_psf(path, track_id)
        elif ext in _DSD_EXTS:
            # ``.dsf`` collision: Sony DSD Stream File ('DSD ') vs Sega
            # Dreamcast Sound Format ('PSF' + version 0x12) share the
            # extension — content decides the pipeline.
            if ext == ".dsf":
                try:
                    with open(path, "rb") as _fh:
                        _m4 = _fh.read(4)
                except OSError as exc:
                    _swallow_io(exc)
                    _m4 = b""
                if _m4[:3] == b"PSF":
                    d = _extract_psf(path, track_id)
                else:
                    d = _extract_dsd(path, track_id)
            else:
                d = _extract_dsd(path, track_id)
        elif ext == ".sndh":
            d = _extract_sndh(path, track_id)
        elif ext == ".ym":
            d = _extract_ym(path, track_id)
        elif ext == ".sc68":
            d = _extract_sc68(path, track_id)
        elif ext == ".mp3":
            d = _mp3(path, track_id)
        elif ext == ".flac":
            d = _flac(path, track_id)
        elif ext in (".m4a", ".aac", ".mp4"):
            d = _mp4(path, track_id)
        elif ext == ".ogg":
            d = _vorbis(path, track_id, OggVorbis(path), "Ogg Vorbis")
        elif ext == ".opus":
            d = _vorbis(path, track_id, OggOpus(path), "Opus")
        elif ext in (".aiff", ".aif"):
            d = _aiff(path, track_id)
        else:
            # Generic fallback via mutagen auto-detect (easy=True gives Vorbis-like keys)
            audio = MutagenFile(path, easy=True)
            if audio is None:
                raise ValueError(f"Unsupported format: {path}")
            trck = (audio.get("tracknumber") or [None])[0]
            d = {
                "id": track_id,
                "path": str(path),
                "format": FORMAT_NAMES.get(ext, ext.lstrip(".").upper()),
                "duration": getattr(audio.info, "length", 0),
                "bitrate": getattr(audio.info, "bitrate", None),
                "channels": getattr(audio.info, "channels", None),
                "sample_rate": getattr(audio.info, "sample_rate", None),
                "title": (audio.get("title") or [path.stem])[0],
                "artist": (audio.get("artist") or [""])[0],
                "album_artist": (audio.get("albumartist") or [""])[0],
                "album": (audio.get("album") or [""])[0],
                "game": str((audio.get("game") or [""])[0] or "").strip(),
                "genre": audio.get("genre") or [],
                "year": _year((audio.get("date") or [None])[0]),
                "track_number": _int(trck),
                "total_tracks": _total(trck),
            }
    except Exception as exc:
        if strict_io and _is_io_error(exc):
            raise
        d = {"id": track_id, "path": str(path), "title": path.stem,
             "format": FORMAT_NAMES.get(ext, ""), "duration": 0.0}

    # Fill common fields
    d.setdefault("format", FORMAT_NAMES.get(ext, ""))
    d["file_size"] = file_size
    d["added_at"] = int(time.time())
    d.setdefault("embedding", [])
    # Lossless flag — derived from the format string AFTER any per-extractor
    # rewrite (e.g. _extract_dsd may rewrite "DSD" to "DSD128").  Down-
    # stream filters use this to badge the track and to drive the
    # "lossless only" smart-search filter.
    d.setdefault("is_lossless", _is_lossless_format(d.get("format")))

    # Normalise title fallback
    if not d.get("title"):
        d["title"] = path.stem

    # Scene-enrichment MD5 (Modland join key) — chiptune/tracker-family
    # files only, capped so no big PCM file is ever hashed.  Mirrors the
    # HVSC ``sid_md5`` pattern; costs one small read at scan time.
    if "file_md5" not in d and _wants_scene_md5(ext, path.name, d):
        try:
            if path.stat().st_size <= 8 * 1024 * 1024:
                import hashlib as _hashlib
                d["file_md5"] = _hashlib.md5(path.read_bytes()).hexdigest()
        except OSError:
            pass

    valid = TrackMeta.model_fields.keys()
    return TrackMeta(**{k: v for k, v in d.items() if k in valid})
