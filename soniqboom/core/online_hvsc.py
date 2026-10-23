# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The High Voltage SID Collection, online (6581.nl).

An online collection (``core/online_catalog.py``): HVSC's own
``DOCUMENTS/Songlengths.md5`` is the index — every tune's HVSC path, its
checksum and the length of each of its tunes — so the whole collection is
listed (tune counts and lengths included, "Skip short tunes" works at once)
without reading a single ``.sid``.  ``STIL.txt`` is downloaded beside it and
gives the tunes their STIL commentary up front.  A ``.sid`` is fetched from
``https://6581.nl/C64Music/…`` only on the listener's intent.

Two options on its Settings card:

* **Leave Modland's HVSC folder out** (default on) — Modland mirrors HVSC
  (``/pub/modules/HVSC``); while this collection is on, the Modland catalogue
  leaves that folder out (``modland_catalog._hvsc_left_out``), its tunes
  parked like a narrower scope's.
* **Use for my own SID files** (default on) — with no local HVSC folder set,
  the downloaded ``Songlengths.md5`` / ``STIL.txt`` serve as the HVSC
  documents for every SID (``apply_docs_fallback``): lengths by checksum,
  STIL by HVSC path or by checksum.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

from soniqboom.core import online_catalog
from soniqboom.core.online_catalog import OnlineCatalogue, user_agent

log = logging.getLogger(__name__)

BASE = "https://6581.nl/C64Music"
_DOCS = ("Songlengths.md5", "STIL.txt")
_ENTRY_RE = re.compile(r"^([0-9a-fA-F]{32})\s*=\s*(.+)$")
OPT_REPLACE_MODLAND = "replace_modland"
OPT_LOCAL_DOCS = "local_docs"


def _parse_len(tok: str) -> float | None:
    # (HVSC's own parser — the lengths the SID lookups give the same file.)
    from soniqboom.core.hvsc import _DURATION_RE, _parse_duration
    return _parse_duration(tok) if _DURATION_RE.fullmatch(tok) else None


def parse_songlengths(text: str):
    """``(rel, md5, lengths)`` of every entry of a Songlengths.md5 —
    ``rel`` without the leading slash (``MUSICIANS/H/Hubbard_Rob/Commando.sid``)."""
    path = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("; /"):
            path = line[3:].strip()
            continue
        m = _ENTRY_RE.match(line)
        if m and path:
            lens = [round(x, 3) for x in (_parse_len(t) for t in m.group(2).split())
                    if x is not None]
            yield path, m.group(1).lower(), lens
        path = None


def _http_date(value: str | None) -> str | None:
    """``Fri, 26 Jun 2026 13:39:13 GMT`` → ``2026-06-26``."""
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        return None


