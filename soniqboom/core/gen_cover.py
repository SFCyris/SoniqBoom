# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generated covers for retro tracks that have no cover of their own.

A chip / tracker / console track without an embedded picture, a folder image
or a game box cover gets a cover drawn here instead of the per-format emoji:
original artwork in the spirit of its platform (a C64 screen with raster
bars, Amiga copper bars over a chequered floor, an Atari ST desktop window, a
PC text-mode box, a console cartridge, a disc …) with the game or album — or
the title when there is neither — and the composer written on it.  No logo,
box design or trademark is copied: the pictures use the platforms' colours
and generic shapes only, and name a platform in plain text.

Rules:

* deterministic — the same platform / format, game or album, composer and
  title always draw the same picture (``Spec.key``); a game's tunes share one
  cover, as an album's tracks do;
* drawn on request only (a list row, the player, Subsonic, cast), never in a
  sweep: ``cover(track, size)`` renders once per key and size, keeps the
  JPEG in memory (``_MEM_MAX`` pictures) and the large sizes in the art cache
  under ``generated/`` — never in a music folder or a track record;
* the ``generated_covers`` setting (config store, default on) switches them
  off: the format emoji shows again;
* a track's own cover, a stored game box cover (``core/igdb_cover``) and an
  online cover all come first — this is the last stand-in before "no cover".
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import random
import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SETTING = "generated_covers"
GEN_VER = 2                     # bump when the drawing changes: new keys, new pictures

_V = 128                        # the virtual canvas (pixel-art resolution)
# size name → (side in pixels, nearest-neighbour factor before the final fit;
# 0: one Hamming resize from the canvas — the list thumbnail, drawn for a
# whole page at once: about a quarter of the two-step cost, within 2/255 a
# pixel on average of the NEAREST ×2 + LANCZOS look)
_SIZES = {"sm": (200, 0), "lg": (550, 5), "full": (1200, 10)}
_QUALITY = 90
_MEM_MAX = 3000
_MEM_BYTES = 96 * 1024 * 1024   # and at most this many bytes of pictures
# concurrent renders (each is a few ms of CPU; a page of list rows asks for
# six at a time, as a browser's connections to the host)
_RENDER_SLOTS = max(1, min(4, os.cpu_count() or 1))

_mem: "OrderedDict[str, bytes]" = OrderedDict()
_mem_bytes = 0
_inflight: dict[str, asyncio.Future] = {}
_sem: asyncio.Semaphore | None = None
_sem_loop = None


# ── What is drawn ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Spec:
    style: str              # the motif (``_STYLES``)
    tag: str                # top line: "C64 · SID"
    headline: str           # game / album / title, as printable text ("" when none is)
    composer: str           # printable composer, "" when unknown
    seed: int               # variant choices (colours, positions)
    key: str                # where the pictures are kept (hex)


# platform (``game_titles.platforms_for``) → (style, plain-text platform name)
_PLATFORM = {
    "c64": ("c64", "C64"), "amiga": ("amiga", "AMIGA"), "atarist": ("atarist", "ATARI ST"),
    "dos": ("dos", "PC"), "nes": ("cart", "NES"), "snes": ("cart16", "SNES"),
    "gb": ("gb", "GAME BOY"), "atari8": ("micro", "ATARI 8-BIT"),
    "megadrive": ("grid", "MEGA DRIVE"), "pce": ("hucard", "PC ENGINE"),
    "psx": ("disc", "PLAYSTATION"), "ps2": ("disc", "PLAYSTATION 2"),
    "n64": ("cart64", "N64"), "gba": ("handheld", "GBA"), "nds": ("handheld", "DS"),
    "saturn": ("disc", "SATURN"), "dreamcast": ("disc", "DREAMCAST"),
    "msx": ("micro", "MSX"), "zx": ("zx", "ZX SPECTRUM"), "cpc": ("micro", "CPC"),
    "x68000": ("fm", "X68000"), "sms": ("cart", "MASTER SYSTEM"), "gamegear": ("handheld", "GAME GEAR"),
}
_FAMILY_STYLE = {
    "tracker": ("tracker", "MODULE"), "midi": ("midi", "MIDI"), "v2": ("synth", "SYNTH"),
    "sunvox": ("synth", "SYNTH"), "sawteeth": ("synth", "SYNTH"), "deflemask": ("fm", "CHIP"),
    "s98": ("fm", "FM"), "vgm": ("pcb", "VGM"), "ay": ("micro", "AY"), "pokey": ("micro", "POKEY"),
    "zxdac": ("zx", "ZX SPECTRUM"), "turbofm": ("fm", "FM"), "saa": ("micro", "SAM COUPE"),
}

_NO_ALBUM = frozenset({"unknown", "unknown album", "<unknown>", "[unknown]", "n/a", "none",
                       "no album", "untitled"})
