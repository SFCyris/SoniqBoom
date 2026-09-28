# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""AIFF metadata: the ID3 tag is an ``ID3 `` chunk inside the IFF container,
not a tag at the head of the file.

* ``extract`` reads it through ``mutagen.aiff.AIFF`` with the same ID3 field
  mapping as MP3 (``metadata._id3_fields``: text frames, TXXX GAME, cover,
  ReplayGain) and the stream info from the COMM chunk — it used to read the
  file as an MP3, which raised, so every AIFF was indexed as its filename
  with no tags and duration 0;
* a file with no ID3 tag (an untagged AIFF, an MP3 without ID3v2) keeps its
  stream info, and so does an AIFF whose ID3 chunk mutagen rejects;
* the cover is an ``APIC`` frame (front cover first), never a ``GEOB``;
* a network-share scan reads an AIFF whole: its tag chunk sits after the
  audio, so a front read finds none;
* lyrics are read from / written into the chunk — the writer used to prepend
  an ID3v2 tag before ``FORM``, leaving an unreadable file;
* "Read game names" re-reads an AIFF's GAME tag.

The AIFFs are generated with ffmpeg (skipped without it)."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from soniqboom.core import metadata, repair
from soniqboom.core.store import TrackStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _ffmpeg(path, *args, secs=2, optional=False):
    """Generate ``path``; a failure fails the test unless the encoder is an
    ``optional`` one some ffmpeg builds lack (libmp3lame)."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", f"sine=f=440:d={secs}", *args, str(path)], capture_output=True)
    if r.returncode:
        msg = f"ffmpeg can't write {path.name}: {r.stderr.decode()[-300:]}"
        if optional:
            pytest.skip(msg)
        pytest.fail(msg)


def _tagged_aiff(path, *extra, secs=2):
    _ffmpeg(path, *extra, "-metadata", "title=RealTitle", "-metadata", "artist=RealArtist",
            "-metadata", "album=RealAlbum", "-metadata", "track=3/12",
            "-metadata", "game=Uridium 2", "-write_id3v2", "1", secs=secs)


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    return s


@pytest.mark.parametrize("name,extra,sr,ch,bits", [
    ("t.aiff", (), 44100, 1, 16),
    ("t.aif", (), 44100, 1, 16),
    ("s24.aiff", ("-ac", "2", "-ar", "48000", "-c:a", "pcm_s24be"), 48000, 2, 24),
    ("c.aif", ("-c:a", "pcm_s16le"), 44100, 1, 16),          # AIFF-C ('sowt')
])
def test_aiff_id3_chunk_is_read(tmp_path, name, extra, sr, ch, bits):
    f = tmp_path / name
    _tagged_aiff(f, *extra)
    m = metadata.extract(f, "x")
    assert (m.title, m.artist, m.album, m.game) == (
        "RealTitle", "RealArtist", "RealAlbum", "Uridium 2")   # ffmpeg writes TXXX:game
    assert (m.track_number, m.total_tracks) == (3, 12)
    assert m.duration == pytest.approx(2.0, abs=0.01)
    assert (m.sample_rate, m.channels, m.bit_depth) == (sr, ch, bits)
    assert m.bitrate == sr * ch * bits
    assert (m.format, m.is_lossless) == ("AIFF", True)


def test_aiff_game_frame_as_the_tag_editor_spells_it(tmp_path):
    from mutagen.aiff import AIFF
    from mutagen.id3 import TXXX
    f = tmp_path / "t.aiff"
    _ffmpeg(f)
    a = AIFF(str(f))
    a.add_tags()
    a.tags.add(TXXX(encoding=3, desc="GAME", text=["  Turrican  "]))
    a.save()
    assert metadata.extract(f, "x").game == "Turrican"


def test_untagged_aiff_keeps_its_stream_info(tmp_path):
    f = tmp_path / "Plain Song.aiff"
    _ffmpeg(f)
    m = metadata.extract(f, "x")
    assert (m.title, m.artist, m.game) == ("Plain Song", "", "")
    assert m.duration == pytest.approx(2.0, abs=0.01)
    assert (m.sample_rate, m.channels, m.bit_depth, m.format) == (44100, 1, 16, "AIFF")


def test_a_rejected_id3_chunk_keeps_the_stream_info(tmp_path):
    """mutagen fails the whole AIFF load on an ID3 chunk it can't parse (here
    an ID3v2.5 header) — the COMM stream info is still read."""
    from mutagen.aiff import AIFF, AIFFFile
    f = tmp_path / "Bad Tag.aiff"
    _tagged_aiff(f)
    b = bytearray(f.read_bytes())
    with open(f, "rb") as fh:
        off = next(c.offset for c in AIFFFile(fh).root.subchunks() if c.id == "ID3")
    assert b[off + 8:off + 11] == b"ID3"
    b[off + 11] = 5                                            # major version 5
    f.write_bytes(bytes(b))
    with pytest.raises(Exception):
        AIFF(str(f))
    m = metadata.extract(f, "x")
    assert (m.title, m.format, m.sample_rate) == ("Bad Tag", "AIFF", 44100)
    assert m.duration == pytest.approx(2.0, abs=0.01)


def test_untagged_mp3_keeps_its_stream_info(tmp_path):
    """No ID3v2 tag: ``MP3.tags`` is None — the shared mapping reads it as an
    empty tag (it used to raise and drop the duration too)."""
    f = tmp_path / "Plain Song.mp3"
    _ffmpeg(f, "-c:a", "libmp3lame", "-id3v2_version", "0", "-write_id3v1", "0",
            optional=True)
    from mutagen.mp3 import MP3
    assert MP3(str(f)).tags is None
    m = metadata.extract(f, "x")
    assert (m.title, m.format, m.sample_rate) == ("Plain Song", "MP3", 44100)
    assert m.duration == pytest.approx(2.0, abs=0.1)


def _add_frames(f, *frames):
    from mutagen.aiff import AIFF
    from mutagen.id3 import ID3
    if f.suffix == ".mp3":
        tags = ID3(str(f))
        for fr in frames:
            tags.add(fr)
        tags.save(str(f))
        return
    a = AIFF(str(f))
    for fr in frames:
        a.tags.add(fr)
    a.save()


@pytest.mark.parametrize("ext", [".aiff", ".mp3"])
def test_the_cover_is_the_front_apic_never_a_geob(tmp_path, ext):
    from mutagen.id3 import APIC, GEOB
    from soniqboom.api.art import _extract_cover
    f = tmp_path / f"t{ext}"
    if ext == ".mp3":
        _ffmpeg(f, "-c:a", "libmp3lame", "-metadata", "title=T", optional=True)
    else:
        _tagged_aiff(f)
    back = b"\xff\xd8\xff" + b"\x01" * 32
    _add_frames(f,
                GEOB(encoding=3, mime="application/octet-stream", filename="",
                     desc="Serato Autotags", data=b"\x01\x01120.00\x00"),
                APIC(encoding=3, mime="image/jpeg", type=4, desc="back", data=back),
                APIC(encoding=3, mime="image/png", type=3, desc="", data=PNG))
    m = metadata.extract(f, "x")
    assert m.cover_art and m.cover_art.startswith("data:image/png;base64,")
    assert _extract_cover(f) == (PNG, "image/png")


def test_aiff_only_picture_is_the_cover(tmp_path):
    from mutagen.id3 import APIC
    from soniqboom.api.art import _extract_cover
    f = tmp_path / "t.aiff"
    _tagged_aiff(f)
    _add_frames(f, APIC(encoding=3, mime="image/png", type=0, desc="", data=PNG))
    assert metadata.extract(f, "x").cover_art.startswith("data:image/png;base64,")
    assert _extract_cover(f) == (PNG, "image/png")


def test_a_network_share_scan_reads_an_aiff_whole(tmp_path):
    """The tag chunk follows the audio: a front read parses (COMM gives the
    duration, so the scan's "looks incomplete" check passes) but has no tag."""
    from soniqboom.core import scanner
    f = tmp_path / "big.aiff"
    _tagged_aiff(f, "-ac", "2", secs=30)                       # ~5 MB
    data = f.read_bytes()
    assert metadata.HEADER_BUDGET[".aiff"] is None and metadata.HEADER_BUDGET[".aif"] is None
    _p, front, *_ = scanner._extract_one_remote(data[:2 * 1024 * 1024], "ftp://h/s/big.aiff", "r")
    assert (front.title, front.game) == ("big", "")
    assert front.duration == pytest.approx(30.0, abs=0.01)
    _p, whole, *_ = scanner._extract_one_remote(data, "ftp://h/s/big.aiff", "r")
    assert (whole.title, whole.artist, whole.game) == ("RealTitle", "RealArtist", "Uridium 2")


def test_aiff_lyrics_written_into_the_chunk(tmp_path):
    from mutagen.aiff import AIFF
    f = tmp_path / "t.aiff"
    _tagged_aiff(f)
    assert metadata.extract_lyrics(f) is None
    assert metadata.write_lyrics(f, "la la la") is True
    assert f.read_bytes()[:4] == b"FORM"                       # still an IFF file
    assert AIFF(str(f)).tags.getall("USLT")[0].text == "la la la"
    assert metadata.extract_lyrics(f) == "la la la"
    m = metadata.extract(f, "x")
    assert (m.title, m.game) == ("RealTitle", "Uridium 2") and m.duration > 1.9
    assert metadata.write_lyrics(f, "other") is False          # never overwrites
    assert metadata.extract_lyrics(f) == "la la la"


def test_untagged_aiff_gets_a_lyrics_chunk(tmp_path):
    f = tmp_path / "t.aiff"
    _ffmpeg(f)
    assert metadata.write_lyrics(f, "words") is True
    assert f.read_bytes()[:4] == b"FORM"
    assert metadata.extract_lyrics(f) == "words"
    assert metadata.extract(f, "x").duration == pytest.approx(2.0, abs=0.01)


def _stale(tid, path):
    """An AIFF as the old extractor indexed it: its filename, duration 0."""
    return {"id": tid, "path": str(path), "title": "t", "artist": "", "album": "",
            "format": "AIFF", "genre": [], "duration": 0.0, "file_size": 10}


async def test_read_game_names_reads_a_local_aiff(tmp_path, store, monkeypatch):
    f = tmp_path / "t.aiff"
    _tagged_aiff(f)
    store.upsert_tracks_batch([_stale("a", f)])
    assert [t["id"] for t in await repair.find_game_tag_candidates()] == ["a"]
    monkeypatch.setattr(repair, "_game_only_ids", frozenset({"a"}))
    ok, applied, err = await repair._process_local(store.get_track("a"))
    a = store.get_track("a")
    assert (ok, applied, err) == (True, True, None)
    assert (a["game"], a.get("game_source")) == ("Uridium 2", None)
    assert (a["title"], a["duration"]) == ("t", 0.0)           # only the game


class _WholeSource:
    """A FileSource stand-in with range reads over a local file."""

    def __init__(self, path):
        self.data = path.read_bytes()
        self.full = 0

    def read_partial(self, path, n, *, lane="scan"):
        return self.data[:n]

    def read_at(self, path, off, n, *, lane="scan"):
        return self.data[off:off + n]

    def read_file(self, path, *, lane="scan"):
        self.full += 1
        return self.data


async def test_read_game_names_reads_a_share_aiff_whole(tmp_path, store, monkeypatch):
    """No tag window for AIFF: the file is read whole, and still only its
    game is written."""
    from soniqboom.core import filesource
    f = tmp_path / "t.aiff"
    _tagged_aiff(f)
    src = _WholeSource(f)
    monkeypatch.setattr(filesource, "get_source", lambda root: src)
    store.upsert_tracks_batch([_stale("r", "ftp://h/s:/a/t.aiff")])
    monkeypatch.setattr(repair, "_game_only_ids", frozenset({"r"}))
    ok, applied, err = await repair._process_remote(store.get_track("r"), None)
    r = store.get_track("r")
    assert (ok, applied, err, src.full) == (True, True, None, 1)
    assert (r["game"], r["title"]) == ("Uridium 2", "t")
