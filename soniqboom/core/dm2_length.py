"""How long a Delta Music 2 module plays before it starts over.

A Delta Music 2.0 song has no end command: each of its four voices steps
through its own track list (a list of 16-row blocks) and starts that list
again from its loop position when it reaches the end.  The song has played
once when every voice has started its list over at least once; a voice with
an empty list counts as having done so from the start.  ``song_seconds_of``
steps the sequencer through the module at 50 ticks per second (no audio) to
find that moment.

uade's Delta Music 2.0 player renders the music; with the length known, it is
run with the player's own song end ignored (``uade123 -n``) and stopped there.

The sequencer and song-end rule follow NostalgicPlayer's DeltaMusic20 player
(``Source/Agents/Players/DeltaMusic20/DeltaMusic20Worker.cs``: ``Identify``,
``InitializeSound``, ``Play``, ``ProcessChannel``, ``ParseEffect``; and
``OnEndReached(int channel)`` in ``ModulePlayerWithSubSongDurationAgentBase``):

    MIT License

    Copyright (c) 2023 Thomas Neumann

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the "Software"),
    to deal in the Software without restriction, including without limitation
    the rights to use, copy, modify, merge, publish, distribute, sublicense,
    and/or sell copies of the Software, and to permit persons to whom the
    Software is furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
    THE SOFTWARE.
"""
from __future__ import annotations

import struct
from pathlib import Path

TICKS_PER_SECOND = 50.0
MAX_SECONDS = 3600.0             # a longer song than this is reported as unknown
_MAGIC_AT = 0xBC6
_MAGIC = b".FNL"
_SPEED_AT = 0xBBB
_TRACKS_AT = 0xBCA + 64 * 16     # past the 64 arpeggio tables
_MIN_SIZE = 0xFDA
_MAX_READ = 2_000_000            # (far above any Delta Music 2.0 module)
_EFFECT_SET_SPEED = 0x01

_CACHE: "dict[tuple, float | None]" = {}
_CACHE_MAX = 4096


def is_dm2(head: bytes) -> bool:
    """Does ``head`` (a file's first bytes, at least 0xBCA of them) carry the
    Delta Music 2.0 mark (".FNL" at 0xBC6)?"""
    return len(head) >= _MAGIC_AT + 4 and head[_MAGIC_AT:_MAGIC_AT + 4] == _MAGIC


