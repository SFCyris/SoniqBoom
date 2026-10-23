# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The AOF's records are encoded by orjson (``aof.encode_record``) wherever
that reads back as what ``json.dumps(record, default=str)`` wrote — through
the reader (``persistence.loads_json``) and a plain ``json.loads`` alike —
and by ``json.dumps`` itself (byte for byte) for whatever orjson would encode
differently or refuses.  A non-finite float is ``null``.  Records written so
replay and merge to what records written the old way did."""
from __future__ import annotations

import dataclasses
import datetime
import json
import math

import pytest

from soniqboom.core import aof, merger, persistence

_WEIRD = ["😀🎵", "日本語", "é", "שלום", " x y\u0085z", "﻿bom",
          "".join(chr(c) for c in range(32)) + "\x7f", "q\"b\\s/", "\U0010ffff"]
_FINITE = [
    {"op": "upsert_track", "ts": 1792118490.25, "id": "t",
     "data": {"id": "t", "title": s, "genre": [s], "duration": f, "n": n}}
    for s, f, n in zip(_WEIRD, [5e-324, -0.0, 1e16, 1e22, 1.7976931348623157e308,
                                0.1 + 0.2, 1.5, 0.0, 123.456],
                       [0, 2 ** 63 - 1, 2 ** 64 - 1, -2 ** 63, 1, -1, 10 ** 18, 7, 3])
] + [
    {"op": "update_track_fields_batch", "ts": 1.0,
     "data": [{"id": f"t{i}", "data": {"album": _WEIRD[i % 9], "is_duplicate_primary": i % 2 == 0,
                                       "x": None, "l": [[], {}, [1, [2]]]}} for i in range(50)]},
    {"op": "set_config", "ts": 2.0, "key": "ключ😀", "value": {"a": [1, 2], "b": {"c": True}}},
]
@dataclasses.dataclass
class _DC:
    x: int = 1


_REFUSED = [
    # (orjson would write these its own way — RFC 3339 times, objects — where
    # the stdlib's ``default=str`` stringifies them)
    {"op": "x", "value": datetime.datetime(2026, 1, 2, 3, 4, 5)},
    {"op": "x", "value": datetime.date(2026, 1, 2)},
    {"op": "x", "value": _DC()},
    {"op": "upsert_track", "data": {"title": "half \ud800 surrogate"}},
    {"op": "set_config", "value": [2 ** 64, -(2 ** 63) - 1, 10 ** 30]},
    {"op": "set_config", "value": {1: "a", 2.5: "b", True: "c", None: "d"}},
    {"op": "x", "value": type("F", (float,), {})(1.5)},
    {"op": "x", "value": [{1, 2}, b"raw", frozenset()]},
]


def _same(a, b) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return list(a) == list(b) and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, float):
        return a == b and math.copysign(1, a) == math.copysign(1, b)
    return a == b


@pytest.mark.parametrize("rec", _FINITE)
def test_a_record_reads_back_as_the_stdlib_wrote_it(rec):
    old = json.dumps(rec, default=str)
    new = aof.encode_record(rec)
    want = json.loads(old)
    assert "\n" not in new                                    # (one line)
    for got in (persistence.loads_json(new.encode()), json.loads(new)):
        assert _same(want, got)


@pytest.mark.parametrize("rec", _REFUSED)
def test_what_orjson_would_change_is_written_by_the_stdlib(rec):
    assert aof.encode_record(rec) == json.dumps(rec, default=str)


def test_non_finite_floats_are_null():
    rec = {"op": "update_track_fields", "id": "t",
           "data": {"duration": float("nan"), "l": [float("inf"), -float("inf"), 1.5]}}
    assert json.loads(aof.encode_record(rec))["data"] == {"duration": None, "l": [None, None, 1.5]}


def test_a_falsified_encoder_is_caught():
    """(The comparison above is strict: a lost sign, an int read as a float.)"""
    assert not _same(json.loads('{"a": -0.0}'), json.loads('{"a": 0.0}'))
    assert not _same(json.loads('{"a": 1}'), json.loads('{"a": 1.0}'))


def test_records_appended_replay_and_merge_like_the_old_ones(tmp_path):
    base = {"db_version": persistence.DB_VERSION,
            "tracks": {"t1": {"id": "t1", "title": "x", "n": 1}}, "config": {},
            "scan_dirs": {}, "playlists": {}, "history": {}, "ratings": {}, "play_stats": {}}
    recs = [("batch_upsert_tracks", {"count": 2, "data": [
                {"id": "t2", "title": _WEIRD[0], "n": 2 ** 64 - 1, "f": -0.0},
                {"id": "t3", "title": "half \ud800", "n": 2 ** 70}]}),
            ("update_track_fields_batch", {"data": [{"id": "t1", "data": {"title": _WEIRD[4]}}]}),
            ("update_track_fields", {"id": "t2", "data": {"duration": 12.5}}),
            ("record_play", {"id": "t1", "ts": 1792118490, "uid": "u1"}),
            ("set_config", {"key": "k😀", "value": {"v": _WEIRD}}),
            ("delete_tracks", {"ids": ["t3"]})]
    out = {}
    for name, enc in (("new", aof.encode_record), ("old", lambda r: json.dumps(r, default=str))):
        d = tmp_path / name
        d.mkdir()
        (d / "library.json").write_text(json.dumps(base), encoding="utf-8")
        with open(d / "library.aof", "w", encoding="utf-8") as f:
            for i, (op, kw) in enumerate(recs):
                f.write(enc({"op": op, "ts": 100.0 + i, **kw}) + "\n")
        state = json.loads(json.dumps(base))
        assert persistence.replay_aof(state, d) == len(recs)
        assert merger._do_merge_locked(d) == len(recs)
        merged = persistence.loads_json((d / "library.json").read_bytes())
        merged.pop(persistence.AOF_MARK, None)
        out[name] = (state, merged)
    assert _same(out["new"][0], out["old"][0])                # replayed alike
    assert _same(out["new"][1], out["old"][1])                # merged alike
    assert _same(out["new"][0]["tracks"], out["new"][1]["tracks"])


def test_records_dumped_at_exit_survive_a_non_utf8_locale(tmp_path):
    """The emergency dump of records the AOF couldn't write keeps them under
    any locale: AOF lines are UTF-8 text now, not ASCII."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    code = f"""
