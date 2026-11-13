# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Search endpoints — full-text and filtered."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from soniqboom.api.tracks import json_bytes, json_route, public_track, public_tracks
from soniqboom.api.users import optional_listener_uid
from soniqboom.core import search_values
from soniqboom.core.data import (_parse_tag_query, and_predicates, ft_search, ft_search_dicts,
                                  get_track, search_indexes_for, with_uid)
from soniqboom.core.store import _tokenize_text
from soniqboom.models.track import TrackMeta

router = APIRouter(prefix="/search", tags=["search"])

_TAG_ESC_RE = re.compile(r'([,.<>{}\[\]"\'`:;!@#$%^&*()\-+=~\\|/ ])')


def _esc_tag(value: str) -> str:
    """Escape a string for use inside a tag query: @field:{value}."""
    return _TAG_ESC_RE.sub(r'\\\1', value)


# The numeric range operators: ``year:``, ``track:``, ``disc:`` (``disk:`` is
# ``disc:``), ``length:``, ``rating:``, ``plays:``, ``bitdepth:``,
# ``samplerate:``, ``bitrate:``, ``bpm:``, ``tunes:``, ``channels:`` — and the
# time specs of ``added:`` / ``played:``.
_RANGE_FIELDS = (r'(?:year|track|disc|disk|length|rating|plays|added|played'
                 r'|bitdepth|samplerate|bitrate|bpm|tunes|channels)')

# Spaces inside a numeric filter — ``year: 2000``, ``year:< 2000``,
# ``track: >= 3``, ``track: -3``, ``year:1990 - 2000``, ``length:3:00 - 4:00``
# — joined up, or the value after the space became free text and the filter
# filtered nothing.
# Only where the operator name starts a token (or follows an exclusion's
# ``-``): not inside ``multi-track: 2``.
_RANGE_OP_SPACE_RE = re.compile(
    r'(?<!\w)(?<!\w-)(' + _RANGE_FIELDS + r'):\s*([<>]=?|-)?\s*(?=\d)', re.IGNORECASE)
_RANGE_RANGE_SPACE_RE = re.compile(
    r'(?<!\w)(?<!\w-)(' + _RANGE_FIELDS
    + r':[\d:.\-]*\d(?:khz|kbps|hz|[smhk])?)(?:\s+-\s*|\s*-\s+)(?=\d)',
    re.IGNORECASE)
# A unit after a space — ``samplerate:44.1 khz``, ``bitrate:128 kbps``,
# ``samplerate:44.1khz-96 khz`` — joined to its number.
_UNIT_SPACE_RE = re.compile(
    r'(?<!\w)(?<!\w-)((?:samplerate|bitrate):\S*?\d)\s+(khz|kbps|hz|k)\b', re.IGNORECASE)
_QUOTED_RE = re.compile(r'("[^"]*")')


def _tighten_ranges(q: str) -> str:
    """``q`` with the spaces inside its numeric / time values removed
    (``track: >= 3`` → ``track:>=3``, ``track: -3`` → ``track:-3``,
    ``year:1990 - 2000`` → ``year:1990-2000``, ``bitrate:128 kbps`` →
    ``bitrate:128kbps``) — outside quoted values only
    (``album:"back on track: 2"`` stays as typed)."""
    pieces = _QUOTED_RE.split(q)
    for i in range(0, len(pieces), 2):                  # (odd pieces: the quoted ones)
        p = _RANGE_OP_SPACE_RE.sub(lambda m: m.group(1) + ':' + (m.group(2) or ''), pieces[i])
        p = _UNIT_SPACE_RE.sub(r'\1\2', p)
        p = _RANGE_RANGE_SPACE_RE.sub(r'\1-', p)
        pieces[i] = _UNIT_SPACE_RE.sub(r'\1\2', p)
    return "".join(pieces)


# A numeric value ``_range_tag`` reads: ``N``, ``>N``, ``<N``, ``>=N``, ``<=N``,
# ``A-B``, ``A-``, ``-B``.
# (At most 9 digits a number: a longer one is no number.)
_NUM_RANGE_RE = re.compile(r'(?:[<>]=?)?\d{1,9}|\d{1,9}-\d{0,9}|-\d{1,9}')


def _plain_number(value: str) -> str | None:
    """A ``year:`` / ``rating:`` / ``plays:`` value as ``_range_tag`` reads
    it, or None when it is no number or range (the query keeps it as free
    text: ``year:abc``)."""
    v = value.replace(' ', '')
    return v if _NUM_RANGE_RE.fullmatch(v) else None


