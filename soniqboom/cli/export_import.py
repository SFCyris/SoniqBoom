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
    from soniqboom.core.persistence import init_persistence
    from soniqboom.core.store import get_store

    data_dir = get_data_dir()
    print("Loading library…")
    init_persistence(data_dir)

    store = get_store()
    count = store.track_count()
    print(f"Loaded {count} tracks.")

    payload = {"version": 2, "data": store.to_snapshot()}

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump(payload, f)

    size_kb = out.stat().st_size // 1024
    print(f"Exported {count} tracks → {out}  ({size_kb} KB)")


async def import_db(input_path: str) -> None:
    from soniqboom.core.persistence import populate_store, write_snapshot_sync
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

    populate_store(data)
    store = get_store()
    store.rebuild_indexes()

    data_dir = get_data_dir()
    write_snapshot_sync(data_dir)

    print(f"Imported {store.track_count()} tracks. Snapshot written to {data_dir}.")