_SPECIAL = str.maketrans({"ß": "ss", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "ł": "l",
                          "Ł": "L", "đ": "d", "Đ": "D", "þ": "th", "Þ": "Th", "œ": "oe",
                          "Œ": "OE", "–": "-", "—": "-", "‘": "'", "’": "'", "“": '"',
                          "”": '"', "…": "...", "·": "-"})


def _g(track, name):
    if isinstance(track, dict):
        return track.get(name)
    return getattr(track, name, None)


def _text(value) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def printable(value: str, limit: int = 80) -> str:
    """``value`` as the plain ASCII the cover font has: accents dropped, a few
    letters spelled out, anything else left out."""
    s = unicodedata.normalize("NFKD", (value or "").translate(_SPECIAL))
    s = "".join(ch for ch in s if 32 <= ord(ch) < 127)
    return " ".join(s.split())[:limit].strip()


def _style_for(fmt: str) -> tuple[str, str]:
    from soniqboom.core import game_titles, retro
    fam = retro.chip_family(fmt)
    if fam in _FAMILY_STYLE and fam not in ("ay", "pokey"):
        return _FAMILY_STYLE[fam]
    plats, _strict = game_titles.platforms_for(fmt)
    if plats and plats[0] in _PLATFORM and not (fam == "vgm" and len(plats) > 1):
        return _PLATFORM[plats[0]]
    if fam in _FAMILY_STYLE:
        return _FAMILY_STYLE[fam]
    return ("scope", "CHIP")


def applies(track) -> bool:
    """Is ``track`` one a cover is generated for — a retro format?"""
    try:
        from soniqboom.core import retro
        return bool(track is not None and retro.is_retro_format(_g(track, "format")))
    except Exception:                                   # noqa: BLE001
        return False


def spec_for(track) -> Spec | None:
    """What ``track``'s cover shows, or None when it gets none (not retro)."""
    if not applies(track):
        return None
    fmt = str(_g(track, "format") or "")
    style, plat = _style_for(fmt)
    game = _text(_g(track, "game"))
    album = _text(_g(track, "album"))
    title = _text(_g(track, "title"))
    if album.casefold() in _NO_ALBUM:
        album = ""
    if game:
        head, raw = game, ("g", game)
    elif album and album.casefold() != title.casefold():
        head, raw = album, ("a", album)
    else:
        head, raw = title, ("t", title)
    composer = _text(_g(track, "artist")) or _text(_g(track, "composer"))
    try:
        from soniqboom.core.artistinfo import _is_placeholder_artist
        if composer and _is_placeholder_artist(composer):
            composer = _text(_g(track, "composer"))
            if composer and _is_placeholder_artist(composer):
                composer = ""
    except Exception:                                   # noqa: BLE001
        pass
    fmt_label = printable(re.sub(r"\s*\(.*?\)", "", fmt).upper(), 18) or "CHIP"
    tag = plat if plat.replace(" ", "") == fmt_label.replace(" ", "") else f"{plat} - {fmt_label}"
    # (the key holds what is printed, exactly — and the raw names, so names
    # the font can't print still tell covers apart)
    head_txt = printable(head) or printable(title) or fmt_label
    comp_txt = printable(composer, 48)
    ident = "\x1f".join((str(GEN_VER), style, tag, raw[0], raw[1], head_txt, composer, comp_txt))
    digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()
    return Spec(style, tag, head_txt, comp_txt,
                int(digest[:12], 16), digest[:32])


def etag(base: str, spec: Spec | None) -> str:
    """``base`` (a quoted ETag) marked with ``spec``'s picture."""
    if spec is None or len(base) < 2 or not base.endswith('"'):
        return base
    return f'{base[:-1]}:g{spec.key[:12]}"'


# ── Drawing ──────────────────────────────────────────────────────────────────

_C64 = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (104, 55, 43), "cyan": (112, 164, 178),
    "purple": (111, 61, 134), "green": (88, 141, 67), "blue": (53, 40, 121),
    "yellow": (184, 199, 111), "orange": (111, 79, 37), "brown": (67, 57, 0),
    "lred": (154, 103, 89), "dgrey": (68, 68, 68), "grey": (108, 108, 108),
    "lgreen": (154, 210, 132), "lblue": (108, 94, 181), "lgrey": (149, 149, 149),
}
_C64_BARS = (
    ("blue", "lblue", "cyan", "white", "cyan", "lblue", "blue"),
    ("brown", "red", "lred", "yellow", "lred", "red", "brown"),
    ("dgrey", "grey", "lgrey", "white", "lgrey", "grey", "dgrey"),
    ("purple", "lred", "orange", "yellow", "orange", "lred", "purple"),
    ("green", "lgreen", "yellow", "white", "yellow", "lgreen", "green"),
)
_NES = ((188, 0, 0), (0, 88, 248), (0, 168, 0), (104, 68, 252), (248, 120, 88), (228, 0, 88),
        (0, 136, 136), (172, 124, 0))
