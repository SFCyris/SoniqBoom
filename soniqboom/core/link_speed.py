# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How fast is the link to a network share — and what that changes.

Why measure instead of looking at the address
---------------------------------------------
A share on ``10.x`` / ``192.168.x`` is usually on the local network, but not
always: a VPN hands out private addresses with internet latency, and a NAS on
the same LAN can sit behind a public name.  So the link is *measured*: a few
tiny round trips on an open connection (an FTP ``NOOP``, a WebDAV / web-folder
``OPTIONS``, an SMB ``stat`` of the share root), their median compared with
``FAST_RTT_S``.  On the test rig (2026-11-22, suite's FTP / WebDAV servers
behind a delay proxy) the probe read 1.3 / 1.9 ms for a simulated 1 ms LAN and
63.6 / 66.4 ms for a simulated 60 ms internet link — a clean split, so one
threshold well between the two does.

What depends on it (``limits``)
-------------------------------
Measured on the same rig, per 40 folder covers warmed:

    ============  ==============  ==============
    at a time     intranet FTP    internet FTP
    ============  ==============  ==============
    1             0.67 s          25.4 s
    4             0.17 s           6.5 s
    8             0.13 s           5.4 s
    ============  ==============  ==============

On a fast link one-at-a-time is already quick, and every extra parallel read
competes with the scan and playback for nothing; on a slow link each read is
mostly waiting, so running several at once is what makes covers arrive.  Hence:

* ``warm_width``  — how many folders the scan's cover warm-up reads at once.
* ``folder_jobs`` — how many on-screen folders' covers are fetched at once
  (``core/folder_covers.py``).
* ``backfill``    — how many embedded covers are recovered at once
  (``core/art_backfill.py``).
* ``ftp_reserve`` — FTP connections a scan may NOT take (opportunity "E"):
  while a scan downloads whole files every pooled connection can be busy for
  a whole download, and a cover waits for one to come free.  Measured with
  12 scan threads downloading 30 MB files on a 9-connection pool:

      ==========  ==================  =================  ===============
      link        cover, no reserve   cover, reserve 1   scan speed
      ==========  ==================  =================  ===============
      internet    9.2 s (one sample)  0.64 s             1.68 → 1.58 f/s
      intranet    274–289 ms          42–45 ms           15.6–15.8 → 15.9–16.0 f/s
      ==========  ==================  =================  ===============

  (intranet: 4 runs each of 20 s, alternating.)  The reserve helps on both
  links and the scan lost nothing measurable on the fast one (6 % on the
  slow one), so it is kept on every MEASURED link.  Before the first
  measurement (or when the probe fails) the pool keeps the old behaviour.

An unmeasured link (no probe yet, the probe failed, a local folder) gets the
limits SoniqBoom always had (``_DEFAULT``), so nothing gets worse before the
first measurement.

