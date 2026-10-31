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
* **Progress** (``progress()``, in ``GET /api/admin/hvsc/status``): one
  check's phase — starting, connecting, downloading (bytes of the
  ``Content-Length``), checking, installing, then the library apply's
  loading / applying (SID tracks matched of all) — and how it ended (done,
  already up to date, offline, failed with a plain reason).  In memory: a
  restart starts at "idle", with the last check's outcome from the store
  (``last_result``).  A download that sends nothing for ``_STALL_S`` or runs
  past ``_MAX_S`` fails as stalled.

State (validators, last check, failures, last result) is kept in the store
under ``hvsc_download``.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import threading
import time
import uuid
from urllib.parse import urlsplit

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
_now_tasks: set = set()                 # Check now's background checks
_cancel = threading.Event()             # the server stops: a running download ends

# A download that sends nothing for this long fails (the read timeout), and
# one still going after ``_MAX_S`` fails too (a trickle) — never a check that
# says "Downloading…" for ever.
_STALL_S = 60.0
_MAX_S = 30 * 60.0


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


def mirror_host() -> str:
    """The HVSC mirror's host name, as the card names it (``6581.nl``)."""
    from soniqboom.core import online_hvsc
    try:
        return urlsplit(online_hvsc.BASE).hostname or online_hvsc.BASE
    except ValueError:
        return online_hvsc.BASE


# Progress ───────────────────────────────────────────────────────────────────
# One check at a time, and what the card shows of it: written by the download
# thread (bytes), by the library apply on the loop (``apply_*``) and read by
# the status endpoint — one dict behind a lock.  ``job`` counts the checks
# (and applies) since the start, so a page sees a new one begin; ``kind`` is
# the job's ("download", "apply", "reextract"), ``error`` how a failed one
# failed ("network", "stalled", "refused", "server", "offline", "apply",
# "superseded", "other") beside its sentence, ``message``.
ACTIVE = frozenset({"starting", "connecting", "downloading", "checking", "installing",
                    "loading", "applying"})
_plock = threading.Lock()
_progress: dict = {"phase": "idle", "job": 0}
_apply_runs = 0                          # library applies running (``apply_started``)
# A pending phase nothing moves on for this long is over (an apply that was
# scheduled but never ran, a "starting" the schedule didn't pick up).
_PENDING_GRACE_S = 20.0


def _set_progress(**kw) -> None:
    with _plock:
        _progress.update(kw)
        _progress["updated_at"] = time.time()


def _set_job(job: int, **kw) -> None:
    """``_set_progress`` for check ``job`` only — a job that began since
    (an apply of its own) is left alone."""
    with _plock:
        if _job_is(job):
            _progress.update(kw)
            _progress["updated_at"] = time.time()


def _new_job(phase: str, **kw) -> None:
    now = time.time()
    with _plock:
        job = int(_progress.get("job") or 0) + 1
        _progress.clear()
        _progress.update(phase=phase, job=job, started_at=now, updated_at=now, **kw)


def busy() -> bool:
    """A check (or its library apply) is under way."""
    return _running or progress()["phase"] in ACTIVE


def progress() -> dict:
    """The current (or last) check as the card shows it — a copy."""
    with _plock:
        p = dict(_progress)
        now = time.time()
        idle_for = now - float(p.get("updated_at") or now)
        if (p.get("phase") == "starting" and not _running and idle_for > _PENDING_GRACE_S):
            # Save chose the download but the schedule found nothing due.
            _progress.update(phase="idle")
            p["phase"] = "idle"
        elif (p.get("phase") in ("loading", "applying") and _apply_runs <= 0
              and not _running and idle_for > _PENDING_GRACE_S):
            _progress.update(phase="error", error="apply",
                             message="Updating the library stopped before it finished.",
                             finished_at=now)
            p = dict(_progress)
    p["now"] = now
    return p


def mark_starting() -> None:
    """Save & reload chose the download and a check is due now: the card
    says so until the schedule's check takes over (``_PENDING_GRACE_S``)."""
    if not busy():
        _new_job("starting", kind="download")


