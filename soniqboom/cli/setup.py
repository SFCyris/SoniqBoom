# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``soniqboom collections`` / ``soniqboom scene-info`` (list, add, remove)
and ``soniqboom first-run-setup`` (the installer's two questions).  They write
requests to SoniqBoom.conf; the server applies them (``core/setup_requests``)."""
from __future__ import annotations

import sys

from soniqboom.core import setup_requests as sr

_WHAT = {"collections": "online collection", "scene_info": "scene information"}
_CMD = {"collections": "collections", "scene_info": "scene-info"}
_WHERE = {"collections": "Settings → Collections",
          "scene_info": "Settings → Metadata, and Settings → Renderers → HVSC SID database"}
_SHORT = sr._SHORT
_SCENE_SHORT = {"hvsc": "HVSC song lengths and STIL", "modland": "the Modland checksum list",
                "songdb": "the UADE song database", "demozoo": "Demozoo"}


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _question(kind: str, names: list[str]) -> str:
    import textwrap
    if kind == "collections":
        text = (f"Add the online collection{'s' if len(names) > 1 else ''} — "
                f"{_and([_SHORT[n] for n in names])}?  Their "
                "tunes are listed in your library and fetched when you play them"
                + ("; the Modland catalogue needs about 1\u00a0GB more memory."
                   if "modland" in names else "."))
    else:
        text = (f"Set up the scene information — {_and([_SCENE_SHORT[n] for n in names])} "
                "(composers, games, scene groups, parties)?  Up to about 250\u00a0MB is downloaded.")
    return textwrap.fill(text, width=78, subsequent_indent="  ")


def _already(kind: str, state: "dict | None") -> dict:
    """``sr.already`` — not what a waiting removal takes out; nothing known
    (``state`` None: the library can't be read) when the server checks it
    instead."""
    if state is None:
        return {}
    gone = sr.pending(remove=True)
    return sr.already(kind, dict(state, removing=gone["collections"],
                                 removing_scene=gone["scene_info"]))


def _tell_already(already: dict) -> None:
    from soniqboom.core.modland_catalog import ROOT
    held = [m for m in already.values() if "root folder" in m]
    for msg in held:
        print(f"{msg} — left as is.")
    if any(ROOT.lower() in m.lower() for m in held):
        print("  (To add the Modland catalogue anyway: remove that network share, then "
              f"switch it on in {_WHERE['collections']}.)")
    if any(ROOT.lower() not in m.lower() for m in held):
        print(f"  (To add it anyway: {_WHERE['collections']}.)")
    for msg in already.values():
        if msg not in held:
            print(f"{msg} — left as is.")


def _applies() -> str:
    return ("A running SoniqBoom takes this within about 10 seconds (after a download "
            "it is already doing), otherwise its next start does.")


_REMOVES = {
    "collections": ("Switched off: its tunes leave the library; ratings, plays, stars and "
                    "playlist entries stay for when it is added again."),
    "scene_info": ("What it filled in is withdrawn (your own edits stay, and so do song "
                   "lengths and the artist names the Modland checksum list filled in), it "
                   "is no longer applied after scans, and its download is deleted.  HVSC: "
                   "its song lengths and STIL are no longer used for your own SID files "
                   "(values already stored stay); the download is deleted when the HVSC "
                   "collection is off."),
}


def cmd_list(kind: str) -> int:
    names = sr.KINDS[kind]
    adding = sr.pending()[kind]
    removing = sr.pending(remove=True)[kind]
    print(f"{'Name':<10} Description")
    print("─" * 72)
    for n, label in names.items():
        mark = ("  (add requested — waiting for the server)" if n in adding else
                "  (remove requested — waiting for the server)" if n in removing else "")
        print(f"  {n:<8} {label}{mark}")
    print(f"\nAdd with `soniqboom {_CMD[kind]} add [name …]` (all when none is named),")
    print(f"remove with `soniqboom {_CMD[kind]} remove name … | all`.")
    print(f"What is on: {_WHERE[kind]}.")
    return 0


def _names(kind: str, names: list[str]) -> "list[str] | None":
    """The names given — real names or ``all`` only (no yes / no words)."""
    names = [p for n in names for p in n.lower().replace(",", " ").split()]
    known = sr.KINDS[kind]
    chosen = (list(known) if "all" in names
              else [n for n in known if n in names]
              if names and all(n in known for n in names) else None)
    if not chosen:
        print(f"error: unknown {_WHAT[kind]} in {' '.join(names)!r}.  "
              f"Known: {', '.join(sr.KINDS[kind])} (or all)", file=sys.stderr)
        return None
    return chosen


def cmd_add(kind: str, names: list[str]) -> int:
    chosen = _names(kind, names or ["all"])
    if chosen is None:
        return 2
    already = {n: m for n, m in _already(kind, sr.read_state()).items() if n in chosen}
    _tell_already(already)
    sr.withdraw(kind, already)          # (an earlier request for it: nothing to do)
    chosen = [n for n in chosen if n not in already]
    if not chosen:
        print("Nothing to add.")
        return 0
    sr.request(kind, chosen)
    print("Requested: " + ", ".join(sr.KINDS[kind][n] for n in chosen) + ".")
    print(_applies())
    print(f"Progress: {_WHERE[kind]}.")
    return 0


def cmd_remove(kind: str, names: list[str]) -> int:
    if not names:
        print(f"error: name the {_WHAT[kind]} to remove ({', '.join(sr.KINDS[kind])}), "
              f"or all", file=sys.stderr)
        return 2
    chosen = _names(kind, names)
    if chosen is None:
        return 2
    sr.request(kind, chosen, remove=True)
    print("Removal requested: " + ", ".join(sr.KINDS[kind][n] for n in chosen) + ".")
    print(_REMOVES[kind])
    print(_applies())
    return 0


def _ask(question: str) -> bool:
    while True:
        try:
            ans = input(f"{question}\n  [Y/n] ").strip().lower()
        except EOFError:
            return True
        if ans in ("", "y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  Please answer y or n.")


def cmd_first_run(collections: "str | None", scene_info: "str | None") -> int:
    """Once: ask (or take the flags; without a terminal a new install gets
    yes, an update nothing) and request what was chosen — not what is
    already there (``_already``: mentioned, not asked, left as is)."""
    hint = ("Online collections and scene information can be added at any time: "
            "`soniqboom collections add`, `soniqboom scene-info add`.")
    if sr.asked():
        print(hint)
        return 0
    new = sr.is_new_install()
    flags = {"collections": collections, "scene_info": scene_info}
    interactive = sys.stdin.isatty()
    if not new and not interactive and all(v is None for v in flags.values()):
        print(hint)                     # (asked at the next update from a terminal)
        return 0
    if not new:
        print("Checking what is already set up…")
    state = sr.read_state()
    chosen = {}
    for kind in sr.KINDS:
        if flags[kind] is not None:
            names = sr.parse_choice(flags[kind], kind)
            if names is None:
                print(f"error: --{_CMD[kind]} takes yes, no or a list of "
                      f"{', '.join(sr.KINDS[kind])}", file=sys.stderr)
                return 2
            if not names:
                sr.withdraw(kind, list(sr.KINDS[kind]))    # (no: also to what was waiting)
        else:
            names = None
        already = _already(kind, state)
        _tell_already(already if names is None else
                      {n: m for n, m in already.items() if n in names})
        sr.withdraw(kind, already)      # (an earlier request for it: nothing to do)
        if kind == "collections" and state is None and names != []:
            print("  (Your library couldn't be read: what it already has is left as is "
                  "when SoniqBoom starts.)")
        avail = [n for n in sr.KINDS[kind] if n not in already]
        if names is not None:
            names = [n for n in names if n in avail]
        elif not avail or not new and not interactive:
            names = []                  # (an update without a terminal: only the flags)
        elif interactive:
            names = avail if _ask(_question(kind, avail)) else []
            if not names:
                sr.withdraw(kind, avail)    # (a "no" also to what was waiting)
        else:
            names = avail
        chosen[kind] = names
    for kind, names in chosen.items():
        if names:
            sr.request(kind, names)
    sr.mark_asked()
    for kind, names in chosen.items():
        label = "Online collections" if kind == "collections" else "Scene information"
        print(f"{label}: " + (", ".join(names) if names else "nothing added"))
    if any(chosen.values()):
        print("They are set up when SoniqBoom starts." if new else _applies())
    print(hint)
    return 0
