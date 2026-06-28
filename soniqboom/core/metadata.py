# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Audio metadata extraction via mutagen + lightweight parsers.

Supported: MP3, FLAC, ALAC/M4A, AAC, Ogg Vorbis, Opus, AIFF, WAV, WavPack, Musepack,
           SID/PSID (C64), MIDI, tracker modules (MOD/S3M/XM/IT and many more).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import struct
import subprocess
import time
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from mutagen.aiff import AIFF

from soniqboom.models.track import TrackMeta

log = logging.getLogger(__name__)

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
    ".rol", ".cmf", ".d00", ".rad", ".laa", ".sci", ".dro",
    ".hsc", ".rix", ".a2m", ".adl", ".bam", ".ksm",
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
    ".ksm": "Ken's AdLib",
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
    ".hsc", ".rix", ".a2m", ".adl", ".bam", ".ksm",
}
_ADLIB_DEFAULT_DURATION = 180   # seconds; the rendered WAV carries the real length

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
#   * SPC: 256-byte header + ID666 tag at offset 0x2E; 64 KB lets us
#           read the optional extended tag block at end of file (but for
#           SPC the file IS only 64 KB).
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
    ".aiff": 512 * 1024,
    ".aif":  512 * 1024,
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

def _decode_tracker_str(b: bytes) -> str:
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
    # on any byte, but we keep the try/except for paranoia.
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


# ── MP3 (ID3) ─────────────────────────────────────────────────────────────────

def _mp3(path: Path, track_id: str) -> dict:
    audio = MP3(path)
    tags = audio.tags or {}
    trck = str(tags.get("TRCK", ""))
    tpos = str(tags.get("TPOS", ""))
    d: dict = {
        "id": track_id,
        "path": str(path),
        "format": "MP3",
        "duration": audio.info.length,
        "bitrate": audio.info.bitrate,
        "channels": audio.info.channels,
        "sample_rate": audio.info.sample_rate,
        "title": _str(tags.get("TIT2")),
        "artist": _str(tags.get("TPE1")),
        "album_artist": _str(tags.get("TPE2")),
        "album": _str(tags.get("TALB")),
        "composer": _str(tags.get("TCOM")),
        "comment": _str(next(iter(tags.getall("COMM") or []), "")),
        "label": _str(tags.get("TPUB")),
        "isrc": _str(tags.get("TSRC")),
        "bpm": _float(tags.get("TBPM")),
        "genre": _list(tags.get("TCON")),
        "year": _year(tags.get("TDRC") or tags.get("TYER")),
        "track_number": _int(trck),
        "total_tracks": _total(trck),
        "disc_number": _int(tpos),
        "total_discs": _total(tpos),
    }
    for tag in tags.values():
        if hasattr(tag, "mime") and hasattr(tag, "data") and tag.data:
            mime = tag.mime[0] if getattr(tag, "mime", None) else "image/jpeg"
            d["cover_art"] = _cover_b64(tag.data, mime)
            break
    d.update(_replaygain_from_id3(tags))
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
        return str(v[0]) if v else default

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
        probed = subprocess.run(
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
        "sample_rate": audio.info.sample_rate,
        "title": g("title", path.stem),
        "artist": g("artist"),
        "album_artist": g("albumartist") or g("album_artist"),
        "album": g("album"),
        "composer": g("composer"),
        "comment": g("comment"),
        "label": g("organization") or g("label"),
        "isrc": g("isrc"),
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

    title_raw     = _decode_tracker_str(header[22:54])
    artist_raw    = _decode_tracker_str(header[54:86])
    copyright_raw = _decode_tracker_str(header[86:118])

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

    return d


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

def _extract_gme(path: Path, track_id: str) -> dict:
    """Best-effort header read for libgme-rendered chiptune formats.

    Most of these formats have a small, well-documented header with a
    title + artist string.  We parse just enough to display in the UI;
    detailed track-list metadata (multi-song NSFs, SPC ID666) needs
    the actual gme library and is left to the renderer."""
    from soniqboom.config import settings
    ext = path.suffix.lower()
    fmt = FORMAT_NAMES.get(ext, ext.lstrip(".").upper())
    title = path.stem
    artist = ""
    duration = float(getattr(settings, "sid_default_duration", 180))
    try:
        with open(path, "rb") as f:
            hdr = f.read(256)
    except OSError:
        hdr = b""

    # NSF header (NES Sound Format) — 0x80 bytes, fields at fixed offsets.
    if ext in (".nsf", ".nsfe") and hdr[:5] == b"NESM\x1a":
        title  = _decode_tracker_str(hdr[0x0E:0x2E])
        artist = _decode_tracker_str(hdr[0x2E:0x4E])
    # SPC700 ID666 (SNES) — 0x100 byte SPC header + 0xD0-byte ID666 block.
    elif ext == ".spc" and hdr[:33] == b"SNES-SPC700 Sound File Data v0.30":
        title  = _decode_tracker_str(hdr[0x2E:0x4E])
        artist = _decode_tracker_str(hdr[0xB1:0xD1])
    # GBS (Game Boy Sound) — 0x70 byte header.
    elif ext == ".gbs" and hdr[:3] == b"GBS":
        title  = _decode_tracker_str(hdr[0x10:0x30])
        artist = _decode_tracker_str(hdr[0x30:0x50])
    # Other formats fall back to filename; gme renderer will surface
    # the proper metadata when streaming.

    d = {
        "id": track_id,
        "path": str(path),
        "format": fmt,
        "title": title or path.stem,
        "artist": artist or None,
        "duration": duration,
        "genre": ["Chiptune"],
    }
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
        out = subprocess.run(
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
            result = subprocess.run(
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
            from mutagen.id3 import ID3
            tags = ID3(path)
            for key in tags:
                if key.startswith("USLT"):
                    return str(tags[key].text).strip() or None
            return None
    except Exception:
        pass
    return None


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

def extract(path: Path, track_id: str) -> TrackMeta:
    """Extract full metadata from any supported audio file."""
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

    try:
        if ext in _SID_EXTS:
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
        elif ext in _DSD_EXTS:
            d = _extract_dsd(path, track_id)
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
            audio = AIFF(path)
            d = _mp3(path, track_id)  # AIFF uses ID3 tags
            d.update({
                "format": "AIFF",
                "duration": audio.info.length,
                "sample_rate": audio.info.sample_rate,
                "channels": audio.info.channels,
                "bit_depth": audio.info.bits_per_sample,
            })
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
                "genre": audio.get("genre") or [],
                "year": _year((audio.get("date") or [None])[0]),
                "track_number": _int(trck),
                "total_tracks": _total(trck),
            }
    except Exception:
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

    valid = TrackMeta.model_fields.keys()
    return TrackMeta(**{k: v for k, v in d.items() if k in valid})
