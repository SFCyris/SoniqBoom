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
    # openmpt trackers (``ftm``: Face The Music, where libopenmpt reads it;
    # ``ptm``: PolyTracker and ProTracker IFF, as the ``ptm.`` prefix already is)
    "mod", "s3m", "xm", "it", "mtm", "med", "oct", "669", "dbm",
    "ult", "stm", "far", "amf", "gdm", "imf", "okt", "sfx", "wow", "dsm", "ftm", "ptm",
    # Sawteeth (sawteethrender) — also uade's SynTracker suffix, never scanned
    # as one (``_SUFFIX_REGISTRATION_DENY``)
    "st",
    # dedicated render routes
    "ahx", "hvl", "sid", "psid", "rsid",
    # Atari ST engines (uade's YM-2149 eagleplayer can't even play YM5 —
    # verified; StSound/psgplay/sc68 own these)
    "ym", "sndh", "sc68",
    # PSF console-rip family (zxtune owns the suffix; uade's ``psf`` token is
    # SoundFactory, still reachable in prefix form ``psf.song``)
    "psf", "psf2", "ssf", "dsf", "2sf", "gsf", "usf", "ncsf",
    "minipsf", "minipsf2", "minissf", "minidsf", "mini2sf", "minigsf",
    "miniusf", "minincsf",
    # zxtune123 (S98 dumps, Farbrausch V2); mdxrender (X68000 MDX + its PDX);
    # sunvoxrender (SunVox); kssrender (MGSDRV).  (``sng`` stays uade's: a
    # GoatTracker 2 song saved so is told apart by its header — metadata.extract)
    "s98", "v2m", "mdx", "pdx", "sunvox", "mgs",
    # AdLib AMUSIC
    "amd",
    # libopenmpt, where it reads them (``metadata._OPENMPT_OPTIONAL``), and
    # other names of the modules it reads (``metadata._TRACKER_ALT_EXTS``)
    "xmf", "dsym", "dtm", "dmf", "mdl", "umx", "psm", "j2b", "mo3", "oxm", "mptm", "rtm",
    "stx", "sfx2", "md0", "md1", "md2", "md3", "mmd3", "omed", "ocss", "chp", "chip",
    # xmprender (libxmp; ``metadata.XMP_EXT_NAMES``) and the packed modules
    # converted for it (``metadata.PW_EXT_NAMES``)
    "musx", "coco", "flx", "fnk", "liq", "mgt", "tip", "sti", "tdd", "eureka",
    "ben", "bnr", "dev", "gnoi", "gpmo", "gnu", "hcd", "nw1", "nw2", "pf1", "pf2", "pmd3",
    "ntc", "p22a", "p30a",
    # ffmpeg audio: WMA, MPEG layer 1 / 2, Ogg as .oga, IFF samples
    "wma", "mp1", "mp2", "m2a", "oga", "8svx", "16sv",
    # other engines / plain audio (defensive — some uade tokens are short and
    # generic; never let a name-prefilter shadow a real audio suffix)
    "nsf", "nsfe", "spc", "gbs", "vgm", "vgz", "ay", "kss", "sap", "gym",
    "hes", "rol", "cmf", "d00", "rad", "laa", "sci", "dro", "hsc", "rix",
    "a2m", "adl", "bam", "ksm", "mid", "midi", "dsf", "dff", "wsd",
    "mp3", "flac", "ogg", "opus", "m4a", "mp4", "aac", "wav", "aiff",
    "aif", "wv", "mpc", "ac3", "eac3", "ec3", "dts", "dtshd", "thd", "truehd", "mlp",
})

# Owned extensions a content-verified Amiga module may still carry: a
# ProTracker-family module saved as ``bp.song.mod`` / ``one.mod``, SidMon as
# ``fred.sid``.  For every OTHER owned extension (plain audio, chip formats,
# AdLib, MIDI, …) a uade name match never decides playback.
_NAME_OVERRIDABLE_SUFFIXES = frozenset({
    "mod", "s3m", "xm", "it", "mtm", "med", "oct", "669", "dbm",
    "ult", "stm", "far", "amf", "gdm", "okt", "sfx", "wow", "dsm",
    "sid", "st",
})


