# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Chapter extraction for podcasts / audiobooks / long tracks.

Two formats are supported, covering the realistic ~95% of files:

* **MP4 / M4A / M4B** — Apple's ``chpl`` atom (single-track text chapter
  list) and the ``nero`` / ``QuickTime`` style chapter track.  mutagen
  exposes both via ``MP4.chapters``.
* **MP3** — ID3v2 ``CHAP`` frames (per-chapter start/end + ``TIT2``
  embedded).  mutagen surfaces these as ``id3['CHAP:<id>']``.

The output is a uniform list of ``{title, start, end?}`` dicts (seconds).
Empty list when the file has no chapters.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def extract_chapters(path: Path) -> list[dict]:
    """Return ``[{title, start, end?}, …]`` in seconds, or ``[]``."""
    ext = path.suffix.lower()
    try:
        if ext in (".m4a", ".m4b", ".mp4"):
            return _from_mp4(path)
        if ext == ".mp3":
            return _from_mp3(path)
    except Exception:
        log.exception("chapter extract failed for %s", path)
    return []


def _from_mp4(path: Path) -> list[dict]:
    from mutagen.mp4 import MP4
    mp4 = MP4(str(path))
    chapters = getattr(mp4, "chapters", None) or []
    out: list[dict] = []
    for ch in chapters:
        # mutagen.mp4.Chapter has ``start`` (float seconds) and ``title``.
        out.append({
            "title": (getattr(ch, "title", "") or "").strip() or None,
            "start": float(getattr(ch, "start", 0.0)),
        })
    # Fill in ``end`` from the next chapter's start (or track duration).
    if out:
        track_dur = getattr(mp4.info, "length", 0.0) or 0.0
        for i, ch in enumerate(out):
            ch["end"] = (out[i + 1]["start"] if i + 1 < len(out)
                         else float(track_dur))
    return out


def _from_mp3(path: Path) -> list[dict]:
    from mutagen.id3 import ID3
    try:
        tags = ID3(path)
    except Exception:
        return []
    # CHAP frames carry start/end (ms) + nested TIT2 sub-frame for title.
    out: list[dict] = []
    for key in tags.keys():
        if not str(key).startswith("CHAP"):
            continue
        chap = tags[key]
        title_frame = chap.sub_frames.get("TIT2") if hasattr(chap, "sub_frames") else None
        title = ""
        if title_frame:
            try: title = str(title_frame.text[0])
            except Exception: title = ""
        out.append({
            "title": title.strip() or None,
            "start": float(getattr(chap, "start_time", 0)) / 1000.0,
            "end":   float(getattr(chap, "end_time",   0)) / 1000.0,
        })
    out.sort(key=lambda c: c["start"])
    return out
