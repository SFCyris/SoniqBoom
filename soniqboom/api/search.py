# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Search endpoints — full-text and filtered."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query, Response

from soniqboom.api.tracks import json_bytes, json_route, public_track, public_tracks
from soniqboom.core.data import ft_search, ft_search_dicts, get_track
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
      artist:VALUE, album_artist:VALUE, album:VALUE (exact album),
      game:VALUE (case-insensitive PREFIX of the track's game or one of its
      other names — ``TrackMeta.game`` / ``game_aliases`` — or, for a C64
      SID / Atari ST tune whose game is unknown or only guessed from its
      folder / a Modland file name (not typed or cleared by the user), of its
      title, where HVSC rips name the
      game: ``game:uridium`` finds
      "Uridium 2"; a leading "The", accents and runs of whitespace are
      ignored, so ``game:"last ninja"`` also finds "The Last Ninja" and
      ``game:pokemon`` finds "Pokémon"),
      genre:VALUE,
      year:VALUE, year:>VALUE, year:<VALUE, year:VALUE-VALUE,
      format:VALUE
    Values can be quoted: artist:"The Ghost Inside"
    """
    # Check if input contains any field: prefix
    if not re.search(r'\b(artist|album_artist|album|game|genre|year|format):', q):
        return None

    parts: list[str] = []
    # Match field:value or field:"quoted value"
    pattern = r'(\w+):(?:"([^"]+)"|(\S+))'
    for m in re.finditer(pattern, q):
        field = m.group(1)
        value = m.group(2) or m.group(3)
        if not value:
            continue

        # artist: / album_artist: match a word start in the name ("Marcin"
        # finds "Marcin Romanowski (Sidder)"); an artist's page stays exact.
        if field == 'artist':
            parts.append(f"@artist_match:{{{_esc_tag(value)}}}")
        elif field == 'album_artist':
            parts.append(f"@album_artist_match:{{{_esc_tag(value)}}}")
        elif field == 'album':
            parts.append(f"@album_tag:{{{_esc_tag(value)}}}")
        elif field == 'game':
            parts.append(f"@game_tag:{{{_esc_tag(value)}}}")
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


async def run_search(q: str, limit: int = 50) -> list[TrackMeta]:
    """Evaluate a query (advanced field-operators or plain text) → tracks.

    Shared by the ``/search`` endpoint and smart-playlist evaluation so the two
    can never drift in how they interpret ``artist:…  year:>2020  format:SID``.
    """
    advanced = _parse_advanced_query(q)
    if advanced:
        return await ft_search(advanced, limit=limit)
    safe = q.replace("-", "\\-").replace(":", "\\:").replace("/", "\\/")
    return await ft_search(safe, limit=limit)


async def run_search_dicts(q: str, limit: int = 50, exclude=None, swap=None) -> list[dict]:
    """Dict-returning twin of :func:`run_search` for the ``/search`` endpoint —
    serializes with orjson and skips the TrackMeta round-trip (see
    ``data.ft_search_dicts``).  ``run_search`` (models) stays for smart-playlist
    evaluation so the two never drift on query parsing.  ``exclude``: ids the
    result leaves out (``core.availability.hidden``); ``swap``: the duplicate
    groups listed by another member (``core.availability.primary_swap``)."""
    advanced = _parse_advanced_query(q)
    if advanced:
        return await ft_search_dicts(advanced, limit=limit, exclude=exclude, swap=swap)
    safe = q.replace("-", "\\-").replace(":", "\\:").replace("/", "\\/")
    return await ft_search_dicts(safe, limit=limit, exclude=exclude, swap=swap)


@router.get("")
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
):
    """Full-text search across artist, album, and title.
    Also supports advanced syntax: artist:Ghost album:Impera year:>2020

    Tracks that can't play right now are flagged (``offline``) or, with
    "Tracks on unavailable drives: Hide", left out (``core.availability``);
    with duplicates hidden, a group whose primary can't play is listed by its
    best playable member.
    """
    from soniqboom.core import availability
    dicts = await run_search_dicts(q, limit=limit, exclude=availability.hidden(),
                                   swap=availability.primary_swap())
    dicts = availability.mark_rows(dicts)
    return Response(content=json_bytes(dicts), media_type="application/json")


@json_route(router, "/quick")
async def quick_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(8, ge=1, le=20),
):
    """Lightweight search for autocomplete preview — TrackMeta-shaped rows
    (``tracks.public_track``), offline ones flagged or hidden as in ``/search``."""
    from soniqboom.core import availability
    rows = public_tracks(await run_search_dicts(q, limit=limit,
                                                exclude=availability.hidden(),
                                                swap=availability.primary_swap()))
    return availability.mark_rows(rows)


@json_route(router, "/filter")
async def filter_tracks(
    artist: str | None = None,
    album_artist: str | None = None,
    album: str | None = None,
    genre: str | None = None,
    scene_group: str | None = None,
    format: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    playable: bool = False,
    after: str | None = None,
):
    """Structured filter — combine any fields.

    Uses *_tag fields (TagField with separator=\\x01) for exact case-insensitive
    matching on artist / album_artist / album.  Genre stays as TagField with
    default comma separator.

    Rows are TrackMeta-shaped (``tracks.public_track``) and encoded with orjson
    rather than validated against a ``list[TrackMeta]`` response model — the
    same JSON, minus the per-row model build.  A stored row the model rejects
    is skipped (the full-text branch always did; the ``scene_group`` branch
    used to 500 the whole drill-down on one).

    Tracks that can't play right now are flagged (``offline``) or, with
    "Tracks on unavailable drives: Hide", left out — the browse counts agree.
    ``playable=1`` (the play queue's pages): ``{tracks, next_offset, ended}``
    without those tracks in Mark / Hide mode, each row with its ``_pos``;
    ``after=<track id>`` starts right after that track (``player_page``).
    """
    from soniqboom.core import availability
    from soniqboom.core.store import get_store
    store = get_store()
    excl = availability.hidden(store)
    # (Duplicates hidden: a group whose primary can't play is listed by its
    # best playable member — the browse counts agree.)
    swap = availability.primary_swap(store)

    async def _reply(fetch):
        if playable:
            page = await availability.player_page_async(fetch, offset, limit, store, after=after)
            out = []
            for r in page["tracks"]:
                t = public_track(r)                 # (drops non-TrackMeta keys:
                if t is not None:                   #  ``_pos`` is put back)
                    out.append({**t, "_pos": r["_pos"]})
            page["tracks"] = out
            return page
        return availability.mark_rows(public_tracks(fetch(offset, limit)), store)

    # ``scene_group`` (the Demozoo browse facet) is NOT a full-text field, so
    # resolve it — and any co-filters — straight against the store's maintained
    # ``_tag_scene_group`` index rather than the FT engine.
    if scene_group:
        return await _reply(lambda off, lim: store.filter_tracks(
            artist=artist, album_artist=album_artist, album=album, genre=genre,
            scene_group=scene_group, format_=format,
            year_min=year_min, year_max=year_max, limit=lim, offset=off,
            # Honour the "hide duplicates" config so the drill-down LIST matches
            # the deduped browse COUNT (every FT-routed facet already does this).
            filter_duplicates=bool(store.get_config("filter_duplicates", False)),
            exclude=excl, swap=swap))
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
    from soniqboom.core.data import _parse_tag_query
    parsed = _parse_tag_query(query)
    hide_dups = bool(store.get_config("filter_duplicates", False))
    return await _reply(lambda off, lim: store.filter_tracks(
        **parsed, limit=lim, offset=off, filter_duplicates=hide_dups, exclude=excl,
        swap=swap))


@router.get("/similar/{track_id}")
async def similar_tracks(track_id: str, k: int = Query(10, ge=1, le=50)):
    """"Sounds-like" tracks for a seed.

    Heuristic affinity (genre / artist / era / tempo / format) blended with a
    loudness-contour cosine where waveform data exists for both tracks (it is
    captured whenever a track plays, so audio-aware coverage grows with use).

    Response: ``[{track, score}]`` where ``score`` is a 0–1 float RELATIVE to
    the top hit of this result set (the UI renders it as a percentage).
    """
    import asyncio as _asyncio

    from soniqboom.core.data import get_all_ratings
    from soniqboom.core.similar import find_similar
    from soniqboom.core.store import get_store

    store = get_store()
    seed = store.get_track(track_id)
    if not seed:
        raise HTTPException(404, "Track not found")

    ratings = await get_all_ratings()
    # Build the candidate pool on the loop (synchronous, no await → atomic
    # w.r.t. the scanner; cheaper than the old all_tracks() copy), then score
    # off-thread.  waveforms_view() is a snapshot, also safe to hand to the thread.
    candidates = store.similar_candidates(seed)
    # Suggestions are something to play: leave out what can't play right now
    # ("Tracks on unavailable drives": Mark / Hide).
    from soniqboom.core import availability
    skip = availability.unplayable(store)
    if skip:
        candidates = [c for c in candidates if c.get("id") not in skip]
    sample_jaccard = store.retro_sample_jaccard(seed, candidates)   # cheap inverted-index walk
    result = await _asyncio.to_thread(
        find_similar, seed, candidates, store.waveforms_view(),
        ratings=ratings or {}, k=k,
        sample_jaccard=sample_jaccard,
    )

    def _clean(t: dict) -> dict:
        d = dict(t)
        d.pop("embedding", None)
        return d

    return [{"track": _clean(r["track"]), "score": r["score"]} for r in result]


@router.get("/query/semantic")
async def semantic_query(
    q: str = Query(..., min_length=1),
    k: int = Query(10, ge=1, le=50),
):
    """Semantic search — not available in Python-only mode."""
    raise HTTPException(501, "Semantic search not available — Python-only mode has no vector index")
