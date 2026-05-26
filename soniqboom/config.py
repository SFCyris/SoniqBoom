# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Centralised settings.

Loading order (highest priority wins):
  1. Environment variables  SONIQBOOM_*
  2. ~/.soniqboom.env
  3. SoniqBoom.conf  (JSON, in the app data directory — see below)
  4. Built-in defaults

Config / data directory (highest priority wins):
  1. SONIQBOOM_CONF env var  → path to a specific SoniqBoom.conf file
  2. <project-root>/SoniqBoom.conf  (legacy; auto-migrated on first run)
  3. Platform default:
       macOS:  ~/Library/Application Support/SoniqBoom/SoniqBoom.conf
       Linux:  ~/.local/share/soniqboom/SoniqBoom.conf

The JSON config is the human-friendly knob; env vars are for CI / containers.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import warnings
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Platform-local app directory ─────────────────────────────────────────────

def _default_app_dir() -> Path:
    """Platform-appropriate local app directory (always on local disk).

    macOS:  ~/Library/Application Support/SoniqBoom
    Linux:  ~/.local/share/soniqboom
    other:  ~/.soniqboom
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SoniqBoom"
    elif sys.platform.startswith("linux"):
        xdg = os.environ.get("XDG_DATA_HOME", "")
        base = Path(xdg) if xdg else Path.home() / ".local" / "share"
        return base / "soniqboom"
    return Path.home() / ".soniqboom"


APP_DIR = _default_app_dir()


# ── SoniqBoom.conf location ─────────────────────────────────────────────────
# Resolved once at import time.  Priority:
#   1. SONIQBOOM_CONF env var
#   2. <project-root>/SoniqBoom.conf  (legacy — migrated to APP_DIR)
#   3. APP_DIR / SoniqBoom.conf       (new default)

_PROJECT_ROOT = Path(__file__).parent.parent
_LEGACY_CONF  = _PROJECT_ROOT / "SoniqBoom.conf"
_LEGACY_PREFS = Path.home() / ".soniqboom.prefs.json"


def _resolve_conf_path() -> Path:
    """Find (or create) the canonical SoniqBoom.conf location."""
    # Explicit override — honour it as-is.
    env = os.environ.get("SONIQBOOM_CONF")
    if env:
        return Path(env)

    target = APP_DIR / "SoniqBoom.conf"

    # Migrate legacy project-root config if the new location is empty.
    # Strip stale keys (e.g. "redis") that no longer apply.
    _STALE_KEYS = {"redis", "embedding", "embedding_model", "redis_url", "redis_index"}
    if _LEGACY_CONF.exists() and not target.exists():
        APP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            raw = json.loads(_LEGACY_CONF.read_text(encoding="utf-8"))
            for key in _STALE_KEYS:
                raw.pop(key, None)
            target.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        except Exception:
            shutil.copy2(_LEGACY_CONF, target)  # fallback: copy as-is

    # Migrate legacy prefs file (~/.soniqboom.prefs.json → APP_DIR).
    prefs_target = APP_DIR / "prefs.json"
    if _LEGACY_PREFS.exists() and not prefs_target.exists():
        APP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_LEGACY_PREFS, prefs_target)

    return target


_CONF_PATH = _resolve_conf_path()
PREFS_PATH = APP_DIR / "prefs.json"


# ── Local conf loader ─────────────────────────────────────────────────────────

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*."""
    result = dict(base)
    for k, v in override.items():
        if k.startswith("_"):
            continue                        # skip comment keys
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        elif v is not None:                 # null values keep the default
            result[k] = v
    return result


