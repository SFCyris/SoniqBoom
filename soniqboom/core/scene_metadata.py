"""Scene-metadata enrichment from Modland's public MD5 index.

Modland (ftp.modland.com) hosts 500k+ scene modules in a strict
``Format/Author[/coop-Partner][/Collection]/file`` tree and publishes a
nightly, no-auth index of every file's MD5:

    https://ftp.modland.com/pub/documents/allmods_md5.zip
    → allmods_md5.txt: ``<md5><space>Format/Author/.../file`` per line

Joining a library module's ``file_md5`` (cached at scan time) against that
index yields the scene AUTHOR (and format cross-check) with byte-exact
confidence — the same shape as the HVSC Songlengths join for SID.  Per the
house rule (confidence-gated enrichment), ONLY exact-MD5 matches are ever
applied; filename fuzzy matching is deliberately not attempted.

The matched path also yields the GAME a tune belongs to, filled into an EMPTY
album only (``album_source`` provenance, see ``core/folder_album.py``):

  * the game/collection dir of ``Format/Author/<Game>/file`` (console rips) →
    ``album_source="modland"``;
  * for tracker/Amiga formats, the ``<game>-<part>`` file name (suffix form
    ``gold of the aztecs-intro.dw`` or Amiga prefix form
    ``mdat.denny-level4``) — but only when the local track's own title equals
    ``<part>``, or, for an Amiga custom-replayer format (no title in the file),
    when the local title IS that file name AND another file in the same Modland
    dir carries the same game prefix (a lone ``slip-stream`` or an
    ``ice-runner tune1``/``tune2`` pair is not split) (setting
    ``modland_filename_game``, default on) → ``"modland-filename"``.

The pass runs from the Admin "Apply" button, after every scan once an index
has been downloaded (``scanner._spawn_scene_autoapply``), and once after an
upgrade (``MODLAND_APPLY_VERSION``).  A track whose md5 drops out of a
refreshed index loses the Modland album and ``scene_path`` it got from it.

Storage: a local sqlite DB (stdlib, ~40 MB, indexed lookups) under
``<data_dir>/scene/modland.sqlite`` — no RAM cost at runtime.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import NamedTuple

from soniqboom.core import folder_album as fa
from soniqboom.core.retro import chip_family

log = logging.getLogger(__name__)

MODLAND_MD5_URL = "https://ftp.modland.com/pub/documents/allmods_md5.zip"
# Settings key (default ON): derive a tracker/Amiga album from a
# "<game>-<part>" Modland file name (see ``filename_game``).
MODLAND_FILENAME_CONFIG_KEY = "modland_filename_game"
# Bumped when the apply pass learns something new, so libraries enriched by an
# older build are re-applied once (persisted as config ``modland_apply_version``
# after a successful apply).  2 = game albums (GitHub #13); 3 = the file-name
# game of title-less custom replayers needs sibling files (withdraws guesses);
# 4 = a ripper-placeholder artist ("<?>") takes the Modland credit; 5 = a lone
# tracker file's name guess needs a tune-part word (withdraws guesses); 6 = the
# Modland game name is recorded also where it is not the album
# (``game_by_modland`` — ``game:`` searches it); 7 = a file-name guess takes the
# game-title lists' spelling ("Backtothefuture3" → "Back to the Future III",
# ``game_titles.list_spelling``).
MODLAND_APPLY_VERSION = 7
APPLY_VERSION_CONFIG_KEY = "modland_apply_version"
# A stale match (md5 no longer in the index) is withdrawn only when the index
# looks complete — a truncated download must never wipe the enrichment of
# every track (the real index has ~500k rows).
_WITHDRAW_MIN_INDEX_ROWS = 100_000
# SQLite's default host-parameter limit is 999 on older builds.
_MD5_LOOKUP_CHUNK = 900
_DOWNLOAD_TIMEOUT_S = 300

_status: dict = {
    "index_rows": 0,
    "index_built_at": 0.0,
    "refreshing": False,
    "applying": False,
    "last_apply": None,     # {"matched": n, "updated": n, "at": ts}
    "error": None,
}


def _db_path() -> Path:
    from soniqboom.config import get_data_dir
    d = get_data_dir() / "scene"
    d.mkdir(parents=True, exist_ok=True)
    return d / "modland.sqlite"


def has_index() -> bool:
    """True when a built (non-empty) index file exists."""
    try:
        return _db_path().stat().st_size > 0
    except OSError:
        return False


def _connect_ro(db: Path) -> sqlite3.Connection:
    """Read-only connection: never CREATES an empty index file (a plain
    connect() would, making "is there an index?" checks lie).  The path goes
    through ``as_uri`` so spaces / ``?`` / ``#`` in the data dir are safe."""
    return sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)


def status() -> dict:
    out = dict(_status)
    if not out["index_rows"] and has_index():
        try:
            con = _connect_ro(_db_path())
            out["index_rows"] = con.execute(
                "SELECT COUNT(*) FROM mods").fetchone()[0]
            con.close()
        except Exception:
            pass
    return out


