# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Playlist files: M3U / M3U8 and XSPF, written from a playlist and read
into one (``api/playlist.py``: ``GET /playlists/{id}/export``,
``POST /playlists/import``).

Export writes each entry's location as another player can open it: a local
file's path (XSPF: its ``file://`` URI), a network-share file's URL without
any user name or password, an archive member as ``archive::member``.  A tune
of a multi-tune file carries its number (``#EXTSB-TUNE:N`` in M3U, a ``meta``
in XSPF — other players skip both), and SoniqBoom's own stored path rides
along in XSPF (``meta rel="urn:soniqboom:path"``) so a file read back here
finds the same tracks.

Import matches each entry to a library track, in this order:

1. ``path`` — the same stored path (a file this server wrote, or a library
   on the same disks);
2. ``folder`` — the same file name and the most trailing folders in common
   (a library moved to another disk or mount, a Windows path, a path
   relative to the playlist file), the entry's length — when it has one —
   within 5 s;
3. ``tags`` — the same artist and title (case and spacing ignored), the
   length within 5 s when both have one.

Several tracks equally close: a duplicate's primary copy first, then a
library file before an online-collection tune; still several, and nothing
tells them apart — the entry is not matched (never a guess).  Entries that
name a web stream (``http://…`` that is no track of a share) are reported,
not matched.

Parsing and matching are pure functions over a snapshot of the library
(``LibraryRow``): the caller takes the snapshot on the event loop (the
store is not thread-safe) and may run the rest in a thread."""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import quote, unquote

MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_ENTRIES = 20_000
# The entries a report lists by name (the rest are counted).
REPORT_MAX = 200
_LENGTH_SLACK_S = 5.0

XSPF_NS = "http://xspf.org/ns/0/"
META_PATH = "urn:soniqboom:path"
META_TUNE = "urn:soniqboom:tune"
_M3U_TUNE = "#EXTSB-TUNE:"


# ── Entries ──────────────────────────────────────────────────────────────────

@dataclass
class Entry:
    """One entry of a playlist file as read: where it points, what it says
    about the track, and its place in the file (for the report)."""
    location: str = ""
    title: str = ""
    artist: str = ""
    duration: float | None = None
    tune: int | None = None            # 1-based tune of a multi-tune file
    stored_path: str = ""              # XSPF written here: the stored path
    line: int = 0                      # 1-based line (M3U) / track number (XSPF)
    uri: bool = False                  # the location is a URI (XSPF)

    def label(self) -> str:
        """How the report names the entry."""
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title or self.location or self.stored_path or "(empty entry)"


@dataclass
class LibraryRow:
    """What matching reads of a library track (``snapshot_rows``)."""
    id: str
    path: str
    artist: str = ""
    title: str = ""
    duration: float = 0.0
    primary: bool = True               # not a hidden duplicate copy
    collection: bool = False           # an online-collection tune
    tunes: int = 0                     # tunes of a multi-tune file (0: one)
    group: str = ""                    # its duplicate group ("" when none)


@dataclass
class ImportResult:
    name: str
    entries: list = field(default_factory=list)      # stored playlist entries
    total: int = 0
    by: dict = field(default_factory=lambda: {"path": 0, "folder": 0, "tags": 0})
    unmatched: list = field(default_factory=list)    # [{line, entry, reason}]
    unmatched_count: int = 0
    truncated: bool = False                           # more than MAX_ENTRIES


# ── Reading ──────────────────────────────────────────────────────────────────

def decode(data: bytes) -> str:
    """The text of a playlist file: UTF-16 with its BOM, UTF-8 (with or
    without a BOM, as M3U8 and XSPF are), else Windows-1252 / Latin-1 (old
    M3U files)."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252")
        except UnicodeDecodeError:
            return data.decode("latin-1")


