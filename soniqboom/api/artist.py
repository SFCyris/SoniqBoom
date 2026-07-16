# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Artist enrichment — a short bio and a portrait, fetched on demand from
Wikipedia (keyless) and cached to disk. Surfaced in the Track Info panel."""
from __future__ import annotations

from fastapi import APIRouter, Query

from soniqboom.core.artistinfo import get_artist_info
from soniqboom.core.retro import is_retro_format

router = APIRouter(tags=["artist"])


@router.get("/artist/info")
async def artist_info(
    name: str = Query(..., min_length=1, max_length=200),
    album: str | None = Query(default=None, max_length=300),
    track: str | None = Query(default=None, max_length=300),
    format: str | None = Query(default=None, max_length=80),
    retro: bool = Query(default=False),
):
    """Return ``{name, found, bio, image, url, source}`` for an artist (cached).

    ``album`` / ``track`` are disambiguation context.  The artist is identified
    **Demozoo-first** (a scene handle resolves to the demoscene musician, not
    the mainstream band a MusicBrainz search would return) when the track is
    scene music — signalled EITHER by a static retro ``format`` (tracker / SID /
    chip) OR by an explicit ``retro`` flag.  The flag exists because the whole
    uade Amiga-exotica family (TFMX, Hippel, Hubbard, Whittaker, ProWizard …)
    is stored under uade's dynamic playernames, which aren't in the static
    format set — yet those classic composers are exactly the ones whose handles
    collide with mainstream MusicBrainz entities.  The frontend sets ``retro``
    from its own scene detection (``isUadeAmigaTrack`` + module/Atari/PSF sets).
    Otherwise identification stays MusicBrainz-first.
    """
    return await get_artist_info(
        name, album=album, track=track,
        is_retro=(retro or is_retro_format(format)))


@router.get("/artist/scene")
async def artist_scene(
    name: str = Query(..., min_length=1, max_length=200),
    track: str | None = Query(default=None, max_length=300),
    format: str | None = Query(default=None, max_length=80),
    retro: bool = Query(default=False),
):
    """Demoscene enrichment for a retro track's SCENE tab.

    Resolves the composer on Demozoo (same confidence gate + shared-handle
    disambiguation as ``/artist/info``) and returns their identity, discography,
    and — when this track matches one of their productions — that production's
    release details (canonical date/year, type, platform, release party,
    competition placing, links).  Only for scene music (an explicit ``retro``
    flag or a static retro ``format``); returns ``{found: False}`` otherwise so
    the panel falls back to baseline module context.
    """
    if not (retro or is_retro_format(format)):
        return {"found": False}
    from soniqboom.core import demozoo
    return await demozoo.scene_card(name, track_title=track)
