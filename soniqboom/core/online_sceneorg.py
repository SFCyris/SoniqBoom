# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""scene.org's music archive, online (archive.scene.org/pub/music).

An online collection (``core/online_catalog.py``) of PACKAGES, like Aminet's
(``core/online_aminet.py``) — but scene.org publishes no index file, so the
index is its folder listings: ``artists/``, ``groups/``, ``compos/``,
``disks/`` and ``compilations/`` are listed one folder at a time (one request
at a time, ``CRAWL_GAP_S`` apart).  A refresh lists again only what can have
changed: a folder without subfolders whose date in its parent's listing is
the one seen last time keeps its files (``crawl.json``), so a refresh costs
about as many requests as there are folders WITH subfolders.  A listing that
breaks part-way leaves what it listed for the next attempt.

Each ZIP (mostly one module, or a music disk / compo pack) is one row until
it is played or its Track Info opened; a big ZIP is listed by ranged reads of
its directory, never downloaded whole for that.  Loose playable files are
plain rows.  The folders name the tunes: an ``artists/<name>/`` folder their
composer, ``groups/<name>/`` their group, a compo / disk folder their album.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

from soniqboom.core import online_catalog
from soniqboom.core.online_catalog import OnlineCatalogue, is_merged, user_agent

log = logging.getLogger(__name__)

BASE = "https://archive.scene.org/pub/music"
TOPS = ("artists", "groups", "compos", "disks", "compilations")
CRAWL_GAP_S = 0.5                  # between two listing requests
_MAX_FAILED_SHARE = 0.01           # more folders than this failing: the listing is refused
_FAILED_KEY = "//failed"           # crawl.json: the folders the last listing couldn't read
# Circuit breaker: this many folders in a row that can't be listed (each
# after its tries; not counting a 404 or a folder that failed the last
# listing too) end the crawl — the server is refusing or gone; what was
# listed is kept and the next attempt resumes from it
# (``OnlineCatalogue._retry_due``: an hour, doubling).  A ``Retry-After`` up
# to ``_RETRY_AFTER_WAIT_MAX_S`` is waited out; a longer one ends the crawl
# and the next attempt waits for it.  The pass over the failed folders after
# the crawl tries each once.
_BREAKER_FOLDERS = 4
_RETRY_AFTER_WAIT_MAX_S = 120.0

_ROW_RE = re.compile(
    r'<td class="n"><a href="([^"]+)">.*?</td>\s*<td class="m">([^<]*)</td>\s*'
    r'<td class="s">([^<]*)</td>\s*<td class="t">([^<]*)</td>', re.S)
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)", re.I)
_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


class CrawlStopped(RuntimeError):
    """The listing was stopped (switch-off, shutdown)."""


class CrawlBroken(ConnectionError):
    """The crawl ended early: scene.org kept refusing (``_BREAKER_FOLDERS``
    folders in a row not listed) or asked for a pause longer than a crawl
    waits (``retry_after``, seconds)."""

    def __init__(self, msg: str, retry_after: "float | None" = None) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


class ListingRefused(online_catalog.IndexRefused):
    """Too many folders couldn't be listed (and kept failing): refused, and
    tried again a day later, not hourly."""


def parse_listing(page: str) -> tuple[list, list]:
    """``(dirs, files)`` of one folder listing: dirs ``(name, date)``, files
    ``(name, approx size, date)``."""
    dirs, files = [], []
    for href, m, s, t in _ROW_RE.findall(page):
        name = unquote(html.unescape(href))
        if name in ("../", "./") or name.startswith(("/", "?")) or "://" in name:
            continue
        date = html.unescape(m).replace("\xa0", " ").strip()
        if name.endswith("/") or t.strip().lower() == "directory":
            name = name.rstrip("/")
            if name and "/" not in name:
                dirs.append((name, date))
            continue
        if "/" in name:
            continue
        sm = _SIZE_RE.match(html.unescape(s))
        size = int(float(sm.group(1)) * _UNITS[sm.group(2).upper()]) if sm else 0
        files.append((name, size, date))
    return dirs, files


def page_tail(page: bytes) -> int | None:
    """How many bytes a complete listing page has after its last row (the
    table's end, the footer, the sorting script — the same on every page)."""
    i = page.rfind(b"</tr>")
    return len(page) - (i + 5) if i >= 0 else None


