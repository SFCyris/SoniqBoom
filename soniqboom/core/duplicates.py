# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Duplicate track detection — identifies the same song across different formats.

Detection algorithm
───────────────────
1. Normalise each track's title + artist → lowercase, stripped, punctuation-collapsed.
2. Build a group key: normalised_title | normalised_artist | duration_bucket (5 s).
3. Tracks sharing the same group key form a *duplicate group*.
4. Within each group the track with the highest format quality score is marked
   as the *primary* (the one shown by default when grouping is active).

Format quality hierarchy (higher = better):
    FLAC / ALAC / WAV / AIFF  →  100  (lossless)
    Ogg Vorbis / Opus          →   82
    AAC                         →   78
    MP3 320+ kbps               →   75
    MP3 < 320 kbps              →   65
    MP3 (unknown bitrate)       →   60
    Everything else              →   50
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any


# ── Format quality scoring ───────────────────────────────────────────────────

# Lossless formats get the highest score
_LOSSLESS_FORMATS = {"FLAC", "ALAC", "WAV", "WAVE", "AIFF"}

# Lossy format tiers (format_upper → base score)
_LOSSY_SCORES: dict[str, int] = {
    "OGG VORBIS": 82,
    "OPUS": 82,
    "AAC": 78,
    "MP3": 65,  # adjusted upward by bitrate below
}


def format_quality_score(fmt: str, bitrate: int | None = None) -> int:
    """Return a 0–100 quality score for a track's format + bitrate.

    Higher values indicate better quality.  Used to pick the "primary"
    version when multiple format variants of the same song exist.
    """
    fmt_upper = (fmt or "").upper()

    # Lossless — always top tier
    if fmt_upper in _LOSSLESS_FORMATS:
        return 100

    # Known lossy formats — base score, with MP3 bitrate bump
    for key, base_score in _LOSSY_SCORES.items():
        if key in fmt_upper:
            if "MP3" in fmt_upper and bitrate:
                if bitrate >= 320_000:
                    return 75
                if bitrate >= 256_000:
                    return 70
                if bitrate >= 192_000:
                    return 67
            return base_score

    # Tracker / SID / MIDI / unknown — mid tier
    return 50


# ── Text normalisation ───────────────────────────────────────────────────────

_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)
_MULTI_SPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Collapse a string to a canonical form for duplicate matching.

    Steps: NFKD decomposition → lowercase → strip punctuation → collapse
    whitespace → strip leading/trailing.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = text.lower()
    text = _STRIP_RE.sub(" ", text)
    text = _MULTI_SPACE.sub(" ", text).strip()
    return text


# ── Group key generation ─────────────────────────────────────────────────────

def _duration_bucket(duration: float) -> int:
    """Round duration to a 5-second bucket for fuzzy matching.

    Songs that are the same recording but encoded in different formats
    typically differ by at most 1–2 seconds (codec padding, etc.).
    A 5-second bucket handles this reliably.
    """
    return int(duration // 5)


def _group_key(title: str, artist: str, duration: float) -> str:
    """Build a deterministic key that identifies a logical song.

    Tracks with the same group key are considered format variants of
    the same recording.
    """
    norm_title  = _normalise(title)
    norm_artist = _normalise(artist)
    bucket      = _duration_bucket(duration)
    return f"{norm_title}|{norm_artist}|{bucket}"


def _group_id(key: str) -> str:
    """Derive a short hash ID for a duplicate group (first 12 hex chars)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


# ── Main duplicate detection entry point ─────────────────────────────────────

def compute_duplicate_groups(
    tracks: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Analyse a list of tracks and return duplicate-group annotations.

    Parameters
    ----------
    tracks : list[dict]
        Each dict must contain at least: id, title, artist (or album_artist),
        duration, format, bitrate.

    Returns
    -------
    dict[str, dict]
        Mapping of track_id → {
            "duplicate_group_id":   str | None,
            "format_score":         int,
            "is_duplicate_primary": bool,
        }
        Only tracks that belong to a group of 2+ are assigned a group_id;
        unique tracks get duplicate_group_id=None and is_duplicate_primary=True.
    """
    # Step 1: group tracks by their normalised key
    groups: dict[str, list[dict]] = {}
    key_for_track: dict[str, str] = {}  # track_id → group_key

    for t in tracks:
        tid      = t.get("id", "")
        title    = t.get("title", "")
        artist   = t.get("artist") or t.get("album_artist") or ""
        duration = float(t.get("duration", 0) or 0)
        fmt      = t.get("format", "")
        bitrate  = t.get("bitrate")

        # Skip tracks with no title (untagged files)
        if not _normalise(title):
            key_for_track[tid] = ""
            continue

        key = _group_key(title, artist, duration)
        key_for_track[tid] = key
        groups.setdefault(key, []).append(t)

    # Step 2: for each group, score formats and pick primary.  Hoist the
    # ``_pick_primary`` + best-score computation out of the per-track loop
    # — they're a function of the group, not of the track — so the cost
    # collapses from O(K² log K) to O(K log K) per K-sized group.
    result: dict[str, dict[str, Any]] = {}

    group_meta: dict[str, dict[str, Any]] = {}
    for key, group in groups.items():
        if len(group) < 2 or not key:
            continue
        primary_id = _pick_primary(group).get("id", "")
        best_score = max(
            format_quality_score(g.get("format", ""), g.get("bitrate"))
            for g in group
        )
        group_meta[key] = {
            "gid": _group_id(key),
            "primary_id": primary_id,
            "best_score": best_score,
        }

    for t in tracks:
        tid     = t.get("id", "")
        fmt     = t.get("format", "")
        bitrate = t.get("bitrate")
        score   = format_quality_score(fmt, bitrate)
        key     = key_for_track.get(tid, "")

        meta = group_meta.get(key)
        if meta is None:
            # Unique track — no duplicate group
            result[tid] = {
                "duplicate_group_id": None,
                "format_score": score,
                "is_duplicate_primary": True,
            }
        else:
            result[tid] = {
                "duplicate_group_id": meta["gid"],
                "format_score": score,
                "is_duplicate_primary": (
                    score == meta["best_score"] and tid == meta["primary_id"]
                ),
            }

    return result


def _pick_primary(group: list[dict]) -> dict:
    """Among a group of duplicate tracks, pick the best one as primary.

    Tie-breaking order: highest format_score → highest bitrate → earliest added.
    """
    def _sort_key(t: dict) -> tuple:
        score   = format_quality_score(t.get("format", ""), t.get("bitrate"))
        bitrate = t.get("bitrate") or 0
        added   = t.get("added_at") or 0
        return (-score, -bitrate, added)  # negative for descending

    return sorted(group, key=_sort_key)[0]