import sys, locale
sys.path.insert(0, {str(root)!r})
from pathlib import Path
from soniqboom.core.aof import AOFWriter
print("ENC", locale.getpreferredencoding(False))
w = AOFWriter(Path({str(tmp_path)!r}) / "library.aof")
def fail(_data):
    raise OSError("disk full")
w._write_sync = fail
w.append("upsert_track", id="t", data={{"title": "日本語 😀"}})
w._atexit_flush()
"""
    env = {**os.environ, "LC_ALL": "en_US.ISO8859-1", "LANG": "en_US.ISO8859-1", "PYTHONUTF8": "0",
           "HOME": str(tmp_path), "SONIQBOOM_DATA_DIR": str(tmp_path),
           "SONIQBOOM_CONF": str(tmp_path / "SoniqBoom.conf")}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         timeout=120)
    enc = next((ln.split()[1] for ln in out.stdout.splitlines() if ln.startswith("ENC")), "")
    if "8859" not in enc.replace("_", "-").upper() and "LATIN" not in enc.upper():
        pytest.skip(f"no ISO-8859-1 locale here ({enc!r})")
    dumped = list(tmp_path.glob("library.aof.dropped-*"))
    assert dumped, out.stderr[-800:]
    lines = dumped[0].read_text(encoding="utf-8").splitlines()
    assert [json.loads(ln)["data"]["title"] for ln in lines] == ["日本語 😀"]