def refresh_index() -> dict:
    """Download the nightly index and (re)build the sqlite DB.  Blocking —
    callers run it in an executor.  Returns the updated status dict."""
    import httpx
    if _status["refreshing"]:
        # Re-entrancy guard (QA): two concurrent refreshes clobber the same
        # .building/.zip/sqlite paths — reject the second.
        return {**status(), "error": "refresh already running"}
    _status.update(refreshing=True, error=None)
    tmp = _db_path().with_suffix(".building")
    try:
        log.info("Modland index: downloading %s", MODLAND_MD5_URL)
        blob = None
        last_exc: Exception | None = None
        for _attempt in range(3):          # ftp.modland.com can be flaky
            try:
                with httpx.Client(timeout=_DOWNLOAD_TIMEOUT_S,
                                  follow_redirects=True) as client:
                    resp = client.get(MODLAND_MD5_URL)
                    resp.raise_for_status()
                    blob = resp.content
                break
            except Exception as exc:
                last_exc = exc
                time.sleep(2 * (_attempt + 1))
        if blob is None:
            raise RuntimeError(f"download failed after 3 attempts: {last_exc}")
        zpath = _db_path().with_suffix(".zip")
        zpath.write_bytes(blob)
        rows = 0
        tmp.unlink(missing_ok=True)
        con = sqlite3.connect(tmp)
        con.execute("CREATE TABLE mods (md5 TEXT PRIMARY KEY, path TEXT)")
        with zipfile.ZipFile(zpath) as zf:
            name = zf.namelist()[0]
            with zf.open(name) as fh:
                batch = []
                for raw in fh:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    md5, sep, path = line.partition(" ")
                    if sep and len(md5) == 32:
                        batch.append((md5.lower(), path))
                        rows += 1
                        if len(batch) >= 20000:
                            con.executemany(
                                "INSERT OR REPLACE INTO mods VALUES (?,?)",
                                batch)
                            batch.clear()
                if batch:
                    con.executemany(
                        "INSERT OR REPLACE INTO mods VALUES (?,?)", batch)
        con.commit()
        con.close()
        zpath.unlink(missing_ok=True)
        tmp.replace(_db_path())
        _status.update(index_rows=rows, index_built_at=time.time())
        log.info("Modland index built: %d rows", rows)
    except Exception as exc:
        _status["error"] = f"index refresh failed: {exc}"
        log.warning("Modland index refresh failed: %s", exc)
        tmp.unlink(missing_ok=True)
    finally:
        _status["refreshing"] = False
    return status()


# ── Modland path grammar ─────────────────────────────────────────────────────
#
# Empirically (504,523-row index, 2026-07): paths are
#   Format/Author/file                                   231,755 rows
#   Format/Author/<X>/file                               196,179 rows
# where <X> is a ``coop-Partner`` credit, a ``not by …`` disclaimer under
# ``- unknown``, OR the GAME / collection the tune belongs to (console rips —
# Nintendo SPC, NDS/GBA/PS1/PS2/N64/Saturn/Dreamcast sound formats, MDX, FMP,
# S98, PMD, MoonBlaster …), and deeper paths are ``Author/coop-X/Game/file`` or
# the mirrored ``HVSC/…`` tree.  Three families nest a SUB-FORMAT / platform
# level first ("Ad Lib/EdLib Packed/…", "Spectrum/Pro Tracker 2/…", "Video
# Game Music/Sega Megadrive/…"), so their author sits one level deeper
# (checked by listing their second-level dirs — all format/platform names;
# every other top dir's second level is authors):
_TWO_LEVEL_FORMAT_DIRS = frozenset({"Ad Lib", "Spectrum", "Video Game Music"})
# Mirrored foreign trees — no Format/Author semantics at all.
_MIRROR_DIRS = frozenset({"HVSC"})
_UNKNOWN_AUTHORS = frozenset({
    "- unknown", "-unknown", "unknown", "- unkown", "unknown_composer",
    "unknown composer", "unknown artist",
})
# "coop-X" (6,312 rows), "coop - X" (28), "coop X" (21), "coop-&X" (3).
_COOP_RE = re.compile(r"^coop(?:\s*-\s*|\s+)&?\s*(.+)$", re.IGNORECASE)
_NOT_BY_RE = re.compile(r"^not by\b", re.IGNORECASE)
# <X> dirs that are containers, not a game/collection name (instrument/sample
# companion dirs of IFF-SMUS / Delitracker Custom / Zoundmonitor, catch-alls).
_GENERIC_GAME_DIRS = frozenset({
    "instruments", "instrument", "instr", "samples", "sample", "smp", "various",
    "mixed", "misc", "games", "demos", "collection", "unknown", "unnamed",
    "untitled", "songs", "sounds", "sfx", "unused",
})
# Per format family: <X> dirs that name the music TOOL the tunes were made with
# (SNDH files its tunes by replay routine: ``SNDH/<Author>/DMA/…``), not a
# game.  Format-scoped: "Quartet" IS a game under MDX / VGM / FMP.  Compared on
# the name with underscores as spaces, lower-cased, whitespace collapsed.
_TOOL_GAME_DIRS_BY_FORMAT = {
    "SNDH": frozenset({"dma", "x32", "music studio", "tsd ste", "quartet", "stos"}),
}
# "B.O.B", "W.I.T.C.H" — a dotted acronym is one word, not single letters.
_DOTTED_ACRONYM_RE = re.compile(r"^(?:[^\W_]\.){2,}[^\W_]?\.?$")
# Amiga prefix-form tokens used when uade's player list is unavailable.
_PREFIX_FALLBACK = frozenset({"mdat", "cust", "jpn", "jpnd"})
# Title case for Modland's all-lowercase names: these stay lowercase unless
# they open the name.
_SMALL_WORDS = frozenset({"of", "the", "and", "a", "in", "on", "to"})
# Platform / chipset abbreviations that are written in capitals
# ("sleepwalker (pc)" → "Sleepwalker (PC)", "dennis aga" → "Dennis AGA").
_UPPER_TOKENS = frozenset({
    "pc", "st", "ste", "nes", "snes", "gb", "gba", "gbc", "n64", "psx", "ps1",
    "ps2", "c64", "msx", "msx2", "pce", "cpc", "dos", "aga", "ecs", "ocs",
    "ost", "cd", "cd32", "cdtv", "ntsc", "pal", "fm", "sid", "opl", "ym",
    "x68k", "vgm", "ufo",
})
_ROMAN_RE = re.compile(r"^(?=[ivx]+$)x{0,3}(?:ix|iv|v?i{0,3})$")
# A trailing alternate-version / backup token on a dir or file stem
# ("WELCOME.alt", "welcome.alt2", "tune.bak").
_ALT_SUFFIX_RE = re.compile(r"\.(?:alt\d*|bak)$", re.IGNORECASE)


class ModlandPath(NamedTuple):
    """A Modland tree path split into its parts (see the grammar above)."""
    fmt_dirs: tuple[str, ...]      # ("Protracker",) or ("Ad Lib", "EdLib D00")
    author: str | None             # None for "- unknown" / mirror trees
    partners: tuple[str, ...]      # coop credits directly under the author
    game: str | None               # the game/collection dir, raw spelling
    filename: str


