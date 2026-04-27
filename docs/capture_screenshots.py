#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Capture SoniqBoom screenshots for documentation using Playwright."""

import json
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
IMG_DIR = Path(__file__).parent / "images"
IMG_DIR.mkdir(exist_ok=True)

WIDTH, HEIGHT = 1400, 900


def api_post(path, data=None):
    """Fire-and-forget POST to the SoniqBoom API."""
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def wait_for_load(page, ms=1500):
    page.wait_for_load_state("networkidle")
    time.sleep(ms / 1000)


def capture(page, name, description=""):
    path = IMG_DIR / f"{name}.png"
    page.screenshot(path=str(path))
    size = path.stat().st_size
    print(f"  [{name}] {description}  ({size // 1024} KB)")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            device_scale_factor=2,  # retina quality
        )
        page = ctx.new_page()

        # ── 1. Main library view (All Tracks) ──────────────────────────
        page.goto(BASE)
        wait_for_load(page, 3000)
        page.evaluate("""
            const li = Array.from(document.querySelectorAll('li'))
                .find(l => l.textContent.trim().startsWith('All Tracks'));
            if (li) li.click();
        """)
        time.sleep(1)
        capture(page, "ui-main", "Main library view - All Tracks")

        # ── 2. Artists view ────────────────────────────────────────────
        page.evaluate("""
            const li = Array.from(document.querySelectorAll('li'))
                .find(l => l.textContent.trim().startsWith('Artists'));
            if (li) li.click();
        """)
        time.sleep(1.5)
        capture(page, "ui-artists", "Artists browser")

        # ── 3. Search results ──────────────────────────────────────────
        page.evaluate("""
            const li = Array.from(document.querySelectorAll('li'))
                .find(l => l.textContent.trim().startsWith('All Tracks'));
            if (li) li.click();
        """)
        time.sleep(0.5)
        search = page.query_selector(
            'input[type="search"], input[type="text"], '
            '#search-input, [placeholder*="Search"], [placeholder*="search"]'
        )
        if search:
            search.click()
            search.fill("zelda")
            time.sleep(1.5)
        capture(page, "ui-search", "Search results for 'zelda'")

        # Clear search
        if search:
            search.fill("")
            time.sleep(0.5)

        # ── 4. Folder tree (expand SID) ────────────────────────────────
        page.evaluate("""
            const li = Array.from(document.querySelectorAll('li'))
                .find(l => l.textContent.trim().startsWith('All Tracks'));
            if (li) li.click();
        """)
        time.sleep(0.5)
        # Expand root folders via .tree-chevron clicks
        page.evaluate("""
            const roots = document.querySelectorAll('.tree-root');
            for (const r of roots) {
                const chevron = r.querySelector('.tree-chevron');
                if (chevron && !chevron.classList.contains('open')) chevron.click();
            }
        """)
        time.sleep(1.5)
        # Expand first subfolder inside SID
        page.evaluate("""
            const sid = document.querySelectorAll('.tree-root')[0];
            if (sid) {
                const subs = sid.querySelectorAll('.tree-children .tree-chevron');
                if (subs[0]) subs[0].click();
            }
        """)
        time.sleep(1)
        # Click a subfolder label to show its tracks
        page.evaluate("""
            const sid = document.querySelectorAll('.tree-root')[0];
            if (sid) {
                const labels = sid.querySelectorAll('.tree-children .tree-label');
                if (labels[1]) labels[1].click();
            }
        """)
        time.sleep(1)
        capture(page, "ui-folders", "Folder tree with expanded directories")

        # ── 5. Admin panel (bypass auth temporarily) ───────────────────
        # Tell the server to disable auth, then set localStorage flag
        # so the JS open() function skips the auth dialog.
        api_post("/api/admin/auth/skip", {"disabled": True})
        time.sleep(0.3)

        page.evaluate("localStorage.setItem('sb_skip_auth', '1');")
        time.sleep(0.2)

        # Click admin gear button — it will read the flag and skip auth
        page.evaluate("""
            const btn = document.getElementById('btn-admin');
            if (btn) btn.click();
        """)
        time.sleep(3)

        capture(page, "ui-admin", "Admin panel - Library health")

        # Restore: re-enable auth + remove localStorage flag
        page.evaluate("localStorage.removeItem('sb_skip_auth');")
        api_post("/api/admin/auth/skip", {"disabled": False})

        browser.close()
        print("\nDone! All screenshots saved to docs/images/")


if __name__ == "__main__":
    main()
