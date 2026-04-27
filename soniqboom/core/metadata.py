# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Audio metadata extraction via mutagen + lightweight parsers.

Supported: MP3, FLAC, ALAC/M4A, AAC, Ogg Vorbis, Opus, AIFF, WAV, WavPack, Musepack,
           SID/PSID (C64), MIDI, tracker modules (MOD/S3M/XM/IT and many more).
"""
from __future__ import annotations

import base64
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
}

FORMAT_NAMES = {
    ".mp3": "MP3", ".flac": "FLAC", ".m4a": "ALAC/AAC", ".aac": "AAC",
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
}

_SID_EXTS = {".sid", ".psid"}
_MIDI_EXTS = {".mid", ".midi"}
_TRACKER_EXTS = {
    ".mod", ".s3m", ".xm", ".it", ".mtm", ".med", ".oct",
    ".669", ".dbm", ".ahx", ".hvl", ".ult", ".stm", ".far",
    ".amf", ".gdm", ".imf", ".okt", ".sfx", ".wow", ".dsm",
}

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

# ── Helpers ───────────────────────────────────────────────────────────────────

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
        return float(str(v).strip())
    except Exception:
        return default


def _cover_b64(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


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
    d: dict = {
        "id": track_id,
        "path": str(path),
        "format": "FLAC",
        "duration": audio.info.length,
        "bitrate": audio.info.bits_per_sample * audio.info.sample_rate,
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
    return d


# ── ALAC / AAC / M4A (MP4 container) ─────────────────────────────────────────

def _mp4(path: Path, track_id: str) -> dict:
    audio = MP4(path)
    tags = audio.tags or {}

    def g(key, default=""):
        v = tags.get(key, [default])
        return str(v[0]) if v else default

    trkn = tags.get("trkn", [(None, None)])[0] or (None, None)
    disk = tags.get("disk", [(None, None)])[0] or (None, None)

    # Detect ALAC vs AAC
    fmt = "ALAC" if getattr(audio.info, "codec", "").startswith("alac") else "AAC/M4A"

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
    return {
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


# ── SID (C64) ────────────────────────────────────────────────────────────────

def _extract_sid(path: Path, track_id: str) -> dict:
    """Parse PSID/RSID binary header to extract SID metadata."""
    from soniqboom.config import settings

    with open(path, "rb") as f:
        header = f.read(124)

    if len(header) < 118:
        return {
            "id": track_id, "path": str(path), "title": path.stem,
            "format": "SID", "duration": float(settings.sid_default_duration),
            "genre": ["Chiptune", "C64"],
        }

    magic = header[0:4]
    if magic not in (b"PSID", b"RSID"):
        return {
            "id": track_id, "path": str(path), "title": path.stem,
            "format": "SID", "duration": float(settings.sid_default_duration),
            "genre": ["Chiptune", "C64"],
        }

    version = struct.unpack(">H", header[4:6])[0]

    title_raw     = header[22:54].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
    artist_raw    = header[54:86].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
    copyright_raw = header[86:118].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()

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
    }
    if sid_model:
        d["sid_model"] = sid_model
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
            title = raw[0:20].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()

            # Channel count from magic bytes at offset 1080
            magic = raw[1080:1084]
            channels = _MOD_MAGIC_CHANNELS.get(magic, 4)

            # 31 sample headers at bytes 20-949 (each 30 bytes)
            for i in range(31):
                offset = 20 + i * 30
                if offset + 30 > len(raw):
                    break
                try:
                    name = raw[offset:offset + 22].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
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
            title = raw[0:28].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()

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
                        name = raw[ptr + 48:ptr + 76].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
                        if name and name.isprintable():
                            instruments.append(name)
                except Exception:
                    pass

        elif ext == ".xm" and len(raw) >= 80:
            # XM starts with "Extended Module: " (17 bytes), then 20-byte title
            if raw[0:17] == b"Extended Module: ":
                title = raw[17:37].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()

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
                    name = raw[inst_offset + 4:inst_offset + 26].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
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
                title = raw[4:30].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()

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
                            name = raw[ptr + 4:ptr + 30].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
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
                                name = raw[ptr + 4:ptr + 30].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
                                if name and name.isprintable():
                                    instruments.append(name)
                        except Exception:
                            pass

        else:
            # Other tracker formats — try reading first 20 bytes as title
            if len(raw) >= 20:
                candidate = raw[0:20].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
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

    d: dict = {
        "id": track_id,
        "path": str(path),
        "format": fmt,
        "title": title or path.stem,
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


# ── Public API ────────────────────────────────────────────────────────────────

def extract(path: Path, track_id: str) -> TrackMeta:
    """Extract full metadata from any supported audio file."""
    ext = path.suffix.lower()
    file_size = path.stat().st_size if path.exists() else None

    try:
        if ext in _SID_EXTS:
            d = _extract_sid(path, track_id)
        elif ext in _MIDI_EXTS:
            d = _extract_midi(path, track_id)
        elif ext in _TRACKER_EXTS:
            d = _extract_tracker(path, track_id)
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

    # Normalise title fallback
    if not d.get("title"):
        d["title"] = path.stem

    valid = TrackMeta.model_fields.keys()
    return TrackMeta(**{k: v for k, v in d.items() if k in valid})