The probe runs in a worker thread (never on the event loop), at most once per
``_TTL`` per share, and is re-run after that so a share that moved (a laptop
leaving the VPN) is re-classified.
"""
from __future__ import annotations

import logging
import statistics
import threading
import time
from typing import NamedTuple

log = logging.getLogger(__name__)

# Between the measured intranet (1–2 ms) and internet (≈ 64 ms) probes, with
# room for Wi-Fi and a busy NAS (a few ms) on the fast side.
FAST_RTT_S = 0.015
_PROBES = 3                    # tiny round trips per measurement (median)
_TTL = 6 * 3600.0              # re-measure after this
_RETRY_S = 300.0               # after a failed probe


class Limits(NamedTuple):
    warm_width: int
    folder_jobs: int
    backfill: int
    ftp_reserve: int


# Today's behaviour — an unmeasured link keeps it.
_DEFAULT = Limits(warm_width=1, folder_jobs=2, backfill=4, ftp_reserve=0)
# Fast link: the old widths (one-at-a-time warm-up already took 0.67 s for
# 40 folders on the intranet rig), plus the FTP reserve (see above).
FAST = _DEFAULT._replace(ftp_reserve=1)
# Slow link: 4 at a time took the internet warm-up from 25.4 s to 6.5 s; 8
# gained little more (5.4 s) on a rig that gave every connection its own
# bandwidth — a real line shares it, so 4.
SLOW = Limits(warm_width=4, folder_jobs=4, backfill=8, ftp_reserve=1)


class Link(NamedTuple):
    rtt: float | None          # median probe round trip, seconds (None: failed)
    fast: bool | None          # None: not known
    at: float                  # time.monotonic() of the measurement


_lock = threading.Lock()
_probing: set[int] = set()     # id(source) while a probe thread runs


def _link_of(source) -> "Link | None":
    return getattr(source, "_sb_link", None)


def _fresh(link: "Link | None") -> bool:
    if link is None:
        return False
    age = time.monotonic() - link.at
    return age < (_TTL if link.fast is not None else _RETRY_S)


def measure(source) -> "bool | None":
    """Probe ``source`` now (blocking — call from a worker thread) and keep
    the result on it.  True = fast link, False = slow, None = unknown (a
    source without a probe, or the probe failed)."""
    probe = getattr(source, "probe_rtt", None)
    if probe is None:
        return None
    try:
        rtt = probe(_PROBES)
    except Exception as exc:                       # noqa: BLE001 — a probe never breaks a caller
        log.debug("link probe failed for %r: %s", source, exc)
        rtt = None
    prev = _link_of(source)
    if rtt is None and prev is not None and prev.fast is not None:
        # A probe that failed (a busy pool, a hiccup) keeps what was known —
        # retried after ``_RETRY_S`` instead of falling back to the defaults.
        try:
            source._sb_link = prev._replace(at=time.monotonic() - _TTL + _RETRY_S)
        except Exception:                          # noqa: BLE001
            pass
        _apply(source, prev.fast)
        return prev.fast
    fast = None if rtt is None else rtt <= FAST_RTT_S
    try:
        source._sb_link = Link(rtt, fast, time.monotonic())
    except Exception:                              # noqa: BLE001 — a source refusing attributes
        return fast
    _apply(source, fast)
    if rtt is not None and (prev is None or prev.fast != fast):
        log.info("Share link %s: %.1f ms round trip → %s", getattr(source, "_label", "") or
                 type(source).__name__, rtt * 1000, "fast" if fast else "slow")
    return fast


def ensure_measured(source) -> "bool | None":
    """``measure`` unless a fresh measurement is there (blocking — a worker
    thread; the scan's warm-up calls it once per scan)."""
    link = _link_of(source)
    if _fresh(link):
        return link.fast
    return measure(source)


def is_fast(source) -> "bool | None":
    """The last measurement of ``source`` (True fast / False slow / None not
    known).  Never blocks: when there is none or it is stale, a probe is
    started in a background thread and the answer comes on a later call."""
    if source is None:
        return None
    link = _link_of(source)
    if not _fresh(link):
        _probe_later(source)
    return None if link is None else link.fast


def _probe_later(source) -> None:
    if getattr(source, "probe_rtt", None) is None:
        return
    key = id(source)
    with _lock:
        if key in _probing:
            return
        _probing.add(key)

    def run():
        try:
            measure(source)
        finally:
            with _lock:
                _probing.discard(key)

    threading.Thread(target=run, name="link-probe", daemon=True).start()


def limits(source) -> Limits:
    """What the link of ``source`` allows (see the module docstring).
    Non-blocking (``is_fast``)."""
    fast = is_fast(source)
    if fast is None:
        return _DEFAULT
    # (the FTP pool can be a new one — an encoding switch, pools closed and
    # made again — while the measurement is still fresh: set its reserve)
    _apply(source, fast)
    return FAST if fast else SLOW


def _apply(source, fast: "bool | None") -> None:
    """Settings that live in the connection layer: the FTP pool's interactive
    reserve (opportunity "E") — set once the link is measured (fast or slow),
    cleared while it is not known."""
    pool = None
    try:
        pool = getattr(source, "_pool", None)
    except Exception:                              # noqa: BLE001 — a property that cannot resolve
        pool = None
    setter = getattr(pool, "set_interactive_reserve", None)
    if setter is not None:              # (a no-op when the value is already set)
        setter(0 if fast is None else (FAST if fast else SLOW).ftp_reserve)


def median_rtt(samples: "list[float]") -> "float | None":
    """The median of probe ``samples`` (None for none) — shared by the
    source probes."""
    return statistics.median(samples) if samples else None
