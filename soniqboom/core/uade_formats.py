"""UADE format knowledge — which files uade123 can play and what they need.

uade (Unix Amiga Delitracker Emulator) ships ~175 "eagleplayers", each
handling one exotic Amiga music format family (TFMX, Future Composer,
SidMon, Hippel, Whittaker, Hubbard, Delta Music, the ~110 ProWizard
packer variants, ...).  Its own routing table is ``eagleplayer.conf``::

    PlayerName    prefixes=tok1,tok2,...    [attribute ...]

A "prefix" token matches BOTH Amiga-style prefix naming (``mdat.song``,
``fc13.intro`` — the format tag comes FIRST) and modern suffix naming
(``intro.fc13``).  This module parses that conf once and answers, for a
bare filename, "is this a uade candidate, and via which player?".

Ownership policy (verified against the shipped conf, 2026-07-01):

* SUFFIX-form names keep their existing engine when SoniqBoom already
  handles the extension (``.mod``/``.med``/``.okt``/... stay libopenmpt,
  ``.ahx`` keeps the dedicated uade route, ``.sid`` stays sidplayfp —
  the SidMon prefix token ``sid`` therefore only ever matches the
  PREFIX form ``sid.song``, never ``song.sid``).
* PREFIX-form names always route to uade.  Today the scanner keys on
  ``Path.suffix`` only, so ``mod.lotus2`` is invisible — there is no
  regression risk, and uade's Paula emulation is the authentic engine
  for Amiga-native prefix-named files.
* Companion halves (TFMX ``smpl.X``, the ``SMP.X`` family, ``X.ins`` /
  ``X.ssd`` / ``X.bank`` / ``X.set`` / Startrekker ``X.nt`` / ``X.as``)
  are never indexed as tracks; they are fetched alongside their module
  (same-directory, same-BODY sibling rule — see
  ``companion_sibling_names``).

The authoritative playability check stays ``uade123 -g <file>`` (exit
0/1, ~110 ms) — name matching here is only the cheap prefilter, because
uade also content-detects and rejects junk that happens to match a name.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

# ── eagleplayer.conf location ────────────────────────────────────────────────

_CONF_CANDIDATES = (
    "/opt/homebrew/share/uade/eagleplayer.conf",   # macOS arm64 Homebrew
    "/usr/local/share/uade/eagleplayer.conf",      # macOS Intel Homebrew / manual
    "/usr/share/uade/eagleplayer.conf",            # Debian/Ubuntu/Fedora
    "/usr/share/uade2/eagleplayer.conf",           # older Debian naming
)


def conf_path() -> Path | None:
    """Locate eagleplayer.conf (env override first, then known prefixes)."""
    env = os.environ.get("SONIQBOOM_EAGLEPLAYER_CONF")
    if env and Path(env).is_file():
        return Path(env)
    for cand in _CONF_CANDIDATES:
        if Path(cand).is_file():
            return Path(cand)
    return None


# ── suffix-form tokens that KEEP their existing engine ───────────────────────
# Anything SoniqBoom already routes by suffix must not be re-claimed by the
# uade prefilter in suffix position.  (Prefix position is always fair game —
# those files are invisible to the suffix-keyed pipeline today.)
#
# Sourced from metadata.py ext sets; kept as bare tokens (no dots).  ahx/hvl
# keep their dedicated routes; thx (AHX clone tag) is new and may claim.
_SUFFIX_OWNED_ELSEWHERE = frozenset({
    # openmpt trackers
    "mod", "s3m", "xm", "it", "mtm", "med", "oct", "669", "dbm",
    "ult", "stm", "far", "amf", "gdm", "imf", "okt", "sfx", "wow", "dsm",
    # dedicated render routes
    "ahx", "hvl", "sid", "psid", "rsid",
    # Atari ST engines (uade's YM-2149 eagleplayer can't even play YM5 —
    # verified; StSound/psgplay/sc68 own these)
    "ym", "sndh", "sc68",
    # PSF console-rip family (zxtune owns the suffix; uade's ``psf`` token is
    # SoundFactory, still reachable in prefix form ``psf.song``)
    "psf",
    # other engines / plain audio (defensive — some uade tokens are short and
    # generic; never let a name-prefilter shadow a real audio suffix)
    "nsf", "nsfe", "spc", "gbs", "vgm", "vgz", "ay", "kss", "sap", "gym",
    "hes", "rol", "cmf", "d00", "rad", "laa", "sci", "dro", "hsc", "rix",
    "a2m", "adl", "bam", "ksm", "mid", "midi", "dsf", "dff", "wsd",
    "mp3", "flac", "ogg", "opus", "m4a", "mp4", "aac", "wav", "aiff",
    "aif", "wv", "mpc",
})

# Prefix tokens other engines keep even in PREFIX position: archived Amiga
# collections named ``mod.X`` / ``med.X`` already route through libopenmpt via
# archive.py's prefix map (appending ``.mod`` etc.) and play well there —
# re-claiming them for uade would silently change the sound of existing
# libraries.  ``hvl`` keeps the dedicated hvl2wav route; ``psid`` stays C64.
# NOTE ``sid`` is deliberately NOT here: ``sid.X`` is Amiga SidMon, never C64
# (C64 uses suffix ``.sid`` + PSID/RSID magic — disambiguated by content).
_PREFIX_OWNED_ELSEWHERE = frozenset({
    "mod", "med", "mmd", "xm", "s3m", "it", "okt", "dbm", "mtm", "stm",
    "digi", "dgi", "ptm", "hvl", "psid",
})

# ── companion halves — never tracks, always fetched with their module ────────
# Prefix-form companion tags (smpl.X etc.) and suffix-form companion
# extensions (X.ins etc.).  From uade's wanted_team player readmes:
# TFMX mdat.X+smpl.X; RJP.X+SMP.X; UFO X.mus+X.bank; PaulRobotham
# X.dat+X.ssd; the SMP. family (TimeTracker/MFP/DirkBialluch/PAP/CoSo/
# Hippel_ST/ThomasHermann/JasonPage/ScottJohnston/BladePacker/SynthDream/
# Alcatraz/Quartet/MaximumEffect/AshleyHogg); Startrekker X.nt/.as;
# shared banks SMP.set / mdtest.ssd.
COMPANION_PREFIXES = frozenset({"smpl", "smp"})
COMPANION_SUFFIXES = frozenset({"ins", "ssd", "bank", "set", "nt", "as"})
_COMPANION_LITERALS = frozenset({"smp.set", "mdtest.ssd"})


# ── conf parsing ─────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_&.!+-]*$")


def _parse_conf(text: str) -> dict[str, str]:
    """``token -> player_name`` for every prefix token in the conf."""
    mapping: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        player = fields[0]
        for field in fields[1:]:
            if not field.startswith("prefixes="):
                continue
            for tok in field[len("prefixes="):].split(","):
                tok = tok.strip().lower()
                # A couple of conf tokens carry odd chars (!pm!, m&k.) —
                # keep anything plausible, they only ever string-match.
                if tok and _TOKEN_RE.match(tok):
                    mapping[tok] = player
    return mapping


@lru_cache(maxsize=1)
def _players_cached(path_str: str, mtime_ns: int) -> dict[str, str]:
    try:
        return _parse_conf(Path(path_str).read_text(errors="replace"))
    except OSError as exc:
        log.warning("uade eagleplayer.conf unreadable (%s): %s", path_str, exc)
        return {}


_MAP_TTL_S = 5.0
_map_memo: dict = {"at": 0.0, "map": None}


def player_map() -> dict[str, str]:
    """token -> eagleplayer name, cached against the conf file's mtime.

    The mtime stat is memoised for a few seconds: this runs once per
    scanned FILE via the scanner gate, and an unconditional ``stat()``
    syscall per file dominated the gate's cost on big libraries (QA M2).
    """
    import time as _time
    now = _time.monotonic()
    if _map_memo["map"] is not None and now - _map_memo["at"] < _MAP_TTL_S:
        return _map_memo["map"]
    p = conf_path()
    if p is None:
        result: dict[str, str] = {}
    else:
        try:
            result = _players_cached(str(p), p.stat().st_mtime_ns)
        except OSError:
            result = {}
    _map_memo["map"] = result
    _map_memo["at"] = now
    return result


# ── filename classification ──────────────────────────────────────────────────

def _split_name(name: str) -> tuple[str, str]:
    """(first_segment, last_segment) of a lowercased basename, no dirs."""
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if "." not in base:
        return "", ""
    first, _, _ = base.partition(".")
    _, _, last = base.rpartition(".")
    return first, last


def is_companion_half(name: str) -> bool:
    """True for sample/instrument halves that must never appear as tracks."""
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if base in _COMPANION_LITERALS:
        return True
    first, last = _split_name(name)
    return first in COMPANION_PREFIXES or last in COMPANION_SUFFIXES


def classify(name: str) -> tuple[str, str] | None:
    """Classify a bare filename as a uade candidate.

    Returns ``(player_name, matched_token)`` when the name matches an
    eagleplayer token in prefix position, or in suffix position for a
    token no other engine owns.  ``None`` otherwise (including for
    companion halves).  This is the cheap prefilter only — callers must
    confirm with ``uade123 -g`` before treating the file as playable.
    """
    if is_companion_half(name):
        return None
    players = player_map()
    if not players:
        return None
    first, last = _split_name(name)
    # Amiga prefix form: mdat.song, fc13.intro — invisible to the suffix-keyed
    # pipeline, EXCEPT the prefixes archive.py already maps to other engines
    # (mod./med./... → libopenmpt), which stay where they are.
    if first and first in players and first not in _PREFIX_OWNED_ELSEWHERE:
        return players[first], first
    # Modland-style suffix form: intro.fc13, song.tfmxpro — only for tokens
    # nothing else claims and that aren't ubiquitous non-music suffixes
    # (README.md must never boot uade).
    if (last and last in players
            and last not in _SUFFIX_OWNED_ELSEWHERE
            and last not in _SUFFIX_REGISTRATION_DENY):
        return players[last], last
    return None


def companion_sibling_names(name: str) -> tuple[str, ...]:
    """Candidate companion filenames for a prefix-form module name.

    The eagleplayers resolve companions themselves by transforming the
    module's name (``mdat.X`` → ``smpl.X``, ``RJP.X`` → ``SMP.X``, ...)
    and asking the emulated OS for that file (same directory,
    case-insensitive).  Rather than encode every player's transform, use
    the generic same-BODY rule the pairs all follow, plus the shared
    literal banks: for ``P.X`` return ``smpl.X``/``smp.X``, for ``X.ext``
    return ``X.ins``/``X.ssd``/``X.bank``/``X.set``/``X.nt``/``X.as``,
    and always the shared ``smp.set`` / ``mdtest.ssd``.

    Callers materialising remote/archived modules should fetch any of
    these that exist next to the module (case-insensitively) — extras are
    harmless, a missing needed half fails the ``-g`` probe loudly.
    """
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in base:
        return tuple(_COMPANION_LITERALS)
    first, _, rest = base.partition(".")
    stem, _, _ = base.rpartition(".")
    players = player_map()
    prefix_form = first.lower() in players
    out: list[str] = []
    if prefix_form and rest:
        # prefix form P.X → companion prefixes with the same body X
        out += [f"{p}.{rest}" for p in sorted(COMPANION_PREFIXES)]
    elif stem:
        # suffix form X.ext → companion suffixes with the same body X
        out += [f"{stem}.{s}" for s in sorted(COMPANION_SUFFIXES)]
    out += sorted(_COMPANION_LITERALS)
    # de-dup, drop self
    seen, uniq = {base.lower()}, []
    for cand in out:
        if cand.lower() not in seen:
            seen.add(cand.lower())
            uniq.append(cand)
    return tuple(uniq)


# Conf tokens that collide with ubiquitous NON-music file suffixes (QA
# 2026-07-02: a music folder containing a git checkout / app data probed
# README.md, app.js, thumbs.db… one 136 ms uade boot each).  These stay
# matchable in Amiga PREFIX form (``md.song`` = Mike Davies) but are never
# registered as suffix extensions.  Deliberately NOT length-based: 2-char
# tokens like ``fc``/``bd``/``rh``/``dw`` are real, common Modland suffixes.
_SUFFIX_REGISTRATION_DENY = frozenset({
    "md", "js", "db", "ma", "st", "gm", "ps", "di", "is", "ss", "ex", "in",
    "py", "sh", "ini", "cfg", "log", "tmp", "bak",
})


def new_suffix_tokens() -> dict[str, str]:
    """Suffix tokens uade may claim as real file extensions: token -> player.

    Excludes tokens other engines own (``_SUFFIX_OWNED_ELSEWHERE``),
    companion tags, and the common-file collision denylist above.  These get
    registered into the scanner's supported-extension set so Modland-style
    suffix naming (``song.fc13``) flows through every existing
    extension-keyed gate unchanged.
    """
    return {
        tok: player
        for tok, player in player_map().items()
        if tok not in _SUFFIX_OWNED_ELSEWHERE
        and tok not in _SUFFIX_REGISTRATION_DENY
        and tok not in COMPANION_PREFIXES
        and tok not in COMPANION_SUFFIXES
        and "." not in tok            # a dotted token can't be an extension
    }


# ── display names ────────────────────────────────────────────────────────────

# Friendly names for the most common eagleplayers; anything unmapped gets a
# cleaned-up conf name (CamelCase/underscores → spaced words).
_FRIENDLY = {
    "TFMX": "TFMX", "TFMX-Pro": "TFMX", "TFMX-7V": "TFMX 7V",
    "TFMX-Pro-TFHD": "TFMX", "TFMX-1.5-TFHD": "TFMX", "TFMX-7V-TFHD": "TFMX 7V",
    "TFMX_ST": "TFMX ST",
    "FutureComposer1.3": "Future Composer", "FutureComposer1.4": "Future Composer",
    "FutureComposer-BSI": "Future Composer", "FuturePlayer": "Future Composer",
    "SIDMon1.0": "SidMon", "SIDMon2.0": "SidMon II",
    "JochenHippel": "Jochen Hippel", "JochenHippel-CoSo": "Jochen Hippel CoSo",
    "JochenHippel-7V": "Jochen Hippel 7V", "Jochen_Hippel_ST": "Jochen Hippel ST",
    "DavidWhittaker": "David Whittaker", "RobHubbard": "Rob Hubbard",
    "RobHubbard_ST": "Rob Hubbard ST", "BenDaglish": "Ben Daglish",
    "BenDaglish-SID": "Ben Daglish SID",
    "DeltaMusic1.3": "Delta Music", "DeltaMusic2.0": "Delta Music 2",
    "SoundMon2.0": "SoundMon", "SoundMon2.2": "SoundMon",
    "PTK-Prowiz": "ProTracker (packed)", "custom": "Amiga Custom",
    "CustomMade": "Custom Made", "Fred": "Fred Editor", "FredGray": "Fred Gray",
    "MusiclineEditor": "Musicline Editor", "DIGI-Booster": "DIGI Booster",
    "ChipTracker": "ChipTracker", "AudioSculpture": "Audio Sculpture",
    "PreTracker": "PreTracker", "Oktalyzer": "Oktalyzer",
    "RichardJoseph": "Richard Joseph", "PaulRobotham": "Paul Robotham",
    "PaulSummers": "Paul Summers", "JasonPage": "Jason Page",
    "AbyssHighestExperience": "AHX",
}

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def display_name(player: str) -> str:
    """Human-facing format name for an eagleplayer conf name."""
    if player in _FRIENDLY:
        return _FRIENDLY[player]
    name = player.replace("_", " ").replace("-", " ")
    return _CAMEL_RE.sub(" ", name)


def format_labels() -> frozenset[str]:
    """Every human-facing format label uade can attach to a track — the friendly
    map's values plus ``display_name`` of each eagleplayer in the conf.

    Lets the retro classifier (``core.retro``) recognise uade-rendered Amiga
    exotica as retro/scene music without hard-coding the ~175 player names.
    Best-effort: uade's *runtime* ``-g`` playername (e.g. "TFMX Pro",
    "SoundMon 2.0") can differ from these conf-derived labels, so ``retro``
    also carries a small curated supplement for the observed variants.  Returns
    an empty set if the conf can't be read (uade not installed)."""
    try:
        labels = set(_FRIENDLY.values())
        labels |= {display_name(p) for p in set(player_map().values())}
        return frozenset(l for l in labels if l)
    except Exception:                                   # noqa: BLE001
        return frozenset()


