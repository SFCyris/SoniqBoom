# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Instant Mix — heuristic "song radio".

Builds an endless, varied queue from a seed track using genre / artist / era /
tempo / rating / format affinity, with artist-diversity guards so the mix never
stalls on one artist.  Pure in-memory over the RAM store, so it returns in tens
of milliseconds even on six-figure libraries (the caller runs it off the event
loop via ``asyncio.to_thread``).

Design notes
────────────
* No hard affinity filter.  Metadata-sparse formats (SID, tracker, chiptune
  rarely carry genre/year/bpm) would otherwise score zero and yield an empty
  mix — fatal for the formats SoniqBoom exists to serve.  Instead, *format*
  affinity keeps a SID radio playing SIDs while a FLAC radio leans on genre, and
  we always have enough candidates to fill ``limit``.
* A little jitter makes repeated mixes from the same seed feel fresh without
  destroying relevance.
* The semantic ("sounds-like") version is a separate, heavier path; this one
  needs no model and ships today.
"""
from __future__ import annotations

import random
from typing import Iterable

# Scoring weights — genre dominates, artist is secondary, the rest nudge.
_W_GENRE = 6.0
_W_ARTIST = 2.4
_W_ALBUMART = 1.4
_W_FORMAT = 1.6            # keeps chiptune/tracker radios in-family
_W_YEAR = 1.2
_W_BPM = 1.0
_W_RATING = 1.5
_W_QUALITY = 0.4
_JITTER = 0.7             # variety between successive mixes from one seed
_RECENT_PENALTY = 4.0    # strongly avoid tracks we just played

_YEAR_SPAN = 15.0        # years before era affinity decays to zero
_BPM_SPAN = 40.0         # bpm delta before tempo affinity decays to zero
_MAX_PER_ARTIST = 4      # whole-mix variety cap


def _genres(t: dict) -> set[str]:
    g = t.get("genre") or []
    if isinstance(g, str):
        g = [g]
    return {x.strip().lower() for x in g if isinstance(x, str) and x.strip()}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b) if inter else 0.0


def _norm(s) -> str:
    return s.strip().lower() if isinstance(s, str) else ""


def build_instant_mix(
    seed: dict,
    candidates: list[dict],
    *,
    ratings: dict[str, int] | None = None,
    recent_ids: Iterable[str] = (),
    limit: int = 60,
    rng: random.Random | None = None,
) -> list[dict]:
    """Return up to ``limit`` track dicts that pair well with ``seed``.

    ``candidates`` is every track in the library as dicts (the seed may be
    among them; it is skipped).  ``ratings`` maps track-id → 0–5 stars.
    ``recent_ids`` are recently-played ids to de-prioritise (avoids repeats).
    The result is ordered best-fit first, then artist-diversified.
    """
    ratings = ratings or {}
    rng = rng or random.Random()
    recent = set(recent_ids)

    sid = seed.get("id")
    s_gen = _genres(seed)
    s_art = _norm(seed.get("artist"))
    s_aart = _norm(seed.get("album_artist"))
    s_fmt = _norm(seed.get("format"))
    s_year = seed.get("year")
    s_bpm = seed.get("bpm")

    scored: list[tuple[float, dict]] = []
    for t in candidates:
        tid = t.get("id")
        if not tid or tid == sid:
            continue
        if not t.get("duration"):          # skip unplayable zero-length stubs
            continue

        score = 0.0
        if s_gen:                          # skip per-candidate set build for sparse seeds
            score += _W_GENRE * _jaccard(s_gen, _genres(t))

        art = _norm(t.get("artist"))
        aart = _norm(t.get("album_artist"))
        if s_art and art == s_art:
            score += _W_ARTIST
        if s_aart and aart == s_aart and aart != s_art:
            score += _W_ALBUMART
        if s_fmt and _norm(t.get("format")) == s_fmt:
            score += _W_FORMAT

        ty = t.get("year")
        if s_year and ty:
            score += _W_YEAR * max(0.0, 1.0 - abs(ty - s_year) / _YEAR_SPAN)
        tb = t.get("bpm")
        if s_bpm and tb:
            score += _W_BPM * max(0.0, 1.0 - abs(tb - s_bpm) / _BPM_SPAN)

        r = ratings.get(tid, 0)
        if r:
            score += _W_RATING * (r / 5.0)
        score += _W_QUALITY * ((t.get("format_score") or 0) / 100.0)

        if tid in recent:
            score -= _RECENT_PENALTY
        score += _JITTER * rng.random()

        scored.append((score, t))

    scored.sort(key=lambda x: -x[0])

    # Greedy pick with diversity: cap per-artist, never the same artist twice in
    # a row.  Adjacency-skipped tracks go to an overflow list that tops up the
    # tail if the primary pass comes up short.
    out: list[dict] = []
    per_artist: dict[str, int] = {}
    overflow: list[dict] = []
    last_art: str | None = None
    for _score, t in scored:
        if len(out) >= limit:
            break
        art = _norm(t.get("artist"))
        if art and per_artist.get(art, 0) >= _MAX_PER_ARTIST:
            continue
        if art and art == last_art:
            overflow.append(t)
            continue
        out.append(t)
        per_artist[art] = per_artist.get(art, 0) + 1
        last_art = art

    for t in overflow:
        if len(out) >= limit:
            break
        art = _norm(t.get("artist"))
        if art and per_artist.get(art, 0) >= _MAX_PER_ARTIST:
            continue
        out.append(t)
        per_artist[art] = per_artist.get(art, 0) + 1

    return out