def _alnum_key(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.lower())


def _is_game_dir(seg: str, filename: str) -> bool:
    """Is Modland dir ``seg`` a game/collection name (vs a container word, a
    credit, a tool name, or a per-module dir that merely repeats the file's
    own name)?"""
    s = seg.strip()
    low = s.lower()
    if (not s or low in _GENERIC_GAME_DIRS or low in _UNKNOWN_AUTHORS
            or _NOT_BY_RE.match(s) or _COOP_RE.match(s)):
        return False
    # "- Misc", "Covers", "DISK", "Unknown Demo", "Digit Tracker", "Vgm", "SID"
    bare = low.lstrip("- ").strip()
    words = ([bare.replace(".", "")] if _DOTTED_ACRONYM_RE.match(bare)
             else re.findall(r"[^\W_]+", bare))
    if (bare in fa._GENERIC_NAMES or fa._PART_RE.match(bare)
            or (words and words[-1] in fa._TOOL_WORDS)
            or (words and all(w in fa._GENERIC_NAMES or len(w) == 1
                              for w in words))):
        return False
    if sum(c.isalnum() for c in s) < 3:
        return False                    # "SC", "FC", "ks" — cryptic codes
    # "WELCOME.alt/WELCOME.smus", "x/the sign of the death.alt.smus": an
    # alternate-version token is not part of the module's name.
    key = _alnum_key(_ALT_SUFFIX_RE.sub("", s))
    name = filename.strip()
    stem = name.rsplit(".", 1)[0] if "." in name else name
    body = name.split(".", 1)[1] if "." in name else name   # Amiga prefix form
    own = {_alnum_key(name), _alnum_key(stem), _alnum_key(body),
           _alnum_key(_ALT_SUFFIX_RE.sub("", stem))}
    ms = _modland_stem(name)
    if ms:
        own.add(_alnum_key(ms))
        own.add(_alnum_key(_ALT_SUFFIX_RE.sub("", ms)))
    # "Delitracker Custom/TSM/melody/cust.melody" — a per-module dir.
    return key not in own


_music_exts_memo: frozenset[str] | None = None


def _music_exts() -> frozenset[str]:
    global _music_exts_memo
    if _music_exts_memo is None:
        try:
            from soniqboom.core.metadata import FORMAT_NAMES
            _music_exts_memo = frozenset(k.lower().lstrip(".") for k in FORMAT_NAMES)
        except Exception:                       # noqa: BLE001
            _music_exts_memo = frozenset()
    return _music_exts_memo


def _modland_stem(filename: str) -> str | None:
    """The tune-name part of a Modland file name, or None for an Amiga
    sample/instrument companion (``smpl.X``, ``X.ins`` …), which never names
    a game.

    Suffix form ``gold of the aztecs-intro.dw`` → ``gold of the aztecs-intro``;
    Amiga prefix form (the replayer token first, as uade classifies it)
    ``mdat.denny-level4`` → ``denny-level4``.  A name whose first segment only
    looks like a replayer token but ends in a real music extension
    (``ice.mod``, ``one.xm``) is the suffix form."""
    name = filename.strip()
    if "." not in name:
        return name
    head, body = name.split(".", 1)
    first = head.lower()
    prefix = False
    try:
        from soniqboom.core import uade_formats as uf
        if uf.is_companion_half(name):
            return None
        if uf.player_map():
            hit = uf.classify(name)
            prefix = hit is not None and hit[1] == first
        else:
            prefix = first in _PREFIX_FALLBACK
    except Exception:                           # noqa: BLE001
        prefix = first in _PREFIX_FALLBACK
    if prefix and name.rsplit(".", 1)[1].lower() not in _music_exts():
        return body.rstrip(". ")
    return name.rsplit(".", 1)[0].rstrip(". ")


def _split_path(p: str) -> tuple[tuple[str, ...], str | None, list[str],
                                 list[str], str]:
    """``(fmt_dirs, author, partners, tail_dirs, filename)`` — the credit part
    of the grammar, shared by ``parse_modland_path`` and the name-set scan."""
    parts = p.split("/")
    filename = parts[-1]
    dirs = parts[:-1]
    if not dirs or dirs[0] in _MIRROR_DIRS:
        return tuple(dirs[:1]), None, [], [], filename
    k = 2 if dirs[0] in _TWO_LEVEL_FORMAT_DIRS else 1
    rest = dirs[k:]
    if not rest:
        return tuple(dirs[:k]), None, [], [], filename
    author: str | None = rest[0]
    if author.strip().lower() in _UNKNOWN_AUTHORS:
        author = None
    tail = rest[1:]
    partners: list[str] = []
    while tail:
        m = _COOP_RE.match(tail[0].strip())
        if not m:
            break
        partners.append(m.group(1).strip())
        tail = tail[1:]
    return tuple(dirs[:k]), author, partners, tail, filename


def parse_modland_path(p: str) -> ModlandPath:
    """Split ``Format[/SubFormat]/Author[/coop-X…][/Game]/file``.

    ``game`` is set only when EXACTLY one non-credit dir follows the author
    (deeper tails — ``Demos/Best_In_Galaxy``, ``Sorcerian …/EX3`` — are
    ambiguous between category, game and part, so they are refused)."""
    fmt_dirs, author, partners, tail, filename = _split_path(p)
    game = tail[0] if len(tail) == 1 and _is_game_dir(tail[0], filename) else None
    if game and fmt_dirs:
        tools = _TOOL_GAME_DIRS_BY_FORMAT.get(fmt_dirs[0])
        if tools and " ".join(game.replace("_", " ").lower().split()) in tools:
            game = None                 # "SNDH/<Author>/DMA/…" — a replay tool
    return ModlandPath(fmt_dirs, author, tuple(partners), game, filename)


