# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Incremental post-scan enrichment (``core/enrich_delta.py``).

* the store's enrichment change log: every write path records the track,
  fields no pass reads don't, the log is per consumer (one pass consuming it
  hides nothing from another), a load / an overflow forces full passes;
* the property that matters, seeded and randomised: on a synthetic library
  shaped like a real one (HVSC and Modland trees, game folders with several
  composers, archives with several members, generic folders inside archives,
  composer packs, category and part-named archives, compo packs, wrappers,
  nested archives, console rips, modern albums), after any sequence of scan
  upserts, rescans, user edits, deletes, moves, duration backfills, new person
  names, HVSC GAMES tunes, index refreshes and setting switches, the passes run
  incrementally (Modland → song database → Demozoo → folder albums → archive
  names) leave every track exactly as a fresh full run of the same passes on
  the same library does — and the status counts match.

Every index is a throw-away sqlite file; the stores are in-memory."""
from __future__ import annotations

import asyncio
import copy
import gzip
import hashlib
import random
import sqlite3
import uuid

import pytest

from soniqboom.core import demozoo, enrich_delta, repair, scanner
from soniqboom.core import folder_album as fa
from soniqboom.core import game_titles as gt
from soniqboom.core import scene_metadata as sm
from soniqboom.core import songdb
from soniqboom.core.store import TrackStore


_REAL_GT_SCHEDULE = gt.schedule


def _t(tid, path="/m/x.mod", **kw):
    d = {"id": tid, "path": path, "title": tid, "artist": "", "album": "",
         "format": "ProTracker", "genre": []}
    d.update(kw)
    return d


# ── the change log ───────────────────────────────────────────────────────────

def test_every_write_path_logs_the_track():
    s = TrackStore()
    c0 = s.enrich_cursor()
    s.upsert_track(_t("a"))
    s.upsert_tracks_batch([_t("b"), _t("c")])
    s.update_track_fields("a", {"artist": "X"})
    s.update_track_fields_batch([("b", {"album": "Y"})])
    s.delete_track("c")
    assert s.enrich_changes(c0) == {"a", "b", "c"}
    c1 = s.enrich_cursor()
    s.upsert_track(_t("d"))
    s.delete_track_ids(["a"])
    assert s.enrich_changes(c1) == {"a", "d"}
    assert s.enrich_changes(s.enrich_cursor()) == set()


def test_fields_no_pass_reads_are_not_logged():
    s = TrackStore()
    s.upsert_tracks_batch([_t("a")])
    c = s.enrich_cursor()
    s.update_track_fields("a", {"cover_art": "data:x", "mtime": 5, "file_size": 9})
    s.update_track_fields_batch([("a", {"duplicate_group_id": "g", "is_duplicate_primary": True,
                                        "format_score": 3})])
    s.update_track_fields("a", {"artist": ""})                  # no change: no write
    assert s.enrich_changes(c) == set()
    s.update_track_fields("a", {"duration": 12.5})              # duration-only: logged
    assert s.enrich_changes(c) == {"a"}


def test_positions_are_per_consumer():
    s = TrackStore()
    s.upsert_tracks_batch([_t("a"), _t("b")])
    first = s.enrich_cursor()
    s.update_track_fields("a", {"artist": "X"})
    second = s.enrich_cursor()                  # one pass ran and moved past "a"
    s.update_track_fields("b", {"artist": "Y"})
    assert s.enrich_changes(first) == {"a", "b"}                # the other pass still sees both
    assert s.enrich_changes(second) == {"b"}


def test_a_load_or_the_cap_forces_a_full_pass(monkeypatch):
    from soniqboom.core import store as store_mod
    s = TrackStore()
    s.upsert_tracks_batch([_t("a")])
    c = s.enrich_cursor()
    s.bulk_load({"a": _t("a")}, {}, {}, {}, {}, [], {}, {}, {})
    assert s.enrich_changes(c) is None                          # another epoch
    assert s.enrich_changes(None) is None
    monkeypatch.setattr(store_mod, "_ENRICH_LOG_MAX", 10)
    c = s.enrich_cursor()
    s.upsert_tracks_batch([_t(f"x{i}") for i in range(8)])
    mid = s.enrich_cursor()
    s.upsert_tracks_batch([_t(f"y{i}") for i in range(4)])     # past the cap: oldest half dropped
    assert len(s._enrich_log) < 10
    assert s.enrich_changes(c) is None
    assert s.enrich_changes(mid) == {f"y{i}" for i in range(4)}
    assert s.enrich_changes(mid, limit=3) is None               # too many for a delta


def test_record_moves_past_own_writes_only():
    s = TrackStore()
    s.upsert_tracks_batch([_t("a"), _t("b")])
    snap = s.enrich_cursor()
    before = s._enrich_seq
    s.update_track_fields("a", {"artist": "X"})                # the pass's own write
    own = s._enrich_seq - before
    last = enrich_delta.record(s, snap, own, ("in",))
    assert enrich_delta.changes(s, last, ("in",)) == set()
    assert enrich_delta.changes(s, last, ("other",)) is None    # an input changed
    assert enrich_delta.changes(s, last, ("in",), force=True) is None
    snap = s.enrich_cursor()
    s.update_track_fields("b", {"artist": "Y"})                # somebody else's write
    last = enrich_delta.record(s, snap, 0, ("in",))
    assert enrich_delta.changes(s, last, ("in",)) == {"b"}


# ── the equivalence property ─────────────────────────────────────────────────

ROOT = "/lib"
_COMPOSERS = ["Chris Huelsbeck", "Jochen Hippel", "Rob Hubbard", "Martin Galway", "Jeroen Tel",
              "Ben Daglish", "David Whittaker", "Tim Follin", "Moby", "Jester", "Dalezy", "4-Mat",
              "Lizardking", "Romeo Knight", "Purple Motion", "Skaven", "Allister Brimble"]
_GAMES = ["Turrican", "Turrican II", "Lotus", "Gold of the Aztecs", "Last Ninja 2",
          "Shadow of the Beast", "Xenon 2", "Speedball", "Apidya", "Agony", "Lionheart",
          "Wings of Fury", "Arkanoid", "Commando", "Rick Dangerous", "Mole Mayhem", "Cobra",
          "Uridium", "Paradroid", "Katakis", "Street Racer", "Game Over", "Pinball Dreams"]
_PARTS = ["Title", "Ingame", "Level 1", "Level 2", "Game Over", "Intro", "Highscore",
          "Ending", "Menu", "Boss"]
_SONGS = ["Stardust", "Elysium", "Dreams", "Popcorn", "Axel F", "Space Debris", "Hyperbased",
          "Klisje", "Overture", "Nightfall", "Satellite", "Echoes", "Firestorm", "Moonlight",
          "Rick Dangerous", "Jack the Nipper", "Tune X", "Mole Mayhem Finish"]
_CATEGORIES = ["weird", "happy", "chill", "dark", "remix"]
_GROUPS = ["Fairlight", "Triad", "Kefrens", "Sanity"]
_TRACKER = ["ProTracker", "FastTracker 2", "ScreamTracker 3"]
_PAULA = ["TFMX Pro", "Jochen Hippel", "David Whittaker", "Rob Hubbard"]
_ML_DIR = {"ProTracker": "Protracker", "FastTracker 2": "Fasttracker 2",
           "ScreamTracker 3": "Screamtracker 3", "TFMX Pro": "TFMX", "Jochen Hippel": "Hippel",
           "David Whittaker": "David Whittaker", "Rob Hubbard": "Rob Hubbard"}
_LIST = [("wikidata", "amiga", g) for g in _GAMES[:16]] + \
        [("mame", "amiga", g) for g in _GAMES[:8]] + \
        [("wikidata", "c64", g) for g in ("Last Ninja 2", "Commando", "Uridium", "Paradroid",
                                         "Cobra", "Arkanoid")] + \
        [("nointro", "c64", g) for g in ("Commando", "Uridium")] + \
        [("wikidata", "snes", "Lotus"), ("wikidata", "atarist", "Turrican")]


def _slug(s: str) -> str:
    return s.lower().replace(" ", "_")


class _Lib:
    """The synthetic library and its indexes for one seed."""

    def __init__(self, rng: random.Random, tmp_path):
        self.rng = rng
        self.tmp = tmp_path
        self.n = 0
        self.modland: dict[str, str] = {}          # md5 → Modland path
        self.songdb_meta: dict[str, tuple] = {}    # key → (authors, publishers, album, year)
        self.songdb_len: dict[str, tuple] = {}
        self.index_gen = 0

    def _md5(self) -> str:
        return hashlib.md5(f"{self.rng.random()}".encode()).hexdigest()

    def track(self, path: str, fmt: str, title: str, artist: str = "", **kw) -> dict:
        r = self.rng
        t = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, path)), "path": path, "title": title,
             "artist": artist, "album": "", "album_artist": "", "composer": "", "format": fmt,
             "genre": ["Amiga", "Module"] if fmt in _PAULA else [],
             "file_md5": self._md5(), "duration": r.choice([0.0, 0.0, 95.0, 181.5]),
             "year": r.choice([None, None, 1989, 1991]), "added_at": self.n}
        t.update(kw)
        self._index_track(t)
        return t

    def _index_track(self, t: dict) -> None:
        """Some of the files are known to Modland / the song database."""
        r, fmt = self.rng, t["format"]
        if fmt not in _ML_DIR:
            return
        name = t["path"].rsplit("::", 1)[-1].replace("\\", "/").rsplit("/", 1)[-1]
        if r.random() < 0.45:
            author = t["artist"] or r.choice(_COMPOSERS + ["- unknown"])
            where = r.random()
            if where < 0.4:
                p = f"{_ML_DIR[fmt]}/{author}/{r.choice(_GAMES)}/{name}"
            elif where < 0.7 and fmt in _PAULA:
                g = _slug(r.choice(_GAMES))
                part = r.choice(_PARTS).lower()
                p = f"{_ML_DIR[fmt]}/{author}/mdat.{g}-{part}"
                if r.random() < 0.6:     # a sibling: the file-name split is evidenced
                    self.modland[self._md5()] = f"{_ML_DIR[fmt]}/{author}/mdat.{g}-boss"
            else:
                p = f"{_ML_DIR[fmt]}/{author}/{name}"
            self.modland[t["file_md5"]] = p
        if r.random() < 0.35:
            key = t["file_md5"][:12]
            self.songdb_meta[key] = (r.choice(_COMPOSERS + ["Unknown"]),
                                     r.choice(["Thalamus", "Ocean~Team17", "Team17", ""]),
                                     r.choice(_GAMES + [""]), r.choice(["1990", "1992", ""]))
            self.songdb_len[key] = ("1", r.choice(["95000,p", "120000,l", "30000,p"]))

    # one unit of the library: a folder / archive / file group
    def unit(self) -> list[dict]:
        r = self.rng
        self.n += 1
        n = self.n
        g, c, song = r.choice(_GAMES), r.choice(_COMPOSERS), r.choice(_SONGS)
        kind = r.choices(
            ["game", "rip", "composer", "plain", "arch_generic", "arch_root", "pack",
             "categories", "part_archive", "compo", "wrapper", "nested", "hvsc", "hvsc_game",
             "c64_arch", "root", "console", "modern"],
            [8, 4, 4, 3, 5, 5, 3, 3, 2, 2, 3, 2, 4, 2, 3, 1, 2, 4])[0]
        fmt = r.choice(_TRACKER)
        out: list[dict] = []
        if kind == "game":
            many = r.random() < 0.3
            for i in range(r.randint(1, 5)):
                part = r.choice(_PARTS)
                out.append(self.track(f"{ROOT}/Amiga/Games/{g} {n % 3}/{part} {i}.mod", fmt,
                                      f"{part} {i}" if r.random() < 0.8 else f"{g} {n % 3}",
                                      r.choice(_COMPOSERS) if many else c))
        elif kind == "rip":
            pf = r.choice(_PAULA)
            for i in range(r.randint(1, 4)):
                fn = f"mdat.{_slug(g)}-{r.choice(_PARTS).lower()}{i}"
                out.append(self.track(f"{ROOT}/Amiga/Rips/{g}/{fn}", pf, fn,
                                      r.choice([c, "", "<?>"])))
        elif kind == "composer":
            for i in range(r.randint(1, 4)):
                out.append(self.track(f"{ROOT}/Amiga/Musicians/{c}/{r.choice(_SONGS)} {i}.mod",
                                      fmt, f"{r.choice(_SONGS)} {i}", r.choice([c, c, ""])))
        elif kind == "plain":
            folder = r.choice(["Best of 1992", "Unsorted", f"{song} Collection", f"Disk {n % 4}"])
            for i in range(r.randint(1, 4)):
                out.append(self.track(f"{ROOT}/Mods/{folder}/{song} {i}.xm", "FastTracker 2",
                                      f"{song} {i}", r.choice(_COMPOSERS + [""])))
        elif kind == "arch_generic":
            for i in range(r.randint(1, 4)):
                sub = r.choice(["music/", "music/", "", "data/"])
                out.append(self.track(f"{ROOT}/Archives/{g}.lha::{sub}{r.choice(_PARTS)} {i}.mod",
                                      fmt, f"{r.choice(_PARTS)} {i}", r.choice([c, ""])))
        elif kind == "arch_root":
            pf = r.choice(_PAULA)
            for i in range(r.randint(1, 4)):
                member = f"{_slug(g)}.hip" if i == 0 and r.random() < 0.5 else f"{i}.{_slug(song)}"
                out.append(self.track(f"{ROOT}/Archives/{g} {n % 2}.lha::{member}", pf, member, c))
        elif kind == "pack":
            for who in r.sample(_COMPOSERS, r.randint(1, 4)):
                for i in range(r.randint(1, 2)):
                    out.append(self.track(f"{ROOT}/Archives/Party Pack {n}.zip::{who}/{song} {i}.mod",
                                          fmt, f"{song} {i}", who))
            for i in range(r.choice([0, 0, 1, 2])):     # a generic folder beside them
                out.append(self.track(f"{ROOT}/Archives/Party Pack {n}.zip::music/{song} x{i}.mod",
                                      fmt, f"{song} x{i}", r.choice(_COMPOSERS)))
        elif kind in ("categories", "part_archive"):
            name = f"Stuff {n}" if kind == "categories" else r.choice(["Ingame", "Title", "Level 1"])
            for cat in r.sample(_CATEGORIES, r.randint(1, 3)) + r.choice([[], [], ["mods"]]):
                for i in range(r.randint(1, 3)):
                    out.append(self.track(f"{ROOT}/Stuff/{name}.zip::{cat}/{song} {i}.mod", fmt,
                                          f"{song} {i}", r.choice(_COMPOSERS)))
        elif kind == "compo":
            for i in range(r.randint(2, 5)):
                entry = f"{r.choice(_SONGS)} {i}"
                out.append(self.track(f"{ROOT}/Compos/Assembly {n}.zip::{entry}/{entry}.xm",
                                      "FastTracker 2", entry, r.choice(_COMPOSERS + [""])))
        elif kind == "wrapper":
            ext = r.choice([".mod.zip", ".zip"])
            out.append(self.track(f"{ROOT}/mods/{song} {n}{ext}::{song} {n}.mod", fmt,
                                  f"{song} {n}", r.choice([c, ""])))
        elif kind == "nested":
            outer = f"{ROOT}/Archives/Outer {n % 3}.zip"
            for i in range(r.randint(1, 3)):
                out.append(self.track(f"{outer}::{g}.lha::{r.choice(_PARTS)} {i}.mod",
                                      fmt, f"part {i}", c))
            if r.random() < 0.5:     # a header game in a generic folder, composers around it
                out.append(self.track(f"{outer}::{g}.lha::spc/t{n}.spc", "SPC", f"Outer {n % 3}", c,
                                      game_by_tag=f"{g} Header", album=g, album_source="folder"))
                for who in r.sample(_COMPOSERS, r.randint(0, 3)):
                    out.append(self.track(f"{outer}::{who}/{song} {n}.mod", fmt, song, who))
        elif kind == "hvsc":
            hc = c.replace(" ", "_")
            for i in range(r.randint(1, 3)):
                out.append(self.track(f"{ROOT}/C64Music/MUSICIANS/{hc[0]}/{hc}/{_slug(song)}_{i}.sid",
                                      "SID", f"{song} {i}", c,
                                      comment=r.choice(["", "1988 Fairlight", "1990 Thalamus"])))
        elif kind == "hvsc_game":
            out.append(self.track(f"{ROOT}/C64Music/GAMES/{g[0]}/{g.replace(' ', '_')}.sid",
                                  "SID", g, c))
        elif kind == "c64_arch":
            for i in range(r.randint(1, 3)):
                out.append(self.track(f"{ROOT}/C64/{r.choice(['games', 'groups'])}/{g}.zip::{_slug(song)} {i}.sid",
                                      "SID", f"{song} {i}", r.choice([c, ""]),
                                      comment=r.choice(["", f"1988 {r.choice(_GROUPS)}"])))
        elif kind == "root":
            out.append(self.track(f"{ROOT}/{song} {n}.mod", fmt, f"{song} {n}", c))
        elif kind == "console":
            for i in range(r.randint(1, 3)):
                hdr = r.random() < 0.6
                out.append(self.track(f"{ROOT}/SNES/{g}/{i} - {r.choice(_PARTS)}.spc", "SPC",
                                      r.choice(_PARTS), c, album=g if hdr else "",
                                      album_source="tag" if hdr else None,
                                      game_by_tag=g if r.random() < 0.7 else None))
        else:
            for i in range(r.randint(1, 3)):
                out.append(self.track(f"{ROOT}/Albums/{c}/{g} OST/{i}.mp3", "MP3", f"{song} {i}",
                                      c, album=f"{g} OST"))
        return out

    # the indexes
    def write_modland(self) -> None:
        db = self.tmp / "modland.sqlite"
        db.unlink(missing_ok=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE mods (md5 TEXT PRIMARY KEY, path TEXT)")
        con.executemany("INSERT INTO mods VALUES (?,?)",
                        sorted(self.modland.items()) + [("f" * 32, "Protracker/Filler/x.mod")])
        con.commit()
        con.close()

    def write_songdb(self) -> None:
        d = self.tmp / f"sdb{self.index_gen}"
        d.mkdir()
        m, s = d / "metadata.tsv", d / "songlengths.tsv"
        filler = "f00000000000"                    # in no library: never an empty index
        m.write_text(f"{filler}\tFiller\t\t\t\n" + "".join(
            f"{k}\t{a}\t{p}\t{al}\t{y}\n" for k, (a, p, al, y) in sorted(self.songdb_meta.items())))
        s.write_text(f"{filler}\t1\t1000,p\n" + "".join(
            f"{k}\t{mn}\t{ss}\n" for k, (mn, ss) in sorted(self.songdb_len.items())))
        songdb.build_index(m, s, self.tmp / "songdb.sqlite")

    def write_demozoo(self) -> None:
        """Sceners named like the library's composers (some in groups),
        music productions named like its songs, some of them a game's
        soundtrack."""
        r = self.rng
        rel = ["300\tMoby\t\\N\t\\N\tf", "301\tJester\t\\N\t\\N\tf", "302\tDalezy\t\\N\t\\N\tf",
               "303\tLizardking\t\\N\t\\N\tf", "304\tSkaven\t\\N\t\\N\tf",
               "900\tFairlight\t\\N\t\\N\tt", "901\tTriad\t\\N\t\\N\tt"]
        nick = ["3000\t300\tMoby\t\\N", "3010\t301\tJester\t\\N", "3020\t302\tDalezy\t\\N",
                "3030\t303\tLizardking\t\\N", "3040\t304\tSkaven\t\\N",
                "9000\t900\tFairlight\t\\N", "9010\t901\tTriad\t\\N"]
        member = ["1\t300\t900", "2\t302\t901", "3\t304\t900"]
        prods, authors, types, links = [], [], [], []
        pid = 10
        for i, title in enumerate(_SONGS + [f"{p} {k}" for p in _PARTS[:3] for k in range(2)]):
            if r.random() < 0.5:
                continue
            nk = r.choice(["3000", "3010", "3020", "3030", "3040"])
            prods.append(f"{pid}\t{title}\tmusic\t{r.choice(['1989', '1990', '1991'])}-01-01")
            authors.append(f"{pid}\t{pid}\t{nk}")
            if r.random() < 0.4:
                gid = pid + 1000
                prods.append(f"{gid}\t{title}\tproduction\t1990-01-01")
                types.append(f"{gid}\t{gid}\t1")
                links.append(f"{gid}\t{gid}\t{pid}")
            pid += 1
        dump = (
            "COPY public.demoscene_releaser (id, name, first_name, surname, is_group) FROM stdin;\n"
            + "\n".join(rel) + "\n\\.\n"
            "COPY public.demoscene_nick (id, releaser_id, name, abbreviation) FROM stdin;\n"
            + "\n".join(nick) + "\n\\.\n"
            "COPY public.demoscene_nickvariant (id, nick_id, name) FROM stdin;\n\\.\n"
            "COPY public.demoscene_membership (id, member_id, group_id) FROM stdin;\n"
            + "\n".join(member) + "\n\\.\n"
            "COPY public.productions_production (id, title, supertype, release_date_date) FROM stdin;\n"
            + "\n".join(prods) + "\n\\.\n"
            "COPY public.productions_production_author_nicks (id, production_id, nick_id) FROM stdin;\n"
            + "\n".join(authors) + "\n\\.\n"
            "COPY public.productions_productiontype (id, name, path, depth, numchild, \"position\", internal_name) FROM stdin;\n"
            "1\tGame\t0001\t1\t0\t1\tgame\n2\tDemo\t0002\t1\t0\t2\tdemo\n\\.\n"
            "COPY public.productions_production_types (id, production_id, productiontype_id) FROM stdin;\n"
            + "\n".join(types) + "\n\\.\n"
            "COPY public.productions_soundtracklink (id, production_id, soundtrack_id) FROM stdin;\n"
            + "\n".join(links) + "\n\\.\n")
        p = self.tmp / "dump.sql.gz"
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write(dump)
        st = demozoo.refresh_index(dump_path=p)
        assert st["error"] is None, st


# Every module-level piece of pass state: swapped out while the reference run
# uses the same modules on its own store.
_STATE = {sm: ("_last_auto_sig", "_matched_ids", "_status"),
          songdb: ("_last_auto_sig", "_matched_ids", "_status"),
          demozoo: ("_last_apply_sig", "_matched_ids", "_stamped_ids", "_status"),
          fa: ("_last_seq", "_book", "_status"),
          gt: ("_last_sig", "_outer", "_members", "_hvsc_ids", "_extra", "_named_ids",
               "_primary_ids", "_last_result")}


def _save_state() -> dict:
    return {(m, a): getattr(m, a) for m, attrs in _STATE.items() for a in attrs}


def _restore_state(saved: dict) -> None:
    for (m, a), v in saved.items():
        setattr(m, a, v)


def _fresh_state() -> None:
    for m, attrs in _STATE.items():
        for a in attrs:
            cur = getattr(m, a)
            setattr(m, a, copy.deepcopy(cur) if a == "_status" else
                    (None if a.startswith("_last") or a == "_book" else type(cur)()))


async def _pipeline(*, full: bool, songdb_on: bool = True, demozoo_on: bool = True) -> dict:
    """The post-scan runner's passes in its order (the song database / Demozoo
    only while their auto-apply is on); the counts they report."""
    out = {}
    res = await sm.apply_to_library(auto=not full)
    assert not res.get("error"), res
    if songdb_on:
        res = await songdb.apply_to_library(force=full)
        assert not res.get("error"), res
    if demozoo_on:
        res = await demozoo.apply_to_library(force=full)
        assert not res.get("error"), res
    if fa.enabled():
        await fa.apply_folder_albums(force=full)
    await gt.apply_archive_games(force=full)
    return _counts(songdb_on, demozoo_on)


def _counts(songdb_on: bool = True, demozoo_on: bool = True) -> dict:
    out = {"modland": (sm._status.get("last_apply") or {}).get("matched"),
           "archive": ((gt._last_result or {}).get("named"), (gt._last_result or {}).get("primary"))}
    if songdb_on:
        out["songdb"] = (songdb._status.get("last_apply") or {}).get("matched")
    if demozoo_on:
        out["demozoo"] = (demozoo._status.get("last_apply") or {}).get("matched")
    return out


class _Harness:
    def __init__(self, seed: int, tmp_path, monkeypatch, units: int,
                 tracks: list[dict] | None = None):
        self.rng = random.Random(seed)
        self.seed = seed
        self.lib = _Lib(self.rng, tmp_path)
        self.store = TrackStore()
        self.cur = {"store": self.store}
        self.log: list[str] = []
        monkeypatch.setattr("soniqboom.core.store.get_store", lambda: self.cur["store"])
        if tracks is None:
            tracks = [t for _ in range(units) for t in self.lib.unit()]
        self.store.upsert_tracks_batch(tracks)
        self.store.upsert_scan_dir(ROOT)
        self.store.set_config(fa.CONFIG_KEY, True)
        self.lib.write_modland()
        self.lib.write_songdb()
        self.lib.write_demozoo()

    # mutations (each the way the app makes it)
    def _pick(self, n=1, where=None) -> list[dict]:
        ts = [t for t in self.store.all_tracks() if where is None or where(t)]
        return self.rng.sample(ts, min(n, len(ts)))

    async def mutate(self) -> None:
        r, s, lib = self.rng, self.store, self.lib
        if not fa.enabled() and r.random() < 0.4:
            self.log.append("folder_on")         # mostly on: its deltas are what is tested
            s.set_config(fa.CONFIG_KEY, True)
            await fa.apply_folder_albums(force=True)
            return
        kind = r.choices(
            ["scan_new", "scan_into", "rescan", "edit", "delete", "move", "duration",
             "person", "hvsc_game", "modland", "songdb", "toggle_filename", "toggle_archive",
             "toggle_folder", "nothing", "credit", "generic_into", "own_title"],
            [6, 6, 5, 5, 4, 3, 2, 2, 1, 1, 1, 1, 1, 1, 1, 3, 3, 3])[0]
        self.log.append(kind)
        if kind == "scan_new":
            s.upsert_tracks_batch(lib.unit())
        elif kind == "scan_into":            # a file added to an existing folder / archive
            for old in self._pick(r.randint(1, 3)):
                path = old["path"]
                head, sep, last = path.rpartition("::") if "::" in path else path.rpartition("/")
                lib.n += 1
                name = f"{r.choice(_SONGS)} new{lib.n}.mod"
                new = lib.track(f"{head}{sep}{name}" if sep == "::" else f"{head}/{name}",
                                old["format"] if old["format"] != "MP3" else "ProTracker",
                                r.choice([name.rsplit('.', 1)[0], r.choice(_PARTS)]),
                                r.choice(_COMPOSERS + ["", old.get("artist") or ""]))
                s.upsert_tracks_batch([new])
        elif kind == "credit":               # an archive member credits its archive's name
            for t in self._pick(1, where=lambda t: "::" in t["path"]):
                stem = t["path"].split("::")[-2].replace("\\", "/").rsplit("/", 1)[-1]
                who = stem.rsplit(".", 1)[0].rsplit(" ", 1)[0] if r.random() < 0.5 else stem.rsplit(".", 1)[0]
                s.update_track_fields(t["id"], {r.choice(["artist", "composer"]): who})
        elif kind == "generic_into":         # a file in an archive's generic folder
            for old in self._pick(1, where=lambda t: "::" in t["path"]):
                arch = old["path"].rsplit("::", 1)[0]
                lib.n += 1
                s.upsert_tracks_batch([lib.track(
                    f"{arch}::{r.choice(['music', 'data', 'mods'])}/{r.choice(_SONGS)} g{lib.n}.mod",
                    "ProTracker", f"g{lib.n}", r.choice(_COMPOSERS + [""]))])
        elif kind == "own_title":            # a tune titled like its folder, or a known production
            for t in self._pick(r.randint(1, 2)):
                inner = t["path"].rsplit("::", 1)[-1].replace("\\", "/")
                folder = (t["path"].rsplit("/", 2)[-2] if "/" in inner or "::" not in t["path"]
                          else t["path"].split("::")[-2].rsplit("/", 1)[-1].rsplit(".", 1)[0])
                s.update_track_fields(t["id"], {"title": r.choice([folder, r.choice(_SONGS)])})
        elif kind == "rescan":               # a changed file re-extracted
            for old in self._pick(r.randint(1, 3)):
                new = {k: v for k, v in old.items()
                       if k in ("id", "path", "format", "genre", "duration", "added_at", "comment")}
                if r.random() < 0.15:        # read as another format now
                    new["format"] = r.choice(["MP3", "ProTracker", "SID", "TFMX Pro"])
                new.update(title=r.choice([old.get("title") or "", r.choice(_PARTS + _SONGS)]),
                           artist=r.choice([old.get("artist") or "", r.choice(_COMPOSERS), ""]),
                           album="", album_artist="", composer="",
                           file_md5=old.get("file_md5") if r.random() < 0.5 else lib._md5())
                lib._index_track(new)
                s.upsert_tracks_batch([new])
        elif kind == "edit":                 # PUT /tracks/{id}/meta
            for t in self._pick(r.randint(1, 2)):
                f = r.choice(["album", "artist", "title", "game"])
                v = r.choice(["", r.choice(_GAMES), r.choice(_COMPOSERS), r.choice(_PARTS)])
                ue = sorted(set(t.get("user_edited") or []) | {f})
                upd = {f: v, "user_edited": ue}
                if f == "album":
                    upd["album_source"] = None
                s.update_track_fields(t["id"], upd)
        elif kind == "delete":
            s.delete_track_ids([t["id"] for t in self._pick(
                r.randint(1, 3), where=lambda t: "::" in t["path"] or r.random() < 0.3)])
        elif kind == "move":                 # renamed on disk: a new path, a new id
            for t in self._pick(1):
                s.delete_track(t["id"])
                lib.n += 1
                path = t["path"].replace("/Archives/", "/Moved/", 1) if r.random() < 0.5 else \
                    f"{ROOT}/Moved/{r.choice(_GAMES)}/{lib.n}.mod"
                new = {**{k: v for k, v in t.items() if k in (
                    "title", "artist", "format", "genre", "file_md5", "duration", "comment")},
                       "id": str(uuid.uuid5(uuid.NAMESPACE_URL, path)), "path": path, "album": ""}
                s.upsert_tracks_batch([new])
        elif kind == "duration":             # a render backfill / a cleared length
            for t in self._pick(2):
                s.update_track_fields(t["id"], {"duration": r.choice([0.0, 77.0])})
        elif kind == "person":               # a new artist named like folders
            lib.n += 1
            who = r.choice(_GAMES + _CATEGORIES + ["Musicians", "Games"])
            s.upsert_tracks_batch([lib.track(f"{ROOT}/Albums/{who}/x{lib.n}.mp3", "MP3",
                                             f"x{lib.n}", who, album="Live")])
        elif kind == "hvsc_game":
            lib.n += 1
            g = r.choice(["Last Ninja 2", "Commando", "Rick Dangerous", "Katakis"])
            s.upsert_tracks_batch([lib.track(
                f"{ROOT}/C64Music/GAMES/{g[0]}/{g.replace(' ', '_')}{'' if r.random() < .5 else lib.n}.sid",
                "SID", g, "")])
        elif kind == "modland":              # a refreshed index: rows come and go
            for k in r.sample(sorted(lib.modland), max(1, len(lib.modland) // 10)):
                del lib.modland[k]
            for t in self._pick(5, where=lambda t: t["format"] in _ML_DIR):
                lib._index_track(t)
            lib.index_gen += 1
            lib.write_modland()
        elif kind == "songdb":
            for k in r.sample(sorted(lib.songdb_meta), max(1, len(lib.songdb_meta) // 10)):
                del lib.songdb_meta[k]
            for t in self._pick(5, where=lambda t: t["format"] in _ML_DIR):
                lib._index_track(t)
            lib.index_gen += 1
            lib.write_songdb()
        elif kind == "toggle_filename":
            s.set_config(sm.MODLAND_FILENAME_CONFIG_KEY, not sm.filename_game_enabled())
        elif kind == "toggle_archive":
            s.set_config(gt.CONFIG_KEY, not gt.enabled())
        elif kind == "toggle_folder":        # PUT /admin/settings
            if fa.enabled():
                s.set_config(fa.CONFIG_KEY, False)
                await fa.revert_album_source(fa.SOURCE_FOLDER)
            else:
                s.set_config(fa.CONFIG_KEY, True)
                await fa.apply_folder_albums(force=True)

    async def reference(self, **on) -> tuple[dict, dict]:
        """A fresh full run on a copy of the library as it is now."""
        ref = TrackStore()
        ref.bulk_load(copy.deepcopy(dict(self.store._tracks)), {}, {}, {}, {}, [],
                      copy.deepcopy(self.store._scan_dirs), {}, copy.deepcopy(self.store._config))
        ref.rebuild_indexes()
        saved = _save_state()
        _fresh_state()
        self.cur["store"] = ref
        try:
            counts = await _pipeline(full=True, **on)
        finally:
            self.cur["store"] = self.store
            _restore_state(saved)
        return {tid: dict(t) for tid, t in ref._tracks.items()}, counts


def _diff(a: dict, b: dict) -> list[str]:
    out = []
    for tid in sorted(set(a) | set(b)):
        x, y = a.get(tid), b.get(tid)
        if x is None or y is None:
            out.append(f"{tid}: present only in {'reference' if x is None else 'incremental'}")
            continue
        for k in sorted(set(x) | set(y)):
            if x.get(k, "<missing>") != y.get(k, "<missing>"):
                out.append(f"{x.get('path')} [{k}]: incremental={x.get(k, '<missing>')!r} "
                           f"full={y.get(k, '<missing>')!r}")
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_db_path", lambda: tmp_path / "modland.sqlite")
    monkeypatch.setattr(songdb, "_db_path", lambda: tmp_path / "songdb.sqlite")
    monkeypatch.setattr(demozoo, "_db_path", lambda: tmp_path / "demozoo.sqlite")
    monkeypatch.setattr(sm, "_WITHDRAW_MIN_INDEX_ROWS", 1)
    monkeypatch.setattr(songdb, "_MIN_META_ROWS", 1)
    monkeypatch.setattr(songdb, "_MIN_LENGTH_ROWS", 1)
    idx = gt.TitleIndex()
    for src, plat, title in _LIST:
        idx.add(src, plat, title)
    monkeypatch.setattr(gt, "get_index", lambda: idx)
    monkeypatch.setattr(gt, "schedule", lambda: None)      # the passes run in order here

    async def _no_refresh(ids):
        return None
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    monkeypatch.setattr("soniqboom.core.store.freeze_long_lived_heap", lambda *a, **k: None)
    monkeypatch.setattr("soniqboom.config.PREFS_PATH", tmp_path / "prefs.json")
    for m, attrs in _STATE.items():
        for a in attrs:
            monkeypatch.setattr(m, a, getattr(m, a))       # restored after the test
    _fresh_state()
    sm._status["applying"] = songdb._status["applying"] = demozoo._status["applying"] = False
    return tmp_path, monkeypatch


async def _check(h: _Harness, what: str) -> None:
    """Incremental run == a fresh full run on the library as it is now."""
    want, want_counts = await h.reference()
    got_counts = await _pipeline(full=False)
    got = {tid: dict(t) for tid, t in h.store._tracks.items()}
    diff = _diff(got, want)
    assert not diff, f"{what}: {len(diff)} difference(s):\n" + "\n".join(diff[:25])
    assert got_counts == want_counts, (what, got_counts, want_counts)


async def _run_seed(seed: int, tmp_path, monkeypatch, *, units: int, steps: int) -> None:
    h = _Harness(seed, tmp_path, monkeypatch, units)
    await _pipeline(full=False)                 # the first run of the process: full
    for step in range(steps):
        await h.mutate()
        await _check(h, f"seed {seed} step {step} after {h.log}")


@pytest.mark.parametrize("seed", range(6))
async def test_incremental_passes_equal_a_full_run(env, seed):
    tmp_path, monkeypatch = env
    await _run_seed(seed, tmp_path, monkeypatch, units=120, steps=16)


# Neighbour effects, one by one: each change below moves the verdict of a
# track it does not touch — the delta must re-judge that track.

def _lt(path, title, artist="", fmt="ProTracker", **kw):
    t = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, path)), "path": path, "title": title,
         "artist": artist, "album": "", "album_artist": "", "composer": "", "format": fmt,
         "genre": [], "file_md5": hashlib.md5(path.encode()).hexdigest(), "duration": 60.0}
    t.update(kw)
    return t


def _id(path):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, path))


_MODS = f"{ROOT}/Mods"
_PACK = f"{ROOT}/Stuff/Bcompo5.zip"
_OUTER = f"{ROOT}/P/Outer.zip"
_CATS = [_lt(f"{_PACK}::{cat}/{i}.mod", f"Tune {cat} {i}", who)
         for cat in ("weird", "happy") for i, who in enumerate(("Jester", "Moby"))]
# (library, change, whether the change moves a track it does not touch)
_SCENARIOS = {
    # a composer folder goes: the others no longer make an index of composers
    "old place": (
        [_lt(f"{_MODS}/{who}/{i}.mod", f"Song {i}", who)
         for who in ("Jester", "Dalezy", "Moby") for i in range(2)]
        + [_lt(f"{_MODS}/Turrican/t.mod", "Intro", "Lizardking"),
           _lt(f"{ROOT}/Albums/x.mp3", "x", "Moby", fmt="MP3", album="Live")],  # names stay
        lambda s: s.delete_track_ids([_id(f"{_MODS}/Moby/{i}.mod") for i in range(2)]), True),
    # one more file in a generic folder makes the pack the release of its categories
    "generic folder member": (
        _CATS + [_lt(f"{_PACK}::music/a.mod", "A", "Dalezy")],
        lambda s: s.upsert_tracks_batch([_lt(f"{_PACK}::data/b.mod", "B", "Dalezy")]), True),
    # a category folder of one composer: the pack is no release any more
    "generic folder vote": (
        _CATS + [_lt(f"{_PACK}::{d}/a.mod", "A", "Dalezy") for d in ("music", "data")],
        lambda s: s.update_track_fields(_id(f"{_PACK}::weird/1.mod"), {"artist": "Jester"}),
        True),
    # an edit in a category folder: the generic folders still vote
    "generic folder voters": (
        _CATS + [_lt(f"{_PACK}::{d}/a.mod", "A", "Dalezy") for d in ("music", "data")],
        lambda s: s.update_track_fields(_id(f"{_PACK}::weird/0.mod"), {"title": "Other"}),
        False),
    # a generic-folder member withdrawn to its header's game no longer votes
    # in its archive (round 2): the archive is no release of its folders now
    "voter withdrawn": (
        [_lt(f"{_OUTER}::Zorblax.zip::spc/title.spc", "Outer", "Jester", fmt="SPC",
             game_by_tag="Some Game", album="Zorblax", album_source="folder"),
         _lt(f"{_OUTER}::Zorblax.zip::Hubbard/a.mod", "A", "Rob Hubbard"),
         _lt(f"{_OUTER}::Zorblax.zip::Turrican/b1.mod", "B1", "Lizardking"),
         _lt(f"{_OUTER}::Zorblax.zip::Turrican/b2.mod", "B2", "Skaven"),
         _lt(f"{_OUTER}::Jester/x.mod", "X", "Jester")],
        lambda s: s.upsert_tracks_batch([_lt(f"{_OUTER}::Moby/y.mod", "Y", "Moby"),
                                         _lt(f"{_OUTER}::Dalezy/z.mod", "Z", "Dalezy")]), True),
    # a second tune: the archive is no single-file wrapper of the first any more
    "several tunes": (
        [_lt(f"{ROOT}/Rips/Lotus.zip::lotus.mod", "Lotus", "Jester")],
        lambda s: s.upsert_tracks_batch([_lt(f"{ROOT}/Rips/Lotus.zip::title.mod", "Title",
                                             "Jester")]), True),
    # a member credits the archive's name: no member is named after it
    "archive credit": (
        [_lt(f"{ROOT}/Archives/Turrican II.lha::{i}.hip", f"{i}.hip", "Moby",
             fmt="Jochen Hippel") for i in ("a", "b", "c")],
        lambda s: s.update_track_fields(_id(f"{ROOT}/Archives/Turrican II.lha::a.hip"),
                                        {"composer": "Turrican II"}), True),
    # a new artist is named like a folder: the folder is a person's now
    "person name": (
        [_lt(f"{_MODS}/Agony/{i}.mod", f"Song {i}", "Jester") for i in range(2)],
        lambda s: s.upsert_tracks_batch([_lt(f"{ROOT}/Albums/x.mp3", "x", "Agony", fmt="MP3",
                                             album="Live")]), True),
    # an HVSC GAMES tune names a C64 archive anywhere in the library
    "hvsc games": (
        [_lt(f"{ROOT}/C64/Bombuzal.zip::{i}.sid", f"Tune {i}", "", fmt="SID") for i in range(2)],
        lambda s: s.upsert_tracks_batch([_lt(f"{ROOT}/C64Music/GAMES/B/Bombuzal.sid", "Bombuzal",
                                             "", fmt="SID")]), True),
    # read as another format: no folder name for a non-retro file
    "format change": (
        [_lt(f"{_MODS}/Agony/{i}.mod", f"Song {i}", "Jester") for i in range(2)],
        lambda s: s.upsert_tracks_batch([_lt(f"{_MODS}/Agony/0.mod", "Song 0", "Jester",
                                             fmt="MP3")]), False),
    # a title like its folder: the tune is the folder's own entry
    "title edit": (
        [_lt(f"{_MODS}/Agony/0.mod", "Intro", "Jester")],
        lambda s: s.update_track_fields(_id(f"{_MODS}/Agony/0.mod"), {"title": "Agony"}), False),
}


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
async def test_a_change_re_judges_the_tracks_whose_verdict_reads_it(env, name):
    tmp_path, monkeypatch = env
    tracks, change, neighbour = _SCENARIOS[name]
    h = _Harness(0, tmp_path, monkeypatch, 0, tracks=copy.deepcopy(tracks))
    await _pipeline(full=False)
    await _pipeline(full=False)
    c = h.store.enrich_cursor()
    change(h.store)
    touched = h.store.enrich_changes(c)
    before = {tid: dict(t) for tid, t in h.store._tracks.items()}
    await _check(h, name)
    moved = {tid for tid, t in h.store._tracks.items() if t != before.get(tid)}
    if neighbour:                    # the change moved a verdict of a track it did not touch
        assert moved - touched, name
    elif name != "generic folder voters":
        assert moved, name           # … of the track it touched


async def test_a_one_file_scan_joins_only_its_neighbourhood(env, monkeypatch):
    """The delta is really small: after a one-file scan each pass looks at
    the changed track and its neighbours only."""
    tmp_path, _mp = env
    h = _Harness(99, tmp_path, monkeypatch, units=120)
    await _pipeline(full=False)
    await _pipeline(full=False)                 # settled: nothing left to do
    seen: dict[str, list[int]] = {"modland": [], "songdb": [], "demozoo": [], "archive": [],
                                  "folder": []}
    real_sm, real_sdb, real_dz = sm.collect_updates, songdb.collect, demozoo.collect_updates
    real_gt, real_fa = gt.desired_slots, fa._judge_tracks
    monkeypatch.setattr(sm, "collect_updates",
                        lambda **kw: seen["modland"].append(len(kw["tracks"])) or real_sm(**kw))
    monkeypatch.setattr(songdb, "collect",
                        lambda tr, **kw: seen["songdb"].append(len(tr)) or real_sdb(tr, **kw))
    monkeypatch.setattr(demozoo, "collect_updates",
                        lambda tr=None, **kw: seen["demozoo"].append(len(tr)) or real_dz(tr, **kw))
    monkeypatch.setattr(gt, "desired_slots",
                        lambda tr, **kw: seen["archive"].append(len(tr)) or real_gt(tr, **kw))

    async def judge(tr, **kw):
        seen["folder"].append(len(tr))
        return await real_fa(tr, **kw)
    monkeypatch.setattr(fa, "_judge_tracks", judge)
    res = await sm.apply_to_library(auto=True)
    assert res.get("skipped") == "unchanged"
    member = next(t for t in h.store.all_tracks() if t["path"].startswith(f"{ROOT}/Archives/")
                  and "::music/" in t["path"])
    head = member["path"].rsplit("/", 1)[0]
    h.store.upsert_tracks_batch([h.lib.track(f"{head}/Boss 9.mod", "ProTracker", "Boss 9", "Moby")])
    await _pipeline(full=False)
    total = h.store.track_count()
    assert seen["modland"] == seen["songdb"] == seen["demozoo"] == [1]
    members = sum(1 for t in h.store.all_tracks()
                  if t["path"].startswith(member["path"].split("::", 1)[0] + "::"))
    assert seen["archive"] == [members]
    assert len(seen["folder"]) == 1 and seen["folder"][0] < total // 4


async def test_an_edit_in_place_during_a_full_folder_pass_reaches_the_next_one(env, monkeypatch):
    """A repair re-reading two tracks' format while the first (full) folder
    pass runs: the next pass re-judges the folder group they voted in then."""
    tmp_path, _mp = env
    tracks, _change, _n = _SCENARIOS["old place"]
    h = _Harness(0, tmp_path, monkeypatch, 0, tracks=copy.deepcopy(tracks))
    real = fa._judge_tracks
    done = []

    async def judge_then_edit(tr, **kw):
        out = await real(tr, **kw)
        if not done:
            done.append(1)
            for i in range(2):
                h.store.update_track_fields(_id(f"{_MODS}/Moby/{i}.mod"), {"format": "MP3"})
        return out
    monkeypatch.setattr(fa, "_judge_tracks", judge_then_edit)
    await _pipeline(full=False)
    monkeypatch.setattr(fa, "_judge_tracks", real)
    assert done
    await _check(h, "edit during a full pass")
    assert store_album(h, f"{_MODS}/Turrican/t.mod") == "Turrican"


def store_album(h, path):
    return h.store.get_track(_id(path)).get("album")


async def test_a_freed_album_takes_its_folder_name_in_the_delta(env, monkeypatch):
    """The song database withdraws an album (a refreshed index): the folder
    pass it runs then judges just that track's group — not forced — and names
    the album after the folder."""
    tmp_path, _mp = env
    t = _lt(f"{_MODS}/Agony/0.mod", "Song 0", "Jester")
    h = _Harness(0, tmp_path, monkeypatch, 0, tracks=[dict(t)])
    h.lib.songdb_meta[t["file_md5"][:12]] = ("Jester", "", "Carrier Command", "1990")
    h.lib.index_gen += 1
    h.lib.write_songdb()
    await _pipeline(full=False, songdb_on=True)
    assert h.store.get_track(t["id"])["album"] == "Carrier Command"
    await _pipeline(full=False)
    h.lib.songdb_meta.clear()
    h.lib.index_gen += 1
    h.lib.write_songdb()
    want, _counts_ = await h.reference()
    calls = []
    real = fa.apply_folder_albums

    async def spy(*, force=False):
        calls.append(force)
        return await real(force=force)
    monkeypatch.setattr(fa, "apply_folder_albums", spy)
    res = await songdb.apply_to_library()
    assert not res.get("error")
    assert calls == [False]
    a = h.store.get_track(t["id"])
    assert (a["album"], a["album_source"]) == ("Agony", fa.SOURCE_FOLDER)
    assert want[t["id"]]["album"] == "Agony"


async def _wait_runner():
    for _ in range(1000):
        await asyncio.sleep(0.01)
        if not (scanner._scene_autoapply_running or scanner._scene_autoapply_pending
                or fa._running or fa._pending or gt._running or gt._pending):
            return
    raise AssertionError("the runner did not finish")


@pytest.mark.parametrize("seed, cap", [(11, None), (12, None), (13, 60)])
async def test_the_real_runner_equals_a_full_run(env, monkeypatch, seed, cap):
    """The post-scan runner as the app runs it — the folder and archive passes
    at the same time, the song database / Demozoo auto-apply switched off and
    on, and (one seed) a change log that keeps overflowing its cap."""
    tmp_path, _mp = env
    if cap is not None:
        monkeypatch.setattr("soniqboom.core.store._ENRICH_LOG_MAX", cap)
    monkeypatch.setattr(gt, "schedule", _REAL_GT_SCHEDULE)
    monkeypatch.setattr(scanner, "_SCENE_AUTOAPPLY_SETTLE_S", 0)
    monkeypatch.setattr(gt, "_SCAN_POLL_S", 0.01)
    on = {"songdb": True, "demozoo": True}
    monkeypatch.setattr(songdb, "auto_apply_enabled", lambda: on["songdb"])
    monkeypatch.setattr(demozoo, "auto_apply_enabled", lambda: on["demozoo"])
    monkeypatch.setattr(scanner, "_scene_autoapply_pending", False)
    monkeypatch.setattr(scanner, "_scene_autoapply_running", False)
    monkeypatch.setattr(fa, "_pending", False)
    monkeypatch.setattr(fa, "_running", False)
    h = _Harness(seed, tmp_path, monkeypatch, 60)
    h.store.set_config(repair.ALBUM_BACKFILL_CONFIG_KEY, True)
    scanner._spawn_scene_autoapply()
    await _wait_runner()
    for step in range(10):
        if h.rng.random() < 0.3:
            k = h.rng.choice(["songdb", "demozoo"])
            on[k] = not on[k]
            h.log.append(f"{k} auto-apply {'on' if on[k] else 'off'}")
        await h.mutate()
        want, want_counts = await h.reference(songdb_on=on["songdb"], demozoo_on=on["demozoo"])
        scanner._spawn_scene_autoapply()
        await _wait_runner()
        got = {tid: dict(t) for tid, t in h.store._tracks.items()}
        diff = _diff(got, want)
        assert not diff, (f"seed {seed} step {step} after {h.log}: {len(diff)} difference(s):\n"
                          + "\n".join(diff[:25]))
        assert _counts(on["songdb"], on["demozoo"]) == want_counts, (seed, step, h.log)


async def test_a_folder_pass_without_the_modland_names_is_not_built_on(env, monkeypatch):
    """The Modland name sets could not be read: the pass judges without them,
    and the next pass judges every track again (no delta builds on it)."""
    tmp_path, _mp = env
    tracks, _change, _n = _SCENARIOS["old place"]
    h = _Harness(0, tmp_path, monkeypatch, 0, tracks=copy.deepcopy(tracks))
    real = sm.modland_name_keys

    def broken():
        raise OSError("index busy")
    monkeypatch.setattr(sm, "modland_name_keys", broken)
    await fa.apply_folder_albums()
    assert fa._last_seq is None and fa._book is None
    monkeypatch.setattr(sm, "modland_name_keys", real)
    await fa.apply_folder_albums()
    assert fa._last_seq is not None and fa._book is not None
    assert h.store.enrich_changes(fa._last_seq[0]) == set()
