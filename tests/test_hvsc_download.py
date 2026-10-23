# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The HVSC documents downloaded for the SID lookups (``core/hvsc_download``:
Settings → Renderers → HVSC SID database, "Download from the internet"),
served by the local HTTP server of ``test_online_hvsc``: the download puts
them in use without listing the collection, a second check is a 304, a local
folder or the collection on means no download, a refusal waits a day, offline
mode fetches nothing, and a scan's auto-detected folder doesn't take over."""
from __future__ import annotations

import time

import pytest

from soniqboom.core import hvsc_download, netpolicy, online_hvsc
from test_online_hvsc import _Handler, _enable, _md5, env  # noqa: F401  (fixture)


@pytest.fixture
def online(env, monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "hvsc_source", "online")
    monkeypatch.setattr(hvsc_download, "_task", None)
    monkeypatch.setattr(hvsc_download, "_wake", None)
    monkeypatch.setattr(hvsc_download, "_lock", None)
    monkeypatch.setattr(hvsc_download, "_last_try", 0.0)
    return env


def _doc_gets(name: str) -> list:
    return [p for m, p in _Handler.log if m == "GET" and p.endswith(name)]


@pytest.mark.asyncio
async def test_the_download_serves_the_sid_lookups_without_listing_the_collection(online):
    from soniqboom.core.hvsc import get_hvsc
    res = await hvsc_download.refresh(manual=True)
    assert res == {"tunes": 3}
    hv = get_hvsc()
    assert hv.docs_path == str(online["cat"].dir() / "DOCUMENTS")
    assert online_hvsc.fallback_active()
    md5 = _md5("MUSICIANS/H/Hubbard_Rob/Commando.sid")
    assert hv.lookup_durations_by_md5(md5) == [279.0, 3.0, 62.0]
    assert "famous" in hv.lookup_stil_by_relpath("/MUSICIANS/H/Hubbard_Rob/Commando.sid")["text"]
    assert not online["cat"].enabled() and online["cat"].tune_count() == 0
    st = hvsc_download.status()
    assert st["checked_at"] and st["index_date"] and st["error"] is None
    assert st["next_at"] >= st["checked_at"] + hvsc_download.INTERVAL_S - 1


@pytest.mark.asyncio
async def test_an_unchanged_hvsc_is_not_downloaded_again(online):
    assert (await hvsc_download.refresh())["tunes"] == 3
    assert (await hvsc_download.refresh()) == {"unchanged": True}
    assert len(_doc_gets("Songlengths.md5")) == 2         # the second answered 304
    assert len(_doc_gets("STIL.txt")) == 1                # not asked for again


@pytest.mark.asyncio
async def test_no_download_for_a_local_folder_or_while_the_collection_is_on(online, monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "hvsc_source", "local")
    assert not hvsc_download.wanted()
    assert "local folder" in (await hvsc_download.refresh())["error"]
    monkeypatch.setattr(settings, "hvsc_source", "online")
    await _enable(online)                  # the collection's refresh keeps the documents
    _Handler.log.clear()
    assert not hvsc_download.wanted()
    assert "online collection" in (await hvsc_download.refresh())["error"]
    assert _doc_gets("Songlengths.md5") == []
    assert hvsc_download.status()["by_collection"]


@pytest.mark.asyncio
async def test_a_local_folder_wins_and_online_ignores_the_local_path(online, monkeypatch, tmp_path):
    from soniqboom.config import settings
    local = tmp_path / "HVSC" / "DOCUMENTS"
    local.mkdir(parents=True)
    monkeypatch.setattr(settings, "hvsc_docs_path", str(local))
    assert online_hvsc.local_docs_path() == ""            # online chosen: the path is kept, unused
    await hvsc_download.refresh()
    from soniqboom.core.hvsc import get_hvsc
    assert get_hvsc().docs_path == str(online["cat"].dir() / "DOCUMENTS")
    monkeypatch.setattr(settings, "hvsc_source", "local")
    assert online_hvsc.local_docs_path() == str(local)
    get_hvsc().configure(str(local))
    online_hvsc.apply_docs_fallback()
    assert get_hvsc().docs_path == str(local) and not online_hvsc.fallback_active()


@pytest.mark.asyncio
async def test_an_implausible_songlengths_is_refused_and_waits_a_day(online, monkeypatch):
    monkeypatch.setattr(online["cat"], "min_rows", 100)
    res = await hvsc_download.refresh()
    assert "implausible" in res["error"]
    assert not (online["cat"].dir() / "DOCUMENTS" / "Songlengths.md5").exists()
    f = hvsc_download.state()["failure"]
    assert f["refused"] and f["streak"] == 1
    assert hvsc_download.next_due() >= f["at"] + 24 * 3600 - 1
    monkeypatch.setattr(online["cat"], "min_rows", 1)
    assert (await hvsc_download.refresh())["tunes"] == 3
    assert hvsc_download.state()["failure"] is None


@pytest.mark.asyncio
async def test_offline_mode_fetches_nothing(online, monkeypatch):
    def refuse(target=None):
        raise netpolicy.Offline("offline")
    monkeypatch.setattr(netpolicy, "check", refuse)
    res = await hvsc_download.refresh()
    assert "Offline" in res["error"] and _doc_gets("Songlengths.md5") == []
    assert hvsc_download.state().get("failure") is None   # no failure counted


def test_a_scan_does_not_switch_an_online_choice_to_a_found_folder(online, monkeypatch, tmp_path):
    from soniqboom.config import settings
    from soniqboom.core import hvsc
    monkeypatch.setattr(settings, "hvsc_docs_path", "")
    assert hvsc.auto_configure(str(tmp_path / "DOCUMENTS")) is False
    assert settings.hvsc_docs_path == ""


@pytest.mark.asyncio
async def test_start_arms_the_schedule_only_when_wanted(online, monkeypatch):
    from soniqboom.config import settings
    hvsc_download.start()
    t = hvsc_download._task
    assert t is not None and not t.done()
    for _ in range(100):                                  # the first check runs at once
        if hvsc_download.state().get("checked_at"):
            break
        await __import__("asyncio").sleep(0.05)
    assert hvsc_download.state().get("checked_at")
    monkeypatch.setattr(settings, "hvsc_source", "local")
    hvsc_download.start()                                 # wakes the loop: no longer wanted
    for _ in range(100):
        if t.done():
            break
        await __import__("asyncio").sleep(0.05)
    assert t.done()
    assert hvsc_download.status()["next_at"] is None
    assert time.time() > 0


@pytest.fixture
def no_floor(monkeypatch):
    monkeypatch.setattr(hvsc_download, "_last_try", 0.0)


@pytest.mark.asyncio
async def test_unchanged_documents_are_applied_to_the_library_once(online, monkeypatch, no_floor):
    calls = []
    monkeypatch.setattr(online_hvsc, "_apply_to_library", lambda: calls.append(1))
    monkeypatch.setattr(online_hvsc, "_applied_sig", None)
    await hvsc_download.refresh()
    assert len(calls) == 1
    online_hvsc.apply_docs_fallback()
    hvsc_download.start()                     # e.g. a Save & reload with the same choice
    assert len(calls) == 1
    (online["cat"].dir() / "DOCUMENTS" / "STIL.txt").write_text("changed", "latin-1")
    online_hvsc.apply_docs_fallback()         # new documents: applied again
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_switching_the_collection_off_downloads_nothing_at_once(online, no_floor):
    await _enable(online)                     # the collection downloads the documents
    _Handler.log.clear()
    await online["cat"].disable()
    st = hvsc_download.state()
    assert st.get("checked_at") and hvsc_download.next_due() > time.time() + 6 * 24 * 3600
    assert _doc_gets("Songlengths.md5") == []


@pytest.mark.asyncio
async def test_a_scheduled_try_waits_an_hour_whatever_happened(online, monkeypatch, no_floor):
    def refuse(target=None):
        raise netpolicy.Offline("offline")
    monkeypatch.setattr(netpolicy, "check", refuse)
    await hvsc_download.refresh()             # scheduled: offline, no failure recorded
    assert hvsc_download.next_due() >= time.time() + 3500


@pytest.mark.asyncio
async def test_check_now_waits_for_a_retry_after(online, no_floor):
    hvsc_download._save({"failure": {"at": time.time(), "streak": 1, "refused": True,
                                     "retry_at": time.time() + 3600}})
    res = await hvsc_download.refresh(manual=True)
    assert "asked to wait" in res["error"] and _doc_gets("Songlengths.md5") == []


def test_leftover_download_folders_are_removed(online):
    left = online["cat"].dir() / "docs-download.dead"
    left.mkdir(parents=True)
    (left / "Songlengths.md5").write_text("x")
    hvsc_download._drop_leftovers()
    assert not left.exists()