def _parse_modland_path(p: str) -> tuple[str, str | None]:
    """(format_dir, author credit) — kept for callers of the old helper.

    ``- unknown`` authors return None; ``coop-X`` subdirs append the partner.
    For the nested families (``Ad Lib/EdLib Packed/Drax/…``) the format dir is
    the joined ``"Ad Lib/EdLib Packed"`` and the author is ``Drax``."""
    mp = parse_modland_path(p)
    return "/".join(mp.fmt_dirs), author_credit(mp)


def author_credit(mp: ModlandPath) -> str | None:
    if not mp.author:
        return None
    return " & ".join([mp.author, *mp.partners])


def smart_title(s: str) -> str:
    """Title-case an ALL-LOWERCASE Modland name (``gold of the aztecs`` →
    ``Gold of the Aztecs``); a name with any capital is Modland's own casing and
    is returned unchanged.  Small words stay lowercase unless first; Roman
    numerals and platform tokens are upper-cased (``mortal kombat ii`` →
    ``Mortal Kombat II``, ``(pc)`` → ``(PC)``)."""
    if s != s.lower():
        return s
    out = []
    first = True
    for w in s.split():
        core = w.strip("()[]{},.:;!?'\"")
        if not first and w in _SMALL_WORDS:
            out.append(w)
        elif _ROMAN_RE.match(w) or core in _UPPER_TOKENS:
            out.append(w.upper())
        else:
            # Upper-case the first ALPHANUMERIC char when it is a letter —
            # "(unused)" → "(Unused)", but "2ndsamurai"/"7colors" stay as-is
            # (no "2Ndsamurai").
            j = next((n for n, c in enumerate(w) if c.isalnum()), None)
            if j is not None and w[j].isalpha():
                w = w[:j] + w[j].upper() + w[j + 1:]
            out.append(w)
        # A sub-title after " - " or "x:" starts a new phrase.
        first = w == "-" or w.endswith(":")
    return " ".join(out)


def game_album_name(game_dir: str) -> str:
    """Display form of a Modland game dir: ``Songs_That_Make_U_Go_Mmh2`` →
    ``Songs That Make U Go Mmh2``; whitespace collapsed; lowercase names
    title-cased."""
    s = game_dir.strip()
    if " " not in s:
        s = s.replace("_", " ")
    return smart_title(" ".join(s.split()))


def _title_key(s: str) -> str:
    """Case/space-insensitive comparison key for the filename-game gate."""
    return re.sub(r"[\s_]+", "", (s or "").lower())


def _game_part(filename: str) -> tuple[str, str, str] | None:
    """``(game, part, stem)`` of a one-dash ``<game>-<part>`` Modland file name
    (suffix or Amiga prefix form, see ``_modland_stem``), both halves
    non-empty — else None (no dash, several dashes, a sample companion)."""
    name = filename.strip()
    # Cheap pre-check: both candidate stems (suffix form: before the last dot;
    # prefix form: after the first) must hold exactly one dash, or there is no
    # game to find — skips the prefix classification for most names.
    if not any(c.count("-") == 1 for c in (name.rsplit(".", 1)[0], name.split(".", 1)[-1])):
        return None
    stem = _modland_stem(name)
    if not stem or stem.count("-") != 1:
        return None
    game, part = (x.strip() for x in stem.split("-"))
    if not game or not part:
        return None
    return game, part, stem


_FIRST_WORD_RE = re.compile(r"[^\W_]+")
# Words that open the <part> of a game's tunes ("cannon fodder-ingame 3",
# "chaos engine-world 2") — siblings sharing one of these still split at the
# dash; any other shared first word ("ice-runner tune1", "zero-g demo 2") means
# the dash is inside the name.
_PART_WORDS = frozenset({
    "ingame", "level", "lvl", "stage", "world", "zone", "act", "round", "area",
    "title", "intro", "outro", "end", "ending", "menu", "highscore", "hiscore",
    "gameover", "loader", "boss", "theme", "jingle", "sfx", "tune", "song",
    "music", "part", "track", "win", "lose",
})


def _first_word(s: str) -> str:
    m = _FIRST_WORD_RE.search(s.lower())
    return m.group(0) if m else ""


def _sibling_key(modland_dir: str, game: str) -> str:
    return f"{modland_dir}\0{_title_key(game)}"


def filename_game(filename: str, local_title: str, *,
                  title_is_filename: bool = False,
                  siblings: "dict[str, bool] | None" = None,
                  modland_dir: str | None = None) -> str | None:
    """The game in a ``<game>-<part>`` Modland file name (suffix form
    ``<game>-<part>.ext`` or Amiga prefix form ``mdat.<game>-<part>``, see
    ``_modland_stem``) — returned ONLY when the local track's title equals
    ``<part>`` (case/space-insensitive).

    That equality is the confidence gate: it proves the part after the dash is
    this tune's own name, so the part before it is something else (the game).
    Without it ``4-mat``/``pop-corn`` style names would be split into nonsense.

    ``title_is_filename`` (the caller's claim that the format carries no title
    of its own — Amiga custom replayers) also accepts a local title equal to
    the whole ``<game>-<part>`` stem, but only with SIBLING evidence, since
    such a title proves nothing about where the name splits: ``siblings``
    (``modland_filename_siblings()``) must list at least two files in the same
    Modland dir (``modland_dir``) with this game prefix, and when the dash is
    unspaced and every one of those files' parts starts with the same word
    (``ice-runner tune1`` / ``ice-runner tune2``) the dash is inside the name
    and the guess is refused — unless that word is a usual tune-part word
    (``cannon fodder-ingame 1`` … ``5``, ``_PART_WORDS``).  Without
    ``siblings`` that path is refused.

    On the ``<part>`` path, a LONE file — ``siblings`` given, but no other
    file in its Modland dir with this game prefix — is accepted only when
    ``<part>`` opens with such a tune-part word (``huckleberry-title``,
    ``the hulk-end theme``): a lone ``fleetwood mac-dreams`` or
    ``i01-experience 1`` is not a game.  Without ``siblings`` (a caller with
    no Modland index) the title gate alone decides.

    Exactly one ``-`` in the stem, a game of ≥3 alphanumerics with balanced
    brackets (a dash inside ``(…)``/``[…]``/``{…}`` splits nothing) that is
    neither just a number nor a container / album-part word, and a part
    different from the game are also required.  Never a game for a sample
    companion (``smpl.*``)."""
    split = _game_part(filename)
    if split is None or not local_title:
        return None
    game, part, stem = split
    have_map = siblings is not None and modland_dir is not None
    # The game's sibling group: None = a lone file, else whether every part
    # in it opens with the same word.
    shared_first_word = siblings.get(_sibling_key(modland_dir, game)) if have_map else None
    part_word = _first_word(part).rstrip("0123456789") in _PART_WORDS
    if _title_key(part) != _title_key(local_title):
        if not (title_is_filename and _title_key(stem) == _title_key(local_title)):
            return None
        if shared_first_word is None:
            return None                     # a lone file / no map: nothing shows the split
        if (shared_first_word and " -" not in stem and "- " not in stem
                and not part_word):
            return None                     # "ice-runner tune1/tune2": one name
    elif have_map and shared_first_word is None and not part_word:
        return None                         # a lone "fleetwood mac-dreams": no game
    if (game.count("(") != game.count(")") or game.count("[") != game.count("]")
            or game.count("{") != game.count("}")):
        return None                         # "Music History (Commando -2)"
    if sum(c.isalnum() for c in game) < 3 or _alnum_key(game).isdigit():
        return None
    if _title_key(game) == _title_key(part):
        return None
    low = " ".join(game.lower().split())
    if low in fa._GENERIC_NAMES or fa._PART_RE.match(low):
        return None
    return smart_title(" ".join(game.split()))


