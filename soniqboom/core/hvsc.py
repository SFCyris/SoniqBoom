# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""HVSC integration — per-tune song lengths + STIL commentary for SID files.

The High Voltage SID Collection ships two flat text databases with
every release:

* **Songlengths.md5** — maps the MD5 of a ``.sid`` file to one or more
  per-subsong durations.
* **STIL.txt** — the SID Tune Information List: per-tune trivia,
  composer notes, sample sources, and commentary.

Pointing SoniqBoom at the HVSC ``DOCUMENTS/`` folder upgrades every
matching SID track with accurate per-subsong durations and the rich
STIL background.  Without HVSC, SID tracks fall back to the user's
``sid_default_duration`` setting (the old behaviour).

This module is **lookup-only** — it doesn't ship the database; the user
configures the path under Admin → Renderers → HVSC.  We index both files
once on load and answer point queries in O(1).
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# Regex for a Songlengths.md5 entry:
#   <md5-hex>=M:SS M:SS M:SS ...
# Older releases use ``M:SS.ms``; newer ones add subsong duration only.
_SONGLENGTH_RE = re.compile(r"^([0-9a-fA-F]{32})=(.+)$")
_DURATION_RE   = re.compile(r"(\d+):(\d+)(?:\.(\d+))?")


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


class HVSC:
    """Lazy-loaded HVSC database.  Thread-safe."""

    def __init__(self) -> None:
        self._songlengths: dict[str, list[float]] = {}
        self._stil:        dict[str, dict]        = {}
        self._docs_path:   Path | None            = None
        self._loaded:      bool                   = False
        self._lock = threading.Lock()

    # ── Configuration ───────────────────────────────────────────────────

    def configure(self, docs_path: str | Path | None) -> None:
        """Point at the HVSC ``DOCUMENTS/`` folder (containing
        ``Songlengths.md5`` + ``STIL.txt``).  Pass ``None`` to disable."""
        with self._lock:
            self._songlengths.clear()
            self._stil.clear()
            self._loaded = False
            self._docs_path = Path(docs_path) if docs_path else None

    def is_configured(self) -> bool:
        return self._docs_path is not None

    # ── Loaders ─────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            if not self._docs_path or not self._docs_path.is_dir():
                self._loaded = True
                return
            try:
                self._load_songlengths(self._docs_path / "Songlengths.md5")
                self._load_stil(self._docs_path / "STIL.txt")
            except Exception:
                log.exception("HVSC: load failed; lookups will return empty")
            self._loaded = True
            log.info(
                "HVSC: indexed %d song-length entries + %d STIL entries from %s",
                len(self._songlengths), len(self._stil), self._docs_path,
            )

    def _load_songlengths(self, path: Path) -> None:
        if not path.is_file():
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
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
                    self._songlengths[md5] = durations

    def _load_stil(self, path: Path) -> None:
        """STIL.txt is a flat plain-text doc.  Entries start with a line
        beginning with ``/`` (HVSC-relative path) and continue until the
        next blank line followed by another ``/`` line."""
        if not path.is_file():
            return
        # Format example:
        #   /MUSICIANS/H/Hubbard_Rob/Commando.sid
        #   #5
        #     TITLE: Commando
        #    ARTIST: Rob Hubbard
        #    AUTHOR: Rob Hubbard
        #
        # We capture the entry text per HVSC-relative path.  We *don't*
        # try to parse the sub-keys here — the raw text is plenty for a
        # "STIL Commentary" panel in Track Info; clients that want
        # structure can split later.
        current_key: str | None = None
        buffer: list[str] = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                if raw.startswith("/") and not raw.startswith("//"):
                    if current_key and buffer:
                        self._stil[current_key.lower()] = {
                            "text": "".join(buffer).strip(),
                        }
                    current_key = raw.strip()
                    buffer = []
                else:
                    if current_key:
                        buffer.append(raw)
            # flush the last entry
            if current_key and buffer:
                self._stil[current_key.lower()] = {"text": "".join(buffer).strip()}

    # ── Lookups ─────────────────────────────────────────────────────────

    def lookup_durations(self, path: Path) -> list[float]:
        """Per-subsong durations for a ``.sid``/``.psid`` file, or [] if
        unknown.  Result list length matches the number of subsongs the
        HVSC database knows about (which may exceed PSID-header
        ``subsongs`` when the file has been retouched)."""
        self._ensure_loaded()
        if not self._songlengths:
            return []
        try:
            return list(self._songlengths.get(_file_md5(path), []))
        except OSError:
            return []

    def lookup_stil(self, sid_path: Path, hvsc_root: Path | None = None) -> dict | None:
        """STIL entry for a given SID file.  ``hvsc_root`` is the HVSC
        installation root (parent of ``DOCUMENTS/``); STIL keys are
        relative to it (``/MUSICIANS/.../foo.sid``).  If ``hvsc_root`` is
        ``None`` we try to derive it from the configured docs path."""
        self._ensure_loaded()
        if not self._stil:
            return None
        root = hvsc_root or (self._docs_path.parent if self._docs_path else None)
        if not root:
            return None
        try:
            rel = sid_path.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            return None
        key = "/" + str(rel).replace("\\", "/")
        return self._stil.get(key.lower())


# Module-level singleton.
_hvsc = HVSC()


def get_hvsc() -> HVSC:
    return _hvsc