def _number_value(value: str) -> str | None:
    """A ``track:`` / ``disc:`` value as ``_range_tag`` reads it — ``3/12``
    (as tags show a track number) reads ``3`` — or None when it is no number
    or range (``track:remix``: the query keeps it as free text)."""
    v = value.replace(' ', '')
    m = re.fullmatch(r'(\d+)/\d*', v)
    if m:
        v = m.group(1)
    return v if _NUM_RANGE_RE.fullmatch(v) else None


def _range_tag(field: str, value: str) -> str:
    """A numeric value as the tag range ``@field:[lo hi]`` (``(`` marks an
    exclusive bound, ``-inf`` / ``+inf`` an open end): ``N``, ``>N``, ``<N``,
    ``>=N``, ``<=N``, ``A-B``, ``A-``, ``-B``."""
    v = value.replace(' ', '')
    if v.startswith('>='):
        return f"@{field}:[{v[2:]} +inf]"
    if v.startswith('<='):
        return f"@{field}:[-inf {v[2:]}]"
    if v.startswith('>'):
        return f"@{field}:[({v[1:]} +inf]"
    if v.startswith('<'):
        return f"@{field}:[-inf ({v[1:]}]"
    if '-' in v:                                    # A-B, A- (from A), -B (up to B)
        lo, hi = v.split('-', 1)
        return f"@{field}:[{lo or '-inf'} {hi or '+inf'}]"
    return f"@{field}:[{v} {v}]"


# The search box's operator names (``_parse_advanced_query``), lower case.
_OPERATORS = frozenset(("artist", "album_artist", "album", "game", "genre", "year",
                        "track", "disc", "disk", "format", "type",
                        "length", "added", "rating", "plays", "played",
                        "folder", "path", "group", "party", "status", "composer",
                        "title", "lossless", "bitdepth", "samplerate", "bitrate",
                        "source", "bpm", "tunes", "chip", "dup", "channels", "has"))
# Any operator name followed by ``:`` (a negated one, ``-format:``, too).
_OPERATOR_RE = re.compile(
    r'\b(' + '|'.join(sorted(_OPERATORS, key=len, reverse=True)) + r'):', re.IGNORECASE)
# ``field:value`` / ``field:"quoted value"`` / ``field:a,"b c"`` (several
# values — ``search_values.split_values``) — with a ``-`` at the start of a
# token (``-format:mp3``, not ``x-format:mp3``): an exclusion.  The value runs
# to the first space outside quotes — but after a comma the next word is the
# next value (``format:flac, alac``) unless it is an exclusion (``-live``);
# an unclosed quote: to the first space.
_VALUE_RUN = r'(?:"[^"]*"|[^\s"])+'
_TERM_RE = re.compile(r'(?:(?<!\S)(-))?(\w+):(' + _VALUE_RUN
                      + r'(?:(?<=,)\s+(?!-[^\W\d_])' + _VALUE_RUN + r')*|\S+)')
# Operator terms and the free text's quoted runs, left to right: a term inside
# a quoted run of the free text (``"song year:1999 remaster"``) is part of the
# phrase, not an operator.
_SCAN_RE = re.compile(_TERM_RE.pattern + r'|(?<!\S)-?"[^"]*"?')
# The free text's words: whitespace-separated, a quoted run kept whole
# (``-"live at"`` is one word).
_WORD_RE = re.compile(r'(?:[^\s"]|"[^"]*"?)+')


def _excluded_word(word: str) -> str | None:
    """The word a free-text ``word`` excludes — ``-live`` → ``live``,
    ``-"live at"`` → ``live at`` — or None when it is no exclusion (no
    leading ``-``, ``-`` / ``--x``, or nothing to search after the dash:
    ``-!!!``)."""
    if len(word) < 2 or word[0] != '-' or word[1] == '-':
        return None
    rest = word[1:].replace('"', ' ').strip()
    return rest if any(ch.isalnum() for ch in rest) else None


def _quoted_phrase(word: str) -> str | None:
    """The phrase of a free-text word that is one quoted run of two or more
    words (``"song of myself"``; an unclosed quote runs to the end — the
    search box while typing), else None."""
    m = re.fullmatch(r'"([^"]*)"?', word)
    if not m:
        return None
    inner = " ".join(m.group(1).split())
    return inner if len(_tokenize_text(inner)) >= 2 else None


def _has_phrase(q: str) -> bool:
    return '"' in q and any(_quoted_phrase(w.lstrip("-")) for w in _WORD_RE.findall(q))


