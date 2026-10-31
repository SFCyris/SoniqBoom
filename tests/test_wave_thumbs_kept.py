# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The playlist rows' waveform thumbnails (``POST /tracks/meta/waveforms``)
are kept once made — and made again when the track's waveform changes."""
from __future__ import annotations

from soniqboom.api import tracks as T
from soniqboom.core.store import TrackStore


async def test_thumbs_are_kept_until_the_waveform_changes(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    T._THUMBS.clear()
    s.store_waveform("a", [0.1, 0.5, 1.0, 0.2] * 50)
    s.store_waveform("b", [0.3] * 200)
    made = []
    real = T._wave_thumb
    monkeypatch.setattr(T, "_wave_thumb", lambda w, n: made.append(n) or real(w, n))
    body = {"ids": ["a", "b", "nope"], "points": 16}
    first = await T.batch_waveforms(body)
    assert set(first["waveforms"]) == {"a", "b"} and len(made) == 2
    again = await T.batch_waveforms(body)
    assert again == first and len(made) == 2                  # kept
    assert (await T.batch_waveforms({"ids": ["a"], "points": 24}))["points"] == 24
    assert len(made) == 3                                     # another size: its own
    s.store_waveform("a", [1.0] * 100 + [0.0] * 100)          # the file changed
    third = await T.batch_waveforms(body)
    assert len(made) == 4 and third["waveforms"]["a"] != first["waveforms"]["a"]
    assert third["waveforms"]["b"] == first["waveforms"]["b"]
    s.forget_waveforms(["b"])
    assert "b" not in (await T.batch_waveforms(body))["waveforms"]