def apply_started(kind: str = "apply", *, follow: bool = True) -> int:
    """The library apply (``hvsc_apply.apply_hvsc_to_library``) begins —
    the documents are read first.  ``follow`` (the downloaded documents'
    own apply): a download's job goes on with it, and while a download
    runs it reports nothing (the card shows the download; the apply after
    it is the one shown).  Anything else is a job of its own: ``kind``
    "apply" (documents put in use: startup, a local folder saved) or
    "reextract" (Re-extract SID metadata).  Returns the job for
    ``apply_reporter`` / ``apply_finished`` / ``apply_failed`` — 0 reports
    nothing, and a job a newer one replaced meanwhile is not written to."""
    global _apply_runs
    with _plock:
        _apply_runs += 1
        phase, jkind = _progress.get("phase"), _progress.get("kind")
        if follow and kind == "apply":
            if jkind == "download" and phase in ("installing", "loading", "applying"):
                _progress.update(phase="loading", done=0, total=None, updated_at=time.time())
                return int(_progress.get("job") or 0)
            if _running or (jkind == "download" and phase in ACTIVE):
                return 0
    _new_job("loading", kind=kind)
    with _plock:
        return int(_progress.get("job") or 0)


def _job_is(job: "int | None") -> bool:
    """(under ``_plock``) ``job`` is the current one."""
    return bool(job) and int(_progress.get("job") or 0) == job


def apply_reporter(job: "int | None" = None):
    """The ``progress`` callback of ``hvsc_apply.apply_hvsc_to_library`` for
    job ``job``: ``stage`` "matching" (``done`` of ``total`` SID tracks) or
    "saving" (``total`` changed tracks written); ``counts``: the documents'
    entries once read (``songlengths``, ``stil``)."""
    def _report(stage: str, done: int, total: int, **counts) -> None:
        with _plock:
            if not _job_is(job):
                return
            _progress.update(phase="applying", stage=stage, done=int(done), total=int(total),
                             updated_at=time.time(), **counts)
    return _report


def apply_finished(res: dict, *, job: "int | None" = None,
                   songlengths: int = 0, stil: int = 0) -> None:
    global _apply_runs
    now = time.time()
    with _plock:
        _apply_runs = max(0, _apply_runs - 1)
        ours = _job_is(job)
        unchanged = bool(_progress.get("unchanged")) if ours else False
        kind = (_progress.get("kind") if ours else None) or "apply"
    result = {"at": now, "kind": kind, "unchanged": unchanged,
              "songlengths": int(songlengths), "stil": int(stil),
              "scanned": int(res.get("scanned") or 0), "updated": int(res.get("updated") or 0),
              "skipped_catalogue": int(res.get("skipped_catalogue") or 0),
              "skipped_missing": int(res.get("skipped_missing") or 0),
              "skipped_unreadable": int(res.get("skipped_unreadable") or 0)}
    _set_job(job, phase="done", finished_at=now, result=result)
    _save({"last_result": result})


def apply_failed(exc: BaseException, *, job: "int | None" = None) -> None:
    global _apply_runs
    with _plock:
        _apply_runs = max(0, _apply_runs - 1)
        if not _job_is(job):
            return
        job_kind = _progress.get("kind") or "download"
    lead = ("The HVSC documents are in place, but updating"
            if job_kind in ("download", "apply") else "Updating")
    _set_job(job, phase="error", error="apply", finished_at=time.time(),
             message=f"{lead} your SID tracks failed ({str(exc)[:200]}).")


def _plain_error(exc: BaseException) -> "tuple[str, str]":
    """``(kind, sentence)`` of a failed check, in the card's words."""
    import httpx

    from soniqboom.core import netpolicy, online_catalog
    host = mirror_host()
    if isinstance(exc, netpolicy.Offline):
        return "offline", "Offline mode is on — nothing is downloaded."
    if isinstance(exc, _Stalled):
        return "stalled", str(exc)
    if isinstance(exc, online_catalog.IndexRefused):
        return "refused", f"The song-length list from {host} looked incomplete, so it was not used ({exc})."
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return "stalled", (f"{host} stopped sending data for {int(_STALL_S)} seconds, "
                           "so the download was stopped.")
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "network", (f"Could not reach {host}. Check this server's internet connection "
                           "(or its proxy).")
    code = online_catalog.http_status_of(exc)
    if code == 429 or (isinstance(code, int) and code >= 500):
        return "server", f"{host} could not answer right now (HTTP {code})."
    if isinstance(code, int):
        return "refused", f"{host} refused the download (HTTP {code})."
    if isinstance(exc, httpx.TransportError):
        return "network", f"The connection to {host} broke off ({exc.__class__.__name__})."
    return "other", f"The download failed: {str(exc)[:200]}"