def _has_exclusion(q: str) -> bool:
    """Does ``q`` hold a free-text exclusion (``-live``) or an excluded
    operator term (``-format:mp3``)?"""
    return any(_excluded_word(w) is not None for w in _WORD_RE.findall(q))


def _escape_text(text: str) -> str:
    """Free text as the tag query carries it: ``-``, ``:`` and ``/``
    backslash-escaped (``_parse_tag_query`` reads them back)."""
    return text.replace("-", "\\-").replace(":", "\\:").replace("/", "\\/")


# Operators whose value is text matched as is (a word start / a substring):
# the tag field has the operator's name.
_TEXT_OPERATORS = frozenset(("folder", "path", "group", "party", "composer", "title"))


# Operators that take a word of a fixed list → that list.
_WORD_OPERATORS = {"status": search_values.STATUSES, "source": search_values.SOURCES,
                   "chip": search_values.CHIPS, "has": search_values.HAS}


def _operator_term(field: str, value: str) -> str | None:
    """The tag term of operator ``field`` (lower case) with ``value``, or None
    when the value is not one the operator reads (it stays free text)."""
    if field == 'artist':
        # artist: / album_artist: / album: match a word start in the name
        # ("Marcin" finds "Marcin Romanowski (Sidder)"); an artist's or an
        # album's page stays exact.  artist: reads the album artist too
        # (``store._candidate_ids``).
        return f"@artist_match:{{{_esc_tag(value)}}}"
    if field == 'album_artist':
        return f"@album_artist_match:{{{_esc_tag(value)}}}"
    if field == 'album':
        return f"@album_match:{{{_esc_tag(value)}}}"
    if field == 'game':
        return f"@game_tag:{{{_esc_tag(value)}}}"
    if field == 'genre':
        # (a word start; the genre drill-down — ``/search/filter?genre=`` —
        # stays exact)
        return f"@genre_match:{{{_esc_tag(value)}}}"
    if field in _TEXT_OPERATORS:
        return f"@{field}:{{{_esc_tag(value)}}}"
    if field in _WORD_OPERATORS:
        v = search_values.word_of(value, _WORD_OPERATORS[field])
        return None if v is None else f"@{field}:{{{v}}}"
    if field in ('lossless', 'dup'):
        v = search_values.yes_no(value)
        return None if v is None else f"@{field}:{{{v}}}"
    if field in ('bitdepth', 'bpm', 'tunes', 'channels'):
        v = _plain_number(value)
        return None if v is None else _range_tag(field, v)
    if field == 'samplerate':
        v = search_values.number_value(value, search_values.RATE_UNITS, small_unit="k")
        return None if v is None else _range_tag(field, v)
    if field == 'bitrate':
        v = search_values.number_value(value, search_values.KBPS_UNITS)
        return None if v is None else _range_tag(field, v)
    if field in ('format', 'type'):
        return f"@format:{{{_esc_tag(value.upper())}}}"
    if field in ('year', 'rating', 'plays'):
        v = value
        if field == 'rating' and value.strip().lower() == 'unrated':
            v = '0'
        v = _plain_number(v)
        return None if v is None else _range_tag(field, v)
    if field in ('track', 'disc', 'disk'):
        v = _number_value(value)
        return None if v is None else _range_tag('disc' if field == 'disk' else field, v)
    if field == 'length':
        v = search_values.length_value(value)
        return None if v is None else _range_tag('length', v)
    if field in ('added', 'played'):
        v = search_values.when_value(value)
        return None if v is None else f"@{field}:{{{_esc_tag(v)}}}"
    return None


def _value_terms(field: str, raw: str, negated: bool) -> str | None:
    """The tag text of operator token ``field:raw`` (``negated``: it had a
    leading ``-``) — one term per value (``search_values.split_values``):
    a single value its term; several a group ``(t1 | t2)`` (any of them), or,
    negated, one exclusion each (``-t1 -t2``).  None when ``field`` is no
    operator, the token holds no value, or a value is not one the operator
    reads (the token stays free text)."""
    if field not in _OPERATORS or not raw:
        return None
    items = search_values.split_values(raw)
    if not items:
        return None
    out: list[str] = []
    for item in items:
        t = _operator_term(field, item)
        if t is None:
            return None
        if t not in out:
            out.append(t)
    if negated:
        return " ".join("-" + t for t in out)
    return out[0] if len(out) == 1 else "(" + " | ".join(out) + ")"


