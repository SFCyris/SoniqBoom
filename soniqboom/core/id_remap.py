# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Move a track's user data from its old id to a new one.

A track id is ``uuid5`` of its path, so a file that is moved or renamed gets
a new id (the Modland catalogue's changelog refresh, ``core/modland_catalog``).
Its rating, play count / last played, listening-history entries, playlist
entries and stored waveform follow it here.  One pure function over the
plain containers, shared by the live store (``TrackStore.remap_track_ids``)
and AOF replay / merging (``merger._apply_entry``, op ``remap_track_ids``) so
the two can't disagree.  Import-light on purpose: the merger process uses it.
"""
from __future__ import annotations


def remap_user_data(mapping: dict, *, ratings: dict | None, play_stats: dict | None,
                    history: list | None, playlists: dict | None,
                    waveforms: dict | None) -> dict:
    """Apply ``mapping`` (old id → new id) in place.  All the old ids are
    moved at once, so chains (a → b while b → c) and swaps land right.  An
    old id's rating, play stats or waveform replaces what the new id had
    (another file once at that path); where the old id has none, the new
    id's is left alone — so applying a mapping again (a redone move) changes
    nothing.  Returns per-container counts of entries moved; playlist
    entries keep their ``subsong``."""
    m = {o: n for o, n in (mapping or {}).items()
         if isinstance(o, str) and isinstance(n, str) and o and n and o != n}
    out = {"ratings": 0, "play_stats": 0, "history": 0, "playlists": 0, "waveforms": 0}
    if not m:
        return out

    def _move(container: dict | None, key: str) -> None:
        # (Values are moved as they are — nested objects are never edited in
        # place: AOF replay rolls a failed record back from shallow copies,
        # persistence._entry_undo.)
        if not container:
            return
        moving = {o: container.pop(o) for o in m if o in container}
        for o, v in moving.items():
            container[m[o]] = v
        out[key] += len(moving)
    _move(ratings, "ratings")
    _move(play_stats, "play_stats")
    if history:
        for i, e in enumerate(history):
            if isinstance(e, dict) and e.get("track_id") in m:
                history[i] = {**e, "track_id": m[e["track_id"]]}
                out["history"] += 1
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