_CONF_DEFAULTS: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 8080},
    "art_cache_dir": "",
    "conversion_cache_dir": "",
    "conversion_cache_max_bytes": 4294967296,   # 4 GB default — configurable in Settings → Renderers
    "data_dir": "",
    "merger_interval": 120,
    "aof_flush_interval": 0.1,
    "scan_zips": True,
    "expose_local_files": True,
    "display_startup_logo": True,
    "folder_aliases": {},          # { "/abs/path": "alias", ... }
    "network_shares": {},          # { "share_id": { protocol, host, ... }, ... }
    "remote_cache_max_mb": 2048,   # LRU cache limit for remote audio files
    "renderers": {
        "sidplayfp_path": "",
        "fluidsynth_path": "",
        "openmpt123_path": "",
        # uade123 (Unix Amiga Delitracker Emulator) covers AHX, Hively,
        # and ~200 other Amiga formats that openmpt123 doesn't decode.
        # Optional; degrades to "renderer not installed" on missing.
        "uade123_path": "",
        "soundfont_path": "",
        "soundfonts_dir": "",
        "sid_default_duration": 180,
    },
    # ── Optional access services ────────────────────────────────────────────
    # Each toggle gates whether the corresponding router + frontend page is
    # mounted on the FastAPI app.  All default ON to preserve historical
    # behaviour — flip individual flags in the Settings → Services panel,
    # via the CLI (``soniqboom services disable subsonic``), or in this
    # config file to disable a service.  Disabled services become 404 to
    # callers, removing both the network attack surface and the startup
    # banner entry.
    "services": {
        "subsonic":   True,   # OpenSubsonic REST API at /rest/* (DSub etc.)
        "multiroom":  True,   # Multiroom controller at /multiroom + WS
        "cast":       True,   # Cast / AirPlay / DLNA discovery (/api/cast)
        # DLNA Media Server — SSDP + UPnP-AV ContentDirectory.  OFF by
        # default because announcing the user's library on the LAN is a
        # privacy-sensitive opt-in: anyone on the same network can
        # browse the track list once enabled.  Flip via Settings →
        # Services or ``soniqboom services enable dlna_server``.
        "dlna_server": False,
    },
}


# ── Service-toggle helpers ─────────────────────────────────────────────────
#
# Treated as a tiny module-level API so ``main.py`` can gate router
# inclusion at startup AND admin endpoints can flip flags at runtime
# without re-importing the config module.

SERVICE_NAMES: tuple[str, ...] = ("subsonic", "multiroom", "cast", "dlna_server")
SERVICE_LABELS: dict[str, str] = {
    "subsonic":    "OpenSubsonic REST API",
    "multiroom":   "Multiroom controller",
    "cast":        "Cast / AirPlay / DLNA (outgoing)",
    "dlna_server": "DLNA Media Server (incoming)",
}


def is_service_enabled(name: str) -> bool:
    """Return True when ``name`` (one of :data:`SERVICE_NAMES`) is enabled.

    Looks up ``services.<name>`` in the active config; falls back to the
    default (currently True) when the key is missing — so an older
    SoniqBoom.conf written before this feature shipped doesn't silently
    disable services after upgrade.
    """
    default = _CONF_DEFAULTS["services"].get(name, True)
    try:
        conf = load_local_conf()
    except Exception:
        return default
    svc = conf.get("services") or {}
    val = svc.get(name)
    return default if val is None else bool(val)


def set_service_enabled(name: str, enabled: bool) -> None:
    """Persist ``services.<name> = enabled`` to SoniqBoom.conf atomically."""
    if name not in SERVICE_NAMES:
        raise ValueError(f"Unknown service: {name!r}")
    conf = load_local_conf()
    svc = dict(conf.get("services") or {})
    svc[name] = bool(enabled)
    conf["services"] = svc
    save_local_conf(conf)


# ── Default config template ──────────────────────────────────────────────────
# Written on first run when no SoniqBoom.conf exists anywhere.  Uses a raw
# string so it stays human-readable with inline comments (JSON doesn't support
# comments, but we use _comment keys that are stripped on load).

_CONF_TEMPLATE = """\
{
  "_comment": "SoniqBoom configuration. All fields are optional; missing values use sensible defaults. Restart the server after editing.",

  "server": {
    "_comment": "Bind address and port. Use 127.0.0.1 to restrict to localhost only.",
    "host": "0.0.0.0",
    "port": 8080
  },

  "_comment_storage": "Paths below default to the platform data directory. Set to an absolute path to override.",
  "data_dir": "",
  "art_cache_dir": "",
  "conversion_cache_dir": "",
  "conversion_cache_max_bytes": 4294967296,

  "_comment_persistence": "merger_interval: seconds between AOF-to-snapshot merges. aof_flush_interval: seconds between AOF buffer flushes.",
  "merger_interval": 120,
  "aof_flush_interval": 0.1,

  "scan_zips": true,
  "expose_local_files": true,
  "display_startup_logo": true,

  "_comment_aliases": "Map absolute directory paths to short display names in the UI.",
  "folder_aliases": {},

  "_comment_network": "Network shares are managed via the admin UI. remote_cache_max_mb limits the local cache for remote audio files.",
  "network_shares": {},
  "remote_cache_max_mb": 2048,

  "renderers": {
    "_comment": "Leave empty to auto-detect from PATH. Set absolute paths to override.",
    "sidplayfp_path": "",
    "fluidsynth_path": "",
    "openmpt123_path": "",
    "soundfont_path": "",
    "soundfonts_dir": "",
    "sid_default_duration": 180
  },

  "_comment_services": "Optional access services. Disable a flag to remove the corresponding router from the FastAPI app — clients then get 404. Toggle live from Settings → Services or `soniqboom services enable|disable <name>`.",
  "services": {
    "subsonic":  true,
    "multiroom": true,
    "cast":      true
  }
}
"""


