# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The HVSC documents, downloaded (Settings → Renderers → HVSC SID database,
"Download from the internet").

With ``hvsc_source`` "online" the two HVSC documents the SID lookups read
(``core/hvsc``) — ``Songlengths.md5`` and ``STIL.txt`` — come from the
6581.nl HVSC mirror (``online_hvsc.BASE``) instead of a local folder, into
the online HVSC collection's own ``DOCUMENTS`` folder
(``online_hvsc.HVSC_ONLINE.docs_dir()``), where ``online_hvsc
.apply_docs_fallback`` points the lookups.  Nothing is listed: the collection
itself stays off.

* **Weekly**, by a conditional GET (If-None-Match / If-Modified-Since): an
  unchanged HVSC is a 304 and nothing else; ``STIL.txt`` is downloaded only
  when ``Songlengths.md5`` changed.  "Check now" (``refresh(manual=True)``)
  asks at once.
* **The collection on** (Settings → Online collections → HVSC): its own
  index refresh keeps the same documents up to date, so this download does
  not run — never two downloads of the same files.
* **Failures** wait as the collections' index refreshes do
  (``online_catalog._RETRY_*``): an hour after a failed connection, a day
  after a refusal (403 / 404 / 410 / 451, or a Songlengths listing
  implausibly few tunes), doubling with each failure in a row up to a week,
  and never before an answer's ``Retry-After``.
* **Offline mode** (``netpolicy``): nothing is fetched.

State (validators, last check, failures) is kept in the store under
``hvsc_download``.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid

log = logging.getLogger(__name__)

CFG = "hvsc_download"
INTERVAL_S = 7 * 24 * 3600
_DOCS = ("Songlengths.md5", "STIL.txt")

# The last scheduled try (in memory): at most one an hour whatever the stored
# state says — a state that can't be saved, offline mode.
_MIN_GAP_S = 3600
_last_try = 0.0

_task: "asyncio.Task | None" = None
_wake: "asyncio.Event | None" = None
_lock: "asyncio.Lock | None" = None
_running = False


def _store():
    from soniqboom.core.store import get_store
    return get_store()


def state() -> dict:
    try:
        v = _store().get_config(CFG)
    except Exception:                                   # noqa: BLE001
        v = None
    return dict(v) if isinstance(v, dict) else {}


def _save(values: dict) -> None:
    st = state()
    st.update(values)
    try:
        _store().set_config(CFG, st)
    except Exception:                                   # noqa: BLE001
        log.debug("HVSC download: state not saved", exc_info=True)


def wanted() -> bool:
    """The HVSC card downloads the documents and the online HVSC collection
    (whose own refresh would) is off."""
    from soniqboom.core import online_hvsc
    return online_hvsc.online_docs_chosen() and not online_hvsc.HVSC_ONLINE.enabled()


def _docs_dir():
    from soniqboom.core import online_hvsc
    return online_hvsc.HVSC_ONLINE.docs_dir()


def have_docs() -> bool:
    return (_docs_dir() / "Songlengths.md5").exists()


# Failures ───────────────────────────────────────────────────────────────────
def _retry_due(f: dict) -> float:
    """When the next try after failure record ``f`` is due — the rule of
    ``OnlineCatalogue._retry_due`` with this download's weekly interval."""
    from soniqboom.core import online_catalog as oc
    try:
        at = float(f.get("at") or 0)
        streak = max(1, int(f.get("streak") or 1))
    except (TypeError, ValueError):
        return 0.0
    base = oc._RETRY_AFTER_REFUSAL_S if f.get("refused") else oc._RETRY_AFTER_FAILURE_S
    cap = max(base, min(oc._RETRY_MAX_S, INTERVAL_S))
    wait = min(cap, base * (2 ** min(streak - 1, 10)))
    try:
        retry_at = min(float(f.get("retry_at") or 0), at + oc._RETRY_MAX_S)
    except (TypeError, ValueError):
        retry_at = 0.0
    return max(at + wait, retry_at)


def _note_failure(exc: BaseException) -> None:
    from soniqboom.core import online_catalog as oc
    prev = state().get("failure") or {}
    now = time.time()
    ra = oc.retry_after_of(exc, now)
    if ra:
        ra = min(ra, float(oc._RETRY_MAX_S))
    f = {"at": now, "streak": min(int(prev.get("streak") or 0) + 1, 30),
         "refused": oc.is_refusal(exc), "error": str(exc)[:300],
         "retry_at": now + ra if ra else 0.0}
    _save({"failure": f})
    log.info("HVSC download: next try no sooner than %s (%s, %d failed in a row)",
             time.strftime("%Y-%m-%d %H:%M", time.localtime(_retry_due(f))),
             "a refusal" if f["refused"] else "a failure", f["streak"])