def _parse_advanced_query(q: str) -> str | None:
    """Parse advanced search syntax like artist:Ghost album:Impera year:>2020.

    Returns a tag-filter query string, or None if the input is plain text
    (no operator, no ``-`` exclusion, no quoted phrase).  Supported syntax:
      artist:VALUE, album_artist:VALUE, album:VALUE — a word start in the
      name (artist: reads the album artist or the artist; album:imagin finds
      "Imaginaerum"),
      game:VALUE (case-insensitive PREFIX of the track's game or one of its
      other names — ``TrackMeta.game`` / ``game_aliases`` — or, for a C64
      SID / Atari ST tune whose game is unknown or only guessed from its
      folder / a Modland file name (not typed or cleared by the user), of its
      title, where HVSC rips name the game: ``game:uridium`` finds
      "Uridium 2"; a leading "The", accents and runs of whitespace are
      ignored, so ``game:"last ninja"`` also finds "The Last Ninja" and
      ``game:pokemon`` finds "Pokémon"),
      genre:VALUE (a word start in a genre the track is listed under:
      genre:metal finds "Heavy Metal"; the genre's own page stays exact),
      year:VALUE, year:>VALUE, year:<VALUE, year:>=VALUE, year:<=VALUE,
      year:VALUE-VALUE, year:VALUE-, year:-VALUE (spaces around the
      operator or the dash are allowed),
      track:VALUE and disc:VALUE (also disk:VALUE) — the track / disc number,
      in the same forms as year: (track:3, track:1-5, track:>10, disc:2;
      track:3/12 reads track:3); a track without that number never matches,
      format:VALUE (also type:VALUE) — the list's Type column: an exact
      format name; else a file extension (format:mod → ProTracker,
      format:m4a → AAC and ALAC; format:dts → DTS and DTS-HD, format:dolby →
      AC-3, E-AC-3 and TrueHD); else a family (tracker / trackers,
      chiptune, lossless, lossy, other — the library Galaxy's families),
      length:VALUE — the length in whole seconds, in the forms of year:,
      each length written 90, 90s, 5m, 1h, 1:30 or 1:02:03 (length:>5:00,
      length:<30s, length:3:00-4:00); a track of unknown length never
      matches,
      added:VALUE — when the track was added: within the last N hours /
      days / weeks / months (30 days) / years (365 days) as <Nh <Nd <Nw <Nm
      <Ny (or Nd alone), longer ago as >Nd; or a calendar year / month / day
      (added:2026, added:2026-10, added:2026-10-23, server local time)
      alone, as a range (added:2026-01-2026-06, added:2026-, added:-2025)
      or with > < >= <= (added:>2026-06-01 = after that day),
      rating:VALUE — the star rating 0–5 in the forms of year:; an unrated
      track counts as 0 (rating:0 or rating:unrated lists them),
      plays:VALUE — how often the signed-in user played the track, in the
      forms of year: (plays:0 = never),
      played:VALUE — when the signed-in user last played it, in the forms
      of added: (played:<7d, played:2026-10),
      folder:VALUE — a word start in the track's folder path (folder:itunes,
      folder:"music/itunes"); path:VALUE — text anywhere in its whole path,
      file name and a share's address included (path:.flac); a share's
      user name and password are never part of either,
      group:VALUE / party:VALUE — a word start in a scene group / in the
      demoparty a tune was released at,
      status:VALUE — broken, glitched (a listener's mark), corrupt, partial,
      unplayable, empty (found at scan or play), cantplay (broken, corrupt or
      unplayable: what "Tracks that can't be played" covers),
      composer:VALUE / title:VALUE — a word start in the composer / the title,
      lossless:yes / lossless:no (also y / n, true / false, 1 / 0) — the
      lossless badge,
      bitdepth:VALUE, samplerate:VALUE (Hz; 44.1k, 44.1khz and — a number
      under 1000 — 44.1 = 44100), bitrate:VALUE (kbps, rounded; 320k,
      320kbps = 320) — in the forms of year:; a unit may follow a space;
      unknown never matches,
      source:VALUE — hvsc, aminet, sceneorg (scene.org), modland (an online
      collection's tracks), ftp, smb, webdav (a network share's), share
      (any network share), collection (any online collection), local,
      bpm:VALUE (rounded), tunes:VALUE (tunes of a file; 1 without tune
      data), channels:VALUE — in the forms of year:; no BPM never matches,
      chip:6581 / chip:8580 (a SID tune's chip model; "6581/8580" matches
      both), chip:2sid / chip:3sid (a SID file with two / three chips),
      dup:yes / dup:no — in a duplicate group (of two or more tracks) or not,
      has:stil / has:game — HVSC STIL text, a game name.
    A quoted run of two or more words in the free text is a phrase:
    "song of myself" finds those words next to each other, in order, in one
    field (title, artist, album artist, album, composer, scene group, game
    and its other names — the last word may be a word's start; an operator
    inside the quotes is part of the phrase); -"live at" leaves that phrase
    out.
    Several values, separated by commas, match any of them:
    format:flac,alac, format:flac, alac, genre:metal,"hard rock" (a quoted
    value keeps its commas); -format:mp3,aac excludes each.  The same
    operator twice must match both (folder:music folder:live).
    A value an operator can't read (year:abc, track:remix, length:long,
    status:fine, one value of several) stays free text (the whole term).  A ``-`` at the start of a term excludes what the term
    matches: an operator (-format:mp3, -genre:podcast) or a free-text word
    (nightwish -live: a word starting "live"; -"live at": both words); a
    query of exclusions only lists every track but those.  A ``-`` inside a
    word (a-ha, x-ray) or a value (year:-1990), ``--x`` and a dash before
    nothing searchable (-!!!) are no exclusion.  A number has at most 9
    digits (a longer value stays free text).  Values can be quoted: artist:"The Ghost Inside".
    Operator names are read in any case (``Artist:``).  A ``word:value``
    whose word is no operator stays free text.
    """
    if not _OPERATOR_RE.search(q) and not _has_exclusion(q) and not _has_phrase(q):
        return None
    q = _tighten_ranges(q)

    parts: list[str] = []
    pieces: list[str] = []
    at = 0
    for m in _SCAN_RE.finditer(q):
        if m.group(2) is None:                  # (a quoted run of the free text)
            continue
        term = _value_terms(m.group(2).lower(), m.group(3), bool(m.group(1)))
        if term is not None:
            parts.append(term)
            pieces.append(q[at:m.start()])
            at = m.end()
    pieces.append(q[at:])

    # The free text: everything that is no operator term — its words with a
    # leading ``-`` (``-live``, ``-remix:2020``) exclude, the rest match.
    remainder = "".join(pieces).strip()
    if remainder:
        # (a quoted run of two or more words is a phrase: those words next to
        # each other in one field — they stay in the free text, which ranks)
        words = _WORD_RE.findall(remainder)
        negs = [w for w in words if _excluded_word(w) is not None]
        for w in negs:
            phrase = _quoted_phrase(w[1:])
            parts.append(f"-@phrase:{{{_esc_tag(phrase)}}}" if phrase else
                         f"-@text:{{{_esc_tag(_excluded_word(w))}}}")
        if negs:
            remainder = " ".join(w for w in words if _excluded_word(w) is None)
        for w in _WORD_RE.findall(remainder):
            phrase = _quoted_phrase(w)
            if phrase:
                parts.append(f"@phrase:{{{_esc_tag(phrase)}}}")
        if remainder:
            parts.append(_escape_text(remainder))

    return " ".join(parts) if parts else None


