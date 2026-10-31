# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""/extended names the renderer that plays a file (``renderer``) — Track
Info's Retro VFD strip shows it.  It is ``stream.renderer_key_for`` on the
stored row (the key ``PUT /health`` records too), else on the model."""
from __future__ import annotations

import types

from soniqboom.api import tracks as tracks_api


class _Store:
    def __init__(self, recs):
        self.recs = recs

    def get_track(self, tid):
        return self.recs.get(tid)


def _patch_store(monkeypatch, recs):
    from soniqboom.core import store as store_mod
    st = _Store(recs)
    monkeypatch.setattr(store_mod, "get_store", lambda: st)
    return st


def test_the_stored_row_names_the_renderer(monkeypatch):
    _patch_store(monkeypatch, {
        "s": {"id": "s", "path": "/m/HVSC/DEMOS/x.sid", "format": "SID"},
        "m": {"id": "m", "path": "/m/mods/x.mod", "format": "ProTracker"},
        "n": {"id": "n", "path": "/m/sndh/x.sndh", "format": "SNDH"},
        "f": {"id": "f", "path": "/m/a/x.flac", "format": "FLAC"},
    })
    model = types.SimpleNamespace(model_dump=lambda: {})
    assert tracks_api._renderer_key("s", model) == "sidplayfp"
    assert tracks_api._renderer_key("m", model) == "openmpt123"
    assert tracks_api._renderer_key("n", model) == "psgplay"
    # a file the browser plays itself (or through ffmpeg): none
    assert tracks_api._renderer_key("f", model) is None


def test_without_a_stored_row_the_model_is_asked(monkeypatch):
    _patch_store(monkeypatch, {})
    model = types.SimpleNamespace(model_dump=lambda: {"path": "/m/x.ahx", "format": "AHX"})
    assert tracks_api._renderer_key("gone", model) == "uade123"


def test_a_failure_is_no_renderer(monkeypatch):
    _patch_store(monkeypatch, {})

    def boom():
        raise RuntimeError("no")
    assert tracks_api._renderer_key("gone", types.SimpleNamespace(model_dump=boom)) is None