def parse(text: str, filename: str = "") -> tuple[str | None, list[Entry], bool]:
    """``(the playlist's own name or None, its entries, truncated)`` of an
    M3U / M3U8 / XSPF file (XSPF: the text is XML with a ``playlist`` root;
    anything else is read as M3U — a plain list of paths too).  Raises
    ValueError for a file that is neither."""
    head = text.lstrip("\ufeff \t\r\n")[:512].lower()
    if head.startswith("<") and "<playlist" in text[:4096].lower():
        name, entries = _parse_xspf(text)
    elif filename.lower().endswith(".xspf"):
        raise ValueError("This XSPF file isn't valid XML.")
    else:
        name, entries = _parse_m3u(text)
    truncated = len(entries) > MAX_ENTRIES
    return name, entries[:MAX_ENTRIES], truncated


def _parse_m3u(text: str) -> tuple[str | None, list[Entry]]:
    name = None
    entries: list[Entry] = []
    pending = Entry()
    for n, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            up = line.upper()
            if up.startswith("#PLAYLIST:"):
                name = line[10:].strip() or name
            elif up.startswith("#EXTINF:"):
                pending.artist = pending.title = ""         # (this line's only)
                info = line[8:]
                dur, _, rest = info.partition(",")
                # (attributes may follow the length: ``#EXTINF:-1 tvg-id="…",Title``)
                dur = dur.strip().split(" ", 1)[0]
                try:
                    secs = float(dur)
                    pending.duration = secs if secs > 0 else None
                except ValueError:
                    pending.duration = None
                rest = rest.strip()
                artist, sep, title = rest.partition(" - ")
                if sep:
                    pending.artist, pending.title = artist.strip(), title.strip()
                else:
                    pending.title = rest
            elif up.startswith(_M3U_TUNE):
                try:
                    t = int(line[len(_M3U_TUNE):].strip())
                    pending.tune = t if t >= 1 else None
                except ValueError:
                    pass
            continue
        pending.location = line
        pending.line = n
        entries.append(pending)
        pending = Entry()
    return name, entries


def _parse_xspf(text: str) -> tuple[str | None, list[Entry]]:
    from defusedxml import ElementTree as ET          # (no entity expansion, no DTD fetch)
    from defusedxml.common import DefusedXmlException
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ValueError("This XSPF file isn't valid XML.") from exc

    def local(tag) -> str:
        return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""

    if local(root.tag) != "playlist":
        raise ValueError("This XML file isn't an XSPF playlist.")

    def child_text(el, name) -> str:
        for c in el:
            if local(c.tag) == name:
                return (c.text or "").strip()
        return ""

    name = child_text(root, "title") or None
    entries: list[Entry] = []
    for tl in (c for c in root if local(c.tag) == "trackList"):
        for n, tr in enumerate((c for c in tl if local(c.tag) == "track"), 1):
            e = Entry(line=n, uri=True)
            e.location = child_text(tr, "location")
            e.title = child_text(tr, "title")
            e.artist = child_text(tr, "creator")
            dur = child_text(tr, "duration")
            try:
                ms = float(dur)
                e.duration = ms / 1000.0 if ms > 0 else None
            except ValueError:
                pass
            for c in tr:
                if local(c.tag) != "meta":
                    continue
                rel = c.get("rel") or ""
                val = (c.text or "").strip()
                if rel == META_PATH and val:
                    e.stored_path = val
                elif rel == META_TUNE:
                    try:
                        t = int(val)
                        e.tune = t if t >= 1 else None
                    except ValueError:
                        pass
            entries.append(e)
    return name, entries


# ── Matching ─────────────────────────────────────────────────────────────────

