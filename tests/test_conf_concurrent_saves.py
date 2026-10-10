# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SoniqBoom.conf saves made at the same time keep each other's changes.

Every settings writer loads the conf, changes it and saves it.  A save used
to write the whole conf the caller had loaded, so a save landing in between
(another request, the CLI) was silently undone.  ``save_local_conf`` now
writes only what the caller changed onto the conf as it is when saving.
"""
from __future__ import annotations

import json
import threading

from soniqboom import config


def _conf(tmp_path):
    p = tmp_path / "SoniqBoom.conf"
    p.write_text(json.dumps({"_comment": "kept", "folder_aliases": {"/a": "A"},
                             "network_shares": {"s0": {"host": "h0"}}}))
    return p


def _disk(p):
    return json.loads(p.read_text())


def test_interleaved_saves_keep_both_changes(tmp_path):
    p = _conf(tmp_path)
    a = config.load_local_conf(p)
    b = config.load_local_conf(p)
    a["scan_zips"] = False
    config.save_local_conf(a, p)
    b["radio_art_lookup"] = False                    # b loaded before a's save
    config.save_local_conf(b, p)
    d = _disk(p)
    assert d["scan_zips"] is False and d["radio_art_lookup"] is False
    assert d["_comment"] == "kept"


def test_nested_sections_merge_key_by_key(tmp_path):
    p = _conf(tmp_path)
    a = config.load_local_conf(p)
    b = config.load_local_conf(p)
    a["network_shares"]["s1"] = {"host": "h1"}
    a["folder_aliases"].pop("/a")
    config.save_local_conf(a, p)
    b["network_shares"]["s2"] = {"host": "h2"}
    b["folder_aliases"]["/b"] = "B"
    config.save_local_conf(b, p)
    d = _disk(p)
    assert set(d["network_shares"]) == {"s0", "s1", "s2"}
    assert d["folder_aliases"] == {"/b": "B"}         # a's removal kept, b's addition too


def test_the_later_save_wins_on_the_same_key(tmp_path):
    p = _conf(tmp_path)
    a = config.load_local_conf(p)
    b = config.load_local_conf(p)
    a["merger_interval"] = 30
    config.save_local_conf(a, p)
    b["merger_interval"] = 60
    config.save_local_conf(b, p)
    assert _disk(p)["merger_interval"] == 60


def test_a_second_save_of_the_same_conf_adds_only_what_is_new(tmp_path):
    p = _conf(tmp_path)
    a = config.load_local_conf(p)
    a["scan_zips"] = False
    config.save_local_conf(a, p)
    other = config.load_local_conf(p)
    other["scan_zips"] = True                        # someone else turns it back on
    config.save_local_conf(other, p)
    a["radio_art_lookup"] = False
    config.save_local_conf(a, p)                     # must not re-apply its old scan_zips
    d = _disk(p)
    assert d["scan_zips"] is True and d["radio_art_lookup"] is False


def test_a_plain_dict_is_written_whole(tmp_path):
    p = _conf(tmp_path)
    config.save_local_conf({"scan_zips": False}, p)
    d = _disk(p)
    assert d == {"_comment": "kept", "scan_zips": False}


def test_many_threads_saving_at_once_lose_nothing(tmp_path):
    p = _conf(tmp_path)
    errors = []

    def writer(n):
        try:
            for i in range(10):
                c = config.load_local_conf(p)
                c.setdefault("folder_aliases", {})[f"/t{n}/{i}"] = f"{n}-{i}"
                config.save_local_conf(c, p)
        except Exception as exc:                     # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    aliases = _disk(p)["folder_aliases"]
    assert {f"/t{n}/{i}" for n in range(6) for i in range(10)} <= set(aliases)
    assert "/a" in aliases


def test_the_lock_file_stays(tmp_path):
    p = _conf(tmp_path)
    c = config.load_local_conf(p)
    c["scan_zips"] = False
    config.save_local_conf(c, p)
    assert (tmp_path / "SoniqBoom.conf.lock").exists()


def test_a_second_save_never_undoes_a_change_made_before_the_first(tmp_path):
    p = _conf(tmp_path)
    a = config.load_local_conf(p)
    other = config.load_local_conf(p)
    other["radio_art_lookup"] = False
    config.save_local_conf(other, p)                 # between a's load and a's first save
    a["scan_zips"] = False
    config.save_local_conf(a, p)
    a["folder_aliases"]["/b"] = "B"
    config.save_local_conf(a, p)
    d = _disk(p)
    assert d["radio_art_lookup"] is False and d["scan_zips"] is False
    assert d["folder_aliases"] == {"/a": "A", "/b": "B"}


def test_a_section_neither_side_loaded_merges_key_by_key(tmp_path):
    p = _conf(tmp_path)                              # no ftp_pools in defaults or file
    a = config.load_local_conf(p)
    b = config.load_local_conf(p)
    a.setdefault("ftp_pools", {})["ha:21"] = {"stream": 2}
    config.save_local_conf(a, p)
    b.setdefault("ftp_pools", {})["hb:21"] = {"stream": 3}
    config.save_local_conf(b, p)
    assert set(_disk(p)["ftp_pools"]) == {"ha:21", "hb:21"}


def test_an_unreadable_conf_is_not_overwritten_with_defaults(tmp_path):
    import pytest
    p = _conf(tmp_path)
    a = config.load_local_conf(p)
    broken = p.read_text().rstrip("}") + ",}"        # a hand edit left a trailing comma
    p.write_text(broken)
    a["scan_zips"] = False
    with pytest.raises(config.ConfUnreadable):
        config.save_local_conf(a, p)
    with pytest.raises(config.ConfUnreadable):
        config.update_local_conf(lambda c: c.update(scan_zips=False), path=p)
    assert p.read_text() == broken                   # shares / aliases still there


def test_a_save_reads_the_file_afresh_not_a_stale_cached_copy(tmp_path):
    import os
    p = tmp_path / "SoniqBoom.conf"
    p.write_text(json.dumps({"merger_interval": 120, "folder_aliases": {}}))
    a = config.load_local_conf(p)                    # caches this version
    st = p.stat()
    # Another process: the same size, within the same mtime tick.
    p.write_text(json.dumps({"merger_interval": 300, "folder_aliases": {}}))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert p.stat().st_size == st.st_size and p.stat().st_mtime_ns == st.st_mtime_ns
    a["scan_zips"] = False
    config.save_local_conf(a, p)
    assert _disk(p)["merger_interval"] == 300 and _disk(p)["scan_zips"] is False


def test_save_returns_what_it_wrote(tmp_path):
    p = _conf(tmp_path)
    a = config.load_local_conf(p)
    b = config.load_local_conf(p)
    b["folder_aliases"]["/b"] = "B"
    config.save_local_conf(b, p)
    a["folder_aliases"]["/c"] = "C"
    written = config.save_local_conf(a, p)
    assert written["folder_aliases"] == {"/a": "A", "/b": "B", "/c": "C"}