def next_due(now: "float | None" = None) -> float:
    now = time.time() if now is None else now
    st = state()
    try:
        checked = float(st.get("checked_at") or 0)
    except (TypeError, ValueError):
        checked = 0.0
    due = checked + INTERVAL_S if (checked and have_docs()) else now
    f = st.get("failure")
    if isinstance(f, dict) and f.get("at"):
        due = max(due if checked > float(f["at"]) else 0, _retry_due(f))
    if _last_try:
        due = max(due, _last_try + _MIN_GAP_S)
    return due


# The download ───────────────────────────────────────────────────────────────
def _fetch() -> dict:
    """Download the documents when they changed.  Blocking (no store
    writes: the caller saves ``info``).  Returns ``{"unchanged": True}`` or
    ``{"tunes": n, "info": validators}``; raises on a failure (a Songlengths
    listing implausibly few tunes: ``online_catalog.IndexRefused``)."""
    import httpx

    from soniqboom.core import netpolicy, online_catalog
    from soniqboom.core import online_hvsc as oh
    netpolicy.check(oh.BASE)
    docs = _docs_dir()
    st = state()
    headers = {"User-Agent": online_catalog.user_agent()}
    if all((docs / n).exists() for n in _DOCS):
        if st.get("etag"):
            headers["If-None-Match"] = st["etag"]
        if st.get("last_modified"):
            headers["If-Modified-Since"] = st["last_modified"]
    work = docs.parent / f"docs-download.{uuid.uuid4().hex[:8]}"
    work.mkdir(exist_ok=True)
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, read=300.0), follow_redirects=True) as c:
            r = c.get(f"{oh.BASE}/DOCUMENTS/Songlengths.md5", headers=headers)
            if r.status_code == 304:
                return {"unchanged": True}
            r.raise_for_status()
            text = r.content.decode("latin-1")
            tunes = sum(1 for _ in oh.parse_songlengths(text))
            if tunes < oh.HVSC_ONLINE.min_rows:
                raise online_catalog.IndexRefused(
                    f"HVSC's Songlengths lists only {tunes} tunes — implausible")
            (work / "Songlengths.md5").write_bytes(r.content)
            info = {"etag": r.headers.get("etag"),
                    "last_modified": r.headers.get("last-modified"),
                    "index_date": oh._http_date(r.headers.get("last-modified"))}
            try:
                s = c.get(f"{oh.BASE}/DOCUMENTS/STIL.txt",
                          headers={"User-Agent": online_catalog.user_agent()})
                s.raise_for_status()
                (work / "STIL.txt").write_bytes(s.content)
            except Exception as exc:                    # noqa: BLE001
                # The song lengths came: they are used, with the STIL in use
                # (or none).  No validators kept, so the next check asks for
                # both again and the STIL catches up then.
                log.warning("HVSC download: STIL.txt not downloaded (%s)", exc)
                kept = docs / "STIL.txt"
                if kept.exists():
                    shutil.copyfile(kept, work / "STIL.txt")
                else:
                    (work / "STIL.txt").write_bytes(b"")
                info["etag"] = info["last_modified"] = None
        from soniqboom.core import online_hvsc
        if not online_hvsc.online_docs_chosen() or online_hvsc.HVSC_ONLINE.enabled():
            # Switched meanwhile (the collection's refresh keeps them now,
            # or a local folder is chosen): not put in place.
            raise _Superseded()
        for n in _DOCS:
            (work / n).replace(docs / n)
            (docs / f"{n}.new").unlink(missing_ok=True)   # a refused index's staged copy
        return {"tunes": tunes, "info": info}
    finally:
        shutil.rmtree(work, ignore_errors=True)


