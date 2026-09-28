#!/usr/bin/env python3
"""Build the bundled game-title lists (soniqboom/data/game_titles/).

One gzip TSV per source — ``<platform>\\t<title>`` lines, sorted — plus
``sources.json`` (licence, origin, date and per-platform counts of each):

  wikidata  Wikidata (CC0 1.0): video games per retro platform, English label,
            English/Japanese aliases, titles and romanisations — fetched from
            the Wikidata Query Service (a handful of polite queries).
  nointro   No-Intro (DAT-o-MATIC "Data Usage License"): the games of the
            cartridge / computer DATs.  DAT-o-MATIC has no scriptable download:
            pass the "No-Intro Love Pack (DAT)" zip (or its extracted folder)
            with --nointro.  The Software Preservation Society-credited DATs
            (Amiga, Atari ST, IBM PC floppy, ZX +3) are left out.
  mame      MAME software lists (hash/*.xml, CC0 1.0).
  zxdb      ZXDB (ODbL 1.0): ZX Spectrum games and their aliases.

  python3 scripts/build_game_titles.py [--only wikidata,mame,...] [--nointro PATH]
         [--mame-dir DIR] [--zxdb-sql FILE] [--wikidata-cache DIR]

Sources not rebuilt keep their current files.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import glob
import gzip
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from soniqboom.core import game_titles_dat as dat  # noqa: E402

OUT = os.path.join(ROOT, "soniqboom", "data", "game_titles")
UA = "SoniqBoom-game-title-list-builder (https://github.com/SFCyris/SoniqBoom)"

SOURCES = {
    "wikidata": {"name": "Wikidata", "licence": "CC0 1.0",
                 "url": "https://www.wikidata.org/wiki/Wikidata:Licensing",
                 "credit": "Data from Wikidata"},
    "nointro": {"name": "No-Intro", "licence": "DAT-o-MATIC Data Usage License",
                "url": "https://datomatic.no-intro.org/terms.html",
                "credit": "No-Intro (no-intro.org)"},
    "mame": {"name": "MAME software lists", "licence": "CC0 1.0",
             "url": "https://github.com/mamedev/mame/tree/master/hash",
             "credit": "MAME software lists (mamedev.org)"},
    "zxdb": {"name": "ZXDB", "licence": "ODbL 1.0",
             "url": "https://github.com/zxdb/ZXDB",
             "credit": "Contains information from ZXDB, made available under the ODbL"},
}


def _get(url: str, *, data: bytes | None = None, headers: dict | None = None,
         timeout: float = 120) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _write(source: str, store: dict[str, set[str]]) -> dict[str, int]:
    os.makedirs(OUT, exist_ok=True)
    buf = io.StringIO()
    counts = {}
    for plat in sorted(store):
        titles = sorted({" ".join(t.replace("\t", " ").split()) for t in store[plat]} - {""},
                        key=str.casefold)
        counts[plat] = len(titles)
        for t in titles:
            buf.write(f"{plat}\t{t}\n")
    with gzip.open(os.path.join(OUT, f"{source}.tsv.gz"), "wt", encoding="utf-8",
                   compresslevel=9) as fh:
        fh.write(buf.getvalue())
    print(f"{source}: {sum(counts.values())} titles", counts, file=sys.stderr)
    return counts


# ── Wikidata ──────────────────────────────────────────────────────────────────

# Platform items, checked by their English labels (2026-09-26).
WIKIDATA_PLATFORMS = {
    "c64": ["Q99775", "Q1115883", "Q1115981"],
    "amiga": ["Q100047", "Q471094", "Q384656", "Q471089", "Q471144", "Q471158", "Q695161",
              "Q955368", "Q380526", "Q1343048"],
    "atarist": ["Q627302", "Q753619", "Q8207936", "Q740626", "Q1574899"],
    "dos": ["Q170434", "Q47604", "Q863568", "Q1419081", "Q751046", "Q202712", "Q80031078",
            "Q1155069", "Q34349", "Q1141670"],
    "nes": ["Q172742", "Q135321", "Q491640", "Q2982516", "Q1323782", "Q26706777"],
    "snes": ["Q183259", "Q1193565", "Q909092", "Q30827691"],
    "gb": ["Q186437", "Q203992", "Q1190117", "Q2916338"],
    "gba": ["Q188642", "Q19878359", "Q963631"],
    "n64": ["Q184839", "Q253044", "Q285025"],
    "nds": ["Q170323", "Q637178", "Q844552", "Q10318711"],
    "psx": ["Q10677", "Q56668812"],
    "ps2": ["Q10680"],
    "saturn": ["Q200912", "Q1067380"],
    "dreamcast": ["Q184198", "Q1369174", "Q843916", "Q757617"],
    "megadrive": ["Q10676", "Q1047516", "Q1063978", "Q62603091", "Q112224827", "Q1374482",
                  "Q388384"],
    "sms": ["Q209868", "Q1192432", "Q1136956", "Q1322287"],
    "gamegear": ["Q751719"],
    "pce": ["Q1057377", "Q10854461", "Q202375", "Q841252"],
    "msx": ["Q853547", "Q11232203", "Q11232199", "Q11232214", "Q2445525"],
    "zx": ["Q23882", "Q9132410"],
    "cpc": ["Q478829", "Q981935", "Q118163709", "Q4862171", "Q2844516"],
    "atari8": ["Q249075", "Q3306898", "Q4889765", "Q10421167", "Q3056443", "Q3777300"],
    "arcade": ["Q192851", "Q15613992", "Q113726751", "Q1349717"],
    "neogeo": ["Q1054350", "Q3338058", "Q64428080", "Q2703883", "Q17042614"],
    "x68000": ["Q1758277"],
}
_ARCADE_BOARD = "Q631229"        # arcade system board: every one of them is a platform too
_WD_EN = {"en", "en-gb", "en-us", "en-ca", "mul"}
_last_query = [0.0]


def _sparql(q: str) -> list[dict]:
    for attempt in range(5):
        wait = _last_query[0] + 2.0 - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            raw = _get("https://query.wikidata.org/sparql",
                       data=urllib.parse.urlencode({"query": q}).encode(),
                       headers={"Accept": "application/sparql-results+json",
                                "Content-Type": "application/x-www-form-urlencoded"},
                       timeout=90)
            _last_query[0] = time.time()
            return json.loads(raw)["results"]["bindings"]
        except urllib.error.HTTPError as exc:
            _last_query[0] = time.time()
            if exc.code == 429:
                time.sleep(int(exc.headers.get("Retry-After", "60")) + 1)
                continue
            if exc.code >= 500:
                time.sleep(10)
                continue
            raise
    raise RuntimeError("Wikidata query failed after retries")


def build_wikidata(cache: str) -> dict[str, set[str]]:
    """Items that are (a subclass of) video game with a retro platform (P400),
    under their English label, English and Japanese aliases, titles (P1476)
    and Hepburn romanisations.  Query results are cached in ``cache``."""
    os.makedirs(os.path.join(cache, "items"), exist_ok=True)
    os.makedirs(os.path.join(cache, "names"), exist_ok=True)
    qid = lambda u: u.rsplit("/", 1)[1]  # noqa: E731
    items: dict[str, dict] = {}
    for code, qs in WIKIDATA_PLATFORMS.items():
        fn = os.path.join(cache, "items", f"{code}.json")
        if not os.path.exists(fn):
            extra = (f"UNION {{ ?p wdt:P31 wd:{_ARCADE_BOARD} . ?g wdt:P400 ?p . }}"
                     if code == "arcade" else "")
            rows = _sparql(f"""SELECT DISTINCT ?g ?p WHERE {{
              {{ VALUES ?p {{ {' '.join('wd:' + x for x in qs)} }} ?g wdt:P400 ?p . }} {extra}
              FILTER EXISTS {{ ?g wdt:P31/wdt:P279* wd:Q7889 }} }}""")
            d: dict[str, list] = {}
            for r in rows:
                d.setdefault(qid(r["g"]["value"]), []).append(qid(r["p"]["value"]))
            with open(fn, "w") as fh:
                json.dump(d, fh)
        with open(fn) as fh:
            items[code] = json.load(fh)
    every = sorted({g for d in items.values() for g in d}, key=lambda s: int(s[1:]))
    names: dict[str, list] = collections.defaultdict(list)
    for i in range(0, len(every), 1200):
        fn = os.path.join(cache, "names", f"batch_{i // 1200:03d}.json")
        if not os.path.exists(fn):
            batch = every[i:i + 1200]
            rows = _sparql(f"""SELECT ?g ?kind ?text WHERE {{
              VALUES ?g {{ {' '.join('wd:' + x for x in batch)} }}
              {{ ?g rdfs:label ?text BIND("label" AS ?kind) }}
              UNION {{ ?g skos:altLabel ?text BIND("alias" AS ?kind)
                       FILTER(LANG(?text) IN ("en","en-gb","en-us","en-ca","mul","ja")) }}
              UNION {{ ?g wdt:P1476 ?text BIND("title" AS ?kind) }}
              UNION {{ ?g p:P1476 ?st . ?st pq:P2125 ?text BIND("romaji" AS ?kind) }} }}""")
            out = [(qid(r["g"]["value"]), r["kind"]["value"], r["text"].get("xml:lang", ""),
                    r["text"]["value"]) for r in rows]
            with open(fn, "w", encoding="utf-8") as fh:
                json.dump(out, fh, ensure_ascii=False)
        with open(fn, encoding="utf-8") as fh:
            for q, kind, lang, text in json.load(fh):
                names[q].append((kind, lang, text.strip()))
    store: dict[str, set[str]] = collections.defaultdict(set)
    for code, d in items.items():
        for q in d:
            for kind, lang, t in names.get(q, ()):
                if not t:
                    continue
                if ((kind == "label" and lang in _WD_EN) or (kind == "alias" and lang in _WD_EN)
                        or (lang == "ja" and kind in ("label", "alias", "title"))
                        or kind in ("romaji", "title")):
                    store[code].add(t)
    return store


# ── No-Intro ──────────────────────────────────────────────────────────────────

def build_nointro(path: str) -> dict[str, set[str]]:
    """``path``: the "No-Intro Love Pack (DAT)" zip, or a folder of its DATs."""
    if os.path.isdir(path):
        return dat.titles_from_nointro_dats(glob.glob(os.path.join(path, "**", "*.dat"),
                                                      recursive=True))
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(path) as z:
        z.extractall(tmp)
        return dat.titles_from_nointro_dats(glob.glob(os.path.join(tmp, "**", "*.dat"),
                                                      recursive=True))


# ── MAME software lists ───────────────────────────────────────────────────────

MAME_LISTS = {
    "c64_cart": "c64", "c64_cass": "c64", "c64_flop_orig": "c64", "c64_flop_misc": "c64",
    "c64_quik": "c64", "c128_flop": "c64", "c128_cart": "c64",
    "amiga_flop": "amiga", "amigaaga_flop": "amiga", "amigaecs_flop": "amiga",
    "amigaocs_flop": "amiga", "cd32": "amiga", "cdtv": "amiga",
    "st_flop": "atarist", "st_cart": "atarist",
    "a800": "atari8", "a800_flop": "atari8", "a800_cass": "atari8", "xegs": "atari8",
    "nes": "nes", "famicom_flop": "nes", "snes": "snes", "snes_bspack": "snes",
    "gameboy": "gb", "gbcolor": "gb", "gba": "gba", "n64": "n64", "nds": "nds", "psx": "psx",
    "saturn": "saturn", "dc": "dreamcast", "megadriv": "megadrive", "megacd": "megadrive",
    "32x": "megadrive", "sms": "sms", "gamegear": "gamegear", "pce": "pce", "pcecd": "pce",
    "tg16": "pce", "sgx": "pce", "msx1_cart": "msx", "msx1_flop": "msx", "msx1_cass": "msx",
    "msx2_cart": "msx", "msx2_flop": "msx", "specpls3_flop": "zx", "spectrum_cass": "zx",
    "cpc_cass": "cpc", "gx4000": "cpc", "x68k_flop": "x68000", "neogeo": "neogeo",
}


def build_mame(local_dir: str | None) -> dict[str, set[str]]:
    """Each list's ``<software><description>`` plus its ``alt_title`` infos."""
    store: dict[str, set[str]] = collections.defaultdict(set)
    for name, plat in MAME_LISTS.items():
        f = os.path.join(local_dir, name + ".xml") if local_dir else None
        if f and os.path.exists(f):
            src = open(f, "rb")
        else:
            src = io.BytesIO(_get(f"https://raw.githubusercontent.com/mamedev/mame/master/hash/{name}.xml"))
            time.sleep(0.5)
        with src:
            for _ev, el in ET.iterparse(src):
                if el.tag != "software":
                    continue
                titles = [el.findtext("description") or ""] + [
                    i.get("value") for i in el.findall("info")
                    if i.get("name") == "alt_title" and i.get("value")]
                for t in titles:
                    t = " ".join(t.split())
                    if t:
                        for a in dat.with_article_aliases(t):
                            store[plat].add(a)
                el.clear()
    return store


