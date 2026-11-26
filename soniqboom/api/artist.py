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


async def _composer_of(name: str) -> dict | None:
    """``{name, count}`` of the scene musician ``name`` is a spelling of, or
    None (``core/composers``)."""
    try:
        from soniqboom.core.store import get_store
        store = get_store()
        idx = await store.composer_index_async(cold=False)
        if idx is None:
            return None                     # (none built yet: Track Info doesn't wait)
        ident = idx.find(name)
        if ident is None:
            return None
        return {"name": ident.name, "count": _counts(store).get(ident.name, 0)}
    except Exception:                       # noqa: BLE001
        log.debug("composer lookup failed for %r", name, exc_info=True)
        return None


def _counts(store) -> dict:
    """``{name: count}`` of the Scene → Composers rows (the counts the list
    and the page show — duplicates hidden, drives offline, as set)."""
    from soniqboom.core import availability
    hide = bool(store.get_config("filter_duplicates", False))
    rows = store.aggregate_facet("composer", primary_only=hide, exclude=availability.hidden(store),
                                 swap=availability.primary_swap(store))
    return {r["composer"]: r["count"] for r in rows}


_HVSC_DIR_RE = None
_HVSC_FOLDER_URL = "https://6581.nl/C64Music/"
_MODLAND_URL = "https://ftp.modland.com/pub/modules/"


def _folder_links(store, ids) -> list[dict]:
    """HVSC and Modland folder links of a musician's tunes — the folders
    most of them are in (``scene_path`` for Modland, the ``MUSICIANS``
    folder of an HVSC path)."""
    import re
    from collections import Counter
    from urllib.parse import quote
    global _HVSC_DIR_RE
    if _HVSC_DIR_RE is None:
        _HVSC_DIR_RE = re.compile(r"(?:^|[/:])(MUSICIANS/[^/:]+/[^/:]+)/[^/]+$")
    from soniqboom.core import scene_metadata as sm
    hv: Counter = Counter()
    ml: Counter = Counter()
    tracks = store._tracks
    for i in ids:
        t = tracks.get(i) or {}
        p = t.get("path") or ""
        if (t.get("format") or "") == "SID" or p.endswith((".sid", ".SID")):
            m = _HVSC_DIR_RE.search(p.replace("\\", "/"))
            if m:
                hv[m.group(1)] += 1
        sp = t.get("scene_path")
        if isinstance(sp, str) and sp and not sp.startswith("HVSC/"):
            mp = sm.parse_modland_path(sp)
            if mp.author and mp.fmt_dirs:
                ml[(*mp.fmt_dirs, mp.author)] += 1
    out = []
    for rel, _n in hv.most_common(2):
        out.append({"label": "HVSC", "url": _HVSC_FOLDER_URL + quote(rel) + "/",
                    "title": "/" + rel})
    for parts, _n in ml.most_common(4):
        out.append({"label": f"Modland · {parts[0]}",
                    "url": _MODLAND_URL + "/".join(quote(x) for x in parts) + "/",
                    "title": "/".join(parts)})
    return out


