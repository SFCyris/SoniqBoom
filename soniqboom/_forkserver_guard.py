# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Loaded by the scan workers' forkserver at its start (its preload —
``scanner.prepare_worker_forkserver``).

A client that connects to the forkserver and closes without sending its
file descriptors (``connect_to_new_process`` runs out of descriptors
between its connect and its send) makes the stdlib's ``reduction.recvfds``
raise ``EOFError``, which the forkserver's loop doesn't catch: the
forkserver dies, and the next pool has to start a new one.  Here that
request becomes ``ConnectionAbortedError`` (``ECONNABORTED``), which the
loop skips — the forkserver goes on."""

import errno
from multiprocessing import reduction

_recvfds = reduction.recvfds


def _recvfds_or_abort(sock, size):
    try:
        return _recvfds(sock, size)
    except EOFError as exc:
        raise ConnectionAbortedError(errno.ECONNABORTED,
                                     "the client sent no descriptors") from exc


if reduction.recvfds is _recvfds:
    reduction.recvfds = _recvfds_or_abort