def search_predicates(q: str, uid: str | None = None) -> dict:
    """The search box's text ``q`` (advanced field operators or plain text) as
    ``TrackStore.filter_tracks`` predicates — the one parse path of the search
    lists (``/search``, ``/search/quick``, ``/search/page``, the search
    shuffle ``/api/tracks/shuffled?q=``), identical to :func:`run_search`'s.
    ``uid``: whose plays ``plays:`` / ``played:`` read — added to the
    predicates (``uid``) only when the query has one of them
    (``data.with_uid``)."""
    advanced = _parse_advanced_query(q)
    query = advanced or _escape_text(q)
    return with_uid(_parse_tag_query(query), uid)


async def run_search(q: str, limit: int = 50, uid: str | None = None) -> list[TrackMeta]:
    """Evaluate a query (advanced field-operators or plain text) → tracks.

    Shared by the ``/search`` endpoint and smart-playlist evaluation so the two
    can never drift in how they interpret ``artist:…  year:>2020  format:SID``.
    ``uid``: whose plays ``plays:`` / ``played:`` read (the viewer's).
    """
    advanced = _parse_advanced_query(q)
    return await ft_search(advanced or _escape_text(q), limit=limit, uid=uid)


async def run_search_dicts(q: str, limit: int = 50, exclude=None, swap=None,
                           uid: str | None = None) -> list[dict]:
    """Dict-returning twin of :func:`run_search` for the ``/search`` endpoint —
    serializes with orjson and skips the TrackMeta round-trip (see
    ``data.ft_search_dicts``).  ``run_search`` (models) stays for smart-playlist
    evaluation so the two never drift on query parsing.  ``exclude``: ids the
    result leaves out (``core.availability.hidden``); ``swap``: the duplicate
    groups listed by another member (``core.availability.primary_swap``);
    ``uid``: whose plays ``plays:`` / ``played:`` read."""
    advanced = _parse_advanced_query(q)
    return await ft_search_dicts(advanced or _escape_text(q), limit=limit, exclude=exclude,
                                 swap=swap, uid=uid)