# ── Aegis Sonix (SonixMusicDriver) ────────────────────────────────────────────
# Sonix ``.smus`` modules keep their samples in a sibling ``Instruments/`` subdir
# keyed by arbitrary INS1 names (invisible to the companion-sibling rule).  Both
# the scanner (to flag a track ``partial`` when the archive is missing some) and
# the play path (to stub the missing ones with silence) need to know which
# instruments a module references and which are present — shared here so the two
# sites agree on what counts as "missing".
SONIX_PLAYERS = frozenset({"SonixMusicDriver"})


def sonix_instrument_names(smus_bytes: bytes) -> list[str]:
    """INS1 instrument names referenced by an IFF SMUS module, in order.

    Each INS1 chunk is a 4-byte index/flags header followed by a
    NUL-terminated instrument name; uade turns that name into the file
    request ``Instruments/<name>.instr``.
    """
    import struct
    names: list[str] = []
    if smus_bytes[:4] != b"FORM" or smus_bytes[8:12] != b"SMUS":
        return names
    pos = 12
    n = len(smus_bytes)
    while pos + 8 <= n:
        cid = smus_bytes[pos:pos + 4]
        ln = struct.unpack(">I", smus_bytes[pos + 4:pos + 8])[0]
        body = smus_bytes[pos + 8:pos + 8 + ln]
        if cid == b"INS1" and len(body) > 4:
            nm = body[4:].split(b"\x00", 1)[0].decode("latin-1").strip()
            if nm:
                names.append(nm)
        pos += 8 + ln + (ln & 1)
    return names


def sonix_missing_instruments(smus_bytes: bytes,
                              present_basenames_lower: set[str]) -> list[str]:
    """INS1 names whose ``<name>.instr`` is NOT in *present_basenames_lower*.

    Callers supply the set of lowercased basenames actually available (from an
    archive namelist at scan, or the extracted dir at play).  Preserves module
    order and de-dups so a repeated instrument is reported once.
    """
    out: list[str] = []
    seen: set[str] = set()
    for nm in sonix_instrument_names(smus_bytes):
        key = f"{nm}.instr".lower()
        if key in present_basenames_lower or key in seen:
            continue
        seen.add(key)
        out.append(nm)
    return out
