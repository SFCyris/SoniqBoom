# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A shuffled queue across engines plays at one level (feature 2's acceptance
test): real renders of a SID (sidplayfp), a tracker module (libopenmpt), an
SPC (libgme), an Amiga Future Composer module (uade), an Atari SNDH
(psgplay) and a GoatTracker song (gt2reloc + sidplayfp), plus a FLAC file
(no ReplayGain tags), are measured as the server measures them
(``core/loudness``), levelled by the gain the web player applies
(``utils.js levelingGain`` — run in node on the very entries) and measured
again: the spread of integrated loudness shrinks from what the engines give
to within the true-peak caps.  Engines that aren't installed are left out;
at least four are required.  Prints the before / after table (``-s``)."""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from soniqboom.api import stream
from soniqboom.core import loudness as L

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "internal/testdata"
FE_UTILS = REPO / "soniqboom/frontend/js/utils.js"
NODE, FFMPEG = shutil.which("node"), shutil.which("ffmpeg")

pytestmark = pytest.mark.skipif(not (NODE and FFMPEG), reason="node / ffmpeg absent")


async def _renders(tmp: Path) -> "dict[str, Path]":
    from soniqboom.config import settings
    out: dict[str, Path] = {}

    async def _try(name, coro_fn, needs):
        if not all(n.exists() if isinstance(n, Path) else shutil.which(n) for n in needs):
            return
        try:
            out[name] = Path(await coro_fn())
        except Exception as exc:                        # noqa: BLE001 — engine missing here
            print(f"{name}: not rendered ({exc})")

    sid = DATA / "sid/SX-64_Demo.sid"
    await _try("SID (sidplayfp)", lambda: stream._render_sid(sid, 0, 30), [sid, "sidplayfp"])
    mod = DATA / "tracker/actionplatform.mod"
    await _try("MOD (libopenmpt)", lambda: stream._render_tracker(mod), [mod, "openmpt123"])
    spc = DATA / "game/mmgg-02.spc"
    await _try("SPC (libgme)", lambda: stream._render_gme(spc, 0), [spc])
    fc = DATA / "uade/Future Composer 1.4/legendcrack.fc"
    await _try("FC 1.4 (uade)", lambda: stream._render_uade(fc), [fc, "uade123"])
    sndh = DATA / "atari/sndh/madmax1.sndh"
    await _try("SNDH (psgplay)", lambda: stream._render_sndh(sndh, 0), [sndh, "psgplay"])
    goat = DATA / "goattracker/sanction.sng"
    settings.gt2reloc_path = shutil.which("gt2reloc") or ""
    await _try("GoatTracker", lambda: stream._render_goattracker(goat),
               [goat, "gt2reloc", "sidplayfp"])
    flac = DATA / "game/mainstream/goldberg05.flac"
    if flac.exists():
        out["FLAC (untagged)"] = flac
    return out


def _gains(entries: "dict[str, list]") -> "dict[str, float]":
    """The web player's gain for each entry (``levelingGain``, Per track)."""
    js = ("const u = await import(%s);\n"
          "const e = JSON.parse(process.argv[1]);\n"
          "const out = {};\n"
          "for (const [k, v] of Object.entries(e)) out[k] = "
          "u.levelingGain({ id: k, format: 'X', loudness: { 0: v } }, 'track');\n"
          "console.log(JSON.stringify(out));\n") % json.dumps(str(FE_UTILS))
    r = subprocess.run([NODE, "--input-type=module", "-e", js, json.dumps(entries)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def _apply(src: Path, gain: float, dst: Path) -> Path:
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                    "-af", f"volume={gain:.6f}", "-c:a", "pcm_f32le", str(dst)], check=True)
    return dst


@pytest.mark.asyncio
async def test_a_mixed_queue_plays_at_one_level(tmp_path, monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 30)
    renders = await _renders(tmp_path)
    try:
        assert len(renders) >= 4, f"only {sorted(renders)} could be rendered"
        before = {k: L.measure(p) for k, p in renders.items()}
        assert all(before.values()), before
        entries = {k: L.entry_of(m) for k, m in before.items()}
        gains = _gains(entries)
        after = {k: L.measure(_apply(p, gains[k], tmp_path / f"{i}.wav"))
                 for i, (k, p) in enumerate(renders.items())}
        rows = []
        for k in renders:
            capped = gains[k] < 10 ** ((L.REF_LUFS - before[k]["i"]) / 20) - 1e-6
            rows.append((k, before[k]["i"], before[k]["tp"], 20 * math.log10(gains[k]),
                         after[k]["i"], after[k]["tp"], capped))
        print("\n%-18s %9s %7s %8s %9s %7s %s" % ("engine", "LUFS", "TP", "gain dB",
                                                   "LUFS ⇒", "TP ⇒", "TP-capped"))
        for r in rows:
            print("%-18s %9.1f %7.3f %+8.2f %9.1f %7.3f %s" % (*r[:6], "yes" if r[6] else ""))
        spread_before = max(m["i"] for m in before.values()) - min(m["i"] for m in before.values())
        free = [r for r in rows if not r[6]]
        spread_after = max(r[4] for r in free) - min(r[4] for r in free)
        print(f"spread before {spread_before:.1f} LU; after {spread_after:.1f} LU "
              f"over the {len(free)} tunes not held back by their true peak")
        # every tune not held back by its peak lands at the reference
        for r in free:
            assert abs(r[4] - L.REF_LUFS) < 0.6, r
        # a held-back one is as loud as its peak allows: peak ≈ 0.99, never above
        for r in rows:
            assert r[5] <= 1.0
            if r[6]:
                assert r[5] > 0.95 and r[4] < L.REF_LUFS
        assert spread_after < 1.2 < spread_before
    finally:
        for k, p in renders.items():
            if not k.startswith("FLAC"):
                Path(p).unlink(missing_ok=True)
                Path(p).with_suffix(".vu").unlink(missing_ok=True)