def _fold(s: str) -> str:
    """Case, accents and runs of whitespace ignored."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.casefold().split())


_WIN_DRIVE_RE = re.compile(r"^[a-zA-Z]:[\\/]")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def location_path(loc: str, uri: bool = False) -> str:
    """An entry's location as a path: a ``file://`` URI or another URL (and
    any location of an XSPF — ``uri``: they are URIs) percent-decoded,
    quotes around it dropped, backslashes as ``/``, Unicode in NFC."""
    loc = (loc or "").strip().strip('"')
    if loc.lower().startswith("file:"):
        rest = unquote(loc[5:])
        if rest.startswith("//"):
            rest = rest[2:]
            # file://host/share/x (UNC) keeps its host as the first folder;
            # file:///x and file://localhost/x are local
            if rest.lower().startswith("localhost/"):
                rest = rest[9:]
        if re.match(r"^/[a-zA-Z]:/", rest):           # file:///C:/Music/…
            rest = rest[1:]
        loc = rest
    elif _SCHEME_RE.match(loc) or uri:
        loc = unquote(loc)                 # (a URL / an XSPF location: percent-encoded)
    if _WIN_DRIVE_RE.match(loc) or ("\\" in loc and not _SCHEME_RE.match(loc)):
        loc = loc.replace("\\", "/")
    return unicodedata.normalize("NFC", loc)


def _components(path: str) -> list[str]:
    """The folders and name of a path, lower-cased, an archive member's
    (``a.zip::x/y.mod``) included — the scheme and host of a URL left out."""
    p = path
    m = _SCHEME_RE.match(p)
    if m:
        rest = p[m.end():]
        p = rest.split("/", 1)[1] if "/" in rest else ""
    p = unicodedata.normalize("NFC", p.replace("::", "/"))
    return [c.casefold() for c in p.split("/") if c and c not in (".", "..")]


def _is_web_stream(loc: str) -> bool:
    return bool(re.match(r"^(https?|rtsp|mms|icy)://", loc or "", re.IGNORECASE))


class Matcher:
    """Matches playlist-file entries to library tracks (module docstring)."""

    def __init__(self, rows: list[LibraryRow]):
        self.rows = {r.id: r for r in rows}
        self._by_path = {unicodedata.normalize("NFC", r.path): r for r in rows}
        self._by_name: dict[str, list[LibraryRow]] | None = None
        self._by_tags: dict[tuple[str, str], list[LibraryRow]] | None = None

    def _names(self) -> dict:
        if self._by_name is None:
            idx: dict[str, list[LibraryRow]] = {}
            for r in self.rows.values():
                comps = _components(r.path)
                if comps:
                    idx.setdefault(comps[-1], []).append(r)
            self._by_name = idx
        return self._by_name

    def _tags(self) -> dict:
        if self._by_tags is None:
            idx: dict[tuple[str, str], list[LibraryRow]] = {}
            for r in self.rows.values():
                if r.title:
                    idx.setdefault((_fold(r.artist), _fold(r.title)), []).append(r)
            self._by_tags = idx
        return self._by_tags

    @staticmethod
    def _length_ok(e: Entry, r: LibraryRow) -> bool:
        return not (e.duration and r.duration
                    and abs(e.duration - r.duration) > _LENGTH_SLACK_S)

    @staticmethod
    def _pick(cands: list[LibraryRow]) -> LibraryRow | None:
        """One of equally close candidates: the one; copies of ONE track (one
        duplicate group) — its primary copy, then a library file before an
        online-collection twin; else None (different tracks: never a guess)."""
        if len(cands) == 1:
            return cands[0]
        if len({r.group for r in cands}) != 1 or not cands[0].group:
            return None
        for keep in (lambda r: r.primary, lambda r: not r.collection):
            narrowed = [r for r in cands if keep(r)]
            if len(narrowed) == 1:
                return narrowed[0]
            if narrowed:
                cands = narrowed
        return None

    @staticmethod
    def _agrees(e: Entry, r: LibraryRow) -> bool:
        """Does what the entry says of the track — its length, its title and
        artist — confirm ``r`` (a file name alone is no proof)?"""
        if e.duration and r.duration:
            return abs(e.duration - r.duration) <= _LENGTH_SLACK_S
        if e.title and r.title and _fold(e.title) == _fold(r.title):
            return not (e.artist and r.artist) or _fold(e.artist) == _fold(r.artist)
        return False

    def match(self, e: Entry) -> tuple[LibraryRow | None, str]:
        """``(track, how)`` — how: ``path`` / ``folder`` / ``tags`` — or
        ``(None, why not)``."""
        for p in (e.stored_path and unicodedata.normalize("NFC", e.stored_path),
                  e.location and location_path(e.location, e.uri)):
            r = self._by_path.get(p) if p else None
            if r is not None:
                return r, "path"
        loc = location_path(e.location, e.uri) if e.location else (e.stored_path or "")
        comps = _components(loc) if loc else []
        several = name_only = False
        if comps:
            cands = self._names().get(comps[-1]) or []
            best, best_n = [], 0
            for r in cands:
                if not self._length_ok(e, r):
                    continue
                rc = _components(r.path)
                n = 0
                while n < min(len(rc), len(comps)) and rc[-1 - n] == comps[-1 - n]:
                    n += 1
                if n > best_n:
                    best, best_n = [r], n
                elif n == best_n:
                    best.append(r)
            # (the file name alone — no folder in common — counts only when
            # the entry's length or tags confirm it)
            if best and best_n < 2:
                confirmed = [r for r in best if self._agrees(e, r)]
                name_only = not confirmed
                best = confirmed
            if best:
                got = self._pick(best)
                if got is not None:
                    return got, "folder"
                several = True
        if e.title:
            cands = [r for r in self._tags().get((_fold(e.artist), _fold(e.title)), ())
                     if self._length_ok(e, r)]
            if cands:
                got = self._pick(cands)
                if got is not None:
                    return got, "tags"
                several = True
        if several:
            return None, "several tracks match"
        if name_only:
            return None, "only the file name matches"
        if loc and _is_web_stream(loc):
            return None, "a web address"
        return None, "not in the library"


def import_entries(name: str, entries: list[Entry], matcher: Matcher,
                   truncated: bool = False) -> ImportResult:
    """The playlist entries (``api/playlist`` form: an id, or ``{id,
    subsong}`` for a tune) and the report of matching ``entries``."""
    res = ImportResult(name=name, total=len(entries), truncated=truncated)
    for e in entries:
        r, how = matcher.match(e)
        if r is None:
            res.unmatched_count += 1
            if len(res.unmatched) < REPORT_MAX:
                res.unmatched.append({"line": e.line, "entry": e.label(), "reason": how})
            continue
        res.by[how] += 1
        if e.tune and r.tunes > 1 and e.tune <= r.tunes:
            res.entries.append({"id": r.id, "subsong": e.tune - 1})
        else:
            res.entries.append(r.id)
    return res


def playlist_name(own: str | None, filename: str) -> str:
    """The new playlist's name: the file's own, else its file name without
    the extension, else "Imported playlist"."""
    if own and own.strip():
        return own.strip()[:200]
    stem = PurePosixPath((filename or "").replace("\\", "/")).name
    stem = re.sub(r"\.(m3u8?|xspf|txt)$", "", stem, flags=re.IGNORECASE).strip()
    return (stem or "Imported playlist")[:200]


# ── Writing ──────────────────────────────────────────────────────────────────

def _split_share(path: str, roots=()) -> "tuple[str, str] | None":
    """``(root, rel)`` of a share's stored ``root:/rel`` — by the configured
    share roots (``roots``: a password may hold ``:/``), else at the first
    ``:/`` after the scheme — or None for a local path."""
    m = _SCHEME_RE.match(path or "")
    if not m:
        return None
    best = max((r for r in roots if r and path.startswith(r + ":")), key=len, default=None)
    if best is not None:
        return best, path[len(best) + 1:]
    sep = path.find(":/", m.end())
    return (path[:sep], path[sep + 1:]) if sep >= 0 else (path, "")


def _clean_root(root: str) -> str:
    """A share root without the ``user:password@`` before its host
    (``search_values._strip_userinfo``: a password may hold ``/`` and ``@``)."""
    from soniqboom.core.search_values import _strip_userinfo
    m = _SCHEME_RE.match(root)
    return root[:m.end()] + _strip_userinfo(root[m.end():]) if m else root


def public_location(path: str, roots=()) -> str:
    """Where another player finds a stored track: a local path as it is; a
    share's ``root:/rel`` as the URL ``root/rel`` without user name or
    password (an archive member keeps its ``::member`` tail)."""
    split = _split_share(path or "", roots)
    if split is None:
        return path or ""
    root, rel = split
    return _clean_root(root) + rel


def stored_location(path: str, roots=()) -> str:
    """A track's stored path as an XSPF written here keeps it (read back by
    ``Matcher``): as it is — a share's root without user name or password."""
    split = _split_share(path or "", roots)
    if split is None:
        return path or ""
    root, rel = split
    return _clean_root(root) + (":" + rel if rel else "")


