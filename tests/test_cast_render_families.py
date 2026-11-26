# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cast (Beta) plays every family the web player renders (v2.2.0 review,
"also worth fixing"; KNOWN-ISSUES).

The cast byte server renders a retro source to WAV before ffmpeg transcodes
it for the receiver (``cast_render.prepare_source_for_stream``).  Its own
dispatch knew SID, MIDI, AHX, HVL, the openmpt trackers and libgme only: a
PSF / Dreamcast DSF rip, an SNDH / YM / SC68 tune, an AdLib module or a uade
format other than AHX reached ffmpeg raw, and the receiver got 0 bytes.
Those families now render through the web player's own dispatch
(``stream.chip_family`` / ``render_chip_family``) under the keys a web play
uses, and ``cast_render.rendered_cache_key`` names the same key.

Real renderers (zxtune123, psgplay, ym2wav, sc68, adplay, uade123) and ffmpeg
on fixtures from ``internal/testdata`` — a family whose binary or fixture is
absent is skipped.  In-process: bare cast router, throw-away store and cache.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soniqboom.core import cast_render, cast_tokens, conversion_cache, silence
from soniqboom.core import metadata
from soniqboom.core import store as store_mod
from soniqboom.core.data import path_hash
from soniqboom.core.store import TrackStore

REPO = Path(__file__).resolve().parent.parent
TD = REPO / "internal/testdata"

# id → (fixture, companions, family, binary)
CASES = {
    "psf": (TD / "psf/psf/GEOSEQ_000C.psf", ["VAB_GEOSEQ_1.psflib"], "psf", "zxtune123"),
    "dsf": (TD / "psf/dsf/jambo_002_0C_00_22.dsf", ["jambo_001_0D.dsflib",
                                                   "jambo_002_0C.dsflib"], "psf", None),
    "sndh": (TD / "atari/sndh/axelf.sndh", [], "sndh", "psgplay"),
    "ym": (TD / "atari/ym/gear1.ym", [], "ym", None),
    "sc68": (TD / "atari/sc68/gear.sc68", [], "sc68", "sc68"),
    "adlib": (TD / "adlib/Newtune.a2m", [], "adlib", "adplay"),
    "uade_fc": (TD / "uade/Future Composer 1.4/legendcrack.fc", [], "uade", "uade123"),
    "uade_prefix": (TD / "uade/mdat.acieed1", ["smpl.acieed1"], "uade", "uade123"),
    "sidmon": (TD / "uade/SidMon 1/primitive.sid", [], "uade", "uade123"),
}


def _skip_unless(case: str):
    src, _comp, _fam, binary = CASES[case]
    if not src.exists():
        pytest.skip(f"fixture {src.relative_to(REPO)} not present")
    if binary and not shutil.which(binary):
        pytest.skip(f"{binary} not installed")
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")


@pytest.fixture
def lib(monkeypatch, tmp_path):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "folder_aliases", {})
    s = TrackStore()
    s._aof = lambda *a, **k: None
    monkeypatch.setattr(store_mod, "_store", s)
    root = tmp_path / "lib"
    root.mkdir()
    s.upsert_scan_dir(str(root))

    def add(case: str) -> tuple[str, Path]:
        src, companions, _fam, _bin = CASES[case]
        d = root / case
        d.mkdir()
        dst = d / src.name
        shutil.copy2(src, dst)
        for c in companions:
            shutil.copy2(src.parent / c, d / c)
        meta = metadata.extract(dst, case).model_dump()
        meta.update(id=case, path=str(dst), scan_root_hash=path_hash(str(root)),
                    dir_hash=path_hash(str(d)))
        s.upsert_tracks_batch([meta])
        return case, dst
    return add


def _audio_bytes(wav: Path) -> int:
    from soniqboom.api.stream import _wav_audio_seconds
    assert wav.exists() and wav.stat().st_size > 44
    assert _wav_audio_seconds(wav) > 0.5
    assert silence.wav_audible(wav)
    return wav.stat().st_size


@pytest.mark.parametrize("case", list(CASES))
def test_cast_prepare_renders_every_family(lib, case):
    _skip_unless(case)
    tid, path = lib(case)
    from soniqboom.api import stream
    ident, named = stream._render_ident(str(path), store_mod._store.get_track(tid))
    assert cast_render.is_rendered_format(ident) or named or case == "dsf"
    assert asyncio.run(cast_render.is_rendered_source(str(path), store_mod._store.get_track(tid),
                                                      path))

    async def run():
        wav, codec = await cast_render.prepare_source_for_stream(
            track_id=tid, track_path=str(path), subsong=0)
        key = cast_render.rendered_cache_key(tid, path.suffix, 0)
        return wav, codec, key, await conversion_cache.get_cached(key)

    try:
        wav, codec, key, cached = asyncio.run(run())
    except Exception as exc:        # a replayer that can't be provided here (ym2wav build)
        if case == "ym" and "ym2wav" in str(exc).lower():
            pytest.skip(f"ym2wav unavailable: {exc}")
        raise
    assert codec == "wav" and wav != path
    assert _audio_bytes(wav) > 100_000
    # the family's cache subdir (``conversion_cache.FORMAT_TYPES``) …
    assert Path(wav).parent.parent.name == CASES[case][2], (key, wav)
    # … and ``rendered_cache_key`` names the very entry (cast_stream pins it)
    assert cached is not None and Path(cached) == Path(wav), (key, cached, wav)


@pytest.mark.parametrize("case", ["psf", "sndh", "adlib", "uade_fc", "uade_prefix"])
def test_cast_byte_server_sends_audio_for_the_family(lib, case, monkeypatch):
    """The symptom itself: the receiver's GET of the cast link (MP3 wanted)
    got 0 bytes for these families."""
    _skip_unless(case)
    tid, _path = lib(case)
    from soniqboom.api import cast_stream
    from soniqboom.core import users as users_mod
    monkeypatch.setattr(users_mod, "accounts_locked", lambda: False)
    app = FastAPI()
    app.include_router(cast_stream.router)
    tok = cast_tokens.issue_token(track_id=tid, codec="mp3")
    c = TestClient(app, client=("192.168.1.30", 40000))
    r = c.get(f"/cast/{tok}/song.mp3")
    assert r.status_code == 200, r.text[:200]
    assert r.headers["content-type"].startswith("audio/mpeg")
    body = r.content
    assert len(body) > 20_000, len(body)
    assert body[:3] == b"ID3" or body[0] == 0xFF          # an MP3 stream