class _Stalled(Exception):
    """A download that sent nothing for ``_STALL_S`` or ran past ``_MAX_S``."""


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
def _still_wanted() -> bool:
    from soniqboom.core import online_hvsc
    return online_hvsc.online_docs_chosen() and not online_hvsc.HVSC_ONLINE.enabled()


def _get(c, url: str, headers: dict, *, job: int, step: int, what: str, deadline: float):
    """GET ``url`` with its progress (check ``job``; ``step`` of 2, ``what``
    the card's name for the file): ``(response, body)`` — body None for a
    304.  Stops between chunks when the choice changed (``_Superseded``) or
    the server is stopping (``_cancel``)."""
    _set_job(job, phase="connecting", step=step, steps=2, doc=what, done=0, total=None)
    with c.stream("GET", url, headers=headers) as r:
        if r.status_code == 304:
            return r, None
        r.raise_for_status()
        try:
            total = int(r.headers.get("content-length") or 0) or None
        except ValueError:
            total = None
        # (bytes on the wire — the Content-Length's own count when the
        # answer is compressed)
        _set_job(job, phase="downloading", done=0, total=total)
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf += chunk
            _set_job(job, done=r.num_bytes_downloaded)
            if _cancel.is_set() or not _still_wanted():
                raise _Superseded()
            if time.monotonic() > deadline:
                raise _Stalled(f"The download from {mirror_host()} was too slow and was stopped "
                               f"after {int(_MAX_S // 60)} minutes.")
        return r, bytes(buf)


def _fetch(job: int = 0) -> dict:
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
    deadline = time.monotonic() + _MAX_S
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, read=_STALL_S), follow_redirects=True) as c:
            r, body = _get(c, f"{oh.BASE}/DOCUMENTS/Songlengths.md5", headers,
                           job=job, step=1, what="song lengths", deadline=deadline)
            if body is None:
                return {"unchanged": True}
            _set_job(job, phase="checking")
            text = body.decode("latin-1")
            tunes = sum(1 for _ in oh.parse_songlengths(text))
            if tunes < oh.HVSC_ONLINE.min_rows:
                raise online_catalog.IndexRefused(
                    f"HVSC's Songlengths lists only {tunes} tunes — implausible")
            _set_job(job, tunes=tunes)
            (work / "Songlengths.md5").write_bytes(body)
            info = {"etag": r.headers.get("etag"),
                    "last_modified": r.headers.get("last-modified"),
                    "index_date": oh._http_date(r.headers.get("last-modified"))}
            try:
                _s, stil = _get(c, f"{oh.BASE}/DOCUMENTS/STIL.txt",
                                {"User-Agent": online_catalog.user_agent()},
                                job=job, step=2, what="STIL commentary", deadline=deadline)
                (work / "STIL.txt").write_bytes(stil or b"")
            except _Superseded:
                raise
            except Exception as exc:                    # noqa: BLE001
                # The song lengths came: they are used, with the STIL in use
                # (or none).  No validators kept, so the next check asks for
                # both again and the STIL catches up then.
                log.warning("HVSC download: STIL.txt not downloaded (%s)", exc)
                _set_job(job, stil_missing=True)
                kept = docs / "STIL.txt"
                if kept.exists():
                    shutil.copyfile(kept, work / "STIL.txt")
                else:
                    (work / "STIL.txt").write_bytes(b"")
                info["etag"] = info["last_modified"] = None
        if not _still_wanted():
            # Switched meanwhile (the collection's refresh keeps them now,
            # or a local folder is chosen): not put in place.
            raise _Superseded()
        _set_job(job, phase="installing")
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


def _not_now(manual: bool) -> "dict | None":
    """Why no check runs at all (``{"error": …}``), or None."""
    from soniqboom.core import online_hvsc
    if not online_hvsc.online_docs_chosen():
        return {"error": "The HVSC database is set to a local folder"}
    if not wanted():
        return {"error": "The HVSC online collection keeps these documents up to date"}
    f = state().get("failure")
    if manual and isinstance(f, dict) and float(f.get("retry_at") or 0) > time.time():
        # The mirror asked to wait (Retry-After): a click doesn't override it.
        return {"error": "The HVSC mirror asked to wait — the next check is possible "
                         + time.strftime("%Y-%m-%d %H:%M", time.localtime(float(f["retry_at"]))),
                "retry_at": float(f["retry_at"])}
    return None


