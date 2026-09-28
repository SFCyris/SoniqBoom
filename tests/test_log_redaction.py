# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Access-log lines never carry Subsonic / stream credentials."""
import logging

from soniqboom.core import log_control


def _line(path):
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                             '%s - "%s %s HTTP/%s" %d',
                             ("10.0.0.2:5", "GET", path, "1.1", 404), None)


def test_secret_params_are_masked_in_every_mode():
    for mode in ("all", "problems"):
        log_control.apply_access_log_mode(mode)
        rec = _line("/rest/stream.view?id=9&u=bob&p=enc:6162&t=abc&s=xy&apiKey=K&c=DSub")
        log_control._access_filter.filter(rec)
        msg = rec.getMessage()
        for secret in ("enc:6162", "t=abc", "s=xy", "apiKey=K"):
            assert secret not in msg
        assert "u=bob" in msg and "c=DSub" in msg and "id=9" in msg
    log_control.apply_access_log_mode(log_control.DEFAULT_ACCESS_MODE)


def test_signed_tokens_are_masked_and_plain_paths_untouched():
    assert log_control.redact_query("/rest/radioStream.view?id=x&token=A.B") == \
        "/rest/radioStream.view?id=x&token=***"
    assert log_control.redact_query("/api/tracks?limit=5&sort=title") == "/api/tracks?limit=5&sort=title"
    assert log_control.redact_query("/api/art/abc") == "/api/art/abc"


def test_aof_replay_of_a_backdated_play_keeps_the_newer_last_played():
    from soniqboom.core import merger
    state = {"play_stats": {"t1": {"count": 3, "last_played": 2_000}}}
    apply = getattr(merger, "_apply_entry", None) or getattr(merger, "apply_entry", None)
    assert apply is not None
    apply(state, {"op": "record_play", "id": "t1", "ts": 1_000})
    assert state["play_stats"]["t1"] == {"count": 4, "last_played": 2_000}


def test_percent_encoded_names_and_cast_path_tokens_are_masked():
    assert log_control.redact_query("/rest/ping.view?u=a&%70=secret") == "/rest/ping.view?u=a&%70=***"
    assert log_control.redact_query("/cast/eyJhbGciOi.sig/track.mp3") == "/cast/***/track.mp3"
    assert log_control.redact_query("/cast/tok123/x.flac?foo=1") == "/cast/***/x.flac?foo=1"


def test_back_dated_history_lands_in_play_order_live_and_on_replay():
    from soniqboom.core import merger
    from soniqboom.core.store import TrackStore
    s = TrackStore()
    for tid, ts in (("a", 100), ("b", 300), ("c", 200)):
        s.push_history({"track_id": tid, "ts": ts})
    assert [e["track_id"] for e in s.get_history(10)] == ["b", "c", "a"]
    state = {}
    for tid, ts in (("a", 100), ("b", 300), ("c", 200)):
        merger._apply_entry(state, {"op": "push_history", "data": {"track_id": tid, "ts": ts}})
    assert [e["track_id"] for e in state["history"]] == ["a", "c", "b"]