_ZX = ((215, 0, 0), (0, 215, 215), (215, 215, 0), (0, 0, 215), (0, 215, 0), (215, 0, 215))
_HUES = ((20, 20, 90), (90, 20, 110), (200, 60, 40), (240, 170, 40), (20, 120, 160),
         (30, 140, 70), (170, 30, 90), (60, 60, 160))


_FONT = None


def _font():
    global _FONT
    if _FONT is None:                               # (decoded once: Pillow builds it anew per call)
        from PIL import ImageFont
        _FONT = ImageFont.load_default_imagefont()  # the 6×11 bitmap font: pixel-art text
    return _FONT


def _text_img(text: str, scale: int, fill, shadow=None):
    """``text`` as an RGBA picture in the bitmap font, ``scale`` virtual pixels
    per font pixel, with a one-pixel drop shadow when ``shadow`` is given."""
    from PIL import Image, ImageDraw
    f = _font()
    x0, y0, x1, y1 = f.getbbox(text or " ")
    w, h = max(1, x1) + 1, max(1, y1) + 1
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if shadow is not None:
        d.text((1, 1), text, font=f, fill=shadow)
    d.text((0, 0), text, font=f, fill=fill)
    if scale > 1:
        im = im.resize((w * scale, h * scale), Image.NEAREST)
    return im


def _width(text: str) -> int:
    """``text``'s width in font pixels."""
    return _font().getbbox(text)[2] if text else 0


def _wrap(text: str, max_w: int) -> list[str] | None:
    """``text`` broken into lines of at most ``max_w`` font pixels at word
    gaps; None when one word alone is wider."""
    lines: list[str] = []
    cur = ""
    for w in text.split():
        if _width(w) > max_w:
            return None
        cand = f"{cur} {w}" if cur else w
        if _width(cand) <= max_w:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _clip(text: str, max_w: int) -> str:
    """``text`` cut to ``max_w`` font pixels, "..." marking a cut."""
    if _width(text) <= max_w:
        return text
    while text and _width(text + "...") > max_w:
        text = text[:-1]
    return text.rstrip() + "..."


_LINE = 11                      # the bitmap font's line height, in font pixels
_COMP_H = _LINE + 2             # the composer line under the headline


def _pitch(scale: int) -> int:
    return _LINE * scale - (scale - 1)