def ext_owned_elsewhere(ext: str) -> bool:
    """Does another engine (or plain audio decoding) own this file extension?

    ``ext`` may carry the dot and any case (``".MP3"``, ``"xm"``).  A uade
    name token in PREFIX position (``one.``, ``p10.``, ``fred.``) must not
    override such an extension: ``One.wav`` is a WAV, ``UFO.XM`` a
    FastTracker module."""
    return ext.lower().lstrip(".") in _SUFFIX_OWNED_ELSEWHERE


def owned_ext_can_be_amiga(ext: str) -> bool:
    """May a file with this owned extension still be an Amiga module when
    the scanner verified its content as one (tracker extensions and
    ``.sid``)?  Plain audio and the other engines' extensions never are."""
    return ext.lower().lstrip(".") in _NAME_OVERRIDABLE_SUFFIXES


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

# Hippel COSO songs that keep their instruments in one file shared between
# them (NostalgicPlayer: "a single sample file which is reused between
# them"), as Modland names them: the bank → the songs played with it.  A
# bank holds instruments and no song, so it is a companion, not a track;
# ``xpk.stage_for_uade`` attaches it to a song that has no instruments of its
# own (``rip_repairs.coso_with_bank``).
_COSO_SHARED_BANKS = {
    "cryptoburners - pack3.hipc": ("cryptoburners - pack2.hipc",
                                   "cryptoburners - pack4.hipc",
                                   "cryptoburners - pack5.hipc"),
}
_COSO_BANK_OF = {song: bank for bank, songs in _COSO_SHARED_BANKS.items() for song in songs}


def coso_bank_for(name: str) -> "str | None":
    """The shared instrument file ``name`` (a Hippel COSO song) plays with,
    as Modland names it, or None."""
    return _COSO_BANK_OF.get(name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower())


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

# Other suffixes modules of uade's players are saved under (NostalgicPlayer's
# collection names them so): suffix → the token uade plays them by.  The
# module is staged under the token's name (``play_name``); a TFMX song saved
# as ``X.tfx`` beside its samples ``X.sam`` is staged as ``mdat.X`` beside
# ``smpl.X`` (``tfx_sample_name``).  An alias whose target token uade's conf
# doesn't list claims nothing.
_SUFFIX_ALIASES = {
    "ch1": "chan", "ch2": "chan", "ch3": "chan", "chn": "chan",   # Channel Player 1–3
    "sd1": "sid1", "sd2": "sid2",                                  # SidMon 1 / 2
    "mp": "mpro", "wnp": "wn", "stp": "stpk", "fuzzac": "fuz",     # ProWizard packers
    "pm10": "pm1", "kms": "kef",
    "is10": "is", "bp2": "bp", "frd": "fred",                      # InStereo!, SoundMon, Fred
    "tfx": "mdat",                                                 # TFMX (+ its .sam)
}
TFX_SAMPLE_SUFFIX = "sam"


def _alias_of(last: str) -> str:
    """The uade token a suffix stands for (``_SUFFIX_ALIASES``), else the
    suffix itself."""
    tok = _SUFFIX_ALIASES.get(last)
    return tok if tok is not None and tok in player_map() else last


def tfx_sample_name(name: str) -> "str | None":
    """For a TFMX song saved as ``X.tfx``: its sample file's name ``X.sam``;
    None for any other name."""
    stem, dot, ext = name.rpartition(".")
    return f"{stem}.{TFX_SAMPLE_SUFFIX}" if dot and stem and ext.lower() == "tfx" else None


def _split_name(name: str) -> tuple[str, str]:
    """(first_segment, last_segment) of a lowercased basename, no dirs."""
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if "." not in base:
        return "", ""
    first, _, _ = base.partition(".")
    _, _, last = base.rpartition(".")
    return first, last