def _file_uri(path: str) -> str:
    """A ``file://`` URI of a local path (an archive member: the ``::``
    tail percent-encoded with the rest)."""
    return "file://" + quote(path if path.startswith("/") else "/" + path, safe="/:@!$&'()*+,;=-._~")


def _tune_of(entry) -> int | None:
    """1-based tune of a playlist entry, or None (the file's default)."""
    if isinstance(entry, dict) and isinstance(entry.get("subsong"), int):
        return entry["subsong"] + 1
    return None


def export_m3u(name: str, rows: list[tuple[dict, int | None]], roots=()) -> str:
    """An extended M3U (UTF-8 — ``.m3u8``) of ``rows``: (track dict, 1-based
    tune or None) in playlist order.  ``roots``: the share roots
    (``public_location``)."""
    out = ["#EXTM3U", f"#PLAYLIST:{_one_line(name)}"]
    for t, tune in rows:
        dur = t.get("duration") or 0
        artist, title = _one_line(t.get("artist") or ""), _one_line(t.get("title") or "")
        label = f"{artist} - {title}" if artist and title else (title or artist)
        out.append(f"#EXTINF:{int(round(dur)) if dur else -1},{label}")
        if tune:
            out.append(f"{_M3U_TUNE}{tune}")
        out.append(public_location(t.get("path") or "", roots))
    return "\n".join(out) + "\n"


