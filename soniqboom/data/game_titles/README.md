# Game-title lists

Titles of games per retro platform, matched against the names of the archives
retro tracks sit in (`soniqboom/core/game_titles.py`). One gzip TSV per
source, one `platform<TAB>title` line per title (a title may appear under
several spellings); `sources.json` holds each list's licence, origin, build
date and per-platform counts.

| File | Source | Licence | Credit |
|------|--------|---------|--------|
| `wikidata.tsv.gz` | [Wikidata](https://www.wikidata.org) — video games per platform (English labels and aliases, Japanese labels, titles) | [CC0 1.0](https://www.wikidata.org/wiki/Wikidata:Licensing) | Data from Wikidata |
| `nointro.tsv.gz` | [No-Intro](https://no-intro.org) DAT files (games only; the Software Preservation Society-credited DATs are not included) | [DAT-o-MATIC Data Usage License](https://datomatic.no-intro.org/terms.html) | No-Intro |
| `mame.tsv.gz` | [MAME software lists](https://github.com/mamedev/mame/tree/master/hash) | CC0 1.0 | MAME |
| `zxdb.tsv.gz` | [ZXDB](https://github.com/zxdb/ZXDB) — ZX Spectrum games and their aliases | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/) | Contains information from ZXDB, made available under the ODbL |

`zxdb.tsv.gz` is a derived database of ZXDB and is made available under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).

TOSEC and Redump titles are not included; they are downloaded into the data
directory (`game_titles/`) when the user clicks Download in the Metadata
settings.

Rebuild with `python3 scripts/build_game_titles.py` (Wikidata, MAME and ZXDB
are fetched; pass the No-Intro "Love Pack (DAT)" zip with `--nointro`).