def _suffix_player(last: str, players: dict) -> "str | None":
    """The player a suffix (lower case, no dot) claims in suffix position:
    its own token's, or the token it stands for (``_SUFFIX_ALIASES``) —
    never for a suffix another engine owns or a common non-music one."""
    if not last or last in _SUFFIX_OWNED_ELSEWHERE or last in _SUFFIX_REGISTRATION_DENY:
        return None
    return players.get(_alias_of(last)) if last in _SUFFIX_ALIASES else players.get(last)


# A YM-ST replay routine Modland stores as "<tune>-replay.ymst" /
# "<tune> - replay.ymst" / "<tune> replay.ymst" (the stem without ".ymst").
_YMST_REPLAY_FILE_RE = re.compile(r"(?:^|[\s_-])replay$")


def is_companion_half(name: str) -> bool:
    """True for sample/instrument halves (and YM-ST replay routines,
    ``ym.mcoder_replay``, ``shadowdancer-replay.ymst``, ``ivanhoe -
    replay.ymst``) and shared Hippel COSO instrument files
    (``_COSO_SHARED_BANKS``) that must never appear as tracks."""
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if base in _COMPANION_LITERALS or base in _COSO_SHARED_BANKS:
        return True
    first, last = _split_name(name)
    if first.lstrip("'`\"") == "ym" and last.endswith("replay"):
        return True
    if base.endswith(".ymst") and _YMST_REPLAY_FILE_RE.search(base[:-5]):
        return True
    return first in COMPANION_PREFIXES or last in COMPANION_SUFFIXES


# ── companions a module names inside itself ─────────────────────────────────
# A YM-ST module ("YMST" magic, Modland's YMST folders) names the replay
# routine uade's YM-2149 player loads beside it — "YM.MCoder_replay" — and
# Modland stores that routine in the composer's folder, spelled in lower case
# and sometimes with a space for an underscore ("ym.mmme sid_replay") or a
# stray leading quote ("'ym.coso mmme_replay").
_YMST_REPLAY_RE = re.compile(rb"YM\.[A-Za-z0-9 _&+!.-]{1,48}?[ _]replay", re.I)
_EMBEDDED_SCAN = 8192


def embedded_companions(head: bytes) -> tuple[str, ...]:
    """The companion files a module names inside itself (``head``: the
    module's first bytes, ``_EMBEDDED_SCAN`` are enough): the replay routine
    of a YM-ST module.  Empty for every other module."""
    if head[:4] != b"YMST":
        return ()
    out: list[str] = []
    for m in _YMST_REPLAY_RE.finditer(head[:_EMBEDDED_SCAN]):
        found = m.group().decode("latin-1")
        if found not in out:
            out.append(found)
    return tuple(out)


def companion_key(name: str) -> str:
    """``name`` compared the way a companion named inside a module is found:
    ignoring case and everything but letters, digits and dots (Modland spells
    "YM.COSO_MMME_replay" as "'ym.coso mmme_replay")."""
    return re.sub(r"[^a-z0-9.]+", "", name.lower())


# ── companion folders ────────────────────────────────────────────────────────
# Players that load their samples from a FOLDER beside the module: a Sonix
# (IFF SMUS) score asks for ``Instruments/<name>.instr`` (and ``.ss``), which
# Modland keeps in an ``Instruments`` folder beside each score; ZoundMonitor
# asks for ``Samples/<name>``, which Modland keeps once for the whole format
# (``Zoundmonitor/Samples``, beside the composers' folders).  uade looks only
# in the module's own folder, so a staged module gets that folder beside it.
_FOLDER_ALL_MAX_FILES = 200
_FOLDER_ALL_MAX_BYTES = 4_000_000


