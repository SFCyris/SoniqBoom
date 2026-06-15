# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

""""Sounds-like" similarity — heuristic affinity blended with audio envelope.

Base ranking comes from the Instant-Mix affinity engine (genre / artist / era /
tempo / format).  Where BOTH the seed and a candidate have a stored waveform
(amplitude envelope, computed whenever a track is played), a cosine similarity
over a 16-bin loudness-contour vector is blended in — tracks that *move* the
same way (quiet intros, sustained walls, choppy chiptune gating) rank up.

Coverage note: waveforms exist only for tracks that have been played at least
once, so the envelope term phases itself in as the library gets listened to.
Everything else falls back to pure affinity — the endpoint never 501s.
"""
from __future__ import annotations

import math
import random

_ENV_BINS = 16
_W_ENV = 4.0           # weight of the envelope similarity in the blend


def envelope_vector(amps: list[float], bins: int = _ENV_BINS) -> list[float] | None:
    """Downsample an amplitude list into a max-normalised loudness contour."""
    if not amps or len(amps) < bins:
        return None
    n = len(amps)
    out = []
    for i in range(bins):
        lo = (i * n) // bins
        hi = max(lo + 1, ((i + 1) * n) // bins)
        seg = amps[lo:hi]
        out.append(sum(abs(x) for x in seg) / len(seg))
    peak = max(out)
    if peak <= 0:
        return None
    return [v / peak for v in out]


def envelope_cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def find_similar(
    seed: dict,
    candidates: list[dict],
    waveforms: dict[str, list[float]],
    *,
    ratings: dict[str, int] | None = None,
    k: int = 20,
) -> list[dict]:
    """Return up to ``k`` of ``[{track, score}]`` most like ``seed``.

    ``score`` is a 0–1 RELATIVE similarity within this result set (top hit =
    1.0) — suitable for the UI's percentage display, not an absolute metric.
    """
    from soniqboom.core.radio import build_instant_mix

    # Affinity shortlist — deterministic (fixed rng) and over-fetched so the
    # envelope re-rank has room to reorder.
    shortlist = build_instant_mix(
        seed, candidates,
        ratings=ratings or {}, recent_ids=(),
        limit=max(k * 4, 60), rng=random.Random(7),
    )
    if not shortlist:
        return []

    seed_env = envelope_vector(waveforms.get(seed.get("id"), []) or [])

    scored = []
    n = len(shortlist)
    for rank, t in enumerate(shortlist):
        score = float(n - rank)                     # preserve affinity order
        if seed_env is not None:
            amps = waveforms.get(t.get("id"))
            if amps:
                env = envelope_vector(amps)
                if env is not None:
                    # Scale into the same magnitude region as the rank scores
                    # so a strong contour match moves a candidate several
                    # places up the list.
                    score += _W_ENV * envelope_cosine(seed_env, env) * (n / 10.0)
        scored.append((score, t))
    scored.sort(key=lambda x: -x[0])
    top = scored[:k]
    peak = top[0][0] if top and top[0][0] > 0 else 1.0
    return [{"track": t, "score": round(s / peak, 3)} for s, t in top]
