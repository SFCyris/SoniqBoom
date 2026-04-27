#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Generate the SoniqBoom logo banner for documentation.

Renders the logo with the same composition as the browser header:
  🔊  SoniqBoom
      by S.F.Cyris

Uses Playwright to render the actual CSS-styled logo at high resolution
against a transparent/dark background, matching the app exactly.
"""

import time
from pathlib import Path
from playwright.sync_api import sync_playwright

IMG_DIR = Path(__file__).parent / "images"
IMG_DIR.mkdir(exist_ok=True)
OUT = IMG_DIR / "logo-banner.png"

# Standalone HTML that replicates the logo from app.css exactly
LOGO_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  @keyframes logo-pulse {
    0%,100% { text-shadow: 0 0 6px rgba(240,114,42,0.55), 0 0 18px rgba(240,114,42,0.30), 0 0 38px rgba(240,114,42,0.12); }
    50%     { text-shadow: 0 0 10px rgba(240,114,42,0.85), 0 0 28px rgba(240,114,42,0.55), 0 0 60px rgba(240,114,42,0.28); }
  }
  @keyframes logo-icon-pulse {
    0%, 100% {
      filter:
        drop-shadow(0 0 3px rgba(107,200,240,0.8))
        drop-shadow(0 0 8px rgba(107,200,240,0.55))
        drop-shadow(0 0 16px rgba(107,200,240,0.3));
    }
    50% {
      filter:
        drop-shadow(0 0 6px rgba(107,200,240,1.0))
        drop-shadow(0 0 14px rgba(107,200,240,0.7))
        drop-shadow(0 0 26px rgba(107,200,240,0.45));
    }
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: #ffffff;
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
  }

  .logo-wrap {
    display: flex;
    flex-direction: row;
    align-items: center;
    gap: 18px;
    padding: 40px 60px;
  }

  .logo-icon {
    position: relative;
    display: inline-flex;
    align-items: center;
    font-size: 64px;
    line-height: 1;
    flex-shrink: 0;
  }
  .logo-icon-glow {
    display: inline-block;
    color: rgba(50,160,210,0.9);
    filter:
      drop-shadow(0 0 3px rgba(50,160,210,0.4))
      drop-shadow(0 0 8px rgba(50,160,210,0.2));
  }
  .logo-icon-top {
    position: absolute;
    top: 0;
    left: 0;
    pointer-events: none;
  }

  .logo-text {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 2px;
  }
  .logo-title {
    font-size: 42px;
    font-weight: 800;
    color: #d95a18;
    letter-spacing: -1px;
    line-height: 1;
  }
  .logo-byline {
    font-size: 14px;
    font-weight: 400;
    color: #9e4515;
    letter-spacing: 0.5px;
    line-height: 1;
  }
</style>
</head>
<body>
  <div class="logo-wrap" id="logo">
    <span class="logo-icon">
      <span class="logo-icon-glow">&#x1f50a;</span>
      <span class="logo-icon-top">&#x1f50a;</span>
    </span>
    <div class="logo-text">
      <span class="logo-title">SoniqBoom</span>
      <span class="logo-byline">by S.F.Cyris</span>
    </div>
  </div>
</body>
</html>
"""


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 800, "height": 300},
            device_scale_factor=2,
        )
        page = ctx.new_page()
        page.set_content(LOGO_HTML)
        time.sleep(0.5)

        # Capture just the logo element with padding
        logo_el = page.query_selector("#logo")
        logo_el.screenshot(path=str(OUT))

        size = OUT.stat().st_size
        print(f"Logo saved: {OUT}  ({size // 1024} KB)")

        browser.close()


if __name__ == "__main__":
    main()