def companion_dirs(name: str, data: bytes = b"") -> "list[tuple[str, int, tuple[str, ...] | None]]":
    """The companion folders ``name``'s player loads samples from:
    ``(folder name, how many folders up it may sit, wanted file stems or None
    for all)`` — the folder name is the one the player asks for; it is matched
    ignoring case.  ``data``: the module's bytes (a Sonix score's instrument
    names).  Empty for every other module."""
    if data[:4] == b"FORM" and data[8:12] == b"SMUS":
        stems = tuple(n.lower() for n in sonix_instrument_names(data))
        return [("Instruments", 0, stems)] if stems else []
    cls = classify(name)
    if cls and cls[0] == "ZoundMonitor":
        return [("Samples", 2, None)]
    return []


# ── PumaTracker modules cut short ────────────────────────────────────────────
# A PumaTracker rip whose last sample ends past the end of the file (Modland's
# "cyf-3-final-2.puma" is 8 bytes short) renders as pure silence; padded with
# zeros to the length its header states, it plays.


def puma_padded(name: str, data: bytes) -> "bytes | None":
    """``data`` padded with zeros to the end of its last sample, for a
    PumaTracker module (``.puma``) shorter than its header says; None when it
    needs none (or isn't one)."""
    cls = classify(name)
    if not cls or cls[0] != "PumaTracker" or len(data) < 0x50:
        return None
    import struct
    end = 0
    for i in range(10):
        off = struct.unpack(">I", data[0x14 + 4 * i:0x18 + 4 * i])[0]
        words = struct.unpack(">H", data[0x3C + 2 * i:0x3E + 2 * i])[0]
        if off and words:
            end = max(end, off + 2 * words)
    if len(data) < end <= len(data) + 4096:     # (a little short, never a wild header)
        return data + bytes(end - len(data))
    return None


# ── a second player for an extension ────────────────────────────────────────
# Extensions whose listed player refuses most files that another installed
# player plays, which no conf token reaches: Modland's Jesper Olsen ``.jo``
# modules (the listed JesperOlsen player refused 37 of 39, JesperOlsen_EP
# played 38).  ``player_args`` adds ``-P <player>`` to the uade command when
# the listed player refuses the file and the other accepts it.
_FALLBACK_PLAYER = {"jo": "JesperOlsen_EP"}
# The same for a prefix: Modland's TFMX ST modules are named ``mdat.<tune>``
# like every TFMX module, which the TFMX player uade picks refuses and its
# TFMX_ST player plays.
_PREFIX_FALLBACK_PLAYER = {"mdat": "TFMX_ST"}
# Listed players with a player bundled with SoniqBoom for the modules they
# refuse (``soniqboom/native/uade-players``): SUNtronic modules of the
# position-independent replayer build.
_BUNDLED_FALLBACK = {"SUN-Tronic": "SUNtronic2"}
_ARGS_CACHE: "dict[tuple, tuple[str, ...]]" = {}


def _accepts(binary: str, path: Path, extra: list[str]) -> bool:
    from soniqboom.core import forksafe
    try:
        r = forksafe.run([binary, "-g", *extra, "--", str(path)],
                         capture_output=True, timeout=30)
    except Exception:
        return False
    return r.returncode == 0


def player_args(path: "Path | str", binary: "str | None") -> list[str]:
    """Extra uade123 arguments for playing ``path``: the player it plays with
    (``_player_pick``), then ``-n`` (ignore the player's song end) for a
    Delta Music 2 module whose length ``dm2_length.song_seconds`` works out
    — the render is stopped at that length instead."""
    from soniqboom.core import dm2_length
    out = _player_pick(Path(path), binary)
    if binary and dm2_length.song_seconds(path) is not None:
        out = [*out, "-n"]
    return out


def _name_players(name: str) -> list[str]:
    """The players ``name``'s tokens list, as ``classify`` reads them: its
    first part's (in prefix position) then its extension's (in suffix
    position) — none for a companion half."""
    if is_companion_half(name):
        return []
    first, last = _split_name(name)
    players = player_map()
    out: list[str] = []
    if first in players and first not in _PREFIX_OWNED_ELSEWHERE:
        out.append(players[first])
    by_last = _suffix_player(last, players)
    if by_last:
        out.append(by_last)
    return out


