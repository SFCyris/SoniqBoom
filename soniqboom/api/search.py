# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Search endpoints — full-text and filtered."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query

from soniqboom.core.data import ft_search, get_track
from soniqboom.models.track import TrackMeta

router = APIRouter(prefix="/search", tags=["search"])

_TAG_ESC_RE = re.compile(r'([,.<>{}\[\]"\'`:;!@#$%^&*()\-+=~\\|/ ])')


def _esc_tag(value: str) -> str:
    """Escape a string for use inside a tag query: @field:{value}."""
    return _TAG_ESC_RE.sub(r'\\\1', value)


def _parse_advanced_query(q: str) -> str | None:
    """Parse advanced search syntax like artist:Ghost album:Impera year:>2020.

    Returns a tag-filter query string, or None if the input is plain text.
    Supported syntax:
      artist:VALUE, album_artist:VALUE, album:VALUE, genre:VALUE,
      year:VALUE, year:>VALUE, year:<VALUE, year:VALUE-VALUE,
      format:VALUE
    Values can be quoted: artist:"The Ghost Inside"
    """
    # Check if input contains any field: prefix
    if not re.search(r'\b(artist|album_artist|album|genre|year|format):', q):
        return None

    parts: list[str] = []
    # Match field:value or field:"quoted value"
    pattern = r'(\w+):(?:"([^"]+)"|(\S+))'
    for m in re.finditer(pattern, q):
        field = m.group(1)
        value = m.group(2) or m.group(3)
        if not value:
            continue

        if field == 'artist':
            parts.append(f"@artist_tag:{{{_esc_tag(value)}}}")
        elif field == 'album_artist':
            parts.append(f"@album_artist_tag:{{{_esc_tag(value)}}}")
        elif field == 'album':
            parts.append(f"@album_tag:{{{_esc_tag(value)}}}")
        elif field == 'genre':
            parts.append(f"@genre:{{{_esc_tag(value)}}}")
        elif field == 'format':
            parts.append(f"@format:{{{_esc_tag(value.upper())}}}")
        elif field == 'year':
            if '-' in value and not value.startswith('>') and not value.startswith('<'):
                lo, hi = value.split('-', 1)
                parts.append(f"@year:[{lo.strip()} {hi.strip()}]")
            elif value.startswith('>'):
                parts.append(f"@year:[({value[1:].strip()} +inf]")
            elif value.startswith('<'):
                parts.append(f"@year:[-inf ({value[1:].strip()}]")
            else:
                parts.append(f"@year:[{value} {value}]")

    # Also capture any free text not part of field:value
    remainder = re.sub(pattern, '', q).strip()
    if remainder:
        safe = remainder.replace("-", "\\-").replace(":", "\\:").replace("/", "\\/")
        parts.append(safe)

    return " ".join(parts) if parts else None


@router.get("", response_model=list[TrackMeta])
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
):
    """Full-text search across artist, album, and title.
    Also supports advanced syntax: artist:Ghost album:Impera year:>2020
    """
    # Try advanced query first
    advanced = _parse_advanced_query(q)
    if advanced:
        return await ft_search(advanced, limit=limit)
    # Escape special characters in query
    safe = q.replace("-", "\\-").replace(":", "\\:").replace("/", "\\/")
    return await ft_search(safe, limit=limit)


@router.get("/quick", response_model=list[TrackMeta])
async def quick_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(8, ge=1, le=20),
):
    """Lightweight search for autocomplete preview — returns minimal results."""
    advanced = _parse_advanced_query(q)
    if advanced:
        return await ft_search(advanced, limit=limit)
    safe = q.replace("-", "\\-").replace(":", "\\:").replace("/", "\\/")
    return await ft_search(safe, limit=limit)


@router.get("/filter", response_model=list[TrackMeta])
async def filter_tracks(
    artist: str | None = None,
    album_artist: str | None = None,
    album: str | None = None,
    genre: str | None = None,
    format: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    """Structured filter — combine any fields.

    Uses *_tag fields (TagField with separator=\\x01) for exact case-insensitive
    matching on artist / album_artist / album.  Genre stays as TagField with
    default comma separator.
    """
    parts: list[str] = []
    if artist:
        parts.append(f"@artist_tag:{{{_esc_tag(artist)}}}")
    if album_artist:
        parts.append(f"@album_artist_tag:{{{_esc_tag(album_artist)}}}")
    if album:
        parts.append(f"@album_tag:{{{_esc_tag(album)}}}")
    if genre:
        parts.append(f"@genre:{{{_esc_tag(genre)}}}")
    if format:
        # ft_search parses @format:{value} → store.filter_tracks(format_=value),
        # which matches case-insensitively via _tag_format[value.lower()] — the
        # same index the library Galaxy chips are built from, so the value the
        # chip sends ("MIDI", "ProTracker", …) always resolves.
        parts.append(f"@format:{{{_esc_tag(format)}}}")
    if year_min is not None and year_max is not None:
        parts.append(f"@year:[{year_min} {year_max}]")
    elif year_min is not None:
        parts.append(f"@year:[{year_min} +inf]")
    elif year_max is not None:
        parts.append(f"@year:[-inf {year_max}]")
    query = " ".join(parts) if parts else "*"
    return await ft_search(query, limit=limit, offset=offset)


@router.get("/similar/{track_id}")
async def similar_tracks(track_id: str, k: int = Query(10, ge=1, le=50)):
    """Similarity search — not available in Python-only mode."""
    raise HTTPException(501, "Similarity search not available — Python-only mode has no vector index")


@router.get("/query/semantic")
async def semantic_query(
    q: str = Query(..., min_length=1),
    k: int = Query(10, ge=1, le=50),
):
    """Semantic search — not available in Python-only mode."""
    raise HTTPException(501, "Semantic search not available — Python-only mode has no vector index")