@router.get("/artist/composer")
async def composer_page(name: str = Query(..., min_length=1, max_length=300)):
    """A scene musician's page (``core/composers``): ``name`` any spelling
    of the name, a handle, a real name or the Scene → Composers key.

    ``{found, name, real_name, handles, aliases, groups, location, notes,
    demozoo_url, releaser_id, links, results, results_total, count, scene,
    platforms, format_platform}`` — ``handles``: the Demozoo nicks;
    ``aliases``: the other spellings the library credits him by (and the
    Demozoo nick variants); ``results``: his music's competition placings
    (Demozoo index, newest first, at most 60); ``platforms``: his tunes in
    the library by platform (``composers.platform_summary``: count, formats,
    years) as the page lists them (``/search/filter?composer=…&order=
    platform``), ``format_platform`` each format's group.  The profile's
    notes, photo and discography come from ``/artist/info`` and
    ``/artist/scene`` (Track Info's).  ``{found: false}`` for a name no scene
    tune credits."""
    import asyncio

    from soniqboom.core import availability, composers, demozoo
    from soniqboom.core.store import get_store
    store = get_store()
    await store.search_indexes_ready()
    ident = (await store.composer_index_async()).find(name.strip())
    if ident is None:
        return {"found": False, "name": name.strip()}
    hide = bool(store.get_config("filter_duplicates", False))
    excl = availability.hidden(store)
    swap = availability.primary_swap(store)
    ids = [i for i in store.filter_track_ids(filter_duplicates=hide, swap=swap,
                                             composer_is=ident.key)
           if not (excl and i in excl)]
    platforms = composers.platform_summary(store, ids)
    fmts = {(store._tracks.get(i) or {}).get("format") or "" for i in ids}
    details = placings = None
    if ident.releaser_id is not None:
        details, placings = await asyncio.gather(
            asyncio.to_thread(demozoo.scener_details, ident.releaser_id),
            asyncio.to_thread(demozoo.scener_placings, ident.releaser_id))
    details = details or {}
    handles = [h for h in (details.get("aliases") or []) if h]
    seen = {composers.ordered_keys(x)[0] for x in (ident.name, ident.real_name, *handles)
            if x and composers.ordered_keys(x)}
    aliases = []
    for a in (*ident.aliases, *(details.get("variants") or [])):
        k = composers.ordered_keys(a)
        if a and k and k[0] not in seen:
            seen.add(k[0])
            aliases.append(a.replace("_", " ") if "_" in a and " " not in a.strip("_") else a)
    groups = list(details.get("groups") or [])
    if not groups:
        from collections import Counter
        c: Counter = Counter()
        for i in ids:
            for g in ((store._tracks.get(i) or {}).get("scene_group") or "").split(" \u2022 "):
                if g.strip():
                    c[g.strip()] += 1
        groups = [g for g, _n in c.most_common(8)]
    links = []
    if ident.releaser_id is not None:
        links.append({"label": "Demozoo", "url": f"https://demozoo.org/sceners/{ident.releaser_id}/"})
    links += [{"label": lk.get("label") or "", "url": lk.get("url") or ""}
              for lk in (details.get("links") or []) if lk.get("url")]
    links += _folder_links(store, ids)
    placings = placings or []
    return {"found": True, "name": ident.name, "key": ident.key,
            "real_name": ident.real_name or details.get("real_name") or "",
            "handles": handles, "aliases": aliases, "groups": groups,
            "location": details.get("location") or "", "releaser_id": ident.releaser_id,
            "demozoo_url": details.get("demozoo_url") or "",
            "links": links, "results": placings[:60], "results_total": len(placings),
            "count": len(ids),
            "scene": sum(1 for i in ids if composers.is_scene_track(store._tracks.get(i))),
            "platforms": platforms,
            "format_platform": {f: composers.platform_of(f) for f in fmts if f}}


@router.get("/artist/composers")
async def composer_search(q: str = Query(..., min_length=1, max_length=200),
                          limit: int = Query(6, ge=1, le=20)):
    """The scene musicians a search names — every word of ``q`` starts a
    word of one of their names (any spelling, handle or real name):
    ``[{name, real_name, count, also}]`` most tunes first (``also``: the name
    ``q`` matched when it is not the one shown).  The search view shows them
    above its results; each opens the composer page."""
    from soniqboom.core import composers
    from soniqboom.core.store import get_store
    store = get_store()
    if store._sx_todo is not None:
        return []                           # (a start still builds what it reads)
    idx = await store.composer_index_async()
    counts = _counts(store)
    qs = composers.spellings(q)
    out = []
    for ident in idx.search(q, limit=limit * 2):
        n = counts.get(ident.name, 0)
        if not n:
            continue                        # (none of his tunes listed in this mode)
        also = ""
        if not composers._words_start(ident.name, qs):
            also = next((nm for nm in ident.names if composers._words_start(nm, qs)), "")
        out.append({"name": ident.name, "real_name": ident.real_name, "count": n,
                    "also": also})
    return out[:limit]


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
        card = await demozoo.scene_card(name.strip(), track_title=track)
        ident = await _composer_of(name.strip())
        if ident is not None:
            # (the composer page of the musician — any spelling — when the
            # library has one: Track Info links it)
            card = {**card, "composer": ident}
        return card
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


@router.get("/scene/csdb")
async def scene_csdb(
    track_id: str = Query(..., min_length=1, max_length=256),
    sid: int | None = Query(default=None, ge=1, le=9_999_999),
):
    """CSDb data for a SID's Track Info (core/csdb.py): the releases that use
    the tune, the best-rated one's screenshot, ratings and csdb.dk links —
    ``{"found": False, "reason": …}`` when the lookup is off, the track is no
    SID, its CSDb id is not known or CSDb's answer is not confidently the
    same tune.  ``sid``: the CSDb SID id of the Demozoo production the Scene
    tab matched (used only when the Demozoo index names none for the tune's
    HVSC path, and checked against CSDb's answer like any other).  Asks CSDb
    at most once per tune not known yet; Track Info calls it on open only."""
    from soniqboom.core import csdb
    from soniqboom.core.data import get_track
    track = await get_track(track_id)
    if track is None:
        return {"found": False, "reason": "not_sid"}
    return await csdb.info_for(track, sid)


@router.get("/scene/csdb/shot/{release_id}.png")
async def scene_csdb_shot(release_id: int):
    """The screenshot of a CSDb release that a tune shown in Track Info
    lists (re-encoded as PNG, kept in the art cache); 404 otherwise."""
    from fastapi import HTTPException
    from fastapi.responses import Response
    from soniqboom.core import csdb
    if not 0 < release_id < 100_000_000:
        raise HTTPException(404, "Unknown release")
    data = await csdb.screenshot(release_id)
    if not data:
        raise HTTPException(404, "No screenshot")
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})
