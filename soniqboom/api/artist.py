# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Artist enrichment — a short bio and a portrait, fetched on demand from
Wikipedia (keyless) and cached to disk. Surfaced in the Track Info panel."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from soniqboom.core.artistinfo import get_artist_info
from soniqboom.core.retro import is_retro_format

router = APIRouter(tags=["artist"])
log = logging.getLogger(__name__)


def _persist_scene_meta(track_id: str, wb: dict) -> None:
    """Opportunistically fill EMPTY ``composer`` / ``scene_group`` from a resolved
    title-first scene card, so the author + crew index for search.  Best-effort;
    never raises; never overwrites existing values.  Runs ON the event-loop thread
    on purpose — the store's in-memory search index is mutated here and is not
    safe to touch from a worker thread while the loop serves a concurrent read
    (mirrors ``demozoo.apply_to_library``, which keeps its store write on-loop)."""
    try:
        from soniqboom.core.store import get_store
        store = get_store()
        t = store.get_track(track_id) or {}
        upd: dict = {}
        if wb.get("composer") and not (t.get("composer") or "").strip():
            upd["composer"] = wb["composer"]
        if wb.get("scene_group") and not (t.get("scene_group") or "").strip():
            upd["scene_group"] = wb["scene_group"]
        if upd:
            store.update_track_fields(track_id, upd)
    except Exception:                       # noqa: BLE001 — enrichment, never fail the card
        log.debug("scene-meta persist failed for %s", track_id, exc_info=True)


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
    name: str | None = Query(default=None, max_length=200),
    track: str | None = Query(default=None, max_length=300),
    format: str | None = Query(default=None, max_length=80),
    track_id: str | None = Query(default=None, max_length=256),
    year: int | None = Query(default=None),
    retro: bool = Query(default=False),
):
    """Demoscene enrichment for a retro track's SCENE tab.

    Resolves the composer on Demozoo and returns their identity, discography,
    and — when this track matches one of their productions — that production's
    release details (canonical date/year, type, platform, release party,
    competition placing, links).  Only for scene music (an explicit ``retro``
    flag or a static retro ``format``); returns ``{found: False}`` otherwise so
    the panel falls back to baseline module context.

    Two resolution modes: with a ``name`` (structured artist tag) it's NAME-first
    (same confidence gate + shared-handle disambiguation as ``/artist/info``).
    With NO name — a scene module that carries only a song title — it's
    TITLE-first: match the SONG TITLE (``track``) to a Demozoo production, and
    when several sceners share that title, narrow by the ``year`` or by author
    hints derived from the module (in-module "by X" credit, the archive's artist
    directory, the title's trailing handle).  Refuses over guessing either way.
    """
    if not (retro or is_retro_format(format)):
        return {"found": False}
    from soniqboom.core import demozoo
    if name and name.strip():
        return await demozoo.scene_card(name.strip(), track_title=track)
    # Title-first — no artist tag.  Pull the module's hint signals from the store.
    if not (track and track.strip()):
        return {"found": False}
    _path = _insts = None
    if track_id:
        try:
            from soniqboom.core.store import get_store
            t = get_store().get_track(track_id) or {}
            _path, _insts = t.get("path"), t.get("instruments")
            if year is None:
                year = t.get("year")
        except Exception:                       # noqa: BLE001 — hints are best-effort
            pass
    narrow, credits = demozoo.author_hints_from_track(
        title=track, path=_path, instruments=_insts)
    card = await demozoo.scene_card_by_title(
        track, year=year, author_hints=tuple(narrow), credit_hints=credits)
    wb = card.pop("_writeback", None) if isinstance(card, dict) else None
    if wb and track_id:                     # opportunistic (a): persist author + crew
        _persist_scene_meta(track_id, wb)   # on the event-loop thread (see helper)
    return card
