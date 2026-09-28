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
    # First subsong NUMBER the replayer uses when it is not 0 (uade: modules
    # that report "min 1 max 3").  The 0-based picker index N maps to replayer
    # subsong ``N + subsong_base``; None ⇒ numbering starts at 0.
    subsong_base: int | None = None
    # 0-based index of the file's DEFAULT tune — the one it plays unasked (a
    # PSID/RSID header's start song, an SNDH ``!#`` tag) — recorded only when
    # that is not tune 1; None ⇒ tune 1.  The bare track id plays this tune,
    # and the multi-tune wire mapping (renderers, Subsonic tune ids, the web
    # picker) swaps it with tune 1 (see core/subsonic_index.py ``wire_tune``).
    start_subsong: int | None = None

    # Track health — a known playback defect detected at scan time, surfaced as
    # a badge in listings + the info panel.  ``defect`` is a coarse class the UI
    # styles on; ``defect_detail`` is the human context shown as tooltip/text.
    #   "partial" — plays, but with degraded/substituted content (e.g. an Aegis
    #               Sonix .smus whose archive is missing some instruments, which
    #               the renderer fills with silent stand-ins).
    #   "corrupt" — cannot be decoded by any available engine (e.g. a foreign
    #               "YMST" file mislabelled .ym, or a corrupt LHA-wrapped .ym);
    #               plays return an honest 415.
    defect: str | None = None
    defect_detail: str | None = None

    # SID (C64) / HVSC enrichment.  ``sid_md5`` is the MD5 of the whole .sid
    # file — the key HVSC's Songlengths database is indexed by — cached at scan
    # time so the re-apply join needs no file I/O (and works for remote tracks,
    # whose bytes aren't on local disk).  ``hvsc_lengths`` is the per-subsong
    # duration list; ``stil`` the STIL commentary blob; ``sid_model`` the chip.
    sid_md5: str | None = None
    sid_model: str | None = None
    hvsc_lengths: list[float] | None = None
    stil: str | None = None

    # Scene-metadata enrichment.  MD5 of the whole module file, cached at
    # scan time for chiptune/tracker-family formats (mirrors ``sid_md5``) —
    # the key Modland's nightly ``allmods_md5`` index is joined on, so
    # author enrichment needs no file I/O and works for remote tracks.
    file_md5: str | None = None
    # The matched Modland tree path ("Future Composer 1.3/Pow/intro.smod") —
    # the module's scene provenance, shown in the track-info modal.
    scene_path: str | None = None
    # Scene group(s) the composer belonged to, from Demozoo enrichment
    # ("Anarchy • Ate Bit • Core Design").  A retro-similarity signal + shown
    # in the track-info modal.
    scene_group: str | None = None
    # Provenance of ``year`` when it did NOT come from the file's own tag:
    # "demozoo" (canonical scene release year, from the Demozoo backfill),
    # "songdb" (the UADE song database filled a MISSING year — carried by a
    # rescan only while the file is unchanged and still has no year) or
    # "user" (a deliberate hand-edit).  ``year_file`` preserves whatever the
    # file/rip originally carried so a stamp can be reverted.  These outlive a
    # rescan (see store.upsert_tracks_batch): a fresh extract re-reads the file
    # year, which for scene rips is exactly the wrong value the backfill
    # replaced — carrying the provenance forward stops a scan silently undoing
    # the correction.  None ⇒ the year is the file's own and a rescan may
    # refresh it normally.
    year_source: str | None = None
    year_file: int | None = None
    # Provenance of ``album`` for retro formats:
    #   "tag"              — the file's own header (SPC ID666 game, NSF/NSFe/GBS
    #                        name, VGM GD3 game, PSF ``game=``);
    #   "modland"          — the game/collection folder of the exact-MD5 Modland
    #                        match (``Format/Author/<Game>/file``);
    #   "modland-filename" — the ``<game>-<part>`` Modland file name, applied
    #                        only when the local title equals ``<part>``;
    #   "folder"           — the opt-in "album from folder name" pass;
    #   "songdb"           — the album (game / production) of the exact-MD5
    #                        match in the UADE song database (core/songdb.py).
    # None ⇒ an ordinary file tag (or no album).  The derived sources
    # ("modland", "modland-filename", "folder", "songdb") survive a rescan while
    # the file still carries no album (see store._carry_enrichment); a real tag
    # that appears later wins.  Apply passes fill an empty album (a "folder"
    # album may be upgraded by a Modland one, a "folder" or "modland-filename"
    # guess by a song-database one), withdraw their own album when it no
    # longer applies (the file header's name comes back —
    # ``folder_album.header_album_back``) and never touch a field listed in
    # ``user_edited``.
    album_source: str | None = None
    # The game (or production) the music belongs to — distinct from the album
    # so a modern remix can name its game too.  A retro track's game is an
    # album the user typed (``game_source`` "user-album"), else the first of
    # its per-source names below in precedence order (``game_source`` = that
    # source) — usually its album's; kept in step by ``store.game_follow``.
    # A file's own GAME tag (``game_source`` None) or a game the user typed
    # (``user_edited``) is never replaced.  ``game:`` searches it.
    game: str = ""
    game_source: str | None = None
    # The game name each source gives a retro track, written only by that
    # source: the file's header (the extractor), the Modland game folder or
    # file name, the Demozoo game a tune named after it is the soundtrack of
    # and the song database (their applies), the archive's name when
    # it is a known game of the track's platform (core/game_titles.py), the
    # folder name (the folder pass) — each cleared by its own withdrawals.  ``game`` is one of
    # them (see above); the others, distinct, are ``game_aliases``, which
    # ``game:`` searches too (``store.game_follow``).
    game_by_tag: str | None = None
    game_by_modland: str | None = None
    game_by_demozoo: str | None = None
    game_by_modland_filename: str | None = None
    game_by_songdb: str | None = None
    game_by_archive: str | None = None
    game_by_folder: str | None = None
    game_aliases: list[str] | None = None
    # Fields the UADE song database (core/songdb.py) filled that have no
    # provenance field of their own — "artist", "label" — so a rescan keeps
    # them while the file is unchanged (store._carry_enrichment), a refreshed
    # index updates or withdraws them, and its Reset clears exactly them.  (Its
    # albums and years are marked by ``album_source`` / ``year_source`` "songdb".)
    songdb_fields: list[str] | None = None
    # Field names the user hand-edited in the LIBRARY only (store-only editor
    # for formats that can't be tag-written — modules, SID, chip, archive
    # members).  Those fields are re-read from the file on a rescan and would
    # revert, so they're carried forward like the year (see
    # store._carry_enrichment).  A file-write tag edit needs no entry here — the
    # file itself holds the value.
    user_edited: list[str] | None = None

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