def _uid_of(uid) -> str | None:
    """The ``optional_listener_uid`` dependency's value — None when a handler
    is called directly without one (its default is the ``Depends`` marker)."""
    return uid if isinstance(uid, str) else None


@router.get("")
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
    uid: str | None = Depends(optional_listener_uid),
):
    """Full-text search across artist, album, and title — the first ``limit``
    matches, newest first (``/search/page?sort=added``, which pages all of
    them; the web lists read ``/search/page``, Best match first).
    Also supports advanced syntax: artist:Ghost album:Impera year:>2020

    Tracks that can't play right now are flagged (``offline``) or, with
    "Tracks on unavailable drives: Hide", left out (``core.availability``);
    with duplicates hidden, a group whose primary can't play is listed by its
    best playable member.
    """
    from soniqboom.core import availability
    dicts = await run_search_dicts(q, limit=limit, exclude=availability.hidden(),
                                   swap=availability.primary_swap(), uid=_uid_of(uid))
    dicts = availability.mark_rows(dicts)
    return Response(content=json_bytes(dicts), media_type="application/json")


@json_route(router, "/quick")
async def quick_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(8, ge=1, le=20),
    uid: str | None = Depends(optional_listener_uid),
):
    """Lightweight search for autocomplete preview — TrackMeta-shaped rows
    (``tracks.public_track``), offline ones flagged or hidden as in ``/search``.
    (The web app's preview reads ``/search/page?limit=8`` — the full results'
    own rows and total.)"""
    from soniqboom.core import availability
    rows = public_tracks(await run_search_dicts(q, limit=limit,
                                                exclude=availability.hidden(),
                                                swap=availability.primary_swap(),
                                                uid=_uid_of(uid)))
    return availability.mark_rows(rows)


# ``sort`` keys a result set can be ordered by (``TrackStore._SORT_INDEX_MAP``,
# the keys ``GET /api/tracks`` takes, and ``rank`` — Best match); anything
# else is the default order.
_PAGE_SORT_KEYS = frozenset(("added", "year", "duration", "bpm", "title", "artist",
                             "album_artist", "album", "format", "rank"))