def _ensure_conf_file(path: Path) -> None:
    """Create a default SoniqBoom.conf if none exists."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_CONF_TEMPLATE, encoding="utf-8")


# mtime-keyed cache so repeated calls (every admin POST hit ~7 callsites
# previously) don't re-stat + re-parse the JSON file each time.  ``save_local_conf``
# invalidates the entry via ``_invalidate_conf_cache``.
_conf_cache: dict[Path, tuple[float, int, dict[str, Any]]] = {}


def _invalidate_conf_cache(path: Path | None = None) -> None:
    if path is None:
        _conf_cache.clear()
    else:
        _conf_cache.pop(path, None)


def load_local_conf(path: Path = _CONF_PATH) -> dict[str, Any]:
    """Load and validate SoniqBoom.conf; return merged-with-defaults dict."""
    _ensure_conf_file(path)
    try:
        st = path.stat()
        cached = _conf_cache.get(path)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            # Return a fresh deep-ish copy so callers can mutate without
            # corrupting the cached representation.
            return json.loads(json.dumps(cached[2]))
    except OSError:
        st = None

    conf = dict(_CONF_DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        conf = _deep_merge(conf, raw)
    except Exception as exc:
        warnings.warn(f"SoniqBoom.conf: could not load '{path}': {exc}", stacklevel=2)
        return conf

    if st is not None:
        _conf_cache[path] = (st.st_mtime_ns, st.st_size, json.loads(json.dumps(conf)))
    return conf


def _atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* atomically.

    The previous code did ``path.write_text(...)``: a crash mid-write leaves
    a half-written ``SoniqBoom.conf`` / ``prefs.json`` and the next launch
    falls back to defaults.  Write to a per-call unique tmp file in the same
    directory (so two concurrent writers can't overwrite each other's tmp
    on the way to ``os.replace``), fsync file *and* parent directory (so a
    power loss can't lose the rename), then ``os.replace`` to swap.
    """
    import secrets

    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = f".{os.getpid()}.{secrets.token_hex(4)}.tmp"
    tmp = path.with_suffix(path.suffix + suffix)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    # fsync the directory so the rename itself is durable across crashes.
    try:
        dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _conf_flock(path: Path):
    """Context-manager helper that flocks the sidecar lock for *path*.

    Yields ``(lock_fd, lock_path)`` — caller must close the fd and may
    unlink the lock file when finished.  Falls through with ``lock_fd=None``
    if fcntl isn't available (Windows) so callers can still proceed
    best-effort.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        import fcntl
    except ImportError:
        return None, lock_path

    try:
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return lock_fd, lock_path
    except OSError as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "config: flock on %s failed (%s); proceeding without "
            "inter-process serialisation",
            lock_path, exc,
        )
        return None, lock_path


def _conf_flock_release(lock_fd, lock_path: Path) -> None:
    if lock_fd is None:
        return
    try:
        import fcntl as _f
        _f.flock(lock_fd, _f.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass
    # Unlink the sidecar so Application Support doesn't accumulate stale
    # ``SoniqBoom.conf.lock`` / ``prefs.json.lock`` artifacts.  Best-effort
    # — if another writer just acquired the lock between our unlock and
    # unlink, the unlink will quietly fail or remove their lock file (they
    # still hold their own fd, so the lock semantics are intact; the file
    # just gets recreated on next save).
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def update_local_conf(
    updater,
    path: Path = _CONF_PATH,
) -> dict[str, Any]:
    """Atomically read-modify-write SoniqBoom.conf under the inter-process lock.

    ``updater(conf)`` receives the merged-with-defaults conf dict and may
    mutate it in place or return a replacement.  The whole load → mutate
    → save cycle stays under ``fcntl.flock`` so two concurrent
    ``update_local_conf`` calls can't lose each other's updates.
    """
    lock_fd, lock_path = _conf_flock(path)
    try:
        conf = load_local_conf(path)
        new_conf = updater(conf)
        if new_conf is None:
            new_conf = conf
        # save_local_conf re-acquires its own flock (re-entrant flock from
        # the same process is a no-op, so this is fine).  The outer lock
        # we already hold prevents *other* callers from interleaving.
        save_local_conf(new_conf, path=path)
        return new_conf
    finally:
        _conf_flock_release(lock_fd, lock_path)


def save_local_conf(data: dict[str, Any], path: Path = _CONF_PATH) -> None:
    """Persist updated conf (round-trips comments as they are stripped by json).

    Read-modify-write is serialised via a sidecar ``.lock`` file and
    ``fcntl.flock`` so two processes (e.g. server + a CLI invocation, or
    two admin POSTs racing) can't lose each other's update — only the
    tmp-file collision was fixed previously.
    """
    # Preserve top-level _comment if the file already has one
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd, lock_path = _conf_flock(path)
    try:
        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        merged = {k: v for k, v in existing.items() if k.startswith("_")}
        merged.update({k: v for k, v in data.items() if not k.startswith("_")})
        _atomic_write_text(path, json.dumps(merged, indent=2))
        _invalidate_conf_cache(path)
    finally:
        _conf_flock_release(lock_fd, lock_path)



# Load once at import time
_local_conf = load_local_conf()


# ── Pydantic settings ─────────────────────────────────────────────────────────

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SONIQBOOM_",
        env_file=str(Path.home() / ".soniqboom.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server — defaults from SoniqBoom.conf
    host: str = _local_conf["server"]["host"]
    port: int = _local_conf["server"]["port"]

    # Audio
    ffmpeg_path:      str = "ffmpeg"
    ffprobe_path:     str = "ffprobe"
    transcode_format: str = "flac"

    # Library — bootstrap dirs (can also be set via the UI)
    library_dirs: list[str] = []

    # Plugin discovery path
    plugin_dirs: str = ""

    # Art cache — filesystem directory for cover art thumbnails and full art
    art_cache_dir: str = _local_conf.get("art_cache_dir", "")

    # Renderers for non-PCM formats (SID, MIDI, tracker modules)
    sidplayfp_path:     str = _local_conf.get("renderers", {}).get("sidplayfp_path", "")
    fluidsynth_path:    str = _local_conf.get("renderers", {}).get("fluidsynth_path", "")
    openmpt123_path:    str = _local_conf.get("renderers", {}).get("openmpt123_path", "")
    uade123_path:       str = _local_conf.get("renderers", {}).get("uade123_path", "")
    soundfont_path:     str = _local_conf.get("renderers", {}).get("soundfont_path", "")
    soundfonts_dir:     str = _local_conf.get("renderers", {}).get("soundfonts_dir", "")
    sid_default_duration: int = _local_conf.get("renderers", {}).get("sid_default_duration", 180)
    # HVSC integration — point at the High Voltage SID Collection's
    # ``DOCUMENTS/`` folder (containing Songlengths.md5 + STIL.txt) to
    # get per-tune accurate durations and STIL commentary.  Empty = off.
    hvsc_docs_path:    str = _local_conf.get("renderers", {}).get("hvsc_docs_path", "")
    # libgme renderer (E-14) — covers NSF/SPC/GBS/VGM/AY/KSS/SAP/HES/GYM.
    # Path to the ``gme`` CLI (from gme-tools or similar) that writes WAV
    # to stdout from a chiptune file.  Empty disables — falls back to
    # ffmpeg's gme plugin if it's compiled in.
    gme_path:          str = _local_conf.get("renderers", {}).get("gme_path", "")

    # Conversion cache (rendered SID/MIDI/tracker WAV files)
    conversion_cache_dir: str = _local_conf.get("conversion_cache_dir", "")
    conversion_cache_max_bytes: int = int(
        _local_conf.get("conversion_cache_max_bytes", 4 * 1024 ** 3)
    )

    # ZIP-extract cache (stable disk path for ZIP-contained tracks so a
    # browser's 5–20 Range requests per playback don't re-extract the
    # whole archive each time).  Defaults to 2 GB — the stream layer
    # also caps live to min(2 GB, conversion_cache_max_bytes/4) so a
    # cache-budget cut implicitly tightens the ZIP budget too.
    zip_extract_cache_max_mb: int = int(
        _local_conf.get("zip_extract_cache_max_mb", 2048)
    )

    # Persistence (in-memory store → disk)
    data_dir: str = _local_conf.get("data_dir", "")
    merger_interval: int = int(_local_conf.get("merger_interval", 120))
    aof_flush_interval: float = float(_local_conf.get("aof_flush_interval", 0.1))

    # ZIP scanning — treat ZIP files as virtual directories
    scan_zips: bool = _local_conf.get("scan_zips", True)

    # UI — startup logo animation
    display_startup_logo: bool = _local_conf.get("display_startup_logo", True)

    # Location / alias display
    expose_local_files: bool = _local_conf.get("expose_local_files", True)
    folder_aliases: dict = _local_conf.get("folder_aliases", {})

    @field_validator("library_dirs", mode="before")
    @classmethod
    def _parse_list(cls, v):
        if isinstance(v, str):
            return json.loads(v) if v.startswith("[") else [v]
        return v


def load_prefs() -> dict:
    if PREFS_PATH.exists():
        return json.loads(PREFS_PATH.read_text())
    return {}


def save_prefs(data: dict) -> None:
    _atomic_write_text(PREFS_PATH, json.dumps(data, indent=2))


settings = Settings()


def get_soundfonts_dir() -> Path:
    """Return resolved soundfonts directory, creating it if needed."""
    if settings.soundfonts_dir:
        p = Path(settings.soundfonts_dir)
    else:
        # The previous fallback resolved next to the source tree.  Inside the
        # Nuitka-built .app bundle that path is read-only, so any upload
        # attempt raised PermissionError.  Detect a bundled layout (the
        # module ends up under ``.app/Contents/.../soniqboom``) and fall
        # back to a user-writable directory in Application Support.
        module_root = Path(__file__).resolve()
        if ".app/Contents/" in str(module_root):
            p = Path.home() / "Library" / "Application Support" / "SoniqBoom" / "soundfonts"
        else:
            p = module_root.parent.parent / "soundfonts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_active_soundfont() -> Path | None:
    """Return the active soundfont path, or None."""
    if settings.soundfont_path:
        p = Path(settings.soundfont_path)
        if p.exists():
            return p
    # Try to find any .sf2/.sf3 in soundfonts dir
    sf_dir = get_soundfonts_dir()
    for sf in sorted(sf_dir.glob("*.sf2")):
        return sf
    for sf in sorted(sf_dir.glob("*.sf3")):
        return sf
    return None


def get_data_dir() -> Path:
    """Return resolved data directory for persistence (library.json, library.aof).

    Defaults to APP_DIR (~/Library/Application Support/SoniqBoom on macOS)
    so that AOF writes and merger snapshots hit the local SSD, not a
    potentially slow NFS/SMB mount.  Override with ``data_dir`` in
    SoniqBoom.conf or the SONIQBOOM_DATA_DIR env var.
    """
    if settings.data_dir:
        p = Path(settings.data_dir)
    else:
        p = APP_DIR
        # Auto-migrate from the old project-relative location (which may
        # sit on a network volume).  Check for the snapshot file, not the
        # directory — the config migration may have already created APP_DIR.
        old_dir = _PROJECT_ROOT / "data"
        if old_dir.exists() and not (p / "library.json").exists():
            p.mkdir(parents=True, exist_ok=True)
            for name in ("library.json", "library.json.bak", "library.aof"):
                src = old_dir / name
                dst = p / name
                if src.exists() and not dst.exists():
                    shutil.copy2(src, dst)
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_art_cache_dir() -> Path:
    """Return resolved art cache directory, creating it if needed.

    Defaults to APP_DIR/cache/art (local SSD) rather than the project
    directory which may sit on a slow network mount.
    """
    if settings.art_cache_dir:
        p = Path(settings.art_cache_dir)
    else:
        p = APP_DIR / "cache" / "art"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_conversion_cache_dir() -> Path:
    """Return resolved conversion cache directory, creating sub-dirs if needed.

    Defaults to APP_DIR/cache/conversion (local SSD).
    """
    if settings.conversion_cache_dir:
        p = Path(settings.conversion_cache_dir)
    else:
        p = APP_DIR / "cache" / "conversion"
    for sub in ("sid", "midi", "tracker"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    return p