# ── ZXDB ──────────────────────────────────────────────────────────────────────

_ZX_GAME_GENRES = ("Adventure Game", "Arcade Game", "Casual Game", "Game:", "Puzzle Game",
                   "Sport Game", "Strategy Game")


def _mysql_rows(s: str) -> list[list]:
    rows, i, n = [], 0, len(s)
    while i < n:
        if s[i] != "(":
            i += 1
            continue
        i += 1
        row: list = []
        cur = None
        while True:
            c = s[i]
            if c == "'":
                i += 1
                buf = []
                while True:
                    c = s[i]
                    if c == "\\":
                        nx = s[i + 1]
                        buf.append({"n": "\n", "r": "\r", "t": "\t", "0": "\0"}.get(nx, nx))
                        i += 2
                    elif c == "'":
                        if i + 1 < n and s[i + 1] == "'":
                            buf.append("'")
                            i += 2
                        else:
                            i += 1
                            break
                    else:
                        buf.append(c)
                        i += 1
                cur = "".join(buf)
            elif c == ",":
                row.append(cur)
                cur = None
                i += 1
            elif c == ")":
                row.append(cur)
                rows.append(row)
                i += 1
                break
            elif c in " \t\r\n":
                i += 1
            else:
                j = i
                while s[j] not in ",)":
                    j += 1
                tok = s[i:j].strip()
                cur = None if tok == "NULL" else tok
                i = j
    return rows


