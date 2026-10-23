# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Move a track's user data from its old id to a new one.

A track id is ``uuid5`` of its path, so a file that is moved or renamed gets
a new id (the Modland catalogue's changelog refresh, ``core/modland_catalog``).
Its rating, every user's play count / last played and listening-history
entries, playlist entries and stored waveform follow it here.  One pure
function over the plain containers, shared by the live store
(``TrackStore.remap_track_ids``) and AOF replay / merging
(``merger._apply_entry``, op ``remap_track_ids``) so the two can't disagree.
Import-light on purpose: the merger process uses it.
"""
from __future__ import annotations


def remap_user_data(mapping: dict, *, ratings: dict | None, play_stats: dict | None,
                    history: dict | None, playlists: dict | None,
                    waveforms: dict | None, keep_both: bool = False) -> dict:
    """Apply ``mapping`` (old id → new id) in place.  ``play_stats`` and
    ``history`` are per user (``{user id: {track id: stats}}`` /
    ``{user id: [entries]}``, ``core/user_data``); each user's are moved on
    their own.  All the old ids are moved at once, so chains (a → b while
    b → c) and swaps land right.  An old id's rating, play stats or waveform
    replaces what the new id had (another file once at that path); where the
    old id has none, the new id's is left alone — so applying a mapping again
    (a redone move) changes nothing.

    ``keep_both`` (the two HVSC copies of one tune, ``online_hvsc``): where
    the new id already has a value of its own in a container — a rating, a
    waveform, one user's play stats — both ids keep theirs (nothing is
    overwritten), and that user's history entries stay too; playlist entries
    always move.  Returns per-container counts of entries moved; playlist
    entries keep their ``subsong``."""
    m = {o: n for o, n in (mapping or {}).items()
         if isinstance(o, str) and isinstance(n, str) and o and n and o != n}
    out = {"ratings": 0, "play_stats": 0, "history": 0, "playlists": 0, "waveforms": 0}
    if not m:
        return out

    def _move(container: dict | None, key: str) -> set:
        # (Values are moved as they are — nested objects are never edited in
        # place: AOF replay rolls a failed record back from shallow copies,
        # persistence._entry_undo.)  Returns the old ids left where they are
        # (``keep_both``).
        if not container:
            return set()
        kept = ({o for o in m if o in container and m[o] in container}
                if keep_both else set())
        moving = {o: container.pop(o) for o in m if o in container and o not in kept}
        for o, v in moving.items():
            container[m[o]] = v
        out[key] += len(moving)
        return kept
    _move(ratings, "ratings")
    for uid in list(play_stats or ()):
        user_stats = play_stats[uid]
        kept = _move(user_stats, "play_stats") if isinstance(user_stats, dict) else set()
        user_hist = (history or {}).get(uid)
        if isinstance(user_hist, list):
            _move_history(user_hist, m, kept, out)
    for uid, user_hist in (history or {}).items():
        if uid not in (play_stats or {}) and isinstance(user_hist, list):
            _move_history(user_hist, m, set(), out)
    if playlists:
        for pid, pl in list(playlists.items()):
            if not isinstance(pl, dict):
                continue
            new_pl = None
            for key in ("track_ids", "tracks"):
                entries = pl.get(key)
                if not isinstance(entries, list):
                    continue
                hit = 0
                fresh = []
                for e in entries:
                    if isinstance(e, str) and e in m:
                        fresh.append(m[e])
                        hit += 1
                    elif isinstance(e, dict) and e.get("id") in m:
                        fresh.append({**e, "id": m[e["id"]]})
                        hit += 1
                    else:
                        fresh.append(e)
                if hit:
                    new_pl = new_pl or dict(pl)
                    new_pl[key] = fresh
                    out["playlists"] += hit
            if new_pl is not None:
                playlists[pid] = new_pl
    _move(waveforms, "waveforms")
    return out


def _move_history(history: list, m: dict, kept: set, out: dict) -> None:
    """Point one user's history entries at ``m``'s new ids (in place; an
    entry is replaced, never edited), except the old ids in ``kept``."""
    for i, e in enumerate(history):
        if isinstance(e, dict) and e.get("track_id") in m and e["track_id"] not in kept:
            history[i] = {**e, "track_id": m[e["track_id"]]}
            out["history"] += 1