class _Superseded(Exception):
    """The download finished after the choice changed — nothing installed."""


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def refresh(*, manual: bool = False) -> dict:
    """Check for new documents now (one check at a time) and point the SID
    lookups at them.  ``{"unchanged": True}`` / ``{"tunes": n}`` /
    ``{"error": "…"}``."""
    global _running, _last_try
    from soniqboom.core import online_hvsc
    if not online_hvsc.online_docs_chosen():
        return {"error": "The HVSC database is set to a local folder"}
    if not wanted():
        return {"error": "The HVSC online collection keeps these documents up to date"}
    f = state().get("failure")
    if manual and isinstance(f, dict) and float(f.get("retry_at") or 0) > time.time():
        # The mirror asked to wait (Retry-After): a click doesn't override it.
        return {"error": "The HVSC mirror asked to wait — the next check is possible "
                         + time.strftime("%Y-%m-%d %H:%M", time.localtime(float(f["retry_at"])))}
    async with _get_lock():
        _running = True
        if not manual:
            _last_try = time.time()
        try:
            res = await asyncio.get_running_loop().run_in_executor(None, _fetch)
        except _Superseded:
            return {"error": "The HVSC source changed meanwhile"}
        except Exception as exc:                        # noqa: BLE001
            from soniqboom.core import netpolicy
            if isinstance(exc, netpolicy.Offline):
                return {"error": "Offline mode — nothing downloaded"}
            log.warning("HVSC download failed%s: %s", " (Check now)" if manual else "", exc)
            _note_failure(exc)
            return {"error": str(exc)[:300]}
        finally:
            _running = False
        _save({**res.pop("info", {}), "checked_at": time.time(), "failure": None})
        if not res.get("unchanged") or not online_hvsc.fallback_active():
            online_hvsc.apply_docs_fallback()
        return res


def status() -> dict:
    st = state()
    f = st.get("failure") if isinstance(st.get("failure"), dict) else None
    from soniqboom.core import netpolicy
    return {"running": _running,
            "offline": netpolicy.offline(),
            "checked_at": st.get("checked_at"),
            "index_date": st.get("index_date"),
            "error": (f or {}).get("error"),
            "next_at": next_due() if wanted() else None,
            "by_collection": not wanted() and _collection_on()}


def _collection_on() -> bool:
    from soniqboom.core import online_hvsc
    return online_hvsc.HVSC_ONLINE.enabled()


# The schedule ───────────────────────────────────────────────────────────────
async def _nap(seconds: float) -> None:
    """Sleep up to ``seconds`` — cut short by ``start`` (the choice changed)."""
    ev = _get_wake()
    try:
        await asyncio.wait_for(ev.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    ev.clear()


async def _loop() -> None:
    failures = 0
    while wanted():
        try:
            wait = next_due() - time.time()
            if wait > 0:
                await _nap(min(wait, 3600))
                continue
            log.info("HVSC download: scheduled check")
            await refresh()
            failures = 0
            await _nap(60)
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            failures += 1
            log.warning("HVSC download: schedule hit an error (%d in a row) — carrying on",
                        failures, exc_info=True)
            await asyncio.sleep(min(3600, 60 * failures))


def _get_wake() -> asyncio.Event:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    return _wake


def start() -> None:
    """Arm the weekly check when it is wanted (from the event loop): at
    startup, and when the HVSC card's source or the collection changes.  The
    documents already downloaded are put in use at once."""
    global _task
    from soniqboom.core import online_hvsc
    _drop_leftovers()
    online_hvsc.apply_docs_fallback()      # (no-op while the same documents are in use)
    if not wanted():
        if _wake is not None:
            _wake.set()                  # a sleeping loop sees it isn't wanted and ends
        return
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop(), name="hvsc-download")
    else:
        _get_wake().set()                # re-reads when it is due


def _drop_leftovers() -> None:
    """Temporary download folders a crash left behind."""
    try:
        for d in _docs_dir().parent.glob("docs-download.*"):
            shutil.rmtree(d, ignore_errors=True)
    except Exception:                                   # noqa: BLE001
        pass


def _take_over_from_collection() -> None:
    """The collection was switched off while its downloaded documents stay in
    use: its validators and its last refresh become this download's, so its
    next check is a 304 a week after the collection's, not a download now."""
    from soniqboom.core import online_hvsc
    try:
        info = online_hvsc.HVSC_ONLINE.index_info()
    except Exception:                                   # noqa: BLE001
        return
    try:
        built = float(info.get("built_at") or 0)
    except (TypeError, ValueError):
        built = 0.0
    st = state()
    if built and have_docs() and built > float(st.get("checked_at") or 0):
        _save({"etag": info.get("etag"), "last_modified": info.get("last_modified"),
               "index_date": info.get("index_date") or st.get("index_date"),
               "checked_at": built, "failure": None})


def all_tasks() -> list:
    return [_task] if _task is not None and not _task.done() else []
