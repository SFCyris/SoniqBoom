# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The HTML shells (``index.html``, ``mobile.html``, ``multiroom.html``) as
served: every frontend JavaScript file is named by a URL that carries its
content hash (``/assets/js/player.js?v=<hash>``).

* Each ``"/assets/js/<file>.js"`` (with or without a hand-set ``?v=N``) quoted
  in the shell — the entry ``<script>``, the ``modulepreload`` links, classic
  scripts — is rewritten to its hashed URL.
* An import map with one entry per file goes before the first module
  script or ``modulepreload`` link, so the modules' own
  ``import … from './player.js'`` and ``import('./admin.js')`` resolve to
  the hashed URLs too.

A module whose content changes gets a new URL, so a browser that keeps
modules by URL across reloads (WebKit's memory cache) loads the new one on
the next page load.  Nothing is bumped by hand; the hashes follow the files
on disk (a file's hash is read again when its size or mtime changes).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

__all__ = ["module_versions", "render_shell", "shell_etag"]

# ``"/assets/js/x.js"`` or ``'/assets/js/sub/x.js?v=12'`` — a quoted URL.
_JS_URL_RE = re.compile(
    r"""(?P<q>["'])/assets/js/(?P<rel>[A-Za-z0-9_\-./]+?\.js)(?:\?v=[A-Za-z0-9_.\-]*)?(?P=q)""")
# The first thing that loads a module: the import map must come before it.
_FIRST_MODULE_RE = re.compile(
    r"""<link\b[^>]*\brel=["']modulepreload["']|<script\b[^>]*\btype=["']module["']""", re.IGNORECASE)
_HEAD_END_RE = re.compile(r"</head\s*>", re.IGNORECASE)

_lock = threading.Lock()
_file_hashes: dict[str, tuple[int, int, str]] = {}      # rel → (size, mtime_ns, hash)
_rendered: dict[str, tuple[tuple, bytes, str]] = {}      # shell path → (key, body, etag)


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def module_versions(js_dir: Path) -> dict[str, str]:
    """``{rel path under js_dir: content hash}`` for every ``.js`` file."""
    js_dir = Path(js_dir)
    out: dict[str, str] = {}
    if not js_dir.is_dir():
        return out
    for root, dirs, files in os.walk(js_dir):
        dirs.sort()
        for name in sorted(files):
            if not name.endswith(".js"):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, js_dir).replace(os.sep, "/")
            try:
                st = os.stat(full)
            except OSError:
                continue
            with _lock:
                known = _file_hashes.get(full)
            if known and known[0] == st.st_size and known[1] == st.st_mtime_ns:
                out[rel] = known[2]
                continue
            try:
                h = _hash_bytes(Path(full).read_bytes())
            except OSError:
                continue
            with _lock:
                _file_hashes[full] = (st.st_size, st.st_mtime_ns, h)
            out[rel] = h
    return out


def _import_map(versions: dict[str, str]) -> str:
    imports = {f"/assets/js/{rel}": f"/assets/js/{rel}?v={h}" for rel, h in sorted(versions.items())}
    # ``</`` can't end the <script> element early: JSON allows "\/".
    body = json.dumps({"imports": imports}, separators=(",", ":")).replace("</", "<\\/")
    return f'<script type="importmap">{body}</script>'


def render_shell(html_path: Path, js_dir: Path) -> tuple[bytes, str]:
    """The shell at ``html_path`` with its JS URLs hashed and the import map
    added, and its ETag.  Re-rendered only when the shell or a JS file changed."""
    html_path = Path(html_path)
    versions = module_versions(js_dir)
    st = os.stat(html_path)
    key = (st.st_size, st.st_mtime_ns, tuple(sorted(versions.items())))
    with _lock:
        hit = _rendered.get(str(html_path))
    if hit and hit[0] == key:
        return hit[1], hit[2]
    text = html_path.read_text(encoding="utf-8")

    def _sub(m: re.Match) -> str:
        rel = m.group("rel")
        h = versions.get(rel)
        if h is None:                      # not a file we serve: leave it as written
            return m.group(0)
        q = m.group("q")
        return f"{q}/assets/js/{rel}?v={h}{q}"

    text = _JS_URL_RE.sub(_sub, text)
    tag = _import_map(versions)
    first = _FIRST_MODULE_RE.search(text) or _HEAD_END_RE.search(text)
    at = first.start() if first else 0
    text = text[:at] + tag + "\n  " + text[at:]
    body = text.encode("utf-8")
    etag = f'"{_hash_bytes(body)}"'
    with _lock:
        _rendered[str(html_path)] = (key, body, etag)
    return body, etag


def shell_etag(if_none_match: str | None, etag: str) -> bool:
    """True when ``If-None-Match`` names ``etag`` (the browser's copy is current)."""
    if not if_none_match:
        return False
    tags = [t.strip() for t in if_none_match.split(",")]
    bare = etag.strip('"')
    return any(t == "*" or t.removeprefix("W/").strip('"') == bare for t in tags)
