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

from soniqboom.core.retro import chip_family, instrument_tokens, is_retro_format

# Scoring weights — genre dominates, artist is secondary, the rest nudge.
_W_GENRE = 6.0
_W_ARTIST = 2.4
_W_ALBUMART = 1.4
_W_FORMAT = 1.6            # keeps chiptune/tracker radios in-family
_W_YEAR = 1.2
_W_BPM = 1.0
_W_RATING = 1.5
_W_QUALITY = 0.4

# Retro (chip/tracker) weights — genre/year/bpm are useless for this music;
# perceived similarity is composer + sound-chip + replayer + sample-set lineage
# (see core/retro.py and the archive taxonomies: Modland=format→author,
# Demozoo/AMP=composer+group+platform).
_W_R_COMPOSER = 3.4       # same artist == same scene composer (~81% coverage)
_W_R_CHIPFAM  = 3.0       # same sound-chip family (SID ≠ Paula ≠ 2A03 ≠ SPC …)
_W_R_FORMAT   = 1.6       # exact same replayer/format (atop chip family)
_W_R_SAMPLES  = 4.0       # instrument/sample-name Jaccard (lineage), scaled 0..1
_W_R_SIDMODEL = 0.6       # 6581 vs 8580 (SID only)
_W_R_CHANNELS = 0.5       # channel-count proximity
_W_R_GROUP    = 1.8       # shared scene group (Demozoo enrichment; dormant until applied)
_R_CHANNEL_SPAN = 16.0
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
    sample_jaccard: dict[str, float] | None = None,
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
    s_art = _norm(seed.get("artist"))
    s_fmt = _norm(seed.get("format"))

    # Retro seeds (chip/tracker/synth) score on composer + sound-chip + sample
    # lineage; modern seeds keep the genre/era model.  Detect once, precompute.
    seed_retro = is_retro_format(seed.get("format"))
    if seed_retro:
        s_chipfam = chip_family(seed.get("format"))
        s_insts = instrument_tokens(seed.get("instruments"))
        s_sidmodel = seed.get("sid_model")
        s_channels = seed.get("channels")
        s_groups = {g for g in (seed.get("scene_group") or "").split(" • ") if g}
    else:
        s_gen = _genres(seed)
        s_aart = _norm(seed.get("album_artist"))
        s_year = seed.get("year")
        s_bpm = seed.get("bpm")

    scored: list[tuple[float, dict]] = []
    for t in candidates:
        tid = t.get("id")
        if not tid or tid == sid:
            continue
        # Zero-length MODERN files are broken stubs — drop them.  But retro
        # chip/tracker formats (AHX/UADE/HVL/…) legitimately store duration 0
        # until they're render-probed, so keep them for a retro seed's mix.
        if not seed_retro and not t.get("duration"):
            continue

        score = 0.0
        if seed_retro:
            # ── Retro: composer + chip family + exact replayer + sample lineage ──
            if s_art and _norm(t.get("artist")) == s_art:
                score += _W_R_COMPOSER
            if s_chipfam and chip_family(t.get("format")) == s_chipfam:
                score += _W_R_CHIPFAM
            if s_fmt and _norm(t.get("format")) == s_fmt:
                score += _W_R_FORMAT
            if s_insts:
                if sample_jaccard is not None:
                    # Precomputed via the store's inverted instrument-token index
                    # (store.retro_sample_jaccard) — equals the on-the-fly value
                    # on a fresh index, ~40 ms → <1 ms (may lag a re-scanned
                    # track's sample sub-signal until the index rebuilds).
                    j = sample_jaccard.get(tid)
                    if j:
                        score += _W_R_SAMPLES * j
                else:
                    # No index supplied (unit tests / direct callers): exact
                    # on-the-fly Jaccard — always current, just slower.
                    ti = instrument_tokens(t.get("instruments"))
                    inter = len(s_insts & ti) if ti else 0
                    if inter:
                        score += _W_R_SAMPLES * (inter / len(s_insts | ti))
            if s_sidmodel and t.get("sid_model") == s_sidmodel:
                score += _W_R_SIDMODEL
            if s_groups:                   # shared scene group (same collective)
                tg = t.get("scene_group")
                if tg and s_groups & {g for g in tg.split(" • ") if g}:
                    score += _W_R_GROUP
            tc = t.get("channels")
            if s_channels and tc:
                score += _W_R_CHANNELS * max(0.0, 1.0 - abs(tc - s_channels) / _R_CHANNEL_SPAN)
        else:
            # ── Modern: genre + artist + era ──
            if s_gen:                      # skip per-candidate set build for sparse seeds
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

        # ── common: rating, quality, recency, jitter ──
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
