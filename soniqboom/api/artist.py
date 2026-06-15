# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Artist enrichment — a short bio and a portrait, fetched on demand from
Wikipedia (keyless) and cached to disk. Surfaced in the Track Info panel."""
from __future__ import annotations

from fastapi import APIRouter, Query

from soniqboom.core.artistinfo import get_artist_info

router = APIRouter(tags=["artist"])


@router.get("/artist/info")
async def artist_info(
    name: str = Query(..., min_length=1, max_length=200),
    album: str | None = Query(default=None, max_length=300),
    track: str | None = Query(default=None, max_length=300),
):
    """Return ``{name, found, bio, image, url, source}`` for an artist (cached).

    ``album`` / ``track`` are disambiguation context: the artist is identified
    through MusicBrainz first, so the bio is about the musician on this record.
    """
    return await get_artist_info(name, album=album, track=track)