def _parse(data: bytes):
    """``(start speed, [(loop position, [block numbers])] × 4, [blocks])`` —
    each block 16 rows of ``(effect, argument)`` — or None when ``data`` is
    not a complete Delta Music 2.0 module (NostalgicPlayer's ``Identify``
    size check, then the track and block tables)."""
    if len(data) < _MIN_SIZE or not is_dm2(data):
        return None
    # Identify: the sizes the header states must fit in the file.
    total = _MIN_SIZE
    o = 0xFCA
    track_bytes = 0
    for _ in range(4):
        track_bytes += struct.unpack_from(">H", data, o + 2)[0]
        o += 4
    total += track_bytes
    o += track_bytes
    if o + 4 > len(data):
        return None
    block_bytes = struct.unpack_from(">I", data, o)[0]
    total += block_bytes + 4
    o += 4 + block_bytes + 256 - 2
    if o + 2 > len(data):
        return None
    inst_bytes = struct.unpack_from(">H", data, o)[0]
    total += inst_bytes + 256
    o += 2 + inst_bytes
    if o + 4 > len(data):
        return None
    total += struct.unpack_from(">I", data, o)[0] + 4 + 64
    if total > len(data):
        return None
    speed = struct.unpack_from("b", data, _SPEED_AT)[0]
    o = _TRACKS_AT
    heads = []
    for _ in range(4):
        loop, length = struct.unpack_from(">HH", data, o)
        heads.append((loop, length // 2))
        o += 4
    tracks = []
    for loop, n in heads:
        tracks.append((loop, [data[o + 2 * j] for j in range(n)]))
        o += 2 * n
    nblocks = struct.unpack_from(">I", data, o)[0] // 64
    o += 4
    blocks = []
    for _ in range(nblocks):
        blocks.append([(data[o + 4 * r + 2], data[o + 4 * r + 3]) for r in range(16)])
        o += 64
    return speed, tracks, blocks


def song_seconds_of(data: bytes) -> "float | None":
    """How many seconds the Delta Music 2.0 module ``data`` plays before
    every voice has started its track list over (the module's song end), or
    None when ``data`` isn't such a module, a track names a block the module
    doesn't have, the song ends at once, or it would play longer than
    ``MAX_SECONDS``."""
    try:
        parsed = _parse(data)
    except (struct.error, IndexError):              # tables running past the file
        parsed = None
    if parsed is None:
        return None
    speed, tracks, blocks = parsed
    tick = 1
    voices = [{"loop": loop, "list": lst, "row": 0, "cur": -1, "next": 0, "block": None}
              for loop, lst in tracks]
    done = [False] * 4
    for t in range(int(MAX_SECONDS * TICKS_PER_SECOND) + 1):
        tick -= 1
        if tick < 0:
            tick = speed
            if tick < 0:
                return None                         # (a negative speed never plays a row)
        for i, v in enumerate(voices):
            n = len(v["list"])
            if n == 0:
                done[i] = True
            elif tick == 0:
                if v["row"] == 0:
                    block_no = v["list"][v["next"]]
                    if block_no >= len(blocks):
                        return None
                    v["block"] = blocks[block_no]
                    if v["next"] <= v["cur"]:
                        done[i] = True
                    v["cur"] = v["next"]
                    v["next"] += 1
                    if v["next"] >= n:
                        v["next"] = 0 if v["loop"] >= n else v["loop"]
                effect, arg = v["block"][v["row"]]
                if effect == _EFFECT_SET_SPEED:
                    speed = arg & 0x0F
                v["row"] = (v["row"] + 1) & 0x0F
            if all(done):
                return round(t / TICKS_PER_SECOND, 2) if t > 0 else None
    return None


def song_seconds(path: "Path | str", tune: int = 0) -> "float | None":
    """``song_seconds_of`` for the module at ``path`` — a file named for
    uade's Delta Music 2.0 player (``uade_formats.classify``: ``.dm2``,
    ``dm2.``, ``dlm2.``) — and its tune ``tune`` in uade's numbering; a Delta
    Music 2.0 module holds one, tune 0, so any other tune is None.
    Remembered for the file's size and time.  An XPK-SQSH or PowerPacker
    packed module is read unpacked (``xpk.unpack_file``).  None for anything
    else (any other name is never looked at on disk) and for a file that
    can't be read."""
    from soniqboom.core import uade_formats, xpk
    p = Path(path)
    if tune or (uade_formats.classify(p.name) or ("",))[0] != "DeltaMusic2.0":
        return None
    try:
        st = p.stat()
        key = (str(p.absolute()), st.st_size, st.st_mtime_ns)
    except OSError:
        return None
    if key in _CACHE:
        return _CACHE[key]
    secs = None
    if st.st_size <= _MAX_READ:
        try:
            with open(p, "rb") as fh:
                head = fh.read(_TRACKS_AT)
                if is_dm2(head):
                    secs = song_seconds_of(head + fh.read(_MAX_READ))
            if secs is None and xpk.xpk_method(head) is not None:
                try:
                    secs = song_seconds_of(xpk.unpack_file(p))  # a packed module
                except xpk.XpkError:
                    secs = None
        except OSError:
            return None
    while len(_CACHE) >= _CACHE_MAX:                    # (oldest first, never all)
        try:
            _CACHE.pop(next(iter(_CACHE)), None)
        except (StopIteration, RuntimeError):           # (another thread evicting too)
            break
    _CACHE[key] = secs
    return secs