def build_zxdb(sql_path: str | None) -> dict[str, set[str]]:
    """Game entries (genre type a game) on Spectrum-family machines, plus
    their aliases — without the "COLLECTION N:" style compilation aliases."""
    if not sql_path:
        raw = _get("https://github.com/zxdb/ZXDB/raw/master/ZXDB_mysql.sql.zip", timeout=600)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            text = z.read(next(n for n in z.namelist() if n.endswith(".sql"))).decode("utf-8")
    else:
        with open(sql_path, encoding="utf-8") as fh:
            text = fh.read()
    want = {"entries", "aliases", "genretypes", "machinetypes"}
    tables: dict[str, list] = collections.defaultdict(list)
    cols: dict[str, list] = {}
    cur = None
    buf: list[str] = []
    for line in text.splitlines(keepends=True):
        m = re.match(r"INSERT INTO `(\w+)` \(([^)]*)\) VALUES", line)
        if m:
            cur = m.group(1) if m.group(1) in want else None
            if cur:
                cols[cur] = [c.strip(" `") for c in m.group(2).split(",")]
                buf = []
            continue
        if cur:
            buf.append(line)
            if line.rstrip().endswith(");"):
                tables[cur].extend(_mysql_rows("".join(buf)))
                buf = []
                cur = None
    recs = lambda t: [dict(zip(cols[t], r)) for r in tables[t]]  # noqa: E731
    genre = {int(r["id"]): r["text"] for r in recs("genretypes")}
    mach = {int(r["id"]): r["text"] for r in recs("machinetypes")}
    spectrum = {i for i, t in mach.items() if t.startswith(("ZX-Spectrum", "Timex")) or t in (
        "Pentagon 128", "Scorpion", "ZX-Evolution", "ZX-UNO", "TK90X/TK95", "ATM", "Baltic",
        "AT Computer System")}
    games = {}
    for e in recs("entries"):
        g = int(e["genretype_id"]) if e["genretype_id"] else None
        mt = int(e["machinetype_id"]) if e["machinetype_id"] else None
        if genre.get(g, "").startswith(_ZX_GAME_GENRES) and (mt is None or mt in spectrum):
            games[int(e["id"])] = e["title"]
    store: dict[str, set[str]] = collections.defaultdict(set)
    for t in games.values():
        store["zx"].add(t)
    for a in recs("aliases"):
        t = a.get("title") or ""
        if int(a["entry_id"]) in games and t and not re.match(r"^COLLECTION\b", t, re.I):
            store["zx"].add(t)
    return store


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default="wikidata,nointro,mame,zxdb")
    ap.add_argument("--nointro", help="No-Intro Love Pack (DAT) zip or folder")
    ap.add_argument("--mame-dir", help="local copy of MAME's hash/ folder")
    ap.add_argument("--zxdb-sql", help="local ZXDB_mysql.sql")
    ap.add_argument("--wikidata-cache", default=os.path.join(tempfile.gettempdir(), "sb-wikidata"))
    args = ap.parse_args()
    meta_path = os.path.join(OUT, "sources.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    today = datetime.date.today().isoformat()
    for src in [s.strip() for s in args.only.split(",") if s.strip()]:
        if src == "wikidata":
            store = build_wikidata(args.wikidata_cache)
        elif src == "nointro":
            if not args.nointro:
                print("nointro: skipped (pass --nointro PACK)", file=sys.stderr)
                continue
            store = build_nointro(args.nointro)
        elif src == "mame":
            store = build_mame(args.mame_dir)
        elif src == "zxdb":
            store = build_zxdb(args.zxdb_sql)
        else:
            raise SystemExit(f"unknown source {src!r}")
        meta[src] = {**SOURCES[src], "generated": today, "counts": _write(src, store)}
    os.makedirs(OUT, exist_ok=True)
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
