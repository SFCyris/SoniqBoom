#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capture SoniqBoom screenshots for the customer documentation.

Authenticates with the read-only ``test`` account via the context's shared
request jar (so the browser is logged in without driving the login form),
then captures the library, folder tree, now-playing visuals, the tracker
per-channel VU modal, the Library Galaxy, search, Settings, and the cast
picker.  Best-effort: each shot is wrapped so one failure can't abort the run.
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
IMG = Path(__file__).parent / "manual" / "img"
IMG.mkdir(parents=True, exist_ok=True)
W, H = 1600, 1000
USER, PW = "test", "soniqboom"


def shot(page, name, desc=""):
    p = IMG / f"{name}.png"
    try:
        page.screenshot(path=str(p))
        print(f"  [{name}] {desc}  ({p.stat().st_size // 1024} KB)")
    except Exception as e:
        print(f"  [{name}] FAILED: {e}")


def settle(page, ms=1800):
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    time.sleep(ms / 1000)


def click_nav(page, label):
    page.evaluate(
        """(label) => {
            const el = Array.from(document.querySelectorAll('li,button,a,.nav-item'))
              .find(l => l.textContent.trim().toLowerCase().startsWith(label.toLowerCase()));
            if (el) el.click();
        }""",
        label,
    )


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": W, "height": H}, device_scale_factor=2)
        # Authenticate via the shared request jar → browser inherits the cookie.
        r = ctx.request.post(f"{BASE}/api/auth/login",
                             data={"username": USER, "password": PW})
        print(f"login: HTTP {r.status}")
        page = ctx.new_page()
        page.goto(BASE)
        settle(page, 2500)
        # The SPA renders its own login overlay regardless of the cookie —
        # drive it directly so the session the JS holds is real.
        try:
            if page.query_selector("#auth-overlay:not(.hidden) #auth-username"):
                page.fill("#auth-username", USER)
                page.fill("#auth-password", PW)
                page.click("#auth-submit")
                page.wait_for_selector("#auth-overlay.hidden", timeout=10000)
                print("logged in via overlay")
        except Exception as e:
            print(f"overlay login note: {e}")
        settle(page, 2500)
        page.evaluate("""() => {
            document.querySelectorAll('.onboarding, .toast')
              .forEach(e => { try { e.remove(); } catch(_){} });
        }""")

        # 1. Main library — All Tracks
        click_nav(page, "All Tracks")
        settle(page, 1500)
        shot(page, "01-library", "Main library — All Tracks")

        # 2. Artists
        click_nav(page, "Artists")
        settle(page, 1500)
        shot(page, "02-artists", "Artists browser")

        # 3. Search
        click_nav(page, "All Tracks")
        settle(page, 600)
        s = page.query_selector('input[type="search"], #search-input, [placeholder*="earch"]')
        if s:
            s.click(); s.fill("moon"); settle(page, 1400)
        shot(page, "03-search", "Search results")
        if s:
            s.fill(""); settle(page, 500)

        # 4. Folder tree — expand roots
        click_nav(page, "All Tracks")
        settle(page, 500)
        page.evaluate("""() => {
            document.querySelectorAll('.tree-root .tree-chevron').forEach((c,i)=>{
              if (i < 3 && !c.classList.contains('open')) c.click();
            });
        }""")
        settle(page, 1600)
        shot(page, "04-folders", "Folder tree (FTP / SMB / local)")

        # 5. Play a track → now-playing visuals.  Prefer a tracker so the VU
        #    overlay shows.  Double-click the first track row.
        click_nav(page, "All Tracks")
        settle(page, 800)
        page.evaluate("""() => {
            const row = document.querySelector('.track-row, tr[data-track-id], .tracklist-row');
            if (row) { row.dispatchEvent(new MouseEvent('dblclick', {bubbles:true})); }
        }""")
        settle(page, 6000)  # allow render + waveform/VU to populate
        shot(page, "05-playing", "Now playing — waveform + transport")

        # 6. Track-info modal (per-channel VU for trackers).  Click the
        #    now-playing art or an info affordance.
        page.evaluate("""() => {
            const t = document.querySelector('#player-art, .player-art, .np-art, [data-act="trackinfo"]');
            if (t) t.click();
        }""")
        settle(page, 3500)
        shot(page, "06-trackinfo", "Track info modal — per-channel VU / metadata")
        page.keyboard.press("Escape")
        settle(page, 800)

        # 7. Library Galaxy
        click_nav(page, "Galaxy")
        settle(page, 4000)
        shot(page, "07-galaxy", "Library Galaxy")

        # 8. Settings / admin (may be gated for read-only — best effort)
        page.evaluate("""() => {
            const g = document.getElementById('btn-admin')
              || document.querySelector('[title*="dmin"], [aria-label*="dmin"], [title*="etting"]');
            if (g) g.click();
        }""")
        settle(page, 2500)
        # try to open the Services tab
        page.evaluate("""() => {
            const t = Array.from(document.querySelectorAll('button,.tab,[role=tab]'))
              .find(e => /service/i.test(e.textContent));
            if (t) t.click();
        }""")
        settle(page, 1500)
        shot(page, "08-settings", "Settings — Services (Beta labels)")
        page.keyboard.press("Escape")
        settle(page, 600)

        # 9. Cast picker (Beta)
        page.evaluate("""() => {
            const c = document.getElementById('btn-cast');
            if (c) c.click();
        }""")
        settle(page, 2500)
        shot(page, "09-cast", "Cast picker (Beta)")
        page.keyboard.press("Escape")

        b.close()
        print(f"\nSaved to {IMG}")


if __name__ == "__main__":
    sys.exit(main())