def _filename_game_eligible(t: dict) -> bool:
    """Tracker / Amiga formats only (the ``<game>-<part>`` naming is Modland's
    convention for Amiga custom-replayer and tracker rips)."""
    if chip_family(t.get("format")) in ("paula", "tracker", "ahx"):
        return True
    return "Amiga" in (t.get("genre") or [])


def modland_game_for(t: dict, mp: ModlandPath, *,
                     use_filename: bool = True,
                     siblings: "dict[str, bool] | None" = None,
                     modland_dir: str | None = None) -> tuple[str | None, str | None]:
    """``(game name, album source)`` an exact Modland match gives track ``t``:
    the game folder's name (``fa.SOURCE_MODLAND``), else — option on, an
    eligible format — the file name's game (``fa.SOURCE_MODLAND_FILENAME``),
    spelled as the game-title lists spell it when they know it
    (``game_titles.list_spelling``: "Backtothefuture3" → "Back to the Future
    III"), else ``(None, None)``.  Whether it becomes the album is
    ``album_update_for``'s call; it is the track's Modland game name either
    way (``TrackMeta.game_by_modland`` / ``_modland_filename``).  Blocking the
    first time (the title lists load) — ``collect_updates`` runs in an executor."""
    if mp.game:
        return game_album_name(mp.game), fa.SOURCE_MODLAND
    if use_filename and _filename_game_eligible(t):
        # Amiga custom replayers carry no title, so theirs came from the file
        # name; tracker modules name themselves (their title must be <part>).
        g = filename_game(mp.filename, t.get("title") or "",
                          title_is_filename=chip_family(t.get("format")) == "paula",
                          siblings=siblings, modland_dir=modland_dir)
        # "Protracker/Tiger/tiger-tekknostuff.mod", "detio - lost in a
        # dream.mod": the prefix is the COMPOSER, not a game — refuse.
        gw = fa._words(g) if g else frozenset()
        if gw and any(gw <= fa._words(who) for who in
                      (mp.author or "", *mp.partners, t.get("artist") or "") if who):
            g = None
        if g:
            from soniqboom.core import game_titles
            return game_titles.list_spelling(g, t.get("format")) or g, fa.SOURCE_MODLAND_FILENAME
    return None, None


def modland_game_slots(want: str | None, want_src: str | None) -> dict:
    """The per-source game names (``game_by_modland`` /
    ``game_by_modland_filename``) of ``modland_game_for``'s answer."""
    return {"game_by_modland": want if want_src == fa.SOURCE_MODLAND else None,
            "game_by_modland_filename": (want if want_src == fa.SOURCE_MODLAND_FILENAME
                                         else None)}


def album_update_for(t: dict, mp: ModlandPath, *,
                     use_filename: bool = True,
                     siblings: "dict[str, bool] | None" = None,
                     modland_dir: str | None = None,
                     want: tuple[str | None, str | None] | None = None) -> dict | None:
    """The album patch an exact Modland match implies for track ``t`` — or None.

    Fill-only: a user-edited album (``user_edited``) is never touched, and an
    album the file itself carries (``album_source`` None/"tag") is kept —
    except that an SPC game name its 32-byte ID666 field cut off
    (``album_source`` "tag", see ``fa.spc_name_completes``) is completed by the
    exact-matched Modland game dir and re-stamped as "modland".  An empty
    album is filled; a ``"folder"`` album (the weakest source) is upgraded; an
    album this pass stamped earlier is re-stamped when the derivation changed
    and withdrawn when it no longer derives (e.g. the file-name option was
    switched off).  ``siblings`` / ``modland_dir`` (the matched path's dir)
    feed ``filename_game``'s sibling gate for title-less custom replayers."""
    if fa.album_edit_locked(t):
        return None
    cur = (t.get("album") or "").strip()
    src = t.get("album_source")
    want, want_src = want if want is not None else modland_game_for(
        t, mp, use_filename=use_filename, siblings=siblings, modland_dir=modland_dir)
    ours = src in (fa.SOURCE_MODLAND, fa.SOURCE_MODLAND_FILENAME)
    if (want and src == fa.SOURCE_TAG and want_src == fa.SOURCE_MODLAND
            and fa.spc_name_completes(want, cur, t.get("format"))):
        # The SPC ID666 game field is 32 bytes: a longer name was cut off, and
        # the Modland game dir of the SAME file (exact md5) completes it.
        # (Re-extracts and rescans keep the completion — same rule.)
        return {"album": want, "album_source": fa.SOURCE_MODLAND}
    if want:
        if (not cur or src == fa.SOURCE_FOLDER
                or (ours and (cur != want or src != want_src))):
            return {"album": want, "album_source": want_src}
        return None
    if ours and cur:
        # A file-name album whose gate no longer holds only because the USER
        # retitled the track stays (the game didn't change); it is withdrawn
        # when the option is off or the derivation genuinely went away.
        if (src == fa.SOURCE_MODLAND_FILENAME and use_filename
                and fa.field_locked(t, "title")):
            return None
        return _header_album_back(t)
    return None


