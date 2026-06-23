# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""HVSC integration — per-tune song lengths + STIL commentary for SID files.

The High Voltage SID Collection ships two flat text databases with
every release, under its ``DOCUMENTS/`` folder:

* **Songlengths.md5** — maps the MD5 of a ``.sid`` file to one or more
  per-subsong durations.  (Older releases name it ``Songlengths.txt``.)
* **STIL.txt** — the SID Tune Information List: per-tune trivia,
  composer notes, sample sources, and commentary.

Pointing SoniqBoom at the HVSC ``DOCUMENTS/`` folder upgrades every
matching SID track with accurate per-subsong durations and the rich
STIL background.  Without HVSC, SID tracks fall back to the user's
``sid_default_duration`` setting (the old behaviour).

The two databases match by DIFFERENT keys, which shapes everything:

* **Song lengths** are keyed by the **MD5 of the whole .sid file** — so
  they resolve for any SID file regardless of where it lives, even
  renamed or on a remote share.  We cache that MD5 on the track at scan
  time (``track.sid_md5``) so re-applying HVSC is a pure in-memory join.
* **STIL** is keyed by the **HVSC-relative path** (``/MUSICIANS/...``) —
  so it only resolves when the SID files sit at their canonical paths
  under an HVSC root.

This module is **lookup-only** for the databases — it doesn't ship them;
the path is configured under Admin → Renderers → HVSC, or auto-detected
during a scan (:func:`auto_configure`).  The ``DOCUMENTS/`` folder may be
local OR on a remote (FTP/SMB) mount — reads go through the FileSource
abstraction in that case.  We index both files once on load and answer
point queries in O(1).
"""
from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Accepted Songlengths filenames (HVSC renamed it across releases) and the
# fixed STIL filename.  ``DOCUMENTS`` is the canonical containing folder.
SONGLENGTHS_NAMES = ("Songlengths.md5", "Songlengths.txt")
STIL_NAME = "STIL.txt"
DOCS_DIR_NAME = "DOCUMENTS"

_REMOTE_PREFIXES = ("ftp://", "smb://")

# Regex for a Songlengths.md5 entry:
#   <md5-hex>=M:SS M:SS M:SS ...
# Older releases use ``M:SS.ms``; newer ones add subsong duration only.
_SONGLENGTH_RE = re.compile(r"^([0-9a-fA-F]{32})=(.+)$")
_DURATION_RE   = re.compile(r"(\d+):(\d+)(?:\.(\d+))?")

_autoconf_lock = threading.Lock()


def _parse_duration(text: str) -> float:
    m = _DURATION_RE.fullmatch(text.strip())
    if not m:
        return 0.0
    minutes = int(m.group(1))
    seconds = int(m.group(2))
    millis  = int(m.group(3) or "0")
    return minutes * 60 + seconds + millis / 1000.0


def _file_md5(path: Path, chunk: int = 65536) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def is_songlengths_basename(name: str) -> bool:
    """True if ``name`` is an HVSC Songlengths file (case-insensitive)."""
    lname = name.lower()
    return any(lname == n.lower() for n in SONGLENGTHS_NAMES)


def _is_remote(path_str: str) -> bool:
    return path_str.startswith(_REMOTE_PREFIXES)


class HVSC:
    """Lazy-loaded HVSC database.  Thread-safe.  Local or remote docs path."""

    def __init__(self) -> None:
        self._songlengths: dict[str, list[float]] = {}
        self._stil:        dict[str, dict]        = {}
        self._docs_path:   str | None             = None
        self._remote:      bool                   = False
        self._loaded:      bool                   = False
        self._lock = threading.Lock()

    # ── Configuration ───────────────────────────────────────────────────

    def configure(self, docs_path: str | Path | None) -> None:
        """Point at the HVSC ``DOCUMENTS/`` folder (containing
        ``Songlengths.md5`` + ``STIL.txt``).  May be a local path or a
        remote ``ftp://``/``smb://`` URL.  Pass ``None`` to disable."""
        with self._lock:
            self._songlengths.clear()
            self._stil.clear()
            self._loaded = False
            if docs_path:
                self._docs_path = str(docs_path)
                self._remote = _is_remote(self._docs_path)
            else:
                self._docs_path = None
                self._remote = False

    def reload(self) -> None:
        """Drop the indexed databases so the next lookup re-reads them from
        disk/share — used by the re-apply action to pick up an updated HVSC
        release without restarting."""
        with self._lock:
            self._songlengths.clear()
            self._stil.clear()
            self._loaded = False

    def is_configured(self) -> bool:
        return self._docs_path is not None

    @property
    def docs_path(self) -> str | None:
        return self._docs_path

    def hvsc_root(self) -> str | None:
        """The HVSC installation root (parent of ``DOCUMENTS/``), as a string
        (local path or remote URL).  STIL paths are relative to this."""
        if not self._docs_path:
            return None
        stripped = self._docs_path.rstrip("/")
        if "/" not in stripped:                  # bare name, not a real path
            return None
        parent = stripped.rsplit("/", 1)[0]
        return parent if parent else "/"         # root-level install → "/"

    # ── Loaders ─────────────────────────────────────────────────────────

    def _read_doc(self, name: str, docs_path: str, remote: bool) -> str | None:
        """Read one DOCUMENTS file as text — local open or remote FileSource.

        Takes ``docs_path``/``remote`` as parameters (the caller's snapshot)
        rather than reading ``self._*`` so a concurrent ``configure()`` can't
        swap the path between the two reads of one load."""
        if not docs_path:
            return None
        if remote:
            try:
                from soniqboom.core.filesource import (
                    parse_remote_path, get_source,
                )
                scan_root, docs_remote = parse_remote_path(docs_path)
                source = get_source(scan_root)
                if source is None:
                    return None
                remote_file = posixpath.join(docs_remote, name)
                data = source.read_file(remote_file, lane="scan")
                return data.decode("utf-8", "replace")
            except Exception:
                return None
        p = Path(docs_path) / name
        if not p.is_file():
            return None
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        # Snapshot the configured path under the lock, then do the (possibly
        # slow, possibly REMOTE) reads + parse WITHOUT holding it — otherwise a
        # 3–4 MB Songlengths.md5 read over FTP would block every concurrent SID
        # lookup.  Only the final dict swap is done under the lock.
        with self._lock:
            if self._loaded:
                return
            docs_path = self._docs_path
            remote = self._remote
        if not docs_path:
            with self._lock:
                if not self._docs_path:          # still unconfigured
                    self._loaded = True
            return

        songlengths_text = None
        for name in SONGLENGTHS_NAMES:
            songlengths_text = self._read_doc(name, docs_path, remote)
            if songlengths_text:
                break
        stil_text = self._read_doc(STIL_NAME, docs_path, remote)

        songlengths: dict[str, list[float]] = {}
        stil: dict[str, dict] = {}
        try:
            if songlengths_text:
                songlengths = self._parse_songlengths(songlengths_text)
            if stil_text:
                stil = self._parse_stil(stil_text)
        except Exception:
            log.exception("HVSC: parse failed; lookups will return empty")

        with self._lock:
            # Don't commit if another thread already loaded, OR if the config
            # changed under us (configure()/reload() swapped the path) — in that
            # case our data is for a stale path; leave _loaded False so the next
            # lookup reloads against the current path.
            if self._loaded or self._docs_path != docs_path:
                return
            self._songlengths = songlengths
            self._stil = stil
            self._loaded = True
            log.info(
                "HVSC: indexed %d song-length entries + %d STIL entries from %s",
                len(songlengths), len(stil), docs_path,
            )

    @staticmethod
    def _parse_songlengths(text: str) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for raw in text.splitlines():
            m = _SONGLENGTH_RE.match(raw.strip())
            if not m:
                continue
            md5 = m.group(1).lower()
            durations = [
                _parse_duration(tok)
                for tok in m.group(2).split()
                if _DURATION_RE.fullmatch(tok)
            ]
            if durations:
                out[md5] = durations
        return out

    @staticmethod
    def _parse_stil(text: str) -> dict[str, dict]:
        """STIL.txt is a flat plain-text doc.  Entries start with a line
        beginning with ``/`` (HVSC-relative path) and continue until the
        next such line.  A leading documentation header (lines before the
        first ``/path``) is ignored because ``current_key`` is still None."""
        out: dict[str, dict] = {}
        # Format example:
        #   /MUSICIANS/H/Hubbard_Rob/Commando.sid
        #   #5
        #     TITLE: Commando
        #    ARTIST: Rob Hubbard
        #    AUTHOR: Rob Hubbard
        #
        # We capture the entry text per HVSC-relative path; we don't parse
        # the sub-keys (TITLE/ARTIST/COMMENT) — the raw text is plenty for a
        # "STIL Commentary" panel, and we deliberately do NOT overwrite the
        # PSID-header title/artist from it.
        current_key: str | None = None
        buffer: list[str] = []
        for raw in text.splitlines(keepends=True):
            if raw.startswith("/") and not raw.startswith("//"):
                if current_key and buffer:
                    out[current_key.lower()] = {"text": "".join(buffer).strip()}
                current_key = raw.strip()
                buffer = []
            elif current_key:
                buffer.append(raw)
        if current_key and buffer:
            out[current_key.lower()] = {"text": "".join(buffer).strip()}
        return out

    # ── Lookups ─────────────────────────────────────────────────────────

    def lookup_durations_by_md5(self, md5: str) -> list[float]:
        """Per-subsong durations for a SID file given its (whole-file) MD5,
        or ``[]``.  Pure in-memory — no file I/O.  This is the preferred
        path: the MD5 is cached on the track at scan time."""
        self._ensure_loaded()
        if not md5:
            return []
        with self._lock:                         # guard against a concurrent reload()
            return list(self._songlengths.get(md5.lower(), []))

    def lookup_durations(self, path: Path) -> list[float]:
        """Per-subsong durations for a local ``.sid``/``.psid`` file, or [].
        Convenience wrapper that MD5s the file then delegates."""
        try:
            return self.lookup_durations_by_md5(_file_md5(path))
        except OSError:
            return []

    def stil_key_for(self, track_path: str) -> str | None:
        """The HVSC-relative STIL key (``/MUSICIANS/.../foo.sid``) for a
        track, or None if it can't be resolved relative to the HVSC root.

        Handles both local absolute paths and remote ``scan_root:/rel``
        paths — but only when the track and the HVSC root are on the SAME
        side (both local, or both the same remote share).  STIL is
        path-keyed, so a track that doesn't sit under the HVSC tree (or a
        local track with a remote HVSC root) simply has no STIL entry."""
        root = self.hvsc_root()
        if not root or not track_path:
            return None
        track_remote = _is_remote(track_path)
        root_remote = _is_remote(root)
        if track_remote != root_remote:
            return None
        if root_remote:
            try:
                from soniqboom.core.filesource import parse_remote_path
                t_root, t_rel = parse_remote_path(track_path)
                r_root, r_rel = parse_remote_path(root)
            except Exception:
                return None
            if t_root != r_root:
                return None
            r_rel = "/" + r_rel.strip("/")
            t_rel = "/" + t_rel.strip("/")
            prefix = r_rel.rstrip("/")
            if prefix and prefix != "/" and not t_rel.startswith(prefix + "/"):
                return None
            rel = t_rel[len(prefix):] if prefix not in ("", "/") else t_rel
            return rel or None
        # Local: resolve both to absolute real paths for a robust compare.
        try:
            rel = os.path.relpath(os.path.realpath(track_path),
                                  os.path.realpath(root))
        except (ValueError, OSError):
            return None
        if rel.startswith(".."):
            return None
        return "/" + rel.replace(os.sep, "/")

    def lookup_stil_by_relpath(self, rel: str) -> dict | None:
        self._ensure_loaded()
        if not rel:
            return None
        with self._lock:                         # guard against a concurrent reload()
            return self._stil.get(rel.lower())

    def lookup_stil(self, sid_path: Path | str, hvsc_root=None) -> dict | None:
        """STIL entry for a SID file at ``sid_path`` (local path or remote
        ``scan_root:/rel`` string).  ``hvsc_root`` is accepted for
        backward-compatibility but ignored — the configured docs path
        determines the root."""
        key = self.stil_key_for(str(sid_path))
        if key is None:
            return None
        return self.lookup_stil_by_relpath(key)


# Module-level singleton.
_hvsc = HVSC()


def get_hvsc() -> HVSC:
    return _hvsc


def auto_configure(docs_path: str) -> bool:
    """Persist an auto-detected HVSC ``DOCUMENTS`` path to config and
    (re)configure the singleton — but ONLY when no path is set yet, so we
    never silently override a path the user chose.  Returns True if applied.

    Thread-safe: serialised so concurrent scan workers that both spot the
    DOCUMENTS folder don't double-write.
    """
    if not docs_path:
        return False
    with _autoconf_lock:
        try:
            from soniqboom.config import (
                settings, load_local_conf, save_local_conf,
            )
            if (getattr(settings, "hvsc_docs_path", "") or "").strip():
                return False  # already configured — leave it alone
            conf = load_local_conf()
            conf.setdefault("renderers", {})
            conf["renderers"]["hvsc_docs_path"] = docs_path
            save_local_conf(conf)
            settings.hvsc_docs_path = docs_path
            _hvsc.configure(docs_path)
            log.info("HVSC: auto-configured DOCUMENTS path → %s", docs_path)
            return True
        except Exception:
            log.exception("HVSC: auto-configure failed for %s", docs_path)
            return False