def _player_pick(path: Path, binary: "str | None") -> list[str]:
    """``["-P", <player>]`` to make uade123 play ``path`` with another player
    than the one it picks, else ``[]``.  The candidates, in order:

    * for a name with a Forgotten Worlds token (``fw``, prefix or
      extension): the corrected player (``uade_players.forgotten_worlds``);
    * for a name with a token of a player in ``_BUNDLED_FALLBACK``: that
      bundled player (``uade_players.bundled``);
    * the installed player in ``_FALLBACK_PLAYER`` for its extension, else
      in ``_PREFIX_FALLBACK_PLAYER`` for its prefix, or, for a name whose
      prefix and extension are tokens of two players, its extension's
      player.

    The first candidate that accepts the file (``uade123 -g``) is picked —
    the corrected Forgotten Worlds player whenever it does, the others only
    when the player uade picks refuses the file.  Remembered for the file's
    size and time and which candidates its name has: a remembered answer
    looks at no player file (beyond the one it names still being there), a
    name with no candidate at none at all."""
    from soniqboom.core import uade_players
    if not binary:
        return []
    listed = _name_players(path.name)
    want_fw = uade_players.FW_PLAYER in listed
    own_names = tuple(dict.fromkeys(_BUNDLED_FALLBACK[n] for n in listed
                                    if n in _BUNDLED_FALLBACK))
    ext = path.name.rpartition(".")[2].lower() if "." in path.name else ""
    alt = _FALLBACK_PLAYER.get(ext)
    if alt is None and "." in path.name:
        alt = _PREFIX_FALLBACK_PLAYER.get(_split_name(path.name)[0])
    if alt is None:
        # A name whose first part is one player's token and whose extension
        # another's ("two.tme": NTSP-system by its prefix, which refuses it;
        # The Musical Enlightenment by its extension, which plays it).
        first, last = _split_name(path.name)
        players = player_map()
        by_first, by_last = players.get(first), _suffix_player(last, players)
        if by_first and by_last and by_first != by_last:
            alt = by_last
    if not (want_fw or own_names or alt):
        return []                       # (no candidate: nothing on disk is looked at)
    try:
        real = path.resolve()
        st = real.stat()
    except OSError:
        return []
    key = (str(real), st.st_size, st.st_mtime_ns, want_fw, own_names, alt)
    hit = _ARGS_CACHE.get(key)
    if hit is not None and (not hit or os.path.isfile(hit[1])):
        return list(hit)                # (the player files are looked at on a miss only)
    cands: "list[tuple[Path, bool]]" = []      # (player, used even when uade's accepts)
    if want_fw:
        fw = uade_players.forgotten_worlds()
        if fw is not None:
            cands.append((fw, True))
    for name in own_names:
        own = uade_players.bundled(name)
        if own is not None and all(own != c for c, _ in cands):
            cands.append((own, False))
    if alt:
        conf = conf_path()
        installed = conf.parent / "players" / alt if conf else None
        if installed is not None and installed.is_file():
            cands.append((installed, False))
    if not cands:
        return []
    hit = ()
    uade_accepts = None
    for player, always in cands:
        extra = ["-P", str(player)]
        if not always:
            if uade_accepts is None:
                uade_accepts = _accepts(binary, path, [])
            if uade_accepts:
                continue
        if _accepts(binary, path, extra):
            hit = tuple(extra)
            break
    while len(_ARGS_CACHE) >= _PICK_CACHE_MAX:     # (oldest first, never all)
        try:
            _ARGS_CACHE.pop(next(iter(_ARGS_CACHE)), None)
        except (StopIteration, RuntimeError):      # (another thread evicting too)
            break
    _ARGS_CACHE[key] = hit
    return list(hit)


