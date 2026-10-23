# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Best match: the relevance order of a search (``core/search_sets``).

A search's free text (the ``query`` predicate — what is left of the search box
after the field operators: ``artist:Ghost love`` ranks by "love", the
``artist:`` part only filters) is split into terms the way the search matches
it (``store._tokenize_text`` + ``fold_token``: lower-cased, accents folded).
Every listed track is given one score from the fields the text search reads,
each with a weight:

    title 8 · artist 4 · album artist 4 · album 2 ·
    composer, scene group, game and its other names 1

A term matches a word of a field EXACTLY (the whole word) or as a PREFIX (the
word starts with it — how the search finds a track).  The score compares, in
this order (the first difference decides):

1. exact title — the title's words are the query's words ("Ghost" for
   ``ghost``, "Ghost Love Score" for ``ghost love score``);
2. whole words — the largest weight of a field holding EVERY term as a
   whole word (an artist "Ghost" for ``ghost`` ranks above a title
   "Ghostbusters", which holds it only as a prefix);
3. phrase — the largest weight of a field holding the query's words next to
   each other, in order, the last one exact or a prefix (queries of two or
   more words);
4. one field — the largest weight of a field holding EVERY term (0 when the
   terms are only found spread over several fields), then how many terms
   that field holds as whole words;
5. exact field — the largest weight of a field whose words are the query's
   words (an artist "Ghost" for ``ghost``);
6. evidence — the sum, over every field and term, of the field's weight,
   doubled for a whole-word match.

Ties go newest first and then by id (descending) — the search's newest-first
order, which is also what a search without free text lists (a pure filter
such as ``format:SID year:>2020`` is not ranked).  The order is complete and
deterministic: no two tracks compare equal.

