# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Append-Only File writer for durability.

Every mutating operation on the TrackStore is recorded as a single JSON line
appended to ``library.aof``.  A background merger process periodically
consolidates the AOF into the full snapshot (``library.json``).

The main process never serialises the full state — only one-line appends.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class AOFWriter:
    """Buffered AOF writer.  Flushes to disk periodically or on demand."""

    def __init__(self, path: Path, flush_interval: float = 0.1) -> None:
        self._path = path
        self._flush_interval = flush_interval
        self._buffer: list[str] = []
        self._flush_task: asyncio.Task | None = None
        self._fd: int | None = None

    def _ensure_fd(self) -> int:
        if self._fd is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(
                str(self._path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o644,
            )
        return self._fd

    def append(self, op: str, **kwargs: Any) -> None:
        """Buffer a single AOF record.  Non-blocking."""
        record = {"op": op, "ts": time.time(), **kwargs}
        self._buffer.append(json.dumps(record, default=str) + "\n")

    async def flush(self) -> None:
        """Write buffered records to disk in a thread to avoid blocking."""
        if not self._buffer:
            return
        lines = self._buffer
        self._buffer = []
        data = "".join(lines)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._write_sync, data)

    def _write_sync(self, data: str) -> None:
        fd = self._ensure_fd()
        os.write(fd, data.encode())

    def flush_sync(self) -> None:
        """Synchronous flush — used during shutdown."""
        if not self._buffer:
            return
        data = "".join(self._buffer)
        self._buffer.clear()
        self._write_sync(data)

    async def start_auto_flush(self) -> None:
        """Start the periodic flush loop."""
        if self._flush_task is not None:
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(self._flush_interval)
                try:
                    await self.flush()
                except Exception:
                    log.exception("AOF flush error")

        self._flush_task = asyncio.create_task(_loop())

    def stop(self) -> None:
        """Cancel the auto-flush task and close the file descriptor."""
        if self._flush_task:
            self._flush_task.cancel()
            self._flush_task = None
        self.flush_sync()
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @property
    def path(self) -> Path:
        return self._path
