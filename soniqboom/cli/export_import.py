# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Export / import SoniqBoom library to/from .sbz (gzip-compressed JSON)."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

from soniqboom.config import get_data_dir


async def export_db(output_path: str) -> None:
    from soniqboom.core.persistence import (DB_VERSION, DatabaseVersionError,
                                            read_library_state)
    from soniqboom.core.users import accounts_on_disk

    # Read only: a server (of this version or an older one) may be using the
    # data folder — an older database is converted in memory for the backup,
    # the folder itself is left as it is.
    data_dir = get_data_dir()
    print("Loading library…")
    try:
        state = read_library_state(data_dir)
    except DatabaseVersionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    count = len(state.get("tracks") or {})
    print(f"Loaded {count} tracks.")

    users_map = {u["id"]: u["username"] for u in accounts_on_disk(data_dir)
                 if isinstance(u.get("id"), str) and isinstance(u.get("username"), str)}
    from soniqboom.core import radiodir
    from soniqboom.core.user_data import OWNER_CONFIG_KEY
    favorites = radiodir.favorites_by_user()
    shared = radiodir.unmigrated_shared_favorites()
    if shared is not None:
        # (a folder no start of this version ran on: its shared list is the
        # legacy owner's, as the start would make it)
        owner = (state.get("config") or {}).get(OWNER_CONFIG_KEY) or ""
        have = favorites.setdefault(owner, [])
        seen = {st.get("sid") for st in have}
        have += [st for st in shared if st.get("sid") not in seen]
    payload = {"version": 2, "db_version": DB_VERSION, "users_map": users_map,
               "favorites": favorites, "data": state}

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump(payload, f)

    size_kb = out.stat().st_size // 1024
    print(f"Exported {count} tracks → {out}  ({size_kb} KB)")


async def import_db(input_path: str) -> None:
    from soniqboom.core import persistence
    from soniqboom.core.persistence import (DatabaseVersionError, claim_library,
                                            import_backup_state, populate_store,
                                            release_library, write_imported_library)
    from soniqboom.core.user_data import claim_accounts
    from soniqboom.core.users import accounts_on_disk, legacy_owner_id
    from soniqboom.core.store import get_store

    inp = Path(input_path)
    if not inp.exists():
        print(f"ERROR: file not found: {inp}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading {inp}…")
    with gzip.open(inp, "rt", encoding="utf-8") as f:
        payload = json.load(f)

    version = payload.get("version", 0)
    if version != 2:
        print(f"ERROR: unsupported export version: {version}", file=sys.stderr)
        sys.exit(1)

    data = payload.get("data", {})
    print(f"Found {len(data.get('tracks', {}))} tracks in archive.")

    # Per-user data (plays, history) lands on this data directory's accounts
    # by user name; a user with no account here keeps it aside until one
    # exists, and an older backup's shared data goes to the legacy owner (the
    # oldest admin; no account yet: the first one).
    data_dir = get_data_dir()
    accounts = accounts_on_disk(data_dir)
    try:
        favorites: dict = {}
        data = import_backup_state(payload, legacy_owner=legacy_owner_id(accounts) or "",
                                   accounts=claim_accounts(accounts), favorites_out=favorites)
    except DatabaseVersionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # A running server holds the data directory and would write its own
    # library over the import when it stops.  (A file system with no locks
    # can't tell — the import goes ahead there, as it always did.)
    claimed = claim_library(data_dir, wait=0, quiet=True)
    if not claimed and persistence.claim_failure == "held":
        print(f"ERROR: a SoniqBoom server is using {data_dir} — stop it, then run the "
              "import again.", file=sys.stderr)
        sys.exit(1)
    try:
        populate_store(data)
        store = get_store()
        store.rebuild_indexes()
        why = write_imported_library(data_dir)
        if why == "empty":
            print(f"ERROR: {inp} holds no tracks — the library in {data_dir} was left "
                  "as it is.", file=sys.stderr)
            sys.exit(1)
        if why:
            print(f"ERROR: could not write the library to {data_dir}.", file=sys.stderr)
            sys.exit(1)
        if favorites:
            # (the backup's favourites replace the stored ones — as the web import)
            from soniqboom.core import radiodir
            for key in list(radiodir.favorites_by_user()):
                if key not in favorites:
                    radiodir.delete_favorites(key)
            for key, stations in favorites.items():
                radiodir.set_favorites(key, stations)
    finally:
        if claimed:
            release_library()

    print(f"Imported {store.track_count()} tracks into {data_dir}.")