@json_route(router, "/page")
async def search_page(
    q: str = Query(..., min_length=1),
    rs: str | None = Query(None, max_length=64),
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=5000),
    sort: str | None = None,
    order: str | None = None,
    fields: str | None = None,
    playable: bool = False,
    after: str | None = None,
    rank_by: str | None = None,
    request: Request = None,
    uid: str | None = Depends(optional_listener_uid),
):
    """One page of a search's whole result set (``core/search_sets``).

    The first request (no ``rs``) builds the set — every match — and answers
    its first page with the exact ``total`` and the set's handle ``rs``; later
    pages name it.  ``sort`` (``added`` ``year`` ``duration`` ``bpm``
    ``title`` ``artist`` ``album_artist`` ``album`` ``format``) + ``order``
    (``asc`` / ``desc``) order the WHOLE set by the sorted index All Tracks'
    column sort uses (same keys, ties by id); ``added`` is newest first
    (``/search``'s order).  Without ``sort`` (or ``sort=rank``) the default
    order, Best match (``core/search_rank``; ``order`` does not apply) — or,
    for a search without free text (only field operators), newest first;
    ``ranked`` in the reply says which.  An ``artist:`` search without free
    text lists its album-artist matches first, then the others, each newest
    first (``search_sets.FIRST``; ``artist_first`` in the reply).
    ``rank_by`` (``title`` ``artist`` ``album`` ``genre`` ``game`` — the
    phone's scope keys, ``core/search_rank.SCOPES``): a search without free
    text that has values of that operator (``title:"space"``) is listed Best
    match for those values, judged on that operator's fields only (the
    matches are the same; ``rank_by`` in the reply names the scope, else
    None); with free text, without such a value, or another key it changes
    nothing.  A handle that no longer holds — the
    library changed, Hide or "Hide duplicates" changed, the server restarted
    — is answered from a fresh set for ``q``: the reply then carries the new
    ``rs`` and ``renewed: true`` (rows of the two sets don't mix).

    ``fields=list``: rows without the fields no list reads
    (``search_sets.LIST_DROP``).  Rows are flagged ``offline`` as in
    ``/search``.  ``playable=1`` (the play queue): ``{tracks, next_offset,
    ended}`` as ``GET /api/tracks?playable=1`` answers it (plus ``rs``,
    ``total``, ``renewed``), each row with its position ``_pos``;
    ``after=<ids>`` starts right after that track.

    ``plays:`` / ``played:`` read the signed-in user's plays (``uid`` — in
    the set's key, so users never share a set).

    A page of at least ``search_sets.FINISH_MIN_LIMIT`` rows (a list's) also
    starts reading the rest of its order in the background.  A request whose
    client went away while its order was read answers 499 (what was read
    stays with the set).

    Reply: ``{rs, total, offset, sort, order, ranked, artist_first, rank_by,
    renewed, tracks}``."""
    from soniqboom.core import availability, search_sets
    from soniqboom.core.store import get_store
    store = get_store()
    preds = search_predicates(q, _uid_of(uid))
    await search_indexes_for(preds)
    sort_by = sort if sort in _PAGE_SORT_KEYS else None
    if sort_by == search_sets.RANK:
        sort_by = None                  # (the default order, by name)
    sort_order = order if order in ("asc", "desc") else None
    desc = store._is_descending(sort_by, sort_order)
    gone = request.is_disconnected if request is not None else None
    slim = fields == "list"
    renewed = False
    for attempt in range(search_sets.RETRIES + 1):
        # A long read gives the event loop a turn between slices and starts
        # over when the set stopped holding meanwhile; the last attempt reads
        # without a turn, so a library that keeps changing (a scan writing on
        # every turn of the loop) still gets an answer.  (Best match scores
        # every member: its last attempt keeps the turns and answers from the
        # set it started in — the next page renews it.)
        last = attempt == search_sets.RETRIES
        state = search_sets.State(store)
        rset, again = search_sets.resolve(store, preds, rs=rs, sort_by=sort_by, state=state,
                                          rank_by=rank_by)
        renewed = renewed or again
        attr = rset.attr_for(store, sort_by)
        ranked = attr == search_sets.RANK
        sync = last and not ranked
        state_of = None if last else (lambda: search_sets.State(store))     # noqa: E731
        try:
            if playable:
                # (an anchor and a playable page read on past the rows they
                # skip: the order is finished first)
                ids = (search_sets.complete_sync(store, rset, attr, desc) if sync else
                       await search_sets.complete(store, rset, attr, desc, state_of, gone))
                start = offset
                if after:
                    at = search_sets.anchor(ids, after)
                    if at is not None:
                        start = at + 1

                def fetch(off: int, lim: int, ids=ids) -> list:
                    return search_sets.rows(store, ids[off:off + lim])
                page = availability.player_page(fetch, start, limit, store)
                if slim:
                    page["tracks"] = [{k: v for k, v in r.items()
                                       if k not in search_sets.LIST_DROP}
                                      for r in page["tracks"]]
                page.update(rs=rset.rs, total=rset.total, renewed=renewed, ranked=ranked,
                            artist_first=attr == search_sets.FIRST,
                            rank_by=rset.scope if ranked else None)
                return page
            ids = (search_sets.page_ids_sync(store, rset, attr, desc, offset, limit) if sync else
                   await search_sets.page_ids(store, rset, attr, desc, offset, limit, state_of, gone))
        except search_sets.Gone:
            return Response(status_code=499)
        except search_sets.Stale:
            rs = rset.rs if rs else None
            renewed = renewed or bool(rs)
            continue
        if limit >= search_sets.FINISH_MIN_LIMIT:            # (a list page, not a preview)
            search_sets.schedule_finish(store, rset, attr, desc)
        return {"rs": rset.rs, "total": rset.total, "offset": offset, "sort": sort_by,
                "order": None if ranked else ("desc" if desc else "asc"), "ranked": ranked,
                "artist_first": attr == search_sets.FIRST,
                "rank_by": rset.scope if ranked else None,
                "renewed": renewed, "tracks": search_sets.rows(store, ids, slim=slim)}