def export_xspf(name: str, rows: list[tuple[dict, int | None]], roots=()) -> str:
    """An XSPF playlist of ``rows`` (as ``export_m3u``)."""
    e = html.escape
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<playlist version="1" xmlns="{XSPF_NS}">',
           f"  <title>{e(name or '')}</title>",
           "  <trackList>"]
    for t, tune in rows:
        path = t.get("path") or ""
        loc = public_location(path, roots)
        uri = loc if _SCHEME_RE.match(loc) else _file_uri(loc)
        out.append("    <track>")
        out.append(f"      <location>{e(uri)}</location>")
        for tag, val in (("title", t.get("title")), ("creator", t.get("artist")),
                         ("album", t.get("album"))):
            if val:
                out.append(f"      <{tag}>{e(str(val))}</{tag}>")
        if t.get("track_number"):
            out.append(f"      <trackNum>{int(t['track_number'])}</trackNum>")
        if t.get("duration"):
            out.append(f"      <duration>{int(round(float(t['duration']) * 1000))}</duration>")
        out.append(f'      <meta rel="{META_PATH}">{e(stored_location(path, roots))}</meta>')
        if tune:
            out.append(f'      <meta rel="{META_TUNE}">{tune}</meta>')
        out.append("    </track>")
    out += ["  </trackList>", "</playlist>"]
    return "\n".join(out) + "\n"


def _one_line(s: str) -> str:
    return " ".join(str(s).split())


def safe_filename(name: str, ext: str) -> str:
    """A download file name for a playlist: its name without characters
    file systems refuse, never empty."""
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', " ", name or "").strip(" .") or "playlist"
    return f"{base[:120]}.{ext}"
