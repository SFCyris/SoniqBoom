# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Turning the online collections and the scene information on and off from
outside the web app: the installer's two questions (``soniqboom
first-run-setup``), ``soniqboom collections add|remove`` / ``soniqboom
scene-info add|remove`` at any time (``docker exec`` in a container), and a new
Docker deployment's first start (``SONIQBOOM_COLLECTIONS`` /
``SONIQBOOM_SCENE_INFO``, both "yes" when unset).

A request is written to SoniqBoom.conf (``setup.pending`` to add,
``setup.removing`` to remove; the later request for a name replaces the
earlier) — the CLI never opens the library, which the running server owns.
The server applies the requests when it starts and picks up new ones while it
runs (``start``): each one does what its Settings switch or button does (the
admin API's handlers), and a removal of scene information also deletes its
download.  What is already there (``already``: a collection on or held by a
root folder of the library — a network share of its site or a mirror, a
local copy — scene information downloaded or in use from elsewhere) is left
as is: not asked, not requested, not applied.  The server records what that
needs in ``setup_state.json`` for the CLI; without it the CLI reads the
library's files.  A request leaves the list once it is under way; one that failed
stays and is tried again an hour later, when it is requested again, or at the
next start of the server process.  Offline (``SONIQBOOM_OFFLINE``) the
additions wait; removals need no internet and go ahead."""
from __future__ import annotations

import asyncio
import logging
import os
import time

log = logging.getLogger(__name__)

COLLECTIONS = {
    "modland":  "Modland",
    "hvsc":     "HVSC (High Voltage SID Collection)",
    "aminet":   "Aminet",
    "sceneorg": "scene.org",
}
SCENE_INFO = {
    "hvsc":    "HVSC song lengths and STIL comments for SID tunes",
    "modland": "Modland checksum list (composers and games of modules)",
    "songdb":  "UADE song database (Amiga module details and lengths)",
    "demozoo": "Demozoo (scene groups, parties and competitions)",
}
KINDS = {"collections": COLLECTIONS, "scene_info": SCENE_INFO}
ENV = {"collections": "SONIQBOOM_COLLECTIONS", "scene_info": "SONIQBOOM_SCENE_INFO"}

_POLL_S = 10.0
_RETRY_S = 3600.0
_BUSY_RETRY_S = 60.0               # a removal waiting for a download / apply to end
BUSY = "busy"                      # an applier's answer: try again soon
LEFT = "left"                      # … already there, left as is (done)

_task: "asyncio.Task | None" = None
_apply_task: "asyncio.Task | None" = None
_retry_at: dict = {}            # (kind, name) → (try again after, failed at)
_offline_logged = False


# ── Choosing (CLI, installer, first start) ───────────────────────────────────

def parse_choice(value, kind: str, default: "list[str] | None" = None) -> "list[str] | None":
    """``yes`` / ``all`` → every name, ``no`` / ``none`` → none, a comma list
    → those names; empty → ``default``.  ``None`` for a value it can't read
    (the caller says so)."""
    names = KINDS[kind]
    # (quotes kept by an env file or Compose's list syntax, a stray comma)
    v = ("" if value is None else str(value)).strip().strip("\"'").strip(" ,").lower()
    if not v:
        return default
    if v in ("y", "yes", "all", "true", "on", "1"):
        return list(names)
    if v in ("n", "no", "none", "false", "off", "0"):
        return []
    out = []
    for part in v.replace(" ", ",").split(","):
        if not part:
            continue
        if part not in names:
            return None
        if part not in out:
            out.append(part)
    return out


def _setup(conf: dict) -> dict:
    s = conf.get("setup")
    return dict(s) if isinstance(s, dict) else {}


_LISTS = {False: "pending", True: "removing"}


def _pending_of(conf: dict, key: str = "pending") -> dict:
    p = _setup(conf).get(key)
    p = p if isinstance(p, dict) else {}
    return {k: [n for n in KINDS[k] if n in (p.get(k) or [])] for k in KINDS}


def pending(remove: bool = False) -> dict:
    """``{"collections": [...], "scene_info": [...]}`` — the additions (or,
    ``remove``, the removals) waiting."""
    from soniqboom.config import load_local_conf
    try:
        return _pending_of(load_local_conf(), _LISTS[remove])
    except Exception:                                   # noqa: BLE001
        return {k: [] for k in KINDS}


def request(kind: str, names, remove: bool = False) -> list[str]:
    """Add ``names`` to the waiting additions (or ``remove``: removals) of
    ``kind`` — and take them off the other list; returns the list."""
    from soniqboom.config import update_local_conf
    names = [n for n in names if n in KINDS[kind]]
    out: list = []

    def upd(conf):
        s = _setup(conf)
        p = _pending_of(conf, _LISTS[remove])
        other = _pending_of(conf, _LISTS[not remove])
        p[kind] = [n for n in KINDS[kind] if n in p[kind] or n in names]
        other[kind] = [n for n in other[kind] if n not in names]
        s[_LISTS[remove]] = p
        s[_LISTS[not remove]] = other
        asked = dict(s.get("requested_at") or {})
        for n in names:
            asked[f"{kind}:{n}"] = now
        s["requested_at"] = asked
        conf["setup"] = s
        out[:] = p[kind]
    now = time.time()
    update_local_conf(upd)
    return out


def withdraw(kind: str, names) -> None:
    """Take ``names`` off the waiting additions of ``kind`` (a "no" to the
    installer's question)."""
    for n in names:
        _drop(kind, n)


def _requested_at(kind: str, name: str) -> float:
    from soniqboom.config import load_local_conf
    try:
        asked = _setup(load_local_conf()).get("requested_at") or {}
        return float(asked.get(f"{kind}:{name}") or 0)
    except Exception:                                   # noqa: BLE001
        return 0.0


def _drop(kind: str, name: str, remove: bool = False) -> None:
    from soniqboom.config import update_local_conf
    key = _LISTS[remove]

    def upd(conf):
        s = _setup(conf)
        p = _pending_of(conf, key)
        if name not in p[kind]:
            return conf
        p[kind] = [n for n in p[kind] if n != name]
        s[key] = p
        conf["setup"] = s
    update_local_conf(upd)
    _retry_at.pop((remove, kind, name), None)


def asked() -> bool:
    """The installer's questions were answered (or a new install's first start
    decided) — they are not asked again."""
    from soniqboom.config import load_local_conf
    try:
        return bool(_setup(load_local_conf()).get("asked"))
    except Exception:                                   # noqa: BLE001
        return False


def mark_asked() -> None:
    from soniqboom.config import update_local_conf

    def upd(conf):
        s = _setup(conf)
        s["asked"] = True
        s.pop("first_run_done", None)       # (the marker's earlier name)
        conf["setup"] = s
    update_local_conf(upd)


def is_new_install() -> bool:
    """No library has been saved in the data folder yet."""
    from soniqboom.config import get_data_dir
    d = get_data_dir()
    return not any((d / n).exists() for n in ("library.json", "library.aof"))


def first_start_defaults(env=None) -> dict | None:
    """A new install's first start without the installer's questions (a new
    Docker deployment, the bundled app): request what ``SONIQBOOM_COLLECTIONS``
    / ``SONIQBOOM_SCENE_INFO`` say — everything when they are unset — once.
    Returns what was requested, or None when nothing was decided now."""
    env = os.environ if env is None else env
    if asked() or not is_new_install():
        return None             # (an existing install: the installer asks, once)
    chosen = {}
    for kind in KINDS:
        names = parse_choice(env.get(ENV[kind]), kind, default=list(KINDS[kind]))
        if names is None:
            log.error("%s=%r is not yes, no or a list of %s — nothing is added",
                      ENV[kind], env.get(ENV[kind]), ", ".join(KINDS[kind]))
            print(f"{ENV[kind]}={env.get(ENV[kind])!r} is not yes, no or a list of "
                  f"{', '.join(KINDS[kind])} — nothing is added", flush=True)
            names = []
        chosen[kind] = names
        if names:
            request(kind, names)
    mark_asked()
    return chosen


# ── Already in the library ───────────────────────────────────────────────────

# A collection the library already holds as a root folder of its own (a
# network share of its site or a mirror, a local copy): the host names and the
# folder-name parts that give it away.
_LINKED = {
    "modland":  (("modland.com",), ("modland",)),
    "hvsc":     (("6581.nl", "hvsc.c64.org"), ("hvsc", "c64music")),
    "aminet":   (("aminet.net",), ("aminet",)),
    "sceneorg": (("scene.org",), ("scene.org", "sceneorg")),
}
_STATE = "setup_state.json"     # the server's: its local root folders, the collections on
_state_written: "str | None" = None


def _looks_like(name: str, text: str) -> bool:
    """``text`` (a root folder's URL, path or alias) is the collection's site,
    a mirror of it, or a folder named after it — the name as a word of the
    host or of a folder name (``hvsc_83``, ``modland.example.org``)."""
    import re
    from urllib.parse import urlsplit
    hosts, words = _LINKED[name]
    parts = []
    path = text
    if "://" in text:
        u = urlsplit(text)
        host = (u.hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in hosts):
            return True
        parts.append(host)
        path = u.path
    parts += [p.lower() for p in path.replace("\\", "/").split("/") if p]
    return any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", p)
               for p in parts for w in words)


def _root_folders(local: "list[str] | None" = None) -> "list[tuple[str, list[str]]]":
    """The root folders the user added: ``(shown as, [texts it is known
    by])`` — the network shares (SoniqBoom.conf) and the local folders
    ``local`` (the server's own list; the CLI reads ``read_state``).  Names
    only: nothing is opened or looked up."""
    from soniqboom.config import load_local_conf
    from soniqboom.core.filesource import scan_root_for_share
    try:
        conf = load_local_conf() or {}
    except Exception:                                   # noqa: BLE001
        conf = {}
    out = []
    shares = conf.get("network_shares")
    for sid, sh in (shares.items() if isinstance(shares, dict) else []):
        if not isinstance(sh, dict):
            continue
        try:
            root = scan_root_for_share(sh)
        except Exception:                               # noqa: BLE001
            root = str(sh.get("host") or "")
        alias = str(sh.get("alias") or "").strip()
        texts = [root, str(sh.get("remote_path") or ""), alias]
        out.append((f"{alias} ({root})" if alias else root, [t for t in texts if t]))
    aliases = conf.get("folder_aliases")
    aliases = aliases if isinstance(aliases, dict) else {}
    for path in local or ():
        alias = str(aliases.get(path) or "").strip()
        out.append((f"{alias} ({path})" if alias else path, [path] + ([alias] if alias else [])))
    return out


def linked(name: str, local: "list[str] | None" = None) -> "str | None":
    """The root folder (shown as) that already holds the collection ``name``
    — a network share of its site or a mirror, or a local copy (``local``:
    the local root folders) — or None."""
    for shown, texts in _root_folders(local):
        if any(_looks_like(name, t) for t in texts):
            return shown
    return None


def collection_on(name: str) -> bool:
    """The server's: the collection ``name`` is switched on."""
    if name == "modland":
        from soniqboom.core import modland_catalog
        return modland_catalog.enabled()
    from soniqboom.core import online_catalog
    cat = online_catalog.get(name)
    return bool(cat and cat.enabled())


def _local_roots() -> list[str]:
    """The server's local root folders (its library's scan folders that are
    not network shares or online collections)."""
    from soniqboom.core.store import get_store
    return sorted(p for p in list(get_store()._scan_dirs) if "://" not in p)


def current_state() -> dict:
    """The server's: what ``already`` looks at — its local root folders, the
    collections on, the HVSC collection's "Use for my own SID files"."""
    from soniqboom.core import online_hvsc
    return {"folders": _local_roots(),
            "collections_on": [n for n in COLLECTIONS if collection_on(n)],
            "hvsc_local_docs": bool(online_hvsc.HVSC_ONLINE.options().get(
                online_hvsc.OPT_LOCAL_DOCS, True))}


def _record_state() -> None:
    """Write ``setup_state.json`` (``current_state``) for the CLI, which
    doesn't open the library while a server runs — when it changed."""
    global _state_written
    import json
    from soniqboom.config import get_data_dir
    text = json.dumps(current_state(), indent=1)
    if text == _state_written:
        return
    path = get_data_dir() / _STATE
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    _state_written = text


def _state_of(d) -> "dict | None":
    if not isinstance(d, dict):
        return None
    out = {k: [str(x) for x in (d.get(k) or []) if isinstance(x, str)]
           for k in ("folders", "collections_on")}
    out["hvsc_local_docs"] = d.get("hvsc_local_docs", True) is not False
    return out


def read_state() -> "dict | None":
    """The CLI's ``current_state``: as the server last recorded it
    (``setup_state.json``), else read from the library's own files (in
    memory, nothing written — a library no server of this version has
    opened yet); a new install's is empty.  None when the library can't be
    read."""
    import json
    from soniqboom.config import get_data_dir
    d = get_data_dir()
    try:
        return _state_of(json.loads((d / _STATE).read_text(encoding="utf-8")))
    except Exception:                                   # noqa: BLE001
        pass
    if is_new_install():
        return {"folders": [], "collections_on": [], "hvsc_local_docs": True}
    try:
        from soniqboom.core.modland_catalog import CONFIG_ENABLED
        from soniqboom.core.persistence import read_library_state
        lib = read_library_state(d)
    except Exception:                                   # noqa: BLE001
        log.debug("library not read", exc_info=True)
        return None
    cfg = lib.get("config") if isinstance(lib.get("config"), dict) else {}
    dirs = lib.get("scan_dirs") if isinstance(lib.get("scan_dirs"), dict) else {}
    on = [n for n in COLLECTIONS
          if cfg.get(CONFIG_ENABLED if n == "modland" else f"online_{n}_enabled")]
    return {"folders": sorted(p for p in dirs if isinstance(p, str) and "://" not in p),
            "collections_on": on,
            "hvsc_local_docs": cfg.get("online_hvsc_local_docs", True) is not False}


def _index_present(name: str) -> bool:
    """The scene information's download is there (a non-empty index file;
    nothing created or opened)."""
    from soniqboom.config import get_data_dir
    try:
        return (get_data_dir() / "scene" / f"{name}.sqlite").stat().st_size > 0
    except OSError:
        return False


_SHORT = {"modland": "Modland", "hvsc": "HVSC", "aminet": "Aminet", "sceneorg": "scene.org"}
_SCENE_PRESENT = {"modland": "The Modland checksum list is already downloaded",
                  "songdb": "The UADE song database is already downloaded",
                  "demozoo": "Demozoo is already downloaded"}


def already(kind: str, state: dict) -> dict:
    """``{name: what is already there}`` — a collection on or held by a root
    folder of the library (``state``: ``current_state`` / ``read_state``),
    scene information downloaded or in use from elsewhere.  Those are left
    as is: not asked, not requested, not applied."""
    from soniqboom.config import settings
    out = {}
    if kind == "collections":
        on = set(state.get("collections_on") or ()) - set(state.get("removing") or ())
        for n in COLLECTIONS:
            root = None if n in on else linked(n, state.get("folders") or [])
            if n in on:
                out[n] = f"{_SHORT[n]} is already on"
            elif root:
                out[n] = f"{_SHORT[n]} is already in your library as the root folder {root}"
        return out
    if getattr(settings, "hvsc_source", "local") == "online":
        out["hvsc"] = "HVSC song lengths and STIL are already downloaded"
    elif (getattr(settings, "hvsc_docs_path", "") or "").strip():
        out["hvsc"] = "HVSC song lengths and STIL already come from your local HVSC folder"
    elif "hvsc" in (state.get("collections_on") or ()) and state.get("hvsc_local_docs", True):
        out["hvsc"] = "HVSC song lengths and STIL already come with the HVSC collection"
    for n, msg in _SCENE_PRESENT.items():
        if _index_present(n):
            out[n] = msg
    for n in state.get("removing_scene") or ():
        out.pop(n, None)                # (a removal is waiting: it isn't, then)
    return out


# ── Applying (the server) ────────────────────────────────────────────────────

async def _apply_collection(name: str) -> bool:
    """Switch the collection on as its Settings switch does — unless it is
    already there (on, or held by a root folder of the library: ``already``,
    left as is).  True when the request is done (on, switching on in the
    background, or left)."""
    from fastapi import HTTPException
    from soniqboom.api import admin
    there = already("collections", current_state()).get(name)
    if there:
        log.info("%s — left as is (setup request)", there)
        return LEFT
    try:
        if name == "modland":
            await admin.admin_modland_catalog_set({"enabled": True}, _tok="")
        else:
            await admin.admin_online_catalog_set(name, {"enabled": True}, _tok="")
    except HTTPException as exc:
        log.warning("Online collection %s not switched on: %s", COLLECTIONS[name], exc.detail)
        if exc.status_code == 409 and "busy" in str(exc.detail):
            return BUSY
        return True                 # (a share conflict: waiting won't change it)
    log.info("Online collection %s: switching on (setup request)", COLLECTIONS[name])
    return True


async def _apply_scene(name: str, collections: tuple = ()) -> bool:
    """Download / switch on the scene information as its Settings button
    does — unless it is already there (``already``: left as is).  True when
    done; False when the download failed (tried again).  ``collections``:
    the collections being switched on in the same pass."""
    from soniqboom.api import admin
    there = already("scene_info", current_state()).get(name)
    if there:
        log.info("%s — left as is (setup request)", there)
        return LEFT
    if name == "hvsc":
        from soniqboom.core import online_hvsc
        if "hvsc" in collections or online_hvsc.HVSC_ONLINE.enabled():
            # The HVSC collection brings the documents ("Use for my own SID
            # files"), and a local HVSC folder found later still takes over.
            if not online_hvsc.HVSC_ONLINE.options().get(online_hvsc.OPT_LOCAL_DOCS, True):
                await admin.admin_online_catalog_set(
                    "hvsc", {"options": {online_hvsc.OPT_LOCAL_DOCS: True}}, _tok="")
            log.info("HVSC song lengths and STIL: from the HVSC online collection")
            return True
        await admin.update_settings({"renderers": {"hvsc_source": "online"}}, _tok="")
        log.info("HVSC song lengths and STIL: downloading from the internet (setup request)")
        return True
    if name == "modland":
        st = await admin.admin_scene_refresh(_tok="")
    elif name == "songdb":
        from soniqboom.core import songdb
        songdb.set_auto_apply(True)             # (the refresh applies it when it's in)
        st = await admin.admin_songdb_refresh(_tok="")
    elif name == "demozoo":
        from soniqboom.core import demozoo
        st = await admin.admin_demozoo_refresh(_tok="")
        if st.get("error") or not demozoo.has_index():
            log.warning("Demozoo index not downloaded: %s", st.get("error"))
            return False
        st = await admin.admin_demozoo_apply(_tok="")
        if (st or {}).get("error"):
            log.warning("Demozoo not applied: %s", st.get("error"))
            return False
        return True
    else:
        return True
    if st.get("error"):
        log.warning("%s not downloaded: %s", SCENE_INFO[name], st.get("error"))
        return False
    log.info("%s: downloaded (setup request)", SCENE_INFO[name])
    return True


async def _remove_collection(name: str) -> bool:
    """Switch the collection off as its Settings switch does (its tunes leave
    the library; ratings, plays, stars and playlist entries stay)."""
    from soniqboom.api import admin
    if name == "modland":
        await admin.admin_modland_catalog_set({"enabled": False}, _tok="")
    else:
        await admin.admin_online_catalog_set(name, {"enabled": False}, _tok="")
    log.info("Online collection %s: switching off (setup request)",
             COLLECTIONS[name])
    return True


def _delete_index(db, *extra: str) -> None:
    """The index file, its sqlite side files and ``extra`` leftovers (glob
    patterns in its folder) of an interrupted download."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    for pattern in extra:
        for f in db.parent.glob(pattern):
            f.unlink(missing_ok=True)


async def _quiet(mod) -> bool:
    """Wait (bounded) until ``mod`` neither downloads nor applies."""
    for _ in range(600):                                # ~60 s ceiling
        st = mod._status
        if not st.get("refreshing") and not st.get("applying"):
            return True
        await asyncio.sleep(0.1)
    return False


async def _remove_scene(name: str):
    """Withdraw what the scene information filled in (the reset its Settings
    card offers; the Modland checksum list: ``remove_from_library``), stop
    applying it after scans, and delete its download.  HVSC: the documents
    are no longer used for the user's own SID files (HVSC card back to Local
    folder, the HVSC collection's "Use for my own SID files" off) and, with the
    collection off, deleted; the values already stored on SID tracks stay.
    True when done, ``BUSY`` while a download / apply runs, else False."""
    from soniqboom.api import admin
    from soniqboom.config import settings
    if name == "hvsc":
        from soniqboom.core import online_hvsc
        hv = online_hvsc.HVSC_ONLINE
        if getattr(settings, "hvsc_source", "local") == "online":
            await admin.update_settings({"renderers": {"hvsc_source": "local"}}, _tok="")
        if hv.options().get(online_hvsc.OPT_LOCAL_DOCS, True):
            # (also with the collection off: switched on later, it then keeps
            # the documents to its own tunes)
            await admin.admin_online_catalog_set(
                "hvsc", {"options": {online_hvsc.OPT_LOCAL_DOCS: False}}, _tok="")
        if not hv.enabled():
            if hv.busy():
                return BUSY             # (switching off, in this very pass perhaps)
            if not online_hvsc.fallback_active():
                import shutil
                shutil.rmtree(hv.docs_dir(), ignore_errors=True)
        log.info("HVSC song lengths and STIL: downloaded documents no longer used for "
                 "your own SID files%s", " (your local HVSC folder stays in use)"
                 if online_hvsc.local_docs_path() else "")
        return True
    if name == "modland":
        from soniqboom.core import scene_metadata
        st = await scene_metadata.remove_from_library()
    elif name == "songdb":
        from soniqboom.core import songdb
        songdb.set_auto_apply(False)
        st = await songdb.reset_to_file_state()
        if not st.get("error"):
            if not await _quiet(songdb):
                return BUSY
            _delete_index(songdb._db_path(), "songdb-*.part", "songdb.building")
    elif name == "demozoo":
        from soniqboom.core import demozoo
        st = await admin.admin_demozoo_reset(_tok="")
        if not st.get("error"):
            if not await _quiet(demozoo):
                return BUSY
            _delete_index(demozoo._db_path(), "demozoo.download.gz")
    else:
        return True
    if st.get("error"):
        busy = "running" in str(st.get("error"))
        log.warning("%s not removed: %s%s", SCENE_INFO[name], st.get("error"),
                    " — trying again in a minute" if busy else "")
        return BUSY if busy else False
    log.info("%s: removed (setup request)", SCENE_INFO[name])
    return True


async def apply_pending() -> dict:
    """Apply the waiting requests, one after the other (the downloads are
    large): the removals first, then the additions.  Returns
    ``{kind: [names added]}`` (``removed``: ``{kind: [names removed]}``;
    ``left``: ``{kind: [names already there, left as is]}``)."""
    global _offline_logged
    from soniqboom.core import netpolicy
    done: dict = {k: [] for k in KINDS}
    gone: dict = {k: [] for k in KINDS}
    left: dict = {k: [] for k in KINDS}
    done_all = {False: done, True: gone}
    want = pending()
    drop = pending(remove=True)
    if not any(want.values()) and not any(drop.values()):
        return {**done, "removed": gone, "left": left}
    offline = netpolicy.offline()
    if offline and any(want.values()) and not _offline_logged:
        log.info("Offline mode: the requested online collections and scene "
                 "information wait until SoniqBoom may use the internet")
        _offline_logged = True
    now = time.time()
    there = already("collections", current_state())
    coll = tuple(n for n in want["collections"] if n not in there)   # (being switched on)
    jobs = [(True, kind, name) for kind in KINDS for name in drop[kind]]
    if not offline:
        jobs += [(False, kind, name) for kind in KINDS for name in want[kind]]
    for remove, kind, name in jobs:
        if name not in pending(remove)[kind]:
            continue                    # (reversed, or done, since the pass began)
        until, failed = _retry_at.get((remove, kind, name), (0.0, 0.0))
        if until > now and _requested_at(kind, name) <= failed:
            continue                # (failed: waits, unless requested again since)
        try:
            if remove:
                ok = await (_remove_collection(name) if kind == "collections"
                            else _remove_scene(name))
            elif kind == "collections":
                ok = await _apply_collection(name)
            else:
                ok = await _apply_scene(name, coll)
        except asyncio.CancelledError:
            raise
        except Exception:                           # noqa: BLE001
            log.warning("Setup request %s %s failed", kind, name, exc_info=True)
            ok = False
        if ok is True or ok == LEFT:
            try:
                _drop(kind, name, remove)
            except Exception:                       # noqa: BLE001
                # (done, but still listed: not done again for an hour)
                log.warning("Setup request %s %s not cleared from SoniqBoom.conf",
                            kind, name, exc_info=True)
                ok = False
        if ok == LEFT:
            left[kind].append(name)
        elif ok is True:
            done_all[remove][kind].append(name)
        else:
            t = time.time()
            _retry_at[(remove, kind, name)] = (t + (_BUSY_RETRY_S if ok == BUSY else _RETRY_S), t)
    return {**done, "removed": gone, "left": left}


async def _watch() -> None:
    global _apply_task
    while True:
        try:
            _record_state()
        except Exception:                               # noqa: BLE001
            log.debug("setup state not recorded", exc_info=True)
        try:
            if ((any(pending().values()) or any(pending(remove=True).values()))
                    and (_apply_task is None or _apply_task.done())):
                _apply_task = asyncio.get_running_loop().create_task(
                    apply_pending(), name="setup-requests.apply")
        except Exception:                               # noqa: BLE001
            log.debug("setup requests not read", exc_info=True)
        await asyncio.sleep(_POLL_S)


def start() -> None:
    """At server start (on the loop): apply the waiting requests now and look
    for new ones every few seconds."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_watch(), name="setup-requests")


def all_tasks() -> list:
    return [t for t in (_task, _apply_task) if t is not None and not t.done()]