def _header_album_back(t: dict) -> dict:
    """The album patch withdrawing a Modland album (``fa.header_album_back``)."""
    return fa.header_album_back(t)


def filename_game_enabled() -> bool:
    from soniqboom.core.store import get_store
    return bool(get_store().get_config(MODLAND_FILENAME_CONFIG_KEY, True))


def _legacy_credit(p: str, fmt_name: str) -> str | None:
    """The artist credit the PRE-#13 parser wrote for Modland path ``p`` (it
    took a two-level family's sub-format as the author, and only knew the
    ``coop-X`` spelling).  Used solely to recognise — and correct — credits
    that the old apply pass stamped."""
    parts = p.split("/")
    if len(parts) < 3:
        return None
    fmt_dir, author = parts[0], parts[1]
    if author.strip().lower() in ("- unknown", "-unknown", "unknown"):
        return None
    for seg in parts[2:-1]:
        if seg.startswith("coop-"):
            author = f"{author} & {seg[5:]}"
    if author.strip().lower() in (fmt_name, fmt_dir.strip().lower()):
        return None
    return author


def collect_updates(*, use_filename: bool | None = None,
                    tracks: list[dict] | None = None,
                    matched_ids: set | None = None,
                    ) -> tuple[int, list[tuple[str, dict]], dict]:
    """Join every ``file_md5``-carrying track against the index; return
    ``(matched, batch, expect)`` WITHOUT touching the store.

    ``tracks`` is the snapshot to join (default: the store's tracks, taken
    here).  The md5s are looked up in chunked ``IN (…)`` queries, then the
    per-track logic runs against that dict.  ``matched_ids`` (optional)
    collects the ids of the matched tracks.

    ``expect`` maps a track id to the values (``album``/``album_source``/
    ``artist``) its guarded patch fields were computed from, plus the
    ``file_md5`` the join matched — the write re-checks them on the loop
    thread (``folder_album.commit_album_updates``), so an edit or rescan that
    lands in between is never overwritten (a different file drops the whole
    patch), and a field the user hand-edited (``user_edited``) is never
    written.

    Artist: an EMPTY artist (or a ripper placeholder such as ``<?>``) is
    filled with the Modland credit; an artist that
    is exactly the wrong credit the pre-#13 parser stamped (a sub-format name
    such as "EdLib Packed & Metal", a coop credit it truncated, an "Unknown
    Artist" dir) is corrected — only ever to a real credit, or cleared for the
    unknown-author placeholder.

    A track whose md5 is no longer in the index (a refreshed index dropped or
    renamed it, or the file changed) loses its ``scene_path`` and the Modland
    album it carried (never a user-edited one) — only when the index has at
    least ``_WITHDRAW_MIN_INDEX_ROWS`` rows, so a truncated index can't wipe
    the library's enrichment.  Such tracks don't count as ``matched``.

    Blocking (sqlite lookups) — run in an executor.  The store WRITE must
    happen on the event-loop thread (QA MAJOR-2: the store has no lock;
    every other batch writer mutates only on the loop thread, so a
    cross-thread write racing an active scan could corrupt indexes).
    """
    db = _db_path()
    if not has_index():
        raise RuntimeError("no Modland index — refresh it first")
    from soniqboom.core.store import get_store
    if use_filename is None:
        use_filename = filename_game_enabled()
    if tracks is None:
        tracks = get_store().all_tracks()      # list[dict] (shallow refs)
    # The file-name gate's sibling evidence (cached per index file).
    siblings = modland_filename_siblings() if use_filename else None
    wanted = list({m.lower() for m in (t.get("file_md5") for t in tracks)
                   if isinstance(m, str) and m})
    rows: dict[str, str] = {}
    con = _connect_ro(db)
    try:
        for i in range(0, len(wanted), _MD5_LOOKUP_CHUNK):
            chunk = wanted[i:i + _MD5_LOOKUP_CHUNK]
            rows.update(con.execute(
                f"SELECT md5, path FROM mods WHERE md5 IN ({','.join('?' * len(chunk))})",
                chunk).fetchall())
        withdraw_ok = (con.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
                       >= _WITHDRAW_MIN_INDEX_ROWS)
    finally:
        con.close()
    matched = 0
    batch: list[tuple[str, dict]] = []
    expect: dict[str, dict] = {}
    for t in tracks:
        md5 = t.get("file_md5")
        if not isinstance(md5, str) or not md5:
            continue
        path = rows.get(md5.lower())
        if path is None:
            if withdraw_ok:
                _withdraw_stale(t, batch, expect)
            continue
        matched += 1
        if matched_ids is not None:
            matched_ids.add(t["id"])
        updates: dict = {}
        # Scene provenance — always stored on an exact match; the
        # track-info modal shows it as "Scene origin".
        if t.get("scene_path") != path:
            updates["scene_path"] = path
        mp = parse_modland_path(path)
        author = author_credit(mp)
        # Tree-artefact guard: an "author" that merely repeats a SUB-format
        # level, or the track's format when that format is not itself named
        # after the author.  (Composer-named replayers — "David Whittaker/
        # David Whittaker/…", "Rob Hubbard/Rob Hubbard/…" — ARE credits.)
        fmt_name = (t.get("format") or "").strip().lower()
        a_low = (mp.author or "").strip().lower()
        artefact = (a_low in {d.strip().lower() for d in mp.fmt_dirs[1:]}
                    or (a_low == fmt_name
                        and a_low != mp.fmt_dirs[0].strip().lower()))
        credit = author if author and not artefact else None
        cur_artist = (t.get("artist") or "").strip()
        if cur_artist.lower() in fa._ARTIST_PLACEHOLDERS:
            cur_artist = ""             # a ripper placeholder ("<?>") names nobody
        exp: dict = {}
        if not fa.field_locked(t, "artist"):
            if credit and not cur_artist:
                updates["artist"] = credit
            elif cur_artist and cur_artist != (credit or ""):
                legacy = _legacy_credit(path, fmt_name)
                if legacy and cur_artist == legacy.strip():
                    if credit:
                        updates["artist"] = credit
                    elif legacy.strip().lower() in _UNKNOWN_AUTHORS:
                        updates["artist"] = ""      # "Unknown Artist" dir
                    # else: no better credit (e.g. a composer-named
                    # sub-format over "- unknown") — keep what's there.
            if "artist" in updates:
                exp["artist"] = t.get("artist")
        want = modland_game_for(t, mp, use_filename=use_filename, siblings=siblings,
                                modland_dir=path.rpartition("/")[0])
        alb = album_update_for(t, mp, use_filename=use_filename, siblings=siblings,
                               modland_dir=path.rpartition("/")[0], want=want)
        if alb is not None:
            updates.update(alb)
            exp["album"] = t.get("album")
            exp["album_source"] = t.get("album_source")
        # The Modland game name — recorded whether or not it is the album (a
        # header game or a typed album keeps that), so ``game:`` finds it.
        for f, v in modland_game_slots(*want).items():
            if (t.get(f) or None) != v:
                updates[f] = v
        if updates:
            batch.append((t["id"], updates))
            # Identity precondition: a rescan that swapped in a different file
            # meanwhile drops the whole patch (``folder_album._guard_item``).
            exp["file_md5"] = md5
            expect[t["id"]] = exp
    return matched, batch, expect


