# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Write metadata tags back into local audio files (mutagen, "easy" keys).

Covers the mainstream tag-bearing formats — MP3 (ID3), FLAC, Ogg Vorbis/Opus,
M4A/ALAC — via mutagen's format-agnostic easy interface.  Retro formats whose
"tags" live in bespoke binary headers (SID, tracker modules, chiptune rips)
are reported as unsupported rather than risk corrupting them.

Only LOCAL files are written.  Remote share paths (smb:// ftp:// http(s)://)
and zip-virtual members never reach mutagen — the API layer refuses them first,
and the ``Path.is_file()`` check here is the final guard.
"""
from __future__ import annotations

from pathlib import Path

# request-field → mutagen easy key
_EASY_KEYS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "album_artist": "albumartist",
    "genre": "genre",
    "year": "date",
    "track_number": "tracknumber",
    "game": "game",
}

def _register_game_key() -> None:
    """The easy-interface ``game`` key (``metadata.register_easy_game_key``,
    already run when metadata was imported)."""
    from soniqboom.core.metadata import register_easy_game_key
    register_easy_game_key()


def write_tags(path: str, updates: dict) -> dict:
    """Apply ``updates`` (request-field keyed) to the file at ``path``.

    Returns the dict of fields actually written.  Raises ``ValueError`` with a
    user-presentable message when the file is missing, the format can't carry
    tags, or nothing valid was supplied.  An empty ``game`` removes the GAME
    tag (other empty fields are skipped).
    """
    p = Path(path)
    if not p.is_file():
        raise ValueError("File is not a local file on this server.")

    from mutagen import File as MFile

    _register_game_key()
    f = MFile(str(p), easy=True)
    if f is None:
        raise ValueError("This file format does not support tag editing.")
    if f.tags is None:
        try:
            f.add_tags()
        except Exception:
            raise ValueError("This file format does not support tag editing.")

    applied: dict = {}
    for field, easy_key in _EASY_KEYS.items():
        if field not in updates or updates[field] is None:
            continue
        val = updates[field]
        if isinstance(val, str):
            val = val.strip()
            if not val and field == "game":
                # A raw ID3 chunk (AIFF / WAV) has no easy keys: nothing
                # can be removed through them, so nothing is reported.
                from mutagen.id3 import ID3
                if isinstance(f.tags, ID3):
                    continue
                try:
                    if easy_key in f:
                        del f[easy_key]
                    applied[field] = ""
                except Exception:
                    pass
                continue
            if not val:
                continue
        try:
            f[easy_key] = [str(val)]
            applied[field] = val
        except Exception:
            # An individual unsupported key (e.g. tracknumber on an odd
            # container) shouldn't abort the rest of the edit.
            continue

    if not applied:
        # Distinguish "you sent nothing" from "the container rejected every
        # key": WAV/AIFF/DSD open in mutagen but their raw-ID3 tags reject
        # the easy interface, so all assignments above fail — reporting
        # that as 'no fields supplied' blamed the user for a format limit.
        if (any(updates.get(field) not in (None, "") for field in _EASY_KEYS)
                or updates.get("game") == ""):
            raise ValueError("This file format does not support tag editing.")
        raise ValueError("No editable fields were supplied.")

    f.save()
    if "game" in applied:
        _drop_other_game_frames(p)
    return applied


def _drop_other_game_frames(p: Path) -> None:
    """An MP3's ID3 tag or an MP4's freeform atoms may already carry the game
    under another spelling of the key (ffmpeg writes ``TXXX:game``): drop
    those, so the GAME just written (or cleared) is the only one the
    extractor can find."""
    if p.suffix.lower() in (".m4a", ".mp4", ".m4b"):
        from mutagen.mp4 import MP4
        try:
            m = MP4(str(p))
        except Exception:                               # noqa: BLE001
            return
        stale = [k for k in list(m.tags or {}) if k != "----:com.apple.iTunes:GAME"
                 and k.lower() == "----:com.apple.itunes:game"]
        if stale:
            for k in stale:
                del m.tags[k]
            m.save()
        return
    if p.suffix.lower() != ".mp3":
        return
    from mutagen.id3 import ID3
    try:
        tags = ID3(str(p))
    except Exception:                                   # noqa: BLE001
        return
    stale = [k for k, fr in tags.items()
             if k.startswith("TXXX:") and fr.desc != "GAME"
             and (fr.desc or "").strip().upper() == "GAME"]
    if stale:
        for k in stale:
            del tags[k]
        tags.save(str(p))
