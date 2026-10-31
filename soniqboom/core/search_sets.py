# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Search result sets: the whole result of a search, kept on the server and
paged by the web lists (``GET /api/search/page``).

The first request for a search builds its set: ``TrackStore.result_ids`` —
every id the search lists, what ``filter_tracks`` pages for its predicates —
kept under a short handle (``rs``).  The total is ``len`` of that set, exact
from the first page on.  Pages are slices of ORDERS of the set:

* the default order, Best match (``RANK``) — every member scored for the
  search's free text (``core/search_rank``: field weights, whole word over
  prefix, every term in one field, the exact title and the phrase first),
  ties newest first.  A scoped search (``rank_by``: the phone's scope keys —
  an operator's values and no free text) is ranked by those values, on that
  operator's fields (``search_rank.SCOPES``); the scope is part of the set's
  key.  Scored once per set, ``RANK_SLICE`` tracks at a time
  with a turn for the event loop between slices (every member before the
  first page: any of them may be the best), the sorted slices then merged as
  far as a page needs; kept, it holds as long as the set does (any write that
  could change a score is a browse write);
* newest first (``sort=added``, and the default of a search without free
  text — a pure filter such as ``format:SID`` is not ranked) —
  ``_sorted_added_at`` (with duplicates hidden and no stand-ins:
  ``_sorted_added_at_primary``) read from its end, the order ``/api/search``
  lists;
* album-artist matches first (``FIRST``, the default of an ``artist:``
  search without free text whose set holds both album-artist matches and
  others — ``TrackStore.album_artist_first``): those members newest first,
  then the rest newest first (``_FirstOrder``: two orders of the newest-first
  index, read one after the other);
* a column order (``sort`` = a ``TrackStore._SORT_INDEX_MAP`` key, ``order``
  asc / desc): the sorted index All Tracks' column sort pages, read in that
  direction keeping the set's ids (``TrackStore.result_order``).  The keys and
  the tie-break (the id) are the index's, so a sorted search lists its tracks
  exactly in the order a sorted All Tracks lists them.  Built when first asked
  for, then kept; descending is ascending reversed, so a finished order serves
  both directions.  (While a scan defers the sorted lists' rebuild, the tracks
  it added are not in the index yet: they follow the walked ones, by the
  index's keys, in each direction — until the rebuild makes the set stale.)

An order is read only as far as a page needs (``store._FilterWalk``).  A deep
page reads the rest in slices of at most ``SLICE_ROWS`` index rows with a turn
for the event loop between them, and once a page of the web list was served
the rest of that order is read the same way in the background, pausing
``BACKGROUND_PAUSE_S`` between slices (``schedule_finish``).

Validity.  A set is used only while the browse state it was made in holds:
the same store, ``_browse_seq`` (any write a list can show, a sorted list
replaced, an index swap), "Hide duplicates", the ids the lists leave out
(``availability.hidden``: Tracks on unavailable drives / Tracks that can't be
played — Hide) and the duplicate stand-ins (``availability.primary_swap``) —
both compared by identity, the same frozen objects until they change.  An
order also needs its index list unchanged: the same list object and
``_primary_order_key`` (for the duration order that includes
``_duration_seq``: a play-time length backfill re-keys only that list).  A
request naming a set that no longer holds — stale, evicted, forgotten by a
restart, or one whose order went stale — is answered from a fresh set for the
same search (``resolve``), with the new handle and ``renewed``; the web list
then reads itself again in place.  A read that gave the loop a turn and found
its set stale meanwhile starts over; the last attempt reads in one go, so a
library written on every turn of the loop (a scan commit) still answers.

Bounds: the ``MAX_SETS`` most recently used sets, holding at most ``MAX_IDS``
ids in all (id sets and the orders read so far — checked when a set or an
order is added, so orders still being read can pass it until the next one).  Only the store's owner
thread keeps, extends or stores sets (``TrackStore._memo_ok``); a request from
another thread computes its answer privately.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import secrets
from typing import Any

log = logging.getLogger(__name__)

MAX_SETS = 32
MAX_IDS = 8_000_000
# Index rows an order reads per slice before the event loop gets a turn
# (~2 ms at 680K tracks: a list slice and a membership test per row).
SLICE_ROWS = 16_384
# The background read of an order (``schedule_finish``) pauses this long
# between slices, so requests arriving meanwhile are served between them.
BACKGROUND_PAUSE_S = 0.002
# A page of at least this many rows (a list's, not the search box's preview)
# starts the background read of its order.
FINISH_MIN_LIMIT = 100
# A request whose set stopped holding while it gave the loop a turn starts
# over at most this often; the last attempt reads without a turn
# (``api.search.search_page``).
RETRIES = 3
# The Best match order's name among a set's orders (``sort=rank``, and the
# default order of a search with free text).
RANK = "rank"
# The default order of an ``artist:`` filter without free text whose set
# holds album-artist matches and others: those first, then the rest, each
# newest first (``TrackStore.album_artist_first``; ``_FirstOrder``).
FIRST = "album_artist_first"
# Members a Best match order scores per slice before the event loop gets a
# turn (~1.5–2.5 µs a track at 680K tracks: ~6–10 ms a slice).
RANK_SLICE = 4096
# Rows of a scored Best match order merged per slice (~5 ms).
MERGE_SLICE = 8192

# Fields a web LIST row never reads (``fields=list``): Track Info asks for
# them itself (``/extended``, ``/patterns``) and no list, queue or player code
# reads them from a row.  Everything else a stored row has is kept — the row,
# the queue, the player and Track Info's first paint read it.
LIST_DROP = frozenset(("embedding", "stil", "instruments", "patterns", "sid_md5",
                       "file_md5", "songdb_fields", "scene_use",
                       "dir_hash", "scan_root_hash", "index_vals", "index_fields"))
_FULL_DROP = frozenset(("embedding",))


class Stale(Exception):
    """The set, or the order a request reads, stopped holding while the
    request gave the event loop a turn."""


class Gone(Exception):
    """The client of a request went away while the request read an order
    (what was read stays with the set)."""


class State:
    """What the lists leave out right now — the browse state a set is made in
    and checked against (see the module docstring)."""

    __slots__ = ("store", "seq", "dedup", "exclude", "swap")

    def __init__(self, store) -> None:
        from soniqboom.core import availability
        self.store = store
        self.seq = store._browse_seq
        self.dedup = bool(store.get_config("filter_duplicates", False))
        self.exclude = availability.hidden(store)
        self.swap = availability.primary_swap(store, dedup=self.dedup)


class _Order:
    """One order of a set: its ids in index order, complete (``ids``) or a
    walk read so far (``walk``)."""

    __slots__ = ("attr", "desc", "key", "idx", "walk", "ids", "task", "exact")

    def __init__(self, store, members, attr: str, desc: bool) -> None:
        self.attr = attr
        self.desc = desc
        self.key = store._primary_order_key(attr)
        self.idx = getattr(store, attr)
        self.task: asyncio.Task | None = None
        # False once members the index doesn't hold were put after the walk
        # (``_finish``): the reverse of this order is then not the other
        # direction's order.
        self.exact = True
        made = store.result_order(members, attr, desc)
        if isinstance(made, list):
            self.ids: list[str] | None = made
            self.walk = None
        else:
            self.ids = None
            self.walk = made

    def valid(self, store) -> bool:
        """Its index list is the one it was read from, unchanged."""
        return (self.key == store._primary_order_key(self.attr)
                and self.idx is getattr(store, self.attr))

    def held(self) -> int:
        return len(self.ids) if self.ids is not None else len(self.walk.out)

    def ready(self, need: int) -> bool:
        return self.ids is not None or len(self.walk.out) >= need

    def step(self, store, rset: "ResultSet") -> None:
        """Read one slice (``SLICE_ROWS`` index rows)."""
        self.walk.advance(SLICE_ROWS)
        _finish(store, rset, self)

    def run(self, store, rset: "ResultSet", need: int) -> None:
        """Read until ``need`` ids are held (or all), in one go."""
        self.walk.upto(need)
        _finish(store, rset, self)

    def partial(self, offset: int, limit: int) -> list[str]:
        return self.walk.out[offset:offset + limit]


class _RankOrder:
    """A set's Best match order (``RANK``): its members scored
    (``search_rank.Ranker``) ``RANK_SLICE`` at a time, each slice sorted on
    its own (best first, ties newest first, then by id descending); once every
    member is scored the slices are merged (``heapq.merge``) as far as a page
    needs, ``MERGE_SLICE`` rows at a time — a first page does not wait for the
    whole order.  ``out``: the order read so far; ``ids``: the whole order."""

    __slots__ = ("attr", "desc", "ranker", "todo", "n", "pos", "runs", "merge", "out",
                 "ids", "task", "exact")

    def __init__(self, store, rset: "ResultSet") -> None:
        from soniqboom.core import search_rank
        self.attr = RANK
        self.desc = False
        self.exact = False              # (one direction only: never read reversed)
        self.task: asyncio.Task | None = None
        self.ranker = search_rank.Ranker(rset.terms, rset.scope)
        self.todo = list(rset.ids)
        self.n = len(self.todo)
        self.pos = 0
        self.runs: list[list[tuple]] = []
        self.merge = None
        self.out: list[str] = []
        self.ids: list[str] | None = None
        if not self.n:
            self.ids = []

    def valid(self, store) -> bool:
        # (a score reads only fields a browse write changes — the set holding
        # is enough)
        return True

    def held(self) -> int:
        return len(self.ids) if self.ids is not None else self.pos

    def ready(self, need: int) -> bool:
        return self.ids is not None or (self.merge is not None and len(self.out) >= need)

    def _score(self, store, n: int) -> None:
        todo, tracks, sort_key = self.todo, store._tracks, self.ranker.sort_key
        lo = self.pos
        hi = min(self.n, lo + n)
        run = []
        for tid in todo[lo:hi]:
            t = tracks.get(tid)
            # (a member deleted since — the set is stale; it still has a row)
            run.append(sort_key(tid, t) if t is not None else (-1, 0, tid))
        run.sort(reverse=True)
        self.runs.append(run)
        self.pos = hi
        if hi >= self.n:
            self.merge = heapq.merge(*self.runs, reverse=True)
            self.runs = []
            self.todo = []
            self.ranker = None

    def _take(self, n: int) -> None:
        self.out += [r[2] for r in itertools.islice(self.merge, n)]
        if len(self.out) >= self.n:
            self.ids = self.out
            self.out = []
            self.merge = None

    def step(self, store, rset: "ResultSet") -> None:
        """Score one slice (``RANK_SLICE`` members) — or, every member scored,
        read ``MERGE_SLICE`` more rows of the merged order."""
        if self.ids is not None:
            return
        if self.merge is None:
            self._score(store, RANK_SLICE)
        else:
            self._take(MERGE_SLICE)

    def run(self, store, rset: "ResultSet", need: int) -> None:
        """Score every member left and read the order as far as ``need``, in one go."""
        if self.ids is None:
            if self.merge is None:
                self._score(store, self.n - self.pos)
            if len(self.out) < need:
                self._take(need - len(self.out))

    def partial(self, offset: int, limit: int) -> list[str]:
        return self.out[offset:offset + limit]


class _FirstOrder:
    """A set's ``FIRST`` order: its album-artist matches (``ResultSet.first``)
    then the rest, each in newest-first order — two ``_Order``s of the
    newest-first index, the second read once the first is complete.  One
    direction only (never read reversed)."""

    __slots__ = ("attr", "desc", "parts", "views", "ids", "task", "exact")

    def __init__(self, store, rset: "ResultSet") -> None:
        import types
        self.attr = FIRST
        self.desc = False
        self.exact = False
        self.task: asyncio.Task | None = None
        head = rset.first
        rest = rset.ids - head
        # (each part's own view of a set: what ``_finish`` reads)
        self.views = [types.SimpleNamespace(ids=p, total=len(p), orders={}) for p in (head, rest)]
        self.parts = [_Order(store, p, rset.default_attr, True) for p in (head, rest)]
        self.ids: list[str] | None = None
        self._join()

    def _join(self) -> None:
        a, b = self.parts
        if a.ids is not None and b.ids is not None:
            self.ids = a.ids + b.ids

    def _current(self) -> int | None:
        for i, p in enumerate(self.parts):
            if p.ids is None:
                return i
        return None

    def valid(self, store) -> bool:
        return all(p.valid(store) for p in self.parts)

    def held(self) -> int:
        return sum(p.held() for p in self.parts)

    def _read(self) -> list[str]:
        a, b = self.parts
        if a.ids is None:
            return a.walk.out
        return a.ids + (b.ids if b.ids is not None else b.walk.out)

    def ready(self, need: int) -> bool:
        return self.ids is not None or len(self._read()) >= need

    def step(self, store, rset: "ResultSet") -> None:
        i = self._current()
        if i is not None:
            self.parts[i].step(store, self.views[i])
        self._join()

    def run(self, store, rset: "ResultSet", need: int) -> None:
        for i, p in enumerate(self.parts):
            if p.ids is None:
                have = len(self.parts[0].ids) if i == 1 else 0
                p.run(store, self.views[i], max(1, need - have))
                if p.ids is None:
                    break
        self._join()

    def partial(self, offset: int, limit: int) -> list[str]:
        return self._read()[offset:offset + limit]


class ResultSet:
    """The ids a search lists (``ids``, ``total``) and the orders read so far."""

    __slots__ = ("rs", "key", "store", "seq", "dedup", "exclude", "swap", "ids",
                 "total", "orders", "default_attr", "terms", "dyn", "first", "scope")

    def __init__(self, key: tuple, state: State, ids, default_attr: str,
                 terms: list[str] | None = None, dyn: tuple | None = None,
                 first: set | None = None, scope: str | None = None) -> None:
        self.rs = secrets.token_urlsafe(6)
        self.key = key
        self.store = state.store
        self.seq = state.seq
        self.dedup = state.dedup
        self.exclude = state.exclude
        self.swap = state.swap
        self.ids = ids
        self.total = len(ids)
        self.orders: dict[tuple[str, bool], _Order | _RankOrder] = {}
        # ``default_attr``: the newest-first index; ``terms``: the free text's
        # terms (``search_rank.query_terms``) — None: the set is not ranked.
        self.default_attr = default_attr
        self.terms = terms or None
        # ``dyn``: what the ids also depend on besides the browse state
        # (``TrackStore.volatile_key``: ratings, plays, the clock) when made.
        self.dyn = dyn
        # ``first``: the members the default order lists first (``FIRST``) —
        # None: that order is the newest-first index.
        self.first = first
        # ``scope``: the ``search_rank.SCOPES`` key whose values ``terms``
        # are and whose fields Best match scores (``rank_by``) — None: the
        # free text's terms, every field.
        self.scope = scope if self.terms else None

    def holds(self, state: State) -> bool:
        """Made in this browse state (see the module docstring)."""
        return (state.store is self.store and state.seq == self.seq
                and state.dedup == self.dedup and state.exclude is self.exclude
                and state.swap is self.swap)

    def orders_hold(self, store, attr: str) -> bool:
        """Every order of ``attr`` read so far is still valid."""
        return all(o.valid(store) for (a, _d), o in self.orders.items() if a == attr)

    def held(self) -> int:
        return (self.total + (len(self.first) if self.first else 0)
                + sum(o.held() for o in self.orders.values()))

    def clone(self, state: State, drop_attr: str) -> "ResultSet":
        """The same ids under a new handle, without the orders of
        ``drop_attr`` (stale while the set is not)."""
        c = ResultSet(self.key, state, self.ids, self.default_attr, self.terms, self.dyn,
                      self.first, self.scope)
        c.orders = {k: o for k, o in self.orders.items() if k[0] != drop_attr}
        return c

    def attr_for(self, store, sort_by: str | None) -> str:
        """The order a request for ``sort_by`` reads: ``RANK`` (no column, or
        ``rank``) for a ranked set, ``FIRST`` (no column) for a set with
        album-artist matches first, the newest-first index (``added``, or
        another set without free text), else the column's sorted index."""
        if not sort_by or sort_by == RANK:
            if self.terms:
                return RANK
            return FIRST if self.first else self.default_attr
        if sort_by == "added":
            return self.default_attr
        return store._sort_index_attr(sort_by, filter_duplicates=self.dedup and self.swap is None)


# ── Kept sets ────────────────────────────────────────────────────────────────

def _kept(store) -> bool:
    return store._memo_ok()


def _touch(store, rset: ResultSet) -> None:
    sets = store._result_sets
    sets[rset.rs] = sets.pop(rset.rs, rset)        # (most recently used last)


def _keep(store, rset: ResultSet) -> None:
    """Keep ``rset`` (most recently used) as the newest set for its search,
    dropping the sets no longer valid and the least recently used ones past
    the bounds."""
    sets, keys = store._result_sets, store._result_set_keys
    seq = store._browse_seq
    for rs in [rs for rs, s in sets.items() if s.seq != seq or s.store is not store]:
        _forget(store, rs)
    sets[rset.rs] = rset
    keys[rset.key] = rset.rs
    _trim(store, rset)


def _trim(store, keep: ResultSet | None = None) -> None:
    sets = store._result_sets
    held = sum(s.held() for s in sets.values())
    while len(sets) > 1 and (len(sets) > MAX_SETS or held > MAX_IDS):
        rs = next(iter(sets))
        if keep is not None and rs == keep.rs:          # (never the one in use)
            sets[rs] = sets.pop(rs)
            rs = next(iter(sets))
            if rs == keep.rs:
                break
        held -= sets[rs].held()
        _forget(store, rs)


def _forget(store, rs: str) -> None:
    s = store._result_sets.pop(rs, None)
    if s is not None and store._result_set_keys.get(s.key) == rs:
        del store._result_set_keys[s.key]
    if s is not None:
        for o in s.orders.values():
            if o.task is not None:
                o.task.cancel()
                o.task = None


def drop_all(store) -> None:
    """Forget every kept set (tests; ``TrackStore._drop_result_sets`` does the
    same when the library is replaced)."""
    for rs in list(store._result_sets):
        _forget(store, rs)


def search_key(preds: dict, dedup: bool, scope: str | None = None) -> tuple:
    """The predicates of a search (``api.search.search_predicates``) as a
    set's lookup key: "crack" and "Crack" share a set.  A search that reads
    one user's plays carries that user (``uid``) among its predicates, so two
    users never share its set.  ``scope``: the scope it is ranked by
    (``resolve``'s ``rank_by`` when it applies) — another order, another
    set."""
    items = []
    for k, v in sorted(preds.items()):
        if isinstance(v, str):
            v = " ".join(v.lower().split()) if k == "query" else v
        items.append((k, v))
    key = (tuple(items), bool(dedup))
    return key + (scope,) if scope else key


def _build(store, preds: dict, state: State, key: tuple, scope: str | None = None,
           scope_terms: list[str] | None = None) -> ResultSet:
    ids = store.result_ids(filter_duplicates=state.dedup, exclude=state.exclude,
                           swap=state.swap, **preds)
    default_attr = store._sort_index_attr(None, filter_duplicates=state.dedup and state.swap is None)
    from soniqboom.core import search_rank
    terms = search_rank.query_terms(preds.get("query"))
    if not terms and scope:
        terms = list(scope_terms or ())
    else:
        scope = None
    first = None
    if not terms:
        hits = store.album_artist_first(preds)
        if hits is not None:
            first = ids & hits
            if not first or len(first) == len(ids):
                first = None                    # (all or none: the plain order)
    return ResultSet(key, state, ids, default_attr, terms, store.volatile_key(preds), first,
                     scope)


def resolve(store, preds: dict, rs: str | None = None, sort_by: str | None = None,
            state: State | None = None, rank_by: str | None = None) -> tuple[ResultSet, bool]:
    """The set to answer a request for the search ``preds`` (``rs``: the
    handle the client has) with — ``(set, renewed)``.  ``rs`` itself while it
    holds for this browse state and its order of ``sort_by`` is valid; else
    the newest kept set for the search that does, a copy of it without a
    stale order, or a new set.  ``renewed``: the answer comes from another set
    than ``rs`` (always False without ``rs``).

    A search whose results also hang on ratings, plays or the clock
    (``rating:``, ``plays:`` / ``played:``, a relative ``added:``:
    ``TrackStore.volatile_key``) keeps paging the set its handle names — its
    rows don't shift under the list or the play queue while tracks are
    played — but a request without that handle gets a set made since the
    last such change (or this minute).

    ``rank_by`` (a ``search_rank.SCOPES`` key — the phone's scope): a search
    without free text that has values of that operator is ranked by them
    (``search_rank.scope_terms``), on that operator's fields; otherwise
    (another key, free text, no such value) it is ignored."""
    if state is None:
        state = State(store)
    from soniqboom.core import search_rank
    sterms = search_rank.scope_terms(preds, rank_by)
    scope = rank_by if sterms else None
    key = search_key(preds, state.dedup, scope)
    owner = _kept(store)
    if owner:
        sets = store._result_sets
        cands = []
        if rs:
            c = sets.get(rs)
            if c is not None and c.key == key:          # (not a handle of another search)
                cands.append(c)
        k = store._result_set_keys.get(key)
        c = sets.get(k) if k else None
        if c is not None and c not in cands and c.dyn == store.volatile_key(preds):
            cands.append(c)
        held = [c for c in cands if c.holds(state)]
        for cand in held:
            if cand.orders_hold(store, cand.attr_for(store, sort_by)):
                _touch(store, cand)
                return cand, bool(rs) and cand.rs != rs
        if held:
            # (the ids hold, an order of this column doesn't — a length
            # backfill re-keyed the duration list: the same ids, a new handle)
            cand = held[0].clone(state, held[0].attr_for(store, sort_by))
            _keep(store, cand)
            return cand, bool(rs)
    rset = _build(store, preds, state, key, scope, sterms)
    if owner:
        _keep(store, rset)
    return rset, bool(rs)


# ── Orders ───────────────────────────────────────────────────────────────────

def _order(store, rset: ResultSet, attr: str, desc: bool) -> _Order:
    if attr in (RANK, FIRST):
        desc = False
    o = rset.orders.get((attr, desc))
    if o is None:
        o = (_RankOrder(store, rset) if attr == RANK else
             _FirstOrder(store, rset) if attr == FIRST else _Order(store, rset.ids, attr, desc))
        rset.orders[(attr, desc)] = o
        if _kept(store) and rset.rs in store._result_sets:
            _trim(store, rset)
    return o


def _finish(store, rset: ResultSet, o: _Order) -> None:
    """A walk that reached the end of its index (or found every member) becomes
    the complete order.  Members the index does not hold — tracks a running
    scan added while it defers the sorted lists' rebuild; the set goes stale
    with the rebuild — follow, by the index's keys in the order's direction
    (in either direction they come last: the order is then not ``exact``)."""
    w = o.walk
    if w is None or not (w.done or len(w.out) == rset.total):
        return                  # (every member found: the rest of the index adds none)
    out = w.out
    if len(out) != rset.total:
        seen = set(out)
        missing = [t for t in rset.ids if t not in seen]
        value = store.sort_value(o.attr)
        tracks = store._tracks
        rows = []
        for tid in missing:
            t = tracks.get(tid)
            v = value(t) if t is not None else None
            rows.append((v if v is not None else float("inf"), tid))
        try:
            rows.sort(reverse=o.desc)
        except TypeError:
            rows.sort(key=lambda r: r[1], reverse=o.desc)
        out = out + [tid for _v, tid in rows]
        o.exact = False
    o.ids = out
    o.walk = None
    if not o.exact:
        return
    # (the other direction: this one reversed — a walk of it is not needed)
    other = rset.orders.get((o.attr, not o.desc))
    if other is not None and other.ids is None:
        if other.task is not None:
            other.task.cancel()
        del rset.orders[(o.attr, not o.desc)]


def _ensure_sync(store, rset: ResultSet, o, need: int) -> None:
    if o.ids is None:
        o.run(store, rset, need)


async def _ensure(store, rset: ResultSet, o, need: int, state_of, gone=None) -> None:
    """Read order ``o`` until it holds ``need`` ids (or all), a slice at a
    time with a turn for the event loop between slices; raise ``Stale`` when
    the set or the order stopped holding meanwhile (``state_of()``: the
    browse state now; None: not checked — the read goes on in the set it
    started in), ``Gone`` when ``gone()`` (awaited every fourth turn) says
    the client went away."""
    turns = 0
    while not o.ready(need):
        o.step(store, rset)
        if o.ready(need):
            return
        await asyncio.sleep(0)
        if state_of is not None and (not rset.holds(state_of()) or not o.valid(store)):
            raise Stale()
        turns += 1
        if gone is not None and turns % 4 == 0 and await gone():
            raise Gone()


def _slice(rset: ResultSet, attr: str, desc: bool, offset: int, limit: int) -> list[str] | None:
    """The page from a complete order of ``attr`` in either direction, or None."""
    if attr in (RANK, FIRST):
        desc = False
    o = rset.orders.get((attr, desc))
    if o is not None and o.ids is not None:
        return o.ids[offset:offset + limit]
    r = rset.orders.get((attr, not desc))
    if r is not None and r.ids is not None and r.exact:
        n = len(r.ids)
        hi = n - offset
        if hi <= 0:
            return []
        lo = hi - limit if hi > limit else 0
        page = r.ids[lo:hi]
        page.reverse()
        return page
    return None


def page_ids_sync(store, rset: ResultSet, attr: str, desc: bool, offset: int,
                  limit: int) -> list[str]:
    """Ids ``offset`` … ``offset + limit`` of the order (read as far as
    needed, in one go — for short reads: the first rows of a search)."""
    page = _slice(rset, attr, desc, offset, limit)
    if page is not None:
        return page
    o = _order(store, rset, attr, desc)
    _ensure_sync(store, rset, o, offset + limit)
    page = _slice(rset, attr, desc, offset, limit)
    if page is not None:
        return page
    return o.partial(offset, limit)


async def page_ids(store, rset: ResultSet, attr: str, desc: bool, offset: int,
                   limit: int, state_of, gone=None) -> list[str]:
    """Like :func:`page_ids_sync`, reading a long stretch of the order in
    slices (``_ensure``)."""
    page = _slice(rset, attr, desc, offset, limit)
    if page is not None:
        return page
    o = _order(store, rset, attr, desc)
    await _ensure(store, rset, o, offset + limit, state_of, gone)
    page = _slice(rset, attr, desc, offset, limit)
    if page is not None:
        return page
    return o.partial(offset, limit)


async def complete(store, rset: ResultSet, attr: str, desc: bool, state_of, gone=None) -> list[str]:
    """The whole order (read in slices), in the requested direction."""
    page = _slice(rset, attr, desc, 0, rset.total)
    if page is not None:
        return page
    o = _order(store, rset, attr, desc)
    await _ensure(store, rset, o, rset.total + 1, state_of, gone)
    return _slice(rset, attr, desc, 0, rset.total) or []


def complete_sync(store, rset: ResultSet, attr: str, desc: bool) -> list[str]:
    """:func:`complete` in one go (no turn for the loop)."""
    page = _slice(rset, attr, desc, 0, rset.total)
    if page is not None:
        return page
    o = _order(store, rset, attr, desc)
    _ensure_sync(store, rset, o, rset.total + 1)
    return _slice(rset, attr, desc, 0, rset.total) or []


def anchor(order: list[str], after: str) -> int | None:
    """Where the player's anchor is in ``order`` (a finished order): ``after``
    names up to ``availability._ANCHOR_IDS`` track ids, the preferred first —
    the position of the most preferred one the order holds, or None (as
    ``availability._anchor_position`` finds it in a list it reads)."""
    from soniqboom.core import availability
    for tid in [i for i in (after or "").split(",") if i][:availability._ANCHOR_IDS]:
        try:
            return order.index(tid)
        except ValueError:
            continue
    return None


def schedule_finish(store, rset: ResultSet, attr: str, desc: bool) -> None:
    """Read the rest of the order in the background (slices, a turn for the
    loop between them) — so a later deep page, a sort-direction flip or the
    player's pages are slices of a finished order.  Stops when the set is no
    longer kept or the order's index changed."""
    if not _kept(store) or rset.rs not in store._result_sets:
        return
    if _slice(rset, attr, desc, 0, 0) is not None:
        return
    if attr in (RANK, FIRST):
        desc = False
    o = rset.orders.get((attr, desc))
    if o is None or o.ids is not None or o.task is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    o.task = loop.create_task(_finish_later(store, rset, o))


async def _finish_later(store, rset: ResultSet, o) -> None:
    try:
        while o.ids is None:
            await asyncio.sleep(BACKGROUND_PAUSE_S)
            if (o.ids is not None or rset.rs not in store._result_sets
                    or rset.orders.get((o.attr, o.desc)) is not o
                    or store._browse_seq != rset.seq or not o.valid(store)):
                return              # (finished by a request meanwhile, dropped, or stale)
            o.step(store, rset)
    except asyncio.CancelledError:
        pass
    except Exception:                                   # noqa: BLE001 — a background read never fails a request
        log.debug("search order read failed", exc_info=True)
    finally:
        o.task = None


# ── Rows ─────────────────────────────────────────────────────────────────────

def rows(store, ids, *, slim: bool = False) -> list[dict]:
    """The stored rows of ``ids`` (ids gone since are left out) — with
    ``slim`` without ``LIST_DROP`` — each a copy, flagged ``offline`` as
    ``availability.mark_rows`` flags it."""
    from soniqboom.core import availability
    kind_of = availability.offline_marker(store)
    drop = LIST_DROP if slim else _FULL_DROP
    tracks = store._tracks
    out = []
    for tid in ids:
        t = tracks.get(tid)
        if not t:
            continue
        row = {k: v for k, v in t.items() if k not in drop}
        if kind_of is not None:
            k = kind_of(t)
            if k:
                row["offline"] = k
        out.append(row)
    return out


def head(store, preds: dict, limit: int) -> tuple[ResultSet, list[str]]:
    """The set of a search and the first ``limit`` ids of its default order
    (Best match, or newest first for a search without free text)."""
    rset, _renewed = resolve(store, preds)
    return rset, page_ids_sync(store, rset, rset.attr_for(store, None), True, 0, limit)


def describe(store) -> dict[str, Any]:
    """How many sets are kept and how many ids they hold (tests, benchmarks)."""
    return {"sets": len(store._result_sets),
            "ids": sum(s.held() for s in store._result_sets.values())}