class HVSCOnline(OnlineCatalogue):
    key = "hvsc"
    label = "HVSC"
    root = BASE
    site_url = "https://6581.nl/"
    default_interval = "weekly"
    min_rows = 20_000
    build_rules = 3                 # 3: group members' folders credit the member
    index_fill_fields = ("hvsc_lengths", "stil")
    description = ("The High Voltage SID Collection — every Commodore 64 SID tune, with "
                   "song lengths and STIL comments")

    def __init__(self) -> None:
        super().__init__()
        self._stil: dict | None = None
        self._flags: tuple | None = None
        self._learned: dict | None = None

    # Options ----------------------------------------------------------------
    def options(self) -> dict:
        from soniqboom.core.store import get_store
        st = get_store()
        return {OPT_REPLACE_MODLAND: bool(st.get_config(self.cfg(OPT_REPLACE_MODLAND), True)),
                OPT_LOCAL_DOCS: bool(st.get_config(self.cfg(OPT_LOCAL_DOCS), True))}

    def option_labels(self) -> dict:
        return {OPT_REPLACE_MODLAND: "Leave Modland's HVSC folder out",
                OPT_LOCAL_DOCS: "Use its song lengths and STIL for my own SID files"}

    def set_option(self, name: str, value) -> None:
        from soniqboom.core.store import get_store
        if name not in (OPT_REPLACE_MODLAND, OPT_LOCAL_DOCS):
            raise ValueError(f"unknown option {name!r}")
        get_store().set_config(self.cfg(name), bool(value))
        if name == OPT_REPLACE_MODLAND:
            if self.enabled():
                _modland_reapply()
        else:
            apply_docs_fallback()

    # Index ------------------------------------------------------------------
    def docs_dir(self) -> Path:
        d = self.dir() / "DOCUMENTS"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def fetch_index(self, workdir: Path, info: dict):
        import httpx
        headers = {"User-Agent": user_agent()}
        have_docs = all((self.docs_dir() / n).exists() for n in _DOCS)
        if info.get("etag") and have_docs:
            headers["If-None-Match"] = info["etag"]
        if info.get("last_modified") and have_docs:
            headers["If-Modified-Since"] = info["last_modified"]
        new_info: dict = {}
        with httpx.Client(timeout=httpx.Timeout(30.0, read=300.0), follow_redirects=True) as c:
            r = c.get(f"{BASE}/DOCUMENTS/Songlengths.md5", headers=headers)
            if r.status_code == 304:
                return None
            r.raise_for_status()
            (workdir / "Songlengths.md5").write_bytes(r.content)
            new_info["etag"] = r.headers.get("etag")
            new_info["last_modified"] = r.headers.get("last-modified")
            new_info["index_date"] = _http_date(r.headers.get("last-modified"))
            try:
                s = c.get(f"{BASE}/DOCUMENTS/STIL.txt", headers={"User-Agent": user_agent()})
                s.raise_for_status()
                (workdir / "STIL.txt").write_bytes(s.content)
            except Exception as exc:                    # noqa: BLE001
                # The song lengths came: the index is built from them, with
                # the STIL in use (a failed STIL would otherwise refuse the
                # whole refresh — and its retries download Songlengths again
                # each time).  The next scheduled refresh asks for both again
                # (no validators kept), so the STIL catches up then.
                kept = self.docs_dir() / "STIL.txt"
                log.warning("HVSC: STIL.txt not downloaded (%s) — %s", exc,
                            "the one in use is kept" if kept.exists() else "none for now")
                if kept.exists():
                    shutil.copyfile(kept, workdir / "STIL.txt")
                else:
                    (workdir / "STIL.txt").write_bytes(b"")
                new_info["etag"] = new_info["last_modified"] = None
        text = (workdir / "Songlengths.md5").read_bytes().decode("latin-1")
        rows = [(rel, 0, {"md5": md5, "len": lens})
                for rel, md5, lens in parse_songlengths(text)]
        if len(rows) < self.min_rows:
            raise online_catalog.IndexRefused(
                f"HVSC's Songlengths lists only {len(rows)} tunes — implausible")
        # The documents kept for the SID lookups (``apply_docs_fallback``):
        # staged now, put in place once the index is built (``after_index``)
        # — an index refused (shrink guard) leaves the documents in use.
        for n in _DOCS:
            shutil.copyfile(workdir / n, self.docs_dir() / f"{n}.new")
        self._stil = None
        return rows, new_info

    def after_index(self) -> None:
        for n in _DOCS:
            staged = self.docs_dir() / f"{n}.new"
            if staged.exists():
                staged.replace(self.docs_dir() / n)
        self._stil = None
        apply_docs_fallback()

    async def enable(self) -> dict:
        res = await super().enable()
        _modland_reapply()               # Modland's HVSC folder leaves (option on)
        _hvsc_download_follow()          # its refresh keeps the documents now
        return res

    async def disable(self) -> dict:
        res = await super().disable()
        _hvsc_download_follow(take_over=True)   # the HVSC card's download takes over (if chosen)
        return res

    def after_disable(self) -> None:
        apply_docs_fallback()
        _modland_reapply()
        schedule_reconcile()

    # Tracks -----------------------------------------------------------------
    def _stil_map(self) -> dict:
        if self._stil is None:
            p = self.docs_dir() / "STIL.txt"
            try:
                from soniqboom.core.hvsc import HVSC
                self._stil = HVSC._parse_stil(p.read_bytes().decode("latin-1"))
            except OSError:
                self._stil = {}
        return self._stil

    async def apply_index(self) -> dict:
        from soniqboom.core import genre_derive, scene_metadata as sm
        self._flags = (sm.filename_game_enabled(), genre_derive.names_enabled())
        self._stil = None
        try:
            result = await super().apply_index()
            try:
                await self.learn_from_library()
            except Exception:                           # noqa: BLE001
                log.debug("HVSC: credits of played tunes not learned", exc_info=True)
            return result
        finally:
            self._stil = None
            schedule_reconcile()          # Modland's HVSC copy ↔ this one: user data follows

    def build_track(self, rel, size, extra, *, added_at, dir_hash, root_h) -> dict:
        from soniqboom.core import modland_catalog as mc
        use_filename, names = self._flags or (False, False)
        # Modland's derivation of an HVSC path (title, credit, genre, game) —
        # the same rows a Modland HVSC tune gets.
        t = dict(mc.build_track("HVSC/" + rel, size, added_at=added_at, siblings=None,
                                use_filename=use_filename, names=names,
                                dir_hash=dir_hash, root_h=root_h))
        t["id"] = self.track_id(rel)
        t["path"] = self.track_path(rel)
        t.pop("scene_path", None)                # (a Modland path: set by a checksum match only)
        learned = self._credits().get(composer_dir(rel) or "")
        if learned:
            t["artist"] = learned                # the composer's own name (a play read it)
        else:
            member = folder_credit(rel)
            if member and (composer_dir(rel) or "").count("/") == 3:
                t["artist"] = member             # a group member's folder: the member
        extra = extra or {}
        lens = [float(x) for x in (extra.get("len") or []) if x]
        if lens:
            t["hvsc_lengths"] = lens
            t["duration"] = lens[0]
            if len(lens) > 1:
                t["subsongs"] = len(lens)
        if extra.get("md5"):
            t["sid_md5"] = extra["md5"]
        stil = self._stil_map().get(("/" + rel).lower())
        if stil and stil.get("text"):
            t["stil"] = stil["text"]
        from soniqboom.core.store import share_values
        return share_values(t)

    def move_key(self, rel, size, extra):
        """A tune HVSC moved to another folder (a release does that for
        hundreds) is the same file: its checksum."""
        md5 = (extra or {}).get("md5")
        return f"md5:{str(md5).lower()}" if md5 else None

    def track_move_key(self, t):
        md5 = (t or {}).get("sid_md5")
        return f"md5:{str(md5).lower()}" if md5 else None

    def row_changed(self, prev_size, prev_extra, size, extra) -> bool:
        """Another version of a tune: another checksum (new lengths or STIL
        alone leave the file as it is)."""
        return ((prev_extra or {}).get("md5") or None) != ((extra or {}).get("md5") or None)

    def changed(self, old: dict, rel: str, size: int, extra) -> bool:
        md5 = (extra or {}).get("md5")
        if (old.get("sid_md5") or None) != md5:
            return True
        if (old.get("mtime") or 0) > 0:
            return False                 # played: what the file said stays
        lens = [float(x) for x in ((extra or {}).get("len") or []) if x]
        if list(old.get("hvsc_lengths") or []) != lens:
            return True
        stil = self._stil_map().get(("/" + rel).lower()) or {}
        return (old.get("stil") or None) != (stil.get("text") or None)

    def merged_fields(self, d: dict, old: dict, rel: str) -> dict:
        for f in ("hvsc_lengths", "stil"):
            if old.get(f) and not d.get(f):
                d[f] = old[f]
        lens = d.get("hvsc_lengths") or []
        if lens and not (old.get("hvsc_lengths") and d.get("duration")
                         and d["duration"] in lens):
            # The read gave the default length (no HVSC documents loaded):
            # the index's lengths stand — the default tune's.
            start = int(d.get("start_subsong") or 1)
            d["duration"] = lens[start - 1] if 0 < start <= len(lens) else lens[0]
            if len(lens) > 1:
                d["subsongs"] = len(lens)
        return d

    # Composer credits learned from plays ------------------------------------
    # Before its first play an HVSC tune is credited from its MUSICIANS folder
    # ("Hubbard_Rob" → "Hubbard Rob", "Wyndex"); the play reads the file's own
    # author ("Rob Hubbard", "Stephen L. Judd (Wyndex)").  When that author is
    # the folder's composer — the same words, the folder's handle or name in
    # "Name (Handle)", or the name with initials — the composer's other tunes
    # take it at once, and every later build of them too (``credits.json``):
    # one composer, one artist.  A collaboration ("A & B") teaches nothing.
    def credits_path(self) -> Path:
        return self.dir() / "credits.json"

    def _credits(self) -> dict:
        if self._learned is None:
            try:
                data = json.loads(self.credits_path().read_text("utf-8"))
                self._learned = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._learned = {}
        return self._learned

    def _learn_credit(self, composer_dir: str, author: str) -> None:
        cr = dict(self._credits())
        cr[composer_dir] = author
        p = self.credits_path()
        tmp = p.with_name(f"{p.name}.tmp")
        try:
            tmp.write_text(json.dumps(cr, ensure_ascii=False, separators=(",", ":")), "utf-8")
            tmp.replace(p)
        except OSError:
            log.debug("HVSC: learned credits not saved", exc_info=True)
        self._learned = cr

    async def learn_from_library(self) -> int:
        """The credits tunes played before could teach (read before learning
        existed, or the credits file lost): each composer folder with a played
        tune whose own author is its composer — its unplayed tunes take that
        name (``after_merge``'s rule).  One pass over the rows, one write of
        the credits, the event loop served in between.  Returns the folders
        learned."""
        from soniqboom.core.store import get_store
        st = get_store()
        known = self._credits()
        played: dict = {}
        unplayed: dict = {}
        for i, tid in enumerate(list(st._tag_scan_root_hash.get(self.root_hash()) or ())):
            t = st._tracks.get(tid)
            if t is not None:
                rel = self.rel_of(t.get("path") or "")
                cdir = composer_dir(rel)
                if cdir and cdir not in known:
                    side = played if (t.get("mtime") or 0) > 0 else unplayed
                    side.setdefault(cdir, []).append((tid, (t.get("artist") or "").strip(), rel))
            if i % 5000 == 4999:
                await asyncio.sleep(0)
        learned: dict = {}
        upd: list = []
        for j, (cdir, rows) in enumerate(played.items()):
            folder = folder_credit(rows[0][2])
            author = next((a for _t, a, _r in rows
                           if a and folder and a != folder and same_composer(folder, a)), None)
            if not author or self.is_group_folder(cdir):
                continue
            learned[cdir] = author
            upd += [(tid, {"artist": author}) for tid, a, _r in unplayed.get(cdir, ())
                    if a == folder]
            if j % 200 == 199:
                await asyncio.sleep(0)
        if not learned:
            return 0
        cr = {**self._credits(), **learned}
        p = self.credits_path()
        tmp = p.with_name(f"{p.name}.tmp")
        try:
            tmp.write_text(json.dumps(cr, ensure_ascii=False, separators=(",", ":")), "utf-8")
            tmp.replace(p)
        except OSError:
            log.debug("HVSC: learned credits not saved", exc_info=True)
        self._learned = cr
        for i in range(0, len(upd), 2000):
            st.update_track_fields_batch(upd[i:i + 2000])
            await asyncio.sleep(0)
        log.info("HVSC: learned %d composer credit(s) from tunes played before", len(learned))
        try:
            await self._after_library_change()
        except Exception:                               # noqa: BLE001
            pass
        return len(learned)

    def is_group_folder(self, cdir: str) -> bool:
        """Does composer folder ``cdir`` hold group members' folders (its own
        tunes are the group's, never one member's to name)?"""
        try:
            return any(e.is_dir and _member_folder(e.name) for e in self.list_dir(cdir))
        except Exception:                               # noqa: BLE001
            return False

    def after_merge(self, track_id: str, d: dict, old: dict) -> None:
        rel = self.rel_of(old.get("path") or "")
        cdir = composer_dir(rel)
        new = (d.get("artist") or "").strip()
        if not cdir or not new:
            return
        folder = folder_credit(rel)
        if not folder or new == folder or not same_composer(folder, new):
            return
        if self.is_group_folder(cdir):
            return
        before = self._credits().get(cdir)
        if before != new:
            self._learn_credit(cdir, new)
            log.info("HVSC: %s is credited as %s", cdir, new)
        from soniqboom.core.store import get_store
        st = get_store()
        prefix = self.track_path(cdir) + "/"
        olds = {folder, before} - {None}
        tracks = st._tracks
        upd = []
        for tid in list(st._tag_scan_root_hash.get(self.root_hash()) or ()):
            t = tracks.get(tid)
            if (t is not None and tid != track_id and (t.get("path") or "").startswith(prefix)
                    and t.get("artist") in olds and not (t.get("mtime") or 0) > 0
                    and composer_dir(self.rel_of(t.get("path") or "")) == cdir):
                upd.append((tid, {"artist": new}))
        if upd:
            st.update_track_fields_batch(upd)
            self._folder_changed_soon(cdir)

    def _folder_changed_soon(self, cdir: str) -> None:
        """Tell open views of folder ``cdir`` its tunes changed (re-credited)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        async def _tell() -> None:
            try:
                from soniqboom.api import library
                await library.broadcast_catalogue_folder_changed(self.root, f"{self.root}/{cdir}")
            except Exception:                           # noqa: BLE001
                log.debug("HVSC: folder-changed broadcast failed", exc_info=True)
        def _spawn() -> None:
            t = loop.create_task(_tell())
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        try:
            loop.call_soon_threadsafe(_spawn)
        except RuntimeError:
            pass


# Folders inside a composer's folder that name no group member.
_NOT_MEMBER_DIRS = frozenset({"worktunes", "unreleased"})


def _member_folder(name: str) -> bool:
    n = (name or "").casefold()
    return bool(n) and n not in _NOT_MEMBER_DIRS and not n.endswith("_original")


def composer_dir(rel: str) -> str | None:
    """The composer folder of a tune under HVSC's MUSICIANS tree:
    ``MUSICIANS/H/Hubbard_Rob`` — a group member's own folder inside a
    group's (``MUSICIANS/0-9/20CC/Paul_Falco``), not a composer's
    ``Worktunes``."""
    parts = (rel or "").split("/")
    if len(parts) < 4 or parts[0] != "MUSICIANS":
        return None
    if len(parts) >= 5 and _member_folder(parts[3]):
        return "/".join(parts[:4])
    return "/".join(parts[:3])


def folder_credit(rel: str) -> str | None:
    """The composer a tune's folder names before its file is read — a group
    member's folder its member ("Paul Falco"), not the group."""
    from soniqboom.core import modland_catalog as mc
    parts = (rel or "").split("/")
    if len(parts) >= 5 and parts[0] == "MUSICIANS" and _member_folder(parts[3]):
        return parts[3].replace("_", " ").strip() or None
    return mc.credit_for("HVSC/" + rel, None)


