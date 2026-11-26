# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scene discovery — the Scene view's Discover list.

* **Compo winners across every party**: the tunes with a stored placing
  (``scene_place``, the Demozoo apply's), the ``place:`` / ``compo:`` search
  operators' tracks, listed newest party first, by competition and place.
* **On this day**: the tunes released at the parties held on today's month
  and day in past years (the party dates of the local Demozoo index,
  ``demozoo.party_calendar``; a date known to the month only counts no day).
* **Parties coming up**: the past entries of the party series whose next
  edition falls within the coming weeks — a dated edition the index lists,
  else (a series held in at least two of the last three years) the
  anniversary of its latest edition.  All from the index on disk: offline,
  no request.
* **Searches**: one-click presets of searches the search box already reads.

Nothing here writes; every list is the library's own tracks.
"""
from __future__ import annotations

import datetime
import re

# A party name's edition after its series name ("Revision 2019", "Assembly
# '94", "X 2008").
_EDITION_RE = re.compile(r"\s*(?:'\d{2}|\d{4}|\d{2})\s*$")


def presets(today: datetime.date) -> list[dict]:
    """``[{label, q}]`` — searches the search box reads (``api/search``
    docstring), newest-minded first."""
    y = today.year
    return [
        {"label": "New on Modland — the last 7 days", "q": "source:modland added:<7d"},
        {"label": "New in HVSC — the last 30 days", "q": "source:hvsc added:<30d"},
        {"label": "New in the online collections — the last 7 days",
         "q": "source:collection added:<7d"},
        {"label": f"Compo winners of {y}", "q": f"place:1 year:{y}"},
        {"label": f"Compo winners of {y - 1}", "q": f"place:1 year:{y - 1}"},
        {"label": "Compo winners you haven't played", "q": "place:1 plays:0"},
        {"label": "Amiga compo winners", "q": "place:1 chip:paula"},
        {"label": "C64 compo winners", "q": "place:1 chip:sid"},
        {"label": "SID tunes for two or three chips", "q": "chip:2sid,3sid"},
    ]


def parse_day(value: str | None, today: datetime.date) -> tuple[int, int]:
    """``"MM-DD"`` (the listener's own date) → ``(month, day)``; anything
    else → today's."""
    m = re.fullmatch(r"\s*(\d{1,2})-(\d{1,2})\s*", value or "")
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        try:
            datetime.date(2000, mo, d)                  # (a leap year: 02-29 reads)
            return mo, d
        except ValueError:
            pass
    return today.month, today.day


def held_on(p: dict, month: int, day: int) -> bool:
    """Was party ``p`` (``party_calendar`` row) on ``month``/``day`` — its
    first day, or any day up to its last (a party of at most two weeks)?"""
    start, end = p.get("start"), p.get("end")
    if start is None:
        return False
    if end is None or end < start or (end - start).days > 14:
        end = start
    d = start
    while d <= end:
        if (d.month, d.day) == (month, day):
            return True
        d += datetime.timedelta(days=1)
    return False


def parties_on(cal: list[dict], month: int, day: int, today: datetime.date) -> list[dict]:
    """The parties of past years held on ``month``/``day``, newest first."""
    out = [p for p in cal if p.get("start") is not None and p["start"].year < today.year
           and held_on(p, month, day)]
    out.sort(key=lambda p: (p["start"], p["name"]), reverse=True)
    return out


def series_key(p: dict) -> str:
    """A party's series: Demozoo's series id, else its name without the
    edition."""
    if p.get("series_id") is not None:
        return f"id:{p['series_id']}"
    base = _EDITION_RE.sub("", p.get("name") or "").strip()
    return "name:" + " ".join((base or p.get("name") or "").split()).casefold()


def series_name(eds: list[dict]) -> str:
    for p in eds:
        if p.get("series"):
            return p["series"]
    p = max(eds, key=lambda e: e.get("start") or datetime.date.min)
    return _EDITION_RE.sub("", p.get("name") or "").strip() or (p.get("name") or "")


def _anniversary(d: datetime.date, year: int) -> datetime.date:
    try:
        return d.replace(year=year)
    except ValueError:                                  # (29 February)
        return d.replace(year=year, day=28)


def upcoming(cal: list[dict], today: datetime.date, weeks: int) -> list[dict]:
    """``[{key, series, next, date, scheduled, past}]`` — the party series
    whose next edition starts within ``weeks`` weeks from ``today``: a dated
    edition the index lists (``scheduled``), else the anniversary of the
    latest one when the series was held in two of the last three years and
    lists no dated edition to come.  ``past``: the names of its editions
    before today.  Soonest first."""
    end = today + datetime.timedelta(weeks=max(1, weeks))
    by: dict[str, list] = {}
    for p in cal:
        by.setdefault(series_key(p), []).append(p)
    out = []
    for key, eds in by.items():
        dated = [p for p in eds if p.get("start") is not None]
        future = sorted((p for p in dated if p["start"] >= today), key=lambda p: p["start"])
        past = [p for p in eds if p.get("start") is None or p["start"] < today]
        if not past:
            continue
        nxt = None
        scheduled = False
        if future:
            if future[0]["start"] <= end:
                nxt, scheduled = future[0], True
        else:
            done = [p for p in dated if p["start"] < today]
            if done:
                latest = max(done, key=lambda p: p["start"])
                years = {p["start"].year for p in done if p["start"].year >= today.year - 3}
                if latest["start"].year >= today.year - 1 and len(years) >= 2:
                    ann = _anniversary(latest["start"], today.year)
                    if ann < today:
                        ann = _anniversary(latest["start"], today.year + 1)
                    if ann <= end:
                        nxt = {"name": "", "start": ann}
        if nxt is None:
            continue
        out.append({"key": key, "series": series_name(eds), "next": nxt.get("name") or "",
                    "date": nxt["start"].isoformat(), "scheduled": scheduled,
                    "past": sorted({p["name"] for p in past if p.get("name")})})
    out.sort(key=lambda s: (s["date"], s["series"].casefold()))
    return out


_PARTY_YEAR_RE = re.compile(r"(?<!\d)(19[7-9]\d|20\d{2})(?!\d)")


def party_year(t: dict) -> int:
    """The year of the party a tune was released at: the year its name
    holds ("Revision 2019"), else the tune's own year; 0 when neither."""
    m = _PARTY_YEAR_RE.search(t.get("scene_party") or "")
    if m:
        return int(m.group(1))
    y = t.get("year")
    try:
        y = int(y)
    except (TypeError, ValueError):
        return 0
    return y if 1970 <= y <= 2100 else 0


def party_order(store, ids) -> list[str]:
    """Party tunes newest party first: the party's year (``party_year``),
    the party, the competition (A–Z, no competition last), the place
    (unplaced last), the title."""
    tracks = store._tracks

    def key(i):
        t = tracks.get(i) or {}
        compo = (t.get("scene_compo") or "").strip()
        place = t.get("scene_place")
        return (-party_year(t), (t.get("scene_party") or "").casefold(), compo == "",
                compo.casefold(), place if isinstance(place, int) else 1 << 30,
                (t.get("title") or "").casefold(), i)
    return sorted((i for i in ids if i in tracks), key=key)


def party_ids(store, names) -> set:
    """The tracks released at any of the parties ``names`` (the Parties
    view's sets: exact names, as it counts them)."""
    from soniqboom.core.store import _facet_key
    sets = store._facet_sets("party")
    out: set = set()
    for n in names:
        hit = sets.get(_facet_key(n))
        if hit:
            out |= hit[1]
    return out
