# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Persistent AirPlay credentials store.

AirPlay 2 receivers (Apple TV 4+, HomePod, macOS AirPlay Receiver) require
a one-time pairing handshake before they'll accept streams.  The user
sees a 4-digit PIN on the device; the controller submits it; pyatv hands
back a credentials blob (typically a hex string a few hundred bytes long)
that proves to the device on every subsequent connect that it has
already authorised us.

The credentials don't expire on Apple's side — once paired, the device
remembers us until the user revokes access in System Settings → AirPlay
Receiver → Allow AirPlay for → … .  So we persist the blob to disk and
re-apply it on every ``connect()``, which removes the PIN prompt on
restart and on session re-establishment.

File format (``data_dir/airplay_credentials.json``):

    {
        "<identifier>": {
            "credentials": "<hex/base64 string from pyatv>",
            "paired_at": <epoch seconds>,
            "device_name": "Optional display name for debugging"
        },
        ...
    }

The identifier is whatever pyatv uses to address the device — usually a
Bonjour service name or a MAC-derived hex string.  Stored verbatim so
``set_credentials(...)`` on the next connect matches exactly.

Security: the credentials are LAN-scope and only useful to whoever can
reach the receiver — they aren't passwords for any external service.
Still, we lock down the file to ``0600`` so a multi-user host doesn't
leak them sibling accounts.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

_FILE_NAME = "airplay_credentials.json"
_data_dir: Path | None = None


def init(data_dir: Path) -> None:
    """Set the data directory.  Idempotent — safe to re-call on reload."""
    global _data_dir
    _data_dir = data_dir


def _path() -> Path | None:
    """Return the credentials-file path, or None if init() hasn't run."""
    if _data_dir is None:
        return None
    return _data_dir / _FILE_NAME


def _load_all() -> dict:
    """Read the full credentials map.  Returns ``{}`` on missing/corrupt."""
    p = _path()
    if p is None or not p.exists():
        return {}
    try:
        with open(p, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.warning("airplay_credentials.json: expected object, got %s — ignoring",
                        type(data).__name__)
            return {}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to read airplay credentials: %s", exc)
        return {}


def _save_all(data: dict) -> bool:
    """Atomically write the full credentials map.

    Tmp+rename guarantees a polling reader (a parallel connect) never
    catches a half-written file.  Returns True on success; failures are
    logged but never raised — pairing is best-effort persistence.
    """
    p = _path()
    if p is None:
        log.warning("airplay_credentials store not initialised — credentials will not persist")
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.new")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        # Lock down — LAN creds, but no reason to make them world-readable.
        try:
            os.chmod(p, 0o600)
        except OSError:
            # FS that doesn't support chmod (FAT, network share) — non-fatal.
            pass
        return True
    except OSError as exc:
        log.warning("Failed to write airplay credentials: %s", exc)
        return False


def get(identifier: str) -> str | None:
    """Return the stored credentials string for ``identifier``, or None.

    The string is whatever pyatv emitted from
    ``pair_handler.service.credentials`` at finish-time — we don't try
    to validate it, just round-trip it back to ``set_credentials()`` on
    the next connect.
    """
    entry = _load_all().get(identifier)
    if isinstance(entry, dict):
        creds = entry.get("credentials")
        if isinstance(creds, str) and creds:
            return creds
    # Legacy / hand-edited entry: tolerate a bare string at the top
    # level too (older builds wrote that shape).
    legacy = _load_all().get(identifier)
    if isinstance(legacy, str) and legacy:
        return legacy
    return None


def set(identifier: str, credentials: str, *, device_name: str = "") -> bool:
    """Save credentials for ``identifier``.

    ``device_name`` is purely informational — written into the entry so
    a future ``soniqboom`` tool / log line can render "Apple TV
    (Bedroom)" instead of an opaque bonjour identifier.
    """
    if not identifier or not credentials:
        log.warning("airplay_credentials.set: missing identifier or credentials — skipping")
        return False
    data = _load_all()
    data[identifier] = {
        "credentials": credentials,
        "paired_at":  int(time.time()),
        "device_name": device_name or "",
    }
    ok = _save_all(data)
    if ok:
        log.info("Stored AirPlay credentials for %s (%s)",
                 identifier, device_name or "no name")
    return ok


def forget(identifier: str) -> bool:
    """Remove stored credentials for ``identifier``.

    Returns True if an entry was removed, False if there was nothing to
    remove.  Used by an admin UI / CLI to revoke pairing locally without
    having to manage the device's allow-list.
    """
    data = _load_all()
    if identifier in data:
        del data[identifier]
        _save_all(data)
        log.info("Forgot AirPlay credentials for %s", identifier)
        return True
    return False


def list_identifiers() -> list[str]:
    """Return identifiers we have credentials for.  Used by admin UI."""
    return sorted(_load_all().keys())