def _fit(head: str, width: int, height: int, comp: bool) -> tuple[int, list[str]]:
    """``(scale, lines)``: the headline at twice the font size when it fits
    ``width`` × ``height`` (less the composer line) in at most three lines
    without breaking a word, else at the font size — cut with "..." when
    even that does not fit."""
    room = height - (_COMP_H if comp else 0)
    for scale in (2, 1):
        lines = _wrap(head, width // scale)
        if lines is not None and len(lines) <= (3 if scale == 2 else 99) \
                and len(lines) * _pitch(scale) - (scale - 1) <= room:
            return scale, lines
    most = max(1, (room + 1) // (_LINE + 1))
    words = head.split()
    lines, cur = [], ""
    for w in words:                                     # word by word, long words cut
        cand = f"{cur} {w}" if cur else w
        if _width(cand) <= width:
            cur = cand
            continue
        if cur:
            lines.append(cur)
        cur = w if _width(w) <= width else _clip(w, width)
    if cur:
        lines.append(cur)
    if len(lines) > most:
        lines = lines[:most]
        lines[-1] = _clip(lines[-1] + "...", width)
    return 1, lines


def _put_text(img, box, headline: str, composer: str, fg, *, shadow=None, sub=None,
              align="center", upper=False):
    """The headline (``_fit``) and the composer under it, centred in ``box``
    (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = box
    width = x1 - x0
    head = headline.upper() if upper else headline
    comp = _clip(composer.upper() if upper else composer, width) if composer else ""
    scale, lines = _fit(head, width, y1 - y0, bool(comp)) if head else (1, [])
    total = len(lines) * _pitch(scale) - ((scale - 1) if lines else 0) + (_COMP_H if comp else 0)
    y = y0 + max(0, (y1 - y0 - total) // 2)
    for ln in lines:
        t = _text_img(ln, scale, fg, shadow)
        x = x0 + (width - t.width) // 2 if align == "center" else x0
        img.alpha_composite(t, (max(0, x), max(0, y)))
        y += _pitch(scale)
    if comp:
        y += 2 - ((scale - 1) if lines else 0)
        t = _text_img(comp, 1, sub or fg, shadow)
        x = x0 + (width - t.width) // 2 if align == "center" else x0
        img.alpha_composite(t, (max(0, x), max(0, y)))


def _tag(img, xy, text: str, fg, shadow=None):
    # (unscaled and opaque: drawn straight onto the canvas — the same pixels
    # as a text picture composited there, without the picture)
    from PIL import ImageDraw
    if len(fg) == 3 and (shadow is None or len(shadow) == 3) and xy[0] >= 0 and xy[1] >= 0:
        d = ImageDraw.Draw(img)
        if shadow is not None:
            d.text((xy[0] + 1, xy[1] + 1), text, font=_font(), fill=shadow)
        d.text(xy, text, font=_font(), fill=fg)
        return
    img.alpha_composite(_text_img(text, 1, fg, shadow), xy)


def _gradient(d, box, top, bottom, step=2):
    x0, y0, x1, y1 = box
    n = max(1, (y1 - y0) // step)
    for i in range(n):
        t = i / max(1, n - 1)
        c = tuple(int(top[k] + (bottom[k] - top[k]) * t) for k in range(3))
        d.rectangle((x0, y0 + i * step, x1, y0 + i * step + step - 1), fill=c)


def _mix(a, b, t):
    return tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))


def _draw_c64(img, d, r, sp):
    c = _C64
    d.rectangle((0, 0, _V, _V), fill=c["lblue"])
    d.rectangle((8, 10, _V - 9, _V - 11), fill=c["blue"])
    bars = r.sample(_C64_BARS, 2)
    for yb, bar in zip((r.randint(68, 74), r.randint(82, 88)), bars):
        for i, name in enumerate(bar):
            d.rectangle((0, yb + i, _V, yb + i), fill=c[name])
    _tag(img, (12, 13), sp.tag, c["lblue"])
    _put_text(img, (12, 26, _V - 12, 68), sp.headline, "", c["white"], upper=True)
    if sp.composer:
        _tag(img, (12, _V - 26), _clip(sp.composer.upper(), _V - 24), c["lblue"])
    d.rectangle((12, _V - 14, 17, _V - 13 + 0), fill=c["lblue"])        # the cursor


def _draw_amiga(img, d, r, sp):
    a, b = r.sample(_HUES, 2)
    horizon = 82
    _gradient(d, (0, 0, _V, horizon), _mix(a, (0, 0, 0), 0.5), b, step=3)
    c1, c2 = (r.choice(((230, 230, 230), (240, 200, 60), (80, 200, 230))), (30, 30, 50))
    off = r.random() * 2
    for y in range(horizon, _V):                        # (in runs: few Python steps per row)
        z = 60.0 / (y - horizon + 6)
        v = int(z + off)
        x = 0
        while x < _V:
            u = int((x - _V / 2) * z / 6 + 40)
            end = x + 1
            while end < _V and int((end - _V / 2) * z / 6 + 40) == u:
                end += 1
            d.line((x, y, end - 1, y), fill=c1 if (u + v) % 2 else c2)
            x = end
    d.line((0, horizon, _V, horizon), fill=(255, 255, 255))
    _tag(img, (5, 4), sp.tag, (255, 255, 255), (0, 0, 0))
    _put_text(img, (6, 15, _V - 6, 80), sp.headline, sp.composer, (255, 255, 255),
              shadow=(0, 0, 0), sub=(255, 230, 150))


_NOTES = ("C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-")


def _draw_tracker(img, d, r, sp):
    d.rectangle((0, 0, _V, _V), fill=(8, 10, 18))
    d.rectangle((0, 0, _V, 14), fill=(40, 46, 70))
    _tag(img, (4, 2), sp.tag, (220, 230, 255))
    hl = r.randint(3, 7)
    for row in range(10):
        y = 17 + row * 11
        if row == hl:
            d.rectangle((0, y - 1, _V, y + 9), fill=(40, 40, 110))
        cells = []
        for _ch in range(2):
            if r.random() < 0.55:
                cells.append(f"{r.choice(_NOTES)}{r.randint(1, 5)} {r.randint(1, 31):02X}")
            else:
                cells.append("--- ..")
        line = f"{row:02X} {cells[0]} {cells[1]}"
        _tag(img, (3, y), line, (90, 170, 120) if row != hl else (230, 230, 120))
    d.rectangle((8, 44, _V - 9, 106), fill=(8, 10, 18), outline=(120, 140, 220))
    _put_text(img, (11, 47, _V - 11, 104), sp.headline, sp.composer, (240, 240, 255),
              sub=(140, 200, 255))


def _draw_atarist(img, d, r, sp):
    from PIL import Image
    g1, g2 = bytes((0, 150, 0, 255)), bytes((0, 110, 0, 255))
    a, b = (g2 + g1) * (_V // 2), (g1 + g2) * (_V // 2)
    img.paste(Image.frombytes("RGBA", (_V, _V), b"".join(a if y % 2 == 0 else b for y in range(_V))))
    d.rectangle((0, 0, _V, 10), fill=(255, 255, 255))
    d.line((0, 10, _V, 10), fill=(0, 0, 0))
    _tag(img, (3, 0), sp.tag, (0, 0, 0))
    wx0, wy0, wx1, wy1 = 10, 22 + r.randint(0, 6), _V - 10, _V - 12
    d.rectangle((wx0 + 3, wy0 + 3, wx1 + 3, wy1 + 3), fill=(0, 0, 0))
    d.rectangle((wx0, wy0, wx1, wy1), fill=(255, 255, 255), outline=(0, 0, 0))
    d.rectangle((wx0, wy0, wx1, wy0 + 10), outline=(0, 0, 0))
    for y in range(wy0 + 2, wy0 + 10, 2):
        d.line((wx0 + 12, y, wx1 - 2, y), fill=(0, 0, 0))
    d.rectangle((wx0 + 2, wy0 + 2, wx0 + 9, wy0 + 8), fill=(255, 255, 255), outline=(0, 0, 0))
    _put_text(img, (wx0 + 4, wy0 + 13, wx1 - 4, wy1 - 3), sp.headline, sp.composer, (0, 0, 0))


def _draw_dos(img, d, r, sp):
    blue, cyan, grey = (0, 0, 170), (0, 170, 170), (170, 170, 170)
    d.rectangle((0, 0, _V, _V), fill=blue)
    d.rectangle((0, 0, _V, 11), fill=grey)
    _tag(img, (3, 0), sp.tag, (0, 0, 0))
    d.rectangle((0, _V - 11, _V, _V), fill=cyan)
    _tag(img, (3, _V - 11), "F1 PLAY  ESC QUIT", (0, 0, 0))
    x0, y0, x1, y1 = 10, 22, _V - 12, _V - 24
    d.rectangle((x0 + 4, y0 + 4, x1 + 4, y1 + 4), fill=(0, 0, 0))
    d.rectangle((x0, y0, x1, y1), fill=blue)
    for k in (1, 4):
        d.rectangle((x0 + k, y0 + k, x1 - k, y1 - k), outline=(255, 255, 255))
    for i in range(2):
        yb = y1 - 8 - i * 4
        d.rectangle((x0 + 8, yb, x0 + 8 + r.randint(20, 80), yb + 1), fill=cyan)
    _put_text(img, (x0 + 6, y0 + 6, x1 - 6, y1 - 13), sp.headline, sp.composer,
              (255, 255, 85), sub=(255, 255, 255))


def _cart(img, d, r, sp, *, body, ridge, label, width=100, top=10, round_=False, label_h=64):
    d.rectangle((0, 0, _V, _V), fill=(26, 26, 30))
    x0 = (_V - width) // 2
    x1 = x0 + width
    if round_:
        d.rounded_rectangle((x0, top, x1, _V - 6), radius=10, fill=body)
    else:
        d.rectangle((x0, top, x1, _V - 6), fill=body)
    for y in range(top + 3, top + 13, 3):
        d.line((x0 + 6, y, x1 - 6, y), fill=ridge)
    lx0, ly0, lx1, ly1 = x0 + 6, top + 17, x1 - 6, top + 17 + label_h
    d.rectangle((lx0, ly0, lx1, ly1), fill=label)
    band = _mix(label, (0, 0, 0), 0.55)
    d.rectangle((lx0, ly1 - 12, lx1, ly1), fill=band)
    stripe = _mix(label, (255, 255, 255), 0.35)
    d.rectangle((lx0, ly0 + 3, lx1, ly0 + 4), fill=stripe)
    _put_text(img, (lx0 + 3, ly0 + 6, lx1 - 3, ly1 - 13), sp.headline, "", (255, 255, 255),
              shadow=(0, 0, 0))
    if sp.composer:
        t = _text_img(_clip(sp.composer, lx1 - lx0 - 6), 1, (255, 255, 255))
        img.alpha_composite(t, (lx0 + 3, ly1 - 12))
    _tag(img, (x0 + 6, _V - 20), sp.tag, _mix(body, (0, 0, 0), 0.6))


def _draw_cart(img, d, r, sp):
    _cart(img, d, r, sp, body=(150, 150, 150), ridge=(110, 110, 110), label=r.choice(_NES))


def _draw_cart16(img, d, r, sp):
    lab = _mix(r.choice(_NES), (120, 100, 200), 0.45)
    _cart(img, d, r, sp, body=(175, 175, 185), ridge=(140, 140, 150), label=lab, width=104,
          round_=True)


def _draw_cart64(img, d, r, sp):
    _cart(img, d, r, sp, body=(70, 70, 76), ridge=(40, 40, 44), label=r.choice(_HUES),
          width=110, round_=True, label_h=66)


def _draw_handheld(img, d, r, sp):
    _cart(img, d, r, sp, body=(90, 80, 150), ridge=(60, 54, 110), label=r.choice(_NES),
          width=96, top=16, round_=True, label_h=60)


def _draw_hucard(img, d, r, sp):
    _cart(img, d, r, sp, body=(235, 235, 225), ridge=(200, 200, 190),
          label=r.choice(((230, 110, 20), (200, 30, 40), (30, 90, 200))), width=100, top=16,
          round_=True, label_h=64)


def _draw_gb(img, d, r, sp):
    g = ((15, 56, 15), (48, 98, 48), (139, 172, 15), (155, 188, 15))
    d.rectangle((0, 0, _V, _V), fill=(196, 196, 188))
    d.rounded_rectangle((10, 10, _V - 10, 96), radius=6, fill=(90, 92, 104))
    d.rectangle((22, 20, _V - 22, 88), fill=g[3])
    px = img.load()
    for y in range(20, 89, 2):
        for x in range(22, _V - 21, 2):
            px[x, y] = (*g[2], 255)
    _put_text(img, (25, 24, _V - 25, 84), sp.headline, sp.composer, g[0], sub=g[1])
    _tag(img, (12, 100), sp.tag, (60, 60, 140))


def _draw_grid(img, d, r, sp):
    d.rectangle((0, 0, _V, _V), fill=(10, 10, 12))
    for x in range(0, _V, 8):
        d.line((x, 0, x, 60), fill=(60, 60, 66))
    for y in range(0, 61, 8):
        d.line((0, y, _V, y), fill=(60, 60, 66))
    d.rectangle((0, 62, _V, 66), fill=r.choice(((200, 20, 30), (20, 80, 200), (220, 150, 0))))
    _tag(img, (4, 2), sp.tag, (230, 230, 235), (0, 0, 0))
    _put_text(img, (6, 70, _V - 6, 124), sp.headline, sp.composer, (255, 255, 255),
              sub=(180, 180, 190))


def _draw_disc(img, d, r, sp):
    base = r.choice(((16, 16, 20), (10, 20, 60), (40, 40, 44), (235, 235, 238)))
    light = sum(base) > 400
    d.rectangle((0, 0, _V, _V), fill=base)
    cx, cy, rad = 64, 46, 32
    for k in range(rad, 7, -1):
        t = (k * 37 % 100) / 100.0
        hue = _mix((180, 200, 230), r.choice(_HUES), t * 0.6)
        d.ellipse((cx - k, cy - k, cx + k, cy + k), fill=_mix(hue, (230, 230, 235), 0.5))
    d.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), fill=base)
    d.ellipse((cx - 13, cy - 13, cx + 13, cy + 13), outline=(150, 150, 160))
    fg = (20, 20, 24) if light else (255, 255, 255)
    _put_text(img, (6, 82, _V - 6, 125), sp.headline, sp.composer, fg,
              sub=(110, 110, 120) if light else (190, 190, 200))
    _tag(img, (4, 2), sp.tag, fg)


def _draw_zx(img, d, r, sp):
    pairs = (((215, 0, 0), (0, 215, 215)), ((215, 215, 0), (0, 0, 215)))
    pa, pb = r.choice(pairs)
    y = 0
    while y < _V:
        h = r.randint(2, 5)
        d.rectangle((0, y, _V, y + h - 1), fill=pa if (y // 3) % 2 else pb)
        y += h
    d.rectangle((10, 12, _V - 11, _V - 13), fill=(215, 215, 215))
    for i, col in enumerate(r.sample(_ZX, 4)):
        d.rectangle((10 + i * 8, _V - 21, 17 + i * 8, _V - 14), fill=col)
    _tag(img, (13, 14), sp.tag, (0, 0, 0))
    _put_text(img, (13, 28, _V - 14, 98), sp.headline, sp.composer, (0, 0, 0),
              sub=(0, 0, 215))


def _draw_micro(img, d, r, sp):
    paper, ink, border = r.choice((
        ((0, 0, 128), (255, 255, 0), (0, 0, 64)),          # 8-bit micro, blue and yellow
        ((24, 96, 200), (190, 220, 255), (12, 48, 120)),
        ((0, 0, 0), (255, 255, 255), (40, 40, 160)),
        ((0, 96, 0), (200, 255, 200), (0, 48, 0)),
    ))
    d.rectangle((0, 0, _V, _V), fill=border)
    d.rectangle((8, 8, _V - 9, _V - 9), fill=paper)
    _tag(img, (11, 11), sp.tag, ink)
    _tag(img, (11, 24), "READY", ink)
    _put_text(img, (11, 38, _V - 11, 104), sp.headline, sp.composer, ink, align="left")
    d.rectangle((11, _V - 20, 16, _V - 12), fill=ink)


def _wave(d, r, box, color, *, cycles=None, width=1):
    x0, y0, x1, y1 = box
    import math
    cyc = cycles or r.uniform(1.5, 4.0)
    ph = r.uniform(0, math.tau)
    mod = r.uniform(0.0, 2.5)
    pts = []
    for x in range(x0, x1 + 1):
        t = (x - x0) / max(1, x1 - x0)
        v = math.sin(math.tau * cyc * t + ph + mod * math.sin(math.tau * t * 2))
        pts.append((x, int((y0 + y1) / 2 - v * (y1 - y0) / 2)))
    d.line(pts, fill=color, width=width)


def _draw_scope(img, d, r, sp):
    d.rectangle((0, 0, _V, _V), fill=(6, 20, 10))
    for x in range(0, _V, 16):
        d.line((x, 0, x, _V), fill=(16, 50, 24))
    for y in range(0, _V, 16):
        d.line((0, y, _V, y), fill=(16, 50, 24))
    _wave(d, r, (4, 14, _V - 4, 50), (40, 140, 60), width=3)
    _wave(d, r, (4, 14, _V - 4, 50), (150, 255, 160))
    _tag(img, (4, 2), sp.tag, (150, 255, 160))
    _put_text(img, (6, 56, _V - 6, 122), sp.headline, sp.composer, (190, 255, 190),
              sub=(110, 200, 120))


def _draw_synth(img, d, r, sp):
    a = r.choice(_HUES)
    _gradient(d, (0, 0, _V, _V), (6, 6, 14), _mix(a, (0, 0, 0), 0.6), step=4)
    neon = _mix(a, (255, 255, 255), 0.55)
    _wave(d, r, (0, 20, _V, 56), _mix(a, (0, 0, 0), 0.2), width=4)
    _wave(d, r, (0, 20, _V, 56), neon)
    _tag(img, (4, 2), sp.tag, neon)
    _put_text(img, (6, 62, _V - 6, 122), sp.headline, sp.composer, (255, 255, 255), sub=neon)


def _draw_fm(img, d, r, sp):
    d.rectangle((0, 0, _V, _V), fill=(36, 36, 42))
    acc = r.choice(((220, 40, 40), (240, 150, 20), (60, 160, 240)))
    d.rectangle((0, 0, 6, _V), fill=acc)
    for i in range(4):                                  # four FM operators
        x = 14 + i * 28
        d.rectangle((x, 12, x + 20, 32), outline=(150, 150, 160))
        _wave(d, r, (x + 2, 14, x + 18, 30), acc, cycles=1 + i)
        if i < 3:
            d.line((x + 20, 22, x + 28, 22), fill=(150, 150, 160))
    _tag(img, (12, 38), sp.tag, (200, 200, 210))
    _put_text(img, (12, 52, _V - 6, 122), sp.headline, sp.composer, (255, 255, 255),
              sub=acc, align="left")


def _draw_pcb(img, d, r, sp):
    d.rectangle((0, 0, _V, _V), fill=(18, 90, 50))
    gold = (200, 170, 70)
    for _ in range(9):
        y = r.randint(4, 60)
        x = r.randint(0, 40)
        d.line((x, y, x + r.randint(20, 70), y), fill=gold)
        d.line((x, y, x, y + r.randint(6, 30)), fill=gold)
    d.rectangle((40, 16, 88, 44), fill=(20, 20, 22))
    for x in range(43, 87, 5):
        d.rectangle((x, 13, x + 2, 15), fill=(190, 190, 195))
        d.rectangle((x, 45, x + 2, 47), fill=(190, 190, 195))
    d.ellipse((43, 19, 46, 22), fill=(60, 60, 62))
    d.rectangle((8, 58, _V - 9, _V - 9), fill=(240, 240, 232))
    d.rectangle((8, 58, _V - 9, 66), fill=r.choice(_NES))
    _tag(img, (11, 56), sp.tag, (255, 255, 255))
    _put_text(img, (11, 68, _V - 11, _V - 11), sp.headline, sp.composer, (20, 20, 24),
              sub=(90, 90, 100))


def _draw_midi(img, d, r, sp):
    d.rectangle((0, 0, _V, _V), fill=(250, 246, 236))
    for i in range(5):
        y = 14 + i * 5
        d.line((4, y, _V - 4, y), fill=(60, 60, 60))
    for k in range(6):
        x = 14 + k * 18
        y = 14 + r.randint(0, 8) * 2.5
        d.ellipse((x, y - 2, x + 6, y + 2), fill=(20, 20, 20))
        d.line((x + 6, y, x + 6, y - 12), fill=(20, 20, 20))
    ky = 96
    d.rectangle((0, ky, _V, _V), fill=(255, 255, 255))
    for x in range(0, _V, 9):
        d.line((x, ky, x, _V), fill=(40, 40, 40))
    for i, x in enumerate(range(6, _V, 9)):
        if i % 7 not in (2, 6):
            d.rectangle((x, ky, x + 5, ky + 18), fill=(20, 20, 20))
    _tag(img, (4, 36), sp.tag, (150, 40, 40))
    _put_text(img, (6, 48, _V - 6, 94), sp.headline, sp.composer, (20, 20, 20),
              sub=(110, 60, 60))


_STYLES = {
    "c64": _draw_c64, "amiga": _draw_amiga, "tracker": _draw_tracker, "atarist": _draw_atarist,
    "dos": _draw_dos, "cart": _draw_cart, "cart16": _draw_cart16, "cart64": _draw_cart64,
    "handheld": _draw_handheld, "hucard": _draw_hucard, "gb": _draw_gb, "grid": _draw_grid,
    "disc": _draw_disc, "zx": _draw_zx, "micro": _draw_micro, "scope": _draw_scope,
    "synth": _draw_synth, "fm": _draw_fm, "pcb": _draw_pcb, "midi": _draw_midi,
}


def render(spec: Spec, size: str = "lg") -> bytes:
    """``spec``'s cover as JPEG bytes, ``size`` ``sm`` / ``lg`` / ``full``
    (200 / 550 / 1200 px square).  Pure: the same spec, the same bytes."""
    from PIL import Image, ImageDraw
    side, factor = _SIZES.get(size, _SIZES["full"])
    img = Image.new("RGBA", (_V, _V), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)
    _STYLES.get(spec.style, _draw_scope)(img, d, random.Random(spec.seed), spec)
    if not factor:
        out = img.convert("RGB").resize((side, side), Image.HAMMING)
    else:
        out = img.convert("RGB").resize((_V * factor, _V * factor), Image.NEAREST)
        if out.width != side:
            out = out.resize((side, side), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=_QUALITY, subsampling=0)
    return buf.getvalue()


# ── Caching ──────────────────────────────────────────────────────────────────

def _path(key: str, size: str) -> Path:
    from soniqboom.config import get_art_cache_dir
    return get_art_cache_dir() / "generated" / key[:2] / f"{key}.{size}.jpg"


# Sizes kept on disk too: a list thumbnail draws in about 1.5 ms, the large
# ones in 10-25 ms (measured on the dev Mac), so only those are worth a file.
_DISK_SIZES = ("lg", "full")


def _load_or_render(spec: Spec, size: str) -> bytes:
    """(worker thread) The stored picture, else drawn now (and stored, for
    ``_DISK_SIZES``)."""
    if size not in _DISK_SIZES:
        return render(spec, size)
    p = _path(spec.key, size)
    try:
        data = p.read_bytes()
        if data:
            return data
    except OSError:
        pass
    data = render(spec, size)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
    except OSError as exc:
        log.debug("generated cover: could not store %s: %s", p.name, exc)
    return data


def _slots() -> asyncio.Semaphore:
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        _sem, _sem_loop = asyncio.Semaphore(_RENDER_SLOTS), loop
    return _sem


def _remember(k: str, data: bytes) -> None:
    global _mem_bytes
    old = _mem.pop(k, None)
    if old is not None:
        _mem_bytes -= len(old)
    _mem[k] = data
    _mem_bytes += len(data)
    while _mem and (len(_mem) > _MEM_MAX or _mem_bytes > _MEM_BYTES):
        _k, gone = _mem.popitem(last=False)
        _mem_bytes -= len(gone)


async def picture(spec: Spec, size: str) -> bytes | None:
    """``spec``'s cover in ``size`` — from memory, the art cache, or drawn
    now (one render per key and size at a time; never raises)."""
    if size not in _SIZES:
        size = "full"
    k = f"{spec.key}.{size}"
    hit = _mem.get(k)
    if hit is not None:
        _mem.move_to_end(k)
        return hit
    fut = _inflight.get(k)
    if fut is not None:
        try:
            return await asyncio.shield(fut)
        except Exception:                               # noqa: BLE001
            return None
    fut = asyncio.get_running_loop().create_future()
    _inflight[k] = fut
    try:
        async with _slots():
            data = await asyncio.to_thread(_load_or_render, spec, size)
        _remember(k, data)
        fut.set_result(data)
        return data
    except Exception as exc:                            # noqa: BLE001
        log.warning("generated cover failed for %s: %s", spec.key, exc)
        return None
    finally:
        _inflight.pop(k, None)
        if not fut.done():
            fut.set_result(None)            # (cancelled: the others asking get nothing, not a hang)


def enabled_now() -> bool:
    """The ``generated_covers`` setting (default on), read synchronously."""
    try:
        from soniqboom.core.store import get_store
        v = get_store().get_config(SETTING, True)
    except Exception:                                   # noqa: BLE001
        return True
    if isinstance(v, str):
        return v.strip().casefold() not in ("", "0", "false", "off", "no")
    return True if v is None else bool(v)


async def spec_if_on(track) -> Spec | None:
    """``spec_for(track)`` while the setting is on; None otherwise."""
    if track is None or not enabled_now():
        return None
    try:
        return spec_for(track)
    except Exception:                                   # noqa: BLE001
        log.debug("generated cover spec failed", exc_info=True)
        return None


async def cover(track, size: str = "full") -> bytes | None:
    """``track``'s generated cover as JPEG bytes, or None (not retro, or the
    setting off)."""
    sp = await spec_if_on(track)
    return await picture(sp, size) if sp is not None else None


def forget_all() -> None:
    """Forget the pictures held in memory (the art cache was emptied)."""
    global _mem_bytes
    _mem.clear()
    _mem_bytes = 0

