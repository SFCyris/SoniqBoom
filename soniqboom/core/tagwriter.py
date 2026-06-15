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
}


def write_tags(path: str, updates: dict) -> dict:
    """Apply ``updates`` (request-field keyed) to the file at ``path``.

    Returns the dict of fields actually written.  Raises ``ValueError`` with a
    user-presentable message when the file is missing, the format can't carry
    tags, or nothing valid was supplied.
    """
    p = Path(path)
    if not p.is_file():
        raise ValueError("File is not a local file on this server.")

    from mutagen import File as MFile

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
        raise ValueError("No editable fields were supplied.")

    f.save()
    return applied