def _withdraw_stale(t: dict, batch: list, expect: dict) -> None:
    """Patch for a track whose md5 has no index row any more: drop its
    ``scene_path`` and the Modland album that came with it (compare-and-set,
    never a user-edited album)."""
    upd: dict = {}
    exp: dict = {}
    if t.get("scene_path"):
        upd["scene_path"] = None
    for f in ("game_by_modland", "game_by_modland_filename"):
        if t.get(f):
            upd[f] = None                      # its Modland game name goes too
    if (t.get("album_source") in (fa.SOURCE_MODLAND, fa.SOURCE_MODLAND_FILENAME)
            and (t.get("album") or "").strip() and not fa.album_edit_locked(t)):
        upd.update(_header_album_back(t))
        exp.update(album=t.get("album"), album_source=t.get("album_source"))
    if upd:
        batch.append((t["id"], upd))
        exp["file_md5"] = t.get("file_md5")
        expect[t["id"]] = exp


# The last auto apply's position in the store's enrichment change log and its
# inputs (index signature, file-name option, game-title lists) —
# ``enrich_delta.record``: the next auto apply joins only the tracks written
# since, all of them when an input changed (None: join everything).
_last_auto_sig: tuple | None = None
# The ids of the tracks the index matches, as of the last apply (a delta apply
# updates its own tracks), so the status counts the whole library.
_matched_ids: set[str] = set()


def _index_sig() -> tuple | None:
    try:
        st = _db_path().stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def apply_version_current() -> bool:
    """True once a successful apply has run under ``MODLAND_APPLY_VERSION``."""
    from soniqboom.core.store import get_store
    try:
        return int(get_store().get_config(APPLY_VERSION_CONFIG_KEY, 0) or 0) >= MODLAND_APPLY_VERSION
    except (TypeError, ValueError):
        return False


async def apply_to_library(*, auto: bool = False) -> dict:
    """Async apply: sqlite JOIN in an executor, store WRITE on the loop
    thread (mirrors ``smart._do_dup_recompute``'s pattern).  Invalidates the
    album/browse caches once when anything changed.

    The write runs under the folder-album lock and re-reads the file-name
    option there, so switching the option while the join runs can't leave a
    guess behind (off) or withdraw guesses the switch-on is about to write;
    a switch-on mid-join also queues one more (auto) apply, which writes the
    guesses this join did not compute.

    ``auto`` (the post-scan / post-upgrade runner) joins only the tracks
    written since the last auto apply (``enrich_delta``: the join is per
    track — the sibling evidence comes from the index) and skips the join
    when there are none; it joins every track when the index, the file-name
    option or the title lists changed, or ``MODLAND_APPLY_VERSION`` is not
    applied yet.  The Admin button always joins every track.  A successful
    apply records ``MODLAND_APPLY_VERSION``.  When albums were withdrawn, the
    folder-album pass (if enabled) runs so the freed albums can take a folder
    name."""
    import asyncio
    global _last_auto_sig, _matched_ids
    if _status["applying"]:
        return {**status(), "error": "apply already running"}
    from soniqboom.core import enrich_delta
    from soniqboom.core.store import get_store
    store = get_store()
    use_filename = filename_game_enabled()              # read on the loop
    from soniqboom.core import game_titles
    lists = game_titles._lists_sig()                    # file-name guesses take their spelling
    inputs = (_index_sig(), use_filename, lists)
    changed = (enrich_delta.changes(store, _last_auto_sig, inputs)
               if auto and apply_version_current() else None)
    if changed is not None and not changed:
        return {**status(), "skipped": "unchanged"}
    _status.update(applying=True, error=None)
    freed = False
    rerun = False
    try:
        loop = asyncio.get_running_loop()
        snap = store.enrich_cursor()                    # taken with the snapshot
        tracks = (store.all_tracks() if changed is None
                  else enrich_delta.tracks_of(store, changed))
        found: set[str] = set()
        matched, batch, expect = await loop.run_in_executor(
            None, lambda: collect_updates(use_filename=use_filename, tracks=tracks,
                                          matched_ids=found))
        async with fa._get_lock():
            now_filename = filename_game_enabled()
            if now_filename != use_filename:
                batch = _drop_stale_filename_patches(batch, expect, now_filename)
                rerun = now_filename
            written: list[str] = []
            updated, albums, own_bumps = await fa._commit_album_updates(
                [(tid, upd, expect.get(tid)) for tid, upd in batch], written=written)
            freed = any(upd.get("album") == "" for _tid, upd in batch)
        if updated:
            await fa.refresh_album_caches(written)
        if store.get_config(APPLY_VERSION_CONFIG_KEY) != MODLAND_APPLY_VERSION:
            store.set_config(APPLY_VERSION_CONFIG_KEY, MODLAND_APPLY_VERSION)
        # Past our own writes when nothing else wrote meanwhile; else the next
        # auto apply re-joins what was written since the snapshot.
        _last_auto_sig = enrich_delta.record(store, snap, own_bumps, inputs)
        if changed is None:
            _matched_ids = found
        else:
            _matched_ids -= changed
            _matched_ids |= found
        _status["last_apply"] = {
            "matched": len(_matched_ids), "updated": updated, "albums": albums,
            "at": time.time()}
        log.info("Modland enrichment: %d matched, %d of %d joined track(s) updated "
                 "(%d album changes)", len(_matched_ids), updated, len(tracks), albums)
    except Exception as exc:
        _status["error"] = f"apply failed: {exc}"
        log.warning("Modland enrichment failed: %s", exc)
    finally:
        _status["applying"] = False
    if rerun:
        _last_auto_sig = None
        try:
            from soniqboom.core import scanner
            scanner._spawn_scene_autoapply()
        except Exception:                               # noqa: BLE001
            log.debug("could not queue the Modland re-apply", exc_info=True)
    elif freed and fa.enabled():
        try:
            await fa.apply_folder_albums()              # the freed tracks are in its delta
        except Exception:                               # noqa: BLE001
            log.debug("folder-album pass after Modland withdrawals failed",
                      exc_info=True)
    return status()