@json_route(router, "/filter")
async def filter_tracks(
    artist: str | None = None,
    album_artist: str | None = None,
    album: str | None = None,
    genre: str | None = None,
    scene_group: str | None = None,
    composer: str | None = None,
    game: str | None = None,
    platform: str | None = None,
    party: str | None = None,
    compo: str | None = None,
    format: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    track_min: int | None = None,
    track_max: int | None = None,
    disc_min: int | None = None,
    disc_max: int | None = None,
    length_min: int | None = None,
    length_max: int | None = None,
    rating_min: int | None = None,
    rating_max: int | None = None,
    plays_min: int | None = None,
    plays_max: int | None = None,
    added: str | None = None,
    played: str | None = None,
    q: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    playable: bool = False,
    after: str | None = None,
    uid: str | None = Depends(optional_listener_uid),
):
    """Structured filter — combine any fields.

    The numeric ranges (``year_*``, ``track_*``, ``disc_*``, ``length_*`` in
    whole seconds, ``rating_*``, ``plays_*`` — the signed-in user's) are
    inclusive; ``added`` / ``played`` take the search box's time specs
    (``<30d``, ``2026-10`` — ``core/search_values``).  ``composer`` /
    ``game`` / ``platform`` / ``party``: one entry of a scene browse view
    (``/library/composers`` …), matched exactly as the view counts it
    (``store.FACETS``); ``compo`` (with ``party``): one competition of it.  ``q``: search-box text
    (any operator, ``search_predicates``): a track must match it and the
    other fields (the same field in both: both values).

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

    # ``scene_group`` (the Demozoo browse facet) and the scene browse views
    # are NOT full-text fields, so resolve them — and any co-filters — straight
    # against the store's maintained indexes rather than the FT engine.
    if scene_group or composer or game or platform or party:
        # (``q``'s predicates and the explicit fields: a track matches both)
        extra = and_predicates(search_predicates(q) if q else {}, {k: v for k, v in (
            ("artist", artist), ("album_artist", album_artist), ("album", album),
            ("genre", genre), ("scene_group", scene_group), ("format_", format),
            ("composer_is", composer), ("game_is", game), ("platform", platform),
            ("party_is", party), ("compo_is", compo if party else None),
            ("year_min", year_min), ("year_max", year_max),
            ("track_min", track_min), ("track_max", track_max),
            ("disc_min", disc_min), ("disc_max", disc_max),
            ("length_min", length_min), ("length_max", length_max),
            ("rating_min", rating_min), ("rating_max", rating_max),
            ("plays_min", plays_min), ("plays_max", plays_max),
            ("added", added), ("played", played)) if v is not None and v != ""})
        extra = with_uid(extra, _uid_of(uid))
        await search_indexes_for(extra)
        hide = bool(store.get_config("filter_duplicates", False))
        if party:
            # A party's tracks in competition order: each compo (A–Z, the
            # tracks of no known compo last), its entries by place (unplaced
            # last), then by title — the Scene view groups them by compo.
            def _party_order(off, lim):
                ids = [i for i in store.filter_track_ids(filter_duplicates=hide, swap=swap, **extra)
                       if not (excl and i in excl)]

                def key(i):
                    t = store._tracks.get(i) or {}
                    compo = (t.get("scene_compo") or "").strip()
                    place = t.get("scene_place")
                    return (compo == "", compo.casefold(),
                            place if isinstance(place, int) else 1 << 30,
                            (t.get("title") or "").casefold(), i)
                ids.sort(key=key)
                return [store._meta_dict(i) for i in ids[off:off + lim]]
            return await _reply(_party_order)
        return await _reply(lambda off, lim: store.filter_tracks(
            **extra, limit=lim, offset=off,
            # Honour the "hide duplicates" config so the drill-down LIST matches
            # the deduped browse COUNT (every FT-routed facet already does this).
            filter_duplicates=hide, exclude=excl, swap=swap))
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
    for name, lo, hi in (("track", track_min, track_max), ("disc", disc_min, disc_max),
                         ("length", length_min, length_max),
                         ("rating", rating_min, rating_max),
                         ("plays", plays_min, plays_max)):
        if lo is not None or hi is not None:
            parts.append(f"@{name}:[{'-inf' if lo is None else lo} "
                         f"{'+inf' if hi is None else hi}]")
    for name, spec in (("added", added), ("played", played)):
        if spec:
            parts.append(f"@{name}:{{{_esc_tag(spec)}}}")
    if q:
        parts.append(_parse_advanced_query(q) or _escape_text(q))
    query = " ".join(parts) if parts else "*"
    parsed = with_uid(_parse_tag_query(query), _uid_of(uid))
    await search_indexes_for(parsed)
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
