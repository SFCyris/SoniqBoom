#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render the three SoniqBoom documentation addenda from the structured JSON
produced by the doc-addenda workflow. Usage: _render_addenda.py <data.json>"""
import html
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent

PAGES = {
    "formats": dict(
        file="format-history.html", letter="A",
        title="A Field Guide to Music Formats",
        sub="Every format SoniqBoom plays — where it came from, the hardware or codec behind it, and where to read more.",
    ),
    "trackers": dict(
        file="tracker-history.html", letter="B",
        title="The Tracker Lineage",
        sub="From the Amiga's Ultimate Soundtracker to Impulse Tracker — module music, channel by channel, and the demoscene that carried it.",
    ),
    "retro-gaming": dict(
        file="retro-gaming.html", letter="C",
        title="Music in Retro Gaming",
        sub="How game soundtracks were made, chip by chip — and the formats those machines left behind.",
    ),
}
# topnav cross-links between the addenda + back to the guide
NAV = [
    ("index.html", "◂ User Guide", ""),
    ("format-history.html", "Formats", "formats"),
    ("tracker-history.html", "Trackers", "trackers"),
    ("retro-gaming.html", "Retro gaming", "retro-gaming"),
]


def esc(s):
    return html.escape(str(s or ""), quote=True)


def render_links(links):
    if not links:
        return ""
    out = ['<div class="links">']
    for L in links:
        url = esc(L.get("url", "#"))
        out.append(
            f'<a href="{url}" target="_blank" rel="noopener">'
            f'{esc(L.get("label",""))} <span class="src">· {esc(L.get("source",""))}</span></a>'
        )
    out.append("</div>")
    return "".join(out)


def render_facts(facts):
    if not facts:
        return ""
    chips = "".join(
        f'<span class="chip"><b>{esc(f.get("label",""))}</b> {esc(f.get("value",""))}</span>'
        for f in facts
    )
    return f'<div class="chips">{chips}</div>'


def render_section(s):
    return (
        f'<article class="ref" id="{esc(s.get("id",""))}">'
        f'<div class="ref-head"><h3>{esc(s.get("title",""))}</h3>'
        f'<span class="era">{esc(s.get("era",""))}</span></div>'
        f'{render_facts(s.get("facts"))}'
        f'{s.get("body_html","")}'        # trusted rich HTML from the writer
        f'{render_links(s.get("links"))}'
        f"</article>"
    )


def render_toc(sections):
    items = "".join(
        f'<a href="#{esc(s.get("id",""))}"><span class="n">{i+1:02d}</span>{esc(s.get("title",""))}</a>'
        for i, s in enumerate(sections)
    )
    return f'<nav class="toc">{items}</nav>'


def render_nav(active_file):
    out = ['<nav aria-label="Guide navigation">']
    for href, label, _ in NAV:
        cls = ' class="cta"' if href != active_file and href != "index.html" else (
            ' class="cta"' if href == "index.html" else "")
        # mark current page with colour + weight (non-colour cue) + aria-current
        if href == active_file:
            out.append(f'<a href="{href}" style="color:var(--accent);font-weight:700" aria-current="page">{label}</a>')
        else:
            out.append(f'<a href="{href}"{cls}>{label}</a>')
    out.append("</nav>")
    return "".join(out)


PAGE = """<!DOCTYPE html>
<!-- SPDX-FileCopyrightText: 2026 S.F. Cyris · SPDX-License-Identifier: AGPL-3.0-or-later -->
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — SoniqBoom</title>
<meta name="description" content="{sub}">
<link rel="stylesheet" href="style.css">
</head>
<body>
<div class="bg-field"></div>
<div class="bg-grid"></div>
<canvas id="electrons"></canvas>
<div class="crt-overlay"></div>

<header class="topnav">
  <a class="brand" href="index.html">
    <img src="img/logo.png" alt="SoniqBoom" class="brand-logo" width="112" height="38">
    <span class="ver">Addendum {letter}</span>
  </a>
  {nav}
</header>

<main><div class="wrap">
  <section class="hero" style="padding-bottom:18px">
    <div class="eyebrow">Reference Addendum {letter}</div>
    <h1 style="font-size:clamp(32px,5.5vw,58px)">{title_html}</h1>
    <p class="lede">{sub}</p>
  </section>

  <section style="padding-top:24px">
    {intro}
    <hr class="glow">
    {toc}
    {body}
  </section>

  <section class="center" style="border-top:1px solid var(--border)">
    <p class="muted">Reference links open external sites — Wikipedia and community archives — in a new tab.</p>
    <a href="index.html" class="btn">◂ Back to the User Guide</a>
  </section>
</div></main>

<footer><div class="wrap">
  <span>SoniqBoom · <a href="https://github.com/SFCyris/SoniqBoom">github.com/SFCyris/SoniqBoom</a></span>
  <span class="muted">© 2026 S.F. Cyris · AGPL-3.0-or-later</span>
</div></footer>
<script src="electrons.js"></script>
</body>
</html>
"""


def title_glow(t):
    # highlight the last word in accent for a little flourish
    parts = t.rsplit(" ", 1)
    if len(parts) == 2:
        return f'{esc(parts[0])} <span class="glow">{esc(parts[1])}</span>'
    return esc(t)


def main():
    data = json.loads(Path(sys.argv[1]).read_text())
    if "result" in data and isinstance(data["result"], dict):
        data = data["result"]
    written = []
    for key, meta in PAGES.items():
        d = data.get(key)
        if not d:
            print(f"  !! missing data for {key}")
            continue
        sections = d.get("sections", [])
        body = "".join(render_section(s) for s in sections)
        page = PAGE.format(
            title=esc(meta["title"]), title_html=title_glow(meta["title"]),
            sub=esc(meta["sub"]), letter=meta["letter"],
            nav=render_nav(meta["file"]),
            intro=d.get("intro", ""), toc=render_toc(sections), body=body,
        )
        (HERE / meta["file"]).write_text(page)
        written.append(f'{meta["file"]} ({len(sections)} sections, {len(page)//1024} KB)')
    print("rendered:\n  " + "\n  ".join(written))


if __name__ == "__main__":
    main()