Cost: a dict lookup per field per track once a field value was seen in this
ranking (``Ranker`` memoises each distinct value: artists, albums and
repeated titles are analysed once) and a few substring tests to skip the
fields no term is in.  ``core/search_sets`` scores a set ``RANK_SLICE``
tracks at a time with a turn for the event loop between slices, sorts each
slice and merges them as far as a page needs.
"""
from __future__ import annotations

from soniqboom.core.store import _SPLIT_RE, _added_value, _tokenize_text, fold_token

W_TITLE = 8
W_ARTIST = 4
W_ALBUM = 2
W_OTHER = 1

# (field, weight) — the fields the text search tokenises (``_tokenize_track``)
# but ``game_aliases``, a list scored per name below.
FIELDS = (("title", W_TITLE), ("artist", W_ARTIST), ("album_artist", W_ARTIST),
          ("album", W_ALBUM), ("composer", W_OTHER), ("scene_group", W_OTHER),
          ("game", W_OTHER))

_MISSING = object()


def query_terms(text: str | None) -> list[str]:
    """The terms of a search's free text, as the search matches them
    (``TrackStore._resolve_query``): lower-cased words, accents folded."""
    return [fold_token(t) for t in _tokenize_text(text or "")]


def _words(v: str) -> list[str]:
    """The words of a field value as the search index holds them, folded."""
    low = v.lower()
    if low.isascii():
        return [w for w in _SPLIT_RE.split(low) if w]
    return [fold_token(w) for w in _tokenize_text(v)]


class Ranker:
    """Scores tracks for the terms ``terms`` (``query_terms``): ``key(t)`` is
    the score, ``sort_key(tid, t)`` the full, unique key of the order (larger
    first)."""

    __slots__ = ("terms", "uterms", "multi", "_memo")

    def __init__(self, terms: list[str]) -> None:
        self.terms = list(terms)
        self.uterms = tuple(dict.fromkeys(terms))
        self.multi = len(self.terms) >= 2
        self._memo: dict = {}

    # ── one field value ──────────────────────────────────────────────────
    def _field(self, v: str):
        """``(matches, phrase, full, sum(matches))`` for field value ``v`` (a
        non-empty string) — ``matches``: per distinct term 2 (a whole word),
        1 (a word's prefix) or 0; ``phrase``: the terms next to each other
        (``_phrase``, two or more terms); ``full``: its words are the terms —
        or None when no term is in it.  Kept in ``_memo`` per value."""
        r = None
        low = v.lower()
        if not low.isascii() or any(q in low for q in self.uterms):
            words = _words(v)
            m = []
            hit = False
            for q in self.uterms:
                best = 0
                for w in words:
                    if w == q:
                        best = 2
                        break
                    if best == 0 and w.startswith(q):
                        best = 1
                m.append(best)
                if best:
                    hit = True
            if hit:
                terms = self.terms
                r = (tuple(m), self.multi and _phrase(words, terms), words == terms, sum(m))
        self._memo[v] = r
        return r

    # ── one track ────────────────────────────────────────────────────────
    def key(self, t: dict) -> int:
        """The score of track ``t`` (larger = better), the comparisons of the
        module docstring packed into one integer."""
        title_full = whole_w = phrase_w = cover_w = cover_ex = full_w = total = 0
        memo = self._memo
        get = t.get
        for name, w in FIELDS:
            v = get(name)
            if not v or v.__class__ is not str:
                continue
            r = memo.get(v, _MISSING)
            if r is _MISSING:
                r = self._field(v)
            if r is None:
                continue
            m, ph, full, s = r
            if full:
                if w == W_TITLE and name == "title":
                    title_full = 1
                if w > full_w:
                    full_w = w
            if ph and w > phrase_w:
                phrase_w = w
            if 0 not in m:
                ex = m.count(2)
                if w > cover_w or (w == cover_w and ex > cover_ex):
                    cover_w, cover_ex = w, ex
                if ex == len(m) and w > whole_w:
                    whole_w = w
            total += w * s
        aliases = get("game_aliases")
        if aliases and isinstance(aliases, (list, tuple)):
            for v in aliases:
                if not v or v.__class__ is not str:
                    continue
                r = memo.get(v, _MISSING)
                if r is _MISSING:
                    r = self._field(v)
                if r is None:
                    continue
                m, ph, full, s = r
                if full and W_OTHER > full_w:
                    full_w = W_OTHER
                if ph and W_OTHER > phrase_w:
                    phrase_w = W_OTHER
                if 0 not in m:
                    ex = m.count(2)
                    if W_OTHER > cover_w or (W_OTHER == cover_w and ex > cover_ex):
                        cover_w, cover_ex = W_OTHER, ex
                    if ex == len(m) and W_OTHER > whole_w:
                        whole_w = W_OTHER
                total += W_OTHER * s
        if cover_ex > 15:
            cover_ex = 15
        if total > 0xFFFFFF:
            total = 0xFFFFFF
        return ((title_full << 44) | (whole_w << 40) | (phrase_w << 36) | (cover_w << 32)
                | (cover_ex << 28) | (full_w << 24) | total)

    def sort_key(self, tid: str, t: dict) -> tuple:
        """``(score, added, id)`` — the order is these keys, largest first."""
        return (self.key(t), _added_value(t), tid)


def _phrase(words: list[str], terms: list[str]) -> bool:
    """The terms next to each other in ``words``, in order — every one a whole
    word but the last, which may be a prefix."""
    k = len(terms)
    last = k - 1
    for i in range(len(words) - last):
        if all(words[i + j] == terms[j] for j in range(last)) and words[i + last].startswith(terms[last]):
            return True
    return False


def rank(store, ids, terms: list[str]) -> list[str]:
    """``ids`` in Best match order for ``terms`` (in one go — tests, small
    sets; ``core/search_sets`` scores large sets a slice at a time)."""
    r = Ranker(terms)
    tracks = store._tracks
    rows = [r.sort_key(tid, tracks[tid]) for tid in ids if tid in tracks]
    rows.sort(reverse=True)
    return [row[2] for row in rows]
