# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""zxtune123 sometimes renders nothing — for an old MED module it exits 0 with
a finished WAV of no frames (``data`` of length 0, then a ``LIST`` chunk) in
about half its runs.  Such a file holds no audio (``stream._render_has_audio``),
and the capped zxtune render runs zxtune again until it renders
(``stream._ZXTUNE_TRIES``) instead of serving silence."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from soniqboom.api import stream

_MED = (Path(__file__).resolve().parents[1]
        / "internal/format-samples/Music Editor/Alex Van Starrex/synth.med")


def _riff(*chunks: bytes) -> bytes:
    body = b"WAVE" + b"".join(chunks)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _chunk(cid: bytes, data: bytes) -> bytes:
    return cid + len(data).to_bytes(4, "little") + data + (b"\0" if len(data) & 1 else b"")


_FMT = _chunk(b"fmt ", (1).to_bytes(2, "little") + (2).to_bytes(2, "little")
              + (44100).to_bytes(4, "little") + (176400).to_bytes(4, "little")
              + (4).to_bytes(2, "little") + (16).to_bytes(2, "little"))
_LIST = _chunk(b"LIST", b"INFOICMT" + (29).to_bytes(4, "little")
               + b"Created using ZXTune toolkit\0")
# What zxtune123 leaves when it rendered nothing (94 bytes, byte for byte).
ZX_EMPTY = _riff(_FMT, _chunk(b"data", b""), _LIST)


def test_a_finished_wav_with_an_empty_data_chunk_holds_no_audio(tmp_path):
    assert len(ZX_EMPTY) == 94
    frames = b"\x01\x00" * 400
    streaming = bytearray(_riff(_FMT, _chunk(b"data", b"")) + frames)
    streaming[4:8] = (36).to_bytes(4, "little")       # a header not finished yet …
    unknown = bytearray(_riff(_FMT, _chunk(b"data", b"")) + frames)
    unknown[4:8] = unknown[40:44] = b"\xff\xff\xff\xff"   # … or a streaming writer's
    odd = _riff(_FMT, _chunk(b"data", b""), _chunk(b"LIST", b"abc"))
    unpadded = odd[:4] + (len(odd) - 9).to_bytes(4, "little") + odd[8:]   # (RIFF size w/o the pad)
    cases = {
        "zxtune_empty.wav": (ZX_EMPTY, False),
        "empty_then_list_pad.wav": (_riff(_FMT, _chunk(b"data", b""), _chunk(b"LIST", b"abc")), False),
        "audio_then_list.wav": (_riff(_FMT, _chunk(b"data", frames), _LIST), True),
        "still_being_written.wav": (bytes(streaming), True),
        "placeholder_sizes.wav": (bytes(unknown), True),
        "header_only.wav": (_riff(_FMT, _chunk(b"data", b"")), False),
        "size_without_pad_byte.wav": (unpadded, False),
    }
    for name, (data, has) in cases.items():
        (tmp_path / name).write_bytes(data)
        assert stream._render_has_audio(tmp_path / name) is has, name


def _fake_zxtune(tmp_path: Path, empty_runs: int, *, exit_code: int = 0) -> tuple[Path, Path]:
    """A stand-in zxtune123 (``--silent --wav filename=<out> <file>``): its
    first ``empty_runs`` runs write zxtune's empty WAV, later ones a short
    finished one with audio; each run is counted in the returned file."""
    calls = tmp_path / "calls"
    calls.write_text("0")
    script = tmp_path / "fake_zxtune.py"
    audio = _riff(_FMT, _chunk(b"data", b"\x01\x00" * 4410 * 2), _LIST)
    script.write_text(
        "import sys\n"
        f"calls = {str(calls)!r}\n"
        "n = int(open(calls).read()) + 1\n"
        "open(calls, 'w').write(str(n))\n"
        "out = [a for a in sys.argv if a.startswith('filename=')][0][len('filename='):]\n"
        f"data = {ZX_EMPTY!r} if n <= {empty_runs} else {audio!r}\n"
        "open(out, 'xb').write(data)\n"                 # (zxtune refuses an existing file)
        f"sys.exit({exit_code})\n")
    launcher = tmp_path / "zxtune123"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    launcher.chmod(0o755)
    return launcher, calls