async def refresh(*, manual: bool = False) -> dict:
    """Check for new documents now (one check at a time) and point the SID
    lookups at them.  ``{"unchanged": True}`` / ``{"tunes": n}`` /
    ``{"error": "…"}``.  Its progress and outcome: ``progress()``."""
    global _running, _last_try
    from soniqboom.core import online_hvsc
    no = _not_now(manual)
    if no:
        return no
    async with _get_lock():
        _running = True
        _cancel.clear()
        if progress()["phase"] == "starting":
            _set_progress(phase="connecting", kind="download", manual=manual)  # Save's / Check now's
        else:
            _new_job("connecting", kind="download", manual=manual)
        job = progress()["job"]
        if not manual:
            _last_try = time.time()
        try:
            res = await asyncio.get_running_loop().run_in_executor(None, _fetch, job)
        except asyncio.CancelledError:
            _cancel.set()                # (the server stops: the thread ends at its next chunk)
            _set_job(job, phase="error", error="other", finished_at=time.time(),
                     message="The download was stopped.")
            raise
        except _Superseded:
            _set_job(job, phase="error", error="superseded", finished_at=time.time(),
                     message="The HVSC source was changed during the download — "
                             "nothing was installed.")
            return {"error": "The HVSC source changed meanwhile"}
        except Exception as exc:                        # noqa: BLE001
            from soniqboom.core import netpolicy
            kind, message = _plain_error(exc)
            if isinstance(exc, netpolicy.Offline):
                _set_job(job, phase="offline", error=kind, message=message,
                         finished_at=time.time())
                return {"error": "Offline mode — nothing downloaded"}
            log.warning("HVSC download failed%s: %s", " (Check now)" if manual else "", exc)
            _note_failure(exc)
            f = state().get("failure") or {}
            _set_job(job, phase="error", error=kind, message=message, finished_at=time.time(),
                     retry_at=float(f.get("retry_at") or 0) or None)
            return {"error": str(exc)[:300]}
        finally:
            _running = False
        _save({**res.pop("info", {}), "checked_at": time.time(), "failure": None})
        unchanged = bool(res.get("unchanged"))
        applying = False
        if not unchanged or not online_hvsc.fallback_active():
            # The SIDs of the library take the documents (a task on the
            # loop: ``apply_started`` … ``apply_finished`` carry the job on).
            _set_job(job, phase="loading", unchanged=unchanged, done=0, total=None)
            applying = bool(online_hvsc.apply_docs_fallback())
        if not applying:
            now = time.time()
            result = {"at": now, "kind": "download", "unchanged": unchanged,
                      "songlengths": int(res.get("tunes") or 0)}
            _set_job(job, phase="done", unchanged=unchanged, finished_at=now, result=result)
            if not unchanged:
                _save({"last_result": result})      # (a 304 keeps the last apply's counts)
        return res


def check_now() -> dict:
    """"Check now": the check runs in the background and the card follows
    it (``progress()``).  ``{"started": True, "job": n}``, ``{"busy": True}``
    while one runs, or ``{"error": …}`` when none can run."""
    no = _not_now(True)
    if no:
        return no
    if busy():
        return {"busy": True, "job": progress().get("job")}
    _new_job("starting", kind="download", manual=True)
    t = asyncio.get_running_loop().create_task(refresh(manual=True), name="hvsc-download.now")
    _now_tasks.add(t)
    t.add_done_callback(_now_tasks.discard)
    return {"started": True, "job": progress().get("job")}


def status() -> dict:
    st = state()
    f = st.get("failure") if isinstance(st.get("failure"), dict) else None
    from soniqboom.core import netpolicy
    return {"running": _running,
            "offline": netpolicy.offline(),
            "checked_at": st.get("checked_at"),
            "index_date": st.get("index_date"),
            "error": (f or {}).get("error"),
            "retry_at": (f or {}).get("retry_at") or None,
            "next_at": next_due() if wanted() else None,
            "by_collection": not wanted() and _collection_on(),
            "mirror": mirror_host(),
            "last_result": st.get("last_result") if isinstance(st.get("last_result"), dict) else None,
            "progress": progress()}


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
            await _nap(min(3600, 60 * failures))     # (a Save wakes it)


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
    if not _running:
        _drop_leftovers()                # (never the folder a running check fills)
    online_hvsc.apply_docs_fallback()      # (no-op while the same documents are in use)
    if not wanted():
        if _wake is not None:
            _wake.set()                  # a sleeping loop sees it isn't wanted and ends
        return
    if next_due() <= time.time():
        mark_starting()                  # the card says a download is coming
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
    out = [_task] if _task is not None and not _task.done() else []
    return out + [t for t in _now_tasks if not t.done()]
