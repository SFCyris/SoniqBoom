# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Track data model — defines the canonical track schema."""
from __future__ import annotations

from pydantic import BaseModel, Field


class TrackMeta(BaseModel):
    """Subset returned in list/search results (no embedding)."""
    id: str
    path: str

    # Core identity
    title: str = ""
    artist: str = ""
    album_artist: str = ""      # TPE2 / albumartist
    album: str = ""
    year: int | None = None
    track_number: int | None = None
    total_tracks: int | None = None
    disc_number: int | None = None
    total_discs: int | None = None

    # Classification
    genre: list[str] = Field(default_factory=list)
    composer: str = ""
    comment: str = ""
    bpm: float | None = None
    label: str = ""             # TPUB / organization
    isrc: str = ""

    # Audio properties
    duration: float = 0.0       # seconds
    bitrate: int | None = None  # bps
    channels: int | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None  # bits per sample (lossless)
    format: str = ""            # "FLAC", "MP3", "ALAC", "Ogg Vorbis", "Opus" …

    # File info
    file_size: int | None = None  # bytes
    added_at: int = 0             # unix timestamp
    mtime: float = 0.0            # file modification time (st_mtime)

    # Directory references (populated by scanner)
    dir_hash: str = ""            # sha256[:16] of parent directory — TAG indexed
    scan_root_hash: str = ""      # sha256[:16] of the scan root — TAG indexed

    # Extended metadata (tracker/SID/MIDI)
    instruments: list[str] | None = None
    patterns: int | None = None
    subsongs: int | None = None

    # Art
    cover_art: str | None = None  # data-URI thumbnail

    # ReplayGain / loudness normalisation (read from tags during scan).
    # All values are in dB except peak which is normalised 0..1 (or larger
    # if true-peak inter-sample peaks were detected).  Player.js applies
    # these via a GainNode in the Web Audio graph so a mixed-mastering
    # library plays at consistent perceived loudness without the user
    # reaching for the volume knob between tracks.
    replaygain_track_gain: float | None = None   # dB
    replaygain_album_gain: float | None = None   # dB
    replaygain_track_peak: float | None = None   # 0..1+ (true-peak allowed)
    replaygain_album_peak: float | None = None
    # Codec lossiness — derived from format at ingest time.  Used by the
    # library UI to surface a lossless/lossy badge and by future "lossless
    # only" filters.  Pre-computed so the read path is O(1) instead of
    # mapping format → bool on every query.
    is_lossless: bool | None = None

    # Duplicate detection (populated by post-scan analysis or manual recompute)
    duplicate_group_id: str | None = None    # hash of normalised title|artist|duration
    format_score: int = 0                     # 0–100 quality score for format+bitrate
    is_duplicate_primary: bool = True          # best-quality version in its group


class Track(TrackMeta):
    """Full track document (includes embedding vector field)."""
    embedding: list[float] = Field(default_factory=list)