@pytest.mark.asyncio
async def test_a_zxtune_run_that_renders_nothing_is_run_again(tmp_path, monkeypatch):
    launcher, calls = _fake_zxtune(tmp_path, empty_runs=3)
    monkeypatch.setattr(stream, "_find_renderer", lambda *_: str(launcher))
    monkeypatch.setattr(stream, "_ZXTUNE_RETRY_S", 60.0)
    module = tmp_path / "MED.Synth.med"
    module.write_bytes(b"MED\x04" + bytes(64))
    out = await stream._render_zxtune_capped(module, kind="MED")
    try:
        assert stream._render_has_audio(out) and calls.read_text() == "4"
    finally:
        out.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_a_module_zxtune_never_renders_is_no_audio_after_the_tries(tmp_path, monkeypatch):
    launcher, calls = _fake_zxtune(tmp_path, empty_runs=10_000)
    monkeypatch.setattr(stream, "_find_renderer", lambda *_: str(launcher))
    monkeypatch.setattr(stream, "_ZXTUNE_TRIES", 5)
    monkeypatch.setattr(stream, "_ZXTUNE_RETRY_S", 60.0)
    module = tmp_path / "MED.Nothing.med"
    module.write_bytes(b"MED\x04" + bytes(64))
    before = set(Path(stream._rtmp()).glob("*.wav"))
    with pytest.raises(HTTPException) as exc:
        await stream._render_zxtune_capped(module, kind="MED")
    assert exc.value.status_code == 422
    assert calls.read_text() == "5"
    assert set(Path(stream._rtmp()).glob("*.wav")) <= before      # (no temp WAV left)


@pytest.mark.asyncio
async def test_no_run_starts_once_the_retry_time_is_up(tmp_path, monkeypatch):
    launcher, calls = _fake_zxtune(tmp_path, empty_runs=10_000)
    monkeypatch.setattr(stream, "_find_renderer", lambda *_: str(launcher))
    monkeypatch.setattr(stream, "_ZXTUNE_RETRY_S", 0.0)
    module = tmp_path / "MED.Nothing.med"
    module.write_bytes(b"MED\x04" + bytes(64))
    with pytest.raises(HTTPException) as exc:
        await stream._render_zxtune_capped(module, kind="MED")
    assert exc.value.status_code == 422 and calls.read_text() == "1"


@pytest.mark.asyncio
async def test_waiting_for_a_render_slot_doesnt_use_up_the_retries(tmp_path, monkeypatch):
    """Every render slot busy for longer than the retry time: the render
    waits for one, then keeps it for its tries."""
    launcher, calls = _fake_zxtune(tmp_path, empty_runs=1)
    monkeypatch.setattr(stream, "_find_renderer", lambda *_: str(launcher))
    monkeypatch.setattr(stream, "_ZXTUNE_RETRY_S", 0.5)
    module = tmp_path / "MED.Busy.med"
    module.write_bytes(b"MED\x04" + bytes(64))

    async def busy():
        async with stream._render_sem:
            await asyncio.sleep(1.5)
    holders = [asyncio.create_task(busy()) for _ in range(stream._RENDER_SLOTS)]
    await asyncio.sleep(0.1)
    try:
        out = await stream._render_zxtune_capped(module, kind="MED")
        try:
            assert stream._render_has_audio(out) and calls.read_text() == "2"
        finally:
            out.unlink(missing_ok=True)
    finally:
        await asyncio.gather(*holders)


@pytest.mark.asyncio
async def test_a_zxtune_run_that_fails_is_not_run_again(tmp_path, monkeypatch):
    launcher, calls = _fake_zxtune(tmp_path, empty_runs=10_000, exit_code=1)
    monkeypatch.setattr(stream, "_find_renderer", lambda *_: str(launcher))
    module = tmp_path / "MED.Broken.med"
    module.write_bytes(b"MED\x04" + bytes(64))
    with pytest.raises(HTTPException) as exc:
        await stream._render_zxtune_capped(module, kind="MED")
    assert exc.value.status_code in (422, 502)
    assert calls.read_text() == "1"


@pytest.mark.asyncio
@pytest.mark.skipif(not (stream._find_renderer(stream.settings.zxtune123_path, "zxtune123")
                         and _MED.exists()),
                    reason="zxtune123 or the MED 4 sample (internal/, not in a clone) is missing")
async def test_an_old_med_module_renders_every_time(tmp_path):
    """The real module zxtune123 renders empty about half the time: every
    render has its capped length."""
    src = tmp_path / "synth.med"
    src.write_bytes(_MED.read_bytes())
    cap = max(5, min(int(stream.settings.sid_default_duration), 3600)) * 44100 * 4
    for _ in range(3):
        out = await stream._render_tracker(src)
        try:
            assert out.stat().st_size == stream._WAV_HEADER_LEN + cap
            assert stream._render_has_audio(out)
        finally:
            out.unlink(missing_ok=True)