def _drop_stale_filename_patches(batch: list[tuple[str, dict]], expect: dict,
                                 use_filename_now: bool) -> list[tuple[str, dict]]:
    """The file-name option changed while the join ran: drop the album part
    of patches computed under the old value (other fields stay valid).

    Switched OFF → drop file-name guesses (``album_source="modland-filename"``);
    switched ON → drop withdrawals of existing file-name guesses."""
    out: list[tuple[str, dict]] = []
    for tid, upd in batch:
        if use_filename_now:
            stale = (upd.get("album") == ""
                     and (expect.get(tid) or {}).get("album_source")
                     == fa.SOURCE_MODLAND_FILENAME)
        else:
            stale = upd.get("album_source") == fa.SOURCE_MODLAND_FILENAME
        if stale:
            upd = {k: v for k, v in upd.items() if k not in ("album", "album_source")}
        if upd:
            out.append((tid, upd))
    return out


# ── Modland name sets (for the folder-album stoplist) ────────────────────────

_name_sets_cache: tuple[tuple, frozenset, frozenset] | None = None


def modland_name_keys() -> tuple[frozenset[str], frozenset[str]]:
    """``(format_dir_keys, author_keys)`` — every Modland format dir (incl. the
    sub-format level of the nested families) and every author dir, as
    ``folder_album.name_key`` keys (format dirs also in their squashed form,
    ``folder_album.format_name_keys``).  Empty sets when no index is
    downloaded.

    One full scan of the index (~0.4 s), cached per index file (size+mtime).
    Blocking — callers on the loop run it in an executor."""
    global _name_sets_cache
    name_key = fa.name_key
    db = _db_path()
    try:
        st = db.stat()
    except OSError:
        return frozenset(), frozenset()
    sig = (str(db), st.st_size, st.st_mtime)
    cached = _name_sets_cache
    if cached is not None and cached[0] == sig:
        return cached[1], cached[2]
    fmts: set[str] = set()
    authors: set[str] = set()
    con = sqlite3.connect(db)
    try:
        for (p,) in con.execute("SELECT path FROM mods"):
            fd, author, partners, _tail, _fn = _split_path(p)
            fmts.update(fd)
            if author:
                authors.add(author)
            authors.update(partners)
    finally:
        con.close()
    fk = frozenset(fa.format_name_keys(fmts))
    ak = frozenset(k for k in map(name_key, authors) if k)
    _name_sets_cache = (sig, fk, ak)
    return fk, ak


_siblings_cache: tuple[tuple, dict[str, bool]] | None = None


def modland_filename_siblings() -> dict[str, bool]:
    """``{dir + "\\0" + game key: all parts share their first word}`` for every
    Modland dir holding at least two one-dash ``<game>-<part>`` files with the
    same game prefix — ``filename_game``'s evidence that a title-less custom
    replayer's file name really splits into game and part.  Empty when no
    index is downloaded.

    One full scan of the index (~0.8 s), cached per index file (size+mtime);
    only the multi-file groups are kept (~6.5K keys for the 504K-row index).
    Blocking — callers on the loop run it in an executor."""
    global _siblings_cache
    db = _db_path()
    try:
        st = db.stat()
    except OSError:
        return {}
    sig = (str(db), st.st_size, st.st_mtime)
    cached = _siblings_cache
    if cached is not None and cached[0] == sig:
        return cached[1]
    groups: dict[str, list] = {}                # key → [count, first word | None]
    con = _connect_ro(db)
    try:
        for (p,) in con.execute("SELECT path FROM mods"):
            d, _, fn = p.rpartition("/")
            split = _game_part(fn)
            if split is None:
                continue
            key = _sibling_key(d, split[0])
            fw = _first_word(split[1])
            g = groups.get(key)
            if g is None:
                groups[key] = [1, fw]
            else:
                g[0] += 1
                if g[1] != fw:
                    g[1] = None
    finally:
        con.close()
    out = {k: g[1] is not None for k, g in groups.items() if g[0] >= 2}
    _siblings_cache = (sig, out)
    return out