# Extensions uade hands to one player by name although many of their files
# are for another.  Modland's "Hippel ST COSO" ``.soc`` modules are mostly
# Atari ST COSO, which the Amiga CoSo player that ``.soc`` is listed for plays
# silent (or refuses) and uade's Hippel ST player plays when the file is named
# for it (``.hst``); a few are Amiga COSO, which only the CoSo player plays.
# ``play_name`` tells them apart by a short render with each player (measured
# on Modland's 96: the ST player was right for 92, the CoSo player for 4).
_ALT_PLAYER = {"soc": ("hst", "Jochen_Hippel_ST")}
_PICK_SECONDS = 3
_PICK_CACHE: "dict[tuple, str]" = {}
_PICK_CACHE_MAX = 4096


def _distinct_values(binary: str, path: Path, extra: list[str]) -> int:
    """How many different sample values uade's render of ``path`` holds after
    its first second (a short render; 0 when uade refuses the file)."""
    from soniqboom.core import forksafe
    try:
        r = forksafe.run([binary, "-1", "-c", "-w", str(_PICK_SECONDS), *extra,
                          "--", str(path)],
                         capture_output=True, timeout=30)
    except Exception:
        return 0
    pcm = r.stdout[44100 * 4 + 68:]   # (uade's 68-byte WAV header, then s16 stereo)
    return len(set(memoryview(pcm[:len(pcm) // 2 * 2]).cast("h")))


def play_name(path: "Path | str", binary: "str | None" = None) -> str:
    """The name uade should see ``path``'s module under: its own, except for
    an extension in ``_ALT_PLAYER`` whose file plays with the other player
    (``thalionb.soc`` → ``thalionb.hst``).  With ``binary`` (uade123) the
    choice is made by a short render with each player — the one whose render
    holds more different sample values wins, the other player on a tie — and
    remembered for the file's size and time; without it the other player's
    name is the answer."""
    path = Path(path)
    stem, dot, ext = path.name.rpartition(".")
    if dot and stem and ext.lower() in _SUFFIX_ALIASES and _suffix_player(ext.lower(), player_map()):
        # Another name of one of uade's formats (``_SUFFIX_ALIASES``): staged
        # under the token uade plays it by — a TFMX ``X.tfx`` as ``mdat.X``.
        tok = _alias_of(ext.lower())
        return f"{tok}.{stem}" if ext.lower() == "tfx" else f"{stem}.{tok}"
    alt = _ALT_PLAYER.get(ext.lower()) if dot and stem else None
    if alt is None:
        return path.name
    alt_name = f"{stem}.{alt[0]}"
    if not binary:
        return alt_name
    conf = conf_path()
    player = conf.parent / "players" / alt[1] if conf else None
    if player is None or not player.is_file():
        return alt_name
    try:
        st = path.stat()
        key = (str(path.absolute()), st.st_size, st.st_mtime_ns)
    except OSError:
        return alt_name
    hit = _PICK_CACHE.get(key)
    if hit is not None:
        return hit
    own = _distinct_values(binary, path, [])
    other = _distinct_values(binary, path, ["-P", str(player)])
    pick = path.name if own > other else alt_name
    while len(_PICK_CACHE) >= _PICK_CACHE_MAX:         # (oldest first, never all)
        _PICK_CACHE.pop(next(iter(_PICK_CACHE)))
    _PICK_CACHE[key] = pick
    return pick


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
    # (README.md must never boot uade); also another name a player's modules
    # are saved under (``_SUFFIX_ALIASES``: song.ch1 → chan).
    by_last = _suffix_player(last, players)
    if by_last:
        return by_last, _alias_of(last)
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
    and always the shared ``smp.set`` / ``mdtest.ssd`` — plus, for a Hippel
    COSO song that plays with a shared instrument file, that file
    (``coso_bank_for``).

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
    bank = coso_bank_for(base)
    if bank:
        out.append(bank)                 # (a shared Hippel COSO instrument file)
    sam = tfx_sample_name(base)
    if sam:
        out.append(sam)                  # (a TFMX ``X.tfx`` song's samples: ``X.sam``)
    # de-dup, drop self
    seen, uniq = {base.lower()}, []
    for cand in out:
        if cand.lower() not in seen:
            seen.add(cand.lower())
            uniq.append(cand)
    return tuple(uniq)


# Players whose modules come as TWO files — the module plus a sample /
# instrument half beside it (the pairs listed with COMPANION_PREFIXES above;
# TFHD variants are single-file).  Used only to word a failed render.
_COMPANION_PLAYERS = frozenset({
    "TFMX", "TFMX-7V", "TFMX-Pro", "TFMX_ST", "RichardJoseph",
    "RichardJoseph_Player", "UFO", "PaulRobotham", "TimeTracker",
    "DirkBialluch", "PierreAdane", "JochenHippel-CoSo", "Jochen_Hippel_ST",
    "ThomasHermann", "JasonPage", "BladePacker", "SynthDream",
    "Alcatraz_Packer", "Quartet", "Maximum_Effect", "Ashley_Hogg",
})
# Suffix-form pairs whose companion is ``<body>.<suffix>``.
_COMPANION_SUFFIX_BY_PLAYER = {"UFO": "bank", "PaulRobotham": "ssd"}


def expected_companions(name: str) -> tuple[str, ...] | None:
    """For a module whose player needs a separate sample half: the usual
    names of that half (``mdat.X`` → ``smpl.X`` / ``smp.X``; UFO ``X.mus``
    → ``X.bank``) — possibly empty when the naming isn't known.  None for a
    single-file player or a name uade doesn't claim."""
    cls = classify(name)
    if cls is None or cls[0] not in _COMPANION_PLAYERS:
        return None
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    sam = tfx_sample_name(base)
    if sam:
        return (sam,)                   # (a TFMX ``X.tfx`` song: ``X.sam``)
    first, _, rest = base.partition(".")
    if first.lower() == cls[1] and rest:
        return (f"smpl.{rest}", f"smp.{rest}")
    suffix = _COMPANION_SUFFIX_BY_PLAYER.get(cls[0])
    stem = base.rpartition(".")[0]
    return (f"{stem}.{suffix}",) if suffix and stem else ()


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


def prefix_tokens() -> dict[str, str]:
    """Every token uade plays in Amiga PREFIX form (``di.title``, ``md.song``):
    token -> player.  Companion tags and dotted tokens excluded; the
    common-file denylist does NOT apply here — it only guards suffixes
    (``readme.md`` is no module, ``md.mercs`` is)."""
    return {
        tok: player
        for tok, player in player_map().items()
        if tok not in COMPANION_PREFIXES
        and tok not in COMPANION_SUFFIXES
        and "." not in tok
    }


def new_suffix_tokens() -> dict[str, str]:
    """Suffix tokens uade may claim as real file extensions: token -> player.

    The prefix tokens minus those other engines own
    (``_SUFFIX_OWNED_ELSEWHERE``) and the common-file collision denylist
    above.  These get registered into the scanner's supported-extension set
    so Modland-style suffix naming (``song.fc13``) flows through every
    existing extension-keyed gate unchanged.
    """
    out = {
        tok: player
        for tok, player in prefix_tokens().items()
        if tok not in _SUFFIX_OWNED_ELSEWHERE
        and tok not in _SUFFIX_REGISTRATION_DENY
    }
    players = player_map()
    for alias in _SUFFIX_ALIASES:              # (``_SUFFIX_ALIASES``: song.ch1 → chan)
        player = _suffix_player(alias, players)
        if player:
            out.setdefault(alias, player)
    return out


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


def svx8_silence(nbytes: int = 32) -> bytes:
    """A minimal valid IFF 8SVX one-shot sample of ``nbytes`` silence: the
    stand-in for a Sonix instrument a rip is missing, so uade's
    SonixMusicDriver loads it (silent) instead of aborting the whole score."""
    import struct
    body = b"\x00" * nbytes
    vhdr = struct.pack(">IIIHBBI", nbytes, 0, 0, 8000, 1, 0, 0x10000)

    def _chunk(cid: bytes, data: bytes) -> bytes:
        out = cid + struct.pack(">I", len(data)) + data
        return out + b"\x00" if (len(data) & 1) else out

    inner = b"8SVX" + _chunk(b"VHDR", vhdr) + _chunk(b"BODY", body)
    return b"FORM" + struct.pack(">I", len(inner)) + inner


def sonix_instruments_dir(folder: Path) -> "Path | None":
    """The ``Instruments`` folder (any case) in ``folder``, or None."""
    try:
        for entry in os.scandir(folder):
            if entry.name.lower() == "instruments" and entry.is_dir():
                return Path(entry.path)
    except OSError:
        pass
    return None


def sonix_local_missing(path: Path, smus_bytes: bytes) -> list[str]:
    """The instruments the Sonix score ``smus_bytes`` (the file ``path``)
    names whose ``<name>.instr`` is not in the ``Instruments`` folder beside
    it — all of them without that folder."""
    found = sonix_instruments_dir(Path(path).absolute().parent)
    present: set[str] = set()
    if found is not None:
        try:
            present = {e.name.lower() for e in os.scandir(found)}
        except OSError:
            pass
    return sonix_missing_instruments(smus_bytes, present)


def sonix_partial_detail(missing: list[str]) -> str:
    """The ``defect_detail`` of a Sonix score with ``missing`` instruments."""
    n = len(missing)
    shown = ", ".join(missing[:3]) + ("…" if n > 3 else "")
    return f"{n} instrument{'s' if n != 1 else ''} substituted (silent): {shown}"


def sonix_all_missing(smus_bytes: bytes, missing: list[str]) -> bool:
    """Is every instrument the Sonix score names among ``missing`` — so
    stand-ins for them would play nothing but silence?"""
    names = {sonix_instr_key(n) for n in sonix_instrument_names(smus_bytes)} - {""}
    return bool(names) and names <= {sonix_instr_key(n) for n in missing}


def sonix_defect(smus_bytes: bytes, missing: list[str]) -> "tuple[str, str] | None":
    """``(defect, defect_detail)`` of a Sonix score with ``missing``
    instruments: "partial" (silent stand-ins for them) when some are there,
    "corrupt" (silence is all it would play) when none is; None when none is
    missing."""
    if not missing:
        return None
    if sonix_all_missing(smus_bytes, missing):
        n = len(missing)
        return ("corrupt", f"none of its {n} instrument{'s' if n != 1 else ''} "
                           f"is in an Instruments folder beside it")
    return ("partial", sonix_partial_detail(missing))


def sonix_instr_key(name: str) -> str:
    """The key an instrument file is compared by: ``<name>.instr``'s bare file
    name (an INS1 name is the module's — only its last part is ever looked
    for, ``Instruments/<basename>.instr``), NFC, case-folded; "" for a name
    with no file name part."""
    import unicodedata
    base = os.path.basename(name.replace("\\", "/")).strip()
    if not base or base in (".", ".."):
        return ""
    return unicodedata.normalize("NFC", f"{base}.instr").casefold()


def sonix_missing_instruments(smus_bytes: bytes,
                              present_basenames_lower: set[str]) -> list[str]:
    """INS1 names whose ``<name>.instr`` is NOT among the file names
    *present_basenames_lower*, compared by ``sonix_instr_key`` (the bare file
    name, NFC, case-folded — as the file is looked for).

    Callers supply the file names actually available (from an archive
    namelist at scan, or the extracted dir at play).  Preserves module order
    and de-dups so a repeated instrument is reported once; a name with no file
    name part is never reported.
    """
    import unicodedata
    have = {unicodedata.normalize("NFC", n).casefold() for n in present_basenames_lower}
    out: list[str] = []
    seen: set[str] = set()
    for nm in sonix_instrument_names(smus_bytes):
        key = sonix_instr_key(nm)
        if not key or key in have or key in seen:
            continue
        seen.add(key)
        out.append(nm)
    return out