def _listing_text(client, url: str, rel: str, tail: int | None = None) -> bytes:
    """One folder listing's page.  archive.scene.org's server ends a long
    listing early over HTTP/1.1: the bytes after its last row — the table's
    end and the page's sorting script, 5,633 bytes — never arrive (measured
    2026-10-13 on every listing over ~150 KB; over HTTP/2 they do).  A page
    cut right after a complete row with EXACTLY that tail missing (``tail``,
    measured on a complete page of the same listing run) is taken as it is;
    any other early end is a failed listing."""
    import httpx
    buf = bytearray()
    with client.stream("GET", url) as r:
        if r.status_code == 404:
            raise FileNotFoundError(rel)
        r.raise_for_status()
        try:
            for chunk in r.iter_raw():
                buf += chunk
        except httpx.RemoteProtocolError:
            try:
                missing = int(r.headers.get("content-length") or 0) - len(buf)
            except ValueError:
                missing = -1
            if not (tail and missing == tail and bytes(buf).endswith(b"</tr>")
                    and r.headers.get("content-encoding") in (None, "", "identity")):
                raise
            log.debug("scene.org: %s/ listing ended %d bytes early, after its last row",
                      rel, missing)
    return bytes(buf)


def _words(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip()


def _stem(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


class SceneOrgOnline(OnlineCatalogue):
    key = "sceneorg"
    label = "scene.org"
    root = BASE
    site_url = "https://files.scene.org/browse/music/"
    default_interval = "monthly"
    min_rows = 1_000
    packaged = True
    build_rules = 2                 # 2: no scene_path (that names a Modland path)
    index_fill_fields = ("scene_group",)
    owned_fields = ("scene_group",)
    description = ("scene.org's music archive — artists, groups, compos, music disks and "
                   "compilations")
    how = ("Listed from scene.org's folders, one entry per file (the first listing takes a "
           "while; later ones list only what changed); a file is fetched when you play it or "
           "open its Track Info, and the tunes inside it take its place.")

    def __init__(self) -> None:
        super().__init__()
        self._run_stop: threading.Event | None = None
        self._flags: bool = False

    def signal_stop(self) -> None:
        stop = self._run_stop
        if stop is not None:
            stop.set()

    def prepare_build(self) -> None:
        from soniqboom.core import genre_derive
        self._flags = genre_derive.names_enabled()

    def seen_for(self, extra: dict | None, now: int) -> int:
        """A file found by a later listing dates from its listing date — a
        folder that couldn't be listed before brings old files, not new ones."""
        import calendar
        m = (extra or {}).get("m")
        try:
            ts = calendar.timegm(time.strptime(str(m), "%Y-%b-%d %H:%M:%S"))
        except (TypeError, ValueError):
            return now
        return min(now, int(ts))

    # Index (the folder listings) --------------------------------------------
    def crawl_path(self) -> Path:
        return self.dir() / "crawl.json"

    def _load_crawl(self) -> dict:
        try:
            data = json.loads(self.crawl_path().read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_crawl(self, data: dict) -> None:
        p = self.crawl_path()
        tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(data, separators=(",", ":")), "utf-8")
            os.replace(tmp, p)
        except OSError:
            tmp.unlink(missing_ok=True)
            log.debug("scene.org: listing state not saved", exc_info=True)

    def fetch_index(self, workdir: Path, info: dict):
        import httpx
        from soniqboom.core import archive, metadata
        from urllib.parse import quote
        stop = self._run_stop = threading.Event()     # this run's own (a cancelled
        prev = self._load_crawl()                      # job's thread checks only its own)
        failed_before = set(prev.pop(_FAILED_KEY, None) or ())
        seen: dict = {}            # dir rel → {"m": date, "d": [subdirs], "f": [[name, size, date]]}
        failed: list = []
        fail_dates: dict = {}
        stats = {"listed": 0, "kept": 0, "found": 0}
        last = [0.0]
        tail: list = [None]
        in_a_row = [0]                 # folders in a row not listed (the circuit breaker)

        def _pause(seconds: float) -> None:
            left = float(seconds)
            while left > 0:                     # (in steps: a stop ends it)
                if stop.is_set():
                    raise CrawlStopped("the listing was stopped")
                step = min(1.0, left)
                time.sleep(step)
                left -= step

        def _get(client, rel: str, tries: int = 3) -> str:
            for attempt in range(tries):
                if stop.is_set():
                    raise CrawlStopped("the listing was stopped")
                wait = CRAWL_GAP_S - (time.monotonic() - last[0])
                if wait > 0:
                    time.sleep(wait)
                last[0] = time.monotonic()
                try:
                    page = _listing_text(client, f"{BASE}/{quote(rel)}/" if rel else f"{BASE}/",
                                         rel, tail[0])
                    return page.decode("utf-8", "replace")
                except FileNotFoundError:
                    raise
                except Exception as exc:                # noqa: BLE001
                    ra = online_catalog.retry_after_of(exc)
                    if ra is not None and ra > _RETRY_AFTER_WAIT_MAX_S:
                        raise CrawlBroken(f"scene.org asked for a pause of {ra:.0f} s",
                                          retry_after=ra) from exc
                    if attempt == tries - 1:
                        raise
                    # A ``Retry-After`` is honoured; else a growing pause.
                    _pause(ra if ra is not None else 2.0 * (attempt + 1))
            raise RuntimeError("unreachable")

        def _visit(client, rel: str, date: str | None, tries: int = 3) -> None:
            old = prev.get(rel)
            if (old is not None and date and old.get("m") == date and not old.get("d")):
                seen[rel] = old                          # a leaf folder, unchanged
                stats["kept"] += 1
                return
            try:
                dirs, files = parse_listing(_get(client, rel, tries))
            except (CrawlStopped, CrawlBroken):
                raise
            except Exception as exc:                    # noqa: BLE001
                # (A folder its parent lists answering 404 is the server's
                # trouble too: its tunes stay, it counts as not listed — and
                # is named in the collection's status, never dropped silently.)
                log.info("scene.org: %s/ not listed (%s)", rel,
                         "not found" if isinstance(exc, FileNotFoundError) else exc)
                failed.append(rel)
                fail_dates[rel] = date
                if old is not None:
                    _keep_subtree(rel)
                # (A folder that failed the last listing too, or answers 404,
                # is that folder's trouble — not counted as the server's.)
                if not isinstance(exc, FileNotFoundError) and rel not in failed_before:
                    in_a_row[0] += 1
                if tries > 1 and in_a_row[0] >= _BREAKER_FOLDERS:
                    raise CrawlBroken(f"scene.org couldn't list {in_a_row[0]} folders in a "
                                      f"row ({exc})", retry_after=online_catalog.retry_after_of(exc))
                return
            in_a_row[0] = 0
            stats["listed"] += 1
            seen[rel] = {"m": date, "d": [d for d, _m in dirs],
                         "f": [[n, s, m] for n, s, m in files]}
            if stats["listed"] % 50 == 0:
                self._save_crawl({**prev, **seen})      # a broken listing resumes from here
            stats["found"] += len(dirs)
            self._progress("listing folders", stats["listed"] + stats["kept"],
                           max(len(prev), stats["found"]))
            for d, m in dirs:
                _visit(client, f"{rel}/{d}" if rel else d, m)

        def _keep_subtree(rel: str) -> None:
            for k, v in prev.items():
                if k == rel or k.startswith(rel + "/"):
                    seen.setdefault(k, v)

        with httpx.Client(timeout=httpx.Timeout(30.0, read=60.0), follow_redirects=True,
                          headers={"User-Agent": user_agent(),
                                   "Accept-Encoding": "identity"}) as client:
            root_page = _get(client, "")
            tail[0] = page_tail(root_page.encode("utf-8"))   # (the short root page is whole)
            dirs, _files = parse_listing(root_page)
            tops = [(d, m) for d, m in dirs if d in TOPS]
            if not tops:
                raise ValueError("scene.org's music folder lists none of its sections")
            stats["found"] = len(tops)
            try:
                for d, m in tops:
                    _visit(client, d, m)
                if failed:
                    # Once more, after a pause: most failures are a moment's.
                    again, failed[:] = list(failed), []
                    _pause(min(10.0, 20 * CRAWL_GAP_S))
                    for rel in again:
                        _visit(client, rel, fail_dates.get(rel), tries=1)
            except CrawlBroken:
                # What was listed is kept for the next attempt (which resumes
                # from it); nothing else is asked of the server now.
                self._save_crawl({**prev, **seen,
                                  _FAILED_KEY: sorted(set(failed) | failed_before)})
                raise
        total = stats["listed"] + stats["kept"] + len(failed)
        # A folder never listed before whose listing failed again: its files
        # (and its subfolders' — a whole section, maybe) would be missing
        # without a word.  Refused — the next attempt resumes from what was
        # listed — unless it failed the last time too (a folder that can't be
        # listed at all must not block the collection).
        new_failures = [r for r in failed if r not in prev and r not in failed_before]
        if new_failures or len(failed) > max(5, total * _MAX_FAILED_SHARE):
            self._save_crawl({**prev, **seen, _FAILED_KEY: sorted(set(failed))})
            msg = (f"{len(failed)} scene.org folder(s) could not be listed "
                   f"({', '.join(failed[:3])}…) — tried again later")
            if not new_failures:
                # The same folders failing again, too many to leave out: a
                # refusal (a day's wait, doubling), not an hour's.
                raise ListingRefused(msg)
            raise ConnectionError(msg)
        self._save_crawl({**seen, _FAILED_KEY: sorted(set(failed))} if failed else seen)
        rows = []
        playable = metadata.SUPPORTED_EXTENSIONS
        for rel, ent in seen.items():
            for name, size, date in ent.get("f") or ():
                ext = os.path.splitext(name)[1].lower()
                if not (archive.is_archive_name(name) or ext in playable):
                    continue
                rows.append((f"{rel}/{name}", 0, {"approx": int(size or 0), "m": date}))
        log.info("scene.org: %d folders listed, %d unchanged, %d failed — %d files",
                 stats["listed"], stats["kept"], len(failed), len(rows))
        return rows, {"index_date": time.strftime("%Y-%m-%d", time.gmtime()),
                      "folders": stats["listed"] + stats["kept"]}

    def unavailable_folders(self) -> list:
        """The folders the last listing couldn't read (their tunes are kept
        from the listing before)."""
        try:
            return sorted(self._load_crawl().get(_FAILED_KEY) or ())
        except Exception:                               # noqa: BLE001
            return []

    # Packages ---------------------------------------------------------------
    def package_marker(self, size: int, extra: dict | None) -> str:
        ex = extra or {}
        return f"{int(ex.get('approx') or size or 0)}|{ex.get('m') or ''}"

    # Tracks -----------------------------------------------------------------
    def build_track(self, rel, size, extra, *, added_at, dir_hash, root_h) -> dict:
        from soniqboom.core import archive, genre_derive, metadata
        from soniqboom.core import modland_catalog as mc
        from soniqboom.core.store import share_values
        ex = extra or {}
        pkg_rel = rel.split("::", 1)[0]
        parts = pkg_rel.split("/")
        pkg_name = parts[-1]
        member = ex.get("member")
        t: dict = {"id": self.row_id(rel, ex), "path": self.track_path(rel),
                   "added_at": int(added_at), "dir_hash": dir_hash, "scan_root_hash": root_h,
                   "is_duplicate_primary": True}
        fam = None
        if member:
            shown = re.split(r"[\\/]", str(member).rsplit("::", 1)[-1])[-1]
            t["title"] = metadata.title_from_filename(shown) or _stem(shown)
            t["format"] = mc.format_for(shown)
            fam = mc.family_genre(shown)
            t["file_size"] = int(size or 0)              # (0: not known before its play)
        elif archive.is_archive_name(pkg_name):
            t["title"] = _words(_stem(pkg_name))
            t["format"] = "LHA package" if archive.is_lha_name(pkg_name) else "ZIP package"
            t["file_size"] = int(ex.get("approx") or size or 0)
        else:
            t["title"] = _words(metadata.title_from_filename(pkg_name) or _stem(pkg_name))
            t["format"] = mc.format_for(pkg_name)
            fam = mc.family_genre(pkg_name)
            t["file_size"] = int(ex.get("approx") or size or 0)
        top = parts[0]
        album = ""
        if top == "artists" and len(parts) >= 3:
            t["artist"] = _words(parts[1])
        elif top == "groups" and len(parts) >= 3:
            t["scene_group"] = _words(parts[1])
        elif top in ("compos", "disks") and len(parts) >= 3:
            album = _words(parts[1])
        if not album and (top in ("disks", "compilations")
                          or (member and int(ex.get("members") or 1) > 1)):
            album = _words(_stem(pkg_name))
        if album:
            t["album"] = album
        if fam:
            t["genre"] = list(fam)
        g = genre_derive.derive(t, names=self._flags, roots=(self.root,))
        if g:
            t["genre_derived"] = g
        return share_values(t)

    def changed(self, old: dict, rel: str, size: int, extra) -> bool:
        ex = extra or {}
        if ex.get("member") or is_merged(old):
            return False
        return (old.get("file_size") or 0) != int(ex.get("approx") or 0)

    def merged_fields(self, d: dict, old: dict, rel: str) -> dict:
        for f in ("album", "scene_group"):
            if old.get(f) and not d.get(f):
                d[f] = old[f]
        return d


SCENEORG_ONLINE = online_catalog.register(SceneOrgOnline())