_COLLAB_RE = re.compile(r"\s(?:&|and|vs\.?|feat\.?|with)\s|[/,+]", re.I)
# HVSC's folder names spell ASCII what the SID's author field may not:
# "Huelsbeck_Chris" / "Chris Hülsbeck", "Oeie_Karl_Bjoernar" / "Karl Bjørnar
# Øie", "Haard_Lars" / "Lars Hård" — or with the accents just dropped.
_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "æ": "ae", "ø": "oe",
                           "å": "aa", "œ": "oe", "ð": "d", "þ": "th", "ł": "l"})
_UNACCENTED = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "ð": "d", "þ": "th",
                             "ł": "l"})        # (what NFKD can't take apart)
# A bracketed part that is no handle ("Rob Hubbard (remix)").
_NOT_HANDLES = frozenset({"remix", "cover", "arrangement", "arr", "edit", "version", "intro",
                          "demo", "tune", "music", "sid", "conversion", "rip", "unknown"})


def _plain(v: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", v).encode("ascii", "ignore").decode("ascii")


def _words(v: str) -> list:
    return sorted(re.findall(r"\w+", _plain((v or "").casefold())))


def _spellings(v: str) -> list:
    """The word lists ``v`` may be spelled with in ASCII (accents dropped,
    or transliterated)."""
    low = (v or "").casefold()
    return [sorted(re.findall(r"\w+", _plain(low.translate(t))))
            for t in (_UNACCENTED, _TRANSLIT)]


def same_composer(folder: str, author: str) -> bool:
    """Is a SID's own author (``author``) the composer its HVSC folder names
    (``folder``)?  "Hubbard Rob" / "Rob Hubbard"; "Wyndex" / "Stephen L. Judd
    (Wyndex)"; "Judd Stephen" / "Stephen L. Judd"; "Huelsbeck Chris" / "Chris
    Hülsbeck".  Never a collaboration, never a bracketed second name or
    remark ("Rob Hubbard (Ben Daglish)", "(remix)"), no extra words beyond
    initials ("DJ Rob Hubbard", "Rob Hubbard 87")."""
    outside = re.sub(r"\([^()]*\)", " ", author or "")
    if not folder or not author or _COLLAB_RE.search(outside):
        return False
    fw = _words(folder)
    if not fw:
        return False
    if fw in _spellings(author):
        return True
    m = re.match(r"^(?P<name>.+?)\s*\((?P<handle>[^()]+)\)\s*$", author)
    if m:
        handle, name = m.group("handle").strip(), m.group("name")
        is_handle = (len(handle.split()) == 1 and handle.casefold() not in _NOT_HANDLES
                     and not handle.isdigit())
        if is_handle and fw in _spellings(handle):
            return True                      # a handle folder: "Real Name (Handle)"
        return is_handle and _name_matches(fw, name)   # a name folder, the handle beside it
    return _name_matches(fw, outside)


def _name_matches(fw: list, name: str) -> bool:
    """Do folder words ``fw`` spell ``name`` — perhaps with initials more
    ("Judd Stephen" / "Stephen L. Judd")?"""
    for aw in _spellings(name):
        extra = [w for w in aw if w not in fw]
        if aw == fw or (set(fw) <= set(aw) and extra
                        and all(len(w) == 1 and w.isalpha() for w in extra)):
            return True
    return False


HVSC_ONLINE = online_catalog.register(HVSCOnline())


def _modland_reapply() -> None:
    """Modland's catalogue follows the "Leave Modland's HVSC folder out"
    option / this collection's switch: it is applied again — once its
    running job ends when one runs.  Callable from any thread."""
    loop = HVSC_ONLINE._loop
    if loop is None or loop.is_closed():
        return

    async def _when_idle() -> None:
        try:
            from soniqboom.core import modland_catalog as mc
            for _ in range(3600):
                if not mc.enabled():
                    return
                if not mc.busy():
                    mc.start_job(mc.reapply)
                    return
                await asyncio.sleep(1.0)
        except Exception:                               # noqa: BLE001
            log.debug("Modland re-apply after an HVSC change failed", exc_info=True)

    def _spawn() -> None:
        global _reapply_task
        if _reapply_task is not None and not _reapply_task.done():
            return
        _reapply_task = loop.create_task(_when_idle(), name="online-hvsc.modland-reapply")
        HVSC_ONLINE._tasks.add(_reapply_task)
        _reapply_task.add_done_callback(HVSC_ONLINE._tasks.discard)
    try:
        loop.call_soon_threadsafe(_spawn)
    except RuntimeError:
        pass


_reapply_task = None


_fallback_active = False


def _hvsc_download_follow(*, take_over: bool = False) -> None:
    """Re-arm (or stop) the HVSC card's own download (``core/hvsc_download``);
    ``take_over``: the collection went off — its last refresh counts as the
    download's (no download again at once)."""
    try:
        from soniqboom.core import hvsc_download
        if take_over and online_docs_chosen():
            hvsc_download._take_over_from_collection()
        hvsc_download.start()
    except Exception:                                   # noqa: BLE001
        log.debug("HVSC download not re-armed", exc_info=True)


def online_docs_chosen() -> bool:
    """The HVSC card's source is "Download from the internet" (``hvsc_source``)."""
    from soniqboom.config import settings
    return getattr(settings, "hvsc_source", "local") == "online"


def local_docs_path() -> str:
    """The local HVSC ``DOCUMENTS`` folder in use: the configured path while
    the HVSC card's source is the local folder, else none."""
    from soniqboom.config import settings
    if online_docs_chosen():
        return ""
    return (getattr(settings, "hvsc_docs_path", "") or "").strip()


def apply_docs_fallback() -> None:
    """Point the SID lookups (``core/hvsc``) at the downloaded documents when
    no local HVSC folder is in use and either the HVSC card downloads them
    (``hvsc_source`` "online", ``core/hvsc_download``) or the collection is on
    with its "Use for my own SID files" option; undo it when that no longer
    holds."""
    global _fallback_active
    from soniqboom.core.hvsc import get_hvsc
    hv = get_hvsc()
    docs = HVSC_ONLINE.dir() / "DOCUMENTS"
    local = local_docs_path()
    want = (not local
            and (online_docs_chosen()
                 or (HVSC_ONLINE.enabled()
                     and HVSC_ONLINE.options().get(OPT_LOCAL_DOCS, True)))
            and (docs / "Songlengths.md5").exists())
    ours = str(docs)
    if want:
        sig = _docs_signature(docs)
        if hv.docs_path == ours and _fallback_active and sig == _applied_sig:
            return                   # these very documents are in use already
        if hv.docs_path != ours:
            hv.configure(ours)
            log.info("HVSC: song lengths and STIL from the downloaded HVSC documents")
        else:
            hv.reload()
        _fallback_active = True
        _set_applied(sig)
        _apply_to_library()          # the SIDs already in the library take them
    elif hv.docs_path == ours:
        hv.configure(local or None)
        _fallback_active = False
        _set_applied(None)


_applied_sig = None


def _set_applied(sig) -> None:
    global _applied_sig
    _applied_sig = sig


def _docs_signature(docs: Path):
    """The documents as they are on disk (size + modification time of each) —
    unchanged documents in use are not applied to the library again."""
    out = []
    for n in _DOCS:
        try:
            st = (docs / n).stat()
            out.append((n, st.st_size, st.st_mtime_ns))
        except OSError:
            out.append((n, None, None))
    return tuple(out)


def fallback_active() -> bool:
    """The SID lookups use the downloaded documents right now."""
    try:
        from soniqboom.core.hvsc import get_hvsc
        return _fallback_active and get_hvsc().docs_path == str(HVSC_ONLINE.dir() / "DOCUMENTS")
    except Exception:                                   # noqa: BLE001
        return False


_apply_again = False


def _apply_to_library() -> None:
    """Join the (new) documents onto every SID of the library — the user's
    own included — in the background.  Callable from any thread."""
    loop = HVSC_ONLINE._loop
    if loop is None or loop.is_closed():
        return

    def _spawn() -> None:
        global _apply_again
        if any(t.get_name() == "online-hvsc.apply-library" and not t.done()
               for t in HVSC_ONLINE._tasks):
            _apply_again = True      # one apply at a time: the running one goes again
            return

        async def _run() -> None:
            global _apply_again
            from soniqboom.core import hvsc_apply
            while True:
                _apply_again = False
                try:
                    res = await hvsc_apply.apply_hvsc_to_library()
                    log.info("HVSC: the downloaded documents applied to the library (%s)", res)
                except Exception:                       # noqa: BLE001
                    log.warning("HVSC apply after the downloaded documents changed failed",
                                exc_info=True)
                if not _apply_again:
                    break
        t = loop.create_task(_run(), name="online-hvsc.apply-library")
        HVSC_ONLINE._tasks.add(t)
        t.add_done_callback(HVSC_ONLINE._tasks.discard)
    try:
        loop.call_soon_threadsafe(_spawn)
    except RuntimeError:
        pass


# ── Ratings, plays and playlist entries between the two HVSC copies ─────────
# Modland mirrors HVSC (``HVSC/<path>``); this collection lists the same tunes
# from 6581.nl (``<path>``) under other track ids.  When only ONE copy of a
# tune is in the library — this collection on with Modland's HVSC folder left
# out, or the other way round after a switch-off — the user data of the copy
# that left (rating, plays, history, playlist entries, Subsonic stars, queue,
# bookmarks) moves to the copy that is there (``store.remap_track_ids``, the
# Modland catalogue's move machinery), and back when that turns around.  Both
# copies there, or neither: nothing moves.  Where the target already has a
# rating, or one user's plays, of its own, both copies keep theirs (nothing is
# overwritten; that user's history entries stay too); playlist entries still
# move.
_reconcile_task = None
_reconcile_again = False


def schedule_reconcile() -> None:
    """Run ``reconcile_user_data`` soon (callable from any thread; runs
    queue up behind one another)."""
    loop = HVSC_ONLINE._loop
    if loop is None or loop.is_closed():
        return

    def _spawn() -> None:
        global _reconcile_task, _reconcile_again
        if _reconcile_task is not None and not _reconcile_task.done():
            _reconcile_again = True
            return
        _reconcile_task = loop.create_task(_reconcile_runs(), name="online-hvsc.reconcile")
        HVSC_ONLINE._tasks.add(_reconcile_task)
        _reconcile_task.add_done_callback(HVSC_ONLINE._tasks.discard)
    try:
        loop.call_soon_threadsafe(_spawn)
    except RuntimeError:
        pass


async def _reconcile_runs() -> None:
    global _reconcile_again
    while True:
        _reconcile_again = False
        try:
            await reconcile_user_data()
        except Exception:                               # noqa: BLE001
            log.warning("HVSC: moving user data between the HVSC copies failed", exc_info=True)
        if not _reconcile_again:
            return


def _index_rels() -> list:
    con = HVSC_ONLINE._ro()
    if con is None:
        return []
    return [f"{d}/{n}" if d else n for d, n in con.execute(
        "SELECT dirs.dir, f.name FROM files f JOIN dirs ON f.dir = dirs.id")]


async def reconcile_user_data() -> dict:
    """Move user data to the HVSC copy that is in the library (see above).
    Returns ``{"moved": n, "conflicts": n}`` — tunes whose data moved: all
    of it ("moved"), or the part the copy in the library had none of
    ("conflicts": both copies hold a rating, or one user's plays, of their
    own).  A tune with nothing left to move is not counted (a later run
    finds nothing to do)."""
    from soniqboom.core import modland_catalog as mc
    from soniqboom.core.store import PARKED_PLAYLIST_IDS, get_store
    if not HVSC_ONLINE.has_index():
        return {"moved": 0, "conflicts": 0}
    store = get_store()
    loop = asyncio.get_running_loop()
    rels = await loop.run_in_executor(None, _index_rels)
    # (Snapshots on the loop: the store's containers change only here.)
    # Strong data is kept per container — the library-wide ratings, each
    # user's plays — and never overwritten: where the target already has its
    # own, both copies keep theirs (``remap_track_ids(keep_both=True)``).
    rated = set(store._ratings)                                 # (a waveform is computed)
    # Per user: (the tunes played, the tunes in the history).
    users = [(set(store._play_stats.get(u) or ()),
              {e["track_id"] for e in store._history.get(u) or ()
               if isinstance(e, dict) and e.get("track_id")})
             for u in {*store._play_stats, *store._history}]
    weak = set()                                    # (entries that always follow)
    for pl in store.list_playlists():
        for e in pl.get("track_ids") or ():
            weak.add(e.get("id") if isinstance(e, dict) else e)
    try:
        from soniqboom.core.subsonic_state import get_state
        weak |= get_state().referenced_track_ids()
    except Exception:                                   # noqa: BLE001
        pass
    tracks = store._tracks

    def _plan():
        mapping, conflicts = {}, 0
        for rel in rels:
            mid, oid = mc.track_id("HVSC/" + rel), HVSC_ONLINE.track_id(rel)
            m_in, o_in = mid in tracks, oid in tracks
            if m_in == o_in:
                continue
            src, dst = (mid, oid) if o_in else (oid, mid)
            # What would move: a rating / a user's plays the target lacks, a
            # user's history entries (unless that user played both copies:
            # they stay with that user's plays), or an entry that only points
            # at the tune (a playlist's, Subsonic's: it always follows).
            movable = (src in weak or (src in rated and dst not in rated)
                       or any((src in p and dst not in p)
                              or (src in h and not (src in p and dst in p))
                              for p, h in users))
            if not movable:
                continue
            if (src in rated and dst in rated) or any(src in p and dst in p for p, _h in users):
                conflicts += 1
            mapping[src] = dst
        return mapping, conflicts
    mapping, conflicts = await loop.run_in_executor(None, _plan)
    if not mapping:
        return {"moved": 0, "conflicts": 0}
    counts = store.remap_track_ids(mapping, keep_both=True)
    try:
        from soniqboom.core.subsonic_state import get_state
        await get_state().remap_track_ids(mapping)
    except Exception:                                   # noqa: BLE001
        log.debug("HVSC: Subsonic stars / queues not moved", exc_info=True)
    parked = store.get_config(PARKED_PLAYLIST_IDS) or []
    if any(p in mapping for p in parked):
        store.set_config(PARKED_PLAYLIST_IDS, sorted({mapping.get(p, p) for p in parked}
                                                     - {i for i in mapping.values()
                                                        if i in tracks}))
    log.info("HVSC: user data of %d tune(s) moved to the HVSC copy in the library (%s)%s",
             len(mapping) - conflicts, counts,
             f"; {conflicts} rated / played on both copies kept each copy's own"
             if conflicts else "")
    return {"moved": len(mapping) - conflicts, "conflicts": conflicts}
