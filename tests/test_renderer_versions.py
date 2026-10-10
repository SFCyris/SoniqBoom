# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings → Renderers shows the version each renderer reports.

``/api/admin/renderers`` already runs every plain-binary renderer with
``--version`` to see that it loads; the version it prints is kept
(``version`` + ``version_detail``), and the run is remembered until the
binary changes.  libgme's version is its loaded library's file name.
"""
from __future__ import annotations

import os
import re
import stat

import pytest

from soniqboom.api import admin
from soniqboom.core import gme_render


@pytest.mark.parametrize("name,text,version", [
    ("zxtune123", "zxtune123 r5112M Oct  9 2026 darwin-aarch64 \n", "r5112M"),
    ("sidplayfp", "Warning, non UTF-8 encoding.\nsidplayfp 3.1.0\nUsing libsidplayfp 3.1.0\n", "3.1.0"),
    ("fluidsynth", "FluidSynth runtime version 2.6.1\nCopyright (C) 2000-2026\n", "2.6.1"),
    ("openmpt123", "openmpt123 v0.8.9, libopenmpt 0.8.9+r25652.pkg (OpenMPT 1.32.11.02)\n", "0.8.9"),
    ("uade123", "uade123 3.05\n", "3.05"),
    ("adplay", "AdPlay/UNIX 1.9\n", "1.9"),
    ("psgplay", "psgplay version 1.0.1-11-g6f1c39f\n", "1.0.1-11-g6f1c39f"),
    ("sc68", "sc68 version 2.2.1\n\n", "2.2.1"),
    ("ffmpeg", "ffmpeg version 9.0.2 Copyright (c) 2000-2026 the FFmpeg developers\n", "9.0.2"),
    ("ffmpeg", "ffmpeg version N-118000-gabc123-static https://johnvansickle.com\n",
     "N-118000-gabc123-static"),
    ("other", "some tool 2.4.1-rc1 (built today)\n", "2.4.1-rc1"),
    ("sc68", "usage: sc68 [options] <file>\n", ""),
    ("sc68", "Usage: sc68 [options]\n -t, --track=#  default track 1.0\n", ""),
    ("other", "Usage: tool [opts]\n  --rate=44100.0  output rate\n", ""),
    ("uade123", "", ""),
])
def test_the_version_each_renderer_states(name, text, version):
    v, detail = admin.renderer_version(name, text)
    assert v == version
    assert (version in detail) if version else detail == ""


def test_the_detail_names_the_library_line_too():
    _, detail = admin.renderer_version(
        "sidplayfp", "sidplayfp 3.1.0\nUsing libsidplayfp 3.1.0\nHome Page: https://x/\n")
    assert detail == "sidplayfp 3.1.0\nUsing libsidplayfp 3.1.0"


def _fake_renderer(path, line, count_file):
    path.write_text(f"#!/bin/sh\necho x >> '{count_file}'\necho '{line}'\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.mark.asyncio
async def test_a_renderer_that_stops_loading_is_seen_on_the_next_check(tmp_path, monkeypatch):
    # A library upgraded under an unchanged binary: the panel must not keep
    # what an earlier check saw.
    flag = tmp_path / "broken"
    exe = tmp_path / "zxtune123"
    exe.write_text(f"#!/bin/sh\nif [ -e '{flag}' ]; then echo 'dyld[1]: Symbol not found: _x' >&2; "
                   f"exit 1; fi\necho 'zxtune123 r5112M'\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(admin.settings, "zxtune123_path", str(exe))
    out = await admin.check_renderers(_tok="x")
    assert out["zxtune123"]["runs"] is True and out["zxtune123"]["version"] == "r5112M"
    flag.write_text("")
    out = await admin.check_renderers(_tok="x")
    assert out["zxtune123"]["runs"] is False and "version" not in out["zxtune123"]


@pytest.mark.asyncio
async def test_the_renderers_panel_reports_versions(tmp_path, monkeypatch):
    count = tmp_path / "runs"
    zx = _fake_renderer(tmp_path / "zxtune123", "zxtune123 r5112M Oct  9 2026", count)
    sc = _fake_renderer(tmp_path / "sc68", "usage: sc68 [options]", count)
    monkeypatch.setattr(admin.settings, "zxtune123_path", str(zx))
    monkeypatch.setattr(admin.settings, "sc68_path", str(sc))
    monkeypatch.setattr(admin.settings, "adplay_path", str(tmp_path / "missing-adplay"))
    out = await admin.check_renderers(_tok="x")
    assert out["zxtune123"]["runs"] is True
    assert out["zxtune123"]["version"] == "r5112M"
    assert out["zxtune123"]["version_detail"].startswith("zxtune123 r5112M")
    assert out["sc68"]["runs"] is True and "version" not in out["sc68"]
    assert out["adplay"]["installed"] is False and "version" not in out["adplay"]
    if out["gme"]["installed"] and gme_render.lib_version():
        assert out["gme"]["version"] == gme_render.lib_version()
    for key in ("ym2wav", "hvl2wav"):                 # built, or built on first use
        assert out[key].get("version") == ("bundled" if out[key]["installed"] else None)


def test_libgme_version_is_its_library_file_name():
    if not gme_render.is_available():
        pytest.skip("libgme missing")
    path = gme_render.lib_path()
    assert os.path.isfile(path) and "libgme" in os.path.basename(path)
    v = gme_render.lib_version()
    if v:                                  # a distribution may ship an unversioned name
        assert re.fullmatch(r"\d+\.\d+\.\d+", v) and v in os.path.basename(path)
